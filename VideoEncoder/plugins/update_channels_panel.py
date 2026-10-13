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
    from ..utils.multi_mode import is_multi
    channels = await _get_update_channels()
    on = await _get_update_toggle()
    multi = await is_multi()
    text = (
        f"📢 **Main Channel Post — Channels ({len(channels)})**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Posting: **{'🟢 ON' if on else '🔴 OFF'}**\n"
        f"Multi Mode: **{'🟢 ON' if multi else '🔴 OFF'}**\n\n"
        "Registered anime ka update post in channels mein jaata hai. "
        "Channel par tap karke detail dekho / remove karo, ya ➕ se naya channel jodo."
        + ("" if channels else "\n\n📭 Abhi koi channel add nahi hai.")
    )
    rows = [[InlineKeyboardButton(
        "🟢 Posting ON — tap karke OFF karo" if on else "🔴 Posting OFF — tap karke ON karo",
        callback_data="cmz:uc:tog")]]
    rows.append([InlineKeyboardButton(
        "🔀 Multi Mode ON — tap karke OFF karo" if multi else "🔀 Multi Mode OFF — tap karke ON karo",
        callback_data="cmz:uc:multi")])
    for i, ch in enumerate(channels, 1):
        rows.append([InlineKeyboardButton(
            f"📢 Channel {i:02d} — {_short(ch.get('channel_title'), 28)}",
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


async def _pos_of(cid: int) -> int:
    from .update_channel import _get_update_channels
    for i, ch in enumerate(await _get_update_channels(), 1):
        if ch.get("channel_id") == cid:
            return i
    return 0


async def _render_detail(client: Client, cb: CallbackQuery, cid: int):
    ch = await _find(cid)
    if not ch:
        await cb.answer("Channel list mein nahi mila.", show_alert=True)
        await render_main(client, cb)
        return
    from ..utils.multi_mode import is_multi, ch_link_idx
    multi = await is_multi()
    pos = await _pos_of(cid)
    extra = ""
    rows = [[InlineKeyboardButton("🧪 Test (post permission check)", callback_data=f"cmz:uc:test:{cid}")]]
    if multi:
        extra = (
            f"\n🔗 Watch link: **Link {ch_link_idx(ch, pos - 1) + 1}**"
            f"\n🎛 Buttons: **{'alag (channel-wise)' if ch.get('buttons') else 'default'}**"
        )
        rows.append([InlineKeyboardButton("🎛 Is channel ke Buttons edit karo", callback_data=f"cmz:pb:chan:{cid}")])
    rows.append([InlineKeyboardButton("🗑 Remove Channel", callback_data=f"cmz:uc:del:{cid}")])
    rows.append(_back())
    text = (
        f"📢 **Update Channel {pos:02d}**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Naam: **{ch.get('channel_title', 'Unknown')}**\n"
        f"🆔 `{cid}`"
        f"{extra}"
    )
    kb = InlineKeyboardMarkup(rows)
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

    if sub == "multi":
        from ..utils.multi_mode import is_multi, set_multi
        now = not await is_multi()
        await set_multi(now)
        await cb.answer("🟢 Multi Mode ON" if now else "🔴 Multi Mode OFF")
        await render_main(client, cb)
        return

    if sub == "clone":
        # Add flow ka aakhri step: kis channel ka clone banana hai (0 = naya)
        sess = _SESS.get(cb.from_user.id)
        if not sess or sess.get("mode") != "clone_pick":
            await cb.answer("⚠️ Ye step ab active nahi hai.", show_alert=True)
            return
        try:
            n = int(args[1])
        except (ValueError, IndexError):
            await cb.answer()
            return
        from .update_channel import new_channel_record
        channels = await _get_update_channels()
        if any(c.get("channel_id") == sess["cid"] for c in channels):
            _SESS.pop(cb.from_user.id, None)
            await cb.answer("Ye channel pehle hi add hai.", show_alert=True)
            await render_main(client, cb)
            return
        rec, err = new_channel_record(channels, sess["cid"], sess["title"], n)
        if err:
            await cb.answer(err, show_alert=True)
            return
        channels.append(rec)
        await _save_update_channels(channels)
        _SESS.pop(cb.from_user.id, None)
        await cb.answer("✅ Channel add ho gaya")
        clone_line = f"\n🔁 Channel {n:02d} ka clone (button-set + link same)" if n else ""
        await _edit(
            client, cb,
            f"✅ **Update Channel add ho gaya!**\n\n📢 **{sess['title']}** (Channel {len(channels):02d})\n"
            f"🆔 `{sess['cid']}`{clone_line}",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Aur add karo", callback_data="cmz:uc:add")],
                _back(),
            ]),
        )
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

    if sub == "test" and cid is not None:
        # Chhota test message bhejo aur turant delete — poora asli reason dikhao
        try:
            m = await client.send_message(cid, "🧪 test")
            try:
                await client.delete_messages(cid, m.id)
            except Exception:
                pass
            await cb.answer("✅ Is channel mein post ja sakta hai — sab theek hai.", show_alert=True)
        except Exception as e:
            await cb.answer(f"❌ Post nahi ja pa raha:\n{type(e).__name__}: {str(e)[:140]}", show_alert=True)
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
    if uid not in _SESS or not _is_auth(uid) or _SESS[uid].get("mode") != "add":
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
            from .update_channel import _can_post
            if not _can_post(member):
                await message.reply(
                    f"❌ Bot **{title}** mein admin hai, lekin **Post Messages** permission nahi hai! "
                    "Permission do, phir ID dobara bhejo.")
                raise StopPropagation
        except StopPropagation:
            raise
        except Exception as e:
            await message.reply(f"❌ Admin check fail: `{e}`")
            raise StopPropagation

        from ..utils.multi_mode import is_multi
        if await is_multi() and channels:
            # Multi mode: naya channel kisi purane ka clone bane? (button-set + link same)
            _SESS[uid] = {"mode": "clone_pick", "cid": channel_id, "title": title}
            rows = [[InlineKeyboardButton(
                f"📋 Channel {i:02d} jaisa — {_short(c.get('channel_title'), 22)}",
                callback_data=f"cmz:uc:clone:{i}")] for i, c in enumerate(channels, 1)]
            rows.append([InlineKeyboardButton("🆕 Naya (default buttons, agla link)", callback_data="cmz:uc:clone:0")])
            rows.append([InlineKeyboardButton("❌ Cancel", callback_data="cmz:open:update_channels")])
            await message.reply(
                f"✅ Channel mil gaya: **{title}**\n\n"
                "🔀 **Multi Mode ON** — is channel ka post kaisa ho?\n"
                "• **Clone** — kisi channel ke button-set + link jaisa\n"
                "• **Naya** — default buttons, aur anime ka agla link (Link N+1)",
                reply_markup=InlineKeyboardMarkup(rows),
            )
            raise StopPropagation
        from .update_channel import new_channel_record
        rec, _err = new_channel_record(channels, channel_id, title, 0)
        channels.append(rec)
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
