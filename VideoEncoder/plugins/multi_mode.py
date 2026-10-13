"""/multi_mode [on|off] — multi update-channel mode ka switch (default OFF)."""

from pyrogram import Client, filters
from pyrogram.types import Message

from .. import owner, sudo_users
from ..utils.multi_mode import is_multi, set_multi


@Client.on_message(filters.command("multi_mode") & filters.private)
async def cmd_multi_mode(client: Client, message: Message):
    uid = message.from_user.id if message.from_user else 0
    if uid not in owner and uid not in sudo_users:
        return

    parts = message.text.split()
    if len(parts) < 2 or parts[1].lower() not in ("on", "off"):
        on = await is_multi()
        await message.reply(
            f"🔀 **Multi Mode:** {'🟢 ON' if on else '🔴 OFF'}\n\n"
            "• `/multi_mode on` — chalu karo\n"
            "• `/multi_mode off` — band karo (pehle jaisa behaviour)\n\n"
            "**ON hone par:**\n"
            "• `/add_anime` mein Link 1, Link 2... add kar sakte ho "
            "(Link 1 → Channel 01, Link 2 → Channel 02)\n"
            "• `/customise` → Main Channel Post Buttons mein har update channel ka alag button-set\n"
            "• `/update_channel <id> <N>` — naya channel Channel N ka clone banega\n\n"
            "OFF mein sab kuch bilkul pehle jaisa chalta hai."
        )
        return

    enable = parts[1].lower() == "on"
    await set_multi(enable)
    if enable:
        await message.reply(
            "🟢 **Multi Mode: ON**\n\n"
            "Ab:\n"
            "• `/add_anime` ke link step mein multiple links\n"
            "• Main Channel Post Buttons mein channel-wise edit\n"
            "• `/update_channel -100xxxx 1` = Channel 01 ka clone\n\n"
            "Jin channels/anime ke liye alag setting nahi ki, unme Link 1 aur default buttons hi jaayenge."
        )
    else:
        await message.reply(
            "🔴 **Multi Mode: OFF**\n\n"
            "Ab sab update channels pe same post, Link 1 aur default buttons jaayenge. "
            "Channel-wise settings save rahengi — `/multi_mode on` karne par wapas kaam karengi."
        )
