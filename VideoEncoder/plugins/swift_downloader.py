"""
swift_downloader.py  v6
========================
Changes from v5:
  - SCAN-FIRST: Page open hone ke baad immediately download nahi hoga
  - 360p GATE: Har 1 second pe scan karo — jab tak 360p button visible na ho
  - 20s TIMEOUT: 20 seconds baad bhi 360p nahi mila to process cancel
  - MISSING QUALITY SKIP: Jo quality page pe nahi hai usse download + upload skip
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
from ..utils.uploads.telegram import upload_video, _make_uploader_client
from ..utils.encoding import get_duration, get_thumbnail, get_width_height
from ..utils.auto_caption import build_auto_caption
from ..utils.database.access_db import db
from ..plugins.custompic import get_custompic_for_file

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

    # Railway pe headless=new better JS rendering karta hai (old headless JS execute karta hai)
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-software-rasterizer")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-background-networking")
    options.add_argument("--disable-default-apps")
    options.add_argument("--disable-sync")
    options.add_argument("--disable-translate")
    options.add_argument("--hide-scrollbars")
    options.add_argument("--metrics-recording-only")
    options.add_argument("--mute-audio")
    options.add_argument("--no-first-run")
    options.add_argument("--safebrowsing-disable-auto-update")
    options.add_argument("--ignore-certificate-errors")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--enable-javascript")
    options.add_argument("--allow-running-insecure-content")
    # Shared memory limit increase — renderer timeout fix
    options.add_argument("--shm-size=2gb")
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )

    prefs = {
        "download.default_directory": dl_dir,
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "safebrowsing.enabled": False,
        "safebrowsing.disable_download_protection": True,
        "profile.default_content_setting_values.automatic_downloads": 1,
        "profile.content_settings.exceptions.automatic_downloads.*.setting": 1,
        # Images load mat karo — page faster load hoga
        "profile.managed_default_content_settings.images": 2,
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

    # System chromedriver use karo — selenium-manager ko bypass karo
    # (Railway pe selenium-manager chromedriver download karta hai but Chrome nahi hota)
    from selenium.webdriver.chrome.service import Service as ChromeService
    chromedriver_paths = [
        "/usr/bin/chromedriver",
        "/usr/lib/chromium/chromedriver",
        "/usr/lib/chromium-browser/chromedriver",
    ]
    service = None
    for cd_path in chromedriver_paths:
        if os.path.exists(cd_path):
            service = ChromeService(executable_path=cd_path)
            break

    if service:
        driver = webdriver.Chrome(service=service, options=options)
    else:
        driver = webdriver.Chrome(options=options)
    # Page load timeout badha diya — slow Railway server ke liye
    driver.set_page_load_timeout(90)
    driver.set_script_timeout(30)

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
        if not os.path.isfile(f):  # directories skip karo (thumbs/ folder etc)
            continue
        if f.endswith((".crdownload", ".part", ".tmp")):
            continue
        basename = os.path.basename(f)
        if basename.startswith("_tmp_"):  # temp rename files skip
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


def _try_requests_scrape(swift_url: str) -> list:
    """
    Pehle requests se static HTML try karo — JS-heavy page nahi hai toh
    Selenium se tez aur reliable hoga.
    Returns list of download hrefs, ya empty list agar fail hua.
    """
    try:
        import requests as req_lib
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        r = req_lib.get(swift_url, headers=headers, timeout=20)
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "html.parser")
        links = []
        seen = set()
        for tag in soup.find_all("a", href=True):
            href = tag.get("href", "").strip()
            if not href or href in seen:
                continue
            if href.startswith("#") or "javascript" in href:
                continue
            if len(href) < 10:
                continue
            label = tag.get_text(separator=" ", strip=True)
            quality = _quality_from(label + " " + href)
            seen.add(href)
            links.append({"href": href, "quality": quality, "label": label})
            LOGGER.info(f"[Swift/requests] Found: {quality} | {href[:80]}")
        return links
    except Exception as e:
        LOGGER.warning(f"[Swift/requests] Failed: {e}")
        return []


# ─────────────────────────────────────────────
#  Main scrape + download (blocking, same session)
# ─────────────────────────────────────────────
def _scan_for_360p(driver) -> bool:
    """
    Current page pe 360p button/link visible hai ya nahi check karo.
    Returns True agar 360p mil gaya (visible, non-hidden element).
    """
    try:
        # Method 1: a.dl-btn elements mein 360p text dhundo
        dl_btns = driver.find_elements(By.CSS_SELECTOR, "a.dl-btn")
        for btn in dl_btns:
            try:
                label = btn.text.strip().lower()
                href = btn.get_attribute("href") or ""
                classes = btn.get_attribute("class") or ""
                if "d-none" in classes:
                    continue
                if "360p" in label or "360p" in href.lower():
                    LOGGER.info(f"[Swift] 360p found via dl-btn: {label or href[:60]}")
                    return True
            except Exception:
                pass

        # Method 2: XPATH se koi bhi element jo 360p text contain kare
        elems = driver.find_elements(By.XPATH,
            "//*[contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'360p')]"
        )
        for elem in elems:
            try:
                if elem.is_displayed():
                    LOGGER.info(f"[Swift] 360p found via XPATH: {elem.tag_name}")
                    return True
            except Exception:
                pass

        # Method 3: BeautifulSoup se page source parse karo
        html = driver.page_source
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup.find_all(["a", "button", "span", "div"]):
            classes = tag.get("class", [])
            if "d-none" in classes:
                continue
            text = tag.get_text(separator=" ", strip=True).lower()
            href = tag.get("href", "").lower()
            if "360p" in text or "360p" in href:
                LOGGER.info(f"[Swift] 360p found via BS4: {tag.name}")
                return True

    except Exception as e:
        LOGGER.warning(f"[Swift] 360p scan error: {e}")

    return False


def _collect_visible_links(driver) -> list:
    """
    Page pe saare visible download links collect karo.
    Returns list of {"href", "quality", "label"} — sirf jo page pe actually present hain.
    """
    html = driver.page_source
    soup = BeautifulSoup(html, "html.parser")
    download_links = []
    seen = set()

    # Pass 1: BeautifulSoup se visible anchors
    for tag in soup.find_all("a", href=True):
        href = tag.get("href", "").strip()
        label = tag.get_text(separator=" ", strip=True)
        if not href or href in seen:
            continue
        if href.startswith("#") or "javascript" in href or href.startswith("about:"):
            continue
        if len(href) < 10:
            continue
        tag_classes = tag.get("class", [])
        if "d-none" in tag_classes:
            LOGGER.info(f"[Swift] Skipping d-none: {href[:60]}")
            continue
        quality = _quality_from(label + " " + href)
        seen.add(href)
        download_links.append({"href": href, "quality": quality, "label": label})
        LOGGER.info(f"[Swift] Collected: {quality} | {href[:80]}")

    # Pass 2: Selenium se visible dl-btn (JS rendered ones)
    if not download_links:
        try:
            dl_btns = driver.find_elements(By.CSS_SELECTOR, "a.dl-btn")
            for btn in dl_btns:
                try:
                    href = btn.get_attribute("href") or ""
                    label = btn.text.strip()
                    classes = btn.get_attribute("class") or ""
                    if not href or href.startswith("about:") or len(href) < 10:
                        continue
                    if "d-none" in classes:
                        continue
                    quality = _quality_from(label + " " + href)
                    if href not in seen:
                        seen.add(href)
                        download_links.append({"href": href, "quality": quality, "label": label})
                        LOGGER.info(f"[Swift] Selenium dl-btn: {quality} | {href[:80]}")
                except Exception:
                    pass
        except Exception as e:
            LOGGER.warning(f"[Swift] Selenium dl-btn collect failed: {e}")

    return download_links


def _scrape_and_download(swift_url: str, dl_dir: str, status_cb=None, quality_filter: str = None) -> dict:
    """
    Same Selenium session mein:
      1. Page visit karo — immediately download mat karo
      2. Har 1 second pe scan karo — 360p visible hone ka wait karo (max 20s)
      3. 20s baad bhi nahi mila → cancel
      4. 360p milte hi saari visible qualities collect karo
      5. Jo quality page pe nahi hai (missing) → click mat karo (skip)
      6. Click karo → Chrome file save kare
      7. Downloads complete hone ka wait karo

    quality_filter: "1080p" / "720p" / "480p" etc — sirf wahi click karo
    Returns: {"files": [...paths], "qualities": [...], "error": str or None}
    """
    driver = None
    result = {"files": [], "qualities": [], "error": None}

    # ── Scan constants ──
    SCAN_INTERVAL = 1       # seconds between each scan
    SCAN_MAX_TRIES = 20     # max 20 tries = 20 seconds timeout

    try:
        driver = _make_driver(dl_dir)
        LOGGER.info(f"[Swift] Opening: {swift_url}")

        # Page load with retry — renderer timeout se bachne ke liye
        loaded = False
        for attempt in range(3):
            try:
                driver.get(swift_url)
                loaded = True
                LOGGER.info(f"[Swift] Page load OK (attempt {attempt+1})")
                break
            except Exception as e:
                LOGGER.warning(f"[Swift] Page load attempt {attempt+1} failed: {e}")
                time.sleep(5)

        if not loaded:
            result["error"] = "Page load 3 baar fail hua — Chrome renderer timeout"
            return result

        main = driver.current_window_handle
        _close_popups(driver, main)

        # ── STEP 1: 360p gate — har 1 second pe scan karo ──
        LOGGER.info("[Swift] Starting 360p scan loop (max 20s)...")
        found_360p = False
        for scan_num in range(1, SCAN_MAX_TRIES + 1):
            _close_popups(driver, main)
            if _scan_for_360p(driver):
                LOGGER.info(f"[Swift] ✅ 360p gate PASSED at scan #{scan_num} ({scan_num}s)")
                found_360p = True
                break
            LOGGER.info(f"[Swift] Scan #{scan_num}/{SCAN_MAX_TRIES} — 360p not yet visible")
            time.sleep(SCAN_INTERVAL)

        if not found_360p:
            result["error"] = (
                f"⏰ 360p button {SCAN_MAX_TRIES} seconds tak nahi mila — "
                f"page render fail ya content unavailable"
            )
            LOGGER.warning(f"[Swift] ❌ 360p gate FAILED after {SCAN_MAX_TRIES}s")
            return result

        # ── STEP 2: Visible links collect karo ──
        _close_popups(driver, main)
        download_links = _collect_visible_links(driver)
        LOGGER.info(f"[Swift] Total visible links collected: {len(download_links)}")

        # ── STEP 3: Quality filter apply karo (agar diya gaya) ──
        if quality_filter:
            filtered = [l for l in download_links if l["quality"] == quality_filter]
            if filtered:
                download_links = filtered
                LOGGER.info(f"[Swift] Quality filter `{quality_filter}` → {len(download_links)} link(s)")
            else:
                # Missing quality — page pe hai hi nahi
                available = list({l["quality"] for l in download_links})
                result["error"] = (
                    f"❌ `{quality_filter}` page pe missing hai!\n"
                    f"Page pe sirf yeh qualities hain: `{' | '.join(available) or 'none'}`"
                )
                LOGGER.warning(f"[Swift] Quality `{quality_filter}` missing. Available: {available}")
                return result

        # ── STEP 4: Sirf page pe present qualities click karo ──
        if not download_links:
            # Fallback: XPATH button click try karo
            LOGGER.warning("[Swift] No hrefs found after 360p gate — trying XPATH button click...")
            qualities_to_try = [quality_filter] if quality_filter else ["360p", "480p", "720p", "1080p"]
            clicked = []
            for q in qualities_to_try:
                try:
                    elems = driver.find_elements(By.XPATH,
                        f"//*[contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'{q}')]"
                    )
                    for elem in elems:
                        try:
                            if not elem.is_displayed():
                                continue
                            driver.execute_script("arguments[0].scrollIntoView();", elem)
                            driver.execute_script("arguments[0].click();", elem)
                            LOGGER.info(f"[Swift] XPATH button clicked: {q}")
                            clicked.append(q)
                            time.sleep(3)
                            _close_popups(driver, main)
                            break
                        except Exception:
                            pass
                except Exception:
                    pass

            if not clicked:
                result["error"] = "Page pe 360p dikh gaya par download links nahi mile — DOM issue"
                return result

            result["qualities"] = clicked

        else:
            qualities_clicked = []
            for lnk in download_links[:4]:  # max 4 qualities
                q = lnk["quality"]
                href = lnk["href"]
                try:
                    driver.execute_script(f"window.open('{href}', '_blank');")
                    time.sleep(1)
                    _close_popups(driver, main)
                    qualities_clicked.append(q)
                    LOGGER.info(f"[Swift] JS opened: {q} | {href[:60]}")
                    time.sleep(2)
                except Exception as e:
                    LOGGER.warning(f"[Swift] JS open failed for {q}: {e}")

            result["qualities"] = qualities_clicked

        # ── STEP 5: Downloads complete hone ka wait ──
        LOGGER.info("[Swift] Waiting for downloads to complete...")
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
                LOGGER.warning("[Swift] Download timeout (1200s)!")
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
#  Single file upload — returns sent Message object (for reorder)
# ─────────────────────────────────────────────
async def _upload_one_file(client, message, msg, filepath: str, dl_dir: str, encode: bool,
                           on_half: asyncio.Event = None):
    """
    Ek file upload karo.
    Returns: (success: bool, sent_message: Message | None, quality: str)
    sent_message = Telegram pe jo actual video message gaya (reorder ke liye chahiye)
    on_half: asyncio.Event — jab upload 50% ho tab fire karo (next file ko signal karne ke liye)
    """
    fname_orig = os.path.basename(filepath)
    quality = _quality_from(fname_orig)
    size_mb = os.path.getsize(filepath) / (1024 * 1024)

    await msg.edit(
        f"🔄 **Renaming `{quality}`...**\n"
        f"📁 `{fname_orig}`\n"
        f"💾 `{size_mb:.1f} MB`"
    )

    filepath = await _auto_rename(filepath, dl_dir)
    fname = os.path.basename(filepath)
    quality = _quality_from(fname)

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
            return True, None, quality

        c_time = time.time()
        duration = get_duration(filepath)

        user_id = message.from_user.id

        # Pehle keyword-based custompic dhundo, phir default thumbnail fallback
        fname_for_thumb = os.path.basename(filepath)
        custompic_id = await get_custompic_for_file(user_id, fname_for_thumb)
        custom_thumb_id = custompic_id if custompic_id else await db.get_thumbnail(user_id)
        if custompic_id:
            LOGGER.info(f"[Swift] Using custompic for '{fname_for_thumb}'")

        thumb = None
        custom_thumb_used = False

        if custom_thumb_id:
            try:
                import random
                unique_id = f"{int(time.time())}_{random.randint(1000,9999)}"
                thumb_dir = os.path.join(dl_dir, "thumbs")
                os.makedirs(thumb_dir, exist_ok=True)
                thumb_path = os.path.join(thumb_dir, f"thumb_{unique_id}.jpg")

                downloaded = await app.download_media(custom_thumb_id, file_name=thumb_path)
                actual_path = downloaded if downloaded else thumb_path

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
                    LOGGER.warning(f"[Swift] Custom thumb not found, using auto-thumb")
            except Exception as e:
                LOGGER.warning(f"[Swift] Thumb download error: {e}, using auto-thumb")

        if not thumb:
            thumb = get_thumbnail(filepath, dl_dir, duration / 4 if duration else 0)
            custom_thumb_used = False

        cover = thumb if thumb and os.path.isfile(thumb) else None
        width, height = get_width_height(filepath)
        caption = f"<b>{fname}</b>"
        disk_fname = os.path.basename(filepath)

        uc = await _make_uploader_client(message.from_user.id)
        sent_msg = None

        # ── 50% staggered upload ke liye custom progress wrapper ──
        # on_half event tab fire hoga jab yeh file 50% upload ho jaaye
        # isse next file ka upload shuru hoga (thumbnail conflict fix)
        _half_fired = False

        async def _progress_with_half(current, total, ud_type, prog_msg, start):
            nonlocal _half_fired
            from ..utils.display_progress import progress_for_pyrogram
            # Normal progress update
            await progress_for_pyrogram(current, total, ud_type, prog_msg, start)
            # 50% check — sirf ek baar fire karo
            if not _half_fired and on_half and total > 0 and current >= total * 0.50:
                _half_fired = True
                on_half.set()

        try:
            sent_msg = await upload_video(
                message, msg, filepath, caption,
                c_time, thumb, duration, width, height,
                file_name=disk_fname,
                cover=cover,
                uploader_client=uc,
                progress=_progress_with_half,
                progress_args=("📤 Uploading...", msg, c_time),
            )
        finally:
            # Upload complete hone pe bhi event fire karo
            # (agar file bahut chhoti ho aur 50% progress callback nahi aaya)
            if on_half and not _half_fired:
                on_half.set()
            if uc:
                try:
                    await uc.disconnect()
                except Exception:
                    pass

        if not custom_thumb_used and thumb and os.path.isfile(thumb):
            try:
                os.remove(thumb)
            except Exception:
                pass

        return True, sent_msg, quality

    except Exception as e:
        LOGGER.error(f"[Swift] Upload error ({quality}): {e}")
        await message.reply(f"⚠️ Upload failed `{fname}`: `{e}`")
        return False, None, quality


# ─────────────────────────────────────────────
#  Reorder helper — galat order ko sahi karo
# ─────────────────────────────────────────────
async def _reorder_if_needed(client, message, uploaded_results: list):
    """
    uploaded_results = [(quality, sent_message), ...]  — jis order mein upload hua

    1. Check karo — kya order already sahi hai? (360p → 480p → 720p → 1080p)
    2. Agar sahi → kuch nahi karo
    3. Agar galat → sahi order mein forward karo → purane messages delete karo
    """
    # Sirf woh entries lo jahan sent_message actually mila
    valid = [(q, m) for q, m in uploaded_results if m is not None]
    if len(valid) <= 1:
        return  # 1 ya 0 files — reorder ka koi matlab nahi

    # Current order
    current_qualities = [q for q, _ in valid]

    # Expected order — QUALITY_ORDER ke hisab se sort
    expected_qualities = sorted(current_qualities, key=lambda q: QUALITY_ORDER.get(q, 99))

    if current_qualities == expected_qualities:
        LOGGER.info(f"[Swift] Order already correct: {' → '.join(current_qualities)}")
        return  # Sab theek hai, kuch karna nahi

    LOGGER.info(f"[Swift] Reorder needed! Got: {current_qualities} → Want: {expected_qualities}")

    # Quality → message mapping
    q_to_msg = {q: m for q, m in valid}

    # Sahi order mein forward karo
    chat_id = message.chat.id
    forwarded = []
    for q in expected_qualities:
        old_msg = q_to_msg.get(q)
        if not old_msg:
            continue
        try:
            # copy_message = same chat mein bhejo (forward without "Forwarded from" tag)
            new_msg = await client.copy_message(
                chat_id=chat_id,
                from_chat_id=chat_id,
                message_id=old_msg.id,
            )
            forwarded.append(new_msg)
            LOGGER.info(f"[Swift] Reordered: {q} → new msg_id={new_msg.id}")
            await asyncio.sleep(1)  # flood control
        except Exception as e:
            LOGGER.warning(f"[Swift] Forward failed for {q}: {e}")

    if not forwarded:
        LOGGER.warning("[Swift] Reorder: no messages forwarded, skipping delete")
        return

    # Purane messages delete karo
    for q, old_msg in valid:
        try:
            await client.delete_messages(chat_id=chat_id, message_ids=old_msg.id)
            LOGGER.info(f"[Swift] Deleted old msg: {q} id={old_msg.id}")
            await asyncio.sleep(0.5)
        except Exception as e:
            LOGGER.warning(f"[Swift] Delete failed for {q} id={old_msg.id}: {e}")

    reordered_str = " → ".join(expected_qualities)
    try:
        await message.reply(
            f"🔀 **Reordered!**\n\n"
            f"✅ Sahi order: `{reordered_str}`\n"
            f"🗑️ Purane {len(valid)} messages delete kar diye"
        )
    except Exception:
        pass


# ─────────────────────────────────────────────
#  Core command logic
# ─────────────────────────────────────────────
async def _run_swift(client, message, swift_url: str, encode: bool, quality_filter: str = None):
    session_id = str(int(time.time()))
    dl_dir = os.path.join(download_dir, f"swift_{session_id}")
    os.makedirs(dl_dir, exist_ok=True)

    filter_text = f" | Filter: `{quality_filter}`" if quality_filter else ""
    msg = await message.reply(
        f"🔗 **Swift Downloader v7**\n\n"
        f"🌐 `{swift_url}`{filter_text}\n\n"
        f"🔍 Page open ho raha hai... 360p button ka wait karega (max 20s scan)"
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
    result = await loop.run_in_executor(None, _scrape_and_download, swift_url, dl_dir, None, quality_filter)
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

    # Size ke hisab se sort — chhoti pehle
    files = _sort_by_size(files)

    if quality_filter:
        filtered = [f for f in files if _quality_from(os.path.basename(f)) == quality_filter.lower()]
        if not filtered:
            available = [_quality_from(os.path.basename(f)) for f in files]
            await msg.edit(
                f"❌ **`{quality_filter}` nahi mili!**\n\n"
                f"📦 Available: `{' | '.join(available)}`\n\n"
                f"Sahi quality likhke dobara try karo."
            )
            try:
                import shutil
                shutil.rmtree(dl_dir, ignore_errors=True)
            except Exception:
                pass
            return
        files = filtered

    qualities_list = ' → '.join(_quality_from(os.path.basename(f)) for f in files)
    await msg.edit(
        f"✅ **{len(files)} file(s) mili!**\n\n"
        f"📊 `{qualities_list}`\n\n"
        f"📤 Upload ho raha hai..."
    )

    # ── Staggered Upload: File N+1 tab start ho jab File N 50% reach kare ──
    # Isse thumbnail conflict solve hota hai (parallel uploads mein ek pe thumb nahi lagta)
    # Chain: file[0] → 50% → file[1] start → 50% → file[2] start → ...

    # Har file ke liye ek status message banao
    _dummy_msgs = {}
    for fp in files:
        q = _quality_from(os.path.basename(fp))
        try:
            dm = await message.reply(f"📤 **Uploading `{q}`...**")
            _dummy_msgs[fp] = dm
        except Exception:
            _dummy_msgs[fp] = msg  # fallback

    # Events chain — file[i] ka event fire hoga jab uska upload 50% ho
    # file[i+1] is event ka wait karega shuru hone se pehle
    _half_events = [asyncio.Event() for _ in files]

    async def _upload_task_staggered(filepath, idx):
        """
        idx = 0  → immediately start
        idx = 1  → wait for files[0] to reach 50%
        idx = 2  → wait for files[1] to reach 50%
        etc.
        """
        # Apni turn ka wait karo (pichli file 50% tak pahunche)
        if idx > 0:
            await _half_events[idx - 1].wait()

        um = _dummy_msgs.get(filepath, msg)
        success, sent_msg, quality = await _upload_one_file(
            client, message, um, filepath, dl_dir, encode,
            on_half=_half_events[idx],   # 50% pe yeh event fire hoga
        )
        # Status message delete karo — clean chat
        try:
            await um.delete()
        except Exception:
            pass
        return success, sent_msg, quality

    results = await asyncio.gather(
        *[_upload_task_staggered(fp, i) for i, fp in enumerate(files)],
        return_exceptions=True
    )

    # Results parse karo
    uploaded_results = []  # [(quality, sent_message), ...]
    uploaded_count = 0
    for r in results:
        if isinstance(r, Exception):
            LOGGER.error(f"[Swift] Upload task exception: {r}")
            continue
        success, sent_msg, quality = r
        if success:
            uploaded_count += 1
            uploaded_results.append((quality, sent_msg))

    # ── Reorder check — agar order galat tha toh fix karo ──
    await _reorder_if_needed(client, message, uploaded_results)

    # Final summary — sirf ek clean message
    expected_order = sorted(
        [q for q, _ in uploaded_results],
        key=lambda q: QUALITY_ORDER.get(q, 99)
    )
    await msg.edit(
        f"🎉 **Complete!**\n\n"
        f"✅ Uploaded : `{uploaded_count}/{len(files)}`\n"
        f"📊 `{' → '.join(expected_order) or 'N/A'}`"
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
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].strip():
        await message.reply(
            "⚠️ **Usage:**\n"
            "`/swift <url>` — sabhi qualities\n"
            "`/swift <url> 1080p` — sirf 1080p\n"
            "`/swift <url> 720p` — sirf 720p\n"
            "`/swift <url> 480p` — sirf 480p"
        )
        return

    swift_url = parts[1].strip()

    # Optional quality filter: 360p / 480p / 720p / 1080p / 2160p
    quality_filter = None
    if len(parts) >= 3:
        candidate = parts[2].strip().lower()
        if re.match(r"^\d{3,4}p$", candidate):
            quality_filter = candidate

    await _run_swift(client, message, swift_url, encode=False, quality_filter=quality_filter)


@Client.on_message(filters.command("swiftencode"))
async def swift_encode_command(client: Client, message: Message):
    c = await check_chat(message, chat="Sudo")
    if not c:
        return
    if not SELENIUM_OK:
        await message.reply("❌ Selenium install nahi hai!")
        return
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].strip():
        await message.reply("⚠️ Usage:\n`/swiftencode https://argon.razorshell.space/downlead/XXXXXXXX/`\n`/swiftencode <url> 1080p` — sirf 1080p encode")
        return

    swift_url = parts[1].strip()
    quality_filter = None
    if len(parts) >= 3:
        candidate = parts[2].strip().lower()
        if re.match(r"^\d{3,4}p$", candidate):
            quality_filter = candidate

    await _run_swift(client, message, swift_url, encode=True, quality_filter=quality_filter)
