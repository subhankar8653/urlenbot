"""
swift_downloader.py  v4
========================
FIX: IP binding issue.
  - Page visit aur download DONO same Selenium session se hoga
  - Same browser = same IP = same cookies = download works
  - Chrome ko download folder set karke buttons click karayenge
  - Chrome khud file save kar lega — koi alag HTTP request nahi

Commands:
  /swift <url>        — download + upload
  /swiftencode <url>  — download + encode + upload
"""

import asyncio
import glob
import os
import time

from pyrogram import Client, filters
from pyrogram.types import Message
from bs4 import BeautifulSoup

from .. import LOGGER, download_dir
from ..utils.helper import check_chat
from ..utils.uploads.telegram import upload_video
from ..utils.encoding import get_duration, get_thumbnail, get_width_height

try:
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    SELENIUM_OK = True
except ImportError:
    SELENIUM_OK = False


# ─────────────────────────────────────────────
#  Driver — download folder set, same session
# ─────────────────────────────────────────────
def _make_driver(dl_dir: str):
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )

    # Download folder Chrome ke andar set karo
    prefs = {
        "download.default_directory": dl_dir,
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "safebrowsing.enabled": True,
        "safebrowsing.disable_download_protection": True,
        "profile.default_content_setting_values.automatic_downloads": 1,
        "profile.content_settings.exceptions.automatic_downloads.*.setting": 1,
    }
    options.add_experimental_option("prefs", prefs)

    for binary in [
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
    ]:
        if os.path.exists(binary):
            options.binary_location = binary
            break

    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(60)

    # Chrome headless mein download enable karna (CDP command)
    try:
        driver.execute_cdp_cmd(
            "Page.setDownloadBehavior",
            {"behavior": "allow", "downloadPath": dl_dir},
        )
    except Exception:
        pass

    return driver


# ─────────────────────────────────────────────
#  Popup tabs band karo
# ─────────────────────────────────────────────
def _close_popups(driver, main):
    try:
        for h in driver.window_handles:
            if h != main:
                driver.switch_to.window(h)
                driver.close()
        driver.switch_to.window(main)
    except Exception:
        pass


# ─────────────────────────────────────────────
#  Quality label from text
# ─────────────────────────────────────────────
def _quality_from(text: str) -> str:
    t = text.lower()
    for q in ["1080p", "720p", "480p", "360p"]:
        if q in t:
            return q
    return "unknown"


# ─────────────────────────────────────────────
#  Download complete hone ka wait
# ─────────────────────────────────────────────
def _get_done_files(dl_dir: str) -> list:
    done = []
    for f in glob.glob(os.path.join(dl_dir, "*")):
        if f.endswith((".crdownload", ".part", ".tmp")):
            continue
        try:
            if os.path.getsize(f) > 100 * 1024:  # > 100KB
                done.append(f)
        except OSError:
            pass
    return done


def _in_progress(dl_dir: str) -> list:
    return (
        glob.glob(os.path.join(dl_dir, "*.crdownload"))
        + glob.glob(os.path.join(dl_dir, "*.part"))
        + glob.glob(os.path.join(dl_dir, "*.tmp"))
    )


# ─────────────────────────────────────────────
#  Main scrape + download (blocking, same session)
# ─────────────────────────────────────────────
def _scrape_and_download(swift_url: str, dl_dir: str, status_cb=None) -> dict:
    """
    Same Selenium session mein:
      1. Page visit karo
      2. Download links/buttons dhundo
      3. Har button click karo → Chrome file save kare
      4. Files ka wait karo

    Returns: {"files": [...paths], "qualities": [...], "error": str or None}
    """
    driver = None
    result = {"files": [], "qualities": [], "error": None}

    try:
        driver = _make_driver(dl_dir)
        LOGGER.info(f"[Swift] Opening: {swift_url}")
        driver.get(swift_url)
        main = driver.current_window_handle

        # JS render hone do
        time.sleep(8)
        _close_popups(driver, main)

        html = driver.page_source
        LOGGER.info(f"[Swift] Page loaded, source len={len(html)}")

        # ── Links dhundo (href attribute mein) ──
        soup = BeautifulSoup(html, "html.parser")
        download_links = []
        seen = set()

        for tag in soup.find_all("a", href=True):
            href = tag.get("href", "").strip()
            label = tag.get_text(separator=" ", strip=True)
            if not href or href in seen:
                continue
            if href.startswith("#") or "javascript" in href:
                continue
            if len(href) < 30:
                continue

            quality = _quality_from(label + " " + href)
            seen.add(href)
            download_links.append({
                "href": href, "quality": quality, "label": label, "tag": tag
            })
            LOGGER.info(f"[Swift] Found href: {quality} | {href[:80]}")

        if not download_links:
            # Fallback: visible buttons click karo (old method)
            LOGGER.warning("[Swift] No hrefs found, trying visible button click...")
            qualities_to_try = ["360p", "480p", "720p", "1080p"]
            clicked = []
            for q in qualities_to_try:
                try:
                    elems = driver.find_elements(By.XPATH,
                        f"//*[contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'{q}')]"
                    )
                    for elem in elems:
                        try:
                            driver.execute_script("arguments[0].scrollIntoView();", elem)
                            driver.execute_script("arguments[0].click();", elem)
                            LOGGER.info(f"[Swift] Button clicked: {q}")
                            clicked.append(q)
                            time.sleep(3)
                            _close_popups(driver, main)
                            break
                        except Exception:
                            pass
                except Exception:
                    pass

            if not clicked:
                result["error"] = "Na href links mile na buttons — page render fail"
                return result

            result["qualities"] = clicked

        else:
            # ── Same session mein JS click karo (same IP/cookies) ──
            qualities_clicked = []
            for lnk in download_links[:3]:
                q = lnk["quality"]
                href = lnk["href"]

                # JS se navigate karo same session mein (ya window.open)
                try:
                    # window.location.href se jayenge — same session
                    driver.execute_script(f"window.open('{href}', '_blank');")
                    time.sleep(2)
                    _close_popups(driver, main)
                    qualities_clicked.append(q)
                    LOGGER.info(f"[Swift] JS opened: {q}")
                    time.sleep(5)
                except Exception as e:
                    LOGGER.warning(f"[Swift] JS open failed: {e}")

            result["qualities"] = qualities_clicked

        # ── Files download hone ka wait (max 20 min) ──
        LOGGER.info("[Swift] Waiting for downloads...")
        start = time.time()
        expected = max(len(result["qualities"]), 1)

        while True:
            done = _get_done_files(dl_dir)
            in_prog = _in_progress(dl_dir)
            elapsed = int(time.time() - start)

            LOGGER.info(f"[Swift] Done={len(done)}, InProg={len(in_prog)}, Elapsed={elapsed}s")

            if len(done) >= expected and not in_prog:
                break
            if elapsed > 5 and len(done) >= 1 and not in_prog:
                # Sab aa gaya jo aana tha
                break
            if elapsed > 1200:
                LOGGER.warning("[Swift] Timeout!")
                break

            time.sleep(5)

        result["files"] = _get_done_files(dl_dir)

    except Exception as e:
        result["error"] = str(e)
        LOGGER.error(f"[Swift] Error: {e}")
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass

    return result


# ─────────────────────────────────────────────
#  Core command logic
# ─────────────────────────────────────────────
async def _run_swift(client, message, swift_url: str, encode: bool):
    session_id = str(int(time.time()))
    dl_dir = os.path.join(download_dir, f"swift_{session_id}")
    os.makedirs(dl_dir, exist_ok=True)

    msg = await message.reply(
        f"🔗 **Swift Downloader v4**\n\n"
        f"🌐 `{swift_url}`\n\n"
        f"⏳ Same session se page visit + download ho raha hai..."
    )

    # Progress update thread mein chal raha hai — blocking call
    loop = asyncio.get_event_loop()

    async def _progress_updater():
        start = time.time()
        while True:
            done = _get_done_files(dl_dir)
            in_prog = _in_progress(dl_dir)
            elapsed = int(time.time() - start)
            total_mb = sum(
                os.path.getsize(f) for f in glob.glob(os.path.join(dl_dir, "*"))
                if os.path.isfile(f)
            ) / (1024 * 1024)
            try:
                await msg.edit(
                    f"⬇️ **Downloading...**\n\n"
                    f"✅ Complete : `{len(done)}`\n"
                    f"📥 In Progress : `{len(in_prog)}`\n"
                    f"💾 Downloaded : `{total_mb:.1f} MB`\n"
                    f"⏱️ Elapsed : `{elapsed}s`"
                )
            except Exception:
                pass
            await asyncio.sleep(8)

    # Progress task start
    prog_task = asyncio.create_task(_progress_updater())

    # Blocking scrape+download thread mein
    result = await loop.run_in_executor(None, _scrape_and_download, swift_url, dl_dir)

    # Progress task band karo
    prog_task.cancel()
    try:
        await prog_task
    except asyncio.CancelledError:
        pass

    if result["error"] and not result["files"]:
        await msg.edit(
            f"❌ **Failed!**\n\n"
            f"Error: `{result['error']}`\n\n"
            f"Railway logs mein `[Swift]` lines check karo."
        )
        return

    files = result["files"]
    if not files:
        await msg.edit("❌ **Koi file download nahi hui!**")
        return

    await msg.edit(
        f"✅ **{len(files)} file(s) mili!**\n\n"
        f"📤 Upload ho raha hai..."
    )

    c_time = time.time()
    uploaded = 0

    for filepath in sorted(files):
        fname = os.path.basename(filepath)
        quality = _quality_from(fname)
        size_mb = os.path.getsize(filepath) / (1024 * 1024)

        await msg.edit(
            f"📤 **Uploading `{quality}`...**\n"
            f"📁 `{fname}`\n"
            f"💾 `{size_mb:.1f} MB`"
        )

        try:
            if encode:
                from ..utils.helper import handle_encode
                await msg.edit(f"⚙️ **Encoding `{quality}`...**")
                await handle_encode(filepath, message, msg)
            else:
                duration = get_duration(filepath)
                thumb = get_thumbnail(filepath, dl_dir, duration / 4 if duration else 0)
                width, height = get_width_height(filepath)
                await upload_video(
                    message, msg, filepath, fname,
                    c_time, thumb, duration, width, height
                )
            uploaded += 1
        except Exception as e:
            LOGGER.error(f"[Swift] Upload error: {e}")
            await message.reply(f"⚠️ Upload failed `{fname}`: `{e}`")

        await asyncio.sleep(1)

    await msg.edit(
        f"🎉 **Complete!**\n\n"
        f"✅ Uploaded : `{uploaded}/{len(files)}`\n"
        f"📊 Qualities : `{', '.join(_quality_from(f) for f in sorted(files))}`"
    )

    try:
        import shutil
        shutil.rmtree(dl_dir, ignore_errors=True)
    except Exception:
        pass


# ─────────────────────────────────────────────
#  Commands
# ─────────────────────────────────────────────
@Client.on_message(filters.command(["swift", "swiftdl"]))
async def swift_command(client: Client, message: Message):
    c = await check_chat(message, chat="Sudo")
    if not c:
        return
    if not SELENIUM_OK:
        await message.reply("❌ Selenium install nahi hai!\n`pip install selenium`\n`apt-get install -y chromium chromium-driver`")
        return
    parts = message.text.split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply("⚠️ Usage:\n`/swift https://swift.multiquality.click/downlead/XXXXXXXX/`")
        return
    await _run_swift(client, message, parts[1].strip(), encode=False)


@Client.on_message(filters.command("swiftencode"))
async def swift_encode_command(client: Client, message: Message):
    c = await check_chat(message, chat="Sudo")
    if not c:
        return
    if not SELENIUM_OK:
        await message.reply("❌ Selenium install nahi hai!")
        return
    parts = message.text.split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply("⚠️ Usage:\n`/swiftencode https://swift.multiquality.click/downlead/XXXXXXXX/`")
        return
    await _run_swift(client, message, parts[1].strip(), encode=True)
