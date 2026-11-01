"""
database_rti.py
================
RareAnimes (RTI) ki poori site ko 2 Telegram "database" channels mein bharne ka bot.

  /database_rti                        -> help + abhi ka status
  /database_rti start [from] [to]      -> page `from` se `to` tak (default: 1 se last page tak)
  /database_rti start <listing_url>    -> ek custom listing page (sirf wahi page)
  /database_rti stop                   -> rok do (current item khatam hone ke baad rukta hai)
  /database_rti status                 -> live status
  /database_rti video <channel_id>     -> DB-1 channel (VIDEO format) set karo
  /database_rti file  <channel_id>     -> DB-2 channel (FILE/document format) set karo
  /database_rti reset                  -> saara progress (done list) saaf — dobara se shuru
  /database_rti retry                  -> jo items adhure reh gaye the unhe dobara try karne ke liye mark karo

Flow (har post / har episode / har movie ke liye):
  listing page -> post links -> discover_hindi() [SIRF Hindi] -> WatchMultQuality -> Argon -> Swift
  -> 360p / 720p / 1080p download (jo Swift pe na mile wo rti_fallback ke mirrors se:
     mega / hubcloud / watchnow / dlbeta)
  -> CLEAN metadata (koi promotion nahi: na RTI, na @sbanime, na website; audio track
     ka naam sirf language) -> DB-1 pe VIDEO, DB-2 pe FILE (document).

ENV (optional — ya /database_rti video|file se runtime pe set karo):
  DB_RTI_VIDEO_CHANNEL=-100xxxxxxxxxx
  DB_RTI_FILE_CHANNEL=-100xxxxxxxxxx
  DB_RTI_BASE=https://www.rareanimes.fans          (site ka base)
  DB_RTI_FALLBACK_QUALITIES=720p,1080p             (kin qualities ke liye mirror server try ho)
  DB_RTI_MAX_BYTES=2040109465                      (isse badi file skip — bot upload limit ~2GB)
"""

import asyncio
import hashlib
import html
import io
import json
import os
import re
import shutil
import subprocess
import threading
import time
import traceback
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait, MessageNotModified

from .. import LOGGER, download_dir
from ..utils.helper import check_chat
from ..utils.database.access_db import db
from ..utils.track_branding import lang_info

BASE = os.getenv("DB_RTI_BASE", "https://www.rareanimes.fans").rstrip("/")
TARGET_QUALITIES = ("360p", "720p", "1080p")
MAX_BYTES = int(os.getenv("DB_RTI_MAX_BYTES", str(2040109465)))
FB_QUALITIES = tuple(
    q.strip().lower()
    for q in os.getenv("DB_RTI_FALLBACK_QUALITIES", "720p,1080p").split(",")
    if q.strip()
)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

# ─────────────────────────────────────────────
#  Mongo helpers (progress + channel config)
# ─────────────────────────────────────────────
_col = db.db["db_rti"]


async def _cfg_get() -> dict:
    return (await _col.find_one({"_id": "cfg"})) or {}


async def _cfg_set(**kw):
    await _col.update_one({"_id": "cfg"}, {"$set": kw}, upsert=True)


def _h(s: str) -> str:
    return hashlib.sha1(s.encode()).hexdigest()


def _ckey(title: str, season: int, label: str, quality: str, mode: str) -> str:
    """Content-level key: same anime + same SEASON + same episode + same quality (alag post pe bhi)."""
    return f"c:{_h(f'{clean_title(title).lower()}|{int(season)}|{label}|{quality}|{mode}')}"


async def _is_done(post: str, label: str, quality: str, mode: str, ckey: str = None) -> bool:
    if await _col.find_one({"_id": f"u:{_h(f'{post}|{label}|{quality}|{mode}')}"}, {"_id": 1}):
        return True
    # content-level check sirf tab jab season page/URL se pakka pata chala (explicit)
    return bool(ckey and await _col.find_one({"_id": ckey}, {"_id": 1}))


async def _mark_done(post: str, label: str, quality: str, mode: str, ckey: str = None):
    await _col.update_one(
        {"_id": f"u:{_h(f'{post}|{label}|{quality}|{mode}')}"},
        {"$set": {"post": post, "label": label, "q": quality, "mode": mode, "ts": time.time()}},
        upsert=True,
    )
    if ckey:
        await _col.update_one({"_id": ckey}, {"$set": {"post": post, "ts": time.time()}}, upsert=True)


async def _mark_post_complete(post: str, name: str, page: int, note: str = ""):
    await _col.update_one(
        {"_id": f"p:{_h(post)}"},
        {"$set": {"post": post, "name": name, "page": page, "note": note, "ts": time.time()}},
        upsert=True,
    )


async def _post_complete_doc(post: str):
    return await _col.find_one({"_id": f"p:{_h(post)}"})


async def _item_state(post: str, label: str):
    return await _col.find_one({"_id": f"i:{_h(f'{post}|{label}')}"})


async def _item_finish(post: str, label: str, missing: list):
    await _col.update_one(
        {"_id": f"i:{_h(f'{post}|{label}')}"},
        {"$set": {"post": post, "label": label, "missing": missing, "ts": time.time()}},
        upsert=True,
    )


# ─────────────────────────────────────────────
#  Run state (in-memory)
# ─────────────────────────────────────────────
class _State:
    def __init__(self):
        self.running = False
        self.stop = False
        self.cancel_ev = threading.Event()   # scrape/fallback thread ke liye
        self.last_page = None
        self.uploaded = 0
        self.skipped = 0
        self.failed = 0
        self.posts_done = 0
        self.nonhindi = 0
        self.started = 0.0
        self.task = None
        self.status_msg = None
        self.notify_chat = None
        self.last_complete = ""
        self.resume_page = 1
        # downloader side
        self.dl_page = 0
        self.dl_title = ""
        self.dl_item = ""
        self.dl_step = ""
        self.dl_dir = ""
        self.dl_t0 = 0.0
        # uploader side
        self.up_page = 0
        self.up_title = ""
        self.up_item = ""
        self.up_step = ""
        self.up_prog = {}


ST = _State()


# ─────────────────────────────────────────────
#  Clean naming — koi promotion nahi
# ─────────────────────────────────────────────
_PROMO_RE = re.compile(
    r"(rare\s*toons?\s*india|raretoonsindia|rare\s*animes?(?:\.fans)?|rareanimes|\brti\b|sb\s*anime|sbanime|"
    r"@\w+|https?://\S+|www\.\S+|\b\w+\.(?:com|net|org|fans|in|co|xyz|me|to|tv)\b)",
    re.I,
)
_JUNK_RE = re.compile(
    r"\b(hindi|english|japanese|tamil|telugu|dual|multi)\s*(audio|dubbed|dub|subbed|sub)?\b|"
    r"\b(dubbed|subbed|download|watch\s*online|watch|online|free|complete|all\s*episodes?|"
    r"episodes?|batch|web[-\s]?dl|blu-?ray|hdrip|hevc|x26[45]|10\s*bit|esub|esubs|untouched|"
    r"(?:360|480|720|1080|2160)p)\b",
    re.I,
)
_SEASON_RE = re.compile(r"\b(?:season|s)\s*0*(\d{1,2})\b", re.I)


def clean_title(raw: str) -> str:
    t = html.unescape(raw or "")
    t = re.sub(r"\[[^\]]*\]|\([^)]*\)|\{[^}]*\}", " ", t)           # [Multi Audio] (2020) jaise brackets
    t = re.split(r"\s[–—|]\s", t)[0]                               # " – SiteName" suffix
    t = _PROMO_RE.sub(" ", t)
    t = _SEASON_RE.sub(" ", t)
    t = _JUNK_RE.sub(" ", t)
    t = re.sub(r"[<>:\"/\\|?*]", " ", t)
    t = re.sub(r"[\s._]+", " ", t).strip(" -–—.,")
    return t or "Anime"


def clean_text(s: str) -> str:
    """Generic safety net — koi bhi string ho, promo hata do."""
    s = _PROMO_RE.sub(" ", s or "")
    return re.sub(r"\s{2,}", " ", s).strip(" -–—[]()")


def build_name(title: str, season: int, item: dict, quality: str) -> str:
    t = clean_title(title)
    kind, num = item.get("kind"), int(item.get("num") or 0)
    if kind == "MOVIE":
        stem = f"{t} {quality}"
    elif kind == "EP":
        stem = f"{t} S{int(season):02d}E{num:02d} {quality}"
    elif kind == "OVA":
        stem = f"{t} S{int(season):02d} OVA{num} {quality}"
    else:
        stem = f"{t} S{int(season):02d} SP{num} {quality}"
    return re.sub(r"\s+", " ", stem).strip()


_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10}
_SEASON_PATS = [
    re.compile(r"\bseason[\s_-]*0*(\d{1,2})\b", re.I),
    re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)[\s_-]+season\b", re.I),
    re.compile(r"\bs0*(\d{1,2})(?![\d]|e\d)\b", re.I),
]
_SEASON_ROMAN = re.compile(r"\bseason[\s_-]+(ii|iii|iv|v|vi|vii|viii|ix|x)\b", re.I)


def detect_season(title: str, url: str, default: int):
    """(season, explicit) — title phir URL slug se. explicit=False => sirf default mana gaya."""
    slug = urlparse(url).path.replace("/", " ")
    for text in (title or "", slug):
        for pat in _SEASON_PATS:
            m = pat.search(text)
            if m:
                return int(m.group(1)), True
        m = _SEASON_ROMAN.search(text)
        if m:
            return _ROMAN[m.group(1).lower()], True
    return default, False


# ─────────────────────────────────────────────
#  ffprobe / ffmpeg
# ─────────────────────────────────────────────
def _probe(path: str) -> dict:
    try:
        out = subprocess.check_output(
            ["ffprobe", "-hide_banner", "-v", "quiet", "-print_format", "json",
             "-show_streams", "-show_format", path],
            stderr=subprocess.DEVNULL, timeout=90,
        )
        return json.loads(out.decode())
    except Exception as e:
        LOGGER.warning(f"[DB-RTI] ffprobe fail: {e!r}")
        return {}


def _quality_of(path: str, info: dict = None) -> str:
    try:
        from .rti_fallback import _probe_quality
        q = _probe_quality(path)
        if q:
            return q
    except Exception:
        pass
    try:
        from .swift_downloader import _quality_from
        return _quality_from(os.path.basename(path))
    except Exception:
        return "unknown"


_LANG_WORDS = [
    ("hindi", "hin"), ("english", "eng"), ("japanese", "jpn"), ("tamil", "tam"),
    ("telugu", "tel"), ("malayalam", "mal"), ("kannada", "kan"), ("bengali", "ben"),
    ("marathi", "mar"), ("korean", "kor"), ("chinese", "chi"), ("spanish", "spa"),
    ("french", "fre"), ("german", "ger"), ("arabic", "ara"), ("indonesian", "ind"),
    ("portuguese", "por"), ("russian", "rus"),
]


def _stream_lang(s: dict, hint: str = ""):
    """(iso, 'Hindi') — tag, phir stream title ke words, phir hint. Na mile to (None, None)."""
    tags = s.get("tags") or {}
    iso, name = lang_info((tags.get("language") or tags.get("LANGUAGE") or "").strip().lower())
    if name:
        return iso, name
    title = (tags.get("title") or tags.get("TITLE") or "").lower()
    for word, code in _LANG_WORDS:
        if word in title:
            return lang_info(code)
    if hint:
        return lang_info(hint)
    return None, None


def _clean_meta_args(streams: list, title: str, hint: str, with_subs: bool) -> tuple:
    """ffmpeg metadata args + audio language list (caption ke liye)."""
    args = ["-metadata", f"title={title}", "-metadata", "comment=", "-metadata", "description=",
            "-metadata", "encoder=", "-metadata", "artist=", "-metadata", "album=",
            "-metadata", "copyright=", "-metadata", "publisher=",
            "-metadata:s:v:0", "title=", "-metadata:s:v:0", "handler_name=VideoHandler"]
    ai = si = 0
    langs = []
    audio_total = sum(1 for s in streams if s.get("codec_type") == "audio")
    for s in streams:
        ct = s.get("codec_type")
        if ct == "audio":
            # hint sirf tab jab ek hi audio track ho (guess se galat label na lage)
            iso, name = _stream_lang(s, hint if audio_total == 1 else "")
            nm = name or ""
            args += [f"-metadata:s:a:{ai}", f"title={nm}",
                     f"-metadata:s:a:{ai}", f"handler_name={nm or 'SoundHandler'}",
                     f"-metadata:s:a:{ai}", f"language={iso or 'und'}"]
            if name:
                langs.append(name)
            ai += 1
        elif ct == "subtitle" and with_subs:
            iso, name = _stream_lang(s, "english")
            nm = name or ""
            args += [f"-metadata:s:s:{si}", f"title={nm}",
                     f"-metadata:s:s:{si}", f"handler_name={nm or 'SubtitleHandler'}",
                     f"-metadata:s:s:{si}", f"language={iso or 'und'}"]
            si += 1
    return args, langs


async def _run_ff(cmd: list, timeout: int = 7200) -> bool:
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            _, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return False
        if proc.returncode != 0:
            LOGGER.warning(f"[DB-RTI] ffmpeg fail: {(err or b'').decode(errors='replace')[-300:]}")
            return False
        return True
    except asyncio.CancelledError:
        raise
    except Exception as e:
        LOGGER.warning(f"[DB-RTI] ffmpeg crash: {e!r}")
        return False


async def clean_file(src: str, out_dir: str, stem: str, hint: str):
    """
    Source file ko clean MP4 mein badlo: global metadata/chapters/attachments sab saaf,
    sirf title=<clean naam>, audio/subtitle track ka naam = sirf language.
    Returns (path, [audio languages]) ya (None, []).
    """
    loop = asyncio.get_event_loop()
    info = await loop.run_in_executor(None, _probe, src)
    streams = info.get("streams", [])
    has_sub = any(s.get("codec_type") == "subtitle" for s in streams)
    out = os.path.join(out_dir, f"{stem}.mp4")
    if os.path.abspath(out) == os.path.abspath(src):
        out = os.path.join(out_dir, f"{stem}__clean.mp4")

    head = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", src,
            "-map", "0:v:0", "-map", "0:a?"]
    tail_pre = ["-map_metadata", "-1", "-map_chapters", "-1", "-fflags", "+bitexact",
                "-flags:v", "+bitexact", "-flags:a", "+bitexact"]

    attempts = []
    if has_sub:
        m, langs = _clean_meta_args(streams, stem, hint, True)
        attempts.append((head + ["-map", "0:s?"] + tail_pre + ["-c:v", "copy", "-c:a", "copy", "-c:s", "mov_text"]
                         + m + ["-movflags", "+faststart", out], langs))
        attempts.append((head + ["-map", "0:s?"] + tail_pre + ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                                                                  "-c:s", "mov_text"]
                         + m + ["-movflags", "+faststart", out], langs))
    m2, langs2 = _clean_meta_args(streams, stem, hint, False)
    attempts.append((head + tail_pre + ["-sn", "-c:v", "copy", "-c:a", "copy"] + m2
                     + ["-movflags", "+faststart", out], langs2))
    attempts.append((head + tail_pre + ["-sn", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k"] + m2
                     + ["-movflags", "+faststart", out], langs2))

    for cmd, langs in attempts:
        if await _run_ff(cmd):
            if os.path.isfile(out) and os.path.getsize(out) > 0:
                try:
                    if os.path.abspath(src) != os.path.abspath(out):
                        os.remove(src)
                except Exception:
                    pass
                return out, langs
        try:
            if os.path.exists(out):
                os.remove(out)
        except Exception:
            pass
    return None, []


async def make_thumb(path: str, out_dir: str, duration: float):
    """Video ka apna ek frame (custom/branded thumbnail NAHI)."""
    th = os.path.join(out_dir, f"frame_{_h(path)[:8]}.jpg")
    at = max(1.0, (duration or 0) * 0.1)
    ok = await _run_ff(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{at:.1f}", "-i", path,
                        "-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "5", th], timeout=120)
    return th if ok and os.path.isfile(th) and os.path.getsize(th) < 200 * 1024 else None


# ─────────────────────────────────────────────
#  HINDI-ONLY discovery
#  (rti_downloader.discover_items Hindi na milne par English/Japanese link utha leta hai —
#   yahan woh fallback hata diya)
# ─────────────────────────────────────────────
# DB_RTI_AUDIO=hindi            -> sirf Hindi (default)
# DB_RTI_AUDIO=hindi,dual,multi -> Dual/Multi audio posts bhi
AUDIO_ALLOWED = [a.strip().lower() for a in os.getenv("DB_RTI_AUDIO", "hindi").split(",") if a.strip()] or ["hindi"]
VERIFY_AUDIO = os.getenv("DB_RTI_VERIFY_AUDIO", "1") != "0"


def _pick_audio_link(links: list, title: str):
    """Sirf allowed audio wala link. 'unknown' tab hi jab post ke title mein Hindi likha ho."""
    for a in AUDIO_ALLOWED:
        for l in links:
            if l["audio"] == a:
                return l
    if re.search(r"hindi", title or "", re.I):
        for l in links:
            if l["audio"] == "unknown":
                return l
    return None


def _item_label(kind: str, num: int) -> str:
    return {"EP": f"EP{num:02d}", "OVA": f"OVA{num}", "SP": f"SP{num}"}.get(kind, f"{kind}{num}")


def discover_hindi(page_url: str) -> dict:
    """discover_items jaisa hi, par sirf Hindi (allowed audio) links. Returns + 'skipped' (non-Hindi labels)."""
    from . import rti_downloader as rd
    r = requests.get(page_url, headers=rd.HEADERS, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.content, "html.parser")
    title = rd._page_title(soup)
    sm = re.search(r"season\s*(\d+)", title, re.IGNORECASE)
    season = int(sm.group(1)) if sm else 1

    seen, items, skipped = set(), [], {}
    for p in soup.find_all("p"):
        kind, num = rd._match_item(p.get_text(" ", strip=True))
        if not kind or (kind, num) in seen:
            continue
        key = (kind, num)
        links = list(rd._find_wmq_links(p))
        for idx, sib in enumerate(p.find_next_siblings()):
            if idx > 8:
                break
            sk, sn = rd._match_item(sib.get_text(" ", strip=True))
            if sk and (sk, sn) != key:
                break
            links.extend(rd._find_wmq_links(sib))
        if not links:
            continue
        best = _pick_audio_link(links, title)
        if best:
            seen.add(key)
            skipped.pop(key, None)
            items.append({"kind": kind, "num": num, "wmq_link": best["href"]})
        else:
            skipped.setdefault(key, [l["audio"] for l in links])

    if not items:                                   # generic "S01E05 Fandub" style pages
        found, gseason = rd._generic_items(soup)
        for key, links in found.items():
            best = _pick_audio_link(links, title)
            if best:
                items.append({"kind": key[0], "num": key[1], "wmq_link": best["href"]})
                skipped.pop(key, None)
            else:
                skipped.setdefault(key, [l["audio"] for l in links])
        if items and not sm and gseason:
            season = gseason

    if not items and not skipped:                   # MOVIE mode
        links = rd._find_wmq_links(soup)
        best = _pick_audio_link(links, title)
        if best:
            return {"title": title, "season": season, "mode": "movie", "skipped": [],
                    "items": [{"kind": "MOVIE", "num": 0, "label": "Movie", "wmq_link": best["href"]}]}
        if links:
            return {"title": title, "season": season, "mode": "movie", "items": [],
                    "skipped": ["Movie"]}

    order = {"EP": 0, "OVA": 1, "SP": 2}
    items.sort(key=lambda x: (order.get(x["kind"], 9), x["num"]))
    for it in items:
        it["label"] = _item_label(it["kind"], it["num"])
    sk = sorted(skipped, key=lambda k: (order.get(k[0], 9), k[1]))
    return {"title": title, "season": season, "mode": "episodes", "items": items,
            "skipped": [_item_label(k, n) for k, n in sk]}


# ─────────────────────────────────────────────
#  Listing crawler
# ─────────────────────────────────────────────
_SKIP_PATH = re.compile(
    r"/(page|category|tag|author|feed|wp-[a-z]+|search|contact|about|dmca|privacy|request|home)(/|$)", re.I)

_POST_SELECTORS = [
    "article h2 a", "article h3 a", "h2.entry-title a", "h3.entry-title a",
    ".entry-title a", ".post-title a", "article a[rel=bookmark]", ".post a[rel=bookmark]",
]


def listing_url(page: int) -> str:
    return f"{BASE}/home/" if page <= 1 else f"{BASE}/home/page/{page}/"


def _get(url: str, timeout: int = 25):
    r = requests.get(url, headers=_HEADERS, timeout=timeout)
    if r.status_code in (403, 503):
        try:
            import cloudscraper
            r = cloudscraper.create_scraper().get(url, headers=_HEADERS, timeout=timeout)
        except Exception:
            pass
    return r


def fetch_listing(url: str):
    """Returns (status_code, [post_urls])."""
    r = _get(url)
    if r.status_code == 404:
        return 404, []
    r.raise_for_status()
    soup = BeautifulSoup(r.content, "html.parser")
    host = urlparse(BASE).netloc.replace("www.", "")
    posts, seen = [], set()

    def _add(href):
        if not href:
            return
        full = urljoin(url, href.split("#")[0]).rstrip("/") + "/"
        pu = urlparse(full)
        if pu.netloc.replace("www.", "") != host:
            return
        if not pu.path.strip("/") or _SKIP_PATH.search(pu.path):
            return
        if full not in seen:
            seen.add(full)
            posts.append(full)

    for sel in _POST_SELECTORS:
        for a in soup.select(sel):
            _add(a.get("href"))
        if posts:
            break
    if not posts:                       # structure ajeeb ho to generic fallback
        for a in soup.find_all("a", href=True):
            _add(a["href"])
    return 200, posts


# ─────────────────────────────────────────────
#  Telegram upload helpers
# ─────────────────────────────────────────────
async def _retry(fn, tries: int = 4):
    last = None
    for n in range(tries):
        try:
            return await fn()
        except FloodWait as fw:
            await asyncio.sleep(int(getattr(fw, "value", 5)) + 1)
            last = fw
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            last = e
            if n == tries - 1:
                break
            await asyncio.sleep(3 * (n + 1))
    raise last


def _prog_cb(key: str):
    last = [0.0]

    async def _cb(cur, total):
        now = time.time()
        if total and now - last[0] > 3:
            last[0] = now
            ST.up_prog[key] = cur * 100 // total
    return _cb


async def upload_video_clean(client, chat_id, path, caption, name, thumb, meta):
    fmt = meta.get("format", {})
    vs = next((s for s in meta.get("streams", []) if s.get("codec_type") == "video"), {})
    dur = int(float(fmt.get("duration") or 0))
    return await _retry(lambda: client.send_video(
        chat_id=chat_id, video=path, caption=caption, parse_mode=ParseMode.DISABLED,
        duration=dur, width=int(vs.get("width") or 0), height=int(vs.get("height") or 0),
        thumb=thumb, file_name=name, supports_streaming=True,
        progress=_prog_cb("video")))


async def upload_file_clean(client, chat_id, path, caption, name):
    return await _retry(lambda: client.send_document(
        chat_id=chat_id, document=path, caption=caption, parse_mode=ParseMode.DISABLED,
        file_name=name, force_document=True, progress=_prog_cb("file")))


async def _notify(client, chat, text: str):
    if not chat:
        return
    for _ in range(2):
        try:
            await client.send_message(chat, text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            return
        except FloodWait as fw:
            await asyncio.sleep(int(getattr(fw, "value", 5)) + 1)
        except Exception:
            return


def _dir_mb(d: str) -> float:
    tot = 0
    try:
        for root, _dirs, files in os.walk(d):
            for f in files:
                try:
                    tot += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
    except Exception:
        pass
    return tot / (1024 * 1024)


# ─────────────────────────────────────────────
#  DOWNLOADER side (producer)
# ─────────────────────────────────────────────
def _resolve_swift(wmq_link: str):
    from .rti_downloader import get_argon_link, argon_to_swift
    argon = get_argon_link(wmq_link)
    return argon_to_swift(argon) if argon else None


def _cancelled() -> bool:
    return ST.stop or ST.cancel_ev.is_set()


async def _gather_files(post_url: str, item: dict, need: list, dl_dir: str) -> dict:
    """{quality: filepath} — pehle Swift, jo na mile wo mirror servers se."""
    from .swift_downloader import _scrape_and_download
    loop = asyncio.get_event_loop()
    got: dict = {}

    ST.dl_step = "🔗 Link resolve"
    swift = None
    for _ in range(2):
        if _cancelled():
            return got
        try:
            swift = await loop.run_in_executor(None, _resolve_swift, item["wmq_link"])
        except Exception as e:  # noqa: BLE001
            LOGGER.warning(f"[DB-RTI] resolve error: {e!r}")
        if swift:
            break

    if swift:
        ST.dl_step = "⬇️ Swift download"
        prog = {"phase": "scan", "dls": {}, "cancel": ST.cancel_ev}
        try:
            res = await loop.run_in_executor(
                None, lambda: _scrape_and_download(swift, dl_dir, None, None, progress=prog))
        except Exception as e:  # noqa: BLE001
            LOGGER.error(f"[DB-RTI] scrape crash: {e!r}")
            res = {"files": []}
        files = res.get("files") or []
        by_q: dict = {}
        for f in files:
            q = _quality_of(f)
            if q in need and q not in by_q:
                by_q[q] = f
            else:
                try:
                    os.remove(f)          # 480p / 2160p / duplicate — kaam ke nahi
                except Exception:
                    pass
        got.update(by_q)

    missing = [q for q in need if q not in got]
    can_fb = item.get("kind") == "EP" and int(item.get("num") or 0) > 0
    if missing and can_fb:
        try:
            from . import rti_fallback as fb
        except Exception:
            fb = None
        if fb is not None and fb.enabled():
            for q in missing:
                if _cancelled():
                    break
                if q not in FB_QUALITIES:
                    continue
                ST.dl_step = f"🔁 {q} mirror server"
                try:
                    p = await fb.fetch_missing(post_url, int(item["num"]), q, dl_dir,
                                               prog={"cancel": ST.cancel_ev, "dls": {}})
                except Exception as e:  # noqa: BLE001
                    LOGGER.error(f"[DB-RTI] fallback error: {e!r}")
                    p = None
                if p and os.path.isfile(p):
                    got[q] = p
    return got



async def _prepare_quality(post: dict, item: dict, q: str, src: str, dl_dir: str):
    stem = build_name(post["title"], post["season"], item, q)
    clean, langs = await clean_file(src, dl_dir, stem, post["hint"])
    if not clean:
        LOGGER.warning(f"[DB-RTI] {item['label']} {q}: clean fail — skip")
        return None
    loop = asyncio.get_event_loop()
    meta = await loop.run_in_executor(None, _probe, clean)
    dur = float((meta.get("format") or {}).get("duration") or 0)
    thumb = await make_thumb(clean, dl_dir, dur)
    caption = stem + (f"\nAudio: {' + '.join(dict.fromkeys(langs))}" if langs else "")
    return {"q": q, "path": clean, "meta": meta, "thumb": thumb, "caption": caption,
            "name": f"{stem}.mp4", "langs": langs}


async def prepare_item(post: dict, item: dict):
    """Download + clean. Returns upload-job dict ya None."""
    label, url = item["label"], post["url"]
    ST.dl_item = label
    need = []
    for q in TARGET_QUALITIES:
        ckv = _ckey(post["title"], post["season"], label, q, "video") if post["explicit"] else None
        ckf = _ckey(post["title"], post["season"], label, q, "file") if post["explicit"] else None
        if not (await _is_done(url, label, q, "video", ckv) and await _is_done(url, label, q, "file", ckf)):
            need.append(q)
    if not need:
        ST.skipped += 1
        # is post ka apna kaam pehle ho chuka tha (sirf content-duplicate nahi)? -> finish_post ke liye note
        own = True
        for q in TARGET_QUALITIES:
            for mode in ("video", "file"):
                if not await _is_done(url, label, q, mode):
                    own = False
        if own:
            post["own_done"] += 1
        return None

    dl_dir = os.path.join(download_dir, f"dbrti_{int(time.time())}_{_h(url + label)[:6]}")
    os.makedirs(dl_dir, exist_ok=True)
    ST.dl_dir, ST.dl_t0 = dl_dir, time.time()
    try:
        files = await _gather_files(url, item, need, dl_dir)
        if _cancelled():
            shutil.rmtree(dl_dir, ignore_errors=True)
            return None
        ST.dl_step = "🧹 Metadata clean"
        results = await asyncio.gather(*[
            _prepare_quality(post, item, q, files[q], dl_dir) for q in TARGET_QUALITIES if q in files
        ])
        ready = []
        wrong_audio = False
        for r in results:
            if r and VERIFY_AUDIO and r["langs"] and "Hindi" not in r["langs"] \
                    and AUDIO_ALLOWED == ["hindi"]:
                # link Hindi bola par file ke saare audio tracks explicitly non-Hindi — upload nahi
                LOGGER.warning(f"[DB-RTI] {label} {r['q']}: audio {r['langs']} — Hindi nahi, skip")
                wrong_audio = True
                try:
                    os.remove(r["path"])
                except Exception:
                    pass
                continue
            if r and os.path.getsize(r["path"]) <= MAX_BYTES:
                ready.append(r)
            elif r:
                LOGGER.warning(f"[DB-RTI] {label} {r['q']}: bot limit se badi file — skip")
        if wrong_audio:
            post["wrong"].append(label)
            if label not in post["nonhindi"]:
                post["nonhindi"].append(label)
        got = {r["q"] for r in ready}
        for q in need:
            if q not in got and not wrong_audio:
                post["missing"].append(f"{label} {q}")
        if not ready:
            shutil.rmtree(dl_dir, ignore_errors=True)
            if not wrong_audio and not (_cancelled() and not files):
                await _item_finish(url, label, list(need))
            return None
        return {"kind": "item", "post": post, "item": item, "files": ready, "dl_dir": dl_dir}
    except BaseException:
        shutil.rmtree(dl_dir, ignore_errors=True)
        raise


async def produce_post(client, q: asyncio.Queue, url: str, page: int):
    loop = asyncio.get_event_loop()
    ST.dl_page, ST.dl_title, ST.dl_item, ST.dl_dir = page, url.rstrip("/").split("/")[-1], "", ""
    ST.dl_step = "📄 Post scan"
    data = None
    for _ in range(2):
        try:
            data = await loop.run_in_executor(None, discover_hindi, url)
            break
        except Exception as e:  # noqa: BLE001
            LOGGER.warning(f"[DB-RTI] discover fail {url}: {e!r}")
            await asyncio.sleep(3)
    if data and not data.get("items") and data.get("skipped"):
        ST.nonhindi += 1
        await _notify(client_ref(), ST.notify_chat,
                      f"⏭ Hindi nahi hai, skip • Page <code>{page}</code>\n{html.escape(clean_title(data['title']))}")
        return
    if not data or not data.get("items"):
        ST.failed += 1
        await _notify(client_ref(), ST.notify_chat,
                      f"⚠️ Post me episodes/links nahi mile • Page <code>{page}</code>\n{html.escape(url)}")
        return
    title = data["title"]
    season, explicit = detect_season(title, url, data.get("season", 1))
    post = {
        "url": url, "title": title, "season": season, "explicit": explicit, "page": page,
        "hint": "hindi" if re.search(r"hindi", title, re.I) else "",
        "mode": data.get("mode", "episodes"), "missing": [], "failed": [], "uploaded": 0, "own_done": 0,
        "nonhindi": list(data.get("skipped") or []), "wrong": [],
    }
    ST.dl_title = post_name(post)
    for item in data["items"]:
        if _cancelled():
            return
        try:
            job = await prepare_item(post, item)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            ST.failed += 1
            post["failed"].append(item["label"])
            LOGGER.error(f"[DB-RTI] item error {url} {item['label']}: {e!r}\n{traceback.format_exc()}")
            await _notify(client_ref(), ST.notify_chat,
                          f"❌ Download error • Page <code>{page}</code> • {html.escape(post_name(post))} "
                          f"{item['label']}\nResume: <code>/database_rti start {page}</code>")
            await asyncio.sleep(5)
            continue
        if job:
            await q.put(job)          # uploader busy ho to yahin rukta hai (max 1 item aage)
    if not _cancelled():
        await q.put({"kind": "post_end", "post": post})


def post_name(post: dict) -> str:
    base = clean_title(post["title"])
    if post.get("mode") == "movie":
        return f"{base} (Movie)"
    return f"{base} S{int(post['season']):02d}"


_CLIENT = {"c": None}


def client_ref():
    return _CLIENT["c"]


# ─────────────────────────────────────────────
#  UPLOADER side (consumer)
# ─────────────────────────────────────────────
async def upload_job(client, cfg: dict, job: dict):
    post, item = job["post"], job["item"]
    label, url = item["label"], post["url"]
    ST.up_page, ST.up_title, ST.up_item = post["page"], post_name(post), label
    ST.resume_page = post["page"]
    vch, fch = cfg["video"], cfg["file"]
    try:
        for f in job["files"]:
            q = f["q"]
            ST.up_prog = {}
            ST.up_step = q
            ckv = _ckey(post["title"], post["season"], label, q, "video") if post["explicit"] else None
            ckf = _ckey(post["title"], post["season"], label, q, "file") if post["explicit"] else None

            async def _v():
                if not await _is_done(url, label, q, "video", ckv):
                    await upload_video_clean(client, vch, f["path"], f["caption"], f["name"], f["thumb"], f["meta"])
                    await _mark_done(url, label, q, "video", ckv)

            async def _f():
                if not await _is_done(url, label, q, "file", ckf):
                    await upload_file_clean(client, fch, f["path"], f["caption"], f["name"])
                    await _mark_done(url, label, q, "file", ckf)

            res = await asyncio.gather(_v(), _f(), return_exceptions=True)
            errs = [r for r in res if isinstance(r, BaseException)]
            if errs:
                raise errs[0]
            ST.uploaded += 1
            post["uploaded"] += 1
            try:
                os.remove(f["path"])
            except Exception:
                pass
        await _item_finish(url, label, [m.split()[-1] for m in post["missing"] if m.startswith(label + " ")])
        await _cfg_set(resume_page=post["page"])
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        post["failed"].append(label)
        ST.failed += 1
        LOGGER.error(f"[DB-RTI] upload error {url} {label}: {e!r}\n{traceback.format_exc()}")
        await _notify(client, ST.notify_chat,
                      f"❌ Upload error • Page <code>{post['page']}</code> • {html.escape(post_name(post))} {label}\n"
                      f"<code>{html.escape(str(e)[:200])}</code>\n"
                      f"Resume: <code>/database_rti start {post['page']}</code>")
    finally:
        shutil.rmtree(job["dl_dir"], ignore_errors=True)
        ST.up_item, ST.up_step, ST.up_prog = "", "", {}


async def finish_post(client, post: dict):
    name = post_name(post)
    if post["failed"]:
        await _notify(client, ST.notify_chat,
                      f"⚠️ <b>Incomplete:</b> {html.escape(name)}\nPage <code>{post['page']}</code> • "
                      f"failed: {html.escape(', '.join(post['failed']))}\n"
                      f"Resume: <code>/database_rti start {post['page']}</code>")
        return
    existing = await _post_complete_doc(post["url"])
    if post["uploaded"] == 0 and (existing or post["own_done"] == 0):
        if post["wrong"] and not existing:
            ST.nonhindi += 1
            await _notify(client, ST.notify_chat,
                          f"🚫 <b>Non-Hindi, upload nahi hua:</b> {html.escape(name)}\n"
                          f"Page <code>{post['page']}</code> • {html.escape(', '.join(post['wrong']))}")
        return          # kuch naya nahi / sirf duplicate post — list mein dobara mat daalo
    note = ", ".join(post["missing"])
    await _mark_post_complete(post["url"], name, post["page"], note)
    ST.posts_done += 1
    ST.last_complete = name
    tag = "🆕 Updated" if existing else "✅ Complete"
    txt = f"{tag}: <b>{html.escape(name)}</b>\n📄 Page <code>{post['page']}</code>"
    if note:
        txt += f"\n⚠️ Missing: {html.escape(note)}"
    if post["nonhindi"]:
        txt += f"\n🚫 Non-Hindi skipped: {html.escape(', '.join(post['nonhindi']))}"
    await _notify(client, ST.notify_chat, txt)
    await _cfg_set(resume_page=post["page"])


async def _consumer(client, cfg: dict, q: asyncio.Queue):
    while True:
        job = await q.get()
        if job is None:
            return
        try:
            if job["kind"] == "item":
                await upload_job(client, cfg, job)
            else:
                await finish_post(client, job["post"])
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            LOGGER.error(f"[DB-RTI] consumer error: {e!r}\n{traceback.format_exc()}")


# ─────────────────────────────────────────────
#  Live status
# ─────────────────────────────────────────────
def _status_text(cfg: dict) -> str:
    v, f = cfg.get("video"), cfg.get("file")
    L = ["<b>📦 Database RTI</b>"]
    if ST.running:
        el = int(time.time() - ST.started)
        L.append(f"▶️ Running • {el // 3600}h {el % 3600 // 60}m")
        L.append("")
        dl = f"⬇️ <b>Download</b> • Page <code>{ST.dl_page}</code>\n   {html.escape(ST.dl_title or '-')}"
        if ST.dl_item:
            dl += f" • <code>{html.escape(ST.dl_item)}</code>"
        dl += f"\n   {html.escape(ST.dl_step or '-')}"
        if ST.dl_dir and ST.dl_t0:
            mb = _dir_mb(ST.dl_dir)
            sp = mb / max(time.time() - ST.dl_t0, 1)
            dl += f" • {mb:.0f} MB ({sp:.1f} MB/s)"
        L.append(dl)
        L.append("")
        if ST.up_item:
            up = (f"⬆️ <b>Upload</b> • Page <code>{ST.up_page}</code>\n   {html.escape(ST.up_title)} • "
                  f"<code>{html.escape(ST.up_item)}</code> • {html.escape(ST.up_step)}")
            pv, pf = ST.up_prog.get("video"), ST.up_prog.get("file")
            up += f"\n   🎞 Video {pv if pv is not None else 0}% • 📁 File {pf if pf is not None else 0}%"
            L.append(up)
        else:
            L.append("⬆️ <b>Upload</b> • idle")
    else:
        L.append(f"⏸ Idle ({html.escape(ST.dl_step or '-')})")
    L.append("")
    L.append(f"✅ Posts complete: <code>{ST.posts_done}</code> • 📤 Files: <code>{ST.uploaded}</code> • "
             f"⏭ Skipped: <code>{ST.skipped}</code> • 🚫 Non-Hindi: <code>{ST.nonhindi}</code> • ❌ Failed: <code>{ST.failed}</code>")
    if ST.last_complete:
        L.append(f"Last complete: {html.escape(ST.last_complete)}")
    rp = ST.resume_page if ST.running or ST.uploaded or ST.posts_done else (cfg.get("resume_page") or 1)
    L.append(f"🔁 Resume page: <code>{rp}</code> → <code>/database_rti start {rp}</code>")
    L.append(f"DB-1: <code>{v or 'not set'}</code> • DB-2: <code>{f or 'not set'}</code>")
    return "\n".join(L)


async def _render(cfg: dict):
    if not ST.status_msg:
        return
    try:
        await ST.status_msg.edit_text(_status_text(cfg), parse_mode=ParseMode.HTML)
    except MessageNotModified:
        pass
    except FloodWait as fw:
        await asyncio.sleep(int(getattr(fw, "value", 5)) + 1)
    except Exception:
        pass


async def _status_loop(cfg: dict):
    while True:
        await asyncio.sleep(6)
        await _render(cfg)


# ─────────────────────────────────────────────
#  Crawl driver
# ─────────────────────────────────────────────
async def run_crawl(client, cfg: dict, start: int, end, single_url: str = None):
    ST.running, ST.stop, ST.started = True, False, time.time()
    ST.cancel_ev.clear()
    ST.uploaded = ST.skipped = ST.failed = ST.posts_done = ST.nonhindi = 0
    ST.last_complete = ""
    ST.resume_page = start
    _CLIENT["c"] = client
    loop = asyncio.get_event_loop()
    q: asyncio.Queue = asyncio.Queue(maxsize=1)
    consumer = asyncio.create_task(_consumer(client, cfg, q))
    try:
        ST.status_msg = await client.send_message(ST.notify_chat, _status_text(cfg), parse_mode=ParseMode.HTML)
    except Exception:
        ST.status_msg = None
    updater = asyncio.create_task(_status_loop(cfg))
    try:
        page = start
        while True:
            if ST.stop or (end and page > end):
                break
            ST.dl_page = page
            url = single_url or listing_url(page)
            ST.dl_title, ST.dl_item, ST.dl_dir = "", "", ""
            ST.dl_step = f"📚 Listing page {page}"
            code, posts = None, []
            for attempt in range(3):
                try:
                    code, posts = await loop.run_in_executor(None, fetch_listing, url)
                    break
                except Exception as e:  # noqa: BLE001
                    LOGGER.error(f"[DB-RTI] listing fail {url}: {e!r}")
                    await asyncio.sleep(8)
            if code is None:
                await _notify(client, ST.notify_chat,
                              f"❌ Listing page <code>{page}</code> load nahi hua.\n"
                              f"Resume: <code>/database_rti start {page}</code>")
                break
            if code == 404 or not posts:
                ST.last_page = page - 1
                break
            for p in posts:
                if ST.stop:
                    break
                await produce_post(client, q, p, page)
            if single_url:
                break
            page += 1
            await asyncio.sleep(2)
    except Exception as e:  # noqa: BLE001
        LOGGER.error(f"[DB-RTI] crawl crash: {e!r}\n{traceback.format_exc()}")
        await _notify(client, ST.notify_chat,
                      f"❌ Crawl crash • Page <code>{ST.dl_page}</code>\n<code>{html.escape(str(e)[:200])}</code>\n"
                      f"Resume: <code>/database_rti start {ST.resume_page}</code>")
    finally:
        await q.put(None)
        await consumer
        updater.cancel()
        ST.running = False
        ST.dl_step = "⏹ Stopped" if ST.stop else "✅ Finished"
        ST.dl_dir = ""
        await _cfg_set(resume_page=ST.resume_page)
        await _render(cfg)
        await _notify(client, ST.notify_chat,
                      f"{ST.dl_step}\nComplete posts: {ST.posts_done} • Files: {ST.uploaded} • "
                      f"Skipped: {ST.skipped} • Failed: {ST.failed}\n"
                      f"Resume page: <code>{ST.resume_page}</code>")


# ─────────────────────────────────────────────
#  Command
# ─────────────────────────────────────────────
_HELP = (
    "<b>/database_rti</b>\n"
    "<code>/database_rti video &lt;channel_id&gt;</code> — DB-1 (video)\n"
    "<code>/database_rti file &lt;channel_id&gt;</code> — DB-2 (file)\n"
    "<code>/database_rti start [from] [to]</code> — crawl shuru\n"
    "<code>/database_rti start resume</code> — last page se aage\n"
    "<code>/database_rti start &lt;listing_url&gt;</code> — ek listing page\n"
    "<code>/database_rti list</code> — complete hui saari seasons/movies ki list (txt)\n"
    "<code>/database_rti stop</code> • <code>status</code> • <code>reset</code> • <code>retry</code>"
)


@Client.on_message(filters.command("database_rti"))
async def database_rti_cmd(client: Client, message):
    if not await check_chat(message, "Sudo"):
        return
    args = message.command[1:]
    cfg = await _cfg_get()
    cfg["video"] = cfg.get("video") or (int(os.getenv("DB_RTI_VIDEO_CHANNEL", "0") or 0) or None)
    cfg["file"] = cfg.get("file") or (int(os.getenv("DB_RTI_FILE_CHANNEL", "0") or 0) or None)

    if not args:
        return await message.reply(f"{_status_text(cfg)}\n\n{_HELP}", parse_mode=ParseMode.HTML)
    sub = args[0].lower()

    if sub in ("video", "file"):
        if len(args) < 2 or not re.fullmatch(r"-?\d+", args[1]):
            return await message.reply("Channel id do: <code>/database_rti video -100123...</code>",
                                       parse_mode=ParseMode.HTML)
        cid = int(args[1])
        try:
            chat = await client.get_chat(cid)
        except Exception as e:  # noqa: BLE001
            return await message.reply(f"❌ Channel access nahi: <code>{html.escape(str(e))}</code>\n"
                                       f"Bot ko channel mein admin banao.", parse_mode=ParseMode.HTML)
        await _cfg_set(**{sub: cid})
        return await message.reply(f"✅ DB ({sub}) set: <b>{html.escape(chat.title or str(cid))}</b>",
                                   parse_mode=ParseMode.HTML)

    if sub == "status":
        return await message.reply(_status_text(cfg), parse_mode=ParseMode.HTML)

    if sub == "list":
        docs = await _col.find({"_id": {"$regex": "^p:"}}).sort("ts", 1).to_list(length=None)
        if not docs:
            return await message.reply("Abhi koi season/movie complete nahi hui.")
        lines = []
        for i, d in enumerate(docs, 1):
            extra = f"  [missing: {d['note']}]" if d.get("note") else ""
            lines.append(f"{i}. {d.get('name')}{extra}")
        bio = io.BytesIO("\n".join(lines).encode("utf-8"))
        bio.name = "database_rti_complete_list.txt"
        return await message.reply_document(bio, caption=f"✅ {len(docs)} complete")

    if sub == "stop":
        if not ST.running:
            return await message.reply("Kuch chal hi nahi raha.")
        ST.stop = True
        ST.cancel_ev.set()
        return await message.reply("🛑 Stop requested — current upload khatam hote hi ruk jayega.")

    if sub == "reset":
        if ST.running:
            return await message.reply("Pehle /database_rti stop karo.")
        r = await _col.delete_many({"_id": {"$ne": "cfg"}})
        await _cfg_set(resume_page=1)
        return await message.reply(f"🗑 Progress reset ({r.deleted_count} records).")

    if sub == "retry":
        r1 = await _col.delete_many({"_id": {"$regex": "^i:"}, "missing.0": {"$exists": True}})
        r2 = await _col.delete_many({"_id": {"$regex": "^p:"}, "note": {"$nin": ["", None]}})
        return await message.reply(f"🔁 {r1.deleted_count} items / {r2.deleted_count} posts dobara try honge (next start pe).")

    if sub == "start":
        if ST.running:
            return await message.reply("Already running. /database_rti status dekho.")
        if not cfg.get("video") or not cfg.get("file"):
            return await message.reply("Pehle dono channel set karo:\n"
                                       "<code>/database_rti video &lt;id&gt;</code>\n"
                                       "<code>/database_rti file &lt;id&gt;</code>", parse_mode=ParseMode.HTML)
        try:
            from .rti_downloader import SELENIUM_OK
            if not SELENIUM_OK:
                return await message.reply("❌ Selenium install nahi hai.")
        except Exception as e:  # noqa: BLE001
            return await message.reply(f"❌ rti_downloader load nahi hua: {e!r}")
        for cid in (cfg["video"], cfg["file"]):
            try:
                await client.get_chat(cid)
            except Exception as e:  # noqa: BLE001
                return await message.reply(f"❌ Channel <code>{cid}</code> access nahi: "
                                           f"<code>{html.escape(str(e))}</code>", parse_mode=ParseMode.HTML)

        single, start, end = None, 1, None
        rest = args[1:]
        if rest and rest[0].startswith("http"):
            single = rest[0]
        elif rest and rest[0].lower() == "resume":
            start = int(cfg.get("resume_page") or 1)
        else:
            nums = [int(x) for x in rest if x.isdigit()]
            if nums:
                start = nums[0]
            if len(nums) > 1:
                end = nums[1]
        ST.notify_chat = message.chat.id
        ST.task = asyncio.create_task(run_crawl(client, cfg, start, end, single))
        return

    await message.reply(_HELP, parse_mode=ParseMode.HTML)
