"""
bot_upload_engine.py
=====================
Phase 2 helpers for /bot_upload:
  - EpisodePostManager : per-episode channel post with 480p/720p/1080p buttons
  - create_batch_link  : automates the file-to-link bot's /batch ... /complete flow
  - run_episode_rti    : downloads+uploads one RTI episode (all qualities) to log channel
  - episode_from_filename / quality grouping helpers for /url -e
"""

import asyncio
import os
import re
import time

from pyrogram import Client
from pyrogram.enums import ParseMode
try:
    from pyrogram.enums import ButtonStyle
    _BUTTON_STYLE_SUPPORTED = True
except ImportError:
    ButtonStyle = None
    _BUTTON_STYLE_SUPPORTED = False
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from .. import LOGGER, log as LOG_CHANNEL
from .uploads.telegram import upload_video, get_thumbnail, _make_uploader_client
from ..plugins.swift_downloader import (
    _quality_from, _sort_by_size, _upload_one_file, _scrape_and_download,
    QUALITY_ORDER,
)
from ..plugins.auto_monitor import _get_suhani_bot_link
from ..plugins.rti_downloader import get_watchmult_link, get_argon_link, argon_to_swift


# ─────────────────────────────────────────────────────────────────────────
#  Colour buttons — quality -> (ButtonStyle, custom_emoji_id, fallback_emoji)
#  low_q slot  = 360p ya 480p (jo bhi pehle ready ho)  -> Blue
#  720p slot                                            -> Green
#  1080p slot                                           -> Red
#  Agar installed pyrofork version mein ButtonStyle/icon_custom_emoji_id
#  support nahi hai, to plain emoji-prefixed text button pe fallback hoga
#  (crash kabhi nahi hoga).
# ─────────────────────────────────────────────────────────────────────────
_BUTTON_STYLE = {
    "low":   (ButtonStyle.PRIMARY if _BUTTON_STYLE_SUPPORTED else None, 5440389890787281213, "🔵"),  # Blue
    "720p":  (ButtonStyle.SUCCESS if _BUTTON_STYLE_SUPPORTED else None, 5355142851615283756, "🟢"),  # Green
    "1080p": (ButtonStyle.DANGER  if _BUTTON_STYLE_SUPPORTED else None, 5354968347094046619, "🔴"),  # Red
}


def _quality_button(text: str, url: str, slot: str) -> InlineKeyboardButton:
    style, icon_id, fallback_emoji = _BUTTON_STYLE.get(slot, _BUTTON_STYLE["low"])
    if _BUTTON_STYLE_SUPPORTED:
        try:
            return InlineKeyboardButton(
                text=text, url=url, icon_custom_emoji_id=icon_id, style=style,
            )
        except Exception as e:
            LOGGER.warning(f"[BotUpload] Colour button failed ({text}), falling back to emoji: {e}")
    # Fallback: plain emoji-prefixed url button (pyrofork ButtonStyle support na ho tab)
    return InlineKeyboardButton(text=f"{fallback_emoji} {text}", url=url)


# ─────────────────────────────────────────────────────────────────────────
#  Episode number detection (for /url -e grouping)
# ─────────────────────────────────────────────────────────────────────────
def episode_from_filename(filename: str) -> int | None:
    m = re.search(r'[Ss]\d{1,2}[Ee](\d{1,3})', filename)
    if m:
        return int(m.group(1))
    m = re.search(r'(?:EP|Episode|E)\s?0*(\d{1,3})\b', filename, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return None


# ─────────────────────────────────────────────────────────────────────────
#  EpisodePostManager — per-episode channel post (480p/720p/1080p buttons)
# ─────────────────────────────────────────────────────────────────────────
class EpisodePostManager:
    # Order matters: lowest available quality shown first, 2160p never shown
    QUALITY_ORDER = ["360p", "480p", "720p", "1080p"]
    # Qualities for which a "⏳ uploading..." placeholder makes sense if missing
    PENDING_CANDIDATES = ["720p", "1080p"]

    def __init__(self, client: Client, channel_id: int, anime_name: str,
                 episode_num: int, season_num: int = 1, language: str = "Hindi",
                 session_code: str | None = None):
        self.client = client
        self.channel_id = channel_id
        self.anime_name = anime_name
        self.episode_num = episode_num
        self.season_num = season_num
        self.language = language
        self.session_code = session_code  # unique code for multi-run support
        self.post_msg_id: int | None = None
        self._buttons: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._db_loaded = False

    def _caption(self) -> str:
        s = f"{self.season_num:02d}"
        e = f"{self.episode_num:02d}"
        return f"<b>➲ Season {s} Episode {e} {self.language}</b>"

    def _keyboard(self) -> InlineKeyboardMarkup | None:
        """
        Sirf wo qualities dikhao jo upload ho gayi hain.
        Koi placeholder/pending button nahi — jab quality ready hogi tab button add hoga.

        Layout:
          - 360p OR 480p (lowest available, ek hi dikhega) — Blue
          - 720p (sirf agar uploaded) — Green
          - 1080p (sirf agar uploaded) — Red
        Sab ek hi row mein.
        """
        row = []

        # 360p / 480p — lowest jo ready hai, sirf wahi ek
        low_q = None
        if "360p" in self._buttons:
            low_q = "360p"
        elif "480p" in self._buttons:
            low_q = "480p"
        if low_q:
            row.append(_quality_button(low_q, self._buttons[low_q], "low"))

        # 720p aur 1080p — sirf tab dikhao jab uploaded ho
        for q in ["720p", "1080p"]:
            if q in self._buttons:
                row.append(_quality_button(q, self._buttons[q], q))
            # agar upload nahi hua — koi button nahi, koi placeholder nahi

        return InlineKeyboardMarkup([row]) if row else None

    def _db_key(self) -> str | None:
        """Unique DB key — session_code + episode number."""
        if not self.session_code:
            return None
        return f"{self.session_code}_ep{self.episode_num:03d}"

    async def _load_from_db(self):
        """DB se existing post_msg_id aur buttons load karo."""
        key = self._db_key()
        if not key or self._db_loaded:
            return
        self._db_loaded = True
        try:
            from .database.access_db import db as _db
            data = await _db.get_ep_post(key)
            if data:
                self.post_msg_id = data.get("msg_id")
                for q, url in data.get("buttons", {}).items():
                    if q not in self._buttons:
                        self._buttons[q] = url
                LOGGER.info(f"[EpisodePost] Loaded from DB: key={key} msg_id={self.post_msg_id} buttons={list(self._buttons.keys())}")
        except Exception as e:
            LOGGER.warning(f"[EpisodePost] DB load failed: {e}")

    async def _save_to_db(self):
        """Current state DB mein save karo."""
        key = self._db_key()
        if not key:
            return
        try:
            from .database.access_db import db as _db
            await _db.set_ep_post(key, {"msg_id": self.post_msg_id, "buttons": dict(self._buttons)})
        except Exception as e:
            LOGGER.warning(f"[EpisodePost] DB save failed: {e}")

    async def add_quality(self, quality: str, deep_link_url: str):
        if quality == "2160p":
            return  # never displayed
        async with self._lock:
            # Pehli baar: DB se load karo (resume mode mein existing msg_id milega)
            await self._load_from_db()

            self._buttons[quality] = deep_link_url
            caption = self._caption()

            if self.post_msg_id is None:
                # Naya message banao
                try:
                    sent = await self.client.send_message(
                        chat_id=self.channel_id, text=caption,
                        reply_markup=self._keyboard(),
                        parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                    )
                    self.post_msg_id = sent.id
                    await self._save_to_db()
                except Exception as e:
                    LOGGER.error(f"[BotUpload] Post create error: {e}")
            else:
                # Existing message edit karo (resume mode)
                try:
                    await self.client.edit_message_text(
                        chat_id=self.channel_id, message_id=self.post_msg_id,
                        text=caption, reply_markup=self._keyboard(),
                        parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                    )
                    await self._save_to_db()
                except Exception as e:
                    LOGGER.error(f"[BotUpload] Post edit error: {e}")


# ─────────────────────────────────────────────────────────────────────────
#  Upload a single file to log channel (skip_forward) + return (quality, sent_msg)
# ─────────────────────────────────────────────────────────────────────────
async def upload_file_to_log(client: Client, message: Message, status_msg: Message,
                              filepath: str, dl_dir: str):
    """
    Ek file ko log channel pe upload karo (skip_forward=True).
    Shared uploader client banao taaki user client se fast upload ho aur
    progress callback bhi kaam kare (bot fallback mein progress nahi aata).
    """
    uc = None
    try:
        uc = await _make_uploader_client(message.from_user.id)
        result = await _upload_one_file(
            client, message, status_msg, filepath, dl_dir,
            encode=False, skip_forward=True, uploader_client=uc,
        )
        return result
    finally:
        if uc:
            try:
                await uc.disconnect()
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────────────────
#  Batch link creation via the file-to-link bot (/batch ... /complete)
#  Both /batch and the uploaded files + /complete go to LOG_CHANNEL where
#  that bot is already added & listening.
# ─────────────────────────────────────────────────────────────────────────
LINK_PATTERN = re.compile(r'https://t\.me/\S+\?start=\S+')


async def _wait_for_bot_reply(client: Client, after_msg_id: int, contains: list[str],
                               timeout: int = 60) -> Message | None:
    """
    after_msg_id ke baad aane wale messages mein contains keywords dhundo.
    after_msg_id+1 se shuru karke +10 tak scan karo har poll mein.
    """
    start = time.time()
    check_from = after_msg_id + 1
    while time.time() - start < timeout:
        for tid in range(check_from, check_from + 10):
            try:
                m = await client.get_messages(LOG_CHANNEL, tid)
                if m and m.text and any(c.lower() in m.text.lower() for c in contains):
                    return m
            except Exception:
                pass
        await asyncio.sleep(2)
    return None


async def create_batch_link(client: Client, message_ids: list[int], timeout: int = 180) -> str | None:
    """
    /batch -> forward all message_ids (within LOG_CHANNEL) -> /complete -> parse link.
    Returns deep-link URL or None.

    Key insight: /complete bhejne ke baad bot ka reply = complete_msg.id + 1
    Isliye directly woh message fetch karo — reliable aur fast.
    """
    if not message_ids:
        return None

    try:
        start_msg = await client.send_message(LOG_CHANNEL, "/batch")
    except Exception as e:
        LOGGER.error(f"[BotUpload] /batch send failed: {e}")
        return None

    # /batch reply ka wait — "Batch Mode ON" confirm hone tak
    ready = await _wait_for_bot_reply(client, start_msg.id, ["batch mode on", "batch"], timeout=30)
    if not ready:
        LOGGER.warning("[BotUpload] Batch Mode ON reply not detected, proceeding anyway")
        await asyncio.sleep(3)

    # Saari files forward karo
    try:
        await client.forward_messages(
            chat_id=LOG_CHANNEL,
            from_chat_id=LOG_CHANNEL,
            message_ids=message_ids,
        )
    except Exception as e:
        LOGGER.error(f"[BotUpload] Forwarding for batch failed: {e}")
        return None

    # Forwards process hone ka wait
    await asyncio.sleep(3)

    try:
        complete_msg = await client.send_message(LOG_CHANNEL, "/complete")
    except Exception as e:
        LOGGER.error(f"[BotUpload] /complete send failed: {e}")
        return None

    LOGGER.info(f"[BotUpload] /complete sent at msg_id={complete_msg.id}. Batch link next msg pe hoga.")

    # /complete ke baad pehle 5s wait karo — bot ko process karne ka time chahiye
    await asyncio.sleep(5)

    # /complete ke baad bot reply = complete_msg.id + 1 (direct fetch, fast path)
    expected_id = complete_msg.id + 1
    deadline = time.time() + timeout
    while time.time() < deadline:
        await asyncio.sleep(3)
        try:
            m = await client.get_messages(LOG_CHANNEL, expected_id)
            if m and m.text:
                match = LINK_PATTERN.search(m.text)
                if match:
                    LOGGER.info(f"[BotUpload] Batch link found at msg_id={expected_id}: {match.group(0)}")
                    return match.group(0)
                # Message aaya but link nahi — bot ne kuch aur reply diya, next try
                LOGGER.warning(f"[BotUpload] msg {expected_id} no link (text='{m.text[:60]}'), trying +1")
                expected_id += 1
        except Exception:
            pass  # Message abhi nahi aaya — loop continue

    LOGGER.error("[BotUpload] Batch link timeout — koi link nahi mila")
    return None


# ─────────────────────────────────────────────────────────────────────────
#  RTI: download + upload all qualities of one episode -> log channel
#  Returns: { quality: sent_msg } for whatever uploaded successfully
# ─────────────────────────────────────────────────────────────────────────
async def run_episode_rti(client: Client, message: Message, status_msg: Message,
                           page_url: str, ep_num: int, total: int,
                           post_mgr: "EpisodePostManager | None" = None) -> dict:
    from .. import download_dir

    wmq_link, _ = get_watchmult_link(page_url, ep_num)
    if not wmq_link:
        await status_msg.edit(f"❌ Ep {ep_num}/{total}: WatchMultQuality link nahi mila.")
        return {}

    argon_link = get_argon_link(wmq_link)
    if not argon_link:
        await status_msg.edit(f"❌ Ep {ep_num}/{total}: Argon link nahi mila.")
        return {}

    swift_url = argon_to_swift(argon_link)
    if not swift_url:
        await status_msg.edit(f"❌ Ep {ep_num}/{total}: Swift URL nahi mila.")
        return {}

    session_id = str(int(time.time()))
    dl_dir = os.path.join(download_dir, f"botup_{session_id}")
    os.makedirs(dl_dir, exist_ok=True)

    await status_msg.edit(f"⬇️ **Ep {ep_num}/{total}** — downloading...")
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _scrape_and_download, swift_url, dl_dir, None, None)

    files = result.get("files") or []
    if not files:
        await status_msg.edit(f"❌ Ep {ep_num}/{total}: Koi file download nahi hui.")
        return {}

    files = _sort_by_size(files)
    uploaded = {}
    for fp in files:
        q = _quality_from(os.path.basename(fp))
        if q == "2160p":
            continue  # 2160p is never uploaded
        await status_msg.edit(f"📤 **Ep {ep_num}/{total}** — uploading `{q}`...")
        success, sent_msg, quality = await upload_file_to_log(client, message, status_msg, fp, dl_dir)
        if success and sent_msg:
            uploaded[quality] = sent_msg
            # ── Button turant add karo (poore episode ka wait mat karo) ──
            if post_mgr:
                try:
                    link = await _get_suhani_bot_link(sent_msg)
                    if link:
                        await post_mgr.add_quality(quality, link)
                except Exception as e:
                    LOGGER.error(f"[BotUpload] RTI immediate add_quality failed ({quality}): {e}")

    try:
        import shutil
        shutil.rmtree(dl_dir, ignore_errors=True)
    except Exception:
        pass

    return uploaded


async def send_batch_summary_post(client: Client, channel_id: int, season_num: int,
                                   batch_links: dict, language: str = "Hindi") -> int | None:
    """
    Posts 'Season XX Full Batch Hindi' with quality buttons (360p/480p, 720p, 1080p)
    pointing to the batch links, to the channel. Returns the sent message id.
    """
    s = f"{season_num:02d}"
    caption = f"<b>➲ Season {s} Full Batch {language}</b>"

    row = []
    low_q = "360p" if "360p" in batch_links else ("480p" if "480p" in batch_links else None)
    if low_q:
        row.append(_quality_button(low_q, batch_links[low_q], "low"))
    for q in ["720p", "1080p"]:
        if q in batch_links:
            row.append(_quality_button(q, batch_links[q], q))

    keyboard = InlineKeyboardMarkup([row]) if row else None

    try:
        sent = await client.send_message(
            chat_id=channel_id, text=caption, reply_markup=keyboard,
            parse_mode=ParseMode.HTML, disable_web_page_preview=True,
        )
        return sent.id
    except Exception as e:
        LOGGER.error(f"[BotUpload] Batch summary post error: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────
#  End message
# ─────────────────────────────────────────────────────────────────────────
async def send_end_message(client: Client, channel_id: int, end_template: str,
                            anime_name: str, season_no: int, batch_links: dict):
    if not end_template:
        return
    text = end_template.format(
        anime_name=anime_name,
        season=season_no,
        q480=batch_links.get("480p", "—"),
        q720=batch_links.get("720p", "—"),
        q1080=batch_links.get("1080p", "—"),
    )
    try:
        await client.send_message(channel_id, text, disable_web_page_preview=True)
    except Exception as e:
        LOGGER.error(f"[BotUpload] End message send failed: {e}")
