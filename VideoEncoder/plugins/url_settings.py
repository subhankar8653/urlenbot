"""
URL Uploader Settings Commands
  /urlsettings  - Show current URL uploader settings
  /urlpreset    - Configure auto-processing settings (kya seedha ho, kya buttons se)
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


# ─── /urlpreset — Auto-processing settings ────────────────────────────────────
@Client.on_message(filters.command("urlpreset"))
async def url_preset_cmd(bot: Client, message: Message):
    """
    /urlpreset — URL auto-processing settings configure karo.

    Ye settings /url <link> command mein automatically apply honge
    (bina buttons ke). Manual buttons ke liye /url <link> -vt use karo.
    """
    c = await check_chat(message, chat="Both")
    if not c:
        return
    await AddUserToDatabase(bot, message)
    await _show_preset_panel(message, message.from_user.id, is_new=True)


async def _show_preset_panel(event, user_id: int, is_new: bool = False):
    """Auto-processing preset panel dikhao."""
    auto = await db.get_url_auto_settings(user_id)

    def tick(key):
        return "✅" if auto.get(key) else "❌"

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                f"{tick('rm_sub')} Remove Subtitles",
                callback_data=f"urlp_toggle_rmsub_{user_id}"
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
        f"• Remove Audio: <b>{'ON' if auto.get('rm_audio') else 'OFF'}</b>\n"
        f"• Hindi Audio Only: <b>{'ON' if auto.get('hindi_only') else 'OFF'}</b>\n"
        f"  ↳ (Remove Audio ON ho toh Hindi Only ignore hogi)\n"
        f"• Name Swap: <b>{'ON' if auto.get('name_swap') else 'OFF'}</b>\n"
        f"  ↳ (Rules set karo: /addswap)\n"
        f"• Apply Metadata: <b>{'ON' if auto.get('apply_metadata') else 'OFF'}</b>\n"
        f"  ↳ (Metadata set karo: /urlsettings)\n"
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
    """URL preset toggle callbacks."""
    parts = cb.data.split("_")
    # urlp_toggle_<key>_<user_id>
    if len(parts) < 4:
        await cb.answer()
        return

    action = parts[1]   # "toggle"
    key_raw = parts[2]  # "rmsub", "rmaudio", "hindionly", "nameswap", "metadata"
    try:
        owner_id = int(parts[3])
    except ValueError:
        await cb.answer()
        return

    if cb.from_user.id != owner_id:
        await cb.answer("❌ Ye tumhara nahi hai!", show_alert=True)
        return

    # Map short key to db key
    key_map = {
        "rmsub":     "rm_sub",
        "rmaudio":   "rm_audio",
        "hindionly":  "hindi_only",
        "nameswap":  "name_swap",
        "metadata":  "apply_metadata",
    }
    db_key = key_map.get(key_raw)
    if not db_key:
        await cb.answer("Unknown key", show_alert=True)
        return

    auto = await db.get_url_auto_settings(owner_id)
    auto[db_key] = not auto.get(db_key, False)

    # Hindi Only aur Remove Audio ek saath ON nahi ho sakte (conflicting)
    if db_key == "rm_audio" and auto["rm_audio"]:
        auto["hindi_only"] = False
    elif db_key == "hindi_only" and auto["hindi_only"]:
        auto["rm_audio"] = False

    await db.set_url_auto_settings(owner_id, auto)
    await cb.answer(f"{'✅ ON' if auto[db_key] else '❌ OFF'}")
    await _show_preset_panel(cb.message, owner_id, is_new=False)


# ─── /urlsettings ─────────────────────────────────────────────────────────────
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
    auto = await db.get_url_auto_settings(user_id)

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

    auto_text = (
        f"  Remove Subs: {'✅' if auto.get('rm_sub') else '❌'} | "
        f"Remove Audio: {'✅' if auto.get('rm_audio') else '❌'}\n"
        f"  Hindi Only: {'✅' if auto.get('hindi_only') else '❌'} | "
        f"Name Swap: {'✅' if auto.get('name_swap') else '❌'} | "
        f"Metadata: {'✅' if auto.get('apply_metadata') else '❌'}"
    )

    text = (
        "<b>⚙️ URL Uploader Settings</b>\n\n"
        "<b>🤖 Auto-Processing:</b>\n"
        f"{auto_text}\n\n"
        "<b>🏷️ Saved Metadata:</b>\n"
        f"{meta_text}\n\n"
        "<b>🔄 Name Swap Rules:</b>\n"
        f"{swap_text}\n\n"
        "<b>Commands:</b>\n"
        "• <code>/url &lt;link&gt;</code> – Auto-process\n"
        "• <code>/url &lt;link&gt; -vt</code> – Manual buttons\n"
        "• <code>/url &lt;link&gt; -e</code> – Unzip + auto-process\n"
        "• <code>/urlpreset</code> – Configure auto-processing\n"
        "• <code>/addswap &lt;from&gt; &lt;to&gt;</code> – Add swap rule\n"
        "• <code>/clearmeta</code> – Reset saved metadata"
    )
    kb = InlineKeyboardMarkup([
        [
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

    elif action == "preset":
        await cb.answer()
        await _show_preset_panel(cb.message, owner_id, is_new=False)


# ─── /metadata — Mirror Leech style full metadata settings ───────────────────
@Client.on_message(filters.command("metadata"))
async def metadata_cmd(bot: Client, message: Message):
    """Full metadata settings — Mirror Leech jaisi."""
    c = await check_chat(message, chat="Both")
    if not c:
        return
    await AddUserToDatabase(bot, message)
    await _show_metadata_panel(message, message.from_user.id, is_new=True)


async def _show_metadata_panel(event, user_id: int, is_new: bool = False):
    meta = await db.get_full_metadata(user_id)

    status = "✅ Enabled" if meta.get('enabled') else "❌ Disabled"
    strip = "✅ ON" if meta.get('strip_attachments') else "❌ OFF"
    clear = "✅ ON" if meta.get('clear_metadata') else "❌ OFF"

    text = (
        "<b>🏷️ Metadata Settings</b>\n\n"
        f"• Status: <b>{status}</b>\n"
        f"• Video Title: <code>{meta.get('video_title') or 'not set'}</code>\n"
        f"• Audio Title: <code>{meta.get('audio_title') or 'not set'}</code>\n"
        f"  ↳ <i>{{audiolang}} = auto detect (Hindi, Japanese...)</i>\n"
        f"• Subtitle Title: <code>{meta.get('subtitle_title') or 'not set'}</code>\n"
        f"  ↳ <i>{{sublang}} = auto detect</i>\n"
        f"• Comment: <code>{meta.get('comment') or 'not set'}</code>\n"
        f"• Strip Attachments: <b>{strip}</b>\n"
        f"• Clear Metadata: <b>{clear}</b>\n\n"
        "<b>Commands:</b>\n"
        "• <code>/setmeta video &lt;title&gt;</code> — Video stream title\n"
        "• <code>/setmeta audio &lt;title&gt;</code> — Audio stream title\n"
        "• <code>/setmeta sub &lt;title&gt;</code> — Subtitle stream title\n"
        "• <code>/setmeta comment &lt;text&gt;</code> — File comment\n"
        "• <code>/setmeta reset</code> — Reset to defaults\n"
    )

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                f"{'✅ Disable' if meta.get('enabled') else '❌ Enable'} Metadata",
                callback_data=f"meta_toggle_enabled_{user_id}"
            )
        ],
        [
            InlineKeyboardButton(
                f"Strip Attachments: {'✅' if meta.get('strip_attachments') else '❌'}",
                callback_data=f"meta_toggle_strip_{user_id}"
            ),
            InlineKeyboardButton(
                f"Clear Metadata: {'✅' if meta.get('clear_metadata') else '❌'}",
                callback_data=f"meta_toggle_clear_{user_id}"
            ),
        ],
        [InlineKeyboardButton("❌ Close", callback_data="closeMeh")],
    ])

    if is_new:
        await event.reply(text, reply_markup=kb)
    else:
        try:
            await event.edit(text, reply_markup=kb)
        except Exception:
            pass


@Client.on_message(filters.command("setmeta"))
async def setmeta_cmd(bot: Client, message: Message):
    """/setmeta video <title> | audio <title> | sub <title> | comment <text> | reset"""
    c = await check_chat(message, chat="Both")
    if not c:
        return
    await AddUserToDatabase(bot, message)

    args = message.command[1:]
    if len(args) < 1:
        await message.reply(
            "Usage:\n"
            "• <code>/setmeta video Sbanime hindi</code>\n"
            "• <code>/setmeta audio {audiolang}</code>\n"
            "• <code>/setmeta sub {sublang}</code>\n"
            "• <code>/setmeta comment Some comment</code>\n"
            "• <code>/setmeta reset</code>"
        )
        return

    user_id = message.from_user.id
    field = args[0].lower()
    value = " ".join(args[1:]).strip() if len(args) > 1 else ""

    if field == "reset":
        await db.set_full_metadata(user_id, {
            'enabled': False,
            'video_title': 'Sbanime hindi',
            'audio_title': '{audiolang}',
            'subtitle_title': '{sublang}',
            'comment': '',
            'strip_attachments': False,
            'clear_metadata': False,
        })
        await message.reply("✅ Metadata defaults restored!")
        return

    field_map = {
        'video': 'video_title',
        'audio': 'audio_title',
        'sub': 'subtitle_title',
        'subtitle': 'subtitle_title',
        'comment': 'comment',
    }

    db_key = field_map.get(field)
    if not db_key:
        await message.reply(f"❌ Unknown field: <code>{field}</code>\nOptions: video, audio, sub, comment, reset")
        return

    meta = await db.get_full_metadata(user_id)
    meta[db_key] = value
    await db.set_full_metadata(user_id, meta)
    await message.reply(f"✅ <b>{field.title()} title</b> set to: <code>{value or '(empty)'}</code>")


@Client.on_callback_query(filters.regex(r"^meta_toggle_"))
async def meta_toggle_cb(bot: Client, cb: CallbackQuery):
    parts = cb.data.split("_")
    # meta_toggle_<key>_<user_id>
    if len(parts) < 4:
        await cb.answer()
        return

    key = parts[2]
    try:
        owner_id = int(parts[3])
    except ValueError:
        await cb.answer()
        return

    if cb.from_user.id != owner_id:
        await cb.answer("❌ Ye tumhara nahi hai!", show_alert=True)
        return

    key_map = {
        'enabled': 'enabled',
        'strip': 'strip_attachments',
        'clear': 'clear_metadata',
    }
    db_key = key_map.get(key)
    if not db_key:
        await cb.answer("Unknown key")
        return

    meta = await db.get_full_metadata(owner_id)
    meta[db_key] = not meta.get(db_key, False)
    await db.set_full_metadata(owner_id, meta)
    await cb.answer(f"{'✅ ON' if meta[db_key] else '❌ OFF'}")
    await _show_metadata_panel(cb.message, owner_id, is_new=False)
