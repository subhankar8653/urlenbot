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


# ─── /testemoji — Test premium custom emoji in channel ───────────────────────
#
# Usage:
#   /testemoji -100XXXXXXXXX Your message text 🔥
#   /testemoji -100XXXXXXXXX Your message text [emoji_id:5368324170671202286]
#
# - Agar message mein actual premium emoji hai → uski entity use hogi
# - Agar [emoji_id:XXXX] likha hai → us ID se custom emoji entity banayi jaegi
# - Dono ek saath bhi ho sakte hain
# ─────────────────────────────────────────────────────────────────────────────
import re as _re
from pyrogram.enums import ParseMode as _ParseMode
from pyrogram.types import MessageEntity as _MessageEntity

@Client.on_message(filters.command("testemoji") & filters.private)
async def test_emoji_cmd(client, message: Message):
    """
    /testemoji <channel_id> <text with emoji or [emoji_id:ID]>
    """
    args = message.text.split(None, 2)
    if len(args) < 3:
        await message.reply(
            "Usage:\n"
            "<code>/testemoji -100XXXXXXXXX Hello 🔥</code>\n"
            "  → channel pe actual emoji entity ke saath bhejta hai\n\n"
            "<code>/testemoji -100XXXXXXXXX Hello [emoji_id:5368324170671202286]</code>\n"
            "  → custom emoji ID se placeholder inject karta hai\n\n"
            "Dono ek saath:\n"
            "<code>/testemoji -100XXXXXXXXX 🔥 Hello [emoji_id:5368324170671202286] World</code>",
            parse_mode=_ParseMode.HTML,
        )
        return

    try:
        channel_id = int(args[1])
    except ValueError:
        await message.reply("❌ Channel ID galat hai. Example: -1003996709628")
        return

    raw_text = args[2]

    # ── Step 1: [emoji_id:XXXX] tags ko placeholder char se replace karo ──
    # Har tag ke liye ek unique placeholder char use karo
    PLACEHOLDER = "⭐"  # 1 char, iske upar entity lagegi
    emoji_id_pattern = _re.compile(r'\[emoji_id:(\d+)\]')
    injected_ids = []  # (offset_in_final_text, emoji_id)

    def replace_tag(m):
        # group(1) for [emoji_id:X] format, group(2) for emoji_id:X format
        eid = m.group(1) or (m.group(2) if m.lastindex and m.lastindex >= 2 else None)
        if eid:
            injected_ids.append(eid)
        return PLACEHOLDER

    processed_text = emoji_id_pattern.sub(replace_tag, raw_text)

    # ── Step 2: Entities banao ──
    entities = []

    # 2a. Message ke existing custom_emoji entities (agar user ne premium emoji type kiya)
    if message.entities:
        # command + space + channel_id + space = skip offset
        cmd_len = len(args[0]) + 1 + len(args[1]) + 1
        for ent in message.entities:
            # Pyrogram mein ent.type ek enum hai — str() se compare karo
            ent_type = str(ent.type).split(".")[-1].lower()  # "MessageEntityType.CUSTOM_EMOJI" → "custom_emoji"
            if ent_type == "custom_emoji" and ent.offset >= cmd_len:
                new_offset = ent.offset - cmd_len
                entities.append(_MessageEntity(
                    type="custom_emoji",
                    offset=new_offset,
                    length=ent.length,
                    custom_emoji_id=ent.custom_emoji_id,
                ))

    # 2b. [emoji_id:XXXX] se inject kiye gaye emojis
    search_from = 0
    for eid in injected_ids:
        idx = processed_text.find(PLACEHOLDER, search_from)
        if idx != -1:
            entities.append(_MessageEntity(
                type="custom_emoji",
                offset=idx,
                length=len(PLACEHOLDER),
                custom_emoji_id=str(eid),
            ))
            search_from = idx + 1

    # ── Step 3: Channel pe bhejo ──
    try:
        sent = await client.send_message(
            chat_id=channel_id,
            text=processed_text,
            parse_mode=_ParseMode.DISABLED,
            entities=entities if entities else None,
            disable_web_page_preview=True,
        )
        result_lines = [f"✅ Sent! msg_id={sent.id}"]
        result_lines.append(f"📝 Text: <code>{processed_text}</code>")
        if entities:
            for e in entities:
                result_lines.append(f"• custom_emoji offset={e.offset} id={e.custom_emoji_id}")
        else:
            result_lines.append("⚠️ Koi entity nahi bani — plain text bheja")
        await message.reply("\n".join(result_lines), parse_mode=_ParseMode.HTML)
    except Exception as e:
        await message.reply(f"❌ Error: <code>{e}</code>", parse_mode=_ParseMode.HTML)
