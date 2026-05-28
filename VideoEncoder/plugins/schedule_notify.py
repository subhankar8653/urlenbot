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

from pyrogram.errors import UserNotParticipant

from .. import LOGGER, app, owner, sudo_users, api_id, api_hash
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
async def cleanup_old_notifications(
    channel_id: int,
    anime_name: str,
    user_session: str | None = None,
) -> int:
    """
    Cleanup (IMPROVED):
      - Channel ke last 50 messages scan karo
      - SKIP: video/document/photo/audio/sticker (media content)
      - SKIP: text mein "end" ya "season" (important msgs)
      - DELETE: sirf schedule/notification text messages
        (jinmein "next episode", "upload on", ya koi bhi plain
         non-media notification text hai)
      - Zyada se zyada 10 messages delete (safety limit)
      - user_session diya → user account se delete (purane msgs bhi jaayenge)
      - user_session nahi → bot se delete (sirf bot ke messages)
      - Returns: deleted count
    """
    # Yeh keywords wale messages ZAROOR delete honge (schedule msgs)
    SCHEDULE_KEYWORDS = [
        "next episode",
        "upload on",
        "coming soon",
        "more coming",
        "be updated",
    ]

    # Yeh keywords wale messages KABHI delete NAHI honge
    PROTECTED_KEYWORDS = [
        "end",
        "season",
        "finale",
    ]

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
            # Message type detect karo
            msg_type = "text"
            if msg.video:      msg_type = "video"
            elif msg.document: msg_type = "document"
            elif msg.photo:    msg_type = "photo"
            elif msg.audio:    msg_type = "audio"
            elif msg.sticker:  msg_type = "sticker"
            elif msg.animation: msg_type = "animation"

            msg_text = (msg.text or msg.caption or "").lower()
            msg_preview = msg_text[:60].replace('\n', ' ') if msg_text else "[no text]"

            LOGGER.info(
                f"[Cleanup-DEBUG] msg_id={msg.id} | type={msg_type} | "
                f"text='{msg_preview}'"
            )

            # Media messages skip — yeh actual content hai
            if msg_type != "text":
                skipped.append(f"msg {msg.id} [media={msg_type}]")
                LOGGER.info(f"[Cleanup-DEBUG] msg {msg.id} → SKIP (media)")
                continue

            # Protected keywords wale skip
            if any(kw in msg_text for kw in PROTECTED_KEYWORDS):
                matched_kw = [kw for kw in PROTECTED_KEYWORDS if kw in msg_text]
                skipped.append(f"msg {msg.id} [protected={matched_kw}]")
                LOGGER.info(f"[Cleanup-DEBUG] msg {msg.id} → SKIP (protected: {matched_kw})")
                continue

            # Schedule keywords wale — delete list mein
            if any(kw in msg_text for kw in SCHEDULE_KEYWORDS):
                matched_kw = [kw for kw in SCHEDULE_KEYWORDS if kw in msg_text]
                to_delete.append(msg.id)
                LOGGER.info(f"[Cleanup-DEBUG] msg {msg.id} → DELETE (schedule keyword: {matched_kw})")
                continue

            # Baaki plain text msgs
            if len(to_delete) + len(skipped) < 5 and msg_text:
                to_delete.append(msg.id)
                LOGGER.info(f"[Cleanup-DEBUG] msg {msg.id} → DELETE (plain text, early scan)")
            else:
                skipped.append(f"msg {msg.id} [non-schedule-text]")
                LOGGER.info(f"[Cleanup-DEBUG] msg {msg.id} → SKIP (non-schedule text)")

        # Safety: zyada se zyada 10 delete
        to_delete = to_delete[:10]

        LOGGER.info(f"[Cleanup] to_delete={to_delete} skipped={skipped}")

        deleted = 0
        for msg_id in to_delete:
            try:
                await delete_client.delete_messages(channel_id, msg_id)
                deleted += 1
                await asyncio.sleep(0.3)
            except Exception as e:
                LOGGER.warning(f"[Cleanup] Could not delete {msg_id}: {e}")

        LOGGER.info(f"[Cleanup] deleted={deleted} skipped={len(skipped)}")
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
    await message.reply(
        f"❌ **Recording Cancelled!**\n\n"
        f"📺 Anime: **{state['anime_name']}**\n"
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
    /end_message_preview [Anime Name]
    """
    if not _is_authorized(message.from_user.id):
        return

    parts = message.text.split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply(
            "**Usage:** `/end_message_preview [Anime Name]`\n"
            "Example: `/end_message_preview Witch Hat Atelier`"
        )
        return

    anime_name = parts[1].strip()
    messages = await _get_end_messages(anime_name)

    if not messages:
        await message.reply(
            f"📭 **'{anime_name}'** ke liye koi end messages saved nahi hain.\n\n"
            f"Add karne ke liye: `/end_message {anime_name}`"
        )
        return

    await message.reply(
        f"📋 **End Messages Preview**\n\n"
        f"📺 Anime: **{anime_name}**\n"
        f"💾 Total: **{len(messages)}** messages saved\n\n"
        f"Ye messages har episode ke baad post honge.\n"
        f"Delete karne ke liye: `/end_message_del {anime_name}`"
    )


# ─────────────────────────────────────────────
#  /end_message_del — saved end messages delete karo
# ─────────────────────────────────────────────
@Client.on_message(filters.command("end_message_del") & filters.private)
async def cmd_end_message_del(client: Client, message: Message):
    """
    /end_message_del [Anime Name]
    """
    if not _is_authorized(message.from_user.id):
        return

    parts = message.text.split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply(
            "**Usage:** `/end_message_del [Anime Name]`\n"
            "Example: `/end_message_del Witch Hat Atelier`"
        )
        return

    anime_name = parts[1].strip()
    existing = await _get_end_messages(anime_name)

    if not existing:
        await message.reply(
            f"❌ **'{anime_name}'** ke liye koi end messages saved nahi hain!"
        )
        return

    await _delete_end_messages_db(anime_name)
    await message.reply(
        f"✅ **End Messages Deleted!**\n\n"
        f"📺 Anime: **{anime_name}**\n"
        f"🗑️ {len(existing)} messages delete ho gaye."
    )
