"""
custompic.py
=============
Keyword-based custom thumbnail plugin.

Commands:
  /setpic <keyword>  (photo ke saath)  → Save karo
  /delpic <keyword>                    → Delete karo
  /listpic                             → Sabke list

Auto-apply:
  Jab bhi koi file upload hogi aur uske filename mein keyword match hoga
  (case-insensitive), to woh keyword ki saved pic thumbnail ban jayegi.

Helper function (swift_downloader se call hogi):
  get_custompic_for_file(user_id, filename) -> file_id | None
"""

import logging
import re

from pyrogram import Client, filters
from pyrogram.types import Message

from .. import app
from ..utils.helper import check_chat
from ..utils.database.access_db import db

LOGGER = logging.getLogger(__name__)


# ─────────────────────────────────────────────
#  Helper — filename se matching keyword pic dhundo
# ─────────────────────────────────────────────
async def get_custompic_for_file(user_id: int, filename: str) -> str | None:
    """
    Filename mein koi saved keyword match karta hai to us keyword ki pic return karo.
    Case-insensitive match. Sabse lamba matching keyword jeetega.

    Usage (swift_downloader.py mein):
        from ..plugins.custompic import get_custompic_for_file
        thumb = await get_custompic_for_file(user_id, filename) or default_thumb
    """
    try:
        pics = await db.get_all_custompics(user_id)   # {keyword: file_id}
        if not pics:
            return None

        fname_lower = filename.lower()

        # Sabse lamba matching keyword prefer karo (e.g. "naruto shippuden" > "naruto")
        best_key = None
        best_len = 0
        for keyword, file_id in pics.items():
            if keyword.lower() in fname_lower:
                if len(keyword) > best_len:
                    best_key = keyword
                    best_len = len(keyword)

        if best_key:
            LOGGER.info(f"[CustomPic] Match: '{best_key}' in '{filename}'")
            return pics[best_key]

    except Exception as e:
        LOGGER.error(f"[CustomPic] get_custompic_for_file error: {e}")

    return None


# ─────────────────────────────────────────────
#  /setpic command
# ─────────────────────────────────────────────
@Client.on_message(filters.command("setpic"))
async def setpic_command(client: Client, message: Message):
    """
    Usage:
      Photo bhejo aur caption mein: /setpic Naruto
      Ya photo reply karke: /setpic Naruto
    """
    c = await check_chat(message, chat="Sudo")
    if not c:
        return

    user_id = message.from_user.id

    # Keyword extract karo
    parts = message.text.split(None, 1) if message.text else []
    keyword = parts[1].strip() if len(parts) > 1 else None

    if not keyword:
        await message.reply(
            "**Usage:** `/setpic <keyword>` — photo ke saath caption mein\n\n"
            "**Example:**\n"
            "Photo bhejo, caption: `/setpic Naruto`\n\n"
            "Jab bhi file mein 'Naruto' hoga, yeh pic thumbnail ban jayegi."
        )
        return

    # Photo dhundo — ya current message mein ya replied message mein
    photo = None
    if message.photo:
        photo = message.photo
    elif message.reply_to_message and message.reply_to_message.photo:
        photo = message.reply_to_message.photo
    else:
        await message.reply(
            f"❌ Photo nahi mili!\n\n"
            f"Photo ke saath caption mein `/setpic {keyword}` likho."
        )
        return

    file_id = photo.file_id

    # Save karo
    await db.set_custompic(user_id, keyword, file_id)

    await message.reply(
        f"✅ **Custom Pic Saved!**\n\n"
        f"🔑 Keyword: `{keyword}`\n"
        f"📁 Jab bhi filename mein `{keyword}` hoga, yeh pic auto-thumbnail ban jayegi."
    )


# ─────────────────────────────────────────────
#  /delpic command
# ─────────────────────────────────────────────
@Client.on_message(filters.command("delpic"))
async def delpic_command(client: Client, message: Message):
    """
    Usage: /delpic Naruto
    """
    c = await check_chat(message, chat="Sudo")
    if not c:
        return

    user_id = message.from_user.id
    parts = message.text.split(None, 1)

    if len(parts) < 2 or not parts[1].strip():
        await message.reply("**Usage:** `/delpic <keyword>`\nExample: `/delpic Naruto`")
        return

    keyword = parts[1].strip()
    pics = await db.get_all_custompics(user_id)

    # Case-insensitive search
    matched_key = None
    for k in pics:
        if k.lower() == keyword.lower():
            matched_key = k
            break

    if not matched_key:
        await message.reply(f"❌ `{keyword}` naam ka koi custom pic nahi mila.")
        return

    await db.del_custompic(user_id, matched_key)
    await message.reply(f"🗑️ **Deleted!** `{matched_key}` ka custom pic remove ho gaya.")


# ─────────────────────────────────────────────
#  /listpic command
# ─────────────────────────────────────────────
@Client.on_message(filters.command("listpic"))
async def listpic_command(client: Client, message: Message):
    """
    Usage: /listpic — sabke keywords list karo
    """
    c = await check_chat(message, chat="Sudo")
    if not c:
        return

    user_id = message.from_user.id
    pics = await db.get_all_custompics(user_id)

    if not pics:
        await message.reply(
            "📭 Koi custom pic save nahi hai.\n\n"
            "Add karne ke liye: `/setpic <keyword>` photo ke saath."
        )
        return

    text = f"🖼️ **Custom Pics ({len(pics)})**\n\n"
    for i, keyword in enumerate(sorted(pics.keys()), 1):
        text += f"`{i}.` `{keyword}`\n"

    text += f"\n💡 Delete: `/delpic <keyword>`"
    await message.reply(text)


# ─────────────────────────────────────────────
#  /previewpic command — keyword ki pic preview karo
# ─────────────────────────────────────────────
@Client.on_message(filters.command("previewpic"))
async def previewpic_command(client: Client, message: Message):
    """
    Usage: /previewpic Naruto — us keyword ki saved pic dikhaao
    """
    c = await check_chat(message, chat="Sudo")
    if not c:
        return

    user_id = message.from_user.id
    parts = message.text.split(None, 1)

    if len(parts) < 2 or not parts[1].strip():
        await message.reply("**Usage:** `/previewpic <keyword>`")
        return

    keyword = parts[1].strip()
    pics = await db.get_all_custompics(user_id)

    matched_key = None
    for k in pics:
        if k.lower() == keyword.lower():
            matched_key = k
            break

    if not matched_key:
        await message.reply(f"❌ `{keyword}` naam ka koi custom pic nahi mila.")
        return

    file_id = pics[matched_key]
    await message.reply_photo(
        photo=file_id,
        caption=f"🖼️ Custom pic for keyword: `{matched_key}`"
    )
