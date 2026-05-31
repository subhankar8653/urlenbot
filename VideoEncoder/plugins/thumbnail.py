import logging

from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from ..utils.database.access_db import db
from ..utils.helper import check_chat

LOGGER = logging.getLogger(__name__)

# ─────────────────────────────────────────────
#  /thumb command — thumbnail menu dikhao
# ─────────────────────────────────────────────
@Client.on_message(filters.command("thumb"))
async def thumb_command(client: Client, message: Message):
    try:
        c = await check_chat(message, chat="Both")
        if not c:
            return
        user_id = message.from_user.id
        thumbnail = await db.get_thumbnail(user_id)
        buttons = [
            [
                InlineKeyboardButton("Set/Replace Thumbnail", callback_data="set_thumb"),
                InlineKeyboardButton("Delete Thumbnail", callback_data="del_thumb"),
            ]
        ]
        if thumbnail:
            await message.reply_photo(
                photo=thumbnail,
                caption="Your current custom thumbnail.",
                reply_markup=InlineKeyboardMarkup(buttons),
            )
        else:
            await message.reply_text(
                "You don't have a custom thumbnail set.",
                reply_markup=InlineKeyboardMarkup(buttons),
            )
    except Exception as e:
        LOGGER.error(f"Error in thumb_command: {e}")
        await message.reply_text(f"An error occurred: {str(e)}")


# ─────────────────────────────────────────────
#  set_thumb / del_thumb callbacks
#  NOTE: group=10 use kar rahe hain taaki callbacks_.py (group=1) se conflict na ho
# ─────────────────────────────────────────────
@Client.on_callback_query(filters.regex(r"^(set_thumb|del_thumb)$"), group=10)
async def cb_thumb_handler(client: Client, query: CallbackQuery):
    data = query.data
    user_id = query.from_user.id
    try:
        if data == "set_thumb":
            await query.answer("Photo bhejo thumbnail set karne ke liye", show_alert=False)
            await query.message.edit_text(
                "📸 **Thumbnail Set Karo**\n\n"
                "Abhi ek photo bhejo (directly ya reply karke) —\n"
                "ya kisi photo ko reply karke **/setpic** bhi use kar sakte ho.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("❌ Cancel", callback_data="thumb_cancel")]]
                ),
            )
        elif data == "del_thumb":
            await db.set_thumbnail(user_id, None)
            await query.answer("✅ Thumbnail delete ho gaya!", show_alert=True)
            try:
                await query.message.delete()
            except Exception:
                pass
    except Exception as e:
        LOGGER.error(f"Error in cb_thumb_handler for {data}: {e}")
        try:
            await query.answer(f"Error: {str(e)}", show_alert=True)
        except Exception:
            pass


@Client.on_callback_query(filters.regex(r"^thumb_cancel$"), group=10)
async def cb_thumb_cancel(client: Client, query: CallbackQuery):
    try:
        await query.answer("Cancelled", show_alert=False)
        await query.message.delete()
    except Exception:
        pass


# ─────────────────────────────────────────────
#  Photo handler — ONLY for /thumb set flow
#
#  Conflict fix:
#  - Pehle check karo ki message ka caption /setpic ya /thumb hai
#  - Ya replied message woh specific prompt hai
#  - Warna ignore karo — custompic.py ka /setpic apna kaam karega
# ─────────────────────────────────────────────
THUMB_PROMPT_TEXT = "📸 **Thumbnail Set Karo**"
LEGACY_THUMB_PROMPT = "Send me a photo to set as your custom thumbnail."

@Client.on_message(filters.photo & filters.private, group=5)
async def save_thumb(client: Client, message: Message):
    try:
        caption = message.caption or ""

        # Caption mein /setpic hai to custompic.py handle karega — yahan ignore karo
        if caption.startswith("/setpic"):
            return

        user_id = message.from_user.id
        file_id = message.photo.file_id

        # Mode 1: caption mein /thumb ya /setthumb
        if caption.strip() in ("/thumb", "/setthumb"):
            c = await check_chat(message, chat="Both")
            if not c:
                return
            await db.set_thumbnail(user_id, file_id)
            await message.reply_text("✅ Custom thumbnail saved!")
            return

        # Mode 2: Reply to the /thumb set prompt
        replied = message.reply_to_message
        if replied and replied.text:
            if THUMB_PROMPT_TEXT in replied.text or LEGACY_THUMB_PROMPT in replied.text:
                c = await check_chat(message, chat="Both")
                if not c:
                    return
                await db.set_thumbnail(user_id, file_id)
                await message.reply_text("✅ Custom thumbnail saved!")
                return

    except Exception as e:
        LOGGER.error(f"Error in save_thumb: {e}")
        await message.reply_text(f"❌ An error occurred: {str(e)}")
