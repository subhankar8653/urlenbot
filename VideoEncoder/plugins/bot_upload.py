"""
bot_upload.py — Phase 1
========================
Global config commands + /bot_upload skeleton (intro → IMDB → border/season sticker).

Commands:
  /set_intro <template>   — Intro message template (placeholders: {anime_name}, {season})
  /set_end <template>     — End message template (placeholders: {anime_name}, {season},
                             {q480}, {q720}, {q1080})
  /border                 — Start border-sticker setup. Send sticker, then /done
  /season_sticker         — Start/continue season-sticker collection. Send sticker
                             per season in order, then /done
  /bot_upload <channel_id> <anime_name> | <season_no>
                           — Phase 1: sends intro → IMDB info → border + season sticker
"""

import os
import re

import aiohttp
from pyrogram import Client, filters
from pyrogram.types import Message

from .. import LOGGER, app
from ..utils.database.access_db import db
from ..utils.helper import check_chat, output

OMDB_API_KEY = os.getenv("OMDB_API_KEY", "")

# ─── In-memory sessions ────────────────────────────────────────────────────
_border_session: set = set()        # user_ids currently in /border setup mode
_season_session: set = set()        # user_ids currently in /season_sticker setup mode
_text_template_session: dict = {}   # { user_id: "end" }  (set_end inline-prompt mode)
_intro_collect_session: dict = {}   # { user_id: [line1, line2, ...] }  /set_intro multi-msg collection


# ─────────────────────────────────────────────────────────────────────────
#  /set_intro & /set_end
# ─────────────────────────────────────────────────────────────────────────
@Client.on_message(filters.command("set_intro"))
async def set_intro_cmd(bot: Client, message: Message):
    c = await check_chat(message, chat="Both")
    if not c:
        return

    args = message.text.split(None, 1)
    if len(args) > 1 and args[1].strip():
        await db.set_intro_template(message.from_user.id, args[1].strip())
        await message.reply(
            "✅ <b>Intro template saved!</b>\n\nUse /bot_upload se test karo.",
            reply_markup=output,
        )
        return

    _intro_collect_session[message.from_user.id] = []
    await message.reply(
        "<b>✏️ Intro template collection mode ON!</b>\n\n"
        "Ab apne saare messages ek-ek karke bhejo (jitne chahiye, e.g. 300 dot/lines).\n"
        "Sab combine ho jayenge (naye line se separated).\n\n"
        "Placeholders use kar sakte ho:\n"
        "<code>{anime_name}</code> — Anime ka naam\n"
        "<code>{season}</code> — Season number\n\n"
        "Khatam karne ke liye <code>/complete</code> bhejo.\n"
        "<i>Cancel karne ke liye <code>/cancel</code> bhejo.</i>",
    )


@Client.on_message(filters.command("complete"))
async def complete_cmd(bot: Client, message: Message):
    user_id = message.from_user.id
    if user_id not in _intro_collect_session:
        await message.reply("ℹ️ Koi active /set_intro collection nahi chal raha.")
        return

    lines = _intro_collect_session.pop(user_id)
    if not lines:
        await message.reply("⚠️ Koi message collect nahi hua. Template save nahi hua.")
        return

    template = "\n".join(lines)
    await db.set_intro_template(user_id, template)
    await message.reply(
        f"✅ <b>Intro template saved!</b> ({len(lines)} lines)\n\nUse /bot_upload se test karo.",
        reply_markup=output,
    )


@Client.on_message(filters.command("cancel"))
async def cancel_collect_cmd(bot: Client, message: Message):
    user_id = message.from_user.id
    if user_id in _intro_collect_session:
        _intro_collect_session.pop(user_id, None)
        await message.reply("❌ Intro collection cancelled.")
    else:
        await message.reply("ℹ️ Koi active collection nahi chal raha.")


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
    "set_intro", "set_end", "border", "season_sticker", "done", "bot_upload", "complete", "cancel"
]), group=4)
async def template_text_input(bot: Client, message: Message):
    """Catches messages for intro multi-collection or the /set_end inline prompt."""
    user_id = message.from_user.id

    # Intro multi-message collection mode
    if user_id in _intro_collect_session:
        _intro_collect_session[user_id].append(message.text)
        return

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
            "<code>/bot_upload -1001234567890 Naruto | 1 | /rti https://rti.site/naruto 1-12</code>"
        )
        return

    rest = args[1].strip()
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

    # ── Step 1: Intro message ──
    intro_template = await db.get_intro_template(user_id)
    if intro_template:
        intro_text = intro_template.format(anime_name=anime_name, season=season_no)
        try:
            await app.send_message(channel_id, intro_text)
        except Exception as e:
            await status.edit(f"❌ Intro msg fail ho gaya: <code>{e}</code>")
            return
    else:
        await status.edit(
            "⚠️ Intro template set nahi hai. <code>/set_intro</code> use karo.\n"
            "Skip kar raha hoon..."
        )

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
    from ..plugins.url_upload import _download_url, _extract_archive_all, _safe_filename
    from ..utils.direct_link_generator import direct_link_generator

    batch_msg_ids: dict = {"360p": [], "480p": [], "720p": [], "1080p": []}

    # ── /rti <url> <start>-<end>  (or "<start> <end>") ──
    rti_match = re.match(r"^/rti\s+(\S+)\s+(\d+)\s*[-\s]\s*(\d+)", source_part, re.IGNORECASE)
    if rti_match:
        page_url, start_ep, end_ep = rti_match.group(1), int(rti_match.group(2)), int(rti_match.group(3))
        total = end_ep - start_ep + 1

        for ep_num in range(start_ep, end_ep + 1):
            await status.edit(f"<b>🎬 Episode {ep_num}/{end_ep}</b> processing...")
            uploaded = await run_episode_rti(app, message, status, page_url, ep_num, end_ep)
            if not uploaded:
                continue

            post_mgr = EpisodePostManager(app, channel_id, anime_name, ep_num, season_no)
            for quality in ["360p", "480p", "720p", "1080p"]:
                sent_msg = uploaded.get(quality)
                if not sent_msg:
                    continue
                link = await _get_suhani_bot_link(sent_msg)
                if link:
                    await post_mgr.add_quality(quality, link)
                    batch_msg_ids[quality].append(sent_msg.id)

    # ── /url <link> -e  (3-pass: 480p all eps -> 720p all eps -> 1080p all eps) ──
    elif source_part.lower().startswith("/url"):
        m = re.match(r"^/url\s+(\S+)", source_part, re.IGNORECASE)
        if not m:
            await status.edit("❌ <code>/url</code> source format galat hai.")
            return
        url = m.group(1)
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

        for quality in ["360p", "480p", "720p", "1080p"]:
            for ep_num in episodes_sorted:
                fp = ep_files.get(ep_num, {}).get(quality)
                if not fp or not os.path.isfile(fp):
                    continue

                await status.edit(f"<b>📤 Ep {ep_num} — {quality}</b> uploading...")
                success, sent_msg, _q = await upload_file_to_log(app, message, status, fp, DL_DIR)

                if not success or not sent_msg:
                    continue

                if ep_num not in post_managers:
                    post_managers[ep_num] = EpisodePostManager(app, channel_id, anime_name, ep_num, season_no)

                link = await _get_suhani_bot_link(sent_msg)
                if link:
                    await post_managers[ep_num].add_quality(quality, link)
                    batch_msg_ids[quality].append(sent_msg.id)

    else:
        await status.edit("❌ Source spec samajh nahi aaya. <code>/rti</code> ya <code>/url ... -e</code> use karo.")
        return

    # ── Batch links per quality ──
    await status.edit("<b>📦 Batch links banaye ja rahe hain...</b>")
    batch_links: dict = {}
    for quality, ids in batch_msg_ids.items():
        if ids:
            link = await create_batch_link(app, ids)
            if link:
                batch_links[quality] = link

    from ..utils.bot_upload_engine import send_batch_summary_post

    # ── Final summary post to channel: "Season XX Full Batch Hindi" + quality buttons ──
    if batch_links:
        await send_batch_summary_post(app, channel_id, season_no, batch_links)

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

    # ── End message ──
    end_template = await db.get_end_template(user_id)
    await send_end_message(app, channel_id, end_template, anime_name, season_no, batch_links)

    summary = "\n".join(f"• {q}: {l}" for q, l in batch_links.items()) or "—"
    await status.edit(
        f"✅ <b>/bot_upload complete!</b>\n"
        f"Anime: <b>{anime_name}</b> | Season {season_no}\n\n"
        f"<b>Batch links:</b>\n{summary}"
    )
