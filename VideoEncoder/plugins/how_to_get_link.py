"""
how_to_get_link.py
====================
/how_to_get_link [url|remove] — GLOBAL (bot-wide) link jo styled update-post
templates (`/update_post_style` ke style1..style10, "Default" mein nahi) ke
neeche ek clickable "➥ ʜᴏᴡ ᴛᴏ ɢᴇᴛ ʟɪɴᴋ" line ke roop mein dikhta hai.

  /how_to_get_link                 -> abhi kya set hai, wo dikhao (+ Add/Remove buttons)
  /how_to_get_link <url>           -> set/update karo
  /how_to_get_link remove          -> hata do (line future posts mein
                                       nahi aayegi)

Buttons (👉 ➕ Set / Update Link, 🗑️ Remove Link) niche har jagah available
hain — standalone command ke reply mein aur bhi /Customise hub ke andar
"🔗 How To Get Link" section mein. "➕ Set / Update Link" tap karne ke baad
bas naya link reply/type karo, bas ho gaya — koi command yaad rakhne ki
zaroorat nahi.

DB: col2, id='how_to_get_link', {url: str}  (utils/update_post_style.py
    ke get_how_to_get_link()/set_how_to_get_link() se accessed).
"""

from pyrogram import Client, ContinuePropagation, StopPropagation, filters
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .. import owner, sudo_users
from ..utils import update_post_style

# { user_id: True }  -> is user ka agla text message "how_to_get_link" ke
# naye link ke roop mein liya jayega (➕ Set / Update Link button ke baad).
_hgl_sessions: dict = {}


def _is_auth(user_id: int) -> bool:
    return user_id in owner or user_id in sudo_users


async def _status_text() -> str:
    current = await update_post_style.get_how_to_get_link()
    if current:
        return (
            f"🔗 **How to get link — abhi set hai:**\n`{current}`\n\n"
            "Neeche se update ya remove kar sakte ho 👇"
        )
    return (
        "📭 **How to get link** abhi set nahi hai.\n\n"
        "_Set hone ke baad, styled update-posts (`/update_post_style` ke "
        "1-10 waale styles) ke neeche ek clickable_ **➥ ʜᴏᴡ ᴛᴏ ɢᴇᴛ ʟɪɴᴋ** "
        "_line add ho jaayegi. \"Default\" style mein nahi aati._\n\n"
        "Neeche se set kar sakte ho 👇"
    )


def _keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➕ Set / Update Link", callback_data="hgl:add"),
            InlineKeyboardButton("🗑️ Remove Link", callback_data="hgl:remove"),
        ],
    ])


@Client.on_message(filters.command("how_to_get_link") & filters.private)
async def cmd_how_to_get_link(client: Client, message: Message):
    if not _is_auth(message.from_user.id):
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply(await _status_text(), reply_markup=_keyboard())
        return

    arg = parts[1].strip()

    if arg.lower() in ("remove", "off", "clear", "none"):
        current = await update_post_style.get_how_to_get_link()
        if not current:
            await message.reply("📭 Pehle se hi koi link set nahi hai.")
            return
        await update_post_style.set_how_to_get_link("")
        await message.reply(
            "🗑️ **How to get link remove kar diya!**\n\n"
            "Ab se styled update-posts mein **➥ ʜᴏᴡ ᴛᴏ ɢᴇᴛ ʟɪɴᴋ** line nahi aayegi.",
            reply_markup=_keyboard(),
        )
        return

    if not (arg.startswith("http://") or arg.startswith("https://") or arg.startswith("t.me/")):
        await message.reply(
            "❌ Yeh ek valid link nahi lagta. `http(s)://` se shuru hona chahiye.\n"
            "Example: `/how_to_get_link https://t.me/YourChannel/123`"
        )
        return

    await update_post_style.set_how_to_get_link(arg)
    await message.reply(
        f"✅ **How to get link set kar diya!**\n`{arg}`\n\n"
        "✨ _Ab se styled update-posts (Default ke alawa) ke neeche "
        "**➥ ʜᴏᴡ ᴛᴏ ɢᴇᴛ ʟɪɴᴋ** clickable line dikhegi._",
        reply_markup=_keyboard(),
    )


@Client.on_callback_query(filters.regex(r"^hgl:"))
async def how_to_get_link_callback(client: Client, cb: CallbackQuery):
    user_id = cb.from_user.id
    if not _is_auth(user_id):
        await cb.answer("Yeh sirf owner/sudo ke liye hai.", show_alert=True)
        return

    action = cb.data.split(":", 1)[1]

    # Agar yeh panel /Customise hub se khula tha, toh "🔙 Customise Menu"
    # row ko re-render karte waqt wapas jod do.
    back_row = None
    if cb.message and cb.message.reply_markup:
        for row in cb.message.reply_markup.inline_keyboard:
            for btn in row:
                if btn.callback_data == "cmz:menu":
                    back_row = list(row)

    if action == "add":
        await cb.answer()
        _hgl_sessions[user_id] = True
        try:
            await cb.message.edit(
                "✏️ **Naya link bhejo** (reply karke ya seedha type karke):\n\n"
                "Example: `https://t.me/YourChannel/123`\n\n"
                "_Cancel karne ke liye_ `cancel` _bhejo._"
            )
        except Exception:
            pass
        return

    if action == "remove":
        current = await update_post_style.get_how_to_get_link()
        if not current:
            await cb.answer("Pehle se hi set nahi hai.", show_alert=True)
            return
        await update_post_style.set_how_to_get_link("")
        await cb.answer("🗑️ Remove kar diya!")
        kb_rows = list(_keyboard().inline_keyboard)
        if back_row:
            kb_rows.append(back_row)
        try:
            await cb.message.edit(await _status_text(), reply_markup=InlineKeyboardMarkup(kb_rows))
        except Exception:
            pass
        return

    await cb.answer()


@Client.on_message(filters.text & filters.private, group=0)
async def how_to_get_link_text_input(client: Client, message: Message):
    """➕ Set / Update Link button ke baad agla text message capture karta hai."""
    user_id = message.from_user.id

    if user_id not in _hgl_sessions:
        raise ContinuePropagation

    if not _is_auth(user_id):
        _hgl_sessions.pop(user_id, None)
        raise ContinuePropagation

    text = message.text.strip()

    if text.lower() in ("cancel", "/cancel"):
        _hgl_sessions.pop(user_id, None)
        await message.reply("❌ Cancelled.", reply_markup=_keyboard())
        raise StopPropagation

    if text.startswith("/"):
        _hgl_sessions.pop(user_id, None)
        raise ContinuePropagation

    if not (text.startswith("http://") or text.startswith("https://") or text.startswith("t.me/")):
        await message.reply(
            "❌ Yeh ek valid link nahi lagta. `http(s)://` se shuru hona chahiye.\n"
            "Dobara bhejo, ya `cancel` bhejo."
        )
        raise StopPropagation

    _hgl_sessions.pop(user_id, None)
    await update_post_style.set_how_to_get_link(text)
    await message.reply(
        f"✅ **How to get link set kar diya!**\n`{text}`\n\n"
        "✨ _Ab se styled update-posts (Default ke alawa) ke neeche "
        "**➥ ʜᴏᴡ ᴛᴏ ɢᴇᴛ ʟɪɴᴋ** clickable line dikhegi._",
        reply_markup=_keyboard(),
    )
    raise StopPropagation
