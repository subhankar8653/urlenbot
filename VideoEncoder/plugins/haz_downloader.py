"""
haz_downloader.py  v1
========================
Command:
  /Haz <series_page_url>   (hindianimeszone.com ka ek anime/series post)

Flow (jaisa Subhankar ne screenshots + text mein bataya):
  1. Series page (static HTML, requests+BS4 se seedha scrape) se title,
     available languages ("Language: Hindi-Tamil-Telugu-English-Japanese"
     field se) aur har episode ke 5 quality-links (480p x264 / 720p x264 /
     720p x265 / 1080p x265 / 1080p HQ) nikalte hain.
  2. Bot pehle Language buttons dikhata hai (Hindi/Tamil/Telugu/...).
  3. Phir Quality buttons — sirf 3 allowed: 480p x264, 720p x265, 1080p x265
     (720p x264 aur 1080p HQ jaanbujh kar skip, jaisa bataya gaya).
  4. Phir episode list (RTI/Toono jaisa hi multi-select button menu).
  5. Chosen quality ka link (002.hindianimeszone.com/download1.php?...)
     Selenium se khola jaata hai — kabhi-kabhi ek "Verify You're Human"
     (I'm not a robot) gate aata hai, usko pass karke asli server-list
     page (GDFlix / MEGA / Gdshare / FilePress) tak pahunchte hain, wahan
     se sirf MEGA wala link nikalte hain.
  6. MEGA link ko mega_download.py ke existing download_mega() se download
     karte hain.
  7. url_upload.py ke existing audio/subtitle-filter helpers reuse karte
     hain — sirf chuni hui language ka audio track + English subtitle
     rakhte hain, baaki audio tracks hata dete hain (jaisa "Hindi select
     kiya to sirf Hindi audio, Tamil select kiya to sirf Tamil audio"
     bataya gaya).
  8. url_upload.py ka hi _do_upload() reuse karke Telegram pe upload.

NOTE / assumptions (live site pe test karke confirm karna padega):
  - Is site pe HAR SEASON APNA ALAG POST/URL hai (tabs nahi, jaisa RTI mein
    hota hai) — isliye is command mein in-bot "season selector" nahi hai;
    jis season ka chahiye uska URL hi /Haz ko do.
  - "Verify You're Human" gate ek baar pass hone ke baad ~1 hour tak
    dubara nahi aata (site khud bolta hai) — isliye ek hi Chrome session
    (driver) poore multi-episode download ke dauran reuse hota hai, taaki
    baar baar checkbox na todna pade.
  - Gate ke checkbox aur MEGA-link-wale server row ke exact selectors is
    sandbox se live click karke test nahi ho paaye (network yahan band
    hai) — isliye kaafi fallback strategies + step-by-step LOGGER.info
    daale hain (jaisa /toono ke codedew step mein), taaki agar kahin atak
    jaaye to turant pata chale (Debug block message mein dikhega) aur
    ek round mein hi fix ho jaaye.
"""

import asyncio
import os
import re
import time
import uuid
from urllib.parse import unquote_plus

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
from ..utils.url_processor import get_audio_streams
from .rti_downloader import SELENIUM_OK, _make_selenium_driver, _kill_driver_tree, _kb
from .mega_download import is_mega_link, download_mega
from .url_upload import (
    get_subtitle_streams,
    _is_english_sub_stream,
    _keep_audio_streams,
    _keep_subtitle_streams,
    _do_upload,
)

try:
    from selenium.webdriver.common.by import By
except ImportError:
    pass

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# In yehi 3 qualities dikhani hain (5 available hain page pe, baaki 2 skip)
HAZ_QUALITIES = ["480p x264", "720p x265", "1080p x265"]

# Language button label -> ffprobe audio-language codes jo isse match karte hain
LANG_STREAM_CODES = {
    "hindi":    {"hin", "hi", "hindi"},
    "tamil":    {"tam", "ta", "tamil"},
    "telugu":   {"tel", "te", "telugu"},
    "english":  {"eng", "en", "english"},
    "japanese": {"jpn", "ja", "jap", "japanese"},
}

PER_PAGE = 10

HAZ_SESSIONS = {}
HAZ_SESSION_TIMEOUT = 3600


def _prune_haz_sessions():
    now = time.time()
    stale = [k for k, v in HAZ_SESSIONS.items() if now - v.created > HAZ_SESSION_TIMEOUT]
    for k in stale:
        HAZ_SESSIONS.pop(k, None)


# ─────────────────────────────────────────────
#  Step 1: series page -> title / languages / episodes (static HTML)
# ─────────────────────────────────────────────
def discover_haz_items(series_url: str) -> dict:
    """
    Returns:
      {"title": str, "languages": [str,...], "episodes": [
          {"num": int, "code": str, "qualities": {q: href}}, ...
      ]}
    """
    r = requests.get(series_url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    title_tag = soup.find("h1")
    title = title_tag.get_text(strip=True) if title_tag else "Unknown"

    # ── Languages: "Language: Hindi-Tamil-Telugu-English-Japanese" field ──
    page_text = soup.get_text("\n")
    languages = []
    m = re.search(r'Language\s*:\s*([A-Za-z][A-Za-z\-\s]*)', page_text)
    if m:
        raw = m.group(1).splitlines()[0].strip()
        languages = [x.strip() for x in re.split(r'[-–]', raw) if x.strip()]
    if not languages:
        languages = ["Hindi", "English"]

    # ── Episodes: har <a href="...download1.php?code=X&q=Y"> ko nearest
    #    pichhle "Episode N" text se group karo (document order, class
    #    names pe depend nahi karta) ──
    episodes = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "download1.php" not in href:
            continue
        qm = re.search(r'[?&]code=([^&]+)&q=([^&]+)', href)
        if not qm:
            continue
        code, q = qm.group(1), unquote_plus(qm.group(2))
        if q not in HAZ_QUALITIES:
            continue

        ep_marker = a.find_previous(string=re.compile(r'^\s*Episode\s+\d+\s*$', re.IGNORECASE))
        if not ep_marker:
            continue
        num_m = re.search(r'\d+', ep_marker)
        if not num_m:
            continue
        ep_num = int(num_m.group())

        ep = episodes.setdefault(ep_num, {"num": ep_num, "code": code, "qualities": {}})
        ep["qualities"][q] = href
        if not ep.get("code"):
            ep["code"] = code

    ep_list = sorted(episodes.values(), key=lambda x: x["num"])
    return {"title": title, "languages": languages, "episodes": ep_list}


# ─────────────────────────────────────────────
#  Step 2: download1.php link -> (agar aaya to) "Verify You're Human"
#  gate pass karo -> server-list page se MEGA wala link nikaalo.
#  Poori tarah Selenium/JS-driven, is sandbox mein live test nahi ho paaya
#  (network band hai) — isliye har step LOGGER.info/warning karta hai.
# ─────────────────────────────────────────────
def _solve_haz_gate_and_get_mega(driver, download_url: str, debug: list) -> str | None:
    def log(msg):
        debug.append(msg)
        LOGGER.info(f"[Haz] {msg}")

    try:
        driver.get(download_url)
    except Exception as e:
        log(f"⚠️ Page load warning: {str(e).splitlines()[0][:120]} (aage badhte hain)")

    try:
        is_gate = "not a robot" in driver.page_source.lower() or "verify you" in driver.page_source.lower()
    except Exception:
        is_gate = False

    if is_gate:
        log("🤖 'Verify You're Human' gate mila, pass karne ki koshish...")
        clicked = False
        for sel in ("input[type=checkbox]", "[class*=checkbox]", "[class*=captcha]", "[class*=verify]"):
            try:
                for el in driver.find_elements(By.CSS_SELECTOR, sel):
                    if el.is_displayed():
                        el.click()
                        clicked = True
                        log(f"✅ Checkbox click kiya ({sel})")
                        break
            except Exception:
                pass
            if clicked:
                break

        for word in ("I'm not a robot", "I am not a robot", "Not a robot", "Continue", "Verify"):
            try:
                for el in driver.find_elements(By.XPATH, f'//*[contains(text(), "{word}")]'):
                    if el.is_displayed():
                        el.click()
                        clicked = True
                        log(f"✅ '{word}' button/label click kiya")
                        time.sleep(0.5)
            except Exception:
                pass

        if not clicked:
            log("⚠️ Koi checkbox/button click nahi hua — shayad JS auto-pass kare")

        gone = False
        for _ in range(50):
            time.sleep(0.3)
            try:
                pt = driver.page_source.lower()
            except Exception:
                continue
            if "not a robot" not in pt and "verify you" not in pt:
                gone = True
                break
        log("✅ Verification gate pass ho gaya (~15s ke andar)" if gone else
            "⚠️ 15s baad bhi gate wahi hai — aage bhi try kar rahe hain")
    else:
        log("➡️ Verification gate nahi aaya (shayad pehle se verified session hai)")

    # ── MEGA server row dhoondo ──
    mega_url = None
    try:
        els = driver.find_elements(
            By.XPATH, '//*[contains(translate(text(),"mega","MEGA"),"MEGA")]'
        )
        log(f"🔎 'MEGA' text wale {len(els)} element mile")

        # Pehle: seedha href attribute mein mega.nz check karo
        for el in els:
            try:
                href = el.get_attribute("href")
            except Exception:
                href = None
            if href and "mega.nz" in href:
                mega_url = href
                log("✅ MEGA href seedha mil gaya")
                break

        # Nahi mila to click karke dekho — naya tab khul sakta hai
        if not mega_url:
            main = driver.current_window_handle
            for el in els:
                try:
                    if not el.is_displayed():
                        continue
                    before = set(driver.window_handles)
                    el.click()
                    time.sleep(1.2)
                    after = set(driver.window_handles)
                    new_tabs = after - before
                    if new_tabs:
                        driver.switch_to.window(new_tabs.pop())
                        time.sleep(1)
                    cur = driver.current_url or ""
                    if "mega.nz" in cur:
                        mega_url = cur
                    else:
                        m = re.search(
                            r'https?://mega\.nz/(?:file|folder)/[A-Za-z0-9_\-]+#[A-Za-z0-9_\-!]+',
                            driver.page_source,
                        )
                        if m:
                            mega_url = m.group(0)
                    if new_tabs:
                        try:
                            driver.close()
                        except Exception:
                            pass
                        driver.switch_to.window(main)
                    if mega_url:
                        log("✅ MEGA row click karke link mila")
                        break
                except Exception as e:
                    log(f"⚠️ MEGA row click error: {str(e).splitlines()[0][:100]}")
                    continue
    except Exception as e:
        log(f"⚠️ MEGA element dhoondte waqt error: {str(e).splitlines()[0][:100]}")

    if not mega_url:
        try:
            m = re.search(
                r'https?://mega\.nz/(?:file|folder)/[A-Za-z0-9_\-]+#[A-Za-z0-9_\-!]+',
                driver.page_source,
            )
            if m:
                mega_url = m.group(0)
                log("✅ MEGA link page HTML mein regex se mil gaya (fallback)")
        except Exception:
            pass

    log(f"✅ Final MEGA link: {mega_url}" if mega_url else "❌ MEGA link nahi mila")
    return mega_url


# ─────────────────────────────────────────────
#  Selector UI: Language -> Quality -> Episode list (multi-select)
# ─────────────────────────────────────────────
class HazSelector:
    def __init__(self, series_url: str, data: dict, orig_message: Message):
        self.series_url = series_url
        self.title = data["title"]
        self.languages = data["languages"]
        self.episodes = data["episodes"]
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
        lines = ["📂 **Select**", "━━━━━━━━━━━━━━━━━━", f"🎬 **{self.title}**", "🌐 Website: 🍥 Hindi Anime Zone"]
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
                row.append((f"🗣️ {lang}", f"haz_lang_{sid}_{lang}"))
                if len(row) == 2:
                    rows.append(row)
                    row = []
            if row:
                rows.append(row)

        elif self.stage == "quality":
            for q in HAZ_QUALITIES:
                rows.append([(f"🎞️ {q}", f"haz_qual_{sid}_{q}")])
            if len(self.languages) > 1:
                rows.append([("🔙 Back to Language", f"haz_back_{sid}")])

        else:
            row = []
            for idx, ep in self.page_items():
                mark = "✅ " if idx in self.selected else ""
                row.append((f"{mark}E{ep['num']:02d}", f"haz_ep_{sid}_{idx}"))
                if len(row) == 3:
                    rows.append(row)
                    row = []
            if row:
                rows.append(row)

            if self.total_pages > 1:
                nav = []
                if self.page > 0:
                    nav.append(("◀️ Prev", f"haz_pg_{sid}_{self.page - 1}"))
                nav.append((f"📄 {self.page + 1}/{self.total_pages}", f"haz_noop_{sid}"))
                if self.page < self.total_pages - 1:
                    nav.append(("Next ▶️", f"haz_pg_{sid}_{self.page + 1}"))
                rows.append(nav)

            if self.multi:
                rows.append([(f"⬇️ Download Selected ({len(self.selected)})", f"haz_dl_{sid}")])
                rows.append([("☑️ Multi-select: ON (tap to turn off)", f"haz_toggle_{sid}")])
            else:
                rows.append([("☑️ Select Multiple", f"haz_toggle_{sid}")])

            rows.append([("🔙 Back to Quality", f"haz_backq_{sid}")])

        rows.append([("❌ Close", f"haz_close_{sid}")])
        return _kb(rows)

    async def render(self):
        if not self.msg:
            return
        try:
            await self.msg.edit(self.header_text(), reply_markup=self.build_markup())
        except Exception as e:
            LOGGER.error(f"[Haz] selector render error: {e}")


# ─────────────────────────────────────────────
#  Audio/Subtitle filter — url_upload.py ke existing helpers reuse,
#  sirf language generic (Hindi hardcoded nahi) banaya
# ─────────────────────────────────────────────
async def _apply_language_filter(filepath: str, language: str, status_msg: Message) -> tuple[str, bool]:
    """Sirf chuni hui language ka audio + English subtitle rakhta hai.
    Returns (final_filepath, has_eng_sub)."""
    lang_key = language.strip().lower()
    codes = LANG_STREAM_CODES.get(lang_key)

    if codes:
        try:
            await status_msg.edit(f"🔄 **Sirf {language} audio rakh raha hoon...**")
        except Exception:
            pass
        audio_streams = get_audio_streams(filepath)
        keep_indices = [s["index"] for s in audio_streams if s.get("lang", "").lower().strip() in codes]
        if keep_indices:
            new_path = await _keep_audio_streams(filepath, keep_indices, status_msg)
            if new_path:
                filepath = new_path
        else:
            LOGGER.warning(f"[Haz] {language} audio nahi mila, saare audio tracks rakh rahe hain")

    has_eng_sub = False
    try:
        await status_msg.edit("🔄 **Sirf English subtitle rakh raha hoon...**")
    except Exception:
        pass
    sub_streams = get_subtitle_streams(filepath)
    eng_indices = [s["index"] for s in sub_streams if _is_english_sub_stream(s)]
    if eng_indices:
        new_path = await _keep_subtitle_streams(filepath, eng_indices, status_msg)
        if new_path:
            filepath = new_path
            has_eng_sub = True

    return filepath, has_eng_sub


# ─────────────────────────────────────────────
#  Ek episode: link nikaalo -> mega download -> filter -> upload
# ─────────────────────────────────────────────
async def _process_haz_item(client, message, ep: dict, status_msg, index: int, total: int,
                             language: str, quality: str, driver):
    ep_label = f"E{ep['num']:02d}"
    loop = asyncio.get_event_loop()
    download_url = ep["qualities"].get(quality)
    if not download_url:
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
    mega_url = await loop.run_in_executor(None, _solve_haz_gate_and_get_mega, driver, download_url, debug)

    if mega_url and not is_mega_link(mega_url):
        debug.append(f"⚠️ Mila hua link MEGA jaisa nahi lagta: {mega_url[:80]}")
        mega_url = None

    if not mega_url:
        debug_text = "\n".join(debug[-10:]) if debug else "(koi debug info nahi mili)"
        try:
            await status_msg.edit(
                f"🛑 **{ep_label} — MEGA Link Fail**\n\n"
                f"❌ MEGA server ka link nahi mila.\n\n"
                f"**Debug (last steps):**\n```\n{debug_text}\n```\n"
                f"⛔ Agle items **band** kar diye gaye."
            )
        except Exception:
            pass
        return "error"

    dl_dir = os.path.join(download_dir, f"haz_{uuid.uuid4().hex[:8]}")
    try:
        await status_msg.edit(f"⬇️ **{ep_label}** — MEGA se download ho raha hai...")
    except Exception:
        pass
    filepath = await download_mega(mega_url, dl_dir, status_msg)
    if not filepath:
        # download_mega() apna khud ka detailed error status_msg pe daal chuka hai
        try:
            cur_text = status_msg.text or ""
            await status_msg.edit(f"{cur_text}\n\n⛔ Agle items **band** kar diye gaye.")
        except Exception:
            pass
        return "error"

    try:
        filepath, has_eng_sub = await _apply_language_filter(filepath, language, status_msg)
    except Exception as e:
        LOGGER.error(f"[Haz] {ep_label} audio/sub filter error: {e}")
        try:
            await status_msg.edit(
                f"🛑 **{ep_label} — Filter Error**\n\n❌ `{str(e)[:150]}`\n\n"
                f"⛔ Agle items **band** kar diye gaye."
            )
        except Exception:
            pass
        return "error"

    before_text = None
    try:
        before_text = status_msg.text
    except Exception:
        pass
    await _do_upload(client, filepath, message, status_msg, has_eng_sub=has_eng_sub)
    # _do_upload apna khud ka success/fail status daal deta hai. Fail hua
    # ho to seedha pata chal jaayega (message text mein "Upload failed"),
    # is se aage single-episode flow mein hum continue kar dete hain
    # (yahi ek jagah hai jahan _do_upload khud detailed error deta hai).
    try:
        final_text = status_msg.text or ""
    except Exception:
        final_text = ""
    if "failed" in final_text.lower():
        return "error"
    return "ok"


async def _download_haz_items(client, status_msg, orig_message, sess: "HazSelector", idxs: list):
    total = len(idxs)
    if not SELENIUM_OK:
        await status_msg.edit("❌ Selenium install nahi hai!")
        return

    driver = _make_selenium_driver()
    all_ok = True
    try:
        for i, idx in enumerate(idxs, 1):
            ep = sess.episodes[idx]
            result = await _process_haz_item(
                client, orig_message, ep, status_msg, i, total, sess.language, sess.quality, driver,
            )
            if result != "ok":
                all_ok = False
                break
            if i < total:
                status_msg = await orig_message.reply("⏳ Agla episode shuru ho raha hai...")
                await asyncio.sleep(1)
    finally:
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, _kill_driver_tree, driver)
        except Exception:
            pass

    if all_ok:
        try:
            await status_msg.delete()
        except Exception:
            pass


# ─────────────────────────────────────────────
#  Callback handler
# ─────────────────────────────────────────────
@Client.on_callback_query(filters.regex(r"^haz_"))
async def haz_callback_handler(client: Client, cb: CallbackQuery):
    try:
        parts = cb.data.split("_")
        action = parts[1] if len(parts) > 1 else ""
        sid = parts[2] if len(parts) > 2 else None
        sess = HAZ_SESSIONS.get(sid)

        if not sess:
            await cb.answer("⌛ Session expired, /Haz <url> dubara bhejo.", show_alert=True)
            return

        if action == "noop":
            await cb.answer()
            return

        if action == "close":
            HAZ_SESSIONS.pop(sid, None)
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
            sess.quality = "_".join(parts[3:])
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
            await _download_haz_items(client, status_msg, sess.orig_message, sess, [idx])
            HAZ_SESSIONS.pop(sid, None)
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
            await _download_haz_items(client, status_msg, sess.orig_message, sess, idxs)
            HAZ_SESSIONS.pop(sid, None)
            return

        await cb.answer()
    except Exception as e:
        LOGGER.error(f"[Haz] callback error: {e}")
        try:
            await cb.answer("❌ Error, dubara try karo.", show_alert=True)
        except Exception:
            pass


# ─────────────────────────────────────────────
#  /Haz Command Handler
# ─────────────────────────────────────────────
@Client.on_message(filters.command("haz"))
async def haz_command(client: Client, message: Message):
    """
    /Haz <series_page_url>  -> Language menu -> Quality menu -> Episode menu
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
            "`/Haz <series_page_url>` — Language/Quality/Episode button menu khulega\n\n"
            "**Example:**\n"
            "`/Haz https://hindianimeszone.com/liar-game-season-1-multi-audio-hindi-tamil-telugu-eng-jap-480p-720p-1080p-hd-web-dl-10bit-hevc-esub/`"
        )
        return

    series_url = parts[1].strip()
    if not series_url.startswith("http"):
        await message.reply("❌ Valid URL dalo.")
        return

    status_msg = await message.reply("🔍 Page scan ho raha hai...")

    loop = asyncio.get_event_loop()
    try:
        data = await loop.run_in_executor(None, discover_haz_items, series_url)
    except Exception as e:
        LOGGER.error(f"[Haz] discover_haz_items error: {e}")
        await status_msg.edit(f"❌ Page load nahi hua: `{str(e)[:100]}`")
        return

    if not data["episodes"]:
        await status_msg.edit("❌ Page se koi episode nahi mila. URL check karo.")
        return

    _prune_haz_sessions()
    sess = HazSelector(series_url, data, orig_message=message)
    sess.sid = uuid.uuid4().hex[:10]
    HAZ_SESSIONS[sess.sid] = sess

    try:
        await status_msg.delete()
    except Exception:
        pass

    sess.msg = await message.reply(sess.header_text(), reply_markup=sess.build_markup())
