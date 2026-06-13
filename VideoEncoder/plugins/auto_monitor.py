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
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton

from .. import LOGGER, app, owner, sudo_users, download_dir
from ..utils.database.access_db import db

# ─────────────────────────────────────────────
#  Lazy imports (avoid circular on startup)
# ─────────────────────────────────────────────
def _get_rti_fns():
    from .rti_downloader import get_watchmult_link, get_argon_link, argon_to_swift
    return get_watchmult_link, get_argon_link, argon_to_swift

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
#  Core: AutoMonitor Swift Runner
#
#  Swift URL milne ke baad — bilkul /swift ki
#  tarah download + upload karo, channel pe
#  send karo, update_post trigger karo.
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
) -> bool:
    """
    Swift URL milne ke baad:
      - 1 Chrome session → teeno downloads parallel shuru
      - Jaise hi koi file complete ho → uska upload ready
      - 50% chain: 360p 50% hone ke baad hi 720p upload shuru,
                   720p 50% hone ke baad hi 1080p upload shuru
      - Sab kuch async — download aur upload overlap karte hain
    Returns True agar kam se kam 1 quality successfully upload hui, else False.
    """
    if matched_entry is None:
        matched_entry = {}

    from .swift_downloader import (
        _make_driver, _close_popups, _scan_for_360p,
        _collect_visible_links, _get_done_files, _in_progress,
        _upload_one_file, _sort_by_size, _quality_from, QUALITY_ORDER
    )

    start_time = time.time()
    loop = asyncio.get_event_loop()

    # ── Delete old bot messages from channel ──
    async def _delete_old_bot_msgs(ch_id: int):
        try:
            from .schedule_notify import get_last_posted_msg_ids, clear_last_posted_msg_ids
            saved_ids = await get_last_posted_msg_ids(ch_id)
            if not saved_ids:
                return
            try:
                await client.delete_messages(ch_id, saved_ids)
            except Exception:
                for mid in saved_ids:
                    try:
                        await client.delete_messages(ch_id, mid)
                        await asyncio.sleep(0.2)
                    except Exception:
                        pass
            await clear_last_posted_msg_ids(ch_id)
        except Exception as _e:
            LOGGER.warning(f"[AutoMonitor] _delete_old_bot_msgs error: {_e}")

    # ── ProxyMsg — upload channel_id pe jaaye ──
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

    # ── DL folder ──
    session_id = f"monitor_ep{episode_num}_{int(time.time())}"
    dl_dir = os.path.join(download_dir, session_id)
    os.makedirs(dl_dir, exist_ok=True)

    status_msg = await log_message.reply(
        f"🎌 **AutoMonitor** | `{anime_name}` | Ep `{episode_num}`\n\n"
        f"🌐 Chrome khul raha hai...\n"
        f"🔗 `{swift_url}`"
    )

    # ── Bot Mode check ──
    _upload_mode    = await _get_upload_mode_for_owner()
    _bot_mode_active = (_upload_mode == 'bot_mode')
    _bot_post_mgr: _BotModePostManager | None = None
    if _bot_mode_active:
        _bot_post_mgr = _BotModePostManager(client, channel_id, anime_name, episode_num)
        LOGGER.info(f"[AutoMonitor] Ep {episode_num}: BOT MODE active")

    # ──────────────────────────────────────────────────────
    #  STEP 1: Chrome se teeno links click karo (blocking)
    #  Returns list of clicked quality names — downloads
    #  background mein dl_dir mein aa rahe hain
    # ──────────────────────────────────────────────────────
    def _chrome_click_all() -> dict:
        """
        1 Chrome session: page open → 360p gate → saare links click.
        Download Chrome ke download manager mein shuru ho jaata hai.
        Returns {"qualities_clicked": [...], "error": str|None}
        """
        driver = None
        result = {"qualities_clicked": [], "error": None}
        try:
            driver = _make_driver(dl_dir)
            LOGGER.info(f"[AutoMonitor] Chrome opened for Ep {episode_num}")

            loaded = False
            for _a in range(3):
                try:
                    driver.get(swift_url)
                    loaded = True
                    break
                except Exception as e:
                    LOGGER.warning(f"[AutoMonitor] Page load attempt {_a+1}: {e}")
                    time.sleep(2)  # was 3s

            if not loaded:
                result["error"] = "Page load fail — Chrome renderer timeout"
                return result

            main = driver.current_window_handle
            _close_popups(driver, main)

            # 360p gate — max 20s
            found_360p = False
            for _s in range(40):  # 40 × 0.5s = 20s max
                _close_popups(driver, main)
                if _scan_for_360p(driver):
                    found_360p = True
                    break
                if _s < 39:
                    time.sleep(0.5)  # was 1s — 2x faster

            if not found_360p:
                result["error"] = "360p button 20s tak nahi mila — page render fail"
                return result

            # 360p gate pass hua — ab links collect karo
            # JS buttons fully render hone mein time lagta hai — retry loop
            links = []
            for _retry in range(8):
                _close_popups(driver, main)
                links = _collect_visible_links(driver)
                if links:
                    LOGGER.info(f"[AutoMonitor] Ep {episode_num}: Links collected retry {_retry+1} — {len(links)} found")
                    break
                LOGGER.info(f"[AutoMonitor] Ep {episode_num}: Links empty retry {_retry+1}, waiting 1s...")
                time.sleep(1)

            if not links:
                # Last resort: page scroll karke JS trigger karo
                try:
                    driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                    time.sleep(0.5)  # was 1s
                    driver.execute_script("window.scrollTo(0, 0);")
                    time.sleep(0.5)  # was 1s
                    _close_popups(driver, main)
                    links = _collect_visible_links(driver)
                    LOGGER.info(f"[AutoMonitor] Ep {episode_num}: After scroll — {len(links)} links")
                except Exception:
                    pass

            if not links:
                # Final fallback: Selenium se saare a.dl-btn directly click karo
                LOGGER.warning(f"[AutoMonitor] Ep {episode_num}: _collect_visible_links empty — trying direct Selenium click")
                clicked_fallback = []
                try:
                    from selenium.webdriver.common.by import By as _By
                    dl_btns = driver.find_elements(_By.CSS_SELECTOR, "a.dl-btn")
                    for btn in dl_btns:
                        try:
                            classes = btn.get_attribute("class") or ""
                            if "d-none" in classes:
                                continue
                            href  = btn.get_attribute("href") or ""
                            label = btn.text.strip()
                            if not href or href.startswith("about:") or len(href) < 10:
                                continue
                            q = _quality_from(label + " " + href)
                            driver.execute_script(f"window.open('{href}', '_blank');")
                            time.sleep(0.2)  # was 0.3s
                            _close_popups(driver, main)
                            clicked_fallback.append(q)
                            LOGGER.info(f"[AutoMonitor] Ep {episode_num}: Fallback clicked {q}")
                        except Exception as _be:
                            LOGGER.warning(f"[AutoMonitor] Fallback btn click failed: {_be}")
                except Exception as _fe:
                    LOGGER.error(f"[AutoMonitor] Fallback Selenium error: {_fe}")

                if clicked_fallback:
                    result["qualities_clicked"] = clicked_fallback
                    time.sleep(3)  # was 5s
                    return result

                result["error"] = "360p dikh gaya par download links nahi mile — JS render timeout"
                return result

            # Saare visible links click karo — downloads shuru
            clicked = []
            for lnk in links[:4]:
                q    = _quality_from(lnk["quality"])
                href = lnk["href"]
                try:
                    driver.execute_script(f"window.open('{href}', '_blank');")
                    time.sleep(0.2)  # was 0.3s
                    _close_popups(driver, main)
                    clicked.append(q)
                    LOGGER.info(f"[AutoMonitor] Ep {episode_num}: Clicked {q}")
                except Exception as e:
                    LOGGER.warning(f"[AutoMonitor] Click failed {q}: {e}")

            result["qualities_clicked"] = clicked

            # Downloads initiate hone do — 3s kafi hai (was 5s)
            time.sleep(3)

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

    # Chrome run karo — async block nahi hoga
    try:
        await status_msg.edit(
            f"🎌 **AutoMonitor** | `{anime_name}` | Ep `{episode_num}`\n\n"
            f"🌐 Chrome khul raha hai — links click ho rahe hain...\n"
            f"🔗 `{swift_url}`"
        )
    except Exception:
        pass

    click_result = await loop.run_in_executor(None, _chrome_click_all)

    if click_result["error"] and not click_result["qualities_clicked"]:
        # ── Chrome fail hua — V27 ka poll-based fallback shuru karo ──
        LOGGER.warning(f"[AutoMonitor] Ep {episode_num}: Chrome failed ({click_result['error'][:80]}). Switching to poll fallback...")
        shutil.rmtree(dl_dir, ignore_errors=True)

        from .swift_downloader import (
            _scrape_and_download, _upload_one_file, _sort_by_size, _quality_from, QUALITY_ORDER
        )

        POLL_INTERVAL_FAST = 30
        POLL_INTERVAL_SLOW = 60
        POLL_FAST_ATTEMPTS = 10
        POLL_SLOW_ATTEMPTS = 20
        TARGET_QUALITIES_SET = set(TARGET_QUALITIES)

        remaining = set(TARGET_QUALITIES_SET)
        poll_start = time.time()
        poll_attempt = 0
        _old_msgs_deleted_poll = False

        try:
            await status_msg.edit(
                f"🎌 **AutoMonitor** | `{anime_name}` | Ep `{episode_num}`\n\n"
                f"⚠️ Chrome fail — Poll mode shuru\n"
                f"🎯 Dhundh raha hoon: `{' | '.join(sorted(remaining))}`"
            )
        except Exception:
            pass

        while remaining:
            poll_attempt += 1
            is_fast = poll_attempt <= POLL_FAST_ATTEMPTS
            is_slow = POLL_FAST_ATTEMPTS < poll_attempt <= (POLL_FAST_ATTEMPTS + POLL_SLOW_ATTEMPTS)
            if not is_fast and not is_slow:
                break  # max attempts khatam

            interval = POLL_INTERVAL_FAST if is_fast else POLL_INTERVAL_SLOW
            elapsed_min = int((time.time() - poll_start) / 60)
            phase_lbl = "⚡ Fast" if is_fast else "🐢 Slow"

            try:
                await status_msg.edit(
                    f"🎌 **AutoMonitor** | `{anime_name}` | Ep `{episode_num}`\n\n"
                    f"🔄 Poll `{poll_attempt}` {phase_lbl} | Elapsed: `{elapsed_min}m`\n"
                    f"🎯 Baki: `{' | '.join(sorted(remaining))}`\n"
                    f"⏳ Swift page scan ho raha hai..."
                )
            except Exception:
                pass

            poll_session_id = f"monitor_ep{episode_num}_poll{poll_attempt}_{int(time.time())}"
            poll_dl_dir = os.path.join(download_dir, poll_session_id)
            os.makedirs(poll_dl_dir, exist_ok=True)

            try:
                poll_result = await loop.run_in_executor(
                    None, _scrape_and_download, swift_url, poll_dl_dir, None, None
                )
            except Exception as e:
                LOGGER.error(f"[AutoMonitor] Poll Ep {episode_num} attempt {poll_attempt} error: {e}")
                shutil.rmtree(poll_dl_dir, ignore_errors=True)
                await asyncio.sleep(interval)
                continue

            if poll_result["error"] and not poll_result["files"]:
                shutil.rmtree(poll_dl_dir, ignore_errors=True)
                await asyncio.sleep(interval)
                continue

            poll_files = poll_result.get("files", [])
            if not poll_files:
                shutil.rmtree(poll_dl_dir, ignore_errors=True)
                await asyncio.sleep(interval)
                continue

            poll_files = _sort_by_size(poll_files)
            new_files = [fp for fp in poll_files if _quality_from(os.path.basename(fp)) in remaining]

            if not new_files:
                shutil.rmtree(poll_dl_dir, ignore_errors=True)
                await asyncio.sleep(interval)
                continue

            # Files mili — upload karo
            qualities_found = [_quality_from(os.path.basename(f)) for f in new_files]
            try:
                await status_msg.edit(
                    f"🎌 **AutoMonitor** | `{anime_name}` | Ep `{episode_num}`\n\n"
                    f"✅ Poll mili: `{' | '.join(qualities_found)}`\n"
                    f"📤 Upload ho raha hai..."
                )
            except Exception:
                pass

            _dummy_msgs_poll = {}
            for fp in new_files:
                q = _quality_from(os.path.basename(fp))
                try:
                    dm = await log_message.reply(f"📤 **Uploading `{q}`** — Ep `{episode_num}`...")
                    _dummy_msgs_poll[fp] = dm
                except Exception:
                    _dummy_msgs_poll[fp] = status_msg

            _half_events_poll = [asyncio.Event() for _ in new_files]

            async def _poll_upload_task(filepath, idx):
                nonlocal _old_msgs_deleted_poll
                if idx > 0:
                    await _half_events_poll[idx - 1].wait()
                if idx == 0 and not _old_msgs_deleted_poll:
                    _old_msgs_deleted_poll = True
                    await _delete_old_bot_msgs(channel_id)
                um = _dummy_msgs_poll.get(filepath, status_msg)
                success, sent_msg, quality = await _upload_one_file(
                    client, proxy_msg, um, filepath, poll_dl_dir, encode=False,
                    on_half=_half_events_poll[idx],
                )
                try:
                    await um.delete()
                except Exception:
                    pass
                if success and sent_msg and quality == "360p":
                    is_first_ep = (start_ep is not None and episode_num == start_ep)
                    already_sent = (update_post_sent is not None and update_post_sent[0])
                    if is_first_ep and not already_sent:
                        try:
                            from .update_channel import send_update_post
                            from ..utils.auto_caption import extract_anime_info as _eai
                            _season = None
                            try:
                                _, _season, _ = _eai(os.path.basename(filepath), {})
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
                # ── Bot Mode: deep link lo aur post pe button add karo ──
                if success and sent_msg and _bot_mode_active and _bot_post_mgr:
                    try:
                        _deep_link = await _get_suhani_bot_link(sent_msg)
                        if _deep_link:
                            await _bot_post_mgr.add_quality(quality, _deep_link)
                    except Exception as _bme:
                        LOGGER.error(f"[BotMode] add_quality (poll) error: {_bme}")
                return success, sent_msg, quality

            poll_results = await asyncio.gather(
                *[_poll_upload_task(fp, i) for i, fp in enumerate(new_files)],
                return_exceptions=True,
            )

            for r in poll_results:
                if isinstance(r, Exception):
                    continue
                success, sent_msg, quality = r
                if success:
                    remaining.discard(quality)

            shutil.rmtree(poll_dl_dir, ignore_errors=True)

            if remaining:
                await asyncio.sleep(interval)

        # Poll loop khatam
        poll_elapsed = int((time.time() - poll_start) / 60)
        if not remaining:
            try:
                await status_msg.edit(
                    f"🎉 **Complete (Poll)!** | `{anime_name}` | Ep `{episode_num}`\n\n"
                    f"✅ Saari qualities upload ho gayi!\n"
                    f"⏱️ Total time: `{poll_elapsed}m`"
                )
            except Exception:
                pass
        else:
            missing_str = ' | '.join(sorted(remaining))
            try:
                await status_msg.edit(
                    f"⚠️ **Incomplete!** | `{anime_name}` | Ep `{episode_num}`\n\n"
                    f"❌ Timeout ke baad bhi nahi mili: `{missing_str}`\n"
                    f"✅ Jo mili: `{' | '.join(q for q in TARGET_QUALITIES if q not in remaining)}`\n\n"
                    f"RTI pe manually check karo."
                )
            except Exception:
                pass
        # Poll fallback mein jo bhi upload hua uske basis pe return karo
        uploaded_in_poll = set(TARGET_QUALITIES) - remaining
        return len(uploaded_in_poll) > 0

    expected_count = len(click_result["qualities_clicked"])
    LOGGER.info(f"[AutoMonitor] Ep {episode_num}: {expected_count} downloads started — {click_result['qualities_clicked']}")

    # ──────────────────────────────────────────────────────
    #  STEP 2: File watcher — jaise hi ek file complete ho
    #  uska Event set karo taaki upload shuru ho sake
    # ──────────────────────────────────────────────────────

    # Tracked files: {filepath: asyncio.Event} — event set = file ready for upload
    _file_ready_events: dict[str, asyncio.Event] = {}
    _file_ready_lock = asyncio.Lock()
    _all_done_event = asyncio.Event()

    # 50% chain events — order: size ke hisaab se (360p=smallest → first)
    # file[0] ka half_event fire hone pe file[1] upload shuru
    # file[1] ka half_event fire hone pe file[2] upload shuru
    # ye events watcher ke baad files sort hone ke baad assign honge
    _half_events_map: dict[str, asyncio.Event] = {}   # filepath → on_half event

    async def _file_watcher():
        """
        Poller: har 2s pe dl_dir check karo.
        Nayi complete file mili → uska ready event set karo.
        Sab aa gaye → _all_done_event set karo.
        """
        seen = set()
        timeout_start = time.time()
        MAX_WAIT = 1800  # 30 min max

        while True:
            if time.time() - timeout_start > MAX_WAIT:
                LOGGER.warning(f"[AutoMonitor] Ep {episode_num}: File watcher timeout!")
                break

            done_files = _get_done_files(dl_dir)
            for fp in done_files:
                if fp not in seen:
                    seen.add(fp)
                    async with _file_ready_lock:
                        if fp not in _file_ready_events:
                            ev = asyncio.Event()
                            _file_ready_events[fp] = ev
                        _file_ready_events[fp].set()
                        q = _quality_from(os.path.basename(fp))
                        LOGGER.info(f"[AutoMonitor] Ep {episode_num}: ✅ {q} download complete → upload ready")

            # Sab expected files aa gayi?
            if len(seen) >= expected_count and not _in_progress(dl_dir):
                _all_done_event.set()
                break

            await asyncio.sleep(2)

        # Edge case: jo bhi mili unhe ready mark karo
        for fp in _get_done_files(dl_dir):
            async with _file_ready_lock:
                if fp not in _file_ready_events:
                    ev = asyncio.Event()
                    _file_ready_events[fp] = ev
                _file_ready_events[fp].set()

        _all_done_event.set()

    # ──────────────────────────────────────────────────────
    #  STEP 3: Upload orchestrator
    #  - Sab files ka wait karo (all_done)
    #  - Sort karo quality order mein
    #  - Phir 50% chain ke saath upload karo
    #  - Har file apna download-ready event wait karti hai
    #    AUR pichli file ka 50% event bhi
    # ──────────────────────────────────────────────────────

    uploaded_results = []
    _old_msgs_deleted = False

    async def _upload_pipeline():
        nonlocal _old_msgs_deleted

        # Pehle sab files aane ka wait karo
        await _all_done_event.wait()

        all_files = _sort_by_size(_get_done_files(dl_dir))
        if not all_files:
            try:
                await status_msg.edit(
                    f"❌ **AutoMonitor** | `{anime_name}` | Ep `{episode_num}`\n\n"
                    f"Koi file download nahi hui."
                )
            except Exception:
                pass
            return

        # 50% chain events banao — ek per file
        half_events = [asyncio.Event() for _ in all_files]

        # Status update
        qualities_list = ' → '.join(_quality_from(os.path.basename(f)) for f in all_files)
        try:
            await status_msg.edit(
                f"✅ **AutoMonitor** | `{anime_name}` | Ep `{episode_num}`\n\n"
                f"📦 `{qualities_list}`\n"
                f"📤 Upload pipeline shuru..."
            )
        except Exception:
            pass

        # Status messages banao
        dummy_msgs = {}
        for fp in all_files:
            q = _quality_from(os.path.basename(fp))
            try:
                dm = await log_message.reply(f"📤 **Uploading `{q}`** — Ep `{episode_num}`...")
                dummy_msgs[fp] = dm
            except Exception:
                dummy_msgs[fp] = status_msg

        async def _upload_one(filepath, idx):
            nonlocal _old_msgs_deleted

            # WAIT 1: Apni file ka download complete hone ka wait
            ready_ev = _file_ready_events.get(filepath)
            if ready_ev:
                await ready_ev.wait()
            else:
                # Edge case: watcher se pehle hi yahan pahunch gaye
                while filepath not in _file_ready_events:
                    await asyncio.sleep(1)
                await _file_ready_events[filepath].wait()

            # WAIT 2: 50% chain — pichli file 50% hone ka wait
            if idx > 0:
                await half_events[idx - 1].wait()

            # Pehli file se pehle old messages delete
            if idx == 0 and not _old_msgs_deleted:
                _old_msgs_deleted = True
                await _delete_old_bot_msgs(channel_id)

            q = _quality_from(os.path.basename(filepath))
            LOGGER.info(f"[AutoMonitor] Ep {episode_num}: 🚀 Upload start — {q}")

            um = dummy_msgs.get(filepath, status_msg)
            success, sent_msg, quality = await _upload_one_file(
                client, proxy_msg, um, filepath, dl_dir, encode=False,
                on_half=half_events[idx],
            )
            try:
                await um.delete()
            except Exception:
                pass

            # update_post — first episode ka 360p pe
            if success and sent_msg and quality == "360p":
                is_first_ep  = (start_ep is not None and episode_num == start_ep)
                already_sent = (update_post_sent is not None and update_post_sent[0])
                if is_first_ep and not already_sent:
                    try:
                        from .update_channel import send_update_post
                        from ..utils.auto_caption import extract_anime_info as _eai
                        _season = None
                        try:
                            _, _season, _ = _eai(os.path.basename(filepath), {})
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

            # ── Bot Mode: deep link lo aur post pe button add karo ──
            if success and sent_msg and _bot_mode_active and _bot_post_mgr:
                try:
                    _deep_link = await _get_suhani_bot_link(sent_msg)
                    if _deep_link:
                        await _bot_post_mgr.add_quality(quality, _deep_link)
                except Exception as _bme:
                    LOGGER.error(f"[BotMode] add_quality (chrome) error: {_bme}")

            return success, sent_msg, quality

        results = await asyncio.gather(
            *[_upload_one(fp, i) for i, fp in enumerate(all_files)],
            return_exceptions=True,
        )

        for r in results:
            if isinstance(r, Exception):
                LOGGER.error(f"[AutoMonitor] Upload task error: {r}")
                continue
            success, sent_msg, quality = r
            if success:
                uploaded_results.append((quality, sent_msg))
                LOGGER.info(f"[AutoMonitor] Ep {episode_num}: ✅ {quality} uploaded!")

    # Progress updater — download phase ke liye
    async def _dl_progress():
        s = time.time()
        while not _all_done_event.is_set():
            done    = _get_done_files(dl_dir)
            in_prog = _in_progress(dl_dir)
            elapsed = int(time.time() - s)
            total_mb = sum(
                os.path.getsize(f) for f in glob.glob(os.path.join(dl_dir, "*"))
                if os.path.isfile(f)
            ) / (1024 * 1024)
            ready_qs = [_quality_from(os.path.basename(f)) for f in done]
            try:
                await status_msg.edit(
                    f"⬇️ **AutoMonitor Downloading...**\n\n"
                    f"🎌 `{anime_name}` | Ep `{episode_num}`\n"
                    f"✅ Ready : `{' | '.join(ready_qs) or '—'}`\n"
                    f"📥 In Progress : `{len(in_prog)}`\n"
                    f"💾 Downloaded : `{total_mb:.1f} MB`\n"
                    f"⏱️ Elapsed : `{elapsed}s`"
                )
            except Exception:
                pass
            await asyncio.sleep(5)

    # Teeno tasks parallel chalao
    await asyncio.gather(
        _file_watcher(),
        _upload_pipeline(),
        _dl_progress(),
    )

    shutil.rmtree(dl_dir, ignore_errors=True)

    # ── Missing quality retry loop ──
    # Jo qualities upload nahi hui unhe 25 min tak same swift_url se retry karo
    uploaded_so_far = set(q for q, _ in uploaded_results)
    missing_after_chrome = set(TARGET_QUALITIES) - uploaded_so_far

    if missing_after_chrome:
        from .swift_downloader import (
            _scrape_and_download, _upload_one_file, _sort_by_size, _quality_from
        )

        RETRY_FAST_ATTEMPTS = 10
        RETRY_SLOW_ATTEMPTS = 20
        retry_attempt = 0
        retry_remaining = set(missing_after_chrome)
        _old_msgs_deleted_retry = False

        LOGGER.info(f"[AutoMonitor] Ep {episode_num}: Missing qualities after Chrome — retrying: {retry_remaining}")

        try:
            await status_msg.edit(
                f"⏳ **AutoMonitor** | `{anime_name}` | Ep `{episode_num}`\n\n"
                f"✅ Uploaded: `{' | '.join(sorted(uploaded_so_far)) or '—'}`\n"
                f"🔄 Missing retry shuru: `{' | '.join(sorted(retry_remaining))}`"
            )
        except Exception:
            pass

        while retry_remaining:
            retry_attempt += 1
            is_fast = retry_attempt <= RETRY_FAST_ATTEMPTS
            is_slow = RETRY_FAST_ATTEMPTS < retry_attempt <= (RETRY_FAST_ATTEMPTS + RETRY_SLOW_ATTEMPTS)
            if not is_fast and not is_slow:
                break

            interval = 30 if is_fast else 60
            phase_lbl = "⚡ Fast" if is_fast else "🐢 Slow"

            try:
                await status_msg.edit(
                    f"⏳ **AutoMonitor** | `{anime_name}` | Ep `{episode_num}`\n\n"
                    f"🔄 Quality retry `{retry_attempt}` {phase_lbl}\n"
                    f"🎯 Dhundh raha hoon: `{' | '.join(sorted(retry_remaining))}`\n"
                    f"⏰ `{interval}s` baad scan..."
                )
            except Exception:
                pass

            await asyncio.sleep(interval)

            for q_target in list(retry_remaining):
                retry_session_id = f"monitor_ep{episode_num}_retry{retry_attempt}_{q_target}_{int(time.time())}"
                retry_dl_dir = os.path.join(download_dir, retry_session_id)
                os.makedirs(retry_dl_dir, exist_ok=True)

                try:
                    retry_result = await loop.run_in_executor(
                        None, _scrape_and_download, swift_url, retry_dl_dir, None, q_target
                    )
                except Exception as e:
                    LOGGER.error(f"[AutoMonitor] Quality retry error ({q_target}): {e}")
                    shutil.rmtree(retry_dl_dir, ignore_errors=True)
                    continue

                if retry_result["error"] and not retry_result["files"]:
                    LOGGER.info(f"[AutoMonitor] Ep {episode_num}: {q_target} not available yet (attempt {retry_attempt})")
                    shutil.rmtree(retry_dl_dir, ignore_errors=True)
                    continue

                retry_files = _sort_by_size(retry_result.get("files", []))
                target_file = next(
                    (f for f in retry_files if _quality_from(os.path.basename(f)) == q_target),
                    None
                )

                if not target_file:
                    shutil.rmtree(retry_dl_dir, ignore_errors=True)
                    continue

                # File mili — upload karo
                try:
                    await status_msg.edit(
                        f"✅ **AutoMonitor** | `{anime_name}` | Ep `{episode_num}`\n\n"
                        f"`{q_target}` mil gaya! Upload ho raha hai..."
                    )
                except Exception:
                    pass

                retry_um = await log_message.reply(f"📤 **Uploading `{q_target}`** — Ep `{episode_num}` (retry)...")

                if not _old_msgs_deleted_retry and not uploaded_so_far:
                    _old_msgs_deleted_retry = True
                    await _delete_old_bot_msgs(channel_id)

                retry_half_ev = asyncio.Event()
                success, sent_msg_r, quality_r = await _upload_one_file(
                    client, proxy_msg, retry_um, target_file, retry_dl_dir, encode=False,
                    on_half=retry_half_ev,
                )
                try:
                    await retry_um.delete()
                except Exception:
                    pass

                if success:
                    uploaded_results.append((quality_r, sent_msg_r))
                    uploaded_so_far.add(quality_r)
                    retry_remaining.discard(quality_r)
                    LOGGER.info(f"[AutoMonitor] Ep {episode_num}: ✅ {quality_r} retry upload done!")

                    # update_post check — 360p pe
                    if quality_r == "360p":
                        is_first_ep = (start_ep is not None and episode_num == start_ep)
                        already_sent = (update_post_sent is not None and update_post_sent[0])
                        if is_first_ep and not already_sent:
                            try:
                                from .update_channel import send_update_post
                                from ..utils.auto_caption import extract_anime_info as _eai
                                _season = None
                                try:
                                    _, _season, _ = _eai(os.path.basename(target_file), {})
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
                                LOGGER.error(f"[AutoMonitor] Update post retry error: {_ue}")

                    # ── Bot Mode: deep link lo aur post pe button add karo ──
                    if _bot_mode_active and _bot_post_mgr:
                        try:
                            _deep_link = await _get_suhani_bot_link(sent_msg_r)
                            if _deep_link:
                                await _bot_post_mgr.add_quality(quality_r, _deep_link)
                        except Exception as _bme:
                            LOGGER.error(f"[BotMode] add_quality (retry) error: {_bme}")

                shutil.rmtree(retry_dl_dir, ignore_errors=True)

        # Retry loop khatam — final missing log
        if retry_remaining:
            LOGGER.warning(f"[AutoMonitor] Ep {episode_num}: Still missing after retries: {retry_remaining}")
            try:
                await status_msg.edit(
                    f"⚠️ **Incomplete!** | `{anime_name}` | Ep `{episode_num}`\n\n"
                    f"✅ Uploaded: `{' | '.join(sorted(uploaded_so_far)) or '—'}`\n"
                    f"❌ Timeout ke baad bhi nahi mili: `{' | '.join(sorted(retry_remaining))}`\n\n"
                    f"RTI pe manually check karo."
                )
            except Exception:
                pass

    # ── Final summary ──
    elapsed_min = int((time.time() - start_time) / 60)
    uploaded_qualities = sorted(
        [q for q, _ in uploaded_results],
        key=lambda q: ["360p", "480p", "720p", "1080p"].index(q) if q in ["360p", "480p", "720p", "1080p"] else 99
    )
    uploaded_count = len(uploaded_results)

    if uploaded_count > 0 and not missing_after_chrome - set(uploaded_so_far):
        # Sab qualities upload ho gayi
        try:
            await status_msg.edit(
                f"🎉 **Complete!** | `{anime_name}` | Ep `{episode_num}`\n\n"
                f"✅ Uploaded : `{uploaded_count}`\n"
                f"📊 `{' → '.join(uploaded_qualities) or 'N/A'}`\n"
                f"⏱️ Time: `{elapsed_min}m`"
            )
        except Exception:
            pass

    LOGGER.info(f"[AutoMonitor] Ep {episode_num}: done in {elapsed_min}m — {uploaded_qualities}")
    return uploaded_count > 0


# ─────────────────────────────────────────────
#  Bot Mode Helpers
# ─────────────────────────────────────────────

async def _get_upload_mode_for_owner() -> str:
    """Owner ka current upload mode return karo — 'file_mode' ya 'bot_mode'."""
    try:
        from .upload_mode_plugin import get_upload_mode
        oid = await _owner_id()
        if not oid:
            return 'file_mode'
        return await get_upload_mode(oid)
    except Exception:
        return 'file_mode'


async def _get_suhani_bot_link(log_channel_msg) -> str | None:
    """
    Log channel pe upload hua message ka Suhani bot deep link banao.
    Format: https://t.me/Get_Suhani_bot?start=<base64(chatid_msgid)>
    """
    if not log_channel_msg:
        return None
    try:
        import base64
        chat_id = log_channel_msg.chat.id
        msg_id  = log_channel_msg.id
        raw     = f"{chat_id}_{msg_id}".encode()
        encoded = base64.urlsafe_b64encode(raw).decode().rstrip('=')
        return f"https://t.me/Get_Suhani_bot?start={encoded}"
    except Exception as _e:
        LOGGER.warning(f"[BotMode] deep link error: {_e}")
        return None


class _BotModePostManager:
    """
    Ek episode ke liye channel pe ek post manage karo.
    Pehli quality → nayi post. Agle quality → same post edit.
    """

    QUALITY_ORDER = ["360p", "480p", "720p", "1080p"]
    QUALITY_EMOJI = {"360p": "🟢", "480p": "🟡", "720p": "🟢", "1080p": "🔴"}

    def __init__(self, client, channel_id: int, anime_name: str, episode_num: int):
        self.client      = client
        self.channel_id  = channel_id
        self.anime_name  = anime_name
        self.episode_num = episode_num
        self.post_msg_id: int | None = None
        self._buttons: dict[str, str] = {}
        self._lock       = asyncio.Lock()

    def _build_keyboard(self) -> InlineKeyboardMarkup | None:
        row = []
        for q in self.QUALITY_ORDER:
            if q in self._buttons:
                emoji = self.QUALITY_EMOJI.get(q, "▶️")
                row.append(InlineKeyboardButton(text=f"{emoji} {q} ↗", url=self._buttons[q]))
        return InlineKeyboardMarkup([row]) if row else None

    def _build_caption(self) -> str:
        ep_str = f"Episode {self.episode_num:02d}" if self.episode_num else "Episode ??"
        qualities_ready = [q for q in self.QUALITY_ORDER if q in self._buttons]
        qual_str = " | ".join(qualities_ready) if qualities_ready else "Coming soon..."
        return (
            f"🎌 <b>{self.anime_name}</b>\n"
            f"📺 <b>{ep_str}</b>\n\n"
            f"✅ Available: <code>{qual_str}</code>"
        )

    async def add_quality(self, quality: str, deep_link_url: str):
        """Quality ka button add/update karo — pehli baar post banao, baad mein edit."""
        async with self._lock:
            self._buttons[quality] = deep_link_url
            keyboard = self._build_keyboard()
            caption  = self._build_caption()

            if self.post_msg_id is None:
                try:
                    sent = await self.client.send_message(
                        chat_id=self.channel_id,
                        text=caption,
                        reply_markup=keyboard,
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                    self.post_msg_id = sent.id
                    LOGGER.info(
                        f"[BotMode] Post created — {self.anime_name} Ep {self.episode_num} "
                        f"msg_id={sent.id} quality={quality}"
                    )
                except Exception as e:
                    LOGGER.error(f"[BotMode] Post create error: {e}")
            else:
                try:
                    await self.client.edit_message_text(
                        chat_id=self.channel_id,
                        message_id=self.post_msg_id,
                        text=caption,
                        reply_markup=keyboard,
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                    LOGGER.info(
                        f"[BotMode] Post edited — {self.anime_name} Ep {self.episode_num} "
                        f"msg_id={self.post_msg_id} added={quality}"
                    )
                except Exception as e:
                    LOGGER.error(f"[BotMode] Post edit error: {e}")


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
    # Track karo ki kam se kam ek episode successfully upload hua ya nahi
    any_ep_uploaded = False

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
        ep_uploaded = await _episode_quality_poller(
            client, message, swift_url,
            ep_num, anime_name, channel_id, oid,
            matched_entry=matched,
            start_ep=start_ep,
            end_ep=end_ep,
            update_post_sent=update_post_sent,
        )
        if ep_uploaded:
            any_ep_uploaded = True

        # Episodes ke beech thoda gap
        if not is_last:
            await asyncio.sleep(3)

    # ── Sirf last episode ke baad schedule/end message bhejo ──
    # Lekin tabhi jab kam se kam ek episode successfully upload hua ho
    if any_ep_uploaded:
        try:
            send_schedule_notification = _get_schedule_fn()
            await send_schedule_notification(client, channel_id, anime_name, end_ep)
            LOGGER.info(f"[AutoMonitor] ✅ Schedule notification sent after last ep {end_ep}")
        except Exception as e:
            LOGGER.error(f"[AutoMonitor] Schedule notification error: {e}")
    else:
        LOGGER.warning(f"[AutoMonitor] No episodes uploaded — schedule notification skipped.")


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
