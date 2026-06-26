"""
bot_upload.py
==============
Global config commands + /bot_upload pipeline.

Commands:
  /set_end <template>     — End message template (placeholders: {anime_name}, {season},
                             {q480}, {q720}, {q1080})
  /border                 — Start border-sticker setup. Send sticker, then /done
  /season_sticker         — Start/continue season-sticker collection. Send sticker
                             per season in order, then /done
  /bot_upload <channel_id> <anime_name> | <season_no> | <source>
                           — Full pipeline: IMDB info →
                             episode upload (RTI/url -e) → batch links →
                             border → batch summary → /set_end → next season sticker →
                             default auto-upload end messages.

  /bot_upload <post_link> | rti <rti_url> <ep_num>
                           — EXISTING POST mode (single). post_link ek already-bani
                             hui channel post ka link hai (https://t.me/c/<id>/<msgid>).
                             Episode ep_num upload karke, sirf usi post ke quality
                             button-row mein same-slot-replace karega:
                               360p/480p ek slot share karte hain (jo bhi naya aaye
                               wahi dikhega), 720p/1080p apne alag slots mein replace
                               hote hain. Baaki (non-quality) buttons untouched.

  /bot_upload <start_post_link> <end_msg_id> | url <link> -e <ep_start> <ep_end> <quality>
                           — EXISTING POST mode (batch). start_post_link se end_msg_id
                             tak SEQUENTIAL message_ids ko episodes ep_start..ep_end
                             se map kiya jaata hai (msg_id N = episode ep_start + (N - start)).
                             Har episode ki file upload hoke uski matching post pe
                             diye gaye <quality> ka button same-slot-replace hota hai.
"""

import asyncio
import os
import random
import re
import string

import aiohttp
from pyrogram import Client, filters, ContinuePropagation
from pyrogram.errors import FloodWait
from pyrogram.types import Message

from .. import LOGGER, app
from ..utils.database.access_db import db
from ..utils.helper import check_chat, output

OMDB_API_KEY = os.getenv("OMDB_API_KEY", "")

# ─────────────────────────────────────────────────────────────────────────
#  /getemoji — Premium emoji ka custom_emoji_id nikalo
#  Use: Bot ko koi bhi premium emoji wala message bhejo
#  Bot reply karega us emoji ka ID lekar
# ─────────────────────────────────────────────────────────────────────────
@Client.on_message(filters.command("getemoji"))
async def get_emoji_cmd(bot: Client, message: Message):
    """Reply mein premium emoji bhejo — uska ID milega."""
    reply = message.reply_to_message
    target = reply if reply else message

    found = []
    if target.entities:
        for e in target.entities:
            if e.type.value == "custom_emoji" and e.custom_emoji_id:
                found.append(e.custom_emoji_id)
    if target.caption_entities:
        for e in target.caption_entities:
            if e.type.value == "custom_emoji" and e.custom_emoji_id:
                found.append(e.custom_emoji_id)

    if found:
        ids_text = "\n".join(f"<code>{eid}</code>" for eid in found)
        await message.reply(
            f"✅ <b>Custom Emoji ID(s) mili:</b>\n\n{ids_text}\n\n"
            f"<i>Inhe <code>bot_upload_engine.py</code> mein paste karo</i>",
            parse_mode="html",
        )
    else:
        await message.reply(
            "❌ <b>Koi premium emoji nahi mila.</b>\n\n"
            "Is tarah use karo:\n"
            "1. Premium emoji wala message bhejo\n"
            "2. Us message ko reply karo <code>/getemoji</code> se\n\n"
            "<i>Ya seedha <code>/getemoji</code> ke saath premium emoji wala message bhejo</i>",
            parse_mode="html",
        )


# ─── In-memory sessions ────────────────────────────────────────────────────
_border_session: set = set()        # user_ids currently in /border setup mode
_season_session: set = set()        # user_ids currently in /season_sticker setup mode
_text_template_session: dict = {}   # { user_id: "end" }  (set_end inline-prompt mode)



# ─────────────────────────────────────────────────────────────────────────
#  /set_intro & /set_end
# ─────────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────


@Client.on_message(filters.command("set_end"))
async def set_end_cmd(bot: Client, message: Message):
    c = await check_chat(message, chat="Both")
    if not c:
        return

    args = message.text.split(None, 1)
    if len(args) > 1 and args[1].strip():
        await db.set_end_template(message.from_user.id, args[1].strip())
        await message.reply(
            "✅ <b>End template saved!</b>\n\nUse /bot_upload se test karo.",
            reply_markup=output,
        )
        return

    _text_template_session[message.from_user.id] = "end"
    await message.reply(
        "<b>✏️ End template bhejo:</b>\n\n"
        "Placeholders use kar sakte ho:\n"
        "<code>{anime_name}</code> — Anime ka naam\n"
        "<code>{season}</code> — Season number\n"
        "<code>{q480}</code> / <code>{q720}</code> / <code>{q1080}</code> — Batch links\n\n"
        "<i>Send <code>-</code> to cancel.</i>",
    )


@Client.on_message(filters.text & filters.private & ~filters.command([
    "set_end", "border", "season_sticker", "done", "bot_upload"
]), group=4)
async def template_text_input(bot: Client, message: Message):
    """Catches the next text message after the /set_end inline prompt."""
    user_id = message.from_user.id

    kind = _text_template_session.get(user_id)
    if not kind:
        return  # not our business

    text = message.text.strip()
    if text == "-":
        _text_template_session.pop(user_id, None)
        await message.reply("❌ Cancelled.")
        return

    _text_template_session.pop(user_id, None)
    await db.set_end_template(user_id, text)
    await message.reply("✅ <b>End template saved!</b>", reply_markup=output)


# ─────────────────────────────────────────────────────────────────────────
#  /border  +  /done
# ─────────────────────────────────────────────────────────────────────────
@Client.on_message(filters.command("border"))
async def border_cmd(bot: Client, message: Message):
    c = await check_chat(message, chat="Both")
    if not c:
        return

    _border_session.add(message.from_user.id)
    await message.reply(
        "<b>🖼️ Border sticker bhejo</b>\n\n"
        "Ye border har episode ke saath channel pe bhejega.\n"
        "Sticker bhejne ke baad <code>/done</code> karo.",
    )


@Client.on_message(filters.sticker & filters.private)
async def border_or_season_sticker_handler(bot: Client, message: Message):
    user_id = message.from_user.id

    if user_id in _border_session:
        await db.set_bot_border(user_id, message.sticker.file_id, "sticker")
        await message.reply(
            "✅ Border sticker save ho gaya (preview)!\n"
            "Aur badalna hai toh phir se sticker bhejo, ya <code>/done</code> karo.",
        )
        return

    if user_id in _season_session:
        stickers = await db.get_season_stickers(user_id)
        stickers.append({"file_id": message.sticker.file_id, "type": "sticker"})
        await db.set_season_stickers(user_id, stickers)
        await message.reply(
            f"✅ <b>Season {len(stickers)}</b> ka sticker save ho gaya!\n\n"
            f"Ab <b>Season {len(stickers) + 1}</b> ka sticker bhejo, "
            f"ya <code>/done</code> karo agar khatam.",
        )
        return


# Photo-based border/season sticker support (some users send images instead of stickers)
@Client.on_message(filters.photo & filters.private)
async def border_or_season_photo_handler(bot: Client, message: Message):
    user_id = message.from_user.id

    if user_id in _border_session:
        await db.set_bot_border(user_id, message.photo.file_id, "photo")
        await message.reply(
            "✅ Border photo save ho gaya!\n"
            "Aur badalna hai toh phir se bhejo, ya <code>/done</code> karo.",
        )
        return

    if user_id in _season_session:
        stickers = await db.get_season_stickers(user_id)
        stickers.append({"file_id": message.photo.file_id, "type": "photo"})
        await db.set_season_stickers(user_id, stickers)
        await message.reply(
            f"✅ <b>Season {len(stickers)}</b> ka photo save ho gaya!\n\n"
            f"Ab <b>Season {len(stickers) + 1}</b> ka sticker/photo bhejo, "
            f"ya <code>/done</code> karo agar khatam.",
        )
        return

    # Koi border/season session match nahi hua — is photo se humein kuch
    # nahi karna. Pyrogram same group mein sirf EK handler chalata hai jab
    # tak ContinuePropagation na ho, isliye yahan explicitly continue karo
    # taaki dusre plugins (jaise update_channel.py ka image-upload step)
    # is photo ko process kar sakein.
    raise ContinuePropagation


# ─────────────────────────────────────────────────────────────────────────
#  /season_sticker  +  /done
# ─────────────────────────────────────────────────────────────────────────
@Client.on_message(filters.command("season_sticker"))
async def season_sticker_cmd(bot: Client, message: Message):
    c = await check_chat(message, chat="Both")
    if not c:
        return

    _season_session.add(message.from_user.id)
    existing = await db.get_season_stickers(message.from_user.id)
    next_season = len(existing) + 1
    await message.reply(
        f"<b>🎟️ Season Sticker Setup</b>\n\n"
        f"Ab tak <b>{len(existing)}</b> season ke sticker saved hain.\n\n"
        f"<b>Season {next_season}</b> ka sticker bhejo.\n"
        f"Har sticker ke baad agle season ka maanga jaayega.\n"
        f"Khatam karne ke liye <code>/done</code> bhejo.",
    )


@Client.on_message(filters.command("done"))
async def done_cmd(bot: Client, message: Message):
    user_id = message.from_user.id
    did_something = False

    if user_id in _border_session:
        _border_session.discard(user_id)
        border = await db.get_bot_border(user_id)
        if border:
            await message.reply("✅ <b>Border set ho gaya!</b> Ab /bot_upload use kar sakte ho.")
        else:
            await message.reply("⚠️ Koi border sticker save nahi hua.")
        did_something = True

    if user_id in _season_session:
        _season_session.discard(user_id)
        stickers = await db.get_season_stickers(user_id)
        await message.reply(
            f"✅ <b>Season stickers set ho gaye!</b>\n"
            f"Total seasons: <b>{len(stickers)}</b>",
        )
        did_something = True

    if not did_something:
        await message.reply("ℹ️ Koi active setup nahi chal raha.")


# ─────────────────────────────────────────────────────────────────────────
#  IMDB info fetch (OMDB API)
# ─────────────────────────────────────────────────────────────────────────
async def _fetch_imdb_info(anime_name: str) -> dict | None:
    """OMDB API se basic info fetch karo. Returns None agar key missing/fail."""
    if not OMDB_API_KEY:
        return None
    try:
        async with aiohttp.ClientSession() as sess:
            url = f"https://www.omdbapi.com/?apikey={OMDB_API_KEY}&t={anime_name}"
            async with sess.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json()
    except Exception as e:
        LOGGER.error(f"[bot_upload] IMDB fetch failed: {e}")
        return None

    if data.get("Response") != "True":
        return None

    return {
        "title": data.get("Title", anime_name),
        "year": data.get("Year", "N/A"),
        "genre": data.get("Genre", "N/A"),
        "plot": data.get("Plot", ""),
        "rating": data.get("imdbRating", "N/A"),
        "poster": data.get("Poster", ""),
    }


def _build_imdb_caption(info: dict, anime_name: str) -> str:
    return (
        f"🎬 <b>{info['title']}</b> ({info['year']})\n\n"
        f"⭐ <b>IMDb Rating:</b> {info['rating']}/10\n"
        f"🎭 <b>Genre:</b> {info['genre']}\n\n"
        f"📖 <b>Plot:</b> {info['plot']}"
    )


def _parse_tme_link(link: str) -> tuple[int, int] | None:
    """
    https://t.me/c/<internal_id>/<msg_id>  →  (chat_id, msg_id)
    chat_id = int("-100" + internal_id). Public links (t.me/username/id) supported
    bhi parse honge but channel_id wahi rahega jo username se resolve hota hai —
    is feature ke liye hum sirf private (/c/) links expect karte hain.
    """
    m = re.search(r"t\.me/c/(\d+)/(\d+)", link)
    if m:
        return int("-100" + m.group(1)), int(m.group(2))
    m = re.search(r"t\.me/([A-Za-z0-9_]+)/(\d+)", link)
    if m:
        # Public channel username — chat_id ko hum yahan resolve nahi kar sakte
        # bina ek extra API call ke; caller ko username pass karna hoga.
        return None
    return None


async def _bot_upload_existing_post_mode(bot: Client, message: Message, rest: str):
    """
    Existing-post mode — naye post nahi banaye jaate, balki kisi already-existing
    channel post ke quality buttons same-slot-replace logic se update kiye jaate hain.

    Single:  <post_link> | rti <rti_url> <ep_num>
    Batch:   <start_post_link> <end_msg_id> | url <link> -e <ep_start> <ep_end> <quality>
    """
    parts = rest.split("|")
    if len(parts) < 2:
        await message.reply(
            "❌ Format galat hai.\n\n"
            "<b>Single:</b> <code>/bot_upload &lt;post_link&gt; | rti &lt;url&gt; &lt;ep_num&gt;</code>\n"
            "<b>Batch:</b> <code>/bot_upload &lt;start_link&gt; &lt;end_msg_id&gt; | url &lt;link&gt; -e &lt;ep_start&gt; &lt;ep_end&gt; &lt;quality&gt;</code>"
        )
        return

    link_part = parts[0].strip()
    source_part = parts[1].strip()
    link_tokens = link_part.split()

    parsed = _parse_tme_link(link_tokens[0])
    if not parsed:
        await message.reply(
            "❌ Invalid post link. Private channel link chahiye: "
            "<code>https://t.me/c/&lt;id&gt;/&lt;msg_id&gt;</code>"
        )
        return
    chat_id, start_msg_id = parsed

    user_id = message.from_user.id

    # ── SINGLE mode: source starts with "rti" (with or without leading /) ──
    source_lower = source_part.lower()
    if source_lower.startswith("rti") or source_lower.startswith("/rti"):
        m = re.match(r"^/?rti\s+(\S+)\s+(\d+)", source_part, re.IGNORECASE)
        if not m:
            await message.reply("❌ <code>rti &lt;url&gt; &lt;ep_num&gt;</code> format galat hai.")
            return
        page_url, ep_num = m.group(1), int(m.group(2))

        if len(link_tokens) > 1:
            await message.reply(
                "ℹ️ Single mode mein sirf ek post_link chahiye "
                "(end_msg_id mat do — wo batch mode ke liye hai)."
            )
            return

        status = await message.reply(
            f"<b>🚀 Existing-post upload started</b>\n"
            f"Post: <code>{link_tokens[0]}</code>\nEpisode: {ep_num}"
        )

        from ..utils.bot_upload_engine import run_episode_rti, update_existing_post_button
        from ..plugins.auto_monitor import _get_suhani_bot_link

        uploaded = await run_episode_rti(app, message, status, page_url, ep_num, ep_num)
        if not uploaded:
            await status.edit(f"❌ Episode {ep_num} — koi quality upload nahi hui.")
            return

        done = []
        for quality, sent_msg in uploaded.items():
            link = await _get_suhani_bot_link(sent_msg)
            if not link:
                continue
            ok = await update_existing_post_button(app, chat_id, start_msg_id, quality, link)
            if ok:
                done.append(quality)

        if done:
            await status.edit(
                f"✅ <b>Done!</b> Episode {ep_num}\n"
                f"Updated qualities on post: {', '.join(done)}"
            )
        else:
            await status.edit(f"❌ Episode {ep_num} — button update fail ho gaya.")
        return

    # ── BATCH mode: source starts with "url" (with or without leading /) ──
    if source_lower.startswith("url") or source_lower.startswith("/url"):
        if len(link_tokens) < 2:
            await message.reply(
                "❌ Batch mode ke liye end_msg_id bhi chahiye:\n"
                "<code>/bot_upload &lt;start_link&gt; &lt;end_msg_id&gt; | url &lt;link&gt; -e &lt;ep_start&gt; &lt;ep_end&gt; &lt;quality&gt;</code>"
            )
            return
        try:
            end_msg_id = int(link_tokens[1])
        except ValueError:
            await message.reply("❌ <code>end_msg_id</code> ek number hona chahiye.")
            return

        m = re.match(
            r"^/?url\s+(\S+)\s+-e\s+(\d+)\s+(\d+)\s+(\d+p)",
            source_part, re.IGNORECASE,
        )
        if not m:
            await message.reply(
                "❌ Format galat hai:\n"
                "<code>url &lt;link&gt; -e &lt;ep_start&gt; &lt;ep_end&gt; &lt;quality&gt;</code>"
            )
            return
        url, ep_start, ep_end, quality = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4).lower()

        if end_msg_id < start_msg_id:
            await message.reply("❌ <code>end_msg_id</code> start se chota nahi ho sakta.")
            return

        post_count = end_msg_id - start_msg_id + 1
        ep_count = ep_end - ep_start + 1
        if post_count != ep_count:
            suggested_end_msg_id = start_msg_id + (ep_count - 1)
            await message.reply(
                f"⚠️ <b>Count match nahi ho raha!</b>\n\n"
                f"📨 Post range: <code>{start_msg_id}</code> → <code>{end_msg_id}</code> = <b>{post_count} posts</b>\n"
                f"🎬 Episode range: <code>{ep_start}</code> → <code>{ep_end}</code> = <b>{ep_count} episodes</b>\n\n"
                f"Dono ka count barabar hona chahiye (1 post = 1 episode, sequential).\n\n"
                f"💡 Agar episodes {ep_start}-{ep_end} hi sahi hain, toh end_msg_id "
                f"<code>{suggested_end_msg_id}</code> hona chahiye.\n\n"
                f"Sahi count ke saath dobara bhejo."
            )
            return

        status = await message.reply(
            f"<b>🚀 Existing-post BATCH upload started</b>\n"
            f"Posts: <code>{start_msg_id}</code> → <code>{end_msg_id}</code>\n"
            f"Episodes: {ep_start} → {ep_end} | Quality: <b>{quality}</b>"
        )

        from ..utils.bot_upload_engine import (
            episode_from_filename, upload_file_to_log, update_existing_post_button,
        )
        from ..plugins.swift_downloader import _quality_from
        from ..plugins.auto_monitor import _get_suhani_bot_link
        from ..plugins.url_upload import _download_url, _extract_archive_all, _safe_filename, apply_urlpreset_to_file
        from ..utils.direct_link_generator import direct_link_generator
        from .. import download_dir as DL_DIR

        direct = direct_link_generator(url)
        if direct:
            url = direct

        await status.edit("<b>💠 Archive download ho raha hai...</b>")
        fname = _safe_filename(os.path.basename(url.split("?")[0]) or "download.zip")
        filepath = await _download_url(url, fname, status, message)
        if not filepath or not os.path.isfile(filepath):
            await status.edit("❌ Download fail ho gaya.")
            return

        all_files = await _extract_archive_all(filepath, status)
        if not all_files:
            return

        # Sirf requested quality ki files chahiye, episode range ke andar
        ep_files: dict = {}
        for fp in all_files:
            ep = episode_from_filename(os.path.basename(fp))
            q = _quality_from(os.path.basename(fp))
            if ep is None or q != quality:
                continue
            if not (ep_start <= ep <= ep_end):
                continue
            ep_files[ep] = fp

        done_eps = []
        failed_eps = []
        for ep_num in range(ep_start, ep_end + 1):
            fp = ep_files.get(ep_num)
            if not fp or not os.path.isfile(fp):
                failed_eps.append(ep_num)
                continue

            msg_id_for_ep = start_msg_id + (ep_num - ep_start)

            await status.edit(f"<b>📤 Ep {ep_num} — {quality}</b> uploading...")
            fp, _has_eng_sub = await apply_urlpreset_to_file(fp, user_id, status)
            success, sent_msg, _q = await upload_file_to_log(app, message, status, fp, DL_DIR)
            if not success or not sent_msg:
                failed_eps.append(ep_num)
                continue

            link = await _get_suhani_bot_link(sent_msg)
            if not link:
                failed_eps.append(ep_num)
                continue

            ok = await update_existing_post_button(app, chat_id, msg_id_for_ep, quality, link)
            if ok:
                done_eps.append(ep_num)
            else:
                failed_eps.append(ep_num)

        summary = f"✅ <b>Batch complete!</b>\nQuality: <b>{quality}</b>\n"
        summary += f"Updated: {len(done_eps)} episode(s)"
        if done_eps:
            summary += f" ({', '.join(map(str, done_eps))})"
        if failed_eps:
            summary += f"\n⚠️ Failed/missing: {', '.join(map(str, failed_eps))}"
        await status.edit(summary)
        return

    await message.reply(
        "❌ Source samajh nahi aaya. <code>rti &lt;url&gt; &lt;ep_num&gt;</code> ya "
        "<code>url &lt;link&gt; -e &lt;ep_start&gt; &lt;ep_end&gt; &lt;quality&gt;</code> use karo."
    )


# ─────────────────────────────────────────────────────────────────────────
#  /bot_upload — Phase 1 skeleton
#  /bot_upload <channel_id> <anime_name> | <season_no>
# ─────────────────────────────────────────────────────────────────────────
@Client.on_message(filters.command("bot_upload"))
async def bot_upload_cmd(bot: Client, message: Message):
    c = await check_chat(message, chat="Both")
    if not c:
        return

    user_id = message.from_user.id
    args = message.text.split(None, 1)

    if len(args) < 2 or "|" not in args[1]:
        await message.reply(
            "<b>Usage:</b>\n"
            "<code>/bot_upload &lt;channel_id&gt; &lt;anime_name&gt; | &lt;season_no&gt; | &lt;source&gt;</code>\n\n"
            "<b>Source examples:</b>\n"
            "<code>/rti &lt;rti_page_url&gt; 1-12</code>\n"
            "<code>/url &lt;link&gt; -e</code>\n\n"
            "<b>Example:</b>\n"
            "<code>/bot_upload -1001234567890 Naruto | 1 | /rti https://rti.site/naruto 1-12</code>\n\n"
            "──────────\n"
            "<b>Existing-post mode (single):</b>\n"
            "<code>/bot_upload &lt;post_link&gt; | rti &lt;rti_url&gt; &lt;ep_num&gt;</code>\n\n"
            "<b>Existing-post mode (batch):</b>\n"
            "<code>/bot_upload &lt;start_post_link&gt; &lt;end_msg_id&gt; | url &lt;link&gt; -e &lt;ep_start&gt; &lt;ep_end&gt; &lt;quality&gt;</code>"
        )
        return

    rest = args[1].strip()
    parts_check = rest.split("|", 1)
    first_part = parts_check[0].strip()

    # ── Existing-post mode detection: first part starts with a t.me link ──
    if re.match(r"^https?://t\.me/", first_part, re.IGNORECASE):
        await _bot_upload_existing_post_mode(bot, message, rest)
        return

    # Split into: "<channel_id> <anime_name>" | "<season_no>" | "<source spec>"
    parts = rest.split("|")
    if len(parts) < 2:
        await message.reply("❌ Format galat hai. <code>/bot_upload &lt;channel_id&gt; &lt;anime_name&gt; | &lt;season_no&gt; | /rti &lt;url&gt; 1-12</code>")
        return

    channel_part = parts[0].strip()
    season_part = parts[1].strip()
    source_part = parts[2].strip() if len(parts) > 2 else ""

    try:
        channel_id = int(channel_part.split()[0])
    except (ValueError, IndexError):
        await message.reply("❌ Invalid <code>channel_id</code>.")
        return

    anime_name = " ".join(channel_part.split()[1:]).strip()
    if not anime_name:
        await message.reply("❌ Anime name missing. Format: <code>/bot_upload &lt;channel_id&gt; &lt;anime_name&gt; | &lt;season_no&gt;</code>")
        return

    try:
        season_no = int(re.sub(r"\D", "", season_part) or "0")
        if season_no < 1:
            raise ValueError
    except ValueError:
        await message.reply("❌ Invalid <code>season_no</code>.")
        return

    status = await message.reply(f"<b>🚀 /bot_upload started</b>\nChannel: <code>{channel_id}</code>\nAnime: <b>{anime_name}</b> | Season {season_no}")

    # ── Step 2: IMDB info ──
    info = await _fetch_imdb_info(anime_name)
    try:
        if info:
            caption = _build_imdb_caption(info, anime_name)
            if info.get("poster") and info["poster"] != "N/A":
                await app.send_photo(channel_id, info["poster"], caption=caption, parse_mode=__import__("pyrogram").enums.ParseMode.HTML)
            else:
                await app.send_message(channel_id, caption, parse_mode=__import__("pyrogram").enums.ParseMode.HTML)
        else:
            LOGGER.info("[bot_upload] IMDB info not found/skipped, continuing.")
    except Exception as e:
        LOGGER.error(f"[bot_upload] IMDB send failed: {e}")

    # ── Step 3: Border ──
    border = await db.get_bot_border(user_id)
    if border:
        try:
            if border["type"] == "sticker":
                await app.send_sticker(channel_id, border["file_id"])
            else:
                await app.send_photo(channel_id, border["file_id"])
        except Exception as e:
            LOGGER.error(f"[bot_upload] Border send failed: {e}")
    else:
        await message.reply("⚠️ Border set nahi hai. <code>/border</code> use karo.")

    # ── Step 4: Season sticker ──
    season_stickers = await db.get_season_stickers(user_id)
    if 1 <= season_no <= len(season_stickers):
        sticker = season_stickers[season_no - 1]
        try:
            if sticker["type"] == "sticker":
                await app.send_sticker(channel_id, sticker["file_id"])
            else:
                await app.send_photo(channel_id, sticker["file_id"])
        except Exception as e:
            LOGGER.error(f"[bot_upload] Season sticker send failed: {e}")
    else:
        await message.reply(
            f"⚠️ Season {season_no} ka sticker set nahi hai "
            f"(sirf {len(season_stickers)} seasons saved hain). "
            f"<code>/season_sticker</code> use karo."
        )

    # ── Step 5 (Phase 2): Episode upload + quality cycling + batch + end msg ──
    if not source_part:
        await status.edit(
            f"✅ <b>Phase 1 done!</b>\nChannel: <code>{channel_id}</code>\n"
            f"Anime: <b>{anime_name}</b> | Season {season_no}\n\n"
            f"<i>Koi source spec nahi diya — episode upload skip kiya.</i>"
        )
        return

    from ..utils.bot_upload_engine import (
        EpisodePostManager, run_episode_rti, create_batch_link,
        send_end_message, episode_from_filename, upload_file_to_log,
    )
    from ..plugins.swift_downloader import _quality_from
    from ..plugins.auto_monitor import _get_suhani_bot_link
    from ..plugins.url_upload import _download_url, _extract_archive_all, _safe_filename, apply_urlpreset_to_file
    from ..utils.direct_link_generator import direct_link_generator

    batch_msg_ids: dict = {"360p": [], "480p": [], "720p": [], "1080p": []}

    # ── /rti <url> <start>-<end>  (or "<start> <end>") ──
    rti_match = re.match(r"^/rti\s+(\S+)\s+(\d+)\s*[-\s]\s*(\d+)", source_part, re.IGNORECASE)
    if rti_match:
        page_url, start_ep, end_ep = rti_match.group(1), int(rti_match.group(2)), int(rti_match.group(3))
        total = end_ep - start_ep + 1

        for ep_num in range(start_ep, end_ep + 1):
            await status.edit(f"<b>🎬 Episode {ep_num}/{end_ep}</b> processing...")
            post_mgr = EpisodePostManager(app, channel_id, anime_name, ep_num, season_no)
            uploaded = await run_episode_rti(app, message, status, page_url, ep_num, end_ep, post_mgr=post_mgr)
            if not uploaded:
                continue

            for quality in ["360p", "480p", "720p", "1080p"]:
                sent_msg = uploaded.get(quality)
                if not sent_msg:
                    continue
                batch_msg_ids[quality].append(sent_msg.id)

    # ── /url <link> -e [unique_code]  ──
    # unique_code optional hai — diya toh existing episode messages edit honge
    elif source_part.lower().startswith("/url"):
        # Parse: /url <link> -e [code]
        m = re.match(r"^/url\s+(\S+)(?:\s+-e)?(?:\s+(SB_[A-Za-z0-9]+))?", source_part, re.IGNORECASE)
        if not m:
            await status.edit("❌ <code>/url</code> source format galat hai.")
            return
        url = m.group(1)
        resume_code = m.group(2)  # None agar naya run hai

        direct = direct_link_generator(url)
        if direct:
            url = direct

        await status.edit("<b>💠 Archive download ho raha hai...</b>")
        fname = _safe_filename(os.path.basename(url.split("?")[0]) or "download.zip")
        filepath = await _download_url(url, fname, status, message)
        if not filepath or not os.path.isfile(filepath):
            await status.edit("❌ Download fail ho gaya.")
            return

        all_files = await _extract_archive_all(filepath, status)
        if not all_files:
            return

        # Group: episode_num -> { quality: filepath }
        ep_files: dict = {}
        for fp in all_files:
            ep = episode_from_filename(os.path.basename(fp))
            q = _quality_from(os.path.basename(fp))
            if ep is None or q == "2160p":
                continue
            ep_files.setdefault(ep, {})[q] = fp

        episodes_sorted = sorted(ep_files.keys())
        post_managers: dict = {}
        from .. import download_dir as DL_DIR

        # Unique session code — naya ya resume
        if resume_code:
            session_code = resume_code
            await status.edit(f"<b>🔄 Resume mode: <code>{session_code}</code></b>\nPurane episode messages pe quality add hogi...")
        else:
            rand = ''.join(random.choices(string.ascii_letters + string.digits, k=6))
            session_code = f"SB_{rand}"

        for quality in ["360p", "480p", "720p", "1080p"]:
            for ep_num in episodes_sorted:
                fp = ep_files.get(ep_num, {}).get(quality)
                if not fp or not os.path.isfile(fp):
                    continue

                await status.edit(f"<b>📤 Ep {ep_num} — {quality}</b> uploading...")
                # ── urlpreset settings apply karo (Hindi only, sub filter, etc.) ──
                fp, _has_eng_sub = await apply_urlpreset_to_file(fp, user_id, status)
                success, sent_msg, _q = await upload_file_to_log(app, message, status, fp, DL_DIR)

                if not success or not sent_msg:
                    continue

                if ep_num not in post_managers:
                    # session_code + ep_num = unique DB key
                    post_managers[ep_num] = EpisodePostManager(
                        app, channel_id, anime_name, ep_num, season_no,
                        session_code=session_code
                    )

                link = await _get_suhani_bot_link(sent_msg)
                if link:
                    await post_managers[ep_num].add_quality(quality, link)
                    batch_msg_ids[quality].append(sent_msg.id)

        # Upload complete — user ko DM mein session code bhejo
        try:
            await app.send_message(
                message.from_user.id,
                f"✅ <b>Upload complete!</b>\n\n"
                f"📦 <b>Session Code:</b> <code>{session_code}</code>\n\n"
                f"720p/1080p add karne ke liye yeh command use karo:\n"
                f"<code>/bot_upload {channel_id} {anime_name} | {season_no} | /url &lt;720p_link&gt; -e {session_code}</code>",
                parse_mode="html",
            )
        except Exception as e:
            LOGGER.warning(f"[bot_upload] DM send failed: {e}")
            await status.edit(f"✅ Done! Session Code: <code>{session_code}</code>")

    else:
        await status.edit("❌ Source spec samajh nahi aaya. <code>/rti</code> ya <code>/url ... -e</code> use karo.")
        return

    # ── Batch links per quality (with gap between each to avoid bot collision) ──
    await status.edit("<b>📦 Batch links banaye ja rahe hain...</b>")
    batch_links: dict = {}
    for quality, ids in batch_msg_ids.items():
        if ids:
            await status.edit(f"<b>📦 Batch creating: {quality}</b> ({len(ids)} files)...")
            link = await create_batch_link(app, ids)
            if link:
                batch_links[quality] = link
            await asyncio.sleep(8)  # gap between batches — avoids overlap/glitches

    from ..utils.bot_upload_engine import send_batch_summary_post

    # ── Border sticker ──
    border = await db.get_bot_border(user_id)
    if border:
        try:
            if border["type"] == "sticker":
                await app.send_sticker(channel_id, border["file_id"])
            else:
                await app.send_photo(channel_id, border["file_id"])
        except Exception as e:
            LOGGER.error(f"[bot_upload] Border send (end) failed: {e}")

    # ── Final summary post to channel: "Season XX Full Batch Hindi" + quality buttons ──
    if batch_links:
        await send_batch_summary_post(app, channel_id, season_no, batch_links)

    # ── /set_end template ──
    end_template = await db.get_end_template(user_id)
    await send_end_message(app, channel_id, end_template, anime_name, season_no, batch_links)

    # ── Next season's sticker ──
    season_stickers = await db.get_season_stickers(user_id)
    next_season = season_no + 1
    if 1 <= next_season <= len(season_stickers):
        sticker = season_stickers[next_season - 1]
        try:
            if sticker["type"] == "sticker":
                await app.send_sticker(channel_id, sticker["file_id"])
            else:
                await app.send_photo(channel_id, sticker["file_id"])
        except Exception as e:
            LOGGER.error(f"[bot_upload] Next season sticker send failed: {e}")

    # ── Default auto-upload end message(s) ──
    try:
        from .schedule_notify import _send_end_messages_to_channel
        await _send_end_messages_to_channel(channel_id)
    except Exception as e:
        LOGGER.error(f"[bot_upload] Default end messages send failed: {e}")

    summary = "\n".join(f"• {q}: {l}" for q, l in batch_links.items()) or "—"
    await status.edit(
        f"✅ <b>/bot_upload complete!</b>\n"
        f"Anime: <b>{anime_name}</b> | Season {season_no}\n\n"
        f"<b>Batch links:</b>\n{summary}"
    )
