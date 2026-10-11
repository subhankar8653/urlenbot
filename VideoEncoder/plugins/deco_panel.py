"""
deco_panel.py
=============
/customise ke andar Border + Season Stickers ka panel (add / edit / delete / preview).

  Customise → 🎟 Border & Stickers
      🖼 Border Line      — set/replace, preview, delete
      🎟 Season Stickers  — season-wise add, replace, preview, delete, clear all

Data wahi hai jo /border aur /season_sticker (bot_upload.py) use karte hain
(db: bot_border, bot_season_stickers), aur auto-upload isi se border / season
sticker bhejta hai. Panel se kiya change SAARE owner/sudo accounts ke liye
ek hi jagah (single source of truth) save hota hai, taaki edit/delete
sach mein asar kare.

Season stickers list mein index = season-1. Beech ka season delete karne par
baaki seasons ka number na khisakne ke liye uski jagah khali placeholder
({"file_id": None, "type": "empty"}) rakhi jaati hai.
"""

from pyrogram import Client, filters, StopPropagation
from pyrogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)

from .. import LOGGER, owner, sudo_users
from ..utils.database.access_db import db

_SESS: dict = {}          # user_id → {"mode": "border"|"season", "idx": int}
_EMPTY = {"file_id": None, "type": "empty"}


def clear_session(user_id: int):
    _SESS.pop(user_id, None)


def _is_auth(user_id: int) -> bool:
    return user_id in owner or user_id in sudo_users


def _auth_ids() -> list:
    seen, out = set(), []
    for uid in list(owner) + list(sudo_users):
        if uid not in seen:
            seen.add(uid)
            out.append(uid)
    return out


def _ok(item) -> bool:
    return bool(item and item.get("file_id"))


def _kind(item) -> str:
    return "sticker" if (item or {}).get("type") == "sticker" else "photo"


# ───────────── storage (saare auth users ke across) ─────────────
async def _get_border():
    for uid in _auth_ids():
        try:
            b = await db.get_bot_border(uid)
            if _ok(b):
                return b
        except Exception:
            continue
    return None


async def _get_stickers() -> list:
    for uid in _auth_ids():
        try:
            lst = await db.get_season_stickers(uid)
            if lst:
                return list(lst)
        except Exception:
            continue
    return []


async def _save_border(acting: int, file_id: str, typ: str):
    for uid in _auth_ids():
        if uid != acting:
            await db.clear_bot_border(uid)
    await db.set_bot_border(acting, file_id, typ)


async def _clear_border():
    for uid in _auth_ids():
        await db.clear_bot_border(uid)


async def _save_stickers(acting: int, lst: list):
    while lst and not _ok(lst[-1]):      # peeche ke khali placeholders hata do
        lst.pop()
    for uid in _auth_ids():
        if uid != acting:
            await db.set_season_stickers(uid, [])
    await db.set_season_stickers(acting, lst)


# ───────────── UI helpers ─────────────
def _back(to_open: str, label: str):
    return [
        InlineKeyboardButton(label, callback_data=f"cmz:open:{to_open}"),
        InlineKeyboardButton("🏠 Menu", callback_data="cmz:menu"),
    ]


async def _edit(client: Client, cb: CallbackQuery, text: str, kb: InlineKeyboardMarkup):
    from .customise import _safe_edit
    await _safe_edit(client, cb, text, kb)


async def _send_item(client: Client, chat_id: int, item: dict):
    if _kind(item) == "sticker":
        await client.send_sticker(chat_id, item["file_id"])
    else:
        await client.send_photo(chat_id, item["file_id"])


# ───────────── Border panel ─────────────
async def render_border(client: Client, cb: CallbackQuery):
    from .customise import _CUR_SECTION
    _CUR_SECTION.set("border_panel")
    clear_session(cb.from_user.id)
    border = await _get_border()
    state = f"✅ Set hai ({_kind(border)})" if border else "❌ Set nahi hai"
    text = (
        "🖼 **Border Line**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Abhi: **{state}**\n\n"
        "Ye har episode ki saari qualities upload hone ke baad, aur season intro "
        "(details post ke baad) channel mein bheja jaata hai."
    )
    rows = [[InlineKeyboardButton("✏️ Replace Border" if border else "➕ Set Border",
                                  callback_data="cmz:deco:bset")]]
    if border:
        rows.append([
            InlineKeyboardButton("👁 Preview", callback_data="cmz:deco:bprev"),
            InlineKeyboardButton("🗑 Delete", callback_data="cmz:deco:bdel"),
        ])
    from .customise import _back_row
    rows.append(_back_row())
    await _edit(client, cb, text, InlineKeyboardMarkup(rows))


# ───────────── Season stickers panel ─────────────
async def render_season(client: Client, cb: CallbackQuery):
    from .customise import _CUR_SECTION
    _CUR_SECTION.set("season_panel")
    clear_session(cb.from_user.id)
    stickers = await _get_stickers()
    n_set = sum(1 for s in stickers if _ok(s))
    text = (
        "🎟 **Season Stickers**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Total saved: **{n_set}** season(s)\n\n"
        "Season ka pehla episode upload hone se pehle us season ka sticker "
        "channel mein jaata hai (details post → border → season sticker).\n\n"
        "Kisi season par tap karke use dekho / badlo / hatao. ✅ = set, ⬜ = khali."
    )
    rows, row = [], []
    for i, s in enumerate(stickers, 1):
        row.append(InlineKeyboardButton(
            f"S{i:02d} {'✅' if _ok(s) else '⬜'}", callback_data=f"cmz:deco:sopen:{i}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(f"➕ Season {len(stickers) + 1} add karo",
                                      callback_data="cmz:deco:sadd")])
    if stickers:
        rows.append([InlineKeyboardButton("🗑 Sab Clear karo", callback_data="cmz:deco:sclr")])
    from .customise import _back_row
    rows.append(_back_row())
    await _edit(client, cb, text, InlineKeyboardMarkup(rows))


async def _render_one(client: Client, cb: CallbackQuery, n: int):
    clear_session(cb.from_user.id)
    stickers = await _get_stickers()
    item = stickers[n - 1] if 1 <= n <= len(stickers) else None
    has = _ok(item)
    state = f"✅ Set hai ({_kind(item)})" if has else "⬜ Khali (set nahi)"
    text = (
        f"🎟 **Season {n} Sticker**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Abhi: **{state}**"
    )
    rows = [[InlineKeyboardButton("✏️ Replace" if has else "➕ Set",
                                  callback_data=f"cmz:deco:sset:{n}")]]
    if has:
        rows.append([
            InlineKeyboardButton("👁 Preview", callback_data=f"cmz:deco:sprev:{n}"),
            InlineKeyboardButton("🗑 Delete", callback_data=f"cmz:deco:sdel:{n}"),
        ])
    rows.append(_back("season_panel", "⬅️ Seasons"))
    await _edit(client, cb, text, InlineKeyboardMarkup(rows))


async def _ask_media(client: Client, cb: CallbackQuery, mode: str, idx: int, title: str, back_open: str):
    _SESS[cb.from_user.id] = {"mode": mode, "idx": idx}
    text = (
        f"**{title}**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Ab sticker (ya photo) bhejo 👇"
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cmz:open:{back_open}")]])
    await _edit(client, cb, text, kb)


async def _confirm(client: Client, cb: CallbackQuery, text: str, yes: str, no_open: str):
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Haan, delete", callback_data=yes),
        InlineKeyboardButton("❌ Nahi", callback_data=f"cmz:open:{no_open}"),
    ]])
    await _edit(client, cb, text, kb)


# ───────────── callbacks:  cmz:deco:<sub>[:n] ─────────────
async def handle(client: Client, cb: CallbackQuery, args: list):
    uid = cb.from_user.id
    sub = args[0] if args else ""
    n = int(args[1]) if len(args) > 1 and args[1].isdigit() else 0

    # ── border ──
    if sub == "bset":
        await cb.answer()
        await _ask_media(client, cb, "border", 0, "🖼 Border set karo", "border_panel")
        return
    if sub == "bprev":
        b = await _get_border()
        if not b:
            await cb.answer("Border set nahi hai.", show_alert=True)
            return
        await cb.answer()
        try:
            await _send_item(client, cb.message.chat.id, b)
        except Exception as e:
            LOGGER.error(f"[DecoPanel] border preview failed: {e}")
        return
    if sub == "bdel":
        await cb.answer()
        await _confirm(client, cb, "🗑 **Border delete kar du?**\n\nIske baad episodes ke baad border nahi jaayega.",
                       "cmz:deco:bdelok", "border_panel")
        return
    if sub == "bdelok":
        await _clear_border()
        await cb.answer("🗑 Border delete ho gaya")
        await render_border(client, cb)
        return

    # ── seasons ──
    if sub == "sadd":
        stickers = await _get_stickers()
        nxt = len(stickers) + 1
        await cb.answer()
        await _ask_media(client, cb, "season", nxt, f"🎟 Season {nxt} ka sticker bhejo", "season_panel")
        return
    if sub == "sopen" and n:
        await cb.answer()
        await _render_one(client, cb, n)
        return
    if sub == "sset" and n:
        await cb.answer()
        await _ask_media(client, cb, "season", n, f"🎟 Season {n} ka sticker bhejo", "season_panel")
        return
    if sub == "sprev" and n:
        stickers = await _get_stickers()
        item = stickers[n - 1] if 1 <= n <= len(stickers) else None
        if not _ok(item):
            await cb.answer("Is season ka sticker set nahi hai.", show_alert=True)
            return
        await cb.answer()
        try:
            await _send_item(client, cb.message.chat.id, item)
        except Exception as e:
            LOGGER.error(f"[DecoPanel] season preview failed: {e}")
        return
    if sub == "sdel" and n:
        await cb.answer()
        await _confirm(client, cb, f"🗑 **Season {n} ka sticker delete kar du?**",
                       f"cmz:deco:sdelok:{n}", "season_panel")
        return
    if sub == "sdelok" and n:
        stickers = await _get_stickers()
        if 1 <= n <= len(stickers):
            if n == len(stickers):
                stickers.pop()
            else:
                stickers[n - 1] = dict(_EMPTY)   # baaki seasons ka number na khisake
            await _save_stickers(uid, stickers)
        await cb.answer("🗑 Delete ho gaya")
        await render_season(client, cb)
        return
    if sub == "sclr":
        await cb.answer()
        await _confirm(client, cb, "🗑 **Saare season stickers delete kar du?**",
                       "cmz:deco:sclrok", "season_panel")
        return
    if sub == "sclrok":
        await _save_stickers(uid, [])
        await cb.answer("🗑 Sab clear ho gaya")
        await render_season(client, cb)
        return

    await cb.answer()


# ───────────── media input (sticker / photo) ─────────────
# group=-3: bot_upload.py ke /border-/season_sticker handlers (group 0) se
# pehle chalta hai; sirf tab consume karta hai jab user is panel ke input
# mode mein ho — warna kuch nahi karta aur baaki handlers normal chalte hain.
@Client.on_message((filters.sticker | filters.photo) & filters.private, group=-3)
async def deco_media_input(client: Client, message: Message):
    if not message.from_user:
        return
    uid = message.from_user.id
    sess = _SESS.get(uid)
    if not sess or not _is_auth(uid):
        return

    if message.sticker:
        item = {"file_id": message.sticker.file_id, "type": "sticker"}
    else:
        item = {"file_id": message.photo.file_id, "type": "photo"}

    try:
        if sess["mode"] == "border":
            await _save_border(uid, item["file_id"], item["type"])
            _SESS.pop(uid, None)
            await message.reply(
                "✅ **Border save ho gaya!**",
                reply_markup=InlineKeyboardMarkup([_back("border_panel", "⬅️ Border panel")]),
            )
        else:
            n = int(sess.get("idx") or 1)
            stickers = await _get_stickers()
            while len(stickers) < n - 1:
                stickers.append(dict(_EMPTY))
            if n - 1 < len(stickers):
                stickers[n - 1] = item
            else:
                stickers.append(item)
            await _save_stickers(uid, stickers)
            _SESS.pop(uid, None)
            await message.reply(
                f"✅ **Season {n} ka sticker save ho gaya!**",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"➕ Season {n + 1} add karo", callback_data="cmz:deco:sadd")],
                    _back("season_panel", "⬅️ Seasons"),
                ]),
            )
    except Exception as e:
        LOGGER.error(f"[DecoPanel] save failed: {e}")
        await message.reply("❌ Save nahi ho paya, dobara try karo.")
    raise StopPropagation
