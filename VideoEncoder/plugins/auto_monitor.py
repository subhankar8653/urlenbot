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
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pyrogram.enums import ParseMode

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

    # ── DL folder ──
    session_id = f"monitor_ep{episode_num}_{int(time.time())}"
    dl_dir = os.path.join(download_dir, session_id)
    os.makedirs(dl_dir, exist_ok=True)

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
#  Paginated anime list — /list_anime aur /del_anime dono is se render
#  hote hain.
#
#  PEHLE: poori anime_list ek hi message mein bheji jaati thi. List badi ho
#  jaane par (jaise ab) Telegram [400 MESSAGE_TOO_LONG] / [400
#  ENTITY_BOUNDS_INVALID] de deta tha (4096 char limit + bold/code markdown
#  entities corrupt ho jaate the) — isliye command "kaam nahi kar raha tha"
#  (crash ho ke exception, koi reply hi nahi jaata tha).
#  AB: hamesha max ANIME_PAGE_SIZE entries ek page mein, Prev/Next buttons
#  se navigate karo.
# ─────────────────────────────────────────────
ANIME_PAGE_SIZE = 10


def _md_escape(value) -> str:
    """
    Anime_name/channel_title/hashtag jaise dynamic fields mein agar
    *, _, `, [ jaisa raw Markdown character aa jaaye toh Telegram entity
    parsing todh deta hai (ENTITY_BOUNDS_INVALID). Har dynamic field yahan
    se hokar hi message mein jaana chahiye.
    """
    if not value:
        return "—"
    text = str(value)
    for ch in ("\\", "*", "_", "`", "["):
        text = text.replace(ch, "\\" + ch)
    return text


def _render_anime_list_page(anime_list: list, page: int, mc_text: str, mode: str):
    """
    anime_list ka ek page (ANIME_PAGE_SIZE items) + pagination buttons
    banao. mode = "list" (/list_anime, full detail) ya "del" (/del_anime
    bina number ke, compact). Return (text, InlineKeyboardMarkup | None).
    Numbering hamesha FULL list ke absolute index pe based hai, taaki
    `/del_anime <number>` kisi bhi page se sahi kaam kare.
    """
    total = len(anime_list)
    total_pages = max(1, (total + ANIME_PAGE_SIZE - 1) // ANIME_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))

    start = page * ANIME_PAGE_SIZE
    chunk = anime_list[start:start + ANIME_PAGE_SIZE]

    header = "📋 **Registered Anime**" if mode == "list" else "🗑️ **Konsa remove karna hai?**"
    text = f"📡 **Monitor:** {mc_text}\n\n{header}\n━━━━━━━━━━━━━━━━━━━━\n\n"

    for i, entry in enumerate(chunk, start + 1):
        name = _md_escape(entry.get('anime_name', 'Unknown'))
        ch_title = _md_escape(entry.get('channel_title', 'Unknown'))
        if mode == "list":
            ch_id = entry.get('channel_id', 'N/A')
            hashtag = _md_escape(entry.get('hashtag', ''))
            link = _md_escape(entry.get('channel_link', ''))
            text += (
                f"**{i}.** 📺 {name}\n"
                f"   📢 {ch_title} (`{ch_id}`)\n"
                f"   🏷️ {hashtag} | 🔗 {link}\n\n"
            )
        else:
            text += f"**{i}.** {name} → {ch_title}\n"

    text += (
        f"\n━━━━━━━━━━━━━━━━━━━━\n"
        f"Page **{page + 1}/{total_pages}** | Total: **{total}**\n\n"
        f"🗑️ Remove: `/del_anime <number>`"
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
    """Monitor channel ka display text banao — list/del dono handlers use karte hain."""
    monitor_ch = await _get_monitor_channel()
    if not monitor_ch:
        return "❌ Set nahi — use `/set_monitor`"
    try:
        mc = await client.get_chat(monitor_ch)
        return f"✅ {mc.title} (`{monitor_ch}`)"
    except Exception:
        return f"⚠️ ID: `{monitor_ch}` (access error)"


# ─────────────────────────────────────────────
#  /list_anime
# ─────────────────────────────────────────────
@Client.on_message(filters.command("list_anime") & filters.private)
async def cmd_list_anime(client: Client, message: Message):
    if not _is_authorized(message.from_user.id):
        return

    anime_list = await _get_anime_list()
    mc_text = await _monitor_channel_text(client)

    if not anime_list:
        await message.reply(
            f"📡 **Monitor Channel:** {mc_text}\n\n"
            f"📋 Koi anime registered nahi hai!\n\n"
            f"Add karo: `/add_anime [channel_id] [Anime Name]`"
        )
        return

    text, markup = _render_anime_list_page(anime_list, 0, mc_text, "list")
    await message.reply(text, reply_markup=markup)


# ─────────────────────────────────────────────
#  /list_anime aur /del_anime ke Prev/Next buttons
# ─────────────────────────────────────────────
@Client.on_callback_query(filters.regex(r"^anime_pg_(list|del)_(\d+)$"))
async def anime_list_page_callback(client: Client, cb: CallbackQuery):
    if not _is_authorized(cb.from_user.id):
        await cb.answer("⛔ Permission nahi hai.", show_alert=True)
        return

    mode = cb.matches[0].group(1)
    page = int(cb.matches[0].group(2))

    anime_list = await _get_anime_list()
    if not anime_list:
        await cb.answer("📋 List ab khaali hai.", show_alert=True)
        try:
            await cb.message.edit("📋 Koi anime registered nahi hai!")
        except Exception:
            pass
        return

    mc_text = await _monitor_channel_text(client)
    text, markup = _render_anime_list_page(anime_list, page, mc_text, mode)
    try:
        await cb.message.edit(text, reply_markup=markup)
    except Exception as e:
        LOGGER.warning(f"[AutoMonitor] anime_list_page_callback edit error: {e}")
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
        await message.reply(text, reply_markup=markup)
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
        f"📺 {_md_escape(removed.get('anime_name'))}\n"
        f"📢 {_md_escape(removed.get('channel_title'))}"
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

