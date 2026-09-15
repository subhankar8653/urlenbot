"""
caption_style.py
==================
/caption_style — /bot_upload se jab episode channel pe post hota hai,
uske caption ke liye 10 professional style presets + "Default" (purana
plain wala) me se ek choose karo, live preview dekho, phir apply karo.
GLOBAL (bot-wide) setting hai — thumb_style ki tarah, sirf owner/sudo
chala sakte hain, aur jo bhi select ho woh sabhi naye episode-posts
(manual + auto-monitor, dono `/bot_upload` engine se hi jaate hain) mein
consistent lagta hai — turant effective, restart ki zaroorat nahi.

Flow:
  /caption_style      -> 10 styles + Default ki list dikhti hai
  style tap           -> us style ka live text-preview (sample data pe)
  ✅ Apply This Style  -> DB mein save + turant effective (in-memory
                          cache bhi refresh ho jaata hai)
  🔙 Back to list      -> list pe wapas
"""

from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .. import owner, sudo_users
from ..utils import caption_style


def _is_auth(user_id: int) -> bool:
    return user_id in owner or user_id in sudo_users


def _status_text() -> str:
    style_id = caption_style.get_current_style_id()
    name = caption_style.STYLES.get(style_id, {}).get("name", style_id)
    return (
        "🎨 **Episode Caption Style** <i>(bot-wide)</i>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Current: **{name}**\n\n"
        "Neeche se koi bhi style tap karo — pehle uska preview dikhega, "
        "phir chaaho toh apply kar sakte ho.\n\n"
        "> ℹ️ _Yeh style `/bot_upload` se jaane waale har naye episode "
        "ke caption pe lagega._"
    )


def _list_keyboard() -> InlineKeyboardMarkup:
    rows = []
    ids = caption_style.STYLE_ORDER
    for i in range(0, len(ids), 2):
        row = [
            InlineKeyboardButton(caption_style.STYLES[sid]["name"], callback_data=f"cst:prev:{sid}")
            for sid in ids[i:i + 2]
        ]
        rows.append(row)
    rows.append([InlineKeyboardButton(
        caption_style.STYLES[caption_style.DEFAULT_STYLE_ID]["name"],
        callback_data=f"cst:prev:{caption_style.DEFAULT_STYLE_ID}",
    )])
    return InlineKeyboardMarkup(rows)


def _preview_keyboard(style_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Apply This Style", callback_data=f"cst:apply:{style_id}")],
        [InlineKeyboardButton("🔙 Back to list", callback_data="cst:list")],
    ])


def _preview_text(style_id: str) -> str:
    name = caption_style.STYLES.get(style_id, {}).get("name", style_id)
    rendered = caption_style.render_caption(style_id, caption_style.PREVIEW_DATA)
    return (
        f"**👀 Preview: {name}**\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{rendered}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"_(sample data — asli post mein anime/episode ki apni details aayengi)_"
    )


@Client.on_message(filters.command("caption_style"))
async def cmd_caption_style(client: Client, message: Message):
    if not _is_auth(message.from_user.id):
        await message.reply("❌ Sirf owner/sudo hi caption style badal sakte hain (yeh bot-wide setting hai).")
        return
    await message.reply(_status_text(), reply_markup=_list_keyboard())


@Client.on_callback_query(filters.regex(r"^cst:"))
async def caption_style_callback(client: Client, cb: CallbackQuery):
    if not _is_auth(cb.from_user.id):
        await cb.answer("Yeh sirf owner/sudo ke liye hai.", show_alert=True)
        return

    parts = cb.data.split(":")
    action = parts[1]

    if action == "list":
        await cb.answer()
        try:
            await cb.message.edit(_status_text(), reply_markup=_list_keyboard())
        except Exception:
            pass
        return

    if action == "prev":
        style_id = parts[2]
        await cb.answer("Preview render ho raha hai...")
        try:
            await cb.message.edit(_preview_text(style_id), reply_markup=_preview_keyboard(style_id))
        except Exception:
            pass
        return

    if action == "apply":
        style_id = parts[2]
        await cb.answer("Apply ho raha hai...")
        await caption_style.set_style(style_id)
        name = caption_style.STYLES.get(style_id, {}).get("name", style_id)
        try:
            await cb.message.edit(
                f"✅ **Applied: {name}**\n\n"
                f"> ✨ _Ab `/bot_upload` se jaane waale har naye episode ke "
                f"caption pe yehi style lagega — turant effective hai._"
            )
        except Exception:
            pass
        return

    await cb.answer()
