"""
rti_mega.py  —  RTI "1080p baaki hai" fallback: Mega link nikaalna  (TEST VERSION)
==================================================================================
Problem: RTI ke WatchMultQuality (argon/swift) pe 360p + 720p turant mil jaate hain, par 1080p
aane mein bahut der lagti hai. Lekin RTI page pe HubCloud / WatchNow / DLBeta / Mega buttons pe
1080p file pehle se maujood hoti hai.

Ye plugin pehle sirf MEGA wala raasta banata hai:

  1. RTI page (requests)  -> us episode ke block mein "Mega" button ka link
  2. Chrome mein wo link kholo -> codedew.com/zipper/?url=...  (Security scan page)
        - ~5s wait ("Scanning Link...")
        - "Continue to Link Generation" button click
        - "Get Secure Link" button click
  3. mega.nz link mil jaata hai  ->  `/mega <link>` se download.

TESTING ke liye poora process dikhta hai: live trace chat mein, har state-change pe screenshot,
aur end mein trace.txt (+ fail hone par page.html). Wahi mujhe de do — dekh ke fix kar dungi.

Command (sirf Sudo):
    /rtimega <rti_page_url> <episode>        — page se Mega button dhundh ke poora flow
    /rtimega <codedew_zipper_url>            — seedha zipper se shuru (sirf step 2-3 test karne ke liye)
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
from urllib.parse import unquote

import requests
from bs4 import BeautifulSoup
from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import InputMediaPhoto, Message

from .. import LOGGER, download_dir
from ..utils import job_ctl
from ..utils.helper import check_chat
from . import rti_downloader as rd

MEGA_RE = re.compile(
    r"https?://mega\.(?:nz|co\.nz)/(?:file/|folder/)[A-Za-z0-9_-]+(?:#[A-Za-z0-9_\-!]+)?"
    r"|https?://mega\.(?:nz|co\.nz)/#!?[A-Za-z0-9_\-!]+",
    re.IGNORECASE,
)
ZIPPER_TOTAL_TIMEOUT = 150           # zipper flow ka max time (sec)
MAX_SHOTS = 9

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
            tag: e.tagName, href: (e.href || '').slice(0, 200),
            dis: !!(e.disabled || e.getAttribute('aria-disabled') === 'true' || e.classList.contains('disabled'))});
});
return out;
"""


# ─────────────────────────────────────────────
#  Trace (thread-safe) + screenshots
# ─────────────────────────────────────────────
class Trace:
    def __init__(self, workdir: str):
        self.t0 = time.time()
        self.lines: list = []
        self.shots: list = []          # [(path, caption)]
        self.workdir = workdir
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
        if driver is None or (len(self.shots) >= MAX_SHOTS and not force):
            return
        try:
            path = os.path.join(self.workdir, f"shot_{len(self.shots) + 1:02d}.png")
            driver.save_screenshot(path)
            with self._lock:
                self.shots.append((path, f"{len(self.shots) + 1}. {label}"))
        except Exception as e:
            self.add(f"(screenshot fail: {e!r})")


# ─────────────────────────────────────────────
#  Step 1: RTI page -> Mega button link
# ─────────────────────────────────────────────
def find_mega_links(page_url: str, episode_num: int, tr: Trace) -> list:
    """Episode ke blocks mein 'Mega' button dhundho. Returns [{href,label,block,has_wmq}] (best pehle)."""
    tr.add(f"STEP 1 — RTI page fetch: {page_url}")
    r = requests.get(page_url, headers=rd.HEADERS, timeout=20)
    tr.add(f"page status={r.status_code} bytes={len(r.content)}")
    r.raise_for_status()
    try:
        with open(os.path.join(tr.workdir, "page.html"), "wb") as f:
            f.write(r.content)
    except Exception:
        pass
    soup = BeautifulSoup(r.content, "html.parser")

    found = []
    blocks_seen = 0
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
        tr.add(f"Episode {episode_num} block #{blocks_seen}: '{text[:60]}' | buttons: "
               + (", ".join(f"{l or '?'}" for l, _h in anchors) or "(koi nahi)"))
        for l, h in anchors:
            tr.add(f"    • {l or '?'} -> {h[:110]}")
            if "mega" in l.lower() and h and not h.startswith("#"):
                found.append({"href": h, "label": l, "block": text[:60], "has_wmq": has_wmq})

    if blocks_seen == 0:
        tr.add(f"❌ Page pe 'Episode {episode_num}' ka koi block hi nahi mila")
    found.sort(key=lambda x: (not x["has_wmq"], "untouched" in x["block"].lower() or "bluray" in x["block"].lower()))
    tr.add(f"Mega buttons mile: {len(found)}" + (f" — chuna: {found[0]['href'][:100]}" if found else ""))
    return found


# ─────────────────────────────────────────────
#  Step 2/3: codedew zipper -> mega link  (Selenium)
# ─────────────────────────────────────────────
def _mega_in(text: str):
    if not text:
        return None
    for cand in (text, unquote(text), unquote(unquote(text))):
        m = MEGA_RE.search(cand)
        if m:
            return m.group(0)
    return None


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


def _sweep_windows(driver, main: str, tr: Trace):
    """Popup/ad tabs band karo. Agar kisi tab mein mega link ho to wahi return karo."""
    hit = None
    try:
        handles = list(driver.window_handles)
    except Exception:
        return None
    if len(handles) <= 1:
        return None
    for h in handles:
        if h == main:
            continue
        try:
            driver.switch_to.window(h)
            u = driver.current_url or ""
            mg = _mega_in(u)
            if mg:
                hit = mg
                tr.add(f"🎯 naye tab ke URL mein mega link mila: {mg[:80]}")
            if "codedew" in u or mg:
                tr.add(f"(naya tab rakha: {u[:80]})")
                continue
            tr.add(f"popup/ad tab band kiya: {u[:80]}")
            driver.close()
        except Exception:
            pass
    try:
        driver.switch_to.window(main)
    except Exception:
        try:
            driver.switch_to.window(driver.window_handles[0])
        except Exception:
            pass
    return hit


def zipper_to_mega(start_url: str, tr: Trace) -> str | None:
    """Chrome mein start_url kholo aur codedew ke 3 step guzar ke mega link nikaalo. Poora process trace mein."""
    driver = None
    try:
        tr.add("STEP 2 — Chrome khol raha hoon")
        driver = rd._make_selenium_driver()
        driver.set_page_load_timeout(40)
        tr.add(f"URL open: {start_url[:120]}")
        try:
            driver.get(start_url)
        except Exception as e:
            tr.add(f"(page load warning: {type(e).__name__} — aage badh raha hoon)")
        main = driver.current_window_handle
        time.sleep(1.0)
        tr.shot(driver, "start page khula")

        deadline = time.time() + ZIPPER_TOTAL_TIMEOUT
        last_sig = None
        clicked_at: dict = {}
        clicks = 0
        while time.time() < deadline:
            # 0) kisi popup tab mein mega mila?
            hit = _sweep_windows(driver, main, tr)
            if hit:
                tr.add(f"✅ MEGA LINK: {hit}")
                return hit

            st = _page_state(driver)
            # 1) mega link url ya page source mein?
            for where, blob in (("current_url", st["url"]), ("page_source", _safe_source(driver))):
                mg = _mega_in(blob)
                if mg:
                    tr.add(f"✅ MEGA LINK ({where} mein): {mg}")
                    tr.shot(driver, "mega link mila")
                    return mg

            # 2) state badla? -> trace + screenshot
            btn_txt = " | ".join(f"{b['text']}{'(disabled)' if b['dis'] else ''}" for b in st["btns"]
                                 if b["text"] and not any(x in b["text"].lower() for x in ("theme",)))[:200]
            sig = (st["url"], st["step"], btn_txt)
            if sig != last_sig:
                last_sig = sig
                tr.add(f"STATE url={st['url'][:90]} | {st['step'] or 'step?'} | title='{st['title'][:40]}'")
                tr.add(f"    text: {st['head']}")
                tr.add(f"    buttons: {btn_txt or '(koi nahi)'}")
                tr.shot(driver, f"{st['step'] or 'page'}")

            # 3) button click karna hai?
            btn = _pick_button(st["btns"])
            if btn:
                key = (btn["text"], st["step"])
                if time.time() - clicked_at.get(key, 0) > 6:      # same button baar-baar na dabaye
                    clicked_at[key] = time.time()
                    clicks += 1
                    tr.add(f"👆 CLICK #{clicks}: '{btn['text']}' ({btn['tag']}) href={btn['href'][:80] or '-'}")
                    try:
                        el = driver.find_element(rd.By.CSS_SELECTOR, f'[data-bx="{btn["i"]}"]')
                        try:
                            el.click()
                            tr.add("    native click OK")
                        except Exception as ce:
                            tr.add(f"    native click fail ({type(ce).__name__}) -> JS click")
                            driver.execute_script("arguments[0].click();", el)
                    except Exception as e:
                        tr.add(f"    ❌ click fail: {type(e).__name__}: {str(e)[:100]}")
                    time.sleep(1.5)
                    tr.shot(driver, f"click ke baad: {btn['text'][:30]}")
                    continue
            else:
                if any(b.get("dis") for b in st["btns"]) or "scanning" in st["head"].lower():
                    pass                                # 5s wait wala step — bas ruko
            time.sleep(1.0)

        tr.add(f"⏰ {ZIPPER_TOTAL_TIMEOUT}s mein mega link nahi mila")
        tr.shot(driver, "TIMEOUT pe page", force=True)
        st = _page_state(driver)
        tr.add(f"FINAL url={st['url'][:100]} | {st['step']} | text: {st['head']}")
        try:
            with open(os.path.join(tr.workdir, "last_page.html"), "w", encoding="utf-8") as f:
                f.write(driver.page_source or "")
        except Exception:
            pass
        return None
    except Exception as e:
        tr.add(f"💥 ERROR: {type(e).__name__}: {str(e)[:200]}")
        LOGGER.error(f"[RTI-Mega] {traceback.format_exc()}")
        if driver is not None:
            tr.shot(driver, "error pe page", force=True)
        return None
    finally:
        rd._kill_driver_tree(driver)


def _safe_source(driver) -> str:
    try:
        return driver.page_source or ""
    except Exception:
        return ""


def resolve_mega_blocking(arg: str, episode_num, tr: Trace) -> str | None:
    """arg = RTI page url (+episode) ya seedha codedew zipper url."""
    if "codedew.com" in arg or "zipper" in arg:
        tr.add("Seedha zipper URL diya gaya — STEP 1 skip")
        return zipper_to_mega(arg, tr)
    found = find_mega_links(arg, episode_num, tr)
    if not found:
        tr.add("❌ Mega button nahi mila (page.html attach hoga)")
        return None
    direct = _mega_in(found[0]["href"])
    if direct:
        tr.add("Button ka href seedha mega link hai — zipper ki zarurat nahi")
        return direct
    return zipper_to_mega(found[0]["href"], tr)


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
    parts = message.text.split()
    if len(parts) < 2:
        await message.reply(
            "**Usage:**\n"
            "`/rtimega <rti_page_url> <episode>` — page se Mega button → zipper → mega link\n"
            "`/rtimega <codedew_zipper_url>` — seedha zipper steps test karo\n\n"
            "Poora process (trace + screenshots) yahin dikhega."
        )
        return
    arg = parts[1].strip()
    ep = None
    if "codedew.com" not in arg and "zipper" not in arg:
        if len(parts) < 3 or not parts[2].isdigit():
            await message.reply("Episode number bhi do: `/rtimega <rti_page_url> 17`")
            return
        ep = int(parts[2])

    workdir = os.path.join(download_dir, "_rtimega", uuid.uuid4().hex[:8])
    tr = Trace(workdir)
    msg = await message.reply("🧪 **RTI → Mega test shuru...**")
    loop = asyncio.get_event_loop()
    fut = job_ctl.run_in_thread(resolve_mega_blocking, arg, ep, tr)

    async def _live():
        last = ""
        while True:
            await asyncio.sleep(2.5)
            t = tr.tail(14)
            if t != last:
                last = t
                try:
                    await msg.edit(f"🧪 **RTI → Mega test chal raha hai...**\n<pre>{_html.escape(t[-3300:])}</pre>",
                                   parse_mode=ParseMode.HTML)
                except Exception:
                    pass

    live = asyncio.ensure_future(_live())
    mega = None
    try:
        mega = await asyncio.wait_for(fut, timeout=ZIPPER_TOTAL_TIMEOUT + 90)
    except asyncio.TimeoutError:
        tr.add("⏰ poora test timeout")
    except Exception as e:
        tr.add(f"💥 {type(e).__name__}: {e}")
    finally:
        live.cancel()

    # Result
    if mega:
        head = (f"✅ <b>Mega link mil gaya!</b>\n<code>{_html.escape(mega)}</code>\n\n"
                f"Ab download: <code>/mega {_html.escape(mega)}</code>")
    else:
        head = "❌ <b>Mega link nahi mila.</b> Neeche trace + screenshots hain — wahi bhej do."
    try:
        await msg.edit(f"{head}\n\n<pre>{_html.escape(tr.tail(16)[-3000:])}</pre>", parse_mode=ParseMode.HTML)
    except Exception:
        await message.reply(head, parse_mode=ParseMode.HTML)

    # trace.txt + html + screenshots
    try:
        tp = os.path.join(workdir, "trace.txt")
        with open(tp, "w", encoding="utf-8") as f:
            f.write(f"ARG: {arg}  EP: {ep}\nRESULT: {mega}\n\n{tr.full()}\n")
        await message.reply_document(tp, caption="🧾 Poora trace")
        if not mega:
            for name in ("last_page.html", "page.html"):
                p = os.path.join(workdir, name)
                if os.path.isfile(p):
                    await message.reply_document(p, caption=f"📄 {name}")
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
