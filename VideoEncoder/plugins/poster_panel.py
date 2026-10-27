"""
poster_panel.py
================
/customise -> 🖼 Thumbnail -> do naye buttons:

  🎬 Video Thumbnail Design             (video ka auto-thumbnail / cover — pehle TMDB poster + band)
  📣 Main Channel Post Thumbnail Design (Main Channel pe jaane wale update post ki image)

Dono mein Anime-Poster-Bot ke 14 poster styles (utils/poster_engine) — LIVE PREVIEW ke saath:

  ⏪ back | n/14 | next ⏩        style badlo, preview turant dikhta hai (asli AniList anime pe render)
  ◀️ art ▶️                       poster / banner / character art badal ke dekho
  🔘 button text / ✏️ custom       WATCH NOW, STREAM NOW ... ya apna text
  🏷 branding                      khali = community naam (@SBANIME), ya apna text
  🔍 Other Anime                  kisi bhi anime ka naam likh ke uspe preview dekho
  ✅ Use This Style               save — bot-wide, turant effective
  🚫 OFF                          purana tareeka (video: TMDB poster + band, post: /update_post wali image)

Purane band styles (/setpic_style) jaise the waise hi hain — Video Thumbnail panel mein "Old Band Styles" button se khulte hain.
"""

import asyncio
import html
import io
import logging
import time

from pyrogram import Client, StopPropagation, filters
from pyrogram.enums import ParseMode
from pyrogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Message,
)

from .. import owner, sudo_users
from ..utils import poster_cfg

LOGGER = logging.getLogger(__name__)
HTML = ParseMode.HTML

KIND_INFO = {
    "video": dict(
        title="🎬 Video Thumbnail Design",
        section="poster_video",
        about=("Video upload hone par jo <b>auto-thumbnail / cover</b> banta hai (pehle anime ka TMDB poster + "
               "@SBANIME band), uska design yahan se chuno.\n"
               "<i>Custom thumbnail (/setpic, keyword-wise pic) ko ye override nahi karta — wo pehle jaisi "
               "priority pe rehti hain.</i>"),
        off_note="OFF = purana tareeka (TMDB poster + band, /setpic_style wale styles)",
    ),
    "post": dict(
        title="📣 Main Channel Post Thumbnail Design",
        section="poster_post",
        about=("Main Channel pe jaane wale <b>update post ki image</b> yahan chune hue style se anime ke naam se "
               "auto-generate hogi.\n"
               "<i>Style OFF ho ya poster ban na paaye to /update_post wali saved image hi jaayegi.</i>"),
        off_note="OFF = /update_post mein save ki hui image",
    ),
}

_S: dict = {}                 # user_id -> session
_LAST_TOUCH: dict = {}
_busy: set = set()


def _is_auth(user_id: int) -> bool:
    return user_id in owner or user_id in sudo_users


def clear_session(user_id: int):
    _S.pop(user_id, None)
    _busy.discard(user_id)


def _api():
    from ..utils.poster_engine import api
    return api


def _dataset():
    from ..utils.poster_engine import dataset
    return dataset


async def _sess(uid: int, kind: str) -> dict:
    s = _S.get(uid)
    if s is None or s.get("kind") != kind:
        cfg = poster_cfg.get(kind)
        s = dict(kind=kind, style=cfg.get("style") or 1, anime=None, aid=None,
                 opts=dict(button=cfg.get("button") or _api().BUTTON_PRESETS[0], pic=0, char=0),
                 brand=cfg.get("brand") or "", wait=None, panel=None)
        _S[uid] = s
    _LAST_TOUCH[uid] = time.time()
    return s


async def _brand_text(s: dict) -> str:
    if s.get("brand"):
        return s["brand"]
    try:
        from ..utils.community import get_community_tag
        return await get_community_tag()
    except Exception:
        return "@SBANIME"


async def _ensure_anime(s: dict):
    if s.get("anime") is None:
        from ..utils.poster_engine import config as _pc
        s["anime"] = await _api().load_by_id(s.get("aid") or _pc.SAMPLE_ANIME_ID)
    return s["anime"]


# ───────────────────────── keyboards / texts ─────────────────────────
def _main_text(kind: str) -> str:
    info = KIND_INFO[kind]
    cfg = poster_cfg.get(kind)
    st = cfg.get("style")
    api = _api()
    if st:
        cur = f"✅ <b>Style {st}</b> — {html.escape(api.POSTERS[st].blurb)}"
    else:
        cur = "🚫 <b>OFF</b> — " + info["off_note"]
    btn_txt = cfg.get("button") or api.BUTTON_PRESETS[0]
    brand = cfg.get("brand") or "community naam (auto)"
    return (
        f"<b>{info['title']}</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{info['about']}\n\n"
        f"🎨 <b>Abhi:</b> {cur}\n"
        f"🔘 Button text: <code>{html.escape(btn_txt)}</code>\n"
        f"🏷 Branding: <code>{html.escape(brand)}</code>\n\n"
        "<i>Neeche se styles ka live preview dekho aur jo pasand aaye use laga do.</i>"
    )


def _main_kb(kind: str) -> InlineKeyboardMarkup:
    cur = poster_cfg.style_of(kind)
    rows = [[InlineKeyboardButton("👀 Preview & Choose Style", callback_data=f"pz:v:{kind}:{cur or 1}")]]
    if cur:
        rows.append([InlineKeyboardButton("🚫 Turn OFF (purana design)", callback_data=f"pz:off:{kind}")])
    if kind == "video":
        rows.append([InlineKeyboardButton("🎨 Old Band Styles (@SBANIME band)", callback_data="cmz:open:setpic_style")])
    rows.append([
        InlineKeyboardButton("🔙 Back", callback_data="cmz:cat:thumb"),
        InlineKeyboardButton("🏠 Menu", callback_data="cmz:menu"),
    ])
    return InlineKeyboardMarkup(rows)


def _art_label(s: dict) -> str:
    ds, a, n = _dataset(), s["anime"], s["style"]
    st = ds.POSTERS[n]
    if st.character:
        if not a.character_tried:
            return "fanart PNG · ▶ load"
        cnt, label = ds.char_state(a, s["opts"].get("char", 0))
        idx = s["opts"].get("char", 0)
        return (f"{idx + 1}/{cnt} {label}" if 0 <= idx < cnt else f"cover card ({cnt} art)")[:30]
    cnt, label, i, listed = ds.art_state(a, n, s["opts"].get("pic"))
    return (f"{i + 1}/{cnt} {label}" if listed else f"{label} · ▶ more")[:30]


def _viewer_kb(s: dict) -> InlineKeyboardMarkup:
    kind, n = s["kind"], s["style"]
    api = _api()
    total = len(api.STYLE_IDS)
    st = api.POSTERS[n]
    rows = [[
        InlineKeyboardButton("⏪ Back", callback_data=f"pz:v:{kind}:{(n - 2) % total + 1}"),
        InlineKeyboardButton(f"{n}/{total}", callback_data="pz:noop"),
        InlineKeyboardButton("Next ⏩", callback_data=f"pz:v:{kind}:{n % total + 1}"),
    ]]
    if s.get("anime") is not None:
        rows.append([
            InlineKeyboardButton("◀️", callback_data=f"pz:ap:{kind}"),
            InlineKeyboardButton("🖼 " + _art_label(s), callback_data="pz:noop"),
            InlineKeyboardButton("▶️", callback_data=f"pz:an:{kind}"),
        ])
    if st.button:
        rows.append([
            InlineKeyboardButton("🔘 " + (s["opts"].get("button") or "")[:18], callback_data=f"pz:bt:{kind}"),
            InlineKeyboardButton("✏️ Custom", callback_data=f"pz:bc:{kind}"),
        ])
    rows.append([
        InlineKeyboardButton("🏷 Branding", callback_data=f"pz:br:{kind}"),
        InlineKeyboardButton("🔍 Other Anime", callback_data=f"pz:sr:{kind}"),
    ])
    cur = poster_cfg.style_of(kind)
    rows.append([InlineKeyboardButton("✅ Applied (tap = dobara save)" if cur == n else "✅ Use This Style",
                                      callback_data=f"pz:use:{kind}:{n}")])
    rows.append([InlineKeyboardButton("🔙 Back", callback_data=f"pz:m:{kind}"),
                 InlineKeyboardButton("🏠 Menu", callback_data="cmz:menu")])
    return InlineKeyboardMarkup(rows)


async def _caption(s: dict) -> str:
    api = _api()
    n = s["style"]
    st = api.POSTERS[n]
    a = s["anime"]
    info = KIND_INFO[s["kind"]]
    cur = poster_cfg.style_of(s["kind"])
    lines = [
        f"<b>{info['title']}</b>",
        f"🎨 <b>Style {n}/{len(api.STYLE_IDS)}</b> — {html.escape(st.blurb)}" + ("  ✅ <i>applied</i>" if cur == n else ""),
        f"🔎 Preview anime: <b>{html.escape(a.title) if a else '?'}</b>",
        f"🏷 Branding: <code>{html.escape(await _brand_text(s))}</code>",
    ]
    if st.button:
        lines.append(f"🔘 Button: <code>{html.escape(s['opts'].get('button') or '')}</code>")
    lines.append("<i>◀️ ▶️ se art badlo (sirf preview ke liye) • ✅ dabane par style save hota hai</i>")
    return "\n".join(lines)


# ───────────────────────── message helpers ─────────────────────────
async def _to_text(client: Client, cb: CallbackQuery, text: str, kb: InlineKeyboardMarkup):
    """Text panel dikhao — agar abhi photo (preview) khula hai to naya text msg bhejo + photo delete."""
    msg = cb.message
    if msg.photo:
        try:
            await client.send_message(msg.chat.id, text, reply_markup=kb, parse_mode=HTML)
            await msg.delete()
            return
        except Exception as e:
            LOGGER.warning(f"[PosterPanel] text switch failed: {e}")
    try:
        await msg.edit_text(text, reply_markup=kb, parse_mode=HTML)
    except Exception as e:
        if "MESSAGE_NOT_MODIFIED" not in str(e):
            try:
                await client.send_message(msg.chat.id, text, reply_markup=kb, parse_mode=HTML)
            except Exception:
                pass


async def _show_photo(client: Client, cb_msg: Message, data: bytes, caption: str, kb: InlineKeyboardMarkup,
                      s: dict):
    bio = io.BytesIO(data)
    bio.name = "preview.jpg"
    if cb_msg.photo:
        try:
            await cb_msg.edit_media(InputMediaPhoto(bio, caption=caption, parse_mode=HTML), reply_markup=kb)
            s["panel"] = cb_msg.id
            return
        except Exception as e:
            if "MESSAGE_NOT_MODIFIED" in str(e):
                return
            LOGGER.info(f"[PosterPanel] edit_media failed ({e}) — naya photo bhej raha hoon")
            bio = io.BytesIO(data)
            bio.name = "preview.jpg"
    sent = await client.send_photo(cb_msg.chat.id, bio, caption=caption, reply_markup=kb, parse_mode=HTML)
    s["panel"] = sent.id
    try:
        await cb_msg.delete()
    except Exception:
        pass


async def _render_view(client: Client, cb_msg: Message, s: dict, uid: int, note: str = ""):
    """Current style/options ka preview render karke dikhao."""
    api = _api()
    if uid in _busy:
        return
    _busy.add(uid)
    try:
        try:
            await client.send_chat_action(cb_msg.chat.id, "upload_photo")
        except Exception:
            pass
        try:
            a = await _ensure_anime(s)
            r = await api.render(a, s["style"], button=s["opts"].get("button"), brand=await _brand_text(s),
                                 pic=s["opts"].get("pic", 0), char=s["opts"].get("char", 0))
        except api.NoArt as e:
            kind_ = e.args[0] if e.args else "art"
            await _view_error(client, cb_msg, s,
                              f"😕 Is anime ka <b>{kind_}</b> nahi mila aur Style {s['style']} ko chahiye. "
                              f"Dusra style ya dusra anime try karo.")
            return
        except Exception as e:
            LOGGER.exception(f"[PosterPanel] preview render failed: {e}")
            await _view_error(client, cb_msg, s, "😕 Preview render nahi ho paaya (internet/AniList?). Dobara try karo.")
            return
        cap = await _caption(s)
        if note:
            cap = note + "\n\n" + cap
        await _show_photo(client, cb_msg, r.jpg, cap, _viewer_kb(s), s)
    finally:
        _busy.discard(uid)


async def _view_error(client: Client, cb_msg: Message, s: dict, text: str):
    kb = _viewer_kb(s) if s.get("anime") is not None else InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔙 Back", callback_data=f"pz:m:{s['kind']}")]])
    try:
        if cb_msg.photo:
            await cb_msg.edit_caption(text, reply_markup=kb, parse_mode=HTML)
        else:
            await cb_msg.edit_text(text, reply_markup=kb, parse_mode=HTML)
    except Exception:
        try:
            await client.send_message(cb_msg.chat.id, text, reply_markup=kb, parse_mode=HTML)
        except Exception:
            pass


# ───────────────────────── entry (customise se) ─────────────────────────
async def open_panel(client: Client, cb: CallbackQuery, kind: str):
    clear_session(cb.from_user.id)
    await _to_text(client, cb, _main_text(kind), _main_kb(kind))


# ───────────────────────── callbacks ─────────────────────────
@Client.on_callback_query(filters.regex(r"^pz:"))
async def poster_panel_cb(client: Client, cb: CallbackQuery):
    uid = cb.from_user.id
    if not _is_auth(uid):
        await cb.answer("Yeh sirf owner/sudo ke liye hai.", show_alert=True)
        return
    parts = cb.data.split(":")
    act = parts[1]
    if act == "noop":
        await cb.answer()
        return
    kind = parts[2] if len(parts) > 2 else "video"
    if kind not in KIND_INFO:
        await cb.answer()
        return
    api = _api()

    if act == "m":
        await cb.answer()
        clear_session(uid)
        await _to_text(client, cb, _main_text(kind), _main_kb(kind))
        return

    if act == "off":
        await poster_cfg.save(kind, style=0)
        await cb.answer("🚫 OFF kar diya — purana design wapas.", show_alert=False)
        await _to_text(client, cb, _main_text(kind), _main_kb(kind))
        return

    s = await _sess(uid, kind)
    s["wait"] = None

    if act == "v":                                       # style n ka preview
        n = int(parts[3])
        if n not in api.POSTERS:
            await cb.answer()
            return
        if uid in _busy:
            await cb.answer("⏳ Pehle wala preview ban raha hai...")
            return
        s["style"] = n
        s["opts"]["pic"] = 0
        s["opts"]["char"] = 0
        await cb.answer("⏳ Preview ban raha hai...")
        await _render_view(client, cb.message, s, uid)
        return

    if act == "use":
        n = int(parts[3])
        await poster_cfg.save(kind, style=n, button=s["opts"].get("button") or "", brand=s.get("brand") or "")
        await cb.answer(f"✅ Style {n} apply ho gaya!", show_alert=False)
        where = ("ab se naye video thumbnails / covers is style mein banenge"
                 if kind == "video" else "ab se Main Channel update posts ki image is style mein banegi")
        note = f"✅ <b>Style {n} applied</b> — {where}."
        try:
            s["style"] = n
            cap = note + "\n\n" + await _caption(s)
            if cb.message.photo:
                await cb.message.edit_caption(cap, reply_markup=_viewer_kb(s), parse_mode=HTML)
            else:
                await _render_view(client, cb.message, s, uid, note=note)
        except Exception as e:
            if "MESSAGE_NOT_MODIFIED" not in str(e):
                LOGGER.warning(f"[PosterPanel] apply caption failed: {e}")
        return

    if act in ("ap", "an"):                              # art prev / next
        if uid in _busy:
            await cb.answer("⏳ Ruko...")
            return
        step = -1 if act == "ap" else 1
        await cb.answer("⏳ Art badal raha hoon...")
        ds = _dataset()
        try:
            a = await _ensure_anime(s)
            st = api.POSTERS[s["style"]]
            if st.character:
                await ds.ensure_art(a, s["style"], s["opts"])      # fanart.tv PNGs load (ek baar)
                cnt = len(a.characters)
                cur = s["opts"].get("char", 0)
                cur = cnt if cur < 0 else cur                       # -1 (cover card) = last slot
                nxt = (cur + step) % (cnt + 1) if cnt else 0
                s["opts"]["char"] = -1 if nxt == cnt else nxt
            else:
                await ds.list_art(a, ds.art_kind(s["style"]))
                cnt = ds.art_count(a, s["style"])
                if cnt:
                    s["opts"]["pic"] = (s["opts"].get("pic", 0) + step) % cnt
        except Exception as e:
            LOGGER.warning(f"[PosterPanel] art change failed: {e}")
        await _render_view(client, cb.message, s, uid)
        return

    if act == "bt":                                      # button preset cycle
        presets = api.BUTTON_PRESETS
        cur = s["opts"].get("button")
        i = (presets.index(cur) + 1) % len(presets) if cur in presets else 0
        s["opts"]["button"] = presets[i]
        await cb.answer("⏳ Preview ban raha hai...")
        await _render_view(client, cb.message, s, uid)
        return

    if act == "bc":
        s["wait"] = "button"
        s["panel"] = cb.message.id
        await cb.answer()
        await _ask(client, cb, "✏️ <b>Button ka text bhejo</b> (max 20 letters, jaise <code>WATCH NOW</code>).")
        return

    if act == "br":
        s["wait"] = "brand"
        s["panel"] = cb.message.id
        await cb.answer()
        cur = s.get("brand") or "community naam (auto)"
        await _ask(client, cb,
                   f"🏷 <b>Branding text bhejo</b> (max 24 letters).\nAbhi: <code>{html.escape(cur)}</code>\n\n"
                   "Community naam wapas lagana ho to <code>reset</code> likho.")
        return

    if act == "sr":
        s["wait"] = "search"
        s["panel"] = cb.message.id
        await cb.answer()
        await _ask(client, cb, "🔍 <b>Anime ka naam bhejo</b> — uspe preview dikhaunga (sirf preview ke liye, setting nahi badlegi).")
        return

    if act == "pick":
        aid = int(parts[3])
        await cb.answer("⏳ Anime load ho raha hai...")
        try:
            s["anime"] = await api.load_by_id(aid)
            s["aid"] = aid
            s["opts"]["pic"] = 0
            s["opts"]["char"] = 0
        except Exception as e:
            LOGGER.warning(f"[PosterPanel] anime load failed: {e}")
            await _view_error(client, cb.message, s, "😕 Anime load nahi hua. Dobara try karo.")
            return
        await _render_view(client, cb.message, s, uid)
        return

    if act == "x":                                       # cancel input
        await cb.answer()
        await _render_view(client, cb.message, s, uid)
        return

    await cb.answer()


async def _ask(client: Client, cb: CallbackQuery, text: str):
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✖️ Cancel", callback_data=f"pz:x:{_S[cb.from_user.id]['kind']}")]])
    if cb.message.photo:
        try:
            await cb.message.edit_caption(text, reply_markup=kb, parse_mode=HTML)
            return
        except Exception:
            pass
    await client.send_message(cb.message.chat.id, text, reply_markup=kb, parse_mode=HTML)


# ───────────────────────── text input (button / brand / search) ─────────────────────────
@Client.on_message(filters.private & filters.text, group=-3)
async def poster_panel_text(client: Client, message: Message):
    if not message.from_user:
        return
    uid = message.from_user.id
    s = _S.get(uid)
    if not s or not s.get("wait") or not _is_auth(uid):
        return
    text = (message.text or "").strip()
    if text.startswith("/"):                 # koi command — input mode chhodo, command ko chalne do
        s["wait"] = None
        return
    mode, s["wait"] = s["wait"], None
    api = _api()
    panel_id = s.get("panel")

    try:
        pm = await client.get_messages(message.chat.id, panel_id) if panel_id else None
        if pm is not None and getattr(pm, "empty", False):
            pm = None
    except Exception:
        pm = None

    try:
        await message.delete()
    except Exception:
        pass

    if mode == "button":
        s["opts"]["button"] = " ".join(text.split())[:20].upper()
    elif mode == "brand":
        from ..utils.poster_engine import chname
        if text.lower() in ("reset", "auto", "default", "-"):
            s["brand"] = ""
        else:
            s["brand"] = chname.clean(text) or ""
    elif mode == "search":
        res = await api.search(text, limit=6)
        if not res:
            if pm:
                await _view_error(client, pm, s, f"😕 <b>{html.escape(text)}</b> AniList pe nahi mila. Dusra naam try karo.")
            raise StopPropagation
        rows = []
        for r in res:
            yr = f" ({r['year']})" if r.get("year") else ""
            rows.append([InlineKeyboardButton(f"{r['title']}{yr}"[:60], callback_data=f"pz:pick:{s['kind']}:{r['id']}")])
        rows.append([InlineKeyboardButton("✖️ Cancel", callback_data=f"pz:x:{s['kind']}")])
        txt = f"🔍 <b>{html.escape(text)}</b> — kaunsa anime?"
        try:
            if pm and pm.photo:
                await pm.edit_caption(txt, reply_markup=InlineKeyboardMarkup(rows), parse_mode=HTML)
            elif pm:
                await pm.edit_text(txt, reply_markup=InlineKeyboardMarkup(rows), parse_mode=HTML)
            else:
                raise RuntimeError("no panel")
        except Exception:
            sent = await client.send_message(message.chat.id, txt, reply_markup=InlineKeyboardMarkup(rows), parse_mode=HTML)
            s["panel"] = sent.id
        raise StopPropagation

    if pm is None:
        pm = await client.send_message(message.chat.id, "⏳ Preview ban raha hai...")
    await _render_view(client, pm, s, uid)
    raise StopPropagation
