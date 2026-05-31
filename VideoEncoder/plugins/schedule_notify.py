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
      1. Schedule set hai → Schedule msg post karo (next date ya END)
      2. End messages HAMESHA post karo (schedule ho ya na ho)
    channel_key: channel-specific end messages ke liye (default = DEFAULT_END_KEY)
    """
    schedule = await _get_schedule_for_anime(anime_name)

    # Step 1: Schedule message — sirf tab jab schedule set ho
    if schedule:
        interval_days = schedule.get('interval_days', 7)
        total_eps     = schedule.get('total_eps', 0)
        is_last_ep    = total_eps > 0 and episode_num >= total_eps

        try:
            if is_last_ep:
                await app.send_message(channel_id, "**END**")
                LOGGER.info(f"[Schedule] Last ep {episode_num} → posted END for '{anime_name}'")
            else:
                next_date = _next_episode_date(interval_days)
                await app.send_message(channel_id, f"**Next episode upload on {next_date}**")
                LOGGER.info(f"[Schedule] Ep {episode_num} → Next on {next_date} for '{anime_name}'")
        except Exception as e:
            LOGGER.error(f"[Schedule] Schedule msg failed: {e}")

        await asyncio.sleep(0.5)
    else:
        LOGGER.info(f"[Schedule] No schedule for '{anime_name}' — skipping schedule msg.")

    # Step 2: End messages — schedule ho ya na ho, HAMESHA bhejo
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
                      "end_message_del", "swift", "swiftdl", "swiftencode",
                      "url", "mega", "meganow", "rti", "dl", "ddl", "batch"])
)
async def capture_any_state(client: Client, message: Message):
    """End message recording state handle karo."""
    user_id = message.from_user.id
    if not _is_authorized(user_id):
        return

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


async def send_broadcast_to_update_channels(*args, **kwargs):
    """Removed — broadcast feature disabled."""
    pass
