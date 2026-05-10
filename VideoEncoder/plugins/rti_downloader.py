"""
rti_downloader.py
==================
RTI (RareAnimes) Download Plugin for Encode Bot

Command:
  /rti <url> <ep1> <ep2> ...

Example:
  /rti https://rareanimes.buzz/wistoria-wand-and-sword-season-2-hindi-dubbed-episodes-download-hd/ 01 02

Flow:
  1. URL page open karo (requests/BeautifulSoup)
  2. Episode number ke liye [WatchMultQuality] link dhundo (Hindi priority)
  3. WatchMultQuality page open karo (Selenium)
  4. Inspect → iframe → argon.razorshell.space/embed wala link nikalo
  5. Unique code extract karo (e.g. 7FFijEtn3e9zJMU)
  6. Swift link banao: https://swift.multiquality.click/downlead/<code>
  7. Existing /swift logic se download + upload karo
"""

import asyncio
import re
import time

import requests
from bs4 import BeautifulSoup
from pyrogram import Client, filters
from pyrogram.types import Message

from .. import LOGGER, app
from ..utils.helper import check_chat

try:
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    SELENIUM_OK = True
except ImportError:
    SELENIUM_OK = False

# ─────────────────────────────────────────────
#  Constants
# ─────────────────────────────────────────────
ARGON_DOMAIN = "argon.razorshell.space/embed"
SWIFT_BASE = "https://swift.multiquality.click/downlead/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# Audio priority — Hindi pehle
AUDIO_PRIORITY = ["hindi", "dual", "multi", "english", "japanese", "sub", "unknown"]


# ─────────────────────────────────────────────
#  Step 1: Page se episode ka WatchMultQuality link nikalo
# ─────────────────────────────────────────────
def _detect_audio(link_el, context_text: str) -> str:
    """Link ke aas-paas ke text se audio type detect karo."""
    # Previous sibling check
    prev = link_el.previous_sibling
    if prev:
        t = str(prev).strip().lower()
        for kw, label in [
            ("hindi", "hindi"), ("dual", "dual"), ("multi", "multi"),
            ("english", "english"), ("japanese", "japanese"), ("sub", "sub"),
        ]:
            if kw in t:
                return label

    # Parent text check
    parent = link_el.find_parent()
    if parent:
        pt = parent.get_text(" ", strip=True).lower()
        lt = link_el.get_text(strip=True).lower()
        if lt in pt:
            before = pt.split(lt)[0][-80:]
            for kw, label in [
                ("hindi", "hindi"), ("dual", "dual"), ("multi", "multi"),
                ("english", "english"), ("japanese", "japanese"), ("sub", "sub"),
            ]:
                if kw in before:
                    return label

    # Context text fallback
    ct = context_text.lower()
    patterns = [
        (r"hindi\s*[-–—]\s*\[?watch", "hindi"),
        (r"english\s*[-–—]\s*\[?watch", "english"),
        (r"japanese\s*[-–—]\s*\[?watch", "japanese"),
        (r"dual\s*audio\s*[-–—]\s*\[?watch", "dual"),
    ]
    for pat, label in patterns:
        if re.search(pat, ct):
            return label

    for kw, label in [
        ("hindi", "hindi"), ("dual", "dual"), ("multi", "multi"),
        ("english", "english"), ("japanese", "japanese"), ("sub", "sub"),
    ]:
        if kw in ct:
            return label

    return "unknown"


def _find_wmq_links(element) -> list:
    """Element mein WatchMultQuality links dhundo."""
    links = []
    ctx = element.get_text(" ", strip=True)
    for a in element.find_all("a", href=True):
        text = a.get_text(strip=True).lower()
        href = a.get("href", "")
        if "watchmultquality" in text or "watchmultquality" in href.lower() or "multiquality" in text:
            audio = _detect_audio(a, ctx)
            links.append({"href": href, "audio": audio})
            LOGGER.info(f"[RTI] WMQ link found — audio: {audio}")
    return links


def _best_link(links: list) -> dict | None:
    """Priority ke hisab se best link choose karo."""
    for priority in AUDIO_PRIORITY:
        for lnk in links:
            if lnk["audio"] == priority:
                return lnk
    return links[0] if links else None


def get_watchmult_link(page_url: str, episode_num: int) -> tuple[str | None, str | None]:
    """
    Page URL aur episode number se WatchMultQuality link nikalo.
    Returns: (watchmult_href, anime_title)
    """
    try:
        r = requests.get(page_url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.content, "html.parser")

        # Anime title
        title_tag = soup.find("h1", class_="entry-title")
        anime_title = title_tag.text.strip() if title_tag else "Unknown Anime"

        # Episode paragraphs scan karo
        for p in soup.find_all("p"):
            text = p.get_text(" ", strip=True)
            match = re.search(r"Episode\s*(\d+)", text, re.IGNORECASE)
            if match and int(match.group(1)) == episode_num:
                # Is episode ke links collect karo (next 8 siblings tak)
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
                    LOGGER.info(f"[RTI] Ep {episode_num}: selected audio={best['audio']}")
                    return best["href"], anime_title

        LOGGER.warning(f"[RTI] Episode {episode_num} not found on page")
        return None, None

    except Exception as e:
        LOGGER.error(f"[RTI] Page scrape error: {e}")
        return None, None


# ─────────────────────────────────────────────
#  Step 2: WatchMultQuality → argon iframe link
# ─────────────────────────────────────────────
def _make_selenium_driver():
    """Headless Chrome driver banao."""
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument(f"user-agent={HEADERS['User-Agent']}")
    options.add_experimental_option("prefs", {
        "profile.managed_default_content_settings.images": 2  # Images disable
    })
    options.page_load_strategy = "eager"
    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(40)
    return driver


def _extract_argon_from_iframes(driver) -> str | None:
    """
    Page ke iframes inspect karo aur argon.razorshell.space/embed wala src nikalo.
    """
    try:
        soup = BeautifulSoup(driver.page_source, "html.parser")
        # Direct iframe tags check
        for iframe in soup.find_all("iframe"):
            src = iframe.get("src", "")
            if ARGON_DOMAIN in src:
                LOGGER.info(f"[RTI] Argon iframe found: {src}")
                return src

        # JavaScript mein embedded src check
        page_src = driver.page_source
        matches = re.findall(
            r'https?://argon\.razorshell\.space/embed/[A-Za-z0-9_-]+',
            page_src
        )
        if matches:
            LOGGER.info(f"[RTI] Argon link from JS: {matches[0]}")
            return matches[0]

        # Selenium se iframes directly check karo
        iframes = driver.find_elements(By.TAG_NAME, "iframe")
        for iframe in iframes:
            src = iframe.get_attribute("src") or ""
            if ARGON_DOMAIN in src:
                LOGGER.info(f"[RTI] Argon iframe (selenium): {src}")
                return src

    except Exception as e:
        LOGGER.error(f"[RTI] Argon extract error: {e}")

    return None


def _close_popups(driver, main_window):
    """Extra popup windows band karo."""
    try:
        if len(driver.window_handles) > 1:
            for handle in driver.window_handles:
                if handle != main_window:
                    driver.switch_to.window(handle)
                    driver.close()
            driver.switch_to.window(main_window)
    except Exception:
        pass


def get_argon_link(watchmult_url: str) -> str | None:
    """
    WatchMultQuality URL open karo → argon embed link nikalo.
    """
    driver = None
    try:
        driver = _make_selenium_driver()
        LOGGER.info(f"[RTI] Opening WMQ: {watchmult_url}")
        driver.get(watchmult_url)
        main = driver.current_window_handle
        time.sleep(5)
        driver.execute_script("window.stop();")
        _close_popups(driver, main)

        # Pehle normal page check karo
        argon = _extract_argon_from_iframes(driver)
        if argon:
            return argon

        # Agar page mein redirect button ho to click karo
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

        LOGGER.warning("[RTI] Argon link not found in WMQ page")
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
#  Step 3: Argon link → Swift link
# ─────────────────────────────────────────────
def argon_to_swift(argon_url: str) -> str | None:
    """
    argon.razorshell.space/embed/UNIQUECODE  →  swift.multiquality.click/downlead/UNIQUECODE
    
    Unique code = URL ke last segment mein jo hai
    Example:
      https://argon.razorshell.space/embed/7FFijEtn3e9zJMU
      →  https://swift.multiquality.click/downlead/7FFijEtn3e9zJMU
    """
    try:
        # Last non-empty segment extract karo
        parts = [p for p in argon_url.rstrip("/").split("/") if p]
        unique_code = parts[-1]

        if len(unique_code) < 5:
            LOGGER.warning(f"[RTI] Unique code too short: {unique_code}")
            return None

        swift_url = SWIFT_BASE + unique_code
        LOGGER.info(f"[RTI] Swift URL: {swift_url}")
        return swift_url

    except Exception as e:
        LOGGER.error(f"[RTI] argon_to_swift error: {e}")
        return None


# ─────────────────────────────────────────────
#  Step 4: Full episode pipeline
# ─────────────────────────────────────────────
async def _process_rti_episode(
    client, message: Message,
    page_url: str, episode_num: int
) -> bool:
    """
    Ek episode ka poora RTI flow:
    1. WatchMultQuality link nikalo
    2. Argon embed link nikalo
    3. Swift link banao
    4. /swift command ki tarah download + upload karo
    """
    from .swift_downloader import _run_swift  # Existing swift logic reuse

    # Status message
    status = await message.reply(
        f"🎌 **RTI — Episode {episode_num}**\n\n"
        f"🔍 Step 1/3: WatchMultQuality link dhundh raha hoon..."
    )

    # Step 1: WMQ link
    loop = asyncio.get_event_loop()
    wmq_link, anime_title = await loop.run_in_executor(
        None, get_watchmult_link, page_url, episode_num
    )

    if not wmq_link:
        await status.edit(
            f"❌ **RTI Failed — Episode {episode_num}**\n\n"
            f"WatchMultQuality link nahi mila!\n"
            f"Check karo ki episode page pe hai ya nahi."
        )
        return False

    await status.edit(
        f"🎌 **RTI — Episode {episode_num}**\n\n"
        f"✅ WMQ link mila!\n"
        f"🔍 Step 2/3: Argon iframe extract ho raha hai...\n\n"
        f"🔗 `{wmq_link[:60]}...`"
    )

    # Step 2: Argon link (blocking selenium, run in executor)
    argon_link = await loop.run_in_executor(None, get_argon_link, wmq_link)

    if not argon_link:
        await status.edit(
            f"❌ **RTI Failed — Episode {episode_num}**\n\n"
            f"Argon embed link nahi mila!\n"
            f"WMQ page pe iframe load nahi hua."
        )
        return False

    await status.edit(
        f"🎌 **RTI — Episode {episode_num}**\n\n"
        f"✅ Argon link mila!\n"
        f"🔍 Step 3/3: Swift link convert ho raha hai...\n\n"
        f"🔗 `{argon_link}`"
    )

    # Step 3: Swift URL
    swift_url = argon_to_swift(argon_link)

    if not swift_url:
        await status.edit(
            f"❌ **RTI Failed — Episode {episode_num}**\n\n"
            f"Argon se Swift conversion fail!\n"
            f"Argon URL: `{argon_link}`"
        )
        return False

    await status.edit(
        f"🎌 **RTI — Episode {episode_num}**\n\n"
        f"✅ Swift link ready!\n"
        f"📺 Anime: `{anime_title}`\n\n"
        f"⬇️ Download + Upload start ho raha hai...\n\n"
        f"🔗 `{swift_url}`"
    )

    # Step 4: Swift download + upload (existing logic reuse)
    await _run_swift(client, message, swift_url, encode=False)
    return True


# ─────────────────────────────────────────────
#  /rti Command Handler
# ─────────────────────────────────────────────
@Client.on_message(filters.command("rti"))
async def rti_command(client: Client, message: Message):
    """
    Usage: /rti <url> <ep1> [ep2] [ep3] ...
    
    Example:
      /rti https://rareanimes.buzz/wistoria-wand-and-sword-season-2/ 01 02
      /rti https://rareanimes.buzz/some-anime/ 5 6 7
    """
    c = await check_chat(message, chat="Sudo")
    if not c:
        return

    if not SELENIUM_OK:
        await message.reply(
            "❌ Selenium install nahi hai!\n"
            "`pip install selenium`\n"
            "`apt-get install -y chromium chromium-driver`"
        )
        return

    # Parse command
    parts = message.text.split()
    # /rti <url> <ep1> [ep2] ...
    if len(parts) < 3:
        await message.reply(
            "⚠️ **RTI Downloader — Usage:**\n\n"
            "`/rti <url> <episode_numbers...>`\n\n"
            "**Example:**\n"
            "`/rti https://rareanimes.buzz/wistoria-wand-and-sword-season-2/ 01 02`\n"
            "`/rti https://rareanimes.buzz/some-anime/ 5 6 7 8`\n\n"
            "**Flow:**\n"
            "1. Page se WatchMultQuality link nikalta hai\n"
            "2. Argon iframe link extract karta hai\n"
            "3. Swift link banata hai\n"
            "4. Download + Upload karta hai 🎉"
        )
        return

    page_url = parts[1].strip()

    # Episode numbers parse karo
    episode_nums = []
    for ep_str in parts[2:]:
        try:
            episode_nums.append(int(ep_str))
        except ValueError:
            await message.reply(f"❌ Invalid episode number: `{ep_str}`")
            return

    if not page_url.startswith("http"):
        await message.reply("❌ Valid URL dalo (http/https se shuru hona chahiye)")
        return

    # Start processing
    await message.reply(
        f"🎌 **RTI Downloader Started!**\n\n"
        f"🌐 URL: `{page_url[:60]}...`\n"
        f"🎬 Episodes: `{', '.join(str(e) for e in episode_nums)}`\n"
        f"📊 Total: `{len(episode_nums)}`\n\n"
        f"⏳ Processing..."
    )

    success_count = 0
    for i, ep_num in enumerate(episode_nums, 1):
        LOGGER.info(f"[RTI] Processing episode {ep_num} ({i}/{len(episode_nums)})")

        try:
            success = await _process_rti_episode(client, message, page_url, ep_num)
            if success:
                success_count += 1
        except Exception as e:
            LOGGER.error(f"[RTI] Episode {ep_num} error: {e}")
            await message.reply(f"❌ Episode {ep_num} mein error: `{str(e)[:100]}`")

        # Multiple episodes ke beech gap
        if i < len(episode_nums):
            await asyncio.sleep(5)

    # Final summary
    await message.reply(
        f"🎉 **RTI Complete!**\n\n"
        f"✅ Success: `{success_count}/{len(episode_nums)}`\n"
        f"📊 Episodes: `{', '.join(str(e) for e in episode_nums)}`"
    )
