"""
schedule_notify.py
===================
Episode Schedule Notification System

Jab saare qualities upload ho jaaye, bot channel pe post karta hai:
  → "**Next episode upload on 4th June**"
  → Last episode ke baad: "**END**"

Commands:
  /schedule [days] [total_eps] [Anime Name]
    Example: /schedule 7 12 Witch Hat Atelier
    → Har 7 din baad episode aata hai, total 12 episodes, Witch Hat Atelier ke liye

  /schedule_list    → Dekho kya set hai
  /schedule_del [Anime Name]  → Remove karo
"""

import logging
from datetime import datetime, timedelta, timezone

from pyrogram import Client, filters
from pyrogram.types import Message

from .. import LOGGER, app, owner, sudo_users
from ..utils.database.access_db import db

# IST = UTC+5:30
IST = timezone(timedelta(hours=5, minutes=30))


def _is_authorized(user_id: int) -> bool:
    return user_id in owner or user_id in sudo_users


# ─────────────────────────────────────────────
#  Ordinal suffix helper  →  1st, 2nd, 3rd, 4th ...
# ─────────────────────────────────────────────
def _ordinal(n: int) -> str:
    if 11 <= (n % 100) <= 13:
        return f"{n}th"
    return f"{n}{['th','st','nd','rd','th','th','th','th','th','th'][n % 10]}"


# ─────────────────────────────────────────────
#  Next episode date calculate karo (IST)
# ─────────────────────────────────────────────
def _next_episode_date(interval_days: int) -> str:
    """Aaj ki IST date + interval_days = next episode date → '4th June' format"""
    today_ist = datetime.now(IST)
    next_date = today_ist + timedelta(days=interval_days)
    day_str = _ordinal(next_date.day)
    month_str = next_date.strftime("%B")   # e.g. "June"
    return f"{day_str} {month_str}"


# ─────────────────────────────────────────────
#  DB Helpers — schedule list owner ke doc mein
# ─────────────────────────────────────────────
async def _owner_id() -> int | None:
    return owner[0] if owner else None


async def _get_schedule_list() -> list:
    oid = await _owner_id()
    if not oid:
        return []
    user = await db._get_user(oid)
    return user.get('episode_schedule_list', [])


async def _save_schedule_list(schedule_list: list):
    oid = await _owner_id()
    if not oid:
        return
    await db.col.update_one(
        {'id': oid},
        {'$set': {'episode_schedule_list': schedule_list}},
        upsert=True
    )


def _normalize(text: str) -> str:
    import re
    return re.sub(r'[^a-z0-9]', '', text.lower())


async def _get_schedule_for_anime(anime_name: str) -> dict | None:
    """Anime name se matching schedule entry dhundo."""
    schedule_list = await _get_schedule_list()
    name_norm = _normalize(anime_name)
    best, best_len = None, 0
    for entry in schedule_list:
        entry_norm = _normalize(entry.get('anime_name', ''))
        if entry_norm and entry_norm in name_norm and len(entry_norm) > best_len:
            best, best_len = entry, len(entry_norm)
    return best


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
    Schedule set hai toh:
      - Agar last episode nahi → "Next episode upload on Xth Month"
      - Agar last episode hai  → "END"
    """
    schedule = await _get_schedule_for_anime(anime_name)
    if not schedule:
        LOGGER.info(f"[Schedule] No schedule set for '{anime_name}', skipping notification.")
        return

    interval_days = schedule.get('interval_days', 7)
    total_eps     = schedule.get('total_eps', 0)

    try:
        if total_eps > 0 and episode_num >= total_eps:
            # Last episode — END post karo
            msg_text = f"**END**"
            LOGGER.info(f"[Schedule] Last episode ({episode_num}/{total_eps}) for '{anime_name}' → posting END")
        else:
            # Next episode date calculate karo
            next_date = _next_episode_date(interval_days)
            msg_text = f"**Next episode upload on {next_date}**"
            LOGGER.info(f"[Schedule] Ep {episode_num} done for '{anime_name}' → posting: {msg_text}")

        await app.send_message(
            chat_id=channel_id,
            text=msg_text,
        )

    except Exception as e:
        LOGGER.error(f"[Schedule] Notification send failed for '{anime_name}': {e}")


# ─────────────────────────────────────────────
#  /schedule command
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
            "💡 days = kitne din baad episode aata hai\n"
            "💡 total_eps = total kitne episodes hain\n"
            "💡 Anime Name = wahi likhna jo /add_anime mein hai"
        )
        return

    try:
        interval_days = int(parts[1])
        total_eps     = int(parts[2])
    except ValueError:
        await message.reply("❌ Days aur total_eps numbers hone chahiye!\nExample: `/schedule 7 12 Witch Hat Atelier`")
        return

    anime_name = parts[3].strip()

    schedule_list = await _get_schedule_list()

    # Agar pehle se exist karta hai toh update karo
    for i, entry in enumerate(schedule_list):
        if _normalize(entry.get('anime_name', '')) == _normalize(anime_name):
            schedule_list[i] = {
                'anime_name': anime_name,
                'interval_days': interval_days,
                'total_eps': total_eps,
            }
            await _save_schedule_list(schedule_list)
            next_date = _next_episode_date(interval_days)
            await message.reply(
                f"✅ **Schedule Updated!**\n\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📺 **Anime:** {anime_name}\n"
                f"📅 **Interval:** {interval_days} days\n"
                f"🎬 **Total Episodes:** {total_eps}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"Next notification preview:\n"
                f"**Next episode upload on {next_date}**"
            )
            return

    # Naya add karo
    schedule_list.append({
        'anime_name': anime_name,
        'interval_days': interval_days,
        'total_eps': total_eps,
    })
    await _save_schedule_list(schedule_list)

    next_date = _next_episode_date(interval_days)
    await message.reply(
        f"✅ **Schedule Set!**\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📺 **Anime:** {anime_name}\n"
        f"📅 **Interval:** {interval_days} days\n"
        f"🎬 **Total Episodes:** {total_eps}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Next notification preview:\n"
        f"**Next episode upload on {next_date}**"
    )


# ─────────────────────────────────────────────
#  /schedule_list command
# ─────────────────────────────────────────────
@Client.on_message(filters.command("schedule_list") & filters.private)
async def cmd_schedule_list(client: Client, message: Message):
    if not _is_authorized(message.from_user.id):
        return

    schedule_list = await _get_schedule_list()

    if not schedule_list:
        await message.reply(
            "📋 Koi schedule set nahi hai!\n\n"
            "Set karo: `/schedule 7 12 Witch Hat Atelier`"
        )
        return

    text = "📅 **Episode Schedules**\n━━━━━━━━━━━━━━━━━━━━\n\n"
    for i, entry in enumerate(schedule_list, 1):
        text += (
            f"**{i}.** 📺 {entry.get('anime_name')}\n"
            f"   📅 Every {entry.get('interval_days')} days\n"
            f"   🎬 Total: {entry.get('total_eps')} eps\n\n"
        )
    text += f"━━━━━━━━━━━━━━━━━━━━\nTotal: **{len(schedule_list)}**\n\n🗑️ Remove: `/schedule_del Anime Name`"
    await message.reply(text)


# ─────────────────────────────────────────────
#  /schedule_del command
# ─────────────────────────────────────────────
@Client.on_message(filters.command("schedule_del") & filters.private)
async def cmd_schedule_del(client: Client, message: Message):
    if not _is_authorized(message.from_user.id):
        return

    parts = message.text.split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply("**Usage:** `/schedule_del Anime Name`\nExample: `/schedule_del Witch Hat Atelier`")
        return

    anime_name = parts[1].strip()
    schedule_list = await _get_schedule_list()

    new_list = [e for e in schedule_list if _normalize(e.get('anime_name', '')) != _normalize(anime_name)]

    if len(new_list) == len(schedule_list):
        await message.reply(f"❌ `{anime_name}` ka koi schedule nahi mila.")
        return

    await _save_schedule_list(new_list)
    await message.reply(f"🗑️ **Deleted!** `{anime_name}` ka schedule remove ho gaya.")
