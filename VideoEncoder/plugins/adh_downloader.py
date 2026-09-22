"""
adh_downloader.py  v1
========================
Command:
  /adh <series_page_url>   (animedubhindi.link ka ek anime/series post)

Flow (jaisa Subhankar ne screenshots + text mein bataya) — /Haz jaisa hi hai,
bas link-resolve wala step alag hai (Cloudflare/cookie nahi, GDF -> GDFlix ->
fastdl-one.pages.dev wait-page):

  1. Series page (static HTML, requests+BS4 se seedha scrape) se title aur
     "Audio Tracks: Hindi | English | Japanese" field se available languages
     nikalte hain, phir "Download / Watch" button ka href leke episode-list
     page (new.adhlinks.com/episode/...) tak pahunchte hain.
  2. Episode-list page se har episode ke 4 quality-blocks milte hain
     (480P x264 / 720P x264 / 1080P x265 10bit / 1080P x264 HQ), har block
     mein "Hcloud | Multi | MEGA | GDF | Fprs" links. Jaisa bataya gaya —
     sirf pehli 3 quality allowed hain (HQ waali jaanbujh kar skip, kyunki
     woh sabse bada file hota hai), aur har quality mein sirf **GDF** wala
     link use karte hain.
  3. Bot pehle Language buttons dikhata hai (Audio Tracks se), phir Quality
     (480p x264 / 720p x264 / 1080p x265 10bit), phir episode list
     (Haz/RTI/Toono jaisa hi multi-select button menu).
  4. Chosen episode+quality ka GDF link (new4.gdflix.io/file/...) kholke
     us page pe **INSTANT DL [10GBPS]** button ka href nikalte hain — yeh
     seedha `fastdl-one.pages.dev/?url=...` "wait" page ka link hota hai
     (real Cloudflare captcha nahi, sirf ek client-side countdown/ad-wait
     hai — asli link static HTML mein hi maujood hota hai, isliye Selenium
     ki zaroorat nahi, seedha requests se fetch karke parse kar lete hain).
  5. Us wait-page pe jo "Download Here" button hai, uska href hi final
     direct-download link hai.
  6. Final link ko **/url wale `_download_url()` se hi** download karte hain
     (jaisa bataya "url upload ki tarah download kar lena") — SmartDL pehle,
     fail ho to aiohttp streaming fallback, dono url_upload.py se reuse.
  7. haz_downloader.py ka hi generic `_apply_language_filter()` reuse karte
     hain — sirf chuni hui language ka audio track + English subtitle
     rakhte hain (site ki files already multi-audio hoti hain, jaisa
     filename "[Hindi-Eng-Jap] Esub.mkv" se pata chalta hai).
  8. url_upload.py ka hi `_do_upload()` reuse karke Telegram pe upload.

NOTE / assumptions (live site pe test karke confirm karna padega — jaisa
khud bataya gaya "pehle testing ke liye karna, dikkat ho to bata dena"):
  - Series page pe "Download / Watch" button ka exact HTML abhi is sandbox
    se (network band hai) live nahi dekha ja saka — isliye text-match
    ("Download" + "Watch" dono ek hi link ke text mein) se dhoondha ja raha
    hai. Agar button HTML kuch alag structure mein hua (e.g. text ke bajaye
    sirf icon/image) to yeh miss ho sakta hai.
  - Episode-list page pe quality-heading aur "GDF" anchor ke beech exact
    tag-nesting bhi live confirm nahi ho paayi — isliye Haz ki tarah hi
    "nearest previous text marker" (find_previous) approach use ki hai,
    jo tag-structure-agnostic hai.
  - GDFlix "INSTANT DL" button aur fastdl-one "Download Here" button dono
    ke liye pehle "exact button-text wala <a>" try hota hai, fir raw-HTML
    regex fallback, taaki koi bhi miss ho to turant LOGGER.info se pata
    chal jaaye (Debug block message mein dikhega) — ek round mein hi fix
    ho jaaye jaisa Haz mein bhi hai.
"""

import asyncio
import re
import time
import uuid
from urllib.parse import urljoin, urlparse

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
from .rti_downloader import _kb
from .haz_downloader import _apply_language_filter
from .url_upload import _get_filename_from_url, _download_url, _do_upload

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

# Session reuse karte hain — connection-keepalive + agar site pehli hit pe
# Cloudflare "cookie/JS check" jaisa kuch set karti hai (bina full challenge
# ke, sirf ek set-cookie) to woh dusri attempt mein kaam aa jaaye.
_SESSION = requests.Session()
_SESSION.headers.update(HEADERS)

# animedubhindi.link jaisi shared-hosting/Cloudflare site kabhi-kabhi pehli
# request pe 20s se zyada le leti hai (cold start / anti-bot delay) —
# isliye lamba timeout + retry-with-backoff, taaki genuine slow response
# aur "site hi down hai" mein farak pata chale.
def _http_get(url: str, timeout: int = 35, retries: int = 2, debug: list | None = None, **kwargs):
    def log(msg):
        if debug is not None:
            debug.append(msg)
        LOGGER.info(f"[Adh] {msg}")

    last_exc = None
    for attempt in range(1, retries + 2):  # total attempts = retries + 1
        try:
            r = _SESSION.get(url, timeout=timeout, **kwargs)
            return r
        except requests.exceptions.ReadTimeout as e:
            last_exc = e
            log(f"⚠️ Attempt {attempt}: read timed out ({timeout}s) on {url[:80]}")
        except requests.exceptions.RequestException as e:
            last_exc = e
            log(f"⚠️ Attempt {attempt}: {str(e).splitlines()[0][:120]}")
        if attempt < retries + 1:
            time.sleep(2 * attempt)
    raise last_exc

# Sirf yehi 3 qualities dikhani hain (4 available hain page pe, HQ skip)
ADH_QUALITIES = ["480p x264", "720p x264", "1080p x265 10bit"]

PER_PAGE = 10

ADH_SESSIONS = {}
ADH_SESSION_TIMEOUT = 3600


def _prune_adh_sessions():
    now = time.time()
    stale = [k for k, v in ADH_SESSIONS.items() if now - v.created > ADH_SESSION_TIMEOUT]
    for k in stale:
        ADH_SESSIONS.pop(k, None)


def _normalize_adh_quality(raw_text: str) -> str | None:
    """
    Episode-list page ke quality-heading text ko teen allowed labels mein se
    ek pe normalize karta hai. "1080P x264 HQ" wale ko jaanbujh kar None
    (skip) return karta hai — bataya gaya tha "last wala jyada MB ka file
    nahin karna".
    """
    low = raw_text.lower()
    if "1080p" in low and "hq" in low:
        return None  # jaanbujh kar skip
    if "480p" in low and "x264" in low:
        return "480p x264"
    if "720p" in low and "x264" in low:
        return "720p x264"
    if "1080p" in low and "x265" in low and "10bit" in low:
        return "1080p x265 10bit"
    return None


# ─────────────────────────────────────────────
#  "Download / Watch" button ka target URL dhoondo — 6 fallback strategies,
#  kyunki kai WP "download" themes button ko plain <a href> ki jagah
#  onclick JS / data-attribute se bhi bana dete hain (bot-scraping se bachne
#  ke liye jaanbujh kar).
# ─────────────────────────────────────────────
def _find_adh_episode_list_url(soup: BeautifulSoup, raw_html: str, debug: list) -> str | None:
    def log(msg):
        debug.append(msg)
        LOGGER.info(f"[Adh] {msg}")

    # 1) <a href> jiske visible text mein "download" + "watch" dono ho
    for a in soup.find_all("a", href=True):
        label = a.get_text(" ", strip=True).lower()
        href = a["href"].strip()
        if "download" in label and "watch" in label and href and href != "#":
            log(f"✅ Strategy1 (a[href] text-match) se mila: {href[:100]}")
            return href

    # 2) href value khud "/episode/" contain karta ho (is site ka episode-list
    #    URL pattern, jaisa user ne screenshot mein dikhaya)
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if "/episode/" in href.lower():
            log(f"✅ Strategy2 (href mein /episode/) se mila: {href[:100]}")
            return href

    # 3) onclick="location.href='...'" / window.location / window.open jaisa
    #    JS redirect — <button> ya <a href="#"> pe common hota hai
    for tag in soup.find_all(onclick=True):
        m = re.search(
            r'''(?:location\.href|window\.location(?:\.href)?|window\.open)\s*=?\s*\(?['"]([^'"]+)['"]''',
            tag["onclick"],
        )
        if m:
            log(f"✅ Strategy3 (onclick JS redirect) se mila: {m.group(1)[:100]}")
            return m.group(1)

    # 4) data-href / data-url / data-link attribute
    for attr in ("data-href", "data-url", "data-link"):
        tag = soup.find(attrs={attr: True})
        if tag:
            val = (tag.get(attr) or "").strip()
            if val and val != "#":
                log(f"✅ Strategy4 (attribute {attr}) se mila: {val[:100]}")
                return val

    # 5) raw-HTML regex — koi bhi href jisme "/episode/" ho (nested tags ki
    #    wajah se BS4 se text-match miss ho jaaye to bhi yeh pakad lega)
    m = re.search(r'''href=["\']([^"\']*?/episode/[^"\']*)["\']''', raw_html, re.IGNORECASE)
    if m:
        log(f"✅ Strategy5 (raw-HTML /episode/ regex) se mila: {m.group(1)[:100]}")
        return m.group(1)

    # 6) raw-HTML regex — "Download / Watch" text ke turant pehle wala href
    m = re.search(
        r'href=["\'](https?://[^"\']+)["\'][^>]*>\s*(?:<[^>]+>\s*)*Download\s*/\s*Watch',
        raw_html, re.IGNORECASE,
    )
    if m:
        log(f"✅ Strategy6 (raw-HTML Download/Watch regex) se mila: {m.group(1)[:100]}")
        return m.group(1)

    log("❌ 6 strategies try ki, kisi se bhi 'Download / Watch' ka target URL nahi mila")
    return None


# ─────────────────────────────────────────────
#  Series-URL slug se seedha episode-list URL derive karo — jaisa khud
#  confirm kiya:
#    .../daemons-of-the-shadow-realm-season-1-hindi-multi-audio/
#      -> https://new.adhlinks.com/episode/daemons-of-the-shadow-realm/
#    .../mushoku-tensei-jobless-reincarnation-season-3-hindi-multi-audio/
#      -> https://new.adhlinks.com/episode/mushoku-tensei-jobless-reincarnation/
#  "-season-<N>" ke baad ka sab (season number + audio/quality suffix)
#  hata dete hain. Isse ek extra HTTP request bachta hai aur button ki
#  JS-obfuscation wali dikkat bhi bypass ho jaati hai.
# ─────────────────────────────────────────────
def _derive_adh_episode_list_url(series_url: str) -> str | None:
    path = urlparse(series_url).path.strip("/")
    if not path:
        return None
    slug = path.split("/")[-1]

    new_slug = re.sub(r'-season-\d+.*$', '', slug, flags=re.IGNORECASE)
    if new_slug == slug:
        # "-season-" nahi mila (movie/OVA jaisa single-part title) — common
        # audio/dub suffix khud try karke hata do
        new_slug = re.sub(
            r'-(hindi-multi-audio|multi-audio|hindi-dubbed|hindi-dub|dual-audio)$',
            '', slug, flags=re.IGNORECASE,
        )

    if not new_slug:
        return None
    return f"https://new.adhlinks.com/episode/{new_slug}/"


# ─────────────────────────────────────────────
#  Step 1: series page -> title / languages / episode-list page url
# ─────────────────────────────────────────────
def discover_adh_series(series_url: str) -> dict:
    """
    Returns: {"title": str, "languages": [str,...], "episode_list_url": str|None,
              "episode_list_html": str|None, "debug": list}
    """
    debug = []
    r = _http_get(series_url, debug=debug)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    title_tag = soup.find("h1")
    title = title_tag.get_text(strip=True) if title_tag else "Unknown"

    # ── Languages: "Audio Tracks: Hindi | English | Japanese" field ──
    page_text = soup.get_text("\n")
    languages = []
    m = re.search(r'Audio\s*Tracks?\s*:\s*([^\n]+)', page_text, re.IGNORECASE)
    if m:
        raw = m.group(1).strip()
        languages = [x.strip() for x in re.split(r'[|/]', raw) if x.strip()]
    if not languages:
        languages = ["Hindi", "English"]

    episode_list_url = None
    episode_list_html = None

    # ── Strategy A (fast, confirmed): URL-slug se seedha derive karo ──
    candidate = _derive_adh_episode_list_url(series_url)
    if candidate:
        try:
            r2 = _http_get(candidate, debug=debug)
            if r2.status_code == 200 and (
                re.search(r'Episode\s*:?\s*\d+', r2.text, re.IGNORECASE) or "gdf" in r2.text.lower()
            ):
                episode_list_url = candidate
                episode_list_html = r2.text
                debug.append(f"✅ URL-pattern se derive kiya episode-list URL valid nikla: {candidate}")
            else:
                debug.append(
                    f"⚠️ Derived URL ({candidate}) valid episode-page jaisa nahi laga "
                    f"(HTTP {r2.status_code}) — button-scrape try kar rahe hain"
                )
        except Exception as e:
            debug.append(
                f"⚠️ Derived URL fetch fail: {str(e).splitlines()[0][:100]} — button-scrape try kar rahe hain"
            )

    # ── Strategy B (fallback): series page ke "Download / Watch" button ──
    if not episode_list_url:
        found = _find_adh_episode_list_url(soup, r.text, debug)
        if found:
            episode_list_url = urljoin(series_url, found)

    return {
        "title": title,
        "languages": languages,
        "episode_list_url": episode_list_url,
        "episode_list_html": episode_list_html,
        "debug": debug,
    }


# ─────────────────────────────────────────────
#  Step 2: episode-list page -> {ep_num: {quality: gdf_href}}
# ─────────────────────────────────────────────
def discover_adh_episodes(episode_list_url: str, html: str | None = None) -> list:
    """
    Returns: [{"num": int, "qualities": {q: gdf_href}}, ...]
    `html` diya ho (discover_adh_series ke Strategy-A validation se already
    fetch ho chuka) to dobara request nahi bhejte — sirf usse parse karte hain.
    """
    if html is None:
        r = _http_get(episode_list_url)
        r.raise_for_status()
        html = r.text
    soup = BeautifulSoup(html, "html.parser")

    episodes = {}
    for a in soup.find_all("a", string=re.compile(r'^\s*GDF\s*$', re.IGNORECASE)):
        href = a.get("href")
        if not href:
            continue

        qual_marker = a.find_previous(
            string=re.compile(r'\b(480P|720P|1080P)\b[^[\n]*x26[45]', re.IGNORECASE)
        )
        if not qual_marker:
            continue
        quality = _normalize_adh_quality(str(qual_marker))
        if not quality:
            continue  # HQ ya unrecognized quality — skip

        ep_marker = a.find_previous(string=re.compile(r'Episode\s*:?\s*\d+', re.IGNORECASE))
        if not ep_marker:
            continue
        num_m = re.search(r'\d+', ep_marker)
        if not num_m:
            continue
        ep_num = int(num_m.group())

        ep = episodes.setdefault(ep_num, {"num": ep_num, "qualities": {}})
        ep["qualities"][quality] = href

    return sorted(episodes.values(), key=lambda x: x["num"])


# ─────────────────────────────────────────────
#  Step 3: GDF page -> GDFlix "INSTANT DL" -> fastdl-one "Download Here"
#  Poori tarah requests+BS4 (Selenium/cookie ki zaroorat nahi — yeh real
#  captcha nahi, sirf client-side wait/ad page hai jiska asli link static
#  HTML mein hi maujood hota hai).
# ─────────────────────────────────────────────
def _resolve_adh_download_link(gdf_url: str, debug: list) -> str | None:
    def log(msg):
        debug.append(msg)
        LOGGER.info(f"[Adh] {msg}")

    try:
        r = _http_get(gdf_url, timeout=30, debug=debug, allow_redirects=True)
    except Exception as e:
        log(f"❌ GDF page fetch fail (after retries): {str(e).splitlines()[0][:120]}")
        return None

    instant_href = None
    soup = BeautifulSoup(r.text, "html.parser")
    for a in soup.find_all("a", href=True):
        label = a.get_text(" ", strip=True)
        if "instant dl" in label.lower():
            instant_href = a["href"]
            break
    if not instant_href:
        m = re.search(
            r'href=["\'](https?://[^"\']+)["\'][^>]*>\s*(?:<[^>]+>\s*)*.*?INSTANT\s*DL',
            r.text, re.IGNORECASE | re.DOTALL,
        )
        if m:
            instant_href = m.group(1)

    if not instant_href:
        log("❌ GDF page pe 'INSTANT DL' button nahi mila")
        return None

    instant_href = urljoin(gdf_url, instant_href)
    log(f"✅ INSTANT DL link mila: {instant_href[:100]}")

    try:
        r2 = _http_get(instant_href, timeout=30, debug=debug, allow_redirects=True)
    except Exception as e:
        log(f"❌ Instant-DL wait-page fetch fail (after retries): {str(e).splitlines()[0][:120]}")
        return None

    text2 = r2.text
    soup2 = BeautifulSoup(text2, "html.parser")
    final_url = None

    # Strategy 1: exact "Download Here" button
    for a in soup2.find_all("a", href=True):
        label = a.get_text(" ", strip=True)
        if "download here" in label.lower():
            final_url = a["href"]
            break

    # Strategy 2: raw-HTML regex fallback (nested tag ke andar text ho to)
    if not final_url:
        m = re.search(
            r'href=["\'](https?://[^"\']+)["\'][^>]*>\s*(?:<[^>]+>\s*)*.*?Download\s*Here',
            text2, re.IGNORECASE | re.DOTALL,
        )
        if m:
            final_url = m.group(1)

    # Strategy 3: base64-encoded link (kai "wait" pages atob() se link banate hain)
    if not final_url:
        for m in re.finditer(r'atob\(["\']([A-Za-z0-9+/=]+)["\']\)', text2):
            try:
                import base64
                decoded = base64.b64decode(m.group(1)).decode("utf-8", "ignore").strip()
                if decoded.startswith("http"):
                    final_url = decoded
                    log("✅ base64-encoded link decode karke mila")
                    break
            except Exception:
                continue

    # Strategy 4: poore HTML mein seedha video-file URL dhoondo (fallback)
    if not final_url:
        m = re.search(r'(https?://[^\s"\'<>]+\.(?:mkv|mp4))', text2, re.IGNORECASE)
        if m:
            final_url = m.group(1)
            log("✅ Raw HTML regex se .mkv/.mp4 link mila (fallback)")

    if not final_url:
        log("❌ Wait-page pe 'Download Here' final link nahi mila")
        return None

    log(f"✅ Final direct link mila: {final_url[:100]}")
    return final_url


# ─────────────────────────────────────────────
#  Selector UI: Language -> Quality -> Episode list (multi-select)
# ─────────────────────────────────────────────
class AdhSelector:
    def __init__(self, series_url: str, title: str, languages: list, episodes: list, orig_message: Message):
        self.series_url = series_url
        self.title = title
        self.languages = languages
        self.episodes = episodes
        self.orig_message = orig_message
        self.sid = None
        self.msg = None
        self.stage = "lang" if len(self.languages) > 1 else "quality"
        self.language = self.languages[0] if len(self.languages) == 1 else None
        self.quality = None
        self.page = 0
        self.multi = False
        self.selected = set()
        self.created = time.time()

    @property
    def total_pages(self):
        return max(1, (len(self.episodes) + PER_PAGE - 1) // PER_PAGE)

    def page_items(self):
        start = self.page * PER_PAGE
        return list(enumerate(self.episodes))[start:start + PER_PAGE]

    def header_text(self):
        lines = ["📂 **Select**", "━━━━━━━━━━━━━━━━━━", f"🎬 **{self.title}**", "🌐 Website: 🍥 Anime Dub Hindi"]
        if self.stage == "lang":
            lines.append("👇 Pehle Language chuno:")
        elif self.stage == "quality":
            if self.language:
                lines.append(f"🗣️ Language: **{self.language}**")
            lines.append("👇 Ab Quality chuno:")
        else:
            lines.append(f"🗣️ Language: **{self.language}** • 🎞️ Quality: **{self.quality}**")
            lines.append(f"📖 Episodes: {len(self.episodes)}")
            lines.append("━━━━━━━━━━━━━━━━━━")
            lines.append(f"👇 Tap an episode to download:  (Page {self.page + 1}/{self.total_pages})")
            if self.multi:
                lines.append(f"\n☑️ **Multi-select ON** — chosen: `{len(self.selected)}`")
        return "\n".join(lines)

    def build_markup(self):
        sid = self.sid
        rows = []

        if self.stage == "lang":
            row = []
            for lang in self.languages:
                row.append((f"🗣️ {lang}", f"adh_lang_{sid}_{lang}"))
                if len(row) == 2:
                    rows.append(row)
                    row = []
            if row:
                rows.append(row)

        elif self.stage == "quality":
            for q in ADH_QUALITIES:
                rows.append([(f"🎞️ {q}", f"adh_qual_{sid}_{q}")])
            if len(self.languages) > 1:
                rows.append([("🔙 Back to Language", f"adh_back_{sid}")])

        else:
            row = []
            for idx, ep in self.page_items():
                mark = "✅ " if idx in self.selected else ""
                row.append((f"{mark}E{ep['num']:02d}", f"adh_ep_{sid}_{idx}"))
                if len(row) == 3:
                    rows.append(row)
                    row = []
            if row:
                rows.append(row)

            if self.total_pages > 1:
                nav = []
                if self.page > 0:
                    nav.append(("◀️ Prev", f"adh_pg_{sid}_{self.page - 1}"))
                nav.append((f"📄 {self.page + 1}/{self.total_pages}", f"adh_noop_{sid}"))
                if self.page < self.total_pages - 1:
                    nav.append(("Next ▶️", f"adh_pg_{sid}_{self.page + 1}"))
                rows.append(nav)

            if self.multi:
                rows.append([(f"⬇️ Download Selected ({len(self.selected)})", f"adh_dl_{sid}")])
                rows.append([("☑️ Multi-select: ON (tap to turn off)", f"adh_toggle_{sid}")])
            else:
                rows.append([("☑️ Select Multiple", f"adh_toggle_{sid}")])

            rows.append([("🔙 Back to Quality", f"adh_backq_{sid}")])

        rows.append([("❌ Close", f"adh_close_{sid}")])
        return _kb(rows)

    async def render(self):
        if not self.msg:
            return
        try:
            await self.msg.edit(self.header_text(), reply_markup=self.build_markup())
        except Exception as e:
            LOGGER.error(f"[Adh] selector render error: {e}")


# ─────────────────────────────────────────────
#  Ek episode: GDF link -> resolve -> download -> filter -> upload
# ─────────────────────────────────────────────
async def _process_adh_item(client, message, ep: dict, status_msg, index: int, total: int,
                             language: str, quality: str):
    ep_label = f"E{ep['num']:02d}"
    loop = asyncio.get_event_loop()
    gdf_url = ep["qualities"].get(quality)
    if not gdf_url:
        try:
            await status_msg.edit(f"🛑 **{ep_label}** — is episode mein `{quality}` quality available nahi hai.")
        except Exception:
            pass
        return "error"

    try:
        await status_msg.edit(f"🔍 **{ep_label}** (`{index}/{total}`)\nDownload link nikal raha hoon...")
    except Exception:
        pass

    debug = []
    final_url = await loop.run_in_executor(None, _resolve_adh_download_link, gdf_url, debug)

    if not final_url:
        debug_text = "\n".join(debug[-10:]) if debug else "(koi debug info nahi mili)"
        try:
            await status_msg.edit(
                f"🛑 **{ep_label} — Link Resolve Fail**\n\n"
                f"❌ Final download link nahi mila.\n\n"
                f"**Debug (last steps):**\n```\n{debug_text}\n```\n"
                f"⛔ Agle items **band** kar diye gaye."
            )
        except Exception:
            pass
        return "error"

    try:
        await status_msg.edit(f"⬇️ **{ep_label}** — download ho raha hai...")
    except Exception:
        pass

    try:
        filename = await _get_filename_from_url(final_url)
        filepath = await _download_url(final_url, filename, status_msg, message)
    except Exception as e:
        LOGGER.error(f"[Adh] {ep_label} download error: {e}")
        try:
            await status_msg.edit(
                f"🛑 **{ep_label} — Download Error**\n\n❌ `{str(e)[:150]}`\n\n"
                f"⛔ Agle items **band** kar diye gaye."
            )
        except Exception:
            pass
        return "error"

    try:
        filepath, has_eng_sub = await _apply_language_filter(filepath, language, status_msg)
    except Exception as e:
        LOGGER.error(f"[Adh] {ep_label} audio/sub filter error: {e}")
        try:
            await status_msg.edit(
                f"🛑 **{ep_label} — Filter Error**\n\n❌ `{str(e)[:150]}`\n\n"
                f"⛔ Agle items **band** kar diye gaye."
            )
        except Exception:
            pass
        return "error"

    await _do_upload(client, filepath, message, status_msg, has_eng_sub=has_eng_sub)
    try:
        final_text = status_msg.text or ""
    except Exception:
        final_text = ""
    if "failed" in final_text.lower():
        return "error"
    return "ok"


async def _download_adh_items(client, status_msg, orig_message, sess: "AdhSelector", idxs: list):
    total = len(idxs)
    all_ok = True
    for i, idx in enumerate(idxs, 1):
        ep = sess.episodes[idx]
        result = await _process_adh_item(
            client, orig_message, ep, status_msg, i, total, sess.language, sess.quality,
        )
        if result != "ok":
            all_ok = False
            break
        if i < total:
            status_msg = await orig_message.reply("⏳ Agla episode shuru ho raha hai...")
            await asyncio.sleep(1)

    if all_ok:
        try:
            await status_msg.delete()
        except Exception:
            pass


# ─────────────────────────────────────────────
#  Callback handler
# ─────────────────────────────────────────────
@Client.on_callback_query(filters.regex(r"^adh_"))
async def adh_callback_handler(client: Client, cb: CallbackQuery):
    try:
        parts = cb.data.split("_")
        action = parts[1] if len(parts) > 1 else ""
        sid = parts[2] if len(parts) > 2 else None
        sess = ADH_SESSIONS.get(sid)

        if not sess:
            await cb.answer("⌛ Session expired, /adh <url> dubara bhejo.", show_alert=True)
            return

        if action == "noop":
            await cb.answer()
            return

        if action == "close":
            ADH_SESSIONS.pop(sid, None)
            await cb.answer()
            try:
                await cb.message.delete()
            except Exception:
                pass
            return

        if action == "lang":
            sess.language = "_".join(parts[3:])
            sess.stage = "quality"
            await cb.answer()
            await sess.render()
            return

        if action == "back":
            sess.stage = "lang"
            sess.quality = None
            await cb.answer()
            await sess.render()
            return

        if action == "qual":
            sess.quality = "_".join(parts[3:]).replace("_", " ")
            sess.stage = "episodes"
            sess.page = 0
            sess.multi = False
            sess.selected.clear()
            await cb.answer()
            await sess.render()
            return

        if action == "backq":
            sess.stage = "quality"
            await cb.answer()
            await sess.render()
            return

        if action == "toggle":
            sess.multi = not sess.multi
            if not sess.multi:
                sess.selected.clear()
            await cb.answer("Multi-select " + ("ON" if sess.multi else "OFF"))
            await sess.render()
            return

        if action == "pg":
            try:
                new_page = int(parts[3])
            except (IndexError, ValueError):
                await cb.answer()
                return
            sess.page = max(0, min(new_page, sess.total_pages - 1))
            await cb.answer()
            await sess.render()
            return

        if action == "ep":
            try:
                idx = int(parts[3])
            except (IndexError, ValueError):
                await cb.answer()
                return

            if sess.multi:
                if idx in sess.selected:
                    sess.selected.discard(idx)
                else:
                    sess.selected.add(idx)
                await cb.answer()
                await sess.render()
                return

            await cb.answer("⬇️ Download shuru ho raha hai...")
            try:
                await cb.message.delete()
            except Exception:
                pass
            status_msg = await sess.orig_message.reply("⏳ Shuru ho raha hai...")
            await _download_adh_items(client, status_msg, sess.orig_message, sess, [idx])
            ADH_SESSIONS.pop(sid, None)
            return

        if action == "dl":
            if not sess.selected:
                await cb.answer("❌ Pehle kuch episodes select karo!", show_alert=True)
                return
            idxs = sorted(sess.selected)
            await cb.answer(f"⬇️ {len(idxs)} episodes download shuru ho rahe hain...")
            try:
                await cb.message.delete()
            except Exception:
                pass
            status_msg = await sess.orig_message.reply("⏳ Shuru ho raha hai...")
            await _download_adh_items(client, status_msg, sess.orig_message, sess, idxs)
            ADH_SESSIONS.pop(sid, None)
            return

        await cb.answer()
    except Exception as e:
        LOGGER.error(f"[Adh] callback error: {e}")
        try:
            await cb.answer("❌ Error, dubara try karo.", show_alert=True)
        except Exception:
            pass


# ─────────────────────────────────────────────
#  /adh Command Handler
# ─────────────────────────────────────────────
@Client.on_message(filters.command("adh"))
async def adh_command(client: Client, message: Message):
    """
    /adh <series_page_url>  -> Language menu -> Quality menu -> Episode menu
    """
    c = await check_chat(message, chat="Sudo")
    if not c:
        return

    parts = message.text.split()
    if len(parts) < 2:
        await message.reply(
            "**Usage:**\n"
            "`/adh <series_page_url>` — Language/Quality/Episode button menu khulega\n\n"
            "**Example:**\n"
            "`/adh https://www.animedubhindi.link/mushoku-tensei-jobless-reincarnation-season-3-hindi-multi-audio/`"
        )
        return

    series_url = parts[1].strip()
    if not series_url.startswith("http"):
        await message.reply("❌ Valid URL dalo.")
        return

    status_msg = await message.reply("🔍 Series page scan ho raha hai...")

    loop = asyncio.get_event_loop()
    try:
        series_data = await loop.run_in_executor(None, discover_adh_series, series_url)
    except Exception as e:
        LOGGER.error(f"[Adh] discover_adh_series error: {e}")
        await status_msg.edit(
            f"❌ Series page load nahi hua (3 attempts ke baad bhi): `{str(e)[:100]}`\n\n"
            f"Site slow ho sakti hai ya bot traffic block kar rahi ho — thodi der baad "
            f"dubara try karo."
        )
        return

    if not series_data.get("episode_list_url"):
        debug_text = "\n".join(series_data.get("debug", [])[-10:]) or "(koi debug info nahi mili)"
        await status_msg.edit(
            "❌ Is page pe 'Download / Watch' button ka link nahi mila.\n\n"
            f"**Debug (last steps):**\n```\n{debug_text}\n```"
        )
        return

    try:
        await status_msg.edit("🔍 Episode list scan ho raha hai...")
    except Exception:
        pass

    try:
        episodes = await loop.run_in_executor(
            None, discover_adh_episodes, series_data["episode_list_url"], series_data.get("episode_list_html")
        )
    except Exception as e:
        LOGGER.error(f"[Adh] discover_adh_episodes error: {e}")
        await status_msg.edit(
            f"❌ Episode list load nahi hua (3 attempts ke baad bhi): `{str(e)[:100]}`\n\n"
            f"Site slow ho sakti hai ya bot traffic block kar rahi ho — thodi der baad "
            f"dubara try karo."
        )
        return

    if not episodes:
        await status_msg.edit("❌ Episode-list page se koi episode nahi mila (480p/720p/1080p x265 mein se koi bhi).")
        return

    _prune_adh_sessions()
    sess = AdhSelector(
        series_url, series_data["title"], series_data["languages"], episodes, orig_message=message,
    )
    sess.sid = uuid.uuid4().hex[:10]
    ADH_SESSIONS[sess.sid] = sess

    try:
        await status_msg.delete()
    except Exception:
        pass

    sess.msg = await message.reply(sess.header_text(), reply_markup=sess.build_markup())
