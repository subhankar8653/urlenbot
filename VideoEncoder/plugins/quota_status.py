"""/quota — upload quota ka live status (kitne chal rahe hain, kitne queue mein)."""

from pyrogram import Client, filters
from pyrogram.types import Message

from .. import owner, sudo_users
from ..utils import quota


@Client.on_message(filters.command("quota"))
async def cmd_quota(client: Client, message: Message):
    uid = message.from_user.id if message.from_user else 0
    if uid not in owner and uid not in sudo_users:
        return
    st = quota.status()
    running = "Multi (akela)" if st["multi"] else f"{st['single']}/{st['max_single']} single"
    queued = st["queued"]
    q_line = (
        "Queue: khali"
        if not queued else
        f"Queue: {queued} — " + ", ".join(
            f"#{i} {'multi' if k == 'multi' else 'single'}" for i, k in enumerate(st["queued_kinds"], 1)
        )
    )
    await message.reply(
        "📊 **Upload Quota**\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"▶️ Chal raha hai: {running}\n"
        f"⏳ {q_line}\n\n"
        f"Rule: ek saath max {st['max_single']} single (1 episode) upload, "
        "ya sirf 1 multi (2+ episodes) upload."
    )
