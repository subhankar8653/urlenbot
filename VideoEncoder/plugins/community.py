"""
community.py (plugin)
======================
/community <name>   — GLOBAL community/channel naam set karo (bot-wide,
                       sabhi upload paths — manual + auto-monitor — is
                       naam ko use karenge). Sirf owner/sudo chala
                       sakte hain, kyunki yeh sab users/uploads ke liye
                       ek saath badalta hai.
/community           — current community naam dekho.
/communityclear       — default 'sbanime' pe wapas reset karo.
"""

from pyrogram import Client, filters
from pyrogram.types import Message

from .. import owner, sudo_users
from ..utils.database.access_db import db
from ..utils.community import DEFAULT_COMMUNITY, get_community_names, parse_names, MAX_COMMUNITY_NAMES


def _is_auth(user_id: int) -> bool:
    return user_id in owner or user_id in sudo_users


@Client.on_message(filters.command("community"))
async def set_community(client: Client, message: Message):
    if len(message.command) < 2:
        current = " ".join(f"@{n}" for n in await get_community_names())
        await message.reply(
            f"⚙️ <b>Community Branding</b> <i>(bot-wide)</i>\n\n"
            f"Current: <code>{current}</code>\n\n"
            f"Usage: <code>/community &lt;name&gt;</code> ya 2 naam:\n"
            f"<code>/community @anime_dekhle @kyami_animes_india</code>\n\n"
            f"1 naam set hai to caption/post mein 1 username dikhega, 2 hain to "
            f"dono dikhenge. Thumbnail band aur metadata title mein pehla naam use hota hai.\n\n"
            f"Reset karne ke liye: <code>/communityclear</code>",
        )
        return

    if not _is_auth(message.from_user.id):
        await message.reply("❌ Sirf owner/sudo hi community naam badal sakte hain (yeh bot-wide setting hai).")
        return

    raw_args = message.command[1:]
    if len(raw_args) > MAX_COMMUNITY_NAMES:
        await message.reply(f"❌ Zyada se zyada {MAX_COMMUNITY_NAMES} usernames allowed hain.")
        return
    names = parse_names(" ".join(raw_args))
    if (not names or len(names) != len({a.strip().lstrip('@').lower() for a in raw_args})
            or any(not n.replace("_", "").isalnum() for n in names)):
        await message.reply(
            "❌ Sahi naam do (sirf letters/numbers/underscore, har naam alag).\n"
            "Example: <code>/community @anime_dekhle @kyami_animes_india</code>"
        )
        return

    await db.set_community(" ".join(names))
    shown = " ".join(f"@{n}" for n in names)
    await message.reply(
        f"✅ <b>Community set:</b> <code>{shown}</code> <i>(bot-wide, sabke liye)</i>\n\n"
        f"Ab caption / post ki \"Powered By\" / \"Main Channel\" line mein "
        f"{'ye username' if len(names) == 1 else 'ye dono usernames'} aayenge. "
        f"Thumbnail band aur metadata title mein <code>@{names[0].upper()}</code> "
        f"use hoga — manual aur auto-monitor dono uploads mein."
    )


@Client.on_message(filters.command("communityclear"))
async def clear_community(client: Client, message: Message):
    if not _is_auth(message.from_user.id):
        await message.reply("❌ Sirf owner/sudo hi community naam reset kar sakte hain.")
        return
    await db.set_community(None)
    await message.reply(
        f"🔄 Community reset ho gaya, ab default "
        f"<code>{DEFAULT_COMMUNITY}</code> use hoga (sabke liye)."
    )
