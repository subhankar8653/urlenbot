"""
update_channels_panel.py
========================
/customise → 🤖 Auto Monitor → 📢 Main Channel Post Channels

Button-driven panel (purane /update_channel, /update_channel_list,
/delete_update_channel commands ka replacement — woh commands abhi bhi chalte hain):

    • Posting ON/OFF toggle
    • Har channel ek button → detail → 🗑 Remove (confirm ke saath)
    • ➕ Add Channel → sirf channel ka ID bhejo (ya channel ki koi post forward karo);
      bot admin-check karke add kar deta hai
"""

from pyrogram import Client, filters, StopPropagation
from pyrogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)

from .. import LOGGER, owner, sudo_users

_SESS: dict = {}          # user_id → {"mode": "add"}


def clear_session(user_id: int):
    _SESS.pop(user_id, None)


def _is_auth(user_id: int) -> bool:
    return user_id in owner or user_id in sudo_users


def _short(title: str, n: int = 34) -> str:
    title = (title or "Unknown").strip()
    return title if len(title) <= n else title[: n - 1] + "…"


async def _edit(client: Client, cb: CallbackQuery, text: str, kb: InlineKeyboardMarkup):
    from .customise import _safe_edit
    await _safe_edit(client, cb, text, kb)


def _back(label: str = "⬅️ Channels") -> list:
    return [
        InlineKeyboardButton(label, callback_data="cmz:open:update_channels"),
        InlineKeyboardButton("🏠 Menu", callback_data="cmz:menu"),
    ]


# ───────────── main screen ─────────────
async def render_main(client: Client, cb: CallbackQuery):
    from .customise import _CUR_SECTION, _back_row
    from .update_channel import _get_update_channels, _get_update_toggle
    _CUR_SECTION.set("update_channels")
    clear_session(cb.from_user.id)
    channels = await _get_update_channels()
    on = await _get_update_toggle()
    text = (
        f"📢 **Main Channel Post — Channels ({len(channels)})**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Posting: **{'🟢 ON' if on else '🔴 OFF'}**\n\n"
        "Registered anime ka update post in channels mein jaata hai. "
        "Channel par tap karke detail dekho / remove karo, ya ➕ se naya channel jodo."
        + ("" if channels else "\n\n📭 Abhi koi channel add nahi hai.")
    )
    rows = [[InlineKeyboardButton(
        "🟢 Posting ON — tap karke OFF karo" if on else "🔴 Posting OFF — tap karke ON karo",
        callback_data="cmz:uc:tog")]]
    for ch in channels:
        rows.append([InlineKeyboardButton(
            f"📢 {_short(ch.get('channel_title'))}",
            callback_data=f"cmz:uc:open:{ch.get('channel_id')}")])
    rows.append([InlineKeyboardButton("➕ Add Channel", callback_data="cmz:uc:add")])
    rows.append(_back_row())
    await _edit(client, cb, text, InlineKeyboardMarkup(rows))


async def _find(cid: int):
    from .update_channel import _get_update_channels
    for ch in await _get_update_channels():
        if ch.get("channel_id") == cid:
            return ch
    return None


async def _render_detail(client: Client, cb: CallbackQuery, cid: int):
    ch = await _find(cid)
    if not ch:
        await cb.answer("Channel list mein nahi mila.", show_alert=True)
        await render_main(client, cb)
        return
    text = (
        "📢 **Update Channel**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Naam: **{ch.get('channel_title', 'Unknown')}**\n"
        f"🆔 `{cid}`"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑 Remove Channel", callback_data=f"cmz:uc:del:{cid}")],
        _back(),
    ])
    await _edit(client, cb, text, kb)


# ───────────── callbacks:  cmz:uc:<sub>[:<id>] ─────────────
async def handle(client: Client, cb: CallbackQuery, args: list):
    from .update_channel import (
        _get_update_channels, _save_update_channels, _get_update_toggle, _set_update_toggle,
    )
    sub = args[0] if args else ""
    cid = None
    if len(args) > 1 and args[1].lstrip("-").isdigit():
        cid = int(args[1])

    if sub == "tog":
        now = not await _get_update_toggle()
        await _set_update_toggle(now)
        await cb.answer("🟢 Posting ON" if now else "🔴 Posting OFF")
        await render_main(client, cb)
        return

    if sub == "add":
        _SESS[cb.from_user.id] = {"mode": "add"}
        await cb.answer()
        text = (
            "**➕ Update Channel add karo**\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "Channel ka **ID** bhejo, jaise `-1001234567890`\n"
            "_(ya us channel ki koi post yahan forward kar do)_\n\n"
            "Bot us channel mein **admin** hona chahiye."
        )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cmz:open:update_channels")]])
        await _edit(client, cb, text, kb)
        return

    if sub == "open" and cid is not None:
        await cb.answer()
        await _render_detail(client, cb, cid)
        return

    if sub == "del" and cid is not None:
        ch = await _find(cid)
        await cb.answer()
        name = (ch or {}).get("channel_title", "Unknown")
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Haan, remove", callback_data=f"cmz:uc:delok:{cid}"),
            InlineKeyboardButton("❌ Nahi", callback_data=f"cmz:uc:open:{cid}"),
        ]])
        await _edit(client, cb,
                    f"🗑 **Channel remove kar du?**\n\n📢 **{name}**\n🆔 `{cid}`\n\n"
                    "Iske baad is channel mein update post nahi jaayega.", kb)
        return

    if sub == "delok" and cid is not None:
        channels = await _get_update_channels()
        new = [c for c in channels if c.get("channel_id") != cid]
        if len(new) == len(channels):
            await cb.answer("Channel pehle hi list mein nahi hai.", show_alert=True)
        else:
            await _save_update_channels(new)
            await cb.answer("🗑 Remove ho gaya")
        await render_main(client, cb)
        return

    await cb.answer()


# ───────────── add: channel ID / forwarded post ─────────────
# group=-3: update_channel.py ke text handlers se pehle chalta hai; sirf tab
# consume karta hai jab user ➕ Add Channel input mode mein ho.
@Client.on_message((filters.text | filters.forwarded) & filters.private, group=-3)
async def uc_input(client: Client, message: Message):
    if not message.from_user:
        return
    uid = message.from_user.id
    if uid not in _SESS or not _is_auth(uid):
        return
    raw = (message.text or "").strip()
    if raw.startswith("/"):
        _SESS.pop(uid, None)       # command aaya — input mode cancel, command normal chale
        return

    from .update_channel import _get_update_channels, _save_update_channels
    back_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Aur add karo", callback_data="cmz:uc:add")],
        _back(),
    ])

    channel_id = None
    fchat = getattr(message, "forward_from_chat", None)
    if fchat is None:
        origin = getattr(message, "forward_origin", None)
        fchat = getattr(origin, "chat", None) if origin is not None else None
    if fchat is not None:
        channel_id = fchat.id
    else:
        try:
            channel_id = int(raw)
        except ValueError:
            await message.reply("❌ Valid channel ID bhejo. Format: `-100xxxxxxxxx` (ya channel ki post forward karo).")
            raise StopPropagation
    if channel_id > 0:
        await message.reply("❌ Channel ID `-100` se shuru hoti hai (negative number). Dobara bhejo.")
        raise StopPropagation

    try:
        channels = await _get_update_channels()
        if any(c.get("channel_id") == channel_id for c in channels):
            await message.reply(f"⚠️ Ye channel already list mein hai!\n\n`{channel_id}`", reply_markup=back_kb)
            _SESS.pop(uid, None)
            raise StopPropagation

        try:
            chat = await client.get_chat(channel_id)
            title = chat.title
        except Exception as e:
            await message.reply(f"❌ Channel nahi mila: `{e}`\n\nPehle bot ko channel mein admin banao, phir ID dobara bhejo.")
            raise StopPropagation
        try:
            me = await client.get_me()
            member = await client.get_chat_member(channel_id, me.id)
            if member.status.name not in ("ADMINISTRATOR", "OWNER"):
                await message.reply(f"❌ Bot **{title}** mein admin nahi hai! Admin banao, phir ID dobara bhejo.")
                raise StopPropagation
        except StopPropagation:
            raise
        except Exception as e:
            await message.reply(f"❌ Admin check fail: `{e}`")
            raise StopPropagation

        channels.append({"channel_id": channel_id, "channel_title": title})
        await _save_update_channels(channels)
        _SESS.pop(uid, None)
        await message.reply(
            f"✅ **Update Channel add ho gaya!**\n\n📢 **{title}**\n🆔 `{channel_id}`",
            reply_markup=back_kb,
        )
    except StopPropagation:
        raise
    except Exception as e:
        LOGGER.error(f"[UpdateChannelsPanel] add failed: {e}")
        await message.reply("❌ Add nahi ho paya, dobara try karo.")
    raise StopPropagation
