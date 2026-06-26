"""
update_channel.py
==================
Update Channel System

Flow:
  - /update_channel [channel_id]          → Update channel add karo
  - /update_channel_list                  → Saare update channels dekho (with IDs)
  - /delete_update_channel [channel_id]   → Update channel remove karo
  - /update_post                          → Anime ka pura update-post entry save karo
                                            (5-step: anime name → invite link → audio →
                                             genres → image)
  - /update_post_button                   → Default "Kaise Dekhein" / "Join Backup"
                                            button links set karo (sab posts pe lagte hain)
  - /update_post_list                     → Saare saved anime entries dekho
  - /delete_update_post [anime_name]      → Kisi anime ka saved post entry remove karo
  - /updatechannel on|off                 → Update channel posting toggle karo
  - /latest_post_delete                   → Last sent update channel post delete karo

Auto-trigger:
  Jab bhi 360p file upload hoti hai kisi anime channel pe (auto_monitor se),
  toh SIRF wohi update channels pe post jaata hai jinka anime name
  update_post_map mein saved hai (exact ya fuzzy match) AUR jiska image+audio+genres
  bhi saved ho (5-step flow complete hua ho).

  Agar anime ka entry incomplete ho (image missing) → post NAHI jaayega.
  Agar /updatechannel off hai → post NAHI jaayega.

  Naya post format (image + caption + 3 colour buttons):

    [photo]
    ➲ Marriagetoxin (S - 01)
    ╭┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄
    ◈ Audio: Hindi ORG
    ◈ Quality: 360p, 720p, 1080p
    ◈ Genres: Action, Comedy, Romance
    ╰┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄
    ➲ Episode: 10 Added!

    Row 1: [ ⎙ ᴡᴀᴛᴄʜ & ᴅᴏᴡɴʟᴏᴀᴅ ⎙ ]   (green, url = per-anime invite_link)
    Row 2: [• ᴋᴀɪꜱᴇ ᴅᴇᴋʜᴇɪɴ •] [• ᴊᴏɪɴ ʙᴀᴄᴋᴜᴘ •]   (blue, red — global default urls)

DB storage:
  - update_channels             → col2, id='update_channels', list of {channel_id, channel_title}
  - update_post_map             → col2, id='update_post_map',
                                  dict {anime_name_lower: {display_name, invite_link,
                                                             audio, genres, image}}
  - update_post_button_defaults → col2, id='update_post_button_defaults',
                                  dict {kaise_dekhein: url, join_backup: url}
  - update_toggle               → col2, id='update_toggle', data: {enabled: True/False}
  - latest_post_ids             → col2, id='latest_post_ids', list of {channel_id, message_id}
  All stored in bot-level col2 (status collection) — user-specific nahi.

Commands registered here (conflict check):
  /update_channel          — unique
  /update_channel_list     — unique
  /delete_update_channel   — unique
  /update_post             — unique (5-step session: anime → link → audio → genres → image)
  /cancel_update_post      — unique (session cancel)
  /update_post_button      — unique (1-step session: kaise_dekhein url → join_backup url)
  /update_post_list        — unique
  /delete_update_post      — unique
  /updatechannel           — unique (on/off toggle)
  /latest_post_delete      — unique

  NOTE: text/photo handler (group=0) sirf tab fire karta hai jab
  _update_post_sessions ya _update_post_button_sessions mein us user ka
  session ho. Isliye dusre plugins ke saath koi conflict nahi.
"""

import logging
import re

from pyrogram import Client, filters, StopPropagation, ContinuePropagation
from pyrogram.types import (
    InlineKeyboardButton, InlineKeyboardMarkup, Message
)

from .. import app, owner, sudo_users
from ..utils.database.access_db import db
from ..utils.bot_upload_engine import _bot_api_send_photo

LOGGER = logging.getLogger(__name__)

# ─────────────────────────────────────────────
#  Colour styles for the 3 update-post buttons
#  (same Bot API 9.4+ "style" mechanism /bot_upload uses)
# ─────────────────────────────────────────────
_WATCH_DL_STYLE = ("success", "🟢")   # green
_KAISE_STYLE    = ("primary", "🔵")   # blue
_BACKUP_STYLE   = ("danger",  "🔴")   # red


# ─────────────────────────────────────────────
#  In-memory session for /update_post 5-step flow
#  step: 'anime' -> 'link' -> 'audio' -> 'genres' -> 'image'
# ─────────────────────────────────────────────
_update_post_sessions: dict = {}

# ─────────────────────────────────────────────
#  In-memory session for /update_post_button (set default button links)
#  step: 'kaise_dekhein' -> 'join_backup'
# ─────────────────────────────────────────────
_update_post_button_sessions: dict = {}


# ─────────────────────────────────────────────
#  Auth helper
# ─────────────────────────────────────────────
def _is_auth(user_id: int) -> bool:
    return user_id in owner or user_id in sudo_users


# ─────────────────────────────────────────────
#  Normalize helper
# ─────────────────────────────────────────────
def _norm(s: str) -> str:
    return re.sub(r'[\s\-_]+', ' ', s.lower()).strip()


# ─────────────────────────────────────────────
#  DB helpers — bot-level (col2 / status collection)
# ─────────────────────────────────────────────
async def _get_update_channels() -> list:
    """Saare registered update channels lo."""
    doc = await db.col2.find_one({'id': 'update_channels'})
    if not doc:
        return []
    return doc.get('channels', [])


async def _save_update_channels(channels: list):
    await db.col2.update_one(
        {'id': 'update_channels'},
        {'$set': {'channels': channels}},
        upsert=True,
    )


async def _get_post_map() -> dict:
    """
    anime_name (lowercase) → entry dict lo.
    Entry shape: {
        "display_name": str,
        "invite_link": str,
        "audio": str,
        "genres": str,
        "image": str,   # local file_id ya path jo Telegram pe already upload hai
    }
    Purane (string-only) entries bhi backward-compat ke liye support karte hain —
    agar value plain string hai toh usko {"invite_link": value} treat karo.
    """
    doc = await db.col2.find_one({'id': 'update_post_map'})
    if not doc:
        return {}
    raw = doc.get('map', {})
    fixed = {}
    for k, v in raw.items():
        if isinstance(v, dict):
            fixed[k] = v
        else:
            fixed[k] = {"display_name": k, "invite_link": v or ""}
    return fixed


async def _save_post_map(data: dict):
    await db.col2.update_one(
        {'id': 'update_post_map'},
        {'$set': {'map': data}},
        upsert=True,
    )


# ── Global default button links (◈ kaise dekhein / join backup) ──
async def _get_button_defaults() -> dict:
    """{"kaise_dekhein": url, "join_backup": url} lo."""
    doc = await db.col2.find_one({'id': 'update_post_button_defaults'})
    if not doc:
        return {}
    return doc.get('buttons', {})


async def _save_button_defaults(data: dict):
    await db.col2.update_one(
        {'id': 'update_post_button_defaults'},
        {'$set': {'buttons': data}},
        upsert=True,
    )


# ── Toggle helpers ──
async def _get_update_toggle() -> bool:
    """True = on (default), False = off."""
    doc = await db.col2.find_one({'id': 'update_toggle'})
    if not doc:
        return True  # default on
    return doc.get('enabled', True)


async def _set_update_toggle(enabled: bool):
    await db.col2.update_one(
        {'id': 'update_toggle'},
        {'$set': {'enabled': enabled}},
        upsert=True,
    )


# ── Latest post IDs helpers ──
async def _get_latest_post_ids() -> list:
    """Last sent messages ka list [{channel_id, message_id}]."""
    doc = await db.col2.find_one({'id': 'latest_post_ids'})
    if not doc:
        return []
    return doc.get('posts', [])


async def _save_latest_post_ids(posts: list):
    await db.col2.update_one(
        {'id': 'latest_post_ids'},
        {'$set': {'posts': posts}},
        upsert=True,
    )


# ─────────────────────────────────────────────
#  PUBLIC: 360p upload hone pe auto_monitor call karega
# ─────────────────────────────────────────────
async def send_update_post(
    client,
    anime_name: str,
    season: int | None,
    episode: int | None = None,
    episode_start: int | None = None,
    episode_end: int | None = None,
):
    """
    Update channels pe naya image-based stylish post bhejta hai.

    RULES:
      1. /updatechannel off hai toh kuch nahi hoga.
      2. Sirf wohi anime ka post jaayega jiska entry update_post_map mein hai
         (exact/fuzzy match) AUR jiska 5-step entry COMPLETE ho (image required).
         Incomplete entry (image missing) → post skip.

    Single episode:  episode=6        → ➲ Episode: 06 Added!
    Episode range:   episode_start=34, episode_end=36  → ➲ Episode: 34-36 Added!
    """
    # ── Toggle check ──
    enabled = await _get_update_toggle()
    if not enabled:
        LOGGER.info(f"[UpdateChannel] Toggle OFF hai, '{anime_name}' ka post skip kiya.")
        return

    channels = await _get_update_channels()
    if not channels:
        LOGGER.warning("[UpdateChannel] send_update_post called but no update channels saved.")
        return

    if not anime_name.strip():
        LOGGER.warning("[UpdateChannel] anime_name empty, post skip kiya.")
        return

    post_map = await _get_post_map()

    # ── Anime match check — REQUIRED ──
    query_norm = _norm(anime_name)
    entry = None
    for key, val in post_map.items():
        if _norm(key) == query_norm:
            entry = val
            break

    if not entry:
        LOGGER.info(
            f"[UpdateChannel] '{anime_name}' update_post_map mein nahi hai — post skip kiya."
        )
        return

    # ── Entry completeness check — image REQUIRED ──
    image = entry.get("image")
    if not image:
        LOGGER.info(
            f"[UpdateChannel] '{anime_name}' ka entry incomplete hai (image missing) — post skip kiya."
        )
        return

    display_name = entry.get("display_name") or anime_name
    invite_link = entry.get("invite_link") or ""
    audio = entry.get("audio") or "—"
    genres = entry.get("genres") or "—"

    # ── Episode line ──
    if episode_start and episode_end and episode_start != episode_end:
        ep_str = f"{episode_start}-{episode_end}"
    elif episode_start:
        ep_str = f"{episode_start:02d}" if episode_start < 100 else str(episode_start)
    elif episode:
        ep_str = f"{episode:02d}" if episode < 100 else str(episode)
    else:
        ep_str = "—"

    # ── Title line: ➲ Anime Name (S - 01) ──
    season_str = f"(S - {season:02d})" if season else ""
    title = f"➲ {display_name} {season_str}".strip() if season_str else f"➲ {display_name}"

    # ── Caption (box layout) ──
    box_top = "╭┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄"
    box_bottom = "╰┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄"
    ep_line = f"➲ Episode: {ep_str} Added!"
    lines = [
        title,
        box_top,
        f"◈ Audio: {audio}",
        "◈ Quality: 360p, 720p, 1080p",
        f"◈ Genres: {genres}",
        box_bottom,
        ep_line,
    ]
    caption = "\n".join(lines)

    # Whole caption bold. Title line also gets blockquote (chip-look, matches
    # reference screenshot). Episode line gets a clickable text_link instead of
    # blockquote — blockquote + text_link can't safely overlap on the same range,
    # so the episode line trades the chip-look for being tap-to-open.
    title_len = len(title)
    ep_offset = len(caption) - len(ep_line)
    caption_entities = [
        {"type": "bold", "offset": 0, "length": len(caption)},
        {"type": "blockquote", "offset": 0, "length": title_len},
    ]
    if invite_link:
        caption_entities.append({
            "type": "text_link", "offset": ep_offset, "length": len(ep_line),
            "url": invite_link,
        })

    # ── Buttons ──
    button_defaults = await _get_button_defaults()
    kaise_url = button_defaults.get("kaise_dekhein")
    backup_url = button_defaults.get("join_backup")

    row1 = []
    if invite_link:
        row1.append({
            "text": "⎙ ᴡᴀᴛᴄʜ & ᴅᴏᴡɴʟᴏᴀᴅ ⎙",
            "url": invite_link,
            "style": _WATCH_DL_STYLE[0],
        })

    row2 = []
    if kaise_url:
        row2.append({
            "text": "• ᴋᴀɪꜱᴇ ᴅᴇᴋʜᴇɪɴ •",
            "url": kaise_url,
            "style": _KAISE_STYLE[0],
        })
    if backup_url:
        row2.append({
            "text": "• ᴊᴏɪɴ ʙᴀᴄᴋᴜᴘ •",
            "url": backup_url,
            "style": _BACKUP_STYLE[0],
        })

    keyboard_rows = [r for r in [row1, row2] if r]
    reply_markup = {"inline_keyboard": keyboard_rows} if keyboard_rows else {"inline_keyboard": []}

    # Pyrogram fallback markup (plain, no colour — used only if Bot API call fails)
    pyrogram_rows = []
    if row1:
        pyrogram_rows.append([InlineKeyboardButton(b["text"], url=b["url"]) for b in row1])
    if row2:
        pyrogram_rows.append([InlineKeyboardButton(b["text"], url=b["url"]) for b in row2])
    pyrogram_markup = InlineKeyboardMarkup(pyrogram_rows) if pyrogram_rows else None

    # ── Send and store latest post IDs ──
    new_latest = []
    for ch in channels:
        ch_id = ch.get("channel_id")
        if not ch_id:
            continue
        msg_id = None
        try:
            msg_id = await _bot_api_send_photo(ch_id, image, caption, caption_entities, reply_markup)
        except Exception as e:
            LOGGER.warning(f"[UpdateChannel] Bot API sendPhoto error for {ch_id}: {e}")

        if not msg_id:
            # Pyrogram fallback — no custom colours, but post still goes out
            try:
                sent = await client.send_photo(
                    chat_id=ch_id,
                    photo=image,
                    caption=caption,
                    reply_markup=pyrogram_markup,
                )
                msg_id = sent.id
            except Exception as e:
                LOGGER.error(f"[UpdateChannel] Failed to send to {ch_id}: {e}")
                continue

        new_latest.append({"channel_id": ch_id, "message_id": msg_id})
        LOGGER.info(
            f"[UpdateChannel] Post sent to {ch_id} for '{display_name}' Ep {ep_str} (msg_id={msg_id})"
        )

    # Save latest post IDs (overwrite with fresh batch)
    if new_latest:
        await _save_latest_post_ids(new_latest)


# ─────────────────────────────────────────────
#  /updatechannel on|off — Toggle
# ─────────────────────────────────────────────
@Client.on_message(filters.command("updatechannel") & filters.private)
async def cmd_updatechannel_toggle(client: Client, message: Message):
    """Update channel posting on/off karo."""
    if not _is_auth(message.from_user.id):
        return

    parts = message.text.split(None, 1)
    if len(parts) < 2 or parts[1].strip().lower() not in ("on", "off"):
        current = await _get_update_toggle()
        status = "🟢 ON" if current else "🔴 OFF"
        await message.reply(
            f"📢 **Update Channel Posting**\n\n"
            f"Current Status: **{status}**\n\n"
            f"Toggle karne ke liye:\n"
            f"• `/updatechannel on` — posting enable karo\n"
            f"• `/updatechannel off` — posting band karo"
        )
        return

    action = parts[1].strip().lower()
    enabled = (action == "on")
    await _set_update_toggle(enabled)

    if enabled:
        await message.reply(
            "✅ **Update Channel Posting: ON**\n\n"
            "Ab 360p uploads pe update channel pe post jaayega\n"
            "_(sirf registered anime ke liye)_"
        )
    else:
        await message.reply(
            "🔴 **Update Channel Posting: OFF**\n\n"
            "Ab koi bhi post update channel pe nahi jaayega\n"
            "jab tak `/updatechannel on` na karo."
        )


# ─────────────────────────────────────────────
#  /latest_post_delete — Last update post delete karo
# ─────────────────────────────────────────────
@Client.on_message(filters.command("latest_post_delete") & filters.private)
async def cmd_latest_post_delete(client: Client, message: Message):
    """Update channel pe last bheja gaya post delete karo."""
    if not _is_auth(message.from_user.id):
        return

    posts = await _get_latest_post_ids()
    if not posts:
        await message.reply(
            "📭 Koi latest post nahi mila delete karne ke liye.\n\n"
            "Pehle koi update post bhejo."
        )
        return

    deleted = []
    failed = []
    for entry in posts:
        ch_id = entry.get("channel_id")
        msg_id = entry.get("message_id")
        if not ch_id or not msg_id:
            continue
        try:
            await client.delete_messages(chat_id=ch_id, message_ids=msg_id)
            deleted.append(f"Channel `{ch_id}` → Msg `{msg_id}`")
            LOGGER.info(f"[UpdateChannel] Deleted latest post: channel={ch_id}, msg={msg_id}")
        except Exception as e:
            failed.append(f"Channel `{ch_id}` → ❌ `{e}`")
            LOGGER.error(f"[UpdateChannel] Delete failed: channel={ch_id}, msg={msg_id}, err={e}")

    # Clear saved IDs after delete attempt
    await _save_latest_post_ids([])

    if deleted:
        del_text = "\n".join(deleted)
        text = f"🗑️ **Latest Post Deleted!**\n\n{del_text}"
        if failed:
            fail_text = "\n".join(failed)
            text += f"\n\n⚠️ **Failed:**\n{fail_text}"
        await message.reply(text)
    else:
        fail_text = "\n".join(failed) if failed else "Unknown error"
        await message.reply(f"❌ Delete nahi ho paya:\n\n{fail_text}")


# ─────────────────────────────────────────────
#  /update_channel [channel_id]
# ─────────────────────────────────────────────
@Client.on_message(filters.command("update_channel") & filters.private)
async def cmd_update_channel(client: Client, message: Message):
    """Update channel add karo."""
    if not _is_auth(message.from_user.id):
        return

    parts = message.text.split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply(
            "**Usage:** `/update_channel [channel_id]`\n\n"
            "**Example:** `/update_channel -1001234567890`"
        )
        return

    try:
        channel_id = int(parts[1].strip())
    except ValueError:
        await message.reply("❌ Valid channel ID chahiye. Format: `-100xxxxxxxxx`")
        return

    channels = await _get_update_channels()
    for ch in channels:
        if ch.get("channel_id") == channel_id:
            await message.reply(
                f"⚠️ Yeh channel already update list mein hai!\n\n`{channel_id}`"
            )
            return

    try:
        chat = await client.get_chat(channel_id)
        channel_title = chat.title
    except Exception as e:
        await message.reply(
            f"❌ Channel nahi mila: `{e}`\n\nBot ko channel mein admin banao pehle."
        )
        return

    try:
        bot_me = await client.get_me()
        member = await client.get_chat_member(channel_id, bot_me.id)
        if member.status.name not in ["ADMINISTRATOR", "OWNER"]:
            await message.reply(f"❌ Bot `{channel_title}` mein admin nahi hai!")
            return
    except Exception as e:
        await message.reply(f"❌ Admin check fail: `{e}`")
        return

    channels.append({"channel_id": channel_id, "channel_title": channel_title})
    await _save_update_channels(channels)

    await message.reply(
        f"✅ **Update Channel Added!**\n\n"
        f"📢 **{channel_title}**\n"
        f"🆔 `{channel_id}`\n\n"
        f"Ab jab bhi registered anime ka 360p upload hoga, yahan post aayega."
    )


# ─────────────────────────────────────────────
#  /update_channel_list
# ─────────────────────────────────────────────
@Client.on_message(filters.command("update_channel_list") & filters.private)
async def cmd_update_channel_list(client: Client, message: Message):
    """Saare update channels list karo."""
    if not _is_auth(message.from_user.id):
        return

    channels = await _get_update_channels()
    toggle = await _get_update_toggle()
    status = "🟢 ON" if toggle else "🔴 OFF"

    if not channels:
        await message.reply(
            f"📭 Koi update channel add nahi hai.\n\n"
            f"Posting Status: **{status}**\n\n"
            f"Add karne ke liye: `/update_channel [channel_id]`"
        )
        return

    text = f"📢 **Update Channels ({len(channels)})** | Posting: **{status}**\n\n"
    for i, ch in enumerate(channels, 1):
        text += f"`{i}.` **{ch.get('channel_title', 'Unknown')}**\n"
        text += f"    🆔 `{ch.get('channel_id')}`\n\n"

    text += "💡 Remove: `/delete_update_channel [channel_id]`\n"
    text += "💡 Toggle: `/updatechannel on` | `/updatechannel off`"
    await message.reply(text)


# ─────────────────────────────────────────────
#  /delete_update_channel [channel_id]
# ─────────────────────────────────────────────
@Client.on_message(filters.command("delete_update_channel") & filters.private)
async def cmd_delete_update_channel(client: Client, message: Message):
    """Update channel remove karo."""
    if not _is_auth(message.from_user.id):
        return

    parts = message.text.split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply(
            "**Usage:** `/delete_update_channel [channel_id]`\n\n"
            "**Example:** `/delete_update_channel -1001234567890`"
        )
        return

    try:
        channel_id = int(parts[1].strip())
    except ValueError:
        await message.reply("❌ Valid channel ID chahiye. Format: `-100xxxxxxxxx`")
        return

    channels = await _get_update_channels()
    new_channels = [ch for ch in channels if ch.get("channel_id") != channel_id]

    if len(new_channels) == len(channels):
        await message.reply(f"❌ Channel `{channel_id}` update list mein nahi mila.")
        return

    await _save_update_channels(new_channels)
    await message.reply(
        f"🗑️ **Removed!** Channel `{channel_id}` update list se hata diya."
    )


# ─────────────────────────────────────────────
#  /update_post — 2-step: anime name → invite link
# ─────────────────────────────────────────────
@Client.on_message(filters.command("update_post") & filters.private)
async def cmd_update_post(client: Client, message: Message):
    """Anime ka pura update-post entry save karo (5-step)."""
    if not _is_auth(message.from_user.id):
        return

    user_id = message.from_user.id
    _update_post_sessions[user_id] = {"step": "anime"}

    await message.reply(
        "**Step 1/5 — Anime ka naam do:**\n\n"
        "**Example:** `Witch Hat Atelier`\n\n"
        "_Cancel karna ho toh `/cancel_update_post` bhejo._"
    )


@Client.on_message(filters.command("cancel_update_post") & filters.private)
async def cmd_cancel_update_post(client: Client, message: Message):
    user_id = message.from_user.id
    _update_post_sessions.pop(user_id, None)
    _update_post_button_sessions.pop(user_id, None)
    await message.reply("❌ Cancelled.")


# ─────────────────────────────────────────────
#  /update_post_button — default Kaise Dekhein / Join Backup links set karo
#  Ye ek baar set karo, sab future update posts pe automatically lagega.
#  Dobara is command se reset/change kiya ja sakta hai.
# ─────────────────────────────────────────────
@Client.on_message(filters.command("update_post_button") & filters.private)
async def cmd_update_post_button(client: Client, message: Message):
    """Kaise Dekhein + Join Backup ke default button links set karo."""
    if not _is_auth(message.from_user.id):
        return

    user_id = message.from_user.id
    _update_post_button_sessions[user_id] = {"step": "kaise_dekhein"}

    await message.reply(
        "**Step 1/2 — • ᴋᴀɪꜱᴇ ᴅᴇᴋʜᴇɪɴ • ka link do:**\n\n"
        "**Example:** `https://t.me/+xxxxxxxxxx`\n\n"
        "_Cancel karna ho toh `/cancel_update_post` bhejo._"
    )


# ─────────────────────────────────────────────
#  /update_post_list
# ─────────────────────────────────────────────
@Client.on_message(filters.command("update_post_list") & filters.private)
async def cmd_update_post_list(client: Client, message: Message):
    """Saare saved anime entries dikho."""
    if not _is_auth(message.from_user.id):
        return

    post_map = await _get_post_map()
    if not post_map:
        await message.reply(
            "📭 Koi anime post entry save nahi hai.\n\n"
            "Add karne ke liye: `/update_post`\n\n"
            "⚠️ **Note:** Sirf yahan registered anime ka hi update channel pe post jaayega!"
        )
        return

    text = f"📋 **Saved Anime Posts ({len(post_map)})**\n"
    text += "_Sirf complete entries (image saved) ka hi post update channel pe jaayega_\n\n"
    for i, (key, entry) in enumerate(post_map.items(), 1):
        name = entry.get("display_name") or key
        link = entry.get("invite_link") or ""
        link_display = f"[link]({link})" if link else "_(no link)_"
        audio = entry.get("audio") or "_(not set)_"
        genres = entry.get("genres") or "_(not set)_"
        complete = "✅" if entry.get("image") else "⚠️ incomplete (no image)"
        text += (
            f"`{i}.` **{name}** — {complete}\n"
            f"    🔗 {link_display}\n"
            f"    🎙 {audio}\n"
            f"    🎭 {genres}\n\n"
        )

    text += "🗑️ Remove: `/delete_update_post [anime name]`"
    await message.reply(text, disable_web_page_preview=True)


# ─────────────────────────────────────────────
#  /delete_update_post [anime_name]
# ─────────────────────────────────────────────
@Client.on_message(filters.command("delete_update_post") & filters.private)
async def cmd_delete_update_post(client: Client, message: Message):
    """Kisi anime ka saved post entry remove karo."""
    if not _is_auth(message.from_user.id):
        return

    parts = message.text.split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        post_map = await _get_post_map()
        if not post_map:
            await message.reply(
                "📭 Koi anime post entry save nahi hai.\n\n"
                "Add karne ke liye: `/update_post`"
            )
            return
        text = "🗑️ **Konsa delete karna hai?**\n\n"
        for i, anime in enumerate(post_map.keys(), 1):
            text += f"`{i}.` {anime}\n"
        text += "\n**Usage:** `/delete_update_post [anime name]`\n"
        text += "**Example:** `/delete_update_post witch hat atelier`"
        await message.reply(text)
        return

    query = parts[1].strip().lower()

    post_map = await _get_post_map()
    if not post_map:
        await message.reply("📭 Koi anime post entry save nahi hai.")
        return

    matched_key = None
    for key in post_map:
        if _norm(key) == _norm(query):
            matched_key = key
            break

    if not matched_key:
        await message.reply(
            f"❌ `{query}` naam se koi entry nahi mili.\n\n"
            f"Sahi naam dekhne ke liye: `/update_post_list`"
        )
        return

    del post_map[matched_key]
    await _save_post_map(post_map)

    await message.reply(
        f"✅ **Deleted!**\n\n"
        f"📺 `{matched_key}` remove ho gaya update post list se.\n\n"
        f"Ab is anime ka koi post update channel pe nahi jaayega."
    )


# ─────────────────────────────────────────────
#  Text input handler for /update_post 5-step flow +
#  /update_post_button 2-step flow.
#  group=0 — sabse pehle fire hoga, kisi se conflict nahi.
#  Agar session nahi → ContinuePropagation (agle handlers ko jaane do)
#  Agar session hai → process karo, StopPropagation (koi aur na pakde)
# ─────────────────────────────────────────────
@Client.on_message(filters.text & filters.private, group=0)
async def update_post_text_input(client: Client, message: Message):
    """update_post ke 5-step aur update_post_button ke 2-step input ko handle karo."""
    user_id = message.from_user.id

    # Auth check — authorized nahi toh agle handlers ko jaane do
    if not _is_auth(user_id):
        raise ContinuePropagation

    text = message.text.strip()

    # ── /update_post_button session ──
    btn_session = _update_post_button_sessions.get(user_id)
    if btn_session:
        if text.lower() in ["/cancel_update_post", "cancel"]:
            _update_post_button_sessions.pop(user_id, None)
            await message.reply("❌ Cancelled.")
            raise StopPropagation

        if text.startswith("/"):
            _update_post_button_sessions.pop(user_id, None)
            raise ContinuePropagation

        if btn_session.get("step") == "kaise_dekhein":
            if not text.startswith("http"):
                await message.reply("⚠️ Valid link do (`https://...`).")
                raise StopPropagation
            _update_post_button_sessions[user_id] = {
                "step": "join_backup", "kaise_dekhein": text
            }
            await message.reply(
                f"✅ • ᴋᴀɪꜱᴇ ᴅᴇᴋʜᴇɪɴ •: `{text}`\n\n"
                f"**Step 2/2 — • ᴊᴏɪɴ ʙᴀᴄᴋᴜᴘ • ka link do:**\n\n"
                f"**Example:** `https://t.me/+xxxxxxxxxx`"
            )
            raise StopPropagation

        if btn_session.get("step") == "join_backup":
            if not text.startswith("http"):
                await message.reply("⚠️ Valid link do (`https://...`).")
                raise StopPropagation
            kaise_url = btn_session["kaise_dekhein"]
            _update_post_button_sessions.pop(user_id, None)

            await _save_button_defaults({
                "kaise_dekhein": kaise_url,
                "join_backup": text,
            })
            await message.reply(
                "✅ **Saved!**\n\n"
                f"• ᴋᴀɪꜱᴇ ᴅᴇᴋʜᴇɪɴ •: `{kaise_url}`\n"
                f"• ᴊᴏɪɴ ʙᴀᴄᴋᴜᴘ •: `{text}`\n\n"
                "Ab se yeh dono buttons sabhi update posts pe automatically lagenge.\n"
                "Change karna ho toh dobara `/update_post_button` karo."
            )
            raise StopPropagation

    # ── /update_post session ──
    session = _update_post_sessions.get(user_id)
    if not session:
        raise ContinuePropagation

    # Session hai — ab sirf hum handle karenge, koi aur nahi
    if text.lower() in ["/cancel_update_post", "cancel"]:
        _update_post_sessions.pop(user_id, None)
        await message.reply("❌ Cancelled.")
        raise StopPropagation

    if text.startswith("/"):
        _update_post_sessions.pop(user_id, None)
        raise ContinuePropagation

    # ── Step 1: Anime name ──
    if session.get("step") == "anime":
        _update_post_sessions[user_id] = {"step": "link", "anime": text}
        await message.reply(
            f"✅ Anime: **{text}**\n\n"
            f"**Step 2/5 — Invite link do:**\n\n"
            f"**Example:** `https://t.me/+xxxxxxxxxx`\n\n"
            f"_Link nahi dena toh `skip` likho — post button ke bina aayega._"
        )
        raise StopPropagation

    # ── Step 2: Invite link ──
    if session.get("step") == "link":
        if text.lower() == "skip":
            invite_link = ""
        elif not text.startswith("http"):
            await message.reply(
                "⚠️ Valid invite link do (`https://t.me/...`) ya `skip` likho."
            )
            raise StopPropagation
        else:
            invite_link = text

        session["invite_link"] = invite_link
        session["step"] = "audio"
        _update_post_sessions[user_id] = session

        await message.reply(
            f"✅ Link: {'`' + invite_link + '`' if invite_link else '_(skip kiya)_'}\n\n"
            f"**Step 3/5 — Audio kya hai?**\n\n"
            f"**Example:** `Hindi ORG`"
        )
        raise StopPropagation

    # ── Step 3: Audio ──
    if session.get("step") == "audio":
        session["audio"] = text
        session["step"] = "genres"
        _update_post_sessions[user_id] = session

        await message.reply(
            f"✅ Audio: **{text}**\n\n"
            f"**Step 4/5 — Genres do:**\n\n"
            f"**Example:** `Action, Comedy, Romance`"
        )
        raise StopPropagation

    # ── Step 4: Genres ──
    if session.get("step") == "genres":
        session["genres"] = text
        session["step"] = "image"
        _update_post_sessions[user_id] = session

        await message.reply(
            f"✅ Genres: **{text}**\n\n"
            f"**Step 5/5 — Ab image bhejo** (poster/banner jo update post pe lagegi):\n\n"
            f"_Photo bhejo (caption ki zaroorat nahi)._"
        )
        raise StopPropagation

    # ── Step 5 (image) is handled by the dedicated photo handler below.
    #     Agar yahan text aaya iska matlab user ne image ke jagah text bheja.
    if session.get("step") == "image":
        await message.reply(
            "⚠️ Image bhejo (photo), text nahi.\n\n"
            "_Cancel karna ho toh `/cancel_update_post` bhejo._"
        )
        raise StopPropagation

    raise ContinuePropagation


# ─────────────────────────────────────────────
#  Photo handler for /update_post Step 5/5 — final image save
#  group=0 — sirf tab fire karta hai jab session step == 'image'
# ─────────────────────────────────────────────
@Client.on_message(filters.photo & filters.private, group=0)
async def update_post_photo_input(client: Client, message: Message):
    """update_post ke Step 5/5 (image) ko handle karo."""
    user_id = message.from_user.id

    if not _is_auth(user_id):
        raise ContinuePropagation

    session = _update_post_sessions.get(user_id)
    if not session or session.get("step") != "image":
        raise ContinuePropagation

    anime_name = session["anime"]
    invite_link = session.get("invite_link", "")
    audio = session.get("audio", "")
    genres = session.get("genres", "")
    file_id = message.photo.file_id

    _update_post_sessions.pop(user_id, None)

    post_map = await _get_post_map()
    post_map[anime_name.lower().strip()] = {
        "display_name": anime_name,
        "invite_link": invite_link,
        "audio": audio,
        "genres": genres,
        "image": file_id,
    }
    await _save_post_map(post_map)

    link_line = f"🔗 Link: `{invite_link}`\n" if invite_link else "🔗 Link: _(none)_\n"
    await message.reply(
        f"✅ **Saved!**\n\n"
        f"📺 Anime: **{anime_name}**\n"
        f"{link_line}"
        f"🎙 Audio: **{audio}**\n"
        f"🎭 Genres: **{genres}**\n"
        f"🖼 Image: ✅ saved\n\n"
        f"Ab jab bhi `{anime_name}` ka 360p upload hoga, "
        f"update channel pe full styled post aayega."
    )
    raise StopPropagation
