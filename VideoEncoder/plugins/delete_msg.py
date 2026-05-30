"""
delete_msg.py
=============
Auto Delete Feature

Jab set kiye hue channel pe koi nayi VIDEO FILE upload hoti hai,
toh us video ke niche wale purane 3 messages automatically delete ho jaate hain.

Commands:
  /delete_message [channel_id]  → Channel add karo ya list dekho
  /delete_message_list          → Saare set channels dekho
  /delete_message_del [number]  → Channel remove karo

How it works:
  - Bot un channels ko monitor karta hai jo set kiye gaye hain
  - Jab koi nayi video file aati hai, uske pehle wale 3 messages delete ho jaate hain
    (video khud DELETE NAHI hoti — sirf usse PEHLE ke 3 messages)
"""

import logging

from pyrogram import Client, filters
from pyrogram.types import Message

from .. import LOGGER, app, owner, sudo_users
from ..utils.database.access_db import db


# ─────────────────────────────────────────────
#  Auth helper
# ─────────────────────────────────────────────
def _is_authorized(user_id: int) -> bool:
    return user_id in owner or user_id in sudo_users


# ─────────────────────────────────────────────
#  DB helpers — owner ke record mein store karo
# ─────────────────────────────────────────────
async def _owner_id() -> int | None:
    return owner[0] if owner else None


async def _get_delete_channels() -> list:
    """Saare set auto-delete channels return karo."""
    oid = await _owner_id()
    if not oid:
        return []
    user = await db._get_user(oid)
    return user.get('auto_delete_channels', [])


async def _save_delete_channels(channels: list):
    """Updated channels list DB mein save karo."""
    oid = await _owner_id()
    if not oid:
        return
    await db.col.update_one(
        {'id': oid},
        {'$set': {'auto_delete_channels': channels}},
        upsert=True
    )


# ─────────────────────────────────────────────
#  /delete_message [channel_id]
#  Bina argument: list dikhao
#  Argument ke saath: channel add karo
# ─────────────────────────────────────────────
@Client.on_message(filters.command("delete_message") & filters.private)
async def cmd_delete_message(client: Client, message: Message):
    """
    /delete_message [channel_id]
    Set karo — jab us channel pe nayi video aaye, uske niche 3 purane msgs delete honge.
    """
    if not _is_authorized(message.from_user.id):
        await message.reply("❌ Tumhare paas ye command use karne ki permission nahi hai!")
        return

    # Bina argument: list dikhao
    if len(message.command) < 2:
        channels = await _get_delete_channels()
        if not channels:
            await message.reply(
                "🗑️ **Auto Delete Channels**\n\n"
                "❌ Abhi koi channel set nahi hai.\n\n"
                "**Usage:** `/delete_message -100xxxxxxxxx`\n"
                "Jab us channel pe nayi video upload hogi, uske pehle ke 3 messages "
                "automatically delete ho jaayenge!"
            )
        else:
            text = "🗑️ **Auto Delete Channels:**\n\n"
            for i, ch in enumerate(channels, 1):
                try:
                    chat = await client.get_chat(ch['channel_id'])
                    title = chat.title
                except Exception:
                    title = ch.get('title', 'Unknown')
                text += f"**{i}.** {title}\n    `{ch['channel_id']}`\n\n"
            text += (
                f"Total: **{len(channels)}**\n\n"
                "🗑️ Remove: `/delete_message_del <number>`"
            )
            await message.reply(text)
        return

    # Argument ke saath: add karo
    try:
        channel_id = int(message.command[1])
    except ValueError:
        await message.reply("❌ Valid channel ID dalo! Format: `-100xxxxxxxxx`")
        return

    # Channel exist check
    try:
        chat = await client.get_chat(channel_id)
        title = chat.title
    except Exception as e:
        await message.reply(
            f"❌ Channel nahi mila: `{e}`\n\n"
            "Bot ko channel mein admin banao pehle (Delete Messages permission chahiye)."
        )
        return

    channels = await _get_delete_channels()

    # Already exists check
    for ch in channels:
        if ch['channel_id'] == channel_id:
            await message.reply(f"⚠️ **{title}** already set hai!")
            return

    channels.append({'channel_id': channel_id, 'title': title})
    await _save_delete_channels(channels)

    await message.reply(
        f"✅ **Auto Delete Channel Added!**\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📢 **{title}**\n"
        f"🆔 `{channel_id}`\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Ab jab bhi is channel pe nayi video upload hogi,\n"
        f"uske pehle ke **3 messages** automatically delete ho jaayenge! 🗑️"
    )


# ─────────────────────────────────────────────
#  /delete_message_list
#  Saare set channels dikhao
# ─────────────────────────────────────────────
@Client.on_message(filters.command("delete_message_list") & filters.private)
async def cmd_delete_message_list(client: Client, message: Message):
    if not _is_authorized(message.from_user.id):
        await message.reply("❌ Tumhare paas ye command use karne ki permission nahi hai!")
        return

    channels = await _get_delete_channels()
    if not channels:
        await message.reply(
            "🗑️ **Auto Delete Channels**\n\n"
            "❌ Koi channel set nahi hai.\n\n"
            "Add karo: `/delete_message -100xxxxxxxxx`"
        )
        return

    text = "🗑️ **Auto Delete Channels:**\n\n"
    for i, ch in enumerate(channels, 1):
        try:
            chat = await client.get_chat(ch['channel_id'])
            title = chat.title
        except Exception:
            title = ch.get('title', 'Unknown')
        text += f"**{i}.** {title}\n    `{ch['channel_id']}`\n\n"
    text += (
        f"Total: **{len(channels)}**\n\n"
        "🗑️ Remove: `/delete_message_del <number>`"
    )
    await message.reply(text)


# ─────────────────────────────────────────────
#  /delete_message_del [number]
#  Channel remove karo
# ─────────────────────────────────────────────
@Client.on_message(filters.command("delete_message_del") & filters.private)
async def cmd_delete_message_del(client: Client, message: Message):
    if not _is_authorized(message.from_user.id):
        await message.reply("❌ Tumhare paas ye command use karne ki permission nahi hai!")
        return

    channels = await _get_delete_channels()
    if not channels:
        await message.reply("❌ Koi channel set nahi hai!")
        return

    # Bina argument: list dikhao
    if len(message.command) < 2:
        text = "🗑️ **Konsa remove karna hai?**\n\n"
        for i, ch in enumerate(channels, 1):
            text += f"**{i}.** {ch.get('title', 'Unknown')} (`{ch['channel_id']}`)\n"
        text += "\nUse: `/delete_message_del <number>`"
        await message.reply(text)
        return

    try:
        num = int(message.command[1])
    except ValueError:
        await message.reply("❌ Sahi number dalo!")
        return

    if num < 1 or num > len(channels):
        await message.reply(f"❌ 1 se {len(channels)} tak dalo.")
        return

    removed = channels.pop(num - 1)
    await _save_delete_channels(channels)
    await message.reply(
        f"✅ **Removed!**\n\n"
        f"📢 {removed.get('title', 'Unknown')}\n"
        f"🆔 `{removed['channel_id']}`"
    )


# ─────────────────────────────────────────────
#  Auto Delete Watcher
#  Channel messages monitor karo — nayi video aane pe
#  uske pehle ke 3 messages delete karo
# ─────────────────────────────────────────────
@Client.on_message(filters.channel & (filters.video | filters.document))
async def auto_delete_old_messages(client: Client, message: Message):
    """
    Jab set kiye hue channel pe nayi video/document file aaye,
    toh usse pehle ke 3 messages delete karo.
    User session available ho toh userbot se delete karo (better permissions),
    warna bot se try karo.
    """
    # Sirf set channels pe kaam karo
    channels = await _get_delete_channels()
    if not channels:
        return

    monitored_ids = {ch['channel_id'] for ch in channels}
    if message.chat.id not in monitored_ids:
        return

    # Document hai toh check karo video type ho
    if message.document:
        mime = message.document.mime_type or ""
        if not mime.startswith("video/"):
            return  # Sirf video documents — images/zip etc skip

    current_id = message.id

    # Actual previous 3 messages fetch karo — ID guess nahi, history se lo
    to_delete = []
    try:
        async for old_msg in client.get_chat_history(message.chat.id, limit=10):
            if old_msg.id >= current_id:
                continue
            to_delete.append(old_msg.id)
            if len(to_delete) == 3:
                break
    except Exception as e:
        LOGGER.warning(
            f"[AutoDelete] Could not fetch chat history "
            f"in {message.chat.id}: {e}"
        )
        return

    if not to_delete:
        return

    # User session try karo pehle (owner ka) — better channel permissions
    delete_client = client
    user_client = None

    try:
        oid = await _owner_id()
        if oid:
            user = await db._get_user(oid)
            session_str = user.get("user_session") if user else None
            if session_str:
                from pyrogram import Client as PyroClient
                user_client = PyroClient(
                    "auto_delete_user",
                    session_string=session_str,
                    in_memory=True,
                )
                await user_client.connect()
                delete_client = user_client
                LOGGER.info("[AutoDelete] Using userbot session for delete")
    except Exception as e:
        LOGGER.warning(f"[AutoDelete] Could not init userbot, using bot: {e}")
        user_client = None
        delete_client = client

    deleted_count = 0
    try:
        for msg_id in to_delete:
            try:
                await delete_client.delete_messages(
                    chat_id=message.chat.id,
                    message_ids=msg_id
                )
                deleted_count += 1
            except Exception as e:
                LOGGER.warning(
                    f"[AutoDelete] Could not delete msg {msg_id} "
                    f"in {message.chat.id}: {e}"
                )
    finally:
        if user_client:
            try:
                await user_client.disconnect()
            except Exception:
                pass

    if deleted_count > 0:
        LOGGER.info(
            f"[AutoDelete] Deleted {deleted_count} old messages "
            f"in channel {message.chat.id} (new video msg_id={current_id})"
        )
