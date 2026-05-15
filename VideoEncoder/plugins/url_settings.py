"""
URL Uploader Settings Commands
  /urlsettings  - Show current URL uploader settings
  /clearmeta    - Clear saved metadata settings
"""

from pyrogram import Client, filters
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from .. import LOGGER
from ..utils.database.access_db import db
from ..utils.database.add_user import AddUserToDatabase
from ..utils.helper import check_chat, output


@Client.on_message(filters.command("urlsettings"))
async def url_settings_cmd(bot: Client, message: Message):
    """Show current URL uploader settings."""
    c = await check_chat(message, chat="Both")
    if not c:
        return
    await AddUserToDatabase(bot, message)

    user_id = message.from_user.id
    meta = await db.get_url_metadata(user_id)
    swap_rules = await db.get_swap(user_id)

    meta_text = (
        f"  🎬 Video Title: <code>{meta.get('video_title') or 'not set'}</code>\n"
        f"  🔊 Audio Title: <code>{meta.get('audio_title') or 'not set'}</code>\n"
        f"  📺 Show Title:  <code>{meta.get('show_title') or 'not set'}</code>"
    )

    swap_text = ""
    if swap_rules:
        for k, v in swap_rules.items():
            swap_text += f"  <code>{k}</code> → <code>{v}</code>\n"
    else:
        swap_text = "  None set"

    text = (
        "<b>⚙️ URL Uploader Settings</b>\n\n"
        "<b>🏷️ Saved Metadata:</b>\n"
        f"{meta_text}\n\n"
        "<b>🔄 Name Swap Rules:</b>\n"
        f"{swap_text}\n\n"
        "<b>Commands:</b>\n"
        "• <code>/url &lt;link&gt;</code> – Download & process\n"
        "• <code>/addswap &lt;from&gt; &lt;to&gt;</code> – Add swap rule\n"
        "• <code>/swaplist</code> – View swap rules\n"
        "• <code>/clearswap</code> – Delete all swap rules\n"
        "• <code>/clearmeta</code> – Reset saved metadata"
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🗑️ Clear Metadata", callback_data=f"urlset_clearmeta_{user_id}"),
            InlineKeyboardButton("🗑️ Clear Swaps",    callback_data=f"urlset_clearswap_{user_id}"),
        ],
        [InlineKeyboardButton("❌ Close", callback_data="closeMeh")],
    ])
    await message.reply(text, reply_markup=kb)


@Client.on_message(filters.command("clearmeta"))
async def clear_meta_cmd(bot: Client, message: Message):
    c = await check_chat(message, chat="Both")
    if not c:
        return
    await db.clear_url_metadata(message.from_user.id)
    await message.reply("✅ Metadata settings cleared.", reply_markup=output)


@Client.on_callback_query(filters.regex(r"^urlset_"))
async def url_settings_callbacks(bot: Client, cb: CallbackQuery):
    parts = cb.data.split("_")
    # urlset_action_userid
    if len(parts) < 3:
        await cb.answer()
        return

    action = parts[1]
    try:
        owner_id = int(parts[2])
    except ValueError:
        await cb.answer()
        return

    if cb.from_user.id != owner_id:
        await cb.answer("❌ Ye tumhara nahi hai!", show_alert=True)
        return

    if action == "clearmeta":
        await db.clear_url_metadata(owner_id)
        await cb.answer("✅ Metadata cleared!", show_alert=True)
        await cb.message.delete()

    elif action == "clearswap":
        await db.clear_swap(owner_id)
        await cb.answer("✅ Swap rules cleared!", show_alert=True)
        await cb.message.delete()
