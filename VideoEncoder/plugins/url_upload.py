"""
URL Uploader Plugin for Encode Bot
Features:
  - URL se direct download + Telegram upload
  - Subtitle remove (soft/hard dono)
  - Audio remove / Hindi-only audio select
  - Name swap (toonweb → sbanime etc.)
  - Metadata editor (video title, audio title, video stream title)
"""

import asyncio
import os
import re
import subprocess
import time
from urllib.parse import unquote_plus

from pyrogram import Client, filters
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from .. import LOGGER, data, download_dir, encode_dir
from ..utils.database.access_db import db
from ..utils.database.add_user import AddUserToDatabase
from ..utils.direct_link_generator import direct_link_generator
from ..utils.display_progress import progress_for_pyrogram
from ..utils.fast_download import fast_download
from ..utils.helper import check_chat, output
from ..utils.url_processor import (
    apply_name_swap,
    build_metadata_ffmpeg_args,
    get_audio_streams,
    process_url_file,
)
from ..utils.uploads import upload_worker

# ─── Pending sessions store ──────────────────────────────────────────────────
# { user_id: { 'filepath': str, 'msg_id': int, 'orig_name': str } }
_url_sessions: dict = {}


# ─── /url command ─────────────────────────────────────────────────────────────
@Client.on_message(filters.command("url"))
async def url_upload_cmd(bot: Client, message: Message):
    """
    Usage:
      /url <link>
      /url <link> | <custom filename>
    """
    c = await check_chat(message, chat="Both")
    if not c:
        return
    await AddUserToDatabase(bot, message)

    if len(message.command) < 2:
        await message.reply(
            "**Usage:** `/url <link>` or `/url <link> | <filename>`\n\n"
            "After download you'll get options:\n"
            "• Remove Subtitles\n"
            "• Remove Audio tracks\n"
            "• Select Hindi Audio only\n"
            "• Name Swap (toonweb → sbanime)\n"
            "• Edit Metadata\n"
            "• Upload as-is"
        )
        return

    raw = message.text.split(None, 1)[1].strip()
    url = raw
    custom_name = None

    if "|" in raw:
        parts = raw.split("|", 1)
        url = parts[0].strip()
        custom_name = parts[1].strip() if parts[1].strip() else None

    # Direct link resolve karo
    direct = direct_link_generator(url)
    if direct:
        url = direct

    if not custom_name:
        custom_name = unquote_plus(os.path.basename(url.split("?")[0])) or "downloaded_file"

    msg = await message.reply("<b>💠 Downloading...</b>")

    try:
        filepath = await _download_url(url, custom_name, msg, message)
    except Exception as e:
        await msg.edit(f"❌ Download failed: <code>{e}</code>")
        return

    if not filepath or not os.path.isfile(filepath):
        await msg.edit("❌ Download failed or file not found.")
        return

    user_id = message.from_user.id
    _url_sessions[user_id] = {
        "filepath": filepath,
        "msg": msg,
        "orig_name": os.path.basename(filepath),
        "message": message,
    }

    await _show_url_options(msg, user_id, filepath)


# ─── Show options keyboard ────────────────────────────────────────────────────
async def _show_url_options(msg: Message, user_id: int, filepath: str):
    fname = os.path.basename(filepath)
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🚫 Remove Subs",      callback_data=f"url_rmsub_{user_id}"),
            InlineKeyboardButton("🔇 Remove Audio",     callback_data=f"url_rmaudio_{user_id}"),
        ],
        [
            InlineKeyboardButton("🇮🇳 Hindi Audio Only", callback_data=f"url_hindionly_{user_id}"),
            InlineKeyboardButton("🔤 Name Swap",         callback_data=f"url_nameswap_{user_id}"),
        ],
        [
            InlineKeyboardButton("🏷️ Edit Metadata",    callback_data=f"url_metadata_{user_id}"),
            InlineKeyboardButton("📤 Upload As-is",     callback_data=f"url_upload_{user_id}"),
        ],
        [
            InlineKeyboardButton("❌ Cancel",           callback_data=f"url_cancel_{user_id}"),
        ],
    ])
    await msg.edit(
        f"✅ <b>Downloaded:</b> <code>{fname}</code>\n\nChoose what to do before uploading:",
        reply_markup=kb,
    )


# ─── Callback handlers ────────────────────────────────────────────────────────
@Client.on_callback_query(filters.regex(r"^url_"))
async def url_upload_callbacks(bot: Client, cb: CallbackQuery):
    data_str = cb.data  # e.g. "url_rmsub_12345"
    parts = data_str.split("_")
    # parts[0] = "url", parts[1] = action, parts[2] = user_id
    if len(parts) < 3:
        await cb.answer("Invalid callback", show_alert=True)
        return

    action = parts[1]
    try:
        owner_id = int(parts[2])
    except ValueError:
        await cb.answer("Invalid user id", show_alert=True)
        return

    # Only owner can interact
    if cb.from_user.id != owner_id:
        await cb.answer("❌ Ye tumhara session nahi hai!", show_alert=True)
        return

    session = _url_sessions.get(owner_id)
    if not session:
        await cb.message.edit("⌛ Session expired. Please /url again.")
        await cb.answer()
        return

    filepath = session["filepath"]
    msg = session["msg"]
    original_message = session["message"]

    # ── Upload as-is ──────────────────────────────────────────────────────────
    if action == "upload":
        await cb.answer()
        await _do_upload(bot, filepath, original_message, msg)
        _url_sessions.pop(owner_id, None)

    # ── Cancel ────────────────────────────────────────────────────────────────
    elif action == "cancel":
        await cb.answer("Cancelled!")
        try:
            os.remove(filepath)
        except Exception:
            pass
        _url_sessions.pop(owner_id, None)
        await msg.edit("🚫 Cancelled.")

    # ── Remove Subtitles ──────────────────────────────────────────────────────
    elif action == "rmsub":
        await cb.answer()
        await msg.edit("<b>🔄 Removing subtitles...</b>")
        new_path = await _remove_subtitles(filepath, msg)
        if new_path:
            session["filepath"] = new_path
            _url_sessions[owner_id] = session
            await _show_url_options(msg, owner_id, new_path)
        else:
            await _show_url_options(msg, owner_id, filepath)

    # ── Remove Audio ──────────────────────────────────────────────────────────
    elif action == "rmaudio":
        await cb.answer()
        await msg.edit("<b>🔄 Removing all audio tracks...</b>")
        new_path = await _remove_audio(filepath, msg)
        if new_path:
            session["filepath"] = new_path
            _url_sessions[owner_id] = session
        await _show_url_options(msg, owner_id, session["filepath"])

    # ── Hindi Audio Only ──────────────────────────────────────────────────────
    elif action == "hindionly":
        await cb.answer()
        await msg.edit("<b>🔄 Detecting audio streams...</b>")
        audio_streams = get_audio_streams(filepath)
        hindi_indices = [
            s["index"] for s in audio_streams
            if any(tag in (s.get("lang", "") + s.get("title", "")).lower()
                   for tag in ["hin", "hindi", "hin2", "hi", "dub"])
        ]
        if not hindi_indices:
            await cb.answer(
                "⚠️ Hindi audio stream nahi mila! Manual stream number dena padega.",
                show_alert=True,
            )
            # Show stream list for manual selection
            streams_text = "\n".join(
                f"  Stream #{s['index']}: lang={s.get('lang','?')} title={s.get('title','?')}"
                for s in audio_streams
            )
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    f"🔊 #{s['index']} {s.get('lang','?')} – {s.get('title','?')[:20]}",
                    callback_data=f"url_selaud_{owner_id}_{s['index']}"
                )]
                for s in audio_streams
            ] + [[InlineKeyboardButton("⬅️ Back", callback_data=f"url_back_{owner_id}")]])
            await msg.edit(
                f"<b>Audio streams:</b>\n<code>{streams_text}</code>\n\nSelect Hindi stream:",
                reply_markup=kb,
            )
            return
        await msg.edit(f"<b>🔄 Keeping only Hindi audio (streams: {hindi_indices})...</b>")
        new_path = await _keep_audio_streams(filepath, hindi_indices, msg)
        if new_path:
            session["filepath"] = new_path
            _url_sessions[owner_id] = session
        await _show_url_options(msg, owner_id, session["filepath"])

    # ── Manual audio stream selection ─────────────────────────────────────────
    elif action == "selaud":
        # url_selaud_{user_id}_{stream_index}
        try:
            stream_idx = int(parts[3])
        except (IndexError, ValueError):
            await cb.answer("Invalid stream index", show_alert=True)
            return
        await cb.answer()
        await msg.edit(f"<b>🔄 Keeping only stream #{stream_idx}...</b>")
        new_path = await _keep_audio_streams(filepath, [stream_idx], msg)
        if new_path:
            session["filepath"] = new_path
            _url_sessions[owner_id] = session
        await _show_url_options(msg, owner_id, session["filepath"])

    # ── Back button ───────────────────────────────────────────────────────────
    elif action == "back":
        await cb.answer()
        await _show_url_options(msg, owner_id, filepath)

    # ── Name Swap ─────────────────────────────────────────────────────────────
    elif action == "nameswap":
        await cb.answer()
        swap_rules = await db.get_swap(owner_id)
        if not swap_rules:
            await msg.edit(
                "⚠️ <b>Name swap rules not set!</b>\n\n"
                "Use: <code>/addswap toonweb sbanime</code>\n"
                "Multiple rules: send one by one.\n\n"
                "Current rules: None",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("⬅️ Back", callback_data=f"url_back_{owner_id}")
                ]])
            )
            return
        old_name = os.path.basename(filepath)
        new_name = apply_name_swap(old_name, swap_rules)
        if new_name == old_name:
            await cb.answer("ℹ️ No matching swap rules found in filename.", show_alert=True)
            await _show_url_options(msg, owner_id, filepath)
            return
        new_path = os.path.join(os.path.dirname(filepath), new_name)
        os.rename(filepath, new_path)
        session["filepath"] = new_path
        _url_sessions[owner_id] = session
        await cb.answer(f"✅ Renamed: {new_name}", show_alert=True)
        await _show_url_options(msg, owner_id, new_path)

    # ── Metadata Editor ───────────────────────────────────────────────────────
    elif action == "metadata":
        await cb.answer()
        meta = await db.get_url_metadata(owner_id)
        video_title = meta.get("video_title", "")
        audio_title = meta.get("audio_title", "")
        show_title = meta.get("show_title", "")
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                f"🎬 Video Title: {video_title or '(not set)'}",
                callback_data=f"url_setmeta_{owner_id}_video"
            )],
            [InlineKeyboardButton(
                f"🔊 Audio Title: {audio_title or '(not set)'}",
                callback_data=f"url_setmeta_{owner_id}_audio"
            )],
            [InlineKeyboardButton(
                f"📺 Show Title: {show_title or '(not set)'}",
                callback_data=f"url_setmeta_{owner_id}_show"
            )],
            [InlineKeyboardButton("✅ Apply & Continue", callback_data=f"url_applymeta_{owner_id}")],
            [InlineKeyboardButton("⬅️ Back", callback_data=f"url_back_{owner_id}")],
        ])
        await msg.edit(
            "<b>🏷️ Metadata Editor</b>\n\n"
            "Tap to set each field. Leave blank to keep original.\n\n"
            f"Current settings:\n"
            f"• Video Title: <code>{video_title or 'not set'}</code>\n"
            f"• Audio Title: <code>{audio_title or 'not set'}</code>\n"
            f"• Show Title: <code>{show_title or 'not set'}</code>",
            reply_markup=kb,
        )

    # ── Set metadata field (triggers text input) ──────────────────────────────
    elif action == "setmeta":
        field = parts[3] if len(parts) > 3 else "video"
        field_names = {"video": "Video Stream Title", "audio": "Audio Stream Title", "show": "Show/Series Title"}
        await cb.answer()
        session["awaiting_meta"] = field
        _url_sessions[owner_id] = session
        await msg.edit(
            f"<b>✏️ Enter new <i>{field_names.get(field, field)}</i>:</b>\n\n"
            "Reply to this message with the new title.\n"
            "Send <code>-</code> (dash) to clear/skip this field.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel input", callback_data=f"url_metadata_{owner_id}")
            ]])
        )

    # ── Apply metadata to file ────────────────────────────────────────────────
    elif action == "applymeta":
        await cb.answer()
        meta = await db.get_url_metadata(owner_id)
        if not any(meta.values()):
            await cb.answer("ℹ️ No metadata set!", show_alert=True)
            await _show_url_options(msg, owner_id, filepath)
            return
        await msg.edit("<b>🔄 Applying metadata...</b>")
        new_path = await _apply_metadata(filepath, meta, msg)
        if new_path:
            session["filepath"] = new_path
            _url_sessions[owner_id] = session
        await _show_url_options(msg, owner_id, session["filepath"])


# ─── Text handler for metadata input ─────────────────────────────────────────
@Client.on_message(filters.text & filters.private)
async def url_meta_text_input(bot: Client, message: Message):
    """Catches text replies for metadata field input."""
    user_id = message.from_user.id
    session = _url_sessions.get(user_id)
    if not session or "awaiting_meta" not in session:
        return  # Not our business

    field = session.pop("awaiting_meta")
    value = message.text.strip()
    if value == "-":
        value = ""

    meta = await db.get_url_metadata(user_id)
    if field == "video":
        meta["video_title"] = value
    elif field == "audio":
        meta["audio_title"] = value
    elif field == "show":
        meta["show_title"] = value

    await db.set_url_metadata(user_id, meta)
    _url_sessions[user_id] = session

    msg = session["msg"]
    await message.delete()
    # Refresh metadata panel
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"🎬 Video Title: {meta.get('video_title') or '(not set)'}",
            callback_data=f"url_setmeta_{user_id}_video"
        )],
        [InlineKeyboardButton(
            f"🔊 Audio Title: {meta.get('audio_title') or '(not set)'}",
            callback_data=f"url_setmeta_{user_id}_audio"
        )],
        [InlineKeyboardButton(
            f"📺 Show Title: {meta.get('show_title') or '(not set)'}",
            callback_data=f"url_setmeta_{user_id}_show"
        )],
        [InlineKeyboardButton("✅ Apply & Continue", callback_data=f"url_applymeta_{user_id}")],
        [InlineKeyboardButton("⬅️ Back", callback_data=f"url_back_{user_id}")],
    ])
    await msg.edit(
        f"<b>✅ Updated!</b>\n\n"
        f"• Video Title: <code>{meta.get('video_title') or 'not set'}</code>\n"
        f"• Audio Title: <code>{meta.get('audio_title') or 'not set'}</code>\n"
        f"• Show Title: <code>{meta.get('show_title') or 'not set'}</code>",
        reply_markup=kb,
    )


# ─── /addswap command ─────────────────────────────────────────────────────────
@Client.on_message(filters.command("addswap"))
async def add_swap_rule(bot: Client, message: Message):
    """
    /addswap <from_text> <to_text>
    Example: /addswap toonweb sbanime
    """
    c = await check_chat(message, chat="Both")
    if not c:
        return
    parts = message.text.split(None, 2)
    if len(parts) < 3:
        await message.reply(
            "**Usage:** `/addswap <from> <to>`\n"
            "Example: `/addswap toonweb sbanime`"
        )
        return
    from_text = parts[1].strip()
    to_text = parts[2].strip()
    rules = await db.get_swap(message.from_user.id)
    rules[from_text] = to_text
    await db.set_swap(message.from_user.id, rules)
    await message.reply(
        f"✅ Swap rule added:\n<code>{from_text}</code> → <code>{to_text}</code>\n\n"
        f"Total rules: {len(rules)}",
        reply_markup=output,
    )


@Client.on_message(filters.command("swaplist"))
async def list_swap_rules(bot: Client, message: Message):
    c = await check_chat(message, chat="Both")
    if not c:
        return
    rules = await db.get_swap(message.from_user.id)
    if not rules:
        await message.reply("No swap rules set. Use `/addswap <from> <to>`")
        return
    text = "<b>🔄 Name Swap Rules:</b>\n\n"
    for k, v in rules.items():
        text += f"• <code>{k}</code> → <code>{v}</code>\n"
    await message.reply(text)


@Client.on_message(filters.command("clearswap"))
async def clear_swap_rules(bot: Client, message: Message):
    c = await check_chat(message, chat="Both")
    if not c:
        return
    await db.clear_swap(message.from_user.id)
    await message.reply("✅ All swap rules cleared.")


# ─── Helper functions ─────────────────────────────────────────────────────────

async def _download_url(url: str, filename: str, msg: Message, orig_message: Message) -> str:
    """Download from URL with progress."""
    filepath = os.path.join(download_dir, filename)
    c_time = time.time()

    try:
        from pySmartDL import SmartDL
        from ..utils.display_progress import progress_for_url
        downloader = SmartDL(url, filepath, progress_bar=False, threads=10)
        downloader.start(blocking=False)
        while not downloader.isFinished():
            await progress_for_url(downloader, msg)
        if downloader.isSuccessful():
            return filepath
        raise RuntimeError(f"SmartDL failed: {downloader.get_errors()}")
    except Exception as e:
        LOGGER.error(f"SmartDL download failed: {e}, trying aiohttp...")

    # Fallback: aiohttp streaming download
    import aiohttp
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("content-length", 0))
            downloaded = 0
            last_edit = time.time()
            with open(filepath, "wb") as f:
                async for chunk in resp.content.iter_chunked(1024 * 512):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if time.time() - last_edit > 3:
                        pct = int(downloaded * 100 / total) if total else 0
                        bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
                        try:
                            await msg.edit(
                                f"<b>💠 Downloading...</b>\n{bar} {pct}%\n"
                                f"<code>{downloaded // 1024 // 1024} MB</code>"
                            )
                        except Exception:
                            pass
                        last_edit = time.time()
    return filepath


async def _ffmpeg_process(input_path: str, output_path: str, extra_args: list, msg: Message) -> str | None:
    """Run ffmpeg with given args, return output_path or None on failure."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", input_path] + extra_args + [output_path]
    LOGGER.info(f"FFmpeg cmd: {' '.join(cmd)}")
    try:
        await msg.edit(f"<b>⚙️ Processing...</b>\n<code>{os.path.basename(output_path)}</code>")
    except Exception:
        pass
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        LOGGER.error(f"FFmpeg error: {stderr.decode()}")
        try:
            await msg.edit(f"❌ Processing failed:\n<code>{stderr.decode()[-300:]}</code>")
        except Exception:
            pass
        return None
    # Remove original, rename output
    try:
        os.remove(input_path)
    except Exception:
        pass
    return output_path


def _make_output_path(filepath: str, suffix: str) -> str:
    base, ext = os.path.splitext(filepath)
    return base + suffix + ext


async def _remove_subtitles(filepath: str, msg: Message) -> str | None:
    out = _make_output_path(filepath, "_nosub")
    args = ["-map", "0:v", "-map", "0:a?", "-c", "copy", "-sn"]
    return await _ffmpeg_process(filepath, out, args, msg)


async def _remove_audio(filepath: str, msg: Message) -> str | None:
    out = _make_output_path(filepath, "_noaudio")
    args = ["-map", "0:v", "-map", "0:s?", "-c", "copy", "-an"]
    return await _ffmpeg_process(filepath, out, args, msg)


async def _keep_audio_streams(filepath: str, stream_indices: list, msg: Message) -> str | None:
    """Keep only specific audio stream indices (absolute stream indices from ffprobe)."""
    out = _make_output_path(filepath, "_hindiaudio")
    # -map 0:v -map 0:s? -map 0:a:<relative_idx>
    # We need to figure out relative audio index from absolute stream index
    audio_streams = get_audio_streams(filepath)
    audio_abs_indices = [s["index"] for s in audio_streams]

    map_args = ["-map", "0:v?", "-map", "0:s?"]
    for i, abs_idx in enumerate(audio_abs_indices):
        if abs_idx in stream_indices:
            map_args += ["-map", f"0:a:{i}"]

    if len(map_args) == 2:
        # None matched, fallback to all audio
        LOGGER.warning("No matching audio streams found, keeping all.")
        return None

    # Set first selected audio as default
    map_args += ["-disposition:a:0", "default", "-c", "copy"]
    return await _ffmpeg_process(filepath, out, map_args, msg)


async def _apply_metadata(filepath: str, meta: dict, msg: Message) -> str | None:
    out = _make_output_path(filepath, "_meta")
    meta_args = ["-map", "0", "-c", "copy"]
    if meta.get("show_title"):
        meta_args += ["-metadata", f"title={meta['show_title']}"]
    if meta.get("video_title"):
        meta_args += ["-metadata:s:v:0", f"title={meta['video_title']}"]
    if meta.get("audio_title"):
        # Apply to all audio streams
        meta_args += ["-metadata:s:a", f"title={meta['audio_title']}"]
    return await _ffmpeg_process(filepath, out, meta_args, msg)


async def _do_upload(bot: Client, filepath: str, message: Message, msg: Message):
    """Upload the processed file to Telegram."""
    await msg.edit("<b>📤 Uploading...</b>")
    try:
        resolution = await db.get_resolution(message.from_user.id)
        link = await upload_worker(filepath, message, msg, resolution=resolution)
        if link:
            await msg.edit(f"✅ <b>Uploaded!</b>\nLink: {link}")
        else:
            await msg.edit("✅ <b>Uploaded!</b>")
    except Exception as e:
        await msg.edit(f"❌ Upload failed: <code>{e}</code>")
        LOGGER.error(f"URL upload failed: {e}")
    finally:
        try:
            os.remove(filepath)
        except Exception:
            pass
