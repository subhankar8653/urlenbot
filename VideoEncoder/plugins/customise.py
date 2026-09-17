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
    "🛠 **Customise — Auto Upload Settings**\n"
    "━━━━━━━━━━━━━━━━━━━━\n\n"
    "Neeche se jo bhi customise karna hai usko tap karo — har button ke "
    "naam se hi pata chal jaayega woh kis cheez ke liye hai:\n\n"
    "🎬 **Episode Caption Style** — jab video upload hota hai, uski "
    "caption kaisi dikhegi\n"
    "🖌 **Update-Post Caption Style** — Update Channel pe jaane waale "
    "post ki caption kaisi dikhegi\n"
    "🖼 **Thumbnail Band Style** — auto-thumbnail ke niche wale "
    "band/box ka look\n"
    "🔗 **How To Get Link** — update-post ke niche wali clickable line\n"
    "🔘 **Update-Post Buttons** — \"Kaise Dekhein\" / \"Join Backup\" "
    "wale default buttons\n"
    "📋 **Update-Post List** — saare saved anime entries edit karo\n"
    "📢 **Update Channels** — kaunse channels pe post jaata hai + on/off\n\n"
    "> ℹ️ _Yeh sab GLOBAL (bot-wide) settings hain._"
)


def _menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "🎬 Episode Caption Style (video upload caption)",
            callback_data="cmz:open:caption_style",
        )],
        [InlineKeyboardButton(
            "🖌 Update-Post Caption Style (update channel post)",
            callback_data="cmz:open:update_post_style",
        )],
        [InlineKeyboardButton(
            "🖼 Thumbnail Band Style (thumbnail ka box/band)",
            callback_data="cmz:open:setpic_style",
        )],
        [InlineKeyboardButton(
            "🔗 How To Get Link (post ke neeche wali line)",
            callback_data="cmz:open:how_to_get_link",
        )],
        [InlineKeyboardButton(
            "🔘 Update-Post Buttons (Kaise Dekhein / Join Backup)",
            callback_data="cmz:open:update_post_button",
        )],
        [InlineKeyboardButton(
            "📋 Update-Post List (saved anime entries edit karo)",
            callback_data="cmz:open:update_post_list",
        )],
        [InlineKeyboardButton(
            "📢 Update Channels (post kahan jaata hai + on/off)",
            callback_data="cmz:open:update_channels",
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
        from ..utils import update_post_style as ups_utils
        current = await ups_utils.get_how_to_get_link()
        if current:
            text = (
                f"🔗 **How to get link — abhi set hai:**\n`{current}`\n\n"
                "Update karna ho toh: `/how_to_get_link <naya link>`\n"
                "Hatana ho toh: `/how_to_get_link remove`"
            )
        else:
            text = (
                "📭 **How to get link** abhi set nahi hai.\n\n"
                "Set karo: `/how_to_get_link <link>`\n\n"
                "_Set hone ke baad, styled update-posts (Update-Post "
                "Caption Style ke 1-10 waale styles) ke neeche ek "
                "clickable_ **➥ ʜᴏᴡ ᴛᴏ ɢᴇᴛ ʟɪɴᴋ** _line add ho jaayegi. "
                "\"Default\" style mein nahi aati._"
            )
        await _safe_edit(client, cb, text, InlineKeyboardMarkup([_back_row()]))
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

    await cb.answer("Yeh section abhi available nahi hai.", show_alert=True)
