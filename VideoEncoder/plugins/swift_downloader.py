"""
swift_downloader.py  v3
========================
Strategy (image se pata chala):
  - Page pe buttons ke andar direct .mp4 download links hain (href attribute)
  - Selenium se page render karo, BeautifulSoup se sabhi href links nikalo
  - Jo links download CDN ki hain unhe aiohttp se download karo
  - Selenium click ki zarurat nahi — seedha link grab karo!

Commands:
  /swift <url>        — download + upload
  /swiftencode <url>  — download + encode + upload
"""

import asyncio
import os
import re
import time

import aiohttp
import aiofiles
from pyrogram import Client, filters
from pyrogram.types import Message
from bs4 import BeautifulSoup

from .. import LOGGER, download_dir
from ..utils.helper import check_chat
from ..utils.uploads.telegram import upload_doc, upload_video
from ..utils.encoding import get_duration, get_thumbnail, get_width_height

# ─────────────────────────────────────────────
#  Selenium import (graceful fallback)
# ─────────────────────────────────────────────
try:
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    SELENIUM_OK = True
except ImportError:
    SELENIUM_OK = False


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://swift.multiquality.click/",
    "Accept": "*/*",
}


# ─────────────────────────────────────────────
#  Driver setup (sirf page render ke liye)
# ─────────────────────────────────────────────
def _make_driver():
    options = webdriver.ChromeOptions()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1280,800")
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
    prefs = {"profile.managed_default_content_settings.images": 2}
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

    return webdriver.Chrome(options=options)


# ─────────────────────────────────────────────
#  Close popup tabs
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
#  Quality label from text/URL
# ─────────────────────────────────────────────
def _quality_from_text(text: str) -> str:
    t = text.lower()
    for q in ["1080p", "720p", "480p", "360p"]:
        if q in t:
            return q
    return "unknown"


# ─────────────────────────────────────────────
#  Page se download links extract karo
# ─────────────────────────────────────────────
def _extract_links(swift_url: str) -> list:
    """
    Returns list of dicts:
      [{"url": "https://...", "quality": "720p", "label": "720P HD"}, ...]
    """
    driver = None
    links = []

    try:
        driver = _make_driver()
        driver.get(swift_url)
        main = driver.current_window_handle

        # JS render hone do — 10 sec
        time.sleep(10)
        _close_popups(driver, main)

        html = driver.page_source
        LOGGER.info(f"[Swift] Page source length: {len(html)}")

        soup = BeautifulSoup(html, "html.parser")

        # Method 1: Sabhi <a href> tags
        seen = set()
        for tag in soup.find_all("a", href=True):
            href = tag.get("href", "").strip()
            label = tag.get_text(separator=" ", strip=True)

            if not href or href in seen:
                continue
            if href.startswith("#") or "javascript" in href:
                continue

            # Long CDN URLs ya mp4 links
            is_download = (
                ".mp4" in href.lower()
                or "download" in href.lower()
                or len(href) > 60
            )
            if not is_download:
                continue

            quality = _quality_from_text(label + " " + href)
            seen.add(href)
            links.append({"url": href, "quality": quality, "label": label})
            LOGGER.info(f"[Swift] <a> found: {quality} — {href[:80]}")

        # Method 2: Regex on raw HTML (agar <a> se nahi mila)
        if not links:
            LOGGER.warning("[Swift] No <a> links, trying regex...")
            patterns = [
                r'https?://[a-z0-9\.\-]+/download/[A-Za-z0-9_\-/=+]{30,}',
                r'https?://[^\s\'"<>]+\.mp4[^\s\'"<>]*',
                r'"(https?://[^"]{60,})"',
            ]
            seen2 = set()
            for pat in patterns:
                for m in re.findall(pat, html):
                    if m not in seen2 and "swift.multiquality" not in m:
                        quality = _quality_from_text(m)
                        seen2.add(m)
                        links.append({"url": m, "quality": quality, "label": m[:50]})
                        LOGGER.info(f"[Swift] Regex found: {m[:80]}")

    except Exception as e:
        LOGGER.error(f"[Swift] Extract error: {e}")
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass

    return links


# ─────────────────────────────────────────────
#  Async download with progress
# ─────────────────────────────────────────────
async def _download_file(url: str, filepath: str, msg, quality: str) -> bool:
    try:
        async with aiohttp.ClientSession(headers=HEADERS) as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=1800)) as resp:
                if resp.status != 200:
                    LOGGER.error(f"[Swift] HTTP {resp.status} for {url[:80]}")
                    return False

                total = int(resp.headers.get("Content-Length", 0))
                downloaded = 0
                last_edit = 0

                async with aiofiles.open(filepath, "wb") as f:
                    async for chunk in resp.content.iter_chunked(512 * 1024):
                        await f.write(chunk)
                        downloaded += len(chunk)

                        if time.time() - last_edit > 5:
                            if total:
                                pct = downloaded / total * 100
                                bar = "█" * int(pct / 10) + "░" * (10 - int(pct / 10))
                                size_mb = downloaded / (1024 * 1024)
                                total_mb = total / (1024 * 1024)
                                try:
                                    await msg.edit(
                                        f"⬇️ **Downloading {quality}...**\n\n"
                                        f"`[{bar}]` {pct:.1f}%\n"
                                        f"💾 `{size_mb:.1f} / {total_mb:.1f} MB`"
                                    )
                                except Exception:
                                    pass
                            last_edit = time.time()

        return True
    except Exception as e:
        LOGGER.error(f"[Swift] Download error: {e}")
        return False


# ─────────────────────────────────────────────
#  Core logic
# ─────────────────────────────────────────────
async def _run_swift(client, message, swift_url: str, encode: bool):
    session_id = str(int(time.time()))
    dl_dir = os.path.join(download_dir, f"swift_{session_id}")
    os.makedirs(dl_dir, exist_ok=True)

    msg = await message.reply(
        f"🔗 **Swift Downloader v3**\n\n"
        f"🌐 `{swift_url}`\n\n"
        f"⏳ Page se links extract ho rahe hain..."
    )

    # Step 1: Links extract (blocking thread)
    loop = asyncio.get_event_loop()
    links = await loop.run_in_executor(None, _extract_links, swift_url)

    if not links:
        await msg.edit(
            "❌ **Koi download link nahi mila!**\n\n"
            "Page render nahi hua ya links format different hai.\n"
            "Railway logs check karo (`[Swift]` lines)."
        )
        return

    # Unique qualities
    seen_q = {}
    for lnk in links:
        q = lnk["quality"]
        if q not in seen_q:
            seen_q[q] = lnk

    final_links = list(seen_q.values())

    await msg.edit(
        f"✅ **{len(final_links)} download links mile!**\n\n"
        + "\n".join(f"📊 `{l['quality']}` — `{l['label'][:35]}`" for l in final_links)
        + "\n\n⬇️ Downloading..."
    )

    # Step 2: Download each
    downloaded_files = []
    for lnk in final_links:
        quality = lnk["quality"]
        url = lnk["url"]
        fname = f"{quality}_{session_id}.mp4"
        filepath = os.path.join(dl_dir, fname)

        ok = await _download_file(url, filepath, msg, quality)
        if ok and os.path.exists(filepath) and os.path.getsize(filepath) > 1024:
            downloaded_files.append((filepath, quality))
        else:
            await message.reply(f"⚠️ Download failed: `{quality}`")

    if not downloaded_files:
        await msg.edit("❌ **Koi file download nahi hui!**")
        return

    # Step 3: Upload / Encode
    c_time = time.time()
    uploaded = 0

    for filepath, quality in downloaded_files:
        fname = os.path.basename(filepath)
        size_mb = os.path.getsize(filepath) / (1024 * 1024)

        await msg.edit(
            f"📤 **Uploading `{quality}`...**\n"
            f"💾 Size: `{size_mb:.1f} MB`\n"
            f"✅ Done: `{uploaded}/{len(downloaded_files)}`"
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
            LOGGER.error(f"[Swift] Upload error {quality}: {e}")
            await message.reply(f"⚠️ Upload failed `{quality}`: `{e}`")

        await asyncio.sleep(1)

    await msg.edit(
        f"🎉 **Complete!**\n\n"
        f"✅ Uploaded : `{uploaded}/{len(downloaded_files)}`\n"
        f"📊 Qualities: `{', '.join(q for _, q in downloaded_files)}`"
    )

    try:
        import shutil
        shutil.rmtree(dl_dir, ignore_errors=True)
    except Exception:
        pass


# ─────────────────────────────────────────────
#  /swift command
# ─────────────────────────────────────────────
@Client.on_message(filters.command(["swift", "swiftdl"]))
async def swift_command(client: Client, message: Message):
    c = await check_chat(message, chat="Sudo")
    if not c:
        return

    if not SELENIUM_OK:
        await message.reply(
            "❌ **Selenium install nahi hai!**\n\n"
            "`pip install selenium`\n"
            "`apt-get install -y chromium chromium-driver`"
        )
        return

    parts = message.text.split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply(
            "⚠️ **Usage:**\n"
            "`/swift https://swift.multiquality.click/downlead/XXXXXXXX/`"
        )
        return

    await _run_swift(client, message, parts[1].strip(), encode=False)


# ─────────────────────────────────────────────
#  /swiftencode command
# ─────────────────────────────────────────────
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
        await message.reply(
            "⚠️ **Usage:**\n"
            "`/swiftencode https://swift.multiquality.click/downlead/XXXXXXXX/`"
        )
        return

    await _run_swift(client, message, parts[1].strip(), encode=True)
