"""
caption_style.py
==================
/caption_style — episode caption ke liye 10 professional style presets +
"Default" (purana plain wala) me se ek choose karo, live preview dekho,
phir apply karo.
GLOBAL (bot-wide) setting hai — thumb_style ki tarah, sirf owner/sudo
chala sakte hain, aur jo bhi select ho woh HAR upload path pe consistent
lagta hai: `/bot_upload` (manual + auto-monitor RTI post), auto-monitor
ka bot-mode post, aur `/upload` / `/url` (manual + auto processing) ke
video captions — turant effective, restart ki zaroorat nahi.

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
        "> ℹ️ _Yeh style har jagah lagega._"
    )


def _list_keyboard() -> InlineKeyboardMarkup:
    current = caption_style.get_current_style_id()

    def _label(sid: str) -> str:
        name = caption_style.STYLES[sid]["name"]
        return f"✅ {name} (Current)" if sid == current else name

    rows = []
    ids = caption_style.STYLE_ORDER
    for i in range(0, len(ids), 2):
        row = [
            InlineKeyboardButton(_label(sid), callback_data=f"cst:prev:{sid}")
            for sid in ids[i:i + 2]
        ]
        rows.append(row)
    rows.append([InlineKeyboardButton(
        _label(caption_style.DEFAULT_STYLE_ID),
        callback_data=f"cst:prev:{caption_style.DEFAULT_STYLE_ID}",
    )])
    rows.append([InlineKeyboardButton("🔙 Customise Menu", callback_data="cmz:menu")])
    return InlineKeyboardMarkup(rows)


def _preview_keyboard_dict(style_id: str) -> dict:
    """Bot API raw-dict keyboard (preview Bot API se hi bhejta/edit karta hai)."""
    return {"inline_keyboard": [
        [{"text": "✅ Apply This Style", "callback_data": f"cst:apply:{style_id}"}],
        [{"text": "🔙 Back to list", "callback_data": "cst:list"}],
    ]}


def _preview_payload(style_id: str) -> tuple:
    """
    Preview ko EXACT wahi Telegram entities (bold/blockquote/italic) ke saath
    banata hai jo asli episode-post pe lagti hain — taaki preview 100%
    accurate ho. (header/footer plain rehte hain, sirf style body pe
    entities lagti hain, offsets header-length se shift ki jaati hain.)
    """
    name = caption_style.STYLES.get(style_id, {}).get("name", style_id)
    header = f"👀 Preview: {name}\n━━━━━━━━━━━━━━━━━━━━\n\n"
    body_text, body_entities = caption_style.render_caption_entities(style_id, caption_style.PREVIEW_DATA)
    footer = (
        "\n\n━━━━━━━━━━━━━━━━━━━━\n"
        "(sample data — asli post mein anime/episode ki apni details aayengi)"
    )
    full_text = header + body_text + footer
    header_len = caption_style._utf16_len(header)

    entities = [{"type": "bold", "offset": 0, "length": caption_style._utf16_len(f"👀 Preview: {name}")}]
    for e in body_entities:
        entities.append({**e, "offset": e["offset"] + header_len})
    return full_text, entities


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
        from ..utils.bot_upload_engine import _bot_api_send_message, _bot_api_edit_message
        text, entities = _preview_payload(style_id)
        chat_id = cb.message.chat.id
        # Bot API se hi bhejo/edit karo — taaki bold/blockquote/italic entities
        # EXACT wahi dikhein jo asli episode-post pe lagti hain.
        success = await _bot_api_edit_message(
            chat_id, cb.message.id, text, entities, _preview_keyboard_dict(style_id)
        )
        if not success:
            try:
                await cb.message.delete()
            except Exception:
                pass
            sent = await _bot_api_send_message(chat_id, text, entities, _preview_keyboard_dict(style_id))
            if not sent:
                # BOT_TOKEN configured nahi hai — plain Markdown fallback (entities ke bina)
                name = caption_style.STYLES.get(style_id, {}).get("name", style_id)
                rendered = caption_style.render_caption(style_id, caption_style.PREVIEW_DATA)
                fallback_text = (
                    f"**👀 Preview: {name}**\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"{rendered}\n\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"_(sample data — asli post mein anime/episode ki apni details aayengi)_"
                )
                try:
                    await client.send_message(
                        chat_id, fallback_text,
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("✅ Apply This Style", callback_data=f"cst:apply:{style_id}")],
                            [InlineKeyboardButton("🔙 Back to list", callback_data="cst:list")],
                        ]),
                    )
                except Exception:
                    pass
        return

    if action == "apply":
        style_id = parts[2]
        await cb.answer("Apply ho raha hai...")
        await caption_style.set_style(style_id)
        name = caption_style.STYLES.get(style_id, {}).get("name", style_id)
        try:
            await cb.message.edit(f"✅ **Applied: {name}**\n\n✨ _Ho gaya — sab jagah lagega._")
        except Exception:
            pass
        return

    await cb.answer()
