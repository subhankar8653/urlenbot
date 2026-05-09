"""
swift_downloader.py  v5
========================
Changes from v4:
  - AUTO RENAME: mega jaise build_auto_caption se proper filename milega
  - THUMBNAIL + COVER: /setpic se set ki gai custom thumbnail aur cover lagegi
  - SIZE ORDER: Chhoti file pehle upload hogi (360p → 720p → 1080p)
  - THREADED UPLOAD: asyncio.gather se parallel upload (fast)

Commands:
  /swift <url>        — download + upload
  /swiftencode <url>  — download + encode + upload
"""

import asyncio
import glob
import os
import re
import time

from pyrogram import Client, filters
from pyrogram.types import Message
from bs4 import BeautifulSoup

from .. import LOGGER, download_dir, app
from ..utils.helper import check_chat
from ..utils.uploads.telegram import upload_video
from ..utils.encoding import get_duration, get_thumbnail, get_width_height
from ..utils.auto_caption import build_auto_caption
from ..utils.database.access_db import db

try:
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    SELENIUM_OK = True
except ImportError:
    SELENIUM_OK = False


# ─────────────────────────────────────────────
#  Quality sort order (small → large)
# ─────────────────────────────────────────────
QUALITY_ORDER = {"360p": 0, "480p": 1, "720p": 2, "1080p": 3, "2160p": 4, "unknown": 99}


def _sort_by_size(files: list) -> list:
    """Files ko size ke hisab se sort karo — chhoti pehle (360p → 1080p)"""
    return sorted(files, key=lambda f: os.path.getsize(f) if os.path.isfile(f) else 0)


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
    for q in ["2160p", "1080p", "720p", "480p", "360p"]:
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
#  Auto rename — mega jaise
# ─────────────────────────────────────────────
async def _auto_rename(filepath: str, dl_dir: str) -> str:
    """
    build_auto_caption se proper naam banao aur ffmpeg se metadata SET karo.
    - title tag = clean filename (external player mein dikhega)
    - Parallel uploads ke liye unique temp paths
    Returns renamed filepath.
    """
    import random
    quality = _quality_from(os.path.basename(filepath))
    resolution = quality.replace("p", "") if quality != "unknown" else "OG"

    caption = build_auto_caption(filepath, resolution=resolution if resolution != "OG" else None)
    # proper_filename: spaces rakho, sirf illegal chars hatao
    proper_filename = re.sub(r'[<>:"/\\|?*]', '', caption).strip()

    # title tag: extension hatao, [@channel] hatao — clean readable naam
    title_tag = re.sub(r'\[@[^\]]+\]', '', os.path.splitext(proper_filename)[0]).strip()

    # Unique temp path — parallel uploads mein conflict avoid karo
    unique_id = f"{int(time.time())}_{random.randint(1000, 9999)}"
    temp_out = os.path.join(dl_dir, f"_tmp_{unique_id}.mp4")
    final_out = os.path.join(dl_dir, proper_filename)

    # ffmpeg: title SET karo (external player mein naam dikhega), garbage clear karo
    cmd = [
        'ffmpeg', '-y', '-i', filepath,
        '-map', '0', '-c', 'copy',
        '-metadata', f'title={title_tag}',
        '-metadata', 'comment=',
        '-metadata', 'description=',
        '-metadata', 'encoder=',
        '-metadata:s:v:0', 'title=',
        '-metadata:s:v:0', 'handler_name=VideoHandler',
        '-metadata:s:a:0', 'title=',
        '-metadata:s:a:0', 'handler_name=SoundHandler',
        temp_out
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        await asyncio.wait_for(proc.communicate(), timeout=600)

        if proc.returncode == 0 and os.path.exists(temp_out) and os.path.getsize(temp_out) > 0:
            try:
                os.remove(filepath)
            except Exception:
                pass
            try:
                os.rename(temp_out, final_out)
                return final_out
            except Exception:
                return temp_out
        else:
            if os.path.exists(temp_out):
                try: os.remove(temp_out)
                except Exception: pass
    except Exception as e:
        LOGGER.warning(f"[Swift] ffmpeg rename failed: {e}")
        if os.path.exists(temp_out):
            try: os.remove(temp_out)
            except Exception: pass

    # Fallback: sirf rename karo (metadata nahi badlega)
    try:
        os.rename(filepath, final_out)
        return final_out
    except Exception:
        pass

    return filepath


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

        time.sleep(8)
        _close_popups(driver, main)

        html = driver.page_source
        LOGGER.info(f"[Swift] Page loaded, source len={len(html)}")

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
            qualities_clicked = []
            for lnk in download_links[:4]:  # 4 qualities tak
                q = lnk["quality"]
                href = lnk["href"]

                try:
                    driver.execute_script(f"window.open('{href}', '_blank');")
                    time.sleep(2)
                    _close_popups(driver, main)
                    qualities_clicked.append(q)
                    LOGGER.info(f"[Swift] JS opened: {q}")
                    time.sleep(5)
                except Exception as e:
                    LOGGER.warning(f"[Swift] JS open failed: {e}")

            result["qualities"] = qualities_clicked

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
#  Single file upload (with auto-rename + thumb + cover)
# ─────────────────────────────────────────────
async def _upload_one_file(client, message, msg, filepath: str, dl_dir: str, encode: bool):
    """Ek file: rename → thumbnail → cover → upload"""
    fname_orig = os.path.basename(filepath)
    quality = _quality_from(fname_orig)
    size_mb = os.path.getsize(filepath) / (1024 * 1024)

    await msg.edit(
        f"🔄 **Renaming `{quality}`...**\n"
        f"📁 `{fname_orig}`\n"
        f"💾 `{size_mb:.1f} MB`"
    )

    # Auto rename (mega style)
    filepath = await _auto_rename(filepath, dl_dir)
    fname = os.path.basename(filepath)
    quality = _quality_from(fname)  # recalculate after rename

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
            return True

        c_time = time.time()
        duration = get_duration(filepath)

        # Custom thumbnail check (user ne /setpic se set kiya ho)
        user_id = message.from_user.id
        custom_thumb_id = await db.get_thumbnail(user_id)
        thumb = None
        custom_thumb_used = False

        if custom_thumb_id:
            try:
                import random
                # Unique path per file — parallel uploads mein conflict nahi hoga
                unique_id = f"{int(time.time())}_{random.randint(1000,9999)}"
                thumb_dir = os.path.join(dl_dir, "thumbs")
                os.makedirs(thumb_dir, exist_ok=True)
                thumb_path = os.path.join(thumb_dir, f"thumb_{unique_id}.jpg")

                downloaded = await app.download_media(
                    custom_thumb_id,
                    file_name=thumb_path
                )
                # Pyrogram returned path use karo (actual saved location)
                actual_path = downloaded if downloaded else thumb_path

                # .temp extension handle karo — Pyrogram kabhi kabhi aise save karta hai
                if actual_path and actual_path.endswith(".temp"):
                    renamed = actual_path.replace(".temp", ".jpg")
                    try:
                        os.rename(actual_path, renamed)
                        actual_path = renamed
                    except Exception:
                        pass

                if actual_path and os.path.isfile(actual_path) and os.path.getsize(actual_path) > 0:
                    thumb = actual_path
                    custom_thumb_used = True
                    LOGGER.info(f"[Swift] Custom thumb ready: {actual_path}")
                else:
                    LOGGER.warning(f"[Swift] Custom thumb not found at {actual_path}, using auto-thumb")
            except Exception as e:
                LOGGER.warning(f"[Swift] Thumb download error: {e}, using auto-thumb")

        if not thumb:
            # Fallback: video frame se auto thumbnail
            thumb = get_thumbnail(filepath, dl_dir, duration / 4 if duration else 0)
            custom_thumb_used = False

        # Cover pic — same as thumb
        cover = thumb if thumb and os.path.isfile(thumb) else None

        width, height = get_width_height(filepath)
        caption = f"<b>{fname}</b>"

        # file_name MUST match actual filename on disk
        # Telegram isi se external player mein naam dikhata hai
        disk_fname = os.path.basename(filepath)

        await upload_video(
            message, msg, filepath, caption,
            c_time, thumb, duration, width, height,
            file_name=disk_fname,
            cover=cover
        )

        # Thumb cleanup — custom thumb rakho (db mein hai), auto-generated hatao
        if not custom_thumb_used and thumb and os.path.isfile(thumb):
            try:
                os.remove(thumb)
            except Exception:
                pass

        return True

    except Exception as e:
        LOGGER.error(f"[Swift] Upload error ({quality}): {e}")
        await message.reply(f"⚠️ Upload failed `{fname}`: `{e}`")
        return False


# ─────────────────────────────────────────────
#  Core command logic
# ─────────────────────────────────────────────
async def _run_swift(client, message, swift_url: str, encode: bool):
    session_id = str(int(time.time()))
    dl_dir = os.path.join(download_dir, f"swift_{session_id}")
    os.makedirs(dl_dir, exist_ok=True)

    msg = await message.reply(
        f"🔗 **Swift Downloader v5**\n\n"
        f"🌐 `{swift_url}`\n\n"
        f"⏳ Same session se page visit + download ho raha hai..."
    )

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

    prog_task = asyncio.create_task(_progress_updater())

    result = await loop.run_in_executor(None, _scrape_and_download, swift_url, dl_dir)

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

    # ── Size ke hisab se sort — chhoti (360p) pehle, badi (1080p) baad mein ──
    files = _sort_by_size(files)

    await msg.edit(
        f"✅ **{len(files)} file(s) mili!**\n\n"
        f"📊 Order: `{' → '.join(_quality_from(f) for f in files)}`\n\n"
        f"📤 Upload ho raha hai (threaded)..."
    )

    # ── Threaded upload: ek ek status message banao, parallel upload karo ──
    # Note: Telegram flood control ki wajah se sequential better hota hai for video
    # Lekin hum asyncio.gather se concurrently rename + upload karenge
    # (Actually video uploads sequential hi acha hai, par rename async hogi)

    upload_messages = []
    for i, fp in enumerate(files):
        q = _quality_from(os.path.basename(fp))
        um = await message.reply(f"⏳ **Queued `{q}`** ({i+1}/{len(files)})")
        upload_messages.append(um)

    uploaded = 0

    async def _upload_task(filepath, um):
        nonlocal uploaded
        success = await _upload_one_file(client, message, um, filepath, dl_dir, encode)
        if success:
            uploaded += 1
            q = _quality_from(os.path.basename(filepath))
            try:
                await um.edit(f"✅ **Done `{q}`**")
            except Exception:
                pass
        return success

    # Sequential upload (Telegram flood control ke liye)
    # Parallel rename + upload hoga via asyncio tasks
    tasks = [
        asyncio.create_task(_upload_task(fp, um))
        for fp, um in zip(files, upload_messages)
    ]

    # asyncio.gather se sab saath chalaao — Telegram flood control handle hoga internally
    await asyncio.gather(*tasks, return_exceptions=True)

    qualities_done = [_quality_from(f) for f in files]
    await msg.edit(
        f"🎉 **Complete!**\n\n"
        f"✅ Uploaded : `{uploaded}/{len(files)}`\n"
        f"📊 Qualities : `{' → '.join(qualities_done)}`"
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
