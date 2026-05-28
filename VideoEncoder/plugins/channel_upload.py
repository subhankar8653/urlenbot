"""
Auto Channel Upload Plugin
Commands:
  /addchannel  → Channel link karo anime ke saath
  /seechannel  → Linked channels dekho
  /delchannel  → Channel remove karo
"""

import re

from pyrogram import Client, filters
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from .. import app
from ..utils.database.access_db import db
from ..utils.helper import check_chat


# ─────────────────────────────────────────────
#  /addchannel command
# ─────────────────────────────────────────────
@Client.on_message(filters.command("addchannel"))
async def cmd_addchannel(bot: Client, message: Message):
    """
    Usage:
      /addchannel [AnimeName]             → All audio
      /addchannel [AnimeName] [Language]  → Specific language (Hindi, Tamil, etc.)
      /addchannel -100xxxx [AnimeName]    → Channel ID se
      /addchannel -100xxxx [AnimeName] [Language]

    Ya forwarded post reply karke:
      Reply to forwarded post + /addchannel [AnimeName] [Language]
    """
    user_id = message.from_user.id

    args = message.text.split(None, 1)

    if len(args) < 2:
        await message.reply(
            "<b>📢 Add Channel for Auto Upload</b>\n\n"
            "<b>Method 1 — Forward post reply:</b>\n"
            "Forward karo channel se koi bhi post,\n"
            "phir reply karo: <code>/addchannel [AnimeName]</code>\n\n"
            "<b>Method 2 — Channel ID:</b>\n"
            "<code>/addchannel -100xxxx [AnimeName]</code>\n\n"
            "<b>Language specify karna (optional):</b>\n"
            "<code>/addchannel [Naruto] [Hindi]</code>\n"
            "<code>/addchannel [Naruto] [Hindi+Tamil]</code>\n"
            "<code>/addchannel [Naruto] [All]</code> ← default\n\n"
            "<b>Supported languages:</b>\n"
            "Hindi, English, Tamil, Telugu, Japanese,\n"
            "Korean, Chinese, Bengali, Marathi, All",
            parse_mode="HTML"
        )
        return

    text = args[1].strip()
    channel_id = None
    channel_title = None

    # ── Method 1: Forwarded post se channel ID nikalo ──
    if message.reply_to_message and message.reply_to_message.forward_from_chat:
        fwd_chat = message.reply_to_message.forward_from_chat
        channel_id = fwd_chat.id
        channel_title = fwd_chat.title

    # ── Method 2: Pehla argument channel ID hai kya? ──
    if channel_id is None:
        parts = text.split(None, 1)
        if parts[0].lstrip('-').isdigit():
            try:
                channel_id = int(parts[0])
                text = parts[1].strip() if len(parts) > 1 else ""
                try:
                    chat = await bot.get_chat(channel_id)
                    channel_title = chat.title
                except Exception:
                    await message.reply(
                        f"❌ Channel <code>{channel_id}</code> nahi mila!\n\n"
                        "Bot ko channel mein admin banana zaroori hai.",
                        parse_mode="HTML"
                    )
                    return
            except ValueError:
                pass

    if channel_id is None:
        await message.reply(
            "❌ <b>Channel nahi mila!</b>\n\n"
            "Ya toh forwarded post reply karo,\n"
            "ya Channel ID provide karo.\n\n"
            "<code>/addchannel -100xxxx [AnimeName]</code>",
            parse_mode="HTML"
        )
        return

    # ── [AnimeName] aur [Language] parse karo ──
    brackets = re.findall(r'\[([^\]]+)\]', text)

    if len(brackets) < 1:
        await message.reply(
            "❌ <b>Anime name chahiye!</b>\n\n"
            f"Channel: <b>{channel_title}</b>\n\n"
            "Format: <code>/addchannel [AnimeName]</code>\n"
            "Ya: <code>/addchannel [AnimeName] [Hindi]</code>",
            parse_mode="HTML"
        )
        return

    anime_name = brackets[0].strip()
    languages = brackets[1].strip() if len(brackets) >= 2 else "All"

    # ── Bot ko channel mein admin check karo ──
    try:
        bot_me = await bot.get_me()
        bot_member = await bot.get_chat_member(channel_id, bot_me.id)
        if bot_member.status.name not in ["ADMINISTRATOR", "OWNER"]:
            await message.reply(
                f"❌ <b>Bot channel mein admin nahi hai!</b>\n\n"
                f"📢 Channel: <b>{channel_title}</b>\n\n"
                "Pehle bot ko admin banao, phir try karo.",
                parse_mode="HTML"
            )
            return
    except Exception as e:
        await message.reply(
            f"❌ <b>Channel access verify nahi ho saka!</b>\n\n"
            f"Error: <code>{str(e)[:80]}</code>\n\n"
            "Bot ko channel mein admin banana zaroori hai.",
            parse_mode="HTML"
        )
        return

    # ── Duplicate check ──
    existing = await db.get_channels(user_id)
    for ch in existing:
        if (
            ch.get('channel_id') == channel_id
            and ch.get('anime', '').lower() == anime_name.lower()
            and ch.get('languages', 'All').lower() == languages.lower()
        ):
            await message.reply(
                f"⚠️ <b>Already exists!</b>\n\n"
                f"📺 Anime: <b>{anime_name}</b>\n"
                f"📢 Channel: <b>{channel_title}</b>\n"
                f"🎧 Language: <b>{languages}</b>",
                parse_mode="HTML"
            )
            return

    # ── Database mein save karo ──
    channel_info = {
        'channel_id': channel_id,
        'channel_title': channel_title or str(channel_id),
        'anime': anime_name,
        'languages': languages,
    }
    await db.add_channel(user_id, channel_info)

    lang_display = "All audio tracks" if languages.lower() == "all" else f"Sirf {languages}"

    await message.reply(
        f"✅ <b>Channel Added!</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📺 <b>Anime:</b> {anime_name}\n"
        f"📢 <b>Channel:</b> {channel_title}\n"
        f"🆔 <b>ID:</b> <code>{channel_id}</code>\n"
        f"🎧 <b>Audio:</b> {lang_display}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Ab jab bhi <b>{anime_name}</b> download hoga,\n"
        f"automatically is channel mein upload ho jaayega! 🚀\n\n"
        f"📋 Dekhne ke liye: /seechannel\n"
        f"🗑️ Remove karne ke liye: /delchannel",
        parse_mode="HTML"
    )


# ─────────────────────────────────────────────
#  /seechannel command
# ─────────────────────────────────────────────
@Client.on_message(filters.command("seechannel"))
async def cmd_seechannel(bot: Client, message: Message):
    """User ke linked channels dikhao."""
    user_id = message.from_user.id
    channels = await db.get_channels(user_id)

    if not channels:
        await message.reply(
            "📢 <b>Koi channel linked nahi hai!</b>\n\n"
            "Channel add karne ke liye:\n"
            "<code>/addchannel [AnimeName]</code>\n\n"
            "<b>Example:</b>\n"
            "<code>/addchannel [Naruto] [Hindi]</code>",
            parse_mode="HTML"
        )
        return

    text = "📢 <b>Your Linked Channels</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"

    for i, ch in enumerate(channels, 1):
        anime = ch.get('anime', 'Unknown')
        ch_title = ch.get('channel_title', 'Unknown')
        ch_id = ch.get('channel_id', 'N/A')
        lang = ch.get('languages', 'All')

        text += (
            f"<b>{i}.</b> 📺 <b>{anime}</b>\n"
            f"   📢 {ch_title}\n"
            f"   🆔 <code>{ch_id}</code>\n"
            f"   🎧 {lang}\n\n"
        )

    text += (
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 Total: <b>{len(channels)}</b> channels\n\n"
        f"🗑️ Remove: <code>/delchannel 1</code>"
    )

    await message.reply(text, parse_mode="HTML")


# ─────────────────────────────────────────────
#  /delchannel command
# ─────────────────────────────────────────────
@Client.on_message(filters.command("delchannel"))
async def cmd_delchannel(bot: Client, message: Message):
    """Channel remove karo."""
    user_id = message.from_user.id
    channels = await db.get_channels(user_id)

    if not channels:
        await message.reply(
            "📢 <b>Koi channel linked nahi hai!</b>",
            parse_mode="HTML"
        )
        return

    # Agar number nahi diya toh list dikhao
    if len(message.command) < 2:
        text = "🗑️ <b>Konsa channel remove karna hai?</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"
        for i, ch in enumerate(channels, 1):
            anime = ch.get('anime', 'Unknown')
            ch_title = ch.get('channel_title', 'Unknown')
            lang = ch.get('languages', 'All')
            text += f"<b>{i}.</b> [{anime}] → {ch_title} ({lang})\n"
        text += "\n━━━━━━━━━━━━━━━━━━━━\n"
        text += "Use: <code>/delchannel &lt;number&gt;</code>\n"
        text += "Example: <code>/delchannel 1</code>"
        await message.reply(text, parse_mode="HTML")
        return

    try:
        num = int(message.command[1])
    except ValueError:
        await message.reply("❌ Sahi number dalo! Example: <code>/delchannel 1</code>", parse_mode="HTML")
        return

    if num < 1 or num > len(channels):
        await message.reply(
            f"❌ Invalid number! 1 se {len(channels)} tak dalo.",
            parse_mode="HTML"
        )
        return

    removed = channels[num - 1]
    success = await db.remove_channel(user_id, num - 1)

    if success:
        # Bot ko channel se leave karwao agar koi aur entry nahi hai us channel ki
        remaining = await db.get_channels(user_id)
        still_has = any(ch.get('channel_id') == removed.get('channel_id') for ch in remaining)

        leave_msg = ""
        if not still_has:
            try:
                await bot.leave_chat(removed['channel_id'])
                leave_msg = "\n🚪 Bot ne channel leave kar diya."
            except Exception:
                leave_msg = ""

        await message.reply(
            f"✅ <b>Channel Removed!</b>\n\n"
            f"📺 Anime: <b>{removed.get('anime')}</b>\n"
            f"📢 Channel: <b>{removed.get('channel_title')}</b>\n"
            f"🎧 Language: <b>{removed.get('languages', 'All')}</b>"
            f"{leave_msg}",
            parse_mode="HTML"
        )
    else:
        await message.reply("❌ Remove karne mein error aaya. Dobara try karo.")
