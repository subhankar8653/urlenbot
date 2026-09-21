"""
toono_downloader.py  v1
========================
Commands:
  /toono <series_url>  -> Season button menu -> episode button menu
                           -> har episode "Watch SxEy" page se hoke
                           codedew.com ke MultiQuality (multi-quality)
                           download page tak Selenium se click-chain
                           follow karta hai, phir swift_downloader.py
                           ke already-generic _run_swift() ko wahi
                           final URL de deta hai (jaisa /rti Argon->Swift
                           URL deta hai) — download+upload ka poora kaam
                           _run_swift khud kar leta hai, kyunki uska
                           scraper (_collect_visible_links/_quality_from)
                           kisi bhi "360p/720p/1080p labelled links wale
                           page" pe already generic tarike se kaam karta
                           hai, sirf swift.the-vipers pe hi nahi.

NOTE: toono.app ka series/episode page static HTML hai (requests+BS4
se seedha scrape ho jaata hai — verified). Lekin episode page ka
"Download" button aur uske baad khulne wala modal poori tarah JS-driven
hai (href="#" / href="#!") — asli codedew.com link sirf button click
karne ke baad JS se milta hai. Isliye woh hissa (get_codedew_link)
Selenium se hi ho sakta hai, RTI ke get_argon_link jaisa hi resilient
retry/click-chain use karta hai. Yeh part live site pe test/tune karna
padega (isi conversation mein isko run karke dekhne ka koi tareeka
nahi tha), isliye har step pe LOGGER.info/warning daala hai taaki
selector fail ho to logs se turant pata chal jaaye kahan atka.
"""

import asyncio
import re
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

from .. import LOGGER
from ..utils.helper import check_chat
from ..utils.database.access_db import db
from .rti_downloader import (
    SELENIUM_OK,
    _make_selenium_driver,
    _kill_driver_tree,
    _kb,
)

try:
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
except ImportError:
    pass

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

CODEDEW_DOMAIN = "codedew.com"
# Hindi ko sabse pehli priority — user ne explicitly maanga hai
AUDIO_PRIORITY = ["hindi", "dual", "multi", "tamil", "telugu", "english", "japanese", "sub", "unknown"]

PER_PAGE = 10  # RTI jaisa hi — 10 episode buttons per page

# sid -> ToonoSelector. In-memory hai, redeploy pe clear ho jaata hai.
TOONO_SESSIONS = {}
TOONO_SESSION_TIMEOUT = 3600  # 1 hour


# ─────────────────────────────────────────────
#  "Toono Channel Upload" toggle — per user save hota hai (RTI ke
#  rti_channel_upload jaisa hi pattern, bilkul alag field taaki RTI aur
#  Toono ka apna-apna independent state rahe).
# ─────────────────────────────────────────────
async def _get_toono_channel_upload(user_id: int) -> bool:
    user = await db._get_user(user_id)
    return user.get("toono_channel_upload", False)


async def _set_toono_channel_upload(user_id: int, enabled: bool):
    await db.col.update_one({"id": int(user_id)}, {"$set": {"toono_channel_upload": enabled}}, upsert=True)


def _prune_toono_sessions():
    now = time.time()
    stale = [k for k, v in TOONO_SESSIONS.items() if now - v.created > TOONO_SESSION_TIMEOUT]
    for k in stale:
        TOONO_SESSIONS.pop(k, None)


# ─────────────────────────────────────────────
#  Step 1: series page -> seasons/episodes (static HTML, requests+BS4)
# ─────────────────────────────────────────────
def _detect_audio_toono(a_tag) -> str:
    """
    "Watch SxEy" link ke turant pehle jo audio-label text hota hai
    (e.g. "Hindi", "Tamil") usse dhundo — RTI ke _detect_audio jaisa
    hi, sirf toono ke language set (Hindi/Tamil/Telugu/English) ke saath.
    """
    prev = a_tag.previous_sibling
    hops = 0
    while prev is not None and hops < 5:
        hops += 1
        text = str(prev).strip() if isinstance(prev, str) else prev.get_text(strip=True)
        if text:
            t = text.lower()
            for kw, label in [
                ("hindi", "hindi"), ("dual", "dual"), ("multi", "multi"),
                ("tamil", "tamil"), ("telugu", "telugu"),
                ("english", "english"), ("japanese", "japanese"), ("sub", "sub"),
            ]:
                if kw in t:
                    return label
            break
        prev = prev.previous_sibling

    parent = a_tag.find_parent()
    if parent:
        pt = parent.get_text(" ", strip=True).lower()
        lt = a_tag.get_text(strip=True).lower()
        if lt in pt:
            before = pt.split(lt)[0][-40:]
            for kw, label in [
                ("hindi", "hindi"), ("dual", "dual"), ("multi", "multi"),
                ("tamil", "tamil"), ("telugu", "telugu"),
                ("english", "english"), ("japanese", "japanese"), ("sub", "sub"),
            ]:
                if kw in before:
                    return label
    return "unknown"


def discover_toono_items(series_url: str):
    """
    Returns:
      {"title": str, "seasons": {season_num: [ {"season","num","title","watch_url"}, ... ]}}

    Detection text-pattern based hai (class names pe depend nahi karta,
    RTI ke OVA/Episode regex jaisa hi philosophy): har "Watch SxEy" link
    dhundo (site ka apna hi visible text hai, e.g. "Watch S1E1"), season
    aur episode number uske text se hi nikal lo. Har (season, ep) ke
    multiple audio options ho sakte hain (Hindi/Tamil/Telugu alag links)
    — un sabme se Hindi-priority wala best pick karo.
    """
    r = requests.get(series_url, headers=HEADERS, timeout=15)
    r.raise_for_status()
    soup = BeautifulSoup(r.content, "html.parser")

    title_tag = soup.find("h1")
    title = title_tag.get_text(strip=True) if title_tag else "Unknown Anime"
    title = re.sub(r"\s*[—\-]\s*Hindi Watch/Download\s*$", "", title, flags=re.IGNORECASE).strip()

    grouped = {}    # (season, ep) -> list of {"href","audio"}
    ep_titles = {}  # (season, ep) -> title text

    watch_links = soup.find_all("a", string=re.compile(r"^\s*Watch\s*S\d+E\d+\s*$", re.IGNORECASE))
    for a in watch_links:
        m = re.search(r"S(\d+)E(\d+)", a.get_text(strip=True), re.IGNORECASE)
        if not m:
            continue
        key = (int(m.group(1)), int(m.group(2)))
        href = a.get("href", "")
        if not href:
            continue

        audio = _detect_audio_toono(a)
        grouped.setdefault(key, []).append({"href": href, "audio": audio})

        if key not in ep_titles:
            heading = a.find_previous(["h2", "h3", "h4"])
            if heading:
                htext = heading.get_text(strip=True)
                htext = re.sub(r"^S\d+[\-\s]E\d+\s*", "", htext, flags=re.IGNORECASE).strip()
                ep_titles[key] = htext or f"Episode {key[1]}"
            else:
                ep_titles[key] = f"Episode {key[1]}"

    seasons = {}
    for (season_num, ep_num), links in grouped.items():
        best = None
        for priority in AUDIO_PRIORITY:
            for lnk in links:
                if lnk["audio"] == priority:
                    best = lnk
                    break
            if best:
                break
        if not best:
            best = links[0]

        seasons.setdefault(season_num, []).append({
            "season": season_num,
            "num": ep_num,
            "title": ep_titles.get((season_num, ep_num), f"Episode {ep_num}"),
            "watch_url": best["href"],
        })

    for s in seasons.values():
        s.sort(key=lambda x: x["num"])

    return {"title": title, "seasons": seasons}


# ─────────────────────────────────────────────
#  Step 2: episode page -> "Download" click -> modal "Hindi Download"
#  click -> codedew.com MultiQuality page (poori tarah Selenium, JS-driven)
# ─────────────────────────────────────────────
_CLICK_TEXTS = ["Continue", "Get Link", "Verify", "Click Here", "Get Download Link", "Download"]


def _xpath_text_click(word: str) -> str:
    up = (
        "translate(text(),"
        "'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ')"
    )
    W = word.upper()
    return (
        f"//a[contains({up}, '{W}')] | "
        f"//button[contains({up}, '{W}')]"
    )


def _handle_new_windows(driver, known_handles: set, main_handle):
    """
    Click ke baad agar naya tab/window khula ho to check karo:
      - codedew.com hai -> usi pe switch rehne do, URL return karo
      - kuch aur (ad/junk) hai -> band karke wapas main pe switch karo
    Returns codedew URL ya None.
    """
    try:
        handles = driver.window_handles
    except Exception:
        return None
    new_handles = [h for h in handles if h not in known_handles]
    if not new_handles:
        return None

    found = None
    for h in new_handles:
        try:
            driver.switch_to.window(h)
            cur = driver.current_url or ""
            if CODEDEW_DOMAIN in cur:
                found = cur
            else:
                driver.close()
        except Exception:
            pass

    try:
        if found:
            for h in driver.window_handles:
                driver.switch_to.window(h)
                if CODEDEW_DOMAIN in (driver.current_url or ""):
                    break
        else:
            driver.switch_to.window(main_handle)
    except Exception:
        pass
    return found


def get_codedew_link(episode_url: str):
    """
    Returns codedew.com ka final MultiQuality page URL, ya None agar
    kahin atak gaya (log mein exact step dikh jaayega).
    """
    driver = None
    try:
        driver = _make_selenium_driver()
        driver.get(episode_url)
        main = driver.current_window_handle
        known = set(driver.window_handles)
        time.sleep(2)

        wait = WebDriverWait(driver, 10)

        # ── Step A: episode page ka main "Download" button ──
        try:
            btn = wait.until(EC.element_to_be_clickable((By.XPATH, _xpath_text_click("Download"))))
            btn.click()
        except Exception as e:
            LOGGER.warning(f"[Toono] Episode page ka Download button nahi mila: {e}")
            return None

        time.sleep(1.5)

        # ── Step B: khule modal ke andar "Hindi" ke paas wala Download
        # button (ya, agar sirf ek hi language hai, generic Download) ──
        clicked = False
        try:
            hindi_btn = WebDriverWait(driver, 6).until(EC.element_to_be_clickable((
                By.XPATH,
                "//*[contains(text(),'Hindi')]/following::a["
                "contains(translate(text(),'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ'),'DOWNLOAD')][1] | "
                "//*[contains(text(),'Hindi')]/following::button["
                "contains(translate(text(),'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ'),'DOWNLOAD')][1]"
            )))
            hindi_btn.click()
            clicked = True
        except Exception:
            pass

        if not clicked:
            try:
                any_dl = WebDriverWait(driver, 6).until(EC.element_to_be_clickable((
                    By.XPATH,
                    "//div[contains(@class,'modal') or contains(@id,'modal') or contains(@class,'popup')]"
                    "//a[contains(translate(text(),'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ'),'DOWNLOAD')] | "
                    "//div[contains(@class,'modal') or contains(@id,'modal') or contains(@class,'popup')]"
                    "//button[contains(translate(text(),'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ'),'DOWNLOAD')]"
                )))
                any_dl.click()
                clicked = True
            except Exception as e:
                LOGGER.warning(f"[Toono] Modal ke andar Hindi/Download button nahi mila: {e}")
                return None

        # ── Step C: codedew.com tak resilient click-chain (RTI ke
        # get_argon_link jaisa hi — beech mein ad-gate/redirect page
        # aana normal hai, isliye retry karte raho) ──
        for attempt in range(8):
            time.sleep(3)

            found = _handle_new_windows(driver, known, main)
            if found:
                return found
            known = set(driver.window_handles)

            cur = driver.current_url or ""
            if CODEDEW_DOMAIN in cur:
                return cur

            try:
                html = driver.page_source
                m = re.search(r'https?://[^\s"\'<>]*codedew\.com[^\s"\'<>]*', html)
                if m:
                    return m.group(0)
            except Exception:
                pass

            for btn_text in _CLICK_TEXTS:
                try:
                    els = driver.find_elements(By.XPATH, _xpath_text_click(btn_text))
                    for el in els:
                        if el.is_displayed():
                            el.click()
                            time.sleep(0.5)
                            break
                except Exception:
                    continue

        cur = driver.current_url or ""
        if CODEDEW_DOMAIN in cur:
            return cur
        LOGGER.warning(f"[Toono] codedew.com tak nahi pahunche. Last URL: {cur}")
        return None

    except Exception as e:
        LOGGER.error(f"[Toono] get_codedew_link error: {e}")
        return None
    finally:
        _kill_driver_tree(driver)


# ─────────────────────────────────────────────
#  NEW: Season -> Episode button selector (/toono <url>)
# ─────────────────────────────────────────────
class ToonoSelector:
    def __init__(self, series_url: str, data: dict, orig_message: Message):
        self.series_url = series_url
        self.title = data["title"]
        self.seasons = data["seasons"]                      # {season_num: [items]}
        self.season_nums = sorted(self.seasons.keys())
        self.orig_message = orig_message
        self.sid = None
        self.msg = None
        self.current_season = None    # None = season list view
        self.page = 0
        self.multi = False
        self.selected = set()
        self.created = time.time()
        # "Update Post" — session-local (in-memory only), global
        # /updatechannel se bilkul independent — RTI wale fix jaisa hi.
        self.update_toggle = False
        self.channel_toggle = False

    async def populate_toggles(self):
        self.channel_toggle = await _get_toono_channel_upload(self.orig_message.from_user.id)

    @property
    def items(self):
        if self.current_season is None:
            return []
        return self.seasons.get(self.current_season, [])

    @property
    def total_pages(self):
        return max(1, (len(self.items) + PER_PAGE - 1) // PER_PAGE)

    def page_items(self):
        start = self.page * PER_PAGE
        return list(enumerate(self.items))[start:start + PER_PAGE]

    def header_text(self):
        lines = ["📂 **Select Episode**", "━━━━━━━━━━━━━━━━━━", f"🎬 **{self.title}**", "🌐 Website: 🍥 Toono"]

        if self.current_season is None:
            lines.append(f"📚 Seasons: {len(self.season_nums)}")
            lines.append("━━━━━━━━━━━━━━━━━━")
            lines.append("👇 Pehle ek Season chuno:")
        else:
            lines.append(f"🏝️ Season: {self.current_season} • 📖 Episodes: {len(self.items)}")
            lines.append("━━━━━━━━━━━━━━━━━━")
            lines.append(f"👇 Tap an episode to download:  (Page {self.page + 1}/{self.total_pages})")
            if self.multi:
                lines.append(f"\n☑️ **Multi-select ON** — chosen: `{len(self.selected)}`")

        return "\n".join(lines)

    def build_markup(self):
        sid = self.sid
        rows = []

        if self.current_season is None:
            row = []
            for s in self.season_nums:
                row.append((f"📀 Season {s}", f"toono_ssel_{sid}_{s}"))
                if len(row) == 2:
                    rows.append(row)
                    row = []
            if row:
                rows.append(row)
        else:
            rows.append([(f"🏝️ SEASON {self.current_season}", f"toono_noop_{sid}")])

            row = []
            for idx, item in self.page_items():
                mark = "✅ " if idx in self.selected else ""
                label = f"{mark}S{item['season']}E{item['num']:02d}"
                row.append((label, f"toono_ep_{sid}_{idx}"))
                if len(row) == 3:
                    rows.append(row)
                    row = []
            if row:
                rows.append(row)

            if self.total_pages > 1:
                nav = []
                if self.page > 0:
                    nav.append(("◀️ Prev", f"toono_pg_{sid}_{self.page - 1}"))
                nav.append((f"📄 {self.page + 1}/{self.total_pages}", f"toono_noop_{sid}"))
                if self.page < self.total_pages - 1:
                    nav.append(("Next ▶️", f"toono_pg_{sid}_{self.page + 1}"))
                rows.append(nav)

            if self.multi:
                rows.append([(f"⬇️ Download Selected ({len(self.selected)})", f"toono_dl_{sid}")])
                rows.append([("☑️ Multi-select: ON (tap to turn off)", f"toono_toggle_{sid}")])
            else:
                rows.append([("☑️ Select Multiple", f"toono_toggle_{sid}")])

            rows.append([("🔙 Back to Seasons", f"toono_back_{sid}")])

        rows.append([(f"📢 Update Post: {'ON' if self.update_toggle else 'OFF'}", f"toono_uptog_{sid}")])
        rows.append([(f"📤 Channel Upload: {'ON' if self.channel_toggle else 'OFF'}", f"toono_chtog_{sid}")])
        rows.append([("❌ Close", f"toono_close_{sid}")])
        return _kb(rows)

    async def render(self):
        if not self.msg:
            return
        try:
            await self.msg.edit(self.header_text(), reply_markup=self.build_markup())
        except Exception as e:
            LOGGER.error(f"[Toono] selector render error: {e}")


# ─────────────────────────────────────────────
#  Download+upload — codedew.com link milte hi swift_downloader.py ka
#  already-generic _run_swift() reuse karo (yeh kisi bhi "quality-labelled
#  download links wale page" pe kaam karta hai, sirf swift.the-vipers pe
#  hi nahi — 360p/720p/1080p text + href dhundhta hai, generic hai).
# ─────────────────────────────────────────────
async def _forward_to_toono_channel_if_enabled(client, message: Message, uploaded_results):
    if not uploaded_results:
        return
    try:
        user_id = message.from_user.id
        enabled = await _get_toono_channel_upload(user_id)
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
                LOGGER.error(f"[Toono] Channel copy error: {e}")
    except Exception as e:
        LOGGER.error(f"[Toono] _forward_to_toono_channel_if_enabled error: {e}")


async def _send_toono_update_post_if_enabled(client, sess: "ToonoSelector", item: dict, uploaded_results):
    """
    Sess ka "Update Post" toggle ON hai to us anime/episode ka post
    update channel pe bhejo — force=True se global /updatechannel toggle
    bypass hota hai (/rti wale fix jaisa hi), sirf isi /toono session
    tak seemit rehta hai.
    """
    if not sess or not sess.update_toggle or not uploaded_results:
        return
    try:
        from .update_channel import send_update_post
        await send_update_post(
            client,
            anime_name=sess.title,
            season=item.get("season"),
            episode=item.get("num"),
            force=True,
        )
    except Exception as e:
        LOGGER.error(f"[Toono] Update post error: {e}")


async def _process_toono_item(client, message, item: dict, status_msg, index: int, total: int,
                               sess: "ToonoSelector" = None):
    ep_label = f"S{item['season']}E{item['num']:02d}"

    try:
        await status_msg.edit(
            f"🔍 **{ep_label}** (`{index}/{total}`)\n"
            f"Download link nikal raha hoon (isme thoda time lag sakta hai, ruk jao)..."
        )
    except Exception:
        pass

    loop = asyncio.get_event_loop()
    codedew_url = await loop.run_in_executor(None, get_codedew_link, item["watch_url"])

    if not codedew_url:
        try:
            await status_msg.edit(
                f"🛑 **{ep_label} — Link Fail**\n\n"
                f"❌ codedew.com ka MultiQuality link nahi mila.\n"
                f"⛔ Agle items **band** kar diye gaye."
            )
        except Exception:
            pass
        return "error"

    try:
        await status_msg.edit(
            f"✅ **{ep_label}** (`{index}/{total}`) — Link mil gaya\n\n"
            f"⬇️ Ab download shuru ho raha hai..."
        )
        from .swift_downloader import _run_swift
        uploaded_results = await _run_swift(
            client, message, codedew_url, encode=False, episode_label=ep_label, show_url=False
        )
        await _forward_to_toono_channel_if_enabled(client, message, uploaded_results)
        await _send_toono_update_post_if_enabled(client, sess, item, uploaded_results)
        return "ok"
    except Exception as e:
        LOGGER.error(f"[Toono] {ep_label} download/upload error: {e}")
        try:
            await status_msg.edit(
                f"🛑 **{ep_label} — Upload Error**\n\n"
                f"❌ `{str(e)[:120]}`\n"
                f"⛔ Agle items **band** kar diye gaye."
            )
        except Exception:
            pass
        return "error"


async def _download_toono_items(client, status_msg, orig_message, sess: "ToonoSelector", idxs: list):
    total = len(idxs)
    all_ok = True
    for i, idx in enumerate(idxs, 1):
        item = sess.items[idx]
        result = await _process_toono_item(client, orig_message, item, status_msg, i, total, sess=sess)
        if result != "ok":
            all_ok = False
            break
        if i < total:
            await asyncio.sleep(3)

    # Sirf success (all_ok) pe status_msg delete karo — error/fail message
    # ko turant delete karne se pehle wala bug tha: "Link Fail" / "Upload
    # Error" wala message ek fatak dikhta tha phir turant gayab ho jaata
    # tha, isliye pata hi nahi chalta tha kya error aaya. Ab error hone pe
    # message wahi rehta hai taaki error text padh sako.
    if all_ok:
        try:
            await status_msg.delete()
        except Exception:
            pass


# ─────────────────────────────────────────────
#  Callback handler
# ─────────────────────────────────────────────
@Client.on_callback_query(filters.regex(r"^toono_"))
async def toono_callback_handler(client: Client, cb: CallbackQuery):
    try:
        parts = cb.data.split("_")
        action = parts[1] if len(parts) > 1 else ""
        sid = parts[2] if len(parts) > 2 else None
        sess = TOONO_SESSIONS.get(sid)

        if not sess:
            await cb.answer("⌛ Session expired, /toono <url> dubara bhejo.", show_alert=True)
            return

        if action == "noop":
            await cb.answer()
            return

        if action == "close":
            TOONO_SESSIONS.pop(sid, None)
            await cb.answer()
            try:
                await cb.message.delete()
            except Exception:
                pass
            return

        if action == "ssel":
            try:
                season = int(parts[3])
            except (IndexError, ValueError):
                await cb.answer()
                return
            sess.current_season = season
            sess.page = 0
            sess.multi = False
            sess.selected.clear()
            await cb.answer()
            await sess.render()
            return

        if action == "back":
            sess.current_season = None
            sess.page = 0
            sess.multi = False
            sess.selected.clear()
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

        if action == "uptog":
            new_val = not sess.update_toggle
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
                    LOGGER.error(f"[Toono] channel lookup error: {e}")
                    channels = []
                if not channels:
                    await cb.answer(
                        "❌ Pehle /addchannel se ek channel add karo!", show_alert=True
                    )
                    return
            await _set_toono_channel_upload(sess.orig_message.from_user.id, new_val)
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
                return

            # Single-tap: turant download shuru karo
            await cb.answer("⬇️ Download shuru ho raha hai...")
            try:
                await cb.message.delete()
            except Exception:
                pass
            status_msg = await sess.orig_message.reply("⏳ Shuru ho raha hai...")
            await _download_toono_items(client, status_msg, sess.orig_message, sess, [idx])
            TOONO_SESSIONS.pop(sid, None)
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
            await _download_toono_items(client, status_msg, sess.orig_message, sess, idxs)
            TOONO_SESSIONS.pop(sid, None)
            return

        await cb.answer()
    except Exception as e:
        LOGGER.error(f"[Toono] callback error: {e}")
        try:
            await cb.answer("❌ Error, dubara try karo.", show_alert=True)
        except Exception:
            pass


# ─────────────────────────────────────────────
#  /toono Command Handler
# ─────────────────────────────────────────────
@Client.on_message(filters.command("toono"))
async def toono_command(client: Client, message: Message):
    """
    /toono <series_url> -> Season button menu, phir episode button menu
    (RTI ke /rti <url> jaisa hi flow, seasons ke extra layer ke saath).
    """
    c = await check_chat(message, chat="Sudo")
    if not c:
        return

    if not SELENIUM_OK:
        await message.reply("❌ Selenium install nahi hai! `pip install selenium`")
        return

    parts = message.text.split()
    if len(parts) < 2:
        await message.reply(
            "**Usage:**\n"
            "`/toono <series_url>` — Season button menu khulega\n\n"
            "**Example:**\n"
            "`/toono https://toono.app/series/bleach-thousand-year-blood-war/`"
        )
        return

    series_url = parts[1].strip()
    if not series_url.startswith("http"):
        await message.reply("❌ Valid URL dalo.")
        return

    status_msg = await message.reply("🔍 Page scan ho raha hai...")

    loop = asyncio.get_event_loop()
    try:
        data = await loop.run_in_executor(None, discover_toono_items, series_url)
    except Exception as e:
        LOGGER.error(f"[Toono] discover_toono_items error: {e}")
        await status_msg.edit(f"❌ Page load nahi hua: `{str(e)[:100]}`")
        return

    if not data["seasons"]:
        await status_msg.edit("❌ Page se koi season/episode nahi mila. URL check karo.")
        return

    _prune_toono_sessions()
    sess = ToonoSelector(series_url, data, orig_message=message)
    sess.sid = uuid.uuid4().hex[:10]
    await sess.populate_toggles()
    TOONO_SESSIONS[sess.sid] = sess

    try:
        await status_msg.delete()
    except Exception:
        pass

    sess.msg = await message.reply(sess.header_text(), reply_markup=sess.build_markup())
