"""/btncolors on|off — coloured buttons runtime toggle (Sudo)."""
from pyrogram import Client, filters
from pyrogram.types import Message

from ..utils import btn_color
from ..utils.helper import check_chat


@Client.on_message(filters.command("btncolors"))
async def btncolors_cmd(client: Client, message: Message):
    c = await check_chat(message, chat="Sudo")
    if not c:
        return
    parts = message.text.split()
    if len(parts) >= 2 and parts[1].lower() in ("on", "off"):
        btn_color.ENABLED = parts[1].lower() == "on"
    state = "ON 🟢" if btn_color.ENABLED else "OFF 🔴"
    await message.reply(
        f"🎨 **Button Colours:** {state}\n\n"
        f"`/btncolors on` / `/btncolors off`\n"
        f"🟢 Download/Apply/Save • 🔴 Close/Cancel/Delete • 🔵 Back/Next/Menu"
    )
