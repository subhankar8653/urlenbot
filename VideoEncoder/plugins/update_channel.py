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

Auto-trigger:
  Jab bhi 360p file upload hoti hai kisi anime channel pe (auto_monitor se),
  toh saare update channels pe ek post jaata hai:

    🔰 Witch Hat Atelier (S01)
    ──────────────────────────
    ⚡EP - 06 | Added
    [❇️ Start the Bot Get Link Here ❇️]   ← button (agar invite link saved hai)
    Start the Bot Get Link Here           ← plain text link (agar invite link saved hai)

DB storage:
  - update_channels  → col2, id='update_channels', data: list of {channel_id, channel_title}
  - update_post_map  → col2, id='update_post_map', data: dict {anime_name_lower: invite_link}
  Both stored in bot-level col2 (status collection) — user-specific nahi.

Commands registered here (conflict check):
  /update_channel          — unique
  /update_channel_list     — unique
  /delete_update_channel   — unique
  /update_post             — unique (2-step session, group=15 text handler)
  /cancel_update_post      — unique (session cancel)
  /update_post_list        — unique
  /delete_update_post      — unique

  NOTE: text handler (group=15) sirf tab fire karta hai jab
  _update_post_sessions mein us user ka session ho.
  Isliye dusre plugins ke saath koi conflict nahi.
"""

import logging
import re

from pyrogram import Client, filters, enums
from pyrogram.errors import StopPropagation
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


# ─────────────────────────────────────────────
#  PUBLIC: 360p upload hone pe auto_monitor call karega
# ─────────────────────────────────────────────
async def send_update_post(
    client,
    anime_name: str,
    season: int | None,
    episode: int | None,
):
    """
    360p upload hone pe call karo.
    Saare update channels pe stylish format post bhejta hai.

    Post format:
        🔰 Witch Hat Atelier (S01)
        ──────────────────────────
        ⚡EP - 06 | Added
        Start the Bot Get Link Here   ← plain text hyperlink (agar invite link saved hai)

    Button (agar invite link saved hai):
        [❇️ Start the Bot Get Link Here ❇️]
    """
    channels = await _get_update_channels()
    if not channels:
        LOGGER.warning("[UpdateChannel] send_update_post called but no update channels saved.")
        return

    post_map = await _get_post_map()

    # ── Title line ──
    season_str = f"(S{season:02d})" if season else ""
    title_line = f"🔰 **{anime_name} {season_str}**".strip() if season_str else f"🔰 **{anime_name}**"

    # ── Episode line ──
    ep_str = ""
    if episode:
        ep_num = f"{episode:02d}" if episode < 100 else str(episode)
        ep_str = f">⚡**EP - {ep_num} | Added**"

    divider = "──────────────────────────"

    # ── Invite link fuzzy match ──
    def _norm(s: str) -> str:
        return re.sub(r'[\s\-_]+', ' ', s.lower()).strip()

    invite_link = None
    query_norm = _norm(anime_name)
    for key, link in post_map.items():
        if _norm(key) == query_norm:
            invite_link = link
            break

    # ── Build text ──
    lines = [title_line, divider]
    if ep_str:
        lines.append(ep_str)
    # Plain text hyperlink (shows as clickable text in Telegram markdown)
    if invite_link:
        lines.append(f"[Start the Bot Get Link Here]({invite_link})")
    text = "\n".join(lines)

    # ── Blank post guard ──
    if not anime_name.strip():
        LOGGER.warning("[UpdateChannel] anime_name empty, post skip kiya.")
        return

    # ── Button ──
    markup = None
    if invite_link:
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("❇️ Start the Bot Get Link Here ❇️", url=invite_link)]
        ])

    for ch in channels:
        ch_id = ch.get("channel_id")
        if not ch_id:
            continue
        try:
            await client.send_message(
                chat_id=ch_id,
                text=text,
                reply_markup=markup,
                parse_mode=enums.ParseMode.MARKDOWN,
                disable_web_page_preview=True,
            )
            LOGGER.info(
                f"[UpdateChannel] Post sent to {ch_id} for '{anime_name}' Ep {episode}"
            )
        except Exception as e:
            LOGGER.error(f"[UpdateChannel] Failed to send to {ch_id}: {e}")


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
        f"Ab jab bhi koi 360p upload hoga, yahan post aayega."
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
    if not channels:
        await message.reply(
            "📭 Koi update channel add nahi hai.\n\n"
            "Add karne ke liye: `/update_channel [channel_id]`"
        )
        return

    text = f"📢 **Update Channels ({len(channels)})**\n\n"
    for i, ch in enumerate(channels, 1):
        text += f"`{i}.` **{ch.get('channel_title', 'Unknown')}**\n"
        text += f"    🆔 `{ch.get('channel_id')}`\n\n"

    text += "💡 Remove: `/delete_update_channel [channel_id]`"
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
            "Add karne ke liye: `/update_post`"
        )
        return

    text = f"📋 **Saved Anime Posts ({len(post_map)})**\n\n"
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

    def _norm(s: str) -> str:
        return re.sub(r'[\s\-_]+', ' ', s.lower()).strip()

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
        f"📺 `{matched_key}` remove ho gaya update post list se."
    )


# ─────────────────────────────────────────────
#  Text input handler for /update_post 2-step flow
#  group=15 — dusre plugins ke saath koi conflict nahi
# ─────────────────────────────────────────────
@Client.on_message(filters.text & filters.private, group=15)
async def update_post_text_input(client: Client, message: Message):
    """update_post ke 2-step input ko handle karo."""
    user_id = message.from_user.id
    if not _is_auth(user_id):
        return

    session = _update_post_sessions.get(user_id)
    if not session:
        return

    text = message.text.strip()

    if text.lower() in ["/cancel_update_post", "cancel"]:
        _update_post_sessions.pop(user_id, None)
        await message.reply("❌ Cancelled.")
        raise StopPropagation

    if text.startswith("/"):
        _update_post_sessions.pop(user_id, None)
        return

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
