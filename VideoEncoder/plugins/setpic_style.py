"""
setpic_style.py
=================
/setpic_style — auto-generated thumbnail ke bottom band (jaisa red box
mein "@SBANIME" dikhta hai) ke liye 10 professional style presets me se
ek choose karo, preview dekho, color badlo, ya style poori tarah OFF kar
do. GLOBAL (bot-wide) setting hai — community branding ki tarah, sirf
owner/sudo chala sakte hain, aur jo bhi select ho woh sabhi uploads
(manual + auto-monitor) mein consistent lagta hai.

Flow:
  /setpic_style -> 10 styles ki list (+ Disable) dikhti hai
  style tap     -> us style ka live preview (sample card pe render karke)
  🎨 Change Color -> 10 color swatches, jo bhi pick karo turant preview
                     update ho jaata hai
  ✅ Apply This Style -> DB mein save + turant effective (in-memory cache
                          bhi refresh ho jaata hai, restart ki zaroorat nahi)
"""

import os

from pyrogram import Client, filters
from pyrogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    InputMediaPhoto, Message,
)

from .. import download_dir, owner, sudo_users
from ..utils import thumb_style


def _is_auth(user_id: int) -> bool:
    return user_id in owner or user_id in sudo_users


def _status_text() -> str:
    style_id = thumb_style.get_current_style_id()
    color = thumb_style.get_current_color()
    if style_id == "none":
        current = "🚫 Disabled (koi style nahi lagega)"
    else:
        name = thumb_style.STYLES.get(style_id, {}).get("name", style_id)
        current = name + (f", color: `{color}`" if color else "")
    return (
        "🎨 **Thumbnail Band Style** <i>(bot-wide)</i>\n\n"
        f"Current: {current}\n\n"
        "Neeche se koi bhi style tap karo — pehle uska preview dikhega, "
        "phir chaaho toh color badal ke apply kar sakte ho."
    )


def _list_keyboard() -> InlineKeyboardMarkup:
    rows = []
    ids = thumb_style.STYLE_ORDER
    for i in range(0, len(ids), 2):
        row = [InlineKeyboardButton(thumb_style.STYLES[sid]["name"], callback_data=f"sp:prev:{sid}:-1")
               for sid in ids[i:i + 2]]
        rows.append(row)
    rows.append([InlineKeyboardButton("🚫 Disable (No Style)", callback_data="sp:off")])
    return InlineKeyboardMarkup(rows)


def _preview_keyboard(style_id: str, color_idx: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎨 Change Color", callback_data=f"sp:colors:{style_id}:{color_idx}")],
        [InlineKeyboardButton("✅ Apply This Style", callback_data=f"sp:apply:{style_id}:{color_idx}")],
        [InlineKeyboardButton("🔙 Back to list", callback_data="sp:list")],
    ])


def _color_keyboard(style_id: str) -> InlineKeyboardMarkup:
    rows = []
    presets = thumb_style.COLOR_PRESETS
    for i in range(0, len(presets), 2):
        row = [
            InlineKeyboardButton(label, callback_data=f"sp:setcolor:{style_id}:{idx}")
            for idx, (label, _hex) in list(enumerate(presets))[i:i + 2]
        ]
        rows.append(row)
    rows.append([InlineKeyboardButton("🔙 Back to preview", callback_data=f"sp:prev:{style_id}:-1")])
    return InlineKeyboardMarkup(rows)


def _color_hex(idx: int):
    if idx is None or idx < 0:
        return None
    presets = thumb_style.COLOR_PRESETS
    if 0 <= idx < len(presets):
        return presets[idx][1]
    return None


@Client.on_message(filters.command("setpic_style"))
async def cmd_setpic_style(client: Client, message: Message):
    if not _is_auth(message.from_user.id):
        await message.reply("❌ Sirf owner/sudo hi thumbnail style badal sakte hain (yeh bot-wide setting hai).")
        return
    await message.reply(_status_text(), reply_markup=_list_keyboard())


@Client.on_callback_query(filters.regex(r"^sp:"))
async def setpic_style_callback(client: Client, cb: CallbackQuery):
    if not _is_auth(cb.from_user.id):
        await cb.answer("Yeh sirf owner/sudo ke liye hai.", show_alert=True)
        return

    parts = cb.data.split(":")
    action = parts[1]

    if action == "list":
        await cb.answer()
        try:
            await client.send_message(cb.message.chat.id, _status_text(), reply_markup=_list_keyboard())
        except Exception:
            pass
        return

    if action == "off":
        await cb.answer("Style disable ho raha hai...")
        await thumb_style.set_style("none")
        try:
            await client.send_message(
                cb.message.chat.id,
                "✅ **Thumbnail style OFF kar diya gaya.**\n\nAb auto-generated "
                "thumbnails pe koi band/box nahi lagega — turant effective hai.",
            )
        except Exception:
            pass
        return

    if action == "prev":
        style_id, color_idx = parts[2], int(parts[3])
        await cb.answer("Preview render ho raha hai...")
        path = thumb_style.generate_preview(style_id, _color_hex(color_idx), dest_dir=download_dir or "/tmp")
        name = thumb_style.STYLES[style_id]["name"]
        caption = f"**Preview: {name}**"
        try:
            if cb.message.photo:
                await cb.message.edit_media(
                    InputMediaPhoto(path, caption=caption),
                    reply_markup=_preview_keyboard(style_id, color_idx),
                )
            else:
                await client.send_photo(
                    cb.message.chat.id, path, caption=caption,
                    reply_markup=_preview_keyboard(style_id, color_idx),
                )
        finally:
            try:
                os.remove(path)
            except Exception:
                pass
        return

    if action == "colors":
        style_id, color_idx = parts[2], int(parts[3])
        await cb.answer()
        try:
            await cb.message.edit_reply_markup(_color_keyboard(style_id))
        except Exception:
            pass
        return

    if action == "setcolor":
        style_id, color_idx = parts[2], int(parts[3])
        await cb.answer("Color apply ho raha hai preview pe...")
        path = thumb_style.generate_preview(style_id, _color_hex(color_idx), dest_dir=download_dir or "/tmp")
        name = thumb_style.STYLES[style_id]["name"]
        label = thumb_style.COLOR_PRESETS[color_idx][0]
        caption = f"**Preview: {name}** — color: {label}"
        try:
            await cb.message.edit_media(
                InputMediaPhoto(path, caption=caption),
                reply_markup=_preview_keyboard(style_id, color_idx),
            )
        finally:
            try:
                os.remove(path)
            except Exception:
                pass
        return

    if action == "apply":
        style_id, color_idx = parts[2], int(parts[3])
        await cb.answer("Apply ho raha hai...")
        await thumb_style.set_style(style_id)
        await thumb_style.set_color(_color_hex(color_idx))
        name = thumb_style.STYLES[style_id]["name"]
        color_hex = _color_hex(color_idx)
        color_line = f"\nColor: `{color_hex}`" if color_hex else ""
        try:
            if cb.message.photo:
                await cb.message.edit_caption(
                    f"✅ **Applied: {name}**{color_line}\n\nSabhi naye auto-generated "
                    f"thumbnails pe ab yehi style lagega — turant effective hai.",
                    reply_markup=None,
                )
        except Exception:
            pass
        return
