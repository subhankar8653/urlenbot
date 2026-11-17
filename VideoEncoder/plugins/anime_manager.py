"""
anime_manager.py
================
/customise → 🤖 Auto Monitor → 📚 Anime List  (ya  /animes , /list_anime , /update_post_list)

PEHLE do alag list thi:
    • 🤖 Auto-Monitor Anime List        (anime_monitor_list)
    • 📋 Main Channel Post Anime List   (update_post_map)
Dono lagbhag same anime ki thi => confusion. AB ek hi list hai.

Har anime pe tap karo => ek hi screen se POORA control:
    📢 Monitor Channel badlo   (bas naye channel ki ID do / forward karo)
    ✏️ Naam                    (rename — sab jagah ek saath badal jaata hai)
    🔗 Link(s)  🎙 Audio  🎭 Genres
    🖼 Post Thumbnail          (Main Channel post mein lagta hai)  — 🤖 Auto (TMDB) / 📷 Custom
    🎞 Video Thumbnail         (upload hui video file pe lagta hai) — 🤖 Auto (TMDB) / 📷 Custom
    📅 Timing                  (next episode kitne din baad) + Total Episodes
    🗑 Delete                  (is naam ka SAB kuch ek saath delete: monitor + post entry +
                                schedule/timing + video thumbnail)

Data jahan pehle se store hota hai wahin rehta hai (naya DB format nahi), isliye purana
/add_anime, auto-monitor, update-post sab bina badlaav ke chalte rehte hain.
"""

import asyncio
import html
import re

from pyrogram import Client, filters, StopPropagation, ContinuePropagation
from pyrogram.enums import ParseMode
from pyrogram.errors import MessageNotModified
from pyrogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)

from .. import LOGGER, owner, sudo_users

PAGE_SIZE = 10
_S: dict = {}          # user_id -> input session


# ─────────────────────────────────────────────
#  Small helpers
# ─────────────────────────────────────────────
def _is_auth(uid: int) -> bool:
    return uid in owner or uid in sudo_users


def _nk(s) -> str:
    """Naam ko compare-friendly banao (case / space / - / _ ignore)."""
    return re.sub(r"[\s\-_]+", " ", (s or "").lower()).strip()


def _e(v) -> str:
    return html.escape(str(v), quote=False) if v not in (None, "") else "—"


def _short(s: str, n: int = 30) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


async def _oid():
    return owner[0] if owner else None


async def _edit(msg, text: str, kb: InlineKeyboardMarkup):
    try:
        await msg.edit(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except MessageNotModified:
        pass
    except Exception as ex:
        LOGGER.warning(f"[AnimeMgr] edit error: {ex!r}")


# ─────────────────────────────────────────────
#  Data layer — 3 purani stores ko ek "item" mein jodta hai
# ─────────────────────────────────────────────
def _find_pm_key(pm: dict, name: str):
    if not name:
        return None
    direct = name.lower().strip()
    if direct in pm:
        return direct
    n = _nk(name)
    for k, v in pm.items():
        if _nk(k) == n or _nk((v or {}).get("display_name")) == n:
            return k
    return None


async def _snapshot():
    from .auto_monitor import _get_anime_list
    from .update_channel import _get_post_map
    from .schedule_notify import _get_schedule_list
    mon = await _get_anime_list()
    pm = await _get_post_map()
    sch = await _get_schedule_list()

    items, used = [], set()
    for e in mon:
        nm = e.get("anime_name") or "Unknown"
        pk = _find_pm_key(pm, nm)
        if pk:
            used.add(pk)
        items.append({"name": nm, "nk": _nk(nm), "ch": e.get("channel_id") or 0,
                      "mon": e, "pm_key": pk})
    for pk, v in pm.items():                      # sirf post-map mein hain (monitor nahi) — orphan
        if pk in used:
            continue
        nm = (v or {}).get("display_name") or pk
        items.append({"name": nm, "nk": _nk(nm), "ch": 0, "mon": None, "pm_key": pk})
    return items, mon, pm, sch


def _find_item(items: list, key):
    nk, ch = key
    for i, it in enumerate(items):
        if it["nk"] == nk and it["ch"] == ch:
            return i, it
    for i, it in enumerate(items):               # channel badal gaya ho to naam se
        if it["nk"] == nk:
            return i, it
    return None, None


def _sch_entry(sch: list, name: str):
    n = _nk(name)
    for e in sch:
        if _nk(e.get("anime_name")) == n:
            return e
    return None


async def _video_pic(name: str):
    """(keyword, file_id) — /setpic jaisa video thumbnail is naam ke liye."""
    oid = await _oid()
    if not oid:
        return None, None
    from ..utils.database.access_db import db
    pics = await db.get_all_custompics(oid) or {}
    n = _nk(name)
    for kw, fid in pics.items():
        if _nk(kw) == n:
            return kw, fid
    return None, None


def _ensure_pm(pm: dict, item: dict) -> str:
    """Item ke liye post-map entry pakki karo, uska key return karo."""
    pk = item.get("pm_key") or _find_pm_key(pm, item["name"])
    if pk and pk in pm:
        return pk
    pk = item["name"].lower().strip()
    link = (item.get("mon") or {}).get("channel_link", "")
    pm[pk] = {"display_name": item["name"], "invite_link": link,
              "audio": "Hindi ORG", "genres": "", "image": ""}
    return pk


def _mode_txt(has: bool, mode) -> str:
    if not has:
        return "❌ set nahi"
    return {"auto": "✅ 🤖 Auto (TMDB)", "custom": "✅ 📷 Custom"}.get(mode, "✅ set")


# ─────────────────────────────────────────────
#  LIST screen
# ─────────────────────────────────────────────
async def show_list(client: Client, event, page: int = 0, is_new: bool = False):
    from .auto_monitor import _monitor_channel_text
    items, *_ = await _snapshot()
    mc = await _monitor_channel_text(client)

    if not items:
        text = (f"📡 <b>Monitor Channel:</b> {mc}\n\n📭 Koi anime registered nahi hai.\n\n"
                f"➕ Naya anime add karo 👇")
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Add Anime", callback_data="amg:add")],
            [InlineKeyboardButton("🔙 Customise Menu", callback_data="cmz:menu")],
        ])
        if is_new:
            await event.reply(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        else:
            await _edit(event, text, kb)
        return

    total = len(items)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    rows = []
    for i in range(page * PAGE_SIZE, min(total, (page + 1) * PAGE_SIZE)):
        it = items[i]
        pm_ok = bool(it["pm_key"])
        flag = "✅" if (it["mon"] and pm_ok) else "⚠️"
        ch = (it["mon"] or {}).get("channel_title") or "no channel"
        label = f"{i + 1}. {flag} {it['name']} → {ch}"
        rows.append([InlineKeyboardButton(_short(label, 50), callback_data=f"amg:o:{i}:{page}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"amg:l:{page - 1}"))
    nav.append(InlineKeyboardButton(f"📄 {page + 1}/{pages}", callback_data="amg:x"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"amg:l:{page + 1}"))
    rows.append(nav)
    rows.append([InlineKeyboardButton("➕ Add Anime", callback_data="amg:add"),
                 InlineKeyboardButton("📢 Post Channels", callback_data="cmz:open:update_channels")])
    rows.append([InlineKeyboardButton("🔙 Customise Menu", callback_data="cmz:menu"),
                 InlineKeyboardButton("❌ Close", callback_data="closeMeh")])

    text = (f"📡 <b>Monitor Channel:</b> {mc}\n\n"
            f"📚 <b>Anime List ({total})</b>\n"
            f"<i>Anime pe tap karo — channel, naam, link, thumbnail, timing sab yahin se edit / delete.</i>\n"
            f"✅ = monitor + post dono set • ⚠️ = kuch adhura")
    if is_new:
        await event.reply(text, reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML)
    else:
        await _edit(event, text, InlineKeyboardMarkup(rows))


# ─────────────────────────────────────────────
#  DETAIL screen
# ─────────────────────────────────────────────
async def show_detail(client: Client, event, key, page: int, is_new: bool = False):
    from ..utils import multi_mode as _mm
    from .update_channel import _get_update_channels
    items, mon, pm, sch = await _snapshot()
    idx, it = _find_item(items, key)
    if it is None:
        await show_list(client, event, page, is_new=is_new)
        return

    mon_e = it["mon"] or {}
    pe = pm.get(it["pm_key"]) if it["pm_key"] else {}
    pe = pe or {}
    se = _sch_entry(sch, it["name"]) or {}
    _, vfid = await _video_pic(it["name"])

    interval = se.get("interval_days")
    interval_s = ("❓ Unknown" if interval == "unknown"
                  else f"{interval} din" if interval else "—")
    total_eps = se.get("total_eps") or 0

    multi = await _mm.is_multi()
    link_rows, link_txt = [], ""
    if multi:
        n_links = max(2, len(await _get_update_channels()), len(pe.get("invite_links") or []))
        links = pe.get("invite_links") or [pe.get("invite_link") or ""]
        for n in range(1, n_links + 1):
            v = links[n - 1] if n - 1 < len(links) else ""
            link_txt += f"🔗 Link {n} (Ch {n:02d}): <code>{_e(v)}</code>\n"
            link_rows.append([InlineKeyboardButton(f"🔗 Link {n}", callback_data=f"amg:f:{idx}:{page}:link{n}")])
    else:
        link_txt = f"🔗 Link: <code>{_e(pe.get('invite_link') or mon_e.get('channel_link'))}</code>\n"
        link_rows.append([InlineKeyboardButton("🔗 Link", callback_data=f"amg:f:{idx}:{page}:link1")])

    ch_line = (f"<code>{_e(mon_e.get('channel_title'))}</code> (<code>{mon_e.get('channel_id')}</code>)"
               if mon_e else "❌ set nahi (⬇️ 'Monitor Channel' se set karo)")
    text = (
        f"📺 <b>{_e(it['name'])}</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        f"📢 Monitor Channel: {ch_line}\n"
        f"{link_txt}"
        f"🎙 Audio: <code>{_e(pe.get('audio'))}</code>\n"
        f"🎭 Genres: <code>{_e(pe.get('genres'))}</code>\n"
        f"🖼 Post Thumbnail: {_mode_txt(bool(pe.get('image')), pe.get('thumb_post_mode'))}\n"
        f"🎞 Video Thumbnail: {_mode_txt(bool(vfid), pe.get('thumb_video_mode'))}\n"
        f"📅 Next Episode: <b>{interval_s}</b> • 🎬 Total Eps: <b>{total_eps or '—'}</b>"
    )
    p = f"{idx}:{page}"
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("📢 Monitor Channel badlo", callback_data=f"amg:f:{p}:ch"),
          InlineKeyboardButton("✏️ Naam", callback_data=f"amg:f:{p}:name")]]
        + link_rows
        + [[InlineKeyboardButton("🎙 Audio", callback_data=f"amg:f:{p}:audio"),
            InlineKeyboardButton("🎭 Genres", callback_data=f"amg:f:{p}:genres")],
           [InlineKeyboardButton("🖼 Post Thumbnail", callback_data=f"amg:th:{p}:p"),
            InlineKeyboardButton("🎞 Video Thumbnail", callback_data=f"amg:th:{p}:v")],
           [InlineKeyboardButton("📅 Timing / Total Eps", callback_data=f"amg:t:{p}")],
           [InlineKeyboardButton("🗑 Delete (sab kuch)", callback_data=f"amg:d:{p}")],
           [InlineKeyboardButton("⬅️ List", callback_data=f"amg:l:{page}"),
            InlineKeyboardButton("🏠 Menu", callback_data="cmz:menu")]]
    )
    if is_new:
        await event.reply(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    else:
        await _edit(event, text, kb)


def _key_of(item: dict):
    return (item["nk"], item["ch"])


async def _item_at(idx: int):
    items, *_ = await _snapshot()
    if 0 <= idx < len(items):
        return items[idx]
    return None


# ─────────────────────────────────────────────
#  Cascade ops: rename / delete / channel change
# ─────────────────────────────────────────────
async def _cascade_delete(item: dict) -> list:
    """Is naam ka sab kuch hatao. Return: kya-kya hata uska list."""
    from .auto_monitor import _get_anime_list, _save_anime_list
    from .update_channel import _get_post_map, _save_post_map
    from .schedule_notify import _get_schedule_list, _save_schedule_list
    from ..utils.database.access_db import db
    done = []
    nk = item["nk"]

    mon = await _get_anime_list()
    new_mon, removed = [], False
    for e in mon:
        same = _nk(e.get("anime_name")) == nk and (e.get("channel_id") or 0) == item["ch"]
        if same and not removed:
            removed = True
            continue
        new_mon.append(e)
    if removed:
        await _save_anime_list(new_mon)
        done.append("📡 Monitor entry")

    # Agar isi naam ka koi aur monitor entry (dusra channel) bacha hai to baaki data mat chhedo
    if any(_nk(e.get("anime_name")) == nk for e in new_mon):
        return done + ["ℹ️ Is naam ka ek aur channel-entry baaki hai — post/timing/thumbnail rakhe"]

    pm = await _get_post_map()
    pk = item.get("pm_key") or _find_pm_key(pm, item["name"])
    if pk and pk in pm:
        pm.pop(pk, None)
        await _save_post_map(pm)
        done.append("📋 Post entry (link/audio/genres/post-thumb)")

    sch = await _get_schedule_list()
    new_sch = [e for e in sch if _nk(e.get("anime_name")) != nk]
    if len(new_sch) != len(sch):
        await _save_schedule_list(new_sch)
        done.append("📅 Timing / schedule")

    oid = await _oid()
    if oid:
        pics = await db.get_all_custompics(oid) or {}
        gone = False
        for kw in list(pics.keys()):
            if _nk(kw) == nk:
                await db.del_custompic(oid, kw)
                gone = True
        if gone:
            done.append("🎞 Video thumbnail")
    return done


async def _cascade_rename(item: dict, new_name: str):
    from .auto_monitor import _get_anime_list, _save_anime_list
    from .update_channel import _get_post_map, _save_post_map
    from .schedule_notify import _get_schedule_list, _save_schedule_list
    from ..utils.database.access_db import db
    old_nk = item["nk"]

    mon = await _get_anime_list()
    for e in mon:
        if _nk(e.get("anime_name")) == old_nk:
            e["anime_name"] = new_name
    await _save_anime_list(mon)

    pm = await _get_post_map()
    pk = item.get("pm_key") or _find_pm_key(pm, item["name"])
    if pk and pk in pm:
        entry = pm.pop(pk)
        entry["display_name"] = new_name
        pm[new_name.lower().strip()] = entry
        await _save_post_map(pm)

    sch = await _get_schedule_list()
    for e in sch:
        if _nk(e.get("anime_name")) == old_nk:
            e["anime_name"] = new_name
    await _save_schedule_list(sch)

    oid = await _oid()
    if oid:
        kw, fid = await _video_pic(item["name"])
        if kw and fid:
            await db.del_custompic(oid, kw)
            await db.set_custompic(oid, new_name, fid)


# ─────────────────────────────────────────────
#  Commands
# ─────────────────────────────────────────────
@Client.on_message(filters.command(["animes", "anime_manager"]) & filters.private)
async def cmd_animes(client: Client, message: Message):
    if not _is_auth(message.from_user.id):
        return
    await show_list(client, message, 0, is_new=True)


# ─────────────────────────────────────────────
#  Callbacks
# ─────────────────────────────────────────────
_PROMPTS = {
    "ch": ("📢 <b>Monitor Channel badlo</b>",
           "Naye channel ki <b>ID</b> bhejo (<code>-100xxxxxxxxxx</code>) ya us channel ka koi message "
           "<b>forward</b> karo.\n<i>Bot us channel mein admin hona chahiye.</i>"),
    "name": ("✏️ <b>Naam badlo</b>", "Naya poora naam bhejo.\n<i>Monitor, post, timing, video-thumb sab jagah badal jaayega.</i>"),
    "audio": ("🎙 <b>Audio</b>", "Naya value bhejo, jaise: <code>Hindi ORG</code>"),
    "genres": ("🎭 <b>Genres</b>", "Naya value bhejo, jaise: <code>Action, Comedy, Romance</code>"),
    "int": ("📅 <b>Next episode kitne din baad?</b>", "Number bhejo, jaise <code>7</code> — ya <code>unknown</code>"),
    "eps": ("🎬 <b>Total Episodes</b>", "Number bhejo, jaise <code>24</code>"),
}


@Client.on_callback_query(filters.regex(r"^amg:"))
async def amg_callbacks(client: Client, cb: CallbackQuery):
    uid = cb.from_user.id
    if not _is_auth(uid):
        await cb.answer("⛔ Permission nahi hai.", show_alert=True)
        return
    parts = cb.data.split(":")
    act = parts[1] if len(parts) > 1 else ""

    if act == "x":
        await cb.answer()
        return

    # ── list / page ──
    if act == "l":
        _S.pop(uid, None)
        await cb.answer()
        await show_list(client, cb.message, int(parts[2]) if len(parts) > 2 else 0)
        return

    # ── add new anime (purana 5-step flow) ──
    if act == "add":
        from .auto_monitor import _add_anime_sessions
        _add_anime_sessions[uid] = {"step": "await_start"}
        await cb.answer()
        await cb.message.reply(
            "🌟 ➕ Add New Anime\n━━━━━━━━━━━━━━━━━━━━\n\n"
            "Channel, naam, post-pic, video-pic aur audio — sab button se.\n"
            "⚠️ Jo channel add karna hai usme bot ka admin hona zaruri hai.\n\n"
            "Cancel: /cancel_add_anime",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("📢 Set Channel", callback_data=f"aa_setchannel_{uid}")]]),
            parse_mode=ParseMode.DISABLED,
        )
        return

    # baaki sab idx:page se shuru hote hain
    try:
        idx = int(parts[2])
        page = int(parts[3])
    except (ValueError, IndexError):
        await cb.answer()
        return
    item = await _item_at(idx)
    if item is None:
        await cb.answer("❌ Anime nahi mila (list badal gayi).", show_alert=True)
        await show_list(client, cb.message, page)
        return
    key = _key_of(item)

    # ── open detail ──
    if act == "o":
        _S.pop(uid, None)
        await cb.answer()
        await show_detail(client, cb.message, key, page)
        return

    # ── text-field prompt: amg:f:idx:page:field ──
    if act == "f":
        field = parts[4]
        if field.startswith("link"):
            n = int(field[4:] or 1)
            title, body = (f"🔗 <b>Link {n}</b>",
                           "Invite link bhejo (<code>https://t.me/+xxxx</code>) — hatane ke liye <code>skip</code>.")
            mode = f"link{n}"
        else:
            title, body = _PROMPTS[field]
            mode = field
        _S[uid] = {"mode": mode, "key": key, "page": page}
        await cb.answer()
        await _edit(cb.message, f"{title}\n📺 <b>{_e(item['name'])}</b>\n\n{body}\n\n<i>Cancel: <code>cancel</code></i>",
                    InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Cancel", callback_data=f"amg:o:{idx}:{page}")]]))
        return

    # ── thumbnail menu: amg:th:idx:page:p|v ──
    if act == "th":
        kind = parts[4]
        label = "🖼 Post Thumbnail (Main Channel post)" if kind == "p" else "🎞 Video Thumbnail (upload hui file)"
        await cb.answer()
        await _edit(
            cb.message,
            f"{label}\n📺 <b>{_e(item['name'])}</b>\n\n"
            f"🤖 <b>Auto</b> — TMDB se banner khud le leta hai\n"
            f"📷 <b>Custom</b> — tum khud photo bhejo",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("🤖 Auto (TMDB)", callback_data=f"amg:ta:{idx}:{page}:{kind}"),
                 InlineKeyboardButton("📷 Custom (photo)", callback_data=f"amg:tc:{idx}:{page}:{kind}")],
                [InlineKeyboardButton("⬅️ Back", callback_data=f"amg:o:{idx}:{page}")],
            ]))
        return

    if act == "tc":
        kind = parts[4]
        _S[uid] = {"mode": "pthumb" if kind == "p" else "vthumb", "key": key, "page": page}
        await cb.answer()
        await _edit(cb.message,
                    f"📷 <b>Custom {'Post' if kind == 'p' else 'Video'} Thumbnail</b>\n📺 <b>{_e(item['name'])}</b>\n\n"
                    f"Ab <b>photo</b> bhejo.\n<i>Cancel: <code>cancel</code></i>",
                    InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Cancel", callback_data=f"amg:o:{idx}:{page}")]]))
        return

    if act == "ta":
        kind = parts[4]
        await cb.answer("⏳ TMDB se la raha hoon...")
        ok, msg = await _apply_auto_thumb(item, kind)
        if not ok:
            await cb.message.reply(msg)
        await show_detail(client, cb.message, key, page)
        return

    # ── timing menu ──
    if act == "t":
        items, mon, pm, sch = await _snapshot()
        se = _sch_entry(sch, item["name"]) or {}
        iv = se.get("interval_days")
        iv_s = "❓ Unknown" if iv == "unknown" else (f"{iv} din" if iv else "—")
        await cb.answer()
        await _edit(
            cb.message,
            f"📅 <b>Timing</b> — 📺 <b>{_e(item['name'])}</b>\n\n"
            f"Next episode har: <b>{iv_s}</b>\n🎬 Total Eps: <b>{se.get('total_eps') or '—'}</b>",
            InlineKeyboardMarkup([
                [InlineKeyboardButton(x, callback_data=f"amg:iv:{idx}:{page}:{v}")
                 for x, v in (("7 din", "7"), ("10 din", "10"), ("14 din", "14"), ("❓ Unknown", "u"))],
                [InlineKeyboardButton("✏️ Custom din", callback_data=f"amg:f:{idx}:{page}:int")],
                [InlineKeyboardButton("🎬 Total Eps (Auto TMDB)", callback_data=f"amg:ea:{idx}:{page}"),
                 InlineKeyboardButton("✏️ Total Eps", callback_data=f"amg:f:{idx}:{page}:eps")],
                [InlineKeyboardButton("⬅️ Back", callback_data=f"amg:o:{idx}:{page}")],
            ]))
        return

    if act == "iv":
        val = parts[4]
        await _set_schedule(item, interval=("unknown" if val == "u" else int(val)))
        await cb.answer("✅ Timing save ho gayi")
        await show_detail(client, cb.message, key, page)
        return

    if act == "ea":
        from ..utils.anime_api import fetch_anime_details
        await cb.answer("⏳ TMDB se la raha hoon...")
        try:
            d = await fetch_anime_details(item["name"])
        except Exception:
            d = None
        n = int((d or {}).get("total_eps") or 0)
        if not n:
            await cb.message.reply("❌ TMDB pe total episodes nahi mile — ✏️ Total Eps se manual daalo.")
            return
        await _set_schedule(item, total_eps=n)
        await show_detail(client, cb.message, key, page)
        return

    # ── delete (confirm → do) ──
    if act == "d":
        await cb.answer()
        await _edit(
            cb.message,
            f"🗑 <b>Delete kar du?</b>\n📺 <b>{_e(item['name'])}</b>\n\n"
            f"Is naam ka <b>SAB kuch</b> hat jaayega:\n"
            f"• 📡 Auto-monitor entry\n• 📋 Post entry (link / audio / genres / post-thumbnail)\n"
            f"• 📅 Timing / schedule\n• 🎞 Video thumbnail\n\n"
            f"⚠️ Iske baad is anime ke naye episodes auto-upload / post nahi honge.",
            InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Haan, sab delete", callback_data=f"amg:dy:{idx}:{page}"),
                InlineKeyboardButton("❌ Nahi", callback_data=f"amg:o:{idx}:{page}")]]))
        return

    if act == "dy":
        done = await _cascade_delete(item)
        LOGGER.info(f"[AnimeMgr] deleted '{item['name']}': {done}")
        await cb.answer("🗑 Delete ho gaya")
        await show_list(client, cb.message, page)
        return

    await cb.answer()


async def _set_schedule(item: dict, interval=None, total_eps=None):
    from .schedule_notify import _get_schedule_list, _save_schedule_list
    from .auto_monitor import _get_anime_list, _save_anime_list
    sch = await _get_schedule_list()
    e = _sch_entry(sch, item["name"])
    if e is None:
        e = {"anime_name": item["name"], "interval_days": 7, "total_eps": 0}
        sch.append(e)
    if interval is not None:
        e["interval_days"] = interval
    if total_eps is not None:
        e["total_eps"] = total_eps
        mon = await _get_anime_list()          # update_post style (New/Complete tag) yahin se padhta hai
        for m in mon:
            if _nk(m.get("anime_name")) == item["nk"]:
                m["total_eps"] = total_eps
        await _save_anime_list(mon)
    await _save_schedule_list(sch)


async def _apply_auto_thumb(item: dict, kind: str):
    """kind 'p' = post thumb, 'v' = video thumb. TMDB se image laakar set karo."""
    from ..utils.anime_api import fetch_anime_details
    from .update_channel import _get_post_map, _save_post_map
    try:
        d = await fetch_anime_details(item["name"])
    except Exception:
        d = None
    url = (d or {}).get("image")
    if not url:
        return False, "❌ TMDB pe is naam ka poster nahi mila — Custom use karo."
    pm = await _get_post_map()
    pk = _ensure_pm(pm, item)
    if kind == "p":
        pm[pk]["image"] = url
        pm[pk]["thumb_post_mode"] = "auto"
        await _save_post_map(pm)
        return True, "✅ Post thumbnail (Auto) set"
    from .auto_monitor import _register_setpic_from_url
    oid = await _oid()
    ok = await _register_setpic_from_url(oid, item["name"], url)
    if not ok:
        return False, "❌ Video thumbnail save nahi hua — Custom try karo."
    pm[pk]["thumb_video_mode"] = "auto"
    await _save_post_map(pm)
    return True, "✅ Video thumbnail (Auto) set"


# ─────────────────────────────────────────────
#  Text / forward / photo input (session based) — group -8
# ─────────────────────────────────────────────
async def _done(client, message, key, page, txt):
    ok = await message.reply(txt, parse_mode=ParseMode.HTML)
    await show_detail(client, ok, key, page, is_new=True)


@Client.on_message(filters.private, group=-5)
async def amg_input(client: Client, message: Message):
    """Ek hi handler: text / forward / photo — sirf tab consume karta hai jab Anime Manager ka session ho."""
    if not message.from_user:
        raise ContinuePropagation
    uid = message.from_user.id
    sess = _S.get(uid)
    if not sess or not _is_auth(uid):
        raise ContinuePropagation
    if message.photo and sess["mode"] in ("pthumb", "vthumb"):
        await _handle_photo(client, message, sess)
        raise StopPropagation
    text = (message.text or "").strip()
    mode, key, page = sess["mode"], sess["key"], sess["page"]

    if mode in ("pthumb", "vthumb"):                 # photo chahiye
        if text.lower() == "cancel":
            _S.pop(uid, None)
            await message.reply("❌ Cancelled.")
            raise StopPropagation
        if text.startswith("/"):
            _S.pop(uid, None)
            raise ContinuePropagation
        await message.reply("⚠️ Photo bhejo (text nahi). Cancel: <code>cancel</code>", parse_mode=ParseMode.HTML)
        raise StopPropagation

    if text.lower() == "cancel":
        _S.pop(uid, None)
        await message.reply("❌ Cancelled.")
        raise StopPropagation
    if text.startswith("/"):
        _S.pop(uid, None)
        raise ContinuePropagation

    items, mon, pm, sch = await _snapshot()
    _, item = _find_item(items, key)
    if item is None:
        _S.pop(uid, None)
        await message.reply("❌ Ye anime ab list mein nahi hai.")
        raise StopPropagation

    # ── Monitor channel badlo ──
    if mode == "ch":
        from .auto_monitor import _fwd_chat, _get_anime_list, _save_anime_list
        fc = _fwd_chat(message)
        if fc:
            new_id = fc.id
        else:
            try:
                new_id = int(text)
            except ValueError:
                await message.reply("⚠️ Channel ID samajh nahi aayi. <code>-100xxxxxxxxxx</code> do ya channel ka message forward karo.",
                                    parse_mode=ParseMode.HTML)
                raise StopPropagation
        try:
            chat = await client.get_chat(new_id)
            me = await client.get_me()
            mem = await client.get_chat_member(new_id, me.id)
            if mem.status.name not in ("ADMINISTRATOR", "OWNER"):
                await message.reply(f"❌ Bot <code>{_e(chat.title)}</code> mein admin nahi hai!", parse_mode=ParseMode.HTML)
                raise StopPropagation
        except StopPropagation:
            raise
        except Exception as ex:
            await message.reply(f"❌ Channel check fail: <code>{_e(ex)}</code>\nBot ko pehle channel mein admin banao.",
                                parse_mode=ParseMode.HTML)
            raise StopPropagation
        lst = await _get_anime_list()
        target = next((m for m in lst if _nk(m.get("anime_name")) == item["nk"]
                       and (m.get("channel_id") or 0) == item["ch"]), None)
        if target is None:                              # orphan — ab monitor mein jodo
            pk = item.get("pm_key")
            target = {"anime_name": item["name"], "hashtag": "",
                      "channel_link": (pm.get(pk) or {}).get("invite_link", "") if pk else ""}
            lst.append(target)
        target["channel_id"] = new_id
        target["channel_title"] = chat.title
        await _save_anime_list(lst)
        _S.pop(uid, None)
        await _done(client, message, (item["nk"], new_id), page,
                    f"✅ Monitor channel ab: <code>{_e(chat.title)}</code> (<code>{new_id}</code>)")
        raise StopPropagation

    # ── Naam ──
    if mode == "name":
        new = re.sub(r"\s+", " ", text).strip()
        if not new:
            raise StopPropagation
        if _nk(new) != item["nk"] and any(i["nk"] == _nk(new) for i in items):
            await message.reply("⚠️ Is naam ka anime pehle se list mein hai. Doosra naam do.")
            raise StopPropagation
        await _cascade_rename(item, new)
        _S.pop(uid, None)
        await _done(client, message, (_nk(new), item["ch"]), page, f"✅ Naam badal gaya: <b>{_e(new)}</b>")
        raise StopPropagation

    # ── Link / Audio / Genres (post-map) ──
    if mode.startswith("link") or mode in ("audio", "genres"):
        from .update_channel import _save_post_map
        from ..utils import multi_mode as _mm
        pk = _ensure_pm(pm, item)
        if mode.startswith("link"):
            n = int(mode[4:] or 1)
            if text.lower() == "skip":
                val = ""
            elif not text.startswith("http"):
                await message.reply("⚠️ Valid link do (<code>https://t.me/...</code>) ya <code>skip</code>.",
                                    parse_mode=ParseMode.HTML)
                raise StopPropagation
            else:
                val = text
            if n == 1 and not pm[pk].get("invite_links"):
                pm[pk]["invite_link"] = val
            else:
                _mm.set_link(pm[pk], n, val)
            if n == 1 and item["mon"] is not None:      # monitor entry ka channel_link bhi sync
                from .auto_monitor import _get_anime_list, _save_anime_list
                lst = await _get_anime_list()
                for m in lst:
                    if _nk(m.get("anime_name")) == item["nk"]:
                        m["channel_link"] = val
                await _save_anime_list(lst)
            what = f"Link {n}"
        else:
            pm[pk][mode] = text
            what = mode.capitalize()
        await _save_post_map(pm)
        _S.pop(uid, None)
        await _done(client, message, key, page, f"✅ {what} updated")
        raise StopPropagation

    # ── Timing / total eps ──
    if mode == "int":
        t = text.lower()
        if t in ("unknown", "?", "u"):
            val = "unknown"
        else:
            try:
                val = int(t)
                if val <= 0:
                    raise ValueError
            except ValueError:
                await message.reply("⚠️ Positive number do (jaise <code>7</code>) ya <code>unknown</code>.",
                                    parse_mode=ParseMode.HTML)
                raise StopPropagation
        await _set_schedule(item, interval=val)
        _S.pop(uid, None)
        await _done(client, message, key, page, "✅ Timing updated")
        raise StopPropagation

    if mode == "eps":
        try:
            n = int(text)
            if n < 0:
                raise ValueError
        except ValueError:
            await message.reply("⚠️ Sirf number do, jaise <code>24</code>.", parse_mode=ParseMode.HTML)
            raise StopPropagation
        await _set_schedule(item, total_eps=n)
        _S.pop(uid, None)
        await _done(client, message, key, page, "✅ Total episodes updated")
        raise StopPropagation




# ── photo handler (thumbnail) — amg_input se bulaya jaata hai ──
async def _handle_photo(client: Client, message: Message, sess: dict):
    uid = message.from_user.id
    key, page = sess["key"], sess["page"]
    items, mon, pm, sch = await _snapshot()
    _, item = _find_item(items, key)
    if item is None:
        _S.pop(uid, None)
        await message.reply("❌ Ye anime ab list mein nahi hai.")
        return

    from .update_channel import _save_post_map
    fid = message.photo.file_id
    pk = _ensure_pm(pm, item)
    if sess["mode"] == "pthumb":
        pm[pk]["image"] = fid
        pm[pk]["thumb_post_mode"] = "custom"
        msg = "✅ Post thumbnail (Custom) set"
    else:
        from ..utils.database.access_db import db
        oid = await _oid()
        old_kw, _ = await _video_pic(item["name"])
        if old_kw and old_kw != item["name"]:
            await db.del_custompic(oid, old_kw)
        await db.set_custompic(oid, item["name"], fid)
        pm[pk]["thumb_video_mode"] = "custom"
        msg = "✅ Video thumbnail (Custom) set"
    await _save_post_map(pm)
    _S.pop(uid, None)
    await _done(client, message, key, page, msg)
