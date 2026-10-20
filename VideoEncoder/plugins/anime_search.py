"""
anime_search.py  v2 (multi-website picker)
================
/anime <naam>  ->  pehle WEBSITE chuno (Raretoon / Hindi Anime Zone / Toono /
                   Aniwatch / Animetsu), phir usi site pe search + episode
                   selector. Har step pe Back button hai.

(neeche purana doc: RareAnimes wala flow, ab "Raretoon" option ke andar)
RareAnimes site pe search karta hai aur results ko
                   buttons mein dikhata hai (Next/Prev page ke saath).
                   Kisi result pe tap karte hi wahi /rti wala episode
                   selector khul jata hai (season/episode buttons, multi
                   select, channel upload — sab /rti jaisa).

Flow (jaisa screenshots mein dikhaya):
  1. rareanimes site open  ->  search icon click  ->  box mein naam likho
  2. box ke side wala search icon tap  ->  result page
  3. Result ke buttons + "NEXT PAGE" button

Tareeka:
  * Fast path : requests se seedha `?s=<naam>` result page (browser ki
                zaroorat nahi, jaldi aur RAM-friendly).
  * Fallback  : agar fast path se result na mile (site block / ajax search),
                to Selenium se asli click-flow chalta hai.
"""

import asyncio
import html as _html
import re
import time
import uuid
from urllib.parse import quote_plus
import traceback

import requests
from bs4 import BeautifulSoup
from pyrogram import Client, filters
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from .. import LOGGER
from ..utils.helper import check_chat
from . import rti_downloader as rti
from . import haz_downloader as haz
from . import toono_downloader as toono
from . import aniwatch_downloader as aniw
from . import aniabyss_downloader as aby
from ..utils.aniwatch import get_binary_path

import os

SITE = os.getenv("ANIME_SITE", "https://www.rareanimes.mov").rstrip("/")
HAZ_SITE = os.getenv("HAZ_SITE", "https://hindianimeszone.com").rstrip("/")
TOONO_SITE = os.getenv("TOONO_SITE", "https://toono.app").rstrip("/")


def _host(u: str) -> str:
    # urllib.parse.urlparse pe project mein global monkey-patch hai (lk21_patch),
    # isliye yahan apna simple regex helper use karte hain.
    m = re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://([^/?#]+)", u or "")
    return (m.group(1) if m else "").lower().replace("www.", "")


def _path(u: str) -> str:
    m = re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://[^/?#]+([^?#]*)", u or "")
    return m.group(1) if m else ""


def _join(base: str, href: str) -> str:
    href = (href or "").strip()
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", href):
        return href
    m = re.match(r"^([a-zA-Z][a-zA-Z0-9+.-]*://[^/?#]+)(/[^?#]*)?", base)
    root = m.group(1) if m else SITE
    if href.startswith("//"):
        return root.split("://")[0] + ":" + href
    if href.startswith("/"):
        return root + href
    if href.startswith("?"):
        return root + (m.group(2) or "/" if m else "/") + href
    cur = (m.group(2) or "/") if m else "/"
    return root + cur.rsplit("/", 1)[0] + "/" + href


SITE_HOST = _host(SITE)

SESSIONS = {}
SESSION_TIMEOUT = 3600
HEADERS = rti.HEADERS

# URL ke yeh hisse result nahi hain (menu/category/pagination links)
_SKIP_PATH = re.compile(
    r"/(category|tag|page|author|wp-|feed|contact|about|privacy|dmca|how-to|telegram)(/|$)",
    re.I,
)


# ───────────────────────── button label ─────────────────────────
_LANG_CUT = re.compile(
    r"\s*[–—:|-]?\s*\b(episodes?|hindi|english|tamil|telugu|dual|multi|dubbed|dub|subbed|sub|download|hd)\b.*$",
    re.I,
)


def label_parts(title: str):
    """-> (label, season_no, name, is_movie)"""
    t = _clean(title)
    low = t.lower()

    m = re.search(r"\bseason\s*0*(\d+)", t, re.I)
    season = int(m.group(1)) if m else None

    has_year = bool(re.search(r"\(\d{4}\)", t))
    is_movie = bool(re.search(r"\bmovie\b", low)) or (
        has_year and season is None and "episode" not in low
    )

    langs = []
    for key, nm in (("hindi", "Hindi"), ("english", "English"), ("tamil", "Tamil"),
                    ("telugu", "Telugu"), ("japanese", "Japanese")):
        if re.search(rf"\b{key}\b", low):
            langs.append(nm)
    if re.search(r"\bdual\s*audio\b", low):
        langs.append("Dual Audio")
    if re.search(r"\bmulti[\s-]*audio\b", low):
        langs.append("Multi Audio")

    kind = ""
    if re.search(r"\bdubbed\b|\bdub\b", low):
        kind = "Dub"
    elif re.search(r"\bsubbed\b|\bsub\b|\bsubtitles?\b|\besub\b", low):
        kind = "Sub"
    lang = " ".join(langs[:3])
    if kind:
        lang = f"{lang} {kind}".strip()

    name = re.sub(r"\bseason\s*0*\d+", "", t, flags=re.I)
    name = re.sub(r"\bmovie\b", "", name, flags=re.I)
    name = re.sub(r"\s{2,}", " ", name).strip()
    cut = _LANG_CUT.sub("", name).strip(" –—:|-")
    name = cut if cut else name.strip(" –—:|-")

    parts = []
    if is_movie:
        parts.append("[Movie]")                       # movie: S01 nahi, seedha Movie
    else:
        parts.append(f"[S{(season if season is not None else 1):02d}]")
    if lang:
        parts.append(f"[{lang}]")
    parts.append(name)
    return " ".join(parts), (season if season is not None else (0 if is_movie else 1)), name, is_movie


def make_label(title: str) -> str:
    """
    'Naruto Shippuden Season 06 – Episodes Hindi Dubbed Download HD'
        -> '[S06] [Hindi Dub] Naruto Shippuden'
    'Finding Nemo (2003) Movie Hindi Dubbed Download'
        -> '[Movie] [Hindi Dub] Finding Nemo (2003)'
    """
    return label_parts(title)[0]


def sort_results(results: list) -> list:
    """Ek anime ke saare seasons saath-saath, S01 -> S02 -> S03 order mein.
    Anime ka order wahi rehta hai jo site ne diya (pehla dikha pehle)."""
    order, groups = [], {}
    for r in results:
        key = r["name"].lower()
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(r)
    out = []
    for key in order:
        out.extend(sorted(groups[key], key=lambda x: (x["season"], x["title"])))
    return out


# ───────────────────────── parsing ─────────────────────────
def _same_site(href: str) -> bool:
    return _host(href) == SITE_HOST


def _clean(t: str) -> str:
    t = _html.unescape(t or "")
    t = t.replace("&#8211;", "–")
    return re.sub(r"\s+", " ", t).strip()


def parse_results(page_html: str, base_url: str):
    """-> (results[list of {title,url}], next_url|None)"""
    soup = BeautifulSoup(page_html, "html.parser")
    anchors = soup.select(
        "article h1 a, article h2 a, article h3 a, "
        ".entry-title a, .post-title a, h2.title a, h3.title a, "
        ".search-results a.title, .result a"
    )
    if not anchors:
        # Fallback: page ke saare post-jaise links
        anchors = [
            a for a in soup.find_all("a", href=True)
            if re.search(r"download|hindi|episodes|season|movie", a.get_text(" "), re.I)
        ]

    results, seen = [], set()
    for a in anchors:
        href = _join(base_url, a.get("href", ""))
        title = _clean(a.get_text(" ", strip=True))
        if not title or not _same_site(href):
            continue
        path = _path(href)
        if path in ("", "/") or _SKIP_PATH.search(path) or "?s=" in href:
            continue
        if href in seen:
            continue
        seen.add(href)
        label, season_no, name, is_movie = label_parts(title)
        results.append({"title": title, "url": href, "label": label,
                        "season": season_no, "name": name, "movie": is_movie})

    results = sort_results(results)

    next_url = None
    nxt = soup.select_one("a.next.page-numbers, a.next, a[rel=next], .nav-links a.next")
    if not nxt:
        for a in soup.find_all("a", href=True):
            if re.search(r"next\s*page|^\s*next\b|»|›", a.get_text(" "), re.I):
                nxt = a
                break
    if nxt and nxt.get("href"):
        next_url = _join(base_url, nxt["href"])
    return results, next_url


# ───────────────────────── fetching ─────────────────────────
def _looks_blocked(r) -> bool:
    if r.status_code in (403, 429, 503):
        return True
    head = r.text[:3000].lower()
    return "just a moment" in head or "cf-chl" in head or "attention required" in head


def _fetch_requests(url: str):
    r = requests.get(url, headers=HEADERS, timeout=20)
    if _looks_blocked(r):
        raise RuntimeError("blocked")
    r.raise_for_status()
    return r.text


def _selenium_get(url: str):
    driver = rti._make_selenium_driver()
    try:
        driver.get(url)
        time.sleep(3)
        return driver.page_source
    finally:
        rti._kill_driver_tree(driver)


def fetch_page(url: str):
    """Next/Prev page ke liye: pehle requests, warna selenium."""
    try:
        return _fetch_requests(url)
    except Exception:
        return _selenium_get(url)


_JS_FIND_SEARCH_ICON = """
const els = [...document.querySelectorAll('a,button,i,span,div,svg')];
const vis = e => { const r = e.getBoundingClientRect(); return r.width>0 && r.height>0; };
const c = els.filter(e => /search/i.test((e.className&&e.className.baseVal!==undefined?e.className.baseVal:e.className)||'') + (e.id||'') && vis(e));
return c.length ? c[0] : null;
"""


def _selenium_search(query: str):
    """Asli click-flow: icon click -> box mein type -> side ka icon tap."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys

    driver = rti._make_selenium_driver()
    try:
        driver.get(SITE)
        time.sleep(3)

        # 1) corner ka search icon
        try:
            icon = driver.execute_script(_JS_FIND_SEARCH_ICON)
            if icon:
                driver.execute_script("arguments[0].click();", icon)
                time.sleep(1.5)
        except Exception as e:
            LOGGER.warning(f"[Anime] search icon click fail: {e}")

        # 2) search box mein naam
        box = None
        for sel in ("input[type=search]", "input[name=s]", "input.search-field",
                    "input[type=text]"):
            for el in driver.find_elements(By.CSS_SELECTOR, sel):
                if el.is_displayed():
                    box = el
                    break
            if box:
                break
        if not box:
            raise RuntimeError("search box nahi mila")
        box.clear()
        box.send_keys(query)
        time.sleep(1)

        # 3) box ke side wala search icon / submit
        clicked = False
        for sel in ("button[type=submit]", ".search-submit", "input[type=submit]",
                    "[class*=search] button", "[class*=search] i", "[class*=search] a"):
            for el in driver.find_elements(By.CSS_SELECTOR, sel):
                if el.is_displayed():
                    try:
                        driver.execute_script("arguments[0].click();", el)
                        clicked = True
                        break
                    except Exception:
                        continue
            if clicked:
                break
        if not clicked:
            box.send_keys(Keys.ENTER)
        time.sleep(4)

        return driver.current_url, driver.page_source
    finally:
        rti._kill_driver_tree(driver)


def search_anime(query: str):
    """-> (results, next_url). Fail hone par poori wajah ke saath error uthata hai."""
    url = f"{SITE}/?s={quote_plus(query)}"
    notes = []

    # 1) Fast path: seedha search URL
    try:
        page_html = _fetch_requests(url)
        results, nxt = parse_results(page_html, url)
        if results:
            return results, nxt
        title = BeautifulSoup(page_html, "html.parser").title
        notes.append(
            f"[requests] page aaya ({len(page_html)} bytes, title="
            f"{_clean(title.get_text()) if title else 'none'}) par 0 result parse hue"
        )
    except Exception as e:
        notes.append(f"[requests] {type(e).__name__}: {str(e)[:200]}")
        LOGGER.warning(f"[Anime] requests fail: {e}")

    # 2) Fallback: Selenium click-flow
    try:
        cur_url, page_html = _selenium_search(query)
        results, nxt = parse_results(page_html, cur_url)
        if results:
            return results, nxt
        notes.append(f"[selenium] url={cur_url} par 0 result parse hue ({len(page_html)} bytes)")
    except Exception as e:
        notes.append(f"[selenium] {type(e).__name__}: {str(e)[:200]}")
        LOGGER.error(f"[Anime] selenium fail:\n{traceback.format_exc()}")

    raise SearchError("\n".join(notes))


class SearchError(Exception):
    pass


# ───────────────────────── extra sites: search ─────────────────────────
_GENERIC_SKIP = re.compile(
    r"/(category|tag|page|author|wp-|feed|contact|about|privacy|dmca|how-to|telegram|genre|year|search)(/|$)",
    re.I,
)


def _next_link(soup, base_url):
    nxt = soup.select_one("a.next.page-numbers, a.next, a[rel=next], .nav-links a.next")
    if not nxt:
        for a in soup.find_all("a", href=True):
            if re.fullmatch(r"\s*(next( page)?|»|›)\s*", a.get_text(" "), re.I):
                nxt = a
                break
    return _join(base_url, nxt["href"]) if nxt and nxt.get("href") else None


def _http_get_html(url: str) -> str:
    r = requests.get(url, headers=HEADERS, timeout=20)
    if _looks_blocked(r):
        raise RuntimeError(f"site ne block kiya (HTTP {r.status_code})")
    r.raise_for_status()
    return r.text


# ---- Hindi Anime Zone (WordPress, har season ka alag post) ----
_HAZ_CUT = re.compile(
    r"\b(hindi|multi|dual|bluray|blu-ray|web-?dl|hdrip|480p|720p|1080p|dubbed|dub|english)\b.*$", re.I)


def _haz_label(title: str) -> str:
    t = re.sub(r"\[[^\]]*\]", " ", title)
    cut = _HAZ_CUT.sub("", t)
    cut = re.sub(r"\s+in$", "", cut.strip(), flags=re.I)
    cut = re.sub(r"\s{2,}", " ", cut).strip(" –—:|-,&")
    return cut or _clean(title)


def parse_haz_results(page_html: str, base_url: str):
    soup = BeautifulSoup(page_html, "html.parser")
    host = _host(HAZ_SITE)
    anchors = soup.select("article h1 a, article h2 a, article h3 a, .entry-title a, .post-title a, h2 a")
    results, seen = [], set()
    for a in anchors:
        href = _join(base_url, a.get("href", ""))
        title = _clean(a.get("title") or a.get_text(" ", strip=True))
        if not title or _host(href) != host:
            continue
        path = _path(href)
        if path in ("", "/") or _GENERIC_SKIP.search(path) or "?s=" in href or href in seen:
            continue
        seen.add(href)
        results.append({"title": title, "url": href, "label": _haz_label(title)})
    return results, _next_link(soup, base_url)


def search_haz(query: str):
    url = f"{HAZ_SITE}/?s={quote_plus(query)}"
    return parse_haz_results(_http_get_html(url), url)


def fetch_haz_page(url: str):
    return parse_haz_results(_http_get_html(url), url)


# ---- Toono (sirf /series/ — /toono movies support nahi karta) ----
def parse_toono_results(page_html: str, base_url: str):
    soup = BeautifulSoup(page_html, "html.parser")
    host = _host(TOONO_SITE)
    found, order = {}, []
    for a in soup.find_all("a", href=True):
        href = _join(base_url, a["href"]).split("#")[0].split("?")[0]
        if _host(href) != host:
            continue
        m = re.match(r"^/series/([^/]+)/?$", _path(href))
        if not m:
            continue
        href = href.rstrip("/") + "/"
        cands = []
        head = a.find(["h1", "h2", "h3", "h4"]) or a.find_parent(["h1", "h2", "h3", "h4"])
        if head:
            cands.append(head.get_text(" ", strip=True))
        img = a.find("img")
        if img and img.get("alt"):
            cands.append(img["alt"])
        cands.append(a.get("title") or "")
        cands.append(a.get_text(" ", strip=True))
        # heading anchor ke bahar (sibling) ho sakti hai -> sirf is series wale card mein dhundo
        node = a
        for _ in range(3):
            node = node.parent
            if node is None or not hasattr(node, "find_all"):
                break
            hrefs = {
                _join(base_url, x["href"]).split("#")[0].split("?")[0].rstrip("/")
                for x in node.find_all("a", href=True)
                if re.match(r"^/(series|movies)/", _path(_join(base_url, x["href"])))
            }
            if hrefs and hrefs <= {href.rstrip("/")}:
                h = node.find(["h1", "h2", "h3", "h4"])
                if h:
                    cands.insert(0, h.get_text(" ", strip=True))
                    break
            else:
                break
        title = next((_clean(c) for c in cands
                      if c and _clean(c).lower() not in ("watch", "play", "watch now", "")
                      and len(_clean(c)) <= 120), "")
        if href not in found:
            found[href] = ""
            order.append((href, m.group(1)))
        if title and not found[href]:
            found[href] = title
    results = []
    for href, slug in order:
        title = found[href] or slug.replace("-", " ").title()
        results.append({"title": title, "url": href, "label": title})
    return results, None


def search_toono(query: str):
    last = None
    for url in (f"{TOONO_SITE}/?s={quote_plus(query)}", f"{TOONO_SITE}/search/{quote_plus(query)}/"):
        try:
            res, nxt = parse_toono_results(_http_get_html(url), url)
            if res:
                return res, nxt
        except Exception as e:
            last = e
            LOGGER.warning(f"[Anime] toono search {url}: {e}")
    if last:
        raise SearchError(f"[toono] {type(last).__name__}: {str(last)[:200]}")
    return [], None


# ---- Aniwatch / Animetsu (scraper API) ----
def search_aniw(source: str, query: str):
    res = aniw._search_blocking(source, query) or []
    out = []
    for r in res:
        kind = f" • {r['type']}" if r.get("type") and r["type"] != "ANIME" else ""
        out.append({"title": r["title"], "url": r["url"], "label": r["title"] + kind})
    return out, None


# ───────────────────────── sites ─────────────────────────
SITES = [
    ("rti", "Raretoon [Hindi]"),
    ("haz", "Hindi anime zone [multi language]"),
    ("toono", "Toono [hindi]"),
    ("aniwatch", "Aniwatch [ dual eng-jap ]"),
    ("animetsu", "Animetsu [ dual eng-jap ]"),
    ("aniabyss", "AniAbyss [ hindi dub • 4K ]"),
]
SITE_LABEL = dict(SITES)


def _site_search(site: str, query: str):
    if site == "rti":
        return search_anime(query)
    if site == "haz":
        return search_haz(query)
    if site == "toono":
        return search_toono(query)
    if site == "aniabyss":
        return aby.search_abyss(query)
    return search_aniw(site, query)


def _site_next(site: str, url: str):
    if site == "rti":
        return parse_results(fetch_page(url), url)
    if site == "haz":
        return fetch_haz_page(url)
    return [], None


# ───────────────────────── session / UI ─────────────────────────
class AnimeSearch:
    def __init__(self, query, orig_message):
        self.query = query
        self.orig = orig_message
        self.user_id = orig_message.from_user.id
        self.sid = uuid.uuid4().hex[:10]
        self.view = "sites"        # "sites" | "results"
        self.site = None
        self.cache = {}            # site -> {"pages": [(results, next_url)], "cur": 0, "error": str}
        self.child = None          # (sessions_dict, sid) — khula hua episode selector
        self.created = time.time()

    def _st(self):
        return self.cache.get(self.site) or {"pages": [([], None)], "cur": 0, "error": ""}

    def text(self):
        head = f"🔎 **Search:** `{self.query}`\n"
        if self.view == "sites":
            return head + "━━━━━━━━━━━━━━━━━━\n🌐 **Website chuno** 👇"
        st = self._st()
        res, _ = st["pages"][st["cur"]]
        t = f"{head}🌐 {SITE_LABEL[self.site]} • 📄 Page {st['cur'] + 1}\n━━━━━━━━━━━━━━━━━━\n"
        if res:
            return t + "👇 Anime pe tap karo (episode list khulegi):"
        err = f"\n```\n{st['error'][:800]}\n```" if st.get("error") else ""
        return t + f"❌ Is website pe `{self.query}` nahi mila.{err}\n👇 Back dabake doosri website chuno."

    def markup(self):
        rows = []
        if self.view == "sites":
            for key, label in SITES:
                rows.append([InlineKeyboardButton(label, callback_data=f"anime_s_{self.sid}_{key}")])
            rows.append([InlineKeyboardButton("❌ Close", callback_data=f"anime_x_{self.sid}")])
            return InlineKeyboardMarkup(rows)

        st = self._st()
        res, nxt = st["pages"][st["cur"]]
        for i, r in enumerate(res):
            t = r.get("label") or r["title"]
            t = t if len(t) <= 60 else t[:57] + "..."
            rows.append([InlineKeyboardButton(t, callback_data=f"anime_r_{self.sid}_{i}")])
        nav = []
        if st["cur"] > 0:
            nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"anime_p_{self.sid}"))
        if nxt:
            nav.append(InlineKeyboardButton("NEXT PAGE ▶️", callback_data=f"anime_n_{self.sid}"))
        if nav:
            rows.append(nav)
        rows.append([InlineKeyboardButton("🔙 Back (Website chuno)", callback_data=f"anime_w_{self.sid}")])
        rows.append([InlineKeyboardButton("❌ Close", callback_data=f"anime_x_{self.sid}")])
        return InlineKeyboardMarkup(rows)

    async def render(self, msg):
        try:
            await msg.edit(self.text(), reply_markup=self.markup())
        except Exception as e:
            if "MESSAGE_NOT_MODIFIED" not in str(e):
                LOGGER.error(f"[Anime] render error: {e}")


def _decorate(markup, sid, drop=()):
    """Selector ke markup mein 'Back to Results / Websites' row jodta hai."""
    rows = [
        [b for b in row if not any((b.callback_data or "").startswith(d) for d in drop)]
        for row in markup.inline_keyboard
    ]
    rows = [r for r in rows if r]
    back = [
        InlineKeyboardButton("🔙 Results", callback_data=f"anime_k_{sid}"),
        InlineKeyboardButton("🌐 Websites", callback_data=f"anime_w_{sid}"),
    ]
    if rows and any("Close" in (b.text or "") for b in rows[-1]):
        rows.insert(len(rows) - 1, back)
    else:
        rows.append(back)
    return InlineKeyboardMarkup(rows)


def _hook(sess, sid, drop=()):
    orig = sess.build_markup
    sess.build_markup = lambda: _decorate(orig(), sid, drop)


def _forget_child(s):
    if s.child:
        d, k = s.child
        d.pop(k, None)
        s.child = None


def _prune():
    now = time.time()
    for k in [k for k, v in SESSIONS.items() if now - v.created > SESSION_TIMEOUT]:
        SESSIONS.pop(k, None)


def _short(e, n=900) -> str:
    t = f"{type(e).__name__}: {e}" if not isinstance(e, SearchError) else str(e)
    return t[:n]


# ───────────────────────── handlers ─────────────────────────
@Client.on_message(filters.command("anime"))
async def anime_command(client: Client, message: Message):
    c = await check_chat(message, chat="Sudo")
    if not c:
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply(
            "**Usage:** `/anime <naam>`\n"
            "**Example:** `/anime naruto`\n\n"
            "Pehle website chuno:\n"
            + "\n".join(f"• {label}" for _, label in SITES)
        )
        return

    _prune()
    s = AnimeSearch(parts[1].strip(), message)
    SESSIONS[s.sid] = s
    await message.reply(s.text(), reply_markup=s.markup())


async def _do_search(s: AnimeSearch, site: str):
    loop = asyncio.get_event_loop()
    try:
        results, nxt = await loop.run_in_executor(None, _site_search, site, s.query)
        s.cache[site] = {"pages": [(results, nxt)], "cur": 0, "error": ""}
    except Exception as e:
        LOGGER.error(f"[Anime] search error ({site}):\n{traceback.format_exc()}")
        s.cache[site] = {"pages": [([], None)], "cur": 0, "error": _short(e)}


async def _open_item(client, cb: CallbackQuery, s: AnimeSearch, item: dict):
    """Chuni hui site ka episode selector isi message mein kholta hai."""
    site = s.site
    msg = cb.message
    loop = asyncio.get_event_loop()
    try:
        await msg.edit(f"🔍 `{item['title'][:60]}` scan ho raha hai...")
    except Exception:
        pass

    async def fail(text):
        _forget_child(s)
        s.view = "results"
        await s.render(msg)
        await msg.reply(text)

    try:
        if site == "rti":
            data = await loop.run_in_executor(None, rti.discover_items, item["url"])
            if not data["items"]:
                return await fail("❌ Is page pe koi episode/movie link nahi mila.")
            rti._prune_sessions()
            sess = rti.RTISelector(item["url"], data, orig_message=s.orig)
            sess.sid = uuid.uuid4().hex[:10]
            await sess.populate_toggles()
            rti.RTI_SESSIONS[sess.sid] = sess
            s.child = (rti.RTI_SESSIONS, sess.sid)
            drop = ()

        elif site == "haz":
            data = await loop.run_in_executor(None, haz.discover_haz_items, item["url"])
            if not data["episodes"]:
                return await fail("❌ Page se koi episode nahi mila.")
            haz._prune_haz_sessions()
            sess = haz.HazSelector(item["url"], data, orig_message=s.orig)
            sess.sid = uuid.uuid4().hex[:10]
            haz.HAZ_SESSIONS[sess.sid] = sess
            s.child = (haz.HAZ_SESSIONS, sess.sid)
            drop = ()

        elif site == "toono":
            data = await loop.run_in_executor(None, toono.discover_toono_items, item["url"])
            if not data["seasons"]:
                return await fail("❌ Page se koi season/episode nahi mila.")
            toono._prune_toono_sessions()
            sess = toono.ToonoSelector(item["url"], data, orig_message=s.orig)
            sess.sid = uuid.uuid4().hex[:10]
            await sess.populate_toggles()
            toono.TOONO_SESSIONS[sess.sid] = sess
            s.child = (toono.TOONO_SESSIONS, sess.sid)
            drop = ()

        elif site == "aniabyss":
            detail = await loop.run_in_executor(None, aby.fetch_detail, item["url"], item.get("_alt") or ())
            sess = aby.make_selector(detail, s.orig)
            if not sess.qualities:
                return await fail("❌ Is anime ke liye koi episode download link nahi mila.")
            s.child = (aby.ABY_SESSIONS, sess.sid)
            drop = ()

        else:  # aniwatch / animetsu
            sess = aniw.AniwSession(s.orig, site, query=s.query)
            ok, err = await aniw._load_episodes(sess, item["url"], item["title"])
            if not ok:
                return await fail(f"❌ {err}")
            await sess.populate_toggles()
            aniw._prune_sessions()
            aniw.ANIW_SESSIONS[sess.sid] = sess
            s.child = (aniw.ANIW_SESSIONS, sess.sid)
            drop = ("aniw_src_", "aniw_back_")   # hamara apna Back hai

    except Exception as e:
        LOGGER.error(f"[Anime] open error ({site}):\n{traceback.format_exc()}")
        return await fail(f"❌ **Page load nahi hua**\n🔗 {item['url']}\n```\n{_short(e)}\n```")

    _hook(sess, s.sid, drop)
    sess.msg = msg
    try:
        await msg.edit(sess.header_text(), reply_markup=sess.build_markup())
    except Exception as e:
        LOGGER.error(f"[Anime] selector render error: {e}")


# sirf apne callbacks (anime_<action>_<10-hex sid>...) — auto_monitor ke anime_pg_del_/anime_pg_noop se na takraye
@Client.on_callback_query(filters.regex(r"^anime_[sxwkpnr]_[0-9a-f]{10}"))
async def anime_callback(client: Client, cb: CallbackQuery):
    try:
        parts = cb.data.split("_")
        action, sid = parts[1], parts[2]
        s = SESSIONS.get(sid)
        if not s:
            await cb.answer("⌛ Session expire, /anime dubara bhejo.", show_alert=True)
            return
        if cb.from_user.id != s.user_id:
            await cb.answer("❌ Yeh tumhara search nahi hai.", show_alert=True)
            return

        if action == "x":
            _forget_child(s)
            SESSIONS.pop(sid, None)
            await cb.answer()
            try:
                await cb.message.delete()
            except Exception:
                pass
            return

        # 🌐 website picker pe wapas
        if action == "w":
            _forget_child(s)
            s.view = "sites"
            await cb.answer()
            await s.render(cb.message)
            return

        # 🔙 results pe wapas (episode selector se)
        if action == "k":
            _forget_child(s)
            s.view = "results"
            await cb.answer()
            await s.render(cb.message)
            return

        # website chuni
        if action == "s":
            site = parts[3]
            if site not in SITE_LABEL:
                await cb.answer()
                return
            if site == "toono" and not toono.SELENIUM_OK:
                await cb.answer("❌ Selenium install nahi hai (Toono ko chahiye).", show_alert=True)
                return
            if site in ("aniwatch", "animetsu") and not await asyncio.to_thread(get_binary_path):
                await cb.answer("❌ N_m3u8DL-RE binary nahi mili (auto-download bhi fail).", show_alert=True)
                return
            s.site = site
            await cb.answer(f"🔍 {SITE_LABEL[site]}")
            if site not in s.cache:
                try:
                    await cb.message.edit(
                        f"🔍 `{s.query}` — **{SITE_LABEL[site]}** pe search ho raha hai..."
                    )
                except Exception:
                    pass
                await _do_search(s, site)
            s.view = "results"
            await s.render(cb.message)
            return

        if s.view != "results" or not s.site:
            await cb.answer("Pehle website chuno.", show_alert=True)
            return
        st = s._st()

        if action == "p":
            st["cur"] = max(0, st["cur"] - 1)
            await cb.answer()
            await s.render(cb.message)
            return

        if action == "n":
            if st["cur"] + 1 < len(st["pages"]):
                st["cur"] += 1
                await cb.answer()
                await s.render(cb.message)
                return
            nxt = st["pages"][st["cur"]][1]
            if not nxt:
                await cb.answer("Aur page nahi hai.", show_alert=True)
                return
            await cb.answer("⏳ Next page load ho raha hai...")
            loop = asyncio.get_event_loop()
            try:
                results, n2 = await loop.run_in_executor(None, _site_next, s.site, nxt)
            except Exception as e:
                LOGGER.error(f"[Anime] next page error:\n{traceback.format_exc()}")
                await cb.message.reply(f"❌ **Next page nahi khula**\n🔗 {nxt}\n```\n{_short(e)}\n```")
                return
            if not results:
                await cb.message.reply("❌ Next page pe kuch nahi mila.")
                return
            st["pages"].append((results, n2))
            st["cur"] += 1
            await s.render(cb.message)
            return

        if action == "r":
            idx = int(parts[3])
            item = st["pages"][st["cur"]][0][idx]
            await cb.answer("📂 Episode list khul rahi hai...")
            await _open_item(client, cb, s, item)
            return

        await cb.answer()
    except Exception as e:
        LOGGER.error(f"[Anime] callback error:\n{traceback.format_exc()}")
        try:
            await cb.answer(f"❌ {_short(e, 180)}", show_alert=True)
        except Exception:
            pass
