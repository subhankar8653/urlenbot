from pyrogram import Client, filters
from pyrogram.types import Message
from ..utils.database.access_db import db


@Client.on_message(filters.command("swap"))
async def set_swap(client, message: Message):
    if len(message.command) < 2:
        await message.reply("Usage:\n/swap old1:new1|old2:new2\n\nExample:\n/swap toonworld.com:@SBANIME|raretoon.com:@SBANIME")
        return
    raw = " ".join(message.command[1:])
    rules = {}
    for pair in raw.strip().split("|"):
        pair = pair.strip()
        if ":" not in pair:
            continue
        old, new = pair.split(":", 1)
        if old.strip():
            rules[old.strip()] = new.strip()
    if not rules:
        await message.reply("Format: /swap old:new|old2:new2")
        return
    existing = await db.get_swap(message.from_user.id)
    existing.update(rules)
    await db.set_swap(message.from_user.id, existing)
    text = "Swap Rules Set!\n\n"
    for o, n in existing.items():
        text += f"- {o} -> {n}\n"
    await message.reply(text)


@Client.on_message(filters.command("swapclear"))
async def swap_clear(client, message: Message):
    await db.clear_swap(message.from_user.id)
    await message.reply("Sab swap rules clear ho gaye!")
