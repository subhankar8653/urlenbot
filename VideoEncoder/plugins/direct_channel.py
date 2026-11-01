"""
direct_channel.py
=================
DIRECT FILE CHANNEL  —  auto-monitor jaisa channel, bas isme link/post nahi, TEXT + seedha FILE aati hai.
Download / upload KUCH nahi hota: file Telegram pe pehle se hai, bot bas file_id se cover pic + caption
lagake anime ke channel mein bhej deta hai (~1 second).

Setup
-----
  /set_direct -100xxxxxxxxxx     (ya channel se post forward karke reply mein /set_direct)
  /set_direct off                (band karne ke liye)
  /set_direct                    (abhi kaun sa channel set hai dekho)
  /direct_slot                   (abhi kaunsa episode "declared" hai) • /direct_slot clear
  Bot is channel mein ADMIN hona chahiye.  Anime /add_anime list mein hona chahiye.
  (/automonitor on|off isse alag hai — direct channel hamesha kaam karta hai.)

Flow  (TEXT PEHLE, phir FILE)
----
  1. Channel mein pehle TEXT likho (anime + season + episode):
         Naruto S01 episode 01                 -> single episode
         Naruto S01 episode 01 - 02            -> ek file mein 2 episode (caption mein 01-02)
         Naruto Movie   /  Naruto Movie 2      -> movie
     Bot owner ko DM mein confirm karta hai ("Slot set").
  2. Uske baad us episode ki files forward/send karo — quality filename/caption/video size se pata chalti hai:
         360p ya 480p  -> turant anime channel mein upload (cover pic + caption) + UPDATE POST
         720p, 1080p   -> order se upload (480 -> 720 -> 1080;  4K bhi aa sakta hai)
     Har file se pehle check: ye (season, episode, quality) channel mein pehle se hai? hai to skip.
     Episode 1 ki pehli file se pehle season intro (details post -> border -> season sticker);
     pichhle episode ke end/schedule messages naye episode ki pehli file se pehle delete.
  3. 360/480 + 720 + 1080 teeno aate hi: border line + schedule / end messages (auto-monitor jaisa).
  4. 4K (2160p) baad mein aaye: border + end/schedule delete -> 4K upload -> border + end/schedule dobara
     (update post dobara nahi jaata).
  5. Naya text bhejoge to agli files naye episode ki maani jaayengi (text ke BAAD aayi file hi us text ki hai).
  Text nahi bheja to purana tareeka chalta hai: filename/caption se anime + season + episode + quality detect.

Movie: update post / schedule nahi — sirf upload + border.
"""

import asyncio
import html
import os
import re
import time

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import Message

from .. import LOGGER, owner, sudo_users
from ..utils import episode_guard as eg
from ..utils.database.access_db import db

# ─────────────────────────────────────────────
#  Constants
# ─────────────────────────────────────────────
QUAL_ORDER = ["144p", "240p", "360p", "480p", "576p", "720p", "1080p", "1440p", "2160p"]   # chhoti -> badi
# 360p aur 480p EK HI tier hai (utils/episode_guard.canon) — dono mein se ek aa gaya to "low" quality ho gayi.
BASE_ORDER = ["360p", "720p", "1080p", "2160p"]                           # tier order (canonical)
REQUIRED = ("360p", "720p", "1080p")                                      # inke aate hi episode "complete"
ORDER_GRACE = int(os.getenv("DIRECT_ORDER_GRACE", "120"))                 # seconds
SLOT_TTL = int(os.getenv("DIRECT_SLOT_TTL", str(24 * 3600)))              # text declaration kitni der valid
MOVIE_SEASON = 99                                                         # movie ka internal season (S01E01 se na takraye)


def _tier(q: str) -> str:
    return eg.canon(q)


def _rank(q: str) -> int:
    return QUAL_ORDER.index(q) if q in QUAL_ORDER else 99


def _is_authorized(user_id: int) -> bool:
    return user_id in owner or user_id in sudo_users


def _norm(t: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (t or "").lower())


# ─────────────────────────────────────────────
#  Direct channel id  (owner ke user doc mein)
# ─────────────────────────────────────────────
_DC_CACHE = {"ts": 0.0, "val": None}


async def _owner_id():
    return owner[0] if owner else None


async def get_direct_channel() -> int | None:
    now = time.time()
    if now - _DC_CACHE["ts"] < 20:
        return _DC_CACHE["val"]
    val = None
    oid = await _owner_id()
    if oid:
        try:
            user = await db._get_user(oid)
            v = user.get("direct_channel_id")
            val = int(v) if v else None
        except Exception as e:
            LOGGER.warning(f"[Direct] channel id read fail: {e!r}")
            return _DC_CACHE["val"]
    _DC_CACHE.update(ts=now, val=val)
    return val


async def _save_direct_channel(channel_id: int | None):
    oid = await _owner_id()
    if not oid:
        return
    if channel_id is None:
        await db.col.update_one({"id": oid}, {"$unset": {"direct_channel_id": ""}}, upsert=True)
    else:
        await db.col.update_one({"id": oid}, {"$set": {"direct_channel_id": channel_id}}, upsert=True)
    _DC_CACHE.update(ts=time.time(), val=channel_id)


# ─────────────────────────────────────────────
#  /set_direct
# ─────────────────────────────────────────────
@Client.on_message(filters.command("set_direct") & filters.private)
async def cmd_set_direct(client: Client, message: Message):
    if not _is_authorized(message.from_user.id):
        return
    from .auto_monitor import _fwd_chat

    parts = (message.text or "").split()
    if len(parts) >= 2 and parts[1].lower() in ("off", "remove", "none", "clear"):
        await _save_direct_channel(None)
        await message.reply("🛑 **Direct File Channel band kar diya.**")
        return

    channel_id, title = None, None
    fc = _fwd_chat(message.reply_to_message)
    if fc:
        channel_id, title = fc.id, fc.title
    elif len(parts) >= 2:
        try:
            channel_id = int(parts[1])
            chat = await client.get_chat(channel_id)
            title = chat.title
        except Exception as e:
            await message.reply(f"❌ Channel nahi mila: `{e}`")
            return

    if channel_id is None:
        cur = await get_direct_channel()
        if cur:
            try:
                ch = await client.get_chat(cur)
                cur_text = f"✅ **Current:** {ch.title} (`{cur}`)"
            except Exception:
                cur_text = f"✅ **Current ID:** `{cur}`"
        else:
            cur_text = "❌ Abhi set nahi hai"
        await message.reply(
            f"📥 **Direct File Channel**\n\n{cur_text}\n\n"
            f"**Kaise set karein:**\n"
            f"• Channel se koi post forward karo, reply mein `/set_direct`\n"
            f"• ya `/set_direct -100xxxxxxxxxx`\n"
            f"• band karne ke liye `/set_direct off`\n\n"
            f"**Use kaise karein:** pehle TEXT likho `Naruto S01 episode 01` "
            f"(2 episode: `Naruto S01 episode 01 - 02`, movie: `Naruto Movie`), phir 360p/480p, 720p, 1080p files bhejo. "
            f"Bot ko channel mein admin banana mat bhulna."
        )
        return

    await _save_direct_channel(channel_id)
    from .auto_monitor import _bot_access_line
    acc = await _bot_access_line(client, channel_id, need_post=False)
    await message.reply(
        f"✅ **Direct File Channel Set!**\n\n📢 **{title}**\n🆔 `{channel_id}`\n\n{acc}\n\n"
        f"Ab pehle text likho (`Naruto S01 episode 01`), phir files bhejo — bot anime ke channel mein upload kar dega. 🚀"
    )


# ─────────────────────────────────────────────
#  Text declaration parsing   ("Naruto S01 episode 01 - 02", "Naruto Movie 2" ...)
# ─────────────────────────────────────────────
class EpRange(int):
    """int jaisa (start episode), par f"{x:02d}" karne par "01-02" dikhata hai — caption styles ko
    badalna nahi padta."""
    def __new__(cls, a: int, b: int):
        o = int.__new__(cls, a)
        o.b = b
        return o

    def __format__(self, spec):
        return f"{int(self):02d}-{self.b:02d}"

    def __str__(self):
        return f"{int(self):02d}-{self.b:02d}"


_RNG = (r'(?:\s*(?:-|–|—|~|to|&|\+|,)\s*(?:(?:episodes?|eps?|e)\s*[-:.#]?\s*)?0*(\d{1,4})(?!\d)(?!\s*p(?![a-z])))?')
_SE_RANGE = re.compile(
    r'(?<![a-z0-9])(?:season|s)\s*[-:.]?\s*0*(\d{1,2})\s*[^a-z0-9]{0,8}\s*'
    r'(?:episodes?|eps?|e)\s*[-:.#]?\s*0*(\d{1,4})(?!\d)' + _RNG)
_EP_RANGE = re.compile(
    r'(?<![a-z0-9])(?:episodes?|eps?|e)\s*[-:.#]?\s*0*(\d{1,4})(?!\d)' + _RNG)
_SEASON = re.compile(r'(?<![a-z0-9])(?:season|s)\s*[-:.]?\s*0*(\d{1,2})(?![a-z0-9])')
_SEASON_ORD = re.compile(r'(?<![a-z0-9])0*(\d{1,2})\s*(?:st|nd|rd|th)\s+season')
_MOVIE = re.compile(r'(?<![a-z0-9])(?:movie|film)s?(?![a-z0-9])(?:\s*[-:.#]?\s*0*(\d{1,2})(?![a-z0-9]))?')
_LANG_RE = re.compile(r'(?<![a-z])(hindi|english|japanese|tamil|telugu|malayalam|kannada|bengali|marathi|'
                      r'punjabi|gujarati|urdu|dual\s*audio|multi\s*audio)(?![a-z])', re.I)


def _range_ok(a: int, b):
    if b and a < b <= a + 40:
        return a, b
    return a, a


def parse_declaration(text: str, anime_list: list) -> dict | None:
    """
    Text -> {entry, anime, season, ep, ep_end, movie, movie_label} ya None (anime match nahi / ep-movie nahi).
    Season na likha ho to 1.  Episode range: 'episode 01 - 02', 'E01-E02', 'ep 1 to 2'.
    """
    from .auto_monitor import _find_matching_anime
    if not text or not anime_list:
        return None
    entry = _find_matching_anime(text, anime_list)
    if not entry:
        return None
    t = eg.norm(text)
    season = ep = ep_end = None
    m = _SE_RANGE.search(t)
    if m:
        season = int(m.group(1))
        ep, ep_end = _range_ok(int(m.group(2)), int(m.group(3)) if m.group(3) else None)
    else:
        m = _EP_RANGE.search(t)
        if m:
            ep, ep_end = _range_ok(int(m.group(1)), int(m.group(2)) if m.group(2) else None)
        ms = _SEASON.search(t) or _SEASON_ORD.search(t)
        if ms:
            season = int(ms.group(1))
    if ep is not None:
        return {"entry": entry, "anime": entry["anime_name"], "season": season or 1,
                "ep": ep, "ep_end": ep_end, "movie": False, "movie_label": ""}
    mm = _MOVIE.search(t)
    if mm:
        n = int(mm.group(1)) if mm.group(1) else 1
        label = f"{entry['anime_name']} Movie" + (f" {n}" if mm.group(1) else "")
        return {"entry": entry, "anime": entry["anime_name"], "season": MOVIE_SEASON,
                "ep": n, "ep_end": n, "movie": True, "movie_label": label}
    return None


def _label(d: dict) -> str:
    if d.get("movie"):
        m = re.search(r"Movie(?: \d+)?$", d.get("movie_label") or "")
        return m.group(0) if m else "Movie"
    s, a, b = d["season"], d["ep"], d.get("ep_end") or d["ep"]
    return f"S{s:02d}E{a:02d}" + (f"-{b:02d}" if b != a else "")


# ─────────────────────────────────────────────
#  Declarations store  (text -> baad ki files)   — restart-proof (DB)
# ─────────────────────────────────────────────
_DECLS: list = []                 # [{msg_id, ts, text, entry, anime, season, ep, ep_end, movie, movie_label}]
_DECLS_LOADED = False
_TEXT_PENDING: set = set()        # (chat_id, msg_id) — text abhi process ho raha hai
_DKEY = "dslots"


async def _load_decls():
    global _DECLS_LOADED
    if _DECLS_LOADED:
        return
    _DECLS_LOADED = True
    try:
        d = await db.col2.find_one({"id": _DKEY}) or {}
        _DECLS[:] = list(d.get("items") or [])
    except Exception as e:
        LOGGER.warning(f"[Direct] decl load fail: {e!r}")


async def _save_decls():
    try:
        await db.col2.update_one({"id": _DKEY}, {"$set": {"items": _DECLS[-40:], "ts": time.time()}}, upsert=True)
    except Exception as e:
        LOGGER.warning(f"[Direct] decl save fail: {e!r}")


async def _add_decl(d: dict):
    await _load_decls()
    _DECLS[:] = [x for x in _DECLS if x.get("msg_id") != d["msg_id"]] + [d]
    _DECLS.sort(key=lambda x: x.get("msg_id", 0))
    del _DECLS[:-40]
    await _save_decls()


async def _decl_for(msg_id: int) -> dict | None:
    """File se pehle ka sabse latest text (msg id chhoti) — TTL ke andar."""
    await _load_decls()
    now = time.time()
    best = None
    for d in _DECLS:
        if d.get("msg_id", 0) < msg_id and now - d.get("ts", 0) <= SLOT_TTL:
            if best is None or d["msg_id"] > best["msg_id"]:
                best = d
    return best


async def _wait_texts(chat_id: int, msg_id: int, timeout: float = 8.0):
    end = time.time() + timeout
    while time.time() < end and any(c == chat_id and i < msg_id for c, i in _TEXT_PENDING):
        await asyncio.sleep(0.3)


# ─────────────────────────────────────────────
#  Notify owner (DM)
# ─────────────────────────────────────────────
async def _notify(client, text: str, important: bool = True):
    """important=True -> hamesha DM (fail / match nahi hua). False -> sirf notify level 'all' mein."""
    try:
        from .auto_monitor import _notify_owner, _get_notify_level
        if not important and (await _get_notify_level()) != "all":
            return
        await _notify_owner(client, text)
    except Exception as e:
        LOGGER.warning(f"[Direct] notify fail: {e!r}")


_LOOKS_DECL = re.compile(r"(?<![a-z0-9])(?:episodes?|eps?|e\s*\d|s\s*\d|season|movie|film)", re.I)


async def _set_decl_from_text(client: Client, message: Message, edited: bool = False):
    from .auto_monitor import _get_anime_list
    text = (message.text or "").strip()
    if not text:
        return
    anime_list = await _get_anime_list()
    d = parse_declaration(text, anime_list)
    if d is None:
        if _LOOKS_DECL.search(text):
            await _notify(client, f"⚠️ <b>Direct channel</b>: text samajh nahi aaya / anime <code>/add_anime</code> list mein nahi\n"
                                  f"<code>{html.escape(text[:120])}</code>\n\n"
                                  f"Aise likho: <code>Naruto S01 episode 01</code> • <code>Naruto S01 episode 01 - 02</code> "
                                  f"• <code>Naruto Movie</code>")
        return
    d.update(msg_id=message.id, ts=time.time(), text=text[:200])
    await _add_decl(d)
    LOGGER.info(f"[Direct] 📝 slot: {d['anime']} {_label(d)} (msg {message.id})")
    kind = "🎞 Movie" if d["movie"] else "📺 Episode"
    await _notify(client, f"📝 <b>{'Slot update' if edited else 'Slot set'}</b>\n🎌 <b>{html.escape(d['anime'])}</b>\n"
                          f"{kind}: <code>{d.get('movie_label') if d['movie'] else _label(d)}</code>\n\n"
                          f"Ab files bhejo: 360p/480p → 720p → 1080p.")


@Client.on_message(filters.channel & filters.text, group=-7)
async def direct_text_handler(client: Client, message: Message):
    k = (message.chat.id, message.id)
    _TEXT_PENDING.add(k)                      # sync — taaki file handler ruk kar is text ka wait kare
    try:
        dc = await get_direct_channel()
        if not dc or message.chat.id != dc:
            return
        await _set_decl_from_text(client, message)
    except Exception as e:
        import traceback
        LOGGER.error(f"[Direct] text handler crash: {e}\n{traceback.format_exc()}")
    finally:
        _TEXT_PENDING.discard(k)


@Client.on_edited_message(filters.channel & filters.text, group=-7)
async def direct_text_edit_handler(client: Client, message: Message):
    k = (message.chat.id, message.id)
    _TEXT_PENDING.add(k)
    try:
        dc = await get_direct_channel()
        if not dc or message.chat.id != dc:
            return
        await _set_decl_from_text(client, message, edited=True)
    except Exception as e:
        LOGGER.error(f"[Direct] text edit handler crash: {e!r}")
    finally:
        _TEXT_PENDING.discard(k)


@Client.on_message(filters.command("direct_slot") & filters.private)
async def cmd_direct_slot(client: Client, message: Message):
    if not _is_authorized(message.from_user.id):
        return
    parts = (message.text or "").split()
    await _load_decls()
    if len(parts) >= 2 and parts[1].lower() in ("clear", "reset"):
        _DECLS.clear()
        await _save_decls()
        await message.reply("🧹 Slot saaf. Ab files filename/caption se detect hongi — ya naya text bhejo.")
        return
    now = time.time()
    live = [d for d in _DECLS if now - d.get("ts", 0) <= SLOT_TTL]
    if not live:
        await message.reply("📝 Koi slot set nahi. Direct channel mein text bhejo: <code>Naruto S01 episode 01</code>",
                            parse_mode=ParseMode.HTML)
        return
    d = live[-1]
    await message.reply(f"📝 <b>Current slot</b>\n🎌 <b>{html.escape(d['anime'])}</b>\n"
                        f"{'🎞 Movie' if d.get('movie') else '📺 Episode'}: <code>{_label(d)}</code>\n"
                        f"<code>/direct_slot clear</code> — slot hatao", parse_mode=ParseMode.HTML)


# ─────────────────────────────────────────────
#  File parsing
# ─────────────────────────────────────────────
_4K_RE = re.compile(r"(?<![a-z0-9])4\s*k(?![a-z0-9])", re.I)


def _real_quality(text: str) -> str | None:
    """Text se asli quality (480p 480p hi rehti hai — tier alag se nikalta hai)."""
    toks = eg._Q.findall(eg.norm(text or ""))
    if toks:
        return max((f"{t}p" for t in toks), key=_rank)
    if text and _4K_RE.search(text):
        return "2160p"
    return None


def _quality_of(fname: str, cap: str, height: int | None) -> str | None:
    for t in (fname, cap):
        q = _real_quality(t)
        if q:
            return q
    if height:
        for lim, q in ((1800, "2160p"), (900, "1080p"), (600, "720p"), (420, "480p"), (300, "360p")):
            if height >= lim:
                return q
    return None


def _lang_of(fname: str, cap: str) -> str:
    try:
        from ..utils.auto_caption import detect_language_from_filename
        langs = detect_language_from_filename(fname or "")
        if langs:
            return " + ".join(langs)
    except Exception:
        pass
    m = _LANG_RE.search(f"{fname} {cap}")
    if m:
        w = re.sub(r"\s+", " ", m.group(1)).title()
        return w
    return "Hindi"          # channel Hindi-dub ka hai (auto-monitor bhi sirf Hindi leta hai)


def _parse_file(m: Message) -> dict | None:
    """Direct channel ki file -> info dict (ya None agar video nahi)."""
    media = m.video or m.document
    if media is None:
        return None
    kind = "video"
    if m.document and not m.video:
        mime = (m.document.mime_type or "").lower()
        name = (m.document.file_name or "").lower()
        if not (mime.startswith("video/") or name.endswith((".mkv", ".mp4", ".webm", ".avi", ".mov", ".m4v", ".ts"))):
            return None
        kind = "document"
    fname = getattr(media, "file_name", None) or ""
    cap = m.caption or ""
    fs, fe = eg.parse(fname)
    cs, ce = eg.parse(cap)
    height = getattr(media, "height", None)
    q = _quality_of(fname, cap, height)
    return {
        "kind": kind, "file_id": media.file_id, "fname": fname, "cap": cap,
        "f_season": fs or cs, "f_ep": fe or ce, "q": q, "tier": _tier(q) if q else None,
        "lang": _lang_of(fname, cap),
        "msg_id": m.id, "chat_id": m.chat.id,
    }


# ─────────────────────────────────────────────
#  Per-episode state
# ─────────────────────────────────────────────
class _Ep:
    def __init__(self, key, d: dict, ctx: bool):
        self.key = key                      # (channel_id, season, ep)
        self.anime = d["anime"]
        self.entry = d["entry"]
        self.ep_end = d.get("ep_end") or key[2]
        self.movie = bool(d.get("movie"))
        self.movie_label = d.get("movie_label") or ""
        self.ctx = ctx                      # True = text-declaration se aaya (update post pehli upload ke turant baad)
        self.lock = asyncio.Lock()
        self.pending: dict = {}             # tier -> info
        self.uploaded: set = set()          # tiers
        self.finalized = False
        self.update_sent = False
        self.border_ids: list = []
        self.end_ids: list = []
        self.first_done = False
        self.timer = None
        self.touched = time.time()

    @property
    def label(self) -> str:
        ch, season, ep = self.key
        return _label({"movie": self.movie, "movie_label": self.movie_label, "season": season,
                       "ep": ep, "ep_end": self.ep_end})


_STATES: dict = {}
_SL = None                                  # lazy asyncio.Lock (state creation)
_INTRO_SENT: set = set()                    # (channel_id, season)
_SEEN: dict = {}                            # (chat, msg) -> ts


def _pid(key) -> str:
    return f"dstate_{key[0]}_{key[1]}_{key[2]}"


async def _persist(st: _Ep):
    try:
        doc = {"finalized": st.finalized, "update_sent": st.update_sent,
               "border_ids": st.border_ids, "end_ids": st.end_ids, "ts": time.time()}
        if st.movie:       # movie ki channel-scan se pehchaan nahi hoti — isliye uploaded tiers DB mein
            doc["uploaded"] = sorted(st.uploaded)
        await db.col2.update_one({"id": _pid(st.key)}, {"$set": doc}, upsert=True)
    except Exception as e:
        LOGGER.warning(f"[Direct] state persist fail: {e!r}")


async def _get_state(key, d: dict, ctx: bool) -> _Ep:
    global _SL
    if _SL is None:
        _SL = asyncio.Lock()
    async with _SL:
        now = time.time()
        for k in [k for k, s in _STATES.items() if now - s.touched > 24 * 3600 and not s.pending]:
            _STATES.pop(k, None)
        st = _STATES.get(key)
        if st is None:
            st = _Ep(key, d, ctx)
            try:   # restart ke baad bhi 4K aaye to border/end msgs ka hisaab mile
                doc = await db.col2.find_one({"id": _pid(key)}) or {}
                st.finalized = bool(doc.get("finalized"))
                st.update_sent = bool(doc.get("update_sent"))
                st.border_ids = list(doc.get("border_ids") or [])
                st.end_ids = list(doc.get("end_ids") or [])
                if st.movie:
                    st.uploaded = set(doc.get("uploaded") or [])
                if st.finalized:
                    eg.mark_complete(*key)
            except Exception:
                pass
            _STATES[key] = st
        elif ctx:
            st.ctx = True
        st.touched = now
        return st


# ─────────────────────────────────────────────
#  Entry point  (file handler)
# ─────────────────────────────────────────────
@Client.on_message(filters.channel & (filters.video | filters.document), group=-8)
async def direct_channel_handler(client: Client, message: Message):
    try:
        dc = await get_direct_channel()
        if not dc or message.chat.id != dc:
            return
        k = (message.chat.id, message.id)
        now = time.time()
        if k in _SEEN:
            return
        _SEEN[k] = now
        if len(_SEEN) > 400:
            for kk in sorted(_SEEN, key=_SEEN.get)[:200]:
                _SEEN.pop(kk, None)
        await _on_file(client, message)
    except Exception as e:
        import traceback
        LOGGER.error(f"[Direct] handler crash: {e}\n{traceback.format_exc()}")
        await _notify(client, f"💥 <b>Direct channel error</b>\n<code>{html.escape(type(e).__name__)}: "
                              f"{html.escape(str(e)[:300])}</code>")


def _decl_from_file(info: dict, anime_list: list) -> dict | None:
    """Text slot nahi hai -> filename + caption se anime/season/episode(range)/movie."""
    from .auto_monitor import _find_matching_anime, _season_hint_from_text
    blob = f"{info['fname']} {info['cap']}"
    d = parse_declaration(blob, anime_list)
    if d:
        return d
    entry = _find_matching_anime(blob, anime_list)
    if entry and info["f_ep"]:      # purana tareeka: "[Group] Naruto - 05" jaise naam (eg.parse)
        season = info["f_season"] or _season_hint_from_text(info["fname"], info["cap"]) or 1
        return {"entry": entry, "anime": entry["anime_name"], "season": season, "ep": int(info["f_ep"]),
                "ep_end": int(info["f_ep"]), "movie": False, "movie_label": ""}
    return None


async def _on_file(client: Client, m: Message):
    info = _parse_file(m)
    if info is None:
        return
    from .auto_monitor import _get_anime_list

    fname, cap = info["fname"], info["cap"]
    label = html.escape(fname or cap[:60] or "file")

    # text ke baad turant file aaye to text ka process hone ka thoda wait (ordering)
    await asyncio.sleep(1.5)
    await _wait_texts(m.chat.id, m.id)

    anime_list = await _get_anime_list()
    d = await _decl_for(m.id)
    ctx = d is not None
    if d is None:
        d = _decl_from_file(info, anime_list)
    if d is None:
        await _notify(client, f"⚠️ <b>Direct channel</b>: anime/episode pata nahi chala\n<code>{label}</code>\n\n"
                              f"Pehle channel mein text likho: <code>Naruto S01 episode 01</code> (anime <code>/add_anime</code> list mein ho), "
                              f"phir file bhejo.")
        return
    if not info["q"]:
        await _notify(client, f"⚠️ <b>Direct channel</b>: quality nahi mili (360p/480p/720p/1080p/4K)\n<code>{label}</code>\n"
                              f"Filename/caption mein quality likho (e.g. 720p).")
        return

    if ctx and not d.get("movie") and info["f_ep"] and not (d["ep"] <= int(info["f_ep"]) <= (d.get("ep_end") or d["ep"])):
        await _notify(client, f"⚠️ <b>Direct channel</b>: file ke naam mein <code>E{int(info['f_ep']):02d}</code> hai par slot "
                              f"<code>{_label(d)}</code> hai — slot ke hisaab se upload ho rahi hai.\n<code>{label}</code>")

    target = int(d["entry"]["channel_id"])
    key = (target, d["season"], d["ep"])
    st = await _get_state(key, d, ctx)
    t, q = info["tier"], info["q"]

    if t in st.pending or t in st.uploaded:
        LOGGER.info(f"[Direct] {st.anime} {st.label} {q}: dobara aayi — skip")
        await _notify(client, f"♻️ Direct: <b>{html.escape(st.anime)}</b> {st.label} "
                              f"<code>{q}</code> pehle hi aa chuki hai — skip", important=False)
        return

    LOGGER.info(f"[Direct] ✅ {st.anime} {st.label} {q} ({'slot' if ctx else 'filename'}) -> channel {target}")
    st.pending[t] = info
    await _drain(client, st)


# ─────────────────────────────────────────────
#  Drain: ready files upload -> phir finalize
# ─────────────────────────────────────────────
def _ready(t: str, have: set, force: bool) -> bool:
    if force:
        return True
    have = {_tier(x) for x in have}
    return all(b in have for b in BASE_ORDER if _rank(b) < _rank(t))


async def _present(client, st: _Ep, wanted) -> set:
    ch, season, ep = st.key
    try:
        oid = await _owner_id()
        pres, _noq, _ok = await eg.present_qualities(client, ch, oid, season, ep, list(wanted))
        return {_tier(x) for x in pres}
    except Exception as e:
        LOGGER.warning(f"[Direct] present check error (fail-open): {e!r}")
        return set()


async def _drain(client, st: _Ep, force: bool = False):
    async with st.lock:
        if st.timer is not None:
            try:
                st.timer.cancel()
            except Exception:
                pass
            st.timer = None
        while st.pending:
            have = st.uploaded | await _present(client, st, BASE_ORDER)
            t = min(st.pending, key=_rank)
            if not _ready(t, have, force):
                break
            info = st.pending.pop(t)
            await _upload_item(client, st, t, info)
        await _maybe_finalize(client, st)
        if st.pending and not force:
            loop = asyncio.get_event_loop()
            st.timer = loop.call_later(
                ORDER_GRACE, lambda: asyncio.ensure_future(_drain(client, st, force=True)))


# ─────────────────────────────────────────────
#  Upload one
# ─────────────────────────────────────────────
async def _delete_ids(client, ch: int, ids: list):
    ids = [i for i in ids if i]
    if not ids:
        return
    try:
        await client.delete_messages(ch, ids)
    except Exception:
        for i in ids:
            try:
                await client.delete_messages(ch, i)
                await asyncio.sleep(0.2)
            except Exception:
                pass


async def _delete_old_bot_msgs(client, ch: int):
    """Pichhle episode ke end/schedule messages (naye episode ki pehli file se pehle)."""
    try:
        from ..utils.old_msgs import purge_old_bot_msgs
        await purge_old_bot_msgs(client, ch)   # DB ids + channel scan (restart-proof)
    except Exception as e:
        LOGGER.warning(f"[Direct] old msgs delete error: {e!r}")


async def _make_caption(st: _Ep, info: dict, rq: str) -> str:
    """rq = file ki ASLI quality (480p 480p hi dikhega)."""
    from ..utils import caption_style
    from ..utils.auto_caption import build_auto_caption, get_caption_data
    from ..utils.community import get_community_tag

    ch, season, ep = st.key
    uid = (await _owner_id()) or 0
    lang = info.get("lang") or "Hindi"
    tag = await get_community_tag(uid, all_names=True)
    if st.movie:
        fname = f"{st.movie_label or st.anime + ' Movie'} [{lang}] {rq}.mkv"
    else:
        fname = f"{st.anime} S{season:02d}E{ep:02d} [{lang}] {rq}.mkv"

    def _default():
        cap = build_auto_caption(fname, resolution=rq.replace("p", ""), channel=tag)
        if not st.movie and st.ep_end != ep:        # ek file mein 2+ episode: S01E01 -> S01E01-02
            cap = re.sub(rf"(S{season:02d}E){ep:02d}(?!\d)", rf"\g<1>{ep:02d}-{st.ep_end:02d}", cap, count=1)
        proper = re.sub(r'[<>:"/\\|?*]', "", cap).strip()
        return f"<b>{html.escape(proper)}</b>"

    style_id = caption_style.get_current_style_id()
    if st.movie or style_id == caption_style.DEFAULT_STYLE_ID:
        return _default()
    try:
        data = get_caption_data(None, fname, channel=tag)
        data["quality"] = rq
        data["season"] = season
        data["episode"] = EpRange(ep, st.ep_end) if st.ep_end != ep else ep
        data["anime_name"] = st.anime
        data["genres"] = await caption_style.lookup_genres(st.anime)
        return caption_style.render_caption_html(style_id, data)
    except Exception as e:
        LOGGER.warning(f"[Direct] styled caption fail, default use: {e!r}")
        return _default()


async def _cover_for(st: _Ep, info: dict):
    """custompic keyword (anime naam se) -> warna default /setpic. Photo file_id (instant, upload nahi)."""
    try:
        from .custompic import get_custompic_for_file
        uid = (await _owner_id()) or 0
        text = f"{st.anime} {info['fname']} {info['cap']}".strip()
        pic = await get_custompic_for_file(uid, text)
        if pic:
            return pic
        return await db.get_thumbnail(uid)
    except Exception as e:
        LOGGER.warning(f"[Direct] cover lookup fail: {e!r}")
        return None


async def _send(client, ch: int, info: dict, caption: str, cover):
    from pyrogram.errors import FloodWait
    for attempt in (1, 2):
        try:
            if info["kind"] == "video":
                from ..utils.uploads.telegram import _send_video_cover_safe
                kw = dict(chat_id=ch, video=info["file_id"], caption=caption,
                          parse_mode=ParseMode.HTML, supports_streaming=True)
                if cover:
                    kw["cover"] = cover
                try:
                    return await _send_video_cover_safe(client.send_video, **kw)
                except FloodWait:
                    raise
                except Exception as e:
                    if cover:      # cover problem ho to bina cover bhej do — upload nahi rukna chahiye
                        LOGGER.warning(f"[Direct] cover ke saath send fail ({e!r}) — bina cover retry")
                        kw.pop("cover", None)
                        return await _send_video_cover_safe(client.send_video, **kw)
                    raise
            return await client.send_document(ch, info["file_id"], caption=caption, parse_mode=ParseMode.HTML)
        except FloodWait as fw:
            if attempt == 2:
                raise
            await asyncio.sleep(int(getattr(fw, "value", 5)) + 1)
        except Exception as e:
            LOGGER.warning(f"[Direct] send fail ({e!r}) — copy_message fallback")
            return await client.copy_message(ch, info["chat_id"], info["msg_id"],
                                             caption=caption, parse_mode=ParseMode.HTML)


async def _send_update(client, st: _Ep):
    """Update post — sirf ek baar (movie ke liye nahi)."""
    if st.update_sent or st.movie:
        return
    ch, season, ep = st.key
    try:
        from .update_channel import send_update_post
        await send_update_post(client, anime_name=st.anime, season=season,
                               episode_start=ep, episode_end=st.ep_end)
    except Exception as e:
        LOGGER.error(f"[Direct] update post error: {e!r}")
    st.update_sent = True
    await _persist(st)


async def _upload_item(client, st: _Ep, t: str, info: dict):
    ch, season, ep = st.key
    rq = info["q"]
    tag = f"{st.anime} {st.label} {rq}"

    # 1) auto-monitor isi quality ko abhi upload kar raha ho to skip
    if not eg.claim(ch, season, ep, t):
        LOGGER.info(f"[Direct] ⏭️ {tag}: koi aur upload kar raha hai — skip")
        await _notify(client, f"⏭️ Direct: <b>{html.escape(st.anime)}</b> {st.label} "
                              f"<code>{rq}</code> abhi doosre process se upload ho rahi hai — skip", important=False)
        return
    try:
        # 2) channel mein pehle se hai?
        pres = await _present(client, st, [t])
        if t in pres:
            LOGGER.info(f"[Direct] ⏭️ {tag}: channel mein pehle se hai — skip")
            await _notify(client, f"⏭️ Direct: <b>{html.escape(st.anime)}</b> {st.label} "
                                  f"<code>{rq}</code> channel mein pehle se hai — skip", important=False)
            return

        # 3) Naya upload aaya aur episode "finalized" tha (4K / late file) -> border + end msgs hatao
        if st.finalized:
            await _undo_finalize(client, st)

        # 4) Episode ki PEHLI file: purane end msgs hatao + (Ep 1) season intro
        if not st.first_done:
            others = await _present(client, st, [b for b in BASE_ORDER if b != t])
            if not st.uploaded and not others:
                await _delete_old_bot_msgs(client, ch)
                if not st.movie and ep == 1 and (ch, season) not in _INTRO_SENT:
                    _INTRO_SENT.add((ch, season))
                    try:
                        from .auto_monitor import _send_season_intro
                        await _send_season_intro(client, ch, st.anime, season)
                    except Exception as e:
                        LOGGER.error(f"[Direct] season intro error: {e!r}")
            st.first_done = True

        # 5) caption + cover, phir send
        caption, cover = await asyncio.gather(_make_caption(st, info, rq), _cover_for(st, info))
        sent = await _send(client, ch, info, caption, cover)
        if sent is None:
            raise RuntimeError("send returned None")
        st.uploaded.add(t)
        for e in range(ep, st.ep_end + 1):          # range file: har episode "maujood" mark
            eg.record_upload(ch, season, e, t)
        LOGGER.info(f"[Direct] 📤 {tag} uploaded (msg {getattr(sent, 'id', '?')})")
        if st.movie:
            await _persist(st)

        # 6) text-declaration mode: pehli (360/480p) upload ke turant baad UPDATE POST
        if st.ctx and not st.update_sent:
            await _send_update(client, st)
    except Exception as e:
        LOGGER.error(f"[Direct] upload fail {tag}: {e!r}")
        await _notify(client, f"❌ <b>Direct upload fail</b>\n🎬 {html.escape(st.anime)} {st.label} "
                              f"<code>{rq}</code>\n<code>{html.escape(str(e)[:200])}</code>\n\n"
                              f"Bot ko anime channel mein admin + Post permission chahiye.")
    finally:
        eg.release(ch, season, ep, t)


# ─────────────────────────────────────────────
#  Finalize / undo
# ─────────────────────────────────────────────
async def _maybe_finalize(client, st: _Ep):
    if st.finalized or st.pending or not st.uploaded:
        return
    have = st.uploaded | await _present(client, st, BASE_ORDER)
    if not all(q in {_tier(x) for x in have} for q in REQUIRED):
        return
    await _finalize(client, st)


async def _finalize(client, st: _Ep):
    ch, season, ep = st.key
    # 0) auto-monitor ko signal: is episode ko ab chhodo
    for e in range(ep, st.ep_end + 1):
        eg.mark_complete(ch, season, e)

    # 1) update post (ctx mode mein pehle hi ja chuka hota hai; 4K re-finalize mein dobara nahi)
    await _send_update(client, st)

    # 2) border line
    st.border_ids = []
    try:
        from .auto_monitor import _get_deco, _send_deco_item
        border, _ = await _get_deco()
        if border:
            bm = await _send_deco_item(client, ch, border)
            if bm is not None:
                st.border_ids = [bm.id]
    except Exception as e:
        LOGGER.error(f"[Direct] border error: {e!r}")

    # 3) schedule / end messages  (movie ke liye nahi)
    st.end_ids = []
    if not st.movie:
        try:
            from .schedule_notify import send_schedule_notification, get_last_posted_msg_ids
            await send_schedule_notification(client, ch, st.anime, st.ep_end)
            st.end_ids = list(await get_last_posted_msg_ids(ch) or [])
        except Exception as e:
            LOGGER.error(f"[Direct] schedule/end msg error: {e!r}")

    st.finalized = True
    await _persist(st)
    LOGGER.info(f"[Direct] 🎉 {st.anime} {st.label} complete")
    await _notify(client, f"🎉 Direct: <b>{html.escape(st.anime)}</b> {st.label} complete "
                          f"({' → '.join(sorted(st.uploaded, key=_rank))})", important=False)


async def _undo_finalize(client, st: _Ep):
    ch, season, ep = st.key
    await _delete_ids(client, ch, list(st.border_ids) + list(st.end_ids))
    try:   # DB mein sirf tab saaf karo jab wahi ids saved hon (naye episode ke end msgs na ud jayein)
        from .schedule_notify import get_last_posted_msg_ids, clear_last_posted_msg_ids
        cur = await get_last_posted_msg_ids(ch)
        if cur and (not st.end_ids or set(cur) == set(st.end_ids)):
            await clear_last_posted_msg_ids(ch)
    except Exception as e:
        LOGGER.warning(f"[Direct] undo clear fail: {e!r}")
    st.border_ids, st.end_ids, st.finalized = [], [], False
    await _persist(st)
    LOGGER.info(f"[Direct] ↩️ {st.anime} {st.label}: border + end msgs hata diye (naya upload aa raha hai)")
