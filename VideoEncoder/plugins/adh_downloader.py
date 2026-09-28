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
import os
import re
import time
import uuid
import base64
from urllib.parse import parse_qsl, unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from pyrogram import Client, filters
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from .. import LOGGER, download_dir
from ..utils.helper import check_chat
from .rti_downloader import _kb, SELENIUM_OK, _kill_driver_tree
from .haz_downloader import _apply_language_filter
from .url_upload import _get_filename_from_url, _download_url, _do_upload

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service as ChromeService
    from selenium.webdriver.common.by import By
except ImportError:  # selenium na ho to sirf static path chalega
    webdriver = None
    ChromeService = None
    By = None

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
    # NOTE: "br" jaanbujh kar hataya — brotli package installed nahi hai, to
    # Cloudflare-fronted pages (new.adhlinks.com) brotli mein compressed aate
    # the aur requests unhe decode nahi kar paata tha (56k chars ka garbage
    # text -> na "Episode" mila na "GDF").
    "Accept-Encoding": "gzip, deflate",
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

# ─────────────────────────────────────────────
#  Ad/interstitial-wall detection
#  --------------------------------------------
#  Root-cause (Subhankar ne confirm kiya): is site ke har page pe ek
#  "1-click ad" laga hai — real browser mein pehla click ek ad-tab/redirect
#  khol deta hai, aur sahi target sirf "wapas aakar dubara click karne" pe
#  milta hai. requests/BS4 JS execute nahi karte, isliye humein woh popup
#  nahi dikhta — lekin kai baar server khud bhi pehli hit pe ek ad-network
#  ka interstitial HTML bhej deta hai (cookie/session set karne ke baad hi
#  asli page deta hai). Isliye: pehli response ko "adwall jaisa lagta hai
#  kya" check karo, agar haan to seedha wahi URL dubara fetch karo (jaisa
#  "back karke fir click" karna hota) — session cookies persist rehti hain
#  isliye dusri hit real page de sakti hai.
# ─────────────────────────────────────────────
_ADWALL_MARKERS = [
    "propellerads", "adsterra", "popads", "exoclick", "juicyads",
    "highperformanceformat", "onclickmega", "adcash", "clickadu",
    "revenuehits", "monetag", "galaksion", "verify you are human",
    "checking your browser", "please wait while we redirect",
    "click here to continue", "continue to your link", "get link",
    "unlock link", "redirecting you", "you will be redirected",
    "skip ad", "ad will close",
]


def _looks_garbled(text: str) -> bool:
    """Undecoded compressed/binary body pakadne ke liye (replacement chars / control chars zyada)."""
    if not text:
        return False
    sample = text[:3000]
    bad = sum(1 for ch in sample if ch == "\ufffd" or (ord(ch) < 32 and ch not in "\r\n\t"))
    return bad / max(len(sample), 1) > 0.05


def _looks_like_adwall(html: str, expected_markers: list | None = None) -> str | None:
    """
    Agar page ek ad/interstitial jaisa lagta hai to reason-string return
    karta hai, warna None. Do signals check karte hain:
      1) Known ad-network script/keyword directly HTML mein mila.
      2) `expected_markers` diye gaye (jaise "Download Here", "INSTANT DL")
         aur unmein se koi bhi text mein nahi mila, jabki page chhota/khaali
         jaisa hai — yeh bhi wrong/ad page pe hone ka signal ho sakta hai.
    """
    low = (html or "").lower()
    for marker in _ADWALL_MARKERS:
        if marker in low:
            return f"ad-network marker '{marker}' mila"
    if _looks_garbled(html):
        return "response garbled/binary jaisa hai (compression/encoding issue?)"
    if expected_markers:
        if not any(em.lower() in low for em in expected_markers):
            return f"expected content ({', '.join(expected_markers)}) page mein nahi mila ({len(html or '')} chars)"
    return None


# animedubhindi.link jaisi shared-hosting/Cloudflare site kabhi-kabhi pehli
# request pe 20s se zyada le leti hai (cold start / anti-bot delay) —
# isliye lamba timeout + retry-with-backoff, taaki genuine slow response
# aur "site hi down hai" mein farak pata chale.
def _http_get(
    url: str,
    timeout: int = 35,
    retries: int = 2,
    debug: list | None = None,
    step: str = "",
    expected_markers: list | None = None,
    ad_retries: int = 2,
    **kwargs,
):
    """
    step: is call ka human-readable naam (jaise "GDF page", "Instant-DL
    wait-page") — sirf logging/debug-trail ke liye, taaki user ko exactly
    pata chale ki kaun se page pe/kaun se link pe bot atka.
    expected_markers + ad_retries: fetch karne ke baad agar response
    ad-interstitial jaisa lage to (jaisa real browser mein "back + click
    again" karna padta hai) wahi URL dubara fetch karte hain, max
    `ad_retries` baar.
    """
    tag = f"[{step}] " if step else ""

    def log(msg):
        if debug is not None:
            debug.append(f"{tag}{msg}")
        LOGGER.info(f"[Adh] {tag}{msg}")

    log(f"🌐 GET {url}")

    last_exc = None
    r = None
    for attempt in range(1, retries + 2):  # total attempts = retries + 1
        try:
            r = _SESSION.get(url, timeout=timeout, **kwargs)
            break
        except requests.exceptions.ReadTimeout as e:
            last_exc = e
            log(f"⚠️ Attempt {attempt}: read timed out ({timeout}s)")
        except requests.exceptions.RequestException as e:
            last_exc = e
            log(f"⚠️ Attempt {attempt}: {str(e).splitlines()[0][:120]}")
        if attempt < retries + 1:
            time.sleep(2 * attempt)
    if r is None:
        log(f"❌ {retries + 1} attempts ke baad bhi fetch fail: {str(last_exc).splitlines()[0][:120] if last_exc else 'unknown error'}")
        raise last_exc

    if r.history:
        chain = " → ".join([str(h.status_code) for h in r.history] + [str(r.status_code)])
        log(f"↪️ Redirect chain ({chain}) final URL: {r.url}")
    else:
        log(f"✅ HTTP {r.status_code}, {len(r.text)} chars, final URL: {r.url}")

    reason = _looks_like_adwall(r.text, expected_markers)
    if reason and ad_retries <= 0:
        log(f"🔎 Page expected jaisa nahi laga ({reason}); Content-Encoding={r.headers.get('Content-Encoding', '-')}")
    elif reason:
        enc = r.headers.get("Content-Encoding", "-")
        snippet = re.sub(r"\s+", " ", r.text[:160])
        log(f"🔎 Content-Encoding={enc}, snippet: {snippet!r}")
        log(f"🛑 Ad/interstitial page jaisa lag raha hai ({reason}) — 'back + dubara click' simulate kar rahe hain")
        for ad_attempt in range(1, ad_retries + 1):
            time.sleep(1.5)
            try:
                r2 = _SESSION.get(url, timeout=timeout, **kwargs)
            except requests.exceptions.RequestException as e:
                log(f"⚠️ Ad-retry {ad_attempt}: fetch fail — {str(e).splitlines()[0][:120]}")
                continue
            reason2 = _looks_like_adwall(r2.text, expected_markers)
            if not reason2:
                log(f"✅ Ad-retry {ad_attempt}: is baar clean page mila (final URL: {r2.url})")
                r = r2
                reason = None
                break
            log(f"🛑 Ad-retry {ad_attempt}: abhi bhi ad-page jaisa lag raha hai ({reason2})")
            r = r2
        if reason:
            log(f"❌ {ad_retries} ad-retries ke baad bhi ad-page/wrong-page se nahi nikal paaye — parsing aage try karenge lekin fail ho sakta hai")

    return r

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
    r = _http_get(series_url, debug=debug, step="Series page", expected_markers=["Audio Tracks", "Download", "Watch"])
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
            r2 = _http_get(
                candidate, debug=debug, step="Derived episode-list URL",
                expected_markers=["Episode", "GDF"],
            )
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
def discover_adh_episodes(episode_list_url: str, html: str | None = None) -> dict:
    """
    Returns: {"episodes": [{"num": int, "qualities": {q: gdf_href}, "fprs": {q: fprs_href}, "multi": {q: multi_href}}, ...], "debug": list}
    `html` diya ho (discover_adh_series ke Strategy-A validation se already
    fetch ho chuka) to dobara request nahi bhejte — sirf usse parse karte hain.
    """
    debug: list = []
    if html is None:
        r = _http_get(
            episode_list_url, debug=debug, step="Episode-list page",
            expected_markers=["Episode", "GDF"],
        )
        r.raise_for_status()
        html = r.text
    soup = BeautifulSoup(html, "html.parser")

    episodes = {}

    def _collect(label_re: str, key: str):
        for a in soup.find_all("a", string=re.compile(label_re, re.IGNORECASE)):
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

            ep = episodes.setdefault(ep_num, {"num": ep_num, "qualities": {}, "fprs": {}, "multi": {}})
            ep[key][quality] = urljoin(episode_list_url, href.strip())

    # GDF (purana, ab sirf optional fallback) + Fprs (ab primary)
    _collect(r'^\s*GDF\s*$', "qualities")
    _collect(r'^\s*Fprs\s*$', "fprs")
    _collect(r'^\s*Multi\s*$', "multi")

    if not episodes:
        debug.append(
            f"❌ Episode-list page pe koi bhi GDF link nahi mila (page length: {len(html)} chars) — "
            f"ho sakta hai yeh ek ad-page ho, ya site ka HTML structure badal gaya ho"
        )
    else:
        n_m = sum(1 for e in episodes.values() if e["multi"])
        debug.append(f"✅ {len(episodes)} episode(s) mile ({n_m} mein Multi link)")

    return {"episodes": sorted(episodes.values(), key=lambda x: x["num"]), "debug": debug}


# ─────────────────────────────────────────────
#  Step 3: GDF page -> GDFlix "INSTANT DL" -> fastdl-one "Download Here"
#  Poori tarah requests+BS4 (Selenium/cookie ki zaroorat nahi — yeh real
#  captcha nahi, sirf client-side wait/ad page hai jiska asli link static
#  HTML mein hi maujood hota hai).
# ─────────────────────────────────────────────
def _safe_debug(lines: list) -> str:
    """
    Debug lines ko Telegram message ke liye safe banao. Pyrogram default
    parse-mode HTML+Markdown dono samajhta hai, isliye page-snippet mein
    '<title>' jaise tags ya backticks aa jaayein to poora message
    mangle/truncate ho jaata tha (aakhri lines gayab). Yahan unhe neutralise
    karte hain.
    """
    text = "\n".join(lines)
    return (text.replace("`", "'").replace("<", "\u2039").replace(">", "\u203a"))[:2800]


def _is_cf_challenge(status: int, text: str, headers=None) -> bool:
    """Cloudflare 'Just a moment...' / managed-challenge page pehchano."""
    low = (text or "")[:4000].lower()
    if headers is not None and str(headers.get("cf-mitigated", "")).lower() == "challenge":
        return True
    return status in (403, 429, 503) and (
        "just a moment" in low or "challenge-platform" in low
        or "cf-chl" in low or "attention required" in low
    )


def _fetch_page(url: str, debug: list, step: str, expected_markers: list, timeout: int = 30):
    """
    Page fetch with Cloudflare fallback chain. Returns (html, final_url) ya None.
      1) plain requests session (ad-wall retry ke saath)
      2) Cloudflare 'Just a moment' mila to -> cloudscraper
      3) phir bhi block to -> Bright Data Web Unlocker (haz_downloader wala
         hi _BDSession; BRIGHTDATA_API_KEY + BRIGHTDATA_ZONE env)
    """
    tag = f"[{step}] "

    def log(msg):
        debug.append(f"{tag}{msg}")
        LOGGER.info(f"[Adh] {tag}{msg}")

    try:
        r = _http_get(url, timeout=timeout, debug=debug, step=step, allow_redirects=True,
                      expected_markers=expected_markers, ad_retries=0)
    except Exception as e:
        log(f"❌ fetch fail (after retries): {str(e).splitlines()[0][:120]}")
        return None

    if not _is_cf_challenge(r.status_code, r.text, r.headers):
        if _looks_like_adwall(r.text, expected_markers):
            # ad-page jaisa laga — 'back + dubara click' simulate
            time.sleep(1.5)
            try:
                r = _http_get(r.url, timeout=timeout, debug=debug, step=step + " (retry)",
                              allow_redirects=True, expected_markers=expected_markers, ad_retries=0)
            except Exception as e:
                log(f"❌ retry fetch fail: {str(e).splitlines()[0][:120]}")
        return r.text, r.url

    target = r.url
    log(f"🛡️ Cloudflare challenge (HTTP {r.status_code}) — {target} — bypass fallbacks try kar rahe hain")

    # ── Fallback 1: cloudscraper ──
    try:
        import cloudscraper
        sc = cloudscraper.create_scraper(browser={"browser": "chrome", "platform": "windows", "desktop": True})
        r2 = sc.get(target, timeout=timeout)
        if not _is_cf_challenge(r2.status_code, r2.text, r2.headers) and r2.status_code == 200:
            log(f"✅ cloudscraper se pass ho gaya ({len(r2.text)} chars)")
            return r2.text, r2.url
        log(f"⚠️ cloudscraper bhi block (HTTP {r2.status_code})")
    except Exception as e:
        log(f"⚠️ cloudscraper error: {str(e).splitlines()[0][:100]}")

    # ── Fallback 2: Bright Data Web Unlocker ──
    try:
        from .haz_downloader import _BDSession, _bd_cfg
        cfg = _bd_cfg()
        if not cfg["key"] or not cfg["zone"]:
            log("❌ BRIGHTDATA_API_KEY / BRIGHTDATA_ZONE set nahi hain — Cloudflare bypass ke liye "
                "ye env vars chahiye (jo /haz ke liye use hote hain)")
            return None
        r3 = _BDSession(log).get(target, timeout=120)
        if _is_cf_challenge(r3.status_code, r3.text):
            log("❌ Bright Data se bhi challenge page hi mila")
            return None
        log(f"✅ Bright Data Web Unlocker se page mila ({len(r3.text)} chars)")
        return r3.text, target
    except Exception as e:
        log(f"❌ Bright Data error: {str(e).splitlines()[0][:120]}")
        return None


def _describe_links(soup: BeautifulSoup, limit: int = 8) -> str:
    """Page pe jo buttons/links dikhe unka short summary (debug ke liye)."""
    items = []
    for a in soup.find_all("a", href=True):
        label = a.get_text(" ", strip=True)[:25]
        if label:
            items.append(f"{label}→{urlparse(a['href']).netloc or a['href'][:20]}")
        if len(items) >= limit:
            break
    return " | ".join(items) or "(koi link nahi)"


def _link_from_query(page_url: str) -> str | None:
    """
    fastdl-one.pages.dev/?url=<...> jaise wait-page ka asli download link
    aksar URL ke query-param mein hi hota hai (page ka "Download Here"
    button JS se baad mein banta hai, isliye static HTML mein nahi milta).
    Value plain/percent-encoded http link ho ya base64 — dono try karte hain.
    """
    for key, val in parse_qsl(urlparse(page_url).query, keep_blank_values=False):
        if key.lower() not in ("url", "link", "file", "download", "dl", "u", "d", "data", "id", "src"):
            continue
        cand = unquote(val).strip()
        if cand.lower().startswith("http"):
            return cand
        try:
            b = cand.replace(" ", "+").replace("-", "+").replace("_", "/")
            dec = base64.b64decode(b + "=" * (-len(b) % 4)).decode("utf-8", "ignore").strip()
            if dec.lower().startswith("http"):
                return dec
        except Exception:
            continue
    return None


def _script_hints(html: str, limit: int = 6) -> str:
    """Wait-page ki <script> mein dikhe http URLs / fetch / atob (debug ke liye)."""
    hints = []
    for m in re.finditer(r'(fetch\(|axios\.|XMLHttpRequest|atob\(|location\.href|window\.open)', html):
        hints.append(m.group(1))
    urls = []
    for m in re.finditer(r'https?://[^\s"\'<>\\)]+', html):
        u = m.group(0)
        if u not in urls:
            urls.append(u)
    return f"js={sorted(set(hints)) or '-'} urls={[u[:80] for u in urls[:limit]] or '-'}"


def _resolve_adh_gdf_link(gdf_url: str, debug: list) -> str | None:
    def log(msg):
        debug.append(msg)
        LOGGER.info(f"[Adh] {msg}")

    got = _fetch_page(gdf_url, debug, "GDF page", ["INSTANT DL"])
    if not got:
        log(f"❌ GDF page ({gdf_url}) load nahi ho paaya")
        return None
    gdf_html, gdf_final = got

    instant_href = None
    soup = BeautifulSoup(gdf_html, "html.parser")
    for a in soup.find_all("a", href=True):
        label = a.get_text(" ", strip=True)
        if "instant dl" in label.lower():
            instant_href = a["href"]
            break
    if not instant_href:
        m = re.search(
            r'href=["\'](https?://[^"\']+)["\'][^>]*>\s*(?:<[^>]+>\s*)*.*?INSTANT\s*DL',
            gdf_html, re.IGNORECASE | re.DOTALL,
        )
        if m:
            instant_href = m.group(1)

    if not instant_href:
        log(f"❌ GDF page ({gdf_url}) pe 'INSTANT DL' button nahi mila")
        log(f"🔗 Page pe mile links: {_describe_links(soup)}")
        return None

    instant_href = urljoin(gdf_final, instant_href)
    log(f"✅ INSTANT DL link mila: {instant_href[:100]}")

    got2 = _fetch_page(instant_href, debug, "Instant-DL wait-page", None)
    if not got2:
        log(f"❌ Instant-DL wait-page ({instant_href}) load nahi ho paaya")
        return None

    text2 = got2[0]
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

    # Strategy 2b: wait-page URL ke query-param (?url=...) mein hi link hota hai —
    # page pe "Please wait" spinner ke baad JS "Download Here" button banata hai
    if not final_url:
        q_link = _link_from_query(instant_href)
        if q_link:
            final_url = q_link
            log(f"✅ Wait-page URL ke ?url= param se link mila: {q_link[:100]}")

    # Strategy 3: base64-encoded link (kai "wait" pages atob() se link banate hain)
    if not final_url:
        for m in re.finditer(r'atob\(["\']([A-Za-z0-9+/=]+)["\']\)', text2):
            try:
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
        log(f"🔗 Wait-page pe mile links: {_describe_links(soup2)}")
        log(f"🧩 Wait-page URL query keys: {[k for k, _ in parse_qsl(urlparse(instant_href).query)] or '-'} | {_script_hints(text2)}")
        log(f"❌ Wait-page ({instant_href}) pe 'Download Here' final link nahi mila — "
            f"shayad abhi bhi ad-page pe hain ya button ka HTML badal gaya hai")
        return None

    log(f"✅ Final direct link mila: {final_url[:100]}")
    return final_url


# ─────────────────────────────────────────────
#  Fprs (FilePress) flow — ab primary:
#    Fprs link -> new1.filepress.lat/file/... page
#      -> "INSTANT DOWNLOAD" click (3-4 baar ad-redirect, back karna padta hai)
#      -> new2.dotflix.shop/sha... page ("Ready for download!")
#      -> "Direct Download" click (1-2 baar ad-redirect, back)
#      -> final direct link
#
#  Do raaste:
#    1) Static (requests+BS4): agar buttons plain <a href> hain to seedha
#       href follow — sabse tez, browser ki zaroorat nahi.
#    2) Selenium fallback: buttons JS se bante hain / href nahi hai to real
#       click, ad-tab band, ad-redirect pe back — bilkul waise jaisa insaan
#       karta hai. (Chromium + selenium already deployed hai — Toono/RTI wala.)
# ─────────────────────────────────────────────
_SITE_HINTS = ("filepress", "dotflix", "adhlinks")
_MEDIA_EXT_RE = re.compile(r'\.(mkv|mp4|avi|webm|mov)(\?|#|$)', re.IGNORECASE)


def _gdf_fallback_on() -> bool:
    """GDF ko sirf tab try karo jab env ADH_GDF_FALLBACK=1 ho (default: band)."""
    return (os.getenv("ADH_GDF_FALLBACK") or "").strip().lower() in ("1", "true", "yes", "on")


def _real_href(h: str | None) -> bool:
    h = (h or "").strip()
    return bool(h) and h != "#" and not h.lower().startswith(("javascript:", "about:", "data:", "mailto:"))


def _probe_file_url(url: str, log) -> bool:
    """
    URL ek asli file hai ya ad/html page? Extension (.mkv/.mp4) ho to seedha haan;
    warna headers dekhte hain (Content-Disposition: attachment ya non-html Content-Type).
    """
    if _MEDIA_EXT_RE.search(urlparse(url).path or "") or _MEDIA_EXT_RE.search(url):
        return True
    try:
        r = _SESSION.get(url, stream=True, timeout=20, allow_redirects=True)
        ct = (r.headers.get("Content-Type") or "").lower()
        cd = (r.headers.get("Content-Disposition") or "").lower()
        code = r.status_code
        r.close()
        ok = code < 400 and ("attachment" in cd or (ct and "html" not in ct and not ct.startswith("text/")))
        log(f"🔬 Probe {url[:70]} → HTTP {code}, type={ct or '-'}, file={'haan' if ok else 'nahi'}")
        return ok
    except Exception as e:
        log(f"⚠️ Probe fail ({str(e).splitlines()[0][:80]}) — is URL ko file nahi maan rahe")
        return False


def _dotflix_final_from_html(html: str, page_url: str, log) -> str | None:
    """Dotflix page ke static HTML se 'Direct Download' ka href / fallbacks."""
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        if "direct download" in a.get_text(" ", strip=True).lower() and _real_href(a["href"]):
            return urljoin(page_url, a["href"].strip())

    m = re.search(
        r'href=["\'](https?://[^"\']+)["\'][^>]*>\s*(?:<[^>]+>\s*)*.*?Direct\s*Download',
        html, re.IGNORECASE | re.DOTALL,
    )
    if m:
        return m.group(1)

    q = _link_from_query(page_url)
    if q:
        log("✅ Dotflix URL ke query-param se link mila")
        return q

    for m in re.finditer(r'atob\(["\']([A-Za-z0-9+/=]+)["\']\)', html):
        try:
            dec = base64.b64decode(m.group(1)).decode("utf-8", "ignore").strip()
            if dec.startswith("http"):
                log("✅ Dotflix page pe base64 link decode hua")
                return dec
        except Exception:
            continue

    m = re.search(r'(https?://[^\s"\'<>\\]+\.(?:mkv|mp4))', html, re.IGNORECASE)
    if m:
        log("✅ Dotflix HTML mein raw .mkv/.mp4 link mila")
        return m.group(1)
    return None


# ── Path 1: static ──
def _fprs_static_resolve(fprs_url: str, debug: list) -> str | None:
    def log(msg):
        debug.append(msg)
        LOGGER.info(f"[Adh] {msg}")

    got = _fetch_page(fprs_url, debug, "Fprs page", None)
    if not got:
        log("⚠️ Fprs page static fetch nahi hua")
        return None
    html, final = got
    soup = BeautifulSoup(html, "html.parser")

    instant = None
    for a in soup.find_all("a", href=True):
        if "instant download" in a.get_text(" ", strip=True).lower() and _real_href(a["href"]):
            instant = urljoin(final, a["href"].strip())
            break
    if not instant:
        m = re.search(r'https?://[^\s"\'<>\\]*dotflix[^\s"\'<>\\]*', html, re.IGNORECASE)
        if m:
            instant = m.group(0)
    if not instant:
        log(f"ℹ️ Static HTML mein INSTANT DOWNLOAD ka href nahi (JS button hoga) — final URL: {final[:80]}")
        log(f"🔗 Page links: {_describe_links(soup)}")
        return None
    log(f"✅ INSTANT DOWNLOAD href mila: {instant[:100]}")

    # href kabhi seedha dotflix hota hai, kabhi ek redirector — _fetch_page redirects follow karta hai
    got2 = _fetch_page(instant, debug, "Dotflix page", None)
    if not got2:
        return None
    html2, final2 = got2
    link = _dotflix_final_from_html(html2, final2, log)
    if not link:
        log(f"ℹ️ Dotflix static HTML mein Direct Download href nahi — final URL: {final2[:80]}")
        return None
    if _probe_file_url(link, log):
        log(f"✅ Static path se final link mila: {link[:100]}")
        return link
    log("⚠️ Static path ka link file jaisa nahi laga (ad/html) — Selenium try karenge")
    return None


# ── Path 2: Selenium (real clicks + ad handling) ──
def _make_adh_driver():
    """
    Stealth headless Chromium: navigator.webdriver hide, "HeadlessChrome" UA
    se "Headless" hata, performance-log ON (network debug ke liye). FilePress jaisi
    SPA headless dekhkar spinner pe hi atka deti hai — isliye yeh sab zaroori hai.
    Fail ho to Toono wale driver pe fallback.
    """
    try:
        profile_dir = os.path.join(download_dir, "_chrome_tmp", f"adh_{uuid.uuid4().hex}")
        os.makedirs(profile_dir, exist_ok=True)

        options = webdriver.ChromeOptions()
        for arg in (
            "--headless=new", "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
            "--disable-extensions", "--mute-audio", "--no-first-run", "--disable-crash-reporter",
            "--window-size=1366,900", "--lang=en-US",
            "--disable-blink-features=AutomationControlled",
            f"--user-data-dir={profile_dir}",
        ):
            options.add_argument(arg)
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)
        options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
        dl_dir = os.path.join(profile_dir, "dl")
        os.makedirs(dl_dir, exist_ok=True)
        options.add_experimental_option("prefs", {
            "download.default_directory": dl_dir,
            "download.prompt_for_download": False,
        })
        options.page_load_strategy = "none"   # SPA: DOMContentLoaded ka wait nahi, hum khud poll karte hain

        for binary in ("/usr/bin/chromium", "/usr/bin/chromium-browser",
                       "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable"):
            if os.path.exists(binary):
                options.binary_location = binary
                break

        service = None
        for cd in (os.getenv("CHROMEDRIVER_PATH", ""), "/usr/bin/chromedriver",
                   "/usr/lib/chromium/chromedriver", "/usr/lib/chromium-browser/chromedriver"):
            if cd and os.path.exists(cd):
                service = ChromeService(executable_path=cd)
                break

        driver = webdriver.Chrome(service=service, options=options) if service \
            else webdriver.Chrome(options=options)
        driver.set_page_load_timeout(45)
        driver._suhani_profile_dir = profile_dir
        try:
            ua = driver.execute_script("return navigator.userAgent") or ""
            driver.execute_cdp_cmd("Network.setUserAgentOverride", {
                "userAgent": ua.replace("HeadlessChrome", "Chrome"),
                "acceptLanguage": "en-US,en;q=0.9",
            })
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": (
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
                "window.chrome=window.chrome||{runtime:{}};"
                "Object.defineProperty(navigator,'languages',{get:()=>['en-US','en']});"
                "Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});"
            )})
        except Exception as e:
            LOGGER.warning(f"[Adh] stealth CDP setup fail: {e}")
        return driver
    except Exception as e:
        LOGGER.warning(f"[Adh] stealth driver fail ({e}), toono driver pe fallback")
        from .toono_downloader import _make_toono_driver
        return _make_toono_driver()


def _sel_diag(driver, debug: list, tag: str, shot: bool = True):
    """Page kis haal mein hai: title, body text, XHR/fetch calls + screenshot (Telegram pe jayega)."""
    def log(msg):
        debug.append(msg)
        LOGGER.info(f"[Adh] {msg}")
    try:
        state = driver.execute_script("return document.readyState")
        title = (driver.title or "")[:50]
        body = driver.execute_script("return (document.body&&document.body.innerText||'').slice(0,160)") or ""
        body = re.sub(r"\s+", " ", body)
        n_btn = driver.execute_script("return document.querySelectorAll('a,button').length")
        log(f"🩺 [{tag}] ready={state} title={title!r} a/button={n_btn} body={body!r}")
    except Exception as e:
        log(f"🩺 [{tag}] diag fail: {str(e).splitlines()[0][:80]}")
    try:
        import json as _json
        seen = []
        for ent in driver.get_log("performance"):
            try:
                m = _json.loads(ent["message"])["message"]
            except Exception:
                continue
            prm = m.get("params", {})
            if m.get("method") == "Network.responseReceived" and prm.get("type") in ("XHR", "Fetch"):
                rs = prm.get("response", {})
                seen.append(f"{rs.get('status')} {rs.get('url', '')[:75]}")
            elif m.get("method") == "Network.loadingFailed":
                seen.append(f"FAIL {prm.get('errorText', '')} {prm.get('type', '')}")
        if seen:
            log("🌐 XHR: " + " | ".join(seen[-6:]))
        else:
            log("🌐 XHR/fetch calls: koi nahi dikhi")
    except Exception:
        pass
    if shot and not any(str(x).startswith("__SHOT__:") for x in debug):
        try:
            path = os.path.join(download_dir, f"adh_dbg_{uuid.uuid4().hex[:8]}.png")
            if driver.save_screenshot(path):
                debug.append(f"__SHOT__:{path}")
        except Exception:
            pass


def _xpath_ci(phrase: str) -> str:
    up = "translate(normalize-space(.),'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ')"
    P = phrase.upper()
    return (
        f"//a[contains({up},'{P}')] | //button[contains({up},'{P}')] | "
        f"//*[@role='button'][contains({up},'{P}')]"
    )


def _sel_find(driver, phrase: str):
    try:
        for el in driver.find_elements(By.XPATH, _xpath_ci(phrase)):
            try:
                if el.is_displayed():
                    return el
            except Exception:
                continue
    except Exception:
        pass
    # fallback: koi bhi element (div/span/etc.) jiska apna text phrase ho (deepest wala)
    try:
        up = "translate(normalize-space(.),'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ')"
        P = phrase.upper()
        xp = (f"//*[not(self::script or self::style or self::head or self::title)]"
              f"[contains({up},'{P}')][not(*[contains({up},'{P}')])]")
        for el in driver.find_elements(By.XPATH, xp):
            try:
                if el.is_displayed():
                    return el
            except Exception:
                continue
    except Exception:
        pass
    return None


def _sel_click(driver, el) -> bool:
    try:
        try:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
        except Exception:
            pass
        try:
            driver.execute_script("arguments[0].click();", el)
        except Exception:
            el.click()
        return True
    except Exception:
        return False


def _sel_sweep(driver, main, hint: str):
    """
    Saare tabs dekho: `hint` wala tab mila to uska handle return. Baaki non-blank,
    non-site tabs (ads) band. about:blank tabs ko chhod do (JS baad mein redirect
    kar sakta hai). Ant mein main tab pe wapas.
    """
    try:
        handles = list(driver.window_handles)
    except Exception:
        return None
    for h in handles:
        try:
            driver.switch_to.window(h)
            cur = (driver.current_url or "").lower()
        except Exception:
            continue
        if hint in cur:
            return h
        if h == main:
            continue
        if cur in ("about:blank", "", "data:,"):
            continue
        if not any(x in cur for x in _SITE_HINTS):
            try:
                driver.close()
            except Exception:
                pass
    try:
        hs = driver.window_handles
        driver.switch_to.window(main if main in hs else hs[0])
    except Exception:
        pass
    return None


def _sel_recover_from_ad(driver, back_url: str, log):
    """Main tab kisi ad-site pe chala gaya ho to back karo (ya page dobara kholo)."""
    try:
        cur = (driver.current_url or "").lower()
    except Exception:
        return
    if cur in ("about:blank", "", "data:,") or any(x in cur for x in _SITE_HINTS):
        return
    log(f"↩️ Ad-redirect mila ({urlparse(cur).netloc[:40]}) — back kar rahe hain")
    try:
        driver.back()
        time.sleep(1.5)
        cur2 = (driver.current_url or "").lower()
        if not any(x in cur2 for x in _SITE_HINTS):
            driver.get(back_url)
    except Exception:
        try:
            driver.get(back_url)
        except Exception:
            pass


def _sel_wait_find(driver, phrase: str, timeout: float):
    end = time.time() + timeout
    while time.time() < end:
        el = _sel_find(driver, phrase)
        if el is not None:
            return el
        time.sleep(0.7)
    return None


def _fprs_selenium_resolve(fprs_url: str, debug: list) -> str | None:
    def log(msg):
        debug.append(msg)
        LOGGER.info(f"[Adh] {msg}")

    if not SELENIUM_OK or By is None:
        log("❌ Selenium install nahi hai — Fprs click-flow nahi chal sakta")
        return None

    driver = None
    try:
        log("🌐 [Fprs] Selenium browser khol rahe hain")
        driver = _make_adh_driver()
        try:
            driver.get(fprs_url)
        except Exception as e:
            log(f"⚠️ [Fprs] get() timeout/err (aage badh rahe hain): {str(e).splitlines()[0][:60]}")
        main = driver.current_window_handle
        page_url = driver.current_url or fprs_url
        log(f"🌐 [Fprs] page: {page_url[:90]}")

        # ── Stage 1: INSTANT DOWNLOAD -> dotflix ──
        dot_handle = None
        for attempt in range(1, 6):
            el = _sel_wait_find(driver, "instant download", 40 if attempt == 1 else 30)
            if el is None:
                log(f"⚠️ [Fprs] attempt {attempt}: INSTANT DOWNLOAD button nahi dikha (url: {(driver.current_url or '')[:70]})")
                if attempt in (1, 3):
                    _sel_diag(driver, debug, "Fprs")
                _sel_recover_from_ad(driver, page_url, log)
                try:
                    if any(x in (driver.current_url or "").lower() for x in _SITE_HINTS):
                        # spinner pe atka ho sakta hai — page dobara load karo
                        driver.get(page_url) if attempt % 2 == 0 else driver.refresh()
                        log(f"🔄 [Fprs] page reload kiya (attempt {attempt})")
                except Exception:
                    pass
                continue

            href = ""
            try:
                href = el.get_attribute("href") or ""
            except Exception:
                pass
            if _real_href(href) and "dotflix" in href.lower():
                log("✅ [Fprs] INSTANT DOWNLOAD ke href mein seedha dotflix link mila")
                driver.get(href)
                dot_handle = driver.current_window_handle
                break

            if not _sel_click(driver, el):
                log(f"⚠️ [Fprs] attempt {attempt}: click fail")
                continue
            log(f"🖱️ [Fprs] attempt {attempt}: INSTANT DOWNLOAD click kiya")

            for _ in range(16):  # ~8 sec poll
                time.sleep(0.5)
                h = _sel_sweep(driver, main, "dotflix")
                if h:
                    dot_handle = h
                    break
            if dot_handle:
                break
            _sel_recover_from_ad(driver, page_url, log)

        if not dot_handle:
            log("❌ [Fprs] 5 attempts ke baad bhi dotflix page nahi mila")
            return None

        try:
            driver.switch_to.window(dot_handle)
        except Exception:
            pass
        main = dot_handle
        for h in list(driver.window_handles):
            if h != main:
                try:
                    driver.switch_to.window(h)
                    driver.close()
                except Exception:
                    pass
        driver.switch_to.window(main)
        dot_url = driver.current_url or ""
        log(f"✅ [Dotflix] page mila: {dot_url[:90]}")

        # ── Stage 2: Direct Download -> final link ──
        for attempt in range(1, 6):
            el = _sel_wait_find(driver, "direct download", 40 if attempt == 1 else 20)
            if el is None:
                # page-source se bhi try (button JS se ban ke chhupa ho sakta hai)
                link = _dotflix_final_from_html(driver.page_source or "", driver.current_url or dot_url, log)
                if link and _probe_file_url(link, log):
                    log(f"✅ [Dotflix] page source se final link: {link[:100]}")
                    return link
                log(f"⚠️ [Dotflix] attempt {attempt}: Direct Download button nahi dikha")
                if attempt in (1, 3):
                    _sel_diag(driver, debug, "Dotflix")
                _sel_recover_from_ad(driver, dot_url, log)
                continue

            href = ""
            try:
                href = el.get_attribute("href") or ""
            except Exception:
                pass
            if _real_href(href):
                full = urljoin(driver.current_url or dot_url, href.strip())
                if _probe_file_url(full, log):
                    log(f"✅ [Dotflix] Direct Download href se final link: {full[:100]}")
                    return full
                log(f"ℹ️ [Dotflix] href file jaisa nahi ({full[:70]}) — click karke dekhte hain")

            if not _sel_click(driver, el):
                log(f"⚠️ [Dotflix] attempt {attempt}: click fail")
                continue
            log(f"🖱️ [Dotflix] attempt {attempt}: Direct Download click kiya")

            for _ in range(16):
                time.sleep(0.5)
                # naye tabs: file-jaisa URL mila to wahi final, warna ad -> band
                try:
                    for h in list(driver.window_handles):
                        driver.switch_to.window(h)
                        cur = driver.current_url or ""
                        if cur in ("about:blank", "", "data:,"):
                            continue
                        if h != main or "dotflix" not in cur.lower():
                            if _real_href(cur) and _probe_file_url(cur, log):
                                log(f"✅ [Dotflix] click ke baad final link: {cur[:100]}")
                                return cur
                        if h != main:
                            try:
                                driver.close()
                            except Exception:
                                pass
                    driver.switch_to.window(main)
                except Exception:
                    try:
                        driver.switch_to.window(driver.window_handles[0])
                        main = driver.current_window_handle
                    except Exception:
                        pass

                # click ke baad button ka href JS se set ho gaya ho
                el2 = _sel_find(driver, "direct download")
                if el2 is not None:
                    try:
                        h2 = el2.get_attribute("href") or ""
                    except Exception:
                        h2 = ""
                    if _real_href(h2) and h2 != href:
                        full = urljoin(driver.current_url or dot_url, h2.strip())
                        if _probe_file_url(full, log):
                            log(f"✅ [Dotflix] click ke baad href badla, final link: {full[:100]}")
                            return full

            _sel_recover_from_ad(driver, dot_url, log)

        log("❌ [Dotflix] 5 attempts ke baad bhi final link nahi mila")
        return None
    except Exception as e:
        log(f"❌ [Fprs] Selenium error: {type(e).__name__}: {str(e).splitlines()[0][:120] if str(e) else ''}")
        return None
    finally:
        if driver is not None:
            try:
                _kill_driver_tree(driver)
            except Exception:
                pass


def _resolve_adh_fprs_link(fprs_url: str, debug: list) -> str | None:
    link = _fprs_static_resolve(fprs_url, debug)
    if link:
        return link
    debug.append("➡️ Static path se nahi mila — Selenium (real click) path shuru")
    return _fprs_selenium_resolve(fprs_url, debug)


def _resolve_adh_download_link(gdf_url: str | None, debug: list, fprs_url: str | None = None,
                               multi_url: str | None = None) -> str | None:
    """Multi primary. Fprs = env ADH_FPRS_FALLBACK=1, GDF = env ADH_GDF_FALLBACK=1 hone pe fallback."""
    _base = "https://new.adhlinks.com/"
    multi_url = urljoin(_base, multi_url) if multi_url else multi_url
    fprs_url = urljoin(_base, fprs_url) if fprs_url else fprs_url
    gdf_url = urljoin(_base, gdf_url) if gdf_url else gdf_url
    if multi_url:
        link = _multi_selenium_resolve(multi_url, debug)
        if link:
            return link
        debug.append("❌ Multi se final link nahi mila")
    if fprs_url and _env_on("ADH_FPRS_FALLBACK"):
        debug.append("➡️ ADH_FPRS_FALLBACK on — Fprs try kar rahe hain")
        link = _resolve_adh_fprs_link(fprs_url, debug)
        if link:
            return link
    if gdf_url and _gdf_fallback_on():
        debug.append("➡️ ADH_GDF_FALLBACK on — GDF try kar rahe hain")
        return _resolve_adh_gdf_link(gdf_url, debug)
    return None


# ─────────────────────────────────────────────
#  MULTI flow (ab primary):
#    Multi link (new.adhlinks.com/re.php?data=...)  -> "Redirecting in 0 seconds" page
#      -> FilesForever page ("Direct Links" mein "Cloud Download" button)
#      -> click -> cldst "Preparing Your File" page (transfer ho raha hota hai, 1-2+ min)
#      -> "Download File" button (transfer 100% ke baad) -> click -> file download
#  Poori tarah Selenium (JS-redirect + JS-progress wali pages hain). Ad-tabs band,
#  same-tab ad-redirect pe back. Final URL: button ke href se, ya click ke baad
#  browser ke network-log se (video/attachment response ka URL).
# ─────────────────────────────────────────────
_MULTI_KEEP = ("adhlinks", "filesforever", "iqsmartgames", "cldst")
_BLANK = ("about:blank", "", "data:,", "chrome://new-tab-page/", "chrome://new-tab-page")


def _env_on(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in ("1", "true", "yes", "on")


def _body_text(driver) -> str:
    try:
        return (driver.execute_script("return (document.body&&document.body.innerText)||''") or "").lower()
    except Exception:
        return ""


def _multi_sweep(driver, main, pred):
    """
    Saare tabs dekho. `pred(driver)` True wala tab mila to uska handle return.
    Baaki naye/foreign tabs (ads) band — blank tabs aur known-site tabs chhod ke.
    """
    try:
        handles = list(driver.window_handles)
    except Exception:
        return None
    found = None
    for h in handles:
        cur = ""
        try:
            driver.switch_to.window(h)
            cur = (driver.current_url or "").lower()
            if pred(driver):
                found = h
                break
        except Exception:
            continue
        if h == main or cur in _BLANK or any(x in cur for x in _MULTI_KEEP):
            continue
        try:
            driver.close()
        except Exception:
            pass
    try:
        hs = driver.window_handles
        if found and found in hs:
            driver.switch_to.window(found)
        elif main in hs:
            driver.switch_to.window(main)
        elif hs:
            driver.switch_to.window(hs[0])
    except Exception:
        pass
    return found


def _multi_back_if_ad(driver, log) -> bool:
    """Main tab kisi ad-site pe chala gaya ho to back karo."""
    try:
        cur = (driver.current_url or "").lower()
    except Exception:
        return False
    if cur in _BLANK or any(x in cur for x in _MULTI_KEEP):
        return False
    log(f"↩️ Ad-redirect ({urlparse(cur).netloc[:40]}) — back")
    try:
        driver.back()
        time.sleep(1.5)
        return True
    except Exception:
        return False


def _perf_file_url(driver) -> str | None:
    """Network-log se file-download ka URL (video/octet-stream/attachment/.mkv)."""
    import json as _json
    best = None
    try:
        logs = driver.get_log("performance")
    except Exception:
        return None
    for ent in logs:
        try:
            m = _json.loads(ent["message"])["message"]
        except Exception:
            continue
        prm = m.get("params", {})
        meth = m.get("method")
        if meth == "Network.responseReceived":
            rs = prm.get("response", {})
            url = rs.get("url", "")
            if not url.startswith("http"):
                continue
            mime = (rs.get("mimeType") or "").lower()
            hdr = {str(k).lower(): str(v).lower() for k, v in (rs.get("headers") or {}).items()}
            if (mime.startswith("video/")
                    or mime in ("application/octet-stream", "application/x-matroska", "binary/octet-stream")
                    or "attachment" in hdr.get("content-disposition", "")
                    or _MEDIA_EXT_RE.search(url.split("?")[0])):
                best = url
        elif meth == "Network.requestWillBeSent":
            url = (prm.get("request") or {}).get("url", "")
            if url.startswith("http") and _MEDIA_EXT_RE.search(url.split("?")[0]):
                best = url
    return best


def _multi_selenium_resolve(multi_url: str, debug: list) -> str | None:
    def log(msg):
        debug.append(msg)
        LOGGER.info(f"[Adh] {msg}")

    if not SELENIUM_OK or By is None:
        log("❌ Selenium install nahi hai — Multi flow nahi chal sakta")
        return None

    try:
        wait_total = int((os.getenv("ADH_MULTI_WAIT") or "420").strip())
    except Exception:
        wait_total = 420

    driver = None
    try:
        log("🌐 [Multi] Selenium browser khol rahe hain")
        driver = _make_adh_driver()
        try:
            driver.get(multi_url)
        except Exception as e:
            log(f"⚠️ [Multi] get() err (aage badh rahe hain): {str(e).splitlines()[0][:60]}")
        main = driver.current_window_handle

        # ── Stage A: re.php redirect -> FilesForever page ("Cloud Download" button) ──
        has_cloud = lambda d: _sel_find(d, "cloud download") is not None
        found = False
        t0 = time.time()
        reloaded = False
        while time.time() - t0 < 70:
            h = _multi_sweep(driver, main, has_cloud)
            if h:
                main = h
                found = True
                break
            if not reloaded and time.time() - t0 > 30:
                reloaded = True
                if not _multi_back_if_ad(driver, log):
                    log("🔄 [Multi] 30s ho gaye, Multi link dobara khol rahe hain")
                    try:
                        driver.get(multi_url)
                    except Exception:
                        pass
            time.sleep(1)
        if not found:
            log(f"❌ [Multi] FilesForever page pe 'Cloud Download' nahi mila (url: {(driver.current_url or '')[:80]})")
            _sel_diag(driver, debug, "Multi-A")
            return None
        log(f"✅ [Multi] FilesForever page mila: {(driver.current_url or '')[:80]}")

        # ── Stage B: Cloud Download click -> cldst "Preparing Your File" ──
        def is_prep(d):
            try:
                cu = (d.current_url or "").lower()
            except Exception:
                cu = ""
            if "cldst" in cu:
                return True
            t = _body_text(d)
            return "preparing your file" in t or "transfer completed" in t or "download file" in t

        prep = False
        for attempt in range(1, 7):
            el = _sel_find(driver, "cloud download")
            if el is None:
                h = _multi_sweep(driver, main, is_prep)
                if h:
                    main = h
                    prep = True
                    break
                log(f"⚠️ [Multi] attempt {attempt}: Cloud Download button nahi dikha")
                _multi_back_if_ad(driver, log)
                time.sleep(2)
                continue
            if not _sel_click(driver, el):
                log(f"⚠️ [Multi] attempt {attempt}: Cloud Download click fail")
                continue
            log(f"🖱️ [Multi] attempt {attempt}: Cloud Download click kiya")
            for _ in range(24):  # ~12s
                time.sleep(0.5)
                h = _multi_sweep(driver, main, is_prep)
                if h:
                    main = h
                    prep = True
                    break
            if prep:
                break
            _multi_back_if_ad(driver, log)
        if not prep:
            log("❌ [Multi] Cloud Download ke baad 'Preparing' page nahi aaya")
            _sel_diag(driver, debug, "Multi-B")
            return None
        log(f"✅ [Multi] Preparing page: {(driver.current_url or '')[:80]}")

        # ── Stage C: transfer complete -> "Download File" button (lamba wait) ──
        btn = None
        t0 = time.time()
        last_log = 0
        while time.time() - t0 < wait_total:
            btn = _sel_find(driver, "download file")
            if btn is not None:
                break
            el_s = int(time.time() - t0)
            if el_s - last_log >= 30:
                last_log = el_s
                t = re.sub(r"\s+", " ", _body_text(driver))[:90]
                log(f"⏳ [Multi] {el_s}s: transfer chal raha hai… ({t})")
            time.sleep(2)
        if btn is None:
            log(f"❌ [Multi] {wait_total}s mein 'Download File' button nahi aaya")
            _sel_diag(driver, debug, "Multi-C")
            return None
        log(f"✅ [Multi] Download File button aa gaya ({int(time.time() - t0)}s)")

        # ── Stage D: Download File -> final URL ──
        try:
            driver.get_log("performance")  # purana log saaf
        except Exception:
            pass
        for attempt in range(1, 6):
            el = _sel_find(driver, "download file")
            if el is None:
                el = btn
            href = ""
            try:
                href = el.get_attribute("href") or ""
            except Exception:
                pass
            cur_url = driver.current_url or ""
            if _real_href(href):
                full = urljoin(cur_url, href.strip())
                if full.split("#")[0] != cur_url.split("#")[0]:
                    log(f"✅ [Multi] Download File ke href se final link: {full[:100]}")
                    return full

            if not _sel_click(driver, el):
                log(f"⚠️ [Multi] attempt {attempt}: Download File click fail")
                continue
            log(f"🖱️ [Multi] attempt {attempt}: Download File click kiya")

            for _ in range(30):  # ~15s
                time.sleep(0.5)
                u = _perf_file_url(driver)
                if u:
                    log(f"✅ [Multi] network-log se download URL mila: {u[:100]}")
                    return u
                try:
                    for h in list(driver.window_handles):
                        if h == main:
                            continue
                        driver.switch_to.window(h)
                        cu = driver.current_url or ""
                        if cu in _BLANK:
                            continue
                        if _MEDIA_EXT_RE.search(cu.split("?")[0]):
                            log(f"✅ [Multi] naye tab mein file URL: {cu[:100]}")
                            return cu
                        if not any(x in cu.lower() for x in _MULTI_KEEP):
                            try:
                                driver.close()
                            except Exception:
                                pass
                    driver.switch_to.window(main)
                except Exception:
                    try:
                        driver.switch_to.window(driver.window_handles[0])
                        main = driver.current_window_handle
                    except Exception:
                        pass
                # click ke baad button ka href set ho gaya?
                el2 = _sel_find(driver, "download file")
                if el2 is not None:
                    try:
                        h2 = el2.get_attribute("href") or ""
                    except Exception:
                        h2 = ""
                    if _real_href(h2):
                        full = urljoin(driver.current_url or cur_url, h2.strip())
                        if full.split("#")[0] != (driver.current_url or "").split("#")[0]:
                            log(f"✅ [Multi] click ke baad href badla: {full[:100]}")
                            return full
            _multi_back_if_ad(driver, log)

        log("❌ [Multi] Download File click ke baad file URL pakad nahi paaye")
        _sel_diag(driver, debug, "Multi-D")
        return None
    except Exception as e:
        log(f"❌ [Multi] Selenium error: {type(e).__name__}: {str(e).splitlines()[0][:120] if str(e) else ''}")
        return None
    finally:
        if driver is not None:
            try:
                _kill_driver_tree(driver)
            except Exception:
                pass


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
#  Ek episode: Multi link -> resolve -> download -> filter -> upload
# ─────────────────────────────────────────────
async def _process_adh_item(client, message, ep: dict, status_msg, index: int, total: int,
                             language: str, quality: str):
    ep_label = f"E{ep['num']:02d}"
    loop = asyncio.get_event_loop()
    gdf_url = (ep.get("qualities") or {}).get(quality)
    fprs_url = (ep.get("fprs") or {}).get(quality)
    multi_url = (ep.get("multi") or {}).get(quality)
    if not multi_url and not (fprs_url and _env_on("ADH_FPRS_FALLBACK")) and not (gdf_url and _gdf_fallback_on()):
        try:
            await status_msg.edit(f"🛑 **{ep_label}** — is episode mein `{quality}` ka Multi link available nahi hai.")
        except Exception:
            pass
        return "error"

    try:
        await status_msg.edit(f"🔍 **{ep_label}** (`{index}/{total}`)\nDownload link nikal raha hoon...")
    except Exception:
        pass

    debug = []
    final_url = await loop.run_in_executor(None, _resolve_adh_download_link, gdf_url, debug, fprs_url, multi_url)

    shot_paths = [str(x)[len("__SHOT__:"):] for x in debug if str(x).startswith("__SHOT__:")]
    debug[:] = [x for x in debug if not str(x).startswith("__SHOT__:")]

    if not final_url:
        for sp in shot_paths:
            try:
                await message.reply_photo(sp, caption=f"🩺 {ep_label} — bot ke browser ne yeh page dekha")
            except Exception as e:
                LOGGER.warning(f"[Adh] debug screenshot bhej nahi paye: {e}")
        debug_text = _safe_debug(debug[-18:]) if debug else "(koi debug info nahi mili)"
        try:
            await status_msg.edit(
                f"🛑 **{ep_label} — Link Resolve Fail**\n\n"
                f"❌ Final download link nahi mila.\n\n"
                f"**Multi URL:** `{multi_url or '-'}`\n\n"
                f"**Debug (kaha atka, step-by-step):**\n```\n{debug_text}\n```\n"
                f"⛔ Agle items **band** kar diye gaye."
            )
        except Exception:
            pass
        return "error"

    for sp in shot_paths:
        try:
            os.remove(sp)
        except Exception:
            pass
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
            f"**URL:** `{series_url}`\n\n"
            f"Site slow ho sakti hai ya bot traffic block kar rahi ho — thodi der baad "
            f"dubara try karo."
        )
        return

    if not series_data.get("episode_list_url"):
        debug_text = _safe_debug(series_data.get("debug", [])[-12:]) or "(koi debug info nahi mili)"
        await status_msg.edit(
            "❌ Is page pe 'Download / Watch' button ka link nahi mila.\n\n"
            f"**URL:** `{series_url}`\n\n"
            f"**Debug (kaha atka, step-by-step):**\n```\n{debug_text}\n```"
        )
        return

    try:
        await status_msg.edit("🔍 Episode list scan ho raha hai...")
    except Exception:
        pass

    try:
        ep_result = await loop.run_in_executor(
            None, discover_adh_episodes, series_data["episode_list_url"], series_data.get("episode_list_html")
        )
    except Exception as e:
        LOGGER.error(f"[Adh] discover_adh_episodes error: {e}")
        await status_msg.edit(
            f"❌ Episode list load nahi hua (3 attempts ke baad bhi): `{str(e)[:100]}`\n\n"
            f"**URL:** `{series_data['episode_list_url']}`\n\n"
            f"Site slow ho sakti hai ya bot traffic block kar rahi ho — thodi der baad "
            f"dubara try karo."
        )
        return

    episodes = ep_result["episodes"]
    if not episodes:
        debug_text = _safe_debug(ep_result.get("debug", [])[-12:]) or "(koi debug info nahi mili)"
        await status_msg.edit(
            "❌ Episode-list page se koi episode nahi mila (480p/720p/1080p x265 mein se koi bhi).\n\n"
            f"**URL:** `{series_data['episode_list_url']}`\n\n"
            f"**Debug (kaha atka, step-by-step):**\n```\n{debug_text}\n```"
        )
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
