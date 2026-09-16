"""
update_post_style.py (plugin)
==============================
/update_post_style — UPDATE CHANNEL pe jaane waale post (send_update_post,
update_channel.py) ke caption ke liye 10 professional style presets +
"Default" (purana wala, jaisa abhi hai) me se ek choose karo, live preview
dekho, phir apply karo. `/caption_style` jaisa hi flow, alag DB key
('update_post_style') — GLOBAL (bot-wide), owner/sudo only, turant
effective.

Flow:
  /update_post_style  -> 10 styles + Default ki list dikhti hai
  style tap           -> us style ka live text-preview (sample data pe)
  ✅ Apply This Style  -> DB mein save + turant effective
  🔙 Back to list      -> list pe wapas
"""

from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .. import owner, sudo_users
from ..utils import update_post_style


def _is_auth(user_id: int) -> bool:
    return user_id in owner or user_id in sudo_users


def _status_text() -> str:
    style_id = update_post_style.get_current_style_id()
    name = update_post_style.STYLES.get(style_id, {}).get("name", style_id)
    return (
        "🎨 **Update-Post Style** <i>(bot-wide)</i>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Current: **{name}**\n\n"
        "Neeche se koi bhi style tap karo — pehle uska preview dikhega, "
        "phir chaaho toh apply kar sakte ho.\n\n"
        "> ℹ️ _Yeh style update channel pe jaane waale har post pe lagega._"
    )


def _list_keyboard() -> InlineKeyboardMarkup:
    current = update_post_style.get_current_style_id()

    def _label(sid: str) -> str:
        name = update_post_style.STYLES[sid]["name"]
        return f"✅ {name} (Current)" if sid == current else name

    rows = []
    ids = update_post_style.STYLE_ORDER
    for i in range(0, len(ids), 2):
        row = [
            InlineKeyboardButton(_label(sid), callback_data=f"ups:prev:{sid}")
            for sid in ids[i:i + 2]
        ]
        rows.append(row)
    rows.append([InlineKeyboardButton(
        _label(update_post_style.DEFAULT_STYLE_ID),
        callback_data=f"ups:prev:{update_post_style.DEFAULT_STYLE_ID}",
    )])
    return InlineKeyboardMarkup(rows)


def _preview_keyboard_dict(style_id: str) -> dict:
    return {"inline_keyboard": [
        [{"text": "✅ Apply This Style", "callback_data": f"ups:apply:{style_id}"}],
        [{"text": "🔙 Back to list", "callback_data": "ups:list"}],
    ]}


def _preview_payload(style_id: str) -> tuple:
    name = update_post_style.STYLES.get(style_id, {}).get("name", style_id)
    header = f"👀 Preview: {name}\n━━━━━━━━━━━━━━━━━━━━\n\n"
    footer = (
        "\n\n━━━━━━━━━━━━━━━━━━━━\n"
        "(sample data — asli post mein anime ki apni details aayengi)"
    )

    if style_id == update_post_style.DEFAULT_STYLE_ID:
        d = update_post_style.PREVIEW_DATA
        body_text = (
            f"➲ {d['anime_name']} (S - {d['season']:02d})\n"
            f"╭┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
            f"◈ Audio: {d['audio']}\n"
            f"◈ Quality: {d['quality']}\n"
            f"◈ Genres: {d['genres']}\n"
            f"╰┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
            f"➲ Episode: {d['episode']} Added!"
        )
        body_entities = []
    else:
        body_text, body_entities = update_post_style.render_update_caption_entities(
            style_id, update_post_style.PREVIEW_DATA
        )

    full_text = header + body_text + footer
    from ..utils.caption_style import _utf16_len
    header_len = _utf16_len(header)

    entities = [{"type": "bold", "offset": 0, "length": _utf16_len(f"👀 Preview: {name}")}]
    for e in body_entities:
        entities.append({**e, "offset": e["offset"] + header_len})
    return full_text, entities


@Client.on_message(filters.command("update_post_style"))
async def cmd_update_post_style(client: Client, message: Message):
    if not _is_auth(message.from_user.id):
        await message.reply("❌ Sirf owner/sudo hi update-post style badal sakte hain (yeh bot-wide setting hai).")
        return
    await message.reply(_status_text(), reply_markup=_list_keyboard())


@Client.on_callback_query(filters.regex(r"^ups:"))
async def update_post_style_callback(client: Client, cb: CallbackQuery):
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
                name = update_post_style.STYLES.get(style_id, {}).get("name", style_id)
                try:
                    await client.send_message(
                        chat_id,
                        f"**👀 Preview: {name}**\n\n_(Bot Api token missing — plain preview)_",
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("✅ Apply This Style", callback_data=f"ups:apply:{style_id}")],
                            [InlineKeyboardButton("🔙 Back to list", callback_data="ups:list")],
                        ]),
                    )
                except Exception:
                    pass
        return

    if action == "apply":
        style_id = parts[2]
        await cb.answer("Apply ho raha hai...")
        await update_post_style.set_style(style_id)
        name = update_post_style.STYLES.get(style_id, {}).get("name", style_id)
        try:
            await cb.message.edit(f"✅ **Applied: {name}**\n\n✨ _Ho gaya — update channel ke posts pe lagega._")
        except Exception:
            pass
        return

    await cb.answer()
