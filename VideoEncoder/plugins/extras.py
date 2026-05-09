"""
Extra Commands:
/swap - Caption text swap rules set karo
/swaplist - Current swap rules dekho
/swapclear - Sab swap rules hata do
/setpic - Thumbnail + cover pic set karo
/clearpic - Thumbnail + cover pic hata do
"""

from pyrogram import Client, filters
from pyrogram.types import Message

from .. import app
from ..utils.database.access_db import db
from ..utils.helper import check_chat, AddUserToDatabase


# ─── /swap command ───────────────────────────────────────────
@Client.on_message(filters.command("swap"))
async def set_swap(client, message: Message):
    c = await check_chat(message, chat='Both')
    if not c:
        return
    await AddUserToDatabase(client, message)

    if len(message.command) < 2:
        await message.reply(
            "**Usage:**\n"
            "`/swap old1:new1|old2:new2`\n\n"
            "**Example:**\n"
            "`/swap toonworld.com:@SBANIME|raretoon.com:@SBANIME|1080p:480p`\n\n"
            "Har rule `|` se alag karo, old aur new `:` se."
        )
        return

    raw = " ".join(message.command[1:])
    pairs = raw.strip().split("|")
    rules = {}
    errors = []

    for pair in pairs:
        pair = pair.strip()
        if ":" not in pair:
            errors.append(f"❌ `{pair}` — `:` missing")
            continue
        old, new = pair.split(":", 1)
        old = old.strip()
        new = new.strip()
        if not old:
            errors.append(f"❌ Empty old value in `{pair}`")
            continue
        rules[old] = new

    if not rules:
        await message.reply("❌ Koi valid rule nahi mila!\n\nFormat: `/swap old:new|old2:new2`")
        return

    # Get existing rules and merge
    existing = await db.get_swap(message.from_user.id)
    existing.update(rules)
    await db.set_swap(message.from_user.id, existing)

    text = "✅ **Swap Rules Set!**\n\n"
    for o, n in existing.items():
        text += f"• `{o}` → `{n}`\n"

    if errors:
        text += "\n⚠️ **Errors:**\n" + "\n".join(errors)

    await message.reply(text)


# ─── /swaplist command ───────────────────────────────────────
@Client.on_message(filters.command("swaplist"))
async def swap_list(client, message: Message):
    c = await check_chat(message, chat='Both')
    if not c:
        return

    rules = await db.get_swap(message.from_user.id)
    if not rules:
        await message.reply("ℹ️ Koi swap rule set nahi hai.\n\nSet karo: `/swap old:new`")
        return

    text = "📋 **Current Swap Rules:**\n\n"
    for o, n in rules.items():
        text += f"• `{o}` → `{n}`\n"
    await message.reply(text)


# ─── /swapclear command ──────────────────────────────────────
@Client.on_message(filters.command("swapclear"))
async def swap_clear(client, message: Message):
    c = await check_chat(message, chat='Both')
    if not c:
        return

    await db.clear_swap(message.from_user.id)
    await message.reply("✅ Sab swap rules clear ho gaye!")


# ─── /setpic command ─────────────────────────────────────────
@Client.on_message(filters.command("setpic"))
async def set_pic(client, message: Message):
    c = await check_chat(message, chat='Both')
    if not c:
        return
    await AddUserToDatabase(client, message)

    # Photo reply mein honi chahiye
    reply = message.reply_to_message
    if not reply or not reply.photo:
        await message.reply(
            "**Usage:**\n"
            "Ek photo ko reply karo aur `/setpic` likho.\n\n"
            "Yeh photo:\n"
            "• Video ka **thumbnail** ban jaayega\n"
            "• Cover pic ki tarah use hogi"
        )
        return

    file_id = reply.photo.file_id
    await db.set_thumbnail(message.from_user.id, file_id)
    await db.set_coverpic(message.from_user.id, file_id)

    await message.reply(
        "✅ **Pic Set Ho Gayi!**\n\n"
        "Ab se:\n"
        "• Har encoded video ka thumbnail yahi hoga 🖼️\n\n"
        "Hata'ne ke liye: `/clearpic`"
    )


# ─── /clearpic command ───────────────────────────────────────
@Client.on_message(filters.command("clearpic"))
async def clear_pic(client, message: Message):
    c = await check_chat(message, chat='Both')
    if not c:
        return

    await db.set_thumbnail(message.from_user.id, None)
    await db.clear_coverpic(message.from_user.id)
    await message.reply("✅ Thumbnail aur cover pic hata di gayi!")
