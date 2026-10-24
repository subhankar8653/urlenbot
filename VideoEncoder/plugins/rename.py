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

import asyncio
import os
import re
import time

from pyrogram import Client, filters
from pyrogram.types import Message

from .. import LOGGER, download_dir
from ..utils.database.access_db import db
from ..utils.database.add_user import AddUserToDatabase
from ..utils import job_ctl
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
    # Download sessions turant taiyar hone lagein (db checks ke wait se pehle)
    _r0 = message.reply_to_message
    if _r0 and (_r0.video or _r0.document or _r0.audio):
        asyncio.ensure_future(_prewarm_download(bot, _r0))

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

    # Download chalte-chalte upload ke connections/sessions taiyar (upload ke time wait nahi)
    asyncio.ensure_future(_prewarm_upload(bot, user_id))

    # Download + rename + upload — ❌ Cancel button ke saath
    async def _rename_body(pm):
        filepath = await _download_replied_file(bot, replied, pm)
        if not filepath:
            await pm.edit("❌ Download failed ya unsupported file type.\n"
                          "<i>(Disk full / network issue bhi ho sakta hai — thodi der baad dobara try karo)</i>")
            return

        if inline_name:
            # Caption seedha diya gaya hai — koi prompt nahi, direct rename
            await _do_rename_and_upload(bot, user_id, filepath, inline_name, message, pm)
            return

        # Interactive: naya naam poocho (yahan Cancel button nahi — download ho chuka, ab sirf naam chahiye)
        orig_ext = os.path.splitext(filepath)[1].lstrip(".") or "original"
        _rename_sessions[user_id] = {
            "filepath": filepath,
            "message": message,
            "msg": status,
        }
        await pm.edit(
            "✅ <b>File download ho gayi!</b>\n\n"
            f"Original: <code>{os.path.basename(filepath)}</code>\n\n"
            "Ab <b>naya filename</b> bhejo.\n"
            f"<i>Extension nahi doge to <code>.{orig_ext}</code> hi rahega, "
            "de doge (e.g. .mkv/.mp4/...) to usi mein convert ho jaayega.</i>\n\n"
            "<i>Cancel karne ke liye <code>-</code> bhejo.</i>",
            reply_markup=None,
        )

    await job_ctl.run_cancelable(status, user_id, "Rename", _rename_body)


async def _prewarm_download(bot: Client, replied: Message):
    try:
        from ..utils.fast_download import prewarm
        await prewarm(bot, replied)
    except Exception:
        pass


async def _prewarm_upload(bot: Client, user_id: int):
    """User session hai to uska uploader client + turbo sessions, warna bot ke turbo sessions.
    Chupchap fail ho jaata hai — upload khud bana lega."""
    try:
        from ..utils.uploads.telegram import _get_user_session, prewarm_uploader
        if await _get_user_session(user_id):
            await prewarm_uploader(user_id)
        else:
            from ..utils import turbo_upload
            await turbo_upload.prewarm(bot)
    except Exception as e:
        LOGGER.debug(f"[rename] upload prewarm skipped: {e!r}")


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
    _jb = job_ctl.CURRENT.get()
    if _jb is not None:
        _jb.dirs.add(file_path)      # ❌ Cancel pe adhoori file delete
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

    if text != "-" and not os.path.isfile(session["filepath"]):
        await message.reply(
            "⚠️ <b>File server se hat chuki hai</b> (bot restart / cleanup ki wajah se).\n"
            "Kripya file pe reply karke dobara <code>/rename</code> bhejo."
        )
        return

    if text == "-":
        await message.reply("❌ Cancelled.")
        try:
            os.remove(session["filepath"])
        except Exception:
            pass
        return

    async def _rn_body(pm):
        await _do_rename_and_upload(bot, user_id, session["filepath"], text, session["message"], pm)

    await job_ctl.run_cancelable(session["msg"], user_id, "Rename", _rn_body)


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


async def _send_with_fallback(clients, kind: str, **kw):
    """clients ko order mein try karo (pehle user uploader, aakhir mein bot). Last client ka
    error upar jaata hai. 'file_name' unsupported ho to wahi kwarg hata ke retry."""
    last = None
    for cl in clients:
        fn = getattr(cl, kind)
        args = dict(kw)
        try:
            try:
                return await fn(**args)
            except TypeError as e:
                if "file_name" in args and "unexpected keyword argument" in str(e):
                    args.pop("file_name")
                    return await fn(**args)
                raise
        except asyncio.CancelledError:
            raise
        except Exception as e:
            last = e
            if cl is not clients[-1]:
                LOGGER.warning(f"[rename] {kind} via user client fail ({e!r}) — bot se retry")
    raise last


async def _upload_renamed_file(bot: Client, filepath: str, new_filename: str, ext: str,
                                user_id: int, orig_message: Message, status: Message):
    """Final renamed file ko sahi media type (video/audio/document) mein upload karo,
    saved default thumbnail (/setpic ya /thumb) apply karke.

    SPEED: uploader client connect | ffprobe | thumbnail — teeno EK SAATH (pehle ek ke baad ek,
    aur TMDB slow ho to 12s tak upload ruka rehta tha). Custom thumb ab cached
    (thumb_source.fetch_custom_thumb) hai aur kabhi upload fail nahi karta — pehle yahi
    bot.download_media thumb ke liye cross-DC auth.ExportAuthorization maangta tha aur
    FloodWait (420) se poora rename fail ho jata tha."""
    from ..utils.encoding import probe_media_async, get_thumbnail_async
    from ..utils.community import get_community_tag
    from ..utils.thumb_source import get_tmdb_thumbnail, fetch_custom_thumb
    from ..utils.uploads.telegram import _make_uploader_client

    caption = f"<b>{new_filename}</b>"
    custom_thumb = await db.get_thumbnail(user_id)
    thumb_path = None
    uc = None
    c_time = time.time()

    uc_task = asyncio.ensure_future(_make_uploader_client(user_id))   # session nahi to turant None

    async def _dl_thumb(fid, path):
        return await bot.download_media(fid, file_name=path)

    async def _custom_thumb():
        if not custom_thumb:
            return None
        try:
            return await fetch_custom_thumb(custom_thumb, download_dir, _dl_thumb)
        except Exception as e:
            LOGGER.warning(f"[rename] custom thumb fail: {e!r}")
            return None

    try:
        probe = {}
        if ext in VIDEO_EXTENSIONS:
            probe_task = asyncio.ensure_future(probe_media_async(filepath))
            thumb_path = await _custom_thumb()
            try:
                probe = await probe_task or {}
            except Exception:
                probe = {}
            if not thumb_path:
                band_text = await get_community_tag(user_id)
                try:
                    thumb_path = await asyncio.wait_for(
                        get_tmdb_thumbnail(filepath, download_dir, band_text=band_text), timeout=8)
                except Exception:
                    thumb_path = None     # TMDB slow / fail — seedha ffmpeg frame, upload na ruke
                if not thumb_path:
                    dur = int(probe.get("duration") or 0)
                    thumb_path = await get_thumbnail_async(
                        filepath, download_dir, dur / 4 if dur else 0, band_text=band_text)
        else:
            thumb_path = await _custom_thumb()

        try:
            uc = await asyncio.wait_for(asyncio.shield(uc_task), timeout=40)
        except Exception:
            uc = None
        clients = [uc, bot] if uc else [bot]

        common = dict(chat_id=orig_message.chat.id, caption=caption, thumb=thumb_path,
                      file_name=new_filename, progress=progress_for_pyrogram,
                      progress_args=("📤 Uploading...", status, c_time))

        if ext in VIDEO_EXTENSIONS:
            await _send_with_fallback(
                clients, "send_video", video=filepath,
                duration=int(probe.get("duration") or 0),
                width=probe.get("width") or 1280, height=probe.get("height") or 720,
                supports_streaming=True, **common)

        elif ext in AUDIO_EXTENSIONS:
            await _send_with_fallback(clients, "send_audio", audio=filepath, **common)

        else:
            # apk / zip / pdf / kuch bhi — plain document
            await _send_with_fallback(clients, "send_document", document=filepath, **common)

        try:
            await status.edit("✅ <b>Renamed & Uploaded!</b>")
        except Exception:
            pass

    finally:
        if uc is None:
            # Connect abhi chal raha ho / timeout ke baad bana ho — ban-ne par chupchap release
            def _late_release(t):
                try:
                    c = t.result()
                    if c:
                        asyncio.ensure_future(c.disconnect())
                except BaseException:
                    pass
            if uc_task.done():
                _late_release(uc_task)
            else:
                uc_task.add_done_callback(_late_release)
        if uc:
            try:
                await uc.disconnect()     # sirf release (shared client)
            except Exception:
                pass
        if thumb_path and os.path.isfile(thumb_path):
            try:
                os.remove(thumb_path)
            except Exception:
                pass
