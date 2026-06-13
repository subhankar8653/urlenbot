"""
upload_mode_plugin.py
=====================
Dо upload modes:

  /file_mode  →  FILE_MODE  (default)
               Jaise abhi hai — direct video file channel pe upload hoti hai.

  /bot_mode   →  BOT_MODE
               Bot ek text post banata hai:
                 • Anime name + episode number
                 • Quality buttons (360p / 720p / 1080p) jaise-jaise files
                   upload hoti hain — same post edit hota hai, button add hota hai
                 • Har button ka URL = Suhani bot deep link (log channel se)

Commands:
  /file_mode  → FILE_MODE on karo
  /bot_mode   → BOT_MODE on karo
  /upload_mode_status → current mode dekho
"""

from pyrogram import Client, filters
from pyrogram.types import Message

from .. import app, owner, sudo_users
from ..utils.database.access_db import db


def _is_authorized(user_id: int) -> bool:
    return user_id in owner or user_id in sudo_users


# ─────────────────────────────────────────────
#  DB Helpers
# ─────────────────────────────────────────────

async def get_upload_mode(user_id: int) -> str:
    """
    Returns: 'file_mode' ya 'bot_mode'
    Default: 'file_mode'
    """
    user = await db._get_user(user_id)
    return user.get('upload_mode', 'file_mode')


async def set_upload_mode(user_id: int, mode: str):
    """mode = 'file_mode' ya 'bot_mode'"""
    await db.col.update_one(
        {'id': int(user_id)},
        {'$set': {'upload_mode': mode}},
        upsert=True,
    )


# ─────────────────────────────────────────────
#  /file_mode
# ─────────────────────────────────────────────

@Client.on_message(filters.command("file_mode") & filters.private)
async def cmd_file_mode(bot: Client, message: Message):
    if not _is_authorized(message.from_user.id):
        return

    await set_upload_mode(message.from_user.id, 'file_mode')

    await message.reply(
        "📁 <b>FILE MODE ON</b>\n\n"
        "Ab bot <b>seedha video file</b> channel pe upload karega.\n"
        "(Purana wala default behaviour)\n\n"
        "🔄 Bot mode ke liye: /bot_mode",
        parse_mode="HTML",
    )


# ─────────────────────────────────────────────
#  /bot_mode
# ─────────────────────────────────────────────

@Client.on_message(filters.command("bot_mode") & filters.private)
async def cmd_bot_mode(bot: Client, message: Message):
    if not _is_authorized(message.from_user.id):
        return

    await set_upload_mode(message.from_user.id, 'bot_mode')

    await message.reply(
        "🤖 <b>BOT MODE ON</b>\n\n"
        "Ab bot channel pe ek <b>text post</b> banega:\n"
        "• Anime name + episode number\n"
        "• Quality buttons (360p / 720p / 1080p)\n"
        "• Jaise-jaise qualities upload hongi, same post edit hoga\n"
        "• Har button = Suhani bot deep link\n\n"
        "📁 File mode ke liye: /file_mode",
        parse_mode="HTML",
    )


# ─────────────────────────────────────────────
#  /upload_mode_status
# ─────────────────────────────────────────────

@Client.on_message(filters.command("upload_mode_status") & filters.private)
async def cmd_upload_mode_status(bot: Client, message: Message):
    if not _is_authorized(message.from_user.id):
        return

    mode = await get_upload_mode(message.from_user.id)

    if mode == 'bot_mode':
        mode_text = "🤖 <b>BOT MODE</b>\nChannel pe text post + quality buttons."
    else:
        mode_text = "📁 <b>FILE MODE</b>\nDirect video file channel pe upload."

    await message.reply(
        f"📊 <b>Current Upload Mode</b>\n\n"
        f"{mode_text}\n\n"
        f"/file_mode  |  /bot_mode",
        parse_mode="HTML",
    )
