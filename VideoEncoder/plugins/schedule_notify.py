"""
schedule_notify.py  v3
=======================
Episode Schedule + End Message + Update Channel + Broadcast System

Flow (episode complete hone ke baad):
  1. Purane schedule/end messages delete karo (last 15 msgs mein se, videos nahi)
  2. Schedule message post karo → "Next episode upload on 4th June"
     (Last episode pe → "END" post hoga, end messages nahi)
  3. End messages post karo (ek ek karke, saare saved messages)
  4. Saare registered update channels pe broadcast bhejo

Commands:
  /schedule [days] [total_eps] [Anime Name]
      Example: /schedule 7 12 Witch Hat Atelier

  /end_message
      → Default end message set karo (sab channels pe apply)
  /end_message [Channel Name]
      → Sirf us channel ke liye custom end message set karo
      → /done se save karo

  /end_message_preview           → Default end messages preview
  /end_message_preview [Name]    → Channel-specific preview
  /end_message_del [Name]        → Delete karo

  /update_channel [channel_id]   → Broadcast update channel add karo
  /update_channel_list           → Saare update channels dekho
  /update_channel_del [number]   → Remove karo

  /broadcast_message             → Manual broadcast bhejo
      → Bot poochega: anime name, hashtag (optional), channel link

  /schedule_list   → Saare schedules dekho
  /schedule_del [Anime Name]
"""

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

from pyrogram import Client, filters
from pyrogram.types import Message

from pyrogram.errors import UserNotParticipant

from .. import LOGGER, app, owner, sudo_users, api_id, api_hash
from ..utils.database.access_db import db

IST = timezone(timedelta(hours=5, minutes=30))

# In-memory state: kaun abhi end_message recording mode mein hai
# { user_id: { 'channel_key': str, 'messages': [ {type, content} ] } }
# channel_key = '' means default, else channel name/key
_recording_state: dict = {}

# Broadcast conversation state
# { user_id: { 'step': 'name'|'hashtag'|'link', 'anime_name': str, 'hashtag': str } }
_broadcast_state: dict = {}


def _is_authorized(user_id: int) -> bool:
    return user_id in owner or user_id in sudo_users


def _normalize(text: str) -> str:
    return re.sub(r'[^a-z0-9]', '', text.lower())


def _ordinal(n: int) -> str:
    if 11 <= (n % 100) <= 13:
        return f"{n}th"
    return f"{n}{['th','st','nd','rd','th','th','th','th','th','th'][n % 10]}"


def _next_episode_date(interval_days: int) -> str:
    today_ist = datetime.now(IST)
    next_date = today_ist + timedelta(days=interval_days)
    return f"{_ordinal(next_date.day)} {next_date.strftime('%B')}"


# ─────────────────────────────────────────────
#  DB Helpers
# ─────────────────────────────────────────────
async def _owner_id() -> int | None:
    return owner[0] if owner else None


async def _get_schedule_list() -> list:
    oid = await _owner_id()
    if not oid:
        return []
    user = await db._get_user(oid)
    return user.get('episode_schedule_list', [])


async def _save_schedule_list(slist: list):
    oid = await _owner_id()
    if not oid:
        return
    await db.col.update_one({'id': oid}, {'$set': {'episode_schedule_list': slist}}, upsert=True)


async def _get_schedule_for_anime(anime_name: str) -> dict | None:
    slist = await _get_schedule_list()
    name_norm = _normalize(anime_name)
    best, best_len = None, 0
    for entry in slist:
        en = _normalize(entry.get('anime_name', ''))
        if en and en in name_norm and len(en) > best_len:
            best, best_len = entry, len(en)
    return best


# ─────────────────────────────────────────────
#  End Messages DB
#  DB structure:
#    end_messages_map = {
#      '__default__': [...],   ← default (sab channels ke liye)
#      'channelkey':  [...],   ← specific channel ke liye
#    }
# ─────────────────────────────────────────────
DEFAULT_END_KEY = '__default__'


async def _get_end_messages(channel_key: str = DEFAULT_END_KEY) -> list:
    """
    Return saved end messages.
    channel_key = DEFAULT_END_KEY  → default messages
    channel_key = 'somename'       → channel-specific messages
    Priority: channel-specific → default → []
    """
    oid = await _owner_id()
    if not oid:
        return []
    user = await db._get_user(oid)
    end_map = user.get('end_messages_map', {})
    key = _normalize(channel_key) if channel_key != DEFAULT_END_KEY else DEFAULT_END_KEY
    # Channel-specific check
    if key != DEFAULT_END_KEY and key in end_map:
        return end_map[key]
    # Default fallback
    return end_map.get(DEFAULT_END_KEY, [])


async def _get_end_messages_raw(channel_key: str) -> list:
    """Sirf us specific key ke messages lo — fallback nahi."""
    oid = await _owner_id()
    if not oid:
        return []
    user = await db._get_user(oid)
    end_map = user.get('end_messages_map', {})
    key = _normalize(channel_key) if channel_key != DEFAULT_END_KEY else DEFAULT_END_KEY
    return end_map.get(key, [])


async def _save_end_messages(channel_key: str, messages: list):
    oid = await _owner_id()
    if not oid:
        return
    user = await db._get_user(oid)
    end_map = user.get('end_messages_map', {})
    key = _normalize(channel_key) if channel_key != DEFAULT_END_KEY else DEFAULT_END_KEY
    end_map[key] = messages
    await db.col.update_one({'id': oid}, {'$set': {'end_messages_map': end_map}}, upsert=True)


async def _delete_end_messages_db(channel_key: str):
    oid = await _owner_id()
    if not oid:
        return
    user = await db._get_user(oid)
    end_map = user.get('end_messages_map', {})
    key = _normalize(channel_key) if channel_key != DEFAULT_END_KEY else DEFAULT_END_KEY
    end_map.pop(key, None)
    await db.col.update_one({'id': oid}, {'$set': {'end_messages_map': end_map}}, upsert=True)


# ─────────────────────────────────────────────
#  Update Channels DB
#  List of channels jahan episode upload hone pe broadcast jayega
# ─────────────────────────────────────────────
async def _get_update_channels() -> list:
    oid = await _owner_id()
    if not oid:
        return []
    user = await db._get_user(oid)
    return user.get('update_channels', [])


async def _save_update_channels(channels: list):
    oid = await _owner_id()
    if not oid:
        return
    await db.col.update_one({'id': oid}, {'$set': {'update_channels': channels}}, upsert=True)


# ─────────────────────────────────────────────
#  Anime Broadcast Info DB
#  Per-anime hashtag + channel_link save karo
#  { 'animenamekey': {'hashtag': '...', 'channel_link': '...'}, ... }
# ─────────────────────────────────────────────
async def _get_anime_broadcast_info(anime_name: str) -> dict | None:
    oid = await _owner_id()
    if not oid:
        return None
    user = await db._get_user(oid)
    bmap = user.get('anime_broadcast_map', {})
    return bmap.get(_normalize(anime_name))


async def _save_anime_broadcast_info(anime_name: str, hashtag: str, channel_link: str):
    oid = await _owner_id()
    if not oid:
        return
    user = await db._get_user(oid)
    bmap = user.get('anime_broadcast_map', {})
    bmap[_normalize(anime_name)] = {
        'anime_name': anime_name,
        'hashtag': hashtag,
        'channel_link': channel_link,
    }
    await db.col.update_one({'id': oid}, {'$set': {'anime_broadcast_map': bmap}}, upsert=True)


# ─────────────────────────────────────────────
#  Button serializer helper
# ─────────────────────────────────────────────
def _serialize_buttons(msg: Message) -> list | None:
    """
    Message ke inline keyboard buttons serialize karo.
    List of rows → har row list of {text, url/callback_data}
    """
    if not msg.reply_markup:
        return None
    try:
        rows = []
        for row in msg.reply_markup.inline_keyboard:
            btn_row = []
            for btn in row:
                b = {'text': btn.text}
                if btn.url:
                    b['url'] = btn.url
                elif btn.callback_data:
                    b['callback_data'] = btn.callback_data
                btn_row.append(b)
            rows.append(btn_row)
        return rows if rows else None
    except Exception:
        return None


def _deserialize_buttons(rows: list):
    """Saved button rows se InlineKeyboardMarkup banao."""
    from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    kb_rows = []
    for row in rows:
        kb_row = []
        for b in row:
            if 'url' in b:
                kb_row.append(InlineKeyboardButton(b['text'], url=b['url']))
            elif 'callback_data' in b:
                kb_row.append(InlineKeyboardButton(b['text'], callback_data=b['callback_data']))
            else:
                kb_row.append(InlineKeyboardButton(b['text'], callback_data='noop'))
        kb_rows.append(kb_row)
    return InlineKeyboardMarkup(kb_rows)


# ─────────────────────────────────────────────
#  Message serializer — save karte waqt
# ─────────────────────────────────────────────
def _serialize_message(msg: Message) -> dict | None:
    """
    Message ko saveable dict mein convert karo.
    chat_id + message_id + buttons (agar hain) save karo.
    Send karte waqt copy_message + reply_markup use hoga.
    """
    item = {
        'type': 'copy_ref',
        'from_chat_id': msg.chat.id,
        'message_id': msg.id,
    }
    # Buttons separately serialize karo — copy_message buttons preserve nahi karta
    buttons = _serialize_buttons(msg)
    if buttons:
        item['buttons'] = buttons
    return item


# ─────────────────────────────────────────────
#  Send saved end messages to channel
# ─────────────────────────────────────────────
async def _send_end_messages_to_channel(channel_id: int, channel_key: str = DEFAULT_END_KEY):
    """
    DB se saved end messages ek ek karke channel pe bhejo.
    channel_key ke liye specific messages check karo,
    nahi toh default use karo.
    """
    messages = await _get_end_messages(channel_key)
    if not messages:
        LOGGER.info(f"[EndMsg] No end messages for key='{channel_key}', trying default...")
        messages = await _get_end_messages(DEFAULT_END_KEY)
    if not messages:
        LOGGER.info(f"[EndMsg] No end messages at all for channel {channel_id}")
        return

    LOGGER.info(f"[EndMsg] Sending {len(messages)} end messages to {channel_id}")

    for item in messages:
        try:
            msg_type = item.get('type')

            if msg_type in ('copy_ref', 'forward'):
                # Saved buttons agar hain toh manually attach karo
                reply_markup = None
                if item.get('buttons'):
                    reply_markup = _deserialize_buttons(item['buttons'])
                await app.copy_message(
                    chat_id=channel_id,
                    from_chat_id=item['from_chat_id'],
                    message_id=item['message_id'],
                    reply_markup=reply_markup,
                )
            elif msg_type == 'sticker':
                # Sticker copy_message se nahi hota — file_id se bhejo
                await app.send_sticker(channel_id, item['file_id'])
            elif msg_type == 'text':
                await app.send_message(channel_id, item['text'])
            elif msg_type == 'photo':
                await app.send_photo(channel_id, item['file_id'], caption=item.get('caption', ''))
            elif msg_type == 'video':
                await app.send_video(channel_id, item['file_id'], caption=item.get('caption', ''))
            elif msg_type == 'animation':
                await app.send_animation(channel_id, item['file_id'], caption=item.get('caption', ''))
            elif msg_type == 'document':
                await app.send_document(channel_id, item['file_id'], caption=item.get('caption', ''))
            elif msg_type == 'audio':
                await app.send_audio(channel_id, item['file_id'], caption=item.get('caption', ''))

            await asyncio.sleep(0.5)

        except Exception as e:
            LOGGER.error(f"[EndMsg] Failed to send end message item {item}: {e}")


# ─────────────────────────────────────────────
#  Cleanup — last 50 msgs scan karke purane 3 non-media messages delete karo
#  (videos/documents/photos nahi — sirf text/sticker/animation messages)
# ─────────────────────────────────────────────
async def cleanup_old_notifications(
    channel_id: int,
    anime_name: str,
    user_session: str | None = None,
) -> int:
    """
    Cleanup (FIXED):
      - Channel ke last 50 messages scan karo
      - SKIP: video/document/photo/audio (actual media content)
      - SKIP: text mein "end", "season", "finale" (important msgs)
      - DELETE: last 3 non-media, non-protected messages
        (sticker, animation, plain text — koi bhi ho)
      - user_session diya → user account se delete (purane msgs bhi jaayenge)
      - user_session nahi → bot se delete (sirf bot ke messages)
      - Returns: deleted count
    """
    # Yeh keywords wale messages KABHI delete NAHI honge
    PROTECTED_KEYWORDS = [
        "end",
        "season",
        "finale",
    ]

    # Yeh media types kabhi delete nahi honge (actual content)
    SKIP_MEDIA_TYPES = {"video", "document", "photo", "audio"}

    # ── User client banao agar session diya ──
    user_client = None
    if user_session:
        try:
            from pyrogram import Client as _Client
            user_client = _Client(
                "cleanup_user",
                session_string=user_session,
                api_id=api_id,
                api_hash=api_hash,
                in_memory=True,
            )
            await user_client.connect()
            LOGGER.info("[Cleanup] User client connected — user account se delete hoga")
        except Exception as ue:
            LOGGER.warning(f"[Cleanup] User client connect failed: {ue} — bot se try karega")
            user_client = None

    # scan_client: messages fetch karne ke liye (app ya user_client)
    scan_client = user_client if user_client else app
    # delete_client: messages delete karne ke liye
    delete_client = user_client if user_client else app

    try:
        to_delete = []
        skipped   = []

        LOGGER.info(f"[Cleanup-DEBUG] Channel {channel_id} ke messages scan shuru...")

        async for msg in scan_client.get_chat_history(channel_id, limit=50):
            # Agar 3 messages mil gaye toh scan band karo
            if len(to_delete) >= 3:
                break

            # Message type detect karo
            msg_type = "text"
            if msg.video:       msg_type = "video"
            elif msg.document:  msg_type = "document"
            elif msg.photo:     msg_type = "photo"
            elif msg.audio:     msg_type = "audio"
            elif msg.sticker:   msg_type = "sticker"
            elif msg.animation: msg_type = "animation"

            msg_text = (msg.text or msg.caption or "").lower()
            msg_preview = msg_text[:60].replace('\n', ' ') if msg_text else "[no text]"

            LOGGER.info(
                f"[Cleanup-DEBUG] msg_id={msg.id} | type={msg_type} | "
                f"text='{msg_preview}'"
            )

            # Heavy media messages skip — yeh actual content hai
            if msg_type in SKIP_MEDIA_TYPES:
                skipped.append(f"msg {msg.id} [media={msg_type}]")
                LOGGER.info(f"[Cleanup-DEBUG] msg {msg.id} → SKIP (media={msg_type})")
                continue

            # Protected keywords wale skip
            if any(kw in msg_text for kw in PROTECTED_KEYWORDS):
                matched_kw = [kw for kw in PROTECTED_KEYWORDS if kw in msg_text]
                skipped.append(f"msg {msg.id} [protected={matched_kw}]")
                LOGGER.info(f"[Cleanup-DEBUG] msg {msg.id} → SKIP (protected: {matched_kw})")
                continue

            # Baaki sab (text, sticker, animation) — delete list mein
            to_delete.append(msg.id)
            LOGGER.info(f"[Cleanup-DEBUG] msg {msg.id} → DELETE (type={msg_type})")

        LOGGER.info(f"[Cleanup] to_delete={to_delete} | skipped={skipped}")

        if not to_delete:
            LOGGER.info("[Cleanup] Koi deletable message nahi mila")
            return 0

        deleted = 0
        for msg_id in to_delete:
            try:
                await delete_client.delete_messages(channel_id, msg_id)
                deleted += 1
                LOGGER.info(f"[Cleanup] Deleted msg_id={msg_id}")
                await asyncio.sleep(0.3)
            except Exception as e:
                LOGGER.warning(f"[Cleanup] Could not delete {msg_id}: {e}")

        LOGGER.info(f"[Cleanup] deleted={deleted} | skipped={len(skipped)}")
        return deleted
    except Exception as e:
        LOGGER.error(f"[Cleanup] Failed: {e}")
        return 0
    finally:
        # User client disconnect karo
        if user_client:
            try:
                await user_client.disconnect()
            except Exception:
                pass


# ─────────────────────────────────────────────
#  Main function — auto_monitor.py se call hoga
# ─────────────────────────────────────────────
async def send_schedule_notification(
    client: Client,
    channel_id: int,
    anime_name: str,
    episode_num: int,
    channel_key: str = DEFAULT_END_KEY,
):
    """
    Episode complete hone ke baad call karo.
    Flow:
      1. Purane schedule/end msgs delete karo
      2. Schedule msg post karo
      3. End messages post karo (last episode pe nahi — END ke baad kuch nahi)
    channel_key: channel-specific end messages ke liye (default = DEFAULT_END_KEY)
    """
    schedule = await _get_schedule_for_anime(anime_name)
    if not schedule:
        LOGGER.info(f"[Schedule] No schedule for '{anime_name}', skipping.")
        return

    interval_days = schedule.get('interval_days', 7)
    total_eps     = schedule.get('total_eps', 0)
    is_last_ep    = total_eps > 0 and episode_num >= total_eps

    # Step 2: Schedule message
    try:
        if is_last_ep:
            await app.send_message(channel_id, "**END**")
            LOGGER.info(f"[Schedule] Last ep {episode_num} → posted END for '{anime_name}'")
            # Last episode pe end messages nahi bhejte
            return
        else:
            next_date = _next_episode_date(interval_days)
            await app.send_message(channel_id, f"**Next episode upload on {next_date}**")
            LOGGER.info(f"[Schedule] Ep {episode_num} → Next on {next_date} for '{anime_name}'")
    except Exception as e:
        LOGGER.error(f"[Schedule] Schedule msg failed: {e}")
        return

    await asyncio.sleep(0.5)

    # Step 3: End messages bhejo
    await _send_end_messages_to_channel(channel_id, channel_key)


# ─────────────────────────────────────────────
#  /end_message command — recording mode start
#  /end_message          → default set karo
#  /end_message SonyYay  → SonyYay channel ke liye custom set karo
# ─────────────────────────────────────────────
@Client.on_message(filters.command("end_message") & filters.private)
async def cmd_end_message(client: Client, message: Message):
    if not _is_authorized(message.from_user.id):
        return

    parts = message.text.split(None, 1)
    # Agar koi name nahi diya → default set karo
    if len(parts) < 2 or not parts[1].strip():
        channel_key = DEFAULT_END_KEY
        display_name = "🌐 Default (sab channels pe apply hoga)"
    else:
        channel_key = parts[1].strip()
        display_name = f"📢 Channel: **{channel_key}**"

    user_id = message.from_user.id

    _recording_state[user_id] = {
        'channel_key': channel_key,
        'messages': []
    }

    existing = await _get_end_messages_raw(channel_key)
    note = f"\n\n⚠️ Pehle se **{len(existing)}** messages saved hain — naye se replace ho jayenge." if existing else ""

    await message.reply(
        f"🎬 **End Message Recording Started!**\n\n"
        f"{display_name}{note}\n\n"
        f"Ab jo bhi bhejoge — text, sticker, forward, photo — sab save hoga.\n\n"
        f"✅ Khatam karne ke liye: `/done`\n"
        f"❌ Cancel karne ke liye: `/cancel_end`"
    )


# ─────────────────────────────────────────────
#  Unified capture handler
#  Broadcast state + Recording state dono yahan handle hote hain.
#  Do alag handlers same filter pe register karne se Pyrogram conflict
#  karta hai — isliye ek hi handler mein merge kiya.
# ─────────────────────────────────────────────
@Client.on_message(
    filters.private &
    ~filters.command(["done", "cancel_end", "end_message", "schedule",
                      "schedule_list", "schedule_del", "end_message_preview",
                      "end_message_del", "update_channel", "update_channel_list",
                      "update_channel_del", "broadcast_message", "cancel_broadcast",
                      "confirm_broadcast"])
)
async def capture_any_state(client: Client, message: Message):
    """Pehle broadcast state check karo, phir recording state."""
    user_id = message.from_user.id
    if not _is_authorized(user_id):
        return

    # ── Priority 1: Broadcast state ──
    if user_id in _broadcast_state:
        state = _broadcast_state[user_id]
        text = (message.text or "").strip()

        if state['step'] == 'name':
            if not text:
                await message.reply("⚠️ Anime ka naam bhejo!")
                return
            state['anime_name'] = text
            state['step'] = 'hashtag'
            await message.reply(
                f"✅ Anime: **{text}**\n\n"
                f"**Step 2/3** — Hashtag bhejo (ya `skip` likho):\n\n"
                f"_Example: #official\\_hindi\\_dub_"
            )

        elif state['step'] == 'hashtag':
            if text.lower() == 'skip':
                state['hashtag'] = ''
            else:
                state['hashtag'] = text if text.startswith('#') else f"#{text}"
            state['step'] = 'link'
            hashtag_info = f"Hashtag: **{state['hashtag']}**\n\n" if state['hashtag'] else "Hashtag: _skipped_\n\n"
            await message.reply(
                f"✅ {hashtag_info}"
                f"**Step 3/3** — Channel ka link bhejo:\n\n"
                f"_Example: https://t.me/yourchannel_"
            )

        elif state['step'] == 'link':
            if not text.startswith('http'):
                await message.reply("⚠️ Valid channel link bhejo! (https://t.me/...)")
                return
            channel_link = text
            anime_name   = state['anime_name']
            hashtag      = state['hashtag']
            _broadcast_state.pop(user_id)

            preview_lines = [f"**🔰 {anime_name}**"]
            if hashtag:
                preview_lines.append(f"**{hashtag}**")
            preview_lines.append("")
            preview_lines.append("**📍Season XX Episode XX Added...!**")
            watch_line = f"**[📌𝙒𝘼𝙏𝘾𝙃 & 𝘿𝙊𝙒𝙉𝙇𝙊𝘼𝘿📌]({channel_link})**"
            preview_lines.append(watch_line)
            preview_lines.append(watch_line)

            channels = await _get_update_channels()
            _broadcast_state[user_id] = {
                'step': 'confirm',
                'anime_name': anime_name,
                'hashtag': hashtag,
                'channel_link': channel_link,
            }
            await message.reply(
                f"📋 **Broadcast Preview:**\n\n"
                f"{'━' * 20}\n"
                f"{chr(10).join(preview_lines)}\n"
                f"{'━' * 20}\n\n"
                f"📢 **{len(channels)}** update channels pe broadcast hoga.\n\n"
                f"✅ Bhejne ke liye: `/confirm_broadcast`\n"
                f"❌ Cancel: `/cancel_broadcast`",
                disable_web_page_preview=True,
            )

        elif state['step'] == 'confirm':
            await message.reply("✅ `/confirm_broadcast` bhejo ya ❌ `/cancel_broadcast`")

        return  # broadcast handle ho gaya

    # ── Priority 2: End message recording state ──
    if user_id not in _recording_state:
        return

    state = _recording_state[user_id]

    if message.sticker:
        item = {'type': 'sticker', 'file_id': message.sticker.file_id}
        state['messages'].append(item)
        count = len(state['messages'])
        await message.reply(f"✅ Saved! ({count} messages total) — `/done` se khatam karo")
        return

    item = _serialize_message(message)
    if item:
        state['messages'].append(item)
        count = len(state['messages'])
        await message.reply(f"✅ Saved! ({count} messages total) — `/done` se khatam karo")
    else:
        await message.reply("⚠️ Kuch save nahi ho saka. Dobara try karo.")


# ─────────────────────────────────────────────
#  /done — save karo
# ─────────────────────────────────────────────
@Client.on_message(filters.command("done") & filters.private)
async def cmd_done(client: Client, message: Message):
    user_id = message.from_user.id
    if not _is_authorized(user_id):
        return

    if user_id not in _recording_state:
        await message.reply("⚠️ Koi recording chal nahi rahi. Pehle `/end_message` ya `/end_message ChannelName` karo.")
        return

    state = _recording_state.pop(user_id)
    channel_key = state['channel_key']
    messages    = state['messages']

    if not messages:
        await message.reply("⚠️ Koi message save nahi hua! Recording cancel ho gaya.")
        return

    await _save_end_messages(channel_key, messages)

    if channel_key == DEFAULT_END_KEY:
        target_info = "🌐 **Default** — sab channels pe apply hoga"
    else:
        target_info = f"📢 Channel: **{channel_key}**"

    await message.reply(
        f"✅ **End Messages Saved!**\n\n"
        f"{target_info}\n"
        f"💾 Total: **{len(messages)}** messages saved\n\n"
        f"Har episode upload ke baad yeh messages automatically post honge."
    )


# ─────────────────────────────────────────────
#  /cancel_end — recording mode cancel
# ─────────────────────────────────────────────
@Client.on_message(filters.command("cancel_end") & filters.private)
async def cmd_cancel_end(client: Client, message: Message):
    user_id = message.from_user.id
    if not _is_authorized(user_id):
        return

    if user_id not in _recording_state:
        await message.reply("⚠️ Koi recording chal nahi rahi.")
        return

    state = _recording_state.pop(user_id)
    channel_key = state['channel_key']
    target = "🌐 Default" if channel_key == DEFAULT_END_KEY else f"📢 {channel_key}"
    await message.reply(
        f"❌ **Recording Cancelled!**\n\n"
        f"{target}\n"
        f"💾 {len(state['messages'])} messages discard ho gaye."
    )


# ─────────────────────────────────────────────
#  /schedule — anime ka episode schedule set karo
# ─────────────────────────────────────────────
@Client.on_message(filters.command("schedule") & filters.private)
async def cmd_schedule(client: Client, message: Message):
    """
    /schedule [days] [total_eps] [Anime Name]
    Example: /schedule 7 12 Witch Hat Atelier
    """
    if not _is_authorized(message.from_user.id):
        return

    parts = message.text.split(None, 3)
    if len(parts) < 4:
        await message.reply(
            "**Usage:**\n"
            "`/schedule [days] [total_eps] [Anime Name]`\n\n"
            "**Example:**\n"
            "`/schedule 7 12 Witch Hat Atelier`\n\n"
            "📅 `days` = kitne din baad next episode aata hai\n"
            "🔢 `total_eps` = total episodes (0 = unlimited)\n"
            "📺 `Anime Name` = anime ka naam"
        )
        return

    try:
        interval_days = int(parts[1])
        total_eps = int(parts[2])
    except ValueError:
        await message.reply("❌ `days` aur `total_eps` numbers hone chahiye!\nExample: `/schedule 7 12 Anime Name`")
        return

    anime_name = parts[3].strip()

    slist = await _get_schedule_list()

    # Already exists? Update karo
    for entry in slist:
        if _normalize(entry.get('anime_name', '')) == _normalize(anime_name):
            entry['interval_days'] = interval_days
            entry['total_eps'] = total_eps
            await _save_schedule_list(slist)
            await message.reply(
                f"✅ **Schedule Updated!**\n\n"
                f"📺 **Anime:** {anime_name}\n"
                f"📅 **Interval:** {interval_days} days\n"
                f"🔢 **Total Episodes:** {total_eps if total_eps > 0 else 'Unlimited'}"
            )
            return

    slist.append({
        'anime_name': anime_name,
        'interval_days': interval_days,
        'total_eps': total_eps,
    })
    await _save_schedule_list(slist)

    await message.reply(
        f"✅ **Schedule Set!**\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📺 **Anime:** {anime_name}\n"
        f"📅 **Next Episode In:** {interval_days} days\n"
        f"🔢 **Total Episodes:** {total_eps if total_eps > 0 else 'Unlimited'}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Har episode ke baad automatically schedule message post hoga! 🚀"
    )


# ─────────────────────────────────────────────
#  /schedule_list — saare schedules dekho
# ─────────────────────────────────────────────
@Client.on_message(filters.command("schedule_list") & filters.private)
async def cmd_schedule_list(client: Client, message: Message):
    if not _is_authorized(message.from_user.id):
        return

    slist = await _get_schedule_list()

    if not slist:
        await message.reply(
            "📋 **Schedule List Empty!**\n\n"
            "Koi schedule set nahi hai.\n"
            "Set karne ke liye: `/schedule [days] [total_eps] [Anime Name]`"
        )
        return

    text = "📋 **Schedule List:**\n\n"
    for i, entry in enumerate(slist, 1):
        name = entry.get('anime_name', 'Unknown')
        days = entry.get('interval_days', 7)
        total = entry.get('total_eps', 0)
        total_str = str(total) if total > 0 else '∞'
        text += (
            f"**{i}.** 📺 {name}\n"
            f"    📅 Every {days} days | 🔢 {total_str} eps\n\n"
        )

    text += f"Total: **{len(slist)}** schedules"
    await message.reply(text)


# ─────────────────────────────────────────────
#  /schedule_del — schedule delete karo
# ─────────────────────────────────────────────
@Client.on_message(filters.command("schedule_del") & filters.private)
async def cmd_schedule_del(client: Client, message: Message):
    """
    /schedule_del [Anime Name]
    Example: /schedule_del Witch Hat Atelier
    """
    if not _is_authorized(message.from_user.id):
        return

    parts = message.text.split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply(
            "**Usage:** `/schedule_del [Anime Name]`\n"
            "Example: `/schedule_del Witch Hat Atelier`"
        )
        return

    anime_name = parts[1].strip()
    slist = await _get_schedule_list()
    name_norm = _normalize(anime_name)

    new_list = [e for e in slist if _normalize(e.get('anime_name', '')) != name_norm]

    if len(new_list) == len(slist):
        await message.reply(
            f"❌ **'{anime_name}'** ka koi schedule nahi mila!\n\n"
            f"List dekhne ke liye: `/schedule_list`"
        )
        return

    await _save_schedule_list(new_list)
    await message.reply(
        f"✅ **Schedule Deleted!**\n\n"
        f"📺 **{anime_name}** ka schedule remove ho gaya."
    )


# ─────────────────────────────────────────────
#  /end_message_preview — saved end messages dekho
# ─────────────────────────────────────────────
@Client.on_message(filters.command("end_message_preview") & filters.private)
async def cmd_end_message_preview(client: Client, message: Message):
    """
    /end_message_preview          → default messages preview
    /end_message_preview SonyYay  → channel-specific preview
    """
    if not _is_authorized(message.from_user.id):
        return

    parts = message.text.split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        channel_key = DEFAULT_END_KEY
        display = "🌐 Default End Messages"
    else:
        channel_key = parts[1].strip()
        display = f"📢 Channel: **{channel_key}**"

    messages = await _get_end_messages_raw(channel_key)

    if not messages:
        no_key_text = (
            "default" if channel_key == DEFAULT_END_KEY else channel_key
        )
        await message.reply(
            f"📭 **'{no_key_text}'** ke liye koi end messages saved nahi hain.\n\n"
            f"Add karne ke liye:\n"
            f"• Default: `/end_message`\n"
            f"• Channel: `/end_message ChannelName`"
        )
        return

    await message.reply(
        f"📋 **End Messages Preview**\n\n"
        f"{display}\n"
        f"💾 Total: **{len(messages)}** messages saved\n\n"
        f"Delete karne ke liye: `/end_message_del {channel_key if channel_key != DEFAULT_END_KEY else 'default'}`"
    )


# ─────────────────────────────────────────────
#  /end_message_del — saved end messages delete karo
# ─────────────────────────────────────────────
@Client.on_message(filters.command("end_message_del") & filters.private)
async def cmd_end_message_del(client: Client, message: Message):
    """
    /end_message_del          → default messages delete karo
    /end_message_del SonyYay  → channel-specific delete karo
    """
    if not _is_authorized(message.from_user.id):
        return

    parts = message.text.split(None, 1)
    # "default" keyword ya koi argument nahi → default delete
    if len(parts) < 2 or not parts[1].strip() or parts[1].strip().lower() == 'default':
        channel_key = DEFAULT_END_KEY
        display = "🌐 Default End Messages"
    else:
        channel_key = parts[1].strip()
        display = f"📢 Channel: **{channel_key}**"

    existing = await _get_end_messages_raw(channel_key)

    if not existing:
        await message.reply(
            f"❌ **'{channel_key if channel_key != DEFAULT_END_KEY else 'default'}'** ke liye koi end messages saved nahi hain!"
        )
        return

    await _delete_end_messages_db(channel_key)
    await message.reply(
        f"✅ **End Messages Deleted!**\n\n"
        f"{display}\n"
        f"🗑️ {len(existing)} messages delete ho gaye."
    )


# ─────────────────────────────────────────────
#  /update_channel — broadcast channel add karo
# ─────────────────────────────────────────────
@Client.on_message(filters.command("update_channel") & (filters.private | filters.group))
async def cmd_update_channel(client: Client, message: Message):
    """
    /update_channel [channel_id]
    Ek baar set karo — episode upload hone pe wahan broadcast jayega.
    """
    if not _is_authorized(message.from_user.id):
        return

    parts = message.text.split()
    if len(parts) < 2:
        channels = await _get_update_channels()
        if not channels:
            await message.reply(
                "📢 **Update Channels**\n\n"
                "❌ Abhi koi update channel set nahi hai.\n\n"
                "**Usage:** `/update_channel -100xxxxxxxxx`\n"
                "Jab bhi episode upload hoga, wahan broadcast jayega!"
            )
        else:
            text = "📢 **Update Channels:**\n\n"
            for i, ch in enumerate(channels, 1):
                try:
                    chat = await client.get_chat(ch['channel_id'])
                    title = chat.title
                except Exception:
                    title = ch.get('title', 'Unknown')
                text += f"**{i}.** {title}\n    `{ch['channel_id']}`\n\n"
            text += f"Total: **{len(channels)}**\n\nRemove: `/update_channel_del <number>`"
            await message.reply(text)
        return

    try:
        channel_id = int(parts[1])
    except ValueError:
        await message.reply("❌ Valid channel ID dalo! Format: `-100xxxxxxxxx`")
        return

    # Channel check
    try:
        chat = await client.get_chat(channel_id)
        title = chat.title
    except Exception as e:
        await message.reply(f"❌ Channel nahi mila: `{e}`\n\nBot ko channel mein admin banao pehle.")
        return

    channels = await _get_update_channels()

    # Already exists check
    for ch in channels:
        if ch['channel_id'] == channel_id:
            await message.reply(f"⚠️ **{title}** already update channels mein hai!")
            return

    channels.append({'channel_id': channel_id, 'title': title})
    await _save_update_channels(channels)

    await message.reply(
        f"✅ **Update Channel Added!**\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📢 **{title}**\n"
        f"🆔 `{channel_id}`\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Ab jab bhi koi episode upload hoga, yahan broadcast jayega! 🚀"
    )


@Client.on_message(filters.command("update_channel_list") & (filters.private | filters.group))
async def cmd_update_channel_list(client: Client, message: Message):
    if not _is_authorized(message.from_user.id):
        return

    channels = await _get_update_channels()
    if not channels:
        await message.reply(
            "📢 **Update Channels**\n\n"
            "❌ Koi update channel set nahi hai.\n\n"
            "Add karo: `/update_channel -100xxxxxxxxx`"
        )
        return

    text = "📢 **Update Channels:**\n\n"
    for i, ch in enumerate(channels, 1):
        try:
            chat = await client.get_chat(ch['channel_id'])
            title = chat.title
        except Exception:
            title = ch.get('title', 'Unknown')
        text += f"**{i}.** {title}\n    `{ch['channel_id']}`\n\n"
    text += f"Total: **{len(channels)}**\n\n🗑️ Remove: `/update_channel_del <number>`"
    await message.reply(text)


@Client.on_message(filters.command("update_channel_del") & (filters.private | filters.group))
async def cmd_update_channel_del(client: Client, message: Message):
    if not _is_authorized(message.from_user.id):
        return

    channels = await _get_update_channels()
    if not channels:
        await message.reply("❌ Koi update channel set nahi hai!")
        return

    if len(message.command) < 2:
        text = "🗑️ **Konsa remove karna hai?**\n\n"
        for i, ch in enumerate(channels, 1):
            text += f"**{i}.** {ch.get('title', 'Unknown')} (`{ch['channel_id']}`)\n"
        text += "\nUse: `/update_channel_del <number>`"
        await message.reply(text)
        return

    try:
        num = int(message.command[1])
    except ValueError:
        await message.reply("❌ Sahi number dalo!")
        return

    if num < 1 or num > len(channels):
        await message.reply(f"❌ 1 se {len(channels)} tak dalo.")
        return

    removed = channels.pop(num - 1)
    await _save_update_channels(channels)
    await message.reply(
        f"✅ **Removed!**\n\n"
        f"📢 {removed.get('title', 'Unknown')}\n"
        f"🆔 `{removed['channel_id']}`"
    )


# ─────────────────────────────────────────────
#  Auto Broadcast — episode upload hone pe
#  update channels pe broadcast bhejo
# ─────────────────────────────────────────────
#  Season detect karo caption se
# ─────────────────────────────────────────────
def _detect_season(caption: str) -> int:
    """
    Caption mein se season number nikalo.
    'Season 2' / 'S02' / 'S2E05' → 2
    Kuch nahi mila → 1 (default)
    """
    if not caption:
        return 1
    # Season 02 / Season 2
    m = re.search(r'[Ss]eason\s*(\d+)', caption)
    if m:
        return int(m.group(1))
    # S02E05 / S2E5
    m = re.search(r'[Ss](\d+)[Ee]\d+', caption)
    if m:
        return int(m.group(1))
    return 1


async def send_broadcast_to_update_channels(
    anime_name: str,
    episode_num: int,
    caption: str = "",
    season: int = 0,
    hashtag: str = "",
    channel_link: str = "",
):
    """
    Saare update channels pe broadcast message bhejo.

    - hashtag/channel_link: agar pass nahi kiya → DB se saved info fetch hogi
    - season: agar 0 → caption se detect karenge (default 1)
    """
    channels = await _get_update_channels()
    if not channels:
        LOGGER.info("[Broadcast] No update channels set, skipping.")
        return

    # ── Saved broadcast info fetch karo agar args empty hain ──
    if not hashtag or not channel_link:
        saved = await _get_anime_broadcast_info(anime_name)
        if saved:
            if not hashtag:
                hashtag = saved.get('hashtag', '')
            if not channel_link:
                channel_link = saved.get('channel_link', '')
            LOGGER.info(f"[Broadcast] Using saved info for '{anime_name}': hashtag='{hashtag}'")
        else:
            LOGGER.info(f"[Broadcast] No saved broadcast info for '{anime_name}', sending without hashtag/link")

    # ── Season detect karo ──
    if season == 0:
        season = _detect_season(caption)

    # ── Message build karo ──
    SEP = "━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    lines = []
    lines.append(SEP)
    if hashtag:
        lines.append(f"**🔰{anime_name} {hashtag}**")
    else:
        lines.append(f"**🔰{anime_name}**")
    lines.append("")
    lines.append(f"**⚡Season {season:02d} Episode {episode_num:02d} Added...!**")

    if channel_link:
        watch_line = f"**[📌𝙒𝘼𝙏𝘾𝙃 & 𝘿𝙊𝙒𝙉𝙇𝙊𝘼𝘿📌]({channel_link})**"
        lines.append(watch_line)
        lines.append(watch_line)
    else:
        lines.append("**📌𝙒𝘼𝙏𝘾𝙃 & 𝘿𝙊𝙒𝙉𝙇𝙊𝘼𝘿📌**")
        lines.append("**📌𝙒𝘼𝙏𝘾𝙃 & 𝘿𝙊𝙒𝙉𝙇𝙊𝘼𝘿📌**")

    broadcast_text = "\n".join(lines)

    LOGGER.info(f"[Broadcast] Sending to {len(channels)} update channels — '{anime_name}' S{season:02d}E{episode_num:02d}")

    for ch in channels:
        try:
            await app.send_message(
                chat_id=ch['channel_id'],
                text=broadcast_text,
                disable_web_page_preview=True,
            )
            LOGGER.info(f"[Broadcast] ✅ Sent to {ch['channel_id']} ({ch.get('title', '?')})")
        except Exception as e:
            LOGGER.error(f"[Broadcast] ❌ Failed for {ch['channel_id']}: {e}")
        await asyncio.sleep(0.5)


# ─────────────────────────────────────────────
#  /broadcast_message — manual broadcast
# ─────────────────────────────────────────────
@Client.on_message(filters.command("broadcast_message") & filters.private)
async def cmd_broadcast_message(client: Client, message: Message):
    """
    /broadcast_message → conversation shuru
    Step 1: anime name
    Step 2: hashtag (ya skip)
    Step 3: channel link
    """
    if not _is_authorized(message.from_user.id):
        return

    user_id = message.from_user.id
    _broadcast_state[user_id] = {'step': 'name', 'anime_name': '', 'hashtag': ''}

    await message.reply(
        "📣 **Broadcast Message**\n\n"
        "**Step 1/3** — Anime ka naam bhejo:\n\n"
        "_Example: Karna the Guardian_\n\n"
        "❌ Cancel: `/cancel_broadcast`"
    )


@Client.on_message(filters.command("cancel_broadcast") & filters.private)
async def cmd_cancel_broadcast(client: Client, message: Message):
    if not _is_authorized(message.from_user.id):
        return
    _broadcast_state.pop(message.from_user.id, None)
    await message.reply("❌ Broadcast cancelled.")




@Client.on_message(filters.command("confirm_broadcast") & filters.private)
async def cmd_confirm_broadcast(client: Client, message: Message):
    if not _is_authorized(message.from_user.id):
        return

    user_id = message.from_user.id
    state = _broadcast_state.pop(user_id, None)
    if not state or state.get('step') != 'confirm':
        await message.reply("⚠️ Pehle `/broadcast_message` karo.")
        return

    anime_name   = state['anime_name']
    hashtag      = state['hashtag']
    channel_link = state['channel_link']

    # Sirf DB mein save karo — channel pe broadcast nahi
    await _save_anime_broadcast_info(anime_name, hashtag, channel_link)

    await message.reply(
        f"✅ **Broadcast Info Saved!**\n\n"
        f"📺 Anime: **{anime_name}**\n"
        f"🏷️ Hashtag: **{hashtag if hashtag else 'None'}**\n"
        f"🔗 Link: `{channel_link}`\n\n"
        f"Ab jab bhi **{anime_name}** ka episode upload hoga,\n"
        f"automatically broadcast ho jaayega! 🚀"
    )
