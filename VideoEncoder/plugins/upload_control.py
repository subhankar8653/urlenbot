"""
upload_control.py
==================
AutoMonitor / /Rtic auto-upload pipeline ke liye Season + Episode level
cancel control. Auto-upload chalte waqt (poora season ho ya ek episode)
kabhi bhi rok sakte ho.

Commands:
  /cancel_season [Anime Name]
      -> Us anime ka abhi chal raha poora upload turant rok do
         (current episode + baaki saare queued episodes skip)

  /cancel_episode [Anime Name] [Episode Number]
      -> Sirf ek specific episode ka process cancel karo,
         baaki season chalta rahega

  /active_uploads
      -> Abhi kaun kaun se anime/episode process ho rahe hain dekho
"""

import re
import time

from pyrogram import Client, filters
from pyrogram.types import Message

from .. import owner, sudo_users


def _is_authorized(user_id: int) -> bool:
    return user_id in owner or user_id in sudo_users


def _normalize(text: str) -> str:
    return re.sub(r'[^a-z0-9]', '', text.lower())


# ─────────────────────────────────────────────
#  In-memory state (process ke jeete-ji hi kaam ka —
#  yeh sirf "abhi chal raha" upload rokne ke liye hai)
# ─────────────────────────────────────────────
_season_cancel: set = set()          # {normalized_anime_name}
_episode_cancel: set = set()         # {(normalized_anime_name, ep_num)}
_active: dict = {}                   # {(normalized_anime_name, ep_num): info}


def mark_active(anime_name: str, episode_num: int, status_msg=None):
    """Episode processing shuru hote hi call karo."""
    key = (_normalize(anime_name), episode_num)
    _active[key] = {
        'anime_name': anime_name,
        'episode_num': episode_num,
        'started_at': time.time(),
        'status_msg': status_msg,
    }
    return key


def unmark_active(anime_name: str, episode_num: int):
    """Episode processing khatam (success/fail/cancel) hote hi call karo."""
    key = (_normalize(anime_name), episode_num)
    _active.pop(key, None)
    _episode_cancel.discard(key)


def is_season_cancelled(anime_name: str) -> bool:
    return _normalize(anime_name) in _season_cancel


def is_episode_cancelled(anime_name: str, episode_num: int) -> bool:
    return (_normalize(anime_name), episode_num) in _episode_cancel


def should_stop(anime_name: str, episode_num: int) -> bool:
    """Poller/loop ke andar yeh check karo — True aaye toh turant ruk jao."""
    return is_season_cancelled(anime_name) or is_episode_cancelled(anime_name, episode_num)


def consume_episode_cancel(anime_name: str, episode_num: int):
    """Ek episode skip karne ke baad flag clear karo, taaki future re-run block na ho."""
    _episode_cancel.discard((_normalize(anime_name), episode_num))


def consume_season_cancel(anime_name: str):
    """
    Ek season-loop jab apna cancel dekh kar ruk jaaye, flag clear kar do —
    taaki isi anime ki agli (alag) upload run permanently blocked na rahe.
    """
    _season_cancel.discard(_normalize(anime_name))


def list_active() -> list:
    return list(_active.values())


# ─────────────────────────────────────────────
#  /cancel_season
# ─────────────────────────────────────────────
@Client.on_message(filters.command("cancel_season") & filters.private)
async def cmd_cancel_season(client: Client, message: Message):
    if not _is_authorized(message.from_user.id):
        return

    parts = message.text.split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply(
            "**Usage:** `/cancel_season [Anime Name]`\n"
            "Example: `/cancel_season Witch Hat Atelier`\n\n"
            "Isse us anime ka **abhi chal raha poora upload** "
            "(current + baaki queued episodes) turant rukh jayega."
        )
        return

    anime_name = parts[1].strip()
    norm = _normalize(anime_name)
    _season_cancel.add(norm)

    hit_any = False
    for key in list(_active.keys()):
        if key[0] == norm:
            hit_any = True
            _episode_cancel.add(key)

    if hit_any:
        await message.reply(
            f"🛑 **Season Cancel Request Bhej Di!**\n\n"
            f"📺 **{anime_name}**\n\n"
            f"Abhi chal raha episode jaldi hi rukh jayega, aur is run ke "
            f"baaki saare queued episodes skip ho jayenge."
        )
    else:
        await message.reply(
            f"⚠️ **{anime_name}** ka abhi koi upload chal hi nahi raha.\n\n"
            f"Active uploads dekhne ke liye: `/active_uploads`"
        )


# ─────────────────────────────────────────────
#  /cancel_episode
# ─────────────────────────────────────────────
@Client.on_message(filters.command("cancel_episode") & filters.private)
async def cmd_cancel_episode(client: Client, message: Message):
    if not _is_authorized(message.from_user.id):
        return

    if len(message.text.split(None, 1)) < 2:
        await message.reply(
            "**Usage:** `/cancel_episode [Anime Name] [Episode Number]`\n"
            "Example: `/cancel_episode Witch Hat Atelier 5`\n\n"
            "Isse sirf **wahi ek episode** cancel hoga, baaki season chalta rahega."
        )
        return

    raw = message.text.split(None, 1)[1].strip()
    tokens = raw.rsplit(None, 1)
    if len(tokens) < 2:
        await message.reply(
            "**Usage:** `/cancel_episode [Anime Name] [Episode Number]`\n"
            "Example: `/cancel_episode Witch Hat Atelier 5`"
        )
        return

    anime_name, ep_str = tokens
    try:
        episode_num = int(ep_str)
    except ValueError:
        await message.reply(
            "❌ Episode number valid nahi hai.\n"
            "Example: `/cancel_episode Witch Hat Atelier 5`"
        )
        return

    norm = _normalize(anime_name)
    key = (norm, episode_num)
    _episode_cancel.add(key)

    if key in _active:
        await message.reply(
            f"🛑 **Episode Cancel Request Bhej Di!**\n\n"
            f"📺 **{anime_name}** — Ep `{episode_num}`\n"
            f"Abhi chal raha process jaldi hi rukh jayega."
        )
    else:
        await message.reply(
            f"🛑 **Cancel Flag Set!**\n\n"
            f"📺 **{anime_name}** — Ep `{episode_num}`\n"
            f"Yeh episode abhi process nahi ho raha — agar queue mein hai "
            f"toh shuru hote hi turant skip ho jayega."
        )


# ─────────────────────────────────────────────
#  /active_uploads
# ─────────────────────────────────────────────
@Client.on_message(filters.command("active_uploads") & filters.private)
async def cmd_active_uploads(client: Client, message: Message):
    if not _is_authorized(message.from_user.id):
        return

    active = list_active()
    if not active:
        await message.reply("😴 **Koi active auto-upload nahi hai abhi.**")
        return

    now = time.time()
    text = "📡 **Active Auto-Uploads:**\n\n"
    for info in active:
        elapsed = int((now - info['started_at']) / 60)
        text += f"📺 **{info['anime_name']}** — Ep `{info['episode_num']}` (`{elapsed}m` se chal raha)\n"

    text += (
        "\nRokne ke liye:\n"
        "`/cancel_episode [Anime Name] [Episode Number]`\n"
        "`/cancel_season [Anime Name]`"
    )
    await message.reply(text)
