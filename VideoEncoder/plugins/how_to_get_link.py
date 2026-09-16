"""
how_to_get_link.py
====================
/how_to_get_link [url|remove] — GLOBAL (bot-wide) link jo styled update-post
templates (`/update_post_style` ke style1..style10, "Default" mein nahi) ke
neeche ek clickable "➥ ʜᴏᴡ ᴛᴏ ɢᴇᴛ ʟɪɴᴋ" line ke roop mein dikhta hai.

  /how_to_get_link                 -> abhi kya set hai, wo dikhao
  /how_to_get_link <url>           -> set/update karo
  /how_to_get_link remove          -> hata do (line future posts mein
                                       nahi aayegi)

DB: col2, id='how_to_get_link', {url: str}  (utils/update_post_style.py
    ke get_how_to_get_link()/set_how_to_get_link() se accessed).
"""

from pyrogram import Client, filters
from pyrogram.types import Message

from .. import owner, sudo_users
from ..utils import update_post_style


def _is_auth(user_id: int) -> bool:
    return user_id in owner or user_id in sudo_users


@Client.on_message(filters.command("how_to_get_link") & filters.private)
async def cmd_how_to_get_link(client: Client, message: Message):
    if not _is_auth(message.from_user.id):
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        current = await update_post_style.get_how_to_get_link()
        if current:
            await message.reply(
                f"🔗 **How to get link — abhi set hai:**\n`{current}`\n\n"
                "Update karna ho toh: `/how_to_get_link <naya link>`\n"
                "Hatana ho toh: `/how_to_get_link remove`"
            )
        else:
            await message.reply(
                "📭 **How to get link** abhi set nahi hai.\n\n"
                "Set karo: `/how_to_get_link <link>`\n"
                "_Set hone ke baad, styled update-posts (`/update_post_style` ke "
                "1-10 waale styles) ke neeche ek clickable_ **➥ ʜᴏᴡ ᴛᴏ ɢᴇᴛ ʟɪɴᴋ** "
                "_line add ho jaayegi. \"Default\" style mein nahi aati._"
            )
        return

    arg = parts[1].strip()

    if arg.lower() in ("remove", "off", "clear", "none"):
        current = await update_post_style.get_how_to_get_link()
        if not current:
            await message.reply("📭 Pehle se hi koi link set nahi hai.")
            return
        await update_post_style.set_how_to_get_link("")
        await message.reply(
            "🗑️ **How to get link remove kar diya!**\n\n"
            "Ab se styled update-posts mein **➥ ʜᴏᴡ ᴛᴏ ɢᴇᴛ ʟɪɴᴋ** line nahi aayegi."
        )
        return

    if not (arg.startswith("http://") or arg.startswith("https://") or arg.startswith("t.me/")):
        await message.reply(
            "❌ Yeh ek valid link nahi lagta. `http(s)://` se shuru hona chahiye.\n"
            "Example: `/how_to_get_link https://t.me/YourChannel/123`"
        )
        return

    await update_post_style.set_how_to_get_link(arg)
    await message.reply(
        f"✅ **How to get link set kar diya!**\n`{arg}`\n\n"
        "✨ _Ab se styled update-posts (Default ke alawa) ke neeche "
        "**➥ ʜᴏᴡ ᴛᴏ ɢᴇᴛ ʟɪɴᴋ** clickable line dikhegi._"
    )
