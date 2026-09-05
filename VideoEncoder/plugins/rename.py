"""
rename.py
─────────
/rename — koi bhi file (video / audio / document / apk / kuch bhi) ko
reply karke naya naam do, bot download + rename + (agar set hai to)
metadata + thumbnail apply karke upload kar dega.

Usage:
  1) File ko reply karke sirf /rename bhejo
     → Bot pehle poori file download karega, phir "naya naam kya doon?"
       poochega. Naam reply mein bhejo → rename + upload shuru.

  2) File ko reply karke seedha /rename New File Name.mkv bhejo
     → Koi prompt nahi — seedha download + rename + upload.

Extension rule:
  - Diye gaye naam ke END mein agar koi known extension ho (.mkv, .mp4,
    .apk, .zip, .mp3, waghera) → output usi extension mein banega.
  - Extension na diya ho → original file ka jo bhi extension tha, wahi
    use hoga.

Metadata + Thumbnail:
  - /setmeta se jo full metadata settings enabled + set hain, woh (sirf
    media container files pe — video/audio) auto-apply ho jaayengi.
  - /setpic (bina keyword) ya /thumb se jo default thumbnail set hai,
    woh bhi auto-apply hogi.
"""

import os
import re
import time

from pyrogram import Client, filters
from pyrogram.types import Message

from .. import LOGGER, download_dir
from ..utils.database.access_db import db
from ..utils.database.add_user import AddUserToDatabase
from ..utils.helper import check_chat
from ..utils.display_progress import progress_for_pyrogram
from ..utils.fast_download import fast_download

# ─── Known extensions ──────────────────────────────────────────────────────
VIDEO_EXTENSIONS = {"mp4", "mkv", "avi", "mov", "flv", "wmv", "ts", "m4v", "webm", "3gp"}
AUDIO_EXTENSIONS = {"mp3", "m4a", "flac", "wav", "ogg", "aac", "opus"}
# Container formats jinpe ffmpeg -map 0 -c copy se metadata safely laga sakte hain
MEDIA_EXTENSIONS_FOR_METADATA = (VIDEO_EXTENSIONS | AUDIO_EXTENSIONS) - {"3gp", "opus"}

OTHER_KNOWN_EXTENSIONS = {
    "apk", "zip", "rar", "7z", "pdf", "txt", "doc", "docx", "xlsx", "pptx",
    "epub", "cbz", "cbr", "json", "xml", "srt", "ass", "vtt", "csv", "iso",
    "exe", "torrent",
}
KNOWN_EXTENSIONS = VIDEO_EXTENSIONS | AUDIO_EXTENSIONS | OTHER_KNOWN_EXTENSIONS

# { user_id: { 'filepath':..., 'message': Message, 'msg': Message } }
_rename_sessions: dict = {}


# ─── /rename command ────────────────────────────────────────────────────────
@Client.on_message(filters.command("rename"))
async def rename_cmd(bot: Client, message: Message):
    c = await check_chat(message, chat="Both")
    if not c:
        return

    replied = message.reply_to_message
    if not replied or not (replied.video or replied.document or replied.audio):
        await message.reply(
            "❌ Kisi <b>video / document / audio / apk</b> file ko reply "
            "karke <code>/rename</code> bhejo.\n\n"
            "<b>Example:</b>\n"
            "• Reply + <code>/rename</code> → naya naam poochega\n"
            "• Reply + <code>/rename Naya Naam.mkv</code> → seedha rename ho jaayega"
        )
        return

    await AddUserToDatabase(bot, message)
    user_id = message.from_user.id

    # Purana koi active session ho toh discard karo
    _rename_sessions.pop(user_id, None)

    # Naam inline diya hai kya? (/rename New Name.mkv)
    args = message.text.split(None, 1)
    inline_name = args[1].strip() if len(args) > 1 else None

    status = await message.reply("<b>💠 Downloading...</b>")

    filepath = await _download_replied_file(bot, replied, status)
    if not filepath:
        await status.edit("❌ Download failed ya unsupported file type.")
        return

    if inline_name:
        # Caption seedha diya gaya hai — koi prompt nahi, direct rename
        await _do_rename_and_upload(bot, user_id, filepath, inline_name, message, status)
        return

    # Interactive: naya naam poocho
    orig_ext = os.path.splitext(filepath)[1].lstrip(".") or "original"
    _rename_sessions[user_id] = {
        "filepath": filepath,
        "message": message,
        "msg": status,
    }
    await status.edit(
        "✅ <b>File download ho gayi!</b>\n\n"
        f"Original: <code>{os.path.basename(filepath)}</code>\n\n"
        "Ab <b>naya filename</b> bhejo.\n"
        f"<i>Extension nahi doge to <code>.{orig_ext}</code> hi rahega, "
        "de doge (e.g. .mkv/.mp4/...) to usi mein convert ho jaayega.</i>\n\n"
        "<i>Cancel karne ke liye <code>-</code> bhejo.</i>"
    )


async def _download_replied_file(bot: Client, replied: Message, status: Message) -> "str | None":
    """Video/document/audio — sabko fast_download se download karo."""
    c_time = time.time()
    if replied.video:
        fname = replied.video.file_name or f"video_{int(time.time())}.mp4"
    elif replied.audio:
        fname = replied.audio.file_name or f"audio_{int(time.time())}.mp3"
    elif replied.document:
        fname = replied.document.file_name or f"file_{int(time.time())}"
    else:
        return None

    file_path = os.path.join(download_dir, fname)
    try:
        path = await fast_download(
            client=bot,
            message=replied,
            file_name=file_path,
            progress_callback=progress_for_pyrogram,
            progress_args=("⚡ Downloading...", status, c_time),
        )
        return path
    except Exception as e:
        LOGGER.error(f"[rename] download failed: {e}")
        return None


# ─── Text handler: naya naam ka input ──────────────────────────────────────
@Client.on_message(filters.text & filters.private, group=6)
async def rename_text_input(bot: Client, message: Message):
    """Rename session ka text input — naya naam mila."""
    user_id = message.from_user.id
    session = _rename_sessions.get(user_id)
    if not session:
        return  # Hamara kaam nahi

    text = message.text.strip()
    if text.startswith("/"):
        return  # Koi aur command hai — ignore karo, apna kaam mat karo

    _rename_sessions.pop(user_id, None)

    if text == "-":
        await message.reply("❌ Cancelled.")
        try:
            os.remove(session["filepath"])
        except Exception:
            pass
        return

    await _do_rename_and_upload(bot, user_id, session["filepath"], text, session["message"], session["msg"])


# ─── Extension resolver ─────────────────────────────────────────────────────
def _resolve_extension(filepath: str, given_name: str) -> tuple[str, str]:
    """
    User ke rule ke mutabik (name_without_ext, extension) return karo:
    - Agar given_name ke end mein koi KNOWN extension ho → usi ka use karo.
    - Warna → original file ka extension use karo.
    """
    orig_ext = os.path.splitext(filepath)[1].lstrip(".").lower()

    root, ext = os.path.splitext(given_name.strip())
    ext_clean = ext.lstrip(".").lower()
    if ext_clean and ext_clean in KNOWN_EXTENSIONS:
        return root.strip(), ext_clean

    return given_name.strip(), orig_ext


def _sanitize_filename(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', '', name).strip()


# ─── Core: rename + (optional metadata) + upload ───────────────────────────
async def _do_rename_and_upload(bot: Client, user_id: int, filepath: str, given_name: str,
                                 orig_message: Message, status: Message):
    try:
        name_part, ext = _resolve_extension(filepath, given_name)
        name_part = _sanitize_filename(name_part) or "renamed_file"
        ext = ext or "bin"

        new_filename = f"{name_part}.{ext}"
        new_path = os.path.join(os.path.dirname(filepath), new_filename)
        if new_path != filepath:
            os.rename(filepath, new_path)
            filepath = new_path

        # ── Metadata (agar /setmeta se enabled hai aur file media container hai) ──
        if ext in MEDIA_EXTENSIONS_FOR_METADATA:
            full_meta = await db.get_full_metadata(user_id)
            if full_meta.get("enabled") and any([
                full_meta.get("movie_name"),
                full_meta.get("video_title"),
                full_meta.get("audio_title"),
                full_meta.get("subtitle_title"),
                full_meta.get("comment"),
            ]):
                from .url_upload import _apply_full_metadata, _resolve_meta_placeholders
                await status.edit("<b>🔄 Applying metadata...</b>")
                resolved = _resolve_meta_placeholders(full_meta, filepath)
                new_out = await _apply_full_metadata(filepath, resolved, status)
                if new_out:
                    filepath = new_out
                    # _apply_full_metadata output ka naam "_meta" suffix ke saath
                    # aata hai — clean naam pe wapas rename karo
                    clean_path = os.path.join(os.path.dirname(filepath), new_filename)
                    if clean_path != filepath:
                        try:
                            os.rename(filepath, clean_path)
                            filepath = clean_path
                        except Exception:
                            pass

        await status.edit("<b>📤 Uploading...</b>")
        await _upload_renamed_file(bot, filepath, new_filename, ext, user_id, orig_message, status)

    except Exception as e:
        LOGGER.error(f"[rename] failed: {e}")
        try:
            await status.edit(f"❌ Rename failed: <code>{e}</code>")
        except Exception:
            pass
    finally:
        try:
            if os.path.isfile(filepath):
                os.remove(filepath)
        except Exception:
            pass


async def _upload_renamed_file(bot: Client, filepath: str, new_filename: str, ext: str,
                                user_id: int, orig_message: Message, status: Message):
    """Final renamed file ko sahi media type (video/audio/document) mein upload karo,
    saved default thumbnail (/setpic ya /thumb) apply karke."""
    from ..utils.encoding import get_duration, get_thumbnail, get_width_height

    caption = f"<b>{new_filename}</b>"
    custom_thumb = await db.get_thumbnail(user_id)
    thumb_path = None
    c_time = time.time()

    try:
        if ext in VIDEO_EXTENSIONS:
            duration = get_duration(filepath) or 0
            if custom_thumb:
                thumb_path = await bot.download_media(
                    custom_thumb,
                    file_name=os.path.join(download_dir, f"{time.time()}.jpg"),
                )
            else:
                thumb_path = get_thumbnail(filepath, download_dir, duration / 4 if duration else 0)
            width, height = get_width_height(filepath) or (0, 0)

            await bot.send_video(
                chat_id=orig_message.chat.id,
                video=filepath,
                caption=caption,
                duration=int(duration),
                width=width or 0,
                height=height or 0,
                thumb=thumb_path,
                file_name=new_filename,
                progress=progress_for_pyrogram,
                progress_args=("📤 Uploading...", status, c_time),
            )

        elif ext in AUDIO_EXTENSIONS:
            if custom_thumb:
                thumb_path = await bot.download_media(
                    custom_thumb,
                    file_name=os.path.join(download_dir, f"{time.time()}.jpg"),
                )
            await bot.send_audio(
                chat_id=orig_message.chat.id,
                audio=filepath,
                caption=caption,
                thumb=thumb_path,
                file_name=new_filename,
                progress=progress_for_pyrogram,
                progress_args=("📤 Uploading...", status, c_time),
            )

        else:
            # apk / zip / pdf / kuch bhi — plain document
            if custom_thumb:
                thumb_path = await bot.download_media(
                    custom_thumb,
                    file_name=os.path.join(download_dir, f"{time.time()}.jpg"),
                )
            await bot.send_document(
                chat_id=orig_message.chat.id,
                document=filepath,
                caption=caption,
                thumb=thumb_path,
                file_name=new_filename,
                progress=progress_for_pyrogram,
                progress_args=("📤 Uploading...", status, c_time),
            )

        try:
            await status.edit("✅ <b>Renamed & Uploaded!</b>")
        except Exception:
            pass

    finally:
        if thumb_path and os.path.isfile(thumb_path):
            try:
                os.remove(thumb_path)
            except Exception:
                pass
