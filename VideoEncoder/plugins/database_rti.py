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
  listing page -> post links -> discover_items() -> WatchMultQuality -> Argon -> Swift
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
from pyrogram.errors import FloodWait

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


async def _is_done(post: str, label: str, quality: str, mode: str) -> bool:
    return bool(await _col.find_one({"_id": f"u:{_h(f'{post}|{label}|{quality}|{mode}')}"}, {"_id": 1}))


async def _mark_done(post: str, label: str, quality: str, mode: str):
    await _col.update_one(
        {"_id": f"u:{_h(f'{post}|{label}|{quality}|{mode}')}"},
        {"$set": {"post": post, "label": label, "q": quality, "mode": mode, "ts": time.time()}},
        upsert=True,
    )


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
        self.page = 0
        self.last_page = None
        self.post = ""
        self.item = ""
        self.step = ""
        self.uploaded = 0
        self.skipped = 0
        self.failed = 0
        self.started = 0.0
        self.task = None


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
    th = os.path.join(out_dir, "frame.jpg")
    at = max(1.0, (duration or 0) * 0.1)
    ok = await _run_ff(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{at:.1f}", "-i", path,
                        "-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "5", th], timeout=120)
    return th if ok and os.path.isfile(th) and os.path.getsize(th) < 200 * 1024 else None


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


def _prog_cb(label: str):
    last = [0.0]

    async def _cb(cur, total):
        now = time.time()
        if now - last[0] > 4 and total:
            last[0] = now
            ST.step = f"{label} {cur * 100 // total}%"
    return _cb


async def upload_video_clean(client, chat_id, path, caption, name, thumb, meta):
    fmt = meta.get("format", {})
    vs = next((s for s in meta.get("streams", []) if s.get("codec_type") == "video"), {})
    dur = int(float(fmt.get("duration") or 0))
    return await _retry(lambda: client.send_video(
        chat_id=chat_id, video=path, caption=caption, parse_mode=ParseMode.DISABLED,
        duration=dur, width=int(vs.get("width") or 0), height=int(vs.get("height") or 0),
        thumb=thumb, file_name=name, supports_streaming=True,
        progress=_prog_cb("📤 Video")))


async def upload_file_clean(client, chat_id, path, caption, name):
    return await _retry(lambda: client.send_document(
        chat_id=chat_id, document=path, caption=caption, parse_mode=ParseMode.DISABLED,
        file_name=name, force_document=True, progress=_prog_cb("📤 File")))


# ─────────────────────────────────────────────
#  Per-item pipeline
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

    ST.step = "🔗 Link resolve"
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
        ST.step = "⬇️ Swift download"
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
                ST.step = f"🔁 {q} mirror server"
                try:
                    p = await fb.fetch_missing(post_url, int(item["num"]), q, dl_dir,
                                               prog={"cancel": ST.cancel_ev, "dls": {}})
                except Exception as e:  # noqa: BLE001
                    LOGGER.error(f"[DB-RTI] fallback error: {e!r}")
                    p = None
                if p and os.path.isfile(p):
                    got[q] = p
    return got


async def process_item(client, cfg: dict, post_url: str, title: str, season: int, item: dict, hint: str):
    label = item["label"]
    vch, fch = cfg["video"], cfg["file"]

    need = []
    for q in TARGET_QUALITIES:
        if not (await _is_done(post_url, label, q, "video") and await _is_done(post_url, label, q, "file")):
            need.append(q)
    if not need:
        ST.skipped += 1
        return

    dl_dir = os.path.join(download_dir, f"dbrti_{int(time.time())}_{_h(post_url + label)[:6]}")
    os.makedirs(dl_dir, exist_ok=True)
    try:
        files = await _gather_files(post_url, item, need, dl_dir)
        for q in TARGET_QUALITIES:
            if q not in files:
                continue
            if _cancelled():
                return
            src = files[q]
            if os.path.getsize(src) > MAX_BYTES:
                LOGGER.warning(f"[DB-RTI] {label} {q}: file bot limit se badi — skip")
                continue
            stem = build_name(title, season, item, q)
            ST.step = f"🧹 {q} metadata clean"
            clean, langs = await clean_file(src, dl_dir, stem, hint)
            if not clean:
                LOGGER.warning(f"[DB-RTI] {label} {q}: clean fail — skip")
                continue
            meta = await asyncio.get_event_loop().run_in_executor(None, _probe, clean)
            dur = float((meta.get("format") or {}).get("duration") or 0)
            thumb = await make_thumb(clean, dl_dir, dur)
            caption = stem + (f"\nAudio: {' + '.join(dict.fromkeys(langs))}" if langs else "")
            fname = f"{stem}.mp4"

            if not await _is_done(post_url, label, q, "video"):
                ST.step = f"📤 {q} video"
                await upload_video_clean(client, vch, clean, caption, fname, thumb, meta)
                await _mark_done(post_url, label, q, "video")
            if not await _is_done(post_url, label, q, "file"):
                ST.step = f"📤 {q} file"
                await upload_file_clean(client, fch, clean, caption, fname)
                await _mark_done(post_url, label, q, "file")
            ST.uploaded += 1
            try:
                os.remove(clean)
            except Exception:
                pass

        left = [q for q in TARGET_QUALITIES if q not in files and q in need]
        if not _cancelled():
            await _item_finish(post_url, label, left)
    finally:
        shutil.rmtree(dl_dir, ignore_errors=True)


async def process_post(client, cfg: dict, post_url: str):
    from .rti_downloader import discover_items
    loop = asyncio.get_event_loop()
    ST.post = post_url
    ST.step = "📄 Post scan"
    data = None
    for _ in range(2):
        try:
            data = await loop.run_in_executor(None, discover_items, post_url)
            break
        except Exception as e:  # noqa: BLE001
            LOGGER.warning(f"[DB-RTI] discover fail {post_url}: {e!r}")
            await asyncio.sleep(3)
    if not data or not data.get("items"):
        ST.failed += 1
        return
    title, season = data["title"], data.get("season", 1)
    hint = "hindi" if re.search(r"hindi", title, re.I) else ""
    for item in data["items"]:
        if _cancelled():
            return
        ST.item = item["label"]
        try:
            await process_item(client, cfg, post_url, title, season, item, hint)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            ST.failed += 1
            LOGGER.error(f"[DB-RTI] item error {post_url} {item['label']}: {e!r}\n{traceback.format_exc()}")
            await asyncio.sleep(5)


async def run_crawl(client, cfg: dict, start: int, end, single_url: str = None, notify_chat=None):
    ST.running, ST.stop, ST.started = True, False, time.time()
    ST.cancel_ev.clear()
    ST.uploaded = ST.skipped = ST.failed = 0
    loop = asyncio.get_event_loop()
    try:
        page = start
        while True:
            if ST.stop or (end and page > end):
                break
            ST.page = page
            url = single_url or listing_url(page)
            ST.step = f"📚 Listing page {page}"
            try:
                code, posts = await loop.run_in_executor(None, fetch_listing, url)
            except Exception as e:  # noqa: BLE001
                LOGGER.error(f"[DB-RTI] listing fail {url}: {e!r}")
                await asyncio.sleep(10)
                try:
                    code, posts = await loop.run_in_executor(None, fetch_listing, url)
                except Exception:
                    break
            if code == 404 or not posts:
                ST.last_page = page - 1
                break
            for p in posts:
                if ST.stop:
                    break
                await process_post(client, cfg, p)
            if single_url:
                break
            page += 1
            await asyncio.sleep(2)
    finally:
        ST.running = False
        ST.step = "⏹ Stopped" if ST.stop else "✅ Finished"
        if notify_chat:
            try:
                await client.send_message(
                    notify_chat,
                    f"{ST.step}\nUploaded: {ST.uploaded} • Skipped: {ST.skipped} • Failed: {ST.failed}")
            except Exception:
                pass


# ─────────────────────────────────────────────
#  Command
# ─────────────────────────────────────────────
def _status_text(cfg: dict) -> str:
    v = cfg.get("video")
    f = cfg.get("file")
    lines = [
        "<b>📦 Database RTI</b>",
        f"DB-1 (video): <code>{v or 'not set'}</code>",
        f"DB-2 (file): <code>{f or 'not set'}</code>",
    ]
    if ST.running:
        el = int(time.time() - ST.started)
        lines += [
            "",
            f"▶️ Running • {el // 3600}h {el % 3600 // 60}m",
            f"Page: <code>{ST.page}</code>",
            f"Item: <code>{html.escape(ST.item or '-')}</code>",
            f"Step: {html.escape(ST.step or '-')}",
        ]
    else:
        lines += ["", f"⏸ Idle ({html.escape(ST.step or '-')})"]
    lines.append(f"Uploaded: <code>{ST.uploaded}</code> • Skipped: <code>{ST.skipped}</code> • "
                 f"Failed: <code>{ST.failed}</code>")
    return "\n".join(lines)


_HELP = (
    "<b>/database_rti</b>\n"
    "<code>/database_rti video &lt;channel_id&gt;</code> — DB-1 (video)\n"
    "<code>/database_rti file &lt;channel_id&gt;</code> — DB-2 (file)\n"
    "<code>/database_rti start [from] [to]</code> — crawl shuru\n"
    "<code>/database_rti start &lt;listing_url&gt;</code> — ek listing page\n"
    "<code>/database_rti stop</code> • <code>status</code> • <code>reset</code> • <code>retry</code>"
)


@Client.on_message(filters.command("database_rti"))
async def database_rti_cmd(client: Client, message):
    if not await check_chat(message, "Sudo"):
        return
    args = message.command[1:]
    cfg = await _cfg_get()
    cfg.setdefault("video", int(os.getenv("DB_RTI_VIDEO_CHANNEL", "0") or 0) or None)
    cfg.setdefault("file", int(os.getenv("DB_RTI_FILE_CHANNEL", "0") or 0) or None)

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

    if sub == "stop":
        if not ST.running:
            return await message.reply("Kuch chal hi nahi raha.")
        ST.stop = True
        ST.cancel_ev.set()
        return await message.reply("🛑 Stop requested — current step khatam hote hi ruk jayega.")

    if sub == "reset":
        if ST.running:
            return await message.reply("Pehle /database_rti stop karo.")
        r = await _col.delete_many({"_id": {"$ne": "cfg"}})
        return await message.reply(f"🗑 Progress reset ({r.deleted_count} records).")

    if sub == "retry":
        r = await _col.delete_many({"_id": {"$regex": "^i:"}, "missing.0": {"$exists": True}})
        return await message.reply(f"🔁 {r.deleted_count} adhure items dobara try honge (next start pe).")

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
        else:
            nums = [int(x) for x in rest if x.isdigit()]
            if nums:
                start = nums[0]
            if len(nums) > 1:
                end = nums[1]
        ST.task = asyncio.create_task(run_crawl(client, cfg, start, end, single, message.chat.id))
        rng = f"{start}–{end or 'last'}" if not single else single
        return await message.reply(f"🚀 Database crawl started ({rng}).\n"
                                   f"Progress: /database_rti status")

    await message.reply(_HELP, parse_mode=ParseMode.HTML)
