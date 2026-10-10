"""
anime_search.py
================
/anime <naam>  ->  RareAnimes site pe search karta hai aur results ko
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
from urllib.parse import quote_plus, urljoin, urlparse

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

import os

SITE = os.getenv("ANIME_SITE", "https://www.rareanimes.mov").rstrip("/")
SITE_HOST = urlparse(SITE).netloc.replace("www.", "")

SESSIONS = {}
SESSION_TIMEOUT = 3600
HEADERS = rti.HEADERS

# URL ke yeh hisse result nahi hain (menu/category/pagination links)
_SKIP_PATH = re.compile(
    r"/(category|tag|page|author|wp-|feed|contact|about|privacy|dmca|how-to|telegram)(/|$)",
    re.I,
)


# ───────────────────────── parsing ─────────────────────────
def _same_site(href: str) -> bool:
    return urlparse(href).netloc.replace("www.", "") == SITE_HOST


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
        href = urljoin(base_url, a.get("href", ""))
        title = _clean(a.get_text(" ", strip=True))
        if not title or not _same_site(href):
            continue
        path = urlparse(href).path
        if path in ("", "/") or _SKIP_PATH.search(path) or "?s=" in href:
            continue
        if href in seen:
            continue
        seen.add(href)
        results.append({"title": title, "url": href})

    next_url = None
    nxt = soup.select_one("a.next.page-numbers, a.next, a[rel=next], .nav-links a.next")
    if not nxt:
        for a in soup.find_all("a", href=True):
            if re.search(r"next\s*page|^\s*next\b|»|›", a.get_text(" "), re.I):
                nxt = a
                break
    if nxt and nxt.get("href"):
        next_url = urljoin(base_url, nxt["href"])
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
    """-> (results, next_url)"""
    url = f"{SITE}/?s={quote_plus(query)}"
    try:
        results, nxt = parse_results(_fetch_requests(url), url)
        if results:
            return results, nxt
    except Exception as e:
        LOGGER.info(f"[Anime] requests search fail ({e}) — selenium fallback")

    cur_url, page_html = _selenium_search(query)
    return parse_results(page_html, cur_url)


# ───────────────────────── session / UI ─────────────────────────
class AnimeSearch:
    def __init__(self, query, orig_message):
        self.query = query
        self.orig = orig_message
        self.user_id = orig_message.from_user.id
        self.sid = uuid.uuid4().hex[:10]
        self.pages = []          # [(results, next_url)]
        self.cur = 0
        self.msg = None
        self.created = time.time()

    def text(self):
        res, nxt = self.pages[self.cur]
        return (
            f"🔎 **Search:** `{self.query}`\n"
            f"🌐 RareAnimes • 📄 Page {self.cur + 1}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"👇 Anime pe tap karo (episode list khulegi):"
        )

    def markup(self):
        res, nxt = self.pages[self.cur]
        rows = []
        for i, r in enumerate(res):
            t = r["title"]
            t = t if len(t) <= 60 else t[:57] + "..."
            rows.append([InlineKeyboardButton(t, callback_data=f"anime_r_{self.sid}_{i}")])
        nav = []
        if self.cur > 0:
            nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"anime_p_{self.sid}"))
        if nxt:
            nav.append(InlineKeyboardButton("NEXT PAGE ▶️", callback_data=f"anime_n_{self.sid}"))
        if nav:
            rows.append(nav)
        rows.append([InlineKeyboardButton("❌ Close", callback_data=f"anime_x_{self.sid}")])
        return InlineKeyboardMarkup(rows)

    async def render(self):
        try:
            await self.msg.edit(self.text(), reply_markup=self.markup())
        except Exception as e:
            LOGGER.error(f"[Anime] render error: {e}")


def _prune():
    now = time.time()
    for k in [k for k, v in SESSIONS.items() if now - v.created > SESSION_TIMEOUT]:
        SESSIONS.pop(k, None)


# ───────────────────────── handlers ─────────────────────────
@Client.on_message(filters.command("anime"))
async def anime_command(client: Client, message: Message):
    c = await check_chat(message, chat="Sudo")
    if not c:
        return
    if not rti.SELENIUM_OK:
        await message.reply("❌ Selenium install nahi hai!")
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply(
            "**Usage:** `/anime <naam>`\n"
            "**Example:** `/anime naruto`\n\n"
            "RareAnimes pe search karke results buttons mein dikhayega."
        )
        return

    query = parts[1].strip()
    status = await message.reply(f"🔍 `{query}` search ho raha hai...")
    loop = asyncio.get_event_loop()
    try:
        results, nxt = await loop.run_in_executor(None, search_anime, query)
    except Exception as e:
        LOGGER.error(f"[Anime] search error: {e}")
        await status.edit(f"❌ Search fail: `{str(e)[:100]}`")
        return

    if not results:
        await status.edit(f"❌ `{query}` ke liye kuch nahi mila.")
        return

    _prune()
    s = AnimeSearch(query, message)
    s.pages.append((results, nxt))
    SESSIONS[s.sid] = s
    s.msg = status
    await s.render()


@Client.on_callback_query(filters.regex(r"^anime_"))
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
            SESSIONS.pop(sid, None)
            await cb.answer()
            try:
                await cb.message.delete()
            except Exception:
                pass
            return

        if action == "p":
            s.cur = max(0, s.cur - 1)
            await cb.answer()
            await s.render()
            return

        if action == "n":
            if s.cur + 1 < len(s.pages):          # pehle se fetch ho chuka
                s.cur += 1
                await cb.answer()
                await s.render()
                return
            nxt = s.pages[s.cur][1]
            if not nxt:
                await cb.answer("Aur page nahi hai.", show_alert=True)
                return
            await cb.answer("⏳ Next page load ho raha hai...")
            loop = asyncio.get_event_loop()
            try:
                page_html = await loop.run_in_executor(None, fetch_page, nxt)
                results, n2 = parse_results(page_html, nxt)
            except Exception as e:
                LOGGER.error(f"[Anime] next page error: {e}")
                await cb.message.reply(f"❌ Next page nahi khula: `{str(e)[:100]}`")
                return
            if not results:
                await cb.message.reply("❌ Next page pe kuch nahi mila.")
                return
            s.pages.append((results, n2))
            s.cur += 1
            await s.render()
            return

        if action == "r":
            idx = int(parts[3])
            item = s.pages[s.cur][0][idx]
            await cb.answer("📂 Episode list khul rahi hai...")
            status = await cb.message.reply(f"🔍 `{item['title'][:60]}` scan ho raha hai...")
            loop = asyncio.get_event_loop()
            try:
                data = await loop.run_in_executor(None, rti.discover_items, item["url"])
            except Exception as e:
                LOGGER.error(f"[Anime] discover error: {e}")
                await status.edit(f"❌ Page load nahi hua: `{str(e)[:100]}`")
                return
            if not data["items"]:
                await status.edit("❌ Is page pe koi episode/movie link nahi mila.")
                return

            # Ab wahi /rti ka selector — aage ka kaam /rti hi sambhalega
            rti._prune_sessions()
            sess = rti.RTISelector(item["url"], data, orig_message=s.orig)
            sess.sid = uuid.uuid4().hex[:10]
            await sess.populate_toggles()
            rti.RTI_SESSIONS[sess.sid] = sess
            try:
                await status.delete()
            except Exception:
                pass
            sess.msg = await cb.message.reply(sess.header_text(), reply_markup=sess.build_markup())
            return

        await cb.answer()
    except Exception as e:
        LOGGER.error(f"[Anime] callback error: {e}")
        try:
            await cb.answer("❌ Error, dubara try karo.", show_alert=True)
        except Exception:
            pass
