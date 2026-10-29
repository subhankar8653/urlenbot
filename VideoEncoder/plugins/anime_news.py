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
import hashlib
import html as _html
import re
import time as _time
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

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


def extract_anime_names(title: str) -> list:
    """Title se anime/manga ke possible naam (priority order): quotes → ':' se pehle → '-' se pehle → season/media word se pehle."""
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
    return out[:3]


_ANILIST_Q = """
query ($search: String) {
  anime: Media (search: $search, type: ANIME, sort: POPULARITY_DESC) { bannerImage coverImage { extraLarge } }
  manga: Media (search: $search, type: MANGA, sort: POPULARITY_DESC) { bannerImage coverImage { extraLarge } }
}
"""


async def get_anilist_poster(session: aiohttp.ClientSession, title: str, retries: int = 2):
    """Har candidate naam AniList pe dhundo; pehla match (cover > banner, anime > manga) return."""
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
                    media = data.get(kind)
                    if not media:
                        continue
                    img = (media.get("coverImage") or {}).get("extraLarge") or media.get("bannerImage")
                    if img:
                        return img
                break   # is naam pe kuch nahi mila → agla candidate
            except asyncio.CancelledError:
                raise
            except Exception as e:
                LOGGER.info(f"[AnimeNews] AniList '{term}' attempt {attempt}: {type(e).__name__}")
                await asyncio.sleep(2 * attempt)
    return None


async def _pick_image(session: aiohttp.ClientSession, item: dict):
    img = await get_anilist_poster(session, item["title"])
    if img:
        return img
    if not item.get("via_google"):          # google redirect link se article page scrape nahi hota
        img = await fetch_image_from_article(session, item["link"])
        if img:
            return img
    return item.get("thumb")


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
        raw_sum = ent.get("summary") or ent.get("description") or ""
        if not raw_sum and ent.get("content"):
            raw_sum = ent["content"][0].get("value", "")
        summary = clean_summary(raw_sum)
        if google:
            # Google News title: "News title - Site Name"; summary sirf title ki copy hoti hai → hata do
            title = re.sub(r"\s+-\s+[^-]{2,60}$", "", title).strip() or title
            low = summary.lower()
            if not summary or low.startswith(title.lower()[:40]):
                summary = ""
        items.append({"title": title, "link": link, "summary": summary, "feed": url,
                      "site": site, "via_google": google})
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
        items.append({"title": _html.unescape(title), "link": link,
                      "summary": clean_summary(ent.get("description") or ent.get("content") or ""),
                      "feed": url, "site": _site_of(url), "via_google": False,
                      "thumb": thumb if is_valid_img(thumb) else None})
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
def build_caption(item: dict, footer: str = "") -> str:
    title = _e(item["title"][:200])
    u = urlparse(item["link"])
    site = item.get("site") or (f"{u.scheme}://{u.netloc}" if u.netloc else item.get("feed", ""))
    site_attr = _html.escape(site, quote=True)

    def make(summary: str) -> str:
        head = [f"<blockquote><b>{title}</b></blockquote>"]
        if summary:
            head.append(f"<blockquote><b>{_e(summary)}</b></blockquote>")
        tail = [f"──── 📖 <a href='{site_attr}'><b>SOURCE</b></a> ────"]
        if footer:
            tail.append(f"<b>💜 Powered By : {_e(footer)}</b>")
        return "\n\n".join(head) + "\n\n" + "\n".join(tail)

    summary = item.get("summary") or ""
    cap = make(summary)
    while len(cap) > CAPTION_MAX and summary:        # caption limit: summary chhota karte jao
        summary = summary[:max(0, len(summary) - 60)].rstrip()
        cap = make(summary)
    return cap


async def _send(client: Client, ch, item: dict, footer: str):
    caption = build_caption(item, footer)
    markup = None
    if item["link"].startswith("http"):
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("Rᴇᴀᴅ ɪᴛ ғᴜʟʟʏ...", url=item["link"])]])
    if item.get("image"):
        try:
            await client.send_photo(ch, item["image"], caption=caption,
                                    parse_mode=ParseMode.HTML, reply_markup=markup)
            return
        except Exception as e:      # Telegram ko image URL pasand nahi aaya → text-only fallback
            LOGGER.warning(f"[AnimeNews] photo send fail, text fallback: {type(e).__name__}: {e}")
    await client.send_message(ch, caption, parse_mode=ParseMode.HTML, reply_markup=markup,
                              disable_web_page_preview=True)


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
        errors, per_feed = [], []
        posted = 0

        async with aiohttp.ClientSession() as session:
            for url in feeds:
                try:
                    per_feed.append(await fetch_feed(session, url))
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
                it["image"] = await _pick_image(session, it)
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
