"""
rti_downloader.py  v3
======================
Commands:
  /rti <url>                -> Latest episode auto-detect + download
  /rti <url> <start> <end>  -> Episode range download
  /rti <url> 5 5            -> Sirf episode 5
"""

import asyncio
import re
import time

import requests
from bs4 import BeautifulSoup
from pyrogram import Client, filters
from pyrogram.types import Message

from .. import LOGGER, download_dir, app
from ..utils.helper import check_chat

try:
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    SELENIUM_OK = True
except ImportError:
    SELENIUM_OK = False

ARGON_DOMAIN = "argon.razorshell.space/embed"
SWIFT_BASE   = "https://swift.multiquality.click/downlead/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

AUDIO_PRIORITY = ["hindi", "dual", "multi", "english", "japanese", "sub", "unknown"]


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
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument(f"user-agent={HEADERS['User-Agent']}")
    options.add_experimental_option("prefs", {
        "profile.managed_default_content_settings.images": 2
    })
    options.page_load_strategy = "eager"
    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(40)
    return driver


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
        time.sleep(5)
        driver.execute_script("window.stop();")
        _close_popups(driver, main)

        argon = _extract_argon_from_iframes(driver)
        if argon:
            return argon

        try:
            wait = WebDriverWait(driver, 10)
            for btn_text in ["Get Download Link", "Download", "Get Link", "Click Here"]:
                try:
                    btn = wait.until(EC.element_to_be_clickable(
                        (By.XPATH, f"//a[contains(translate(text(),'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ'), '{btn_text.upper()}')]")
                    ))
                    btn.click()
                    time.sleep(4)
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
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


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
    from .swift_downloader import (
        _scrape_and_download, _sort_by_size,
        _quality_from, _upload_one_file
    )
    import os, shutil

    session_id = str(int(time.time()))
    dl_dir = os.path.join(download_dir, f"rti_{session_id}")
    os.makedirs(dl_dir, exist_ok=True)

    try:
        await status_msg.edit(f"⬇️ **Ep {ep_num}/{total_eps}** — Downloading...")

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, _scrape_and_download, swift_url, dl_dir)

        if result.get("error") and not result.get("files"):
            await status_msg.edit(f"❌ **Ep {ep_num}** — Download failed: `{result['error']}`")
            return False

        files = result.get("files", [])
        if not files:
            await status_msg.edit(f"❌ **Ep {ep_num}** — Koi file nahi mili.")
            return False

        files = _sort_by_size(files)

        for i, filepath in enumerate(files, 1):
            quality = _quality_from(os.path.basename(filepath))
            await status_msg.edit(
                f"📤 **Ep {ep_num}/{total_eps}** — Uploading `{quality}` ({i}/{len(files)})"
            )
            await _upload_one_file(client, message, status_msg, filepath, dl_dir, encode=False)
            await asyncio.sleep(2)

        return True

    except Exception as e:
        LOGGER.error(f"[RTI] _run_rti_swift error: {e}")
        await status_msg.edit(f"❌ **Ep {ep_num}** — Error: `{str(e)[:100]}`")
        return False
    finally:
        try:
            shutil.rmtree(dl_dir, ignore_errors=True)
        except Exception:
            pass


# ─────────────────────────────────────────────
#  Single episode pipeline
# ─────────────────────────────────────────────
async def _process_episode(client, message, page_url, episode_num, total_episodes, status_msg):
    loop = asyncio.get_event_loop()

    await status_msg.edit(f"🔍 **Ep {episode_num}/{total_episodes}** — Link dhundh raha hoon...")
    wmq_link, _ = await loop.run_in_executor(None, get_watchmult_link, page_url, episode_num)
    if not wmq_link:
        await status_msg.edit(f"❌ **Ep {episode_num}** — WatchMultQuality link nahi mila.")
        return False

    await status_msg.edit(f"🔍 **Ep {episode_num}/{total_episodes}** — Argon link extract ho raha hai...")
    argon_link = await loop.run_in_executor(None, get_argon_link, wmq_link)
    if not argon_link:
        await status_msg.edit(f"❌ **Ep {episode_num}** — Argon iframe nahi mila.")
        return False

    swift_url = argon_to_swift(argon_link)
    if not swift_url:
        await status_msg.edit(f"❌ **Ep {episode_num}** — Swift URL fail.")
        return False

    return await _run_rti_swift(client, message, swift_url, status_msg, ep_num=episode_num, total_eps=total_episodes)


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
            "`/rti <url>` — Latest episode auto-download\n"
            "`/rti <url> <start> <end>` — Episode range\n\n"
            "**Examples:**\n"
            "`/rti https://rareanimes.buzz/wistoria/` — Latest\n"
            "`/rti https://rareanimes.buzz/wistoria/ 01 10` — Ep 1 to 10\n"
            "`/rti https://rareanimes.buzz/wistoria/ 5 5` — Sirf Ep 5"
        )
        return

    page_url = parts[1].strip()

    if not page_url.startswith("http"):
        await message.reply("❌ Valid URL dalo.")
        return

    # ── AUTO LATEST MODE: sirf URL diya, koi number nahi ──
    if len(parts) == 2:
        status_msg = await message.reply("🔍 Latest episode detect ho raha hai...")

        loop = asyncio.get_event_loop()
        latest_ep, anime_title = await loop.run_in_executor(None, get_latest_episode, page_url)

        if not latest_ep:
            await status_msg.edit("❌ Page se koi episode nahi mila. URL check karo.")
            return

        await status_msg.edit(
            f"🎌 **RTI** — Latest Ep `{latest_ep}` detected\n"
            f"📺 `{anime_title}`\n"
            f"⏳ Starting..."
        )

        try:
            success = await _process_episode(
                client, message, page_url,
                episode_num=latest_ep,
                total_episodes=1,
                status_msg=status_msg,
            )
        except Exception as e:
            LOGGER.error(f"[RTI] Latest ep error: {e}")
            await status_msg.edit(f"❌ Error: `{str(e)[:100]}`")
            return

        try:
            await status_msg.delete()
        except Exception:
            pass
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
        try:
            success = await _process_episode(
                client, message, page_url,
                episode_num=ep_num,
                total_episodes=total_eps,
                status_msg=status_msg,
            )
            if success:
                success_count += 1
        except Exception as e:
            LOGGER.error(f"[RTI] Ep {ep_num} error: {e}")
            await status_msg.edit(f"❌ Ep {ep_num} error: `{str(e)[:100]}`")

        if i < total_eps:
            await asyncio.sleep(3)

    try:
        await status_msg.delete()
    except Exception:
        pass
