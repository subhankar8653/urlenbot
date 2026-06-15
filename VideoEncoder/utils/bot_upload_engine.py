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
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from .. import LOGGER, log as LOG_CHANNEL
from .uploads.telegram import upload_video, get_thumbnail
from ..plugins.swift_downloader import (
    _quality_from, _sort_by_size, _upload_one_file, _scrape_and_download,
    QUALITY_ORDER,
)
from ..plugins.auto_monitor import _get_suhani_bot_link
from ..plugins.rti_downloader import get_watchmult_link, get_argon_link, argon_to_swift


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
    UPLOAD_QUALITIES = ["480p", "720p", "1080p"]

    def __init__(self, client: Client, channel_id: int, anime_name: str,
                 episode_num: int, season_num: int = 1, language: str = "Hindi"):
        self.client = client
        self.channel_id = channel_id
        self.anime_name = anime_name
        self.episode_num = episode_num
        self.season_num = season_num
        self.language = language
        self.post_msg_id: int | None = None
        self._buttons: dict[str, str] = {}
        self._lock = asyncio.Lock()

    def _caption(self) -> str:
        s = f"{self.season_num:02d}"
        e = f"{self.episode_num:02d}"
        return f"<b>➲ {self.anime_name} | Season {s} Episode {e} {self.language}</b>"

    def _keyboard(self, first: bool = False) -> InlineKeyboardMarkup | None:
        row = []
        for q in self.UPLOAD_QUALITIES:
            if q in self._buttons:
                row.append(InlineKeyboardButton(text=f"➲ {q}", url=self._buttons[q]))
            elif first or self.post_msg_id is not None or self._buttons:
                row.append(InlineKeyboardButton(text=f"⏳ {q} uploading...", callback_data=f"bm_pending_{q}"))
        return InlineKeyboardMarkup([row]) if row else None

    async def add_quality(self, quality: str, deep_link_url: str):
        async with self._lock:
            self._buttons[quality] = deep_link_url
            caption = self._caption()
            if self.post_msg_id is None:
                try:
                    sent = await self.client.send_message(
                        chat_id=self.channel_id, text=caption,
                        reply_markup=self._keyboard(first=True),
                        parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                    )
                    self.post_msg_id = sent.id
                except Exception as e:
                    LOGGER.error(f"[BotUpload] Post create error: {e}")
            else:
                try:
                    await self.client.edit_message_text(
                        chat_id=self.channel_id, message_id=self.post_msg_id,
                        text=caption, reply_markup=self._keyboard(),
                        parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                    )
                except Exception as e:
                    LOGGER.error(f"[BotUpload] Post edit error: {e}")


# ─────────────────────────────────────────────────────────────────────────
#  Upload a single file to log channel (skip_forward) + return (quality, sent_msg)
# ─────────────────────────────────────────────────────────────────────────
async def upload_file_to_log(client: Client, message: Message, status_msg: Message,
                              filepath: str, dl_dir: str):
    return await _upload_one_file(client, message, status_msg, filepath, dl_dir,
                                    encode=False, skip_forward=True)


# ─────────────────────────────────────────────────────────────────────────
#  Batch link creation via the file-to-link bot (/batch ... /complete)
#  Both /batch and the uploaded files + /complete go to LOG_CHANNEL where
#  that bot is already added & listening.
# ─────────────────────────────────────────────────────────────────────────
LINK_PATTERN = re.compile(r'https://t\.me/\S+\?start=\S+')


async def _wait_for_bot_reply(client: Client, after_msg_id: int, contains: list[str],
                               timeout: int = 60) -> Message | None:
    start = time.time()
    while time.time() - start < timeout:
        for tid in range(after_msg_id + 1, after_msg_id + 5):
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
    """
    if not message_ids:
        return None

    try:
        start_msg = await client.send_message(LOG_CHANNEL, "/batch")
    except Exception as e:
        LOGGER.error(f"[BotUpload] /batch send failed: {e}")
        return None

    ready = await _wait_for_bot_reply(client, start_msg.id, ["batch mode on"], timeout=30)
    if not ready:
        LOGGER.warning("[BotUpload] Batch Mode ON reply not detected, proceeding anyway")

    try:
        await client.forward_messages(chat_id=LOG_CHANNEL, from_chat_id=LOG_CHANNEL,
                                        message_ids=message_ids)
    except Exception as e:
        LOGGER.error(f"[BotUpload] Forwarding for batch failed: {e}")

    await asyncio.sleep(2)

    try:
        complete_msg = await client.send_message(LOG_CHANNEL, "/complete")
    except Exception as e:
        LOGGER.error(f"[BotUpload] /complete send failed: {e}")
        return None

    reply = await _wait_for_bot_reply(client, complete_msg.id, ["t.me/"], timeout=timeout)
    if not reply:
        return None

    m = LINK_PATTERN.search(reply.text)
    return m.group(0) if m else None


# ─────────────────────────────────────────────────────────────────────────
#  RTI: download + upload all qualities of one episode -> log channel
#  Returns: { quality: sent_msg } for whatever uploaded successfully
# ─────────────────────────────────────────────────────────────────────────
async def run_episode_rti(client: Client, message: Message, status_msg: Message,
                           page_url: str, ep_num: int, total: int) -> dict:
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
        await status_msg.edit(f"📤 **Ep {ep_num}/{total}** — uploading `{q}`...")
        success, sent_msg, quality = await upload_file_to_log(client, message, status_msg, fp, dl_dir)
        if success and sent_msg:
            uploaded[quality] = sent_msg

    try:
        import shutil
        shutil.rmtree(dl_dir, ignore_errors=True)
    except Exception:
        pass

    return uploaded


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
