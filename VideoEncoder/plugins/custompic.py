"""
custompic.py
=============
Keyword-based custom thumbnail plugin.

Commands:
  /setpic              (photo reply karke, bina keyword)  → Default thumbnail save karo
  /setpic <keyword>    (photo reply karke ya caption mein) → Keyword wali custom pic save karo
  /deletepic <keyword>                                     → Delete karo (alias: /delpic)
  /delpic <keyword>                                        → Delete karo
  /listpic                                                 → Sabke list

Auto-apply:
  Jab bhi koi file upload hogi aur uske filename/caption mein keyword match hoga
  (case-insensitive), to woh keyword ki saved pic thumbnail ban jayegi.

Helper function (swift_downloader se call hogi):
  get_custompic_for_file(user_id, filename) -> file_id | None
"""

import logging

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
    2 modes:

    Mode 1 — Bina keyword (default thumbnail):
      Kisi bhi image ko reply karke: /setpic
      → Woh image default thumbnail ban jaegi

    Mode 2 — Keyword ke saath (custom keyword thumbnail):
      Kisi image ko reply karke: /setpic Naruto
      → "Naruto" keyword ke liye woh pic save hogi
      → Jab bhi file caption mein "Naruto" hoga, auto-apply hoga
    """
    c = await check_chat(message, chat="Sudo")
    if not c:
        return

    user_id = message.from_user.id

    # Keyword extract karo (optional hai)
    # message.text ya message.caption dono check karo (photo ke saath caption bhi ho sakta hai)
    cmd_text = message.text or message.caption or ""
    parts = cmd_text.split(None, 1)
    keyword = parts[1].strip() if len(parts) > 1 else None

    # Photo dhundo — ya current message mein ya replied message mein
    photo = None
    if message.photo:
        photo = message.photo
    elif message.reply_to_message and message.reply_to_message.photo:
        photo = message.reply_to_message.photo
    else:
        if keyword:
            await message.reply(
                f"❌ Photo nahi mili!\n\n"
                f"Kisi photo ko **reply** karke `/setpic {keyword}` bhejo."
            )
        else:
            await message.reply(
                "❌ Photo nahi mili!\n\n"
                "**Usage:**\n"
                "• Kisi photo ko reply karke `/setpic` — default thumbnail save hoga\n"
                "• Kisi photo ko reply karke `/setpic Naruto` — keyword wali pic save hogi"
            )
        return

    file_id = photo.file_id

    if keyword:
        # Mode 2: Keyword ke saath — custom pic save karo
        await db.set_custompic(user_id, keyword, file_id)
        await message.reply(
            f"✅ **Custom Pic Saved!**\n\n"
            f"🔑 Keyword: `{keyword}`\n"
            f"📁 Jab bhi file/caption mein `{keyword}` hoga, yeh pic auto-thumbnail ban jayegi."
        )
    else:
        # Mode 1: Bina keyword — default thumbnail save karo
        await db.set_thumbnail(user_id, file_id)
        await message.reply(
            "✅ **Default Thumbnail Saved!**\n\n"
            "📌 Yeh pic ab default thumbnail ke roop mein use hogi."
        )


# ─────────────────────────────────────────────
#  /deletepic & /delpic command (dono kaam karenge)
# ─────────────────────────────────────────────
@Client.on_message(filters.command(["deletepic", "delpic"]))
async def deletepic_command(client: Client, message: Message):
    """
    Usage: /deletepic Naruto   ya   /delpic Naruto
    Keyword wali custom pic delete karo.
    """
    c = await check_chat(message, chat="Sudo")
    if not c:
        return

    user_id = message.from_user.id
    parts = message.text.split(None, 1)

    if len(parts) < 2 or not parts[1].strip():
        await message.reply(
            "**Usage:** `/deletepic <keyword>` ya `/delpic <keyword>`\n"
            "**Example:** `/deletepic Naruto`"
        )
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
