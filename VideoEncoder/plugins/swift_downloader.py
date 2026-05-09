"""
swift_downloader.py
====================
Encode-bot mein swift.multiquality.click support add karta hai.

Usage:
  /swift <link>
  Example: /swift https://swift.multiquality.click/downlead/upW8gLwOun3vTP6/

Kya karta hai:
  1. Swift page open karta hai Selenium headless Chrome se
  2. Teeno quality (360p, 720p, 1080p) ke download buttons click karta hai
  3. Har file download hone ke baad Telegram pe upload karta hai
  4. Encode bhi kar sakta hai (optional: /swiftencode)

Requirements (install karo agar nahi hai):
  pip install selenium
  apt-get install -y google-chrome-stable  (ya chromium-browser)
"""

import asyncio
import glob
import os
import re
import time

from pyrogram import Client, filters
from pyrogram.types import Message

from .. import LOGGER, app, download_dir
from ..utils.helper import check_chat
from ..utils.uploads.telegram import upload_doc, upload_video
from ..utils.encoding import get_duration, get_thumbnail, get_width_height

# ─────────────────────────────────────────────
#  Selenium import (graceful fallback if missing)
# ─────────────────────────────────────────────
try:
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    SELENIUM_OK = True
except ImportError:
    SELENIUM_OK = False


# ─────────────────────────────────────────────
#  Helper: Headless Chrome driver setup
# ─────────────────────────────────────────────
def _make_driver(dl_dir: str):
    """
    Download-folder ke saath headless Chrome driver banata hai.
    Files automatically us folder mein save hongi.
    """
    options = webdriver.ChromeOptions()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1280,800")
    options.add_argument(
        "user-agent=Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )

    prefs = {
        "download.default_directory": dl_dir,
        "download.prompt_for_download": False,
        "safebrowsing.enabled": True,
        "profile.default_content_setting_values.automatic_downloads": 1,
    }
    options.add_experimental_option("prefs", prefs)

    # Railway / Colab / VPS pe Chrome binary different jagah ho sakta hai
    for binary in [
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
    ]:
        if os.path.exists(binary):
            options.binary_location = binary
            break

    return webdriver.Chrome(options=options)


# ─────────────────────────────────────────────
#  Close popup windows
# ─────────────────────────────────────────────
def _close_popups(driver, main_handle):
    try:
        for handle in driver.window_handles:
            if handle != main_handle:
                driver.switch_to.window(handle)
                driver.close()
        driver.switch_to.window(main_handle)
    except Exception:
        pass


# ─────────────────────────────────────────────
#  Page mein available qualities check
# ─────────────────────────────────────────────
def _check_qualities(driver) -> list:
    try:
        body_text = driver.find_element(By.TAG_NAME, "body").text.lower()
        return [q for q in ["360p", "480p", "720p", "1080p"] if q in body_text]
    except Exception:
        return []


# ─────────────────────────────────────────────
#  Quality button click
# ─────────────────────────────────────────────
def _click_quality(driver, quality: str) -> bool:
    try:
        for elem in driver.find_elements(By.TAG_NAME, "a"):
            if quality.lower() in elem.text.lower():
                driver.execute_script("arguments[0].click();", elem)
                LOGGER.info(f"[Swift] Clicked: {quality}")
                return True
        # button tag bhi try karo
        for elem in driver.find_elements(By.TAG_NAME, "button"):
            if quality.lower() in elem.text.lower():
                driver.execute_script("arguments[0].click();", elem)
                LOGGER.info(f"[Swift] Clicked button: {quality}")
                return True
    except Exception as e:
        LOGGER.warning(f"[Swift] Click error for {quality}: {e}")
    return False


# ─────────────────────────────────────────────
#  Download complete hone ka wait
# ─────────────────────────────────────────────
async def _wait_downloads(dl_dir: str, expected: int, msg, timeout: int = 600) -> list:
    start = time.time()
    last_edit = 0

    while True:
        in_progress = (
            glob.glob(f"{dl_dir}/*.crdownload")
            + glob.glob(f"{dl_dir}/*.part")
            + glob.glob(f"{dl_dir}/*.tmp")
        )
        done = []
        for f in glob.glob(f"{dl_dir}/*"):
            if f.endswith((".crdownload", ".part", ".tmp")):
                continue
            try:
                if os.path.getsize(f) > 1024:
                    done.append(f)
            except OSError:
                pass

        elapsed = int(time.time() - start)

        if time.time() - last_edit > 8:
            total = sum(os.path.getsize(f) for f in glob.glob(f"{dl_dir}/*") if os.path.isfile(f))
            mb = total / (1024 * 1024)
            bar_done = min(len(done), expected)
            bar = "█" * bar_done + "░" * (expected - bar_done)
            try:
                await msg.edit(
                    f"⬇️ **Downloading...**\n\n"
                    f"`[{bar}]`\n\n"
                    f"✅ Complete : `{len(done)}/{expected}`\n"
                    f"💾 Downloaded : `{mb:.1f} MB`\n"
                    f"⏱️ Elapsed : `{elapsed}s`\n"
                    f"📥 In Progress : `{len(in_progress)}`"
                )
            except Exception:
                pass
            last_edit = time.time()

        if len(done) >= expected and not in_progress:
            return done

        if elapsed > timeout:
            LOGGER.warning("[Swift] Download timeout!")
            return done

        await asyncio.sleep(3)


# ─────────────────────────────────────────────
#  Main scraper function (blocking → thread mein run)
# ─────────────────────────────────────────────
def _scrape_swift(swift_url: str, dl_dir: str) -> dict:
    """
    Returns:
      {
        "success": bool,
        "qualities": ["360p", "720p", "1080p"],   # found on page
        "clicked": ["360p", "720p", "1080p"],      # actually clicked
        "error": str or None
      }
    """
    driver = None
    result = {"success": False, "qualities": [], "clicked": [], "error": None}

    try:
        driver = _make_driver(dl_dir)
        driver.get(swift_url)
        main_handle = driver.current_window_handle

        # Page load hone do (max 30 sec wait for quality buttons)
        for attempt in range(15):
            qualities = _check_qualities(driver)
            if len(qualities) >= 1:
                break
            time.sleep(2)
            try:
                driver.refresh()
                time.sleep(3)
                _close_popups(driver, main_handle)
            except Exception:
                pass

        qualities = _check_qualities(driver)
        result["qualities"] = qualities

        if not qualities:
            result["error"] = "Page pe koi quality nahi mili (360p/720p/1080p)"
            return result

        # Download karo (max 3 qualities)
        clicked = []
        for q in qualities[:3]:
            if _click_quality(driver, q):
                clicked.append(q)
            time.sleep(2)
            _close_popups(driver, main_handle)
            time.sleep(6)

        result["clicked"] = clicked
        result["success"] = True

    except Exception as e:
        result["error"] = str(e)
        LOGGER.error(f"[Swift] Scrape error: {e}")
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass

    return result


# ─────────────────────────────────────────────
#  Quality label from filename
# ─────────────────────────────────────────────
def _quality_label(path: str) -> str:
    name = os.path.basename(path).lower()
    for q in ["1080p", "720p", "480p", "360p"]:
        if q in name:
            return q
    return "unknown"


# ─────────────────────────────────────────────
#  /swift command handler
# ─────────────────────────────────────────────
@Client.on_message(filters.command(["swift", "swiftdl"]))
async def swift_command(client: Client, message: Message):
    # Auth check
    c = await check_chat(message, chat="Sudo")
    if not c:
        return

    # Selenium check
    if not SELENIUM_OK:
        await message.reply(
            "❌ **Selenium install nahi hai!**\n\n"
            "Server pe ye run karo:\n"
            "`pip install selenium`\n"
            "`apt-get install -y google-chrome-stable`"
        )
        return

    # URL parse
    parts = message.text.split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply(
            "⚠️ **Usage:**\n"
            "`/swift https://swift.multiquality.click/downlead/XXXXXXXX/`"
        )
        return

    swift_url = parts[1].strip()

    # Basic URL validation
    if "swift.multiquality.click" not in swift_url and "multiquality" not in swift_url:
        await message.reply(
            "❌ Ye link swift.multiquality.click ka nahi lag raha!\n\n"
            "Valid format:\n"
            "`https://swift.multiquality.click/downlead/XXXXXXXX/`"
        )
        return

    # Download folder (alag subfolder taaki dusre files se conflict na ho)
    session_id = str(int(time.time()))
    dl_dir = os.path.join(download_dir, f"swift_{session_id}")
    os.makedirs(dl_dir, exist_ok=True)

    msg = await message.reply(
        f"🔗 **Swift Downloader Started!**\n\n"
        f"🌐 URL: `{swift_url}`\n\n"
        f"⏳ Page load ho raha hai..."
    )

    # ── Step 1: Scrape (blocking, run in thread) ──
    try:
        loop = asyncio.get_event_loop()
        scrape_result = await loop.run_in_executor(
            None, _scrape_swift, swift_url, dl_dir
        )
    except Exception as e:
        await msg.edit(f"❌ **Scraping failed:**\n`{e}`")
        return

    if not scrape_result["success"]:
        await msg.edit(
            f"❌ **Download start nahi hua!**\n\n"
            f"Error: `{scrape_result.get('error', 'Unknown')}`\n"
            f"Qualities found: `{scrape_result['qualities']}`"
        )
        return

    await msg.edit(
        f"✅ **Qualities Found:** `{', '.join(scrape_result['qualities'])}`\n"
        f"🖱️ **Clicked:** `{', '.join(scrape_result['clicked'])}`\n\n"
        f"⬇️ Files download ho rahe hain..."
    )

    # ── Step 2: Wait for downloads ──
    expected = max(len(scrape_result["clicked"]), 1)
    files = await _wait_downloads(dl_dir, expected, msg, timeout=900)

    if not files:
        await msg.edit("❌ **Koi file download nahi hui!** Timeout ya error.")
        return

    # ── Step 3: Upload to Telegram ──
    await msg.edit(
        f"📤 **Upload Starting...**\n"
        f"📁 Files: `{len(files)}`"
    )

    c_time = time.time()
    uploaded = 0
    failed = 0

    for filepath in sorted(files):
        quality = _quality_label(filepath)
        fname = os.path.basename(filepath)
        size_mb = os.path.getsize(filepath) / (1024 * 1024)

        await msg.edit(
            f"📤 **Uploading...**\n\n"
            f"📊 Quality : `{quality}`\n"
            f"📁 File    : `{fname}`\n"
            f"💾 Size    : `{size_mb:.1f} MB`\n"
            f"✅ Done    : `{uploaded}/{len(files)}`"
        )

        try:
            # Video file hai toh video mode, warna doc
            ext = os.path.splitext(fname)[1].lower()
            is_video = ext in [".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v"]

            if is_video:
                duration = get_duration(filepath)
                thumb = get_thumbnail(filepath, dl_dir, duration / 4 if duration else 0)
                width, height = get_width_height(filepath)
                await upload_video(
                    message, msg, filepath, fname,
                    c_time, thumb, duration, width, height
                )
            else:
                await upload_doc(message, msg, c_time, fname, filepath)

            uploaded += 1
            LOGGER.info(f"[Swift] Uploaded: {fname}")

        except Exception as e:
            LOGGER.error(f"[Swift] Upload failed for {fname}: {e}")
            await message.reply(f"⚠️ Upload failed: `{fname}`\nError: `{e}`")
            failed += 1

        # Small delay between uploads
        await asyncio.sleep(2)

    # ── Step 4: Summary ──
    await msg.edit(
        f"🎉 **Swift Download Complete!**\n\n"
        f"✅ Uploaded  : `{uploaded}`\n"
        f"❌ Failed    : `{failed}`\n"
        f"📊 Qualities : `{', '.join(scrape_result['qualities'])}`\n\n"
        f"_Files automatically delete ho gaye._"
    )

    # Cleanup
    try:
        import shutil
        shutil.rmtree(dl_dir, ignore_errors=True)
    except Exception:
        pass


# ─────────────────────────────────────────────
#  /swiftencode - Download + Encode + Upload
# ─────────────────────────────────────────────
@Client.on_message(filters.command("swiftencode"))
async def swift_encode_command(client: Client, message: Message):
    """
    /swiftencode <swift_url> - Download karke encode bhi karta hai
    """
    c = await check_chat(message, chat="Sudo")
    if not c:
        return

    if not SELENIUM_OK:
        await message.reply("❌ Selenium install nahi hai!")
        return

    parts = message.text.split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply(
            "⚠️ **Usage:**\n"
            "`/swiftencode https://swift.multiquality.click/downlead/XXXXXXXX/`\n\n"
            "Ye download ke baad encode bhi karega."
        )
        return

    swift_url = parts[1].strip()

    session_id = str(int(time.time()))
    dl_dir = os.path.join(download_dir, f"swift_{session_id}")
    os.makedirs(dl_dir, exist_ok=True)

    msg = await message.reply(
        f"🔗 **Swift + Encode Mode**\n\n"
        f"⏳ Downloading + Encoding karega..."
    )

    # Download
    loop = asyncio.get_event_loop()
    scrape_result = await loop.run_in_executor(None, _scrape_swift, swift_url, dl_dir)

    if not scrape_result["success"]:
        await msg.edit(f"❌ Failed: `{scrape_result.get('error', 'Unknown')}`")
        return

    expected = max(len(scrape_result["clicked"]), 1)
    files = await _wait_downloads(dl_dir, expected, msg, timeout=900)

    if not files:
        await msg.edit("❌ Koi file nahi mili!")
        return

    # Encode each file
    from ..utils.helper import handle_encode

    for filepath in sorted(files):
        fname = os.path.basename(filepath)
        await msg.edit(f"⚙️ **Encoding:** `{fname}`")
        try:
            await handle_encode(filepath, message, msg)
        except Exception as e:
            await message.reply(f"⚠️ Encode failed: `{fname}`\n`{e}`")

    await msg.edit("✅ **Swift + Encode Complete!**")

    # Cleanup
    try:
        import shutil
        shutil.rmtree(dl_dir, ignore_errors=True)
    except Exception:
        pass
