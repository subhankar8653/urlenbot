"""
schedule_notify.py  v2
=======================
Episode Schedule + End Message Notification System

Flow (episode complete hone ke baad):
  1. Purane schedule/end messages delete karo (last 15 msgs mein se, videos nahi)
  2. Schedule message post karo → "Next episode upload on 4th June"
     (Last episode pe → "END" post hoga, end messages nahi)
  3. End messages post karo (ek ek karke, saare saved messages)

Commands:
  /schedule [days] [total_eps] [Anime Name]
      Example: /schedule 7 12 Witch Hat Atelier

  /end_message [Anime Name]
      → Bot bolta hai "Ab bhejo jo messages end mein add karne hain"
      → Tum bhejte ho (text, sticker, forward, kuch bhi)
      → /done  → save ho jaata hai

  /end_message_preview [Anime Name]  → Dekho kya saved hai
  /end_message_del [Anime Name]      → Delete karo

  /schedule_list   → Saare schedules dekho
  /schedule_del [Anime Name]
"""

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

from pyrogram import Client, filters
from pyrogram.types import Message

from .. import LOGGER, app, owner, sudo_users
from ..utils.database.access_db import db

IST = timezone(timedelta(hours=5, minutes=30))

# In-memory state: kaun abhi end_message recording mode mein hai
# { user_id: { 'anime_name': str, 'messages': [ {type, content} ] } }
_recording_state: dict = {}


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


# End messages DB
async def _get_end_messages(anime_name: str) -> list:
    """Return saved end message list for anime. Each item: dict with type + content."""
    oid = await _owner_id()
    if not oid:
        return []
    user = await db._get_user(oid)
    end_map = user.get('end_messages_map', {})
    key = _normalize(anime_name)
    return end_map.get(key, [])


async def _save_end_messages(anime_name: str, messages: list):
    oid = await _owner_id()
    if not oid:
        return
    user = await db._get_user(oid)
    end_map = user.get('end_messages_map', {})
    key = _normalize(anime_name)
    end_map[key] = messages
    await db.col.update_one({'id': oid}, {'$set': {'end_messages_map': end_map}}, upsert=True)


async def _delete_end_messages_db(anime_name: str):
    oid = await _owner_id()
    if not oid:
        return
    user = await db._get_user(oid)
    end_map = user.get('end_messages_map', {})
    key = _normalize(anime_name)
    end_map.pop(key, None)
    await db.col.update_one({'id': oid}, {'$set': {'end_messages_map': end_map}}, upsert=True)


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
async def _send_end_messages_to_channel(channel_id: int, anime_name: str):
    """DB se saved end messages ek ek karke channel pe bhejo."""
    messages = await _get_end_messages(anime_name)
    if not messages:
        LOGGER.info(f"[EndMsg] No end messages saved for '{anime_name}'")
        return

    LOGGER.info(f"[EndMsg] Sending {len(messages)} end messages for '{anime_name}' to {channel_id}")

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
#  Cleanup — last 15 msgs mein se schedule/end msgs delete karo
#  (videos/documents nahi — sirf text/sticker messages)
# ─────────────────────────────────────────────
async def cleanup_old_notifications(channel_id: int, anime_name: str):
    """
    Cleanup (FINAL):
      - Channel ke last 3 messages scan karo
      - SKIP: video/document | text mein "end" ya "season"
      - Baaki sab DELETE
    """
    try:
        to_delete = []
        skipped   = []

        async for msg in app.get_chat_history(channel_id, limit=3):
            if msg.video or msg.document:
                skipped.append(f"msg {msg.id} [video/doc]")
                continue
            msg_text = (msg.text or msg.caption or "").lower()
            if "end" in msg_text or "season" in msg_text:
                skipped.append(f"msg {msg.id} [end/season]")
                continue
            to_delete.append(msg.id)

        LOGGER.info(f"[Cleanup] to_delete={to_delete} skipped={skipped}")

        deleted = 0
        for msg_id in to_delete:
            try:
                await app.delete_messages(channel_id, msg_id)
                deleted += 1
                await asyncio.sleep(0.3)
            except Exception as e:
                LOGGER.warning(f"[Cleanup] Could not delete {msg_id}: {e}")

        LOGGER.info(f"[Cleanup] deleted={deleted} skipped={len(skipped)}")
    except Exception as e:
        LOGGER.error(f"[Cleanup] Failed: {e}")


# ─────────────────────────────────────────────
#  Main function — auto_monitor.py se call hoga
# ─────────────────────────────────────────────
async def send_schedule_notification(
    client: Client,
    channel_id: int,
    anime_name: str,
    episode_num: int,
):
    """
    Episode complete hone ke baad call karo.
    Flow:
      1. Purane schedule/end msgs delete karo
      2. Schedule msg post karo
      3. End messages post karo (last episode pe nahi — END ke baad kuch nahi)
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
    await _send_end_messages_to_channel(channel_id, anime_name)


# ─────────────────────────────────────────────
#  /end_message command — recording mode start
# ─────────────────────────────────────────────
@Client.on_message(filters.command("end_message") & filters.private)
async def cmd_end_message(client: Client, message: Message):
    """
    /end_message Witch Hat Atelier
    → Recording mode shuru — ab jo bhejoge woh save hoga
    → /done se band karo
    """
    if not _is_authorized(message.from_user.id):
        return

    parts = message.text.split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply(
            "**Usage:** `/end_message Anime Name`\n"
            "Example: `/end_message Witch Hat Atelier`\n\n"
            "Phir jo messages bhejoge woh save honge.\n"
            "Khatam karne ke liye: `/done`"
        )
        return

    anime_name = parts[1].strip()
    user_id = message.from_user.id

    _recording_state[user_id] = {
        'anime_name': anime_name,
        'messages': []
    }

    existing = await _get_end_messages(anime_name)
    note = f"\n\n⚠️ Pehle se **{len(existing)}** messages saved hain — naye se replace ho jayenge." if existing else ""

    await message.reply(
        f"🎬 **End Message Recording Started!**\n\n"
        f"📺 Anime: **{anime_name}**\n\n"
        f"Ab jo bhi bhejoge — text, sticker, forward, photo — sab save hoga.{note}\n\n"
        f"✅ Khatam karne ke liye: `/done`\n"
        f"❌ Cancel karne ke liye: `/cancel_end`"
    )


# ─────────────────────────────────────────────
#  Recording mode — messages capture karo
# ─────────────────────────────────────────────
@Client.on_message(
    filters.private &
    ~filters.command(["done", "cancel_end", "end_message", "schedule",
                      "schedule_list", "schedule_del", "end_message_preview",
                      "end_message_del"])
)
async def capture_end_message(client: Client, message: Message):
    """Agar user recording mode mein hai toh messages capture karo."""
    user_id = message.from_user.id
    if user_id not in _recording_state:
        return
    if not _is_authorized(user_id):
        return

    state = _recording_state[user_id]

    # Sticker ko alag se handle karo (copy_message sticker pe kaam nahi karta)
    if message.sticker:
        item = {'type': 'sticker', 'file_id': message.sticker.file_id}
        state['messages'].append(item)
        count = len(state['messages'])
        await message.reply(f"✅ Saved! ({count} messages total) — `/done` se khatam karo")
        return

    # Baaki sabhi messages (text, forward, photo, buttons wale) — copy_message reference save karo
    # copy_message se inline buttons, hyperlinks, formatting sab perfectly preserve hoti hai
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
        await message.reply("⚠️ Koi recording chal nahi rahi. Pehle `/end_message Anime Name` karo.")
        return

    state = _recording_state.pop(user_id)
    anime_name = state['anime_name']
    messages   = state['messages']

    if not messages:
        await message.reply("⚠️ Koi message save nahi hua! Recording cancel ho gaya.")
        return

    await _save_end_messages(anime_name, messages)

    await message.reply(
        f"✅ **End Messages Saved!**\n\n"
        f"📺 Anime: **{anime_name}**\n"
        f"💾 Total: **{len(messages)}** messages saved\n\n"
        f"Har episode upload ke baad yeh messages automatically post honge."
    )
