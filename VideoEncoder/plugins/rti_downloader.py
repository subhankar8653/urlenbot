"""
rti_downloader.py  v3
======================
Commands:
  /rti <url>                -> Latest episode auto-detect + download
  /rti <url> <start> <end>  -> Episode range download
  /rti <url> 5 5            -> Sirf episode 5
  /rti <url> 0 0            -> MOVIE mode — episode number dhundhta hi
                                nahi, seedha page pe jo WatchMultQuality
                                link mile use utha leta hai
"""

import asyncio
import os
import re
import shutil
import time
import uuid

import requests
from bs4 import BeautifulSoup
from pyrogram import Client, filters
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from .. import LOGGER, download_dir, app
from ..utils.helper import check_chat
from ..utils.database.access_db import db
from .update_channel import _get_update_toggle, _set_update_toggle

try:
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    SELENIUM_OK = True
except ImportError:
    SELENIUM_OK = False

ARGON_DOMAIN = "argon.razorshell.space/embed"
SWIFT_BASE   = "https://argon.razorshell.space/downlead/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

AUDIO_PRIORITY = ["hindi", "dual", "multi", "english", "japanese", "sub", "unknown"]

# ─────────────────────────────────────────────
#  NEW: Button-based episode selector (/rti <url>)
# ─────────────────────────────────────────────
# sid -> RTISelector. In-memory hai, redeploy pe clear ho jaata hai —
# usse zyada purani sessions bhi _prune_sessions() clean kar deta hai.
RTI_SESSIONS = {}
RTI_SESSION_TIMEOUT = 3600  # 1 hour
PER_PAGE = 10  # screenshot jaisa hi — 10 episode buttons per page

# OVA/Special ko pehle check karo taaki generic "Episode" pattern
# "OVA Episode 1" jaisa text galti se EP na bana de.
ITEM_PATTERNS = [
    (re.compile(r"\bOVA\s*(\d+)\b", re.IGNORECASE), "OVA"),
    (re.compile(r"\bSpecial(?:\s*Episode)?\s*(\d+)\b", re.IGNORECASE), "SP"),
    (re.compile(r"\bEpisode\s*(\d+)\b", re.IGNORECASE), "EP"),
]


# ─────────────────────────────────────────────
#  NEW: RTI ka apna "Channel Upload" toggle — per user save hota hai.
#  ON hone pe har successful upload us user ke pehle se /addchannel se
#  add kiye gaye channel pe bhi copy ho jaata hai. Default OFF.
#  ("Update Post" toggle ke liye humne update_channel.py ka already
#  bana hua bot-wide _get_update_toggle/_set_update_toggle reuse kiya hai.)
# ─────────────────────────────────────────────
async def _get_rti_channel_upload(user_id: int) -> bool:
    user = await db._get_user(user_id)
    return user.get("rti_channel_upload", False)


async def _set_rti_channel_upload(user_id: int, enabled: bool):
    await db.col.update_one({"id": int(user_id)}, {"$set": {"rti_channel_upload": enabled}}, upsert=True)


# ─────────────────────────────────────────────
#  Audio detection helpers
# ─────────────────────────────────────────────
def _detect_audio(link_el, context_text: str) -> str:
    prev = link_el.previous_sibling
    if prev:
        t = str(prev).strip().lower()
        for kw, label in [("hindi","hindi"),("dual","dual"),("multi","multi"),
                           ("english","english"),("japanese","japanese"),("sub","sub")]:
            if kw in t:
                return label

    parent = link_el.find_parent()
    if parent:
        pt = parent.get_text(" ", strip=True).lower()
        lt = link_el.get_text(strip=True).lower()
        if lt in pt:
            before = pt.split(lt)[0][-80:]
            for kw, label in [("hindi","hindi"),("dual","dual"),("multi","multi"),
                               ("english","english"),("japanese","japanese"),("sub","sub")]:
                if kw in before:
                    return label

    ct = context_text.lower()
    for pat, label in [
        (r"hindi\s*[-\u2013\u2014]\s*\[?watch", "hindi"),
        (r"english\s*[-\u2013\u2014]\s*\[?watch", "english"),
        (r"japanese\s*[-\u2013\u2014]\s*\[?watch", "japanese"),
        (r"dual\s*audio\s*[-\u2013\u2014]\s*\[?watch", "dual"),
    ]:
        if re.search(pat, ct):
            return label

    for kw, label in [("hindi","hindi"),("dual","dual"),("multi","multi"),
                      ("english","english"),("japanese","japanese"),("sub","sub")]:
        if kw in ct:
            return label

    return "unknown"


def _find_wmq_links(element) -> list:
    links = []
    ctx = element.get_text(" ", strip=True)
    for a in element.find_all("a", href=True):
        text = a.get_text(strip=True).lower()
        href = a.get("href", "")
        if "watchmultquality" in text or "watchmultquality" in href.lower() or "multiquality" in text:
            audio = _detect_audio(a, ctx)
            links.append({"href": href, "audio": audio})
    return links


def _best_link(links: list):
    for priority in AUDIO_PRIORITY:
        for lnk in links:
            if lnk["audio"] == priority:
                return lnk
    return links[0] if links else None


def _match_item(text: str):
    """Text mein Episode/OVA/Special number dhundo. Returns (kind, num) ya (None, None)."""
    for pattern, kind in ITEM_PATTERNS:
        m = pattern.search(text)
        if m:
            return kind, int(m.group(1))
    return None, None


# ─────────────────────────────────────────────
#  NEW: Poore page ka ek-hi-baar scan — saare Episode/OVA/Special
#  items aur unke WatchMultQuality (Hindi priority) links nikaal lo.
#  Isse har episode ke liye baar baar page fetch nahi karna padta.
# ─────────────────────────────────────────────
def discover_items(page_url: str):
    """
    Returns dict:
      {
        "title": str, "season": int, "mode": "episodes" | "movie",
        "items": [{"kind": "EP"/"OVA"/"SP"/"MOVIE", "num": int,
                    "label": str, "wmq_link": str}, ...]
      }
    """
    r = requests.get(page_url, headers=HEADERS, timeout=15)
    r.raise_for_status()
    soup = BeautifulSoup(r.content, "html.parser")

    title_tag = soup.find("h1", class_="entry-title")
    title = title_tag.text.strip() if title_tag else "Unknown Anime"

    season_match = re.search(r"season\s*(\d+)", title, re.IGNORECASE)
    season = int(season_match.group(1)) if season_match else 1

    # Saare candidate <p> blocks jisme Episode/OVA/Special text ho, DOM order mein
    blocks = []
    for p in soup.find_all("p"):
        text = p.get_text(" ", strip=True)
        kind, num = _match_item(text)
        if kind:
            blocks.append((p, kind, num))

    seen = set()
    items = []
    for p, kind, num in blocks:
        key = (kind, num)
        if key in seen:
            # Same episode ke duplicate headings (e.g. "Untouched CR (Multi
            # Audio)" wala dusra block) — pehla wala hi rakho.
            continue

        all_links = list(_find_wmq_links(p))
        for sib_idx, sibling in enumerate(p.find_next_siblings()):
            if sib_idx > 8:
                break
            sib_text = sibling.get_text(" ", strip=True)
            sib_kind, sib_num = _match_item(sib_text)
            if sib_kind and (sib_kind, sib_num) != key:
                break
            all_links.extend(_find_wmq_links(sibling))

        best = _best_link(all_links)
        if not best:
            continue

        seen.add(key)
        items.append({"kind": kind, "num": num, "wmq_link": best["href"]})

    if not items:
        # ── MOVIE MODE ── Episode/OVA/Special jaisa kuch nahi mila,
        # seedha poore page pe best (Hindi priority) WatchMultQuality
        # link utha lo.
        all_links = _find_wmq_links(soup)
        best = _best_link(all_links)
        if not best:
            return {"title": title, "season": season, "mode": "movie", "items": []}
        return {
            "title": title,
            "season": season,
            "mode": "movie",
            "items": [{"kind": "MOVIE", "num": 0, "label": "Movie", "wmq_link": best["href"]}],
        }

    kind_order = {"EP": 0, "OVA": 1, "SP": 2}
    items.sort(key=lambda x: (kind_order.get(x["kind"], 9), x["num"]))

    for it in items:
        if it["kind"] == "EP":
            it["label"] = f"EP{it['num']:02d}"
        elif it["kind"] == "OVA":
            it["label"] = f"OVA{it['num']}"
        else:
            it["label"] = f"SP{it['num']}"

    return {"title": title, "season": season, "mode": "episodes", "items": items}


def _kb(rows):
    """rows = list of list of (text, callback_data) tuples"""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(t, callback_data=d) for t, d in row] for row in rows]
    )


def _prune_sessions():
    now = time.time()
    stale = [k for k, v in RTI_SESSIONS.items() if now - v.created > RTI_SESSION_TIMEOUT]
    for k in stale:
        RTI_SESSIONS.pop(k, None)


class RTISelector:
    """
    /rti <url> ke liye interactive button menu:
    Season header, EPxx grid (paginated), single-tap download,
    ya "Select Multiple" ON karke checkmark se multi-episode select
    karke ek saath download.
    """

    def __init__(self, page_url: str, data: dict, orig_message: Message):
        self.page_url = page_url
        self.title = data["title"]
        self.season = data["season"]
        self.mode = data["mode"]
        self.items = data["items"]
        self.orig_message = orig_message  # asli command bhejne wala user message
        self.sid = None
        self.msg = None  # selector card Message
        self.page = 0
        self.multi = False
        self.selected = set()
        self.created = time.time()
        # Cached toggle states — populate_toggles() se load hote hain
        # session banate waqt, taaki build_markup() sync reh sake.
        self.update_toggle = False
        self.channel_toggle = False

    async def populate_toggles(self):
        self.update_toggle = await _get_update_toggle()
        self.channel_toggle = await _get_rti_channel_upload(self.orig_message.from_user.id)

    @property
    def total_pages(self):
        return max(1, (len(self.items) + PER_PAGE - 1) // PER_PAGE)

    def page_items(self):
        start = self.page * PER_PAGE
        return list(enumerate(self.items))[start:start + PER_PAGE]

    def header_text(self):
        counts = {"EP": 0, "OVA": 0, "SP": 0}
        for it in self.items:
            if it["kind"] in counts:
                counts[it["kind"]] += 1

        lines = [
            "📂 **Select Episode**",
            "━━━━━━━━━━━━━━━━━━",
            f"🎬 **{self.title}**",
            "🌐 Website: 🎭 RareAnimes",
        ]

        if self.mode == "movie":
            lines.append("🍿 Type: Movie")
        else:
            info = f"🏝️ Season: {self.season} • 📖 Episodes: {counts['EP']}"
            if counts["OVA"]:
                info += f" • 🎞 OVA: {counts['OVA']}"
            if counts["SP"]:
                info += f" • ✨ Special: {counts['SP']}"
            lines.append(info)

        lines.append("━━━━━━━━━━━━━━━━━━")
        if self.mode == "movie":
            lines.append("👇 Tap the button to download:")
        else:
            lines.append(f"👇 Tap an episode to download:  (Page {self.page + 1}/{self.total_pages})")

        if self.multi:
            lines.append(f"\n☑️ **Multi-select ON** — chosen: `{len(self.selected)}`")

        return "\n".join(lines)

    def build_markup(self):
        sid = self.sid
        rows = []

        if self.mode == "movie":
            mark = "✅ " if 0 in self.selected else "🎬 "
            rows.append([(f"{mark}Movie", f"rti_ep_{sid}_0")])
        else:
            rows.append([(f"🏝️ SEASON {self.season:02d}", f"rti_noop_{sid}")])

            row = []
            for idx, item in self.page_items():
                mark = "✅ " if idx in self.selected else ""
                row.append((f"{mark}{item['label']}", f"rti_ep_{sid}_{idx}"))
                if len(row) == 3:
                    rows.append(row)
                    row = []
            if row:
                rows.append(row)

            if self.total_pages > 1:
                nav = []
                if self.page > 0:
                    nav.append(("◀️ Prev", f"rti_pg_{sid}_{self.page - 1}"))
                nav.append((f"📄 {self.page + 1}/{self.total_pages}", f"rti_noop_{sid}"))
                if self.page < self.total_pages - 1:
                    nav.append(("Next ▶️", f"rti_pg_{sid}_{self.page + 1}"))
                rows.append(nav)

            if self.multi:
                rows.append([(f"⬇️ Download Selected ({len(self.selected)})", f"rti_dl_{sid}")])
                rows.append([("☑️ Multi-select: ON (tap to turn off)", f"rti_toggle_{sid}")])
            else:
                rows.append([("☑️ Select Multiple", f"rti_toggle_{sid}")])

        rows.append([
            (f"📢 Update Post: {'ON' if self.update_toggle else 'OFF'}", f"rti_uptog_{sid}"),
        ])
        rows.append([
            (f"📤 Channel Upload: {'ON' if self.channel_toggle else 'OFF'}", f"rti_chtog_{sid}"),
        ])
        rows.append([("❌ Close", f"rti_close_{sid}")])
        return _kb(rows)

    async def render(self):
        if not self.msg:
            return
        try:
            await self.msg.edit(self.header_text(), reply_markup=self.build_markup())
        except Exception as e:
            LOGGER.error(f"[RTI] selector render error: {e}")


# ─────────────────────────────────────────────
#  NEW: Latest episode number nikalo
# ─────────────────────────────────────────────
def get_latest_episode(page_url: str):
    """
    Page ke sabse latest (highest number) episode detect karo.
    Returns: (latest_ep_num, anime_title) or (None, None)
    """
    try:
        r = requests.get(page_url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.content, "html.parser")

        title_tag = soup.find("h1", class_="entry-title")
        anime_title = title_tag.text.strip() if title_tag else "Unknown Anime"

        # Saare episode numbers collect karo
        ep_numbers = []
        for p in soup.find_all("p"):
            text = p.get_text(" ", strip=True)
            match = re.search(r"Episode\s*(\d+)", text, re.IGNORECASE)
            if match:
                ep_numbers.append(int(match.group(1)))

        if not ep_numbers:
            LOGGER.warning("[RTI] No episodes found on page")
            return None, None

        latest = max(ep_numbers)
        LOGGER.info(f"[RTI] Latest episode detected: {latest}")
        return latest, anime_title

    except Exception as e:
        LOGGER.error(f"[RTI] get_latest_episode error: {e}")
        return None, None


# ─────────────────────────────────────────────
#  Step 1: Page → WatchMultQuality link
# ─────────────────────────────────────────────
def get_watchmult_link(page_url: str, episode_num: int):
    try:
        r = requests.get(page_url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.content, "html.parser")

        title_tag = soup.find("h1", class_="entry-title")
        anime_title = title_tag.text.strip() if title_tag else "Unknown Anime"

        # ── MOVIE MODE (episode_num == 0) ──
        # Movies ke page pe "Episode N" jaisa koi text hota hi nahi
        # (seedha "Hindi – Download" ke baad "WatchMultiQuality" heading
        # aa jaati hai). Aise pages ke liye episode number dhundhne ki
        # koshish karna hi galat hai — /rti <url> 0 0 use karo, tab
        # seedha poore page pe jo bhi WatchMultQuality link mile
        # (audio-priority ke hisaab se best) utha lo, episode text
        # match kiye bina.
        if episode_num == 0:
            all_links = _find_wmq_links(soup)
            best = _best_link(all_links)
            if best:
                return best["href"], anime_title
            return None, None

        for p in soup.find_all("p"):
            text = p.get_text(" ", strip=True)
            match = re.search(r"Episode\s*(\d+)", text, re.IGNORECASE)
            if match and int(match.group(1)) == episode_num:
                all_links = list(_find_wmq_links(p))
                for idx, sibling in enumerate(p.find_next_siblings()):
                    if idx > 8:
                        break
                    sib_text = sibling.get_text(" ", strip=True)
                    ep_match = re.search(r"Episode\s*(\d+)", sib_text, re.IGNORECASE)
                    if ep_match and int(ep_match.group(1)) != episode_num:
                        break
                    all_links.extend(_find_wmq_links(sibling))

                best = _best_link(all_links)
                if best:
                    return best["href"], anime_title

        return None, None
    except Exception as e:
        LOGGER.error(f"[RTI] Page scrape error: {e}")
        return None, None


# ─────────────────────────────────────────────
#  Step 2: WatchMultQuality -> Argon embed link
# ─────────────────────────────────────────────
def _make_selenium_driver():
    # ── Har session ka apna disposable profile dir ──
    # Chrome ko --user-data-dir diye bina chalane pe woh khud /tmp mein
    # ek random profile folder banata hai. Normal case mein quit() pe
    # khud clean kar deta hai, lekin agar Railway ke constrained container
    # mein renderer crash ho jaaye ya OOM aa jaaye, profile folder /tmp
    # mein hi reh jaata hai. get_argon_link 1 episode ke 30 retry attempts
    # tak baar-baar call hota hai — thodi si bhi leak rate ke saath yeh
    # kuch hi episodes mein /tmp/storage bhar deta hai aur bot stuck ho
    # jaata hai jab tak redeploy na ho.
    # Fix: apna hi disposable dir do (download_dir ke andar) taaki humein
    # pata ho iska exact path — aur cleanup (_kill_driver_tree) mein
    # explicitly delete kar dein, Chrome ke bharose na rahe.
    profile_dir = os.path.join(download_dir, "_chrome_tmp", f"rti_{uuid.uuid4().hex}")
    os.makedirs(profile_dir, exist_ok=True)

    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument(f"user-agent={HEADERS['User-Agent']}")
    options.add_argument(f"--user-data-dir={profile_dir}")
    options.add_argument("--disable-crash-reporter")
    options.add_experimental_option("prefs", {
        "profile.managed_default_content_settings.images": 2
    })
    options.page_load_strategy = "eager"
    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(40)
    # Cleanup ke liye profile_dir ko driver pe hi attach kar do
    driver._suhani_profile_dir = profile_dir
    return driver


def _kill_driver_tree(driver):
    """
    driver.quit() bhaunsa/fail ho jaaye (renderer crash, hung tab, etc.)
    to bhi chromedriver + chrome + unke saare child processes ko
    forcefully kill karo, aur uska disposable profile dir bhi delete
    karo. Warna zombie chrome processes RAM/fd jama karte rehte hain
    aur kuch episodes ke baad bot response dena band kar deta hai —
    sirf redeploy (fresh process) se hi theek hota hai.
    """
    if not driver:
        return

    pid = None
    try:
        pid = driver.service.process.pid
    except Exception:
        pid = None

    try:
        driver.quit()
    except Exception:
        pass

    if pid:
        try:
            import psutil
            if psutil.pid_exists(pid):
                proc = psutil.Process(pid)
                for child in proc.children(recursive=True):
                    try:
                        child.kill()
                    except Exception:
                        pass
                try:
                    proc.kill()
                except Exception:
                    pass
        except Exception:
            pass

    profile_dir = getattr(driver, "_suhani_profile_dir", None)
    if profile_dir:
        shutil.rmtree(profile_dir, ignore_errors=True)


def _extract_argon_from_iframes(driver):
    try:
        soup = BeautifulSoup(driver.page_source, "html.parser")
        for iframe in soup.find_all("iframe"):
            src = iframe.get("src", "")
            if ARGON_DOMAIN in src:
                return src

        matches = re.findall(
            r'https?://argon\.razorshell\.space/embed/[A-Za-z0-9_-]+',
            driver.page_source
        )
        if matches:
            return matches[0]

        for iframe in driver.find_elements(By.TAG_NAME, "iframe"):
            src = iframe.get_attribute("src") or ""
            if ARGON_DOMAIN in src:
                return src
    except Exception as e:
        LOGGER.error(f"[RTI] Argon extract error: {e}")
    return None


def _close_popups(driver, main_window):
    try:
        if len(driver.window_handles) > 1:
            for handle in driver.window_handles:
                if handle != main_window:
                    driver.switch_to.window(handle)
                    driver.close()
            driver.switch_to.window(main_window)
    except Exception:
        pass


def get_argon_link(watchmult_url: str):
    driver = None
    try:
        driver = _make_selenium_driver()
        driver.get(watchmult_url)
        main = driver.current_window_handle
        # Pehle 2s mein hi argon iframe aa jata hai mostly
        time.sleep(2)
        driver.execute_script("window.stop();")
        _close_popups(driver, main)

        argon = _extract_argon_from_iframes(driver)
        if argon:
            return argon

        try:
            wait = WebDriverWait(driver, 8)
            for btn_text in ["Get Download Link", "Download", "Get Link", "Click Here"]:
                try:
                    btn = wait.until(EC.element_to_be_clickable(
                        (By.XPATH, f"//a[contains(translate(text(),'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ'), '{btn_text.upper()}')]")
                    ))
                    btn.click()
                    time.sleep(2)
                    _close_popups(driver, main)
                    argon = _extract_argon_from_iframes(driver)
                    if argon:
                        return argon
                    break
                except Exception:
                    continue
        except Exception:
            pass

        return None
    except Exception as e:
        LOGGER.error(f"[RTI] get_argon_link error: {e}")
        return None
    finally:
        _kill_driver_tree(driver)


# ─────────────────────────────────────────────
#  Step 3: Argon -> Swift URL
# ─────────────────────────────────────────────
def argon_to_swift(argon_url: str):
    try:
        parts = [p for p in argon_url.rstrip("/").split("/") if p]
        unique_code = parts[-1]
        if len(unique_code) < 5:
            return None
        return SWIFT_BASE + unique_code
    except Exception as e:
        LOGGER.error(f"[RTI] argon_to_swift error: {e}")
        return None


# ─────────────────────────────────────────────
#  Step 4: Download + Sequential upload
# ─────────────────────────────────────────────
async def _run_rti_swift(client, message: Message, swift_url: str, status_msg, ep_num: int, total_eps: int):
    from .swift_downloader import _run_swift
    ep_label = "Movie" if ep_num == 0 else f"Ep {ep_num}/{total_eps}"
    # /swift wala exact flow use karo — download + queued messages + sequential upload
    # show_url=False: DM mein swift_url kabhi nahi dikhega, sirf status (downloading/
    # uploading/quality/episode) — episode_label se pata chalega kaunsa episode chal raha hai
    uploaded_results = await _run_swift(
        client, message, swift_url, encode=False, episode_label=ep_label, show_url=False
    )
    await _forward_to_channel_if_enabled(client, message, uploaded_results)
    return True


async def _forward_to_channel_if_enabled(client, message: Message, uploaded_results):
    """
    "📤 Channel Upload" toggle ON hai to har uploaded quality ko user ke
    pehle se /addchannel se add kiye gaye channel pe bhi copy kar do.
    Bot chat mein upload waise hi normal rehta hai — yeh sirf ek extra copy hai.
    """
    if not uploaded_results:
        return
    try:
        user_id = message.from_user.id
        enabled = await _get_rti_channel_upload(user_id)
        if not enabled:
            return
        channels = await db.get_channels(user_id)
        if not channels:
            return
        target_id = channels[0].get("channel_id")
        if not target_id:
            return
        for _quality, sent_msg in uploaded_results:
            try:
                await client.copy_message(
                    chat_id=target_id, from_chat_id=sent_msg.chat.id, message_id=sent_msg.id
                )
            except Exception as e:
                LOGGER.error(f"[RTI] Channel copy error: {e}")
    except Exception as e:
        LOGGER.error(f"[RTI] _forward_to_channel_if_enabled error: {e}")

async def _process_episode(client, message, page_url, episode_num, total_episodes, status_msg):
    """
    Ek episode ke liye Swift URL nikalo aur download+upload karo.

    Return values:
      "ok"       → success, agle episode pe jao
      "retry"    → sabhi retries ke baad bhi Swift URL nahi mila → BAND karo
      "error"    → download/upload mein exception → BAND karo
    """
    loop = asyncio.get_event_loop()

    # Movie mode (episode_num == 0) mein status messages "Ep 0" ki jagah
    # "Movie" dikhayenge — zyada readable hai
    ep_label = "Movie" if episode_num == 0 else f"Ep {episode_num}"

    # ── Swift URL nikalne ke liye retry constants ──
    SWIFT_MAX_ATTEMPTS   = 5    # 5 baar try karo
    SWIFT_RETRY_INTERVAL = 60   # har attempt ke beech 60s wait

    swift_url = None

    for attempt in range(1, SWIFT_MAX_ATTEMPTS + 1):
        # Step 1: WatchMultQuality link
        try:
            await status_msg.edit(
                f"🔍 **{ep_label}/{total_episodes}**\n"
                f"Attempt `{attempt}/{SWIFT_MAX_ATTEMPTS}` — WatchMultQuality link dhundh raha hoon..."
            )
        except Exception:
            pass

        wmq_link, _ = await loop.run_in_executor(None, get_watchmult_link, page_url, episode_num)

        if wmq_link:
            # Step 2: Argon link
            try:
                await status_msg.edit(
                    f"🔍 **{ep_label}/{total_episodes}**\n"
                    f"Attempt `{attempt}/{SWIFT_MAX_ATTEMPTS}` — Argon link extract ho raha hai..."
                )
            except Exception:
                pass

            argon_link = await loop.run_in_executor(None, get_argon_link, wmq_link)

            if argon_link:
                # Step 3: Swift URL
                swift_url = argon_to_swift(argon_link)
                if swift_url:
                    break  # ✅ Swift URL mil gaya — loop se bahar

        # Yahan tak matlab kuch na kuch fail hua
        if attempt == SWIFT_MAX_ATTEMPTS:
            # Sabhi attempts khatam — HARD STOP signal bhejo
            try:
                await status_msg.edit(
                    f"🛑 **{ep_label} — Complete Fail**\n\n"
                    f"❌ `{SWIFT_MAX_ATTEMPTS}` attempts ke baad bhi Swift URL nahi mila.\n"
                    f"⛔ Agle episodes **band** kar diye gaye.\n"
                    f"RTI pe manually check karo."
                )
            except Exception:
                pass
            return "retry"

        # Retry message
        remaining = SWIFT_MAX_ATTEMPTS - attempt
        try:
            await status_msg.edit(
                f"⏳ **{ep_label}/{total_episodes}**\n\n"
                f"🔄 Attempt `{attempt}/{SWIFT_MAX_ATTEMPTS}` fail — link nahi mila\n"
                f"⏰ `{SWIFT_RETRY_INTERVAL}s` baad retry... (`{remaining}` attempts baki)"
            )
        except Exception:
            pass

        await asyncio.sleep(SWIFT_RETRY_INTERVAL)

    # ── Swift URL mil gaya — download + upload ──
    try:
        await status_msg.edit(
            f"✅ **{ep_label}/{total_episodes} — Link mil gaya**\n\n"
            f"⬇️ Ab download shuru ho raha hai..."
        )
        await _run_rti_swift(client, message, swift_url, status_msg, ep_num=episode_num, total_eps=total_episodes)
        return "ok"
    except Exception as e:
        LOGGER.error(f"[RTI] {ep_label} download/upload error: {e}")
        try:
            await status_msg.edit(
                f"🛑 **{ep_label} — Upload Error**\n\n"
                f"❌ `{str(e)[:120]}`\n"
                f"⛔ Agle episodes **band** kar diye gaye."
            )
        except Exception:
            pass
        return "error"


# ─────────────────────────────────────────────
#  NEW: Selector se cached WatchMultQuality link ke saath download
#  (page dubara scrape nahi karna — link already discover_items() se
#  mil chuka hai, seedha argon → swift step se shuru karo)
# ─────────────────────────────────────────────
async def _process_cached_item(client, message, item, status_msg, index, total):
    loop = asyncio.get_event_loop()
    ep_label = item["label"]
    wmq_link = item["wmq_link"]

    SWIFT_MAX_ATTEMPTS = 5
    SWIFT_RETRY_INTERVAL = 60
    swift_url = None

    for attempt in range(1, SWIFT_MAX_ATTEMPTS + 1):
        try:
            await status_msg.edit(
                f"🔍 **{ep_label}** (`{index}/{total}`)\n"
                f"Attempt `{attempt}/{SWIFT_MAX_ATTEMPTS}` — Argon link extract ho raha hai..."
            )
        except Exception:
            pass

        argon_link = await loop.run_in_executor(None, get_argon_link, wmq_link)
        if argon_link:
            swift_url = argon_to_swift(argon_link)
            if swift_url:
                break

        if attempt == SWIFT_MAX_ATTEMPTS:
            try:
                await status_msg.edit(
                    f"🛑 **{ep_label} — Complete Fail**\n\n"
                    f"❌ `{SWIFT_MAX_ATTEMPTS}` attempts ke baad bhi Swift URL nahi mila.\n"
                    f"⛔ Agle items **band** kar diye gaye.\n"
                    f"RTI pe manually check karo."
                )
            except Exception:
                pass
            return "retry"

        remaining = SWIFT_MAX_ATTEMPTS - attempt
        try:
            await status_msg.edit(
                f"⏳ **{ep_label}** (`{index}/{total}`)\n\n"
                f"🔄 Attempt `{attempt}/{SWIFT_MAX_ATTEMPTS}` fail — link nahi mila\n"
                f"⏰ `{SWIFT_RETRY_INTERVAL}s` baad retry... (`{remaining}` attempts baki)"
            )
        except Exception:
            pass

        await asyncio.sleep(SWIFT_RETRY_INTERVAL)

    try:
        await status_msg.edit(
            f"✅ **{ep_label}** (`{index}/{total}`) — Link mil gaya\n\n"
            f"⬇️ Ab download shuru ho raha hai..."
        )
        ep_num_for_swift = 0 if item["kind"] == "MOVIE" else item["num"]
        await _run_rti_swift(client, message, swift_url, status_msg, ep_num=ep_num_for_swift, total_eps=total)
        return "ok"
    except Exception as e:
        LOGGER.error(f"[RTI] {ep_label} download/upload error: {e}")
        try:
            await status_msg.edit(
                f"🛑 **{ep_label} — Upload Error**\n\n"
                f"❌ `{str(e)[:120]}`\n"
                f"⛔ Agle items **band** kar diye gaye."
            )
        except Exception:
            pass
        return "error"


async def _download_items(client, status_msg, orig_message, sess: "RTISelector", idxs: list):
    total = len(idxs)
    for i, idx in enumerate(idxs, 1):
        item = sess.items[idx]
        result = await _process_cached_item(client, orig_message, item, status_msg, i, total)
        if result != "ok":
            break
        if i < total:
            await asyncio.sleep(3)

    try:
        await status_msg.delete()
    except Exception:
        pass


# ─────────────────────────────────────────────
#  NEW: Episode selector ke buttons ka callback handler
# ─────────────────────────────────────────────
@Client.on_callback_query(filters.regex(r"^rti_"))
async def rti_callback_handler(client: Client, cb: CallbackQuery):
    try:
        parts = cb.data.split("_")
        action = parts[1] if len(parts) > 1 else ""
        sid = parts[2] if len(parts) > 2 else None
        sess = RTI_SESSIONS.get(sid)

        if not sess:
            await cb.answer("⌛ Session expired, /rti <url> dubara bhejo.", show_alert=True)
            return

        if action == "noop":
            await cb.answer()
            return

        if action == "close":
            RTI_SESSIONS.pop(sid, None)
            await cb.answer()
            try:
                await cb.message.delete()
            except Exception:
                pass
            return

        if action == "toggle":
            sess.multi = not sess.multi
            if not sess.multi:
                sess.selected.clear()
            await cb.answer("Multi-select " + ("ON" if sess.multi else "OFF"))
            await sess.render()
            return

        if action == "uptog":
            new_val = not sess.update_toggle
            await _set_update_toggle(new_val)
            sess.update_toggle = new_val
            await cb.answer("📢 Update Post " + ("ON" if new_val else "OFF"))
            await sess.render()
            return

        if action == "chtog":
            new_val = not sess.channel_toggle
            if new_val:
                try:
                    channels = await db.get_channels(sess.orig_message.from_user.id)
                except Exception as e:
                    LOGGER.error(f"[RTI] channel lookup error: {e}")
                    channels = []
                if not channels:
                    await cb.answer(
                        "❌ Pehle /addchannel se ek channel add karo!", show_alert=True
                    )
                    return
            await _set_rti_channel_upload(sess.orig_message.from_user.id, new_val)
            sess.channel_toggle = new_val
            await cb.answer("📤 Channel Upload " + ("ON" if new_val else "OFF"))
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
            else:
                await cb.answer()
                RTI_SESSIONS.pop(sid, None)
                status_msg = cb.message
                try:
                    await status_msg.edit(f"🎌 **RTI** — Starting `{sess.items[idx]['label']}`...")
                except Exception:
                    pass
                await _download_items(client, status_msg, sess.orig_message, sess, [idx])
            return

        if action == "dl":
            if not sess.selected:
                await cb.answer("Pehle kuch episode select karo!", show_alert=True)
                return
            idxs = sorted(sess.selected)
            RTI_SESSIONS.pop(sid, None)
            await cb.answer()
            status_msg = cb.message
            try:
                await status_msg.edit(f"🎌 **RTI** — Starting `{len(idxs)}` selected item(s)...")
            except Exception:
                pass
            await _download_items(client, status_msg, sess.orig_message, sess, idxs)
            return

        await cb.answer()
    except Exception as e:
        LOGGER.error(f"[RTI] callback error: {e}")
        try:
            await cb.answer("❌ Error, dubara try karo.", show_alert=True)
        except Exception:
            pass


# ─────────────────────────────────────────────
#  /rti Command Handler
# ─────────────────────────────────────────────
@Client.on_message(filters.command("rti"))
async def rti_command(client: Client, message: Message):
    """
    /rti <url>               -> Latest episode auto-download
    /rti <url> <start> <end> -> Episode range
    /rti <url> 5 5           -> Sirf episode 5
    """
    c = await check_chat(message, chat="Sudo")
    if not c:
        return

    if not SELENIUM_OK:
        await message.reply("❌ Selenium install nahi hai! `pip install selenium`")
        return

    parts = message.text.split()

    # Minimum: /rti <url>
    if len(parts) < 2:
        await message.reply(
            "**Usage:**\n"
            "`/rti <url>` — Button menu khulega (season/episode select karo)\n"
            "`/rti <url> <start> <end>` — Episode range (bina button ke, direct)\n"
            "`/rti <url> 0 0` — Movie mode (no episode number on page)\n\n"
            "**Examples:**\n"
            "`/rti https://rareanimes.buzz/wistoria/` — Button menu\n"
            "`/rti https://rareanimes.buzz/wistoria/ 01 10` — Ep 1 to 10\n"
            "`/rti https://rareanimes.buzz/wistoria/ 5 5` — Sirf Ep 5\n"
            "`/rti https://rareanimes.mov/a-magnificent-life/ 0 0` — Movie"
        )
        return

    page_url = parts[1].strip()

    if not page_url.startswith("http"):
        await message.reply("❌ Valid URL dalo.")
        return

    # ── BUTTON MENU MODE: sirf URL diya, koi number nahi ──
    if len(parts) == 2:
        status_msg = await message.reply("🔍 Page scan ho raha hai...")

        loop = asyncio.get_event_loop()
        try:
            data = await loop.run_in_executor(None, discover_items, page_url)
        except Exception as e:
            LOGGER.error(f"[RTI] discover_items error: {e}")
            await status_msg.edit(f"❌ Page load nahi hua: `{str(e)[:100]}`")
            return

        if not data["items"]:
            await status_msg.edit("❌ Page se koi episode/movie link nahi mila. URL check karo.")
            return

        _prune_sessions()
        sess = RTISelector(page_url, data, orig_message=message)
        sess.sid = uuid.uuid4().hex[:10]
        await sess.populate_toggles()
        RTI_SESSIONS[sess.sid] = sess

        try:
            await status_msg.delete()
        except Exception:
            pass

        sess.msg = await message.reply(sess.header_text(), reply_markup=sess.build_markup())
        return

    # ── RANGE MODE: /rti <url> <start> <end> ──
    if len(parts) < 4:
        await message.reply(
            "❌ Range ke liye do numbers chahiye.\n"
            "Example: `/rti <url> 1 10`"
        )
        return

    try:
        start_ep = int(parts[2])
        end_ep   = int(parts[3])
    except ValueError:
        await message.reply("❌ Episode number valid nahi.\nExample: `/rti <url> 1 10`")
        return

    if start_ep > end_ep:
        await message.reply("❌ Start > End nahi ho sakta.")
        return

    if end_ep - start_ep > 50:
        await message.reply("❌ Max 50 episodes ek baar mein.")
        return

    total_eps    = end_ep - start_ep + 1
    episode_list = list(range(start_ep, end_ep + 1))

    status_msg = await message.reply(
        f"🎌 **RTI** — Ep `{start_ep}` to `{end_ep}` (Total: `{total_eps}`)\n"
        f"⏳ Starting..."
    )

    success_count = 0
    for i, ep_num in enumerate(episode_list, 1):
        result = await _process_episode(
            client, message, page_url,
            episode_num=ep_num,
            total_episodes=total_eps,
            status_msg=status_msg,
        )

        if result == "ok":
            success_count += 1
        else:
            # "retry" ya "error" — dono cases mein HARD STOP
            LOGGER.warning(f"[RTI] Ep {ep_num} failed ({result}). Stopping range.")
            break

        if i < total_eps:
            await asyncio.sleep(3)

    try:
        await status_msg.delete()
    except Exception:
        pass
