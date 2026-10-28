"""
rti_mega.py  —  RTI "1080p baaki hai" fallback: Mega / HubCloud / WatchNow / DLBeta link nikaalna  (TEST)
=====================================================================================================
RTI page pe har episode ke neeche HubCloud / WatchNow / DLBeta / Mega buttons hote hain; sab
`codedew.com/zipper` (3-step "security scan") se guzarte hain:

  1. RTI page (requests)  -> us episode ke block mein chuna hua button ka link
  2. Chrome: zipper -> ~5s "Scanning Link..." -> "Continue to Link Generation" -> "Get Secure Link"
  3. final destination link (mega.nz / hubcloud / ...) mil jaata hai

FAST: Chrome page fetch ke SAATH start hota hai (system chromium + chromedriver, selenium-manager
bypass), polling 0.3s, button khulte hi click, screenshots sirf `debug` mein.

Command (sirf Sudo):
    /rtimega <rti_page_url> <ep> [mega|hubcloud|watchnow|dlbeta|all] [debug]
    /rtimega <codedew_zipper_url> [mega|hubcloud|watchnow|dlbeta] [debug]
"""

import asyncio
import html as _html
import os
import re
import shutil
import threading
import time
import traceback
import uuid
from urllib.parse import unquote, urlparse

import requests
from bs4 import BeautifulSoup
from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import InputMediaPhoto, Message

from .. import LOGGER, download_dir
from ..utils import job_ctl
from ..utils.helper import check_chat
from . import rti_downloader as rd

import base64 as _b64

MEGA_RE = re.compile(
    r"https?://mega\.(?:nz|co\.nz)/(?:file/|folder/)[A-Za-z0-9_-]+(?:#[A-Za-z0-9_\-!]+)?"
    r"|https?://mega\.(?:nz|co\.nz)/#!?[A-Za-z0-9_\-!]+",
    re.IGNORECASE,
)
ZIPPER_TOTAL_TIMEOUT = 120          # ek zipper flow ka max time (sec)
TICK = 0.3                          # polling interval
MAX_SHOTS = 10

# kind -> (button label match, destination URL hint regex)
KINDS = {
    "mega":     (("mega",), None),
    "hubcloud": (("hubcloud",), re.compile(r"https?://[^\s\"'<>]*hubcloud[^\s\"'<>]*", re.I)),
    "watchnow": (("watchnow", "watch now"), None),      # destination = codedew.com/streambeta/ (Download v1-v3 page)
    "generic":  ((), None),                              # 2nd hop (streambeta -> Download vN -> zipper -> final)
    "dlbeta":   (("dlbeta", "dl beta"), re.compile(r"https?://[^\s\"'<>]*dl-?beta[^\s\"'<>]*", re.I)),
}
_OWN_HOSTS = ("codedew.com", "rareanimes.", "google", "gstatic", "cloudflare", "fonts.", "doubleclick")

# Button text priority (lower = pehle). Ads / nav wale text exclude.
_BTN_PRIORITY = [
    "get secure link", "continue to link generation", "secure link", "link generation",
    "get link", "generate link", "continue", "proceed", "unlock", "verify", "download",
]
_BTN_EXCLUDE = ("telegram", "skip ads", "how to", "theme", "close", "cancel", "home", "search", "menu")

_JS_BUTTONS = """
var els = Array.from(document.querySelectorAll('a,button,[role=button],input[type=button],input[type=submit],div[onclick]'));
var out = [];
els.forEach(function(e, i) {
  var r = e.getBoundingClientRect();
  var cs = getComputedStyle(e);
  if (!(r.width > 0 && r.height > 0) || cs.visibility === 'hidden' || cs.display === 'none') return;
  e.setAttribute('data-bx', String(i));
  out.push({i: i, text: (e.innerText || e.value || '').trim().replace(/\\s+/g, ' ').slice(0, 70),
            tag: e.tagName, href: (e.href || '').slice(0, 300),
            dis: !!(e.disabled || e.getAttribute('aria-disabled') === 'true' || e.classList.contains('disabled'))});
});
return out;
"""


# ─────────────────────────────────────────────
#  Trace (thread-safe) + screenshots
# ─────────────────────────────────────────────
class Trace:
    def __init__(self, workdir: str, debug: bool = False):
        self.t0 = time.time()
        self.lines: list = []
        self.shots: list = []          # [(path, caption)]
        self.workdir = workdir
        self.debug = debug
        self._lock = threading.Lock()
        os.makedirs(workdir, exist_ok=True)

    def add(self, msg: str):
        line = f"[{time.time() - self.t0:6.1f}s] {msg}"
        with self._lock:
            self.lines.append(line)
        LOGGER.info(f"[RTI-Mega] {msg}")

    def tail(self, n: int = 14) -> str:
        with self._lock:
            return "\n".join(self.lines[-n:])

    def full(self) -> str:
        with self._lock:
            return "\n".join(self.lines)

    def shot(self, driver, label: str, force: bool = False):
        """Screenshot sirf debug mode mein (ya force=True: fail/final) — warna flow slow hota hai."""
        if driver is None or not (self.debug or force) or len(self.shots) >= MAX_SHOTS:
            return
        try:
            path = os.path.join(self.workdir, f"shot_{len(self.shots) + 1:02d}.png")
            driver.save_screenshot(path)
            with self._lock:
                self.shots.append((path, f"{len(self.shots) + 1}. {label}"))
        except Exception as e:
            self.add(f"(screenshot fail: {e!r})")


# ─────────────────────────────────────────────
#  Fast Chrome (system chromium + chromedriver — selenium-manager bypass)
# ─────────────────────────────────────────────
def _fast_driver():
    """rti_downloader._make_selenium_driver ~23s leta tha (selenium-manager chromedriver dhundhta hai).
    Yahan seedha /usr/bin/chromium + /usr/bin/chromedriver, eager load, images off."""
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    profile_dir = os.path.join(download_dir, "_chrome_tmp", f"rm_{uuid.uuid4().hex}")
    os.makedirs(profile_dir, exist_ok=True)
    o = webdriver.ChromeOptions()
    for a in ("--headless=new", "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
              "--disable-extensions", "--disable-background-networking", "--disable-default-apps",
              "--disable-sync", "--disable-translate", "--no-first-run", "--mute-audio",
              "--disable-crash-reporter", "--disable-popup-blocking", "--window-size=1280,900",
              "--blink-settings=imagesEnabled=false", f"--user-data-dir={profile_dir}",
              f"user-agent={rd.HEADERS['User-Agent']}"):
        o.add_argument(a)
    o.add_experimental_option("prefs", {"profile.managed_default_content_settings.images": 2})
    o.page_load_strategy = "eager"
    for b in ("/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable"):
        if os.path.exists(b):
            o.binary_location = b
            break
    service = None
    for cd in ("/usr/bin/chromedriver", "/usr/lib/chromium/chromedriver", "/usr/lib/chromium-browser/chromedriver"):
        if os.path.exists(cd):
            service = Service(executable_path=cd)
            break
    drv = webdriver.Chrome(service=service, options=o) if service else webdriver.Chrome(options=o)
    drv.set_page_load_timeout(40)
    drv._suhani_profile_dir = profile_dir
    try:
        job_ctl.register_driver(drv)
    except Exception:
        pass
    return drv


class _DriverFuture:
    """Chrome ko background thread mein start karo (page fetch ke saath-saath)."""

    def __init__(self, tr: Trace):
        self.tr = tr
        self.driver = None
        self.error = None
        self._ev = threading.Event()
        t0 = time.time()

        def _run():
            try:
                self.driver = _fast_driver()
                tr.add(f"Chrome ready ({time.time() - t0:.1f}s)")
            except Exception as e:
                self.error = e
                tr.add(f"❌ Chrome start fail: {type(e).__name__}: {str(e)[:150]}")
            finally:
                self._ev.set()

        threading.Thread(target=_run, daemon=True).start()

    def get(self, timeout: float = 90):
        self._ev.wait(timeout)
        if self.error:
            raise self.error
        return self.driver

    def kill(self):
        """Background mein band karo — result dikhane mein Chrome start/stop ka wait na lage."""
        def _k():
            self._ev.wait(60)
            rd._kill_driver_tree(self.driver)
        threading.Thread(target=_k, daemon=True).start()


# ─────────────────────────────────────────────
#  Step 1: RTI page -> button link
# ─────────────────────────────────────────────
def find_links(page_url: str, episode_num: int, kind: str, tr: Trace) -> list:
    """Episode ke blocks mein `kind` ka button dhundo. Returns [{href,label,block,has_wmq}] (best pehle)."""
    labels = KINDS[kind][0]
    content = getattr(tr, "_page", None)
    if content is None:
        tr.add(f"STEP 1 — RTI page fetch ({kind})")
        r = requests.get(page_url, headers=rd.HEADERS, timeout=20)
        tr.add(f"page status={r.status_code} bytes={len(r.content)}")
        r.raise_for_status()
        content = tr._page = r.content          # 'all' mode mein dobara fetch nahi
        try:
            with open(os.path.join(tr.workdir, "page.html"), "wb") as f:
                f.write(content)
        except Exception:
            pass
    else:
        tr.add(f"STEP 1 — ({kind}) cached page se")
    soup = BeautifulSoup(content, "html.parser")

    found, blocks_seen = [], 0
    for p in soup.find_all("p"):
        text = p.get_text(" ", strip=True)
        m = re.search(r"Episode\s*(\d+)", text, re.IGNORECASE)
        if not (m and int(m.group(1)) == episode_num):
            continue
        blocks_seen += 1
        elems = [p]
        for idx, sib in enumerate(p.find_next_siblings()):
            if idx > 8:
                break
            st = sib.get_text(" ", strip=True)
            em = re.search(r"Episode\s*(\d+)", st, re.IGNORECASE)
            if em and int(em.group(1)) != episode_num:
                break
            elems.append(sib)
        anchors = []
        for el in elems:
            anchors += [(a.get_text(" ", strip=True), (a.get("href") or "").strip()) for a in el.find_all("a", href=True)]
        has_wmq = any(re.search(r"mult\w*quality", l, re.IGNORECASE) for l, _h in anchors)
        tr.add(f"Ep {episode_num} block #{blocks_seen}: '{text[:50]}' | " + ", ".join(l or "?" for l, _h in anchors[:9]))
        for l, h in anchors:
            if any(x in l.lower() for x in labels) and h and not h.startswith("#"):
                found.append({"href": h, "label": l, "block": text[:60], "has_wmq": has_wmq})
                if tr.debug:
                    tr.add(f"    • {l} -> {h[:100]}")

    if blocks_seen == 0:
        tr.add(f"❌ Page pe 'Episode {episode_num}' ka koi block nahi mila")
    found.sort(key=lambda x: (not x["has_wmq"], "untouched" in x["block"].lower() or "bluray" in x["block"].lower()))
    tr.add(f"{kind} buttons mile: {len(found)}" + (f" — chuna: block '{found[0]['block'][:35]}'" if found else ""))
    return found


# ─────────────────────────────────────────────
#  Destination detection
# ─────────────────────────────────────────────
def _mega_in(text: str):
    if not text:
        return None
    for cand in (text, unquote(text), unquote(unquote(text))):
        m = MEGA_RE.search(cand)
        if m:
            return m.group(0)
    return None


_STREAMBETA_RE = re.compile(r"https?://(?:www\.)?codedew\.com/streambeta[^\s\"'<>]*", re.I)
_PIXEL_RE = re.compile(r"https?://pixeldrain\.(?:net|dev|com)/(?:u|api/file)/[A-Za-z0-9]+[^\s\"'<>]*", re.I)
_HUBDRIVE_RE = re.compile(r"https?://[^\s\"'<>/]*hubcloud[^\s\"'<>/]*/drive/[A-Za-z0-9]+", re.I)
_VIDEO_URL_RE = re.compile(r"https?://[^\s\"'<>]+\.(?:mkv|mp4|m4v|webm)(?:\?[^\s\"'<>]*)?", re.I)


def _dest_in(kind: str, text: str, url_only: bool = False):
    """kind ka destination URL text mein? (mega: mega.nz; baaki: unka hint regex, apni sites chhodke)"""
    if not text:
        return None
    if kind == "mega":
        return _mega_in(text)
    if kind == "watchnow":
        m = _STREAMBETA_RE.search(text)
        return m.group(0) if m else None
    if kind == "generic":
        mg = _mega_in(text)
        if mg:
            return mg
        for rx in ((_PIXEL_RE, _HUBDRIVE_RE) + ((_VIDEO_URL_RE,) if url_only else ())):
            for cand in (text, unquote(text)):
                m = rx.search(cand)
                if m:
                    return m.group(0)
        return None
    rx = KINDS[kind][1]
    for cand in (text, unquote(text)):
        for m in rx.finditer(cand):
            u = m.group(0).rstrip("\\\"'),;")
            if any(h in u.lower() for h in _OWN_HOSTS) or urlparse(u).path in ("", "/"):
                continue                         # apni site / sirf domain = destination nahi
            return u
    return None


def _static_try(start_url: str, kind: str, tr: Trace):
    """Chrome ke bina: zipper page ke HTML/base64 blobs mein destination mil jaaye to 15s bach jaate hain."""
    try:
        r = requests.get(start_url, headers=rd.HEADERS, timeout=10)
        txt = r.text
        hit = _dest_in(kind, txt)
        if not hit:
            for blob in re.findall(r"[A-Za-z0-9+/=_-]{40,}", txt)[:60]:
                try:
                    dec = _b64.b64decode(blob + "=" * (-len(blob) % 4)).decode("utf-8", "ignore")
                except Exception:
                    continue
                hit = _dest_in(kind, dec)
                if hit:
                    break
        tr.add(f"static try: {'✅ ' + hit[:80] if hit else 'nahi (JS chahiye) — Chrome use hoga'}")
        return hit
    except Exception as e:
        tr.add(f"static try fail: {type(e).__name__}")
        return None


# ─────────────────────────────────────────────
#  Step 2/3: codedew zipper (Selenium)
# ─────────────────────────────────────────────
def _safe_source(driver) -> str:
    try:
        return driver.page_source or ""
    except Exception:
        return ""


def _page_state(driver) -> dict:
    st = {"url": "", "title": "", "step": "", "head": "", "btns": []}
    try:
        st["url"] = driver.current_url
        st["title"] = driver.title or ""
    except Exception:
        pass
    try:
        body = driver.execute_script("return document.body ? document.body.innerText : ''") or ""
        m = re.search(r"STEP\s*\d\s*OF\s*\d", body, re.IGNORECASE)
        st["step"] = m.group(0).upper() if m else ""
        st["head"] = re.sub(r"\s+", " ", body)[:140]
    except Exception:
        pass
    try:
        st["btns"] = driver.execute_script(_JS_BUTTONS) or []
    except Exception:
        st["btns"] = []
    return st


def _pick_button(btns: list):
    best, best_p = None, 999
    for b in btns:
        t = (b.get("text") or "").lower()
        if not t or b.get("dis") or any(x in t for x in _BTN_EXCLUDE):
            continue
        for pi, pat in enumerate(_BTN_PRIORITY):
            if pat in t:
                if pi < best_p:
                    best, best_p = b, pi
                break
    return best


def _sweep_windows(driver, holder: list, kind: str, tr: Trace):
    """Popup/ad tabs band karo; naya codedew tab aaye to wahi main ban jaata hai;
    destination wala tab mile to uska URL return karo."""
    main = holder[0]
    try:
        handles = list(driver.window_handles)
    except Exception:
        return None
    if len(handles) <= 1:
        return None
    hit = None
    for h in handles:
        if h == main or h == holder[0] and h != main:
            continue
        try:
            driver.switch_to.window(h)
            u = driver.current_url or ""
            d = _dest_in(kind, u, url_only=True)
            if d:
                hit = d
                tr.add(f"🎯 naye tab mein destination: {d[:90]}")
                continue
            if "codedew" in u:
                tr.add(f"codedew naya tab adopt: {u[:70]}")
                holder[0] = h
                continue
            tr.add(f"popup tab band: {u[:70]}")
            driver.close()
        except Exception:
            pass
    if holder[0] != main:                      # purana main band (flip-flop na ho)
        try:
            driver.switch_to.window(main)
            driver.close()
        except Exception:
            pass
    try:
        driver.switch_to.window(holder[0])
    except Exception:
        try:
            holder[0] = driver.window_handles[0]
            driver.switch_to.window(holder[0])
        except Exception:
            pass
    return hit


def _dump_destination(driver, kind: str, tr: Trace):
    """Non-mega destination pe pahunche: page ka haal (next hop banane ke liye) trace mein."""
    time.sleep(2.0)
    st = _page_state(driver)
    tr.add(f"DEST PAGE url={st['url'][:100]} | title='{st['title'][:50]}'")
    tr.add(f"    text: {st['head']}")
    bt = [f"{b['text']}→{b['href'][:60]}" if b["href"] else b["text"] for b in st["btns"] if b["text"]][:14]
    tr.add(f"    buttons: {' | '.join(bt) or '(koi nahi)'}")
    ext = [b["href"] for b in st["btns"] if b["href"].startswith("http")
           and not any(h in b["href"].lower() for h in _OWN_HOSTS)][:10]
    if ext:
        tr.add("    external links: " + " ; ".join(x[:80] for x in ext))
    tr.shot(driver, f"{kind} destination page", force=True)


def zipper_flow(driver, start_url: str, kind: str, tr: Trace, navigate: bool = True):
    """driver pe start_url kholo (navigate=False: jahan hai wahin se), codedew ke steps guzar ke
    `kind` ka destination URL return karo."""
    t_start = time.time()
    if navigate:
        try:
            driver.get(start_url)
        except Exception as e:
            tr.add(f"(page load warning: {type(e).__name__} — aage badh raha hoon)")
    holder = [driver.current_window_handle]
    tr.add(f"zipper {'open: ' + start_url[:80] if navigate else 'continue (click ke baad)'}")
    tr.shot(driver, f"{kind}: start")

    deadline = time.time() + ZIPPER_TOTAL_TIMEOUT
    last_sig, clicked_at, clicks = None, {}, 0
    while time.time() < deadline:
        hit = _sweep_windows(driver, holder, kind, tr)
        if hit:
            tr.add(f"✅ {kind.upper()} LINK ({time.time() - t_start:.1f}s): {hit}")
            return hit

        st = _page_state(driver)
        # destination page_source / url / anchors mein?
        for where, blob in (("url", st["url"]), ("page", _safe_source(driver))):
            d = _dest_in(kind, blob, url_only=(where == "url"))
            if d:
                tr.add(f"✅ {kind.upper()} LINK ({where}, {time.time() - t_start:.1f}s): {d}")
                tr.shot(driver, f"{kind}: link mila")
                if kind not in ("mega", "generic"):
                    _dump_destination(driver, kind, tr)
                return d
        # zipper se bahar nikal gaye (redirect) — non-mega ke liye yahi destination
        if kind not in ("mega", "watchnow") and st["url"] and "codedew.com" not in st["url"] \
                and not st["url"].startswith(("about:", "data:", "chrome")):
            tr.add(f"✅ {kind.upper()} redirect ({time.time() - t_start:.1f}s): {st['url']}")
            if kind != "generic":
                _dump_destination(driver, kind, tr)
            return st["url"]

        btn_txt = " | ".join(f"{b['text']}{'(disabled)' if b['dis'] else ''}" for b in st["btns"]
                             if b["text"] and "theme" not in b["text"].lower())[:200]
        sig = (st["url"], st["step"], btn_txt)
        if sig != last_sig:
            last_sig = sig
            tr.add(f"STATE {st['step'] or 'step?'} | '{st['title'][:38]}' | btns: {btn_txt[:110] or '-'}")
            if tr.debug:
                tr.add(f"    url={st['url'][:90]} | text: {st['head']}")
            tr.shot(driver, f"{kind}: {st['step'] or 'page'}")

        btn = _pick_button(st["btns"])
        if btn:
            key = (btn["text"], st["step"])
            if time.time() - clicked_at.get(key, 0) > 3:
                clicked_at[key] = time.time()
                clicks += 1
                tr.add(f"👆 CLICK #{clicks}: '{btn['text']}' href={btn['href'][:60] or '-'}")
                try:
                    el = driver.find_element(rd.By.CSS_SELECTOR, f'[data-bx="{btn["i"]}"]')
                    try:
                        el.click()
                    except Exception as ce:
                        tr.add(f"    native click fail ({type(ce).__name__}) -> JS click")
                        driver.execute_script("arguments[0].click();", el)
                except Exception as e:
                    tr.add(f"    ❌ click fail: {type(e).__name__}: {str(e)[:100]}")
                time.sleep(0.2)
                continue
        time.sleep(TICK)

    tr.add(f"⏰ {ZIPPER_TOTAL_TIMEOUT}s mein {kind} link nahi mila")
    tr.shot(driver, f"{kind}: TIMEOUT", force=True)
    st = _page_state(driver)
    tr.add(f"FINAL url={st['url'][:100]} | {st['step']} | text: {st['head']}")
    try:
        with open(os.path.join(tr.workdir, f"last_page_{kind}.html"), "w", encoding="utf-8") as f:
            f.write(driver.page_source or "")
    except Exception:
        pass
    return None


def resolve_blocking(arg: str, episode_num, kinds: list, tr: Trace, post=None) -> dict:
    """arg = RTI page url (+episode) ya seedha zipper url. Returns {kind: url|None}.
    post(kind, dest_url, driver, tr) -> value : same Chrome mein agla hop (hubcloud servers / streambeta Download vN)."""
    out = {k: None for k in kinds}
    dfut = None
    try:
        if "codedew.com" in arg or "zipper" in arg:
            starts = {kinds[0]: arg}
            dfut = _DriverFuture(tr)
        else:
            dfut = _DriverFuture(tr)                       # Chrome page fetch ke saath-saath start
            starts = {}
            for k in kinds:
                found = find_links(arg, episode_num, k, tr)
                if not found:
                    tr.add(f"❌ {k}: button nahi mila")
                    continue
                direct = _dest_in(k, found[0]["href"])
                if direct:
                    out[k] = post(k, direct, None, tr) if post is not None else direct
                    tr.add(f"{k}: href seedha destination hai")
                    continue
                starts[k] = found[0]["href"]

        for k, url in starts.items():
            tr.add(f"━━ {k.upper()} ━━")
            t_k = time.time()
            hit = _static_try(url, k, tr) if k == "mega" else None
            if hit:
                out[k] = post(k, hit, None, tr) if post is not None else hit
                tr.add(f"⏱ {k}: {time.time() - t_k:.1f}s (Chrome ke bina)")
                continue
            try:
                drv = dfut.get()
            except Exception:
                break
            try:
                drv.delete_all_cookies()
            except Exception:
                pass
            out[k] = zipper_flow(drv, url, k, tr)
            if out[k] and post is not None:
                try:
                    out[k] = post(k, out[k], drv, tr)
                except Exception as pe:
                    tr.add(f"💥 {k} next-hop error: {type(pe).__name__}: {str(pe)[:150]}")
                    LOGGER.error(f"[RTI-Mega] post {k}: {traceback.format_exc()}")
                    out[k] = None
            tr.add(f"⏱ {k}: {time.time() - t_k:.1f}s")
    except Exception as e:
        tr.add(f"💥 ERROR: {type(e).__name__}: {str(e)[:200]}")
        LOGGER.error(f"[RTI-Mega] {traceback.format_exc()}")
        if dfut is not None and dfut.driver is not None:
            tr.shot(dfut.driver, "error pe page", force=True)
    finally:
        if dfut is not None:
            dfut.kill()
    return out


# ─────────────────────────────────────────────
#  /rtimega  (test command)
# ─────────────────────────────────────────────
@Client.on_message(filters.command("rtimega"))
async def rtimega_command(client: Client, message: Message):
    c = await check_chat(message, chat="Sudo")
    if not c:
        return
    if not rd.SELENIUM_OK:
        await message.reply("❌ Selenium install nahi hai!")
        return
    parts = message.text.split()[1:]
    if not parts:
        await message.reply(
            "**Usage:**\n"
            "`/rtimega <rti_page_url> <ep> [mega|hubcloud|watchnow|dlbeta|all] [debug]`\n"
            "`/rtimega <codedew_zipper_url> [mega|hubcloud|watchnow|dlbeta] [debug]`\n\n"
            "• default = `mega` • `all` = chaaron ek saath • `debug` = har step ka screenshot\n"
            "• Links milne ke baad mega/dlbeta/hubcloud se **download + Telegram upload** bhi hota hai "
            "(`noup` likho to sirf links)\n"
            "Poora process (trace) yahin dikhega."
        )
        return
    arg = parts[0]
    debug = any(p.lower() == "debug" for p in parts)
    noup = any(p.lower() == "noup" for p in parts)          # noup = sirf links, upload nahi
    rest = [p for p in parts[1:] if p.lower() not in ("debug", "noup")]
    ep = None
    if "codedew.com" not in arg and "zipper" not in arg:
        if not rest or not rest[0].isdigit():
            await message.reply("Episode number bhi do: `/rtimega <rti_page_url> 17`")
            return
        ep = int(rest.pop(0))
    kind = (rest[0].lower() if rest else "mega")
    if kind == "all":
        kinds = ["mega", "hubcloud", "watchnow", "dlbeta"] if ep is not None else ["mega"]
    elif kind in KINDS:
        kinds = [kind]
    else:
        await message.reply("Kind: `mega` `hubcloud` `watchnow` `dlbeta` ya `all`")
        return

    workdir = os.path.join(download_dir, "_rtimega", uuid.uuid4().hex[:8])
    tr = Trace(workdir, debug=debug)
    msg = await message.reply("🧪 **RTI test shuru...**")
    fut = job_ctl.run_in_thread(resolve_blocking, arg, ep, kinds, tr)

    async def _live():
        last = ""
        while True:
            await asyncio.sleep(2.5)
            t = tr.tail(14)
            if t != last:
                last = t
                try:
                    await msg.edit(f"🧪 **RTI test chal raha hai...**\n<pre>{_html.escape(t[-3300:])}</pre>",
                                   parse_mode=ParseMode.HTML)
                except Exception:
                    pass

    live = asyncio.ensure_future(_live())
    results: dict = {}
    try:
        results = await asyncio.wait_for(fut, timeout=ZIPPER_TOTAL_TIMEOUT * len(kinds) + 90)
    except asyncio.TimeoutError:
        tr.add("⏰ poora test timeout")
    except Exception as e:
        tr.add(f"💥 {type(e).__name__}: {e}")
    finally:
        live.cancel()

    # Result summary
    lines = []
    for k in kinds:
        u = results.get(k)
        lines.append(f"{'✅' if u else '❌'} <b>{k}</b>: " + (f"<code>{_html.escape(u)}</code>" if u else "nahi mila"))
    head = "\n".join(lines)
    mega = results.get("mega")
    if mega:
        head += (f"\n\nDownload: <code>/mega {_html.escape(mega)}</code>"
                 f"\nURL-style upload: <code>/megaurl {_html.escape(mega)}</code>")
    if not all(results.get(k) for k in kinds):
        head += "\n\nTrace + screenshots neeche — wahi bhej do."
    try:
        await msg.edit(f"{head}\n\n<pre>{_html.escape(tr.tail(12)[-2500:])}</pre>", parse_mode=ParseMode.HTML,
                       disable_web_page_preview=True)
    except Exception:
        await message.reply(head, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

    try:
        tp = os.path.join(workdir, "trace.txt")
        with open(tp, "w", encoding="utf-8") as f:
            f.write(f"ARG: {arg}  EP: {ep}  KINDS: {kinds}\nRESULT: {results}\n\n{tr.full()}\n")
        await message.reply_document(tp, caption="🧾 Poora trace")
        if not all(results.get(k) for k in kinds):
            for name in sorted(os.listdir(workdir)):
                if name.endswith(".html"):
                    await message.reply_document(os.path.join(workdir, name), caption=f"📄 {name}")
        if tr.shots:
            media = [InputMediaPhoto(p, caption=(c_ or "")[:200]) for p, c_ in tr.shots[:10]]
            try:
                await message.reply_media_group(media)
            except Exception:
                for p, c_ in tr.shots[:10]:
                    try:
                        await message.reply_photo(p, caption=c_[:200])
                    except Exception:
                        pass
    except Exception as e:
        LOGGER.warning(f"[RTI-Mega] result bhejne mein error: {e!r}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    # ── REAL TEST: mile hue links se download -> Telegram upload (ek-ek karke, alag status msg) ──
    if noup:
        return
    from .mega_url import run_source_upload
    todo = [(k, results.get(k)) for k in ("mega", "dlbeta", "hubcloud") if results.get(k)]
    if results.get("watchnow"):
        await message.reply("ℹ️ <b>watchnow</b> sirf streaming page hai (direct file nahi) — upload skip.",
                            parse_mode=ParseMode.HTML)
    if not todo:
        await message.reply("⚠️ Upload test ke liye koi mega / dlbeta / hubcloud link nahi mila.")
        return
    summary = []
    for k, u in todo:
        ok = await run_source_upload(client, message, k, u)
        summary.append(f"{'✅' if ok else '❌'} {k}")
    if len(todo) > 1:
        await message.reply("🧪 <b>Upload test result</b>\n" + "\n".join(summary), parse_mode=ParseMode.HTML)
