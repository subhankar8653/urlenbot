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
  /add_anime            → 4-step button-driven flow — channel, naam
                          (auto/manual), poster (auto/custom), audio
                          (ORG/FanDub) + quick interval/link. Anime +
                          monitor + update-post + schedule sab EK saath
                          set ho jaate hain.
  /cancel_add_anime     → Beech mein /add_anime cancel karo
  /list_anime           → Kya set hai dekho
  /del_anime [number]   → Remove karo
  /set_monitor          → Monitor channel set karo (forward reply ya ID)
  /monitor_status       → System health check
"""

import asyncio
import glob
import html
import logging
import os
import re
import shutil
import time

from pyrogram import Client, filters, StopPropagation, ContinuePropagation
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pyrogram.enums import ParseMode

from .. import LOGGER, app, owner, sudo_users, download_dir, log
from ..utils.database.access_db import db
from ..utils.anime_api import fetch_anime_details
from ..utils.helper import check_chat

# ─────────────────────────────────────────────
#  Lazy imports (avoid circular on startup)
# ─────────────────────────────────────────────
def _get_rti_fns():
    from .rti_downloader import get_watchmult_link, get_argon_link, argon_to_swift
    return get_watchmult_link, get_argon_link, argon_to_swift

def _get_rti_latest_fn():
    from .rti_downloader import get_latest_episode
    return get_latest_episode

def _get_schedule_fn():
    from .schedule_notify import send_schedule_notification
    return send_schedule_notification

def _get_update_post_fns():
    from .update_channel import _get_post_map, _save_post_map
    return _get_post_map, _save_post_map

def _get_schedule_list_fns():
    from .schedule_notify import _get_schedule_list, _save_schedule_list
    return _get_schedule_list, _save_schedule_list

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


DEFAULT_MONITOR_CHANNEL_ID = -1003950952828


async def _get_monitor_channel() -> int | None:
    """
    Monitor channel ID lo. Agar owner ne /set_monitor se kabhi apna
    channel set nahi kiya, toh DEFAULT_MONITOR_CHANNEL_ID use hota hai
    (pehle se hi ek channel monitor ke liye ready rehta hai).
    """
    oid = await _owner_id()
    if not oid:
        return DEFAULT_MONITOR_CHANNEL_ID
    user = await db._get_user(oid)
    val = user.get('monitor_channel_id')
    return int(val) if val else DEFAULT_MONITOR_CHANNEL_ID


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
    # "EPISODE 4-13 + ZIP PACK ADDED!" / "Episode 34-36" / "Ep 34 - 36" / "EP 34-36"
    m = re.search(r'episodes?\s*(\d+)\s*[-\u2013]\s*(\d+)', text, re.IGNORECASE)
    if m:
        return int(m.group(1)), int(m.group(2))
    # Ep/Episode + to range (case-insensitive — handles all-caps "EP" / "EPISODE" too)
    m = re.search(r'ep(?:isode)?\s*(\d+)\s*[-\u2013to]+\s*(\d+)', text, re.IGNORECASE)
    if m:
        return int(m.group(1)), int(m.group(2))
    # Single "Episode 5" / "EP 2" / "ep 2" — grabs the FIRST ep mentioned
    # (matches your convention of always listing Hindi ep first, e.g.
    #  "EP 2 HINDI DUB + EP 4 HINDI SUB ADDED!" -> picks Ep 2)
    m = re.search(r'ep(?:isode)?\s*(\d+)', text, re.IGNORECASE)
    if m:
        ep = int(m.group(1))
        return ep, ep
    # "34-36 Added" / "34-36 + ZIP PACK ADDED"
    m = re.search(r'(\d+)\s*[-\u2013]\s*(\d+)[^\n]*added', text, re.IGNORECASE)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None


def _extract_url(text: str) -> str | None:
    m = re.search(r'https?://[^\s]+', text)
    return m.group(0) if m else None


# ─────────────────────────────────────────────
#  OLD Format — Language + Alt-Dub Guard
#  ─────────────────────────────────────────────
#  Yeh channel SIRF Hindi ke liye hai. Do cases mein OLD-format post
#  ko SKIP karna hai — chahe URL + episode number match ho jaaye:
#
#  1. Title mein koi specific non-Hindi language ka naam explicitly
#     likha ho (Tamil, Telugu, English, Malayalam, Kannada, Bengali,
#     Marathi, Punjabi, Gujarati, Urdu) AUR "Hindi" word kahin bhi
#     na ho.
#       "Episode 4 Tamil-Telugu Added!"          -> SKIP (Hindi nahi hai)
#       "Episode 4 Hindi-Tamil-Telugu Added!"    -> ALLOW (Hindi hai)
#       "Episode 4 Added!"                       -> ALLOW (koi language
#                                                    naam hi nahi, default
#                                                    Hindi maana jaata hai)
#
#  2. "Added!" ke turant baad ek "(...)" bracket ho jisme kuch likha ho
#     — yeh ek ALTERNATE/SECOND dub source ko batata hai
#     (e.g. "Episode 2 Added! (AnimeTimes DUB)"), jo already handle ho
#     chuke episode ka duplicate/alt release hota hai. Aise posts skip.
#
#  IMPORTANT: dono check sirf URL SE PEHLE wale "title" segment pe
#  lagte hain, poore text pe nahi — kyunki RTI ke URLs mein hamesha
#  "/hindi/...-hindi-dubbed-..." jaisa path hota hai (site ka apna
#  naming convention), jo warna har baar false-positive "Hindi mila"
#  de deta chahe title mein Hindi ka naam-o-nishaan na ho.
_NON_HINDI_LANG_RE = re.compile(
    r'\b(tamil|telugu|malayalam|kannada|bengali|marathi|punjabi|gujarati|urdu|english)\b',
    re.IGNORECASE
)
_HINDI_WORD_RE     = re.compile(r'\bhindi\b', re.IGNORECASE)
_ADDED_BRACKET_RE  = re.compile(r'added\s*!?\s*\(([^)]+)\)', re.IGNORECASE)


def _old_format_title_segment(text: str, url: str | None) -> str:
    """URL se pehle wala hissa nikaalo — language/bracket checks isi
    pe lagane hain, URL ke andar ke words (jaise '/hindi/') pe nahi."""
    if url:
        idx = text.find(url)
        if idx != -1:
            return text[:idx]
    return text


def _old_format_should_skip(text: str, url: str | None) -> str | None:
    """OLD-format post ko skip karne ki wajah return karta hai (logging
    ke liye), ya None agar post process karna theek hai."""
    title_seg = _old_format_title_segment(text, url)

    # ── Sirf non-Hindi language(s) named, Hindi kahin nahi ──
    if _NON_HINDI_LANG_RE.search(title_seg) and not _HINDI_WORD_RE.search(title_seg):
        return f"non-Hindi language post (no 'Hindi' in title): {title_seg.strip()[:80]}"

    # ── "Added! (kuch bhi)" — alt/second dub source ──
    m = _ADDED_BRACKET_RE.search(title_seg)
    if m:
        return f"alt-dub bracket detected ({m.group(1).strip()}): {title_seg.strip()[:80]}"

    return None


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

    # ── Bot Mode check ── (ProxyMsg banane se PEHLE karna zaroori hai,
    # warna _bot_mode_active use-before-assign error aata hai)
    _upload_mode    = await _get_upload_mode_for_owner()
    _bot_mode_active = (_upload_mode == 'bot_mode')
    _bot_post_mgr: _BotModePostManager | None = None
    if _bot_mode_active:
        _bot_post_mgr = _BotModePostManager(client, channel_id, anime_name, episode_num)
        LOGGER.info(f"[AutoMonitor] Ep {episode_num}: BOT MODE active")

    # ── ProxyMsg — upload target pe jaaye ──
    # file_mode: channel_id pe directly upload
    # bot_mode: log channel pe upload (taaki sent_msg.link mil sake for buttons)
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

    # bot_mode: proxy_msg.chat.id = LOG_CHANNEL (file wahan upload hogi)
    # file_mode: proxy_msg.chat.id = channel_id (seedha channel pe)
    if _bot_mode_active:
        from .. import log as _LOG_CH
        proxy_msg = _ProxyMsg(log_message, owner_id, _LOG_CH)
    else:
        proxy_msg = _ProxyMsg(log_message, owner_id, channel_id)

    status_msg = await log_message.reply(
        f"🎌 **AutoMonitor** | `{anime_name}` | Ep `{episode_num}`\n\n"
        f"⏳ Swift page scan ho raha hai..."
    )

    # ──────────────────────────────────────────────────────
    #  Direct Poll Mode — Chrome attempt skip, seedha scrape
    # ──────────────────────────────────────────────────────
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

    while remaining:
        poll_attempt += 1
        is_fast = poll_attempt <= POLL_FAST_ATTEMPTS
        is_slow = POLL_FAST_ATTEMPTS < poll_attempt <= (POLL_FAST_ATTEMPTS + POLL_SLOW_ATTEMPTS)
        if not is_fast and not is_slow:
            break

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

        qualities_found = [_quality_from(os.path.basename(f)) for f in new_files]
        try:
            await status_msg.edit(
                f"🎌 **AutoMonitor** | `{anime_name}` | Ep `{episode_num}`\n\n"
                f"✅ Mili: `{' | '.join(qualities_found)}`\n"
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
                skip_forward=_bot_mode_active,
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
            if success and sent_msg and _bot_mode_active and _bot_post_mgr:
                try:
                    _deep_link = await _get_suhani_bot_link(sent_msg)
                    if _deep_link:
                        _bm_season = None
                        _bm_lang = None
                        if quality == "360p":
                            try:
                                from ..utils.auto_caption import extract_anime_info as _eai2, detect_language_from_filename as _dlf
                                _, _bm_season, _ = _eai2(os.path.basename(filepath), {})
                                _langs = _dlf(os.path.basename(filepath))
                                _bm_lang = " + ".join(_langs) if _langs else "Hindi"
                            except Exception:
                                pass
                        await _bot_post_mgr.add_quality(quality, _deep_link, season=_bm_season, language=_bm_lang)
                    else:
                        LOGGER.warning(f"[BotMode] No link for {quality}")
                except Exception as _bme:
                    LOGGER.error(f"[BotMode] add_quality error: {_bme}")
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
    uploaded_qualities = sorted(
        set(TARGET_QUALITIES) - remaining,
        key=lambda q: ["360p", "480p", "720p", "1080p"].index(q) if q in ["360p", "480p", "720p", "1080p"] else 99
    )

    if not remaining:
        try:
            await status_msg.edit(
                f"🎉 **Complete!** | `{anime_name}` | Ep `{episode_num}`\n\n"
                f"✅ Uploaded: `{' → '.join(uploaded_qualities)}`\n"
                f"⏱️ Time: `{poll_elapsed}m`"
            )
        except Exception:
            pass
    else:
        missing_str = ' | '.join(sorted(remaining))
        try:
            await status_msg.edit(
                f"⚠️ **Incomplete!** | `{anime_name}` | Ep `{episode_num}`\n\n"
                f"❌ Timeout ke baad bhi nahi mili: `{missing_str}`\n"
                f"✅ Jo mili: `{' | '.join(uploaded_qualities) or '—'}`\n\n"
                f"RTI pe manually check karo."
            )
        except Exception:
            pass

    LOGGER.info(f"[AutoMonitor] Ep {episode_num}: done in {poll_elapsed}m — {uploaded_qualities}")
    return len(uploaded_qualities) > 0


# ─────────────────────────────────────────────
#  Bot Mode Helpers
# ─────────────────────────────────────────────

async def _get_upload_mode_for_owner() -> str:
    """
    Kisi bhi owner/sudo user ne 'bot_mode' set kiya hai to wahi use karo.
    (owner[0] use karna unreliable hai — kyunki OWNER_ID multiple ids
    ho sakta hai aur set() order guarantee nahi karta)
    """
    try:
        from .upload_mode_plugin import get_upload_mode
        from .. import owner as _OWNERS, sudo_users as _SUDOS
        for _uid in list(_OWNERS) + list(_SUDOS):
            try:
                _mode = await get_upload_mode(_uid)
                if _mode == 'bot_mode':
                    return 'bot_mode'
            except Exception:
                continue
        return 'file_mode'
    except Exception:
        return 'file_mode'



async def _get_suhani_bot_link(log_channel_msg, timeout: int = 30) -> str | None:
    """
    Log channel pe upload ke baad dusra bot 'Link Ready!' message bhejta hai
    — wo message hamesha uploaded video ke turant baad (msg_id + 1) hota hai,
    aur 1-3 second ke andar ban jaata hai.
    Us message se https://t.me/Get_Suhani_bot?start=... URL uthao.

    log_channel_msg = woh message jo log channel pe upload hua (sent_msg)
    timeout = kitne seconds tak wait karo (default 30s)
    """
    if not log_channel_msg:
        return None

    import re as _re
    from .. import log as _LOG_CHANNEL_ID

    target_id = log_channel_msg.id + 1
    LOGGER.info(f"[BotMode] Polling for Link Ready at msg_id={target_id}")

    start_time = asyncio.get_event_loop().time()

    while asyncio.get_event_loop().time() - start_time < timeout:
        try:
            # target_id aur uske aas-paas ke 2-3 messages bhi check karo
            # (agar koi extra message beech mein aa jaye)
            for tid in (target_id, target_id + 1, target_id + 2):
                try:
                    msgs = await app.get_messages(_LOG_CHANNEL_ID, tid)
                    if not msgs:
                        continue
                    msg_list = msgs if isinstance(msgs, list) else [msgs]
                    for m in msg_list:
                        if not m or not m.text:
                            continue
                        text = m.text or ""
                        if "t.me/Get_Suhani_bot" in text:
                            match = _re.search(r'https://t\.me/Get_Suhani_bot\?start=\S+', text)
                            if match:
                                url = match.group(0).strip()
                                LOGGER.info(f"[BotMode] ✅ Link Ready found at msg_id={tid}: {url[:60]}")
                                return url
                except Exception:
                    pass
        except Exception as _e:
            LOGGER.warning(f"[BotMode] poll error: {_e}")

        await asyncio.sleep(1.5)

    LOGGER.warning(f"[BotMode] ⏰ Link Ready timeout ({timeout}s) for msg_id={target_id}")
    return None





class _BotModePostManager:
    """
    Ek episode ke liye channel pe ek post manage karo.
    Pehli quality → nayi post. Agle quality → same post edit.

    Progressive flow (360p → 720p → 1080p):
      - 360p upload:  [➲ 360p] [⏳ 720p uploading...]  [⏳ 1080p uploading...]
      - 720p upload:  [➲ 360p] [➲ 720p]               [⏳ 1080p uploading...]
      - 1080p upload: [➲ 360p] [➲ 720p]               [➲ 1080p]
    """

    # Sirf ye 3 qualities is bot mein upload hoti hain
    UPLOAD_QUALITIES = ["360p", "720p", "1080p"]
    # Full order (agar kabhi 480p bhi aaye to sahi jagah pe aaye)
    QUALITY_ORDER    = ["360p", "480p", "720p", "1080p"]

    def __init__(self, client, channel_id: int, anime_name: str, episode_num: int):
        self.client      = client
        self.channel_id  = channel_id
        self.anime_name  = anime_name
        self.episode_num = episode_num
        self.season_num: int | None = None    # add_quality(season=...) se set hoga
        self.language_str: str = "Hindi"      # add_quality(language=...) se set hoga
        self.post_msg_id: int | None = None
        self._buttons: dict[str, str] = {}    # quality → deep_link_url (ready ones)
        self._lock       = asyncio.Lock()

    # ── Caption ──────────────────────────────────────────────────────────
    def _build_caption(self) -> str:
        s = f"{self.season_num:02d}" if self.season_num else "01"
        e = f"{self.episode_num:02d}" if self.episode_num else "??"
        lang = self.language_str or "Hindi"
        return f"<b>➲ Season {s} Episode {e} {lang}</b>"

    # ── Keyboard ─────────────────────────────────────────────────────────
    def _build_keyboard(self) -> InlineKeyboardMarkup | None:
        """
        Ready qualities → url button (➲ 360p)
        Pending qualities → callback popup button (⏳ 720p uploading...)
        Pending = upload nahi hua abhi tak (not in self._buttons)
        """
        _Q_EMOJI = {"360p": "🟢", "480p": "🔵", "720p": "🟡", "1080p": "🔴"}
        row = []
        for q in self.QUALITY_ORDER:
            if q not in self.UPLOAD_QUALITIES:
                continue  # 480p skip

            if q in self._buttons:
                # Ready button
                em = _Q_EMOJI.get(q, "🔵")
                row.append(InlineKeyboardButton(
                    text=f"{em} {q}",
                    url=self._buttons[q],
                ))
            else:
                if self.post_msg_id is not None or self._buttons:
                    row.append(InlineKeyboardButton(
                        text=f"⏳ {q} uploading...",
                        callback_data=f"bm_pending_{q}",
                    ))

        return InlineKeyboardMarkup([row]) if row else None

    # ── Add quality ───────────────────────────────────────────────────────
    async def add_quality(self, quality: str, deep_link_url: str, season: int | None = None, language: str | None = None):
        """Quality ka button add/update karo — pehli baar post banao, baad mein edit."""
        async with self._lock:
            if season is not None:
                self.season_num = season
            if language is not None:
                self.language_str = language

            self._buttons[quality] = deep_link_url
            keyboard = self._build_keyboard()
            caption  = self._build_caption()

            if self.post_msg_id is None:
                # Pehli quality — nayi post banao
                # Pending buttons bhi add karo (baaki jo abhi nahi aayi)
                # _build_keyboard already handles it via post_msg_id check
                # But first post mein post_msg_id=None, so pending won't show yet
                # → manually build with pending for first post
                first_keyboard = self._build_first_keyboard()
                try:
                    sent = await self.client.send_message(
                        chat_id=self.channel_id,
                        text=caption,
                        reply_markup=first_keyboard,
                        parse_mode=ParseMode.HTML,
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
                # Existing post edit karo
                try:
                    await self.client.edit_message_text(
                        chat_id=self.channel_id,
                        message_id=self.post_msg_id,
                        text=caption,
                        reply_markup=keyboard,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True,
                    )
                    LOGGER.info(
                        f"[BotMode] Post edited — {self.anime_name} Ep {self.episode_num} "
                        f"msg_id={self.post_msg_id} added={quality}"
                    )
                except Exception as e:
                    LOGGER.error(f"[BotMode] Post edit error: {e}")

    def _build_first_keyboard(self) -> InlineKeyboardMarkup | None:
        """
        Pehli quality ke baad keyboard — ready + saari pending (higher) qualities.
        """
        _Q_EMOJI = {"360p": "🟢", "480p": "🔵", "720p": "🟡", "1080p": "🔴"}
        row = []
        for q in self.QUALITY_ORDER:
            if q not in self.UPLOAD_QUALITIES:
                continue
            if q in self._buttons:
                em = _Q_EMOJI.get(q, "🔵")
                row.append(InlineKeyboardButton(
                    text=f"{em} {q}",
                    url=self._buttons[q],
                ))
            else:
                row.append(InlineKeyboardButton(
                    text=f"⏳ {q} uploading...",
                    callback_data=f"bm_pending_{q}",
                ))
        return InlineKeyboardMarkup([row]) if row else None


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
#  NEW Format Detection — Title/Genre/Audio/Dub template
#  ─────────────────────────────────────────────
#  RTI ne recently ek naya post template shuru kiya hai jahan
#  plain URL text mein nahi hota — balki pehle row ke inline
#  button (Download & Watch) mein hota hai. Structure kuch aisi:
#
#     🔥 Episode 3 Added!
#     ━━━━━━━━━━━━━━━━━━━━━━
#     🎬 Title: Mob Psycho 100 – Season 3
#     🗣 Genre: Action, Comedy, ...
#     🔊 Audio: Hindi | Tamil | Telugu
#     🎙 Dub By: Muse India
#     ━━━━━━━━━━━━━━━━━━━━━━
#
#  Ya tree-style:
#     ├ 📌 Episode 19-21 Added!
#     ├ 🎬 Title: Captain Tsubasa – Season 2
#     ├ 🗣 Genre: Sports, Drama, School, Shounen
#     ├ 🔊 Audio: Hindi
#     ├ 🎙 Dub By: Anime Times
#
#  Purana format (plain "Episode X-Y Added! <url>") bilkul
#  waisa hi chalta rahega — yeh sirf ek NAYA extra detection
#  path hai, purane wale ko kuch nahi hua.
#
#  Sirf HINDI audio wale posts process karne hain — agar
#  "Audio:" line mein Hindi nahi hai (sirf Tamil/Telugu/etc)
#  toh us post ko skip kar do.
# ─────────────────────────────────────────────
#  FIX (Sept 3): Pehle yeh regex episode number ke turant baad literal
#  "added" maangta tha — isliye "Episode 9 Added!" wale posts detect ho
#  jaate the, lekin "Episode 9 Hindi DUB Fixed + ZIP Repacked!" jaise
#  posts (jahan "added" word hi nahi hota, ya kahin aur hota hai) miss ho
#  jaate the. Ab sirf "Episode <num>[-<num>]" match karta hai, chaahe
#  uske baad "Added!" ho, "Fixed + ZIP Repacked!" ho, ya kuch bhi.
_NEW_FMT_EP_RE    = re.compile(r'episode\s*(\d+)(?:\s*[-\u2013]\s*(\d+))?', re.IGNORECASE)
_NEW_FMT_TITLE_RE = re.compile(r'title\s*:\s*(.+)', re.IGNORECASE)
_NEW_FMT_AUDIO_RE = re.compile(r'audio\s*:\s*(.+)', re.IGNORECASE)
#  Dub vs Sub detection — RTI ke posts mein "🎙 Dub By: ..." line hoti
#  hai dubbed releases ke liye, aur "Sub By: ..." (ya similar) subbed
#  releases ke liye. Hindi SUB/subbed posts skip karne hain, sirf Hindi
#  DUB wale process karne hain.
_NEW_FMT_DUBBY_RE = re.compile(r'\bdub\s*by\s*:', re.IGNORECASE)
_NEW_FMT_SUBBY_RE = re.compile(r'\bsub\s*by\s*:', re.IGNORECASE)


def _extract_new_format_url(message: Message) -> str | None:
    """Post ke inline keyboard se pehla valid URL button nikalo
    (usually pehli row ka 'Download & Watch' button)."""
    markup = message.reply_markup
    if not markup or not getattr(markup, 'inline_keyboard', None):
        return None
    for row in markup.inline_keyboard:
        for btn in row:
            btn_url = getattr(btn, 'url', None)
            if btn_url and btn_url.startswith('http'):
                return btn_url
    return None


def _extract_new_format_info(message: Message, text: str) -> tuple[str, tuple[int, int], str] | None:
    """
    Naya Title/Genre/Audio/Dub template detect + parse karo.
    Returns (url, (start_ep, end_ep), match_text) ya None agar
    format match nahi hua / Hindi audio nahi hai / button nahi mila.
    """
    title_m = _NEW_FMT_TITLE_RE.search(text)
    audio_m = _NEW_FMT_AUDIO_RE.search(text)
    ep_m    = _NEW_FMT_EP_RE.search(text)

    if not (title_m and audio_m and ep_m):
        return None

    # ── Sirf Hindi audio wale posts ──
    audio_line = audio_m.group(1)
    if 'hindi' not in audio_line.lower():
        LOGGER.info(f"[AutoMonitor] NEW-format post skip (no Hindi audio): {audio_line.strip()[:60]}")
        return None

    # ── Sirf Hindi DUB wale posts — Sub/Subbed skip karo ──
    # "Sub By:" line mile aur "Dub By:" na mile → yeh ek subbed release
    # hai, isse process nahi karna. Agar "Dub By:" line mil jaaye (jyada
    # reliable signal) toh dub hi maano, chahe kahin "sub" word bhi ho
    # (e.g. "subscribe", "ZIP Repacked" mein nahi hota but future-proofing).
    if _NEW_FMT_SUBBY_RE.search(text) and not _NEW_FMT_DUBBY_RE.search(text):
        LOGGER.info(f"[AutoMonitor] NEW-format post skip (Sub, not Dub): {text[:80]}")
        return None

    # ── URL button se nikalo (text mein URL nahi hota is format mein) ──
    url = _extract_new_format_url(message)
    if not url:
        LOGGER.info("[AutoMonitor] NEW-format post skip (no URL button found)")
        return None

    start_ep = int(ep_m.group(1))
    end_ep   = int(ep_m.group(2)) if ep_m.group(2) else start_ep

    # Matching ke liye sirf Title line use karo — Genre/Dub By text
    # mein galti se koi dusra registered anime name match na ho jaaye
    match_text = title_m.group(1).strip()

    return url, (start_ep, end_ep), match_text


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

    Do formats support karta hai:
      1. OLD — "Episode X-Y Added! <url>" (url plain text mein)
      2. NEW — Title/Genre/Audio/Dub template (url inline button mein,
         sirf Hindi audio wale posts process hote hain)
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

    # ── Format 1: OLD — plain "Episode X-Y Added! <url>" ──
    url = _extract_url(text)
    ep_info = _extract_episodes(text) if url else None
    match_text = text
    fmt_label = "OLD"

    if url and ep_info:
        skip_reason = _old_format_should_skip(text, url)
        if skip_reason:
            LOGGER.info(f"[AutoMonitor] OLD-format post skip ({skip_reason})")
            url, ep_info = None, None

    if not (url and ep_info):
        # ── Format 2: NEW — Title/Genre/Audio/Dub template ──
        new_fmt = _extract_new_format_info(message, text)
        if not new_fmt:
            return
        url, ep_info, match_text = new_fmt
        fmt_label = "NEW"

    start_ep, end_ep = ep_info
    anime_list = await _get_anime_list()
    if not anime_list:
        return

    matched = _find_matching_anime(match_text + " " + url, anime_list)
    if not matched:
        LOGGER.info(f"[AutoMonitor] No anime match ({fmt_label} format) for: {match_text[:80]}")
        return

    anime_name = matched['anime_name']
    channel_id = matched['channel_id']
    oid = await _owner_id()

    LOGGER.info(f"[AutoMonitor] ✅ Match ({fmt_label}): {anime_name} | Ep {start_ep}–{end_ep}")

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
#  /Rtic — manual RTI URL se auto-upload
#  ─────────────────────────────────────────────
#  /rti (rti_downloader.py) ki tarah hi URL leta hai, lekin bot ki apni DM
#  mein bhejne ke bajaye — /add_anime list se match hone wale channel pe
#  seedha upload kar deta hai, aur pehle episode ke 360p ke saath update
#  channel pe bhi post daal deta hai. Yeh essentially auto_monitor_handler()
#  ka wahi engine (_get_swift_url_for_episode + _episode_quality_poller) hai,
#  bas monitor-channel-post ki jagah user manually URL deta hai.
#
#  Usage (waisa hi jaisa /rti):
#    /Rtic <url>                -> Latest episode auto-detect
#    /Rtic <url> <start> <end>  -> Episode range
#    /Rtic <url> 5 5            -> Sirf episode 5
#    /Rtic <url> 0 0            -> Movie mode
#
#  Koi bhi status message mein swift_url ya RTI page url nahi dikhta —
#  sirf anime/channel match, episode number, aur quality/upload status.
# ─────────────────────────────────────────────
@Client.on_message(filters.command(["Rtic", "rtic"]))
async def cmd_rtic(client: Client, message: Message):
    c = await check_chat(message, chat="Sudo")
    if not c:
        return

    get_latest_episode = _get_rti_latest_fn()

    parts = message.text.split()
    if len(parts) < 2:
        await message.reply(
            "**Usage:**\n"
            "`/Rtic <url>` — Latest episode, matching channel pe auto-upload\n"
            "`/Rtic <url> <start> <end>` — Episode range\n"
            "`/Rtic <url> 5 5` — Sirf episode 5\n"
            "`/Rtic <url> 0 0` — Movie mode\n\n"
            "⚠️ Anime pehle `/add_anime` se add hona chahiye (channel match "
            "usi list se hota hai)."
        )
        return

    page_url = parts[1].strip()
    if not page_url.startswith("http"):
        await message.reply("❌ Valid URL dalo.")
        return

    anime_list = await _get_anime_list()
    if not anime_list:
        await message.reply("❌ Koi anime `/add_anime` se add nahi hai — pehle add karo.")
        return

    # ── Episode range decide karo (waisa hi jaisa /rti) ──
    if len(parts) == 2:
        prep = await message.reply("🔍 Latest episode detect ho raha hai...")
        loop = asyncio.get_event_loop()
        latest_ep, page_title = await loop.run_in_executor(None, get_latest_episode, page_url)
        if not latest_ep:
            await prep.edit("❌ Page se koi episode nahi mila. URL check karo.")
            return
        start_ep = end_ep = latest_ep
    else:
        if len(parts) < 4:
            await message.reply("❌ Range ke liye do numbers chahiye.\nExample: `/Rtic <url> 1 10`")
            return
        try:
            start_ep = int(parts[2])
            end_ep = int(parts[3])
        except ValueError:
            await message.reply("❌ Episode number valid nahi.\nExample: `/Rtic <url> 1 10`")
            return
        if start_ep > end_ep:
            await message.reply("❌ Start > End nahi ho sakta.")
            return
        if end_ep - start_ep > 50:
            await message.reply("❌ Max 50 episodes ek baar mein.")
            return
        prep = await message.reply(f"🔍 `{page_url.split('//')[-1].split('/')[0]}` se anime naam nikal raha hoon...")
        loop = asyncio.get_event_loop()
        _, page_title = await loop.run_in_executor(None, get_latest_episode, page_url)

    page_title = page_title or ""

    # ── Anime list se match dhundo — page title + URL dono se try karo ──
    matched = _find_matching_anime(page_title + " " + page_url, anime_list)
    if not matched:
        await prep.edit(
            f"❌ **Koi matching anime nahi mila!**\n\n"
            f"📺 Page se mila naam: `{page_title or 'pata nahi chala'}`\n\n"
            f"Pehle `/add_anime` se is anime ko (page ke naam se milta-julta) "
            f"add karo, phir dobara try karo."
        )
        return

    anime_name = matched['anime_name']
    channel_id = matched['channel_id']
    oid = await _owner_id()

    total = end_ep - start_ep + 1
    ep_label_range = "Movie" if (start_ep == 0 and end_ep == 0) else f"Ep {start_ep}-{end_ep}"
    await prep.edit(
        f"✅ **Matched:** `{anime_name}`\n"
        f"🎯 {ep_label_range} — `{total}` episode(s)\n"
        f"⏳ Shuru ho raha hai..."
    )

    update_post_sent = [False]
    any_ep_uploaded = False

    for i, ep_num in enumerate(range(start_ep, end_ep + 1), 1):
        is_last = (i == total)
        ep_lbl = "Movie" if ep_num == 0 else f"Ep {ep_num}"

        find_msg = await message.reply(
            f"🎌 **Rtic** | `{anime_name}` | {ep_lbl}\n\n"
            f"🔍 Swift URL nikaal raha hoon..."
        )

        SWIFT_FAST_ATTEMPTS = 10
        SWIFT_SLOW_ATTEMPTS = 20
        SWIFT_MAX_ATTEMPTS = SWIFT_FAST_ATTEMPTS + SWIFT_SLOW_ATTEMPTS

        swift_url = None
        for swift_attempt in range(1, SWIFT_MAX_ATTEMPTS + 1):
            swift_url = await _get_swift_url_for_episode(page_url, ep_num, find_msg)
            if swift_url:
                break

            if swift_attempt == SWIFT_MAX_ATTEMPTS:
                await find_msg.edit(
                    f"❌ **Rtic** | `{anime_name}` | {ep_lbl}\n\n"
                    f"⏱️ {SWIFT_MAX_ATTEMPTS} attempts (~25 min) ke baad bhi\n"
                    f"link nahi mila. RTI pe manually check karo."
                )
                break

            is_fast = swift_attempt <= SWIFT_FAST_ATTEMPTS
            interval = 30 if is_fast else 60
            phase_lbl = "⚡ Fast" if is_fast else "🐢 Slow"
            remaining_attempts = SWIFT_MAX_ATTEMPTS - swift_attempt
            try:
                await find_msg.edit(
                    f"⏳ **Rtic** | `{anime_name}` | {ep_lbl}\n\n"
                    f"🔄 Attempt `{swift_attempt}/{SWIFT_MAX_ATTEMPTS}` {phase_lbl} — link nahi mila\n"
                    f"⏰ `{interval}s` baad retry... ({remaining_attempts} attempts left)"
                )
            except Exception:
                pass
            await asyncio.sleep(interval)

        if not swift_url:
            continue

        try:
            await find_msg.delete()
        except Exception:
            pass

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

        if not is_last:
            await asyncio.sleep(3)

    if any_ep_uploaded:
        try:
            send_schedule_notification = _get_schedule_fn()
            await send_schedule_notification(client, channel_id, anime_name, end_ep)
        except Exception as e:
            LOGGER.error(f"[Rtic] Schedule notification error: {e}")


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
#  /add_anime — Interactive button-flow (v3)
#
#  Step 1/4 (channel)  : "📢 Set Channel" button dabao → channel ID bhejo
#                          YA us channel ka koi bhi message forward karo.
#                          Bot us channel mein *admin* hona chahiye.
#  Step 2/4 (name)      : 🤖 Auto Add  — channel ke naam se TMDB pe anime
#                                        dhoondta hai, match milte hi wahi
#                                        naam save ho jaata hai; na mile
#                                        toh error + Manual Add ka option.
#                          ✍️ Manual Add — khud poora sahi naam type karo.
#                          (Dono case mein genres turant TMDB se auto-fill
#                          ho jaate hain — koi extra step nahi lagta.)
#  Step 3/4 (poster)    : 🤖 Auto Add  — anime-name wale TMDB match se mila
#                                        16:9 ("YouTube size") banner lagta hai.
#                          🖼 Custom Add — khud ek photo bhejo.
#  Step 4/4 (audio/dub) : 🎙 ORG ya 🎙 FanDub choose karo → is step ke baad
#                          bas schedule-interval + channel-link (quick text)
#                          maang ke sab kuch save ho jaata hai.
#
#  Finalize par teeno system ek saath save ho jaate hain:
#    - anime_monitor_list   (RTI auto-monitor)
#    - update_post_map      (update-channel post ke liye)
#    - episode_schedule_list (agla episode kab expect karna hai)
# ─────────────────────────────────────────────

# { user_id: {
#     'step': 'await_start'|'channel'|'name_choice'|'name_manual'|
#             'image_choice'|'image_custom'|'dub_choice'|
#             'interval_choice'|'interval_custom'|
#             'link_choice'|'link_manual',
#     'channel_id', 'channel_title',
#     'anime_name', 'audio', 'genres', 'image', 'season', 'total_eps',
#     'season_breakdown', 'interval_days', 'channel_link',
# } }
_add_anime_sessions: dict = {}


async def _register_setpic_from_url(user_id: int, keyword: str, image_url: str) -> bool:
    """
    Update-post ke liye jo image (URL) use ho rahi hai, wahi image ko
    Telegram pe upload karke uska file_id nikalo aur `/setpic <keyword>`
    ki tarah save kar do — taaki us anime ki uploaded files ko bhi
    automatically wahi thumbnail lag jaaye.

    Photo ko LOG_CHANNEL pe silently bhejte hain sirf file_id lene ke liye.
    """
    if not image_url:
        return False
    try:
        sent = await app.send_photo(log, photo=image_url)
        file_id = sent.photo.file_id
        await db.set_custompic(user_id, keyword, file_id)
        return True
    except Exception as e:
        LOGGER.warning(f"[AddAnime] setpic auto-register failed for '{keyword}': {e}")
        return False


def _apply_fetched_details(session: dict, fetched: dict | None):
    """TMDB se fetch hue details session mein bhar do (ya empty defaults)."""
    if fetched:
        session["audio"] = fetched.get("audio", "Hindi ORG")
        session["genres"] = fetched.get("genres", "")
        session["image"] = fetched.get("image", "")
        session["season"] = fetched.get("season")
        session["total_eps"] = fetched.get("total_eps", 0)
        session["season_breakdown"] = fetched.get("season_breakdown", "")
    else:
        session["audio"] = "Hindi ORG"
        session["genres"] = ""
        session["image"] = ""
        session["season"] = None
        session["total_eps"] = 0
        session["season_breakdown"] = ""


def _fetch_summary_text(fetched: dict | None) -> str:
    if not fetched:
        return (
            "⚠️ TMDB pe is naam se koi match nahi mila.\n\n"
            "Genres/poster baad mein `/update_post_list` se manually bhar sakte ho."
        )
    status_str = fetched.get("status") or "—"
    source = fetched.get("source", "TMDB")
    season_breakdown = fetched.get("season_breakdown", "")
    season_line = f"📚 Season-wise: {season_breakdown}\n" if season_breakdown else ""
    eps_label = (
        f"{fetched.get('total_eps', 0) or '—'} (Season {fetched.get('season')})"
        if fetched.get("season") else f"{fetched.get('total_eps', 0) or '—'}"
    )
    return (
        f"✅ **Details mil gaye!**\n\n"
        f"📺 {source} Match: **{fetched.get('matched_name')}**\n"
        f"📡 Status: {status_str}\n"
        f"🎬 Total Episodes: {eps_label}\n"
        f"{season_line}"
        f"🎭 Genres: {fetched.get('genres') or '—'}\n"
        f"🖼 Poster: {'✅ (16:9 banner)' if fetched.get('image') else '❌ nahi mila'}"
    )


async def _show_image_choice(event, session: dict, user_id: int):
    has_image = bool(session.get("image"))
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "🤖 Auto Add" if has_image else "🤖 Auto Add (nahi mila)",
            callback_data=f"aa_img_auto_{user_id}",
        )],
        [InlineKeyboardButton("🖼 Custom Add", callback_data=f"aa_img_custom_{user_id}")],
        [InlineKeyboardButton("❌ Cancel", callback_data=f"aa_cancel_{user_id}")],
    ])
    note = (
        "🤖 **Auto Add** — TMDB se mila 16:9 poster/banner use hoga\n"
        if has_image else
        "🤖 **Auto Add** — ⚠️ TMDB pe clean poster nahi mila, Custom Add use karo\n"
    )
    await event.reply(
        f"**Step 3/4 — Thumbnail/Poster set karo:**\n\n"
        f"{note}"
        f"🖼 **Custom Add** — khud ek photo bhejo\n\n"
        f"_Cancel karna ho toh `/cancel_add_anime` bhejo._",
        reply_markup=kb,
    )


async def _show_dub_choice(event, user_id: int):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎙 ORG", callback_data=f"aa_dub_org_{user_id}")],
        [InlineKeyboardButton("🎙 FanDub", callback_data=f"aa_dub_fandub_{user_id}")],
        [InlineKeyboardButton("❌ Cancel", callback_data=f"aa_cancel_{user_id}")],
    ])
    await event.reply(
        "**Step 4/4 — Audio/Dub type select karo:**\n\n"
        "🎙 **ORG** — Official Hindi dub\n"
        "🎙 **FanDub** — Fan-made Hindi dub\n\n"
        "_Select karte hi baaki quick setup khud maang liya jaayega._",
        reply_markup=kb,
    )


async def _show_interval_choice(event, user_id: int):
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📅 7 Din", callback_data=f"aa_int_7_{user_id}"),
            InlineKeyboardButton("⚡ 1 Din", callback_data=f"aa_int_1_{user_id}"),
        ],
        [InlineKeyboardButton("✏️ Custom", callback_data=f"aa_int_custom_{user_id}")],
        [InlineKeyboardButton("❓ Unknown", callback_data=f"aa_int_unknown_{user_id}")],
        [InlineKeyboardButton("❌ Cancel", callback_data=f"aa_cancel_{user_id}")],
    ])
    await event.reply(
        "**🗓️ Almost Done — Bas Ek Aakhri Cheez! 🚀**\n\n"
        "Next episode kitne din baad aata hai? _(schedule reminder ke liye)_\n\n"
        "📅 **7 Din** — weekly release\n"
        "⚡ **1 Din** — daily release\n"
        "✏️ **Custom** — khud number set karo\n"
        "❓ **Unknown** — pata nahi; episode post hone ke baad channel pe "
        "*\"More episodes comming soon...\"* dikhega\n\n"
        "_Cancel karna ho toh `/cancel_add_anime` bhejo._",
        reply_markup=kb,
    )


async def _show_link_choice(event, user_id: int):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Set Link", callback_data=f"aa_link_set_{user_id}")],
        [InlineKeyboardButton("⏭️ Skip", callback_data=f"aa_link_skip_{user_id}")],
        [InlineKeyboardButton("❌ Cancel", callback_data=f"aa_cancel_{user_id}")],
    ])
    await event.reply(
        "**🔗 Channel Invite Link**\n\n"
        "Ye link *\"Watch & Download\"* button ke liye use hoga.\n\n"
        "🔗 **Set Link** — invite link bhejo\n"
        "⏭️ **Skip** — link ke bina aage badho\n\n"
        "_Cancel karna ho toh `/cancel_add_anime` bhejo._",
        reply_markup=kb,
    )


@Client.on_message(filters.command("add_anime") & filters.private)
async def cmd_add_anime(client: Client, message: Message):
    """/add_anime — button-flow shuru karo (Step 1/4: channel)."""
    if not _is_authorized(message.from_user.id):
        return

    user_id = message.from_user.id
    _add_anime_sessions[user_id] = {"step": "await_start"}

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Set Channel", callback_data=f"aa_setchannel_{user_id}")],
    ])
    await message.reply(
        "**➕ Add New Anime**\n\n"
        "4 aasaan steps mein set ho jaayega — channel, naam, poster aur "
        "audio/dub. Sab kuch button se. 🚀\n\n"
        "⚠️ **Note:** Jo channel add karna hai, usme bot ka **admin** hona zaruri hai.\n\n"
        "_Cancel karna ho toh `/cancel_add_anime` bhejo._",
        reply_markup=kb,
    )


@Client.on_message(filters.command("cancel_add_anime") & filters.private)
async def cmd_cancel_add_anime(client: Client, message: Message):
    if not _is_authorized(message.from_user.id):
        return
    user_id = message.from_user.id
    if _add_anime_sessions.pop(user_id, None):
        await message.reply("❌ Cancelled.")
    else:
        await message.reply("Koi active `/add_anime` session nahi hai.")


# ── Step handlers (text-driven steps) ──────────────────────────

async def _add_anime_step_channel(client: Client, message: Message, session: dict, user_id: int):
    channel_id = None

    if message.forward_from_chat:
        channel_id = message.forward_from_chat.id
    else:
        text = (message.text or "").strip()
        try:
            channel_id = int(text)
        except ValueError:
            await message.reply(
                "⚠️ Channel ID samajh nahi aayi.\n\n"
                "Channel ki ID do (`-100xxxxxxxxx`) ya us channel ka koi bhi "
                "message yahan forward karo."
            )
            return

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

    session["channel_id"] = channel_id
    session["channel_title"] = channel_title
    session["step"] = "name_choice"
    _add_anime_sessions[user_id] = session

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🤖 Auto Add", callback_data=f"aa_name_auto_{user_id}")],
        [InlineKeyboardButton("✍️ Manual Add", callback_data=f"aa_name_manual_{user_id}")],
        [InlineKeyboardButton("❌ Cancel", callback_data=f"aa_cancel_{user_id}")],
    ])
    await message.reply(
        f"✅ Channel: **{channel_title}**\n\n"
        f"**Step 2/4 — Anime ka naam kaise set karna hai?**\n\n"
        f"🤖 **Auto Add** — channel ke naam (\"{channel_title}\") se TMDB pe "
        f"anime dhoondega, match milte hi wahi naam save ho jaayega\n"
        f"✍️ **Manual Add** — khud se poora sahi naam type karo\n\n"
        f"_Cancel karna ho toh `/cancel_add_anime` bhejo._",
        reply_markup=kb,
    )


async def _add_anime_step_name_manual(client: Client, message: Message, session: dict, user_id: int):
    anime_name = (message.text or "").strip()
    if not anime_name:
        await message.reply("⚠️ Anime ka naam do.")
        return

    session["anime_name"] = anime_name

    status_msg = await message.reply("🔎 TMDB se anime ki details dhoondh raha hoon...")
    fetched = None
    try:
        fetched = await fetch_anime_details(anime_name)
    except Exception as e:
        LOGGER.warning(f"[AddAnime] fetch_anime_details error: {e}")

    _apply_fetched_details(session, fetched)

    try:
        await status_msg.edit(_fetch_summary_text(fetched))
    except Exception:
        pass

    session["step"] = "image_choice"
    _add_anime_sessions[user_id] = session
    await _show_image_choice(message, session, user_id)


async def _add_anime_step_interval(client: Client, message: Message, session: dict, user_id: int):
    """Sirf 'interval_custom' step ke liye — user ne ✏️ Custom choose kiya hai."""
    text = (message.text or "").strip()
    try:
        interval_days = int(text)
        if interval_days <= 0:
            raise ValueError
    except ValueError:
        await message.reply("⚠️ Sirf ek positive number do, jaise `10`.")
        return

    session["interval_days"] = interval_days
    session["step"] = "link_choice"
    _add_anime_sessions[user_id] = session

    await message.reply(f"✅ **Interval:** {interval_days} din")
    await _show_link_choice(message, user_id)


async def _add_anime_step_link(client: Client, message: Message, session: dict, user_id: int):
    """Sirf 'link_manual' step ke liye — user ne 🔗 Set Link choose kiya hai."""
    text = (message.text or "").strip()
    if not text.startswith("http"):
        await message.reply("⚠️ Valid link do, jaise `https://t.me/+xxxxxxxxxx`.")
        return

    session["channel_link"] = text
    _add_anime_sessions.pop(user_id, None)
    await _finalize_add_anime(client, message, session)


async def _finalize_add_anime(client: Client, message: Message, session: dict):
    """Session complete — anime_monitor_list + update_post_map +
    episode_schedule_list, teeno ek saath save karo."""
    channel_id = session["channel_id"]
    channel_title = session["channel_title"]
    anime_name = session["anime_name"]
    channel_link = session.get("channel_link", "")
    interval_days = session.get("interval_days", 0)
    audio = session.get("audio", "")
    genres = session.get("genres", "")
    image = session.get("image", "")
    season = session.get("season")
    total_eps = session.get("total_eps", 0)

    # ── 1) RTI auto-monitor list ──
    anime_list = await _get_anime_list()
    for entry in anime_list:
        if (entry.get('channel_id') == channel_id and
                entry.get('anime_name', '').lower() == anime_name.lower()):
            await message.reply(f"⚠️ Ye already monitor list mein hai!\n\n📺 **{anime_name}** → `{channel_title}`")
            return
    anime_list.append({
        'channel_id':    channel_id,
        'channel_title': channel_title,
        'anime_name':    anime_name,
        'hashtag':       '',
        'channel_link':  channel_link,
    })
    await _save_anime_list(anime_list)

    # ── 2) update_post_map entry (jo pehle /update_post 5-step karta tha) ──
    _get_post_map, _save_post_map = _get_update_post_fns()
    post_map = await _get_post_map()
    post_map[anime_name.lower().strip()] = {
        "display_name": anime_name,
        "invite_link":  channel_link,
        "audio":        audio,
        "genres":       genres,
        "image":        image,
    }
    await _save_post_map(post_map)

    # ── 2b) Wahi image `/setpic <anime_name>` ki tarah bhi save karo —
    #        taaki is anime ki uploaded episode-files pe auto-thumbnail lage ──
    setpic_saved = False
    if image:
        setpic_saved = await _register_setpic_from_url(message.from_user.id, anime_name, image)

    # ── 3) episode schedule (jo pehle /schedule karta tha) ──
    _get_schedule_list, _save_schedule_list = _get_schedule_list_fns()
    slist = await _get_schedule_list()
    for entry in slist:
        if _normalize(entry.get('anime_name', '')) == _normalize(anime_name):
            entry['interval_days'] = interval_days
            entry['total_eps'] = total_eps
            break
    else:
        slist.append({
            'anime_name':     anime_name,
            'interval_days':  interval_days,
            'total_eps':      total_eps,
        })
    await _save_schedule_list(slist)

    image_line = (
        "🖼 **Poster:** ✅ set (16:9 banner)\n" if image
        else "🖼 **Poster:** ⚠️ nahi mila — `/update_post_list` se add karo\n"
    )
    season_breakdown = session.get("season_breakdown", "")
    if season:
        eps_str = f"{total_eps} (Season {season})" if total_eps else "— (baad mein pata chalega)"
    else:
        eps_str = f"{total_eps} episodes" if total_eps else "— (baad mein pata chalega)"
    season_line = f"📚 **Season-wise:** {season_breakdown}\n" if season_breakdown else ""
    setpic_line = (
        "📌 **Auto-Thumbnail:** ✅ `/setpic` mein bhi save ho gaya\n" if setpic_saved
        else ("📌 **Auto-Thumbnail:** ⚠️ save nahi ho paaya — `/setpic " + anime_name + "` manually karo\n" if image else "")
    )
    interval_str = "❓ Unknown" if interval_days == "unknown" else f"{interval_days} din"
    await message.reply(
        f"✅ **Anime Fully Added!** 🎉\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📺 **Anime:** {anime_name}\n"
        f"📢 **Channel:** {channel_title}\n"
        f"🎬 **Total Episodes:** {eps_str}\n"
        f"{season_line}"
        f"🎙 **Audio:** {audio or 'Hindi ORG'}\n"
        f"🎭 **Genres:** {genres or '—'}\n"
        f"{image_line}"
        f"{setpic_line}"
        f"📅 **Next Episode In:** {interval_str}\n"
        f"🔗 **Link:** {channel_link or '—'}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Monitor + Update Post + Schedule — teeno set ho gaye! Ab uploads automatic honge 🚀"
    )


# ── Button callbacks ──────────────────────────────────────────
@Client.on_callback_query(filters.regex(r"^aa_"))
async def add_anime_callbacks(client: Client, cb: CallbackQuery):
    """/add_anime button-flow ke saare callbacks."""
    data = cb.data
    user_id = cb.from_user.id

    if not _is_authorized(user_id):
        await cb.answer("❌ Authorized nahi ho!", show_alert=True)
        return

    parts = data.split("_")
    try:
        owner_id = int(parts[-1])
    except (ValueError, IndexError):
        await cb.answer()
        return

    if user_id != owner_id:
        await cb.answer("❌ Ye tumhara nahi hai!", show_alert=True)
        return

    # ── aa_cancel_<uid> ──
    if data.startswith("aa_cancel_"):
        _add_anime_sessions.pop(owner_id, None)
        await cb.answer("❌ Cancelled")
        try:
            await cb.message.edit("❌ **Cancelled.**")
        except Exception:
            pass
        return

    session = _add_anime_sessions.get(owner_id)
    if session is None:
        await cb.answer("⚠️ Session expire ho gaya, /add_anime dobara chalao.", show_alert=True)
        return

    # ── aa_setchannel_<uid> ──
    if data.startswith("aa_setchannel_"):
        session["step"] = "channel"
        _add_anime_sessions[owner_id] = session
        await cb.answer()
        try:
            await cb.message.edit(
                "**Step 1/4 — Channel batao:**\n\n"
                "Channel ki ID bhejo (`-100xxxxxxxxx`) *ya* us channel ka koi bhi "
                "message yahan forward kar do.\n\n"
                "⚠️ _Bot us channel mein admin hona chahiye._\n\n"
                "_Cancel karna ho toh `/cancel_add_anime` bhejo._"
            )
        except Exception:
            pass
        return

    # ── aa_name_auto_<uid> / aa_name_manual_<uid> ──
    if data.startswith("aa_name_"):
        if session.get("step") != "name_choice":
            await cb.answer("⚠️ Ye step ab active nahi hai.", show_alert=True)
            return
        mode = parts[2]

        if mode == "manual":
            session["step"] = "name_manual"
            _add_anime_sessions[owner_id] = session
            await cb.answer()
            try:
                await cb.message.edit(
                    "**Step 2/4 — Anime ka poora aur bilkul sahi naam do:**\n\n"
                    "_Isi naam se genres aur poster TMDB se auto-fetch honge._\n\n"
                    "**Example:** `Fullmetal Alchemist: Brotherhood`\n\n"
                    "_Cancel karna ho toh `/cancel_add_anime` bhejo._"
                )
            except Exception:
                pass
            return

        # mode == "auto" — channel ke naam se TMDB pe search karo
        channel_title = session.get("channel_title", "")
        await cb.answer()
        try:
            await cb.message.edit(f"🔎 Channel naam **\"{channel_title}\"** se TMDB pe anime dhoondh raha hoon...")
        except Exception:
            pass

        fetched = None
        try:
            fetched = await fetch_anime_details(channel_title)
        except Exception as e:
            LOGGER.warning(f"[AddAnime] auto-name fetch error: {e}")

        if not fetched:
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✍️ Manual Add", callback_data=f"aa_name_manual_{owner_id}")],
                [InlineKeyboardButton("❌ Cancel", callback_data=f"aa_cancel_{owner_id}")],
            ])
            try:
                await cb.message.edit(
                    f"❌ **\"{channel_title}\"** naam se TMDB pe koi anime match nahi mila.\n\n"
                    f"Manual Add try karo — anime ka poora sahi naam khud type karo.",
                    reply_markup=kb,
                )
            except Exception:
                pass
            return

        _apply_fetched_details(session, fetched)
        session["anime_name"] = fetched["matched_name"]
        session["step"] = "image_choice"
        _add_anime_sessions[owner_id] = session

        try:
            await cb.message.edit(_fetch_summary_text(fetched))
        except Exception:
            pass
        await _show_image_choice(cb.message, session, owner_id)
        return

    # ── aa_img_auto_<uid> / aa_img_custom_<uid> ──
    if data.startswith("aa_img_"):
        if session.get("step") != "image_choice":
            await cb.answer("⚠️ Ye step ab active nahi hai.", show_alert=True)
            return
        mode = parts[2]

        if mode == "auto":
            if not session.get("image"):
                await cb.answer("⚠️ TMDB se koi image nahi mila — Custom Add try karo!", show_alert=True)
                return
            session["step"] = "dub_choice"
            _add_anime_sessions[owner_id] = session
            await cb.answer("✅ TMDB poster use hoga")
            try:
                await cb.message.edit("✅ **Poster:** TMDB se auto set ho gaya (16:9 banner).")
            except Exception:
                pass
            await _show_dub_choice(cb.message, owner_id)
            return

        # mode == "custom"
        session["step"] = "image_custom"
        _add_anime_sessions[owner_id] = session
        await cb.answer()
        try:
            await cb.message.edit(
                "**Step 3/4 — Poster/thumbnail image bhejo:**\n\n"
                "_Photo bhejo, caption ki zaroorat nahi._\n\n"
                "_Cancel karna ho toh `/cancel_add_anime` bhejo._"
            )
        except Exception:
            pass
        return

    # ── aa_dub_org_<uid> / aa_dub_fandub_<uid> ──
    if data.startswith("aa_dub_"):
        if session.get("step") != "dub_choice":
            await cb.answer("⚠️ Ye step ab active nahi hai.", show_alert=True)
            return
        mode = parts[2]
        session["audio"] = "Hindi ORG" if mode == "org" else "Hindi FanDub"
        session["step"] = "interval_choice"
        _add_anime_sessions[owner_id] = session
        await cb.answer(f"✅ {session['audio']}")
        try:
            await cb.message.edit(f"✅ **Audio:** {session['audio']}")
        except Exception:
            pass
        await _show_interval_choice(cb.message, owner_id)
        return

    # ── aa_int_7_<uid> / aa_int_1_<uid> / aa_int_custom_<uid> / aa_int_unknown_<uid> ──
    if data.startswith("aa_int_"):
        if session.get("step") != "interval_choice":
            await cb.answer("⚠️ Ye step ab active nahi hai.", show_alert=True)
            return
        mode = parts[2]

        if mode == "custom":
            session["step"] = "interval_custom"
            _add_anime_sessions[owner_id] = session
            await cb.answer()
            try:
                await cb.message.edit(
                    "**✏️ Custom Interval**\n\n"
                    "Kitne din baad next episode aata hai? Number bhejo (jaise `10`).\n\n"
                    "_Cancel karna ho toh `/cancel_add_anime` bhejo._"
                )
            except Exception:
                pass
            return

        if mode == "unknown":
            session["interval_days"] = "unknown"
            session["step"] = "link_choice"
            _add_anime_sessions[owner_id] = session
            await cb.answer("✅ Unknown")
            try:
                await cb.message.edit(
                    "✅ **Interval:** Unknown\n\n"
                    "_Episode post hone ke baad channel pe \"More episodes comming "
                    "soon...\" dikhega._"
                )
            except Exception:
                pass
            await _show_link_choice(cb.message, owner_id)
            return

        # mode == "7" ya "1"
        interval_days = int(mode)
        session["interval_days"] = interval_days
        session["step"] = "link_choice"
        _add_anime_sessions[owner_id] = session
        await cb.answer(f"✅ {interval_days} din")
        try:
            await cb.message.edit(f"✅ **Interval:** {interval_days} din")
        except Exception:
            pass
        await _show_link_choice(cb.message, owner_id)
        return

    # ── aa_link_set_<uid> / aa_link_skip_<uid> ──
    if data.startswith("aa_link_"):
        if session.get("step") != "link_choice":
            await cb.answer("⚠️ Ye step ab active nahi hai.", show_alert=True)
            return
        mode = parts[2]

        if mode == "skip":
            session["channel_link"] = ""
            _add_anime_sessions.pop(owner_id, None)
            await cb.answer("✅ Skipped")
            try:
                await cb.message.edit("✅ **Link:** Skipped")
            except Exception:
                pass
            await _finalize_add_anime(client, cb.message, session)
            return

        # mode == "set"
        session["step"] = "link_manual"
        _add_anime_sessions[owner_id] = session
        await cb.answer()
        try:
            await cb.message.edit(
                "**🔗 Channel ka invite link bhejo:**\n\n"
                "**Example:** `https://t.me/+xxxxxxxxxx`\n\n"
                "_Cancel karna ho toh `/cancel_add_anime` bhejo._"
            )
        except Exception:
            pass
        return

    await cb.answer()


# ── Photo handler — sirf "image_custom" step ke liye ───────────
@Client.on_message(filters.photo & filters.private, group=0)
async def add_anime_photo_input(client: Client, message: Message):
    if not message.from_user:
        raise ContinuePropagation

    user_id = message.from_user.id
    session = _add_anime_sessions.get(user_id)
    if not session or not _is_authorized(user_id) or session.get("step") != "image_custom":
        raise ContinuePropagation

    session["image"] = message.photo.file_id
    session["step"] = "dub_choice"
    _add_anime_sessions[user_id] = session

    await message.reply("✅ **Poster:** custom image saved!")
    await _show_dub_choice(message, user_id)
    raise StopPropagation


# ── Session router — sabse pehle chalta hai, apni session na ho toh
#    aage baaki handlers ke liye chhod deta hai ──
@Client.on_message(filters.private, group=0)
async def add_anime_flow_router(client: Client, message: Message):
    if not message.from_user:
        raise ContinuePropagation

    user_id = message.from_user.id
    session = _add_anime_sessions.get(user_id)
    if not session or not _is_authorized(user_id):
        raise ContinuePropagation

    text = (message.text or "").strip()

    if text.lower() in ("/cancel_add_anime", "cancel"):
        _add_anime_sessions.pop(user_id, None)
        await message.reply("❌ Cancelled.")
        raise StopPropagation

    # Channel-forward step ke alawa har jagah text chahiye hota hai —
    # agar user beech mein koi aur command chala de toh session drop karo.
    if text.startswith("/"):
        _add_anime_sessions.pop(user_id, None)
        raise ContinuePropagation

    step = session.get("step")

    if step == "channel":
        await _add_anime_step_channel(client, message, session, user_id)
        raise StopPropagation
    if step == "name_manual":
        await _add_anime_step_name_manual(client, message, session, user_id)
        raise StopPropagation
    if step == "interval_custom":
        await _add_anime_step_interval(client, message, session, user_id)
        raise StopPropagation
    if step == "link_manual":
        await _add_anime_step_link(client, message, session, user_id)
        raise StopPropagation
    if step == "image_custom":
        # Photos yahan nahi — dedicated add_anime_photo_input (upar) handle
        # karta hai. Sirf stray text ko yahan nudge karo.
        if message.text:
            await message.reply(
                "⚠️ Image bhejo (photo), text nahi.\n\n"
                "_Cancel karna ho toh `/cancel_add_anime` bhejo._"
            )
            raise StopPropagation
        raise ContinuePropagation
    if step in ("await_start", "name_choice", "image_choice", "dub_choice",
                "interval_choice", "link_choice"):
        if message.text:
            await message.reply("⬆️ Upar diye gaye buttons mein se ek option choose karo.")
        raise StopPropagation

    raise ContinuePropagation



# ─────────────────────────────────────────────
#  Paginated anime list — ab sirf /del_anime is se render hota hai.
#  (/list_anime niche button-panel style use karta hai, _show_list_anime_panel)
#
#  PEHLE: poori anime_list ek hi message mein bheji jaati thi. List badi ho
#  jaane par Telegram [400 MESSAGE_TOO_LONG] / [400 ENTITY_BOUNDS_INVALID]
#  de deta tha (4096 char limit + bold/code markdown entities corrupt ho
#  jaate the) — isliye command "kaam nahi kar raha tha".
#  AB: hamesha max ANIME_PAGE_SIZE entries ek page mein, Prev/Next buttons
#  se navigate karo.
# ─────────────────────────────────────────────
ANIME_PAGE_SIZE = 10


def _esc(value) -> str:
    """
    Saare dynamic fields (anime_name/channel_title/hashtag/link/monitor
    channel title) yahan se hokar jaana chahiye.

    PEHLE do escaping attempts Markdown (parse_mode default) ke saath kiye
    gaye the — pehle raw *, _, `, [ ko backslash-escape kiya, phir backtick
    ko alag se handle kiya. Dono baar crash wapas aaya (/list_anime AUR
    /del_anime dono mein), kyunki Pyrofork ka Markdown tokenizer bahut saari
    edge cases mein (nested delimiters, fixed-width spans, wgera) predictable
    tarike se escape hone hi nahi deta — chahe kitna bhi escape karo, koi na
    koi character combination bach jaata hai jo entity offsets todh deta hai.

    FIX: ab is poore anime-list/monitor section ke har message mein
    parse_mode=ParseMode.HTML use ho raha hai. HTML mode mein escaping rules
    fixed aur simple hain — sirf `<`, `>`, `&` ko escape karna hota hai
    (html.escape() yahi karta hai), baaki KOI bhi character (*, _, `, [, ~,
    |, kuch bhi) literal reh jaata hai, kabhi entity nahi bantа. Isliye ab
    yeh channel title/anime name mein chahe kuch bhi ho, crash structurally
    hi possible nahi hai.
    """
    if not value:
        return "—"
    return html.escape(str(value), quote=False)


def _render_anime_list_page(anime_list: list, page: int, mc_text: str, mode: str):
    """
    anime_list ka ek page (ANIME_PAGE_SIZE items) + pagination buttons
    banao. mode = "list" (/list_anime, full detail) ya "del" (/del_anime
    bina number ke, compact). Return (text, InlineKeyboardMarkup | None).
    Numbering hamesha FULL list ke absolute index pe based hai, taaki
    `/del_anime <number>` kisi bhi page se sahi kaam kare.

    NOTE: text ab HTML entities use karta hai (<b>, <code>) — is function ka
    output hamesha parse_mode=ParseMode.HTML ke saath hi bhejo/edit karo.
    """
    total = len(anime_list)
    total_pages = max(1, (total + ANIME_PAGE_SIZE - 1) // ANIME_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))

    start = page * ANIME_PAGE_SIZE
    chunk = anime_list[start:start + ANIME_PAGE_SIZE]

    header = "📋 <b>Registered Anime</b>" if mode == "list" else "🗑️ <b>Konsa remove karna hai?</b>"
    text = f"📡 <b>Monitor:</b> {mc_text}\n\n{header}\n━━━━━━━━━━━━━━━━━━━━\n\n"

    for i, entry in enumerate(chunk, start + 1):
        name = _esc(entry.get('anime_name', 'Unknown'))
        ch_title = _esc(entry.get('channel_title', 'Unknown'))
        if mode == "list":
            ch_id = entry.get('channel_id', 'N/A')
            hashtag = _esc(entry.get('hashtag', ''))
            link = _esc(entry.get('channel_link', ''))
            text += (
                f"<b>{i}.</b> 📺 {name}\n"
                f"   📢 {ch_title} (<code>{ch_id}</code>)\n"
                f"   🏷️ {hashtag} | 🔗 {link}\n\n"
            )
        else:
            text += f"<b>{i}.</b> {name} → {ch_title}\n"

    text += (
        f"\n━━━━━━━━━━━━━━━━━━━━\n"
        f"Page <b>{page + 1}/{total_pages}</b> | Total: <b>{total}</b>\n\n"
        f"🗑️ Remove: <code>/del_anime &lt;number&gt;</code>"
    )

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"anime_pg_{mode}_{page - 1}"))
    nav_row.append(InlineKeyboardButton(f"📄 {page + 1}/{total_pages}", callback_data="anime_pg_noop"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"anime_pg_{mode}_{page + 1}"))

    markup = InlineKeyboardMarkup([nav_row]) if len(nav_row) > 1 else None
    return text, markup


async def _monitor_channel_text(client: Client) -> str:
    """
    Monitor channel ka display text banao — list/del dono handlers use karte
    hain. HTML mode ke liye title ko html.escape() se hokar <code> span mein
    daala jaata hai — is baar backtick, asterisk, underscore, kuch bhi ho
    title mein, entity kabhi nahi tootega (HTML escaping position-independent
    hai, Markdown code-span jaisi koi ambiguity nahi).
    """
    monitor_ch = await _get_monitor_channel()
    if not monitor_ch:
        return "❌ Set nahi — use <code>/set_monitor</code>"
    try:
        mc = await client.get_chat(monitor_ch)
        return f"✅ <code>{_esc(mc.title)}</code> (<code>{monitor_ch}</code>)"
    except Exception:
        return f"⚠️ ID: <code>{monitor_ch}</code> (access error)"


# ─────────────────────────────────────────────
#  /list_anime — button-style panel (/update_post_list jaisa)
#
#  PEHLE: har anime raw message TEXT mein **bold** ke saath likha jaata
#  tha. Woh MESSAGE_TOO_LONG toh fix ho gaya tha, lekin _monitor_channel_text
#  ka channel title kabhi escape nahi hua — ek stray *, _ ya ` waale title
#  ne poore message ke Markdown entities todh diye (ENTITY_BOUNDS_INVALID).
#  Do escaping attempts ke baad bhi Markdown mode mein crash wapas aata raha
#  (/list_anime AUR /del_anime dono mein) — Pyrofork ka Markdown tokenizer
#  bahut edge cases mein predictable escaping allow hi nahi karta.
#  AB: is poore panel mein parse_mode=ParseMode.HTML use hota hai (fixed,
#  simple escaping rules — sirf <, >, & — koi ambiguity nahi), aur har
#  anime ek BUTTON label hai jo kabhi parse hi nahi hota. Dono wajah se ab
#  crash structurally hi possible nahi hai, naam mein kuch bhi ho.
# ─────────────────────────────────────────────
async def _show_list_anime_panel(client: Client, event, user_id: int, page: int, is_new: bool):
    anime_list = await _get_anime_list()
    mc_text = await _monitor_channel_text(client)

    if not anime_list:
        text = (
            f"📡 <b>Monitor Channel:</b> {mc_text}\n\n"
            f"📋 Koi anime registered nahi hai!\n\n"
            f"Add karo: <code>/add_anime</code>"
        )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Close", callback_data="closeMeh")]])
        if is_new:
            await event.reply(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        else:
            try:
                await event.edit(text, reply_markup=kb, parse_mode=ParseMode.HTML)
            except Exception:
                pass
        return

    total = len(anime_list)
    total_pages = max(1, (total + ANIME_PAGE_SIZE - 1) // ANIME_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * ANIME_PAGE_SIZE
    end = min(start + ANIME_PAGE_SIZE, total)

    rows = []
    for idx in range(start, end):
        entry = anime_list[idx]
        name = entry.get('anime_name', 'Unknown')
        ch_title = entry.get('channel_title', 'Unknown')
        label = f"{idx + 1}. 📺 {name} → {ch_title}"
        if len(label) > 50:
            label = label[:47] + "…"
        rows.append([InlineKeyboardButton(label, callback_data=f"alist_open_{idx}_{user_id}_{page}")])

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"alist_page_{page - 1}_{user_id}"))
    nav_row.append(InlineKeyboardButton(f"📄 {page + 1}/{total_pages}", callback_data=f"alist_noop_{user_id}"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"alist_page_{page + 1}_{user_id}"))
    rows.append(nav_row)
    rows.append([InlineKeyboardButton("❌ Close", callback_data="closeMeh")])

    kb = InlineKeyboardMarkup(rows)
    text = (
        f"📡 <b>Monitor:</b> {mc_text}\n\n"
        f"📋 <b>Registered Anime ({total})</b>\n"
        f"<i>Kisi bhi anime pe tap karke full detail dekho.</i>\n\n"
        f"🗑️ Remove: <code>/del_anime &lt;number&gt;</code>"
    )

    if is_new:
        await event.reply(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    else:
        try:
            await event.edit(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception:
            pass


async def _show_anime_detail(client: Client, event, user_id: int, idx: int, page: int):
    """
    Single anime ka detail view. HTML mode + html.escape() — chahe
    name/channel/hashtag/link mein kuch bhi character ho, entity crash
    structurally possible nahi hai.
    """
    anime_list = await _get_anime_list()

    if idx < 0 or idx >= len(anime_list):
        await _show_list_anime_panel(client, event, user_id, page, is_new=False)
        return

    entry = anime_list[idx]
    name = _esc(entry.get('anime_name', 'Unknown'))
    ch_title = _esc(entry.get('channel_title', 'Unknown'))
    ch_id = entry.get('channel_id', 'N/A')
    hashtag = _esc(entry.get('hashtag', ''))
    link = _esc(entry.get('channel_link', ''))

    text = (
        f"📺 <b>Anime Detail</b>\n\n"
        f"<b>Name:</b> <code>{name}</code>\n"
        f"<b>Channel:</b> <code>{ch_title}</code> (<code>{ch_id}</code>)\n"
        f"<b>Hashtag:</b> <code>{hashtag}</code>\n"
        f"<b>Link:</b> <code>{link}</code>\n\n"
        f"🗑️ Remove: <code>/del_anime {idx + 1}</code>"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Back", callback_data=f"alist_back_{page}_{user_id}")],
    ])

    try:
        await event.edit(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except Exception as e:
        LOGGER.warning(f"[AutoMonitor] _show_anime_detail edit error: {e}")


@Client.on_message(filters.command("list_anime") & filters.private)
async def cmd_list_anime(client: Client, message: Message):
    if not _is_authorized(message.from_user.id):
        return

    # NOTE: pehle yahan empty-list ke liye ek ALAG duplicate Markdown text
    # tha (bina html.escape() / mc_text ke) — usi class ka crash risk tha.
    # Ab _show_list_anime_panel() ko hi call karo, wo empty aur non-empty
    # dono cases HTML-safe tarike se already handle karta hai.
    await _show_list_anime_panel(client, message, message.from_user.id, page=0, is_new=True)


# ─────────────────────────────────────────────
#  /list_anime panel ke callbacks (alist_*) — /update_post_list (upe_*) jaisa
# ─────────────────────────────────────────────
@Client.on_callback_query(filters.regex(r"^alist_"))
async def anime_list_panel_callbacks(client: Client, cb: CallbackQuery):
    data = cb.data
    user_id = cb.from_user.id

    if not _is_authorized(user_id):
        await cb.answer("⛔ Permission nahi hai.", show_alert=True)
        return

    # ── alist_noop_<uid> — page indicator, sirf display ──
    if data.startswith("alist_noop_"):
        await cb.answer()
        return

    # ── alist_page_<page>_<uid> ──
    if data.startswith("alist_page_"):
        parts = data.split("_")
        try:
            page = int(parts[2])
            owner_id = int(parts[3])
        except (ValueError, IndexError):
            await cb.answer()
            return
        if user_id != owner_id:
            await cb.answer("❌ Ye tumhara nahi hai!", show_alert=True)
            return
        await cb.answer()
        await _show_list_anime_panel(client, cb.message, owner_id, page, is_new=False)
        return

    # ── alist_open_<idx>_<uid>_<page> ──
    if data.startswith("alist_open_"):
        parts = data.split("_")
        try:
            idx = int(parts[2])
            owner_id = int(parts[3])
            page = int(parts[4]) if len(parts) > 4 else 0
        except (ValueError, IndexError):
            await cb.answer()
            return
        if user_id != owner_id:
            await cb.answer("❌ Ye tumhara nahi hai!", show_alert=True)
            return
        await cb.answer()
        await _show_anime_detail(client, cb.message, owner_id, idx, page)
        return

    # ── alist_back_<page>_<uid> ──
    if data.startswith("alist_back_"):
        parts = data.split("_")
        try:
            page = int(parts[2])
            owner_id = int(parts[3])
        except (ValueError, IndexError):
            await cb.answer()
            return
        if user_id != owner_id:
            await cb.answer("❌ Ye tumhara nahi hai!", show_alert=True)
            return
        await cb.answer()
        await _show_list_anime_panel(client, cb.message, owner_id, page, is_new=False)
        return

    await cb.answer()


# ─────────────────────────────────────────────
#  /del_anime ke Prev/Next buttons (compact text-list style, unchanged)
# ─────────────────────────────────────────────
@Client.on_callback_query(filters.regex(r"^anime_pg_del_(\d+)$"))
async def anime_del_page_callback(client: Client, cb: CallbackQuery):
    if not _is_authorized(cb.from_user.id):
        await cb.answer("⛔ Permission nahi hai.", show_alert=True)
        return

    page = int(cb.matches[0].group(1))

    anime_list = await _get_anime_list()
    if not anime_list:
        await cb.answer("📋 List ab khaali hai.", show_alert=True)
        try:
            await cb.message.edit("📋 Koi anime registered nahi hai!")
        except Exception:
            pass
        return

    mc_text = await _monitor_channel_text(client)
    text, markup = _render_anime_list_page(anime_list, page, mc_text, "del")
    try:
        await cb.message.edit(text, reply_markup=markup, parse_mode=ParseMode.HTML)
    except Exception as e:
        LOGGER.warning(f"[AutoMonitor] anime_del_page_callback edit error: {e}")
    await cb.answer()


@Client.on_callback_query(filters.regex(r"^anime_pg_noop$"))
async def anime_list_page_noop(client: Client, cb: CallbackQuery):
    await cb.answer()


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
        mc_text = await _monitor_channel_text(client)
        text, markup = _render_anime_list_page(anime_list, 0, mc_text, "del")
        await message.reply(text, reply_markup=markup, parse_mode=ParseMode.HTML)
        return

    try:
        num = int(message.command[1])
    except ValueError:
        await message.reply("❌ Sahi number dalo! Example: <code>/del_anime 1</code>", parse_mode=ParseMode.HTML)
        return

    if num < 1 or num > len(anime_list):
        await message.reply(f"❌ 1 se {len(anime_list)} tak dalo.")
        return

    removed = anime_list.pop(num - 1)
    await _save_anime_list(anime_list)

    await message.reply(
        f"✅ <b>Removed!</b>\n\n"
        f"📺 {_esc(removed.get('anime_name'))}\n"
        f"📢 {_esc(removed.get('channel_title'))}",
        parse_mode=ParseMode.HTML,
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
            mc_text = f"✅ {_esc(mc.title)} (<code>{monitor_ch}</code>)"
        except Exception:
            mc_text = f"⚠️ ID set (<code>{monitor_ch}</code>) but access error"
    else:
        mc_text = "❌ Set nahi — use <code>/set_monitor</code>"

    await message.reply(
        f"📊 <b>AutoMonitor Status</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📡 Monitor Channel: {mc_text}\n"
        f"📺 Anime Count: <b>{len(anime_list)}</b>\n"
        f"🎯 Target Qualities: <code>{' | '.join(TARGET_QUALITIES)}</code>\n"
        f"⚡ Fast Poll: <b>{POLL_FAST_ATTEMPTS} × {POLL_INTERVAL_FAST}s</b> (first 5 min)\n"
        f"🐢 Slow Poll: <b>{POLL_SLOW_ATTEMPTS} × {POLL_INTERVAL_SLOW}s</b> (next 20 min)\n"
        f"⏰ Max Attempts: <b>{POLL_FAST_ATTEMPTS + POLL_SLOW_ATTEMPTS}</b> (~25 min total)\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📋 /list_anime\n"
        f"➕ /add_anime",
        parse_mode=ParseMode.HTML,
    )


# ─────────────────────────────────────────────
#  Bot Mode — Pending quality popup callback
#  User "⏳ 720p uploading..." pe click kare toh toast show karo
# ─────────────────────────────────────────────
@Client.on_callback_query(filters.regex(r"^bm_pending_(.+)$"))
async def bm_pending_callback(client: Client, cb: CallbackQuery):
    """Pending quality button pe click → toast popup answer karo."""
    quality = cb.matches[0].group(1)
    try:
        await cb.answer(
            f"⏳ {quality} abhi upload nahi hua hai, thoda wait karo!",
            show_alert=True,
        )
    except Exception as e:
        LOGGER.warning(f"[BotMode] bm_pending_callback answer error: {e}")

