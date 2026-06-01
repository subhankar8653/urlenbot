"""
auto_monitor.py  v2
====================
RTI Channel Auto-Monitor System

Flow:
  1. Tum MONITOR_CHANNEL pe RTI ka post forward karte ho
     (e.g. "Episode 34-36 Added! https://rareanimes.buzz/...")
  2. Bot detect karta hai → URL + episode range nikalta hai
  3. Anime name match karta hai /add_anime list se
  4. Har episode ke liye — Quality Poller shuru:
       - Swift URL pe jaata hai, jo qualities available hain unhe download + upload
       - 360p mila → upload | 720p mila → upload | 1080p mila → upload
       - (Swift ki tarah ek saath — jo available hai woh)
       - 60s baad dobara check — jo quality abhi tak nahi aayi usse phir try karo
       - Max 30 min tak monitor karta rahega
       - 30 min baad jo missing raha → failure message
  5. Custom pic (existing custompic.py se auto-apply) + auto caption

Commands:
  /add_anime [channel_id] [Anime Name]   → Anime + channel link karo
  /list_anime                            → Kya set hai dekho
  /del_anime [number]                    → Remove karo
  /set_monitor                           → Monitor channel set karo (forward reply ya ID)
  /monitor_status                        → System health check
"""

import asyncio
import glob
import logging
import os
import re
import shutil
import time

from pyrogram import Client, filters
from pyrogram.types import Message

from .. import LOGGER, app, owner, sudo_users, download_dir
from ..utils.database.access_db import db

# ─────────────────────────────────────────────
#  Lazy imports (avoid circular on startup)
# ─────────────────────────────────────────────
def _get_rti_fns():
    from .rti_downloader import get_watchmult_link, get_argon_link, argon_to_swift
    return get_watchmult_link, get_argon_link, argon_to_swift

def _get_swift_fns():
    from .swift_downloader import (
        _scrape_and_download, _upload_one_file, _sort_by_size,
        _auto_rename, _quality_from, QUALITY_ORDER
    )
    return _scrape_and_download, _upload_one_file, _sort_by_size, _auto_rename, _quality_from, QUALITY_ORDER

def _get_schedule_fn():
    from .schedule_notify import send_schedule_notification
    return send_schedule_notification

# ─────────────────────────────────────────────
#  Constants
# ─────────────────────────────────────────────
POLL_INTERVAL_FAST = 30         # seconds — pehle 10 attempts (5 min)
POLL_INTERVAL_SLOW = 60         # seconds — baad ke 20 attempts (20 min)
POLL_FAST_ATTEMPTS = 10         # kitne attempts fast interval pe
POLL_SLOW_ATTEMPTS = 20         # kitne attempts slow interval pe
# Total max time = (10×30s) + (20×60s) = 5min + 20min = 25min
TARGET_QUALITIES   = ["360p", "720p", "1080p"]   # inhe dhundna hai

# ─────────────────────────────────────────────
#  DB Helpers — owner ke user doc mein store hota hai
# ─────────────────────────────────────────────
async def _owner_id() -> int | None:
    return owner[0] if owner else None


async def _get_anime_list() -> list:
    oid = await _owner_id()
    if not oid:
        return []
    user = await db._get_user(oid)
    return user.get('anime_monitor_list', [])


async def _save_anime_list(anime_list: list):
    oid = await _owner_id()
    if not oid:
        return
    await db.col.update_one({'id': oid}, {'$set': {'anime_monitor_list': anime_list}}, upsert=True)


async def _get_monitor_channel() -> int | None:
    oid = await _owner_id()
    if not oid:
        return None
    user = await db._get_user(oid)
    val = user.get('monitor_channel_id')
    return int(val) if val else None


async def _save_monitor_channel(channel_id: int):
    oid = await _owner_id()
    if not oid:
        return
    await db.col.update_one({'id': oid}, {'$set': {'monitor_channel_id': channel_id}}, upsert=True)


def _is_authorized(user_id: int) -> bool:
    return user_id in owner or user_id in sudo_users


# ─────────────────────────────────────────────
#  Text Parsing Helpers
# ─────────────────────────────────────────────
def _normalize(text: str) -> str:
    return re.sub(r'[^a-z0-9]', '', text.lower())


def _find_matching_anime(text: str, anime_list: list) -> dict | None:
    text_norm = _normalize(text)
    best, best_len = None, 0
    for entry in anime_list:
        name_norm = _normalize(entry.get('anime_name', ''))
        if name_norm and name_norm in text_norm and len(name_norm) > best_len:
            best, best_len = entry, len(name_norm)
    return best


def _extract_episodes(text: str) -> tuple[int, int] | None:
    # "EPISODE 4-13 + ZIP PACK ADDED!" / "Episode 34-36" / "Ep 34 - 36"
    m = re.search(r'(?i)episodes?\s*(\d+)\s*[-\u2013]\s*(\d+)', text)
    if m:
        return int(m.group(1)), int(m.group(2))
    # Ep/Episode + to range
    m = re.search(r'[Ee]p(?:isode)?\s*(\d+)\s*[-\u2013to]+\s*(\d+)', text)
    if m:
        return int(m.group(1)), int(m.group(2))
    # Single "Episode 5"
    m = re.search(r'[Ee]p(?:isode)?\s*(\d+)', text)
    if m:
        ep = int(m.group(1))
        return ep, ep
    # "34-36 Added" / "34-36 + ZIP PACK ADDED"
    m = re.search(r'(\d+)\s*[-\u2013]\s*(\d+)[^\n]*[Aa]dded', text)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None


def _extract_url(text: str) -> str | None:
    m = re.search(r'https?://[^\s]+', text)
    return m.group(0) if m else None


# ─────────────────────────────────────────────
#  Core: Episode Quality Poller
#
#  Ye function ek episode ke liye 30 min tak
#  360p / 720p / 1080p dhundhta rahega.
#  Jo quality mil gayi → immediately upload.
#  Sab mil gaye → done.
#  30 min baad bhi jo nahi mila → failure notice.
# ─────────────────────────────────────────────
async def _episode_quality_poller(
    client: Client,
    log_message: Message,
    swift_url: str,
    episode_num: int,
    anime_name: str,
    channel_id: int,
    owner_id: int,
    matched_entry: dict = None,
    start_ep: int = None,
    end_ep: int = None,
    update_post_sent: list = None,
):
    # update_post_sent — mutable list [False] shared across episodes
    # Pehle episode ke 360p pe True ho jaata hai → baaki episodes skip karte hain
    _scrape_and_download, _upload_one_file, _sort_by_size, _auto_rename, _quality_from, QUALITY_ORDER = _get_swift_fns()
    if matched_entry is None:
        matched_entry = {}

    remaining = set(TARGET_QUALITIES)   # jo qualities abhi tak nahi mili
    start_time = time.time()
    attempt = 0
    max_attempts = POLL_FAST_ATTEMPTS + POLL_SLOW_ATTEMPTS  # 10 + 20 = 30

    status_msg = await log_message.reply(
        f"🎌 **AutoMonitor** | `{anime_name}` | Ep `{episode_num}`\n\n"
        f"⏳ Quality poller shuru — Fast: 10×30s → Slow: 20×60s\n"
        f"🎯 Dhundh raha hoon: `{' | '.join(sorted(remaining))}`"
    )

    loop = asyncio.get_event_loop()

    # ── DL folder ek baar banao — Chrome session ke saath rehega ──
    session_id = f"monitor_ep{episode_num}_{int(time.time())}"
    dl_dir = os.path.join(download_dir, session_id)
    os.makedirs(dl_dir, exist_ok=True)

    # ── Chrome ek baar kholenge — blocking function in executor ──
    def _monitor_with_persistent_chrome() -> dict:
        """
        Chrome ek baar khulta hai.
        Jab tak saari qualities nahi milti:
          - Page refresh karo
          - Jo qualities already downloaded → skip
          - Jo nayi mili → click karke download karo
        Returns: {"files": [...], "error": ...}
        """
        from .swift_downloader import (
            _make_driver, _close_popups, _scan_for_360p,
            _collect_visible_links, _get_done_files, _in_progress,
            _quality_from as _qf
        )
        import glob as _glob

        driver = None
        result = {"files": [], "error": None}
        downloaded_qualities = set()   # jo already download ho chuke hain

        try:
            driver = _make_driver(dl_dir)
            LOGGER.info(f"[AutoMonitor] Chrome opened for Ep {episode_num}")

            # Pehli baar page load
            loaded = False
            for _attempt in range(3):
                try:
                    driver.get(swift_url)
                    loaded = True
                    break
                except Exception as e:
                    LOGGER.warning(f"[AutoMonitor] Page load attempt {_attempt+1} fail: {e}")
                    time.sleep(3)

            if not loaded:
                result["error"] = "Page load fail — Chrome renderer timeout"
                return result

            main = driver.current_window_handle
            _close_popups(driver, main)

            _inner_attempt = 0
            _inner_max = POLL_FAST_ATTEMPTS + POLL_SLOW_ATTEMPTS

            while remaining and _inner_attempt < _inner_max:
                _inner_attempt += 1
                is_fast   = _inner_attempt <= POLL_FAST_ATTEMPTS
                interval  = POLL_INTERVAL_FAST if is_fast else POLL_INTERVAL_SLOW

                LOGGER.info(
                    f"[AutoMonitor] Ep {episode_num} Chrome attempt {_inner_attempt}/{_inner_max} "
                    f"| Remaining: {remaining}"
                )

                # ── 360p gate — max 10s wait ──
                found_360p = False
                for _scan in range(10):
                    _close_popups(driver, main)
                    if _scan_for_360p(driver):
                        found_360p = True
                        break
                    if _scan < 9:
                        time.sleep(1)

                if not found_360p:
                    LOGGER.info(f"[AutoMonitor] Ep {episode_num}: 360p not visible yet, refresh...")
                    driver.refresh()
                    _close_popups(driver, main)
                    time.sleep(interval)
                    continue

                # ── Links collect karo ──
                _close_popups(driver, main)
                links = _collect_visible_links(driver)
                available = {_qf(lnk["quality"]) for lnk in links
                             if _qf(lnk["quality"]) != "unknown"}

                # Jo needed hain aur abhi tak downloaded nahi hue
                to_download = remaining - downloaded_qualities

                # Page pe available hain unhe hi click karo
                click_these = [
                    lnk for lnk in links
                    if _qf(lnk["quality"]) in to_download
                ]

                if not click_these:
                    LOGGER.info(
                        f"[AutoMonitor] Ep {episode_num}: "
                        f"Available={available}, Need={to_download} — refresh karenge"
                    )
                    driver.refresh()
                    _close_popups(driver, main)
                    time.sleep(interval)
                    continue

                # ── Download karo ──
                clicked = []
                for lnk in click_these:
                    q    = _qf(lnk["quality"])
                    href = lnk["href"]
                    try:
                        driver.execute_script(f"window.open('{href}', '_blank');")
                        time.sleep(0.3)
                        _close_popups(driver, main)
                        clicked.append(q)
                        downloaded_qualities.add(q)
                        LOGGER.info(f"[AutoMonitor] Ep {episode_num}: Clicked {q}")
                    except Exception as e:
                        LOGGER.warning(f"[AutoMonitor] Click failed {q}: {e}")

                if not clicked:
                    driver.refresh()
                    _close_popups(driver, main)
                    time.sleep(interval)
                    continue

                # ── Downloads complete hone ka wait ──
                dl_start = time.time()
                while True:
                    done     = _get_done_files(dl_dir)
                    in_prog  = _in_progress(dl_dir)
                    elapsed  = int(time.time() - dl_start)
                    # Jitne click kiye + jo pehle se hain
                    expected = len(downloaded_qualities)
                    if len(done) >= expected and not in_prog:
                        break
                    if elapsed > 5 and len(done) >= 1 and not in_prog:
                        break
                    if elapsed > 1200:
                        LOGGER.warning("[AutoMonitor] Download wait timeout!")
                        break
                    time.sleep(2)

                # Nayi files result mein add karo
                result["files"] = _get_done_files(dl_dir)

                # Agar sab mil gaye toh loop khatam
                downloaded_so_far = {
                    _qf(os.path.basename(f))
                    for f in result["files"]
                }
                if remaining.issubset(downloaded_so_far):
                    LOGGER.info(f"[AutoMonitor] Ep {episode_num}: All qualities downloaded ✅")
                    break

                # Kuch baki hain — page refresh karke next attempt
                LOGGER.info(
                    f"[AutoMonitor] Ep {episode_num}: "
                    f"Got {downloaded_so_far}, still need {remaining - downloaded_so_far}"
                )
                driver.refresh()
                _close_popups(driver, main)
                time.sleep(interval)

        except Exception as e:
            result["error"] = str(e)
            LOGGER.error(f"[AutoMonitor] Chrome session error: {e}")
        finally:
            if driver:
                try:
                    driver.quit()
                    LOGGER.info(f"[AutoMonitor] Chrome closed for Ep {episode_num}")
                except Exception:
                    pass

        return result

    # ── Chrome executor mein chalaao — async block nahi hoga ──
    try:
        await status_msg.edit(
            f"🎌 **AutoMonitor** | `{anime_name}` | Ep `{episode_num}`\n\n"
            f"🌐 Chrome khul raha hai...\n"
            f"🎯 Dhundh raha hoon: `{' | '.join(sorted(remaining))}`"
        )
    except Exception:
        pass

    chrome_result = await loop.run_in_executor(None, _monitor_with_persistent_chrome)

    if chrome_result["error"] and not chrome_result["files"]:
        try:
            await status_msg.edit(
                f"❌ **AutoMonitor** | `{anime_name}` | Ep `{episode_num}`\n\n"
                f"`{chrome_result['error'][:150]}`"
            )
        except Exception:
            pass
        shutil.rmtree(dl_dir, ignore_errors=True)
        return

    all_files = _sort_by_size(chrome_result.get("files", []))
    if not all_files:
        try:
            await status_msg.edit(
                f"❌ **AutoMonitor** | `{anime_name}` | Ep `{episode_num}`\n\n"
                f"Koi file download nahi hui."
            )
        except Exception:
            pass
        shutil.rmtree(dl_dir, ignore_errors=True)
        return

    # ── Upload — jo files mili unhe upload karo ──
    new_files = [f for f in all_files if _quality_from(os.path.basename(f)) in remaining]

    if not new_files:
        elapsed_min = int((time.time() - start_time) / 60)
        missing_str = ' | '.join(sorted(remaining))
        try:
            await status_msg.edit(
                f"⚠️ **Incomplete!** | `{anime_name}` | Ep `{episode_num}`\n\n"
                f"❌ {elapsed_min}m baad bhi nahi mili: `{missing_str}`\n"
                f"RTI pe manually check karo."
            )
        except Exception:
            pass
        shutil.rmtree(dl_dir, ignore_errors=True)
        return

    qualities_found = [_quality_from(os.path.basename(f)) for f in new_files]
    try:
        await status_msg.edit(
            f"🎌 **AutoMonitor** | `{anime_name}` | Ep `{episode_num}`\n\n"
            f"✅ Mili: `{' | '.join(qualities_found)}`\n"
            f"📤 Upload ho raha hai..."
        )
    except Exception:
        pass

    # Upload status messages
    _dummy_msgs = {}
    for fp in new_files:
        q = _quality_from(os.path.basename(fp))
        try:
            dm = await log_message.reply(f"📤 **Uploading `{q}`** — Ep `{episode_num}`...")
            _dummy_msgs[fp] = dm
        except Exception:
            _dummy_msgs[fp] = status_msg

    _half_events = [asyncio.Event() for _ in new_files]

    class _ProxyMsg:
        def __init__(self, original_msg, uid, upload_chat_id):
            self._msg = original_msg
            self.from_user = type('U', (), {'id': uid})()
            self.chat = type('C', (), {'id': upload_chat_id})()
            self.id = original_msg.id

        async def reply(self, *args, **kwargs):
            return await self._msg.reply(*args, **kwargs)

        async def reply_video(self, video, **kwargs):
            return await app.send_video(chat_id=self.chat.id, video=video, **kwargs)

        async def reply_document(self, document, **kwargs):
            return await app.send_document(chat_id=self.chat.id, document=document, **kwargs)

        async def reply_audio(self, audio, **kwargs):
            return await app.send_audio(chat_id=self.chat.id, audio=audio, **kwargs)

    proxy_msg = _ProxyMsg(log_message, owner_id, channel_id)

    async def _delete_old_bot_msgs(ch_id: int):
        try:
            from .schedule_notify import get_last_posted_msg_ids, clear_last_posted_msg_ids
            saved_ids = await get_last_posted_msg_ids(ch_id)
            if not saved_ids:
                return
            try:
                await client.delete_messages(ch_id, saved_ids)
            except Exception as de:
                for mid in saved_ids:
                    try:
                        await client.delete_messages(ch_id, mid)
                        await asyncio.sleep(0.2)
                    except Exception:
                        pass
            await clear_last_posted_msg_ids(ch_id)
        except Exception as _e:
            LOGGER.warning(f"[AutoMonitor] _delete_old_bot_msgs error: {_e}")

    async def _upload_task(filepath, idx):
        if idx > 0:
            await _half_events[idx - 1].wait()
        if idx == 0:
            await _delete_old_bot_msgs(channel_id)
        um = _dummy_msgs.get(filepath, status_msg)
        success, sent_msg, quality = await _upload_one_file(
            client, proxy_msg, um, filepath, dl_dir, encode=False,
            on_half=_half_events[idx],
        )
        try:
            await um.delete()
        except Exception:
            pass

        if success and sent_msg:
            if quality == "360p":
                LOGGER.info(f"[AutoMonitor] Ep {episode_num}: 360p upload done ✅")
                is_first_ep   = (start_ep is not None and episode_num == start_ep)
                already_sent  = (update_post_sent is not None and update_post_sent[0])
                if is_first_ep and not already_sent:
                    try:
                        from .update_channel import send_update_post
                        from ..utils.auto_caption import extract_anime_info as _eai
                        _season = None
                        try:
                            _fname = os.path.basename(filepath)
                            if _fname:
                                _, _season, _ = _eai(_fname, {})
                        except Exception:
                            pass
                        _ep_end = end_ep if end_ep else episode_num
                        await send_update_post(
                            client, anime_name=anime_name, season=_season,
                            episode_start=episode_num, episode_end=_ep_end,
                        )
                        if update_post_sent is not None:
                            update_post_sent[0] = True
                    except Exception as _ue:
                        LOGGER.error(f"[AutoMonitor] Update post error: {_ue}")

        return success, sent_msg, quality

    results = await asyncio.gather(
        *[_upload_task(fp, i) for i, fp in enumerate(new_files)],
        return_exceptions=True
    )

    for r in results:
        if isinstance(r, Exception):
            LOGGER.error(f"[AutoMonitor] Upload task error: {r}")
            continue
        success, sent_msg, quality = r
        if success:
            remaining.discard(quality)
            LOGGER.info(f"[AutoMonitor] Ep {episode_num}: ✅ {quality} uploaded!")

    shutil.rmtree(dl_dir, ignore_errors=True)

    # ── Final status ──
    elapsed_min = int((time.time() - start_time) / 60)

    if not remaining:
        try:
            await status_msg.edit(
                f"🎉 **Complete!** | `{anime_name}` | Ep `{episode_num}`\n\n"
                f"✅ Saari qualities upload ho gayi!\n"
                f"⏱️ Total time: `{elapsed_min}m`"
            )
        except Exception:
            pass
        LOGGER.info(f"[AutoMonitor] Ep {episode_num}: ALL qualities done in {elapsed_min}m")
    else:
        missing_str = ' | '.join(sorted(remaining))
        try:
            await status_msg.edit(
                f"⚠️ **Incomplete!** | `{anime_name}` | Ep `{episode_num}`\n\n"
                f"❌ {elapsed_min}m baad bhi nahi mili: `{missing_str}`\n"
                f"✅ Jo mili: `{' | '.join(q for q in TARGET_QUALITIES if q not in remaining)}`\n\n"
                f"RTI pe manually check karo."
            )
        except Exception:
            await log_message.reply(
                f"⚠️ **AutoMonitor Warning** | `{anime_name}` | Ep `{episode_num}`\n\n"
                f"❌ {elapsed_min}m baad bhi nahi mili: `{missing_str}`\n"
                f"RTI pe manually check karo."
            )
        LOGGER.warning(f"[AutoMonitor] Ep {episode_num}: Done. Missing: {missing_str}")


async def _forward_to_anime_channel(client: Client, sent_msg, channel_id: int, anime_name: str):
    """Upload hua message anime ke channel pe forward karo."""
    if not channel_id or not sent_msg:
        return
    try:
        await client.copy_message(
            chat_id=channel_id,
            from_chat_id=sent_msg.chat.id,
            message_id=sent_msg.id,
        )
        LOGGER.info(f"[AutoMonitor] Forwarded to channel {channel_id} for {anime_name}")
    except Exception as e:
        LOGGER.error(f"[AutoMonitor] Forward to channel failed: {e}")


# ─────────────────────────────────────────────
#  Episode ko RTI se Swift URL tak le jaao
# ─────────────────────────────────────────────
async def _get_swift_url_for_episode(page_url: str, episode_num: int, status_msg) -> str | None:
    get_watchmult_link, get_argon_link, argon_to_swift = _get_rti_fns()
    loop = asyncio.get_event_loop()

    try:
        await status_msg.edit(
            f"{status_msg.text.split(chr(10))[0]}\n\n"
            f"🔍 Ep `{episode_num}` — WatchMultQuality link..."
        )
    except Exception:
        pass

    wmq_link, _ = await loop.run_in_executor(None, get_watchmult_link, page_url, episode_num)
    if not wmq_link:
        return None

    try:
        await status_msg.edit(
            f"{status_msg.text.split(chr(10))[0]}\n\n"
            f"🔍 Ep `{episode_num}` — Argon link extract..."
        )
    except Exception:
        pass

    argon_link = await loop.run_in_executor(None, get_argon_link, wmq_link)
    if not argon_link:
        return None

    return argon_to_swift(argon_link)


# ─────────────────────────────────────────────
#  Monitor Channel Message Handler
# ─────────────────────────────────────────────
@Client.on_message(
    filters.channel & (filters.text | filters.caption)
)
async def auto_monitor_handler(client: Client, message: Message):
    """
    Monitor channel pe message aaya → check karo.
    RTI URL + episode info mila → process karo.
    """
    monitor_ch = await _get_monitor_channel()
    if not monitor_ch:
        return

    if message.chat.id != monitor_ch:
        return

    text = message.text or message.caption or ""
    if not text:
        return

    LOGGER.info(f"[AutoMonitor] Message in monitor channel: {text[:100]}")

    url = _extract_url(text)
    if not url:
        return

    ep_info = _extract_episodes(text)
    if not ep_info:
        return

    start_ep, end_ep = ep_info
    anime_list = await _get_anime_list()
    if not anime_list:
        return

    matched = _find_matching_anime(text + " " + url, anime_list)
    if not matched:
        LOGGER.info(f"[AutoMonitor] No anime match for: {text[:80]}")
        return

    anime_name = matched['anime_name']
    channel_id = matched['channel_id']
    oid = await _owner_id()

    LOGGER.info(f"[AutoMonitor] ✅ Match: {anime_name} | Ep {start_ep}–{end_ep}")

    total = end_ep - start_ep + 1

    # Shared flag — pehle episode ke 360p pe True hoga, baaki skip karenge
    update_post_sent = [False]

    for i, ep_num in enumerate(range(start_ep, end_ep + 1), 1):
        is_last = (i == total)

        # Swift URL nikalo
        prep_msg = await message.reply(
            f"🎌 **AutoMonitor** | `{anime_name}` | Ep `{ep_num}/{end_ep}`\n\n"
            f"🔍 Swift URL nikaal raha hoon..."
        )

        # ── Swift URL Retry Logic — 2-phase ──
        # Phase 1: pehle 10 attempts × 30s = 5 min
        # Phase 2: baad ke 20 attempts × 60s = 20 min
        SWIFT_FAST_ATTEMPTS = 10
        SWIFT_SLOW_ATTEMPTS = 20
        SWIFT_MAX_ATTEMPTS  = SWIFT_FAST_ATTEMPTS + SWIFT_SLOW_ATTEMPTS  # 30

        swift_url = None
        for swift_attempt in range(1, SWIFT_MAX_ATTEMPTS + 1):
            swift_url = await _get_swift_url_for_episode(url, ep_num, prep_msg)
            if swift_url:
                break

            # Last attempt ke baad fail → bahar niklo
            if swift_attempt == SWIFT_MAX_ATTEMPTS:
                await prep_msg.edit(
                    f"❌ **AutoMonitor** | `{anime_name}` | Ep `{ep_num}`\n\n"
                    f"⏱️ {SWIFT_MAX_ATTEMPTS} attempts (~25 min) ke baad bhi\n"
                    f"Swift URL nahi mila. RTI pe manually check karo."
                )
                break

            # Phase decide karo
            is_fast   = swift_attempt <= SWIFT_FAST_ATTEMPTS
            interval  = 30 if is_fast else 60
            phase_lbl = "⚡ Fast" if is_fast else "🐢 Slow"
            remaining_attempts = SWIFT_MAX_ATTEMPTS - swift_attempt
            try:
                await prep_msg.edit(
                    f"⏳ **AutoMonitor** | `{anime_name}` | Ep `{ep_num}`\n\n"
                    f"🔄 Attempt `{swift_attempt}/{SWIFT_MAX_ATTEMPTS}` {phase_lbl} — Swift URL nahi mila\n"
                    f"⏰ `{interval}s` baad retry... ({remaining_attempts} attempts left)"
                )
            except Exception:
                pass

            await asyncio.sleep(interval)

        if not swift_url:
            continue

        await prep_msg.edit(
            f"✅ **AutoMonitor** | `{anime_name}` | Ep `{ep_num}`\n\n"
            f"Swift URL mila! Quality poller shuru...\n"
            f"`{swift_url}`"
        )

        # Episode fully complete hone ke baad hi agli episode shuru karo (sequential)
        await _episode_quality_poller(
            client, message, swift_url,
            ep_num, anime_name, channel_id, oid,
            matched_entry=matched,
            start_ep=start_ep,
            end_ep=end_ep,
            update_post_sent=update_post_sent,
        )

        # Episodes ke beech thoda gap
        if not is_last:
            await asyncio.sleep(3)

    # ── Sirf last episode ke baad schedule/end message bhejo ──
    try:
        send_schedule_notification = _get_schedule_fn()
        await send_schedule_notification(client, channel_id, anime_name, end_ep)
        LOGGER.info(f"[AutoMonitor] ✅ Schedule notification sent after last ep {end_ep}")
    except Exception as e:
        LOGGER.error(f"[AutoMonitor] Schedule notification error: {e}")


# ─────────────────────────────────────────────
#  /set_monitor
# ─────────────────────────────────────────────
@Client.on_message(filters.command("set_monitor") & filters.private)
async def cmd_set_monitor(client: Client, message: Message):
    """
    /set_monitor -100xxxxxxxxx
    Ya channel se post forward karke reply mein: /set_monitor
    """
    if not _is_authorized(message.from_user.id):
        return

    channel_id = None
    title = None

    # Method 1: Forwarded post reply
    if message.reply_to_message and message.reply_to_message.forward_from_chat:
        fwd = message.reply_to_message.forward_from_chat
        channel_id = fwd.id
        title = fwd.title

    # Method 2: ID directly
    if channel_id is None:
        parts = message.text.split()
        if len(parts) >= 2:
            try:
                channel_id = int(parts[1])
                chat = await client.get_chat(channel_id)
                title = chat.title
            except Exception as e:
                await message.reply(f"❌ Channel nahi mila: `{e}`")
                return

    if channel_id is None:
        current = await _get_monitor_channel()
        if current:
            try:
                ch = await client.get_chat(current)
                cur_text = f"✅ **Current:** {ch.title} (`{current}`)"
            except Exception:
                cur_text = f"✅ **Current ID:** `{current}`"
        else:
            cur_text = "❌ Abhi set nahi hai"

        await message.reply(
            f"📡 **Monitor Channel**\n\n"
            f"{cur_text}\n\n"
            f"**Kaise set karein:**\n"
            f"Method 1 — Channel se koi post forward karo, phir reply mein `/set_monitor`\n"
            f"Method 2 — `/set_monitor -100xxxxxxxxx`"
        )
        return

    await _save_monitor_channel(channel_id)
    await message.reply(
        f"✅ **Monitor Channel Set!**\n\n"
        f"📢 **{title}**\n"
        f"🆔 `{channel_id}`\n\n"
        f"Ab is channel pe RTI links aane pe bot automatically process karega! 🚀"
    )


# ─────────────────────────────────────────────
#  /add_anime — Interactive 3-step flow
#  Step 1: /add_anime [channel_id] [Anime Name]
#  Step 2: Bot poochega hashtag (ya skip)
#  Step 3: Bot poochega channel link (ya skip)
# ─────────────────────────────────────────────

# { user_id: { 'step': 'hashtag'|'link', 'channel_id': int, 'channel_title': str, 'anime_name': str, 'hashtag': str } }
# _add_anime_sessions removed — /add_anime is now a single-step command


@Client.on_message(filters.command("add_anime") & filters.private)
async def cmd_add_anime(client: Client, message: Message):
    """
    /add_anime [channel_id] [Anime Name]

    Example:
      /add_anime -1001234567890 Fullmetal Alchemist: Brotherhood
    """
    if not _is_authorized(message.from_user.id):
        return

    parts = message.text.split(None, 2)

    if len(parts) < 3:
        await message.reply(
            "**Usage:**\n"
            "`/add_anime [channel_id] [Anime Name]`\n\n"
            "**Example:**\n"
            "`/add_anime -1001234567890 Fullmetal Alchemist: Brotherhood`\n\n"
            "💡 Anime Name wahi likhna jo RTI post ya URL mein aata hai"
        )
        return

    try:
        channel_id = int(parts[1])
    except ValueError:
        await message.reply("❌ Channel ID valid nahi! Format: `-100xxxxxxxxx`")
        return

    anime_name = parts[2].strip()

    try:
        chat = await client.get_chat(channel_id)
        channel_title = chat.title
    except Exception as e:
        await message.reply(f"❌ Channel nahi mila: `{e}`\n\nBot ko channel mein admin banao pehle.")
        return

    try:
        bot_me = await client.get_me()
        member = await client.get_chat_member(channel_id, bot_me.id)
        if member.status.name not in ["ADMINISTRATOR", "OWNER"]:
            await message.reply(f"❌ Bot `{channel_title}` mein admin nahi hai!")
            return
    except Exception as e:
        await message.reply(f"❌ Admin check fail: `{e}`")
        return

    anime_list = await _get_anime_list()

    for entry in anime_list:
        if (entry.get('channel_id') == channel_id and
                entry.get('anime_name', '').lower() == anime_name.lower()):
            await message.reply(f"⚠️ Already exists!\n\n📺 **{anime_name}** → `{channel_title}`")
            return

    # Seedha save karo — no extra steps needed
    anime_list = await _get_anime_list()
    anime_list.append({
        'channel_id':    channel_id,
        'channel_title': channel_title,
        'anime_name':    anime_name,
        'hashtag':       '',
        'channel_link':  '',
    })
    await _save_anime_list(anime_list)

    await message.reply(
        f"✅ **Anime Added!**\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📺 **Anime:** {anime_name}\n"
        f"📢 **Channel:** {channel_title}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Ab jab bhi RTI pe `{anime_name}` ka post aayega,\n"
        f"bot automatically download + upload karega! \U0001f680"
    )



# ─────────────────────────────────────────────
#  /list_anime
# ─────────────────────────────────────────────
@Client.on_message(filters.command("list_anime") & filters.private)
async def cmd_list_anime(client: Client, message: Message):
    if not _is_authorized(message.from_user.id):
        return

    anime_list = await _get_anime_list()
    monitor_ch = await _get_monitor_channel()

    if monitor_ch:
        try:
            mc = await client.get_chat(monitor_ch)
            mc_text = f"✅ {mc.title} (`{monitor_ch}`)"
        except Exception:
            mc_text = f"⚠️ ID: `{monitor_ch}` (access error)"
    else:
        mc_text = "❌ Set nahi — use `/set_monitor`"

    if not anime_list:
        await message.reply(
            f"📡 **Monitor Channel:** {mc_text}\n\n"
            f"📋 Koi anime registered nahi hai!\n\n"
            f"Add karo: `/add_anime [channel_id] [Anime Name]`"
        )
        return

    text = f"📡 **Monitor:** {mc_text}\n\n📋 **Registered Anime**\n━━━━━━━━━━━━━━━━━━━━\n\n"

    for i, entry in enumerate(anime_list, 1):
        name = entry.get('anime_name', 'Unknown')
        ch_title = entry.get('channel_title', 'Unknown')
        ch_id = entry.get('channel_id', 'N/A')
        hashtag = entry.get('hashtag', '') or '—'
        link = entry.get('channel_link', '') or '—'
        text += (
            f"**{i}.** 📺 {name}\n"
            f"   📢 {ch_title} (`{ch_id}`)\n"
            f"   🏷️ {hashtag} | 🔗 {link}\n\n"
        )

    text += (
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Total: **{len(anime_list)}**\n\n"
        f"🗑️ Remove: `/del_anime 1`"
    )
    await message.reply(text)


# ─────────────────────────────────────────────
#  /del_anime
# ─────────────────────────────────────────────
@Client.on_message(filters.command("del_anime") & filters.private)
async def cmd_del_anime(client: Client, message: Message):
    if not _is_authorized(message.from_user.id):
        return

    anime_list = await _get_anime_list()
    if not anime_list:
        await message.reply("📋 Koi anime registered nahi hai!")
        return

    if len(message.command) < 2:
        text = "🗑️ **Konsa remove karna hai?**\n━━━━━━━━━━━━━━━━━━━━\n\n"
        for i, entry in enumerate(anime_list, 1):
            text += f"**{i}.** {entry.get('anime_name')} → {entry.get('channel_title')}\n"
        text += "\n━━━━━━━━━━━━━━━━━━━━\nUse: `/del_anime <number>`"
        await message.reply(text)
        return

    try:
        num = int(message.command[1])
    except ValueError:
        await message.reply("❌ Sahi number dalo! Example: `/del_anime 1`")
        return

    if num < 1 or num > len(anime_list):
        await message.reply(f"❌ 1 se {len(anime_list)} tak dalo.")
        return

    removed = anime_list.pop(num - 1)
    await _save_anime_list(anime_list)

    await message.reply(
        f"✅ **Removed!**\n\n"
        f"📺 {removed.get('anime_name')}\n"
        f"📢 {removed.get('channel_title')}"
    )


# ─────────────────────────────────────────────
#  /monitor_status
# ─────────────────────────────────────────────
@Client.on_message(filters.command("monitor_status") & filters.private)
async def cmd_monitor_status(client: Client, message: Message):
    if not _is_authorized(message.from_user.id):
        return

    monitor_ch = await _get_monitor_channel()
    anime_list = await _get_anime_list()

    if monitor_ch:
        try:
            mc = await client.get_chat(monitor_ch)
            mc_text = f"✅ {mc.title} (`{monitor_ch}`)"
        except Exception:
            mc_text = f"⚠️ ID set (`{monitor_ch}`) but access error"
    else:
        mc_text = "❌ Set nahi — use `/set_monitor`"

    await message.reply(
        f"📊 **AutoMonitor Status**\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📡 Monitor Channel: {mc_text}\n"
        f"📺 Anime Count: **{len(anime_list)}**\n"
        f"🎯 Target Qualities: `{' | '.join(TARGET_QUALITIES)}`\n"
        f"⚡ Fast Poll: **{POLL_FAST_ATTEMPTS} × {POLL_INTERVAL_FAST}s** (first 5 min)\n"
        f"🐢 Slow Poll: **{POLL_SLOW_ATTEMPTS} × {POLL_INTERVAL_SLOW}s** (next 20 min)\n"
        f"⏰ Max Attempts: **{POLL_FAST_ATTEMPTS + POLL_SLOW_ATTEMPTS}** (~25 min total)\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📋 /list_anime\n"
        f"➕ /add_anime"
    )
