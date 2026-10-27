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

from types import SimpleNamespace as types_SimpleNamespace

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


def _ts():
    from ..utils import thumb_style
    return thumb_style


def _n_new() -> int:
    return len(_api().STYLE_IDS)


def _n_old(kind: str) -> int:
    """Purane band styles (/setpic_style wale) sirf VIDEO thumbnail ke liye hain."""
    return len(_ts().STYLE_ORDER) if kind == "video" else 0


def _total(kind: str) -> int:
    return _n_new() + _n_old(kind)


def _old_id(kind: str, n: int):
    """Viewer index n (15..) -> purana band style id, warna None."""
    i = n - _n_new() - 1
    order = _ts().STYLE_ORDER
    return order[i] if kind == "video" and 0 <= i < len(order) else None


def _start_index(kind: str) -> int:
    """Panel hamesha naye styles se shuru: laga hua naya style, warna Style 1 (purane band styles last mein)."""
    return poster_cfg.style_of(kind) or 1


def _is_applied(kind: str, n: int) -> bool:
    if n <= _n_new():
        return poster_cfg.style_of(kind) == n
    oid = _old_id(kind, n)
    return bool(oid) and poster_cfg.style_of(kind) == 0 and _ts().get_current_style_id() == oid


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
    total = _total(kind)
    rows = [[
        InlineKeyboardButton("⏪ Back", callback_data=f"pz:v:{kind}:{(n - 2) % total + 1}"),
        InlineKeyboardButton(f"{n}/{total}", callback_data="pz:noop"),
        InlineKeyboardButton("Next ⏩", callback_data=f"pz:v:{kind}:{n % total + 1}"),
    ]]
    if n <= _n_new():
        st = api.POSTERS[n]
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
    else:                                                # purana band style
        rows.append([
            InlineKeyboardButton("🎨 Change Color", callback_data=f"pz:cl:{kind}"),
            InlineKeyboardButton("🔍 Other Anime", callback_data=f"pz:sr:{kind}"),
        ])
    rows.append([InlineKeyboardButton("✅ Applied (tap = dobara save)" if _is_applied(kind, n) else "✅ Use This Style",
                                      callback_data=f"pz:use:{kind}:{n}")])
    rows.append([InlineKeyboardButton("🚫 Disable (No Style)" if kind == "video" else "🚫 Turn OFF (saved image)",
                                      callback_data=f"pz:off:{kind}")])
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="cmz:cat:thumb"),
                 InlineKeyboardButton("🏠 Menu", callback_data="cmz:menu")])
    return InlineKeyboardMarkup(rows)


def _color_kb(s: dict) -> InlineKeyboardMarkup:
    kind, n = s["kind"], s["style"]
    presets = _ts().COLOR_PRESETS
    rows = []
    for i in range(0, len(presets), 2):
        rows.append([InlineKeyboardButton(label, callback_data=f"pz:col:{kind}:{idx}")
                     for idx, (label, _hex) in list(enumerate(presets))[i:i + 2]])
    rows.append([InlineKeyboardButton("🔙 Back to preview", callback_data=f"pz:v:{kind}:{n}")])
    return InlineKeyboardMarkup(rows)


async def _caption(s: dict) -> str:
    api = _api()
    n = s["style"]
    a = s["anime"]
    kind = s["kind"]
    info = KIND_INFO[kind]
    total = _total(kind)
    ap = " ✅ <i>applied</i>" if _is_applied(kind, n) else ""
    if n > _n_new():
        oid = _old_id(kind, n)
        name = html.escape(_ts().STYLES.get(oid, {}).get("name", oid))
        col = s["opts"].get("color")
        lines = [
            f"<b>{info['title']}</b>",
            f"🎨 <b>Style {n}/{total}</b> — purana band style: <b>{name}</b>{ap}",
            f"🔎 Preview anime: <b>{html.escape(a.title) if a else '?'}</b>",
            f"🏷 Band text: <code>{html.escape(await _brand_text(s))}</code>"
            + (f" • color <code>{col}</code>" if col else ""),
            "<i>Ye wahi /setpic_style wale purane band styles hain</i>",
        ]
        return "\n".join(lines)
    st = api.POSTERS[n]
    lines = [
        f"<b>{info['title']}</b>",
        f"🎨 <b>Style {n}/{total}</b> — {html.escape(st.blurb)}{ap}",
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
            if s["style"] > _n_new():
                r = types_SimpleNamespace(jpg=await _render_old(s, a))
            else:
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


async def _viewer_ready(s: dict) -> dict:
    await _ensure_anime(s)
    return s


def _color_hex(s: dict):
    return s["opts"].get("color")


async def _render_old(s: dict, a) -> bytes:
    """Purana band style, anime ki asli image (banner, warna poster) pe — bilkul video thumbnail jaisa."""
    from PIL import Image
    ds = _dataset()
    base = None
    for which in ("banner", "cover"):
        try:
            await ds._need_image(a, which)
            raw = getattr(a, f"{which}_bytes")
            if raw:
                base = Image.open(io.BytesIO(raw)).convert("RGB")
                break
        except Exception:
            continue
    if base is None:
        base = Image.new("RGB", (1280, 720), (40, 42, 48))
    W, H = 1280, 720                                     # 16:9 centre-crop (thumbnail jaisa)
    ar = W / H
    w, h = base.size
    if w / h > ar:
        cw, ch = int(h * ar), h
    else:
        cw, ch = w, int(w / ar)
    l, t = (w - cw) // 2, (h - ch) // 2
    img = base.crop((l, t, l + cw, t + ch)).resize((W, H), Image.LANCZOS)
    ts = _ts()
    style = ts.resolve_style(_old_id(s["kind"], s["style"]), _color_hex(s))
    ts.render_band(img, await _brand_text(s), style)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=90)
    return out.getvalue()


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
    """Seedha viewer (naye 14 styles, phir purane band styles) — beech mein koi old/new choice nahi."""
    uid = cb.from_user.id
    clear_session(uid)
    s = await _sess(uid, kind)
    s["style"] = _start_index(kind)
    if s["style"] > _n_new():
        col = _ts().get_current_color()
        s["opts"]["color"] = col
    await _render_view(client, cb.message, s, uid)


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
        await open_panel(client, cb, kind)
        return

    s = await _sess(uid, kind)
    s["wait"] = None

    if act == "off":
        await poster_cfg.save(kind, style=0)
        if kind == "video":
            await _ts().set_style("none")
            note = "🚫 <b>Disabled</b> — ab video thumbnail pe koi style/band nahi lagega (sirf anime ka poster)."
        else:
            note = "🚫 <b>OFF</b> — ab Main Channel post pe /update_post wali saved image jayegi."
        await cb.answer("🚫 Disable ho gaya")
        try:
            cap = note + "\n\n" + await _caption(await _viewer_ready(s))
            if cb.message.photo:
                await cb.message.edit_caption(cap, reply_markup=_viewer_kb(s), parse_mode=HTML)
            else:
                await _render_view(client, cb.message, s, uid, note=note)
        except Exception as e:
            if "MESSAGE_NOT_MODIFIED" not in str(e):
                LOGGER.warning(f"[PosterPanel] off caption failed: {e}")
        return

    if act == "cl":                                      # purane style ka color picker
        await cb.answer()
        try:
            await cb.message.edit_reply_markup(_color_kb(s))
        except Exception:
            pass
        return

    if act == "col":
        idx = int(parts[3])
        presets = _ts().COLOR_PRESETS
        s["opts"]["color"] = presets[idx][1] if 0 <= idx < len(presets) else None
        await cb.answer("⏳ Color lagake preview ban raha hai...")
        await _render_view(client, cb.message, s, uid)
        return

    if act == "v":                                       # style n ka preview
        n = int(parts[3])
        if not (1 <= n <= _total(kind)):
            await cb.answer()
            return
        if uid in _busy:
            await cb.answer("⏳ Pehle wala preview ban raha hai...")
            return
        s["style"] = n
        s["opts"]["pic"] = 0
        s["opts"]["char"] = 0
        if n > _n_new() and "color" not in s["opts"]:
            s["opts"]["color"] = _ts().get_current_color()
        await cb.answer("⏳ Preview ban raha hai...")
        await _render_view(client, cb.message, s, uid)
        return

    if act == "use":
        n = int(parts[3])
        if n > _n_new():                                 # purana band style
            oid = _old_id(kind, n)
            await poster_cfg.save(kind, style=0)
            await _ts().set_style(oid)
            await _ts().set_color(s["opts"].get("color"))
            await cb.answer("✅ Apply ho gaya!", show_alert=False)
            note = f"✅ <b>Style {n} applied</b> — ab se video thumbnails pe ye purana band style lagega."
        else:
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
