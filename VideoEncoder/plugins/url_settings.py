"""
URL Uploader Settings Commands
  /urlsettings  - Show current URL uploader settings
  /urlpreset    - Configure auto-processing settings
  /setmeta      - Interactive button panel for metadata
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
import asyncio

# ─── Active setmeta sessions ──────────────────────────────────────────────────
# { user_id: 'field_key' }  e.g. { 123: 'video_title' }
_setmeta_sessions: dict = {}


# ─── /urlpreset — Auto-processing settings ────────────────────────────────────
@Client.on_message(filters.command("urlpreset"))
async def url_preset_cmd(bot: Client, message: Message):
    c = await check_chat(message, chat="Both")
    if not c:
        return
    await AddUserToDatabase(bot, message)
    await _show_preset_panel(message, message.from_user.id, is_new=True)


async def _show_preset_panel(event, user_id: int, is_new: bool = False):
    auto = await db.get_url_auto_settings(user_id)

    def tick(key):
        return "✅" if auto.get(key) else "❌"

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                f"{tick('rm_sub')} Remove Subtitles",
                callback_data=f"urlp_toggle_rmsub_{user_id}"
            ),
            InlineKeyboardButton(
                f"{tick('eng_sub_only')} Eng Sub Only",
                callback_data=f"urlp_toggle_engsubonly_{user_id}"
            ),
        ],
        [
            InlineKeyboardButton(
                f"{tick('rm_audio')} Remove Audio",
                callback_data=f"urlp_toggle_rmaudio_{user_id}"
            ),
            InlineKeyboardButton(
                f"{tick('hindi_only')} Hindi Only",
                callback_data=f"urlp_toggle_hindionly_{user_id}"
            ),
        ],
        [
            InlineKeyboardButton(
                f"{tick('name_swap')} Name Swap",
                callback_data=f"urlp_toggle_nameswap_{user_id}"
            ),
            InlineKeyboardButton(
                f"{tick('apply_metadata')} Apply Metadata",
                callback_data=f"urlp_toggle_metadata_{user_id}"
            ),
        ],
        [InlineKeyboardButton("❌ Close", callback_data="closeMeh")],
    ])

    text = (
        "<b>⚙️ URL Auto-Processing Settings</b>\n\n"
        "Ye settings <code>/url &lt;link&gt;</code> mein auto apply hongi.\n"
        "Manual buttons ke liye <code>/url &lt;link&gt; -vt</code> use karo.\n\n"
        f"• Remove Subtitles: <b>{'ON' if auto.get('rm_sub') else 'OFF'}</b>\n"
        f"• Eng Sub Only: <b>{'ON' if auto.get('eng_sub_only') else 'OFF'}</b>\n"
        f"  ↳ (Remove Subs ON ho toh Eng Sub Only ignore hogi)\n"
        f"  ↳ (Eng sub mile toh caption mein 'Esub' add hoga)\n"
        f"• Remove Audio: <b>{'ON' if auto.get('rm_audio') else 'OFF'}</b>\n"
        f"• Hindi Audio Only: <b>{'ON' if auto.get('hindi_only') else 'OFF'}</b>\n"
        f"  ↳ (Remove Audio ON ho toh Hindi Only ignore hogi)\n"
        f"• Name Swap: <b>{'ON' if auto.get('name_swap') else 'OFF'}</b>\n"
        f"  ↳ (Rules set karo: /addswap)\n"
        f"• Apply Metadata: <b>{'ON' if auto.get('apply_metadata') else 'OFF'}</b>\n"
        f"  ↳ (Metadata set karo: /setmeta)\n"
    )

    if is_new:
        await event.reply(text, reply_markup=kb)
    else:
        try:
            await event.edit(text, reply_markup=kb)
        except Exception:
            pass


@Client.on_callback_query(filters.regex(r"^urlp_"))
async def url_preset_callbacks(bot: Client, cb: CallbackQuery):
    parts = cb.data.split("_")
    if len(parts) < 4:
        await cb.answer()
        return

    action = parts[1]
    key_raw = parts[2]
    try:
        owner_id = int(parts[3])
    except ValueError:
        await cb.answer()
        return

    if cb.from_user.id != owner_id:
        await cb.answer("❌ Ye tumhara nahi hai!", show_alert=True)
        return

    key_map = {
        "rmsub":       "rm_sub",
        "engsubonly":  "eng_sub_only",
        "rmaudio":     "rm_audio",
        "hindionly":   "hindi_only",
        "nameswap":    "name_swap",
        "metadata":    "apply_metadata",
    }
    db_key = key_map.get(key_raw)
    if not db_key:
        await cb.answer("Unknown key", show_alert=True)
        return

    auto = await db.get_url_auto_settings(owner_id)
    auto[db_key] = not auto.get(db_key, False)

    if db_key == "rm_audio" and auto["rm_audio"]:
        auto["hindi_only"] = False
    elif db_key == "hindi_only" and auto["hindi_only"]:
        auto["rm_audio"] = False
    # rm_sub ON ho toh eng_sub_only ka koi matlab nahi
    if db_key == "rm_sub" and auto["rm_sub"]:
        auto["eng_sub_only"] = False
    elif db_key == "eng_sub_only" and auto["eng_sub_only"]:
        auto["rm_sub"] = False

    await db.set_url_auto_settings(owner_id, auto)
    await cb.answer(f"{'✅ ON' if auto[db_key] else '❌ OFF'}")
    await _show_preset_panel(cb.message, owner_id, is_new=False)


# ─── /urlsettings ─────────────────────────────────────────────────────────────
@Client.on_message(filters.command("urlsettings"))
async def url_settings_cmd(bot: Client, message: Message):
    c = await check_chat(message, chat="Both")
    if not c:
        return
    await AddUserToDatabase(bot, message)

    user_id = message.from_user.id
    meta = await db.get_full_metadata(user_id)
    swap_rules = await db.get_swap(user_id)
    auto = await db.get_url_auto_settings(user_id)

    swap_text = ""
    if swap_rules:
        for k, v in swap_rules.items():
            swap_text += f"  <code>{k}</code> → <code>{v}</code>\n"
    else:
        swap_text = "  None set"

    auto_text = (
        f"  Remove Subs: {'✅' if auto.get('rm_sub') else '❌'} | "
        f"Eng Sub Only: {'✅' if auto.get('eng_sub_only') else '❌'}\n"
        f"  Remove Audio: {'✅' if auto.get('rm_audio') else '❌'} | "
        f"Hindi Only: {'✅' if auto.get('hindi_only') else '❌'}\n"
        f"  Name Swap: {'✅' if auto.get('name_swap') else '❌'} | "
        f"Metadata: {'✅' if auto.get('apply_metadata') else '❌'}"
    )

    status = "✅ Enabled" if meta.get("enabled") else "❌ Disabled"
    text = (
        "<b>⚙️ URL Uploader Settings</b>\n\n"
        "<b>🤖 Auto-Processing:</b>\n"
        f"{auto_text}\n\n"
        "<b>🏷️ Metadata:</b> " + status + "\n"
        f"  🎬 Video Title: <code>{meta.get('video_title') or 'not set'}</code>\n"
        f"  🔊 Audio Title: <code>{meta.get('audio_title') or 'not set'}</code>\n"
        f"     ↳ <i>{{audiolang}} = actual language name auto fill hoga</i>\n"
        f"  📝 Sub Title:   <code>{meta.get('subtitle_title') or 'not set'}</code>\n"
        f"  💬 Comment:     <code>{meta.get('comment') or 'not set'}</code>\n\n"
        "<b>🔄 Name Swap Rules:</b>\n"
        f"{swap_text}\n\n"
        "<b>Commands:</b>\n"
        "• <code>/setmeta</code> – Interactive metadata panel\n"
        "• <code>/urlpreset</code> – Auto-processing toggle\n"
        "• <code>/addswap &lt;from&gt; &lt;to&gt;</code> – Add swap rule\n"
        "• <code>/clearmeta</code> – Reset metadata"
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🏷️ Edit Metadata", callback_data=f"urlset_setmeta_{user_id}"),
            InlineKeyboardButton("⚙️ Auto-Processing", callback_data=f"urlset_preset_{user_id}"),
        ],
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

    elif action == "preset":
        await cb.answer()
        await _show_preset_panel(cb.message, owner_id, is_new=False)

    elif action == "setmeta":
        await cb.answer()
        await _show_setmeta_panel(cb.message, owner_id, is_new=False)


# ─── /setmeta — Interactive button panel ──────────────────────────────────────
@Client.on_message(filters.command("setmeta"))
async def setmeta_cmd(bot: Client, message: Message):
    """/setmeta → Interactive button panel for metadata."""
    c = await check_chat(message, chat="Both")
    if not c:
        return
    await AddUserToDatabase(bot, message)
    await _show_setmeta_panel(message, message.from_user.id, is_new=True)


async def _show_setmeta_panel(event, user_id: int, is_new: bool = False):
    """Interactive metadata panel — har field pe button, click karo aur naam bhejo."""
    meta = await db.get_full_metadata(user_id)

    enabled = meta.get("enabled", False)
    toggle_label = "✅ Disable Metadata" if enabled else "❌ Enable Metadata"

    def val(key):
        v = meta.get(key, "")
        return v if v else "not set"

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(toggle_label, callback_data=f"sm_toggle_enabled_{user_id}")
        ],
        [
            InlineKeyboardButton(f"🎬 Video: {val('video_title')}", callback_data=f"sm_edit_video_title_{user_id}"),
        ],
        [
            InlineKeyboardButton(f"🔊 Audio: {val('audio_title')}", callback_data=f"sm_edit_audio_title_{user_id}"),
        ],
        [
            InlineKeyboardButton(f"📝 Sub: {val('subtitle_title')}", callback_data=f"sm_edit_subtitle_title_{user_id}"),
        ],
        [
            InlineKeyboardButton(f"💬 Comment: {val('comment')}", callback_data=f"sm_edit_comment_{user_id}"),
        ],
        [
            InlineKeyboardButton("🔄 Reset Defaults", callback_data=f"sm_reset_{user_id}"),
            InlineKeyboardButton("❌ Close", callback_data="closeMeh"),
        ],
    ])

    text = (
        "<b>🏷️ Metadata Settings</b>\n\n"
        f"Status: <b>{'✅ Enabled' if enabled else '❌ Disabled'}</b>\n\n"
        f"🎬 <b>Video Title:</b> <code>{val('video_title')}</code>\n"
        f"🔊 <b>Audio Title:</b> <code>{val('audio_title')}</code>\n"
        f"   ↳ <i>{{audiolang}} likhne se actual language name auto fill hoga</i>\n"
        f"📝 <b>Sub Title:</b>   <code>{val('subtitle_title')}</code>\n"
        f"   ↳ <i>{{sublang}} likhne se subtitle language auto fill hoga</i>\n"
        f"💬 <b>Comment:</b>     <code>{val('comment')}</code>\n\n"
        "<i>Kisi bhi button pe tap karo → naam type karke bhejo → ho gaya!</i>"
    )

    if is_new:
        await event.reply(text, reply_markup=kb)
    else:
        try:
            await event.edit(text, reply_markup=kb)
        except Exception:
            pass


@Client.on_callback_query(filters.regex(r"^sm_"))
async def setmeta_callbacks(bot: Client, cb: CallbackQuery):
    """Setmeta panel ke saare callbacks."""
    parts = cb.data.split("_")
    # sm_action_field_userid  OR  sm_action_userid
    if len(parts) < 3:
        await cb.answer()
        return

    action = parts[1]

    # sm_toggle_enabled_userid  (3 parts after split: toggle, enabled, userid)
    # sm_edit_video_title_userid (4 parts: edit, video, title, userid)
    # sm_reset_userid (2 parts: reset, userid)

    if action == "toggle":
        # parts: sm toggle enabled userid
        try:
            owner_id = int(parts[3])
        except (IndexError, ValueError):
            await cb.answer()
            return
        if cb.from_user.id != owner_id:
            await cb.answer("❌ Ye tumhara nahi hai!", show_alert=True)
            return
        meta = await db.get_full_metadata(owner_id)
        meta["enabled"] = not meta.get("enabled", False)
        await db.set_full_metadata(owner_id, meta)
        await cb.answer(f"{'✅ Enabled' if meta['enabled'] else '❌ Disabled'}")
        await _show_setmeta_panel(cb.message, owner_id, is_new=False)

    elif action == "reset":
        # parts: sm reset userid
        try:
            owner_id = int(parts[2])
        except (IndexError, ValueError):
            await cb.answer()
            return
        if cb.from_user.id != owner_id:
            await cb.answer("❌ Ye tumhara nahi hai!", show_alert=True)
            return
        await db.set_full_metadata(owner_id, {
            "enabled": True,
            "video_title": "Sbanime",
            "audio_title": "{audiolang}",
            "subtitle_title": "{sublang}",
            "comment": "",
            "strip_attachments": False,
            "clear_metadata": False,
        })
        await cb.answer("✅ Defaults restored!")
        await _show_setmeta_panel(cb.message, owner_id, is_new=False)

    elif action == "edit":
        # parts: sm edit <field1> <field2_maybe> <userid>
        # field can be: video_title, audio_title, subtitle_title, comment
        # callback_data format: sm_edit_video_title_userid
        try:
            owner_id = int(parts[-1])
        except (IndexError, ValueError):
            await cb.answer()
            return
        if cb.from_user.id != owner_id:
            await cb.answer("❌ Ye tumhara nahi hai!", show_alert=True)
            return

        # field = everything between "edit" and last part (owner_id)
        field_key = "_".join(parts[2:-1])  # e.g. "video_title", "audio_title"

        field_labels = {
            "video_title":    "🎬 Video Stream Title",
            "audio_title":    "🔊 Audio Stream Title",
            "subtitle_title": "📝 Subtitle Stream Title",
            "comment":        "💬 Comment / Description",
        }
        field_hints = {
            "video_title":    "e.g. <code>Sbanime</code>",
            "audio_title":    "e.g. <code>{audiolang}</code> ya <code>Hindi</code>",
            "subtitle_title": "e.g. <code>{sublang}</code> ya <code>English</code>",
            "comment":        "e.g. <code>@SBANIME</code>",
        }

        if field_key not in field_labels:
            await cb.answer("Unknown field", show_alert=True)
            return

        # Session store karo
        _setmeta_sessions[owner_id] = field_key

        await cb.answer()
        await cb.message.edit(
            f"<b>✏️ {field_labels[field_key]}</b>\n\n"
            f"Hint: {field_hints[field_key]}\n\n"
            "Ab yeh title type karke bhejo.\n"
            "Send <code>-</code> (dash) to clear this field.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Back", callback_data=f"sm_back_{owner_id}")
            ]])
        )

    elif action == "back":
        try:
            owner_id = int(parts[2])
        except (IndexError, ValueError):
            await cb.answer()
            return
        _setmeta_sessions.pop(owner_id, None)
        await cb.answer()
        await _show_setmeta_panel(cb.message, owner_id, is_new=False)


# ─── Text handler: setmeta field input ────────────────────────────────────────
@Client.on_message(filters.text & filters.private, group=1)
async def setmeta_text_input(bot: Client, message: Message):
    """Setmeta panel ka text input — user ne field naam type kiya."""
    user_id = message.from_user.id
    field_key = _setmeta_sessions.get(user_id)
    if not field_key:
        return  # Hamara kaam nahi

    _setmeta_sessions.pop(user_id)

    value = message.text.strip()
    if value == "-":
        value = ""

    meta = await db.get_full_metadata(user_id)
    meta[field_key] = value
    await db.set_full_metadata(user_id, meta)

    field_labels = {
        "video_title":    "Video Title",
        "audio_title":    "Audio Title",
        "subtitle_title": "Subtitle Title",
        "comment":        "Comment",
    }

    try:
        await message.delete()
    except Exception:
        pass

    confirm = await message.reply(
        f"✅ <b>{field_labels.get(field_key, field_key)}</b> set to: "
        f"<code>{value or '(cleared)'}</code>"
    )
    await asyncio.sleep(2)

    # Panel wapas dikhao
    await _show_setmeta_panel(confirm, user_id, is_new=True)


# ─── /metadata — panel shortcut ───────────────────────────────────────────────
@Client.on_message(filters.command("metadata"))
async def metadata_cmd(bot: Client, message: Message):
    """/metadata → same as /setmeta."""
    c = await check_chat(message, chat="Both")
    if not c:
        return
    await AddUserToDatabase(bot, message)
    await _show_setmeta_panel(message, message.from_user.id, is_new=True)


