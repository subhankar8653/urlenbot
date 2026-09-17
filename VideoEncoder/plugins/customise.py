"""
customise.py
=============
/Customise (alias: /customise, /customize) — auto-upload se related SAARI
customization commands (caption style, update-post style, thumbnail band
style, how-to-get-link, update-post buttons, update-post list, update
channels) ka ek hi central button-menu / hub.

Client ko ab yeh yaad rakhne ki zaroorat nahi ki kaun sa command kis kaam
ke liye hai — har button ka naam hi bata deta hai ki woh kis cheez ko
customise karta hai, aur har jagah "🔙 Customise Menu" button hai taaki
kahin se bhi wapas hub pe aaya ja sake.

Yeh plugin naya business-logic nahi likhta — har button apne respective
plugin (caption_style.py, update_post_style.py, setpic_style.py,
update_channel.py, how_to_get_link ka data) ke asli functions/DB helpers
reuse karta hai, bas unhe ek button ke through open karta hai aur "back to
menu" navigation add karta hai. Isliye har original /command (jaise
/caption_style, /update_post_button, etc.) pehle jaisa hi standalone bhi
kaam karta rehta hai — kuch bhi remove/replace nahi hua.

Flow:
  /Customise           -> main hub (saari categories buttons mein)
  category tap         -> us category ka status/list dikhta hai (2nd tap
                           se aage wahi original flow chalta hai, jaise
                           /caption_style karne pe chalta)
  🔙 Customise Menu     -> wapas hub pe
  ❌ Close              -> hub message delete
"""

from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .. import owner, sudo_users


def _is_auth(user_id: int) -> bool:
    return user_id in owner or user_id in sudo_users


_MENU_TEXT = (
    "🛠 **Customise Menu**\n"
    "━━━━━━━━━━━━━━━━━━━━\n\n"
    "Jo bhi setting change karni hai, uska button neeche tap karo.\n"
    "Har button ke saath ek chhoti si line hai jo bata degi ki woh "
    "**kahan** use hota hai — koi command yaad rakhne ki zaroorat "
    "nahi.\n\n"
    "**✍️ 1. Caption & Post Look**\n"
    "_Video/post kaisa dikhega, uski settings_\n\n"
    "**🤖 2. Auto-Monitor & Channels**\n"
    "_Anime auto-detect, upload channels, kaunsa anime kahan jaata hai_\n\n"
    "**✂️ 3. Filename & Thumbnail**\n"
    "_Filename ke text replace/hide, keyword se thumbnail_\n\n"
    "**⏰ 4. Cleanup & Schedule**\n"
    "_Purane messages hatana, next-episode message, end message_\n\n"
    "**⚙️ 5. Other Settings**\n"
    "_Baaki chhoti settings (community/brand naam waghera)_\n\n"
    "> ℹ️ _Yeh sab settings poore bot ke liye hain (sabke liye same lagengi)._"
)


def _section_header(title: str) -> InlineKeyboardButton:
    # Sirf ek visual divider — tap karne se kuch nahi hota.
    return InlineKeyboardButton(title, callback_data="cmz:noop")


def _menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [_section_header("✍️ ── Caption & Post Look ──")],
        [InlineKeyboardButton(
            "🎬 Video Caption (upload hote waqt)",
            callback_data="cmz:open:caption_style",
        )],
        [InlineKeyboardButton(
            "🖌 Update-Post Caption (channel post ki caption)",
            callback_data="cmz:open:update_post_style",
        )],
        [InlineKeyboardButton(
            "🖼 Thumbnail Band Design",
            callback_data="cmz:open:setpic_style",
        )],
        [InlineKeyboardButton(
            "🔗 Post ke Niche waali Link Line",
            callback_data="cmz:open:how_to_get_link",
        )],
        [InlineKeyboardButton(
            "🔘 \"Kaise Dekhein\" / \"Join\" Buttons",
            callback_data="cmz:open:update_post_button",
        )],
        [InlineKeyboardButton(
            "📋 Saved Anime Posts (list/edit)",
            callback_data="cmz:open:update_post_list",
        )],
        [InlineKeyboardButton(
            "📢 Update Channels (kahan post jaata hai)",
            callback_data="cmz:open:update_channels",
        )],

        [_section_header("🤖 ── Auto-Monitor & Channels ──")],
        [InlineKeyboardButton(
            "🤖 Auto-Monitor Anime List",
            callback_data="cmz:open:auto_monitor",
        )],
        [InlineKeyboardButton(
            "📡 Channel ↔ Anime Link",
            callback_data="cmz:open:channel_upload",
        )],

        [_section_header("✂️ ── Filename & Thumbnail ──")],
        [InlineKeyboardButton(
            "🔄 Filename Text Replace",
            callback_data="cmz:open:swap_rules",
        )],
        [InlineKeyboardButton(
            "🚫 Filename Banned Words",
            callback_data="cmz:open:blacklist",
        )],
        [InlineKeyboardButton(
            "🖼 Keyword-wise Thumbnail",
            callback_data="cmz:open:custompic",
        )],

        [_section_header("⏰ ── Cleanup & Schedule ──")],
        [InlineKeyboardButton(
            "🗑 Auto-Delete Old Messages",
            callback_data="cmz:open:delete_message",
        )],
        [InlineKeyboardButton(
            "📅 Next-Episode / End Message",
            callback_data="cmz:open:schedule",
        )],
        [InlineKeyboardButton(
            "🏁 Upload Extras (end template/stickers)",
            callback_data="cmz:open:bot_upload_extras",
        )],

        [_section_header("⚙️ ── Other Settings ──")],
        [InlineKeyboardButton(
            "🏷 Metadata & URL Settings",
            callback_data="cmz:open:url_settings",
        )],
        [InlineKeyboardButton(
            "👥 Community / Brand Naam",
            callback_data="cmz:open:community",
        )],
        [InlineKeyboardButton("❌ Close", callback_data="cmz:close")],
    ])


def _back_row() -> list:
    return [InlineKeyboardButton("🔙 Customise Menu", callback_data="cmz:menu")]


def with_customise_back(markup) -> InlineKeyboardMarkup:
    """
    Kisi bhi existing InlineKeyboardMarkup ke neeche ek permanent
    "🔙 Customise Menu" row jod deta hai. Dusre plugins (caption_style,
    update_post_style, setpic_style, update_channel) apni list-keyboards
    mein isko use karte hain taaki hub se aaya user kahin se bhi wapas
    hub pe ja sake.
    """
    rows = list(markup.inline_keyboard) if markup else []
    rows.append(_back_row())
    return InlineKeyboardMarkup(rows)


async def _safe_edit(client: Client, cb: CallbackQuery, text: str, kb: InlineKeyboardMarkup):
    try:
        await cb.message.edit(text, reply_markup=kb)
    except Exception:
        try:
            await client.send_message(cb.message.chat.id, text, reply_markup=kb)
        except Exception:
            pass


@Client.on_message(filters.command(["Customise", "customise", "Customize", "customize"]) & filters.private)
async def cmd_customise(client: Client, message: Message):
    if not _is_auth(message.from_user.id):
        await message.reply("❌ Sirf owner/sudo hi auto-upload customise kar sakte hain.")
        return
    await message.reply(_MENU_TEXT, reply_markup=_menu_keyboard())


@Client.on_callback_query(filters.regex(r"^cmz:"))
async def customise_callback(client: Client, cb: CallbackQuery):
    if not _is_auth(cb.from_user.id):
        await cb.answer("Yeh sirf owner/sudo ke liye hai.", show_alert=True)
        return

    parts = cb.data.split(":")
    action = parts[1]

    if action == "menu":
        await cb.answer()
        await _safe_edit(client, cb, _MENU_TEXT, _menu_keyboard())
        return

    if action == "close":
        await cb.answer()
        try:
            await cb.message.delete()
        except Exception:
            pass
        return

    if action == "noop":
        # Sirf section-divider hai, ismein kuch karna nahi hai.
        await cb.answer()
        return

    if action == "open":
        section = parts[2]
        await cb.answer()
        await _open_section(client, cb, section)
        return

    if action == "rmbtn":
        await cb.answer("Remove ho raha hai...")
        from .update_channel import _save_button_defaults
        await _save_button_defaults({"kaise_dekhein": None, "join_backup": None})
        text = (
            "🗑️ **Dono default buttons remove kar diye!**\n\n"
            "Ab se update posts pe • ᴋᴀɪꜱᴇ ᴅᴇᴋʜᴇɪɴ • / • ᴊᴏɪɴ ʙᴀᴄᴋᴜᴘ • "
            "buttons nahi lagenge.\n"
            "Dobara set karna ho toh isi menu se ya `/update_post_button` "
            "se kar sakte ho."
        )
        await _safe_edit(client, cb, text, InlineKeyboardMarkup([_back_row()]))
        return

    # ── Anime list panel (reuses auto_monitor.py ka real panel) ──
    if action == "analist":
        await cb.answer()
        from .auto_monitor import _show_list_anime_panel
        await _show_list_anime_panel(client, cb.message, cb.from_user.id, page=0, is_new=False)
        return

    # ── URL auto-preset panel (reuses url_settings.py ka /urlpreset panel) ──
    if action == "urlpresetpanel":
        await cb.answer()
        from .url_settings import _show_preset_panel
        await _show_preset_panel(cb.message, cb.from_user.id, is_new=False)
        return

    # ── Metadata panel (reuses url_settings.py ka /setmeta panel) ──
    if action == "setmetapanel":
        await cb.answer()
        from .url_settings import _show_setmeta_panel
        await _show_setmeta_panel(cb.message, cb.from_user.id, is_new=False)
        return

    # ── Community naam reset to default ──
    if action == "commreset":
        from ..utils.database.access_db import db
        await db.set_community(None)
        await cb.answer("🔄 Community naam reset ho gaya!")
        await _open_section(client, cb, "community")
        return

    await cb.answer()


async def _open_section(client: Client, cb: CallbackQuery, section: str):
    user_id = cb.from_user.id

    if section == "caption_style":
        from . import caption_style as mod
        await _safe_edit(client, cb, mod._status_text(), with_customise_back(mod._list_keyboard()))
        return

    if section == "update_post_style":
        from . import update_post_style as mod
        await _safe_edit(client, cb, mod._status_text(), with_customise_back(mod._list_keyboard()))
        return

    if section == "setpic_style":
        from . import setpic_style as mod
        await _safe_edit(client, cb, mod._status_text(), with_customise_back(mod._list_keyboard()))
        return

    if section == "how_to_get_link":
        from . import how_to_get_link as mod
        text = await mod._status_text()
        kb_rows = list(mod._keyboard().inline_keyboard) + [_back_row()]
        await _safe_edit(client, cb, text, InlineKeyboardMarkup(kb_rows))
        return

    if section == "update_post_button":
        from .update_channel import _get_button_defaults, _update_post_button_sessions
        _update_post_button_sessions[user_id] = {"step": "kaise_dekhein"}
        current = await _get_button_defaults()
        cur_line = ""
        if current.get("kaise_dekhein") or current.get("join_backup"):
            cur_line = (
                f"\n**Abhi set hai:**\n"
                f"• ᴋᴀɪꜱᴇ ᴅᴇᴋʜᴇɪɴ •: `{current.get('kaise_dekhein') or '—'}`\n"
                f"• ᴊᴏɪɴ ʙᴀᴄᴋᴜᴘ •: `{current.get('join_backup') or '—'}`\n"
            )
        text = (
            f"🔘 **Update-Post Buttons**\n\n"
            f"**Step 1/2 — • ᴋᴀɪꜱᴇ ᴅᴇᴋʜᴇɪɴ • ka link reply karke bhejo:**\n"
            f"{cur_line}\n"
            "**Example:** `https://t.me/+xxxxxxxxxx`\n"
            "**Us button ko hatana ho toh:** `skip` bhejo\n\n"
            "_Ya neeche button se dono default buttons ek saath hata "
            "sakte ho._"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🗑️ Remove Both Buttons", callback_data="cmz:rmbtn")],
            _back_row(),
        ])
        await _safe_edit(client, cb, text, kb)
        return

    if section == "update_post_list":
        from .update_channel import _get_post_map, _show_update_post_list_panel
        post_map = await _get_post_map()
        if not post_map:
            text = (
                "📋 **Update-Post List**\n\n"
                "📭 Koi anime post entry save nahi hai.\n\n"
                "Add karne ke liye: `/update_post`\n\n"
                "⚠️ **Note:** Sirf yahan registered anime ka hi update "
                "channel pe post jaayega!"
            )
            await _safe_edit(client, cb, text, InlineKeyboardMarkup([_back_row()]))
            return
        # Panel apni khud ki paginated keyboard bana ke edit karta hai
        # (Customise Menu row + Close button ke saath, update_channel.py
        # mein already add kiya gaya hai).
        await _show_update_post_list_panel(cb.message, user_id, page=0, is_new=False)
        return

    if section == "update_channels":
        from .update_channel import _get_update_channels, _get_update_toggle
        channels = await _get_update_channels()
        toggle = await _get_update_toggle()
        status = "🟢 ON" if toggle else "🔴 OFF"
        if not channels:
            text = (
                f"📢 **Update Channels**\n\n"
                f"📭 Koi update channel add nahi hai.\n\n"
                f"Posting Status: **{status}**\n\n"
                f"Add karne ke liye: `/update_channel [channel_id]`"
            )
        else:
            text = f"📢 **Update Channels ({len(channels)})** | Posting: **{status}**\n\n"
            for i, ch in enumerate(channels, 1):
                text += f"`{i}.` **{ch.get('channel_title', 'Unknown')}**\n"
                text += f"    🆔 `{ch.get('channel_id')}`\n\n"
            text += "💡 Add: `/update_channel [channel_id]`\n"
            text += "💡 Remove: `/delete_update_channel [channel_id]`\n"
            text += "💡 Toggle: `/updatechannel on` | `/updatechannel off`"
        await _safe_edit(client, cb, text, InlineKeyboardMarkup([_back_row()]))
        return

    # ── 🤖 Auto-Monitor & Anime List ──
    if section == "auto_monitor":
        from .auto_monitor import _get_anime_list, _get_monitor_channel
        channel = await _get_monitor_channel()
        anime_list = await _get_anime_list()
        text = (
            "🤖 **Auto-Monitor & Anime List**\n\n"
            "RTI (source) channel pe jab bhi \"Episode X-Y Added\" type ka "
            "post aata hai, bot use automatically detect karke, anime "
            "match karke, saari qualities download + upload kar deta hai — "
            "manually kuch karne ki zaroorat nahi.\n\n"
            f"📡 **Monitor Channel ID:** `{channel}`\n"
            f"📺 **Saved Anime:** `{len(anime_list)}`\n\n"
            "**Commands:**\n"
            "• `/add_anime` — button-driven flow se naya anime add karo "
            "(channel, naam, poster, audio, interval, sab ek saath)\n"
            "• `/list_anime` — saare saved anime dekho/edit karo (niche "
            "button se bhi khul jayega)\n"
            "• `/del_anime` — kisi anime ko list se hatao\n"
            "• `/set_monitor <channel_id>` — RTI source channel badlo\n"
            "• `/monitor_status` — abhi monitor kya kar raha hai, live dekho"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 Open Anime List", callback_data="cmz:analist")],
            _back_row(),
        ])
        await _safe_edit(client, cb, text, kb)
        return

    # ── 📡 Auto Channel Upload ──
    if section == "channel_upload":
        from ..utils.database.access_db import db
        channels = await db.get_channels(user_id)
        text = (
            "📡 **Auto Channel Upload**\n\n"
            "Kisi bhi anime naam ko ek specific channel se link kar do — "
            "us anime ki files phir automatically usi channel pe jaayengi.\n\n"
            f"🔗 **Linked Channels:** `{len(channels) if channels else 0}`\n\n"
            "**Commands:**\n"
            "• `/addchannel` — naya anime ↔ channel link add karo\n"
            "• `/seechannel` — saare linked channels dekho\n"
            "• `/delchannel` — kisi link ko remove karo"
        )
        await _safe_edit(client, cb, text, InlineKeyboardMarkup([_back_row()]))
        return

    # ── 🔄 Filename Swap Rules (reuses url_upload.py ka /addswap panel) ──
    if section == "swap_rules":
        from .url_upload import _show_addswap_panel
        await _show_addswap_panel(cb.message, user_id, is_new=False)
        return

    # ── 🚫 Filename Blacklist (reuses url_upload.py ka /blacklist panel) ──
    if section == "blacklist":
        from .url_upload import _show_blacklist_panel
        await _show_blacklist_panel(cb.message, user_id, is_new=False)
        return

    # ── 🖼 Custom Thumbnails (Keyword) ──
    if section == "custompic":
        from ..utils.database.access_db import db
        pics = await db.get_all_custompics(user_id)
        text = (
            "🖼 **Custom Thumbnails (Keyword)**\n\n"
            "Kisi bhi keyword (jaise anime ka naam) ke liye ek fix "
            "thumbnail save kar do. Jab bhi upload hone waali file ke "
            "filename/caption mein wo keyword match karega, thumbnail "
            "automatically apply ho jaayegi.\n\n"
            f"🖼 **Saved Pics:** `{len(pics) if pics else 0}`\n\n"
            "**Commands:**\n"
            "• `/setpic <keyword>` — photo reply karke keyword pic save "
            "karo (bina keyword ke → default thumbnail)\n"
            "• `/listpic` — sabki list dekho\n"
            "• `/previewpic <keyword>` — us keyword ki pic dekho\n"
            "• `/deletepic <keyword>` (alias: `/delpic`) — delete karo"
        )
        await _safe_edit(client, cb, text, InlineKeyboardMarkup([_back_row()]))
        return

    # ── 🏷 Metadata & URL Auto-Settings ──
    if section == "url_settings":
        text = (
            "🏷 **Metadata & URL Auto-Settings**\n\n"
            "Ye sab settings sirf `/url <link>` (manual URL upload/"
            "download) ke auto-processing ko control karti hain:\n\n"
            "• `/urlpreset` — auto-processing options (auto-rename, "
            "auto-thumbnail, etc.) — niche button se interactive panel "
            "khulega\n"
            "• `/setmeta` — video metadata (title, author, etc.) jo har "
            "upload mein auto-apply hogi — niche button se panel khulega\n"
            "• `/urlsettings` — abhi sab kya set hai, poora summary "
            "dekho\n"
            "• `/clearmeta` — saari saved metadata clear karo"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎛 Open URL Preset Panel", callback_data="cmz:urlpresetpanel")],
            [InlineKeyboardButton("🏷 Open Metadata Panel", callback_data="cmz:setmetapanel")],
            _back_row(),
        ])
        await _safe_edit(client, cb, text, kb)
        return

    # ── 🗑 Auto Delete Old Messages ──
    if section == "delete_message":
        from .delete_msg import _get_delete_channels
        channels = await _get_delete_channels()
        text = (
            "🗑 **Auto Delete Old Messages**\n\n"
            "Jab set kiye hue channel pe koi nayi VIDEO FILE upload hoti "
            "hai, uske niche wale purane 3 messages automatically delete "
            "ho jaate hain (video khud delete nahi hoti — sirf usse pehle "
            "ke 3 messages).\n\n"
            f"📡 **Set Channels:** `{len(channels)}`\n\n"
            "**Commands:**\n"
            "• `/delete_message [channel_id]` — channel add karo (ya "
            "bina id ke → list dikhao)\n"
            "• `/delete_message_list` — saare set channels dekho\n"
            "• `/delete_message_del [number]` — channel remove karo"
        )
        await _safe_edit(client, cb, text, InlineKeyboardMarkup([_back_row()]))
        return

    # ── 📅 Schedule & End Message ──
    if section == "schedule":
        from .schedule_notify import _get_schedule_list
        slist = await _get_schedule_list()
        text = (
            "📅 **Schedule & End Message**\n\n"
            "Episode complete hone ke baad bot automatically ek schedule "
            "message post karta hai (\"Next episode on ...\"), aur series "
            "ke last episode pe end messages + saare update channels pe "
            "broadcast bhejta hai.\n\n"
            f"📋 **Saved Schedules:** `{len(slist)}`\n\n"
            "**Commands:**\n"
            "• `/schedule [days] [total_eps] [Anime Name]` — schedule set "
            "karo\n"
            "• `/schedule_list` — saare saved schedules dekho\n"
            "• `/schedule_del [Anime Name]` — schedule remove karo\n"
            "• `/end_message` — default end message set karo (`/done` se "
            "save)\n"
            "• `/end_message [Channel Name]` — sirf ek channel ke liye "
            "custom end message\n"
            "• `/end_message_preview` — abhi kya set hai dekho\n"
            "• `/end_message_del [Name]` — end message delete karo"
        )
        await _safe_edit(client, cb, text, InlineKeyboardMarkup([_back_row()]))
        return

    # ── 🏁 Upload Extras (bot_upload.py globals) ──
    if section == "bot_upload_extras":
        text = (
            "🏁 **Upload Extras** _(`/bot_upload` pipeline ke liye)_\n\n"
            "• `/set_end <template>` — end message template set karo. "
            "Placeholders: `{anime_name}`, `{season}`, `{q480}`, `{q720}`, "
            "`{q1080}`\n"
            "• `/border` — sticker bhejo phir `/done` — batch summary ke "
            "border pe use hoga\n"
            "• `/season_sticker` — har season ke liye sticker set karo "
            "(order mein bhejo, phir `/done`)\n\n"
            "_Ye teeno `/bot_upload` ke full pipeline (IMDB info → "
            "episode upload → batch links → border → summary → end "
            "message → next season sticker) mein automatically use "
            "hote hain._"
        )
        await _safe_edit(client, cb, text, InlineKeyboardMarkup([_back_row()]))
        return

    # ── 👥 Community Tag ──
    if section == "community":
        from ..utils.community import DEFAULT_COMMUNITY, get_community_name
        current = await get_community_name()
        text = (
            "👥 **Community Tag** _(bot-wide)_\n\n"
            f"Current: `{current}`\n\n"
            "Ye naam thumbnail band, auto-caption tag aur metadata title "
            "mein — manual aur auto-monitor dono uploads mein — sabki "
            "jagah use hota hai.\n\n"
            "**Command:**\n"
            "• `/community <name>` — naya naam set karo (sirf letters/"
            "numbers/underscore)\n"
            f"• `/communityclear` — default `{DEFAULT_COMMUNITY}` pe reset "
            "(niche button se bhi ho jayega)"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Reset to Default", callback_data="cmz:commreset")],
            _back_row(),
        ])
        await _safe_edit(client, cb, text, kb)
        return

    await cb.answer("Yeh section abhi available nahi hai.", show_alert=True)
