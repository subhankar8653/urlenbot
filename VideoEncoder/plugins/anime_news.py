"""
anime_news.py
=============
Anime News ko RSS se channel mein auto-post karo (Anime-News-Bot ka logic, ab urlenbot ke andar).

  Source : RSS feeds (default: Anime News Network) — panel/commands se add/remove
           Site 403 de to auto-fallback: direct → rss2json proxy → Google News RSS (AniNewsAPI wali trick)
  Image  : 🥇 AniList poster  →  🥈 article ka figure/og:image  →  🥉 text-only
  Post   : photo + title + chhota summary + "Read it fully" button — set kiye channel mein
  Check  : har N minute (default 10). Sirf NAYE links post hote hain, duplicate kabhi nahi.

Commands (owner/sudo, private chat):
  /add_anime_news_channel <channel_id>   — news channel set karo
  /rem_anime_news_channel                — channel hatao (auto-post band)
  /anime_news [on|off]                   — status / chalu / band
  /anime_news_interval <min>             — kitne minute mein check ho (5-360)
  /anime_news_now                        — abhi check karke post karo (test)
  /add_news_rss <url>                    — RSS feed add karo (max 3)
  /rem_news_rss <url|number>             — RSS feed hatao
  /view_news_rss                         — feeds dekho

/customise → ⏰ Cleanup & Schedule → 📰 Anime News  (sab buttons se bhi)

Data:
  db.col2 {'id': 'anime_news_cfg'} → {'cfg': {channel_id, channel_title, enabled, interval (min),
        feeds [url], footer, last_check (epoch), last_error, posted_total}}
  db.db['anime_news_seen']          → har post hua link (sha1 _id) — 60 din baad TTL se apne aap delete
"""

import asyncio
import base64
import hashlib
import io
import html as _html
import json
import re
import time as _time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urljoin, urlparse

import aiohttp
import feedparser
from bs4 import BeautifulSoup
from pyrogram import Client, filters, StopPropagation, ContinuePropagation
from pyrogram.enums import ParseMode
from pyrogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)

from .. import LOGGER, owner, sudo_users
from ..utils.database.access_db import db

CFG_ID = "anime_news_cfg"
SEEN = db.db["anime_news_seen"]

IST = timezone(timedelta(hours=5, minutes=30))
DEFAULT_FEEDS = ["https://www.animenewsnetwork.com/all/rss.xml"]
FEED_PRESETS = [             # 📚 Popular feeds button (AniNewsAPI ke sources)
    ("Anime News Network", "https://www.animenewsnetwork.com/all/rss.xml"),
    ("Anime Corner", "https://animecorner.me/feed/"),
    ("Anime Herald", "https://www.animeherald.com/feed/"),
    ("ComicBook Anime", "https://comicbook.com/anime/feed/"),
    ("Otaku USA", "https://otakuusamagazine.com/feed/"),
]
RSS2JSON = "https://api.rss2json.com/v1/api.json?rss_url={u}"
GNEWS = "https://news.google.com/rss/search?q=site:{h}+when:7d&hl=en-US&gl=US&ceid=US:en"
MAX_FEEDS = 3
ENTRIES_PER_FEED = 3         # har feed ke latest itne items dekhe jaate hain
DEFAULT_INTERVAL = 10        # minute
MIN_INTERVAL = 5
MAX_INTERVAL = 360
INTERVAL_PRESETS = [5, 10, 15, 30, 60, 120]
SEEN_TTL_DAYS = 60
SUMMARY_MAX = 250
CAPTION_MAX = 1000           # Telegram photo caption limit 1024
POST_GAP = 3                 # do posts ke beech sec
ALERT_GAP = 6 * 3600         # same error ka owner DM itne sec mein ek hi baar
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

_SESS: dict = {}             # user_id → {"mode": "channel" | "feed"}
_LOCK = asyncio.Lock()       # auto + manual cycle ek saath na chalein
_WAKE = asyncio.Event()      # channel set / toggle ON hone par loop ko turant jagao
_ALERT_AT: dict = {}
_INDEX_DONE = False


# ───────────────────────── config ─────────────────────────
async def _cfg() -> dict:
    doc = await db.col2.find_one({"id": CFG_ID})
    return dict((doc or {}).get("cfg") or {})


async def _set(**kw):
    """Sirf diye hue fields badalta hai (poora cfg overwrite nahi — loop aur panel ek saath safe)."""
    await db.col2.update_one({"id": CFG_ID}, {"$set": {f"cfg.{k}": v for k, v in kw.items()}}, upsert=True)


async def _unset(*keys):
    await db.col2.update_one({"id": CFG_ID}, {"$unset": {f"cfg.{k}": "" for k in keys}})


def _is_auth(uid: int) -> bool:
    return uid in owner or uid in sudo_users


def clear_session(uid: int):
    _SESS.pop(uid, None)


def _feeds(cfg: dict) -> list:
    """Kuch set nahi kiya to default feed; set kiya (khaali bhi) to wahi."""
    f = cfg.get("feeds")
    return list(DEFAULT_FEEDS) if f is None else list(f)


def _interval(cfg: dict) -> int:
    try:
        v = int(cfg.get("interval") or DEFAULT_INTERVAL)
    except (TypeError, ValueError):
        v = DEFAULT_INTERVAL
    return max(MIN_INTERVAL, min(MAX_INTERVAL, v))


def _host(url: str) -> str:
    try:
        return urlparse(url).netloc.replace("www.", "") or url
    except Exception:
        return url


def _plain(s) -> str:
    """Panel text (markdown) mein safe rakhne ke liye."""
    return re.sub(r"[*_`\[\]]", "", str(s or ""))


# ───────────────────────── seen-links (duplicate guard) ─────────────────────────
def _hash(link: str) -> str:
    return hashlib.sha1(link.encode("utf-8", "ignore")).hexdigest()


async def _ensure_index():
    global _INDEX_DONE
    if _INDEX_DONE:
        return
    try:
        await SEEN.create_index("ts", expireAfterSeconds=SEEN_TTL_DAYS * 86400)
        _INDEX_DONE = True
    except Exception as e:
        LOGGER.warning(f"[AnimeNews] TTL index nahi bana: {e}")


async def _claim(link: str) -> bool:
    """Atomic: link pehli baar dekha to True (aur lock), warna False — double post impossible."""
    try:
        r = await SEEN.update_one(
            {"_id": _hash(link)},
            {"$setOnInsert": {"link": link, "ts": datetime.utcnow()}},
            upsert=True,
        )
        return r.upserted_id is not None
    except Exception as e:
        LOGGER.error(f"[AnimeNews] claim fail ({link}): {e}")
        return False


async def _release(link: str):
    try:
        await SEEN.delete_one({"_id": _hash(link)})
    except Exception:
        pass


def _tkey(title: str) -> str:
    return "t:" + re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()


async def _claim_item(it: dict) -> bool:
    """
    Link ke saath TITLE bhi claim hota hai — taaki same news fallback (Google link) aur direct feed
    (asli link) dono se aaye to bhi double post na ho.
    """
    if not await _claim(it["link"]):
        return False
    return await _claim(_tkey(it["title"]))


async def _release_item(it: dict):
    await _release(it["link"])
    await _release(_tkey(it["title"]))


# ───────────────────────── text helpers ─────────────────────────
def _e(s) -> str:
    return _html.escape(str(s or ""), quote=False)


def clean_summary(raw: str) -> str:
    text = BeautifulSoup(raw or "", "html.parser").get_text(" ")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > SUMMARY_MAX:
        cut = text[:SUMMARY_MAX].rsplit(" ", 1)[0]
        text = (cut or text[:SUMMARY_MAX]) + "..."
    return text


# ───────────────────────── http (anti-bot safe) ─────────────────────────
async def _http_get(session: aiohttp.ClientSession, url: str, timeout: int = 20, accept: str = "*/*") -> bytes:
    """
    URL ka body (bytes). ANN jaise sites aiohttp ko HTTP 403 dete hain (TLS fingerprint se bot pehchante hain),
    isliye pehle curl_cffi (Chrome jaisa fingerprint). curl_cffi na ho to aiohttp.
    """
    try:
        from curl_cffi import requests as cr
    except Exception:
        cr = None

    if cr is not None:
        def _do():
            last = None
            for imp in ("chrome124", "chrome"):     # purane curl_cffi mein 'chrome124' na ho to 'chrome'
                try:
                    r = cr.get(url, impersonate=imp, headers={"Accept": accept, "Accept-Language": "en-US,en;q=0.9"},
                               timeout=timeout, allow_redirects=True)
                    return r.status_code, r.content
                except Exception as e:
                    last = e
            raise last
        status, body = await asyncio.get_running_loop().run_in_executor(None, _do)
    else:
        async with session.get(url, headers={"User-Agent": UA, "Accept": accept},
                               timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            status, body = resp.status, await resp.read()
    if status != 200:
        hint = " (site ne block kiya — koi aur RSS feed try karo)" if status in (403, 429) else ""
        raise RuntimeError(f"HTTP {status}{hint}")
    return body


# ───────────────────────── images ─────────────────────────
def is_valid_img(src) -> bool:
    """Tracking pixel / placeholder / svg / base64 jaisi nakli images filter."""
    if not src:
        return False
    src = src.strip()
    if not src.startswith("http") or src.startswith("data:") or src.endswith(".svg"):
        return False
    low = src.lower()
    bad = ("pixel", "tracker", "analytics", "beacon", "1x1", "blank", "placeholder",
           "spacer", "logo", "icon", "avatar", "favicon")
    return not any(p in low for p in bad)


async def fetch_image_from_article(session: aiohttp.ClientSession, article_url: str):
    """Article page se best image: <figure> → og:image → data-src → normal <img>."""
    if not article_url:
        return None
    try:
        html_text = (await _http_get(session, article_url, 15, "text/html,*/*")).decode("utf-8", "ignore")
        soup = BeautifulSoup(html_text, "html.parser")

        fig = soup.find("figure")
        if fig:
            tag = fig.find("img")
            if tag:
                src = urljoin(article_url, tag.get("data-src") or tag.get("src") or "")
                if is_valid_img(src):
                    return src

        og = soup.find("meta", attrs={"property": "og:image"})
        if og:
            src = urljoin(article_url, og.get("content") or "")
            if is_valid_img(src):
                return src

        for attr in ("data-src", "src"):
            for tag in soup.find_all("img"):
                src = urljoin(article_url, tag.get(attr) or "")
                if is_valid_img(src):
                    return src
    except Exception as e:
        LOGGER.info(f"[AnimeNews] article image nahi mili ({article_url}): {type(e).__name__}")
    return None


# ───────────────────────── text trimming / body cleaning ─────────────────────────
BODY_MAX = 850               # news body (caption mein) — build_caption limit ke hisaab se aur chhota karta hai


def _trim(text: str, limit: int) -> str:
    """Limit ke andar poora sentence kaat ke rakho (beech mein word na tute)."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    m = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "), cut.rfind("。"))
    if m >= limit * 0.5:
        return cut[:m + 1]
    return cut.rsplit(" ", 1)[0].rstrip(",;:- ") + "…"


def clean_body(raw: str, limit: int = BODY_MAX) -> str:
    """HTML/feed content se readable news paragraph(s) — 'The post ... appeared first on' jaisa kachra hata ke."""
    soup = BeautifulSoup(raw or "", "html.parser")
    for t in soup(["script", "style", "figure", "iframe", "table"]):
        t.decompose()
    paras = [re.sub(r"\s+", " ", p.get_text(" ")).strip() for p in soup.find_all("p")]
    paras = [p for p in paras if len(p) >= 25 and "appeared first on" not in p]
    text = " ".join(paras) if paras else re.sub(r"\s+", " ", soup.get_text(" ")).strip()
    text = re.sub(r"The post .*? appeared first on .*$", "", text).strip()
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    return _trim(text, limit)


def _entry_image(ent, raw_html: str):
    """Feed entry ki apni image: media:content / thumbnail / enclosure / content ka pehla <img>."""
    for k in ("media_content", "media_thumbnail"):
        for m in ent.get(k) or []:
            if is_valid_img(m.get("url")):
                return m["url"]
    for l in ent.get("links") or []:
        if str(l.get("type", "")).startswith("image") and is_valid_img(l.get("href")):
            return l["href"]
    for tag in BeautifulSoup(raw_html or "", "html.parser").find_all("img"):
        src = tag.get("src") or tag.get("data-src")
        if is_valid_img(src):
            return src
    return None


# ───────────────────────── anime name + AniList info ─────────────────────────
_NEWS_VERBS = (r"announces?|announced|reveals?|revealed|unveils?|gets|confirms?|confirmed|streams?|premieres?|adds?|"
               r"casts?|launches|launched|teases?|ends|reports?|sets|delays?|delayed|debuts?|returns?|wins|shares?|"
               r"releases?|opens?|hits|crosses|reaches|will|stars?|previews?|streaming|film|movie|new|full|special")


def extract_anime_names(title: str) -> list:
    """Title se anime/manga ke possible naam (priority order): quotes → ':' → '-' → season/media word → news-verb se pehle."""
    title = (title or "").strip()
    out, seen = [], set()

    def add(v):
        v = (v or "").strip().strip("\"'“”‘’").strip(" -–:")
        if len(v) > 2 and v not in seen:
            out.append(v)
            seen.add(v)

    q = (re.search(r'["“]([^"“”]+)["”]', title)           # pehle double/curly quotes (apostrophe se na tute)
         or re.search(r"'([^']+)'", title))
    if q:
        add(q.group(1))
        if ":" in q.group(1):
            add(q.group(1).split(":")[0])
    c = re.match(r"^(.+?):", title)
    if c:
        add(c.group(1))
    d = re.match(r"^(.+?)\s[-–]\s", title)
    if d and not re.search(r"\b(?:season|vol|ep|chapter)\s*\d*$", d.group(1), re.IGNORECASE):
        add(d.group(1))
    m = re.match(r"^(.+?)\s+(?:season|anime|manga|manhwa|manhua|donghua|vol|volume|episode|ep|ch|chapter)\b",
                 title, re.IGNORECASE)
    if m:
        add(m.group(1))
    v = re.match(rf"^(.+?)\s+(?:{_NEWS_VERBS})\b", title, re.IGNORECASE)
    if v:
        add(v.group(1))
    return out[:4]


def _norm(s) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


_GENERIC = {"best", "top", "new", "anime", "manga", "list", "watch", "netflix", "crunchyroll", "episodes", "episode",
            "season", "release", "date", "india", "hindi", "dub", "dubbed", "streaming", "movies", "movie", "series",
            "updates", "news", "guide", "must", "every", "all", "the", "to", "of", "on", "in", "for", "and", "a"}


def _similar(term: str, names: list) -> bool:
    """
    AniList ka result sach mein usi naam ka hai? Sakht check — galat series ka poster/info lagne se behtar hai kuch na lage.
    Term poora kisi title mein (ya title poora term mein) *poore words* ke saath milna chahiye; generic/listicle term reject.
    """
    t = _norm(term)
    words = t.split()
    if len(t) < 5 or t[0].isdigit() or all(w in _GENERIC or w.isdigit() for w in words):
        return False
    pt = f" {t} "
    for n in names:
        n = _norm(n)
        if len(n) < 4:
            continue
        pn = f" {n} "
        if pt in pn or pn in pt:
            return True
    return False


_ANILIST_Q = """
query ($search: String) {
  anime: Media (search: $search, type: ANIME, sort: POPULARITY_DESC) { ...F }
  manga: Media (search: $search, type: MANGA, sort: POPULARITY_DESC) { ...F }
}
fragment F on Media {
  title { romaji english } synonyms bannerImage coverImage { extraLarge }
  description(asHtml: false) genres averageScore status format episodes seasonYear
  studios(isMain: true) { nodes { name } }
}
"""
_STATUS = {"FINISHED": "Finished", "RELEASING": "Airing", "NOT_YET_RELEASED": "Upcoming",
           "CANCELLED": "Cancelled", "HIATUS": "Hiatus"}


async def get_anilist_info(session: aiohttp.ClientSession, title: str, retries: int = 2):
    """
    Har candidate naam AniList pe dhundo. Pehla *sahi match* (anime > manga) return:
    {name, image, desc, genres, score, status, format, episodes, year, studio}  — nahi mila to None.
    """
    for term in extract_anime_names(title):
        for attempt in range(1, retries + 1):
            try:
                async with session.post(
                    "https://graphql.anilist.co",
                    json={"query": _ANILIST_Q, "variables": {"search": term}},
                    headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": UA},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as r:
                    if r.status == 429:
                        await asyncio.sleep(min(int(r.headers.get("Retry-After", 10)), 30))
                        continue
                    if r.status != 200:
                        await asyncio.sleep(2 * attempt)
                        continue
                    data = (await r.json()).get("data") or {}
                for kind in ("anime", "manga"):
                    md = data.get(kind)
                    if not md:
                        continue
                    t = md.get("title") or {}
                    names = [t.get("romaji"), t.get("english")] + list(md.get("synonyms") or [])
                    if not _similar(term, names):
                        continue
                    desc = BeautifulSoup(md.get("description") or "", "html.parser").get_text(" ")
                    desc = re.sub(r"\(Source:.*?\)|\[Written by.*?\]", "", desc, flags=re.I)
                    studios = ((md.get("studios") or {}).get("nodes") or [])
                    return {
                        "name": t.get("english") or t.get("romaji") or term,
                        "image": (md.get("coverImage") or {}).get("extraLarge") or md.get("bannerImage"),
                        "desc": _trim(desc, 450),
                        "genres": list(md.get("genres") or [])[:3],
                        "score": md.get("averageScore"),
                        "status": _STATUS.get(md.get("status") or "", ""),
                        "format": re.sub(r"\b(Tv|Ova|Ona)\b", lambda m_: m_.group(1).upper(),
                                         (md.get("format") or "").replace("_", " ").title()),
                        "episodes": md.get("episodes") if kind == "anime" else None,
                        "year": md.get("seasonYear"),
                        "studio": studios[0].get("name") if studios else "",
                    }
                break   # is naam pe sahi match nahi → agla candidate
            except asyncio.CancelledError:
                raise
            except Exception as e:
                LOGGER.info(f"[AnimeNews] AniList '{term}' attempt {attempt}: {type(e).__name__}")
                await asyncio.sleep(2 * attempt)
    return None


# ───────────────────────── 16:9 (YouTube-thumbnail) photo ─────────────────────────
THUMB_W, THUMB_H = 1280, 720


def _to_16x9(data: bytes):
    """
    Kisi bhi image ko 1280x720 (16:9) JPEG banao:
      • landscape (≈16:9) → seedha center-crop (sharp)
      • portrait/square/bahut wide poster → blur background + beech mein poori sharp image (dhundhla stretch nahi)
    Fail hone par None (tab original URL hi bheja jaata hai).
    """
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps
    im = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    w, h = im.size
    if w < 200 or h < 150:
        return None
    ar = w / h
    if 1.55 <= ar <= 2.1:
        tw, th = (int(h * 16 / 9), h) if ar > 16 / 9 else (w, int(w * 9 / 16))
        l, t = (w - tw) // 2, (h - th) // 2
        out = im.crop((l, t, l + tw, t + th)).resize((THUMB_W, THUMB_H), Image.LANCZOS)
    else:
        bg = ImageOps.fit(im, (THUMB_W, THUMB_H), Image.LANCZOS).filter(ImageFilter.GaussianBlur(30))
        out = ImageEnhance.Brightness(bg).enhance(0.55)
        scale = min((THUMB_W - 120) / w, (THUMB_H - 40) / h, 2.0)
        fg = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
        shadow = Image.new("RGB", (fg.width + 24, fg.height + 24), (0, 0, 0)).filter(ImageFilter.GaussianBlur(12))
        x, y = (THUMB_W - fg.width) // 2, (THUMB_H - fg.height) // 2
        out.paste(shadow, (x - 12, y - 12))
        out.paste(fg, (x, y))
    buf = io.BytesIO()
    out.save(buf, "JPEG", quality=92, optimize=True)
    return buf.getvalue()


async def _make_photo(session: aiohttp.ClientSession, url: str):
    try:
        raw = await _http_get(session, url, 20, "image/*,*/*")
        if len(raw) > 12 * 1024 * 1024:
            return None
        return await asyncio.get_running_loop().run_in_executor(None, _to_16x9, raw)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        LOGGER.info(f"[AnimeNews] 16:9 photo nahi bani ({type(e).__name__}: {e}) — original URL bhejenge")
        return None


# ───────────────────────── article page (full text + image) ─────────────────────────
async def _gnews_real_url(session: aiohttp.ClientSession, link: str, dbg: list):
    """Google News redirect link → asli article URL (purana base64 format + naya batchexecute format). Fail → None."""
    m = re.search(r"/(?:articles|read)/([^/?]+)", link)
    if not m:
        dbg.append("dec:noid")
        return None
    gid = m.group(1)
    try:
        raw = base64.urlsafe_b64decode(gid + "=" * (-len(gid) % 4)).decode("latin1")
        if "AU_yqL" not in raw:
            u = re.search(r"https?://[\x21-\x7e]+", raw)
            if u:
                dbg.append("dec:b64")
                return u.group(0)
    except Exception:
        pass
    hdr = {"User-Agent": UA, "Cookie": "CONSENT=YES+cb.20230101-00-p0.en+FX+000; SOCS=CAI"}
    try:
        sg = ts = None
        for pu in (f"https://news.google.com/rss/articles/{gid}", f"https://news.google.com/articles/{gid}"):
            async with session.get(pu, headers=hdr, timeout=aiohttp.ClientTimeout(total=15)) as r:
                final, page = str(r.url), await r.text()
            if "news.google." not in urlparse(final).netloc:       # redirect seedha publisher pe ho gaya
                dbg.append("dec:redirect")
                return final
            ms, mt = re.search(r'data-n-a-sg="([^"]+)"', page), re.search(r'data-n-a-ts="([^"]+)"', page)
            if ms and mt:
                sg, ts = ms.group(1), mt.group(1)
                break
        if not sg:
            dbg.append("dec:noparams")
            return None
        ts = int(ts) if str(ts).isdigit() else ts          # timestamp number hona chahiye (string nahi)
        inner = ["garturlreq", [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1, None, None, None, None,
                                 None, 0, 1], "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0], gid, ts, sg]
        freq = json.dumps([[["Fbv4je", json.dumps(inner), None, "generic"]]])
        async with session.post("https://news.google.com/_/DotsSplashUi/data/batchexecute",
                                data="f.req=" + quote(freq),
                                headers={**hdr, "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
                                timeout=aiohttp.ClientTimeout(total=15)) as r:
            body = await r.text()
        parsed = json.loads(body.split("\n\n")[1])[:-2]
        url = json.loads(parsed[0][2])[1]
        if str(url).startswith("http"):
            dbg.append("dec:batch")
            return url
        dbg.append("dec:badurl")
    except Exception as e:
        dbg.append(f"dec:err-{type(e).__name__}")
        LOGGER.warning(f"[AnimeNews] google link decode fail: {type(e).__name__}: {e}")
    return None


def _text_from_html(soup) -> str:
    # 1) JSON-LD NewsArticle.articleBody (TOI, HT, indiatimes jaisi sites mein poora text yahin hota hai)
    for sc in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(sc.string or sc.get_text() or "")
        except Exception:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            d = stack.pop()
            if isinstance(d, dict):
                body = d.get("articleBody")
                if isinstance(body, str) and len(body) > 150:
                    return re.sub(r"\s+", " ", BeautifulSoup(body, "html.parser").get_text(" ")).strip()
                stack.extend(v for v in d.values() if isinstance(v, (dict, list)))
            elif isinstance(d, list):
                stack.extend(d)
    # 2) <p> paragraphs
    root = soup.select_one(".meat") or soup.find("article") or soup.find("main") or soup
    paras = [re.sub(r"\s+", " ", p.get_text(" ")).strip() for p in root.find_all("p")]
    paras = [p for p in paras if len(p) >= 60 and "cookie" not in p.lower()]
    text = " ".join(paras)
    # 3) og:description
    if len(text) < 150:
        for attrs in ({"property": "og:description"}, {"name": "description"}):
            m = soup.find("meta", attrs=attrs)
            d = (m.get("content") if m else "") or ""
            if len(d) > len(text):
                text = d
    return re.sub(r"\s+([,.;:!?])", r"\1", text)


def _from_markdown(md: str):
    """r.jina.ai (reader proxy) ka markdown → (text, image)."""
    img = None
    for m in re.finditer(r"!\[[^\]]*\]\((https?://[^)\s]+)", md):
        if is_valid_img(m.group(1)):
            img = m.group(1)
            break
    paras = []
    for line in md.splitlines():
        line = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", line.strip())
        line = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", line).strip()
        if len(line) < 60 or line[0] in "#|>*-" or re.match(r"(Title|URL Source|Published Time|Markdown Content):", line):
            continue
        paras.append(line)
    return re.sub(r"\s+([,.;:!?])", r"\1", " ".join(paras)), img


async def _article_via_reader(session: aiohttp.ClientSession, url: str, dbg: list) -> dict:
    """r.jina.ai reader (headless browser) — block wali site ya Google redirect page ke liye."""
    try:
        md = (await _http_get(session, "https://r.jina.ai/" + url, 40, "text/plain")).decode("utf-8", "ignore")
        text, img = _from_markdown(md)
        dbg.append(f"jina:{len(text)}c")
        return {"text": text, "image": img}
    except asyncio.CancelledError:
        raise
    except Exception as e:
        dbg.append(f"jina:err-{str(e)[:25]}")
        LOGGER.info(f"[AnimeNews] article reader fail ({_host(url)}): {e}")
    return {}


async def _article_info(session: aiohttp.ClientSession, url: str, dbg: list) -> dict:
    """Asli article se {text, image}. Pehle direct; site block kare to reader proxy (r.jina.ai)."""
    try:
        html_text = (await _http_get(session, url, 15, "text/html,*/*")).decode("utf-8", "ignore")
        soup = BeautifulSoup(html_text, "html.parser")
        img = None
        og = soup.find("meta", attrs={"property": "og:image"})
        if og and is_valid_img(urljoin(url, og.get("content") or "")):
            img = urljoin(url, og["content"])
        else:
            fig = soup.find("figure")
            tag = fig.find("img") if fig else None
            if tag:
                src = urljoin(url, tag.get("data-src") or tag.get("src") or "")
                img = src if is_valid_img(src) else None
        text = _text_from_html(soup)
        dbg.append(f"direct:{len(text)}c")
        if len(text) >= 80:
            return {"text": text, "image": img}
        # page khula par text nahi mila (JS page) → reader se bhi try, image direct wali rakho
        rd = await _article_via_reader(session, url, dbg)
        return {"text": rd.get("text") or text, "image": img or rd.get("image")}
    except asyncio.CancelledError:
        raise
    except Exception as e:
        dbg.append(f"direct:err-{str(e)[:25]}")
        LOGGER.info(f"[AnimeNews] article direct fail ({_host(url)}): {e}")
    return await _article_via_reader(session, url, dbg)


async def _prepare_inner(session: aiohttp.ClientSession, it: dict):
    dbg = it.setdefault("dbg", [])
    link = it["link"]
    is_g = "news.google." in urlparse(link).netloc
    real = (await _gnews_real_url(session, link, dbg) or "") if is_g else link
    it["real"] = real
    info = {}
    if len(it.get("summary") or "") < 200 or not it.get("image"):
        if real:
            info = await _article_info(session, real, dbg)
        elif is_g:                                   # decode nahi hua → reader se Google link hi kholo (JS redirect follow karta hai)
            info = await _article_via_reader(session, link, dbg)
            if re.search(r"(?i)before you continue|sign in|consent\.google", info.get("text", "")[:300]):
                info = {}
    if len(it.get("summary") or "") < 200 and len(info.get("text") or "") >= 80:
        it["summary"] = _trim(info["text"], BODY_MAX)
    if not it.get("image") and info.get("image"):
        it["image"] = info["image"]
    al = await get_anilist_info(session, it["title"])
    it["al"] = al
    dbg.append("al:" + ("ok" if al else "no"))
    if al:
        if not it.get("image") and al.get("image"):
            it["image"] = al["image"]
        if len(it.get("summary") or "") < 120 and al.get("desc"):
            it["about"] = al["desc"]
    if it.get("image"):
        it["photo"] = await _make_photo(session, it["image"])
        dbg.append("img:" + ("16x9" if it.get("photo") else "url"))
    else:
        dbg.append("img:none")


async def _prepare(session: aiohttp.ClientSession, it: dict):
    """Post ke liye sab jama karo: poori news text, image (feed → article → AniList), AniList info. Kuch fail ho to chalta hai."""
    try:
        await asyncio.wait_for(_prepare_inner(session, it), 90)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        LOGGER.warning(f"[AnimeNews] prepare adhoori ({type(e).__name__}: {e})")


# ───────────────────────── feed fetch ─────────────────────────
_RSS_ACCEPT = "application/rss+xml, application/atom+xml, application/xml, text/xml, */*"


def _site_of(url: str) -> str:
    u = urlparse(url)
    return f"{u.scheme}://{u.netloc}" if u.netloc else url


def _items_from_feedparser(feed, url: str, site: str, google: bool = False) -> list:
    items = []
    for ent in feed.entries[:ENTRIES_PER_FEED]:
        link = (ent.get("link") or "").strip()
        title = re.sub(r"\s+", " ", (ent.get("title") or "")).strip()
        if not link or not title:
            continue
        # Google News ka link (feed seedha Google News ki ho tab bhi) — title mein " - Site" aur summary mein title ki copy hoti hai
        is_g = google or "news.google." in urlparse(link).netloc
        raw_sum = ent.get("summary") or ent.get("description") or ""
        raw_full = (ent["content"][0].get("value", "") if ent.get("content") else "") or raw_sum
        if len(BeautifulSoup(raw_sum, "html.parser").get_text()) > len(BeautifulSoup(raw_full, "html.parser").get_text()):
            raw_full = raw_sum
        body, image = clean_body(raw_full), _entry_image(ent, raw_full)
        if is_g:
            src = ((ent.get("source") or {}).get("title") or "").strip()
            if src and title.endswith(src):
                title = title[:-len(src)].rstrip(" -–—|").strip() or title
            else:
                title = re.sub(r"\s+[-–—|]\s+[^-–—|]{2,40}$", "", title).strip() or title
            nb = _norm(body)
            if not nb or nb.startswith(_norm(title)[:30]):      # body = sirf title ki copy → hata do
                body = ""
            image = None
        items.append({"title": title, "link": link, "summary": body, "feed": url,
                      "site": site, "via_google": is_g, "image": image})
    return items


async def _feed_direct(session, url: str) -> list:
    raw = await _http_get(session, url, 20, _RSS_ACCEPT)
    feed = await asyncio.get_running_loop().run_in_executor(None, feedparser.parse, raw)
    items = _items_from_feedparser(feed, url, _site_of(url))
    if not items and feed.bozo:
        raise RuntimeError("RSS parse nahi hua (feed valid nahi lagti)")
    return items


async def _feed_rss2json(session, url: str) -> list:
    """rss2json unke server se feed lata hai (hamare IP pe block ho to bhi chal jaata hai). Asli link + summary milti hai."""
    import json
    from urllib.parse import quote
    raw = await _http_get(session, RSS2JSON.format(u=quote(url, safe="")), 25, "application/json")
    data = json.loads(raw.decode("utf-8", "ignore"))
    if data.get("status") != "ok":
        raise RuntimeError(f"rss2json: {str(data.get('message') or data.get('status'))[:80]}")
    items = []
    for ent in (data.get("items") or [])[:ENTRIES_PER_FEED]:
        link = (ent.get("link") or "").strip()
        title = re.sub(r"\s+", " ", (ent.get("title") or "")).strip()
        if not link or not title:
            continue
        thumb = ent.get("thumbnail") or (ent.get("enclosure") or {}).get("link") or ""
        raw = ent.get("content") or ent.get("description") or ""
        items.append({"title": _html.unescape(title), "link": link, "summary": clean_body(raw),
                      "feed": url, "site": _site_of(url), "via_google": False,
                      "image": thumb if is_valid_img(thumb) else _entry_image({}, raw)})
    return items


async def _feed_google(session, url: str) -> list:
    """Aakhri fallback: Google News RSS (site:<host>). Link google redirect hota hai, par button se asli article khulta hai."""
    host = urlparse(url).netloc
    if not host or "google." in host:
        raise RuntimeError("google fallback is feed pe lagu nahi")
    raw = await _http_get(session, GNEWS.format(h=host), 20, _RSS_ACCEPT)
    feed = await asyncio.get_running_loop().run_in_executor(None, feedparser.parse, raw)
    items = _items_from_feedparser(feed, url, _site_of(url), google=True)
    if not items:
        raise RuntimeError("google news mein bhi kuch nahi mila")
    return items


async def fetch_feed(session: aiohttp.ClientSession, url: str) -> list:
    """
    Feed ke latest ENTRIES_PER_FEED items (newest pehle).
    Chain: direct → rss2json → Google News. Teeno fail hone par hi exception.
    """
    errs = []
    for name, fn in (("direct", _feed_direct), ("rss2json", _feed_rss2json), ("google", _feed_google)):
        try:
            items = await fn(session, url)
            if items:
                for _i in items:
                    _i['via'] = name
                if errs:
                    LOGGER.info(f"[AnimeNews] {_host(url)}: '{name}' se mili ({'; '.join(errs)})")
                return items
            errs.append(f"{name}: khaali")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            errs.append(f"{name}: {type(e).__name__}: {e}"[:120])
    raise RuntimeError(errs[0] + (" | fallback bhi fail: " + " ; ".join(errs[1:]) if len(errs) > 1 else ""))


# ───────────────────────── post building / sending ─────────────────────────
def _info_line(al) -> str:
    """AniList se chhota info card: 🎬 naam (format, saal) / ⭐ score • 🎭 genres • 🏢 studio • 📺 eps • 📌 status."""
    if not al:
        return ""
    head = f"🎬 <b>{_e(al.get('name'))}</b>"
    sub = ", ".join(str(x) for x in (al.get("format"), al.get("year")) if x)
    if sub:
        head += f" <i>({_e(sub)})</i>"
    bits = []
    if al.get("score"):
        bits.append(f"⭐ {al['score'] / 10:.1f}/10")
    if al.get("genres"):
        bits.append("🎭 " + _e(", ".join(al["genres"])))
    if al.get("studio"):
        bits.append("🏢 " + _e(al["studio"]))
    if al.get("episodes"):
        bits.append(f"📺 {al['episodes']} eps")
    if al.get("status"):
        bits.append("📌 " + _e(al["status"]))
    return head + ("\n" + "  •  ".join(bits) if bits else "")


def build_caption(item: dict, footer: str = "") -> str:
    """
    📰 Title  →  poori news (blockquote)  →  🎬 series info  →  Powered By.
    Website pe jaana na pade isliye news text caption mein; 1024 limit ke andar sentence pe kaata jaata hai.
    """
    title = _e(item["title"][:200])
    info = _info_line(item.get("al"))
    body = item.get("summary") or ""
    text = body or item.get("about") or ""
    label = "" if body else "<b>📝 About:</b> "
    tail = []
    if footer:
        tail.append(f"<b>💜 Powered By : {_e(footer)}</b>")

    def make(t: str) -> str:
        parts = [f"📰 <b>{title}</b>"]
        if t:
            parts.append(f"<blockquote>{label}{_e(t)}</blockquote>")
        if info:
            parts.append(info)
        if tail:
            parts.append("\n".join(tail))
        return "\n\n".join(parts)

    cap = make(text)
    while len(cap) > CAPTION_MAX and text:
        text = _trim(text, len(text) - 80) if len(text) > 120 else ""
        cap = make(text)
    return cap


async def _send(client: Client, ch, item: dict, footer: str):
    caption = build_caption(item, footer)         # koi button nahi — news caption mein hi poori hai
    if item.get("photo") or item.get("image"):
        try:
            photo = item["image"]
            if item.get("photo"):                      # 1280x720 (16:9) banai hui photo
                photo = io.BytesIO(item["photo"])
                photo.name = "news.jpg"
            await client.send_photo(ch, photo, caption=caption, parse_mode=ParseMode.HTML)
            return
        except Exception as e:      # Telegram ko image URL pasand nahi aaya → text-only fallback
            LOGGER.warning(f"[AnimeNews] photo send fail, text fallback: {type(e).__name__}: {e}")
    await client.send_message(ch, caption, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def _footer(cfg: dict) -> str:
    if not cfg.get("footer"):
        return ""
    try:
        from ..utils.community import get_community_tag
        from ..utils.caption_style import _handles_brackets
        return _handles_brackets(await get_community_tag(all_names=True), spaced=True)
    except Exception:
        return ""


async def _alert(client: Client, key: str, text: str):
    """Owner ko DM — same key ka alert ALERT_GAP mein ek hi baar (spam nahi)."""
    now = _time.time()
    if now - _ALERT_AT.get(key, 0) < ALERT_GAP:
        return
    _ALERT_AT[key] = now
    for oid in dict.fromkeys(list(owner)):
        try:
            await client.send_message(oid, text, parse_mode=ParseMode.DISABLED)
        except Exception:
            pass


# ───────────────────────── main cycle ─────────────────────────
async def run_cycle(client: Client, manual: bool = False):
    """
    Saari feeds check karo, naye items channel mein post karo.
    manual=True: agar koi naya item nahi, to test ke liye latest item dobara post kar deta hai.
    Returns (posted_count, info_text).
    """
    async with _LOCK:
        cfg = await _cfg()
        ch = cfg.get("channel_id")
        if not ch:
            return 0, "Channel set nahi hai. /add_anime_news_channel <channel_id>"
        feeds = _feeds(cfg)
        if not feeds:
            return 0, "Koi RSS feed nahi hai. /add_news_rss <url>"
        await _ensure_index()
        footer = await _footer(cfg)
        errors, per_feed, routes, dbg_lines = [], [], [], []
        posted = 0

        async with aiohttp.ClientSession() as session:
            for url in feeds:
                try:
                    its = await fetch_feed(session, url)
                    per_feed.append(its)
                    if its:
                        routes.append(f"{_host(url)}→{its[0].get('via', '?')}")
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    errors.append(f"{_host(url)}: {type(e).__name__}: {e}")
                    LOGGER.error(f"[AnimeNews] feed fail {url}: {e}")

            fresh = []
            for items in per_feed:
                for it in reversed(items):          # purani news pehle, nayi baad mein
                    if await _claim_item(it):
                        fresh.append(it)
            forced = False
            if not fresh and manual:
                first = next((its[0] for its in per_feed if its), None)
                if first:
                    fresh, forced = [first], True

            for idx, it in enumerate(fresh):
                await _prepare(session, it)
                try:
                    await _send(client, ch, it, footer)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    errors.append(f"Channel {ch}: {type(e).__name__}: {e}")
                    LOGGER.error(f"[AnimeNews] send fail '{it['title']}': {e}")
                    if not forced:                  # bheji nahi gayi → agli baar dobara try ho
                        for left in fresh[idx:]:
                            await _release_item(left)
                    break                           # channel hi problem hai to baaki bhi fail honge
                posted += 1
                dbg_lines.append(f"{it['title'][:24]}… → " + " ".join(it.get('dbg', [])))
                LOGGER.info(f"[AnimeNews] posted: {it['title']}")
                await asyncio.sleep(POST_GAP)

        upd = {"last_check": _time.time()}
        await _set(**upd)
        if posted:
            await db.col2.update_one({"id": CFG_ID}, {"$inc": {"cfg.posted_total": posted}}, upsert=True)
        if errors:
            await _set(last_error=errors[0][:300])
            if not manual:
                await _alert(client, "cycle", "⚠️ Anime News auto-post mein problem:\n" + "\n".join(errors[:3])
                             + f"\n\nAgla check {_interval(cfg)} min baad. Panel: /customise → Anime News")
        else:
            await _unset("last_error")

        if posted:
            info = f"{posted} news post ho gayi" + (" (test: latest news dobara bheji)" if forced else "") + "."
        elif errors:
            info = "News post nahi ho paayi."
        else:
            info = "Koi nayi news nahi mili (sab pehle hi post ho chuki hain)."
        if errors:
            info += "\n⚠️ " + "\n⚠️ ".join(errors[:3])
        if routes:
            info += "\n🛣 Route: " + ", ".join(routes)
        if manual and dbg_lines:
            info += "\n🔎 " + "\n🔎 ".join(dbg_lines[:3])
        return posted, info


async def news_loop():
    """Har `interval` minute pe auto-check. janitor.start_janitor() se start hota hai."""
    from .. import app
    await asyncio.sleep(60)
    while True:
        interval = DEFAULT_INTERVAL
        try:
            cfg = await _cfg()
            interval = _interval(cfg)
            if cfg.get("channel_id") and cfg.get("enabled", True):
                await run_cycle(app)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            LOGGER.error(f"[AnimeNews] loop error: {e}")
        try:
            await asyncio.wait_for(_WAKE.wait(), timeout=interval * 60)
        except asyncio.TimeoutError:
            pass
        _WAKE.clear()


# ───────────────────────── channel validation ─────────────────────────
async def _validate_channel(client: Client, channel_id: int):
    """(title, error|None) — bot admin + Post Messages check."""
    try:
        chat = await client.get_chat(channel_id)
        title = chat.title
    except Exception as e:
        return None, f"Channel nahi mila: {e}"
    try:
        me = await client.get_me()
        member = await client.get_chat_member(channel_id, me.id)
        if member.status.name not in ("ADMINISTRATOR", "OWNER"):
            return None, f"Bot {title} mein admin nahi hai!"
        from .update_channel import _can_post
        if not _can_post(member):
            return None, f"Bot {title} mein admin hai, lekin 'Post Messages' permission nahi hai!"
    except Exception as e:
        return None, f"Admin check fail: {e}"
    return title, None


async def _set_channel(client: Client, channel_id: int):
    title, err = await _validate_channel(client, channel_id)
    if err:
        return None, err
    cfg = await _cfg()
    upd = {"channel_id": channel_id, "channel_title": title}
    if "enabled" not in cfg:
        upd["enabled"] = True
    await _set(**upd)
    _WAKE.set()          # loop ko turant jagao — pehla check abhi
    return title, None


async def _add_feed(url: str):
    """(ok, msg) — feed validate karke add karo."""
    url = (url or "").strip()
    if not re.match(r"^https?://\S+$", url):
        return False, "Valid RSS URL do (http:// ya https:// se shuru)."
    cfg = await _cfg()
    feeds = _feeds(cfg)
    if url in feeds:
        return False, "Yeh feed pehle se added hai."
    if len(feeds) >= MAX_FEEDS:
        return False, f"Limit: max {MAX_FEEDS} feeds. Pehle koi hatao."
    try:
        async with aiohttp.ClientSession() as session:
            items = await fetch_feed(session, url)
    except Exception as e:
        return False, f"Feed load nahi hui: {type(e).__name__}: {e}"
    if not items:
        return False, "Is feed mein koi news entry nahi mili."
    await _set(feeds=feeds + [url])
    _WAKE.set()
    return True, f"Feed add ho gayi ({len(items)} latest entries mili)."


# ───────────────────────── commands ─────────────────────────
@Client.on_message(filters.command("add_anime_news_channel") & filters.private)
async def cmd_add_channel(client: Client, message: Message):
    if not _is_auth(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) < 2:
        await message.reply(
            "📰 **Anime News Channel**\n\nUsage: `/add_anime_news_channel -1001234567890`\n\n"
            "Is channel mein RSS se anime news automatic aayegi. "
            "Bot us channel mein admin (Post Messages ke saath) hona chahiye."
        )
        return
    try:
        cid = int(parts[1])
    except ValueError:
        await message.reply("❌ Valid channel ID do. Format: `-100xxxxxxxxxx`")
        return
    title, err = await _set_channel(client, cid)
    if err:
        await message.reply(f"❌ {err}")
        return
    cfg = await _cfg()
    await message.reply(
        f"✅ **Anime News channel set!**\n\n📢 **{_plain(title)}**\n🆔 `{cid}`\n"
        f"⏱ Har **{_interval(cfg)} min** mein check hoga — pehli news kuch hi der mein aa jayegi.\n\n"
        "• Interval: `/anime_news_interval 15`\n• Feeds: `/view_news_rss`\n"
        "• Sab buttons se: `/customise` → Cleanup & Schedule → Anime News"
    )


@Client.on_message(filters.command("rem_anime_news_channel") & filters.private)
async def cmd_rem_channel(client: Client, message: Message):
    if not _is_auth(message.from_user.id):
        return
    await _unset("channel_id", "channel_title")
    await message.reply("🗑 Anime News channel hata diya. Auto-post band.")


@Client.on_message(filters.command("anime_news") & filters.private)
async def cmd_status(client: Client, message: Message):
    if not _is_auth(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) > 1 and parts[1].lower() in ("on", "off"):
        on = parts[1].lower() == "on"
        await _set(enabled=on)
        if on:
            _WAKE.set()
        await message.reply("🟢 Anime News auto-post **ON**" if on else "🔴 Anime News auto-post **OFF**")
        return
    cfg = await _cfg()
    await message.reply(_status_text(cfg) + "\n\nCommands: `/anime_news on|off`, `/anime_news_interval`, "
                        "`/anime_news_now`, `/add_anime_news_channel`, `/add_news_rss`")


@Client.on_message(filters.command("anime_news_interval") & filters.private)
async def cmd_interval(client: Client, message: Message):
    if not _is_auth(message.from_user.id):
        return
    parts = message.text.split()
    try:
        v = int(parts[1])
        assert MIN_INTERVAL <= v <= MAX_INTERVAL
    except Exception:
        await message.reply(f"Usage: `/anime_news_interval 10` (minute, {MIN_INTERVAL}-{MAX_INTERVAL})")
        return
    await _set(interval=v)
    _WAKE.set()
    await message.reply(f"✅ Ab har **{v} min** mein anime news check hogi.")


@Client.on_message(filters.command("anime_news_now") & filters.private)
async def cmd_now(client: Client, message: Message):
    if not _is_auth(message.from_user.id):
        return
    wait = await message.reply("⏳ Feeds check ho rahi hain...")
    posted, info = await run_cycle(client, manual=True)
    await wait.edit(("✅ " if posted else "ℹ️ ") + info)


@Client.on_message(filters.command("add_news_rss") & filters.private)
async def cmd_add_rss(client: Client, message: Message):
    if not _is_auth(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) < 2:
        await message.reply("Usage: `/add_news_rss https://example.com/rss.xml`")
        return
    ok, msg = await _add_feed(parts[1])
    await message.reply(("✅ " if ok else "❌ ") + msg)


@Client.on_message(filters.command("rem_news_rss") & filters.private)
async def cmd_rem_rss(client: Client, message: Message):
    if not _is_auth(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) < 2:
        await message.reply("Usage: `/rem_news_rss <url>` ya `/rem_news_rss <number>` (`/view_news_rss` se number dekho)")
        return
    cfg = await _cfg()
    feeds = _feeds(cfg)
    key = parts[1].strip()
    target = None
    if key.isdigit() and 1 <= int(key) <= len(feeds):
        target = feeds[int(key) - 1]
    elif key in feeds:
        target = key
    if target is None:
        await message.reply("❌ Yeh feed list mein nahi hai. `/view_news_rss` dekho.")
        return
    feeds.remove(target)
    await _set(feeds=feeds)
    await message.reply(f"🗑 Feed hata di:\n`{target}`")


@Client.on_message(filters.command("view_news_rss") & filters.private)
async def cmd_view_rss(client: Client, message: Message):
    if not _is_auth(message.from_user.id):
        return
    cfg = await _cfg()
    feeds = _feeds(cfg)
    body = "\n".join(f"{i}. `{f}`" for i, f in enumerate(feeds, 1)) if feeds else "⚠️ Koi feed nahi hai."
    tag = " _(default)_" if cfg.get("feeds") is None else ""
    await message.reply(f"📡 **Anime News RSS Feeds**{tag}\n━━━━━━━━━━━━━━━━━━━━\n\n{body}")


# ───────────────────────── status text ─────────────────────────
def _status_text(cfg: dict) -> str:
    if cfg.get("channel_id"):
        ch = f"**{_plain(cfg.get('channel_title', 'Unknown'))}**\n🆔 `{cfg['channel_id']}`"
    else:
        ch = "❌ Set nahi hai"
    feeds = _feeds(cfg)
    if feeds:
        fl = "\n".join(f"   • `{_host(f)}`" for f in feeds) + ("  _(default)_" if cfg.get("feeds") is None else "")
    else:
        fl = "   ⚠️ Koi feed nahi — news nahi aayegi"
    lc = cfg.get("last_check")
    last = datetime.fromtimestamp(lc, IST).strftime("%d %b, %I:%M %p") if lc else "—"
    err = f"\n⚠️ Last error: `{_plain(cfg['last_error'])[:200]}`" if cfg.get("last_error") else ""
    return (
        "📰 **Anime News**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📢 Channel: {ch}\n"
        f"⏱ Check: har **{_interval(cfg)} min**\n"
        f"🔔 Auto-post: **{'🟢 ON' if cfg.get('enabled', True) else '🔴 OFF'}**\n"
        f"📡 RSS feeds ({len(feeds)}/{MAX_FEEDS}):\n{fl}\n"
        f"💜 Powered By line: **{'🟢 ON' if cfg.get('footer') else '🔴 OFF'}**\n"
        f"📰 Total posted: **{cfg.get('posted_total', 0)}**\n"
        f"🕒 Last check: {last}{err}"
    )


# ───────────────────────── /customise panel ─────────────────────────
def _back(label="⬅️ Anime News"):
    return [InlineKeyboardButton(label, callback_data="cmz:open:anime_news"),
            InlineKeyboardButton("🏠 Menu", callback_data="cmz:menu")]


async def _edit(client, cb, text, kb):
    from .customise import _safe_edit
    await _safe_edit(client, cb, text, kb)


async def render(client: Client, cb: CallbackQuery):
    from .customise import _CUR_SECTION, _back_row
    _CUR_SECTION.set("anime_news")
    clear_session(cb.from_user.id)
    cfg = await _cfg()
    on = cfg.get("enabled", True)
    rows = [
        [InlineKeyboardButton("✏️ Channel badlo" if cfg.get("channel_id") else "➕ Channel set karo",
                              callback_data="cmz:an:chan"),
         InlineKeyboardButton("⏱ Interval", callback_data="cmz:an:intv")],
        [InlineKeyboardButton("🟢 Auto-post ON — OFF karo" if on else "🔴 Auto-post OFF — ON karo",
                              callback_data="cmz:an:tog")],
        [InlineKeyboardButton("📡 RSS Feeds", callback_data="cmz:an:fds")],
        [InlineKeyboardButton("💜 Powered By line: ON — OFF karo" if cfg.get("footer")
                              else "💜 Powered By line: OFF — ON karo", callback_data="cmz:an:foot")],
        [InlineKeyboardButton("📤 Abhi check + post karo (test)", callback_data="cmz:an:now")],
    ]
    if cfg.get("channel_id"):
        rows.append([InlineKeyboardButton("🗑 Channel hatao", callback_data="cmz:an:rm")])
    rows.append(_back_row())
    await _edit(client, cb, _status_text(cfg), InlineKeyboardMarkup(rows))


async def _render_feeds(client: Client, cb: CallbackQuery):
    cfg = await _cfg()
    feeds = _feeds(cfg)
    rows = [[InlineKeyboardButton(f"🗑 {i}. {_host(f)[:30]}", callback_data=f"cmz:an:frm:{i - 1}")]
            for i, f in enumerate(feeds, 1)]
    if len(feeds) < MAX_FEEDS:
        rows.append([InlineKeyboardButton("➕ Feed add karo", callback_data="cmz:an:fadd"),
                     InlineKeyboardButton("📚 Popular feeds", callback_data="cmz:an:fpre")])
    if cfg.get("feeds") is not None:
        rows.append([InlineKeyboardButton("♻️ Default feed par reset", callback_data="cmz:an:frst")])
    rows.append(_back())
    body = "\n".join(f"{i}. `{f}`" for i, f in enumerate(feeds, 1)) if feeds else "⚠️ Koi feed nahi hai."
    await _edit(client, cb,
                f"**📡 RSS Feeds** ({len(feeds)}/{MAX_FEEDS})\n━━━━━━━━━━━━━━━━━━━━\n\n{body}\n\n"
                "Feed hatane ke liye 🗑 button dabao.", InlineKeyboardMarkup(rows))


async def handle(client: Client, cb: CallbackQuery, args: list):
    sub = args[0] if args else ""
    uid = cb.from_user.id

    if sub == "chan":
        _SESS[uid] = {"mode": "channel"}
        await cb.answer()
        await _edit(client, cb,
                    "**📢 Anime News channel set karo**\n━━━━━━━━━━━━━━━━━━━━\n\n"
                    "Channel ka **ID** bhejo, jaise `-1001234567890`\n_(ya us channel ki koi post forward karo)_\n\n"
                    "Bot us channel mein **admin** (Post Messages ke saath) hona chahiye.",
                    InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cmz:open:anime_news")]]))
        return

    if sub == "intv":
        await cb.answer()
        cur = _interval(await _cfg())
        rows, row = [], []
        for p in INTERVAL_PRESETS:
            row.append(InlineKeyboardButton(("✅ " if p == cur else "") + (f"{p} min" if p < 60 else f"{p // 60} hr"),
                                            callback_data=f"cmz:an:iset:{p}"))
            if len(row) == 3:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append(_back())
        await _edit(client, cb,
                    f"**⏱ Kitni der mein news check ho?**\n━━━━━━━━━━━━━━━━━━━━\n\nAbhi: **{cur} min**\n\n"
                    f"Custom ({MIN_INTERVAL}-{MAX_INTERVAL} min): `/anime_news_interval 20`",
                    InlineKeyboardMarkup(rows))
        return

    if sub == "iset" and len(args) > 1 and args[1].isdigit():
        v = int(args[1])
        if MIN_INTERVAL <= v <= MAX_INTERVAL:
            await _set(interval=v)
            _WAKE.set()
            await cb.answer(f"✅ Har {v} min")
        await render(client, cb)
        return

    if sub == "tog":
        cfg = await _cfg()
        on = not cfg.get("enabled", True)
        await _set(enabled=on)
        if on:
            _WAKE.set()
        await cb.answer("🟢 ON" if on else "🔴 OFF")
        await render(client, cb)
        return

    if sub == "foot":
        cfg = await _cfg()
        val = not cfg.get("footer", False)
        await _set(footer=val)
        await cb.answer("💜 Powered By ON" if val else "Powered By OFF")
        await render(client, cb)
        return

    if sub == "fds":
        await cb.answer()
        await _render_feeds(client, cb)
        return

    if sub == "fadd":
        _SESS[uid] = {"mode": "feed"}
        await cb.answer()
        await _edit(client, cb,
                    "**➕ RSS feed add karo**\n━━━━━━━━━━━━━━━━━━━━\n\n"
                    "Feed ka URL bhejo, jaise `https://www.animenewsnetwork.com/all/rss.xml`\n\n"
                    "Bot pehle check karega ki feed chal rahi hai.",
                    InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cmz:an:fds")]]))
        return

    if sub == "fpre":
        await cb.answer()
        have = _feeds(await _cfg())
        rows = [[InlineKeyboardButton(("✅ " if u in have else "➕ ") + n, callback_data=f"cmz:an:fpa:{i}")]
                for i, (n, u) in enumerate(FEED_PRESETS)]
        rows.append([InlineKeyboardButton("⬅️ Feeds", callback_data="cmz:an:fds"),
                     InlineKeyboardButton("🏠 Menu", callback_data="cmz:menu")])
        await _edit(client, cb,
                    "**📚 Popular anime news feeds**\n━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"Tap karke add karo (max {MAX_FEEDS}). Site block kare to bot khud fallback use karta hai.",
                    InlineKeyboardMarkup(rows))
        return

    if sub == "fpa" and len(args) > 1 and args[1].isdigit() and int(args[1]) < len(FEED_PRESETS):
        name, url = FEED_PRESETS[int(args[1])]
        ok, msg = await _add_feed(url)
        try:
            await cb.answer(("✅ " + name) if ok else ("❌ " + msg)[:190], show_alert=not ok)
        except Exception:
            pass
        await _render_feeds(client, cb)
        return

    if sub == "frm" and len(args) > 1 and args[1].isdigit():
        cfg = await _cfg()
        feeds = _feeds(cfg)
        i = int(args[1])
        if 0 <= i < len(feeds):
            feeds.pop(i)
            await _set(feeds=feeds)
            await cb.answer("🗑 Hata di")
        else:
            await cb.answer()
        await _render_feeds(client, cb)
        return

    if sub == "frst":
        await _unset("feeds")
        await cb.answer("♻️ Default feed")
        await _render_feeds(client, cb)
        return

    if sub == "now":
        cfg = await _cfg()
        if not cfg.get("channel_id"):
            await cb.answer("Pehle channel set karo.", show_alert=True)
            return
        await cb.answer("⏳ Check ho raha hai (kuch sec)...", show_alert=True)
        posted, info = await run_cycle(client, manual=True)
        try:
            await client.send_message(cb.message.chat.id, ("✅ " if posted else "ℹ️ ") + info)
        except Exception:
            pass
        return

    if sub == "rm":
        await cb.answer()
        await _edit(client, cb, "🗑 **Anime News channel hata du?**\n\nAuto-post band ho jayega.",
                    InlineKeyboardMarkup([[InlineKeyboardButton("✅ Haan, hatao", callback_data="cmz:an:rmok"),
                                           InlineKeyboardButton("❌ Nahi", callback_data="cmz:open:anime_news")]]))
        return

    if sub == "rmok":
        await _unset("channel_id", "channel_title")
        await cb.answer("🗑 Hata diya")
        await render(client, cb)
        return

    await cb.answer()


# group=-7: sirf tab consume karta hai jab user is panel ke input mode mein ho
@Client.on_message((filters.text | filters.forwarded) & filters.private, group=-7)
async def an_input(client: Client, message: Message):
    if not message.from_user:
        raise ContinuePropagation
    uid = message.from_user.id
    sess = _SESS.get(uid)
    if not sess or not _is_auth(uid):
        raise ContinuePropagation
    raw = (message.text or "").strip()
    if raw.startswith("/"):
        _SESS.pop(uid, None)
        raise ContinuePropagation

    if sess["mode"] == "feed":
        wait = await message.reply("⏳ Feed check ho rahi hai...")
        ok, msg = await _add_feed(raw)
        if ok:
            _SESS.pop(uid, None)
            await wait.edit("✅ " + msg, reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("📡 Feeds", callback_data="cmz:an:fds")], _back()]))
        else:
            await wait.edit(f"❌ {msg}\n\nDobara URL bhejo ya /customise se wapas jao.")
        raise StopPropagation

    # mode == channel
    cid = None
    fchat = getattr(message, "forward_from_chat", None)
    if fchat is None:
        origin = getattr(message, "forward_origin", None)
        fchat = getattr(origin, "chat", None) if origin is not None else None
    if fchat is not None:
        cid = fchat.id
    else:
        try:
            cid = int(raw)
        except ValueError:
            await message.reply("❌ Valid channel ID bhejo (`-100xxxxxxxxx`) ya channel ki post forward karo.")
            raise StopPropagation
    if cid > 0:
        await message.reply("❌ Channel ID `-100` se shuru hoti hai. Dobara bhejo.")
        raise StopPropagation
    title, err = await _set_channel(client, cid)
    if err:
        await message.reply(f"❌ {err}\n\nSahi karke ID dobara bhejo.")
        raise StopPropagation
    _SESS.pop(uid, None)
    await message.reply(f"✅ **Anime News channel set!**\n\n📢 **{_plain(title)}**\n🆔 `{cid}`\n\n"
                        "Pehli news kuch hi der mein aa jayegi.",
                        reply_markup=InlineKeyboardMarkup([_back()]))
    raise StopPropagation
