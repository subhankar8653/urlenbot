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

import httpx

from pyrogram import Client
from pyrogram.enums import ParseMode
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
#  Bot API token — styled buttons + custom emoji ke liye httpx use karenge
# ─────────────────────────────────────────────────────────────────────────
_BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# ─────────────────────────────────────────────────────────────────────────
#  Colour buttons — quality -> (style, custom_emoji_id, fallback_emoji)
#  Bot API ke through send karenge — kurigram/pyrofork dependency nahi
# ─────────────────────────────────────────────────────────────────────────
_BUTTON_STYLE = {
    "low":   ("primary", 6178956770564645948, "🔵"),  # Blue
    "720p":  ("success", 6179433490459665818, "🟢"),  # Green
    "1080p": ("danger",  6179270925947510542, "🔴"),  # Red
}

# ─────────────────────────────────────────────────────────────────────────
#  Premium emoji IDs for caption prefix (➲ replacement) and quality buttons
# ─────────────────────────────────────────────────────────────────────────
_CAPTION_EMOJI_ID = 6179062315090977332  # ✅ verified premium emoji

_QUALITY_EMOJI_IDS = {
    "low":   6178956770564645948,
    "720p":  6179433490459665818,
    "1080p": 6179270925947510542,
}


def _make_caption_with_entity(text_without_prefix: str) -> tuple:
    """
    Entities approach — Bot API ke liye plain dict format.
    """
    placeholder = "➲"
    full_text = f"{placeholder} {text_without_prefix}"
    total_len = len(full_text)
    entities = [
        {"type": "bold", "offset": 0, "length": total_len},
        {"type": "custom_emoji", "offset": 0, "length": len(placeholder),
         "custom_emoji_id": str(_CAPTION_EMOJI_ID)},
    ]
    return full_text, entities


def _quality_button_dict(text: str, url: str, slot: str) -> dict:
    """
    Bot API ke liye button dict — Bot API 9.4+ se 'style' aur
    'icon_custom_emoji_id' fields directly support karta hai
    (sirf reply_markup JSON ke through, kisi library object se nahi).
    Fallback emoji bhi rakha hai (agar custom emoji render na ho premium
    account na hone ki wajah se).
    """
    style, emoji_id, fallback_emoji = _BUTTON_STYLE.get(slot, _BUTTON_STYLE["low"])
    return {
        "text": f"{fallback_emoji} {text}",
        "url": url,
        "style": style,
        "icon_custom_emoji_id": str(emoji_id),
    }


def _quality_button(text: str, url: str, slot: str) -> InlineKeyboardButton:
    """Pyrogram fallback — sirf tab jab Bot API na ho."""
    _, _, fallback_emoji = _BUTTON_STYLE.get(slot, _BUTTON_STYLE["low"])
    return InlineKeyboardButton(text=f"{fallback_emoji} {text}", url=url)


async def _bot_api_send_message(chat_id: int, text: str, entities: list,
                                 reply_markup: dict) -> int | None:
    """
    httpx se Bot API call — styled buttons + custom emoji.
    Returns message_id (int) agar success, None agar fail.
    """
    if not _BOT_TOKEN:
        return None
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{_BOT_TOKEN}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "entities": entities,
                    "reply_markup": reply_markup,
                }
            )
            data = resp.json()
            if not data.get("ok"):
                LOGGER.warning(f"[BotUpload] Bot API sendMessage failed: {data.get('description')}")
                return None
            # Response mein directly message_id milta hai
            return data["result"]["message_id"]
    except Exception as e:
        LOGGER.warning(f"[BotUpload] Bot API call failed: {e}")
        return None


async def _bot_api_send_photo(chat_id: int, photo: str, caption: str,
                               caption_entities: list, reply_markup: dict) -> int | None:
    """
    httpx se Bot API sendPhoto — image + caption + styled buttons.
    `photo` ek file_id, URL, ya local file path ho sakta hai. Local path hone par
    multipart upload karte hain, warna seedha JSON mein bhej dete hain (file_id/URL).
    Returns message_id agar success, None agar fail.
    """
    if not _BOT_TOKEN:
        return None
    import json as _json
    import os as _os
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            if photo and _os.path.isfile(photo):
                with open(photo, "rb") as fh:
                    resp = await client.post(
                        f"https://api.telegram.org/bot{_BOT_TOKEN}/sendPhoto",
                        data={
                            "chat_id": str(chat_id),
                            "caption": caption,
                            "caption_entities": _json.dumps(caption_entities or []),
                            "reply_markup": _json.dumps(reply_markup or {}),
                        },
                        files={"photo": fh},
                    )
            else:
                resp = await client.post(
                    f"https://api.telegram.org/bot{_BOT_TOKEN}/sendPhoto",
                    json={
                        "chat_id": chat_id,
                        "photo": photo,
                        "caption": caption,
                        "caption_entities": caption_entities or [],
                        "reply_markup": reply_markup,
                    }
                )
            data = resp.json()
            if not data.get("ok"):
                LOGGER.warning(f"[UpdatePost] Bot API sendPhoto failed: {data.get('description')}")
                return None
            return data["result"]["message_id"]
    except Exception as e:
        LOGGER.warning(f"[UpdatePost] Bot API sendPhoto error: {e}")
        return None


async def _bot_api_edit_message(chat_id: int, message_id: int, text: str,
                                 entities: list, reply_markup: dict) -> bool:
    """httpx se Bot API editMessageText — styled buttons update ke liye."""
    if not _BOT_TOKEN:
        return False
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{_BOT_TOKEN}/editMessageText",
                json={
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": text,
                    "entities": entities,
                    "reply_markup": reply_markup,
                }
            )
            data = resp.json()
            if not data.get("ok"):
                LOGGER.warning(f"[BotUpload] Bot API editMessage failed: {data.get('description')}")
                return False
            return True
    except Exception as e:
        LOGGER.warning(f"[BotUpload] Bot API edit failed: {e}")
        return False

async def _bot_api_edit_reply_markup(chat_id: int, message_id: int, reply_markup: dict) -> bool:
    """
    httpx se Bot API editMessageReplyMarkup — sirf buttons change karo, caption/text
    bilkul untouched rehta hai. Existing posts (jo /bot_upload se nahi bani) ke
    quality buttons update karne ke liye use hota hai.
    """
    if not _BOT_TOKEN:
        return False
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{_BOT_TOKEN}/editMessageReplyMarkup",
                json={
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "reply_markup": reply_markup,
                }
            )
            data = resp.json()
            if not data.get("ok"):
                LOGGER.warning(f"[BotUpload] Bot API editReplyMarkup failed: {data.get('description')}")
                return False
            return True
    except Exception as e:
        LOGGER.warning(f"[BotUpload] Bot API editReplyMarkup error: {e}")
        return False



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

    def _caption(self) -> tuple:
        s = f"{self.season_num:02d}"
        e = f"{self.episode_num:02d}"
        return _make_caption_with_entity(f"Season {s} Episode {e} {self.language}")

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
            row.append(_quality_button_dict(low_q, self._buttons[low_q], "low"))

        # 720p aur 1080p — sirf tab dikhao jab uploaded ho
        for q in ["720p", "1080p"]:
            if q in self._buttons:
                row.append(_quality_button_dict(q, self._buttons[q], q))

        # Bot API format: {"inline_keyboard": [[btn, btn], ...]}
        return {"inline_keyboard": [row]} if row else None

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
            caption_text, caption_entities = self._caption()

            keyboard = self._keyboard()

            if self.post_msg_id is None:
                # Naya message banao — Bot API se (styled buttons + custom emoji)
                # Response mein seedha message_id milta hai — race condition nahi
                msg_id = await _bot_api_send_message(
                    self.channel_id, caption_text,
                    caption_entities or [], keyboard or {"inline_keyboard": []},
                )
                if msg_id:
                    self.post_msg_id = msg_id
                else:
                    # Pyrogram fallback
                    try:
                        pyrogram_kb = None
                        if keyboard:
                            row = [InlineKeyboardButton(text=b.get("text",""), url=b.get("url",""))
                                   for b in keyboard["inline_keyboard"][0]]
                            pyrogram_kb = InlineKeyboardMarkup([row])
                        sent = await self.client.send_message(
                            chat_id=self.channel_id, text=caption_text,
                            reply_markup=pyrogram_kb,
                            parse_mode=ParseMode.DISABLED, disable_web_page_preview=True,
                        )
                        self.post_msg_id = sent.id
                    except Exception as e:
                        LOGGER.error(f"[BotUpload] Post create error: {e}")
                await self._save_to_db()
            else:
                # Existing message edit karo (resume mode) — Bot API se
                success = await _bot_api_edit_message(
                    self.channel_id, self.post_msg_id, caption_text,
                    caption_entities or [], keyboard or {"inline_keyboard": []},
                )
                if not success:
                    # Pyrogram fallback
                    try:
                        pyrogram_kb = None
                        if keyboard:
                            row = [InlineKeyboardButton(text=b.get("text",""), url=b.get("url",""))
                                   for b in keyboard["inline_keyboard"][0]]
                            pyrogram_kb = InlineKeyboardMarkup([row])
                        await self.client.edit_message_text(
                            chat_id=self.channel_id, message_id=self.post_msg_id,
                            text=caption_text, reply_markup=pyrogram_kb,
                            parse_mode=ParseMode.DISABLED, disable_web_page_preview=True,
                        )
                    except Exception as e:
                        LOGGER.error(f"[BotUpload] Post edit error: {e}")
                await self._save_to_db()


# ─────────────────────────────────────────────────────────────────────────
#  Existing-post quality-button editor (for /bot_upload <post_link> mode)
#  Kisi bhi existing channel post ke quality buttons ko same-slot-replace
#  logic se update karta hai — 360p/480p ek slot (sirf ek dikhega),
#  720p alag slot, 1080p alag slot. Baaki buttons (non-quality) untouched.
# ─────────────────────────────────────────────────────────────────────────
_QUALITY_RE = re.compile(r"\b(360p|480p|720p|1080p|2160p)\b", re.IGNORECASE)


def _quality_slot_for(quality: str) -> str:
    """360p/480p => 'low' slot; 720p/1080p => apna hi slot."""
    q = quality.lower()
    if q in ("360p", "480p"):
        return "low"
    return q


def _button_quality_slot(button: dict) -> str | None:
    """Button ke text se quality nikal ke uska slot batao (None agar quality button nahi hai)."""
    text = button.get("text", "")
    m = _QUALITY_RE.search(text)
    if not m:
        return None
    return _quality_slot_for(m.group(1).lower())


async def _fetch_message_buttons(client: Client, chat_id: int, msg_id: int) -> list:
    """
    Existing message ka current inline_keyboard nikalo (list of rows, har row list of
    {"text", "url"} dicts). Bot API se direct fetch ka koi method nahi hai isliye
    Pyrogram ke get_messages se reply_markup parse karte hain.
    """
    try:
        msg = await client.get_messages(chat_id, msg_id)
    except Exception as e:
        LOGGER.warning(f"[BotUpload] get_messages failed for {chat_id}/{msg_id}: {e}")
        return []

    if not msg or not msg.reply_markup or not getattr(msg.reply_markup, "inline_keyboard", None):
        return []

    rows = []
    for row in msg.reply_markup.inline_keyboard:
        new_row = []
        for btn in row:
            new_row.append({"text": btn.text or "", "url": btn.url or ""})
        rows.append(new_row)
    return rows


async def update_existing_post_button(client: Client, chat_id: int, msg_id: int,
                                       quality: str, deep_link_url: str) -> bool:
    """
    Kisi existing channel post (jo /bot_upload se nahi bani thi, jaise koi
    pehle se mojood post) ke quality buttons ko same-slot-replace logic se
    update karta hai:
      - 360p/480p ek slot share karte hain — naya jo bhi ho (360p ya 480p),
        purana 360p/480p button (jo bhi tha) hat jaayega, naya add hoga.
      - 720p apna alag slot, 1080p apna alag slot — same tarah replace.
      - Quality se related na ho wo koi bhi button (e.g. "Join Channel")
        bilkul untouched rehta hai.
    Returns True agar edit successful hua.
    """
    if quality == "2160p":
        return False

    new_slot = _quality_slot_for(quality)
    rows = await _fetch_message_buttons(client, chat_id, msg_id)

    # Quality row dhoondo — jis row mein koi bhi quality-button ho, wahi
    # quality-row treat karenge (typically ek hi row hota hai). Agar kahin
    # quality button nahi mila, naya row banake end mein add karenge.
    quality_row_idx = None
    other_rows = []
    quality_row = []

    for idx, row in enumerate(rows):
        if any(_button_quality_slot(b) is not None for b in row):
            quality_row_idx = idx
            quality_row = row
        else:
            other_rows.append((idx, row))

    # Quality row se: same slot wala purana button hatao, baaki rakho
    kept = [b for b in quality_row if _button_quality_slot(b) != new_slot]
    new_button = _quality_button_dict(quality, deep_link_url, new_slot)
    kept.append(new_button)

    # Order maintain karo: low -> 720p -> 1080p
    order = {"low": 0, "720p": 1, "1080p": 2}
    kept.sort(key=lambda b: order.get(_button_quality_slot(b), 99))

    # Final rows rebuild — quality row ko apni original position pe rakho
    final_rows = [r for _, r in other_rows]
    if quality_row_idx is not None:
        final_rows.insert(min(quality_row_idx, len(final_rows)), kept)
    else:
        final_rows.append(kept)

    keyboard = {"inline_keyboard": final_rows}

    success = await _bot_api_edit_reply_markup(chat_id, msg_id, keyboard)
    if success:
        return True

    # Pyrogram fallback — koi style/emoji nahi, sirf text+url
    try:
        pyrogram_rows = [
            [InlineKeyboardButton(text=b["text"], url=b["url"]) for b in row]
            for row in final_rows
        ]
        await client.edit_message_reply_markup(
            chat_id=chat_id, message_id=msg_id,
            reply_markup=InlineKeyboardMarkup(pyrogram_rows) if pyrogram_rows else None,
        )
        return True
    except Exception as e:
        LOGGER.error(f"[BotUpload] update_existing_post_button fallback failed: {e}")
        return False


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
    caption_text, caption_entities = _make_caption_with_entity(f"Season {s} Full Batch {language}")

    row = []
    low_q = "360p" if "360p" in batch_links else ("480p" if "480p" in batch_links else None)
    if low_q:
        row.append(_quality_button_dict(low_q, batch_links[low_q], "low"))
    for q in ["720p", "1080p"]:
        if q in batch_links:
            row.append(_quality_button_dict(q, batch_links[q], q))

    keyboard = {"inline_keyboard": [row]} if row else {"inline_keyboard": []}

    # Bot API se send karo — styled buttons + premium emoji
    # Response mein seedha message_id milta hai
    msg_id = await _bot_api_send_message(channel_id, caption_text, caption_entities, keyboard)
    if msg_id:
        return msg_id

    # Pyrogram fallback
    try:
        pyrogram_kb = None
        if row:
            pyrogram_row = [InlineKeyboardButton(text=b["text"], url=b["url"]) for b in row]
            pyrogram_kb = InlineKeyboardMarkup([pyrogram_row])
        sent = await client.send_message(
            chat_id=channel_id,
            text=f"<b>➲ Season {s} Full Batch {language}</b>",
            reply_markup=pyrogram_kb,
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
