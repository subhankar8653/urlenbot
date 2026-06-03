"""
update_channel.py
==================
Update Channel System

Flow:
  - /update_channel [channel_id]          → Update channel add karo
  - /update_channel_list                  → Saare update channels dekho (with IDs)
  - /delete_update_channel [channel_id]   → Update channel remove karo
  - /update_post                          → Anime + invite link ka pair save karo
                                            (2-step: anime name → invite link)
  - /update_post_list                     → Saare saved anime → invite link pairs dekho
  - /delete_update_post [anime_name]      → Kisi anime ka saved post entry remove karo
  - /updatechannel on|off                 → Update channel posting toggle karo
  - /latest_post_delete                   → Last sent update channel post delete karo

Auto-trigger:
  Jab bhi 360p file upload hoti hai kisi anime channel pe (auto_monitor se),
  toh SIRF wohi update channels pe post jaata hai jinka anime name
  update_post_map mein saved hai (exact ya fuzzy match).

  Agar anime ka koi entry update_post_map mein nahi → post NAHI jaayega.
  Agar /updatechannel off hai → post NAHI jaayega.

    🔰 Witch Hat Atelier (S01)
    ──────────────────────────
    ⚡EP - 06 | Added
    [❇️ Start the Bot Get Link Here ❇️]   ← button (agar invite link saved hai)
    Start the Bot Get Link Here           ← plain text link (agar invite link saved hai)

DB storage:
  - update_channels  → col2, id='update_channels', data: list of {channel_id, channel_title}
  - update_post_map  → col2, id='update_post_map', data: dict {anime_name_lower: invite_link}
  - update_toggle    → col2, id='update_toggle', data: {enabled: True/False}
  - latest_post_ids  → col2, id='latest_post_ids', data: list of {channel_id, message_id}
  Both stored in bot-level col2 (status collection) — user-specific nahi.

Commands registered here (conflict check):
  /update_channel          — unique
  /update_channel_list     — unique
  /delete_update_channel   — unique
  /update_post             — unique (2-step session, group=15 text handler)
  /cancel_update_post      — unique (session cancel)
  /update_post_list        — unique
  /delete_update_post      — unique
  /updatechannel           — unique (on/off toggle)
  /latest_post_delete      — unique

  NOTE: text handler (group=0) sirf tab fire karta hai jab
  _update_post_sessions mein us user ka session ho.
  Isliye dusre plugins ke saath koi conflict nahi.
"""

import logging
import re

from pyrogram import Client, filters, enums, StopPropagation, ContinuePropagation
from pyrogram.types import (
    InlineKeyboardButton, InlineKeyboardMarkup, Message
)

from .. import app, owner, sudo_users
from ..utils.database.access_db import db

LOGGER = logging.getLogger(__name__)

# ─────────────────────────────────────────────
#  In-memory session for /update_post 2-step flow
# ─────────────────────────────────────────────
_update_post_sessions: dict = {}   # { user_id: {'step': 'anime'|'link', 'anime': str} }


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
    """anime_name (lowercase) → invite_link map lo."""
    doc = await db.col2.find_one({'id': 'update_post_map'})
    if not doc:
        return {}
    return doc.get('map', {})


async def _save_post_map(data: dict):
    await db.col2.update_one(
        {'id': 'update_post_map'},
        {'$set': {'map': data}},
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
    Update channels pe stylish format post bhejta hai.

    RULES:
      1. /updatechannel off hai toh kuch nahi hoga.
      2. Sirf wohi anime ka post jaayega jiska entry update_post_map mein hai
         (exact/fuzzy match). Agar entry nahi → post skip.

    Single episode:  episode=6        → ⚡EP - 06 | Added
    Episode range:   episode_start=34, episode_end=36  → ⚡EP 34-36 | Added
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

    post_map = await _get_post_map()

    # ── Anime match check — REQUIRED ──
    # Sirf tab post hoga jab anime update_post_map mein registered ho
    query_norm = _norm(anime_name)
    invite_link = None
    matched = False
    for key, link in post_map.items():
        if _norm(key) == query_norm:
            invite_link = link
            matched = True
            break

    if not matched:
        LOGGER.info(
            f"[UpdateChannel] '{anime_name}' update_post_map mein nahi hai — post skip kiya."
        )
        return

    # ── Title line ──
    season_str = f"(S{season:02d})" if season else ""
    title_line = f"🔰 **{anime_name} {season_str}**".strip() if season_str else f"🔰 **{anime_name}**"

    # ── Episode line ──
    ep_str = ""
    if episode_start and episode_end and episode_start != episode_end:
        ep_str = f">⚡**EP {episode_start}-{episode_end} | Added**"
    elif episode_start:
        ep_num = f"{episode_start:02d}" if episode_start < 100 else str(episode_start)
        ep_str = f">⚡**EP - {ep_num} | Added**"
    elif episode:
        ep_num = f"{episode:02d}" if episode < 100 else str(episode)
        ep_str = f">⚡**EP - {ep_num} | Added**"

    divider = "──────────────────────────"

    # ── Build text ──
    lines = [title_line, divider]
    if ep_str:
        lines.append(ep_str)
    if invite_link:
        lines.append(f"[Start the Bot Get Link Here]({invite_link})")
    text = "\n".join(lines)

    if not anime_name.strip():
        LOGGER.warning("[UpdateChannel] anime_name empty, post skip kiya.")
        return

    # ── Button ──
    markup = None
    if invite_link:
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("❇️ Start the Bot Get Link Here ❇️", url=invite_link)]
        ])

    # ── Send and store latest post IDs ──
    new_latest = []
    for ch in channels:
        ch_id = ch.get("channel_id")
        if not ch_id:
            continue
        try:
            sent = await client.send_message(
                chat_id=ch_id,
                text=text,
                reply_markup=markup,
                parse_mode=enums.ParseMode.MARKDOWN,
                disable_web_page_preview=True,
            )
            new_latest.append({"channel_id": ch_id, "message_id": sent.id})
            LOGGER.info(
                f"[UpdateChannel] Post sent to {ch_id} for '{anime_name}' Ep {episode or episode_start} (msg_id={sent.id})"
            )
        except Exception as e:
            LOGGER.error(f"[UpdateChannel] Failed to send to {ch_id}: {e}")

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
    """Anime ka invite link save karo."""
    if not _is_auth(message.from_user.id):
        return

    user_id = message.from_user.id
    _update_post_sessions[user_id] = {"step": "anime"}

    await message.reply(
        "**Step 1/2 — Anime ka naam do:**\n\n"
        "**Example:** `Witch Hat Atelier`\n\n"
        "_Cancel karna ho toh `/cancel_update_post` bhejo._"
    )


@Client.on_message(filters.command("cancel_update_post") & filters.private)
async def cmd_cancel_update_post(client: Client, message: Message):
    user_id = message.from_user.id
    _update_post_sessions.pop(user_id, None)
    await message.reply("❌ Cancelled.")


# ─────────────────────────────────────────────
#  /update_post_list
# ─────────────────────────────────────────────
@Client.on_message(filters.command("update_post_list") & filters.private)
async def cmd_update_post_list(client: Client, message: Message):
    """Saare saved anime → invite link pairs dikho."""
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
    text += "_Sirf inhi anime ka post update channel pe jaayega_\n\n"
    for i, (anime, link) in enumerate(post_map.items(), 1):
        link_display = f"[link]({link})" if link else "_(no link)_"
        text += f"`{i}.` **{anime}**\n    🔗 {link_display}\n\n"

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
#  Text input handler for /update_post 2-step flow
#  group=0 — sabse pehle fire hoga, kisi se conflict nahi
#  Agar session nahi → ContinuePropagation (agle handlers ko jaane do)
#  Agar session hai → process karo, StopPropagation (koi aur na pakde)
# ─────────────────────────────────────────────
@Client.on_message(filters.text & filters.private, group=0)
async def update_post_text_input(client: Client, message: Message):
    """update_post ke 2-step input ko handle karo."""
    user_id = message.from_user.id

    # Auth check — authorized nahi toh agle handlers ko jaane do
    if not _is_auth(user_id):
        raise ContinuePropagation

    # Session nahi → hamara kaam nahi, agle handler ko jaane do
    session = _update_post_sessions.get(user_id)
    if not session:
        raise ContinuePropagation

    # Session hai — ab sirf hum handle karenge, koi aur nahi
    text = message.text.strip()

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
            f"**Step 2/2 — Invite link do:**\n\n"
            f"**Example:** `https://t.me/+xxxxxxxxxx`\n\n"
            f"_Link nahi dena toh `skip` likho — post button ke bina aayega._"
        )
        raise StopPropagation

    # ── Step 2: Invite link ──
    if session.get("step") == "link":
        anime_name = session["anime"]
        _update_post_sessions.pop(user_id, None)

        if text.lower() == "skip":
            invite_link = ""
        elif not text.startswith("http"):
            await message.reply(
                "⚠️ Valid invite link do (`https://t.me/...`) ya `skip` likho."
            )
            _update_post_sessions[user_id] = session
            raise StopPropagation
        else:
            invite_link = text

        post_map = await _get_post_map()
        post_map[anime_name.lower().strip()] = invite_link
        await _save_post_map(post_map)

        if invite_link:
            await message.reply(
                f"✅ **Saved!**\n\n"
                f"📺 Anime: **{anime_name}**\n"
                f"🔗 Link: `{invite_link}`\n\n"
                f"Ab jab bhi `{anime_name}` ka 360p upload hoga, "
                f"update channel pe link ke saath post aayega."
            )
        else:
            await message.reply(
                f"✅ **Saved!** (link ke bina)\n\n"
                f"📺 Anime: **{anime_name}**\n\n"
                f"Post aayega par button nahi hoga.\n"
                f"Link add karna ho toh dobara `/update_post` karo."
            )
        raise StopPropagation
