"""
URL Uploader Plugin for Encode Bot
Features:
  - /url <link>           → Download + auto-apply saved settings → upload
  - /url <link> -vt       → Download + show manual option buttons (old behavior)
  - /url <link> -e        → Download zip/archive → unzip → auto-apply settings → upload ALL files
  - /url <link> -e -vt    → Download zip/archive → unzip → show manual option buttons
  - /url <link> | <name>  → Custom filename (all flags still work)

  Auto-settings configure karne ke liye: /urlpreset command use karo
"""

import asyncio
import os
import re
import subprocess
import time
import zipfile
import tarfile
import shutil
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
# { user_id: { 'filepath': str, 'msg': Message, 'orig_name': str, 'message': Message } }
_url_sessions: dict = {}


# ─── /url command ─────────────────────────────────────────────────────────────
@Client.on_message(filters.command("url"))
async def url_upload_cmd(bot: Client, message: Message):
    """
    Usage:
      /url <link>              → auto-process (saved settings ke hisaab se)
      /url <link> -vt          → manual options buttons dikhao
      /url <link> -e           → zip/archive download → unzip → ALL files auto-process
      /url <link> -e -vt       → zip/archive download → unzip → manual options (first file)
      /url <link> | <filename> → custom filename (flags bhi kaam karte hain)
    """
    c = await check_chat(message, chat="Both")
    if not c:
        return
    await AddUserToDatabase(bot, message)

    if len(message.command) < 2:
        await message.reply(
            "<b>Usage:</b>\n"
            "• <code>/url &lt;link&gt;</code> – Auto-process with saved settings\n"
            "• <code>/url &lt;link&gt; -vt</code> – Show manual option buttons\n"
            "• <code>/url &lt;link&gt; -e</code> – Unzip then auto-process ALL files\n"
            "• <code>/url &lt;link&gt; -e -vt</code> – Unzip then show buttons\n"
            "• <code>/url &lt;link&gt; | &lt;filename&gt;</code> – Custom filename\n\n"
            "Auto-settings configure karo: /urlpreset"
        )
        return

    raw = message.text.split(None, 1)[1].strip()

    # ── Flags parse karo ──
    show_buttons = "-vt" in raw
    extract_zip  = "-e"  in raw

    # Flags remove karo
    raw = raw.replace("-vt", "").replace("-e", "").strip()

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
        custom_name = await _get_filename_from_url(url)

    # Filename too long fix — Linux max = 255 bytes, safe limit = 200 chars
    custom_name = _safe_filename(custom_name)

    msg = await message.reply("<b>💠 Downloading...</b>")

    try:
        filepath = await _download_url(url, custom_name, msg, message)
    except Exception as e:
        await msg.edit(f"❌ Download failed: <code>{e}</code>")
        return

    if not filepath or not os.path.isfile(filepath):
        await msg.edit("❌ Download failed or file not found.")
        return

    # ── ZIP/Archive extract ────────────────────────────────────────────────────
    if extract_zip:
        await msg.edit("<b>📦 Extracting archive...</b>")
        # FIX: ab ALL files return hongi (list), sirf ek nahi
        all_files = await _extract_archive_all(filepath, msg)
        if not all_files:
            return

        total = len(all_files)

        if show_buttons:
            # -vt mode: sirf pehli file ke liye buttons dikhao
            filepath = all_files[0]
            user_id = message.from_user.id
            _url_sessions[user_id] = {
                "filepath": filepath,
                "msg": msg,
                "orig_name": os.path.basename(filepath),
                "message": message,
                # Remaining files queue mein
                "zip_queue": all_files[1:],
            }
            await _show_url_options(msg, user_id, filepath)
        else:
            # Auto mode: SAARI files process + upload karo (urlpreset settings apply hongi)
            await msg.edit(f"<b>📦 Extracted {total} files! Processing...</b>")
            user_id = message.from_user.id
            for idx, fp in enumerate(all_files, 1):
                status_msg = await message.reply(
                    f"<b>⚙️ [{idx}/{total}] Processing:</b> <code>{os.path.basename(fp)}</code>"
                )
                _url_sessions[user_id] = {
                    "filepath": fp,
                    "msg": status_msg,
                    "orig_name": os.path.basename(fp),
                    "message": message,
                    "has_eng_sub": False,
                }
                # urlpreset ki saari settings apply hongi _auto_process_and_upload mein
                await _auto_process_and_upload(bot, user_id, status_msg, message)
            await msg.delete()
        return

    # ── Single file flow (no zip) ──────────────────────────────────────────────
    user_id = message.from_user.id
    _url_sessions[user_id] = {
        "filepath": filepath,
        "msg": msg,
        "orig_name": os.path.basename(filepath),
        "message": message,
    }

    if show_buttons:
        await _show_url_options(msg, user_id, filepath)
    else:
        await _auto_process_and_upload(bot, user_id, msg, message)


# ─── Auto-process (saved settings ke hisaab se) ───────────────────────────────
async def _auto_process_and_upload(bot: Client, user_id: int, msg: Message, original_message: Message):
    """
    User ki saved URL auto-settings ke hisaab se file process karo aur upload karo.
    """
    session = _url_sessions.get(user_id)
    if not session:
        await msg.edit("⌛ Session expired.")
        return

    filepath = session["filepath"]
    auto = await db.get_url_auto_settings(user_id)

    # ── 1. Remove Subtitles ──
    if auto.get("rm_sub"):
        await msg.edit("<b>🔄 Removing subtitles (auto)...</b>")
        new_path = await _remove_subtitles(filepath, msg)
        if new_path:
            filepath = new_path
            session["filepath"] = filepath
            _url_sessions[user_id] = session

    # ── 2. Remove Audio ──
    if auto.get("rm_audio"):
        await msg.edit("<b>🔄 Removing audio (auto)...</b>")
        new_path = await _remove_audio(filepath, msg)
        if new_path:
            filepath = new_path
            session["filepath"] = filepath
            _url_sessions[user_id] = session

    # ── 3. Hindi Audio Only (rm_audio off ho toh hi) ──
    # FIX: sirf 'language' field se detect karo (title se nahi)
    elif auto.get("hindi_only"):
        await msg.edit("<b>🔄 Keeping Hindi audio only (auto)...</b>")
        audio_streams = get_audio_streams(filepath)
        hindi_indices = [
            s["index"] for s in audio_streams
            if _is_hindi_stream(s)
        ]
        if hindi_indices:
            new_path = await _keep_audio_streams(filepath, hindi_indices, msg)
            if new_path:
                filepath = new_path
                session["filepath"] = filepath
                _url_sessions[user_id] = session
        else:
            stream_info = ", ".join(
                f"#{s['index']}:{s.get('lang','?')}" for s in audio_streams
            )
            await msg.edit(
                f"<b>⚠️ Hindi audio nahi mila!</b>\n"
                f"Streams found: <code>{stream_info}</code>\n"
                "Sab streams rakh rahe hain..."
            )
            await asyncio.sleep(3)

    # ── 4. Eng Sub Only (rm_sub off ho toh hi) ──
    # rm_sub ON ho toh sab subs pehle hi hata diye — eng_sub_only skip karo
    if not auto.get("rm_sub") and auto.get("eng_sub_only"):
        await msg.edit("<b>🔄 Keeping English subtitles only (auto)...</b>")
        sub_streams = get_subtitle_streams(filepath)
        eng_indices = [
            s["index"] for s in sub_streams
            if _is_english_sub_stream(s)
        ]
        if eng_indices:
            new_path = await _keep_subtitle_streams(filepath, eng_indices, msg)
            if new_path:
                filepath = new_path
                # ✅ Flag: eng sub mili — caption mein Esub lagega
                session["has_eng_sub"] = True
                session["filepath"] = filepath
                _url_sessions[user_id] = session
        else:
            stream_info = ", ".join(
                f"#{s['index']}:{s.get('lang','?')}" for s in sub_streams
            )
            await msg.edit(
                f"<b>⚠️ English subtitle nahi mila!</b>\n"
                f"Streams found: <code>{stream_info or 'none'}</code>\n"
                "Koi sub nahi rakha ja raha..."
            )
            await asyncio.sleep(3)

    # ── 5. Convert Audio to AAC (rm_audio off ho toh hi) ──
    # E-AC3, DTS, TrueHD jaisi formats ko AAC mein convert karo
    if not auto.get("rm_audio") and auto.get("to_aac"):
        already_aac = await _check_all_audio_aac(filepath)
        if not already_aac:
            await msg.edit("<b>🔊 Converting audio to AAC (auto)...</b>")
            new_path = await _convert_audio_to_aac(filepath, msg)
            if new_path:
                filepath = new_path
                session["filepath"] = filepath
                _url_sessions[user_id] = session
        else:
            LOGGER.info("to_aac: audio already AAC — skipping conversion")

    # ── 6. Name Swap ──
    if auto.get("name_swap"):
        swap_rules = await db.get_swap(user_id)
        if swap_rules:
            old_name = os.path.basename(filepath)
            new_name = apply_name_swap(old_name, swap_rules)
            if new_name != old_name:
                new_path = os.path.join(os.path.dirname(filepath), new_name)
                try:
                    os.rename(filepath, new_path)
                    filepath = new_path
                    session["filepath"] = filepath
                    _url_sessions[user_id] = session
                    LOGGER.info(f"Name swap: {old_name} → {new_name}")
                except Exception as e:
                    LOGGER.error(f"Name swap rename failed: {e}")
            else:
                LOGGER.info(f"Name swap: no match in '{old_name}'")
        else:
            LOGGER.info("Name swap ON but no rules set — skipping")

    # ── 6. Apply Metadata ──
    # FIX: get_full_metadata use karo (yahi /setmeta se set hota hai)
    # {audiolang} aur {sublang} ko actual language names se replace karo
    if auto.get("apply_metadata"):
        full_meta = await db.get_full_metadata(user_id)
        if full_meta.get("enabled") and any([
            full_meta.get("movie_name"),
            full_meta.get("video_title"),
            full_meta.get("audio_title"),
            full_meta.get("subtitle_title"),
            full_meta.get("comment"),
        ]):
            await msg.edit("<b>🔄 Applying metadata (auto)...</b>")
            # {audiolang}/{sublang} resolve karo
            resolved = _resolve_meta_placeholders(full_meta, filepath)
            new_path = await _apply_full_metadata(filepath, resolved, msg)
            if new_path:
                filepath = new_path
                session["filepath"] = filepath
                _url_sessions[user_id] = session

    # ── Upload ──
    await _do_upload(bot, filepath, original_message, msg, has_eng_sub=session.get("has_eng_sub", False))
    _url_sessions.pop(user_id, None)


# ─── Hindi stream detection ────────────────────────────────────────────────────
def _is_hindi_stream(stream: dict) -> bool:
    """
    Stream Hindi hai ya nahi — sirf 'language' metadata field se check karo.
    ffprobe 'lang' field = ISO 639 code (hin) ya full name (Hindi).
    """
    lang = stream.get("lang", "").lower().strip()
    HINDI_CODES = {"hin", "hi", "hindi", "hin2", "hin-in"}
    return lang in HINDI_CODES


# ─── Subtitle stream helpers ───────────────────────────────────────────────────
def get_subtitle_streams(filepath: str) -> list:
    """
    File ke saare subtitle streams return karo.
    List of dicts: [{ 'index': int, 'lang': str, 'title': str }, ...]
    """
    import json, subprocess as _sp
    try:
        cmd = [
            "ffprobe", "-hide_banner", "-print_format", "json",
            "-show_streams", filepath
        ]
        out = _sp.check_output(cmd, stderr=_sp.DEVNULL)
        streams = json.loads(out.decode()).get("streams", [])
    except Exception:
        return []

    result = []
    sub_idx = 0
    for s in streams:
        if s.get("codec_type") == "subtitle":
            tags = s.get("tags", {})
            result.append({
                "index":      s.get("index", 0),   # absolute stream index
                "sub_index":  sub_idx,              # relative subtitle index (0,1,2...)
                "lang":       tags.get("language", tags.get("LANGUAGE", "")).lower().strip(),
                "title":      tags.get("title",    tags.get("TITLE",    "")),
            })
            sub_idx += 1
    return result


def _is_english_sub_stream(stream: dict) -> bool:
    """
    Stream English subtitle hai ya nahi.
    'language' field se check karo (ISO 639: eng / en).
    """
    lang = stream.get("lang", "").lower().strip()
    ENG_CODES = {"eng", "en", "english"}
    return lang in ENG_CODES


async def _keep_subtitle_streams(filepath: str, abs_indices: list, msg: Message) -> str | None:
    """
    Sirf given absolute stream indices wale subtitles rakh, baaki hata do.
    Video + Audio sab waise rakhna, sirf sub filter karna.
    """
    out = _make_output_path(filepath, "_engsub")

    # Saare subtitle streams lo — jinhe rakhna hai unka relative index chahiye ffmpeg ke liye
    sub_streams = get_subtitle_streams(filepath)
    if not sub_streams:
        return None

    map_args = ["-map", "0:v?", "-map", "0:a?"]
    kept = 0
    for s in sub_streams:
        if s["index"] in abs_indices:
            map_args += ["-map", f"0:s:{s['sub_index']}"]
            if kept == 0:
                # Pehli wali sub ko default mark karo
                map_args += [f"-disposition:s:{kept}", "default"]
            kept += 1

    if kept == 0:
        LOGGER.warning("_keep_subtitle_streams: no matching streams — skipping")
        return None

    map_args += ["-c", "copy"]
    return await _ffmpeg_process(filepath, out, map_args, msg)


# ─── Placeholder resolver ─────────────────────────────────────────────────────
def _resolve_meta_placeholders(meta: dict, filepath: str) -> dict:
    """
    {audiolang} → actual audio language name (e.g. Hindi, Japanese)
    {sublang}   → actual subtitle language name
    File ke streams se actual language fetch karke replace karo.
    """
    resolved = dict(meta)

    audio_title = meta.get("audio_title", "")
    sub_title = meta.get("subtitle_title", "")

    needs_audio_lang = "{audiolang}" in audio_title
    needs_sub_lang = "{sublang}" in sub_title

    if not (needs_audio_lang or needs_sub_lang):
        return resolved

    # ffprobe se streams lo
    try:
        import json, subprocess
        cmd = [
            "ffprobe", "-hide_banner", "-print_format", "json",
            "-show_streams", filepath
        ]
        output = subprocess.check_output(cmd, stderr=subprocess.DEVNULL)
        streams = json.loads(output.decode()).get("streams", [])
    except Exception:
        return resolved

    # Language code → human readable name
    LANG_MAP = {
        "hin": "Hindi", "hi": "Hindi", "hindi": "Hindi",
        "jpn": "Japanese", "ja": "Japanese",
        "eng": "English", "en": "English",
        "tam": "Tamil", "tel": "Telugu",
        "kan": "Kannada", "mal": "Malayalam",
        "ben": "Bengali", "mar": "Marathi",
        "chi": "Chinese", "zho": "Chinese",
        "kor": "Korean", "ko": "Korean",
        "ara": "Arabic", "spa": "Spanish",
        "fre": "French", "fra": "French",
        "ger": "German", "deu": "German",
        "rus": "Russian", "por": "Portuguese",
        "ita": "Italian", "dub": "Dubbed",
        "und": "Unknown", "": "Unknown",
    }

    if needs_audio_lang:
        # Pehla audio stream ki language lo
        for s in streams:
            if s.get("codec_type") == "audio":
                tags = s.get("tags", {})
                lang_code = tags.get("language", tags.get("LANGUAGE", "")).lower()
                lang_name = LANG_MAP.get(lang_code, lang_code.title() if lang_code else "Audio")
                resolved["audio_title"] = audio_title.replace("{audiolang}", lang_name)
                break

    if needs_sub_lang:
        # Pehla subtitle stream ki language lo
        for s in streams:
            if s.get("codec_type") == "subtitle":
                tags = s.get("tags", {})
                lang_code = tags.get("language", tags.get("LANGUAGE", "")).lower()
                lang_name = LANG_MAP.get(lang_code, lang_code.title() if lang_code else "Sub")
                resolved["subtitle_title"] = sub_title.replace("{sublang}", lang_name)
                break

    return resolved


# ─── Archive Extract (ALL files) ──────────────────────────────────────────────
async def _extract_archive_all(filepath: str, msg: Message) -> list:
    """
    ZIP/TAR archive ko extract karo — saari video files return karo (list).
    FIX: pehle sirf ek file return karta tha, ab saari return hoti hain.
    """
    VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".flv", ".ts", ".m4v", ".wmv", ".webm"}
    ARCHIVE_EXTS = {".zip", ".tar", ".gz", ".bz2", ".xz"}

    extract_dir = filepath + "_extracted"
    os.makedirs(extract_dir, exist_ok=True)

    def _extract_one(src_path: str, dst_dir: str) -> bool:
        try:
            if zipfile.is_zipfile(src_path):
                with zipfile.ZipFile(src_path, "r") as zf:
                    zf.extractall(dst_dir)
                return True
            elif tarfile.is_tarfile(src_path):
                with tarfile.open(src_path, "r:*") as tf:
                    tf.extractall(dst_dir)
                return True
        except Exception:
            pass
        return False

    # Step 1: Main archive extract karo
    ok = _extract_one(filepath, extract_dir)
    try:
        os.remove(filepath)
    except Exception:
        pass

    if not ok:
        await msg.edit(
            "❌ <b>Extract failed:</b> File ZIP ya TAR format mein nahi hai.\n"
            "Supported: .zip, .tar, .tar.gz, .tar.bz2, .tar.xz"
        )
        shutil.rmtree(extract_dir, ignore_errors=True)
        return []

    # Step 2: Nested archives bhi extract karo (max 3 levels deep)
    for _level in range(3):
        nested_found = False
        for root, dirs, files in os.walk(extract_dir):
            for f in files:
                ext = os.path.splitext(f)[1].lower()
                if ext in ARCHIVE_EXTS:
                    nested_path = os.path.join(root, f)
                    nested_dir = nested_path + "_inner"
                    os.makedirs(nested_dir, exist_ok=True)
                    if _extract_one(nested_path, nested_dir):
                        nested_found = True
                        try:
                            os.remove(nested_path)
                        except Exception:
                            pass
                        for item in os.listdir(nested_dir):
                            shutil.move(os.path.join(nested_dir, item), root)
                        shutil.rmtree(nested_dir, ignore_errors=True)
        if not nested_found:
            break

    # Step 3: SAARI video files dhundo (recursively)
    video_files = []
    for root, dirs, files in os.walk(extract_dir):
        for f in files:
            if os.path.splitext(f)[1].lower() in VIDEO_EXTS:
                video_files.append(os.path.join(root, f))

    if not video_files:
        all_files = []
        for root, dirs, files in os.walk(extract_dir):
            all_files.extend(files)
        await msg.edit(
            "❌ <b>Extract failed:</b> Archive mein koi video file nahi mili.\n"
            f"Extracted files: {all_files[:10]}"
        )
        shutil.rmtree(extract_dir, ignore_errors=True)
        return []

    # Episode number se sort karo (natural sort)
    def _natural_key(path):
        name = os.path.basename(path)
        parts = re.split(r'(\d+)', name)
        return [int(p) if p.isdigit() else p.lower() for p in parts]

    video_files.sort(key=_natural_key)

    # Saari files download_dir mein move karo
    dest_files = []
    for vf in video_files:
        dest = os.path.join(download_dir, os.path.basename(vf))
        # Same name conflict handle karo
        if os.path.exists(dest):
            base, ext = os.path.splitext(os.path.basename(vf))
            dest = os.path.join(download_dir, f"{base}_{int(time.time())}{ext}")
        shutil.move(vf, dest)
        dest_files.append(dest)

    shutil.rmtree(extract_dir, ignore_errors=True)

    await msg.edit(
        f"📦 <b>Extracted!</b> {len(dest_files)} video files mili.\n"
        f"Processing all in order..."
    )
    await asyncio.sleep(2)

    return dest_files


# ─── Show options keyboard ────────────────────────────────────────────────────
async def _show_url_options(msg: Message, user_id: int, filepath: str):
    fname = os.path.basename(filepath)
    session = _url_sessions.get(user_id, {})
    queue = session.get("zip_queue", [])
    queue_info = f"\n<i>({len(queue)} more files in queue)</i>" if queue else ""

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🚫 Remove Subs",       callback_data=f"url_rmsub_{user_id}"),
            InlineKeyboardButton("🔇 Remove Audio",      callback_data=f"url_rmaudio_{user_id}"),
        ],
        [
            InlineKeyboardButton("🇮🇳 Hindi Audio Only", callback_data=f"url_hindionly_{user_id}"),
            InlineKeyboardButton("🇬🇧 Eng Sub Only",     callback_data=f"url_engsub_{user_id}"),
        ],
        [
            InlineKeyboardButton("🔊 Convert to AAC",    callback_data=f"url_toaac_{user_id}"),
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
        f"✅ <b>Downloaded:</b> <code>{fname}</code>{queue_info}\n\nChoose what to do before uploading:",
        reply_markup=kb,
    )


# ─── Callback handlers ────────────────────────────────────────────────────────
@Client.on_callback_query(filters.regex(r"^url_"))
async def url_upload_callbacks(bot: Client, cb: CallbackQuery):
    data_str = cb.data  # e.g. "url_rmsub_12345"
    parts = data_str.split("_")
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
        await _do_upload(bot, filepath, original_message, msg, has_eng_sub=session.get("has_eng_sub", False))
        # FIX: zip_queue process karo upload ke baad
        await _process_zip_queue(bot, owner_id, original_message)

    # ── Cancel ────────────────────────────────────────────────────────────────
    elif action == "cancel":
        await cb.answer("Cancelled!")
        try:
            os.remove(filepath)
        except Exception:
            pass
        # Queue mein baaki files bhi delete karo
        for qf in session.get("zip_queue", []):
            try:
                os.remove(qf)
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

    # ── Eng Sub Only ─────────────────────────────────────────────────────────
    elif action == "engsub":
        await cb.answer()
        await msg.edit("<b>🔄 Detecting subtitle streams...</b>")
        sub_streams = get_subtitle_streams(filepath)
        eng_indices = [
            s["index"] for s in sub_streams
            if _is_english_sub_stream(s)
        ]
        if not eng_indices:
            streams_text = ", ".join(
                f"#{s['index']}:{s.get('lang','?')}" for s in sub_streams
            ) or "none"
            await cb.answer(
                f"⚠️ English subtitle nahi mila! Streams: {streams_text}",
                show_alert=True,
            )
            await _show_url_options(msg, owner_id, filepath)
            return
        await msg.edit(f"<b>🔄 Keeping only English subtitles (streams: {eng_indices})...</b>")
        new_path = await _keep_subtitle_streams(filepath, eng_indices, msg)
        if new_path:
            # ✅ Flag: eng sub mili — caption mein Esub lagega
            session["has_eng_sub"] = True
            session["filepath"] = new_path
            _url_sessions[owner_id] = session
        await _show_url_options(msg, owner_id, session["filepath"])

    # ── Convert Audio to AAC ──────────────────────────────────────────────────
    elif action == "toaac":
        await cb.answer()
        await msg.edit("<b>🔄 Checking audio streams...</b>")
        # Check karo kya already AAC hai
        already_aac = await _check_all_audio_aac(filepath)
        if already_aac:
            await cb.answer("ℹ️ Audio already AAC format mein hai!", show_alert=True)
            await _show_url_options(msg, owner_id, filepath)
            return
        await msg.edit(
            "<b>🔊 Converting audio to AAC...</b>\n"
            "<i>E-AC3/DTS/TrueHD → AAC 192k stereo (sabhi devices compatible)</i>"
        )
        new_path = await _convert_audio_to_aac(filepath, msg)
        if new_path:
            session["filepath"] = new_path
            _url_sessions[owner_id] = session
            await cb.answer("✅ Audio AAC mein convert ho gaya!", show_alert=False)
        else:
            await cb.answer("❌ Conversion failed!", show_alert=True)
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


# ─── ZIP Queue processor (after -vt upload) ───────────────────────────────────
async def _process_zip_queue(bot: Client, user_id: int, original_message: Message):
    """
    -vt mode mein pehli file upload hone ke baad
    zip_queue mein baaki files auto-process karo.
    """
    session = _url_sessions.pop(user_id, None)
    if not session:
        return
    queue = session.get("zip_queue", [])
    if not queue:
        return

    total = len(queue)
    for idx, fp in enumerate(queue, 1):
        if not os.path.isfile(fp):
            continue
        status_msg = await original_message.reply(
            f"<b>⚙️ [{idx}/{total}] Processing queue:</b> <code>{os.path.basename(fp)}</code>"
        )
        _url_sessions[user_id] = {
            "filepath": fp,
            "msg": status_msg,
            "orig_name": os.path.basename(fp),
            "message": original_message,
        }
        await _auto_process_and_upload(bot, user_id, status_msg, original_message)


# ─── Text handler for metadata input ─────────────────────────────────────────
@Client.on_message(filters.text & filters.private, group=3)
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


# ─── /addswap — Interactive button panel (like /setmeta) ──────────────────────
# { user_id: 'awaiting_from' | 'awaiting_to' | from_value }
# We store state as a dict:  { 'step': 'from'|'to', 'from': str }
_addswap_sessions: dict = {}


@Client.on_message(filters.command("addswap"))
async def add_swap_rule(bot: Client, message: Message):
    """
    /addswap  → Interactive button panel jisme existing rules dikh'te hain
               aur "➕ Add New Rule" button se naya rule add kar sakte hain.

    Old text format bhi kaam karta hai:
      /addswap old_text new_text
      /addswap find1:change1|find2:change2
    """
    c = await check_chat(message, chat="Both")
    if not c:
        return

    args = message.text.split(None, 1)

    # ── Agar inline args diye hain toh seedha add karo (backwards compat) ──
    if len(args) >= 2 and args[1].strip():
        raw = args[1].strip()
        rules = await db.get_swap(message.from_user.id)
        added = []

        if "|" in raw or (":" in raw and len(raw.split(None)) == 1):
            for pair in raw.split("|"):
                pair = pair.strip()
                if ":" not in pair:
                    continue
                from_text, to_text = pair.split(":", 1)
                from_text, to_text = from_text.strip(), to_text.strip()
                if from_text:
                    rules[from_text] = to_text
                    added.append((from_text, to_text))
        else:
            parts = raw.split(None, 1)
            if len(parts) < 2:
                await message.reply(
                    "❌ <b>2 arguments chahiye!</b>\n"
                    "<code>/addswap old_text new_text</code>"
                )
                return
            from_text, to_text = parts[0].strip(), parts[1].strip()
            rules[from_text] = to_text
            added.append((from_text, to_text))

        await db.set_swap(message.from_user.id, rules)
        lines = "\n".join(f"• <code>{f}</code> → <code>{t}</code>" for f, t in added)
        await message.reply(
            f"✅ <b>{len(added)} swap rule(s) added:</b>\n{lines}\n\n"
            f"Total rules: <b>{len(rules)}</b>",
            reply_markup=output,
        )
        return

    # ── No args → show interactive panel ──
    await _show_addswap_panel(message, message.from_user.id, is_new=True)


async def _show_addswap_panel(event, user_id: int, is_new: bool = False):
    """Interactive swap rules panel — existing rules dikhao + add/delete buttons."""
    rules = await db.get_swap(user_id)

    # Build rule buttons (each rule = one row with a ❌ delete button)
    rule_rows = []
    for from_text, to_text in rules.items():
        label = f"🔄 {from_text} → {to_text}"
        # Truncate label agar bahut lamba ho
        if len(label) > 48:
            label = label[:45] + "…"
        rule_rows.append([
            InlineKeyboardButton(label, callback_data=f"asw_noop_{user_id}"),
            InlineKeyboardButton("🗑️", callback_data=f"asw_del_{from_text}_{user_id}"),
        ])

    bottom_row = [
        InlineKeyboardButton("➕ Add New Rule", callback_data=f"asw_add_{user_id}"),
    ]
    if rules:
        bottom_row.append(
            InlineKeyboardButton("🗑️ Clear All", callback_data=f"asw_clearall_{user_id}")
        )
    bottom_row.append(InlineKeyboardButton("❌ Close", callback_data="closeMeh"))

    kb = InlineKeyboardMarkup(rule_rows + [bottom_row])

    if rules:
        rules_text = "\n".join(f"• <code>{k}</code> → <code>{v}</code>" for k, v in rules.items())
    else:
        rules_text = "  <i>Koi rule set nahi hai</i>"

    text = (
        "<b>🔄 Name Swap Rules</b>\n\n"
        f"{rules_text}\n\n"
        f"Total rules: <b>{len(rules)}</b>\n\n"
        "<i>➕ Add New Rule pe tap karo → pehle 'find text' bhejo → phir 'replace text' bhejo → done!</i>\n"
        "<i>🗑️ button se koi bhi rule delete kar sakte ho.</i>"
    )

    if is_new:
        await event.reply(text, reply_markup=kb)
    else:
        try:
            await event.edit(text, reply_markup=kb)
        except Exception:
            pass


@Client.on_callback_query(filters.regex(r"^asw_"))
async def addswap_callbacks(bot: Client, cb: CallbackQuery):
    """Addswap panel ke saare callbacks."""
    data = cb.data  # e.g. asw_add_123 | asw_del_HindiZone_123 | asw_clearall_123

    # ── asw_noop (rule label button — sirf display, kuch nahi karna) ──
    if data.startswith("asw_noop_"):
        await cb.answer("Ye rule ka naam hai. Delete karne ke liye 🗑️ dabao.", show_alert=False)
        return

    # ── asw_add_<userid> ──
    if data.startswith("asw_add_"):
        try:
            owner_id = int(data.split("_")[-1])
        except ValueError:
            await cb.answer()
            return
        if cb.from_user.id != owner_id:
            await cb.answer("❌ Ye tumhara nahi hai!", show_alert=True)
            return
        # Session start: step 1 — "from" text maango
        _addswap_sessions[owner_id] = {"step": "from", "from": ""}
        await cb.answer()
        await cb.message.edit(
            "<b>➕ New Swap Rule — Step 1/2</b>\n\n"
            "Wo <b>text</b> bhejo jo <b>find</b> karna hai (purana text).\n\n"
            "<b>Example:</b> <code>HindiAnimeZone.com</code>\n\n"
            "<i>Send <code>-</code> (dash) to cancel.</i>",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Back", callback_data=f"asw_back_{owner_id}")
            ]])
        )
        return

    # ── asw_del_<from_text>_<userid> ──
    if data.startswith("asw_del_"):
        # format: asw_del_<from_text>_<userid>
        # from_text mein _ ho sakti hai, isliye last part = userid
        parts = data.split("_")
        # parts[0]=asw, parts[1]=del, parts[-1]=userid, parts[2:-1]=from_text
        try:
            owner_id = int(parts[-1])
        except ValueError:
            await cb.answer()
            return
        if cb.from_user.id != owner_id:
            await cb.answer("❌ Ye tumhara nahi hai!", show_alert=True)
            return
        from_key = "_".join(parts[2:-1])
        rules = await db.get_swap(owner_id)
        if from_key in rules:
            del rules[from_key]
            await db.set_swap(owner_id, rules)
            await cb.answer(f"✅ Rule deleted: {from_key}")
        else:
            await cb.answer("Rule nahi mila!", show_alert=True)
        await _show_addswap_panel(cb.message, owner_id, is_new=False)
        return

    # ── asw_clearall_<userid> ──
    if data.startswith("asw_clearall_"):
        try:
            owner_id = int(data.split("_")[-1])
        except ValueError:
            await cb.answer()
            return
        if cb.from_user.id != owner_id:
            await cb.answer("❌ Ye tumhara nahi hai!", show_alert=True)
            return
        await db.clear_swap(owner_id)
        await cb.answer("✅ All rules cleared!")
        await _show_addswap_panel(cb.message, owner_id, is_new=False)
        return

    # ── asw_back_<userid> ──
    if data.startswith("asw_back_"):
        try:
            owner_id = int(data.split("_")[-1])
        except ValueError:
            await cb.answer()
            return
        _addswap_sessions.pop(owner_id, None)
        await cb.answer()
        await _show_addswap_panel(cb.message, owner_id, is_new=False)
        return

    await cb.answer()


# ─── Text handler: addswap step input ─────────────────────────────────────────
@Client.on_message(filters.text & filters.private, group=2)
async def addswap_text_input(bot: Client, message: Message):
    """Addswap panel ka text input — 2 step: from text, phir to text."""
    user_id = message.from_user.id
    session = _addswap_sessions.get(user_id)
    if not session:
        return  # Hamara kaam nahi

    text = message.text.strip()

    # Cancel
    if text == "-":
        _addswap_sessions.pop(user_id, None)
        confirm = await message.reply("❌ Cancelled.")
        await asyncio.sleep(1)
        await _show_addswap_panel(confirm, user_id, is_new=True)
        return

    if session["step"] == "from":
        # Step 1 done — "from" text mila, ab "to" maango
        _addswap_sessions[user_id] = {"step": "to", "from": text}
        try:
            await message.delete()
        except Exception:
            pass
        await message.reply(
            "<b>➕ New Swap Rule — Step 2/2</b>\n\n"
            f"Find text: <code>{text}</code>\n\n"
            "Ab <b>replacement text</b> bhejo (naya text kya hoga).\n\n"
            "<b>Example:</b> <code>@SBANIME</code>\n\n"
            "<i>Send <code>-</code> (dash) to cancel.</i>",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Back", callback_data=f"asw_back_{user_id}")
            ]])
        )

    elif session["step"] == "to":
        # Step 2 done — rule complete
        from_text = session["from"]
        to_text = text
        _addswap_sessions.pop(user_id)

        rules = await db.get_swap(user_id)
        rules[from_text] = to_text
        await db.set_swap(user_id, rules)

        try:
            await message.delete()
        except Exception:
            pass

        confirm = await message.reply(
            f"✅ <b>Swap rule added!</b>\n"
            f"<code>{from_text}</code> → <code>{to_text}</code>\n\n"
            f"Total rules: <b>{len(rules)}</b>"
        )
        await asyncio.sleep(2)
        await _show_addswap_panel(confirm, user_id, is_new=True)


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


# ─── Public: urlpreset apply (bot_upload.py bhi use karta hai) ───────────────

async def apply_urlpreset_to_file(filepath: str, user_id: int, msg: Message) -> tuple[str, bool]:
    """
    User ki /urlpreset settings file pe apply karo.
    Returns: (processed_filepath, has_eng_sub)
    bot_upload.py upload se pehle is function ko call karta hai.
    """
    auto = await db.get_url_auto_settings(user_id)
    has_eng_sub = False

    # 1. Remove Subtitles
    if auto.get("rm_sub"):
        new_path = await _remove_subtitles(filepath, msg)
        if new_path:
            filepath = new_path

    # 2. Remove Audio
    if auto.get("rm_audio"):
        new_path = await _remove_audio(filepath, msg)
        if new_path:
            filepath = new_path

    # 3. Hindi Audio Only (sirf jab rm_audio OFF ho)
    elif auto.get("hindi_only"):
        audio_streams = get_audio_streams(filepath)
        hindi_indices = [s["index"] for s in audio_streams if _is_hindi_stream(s)]
        if hindi_indices:
            new_path = await _keep_audio_streams(filepath, hindi_indices, msg)
            if new_path:
                filepath = new_path
        else:
            stream_info = ", ".join(f"#{s['index']}:{s.get('lang','?')}" for s in audio_streams)
            LOGGER.warning(f"[urlpreset] Hindi audio nahi mila. Streams: {stream_info}")

    # 4. Eng Sub Only (sirf jab rm_sub OFF ho)
    if not auto.get("rm_sub") and auto.get("eng_sub_only"):
        sub_streams = get_subtitle_streams(filepath)
        eng_indices = [s["index"] for s in sub_streams if _is_english_sub_stream(s)]
        if eng_indices:
            new_path = await _keep_subtitle_streams(filepath, eng_indices, msg)
            if new_path:
                has_eng_sub = True
                filepath = new_path

    # 5. Convert to AAC (sirf jab rm_audio OFF ho)
    if not auto.get("rm_audio") and auto.get("to_aac"):
        already_aac = await _check_all_audio_aac(filepath)
        if not already_aac:
            new_path = await _convert_audio_to_aac(filepath, msg)
            if new_path:
                filepath = new_path

    # 6. Name Swap
    if auto.get("name_swap"):
        swap_rules = await db.get_swap(user_id)
        if swap_rules:
            old_name = os.path.basename(filepath)
            new_name = apply_name_swap(old_name, swap_rules)
            if new_name != old_name:
                new_path = os.path.join(os.path.dirname(filepath), new_name)
                try:
                    os.rename(filepath, new_path)
                    filepath = new_path
                except Exception as e:
                    LOGGER.error(f"[urlpreset] Name swap rename failed: {e}")

    # 7. Apply Metadata
    if auto.get("apply_metadata"):
        full_meta = await db.get_full_metadata(user_id)
        if full_meta.get("enabled") and any([
            full_meta.get("movie_name"), full_meta.get("video_title"),
            full_meta.get("audio_title"), full_meta.get("subtitle_title"),
            full_meta.get("comment"),
        ]):
            resolved = _resolve_meta_placeholders(full_meta, filepath)
            new_path = await _apply_full_metadata(filepath, resolved, msg)
            if new_path:
                filepath = new_path

    return filepath, has_eng_sub


# ─── Helper functions ─────────────────────────────────────────────────────────

async def _get_filename_from_url(url: str) -> str:
    """
    URL se real filename nikalo.
    Priority:
    1. HTTP HEAD request ka Content-Disposition header (real filename hota hai)
    2. URL ke basename mein valid extension ho toh use karo
    3. Fallback: 'downloaded_file'

    Yeh fix isliye zaruri hai kyunki Google jaisi URLs mein
    basename ek lamba encoded string hota hai (e.g. ADGPM2n...) jo
    caption mein URL jaisi dikh ti hai — real filename nahi hoti.
    """
    import aiohttp
    import re as _re

    VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".flv", ".ts", ".m4v", ".wmv", ".webm"}

    # Step 1: HEAD request se Content-Disposition check karo
    try:
        async with aiohttp.ClientSession() as _sess:
            async with _sess.head(url, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                cd = resp.headers.get("Content-Disposition", "")
                if cd:
                    m = _re.search(r'filename\*?=["\']?(?:UTF-8\'\')?([^"\';\r\n]+)', cd, _re.IGNORECASE)
                    if m:
                        fname = unquote_plus(m.group(1).strip().strip('"\''))
                        if fname:
                            LOGGER.info(f"[URL] Filename from Content-Disposition: {fname}")
                            return fname
    except Exception as e:
        LOGGER.warning(f"[URL] HEAD request failed for filename detection: {e}")

    # Step 2: URL ke path se basename lo — sirf tab use karo jab valid extension ho
    try:
        path_part = url.split("?")[0]
        basename = unquote_plus(os.path.basename(path_part))
        _, ext = os.path.splitext(basename)
        if ext.lower() in VIDEO_EXTS and len(basename) < 200:
            LOGGER.info(f"[URL] Filename from URL basename: {basename}")
            return basename
    except Exception:
        pass

    # Step 3: Fallback
    LOGGER.info("[URL] Could not detect filename, using 'downloaded_file'")
    return "downloaded_file"


def _safe_filename(name: str, max_len: int = 180) -> str:
    """
    Filename ko safe length tak truncate karo.
    Extension preserve karta hai, naam ke beech se cut karta hai.
    Linux max = 255 bytes. Hum 180 rakhtein hain — path overhead ke liye room.
    """
    # Agar already short hai to kuch mat karo
    if len(name.encode("utf-8")) <= max_len:
        return name

    # Extension alag karo
    base, ext = os.path.splitext(name)
    ext_bytes = len(ext.encode("utf-8"))
    allowed_base = max_len - ext_bytes - 1  # 1 extra safety byte

    # Base ko truncate karo
    base_encoded = base.encode("utf-8")[:allowed_base]
    # Incomplete multi-byte char se bachne ke liye decode with errors='ignore'
    base_truncated = base_encoded.decode("utf-8", errors="ignore")

    safe = base_truncated + ext
    LOGGER.warning(f"[URL] Filename too long, truncated: '{name[:60]}...' -> '{safe}'")
    return safe


async def _download_url(url: str, filename: str, msg: Message, orig_message: Message) -> str:
    """Download from URL with progress."""
    filename = _safe_filename(filename)  # Double safety — koi bhi caller se aaye
    filepath = os.path.join(download_dir, filename)

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
    """Keep only specific audio stream indices."""
    out = _make_output_path(filepath, "_hindiaudio")
    audio_streams = get_audio_streams(filepath)
    audio_abs_indices = [s["index"] for s in audio_streams]

    map_args = ["-map", "0:v?", "-map", "0:s?"]
    for i, abs_idx in enumerate(audio_abs_indices):
        if abs_idx in stream_indices:
            map_args += ["-map", f"0:a:{i}"]

    if len(map_args) == 2:
        LOGGER.warning("No matching audio streams found, keeping all.")
        return None

    map_args += ["-disposition:a:0", "default", "-c", "copy"]
    return await _ffmpeg_process(filepath, out, map_args, msg)


async def _check_all_audio_aac(filepath: str) -> bool:
    """
    Check karo kya file ke saare audio streams already AAC hain.
    True = sab AAC hain (conversion zaruri nahi)
    False = koi non-AAC stream hai (conversion zaruri hai)
    """
    import json, subprocess as _sp
    try:
        cmd = [
            "ffprobe", "-hide_banner", "-print_format", "json",
            "-show_streams", "-select_streams", "a", filepath
        ]
        out = _sp.check_output(cmd, stderr=_sp.DEVNULL)
        streams = json.loads(out.decode()).get("streams", [])
    except Exception:
        return False

    if not streams:
        return True  # Koi audio hi nahi

    for s in streams:
        codec = s.get("codec_name", "").lower()
        if codec not in ("aac", "mp3", "opus"):
            return False  # Koi non-compatible stream mila
    return True


async def _convert_audio_to_aac(filepath: str, msg: Message) -> str | None:
    """
    Saare audio streams ko AAC format mein convert karo.
    - Video aur Subtitles: copy (re-encode nahi)
    - Audio: libfdk_aac (agar available) ya aac codec, 192k, stereo
    - Saare streams aur language metadata preserve hota hai

    Kaun se formats handle hote hain:
      E-AC3 (Dolby Digital Plus), AC-3, DTS, TrueHD, FLAC, Opus, PCM etc.
    Kaun se already pass-through hote hain:
      AAC, MP3 — inhe copy karo (re-encode mat karo)
    """
    import json, subprocess as _sp

    # Audio streams info fetch karo
    try:
        cmd = [
            "ffprobe", "-hide_banner", "-print_format", "json",
            "-show_streams", "-select_streams", "a", filepath
        ]
        out = _sp.check_output(cmd, stderr=_sp.DEVNULL)
        audio_streams = json.loads(out.decode()).get("streams", [])
    except Exception:
        audio_streams = []

    if not audio_streams:
        LOGGER.warning("_convert_audio_to_aac: no audio streams found")
        return None

    out_path = _make_output_path(filepath, "_aac")

    # Build ffmpeg args:
    # -map 0:v? -map 0:a? -map 0:s? -map 0:t?
    # Video/Sub/Attachment: copy
    # Per audio stream: already AAC/MP3 → copy, else → libfdk_aac/aac 192k stereo
    map_args = ["-map", "0:v?", "-map", "0:a?", "-map", "0:s?", "-map", "0:t?"]
    codec_args = ["-c:v", "copy", "-c:s", "copy", "-c:t", "copy"]

    PASS_THROUGH = {"aac", "mp3"}  # Inhe copy karo

    for i, s in enumerate(audio_streams):
        codec = s.get("codec_name", "").lower()
        if codec in PASS_THROUGH:
            codec_args += [f"-c:a:{i}", "copy"]
            LOGGER.info(f"Audio stream {i} ({codec}): copy (already compatible)")
        else:
            # AAC mein convert karo — pehle libfdk_aac try karo (better quality)
            # Fallback: built-in 'aac' codec
            codec_args += [
                f"-c:a:{i}", "aac",
                f"-b:a:{i}", "192k",
                f"-ac:{i}", "2",   # stereo (surround se stereo downmix)
            ]
            LOGGER.info(f"Audio stream {i} ({codec}): converting to AAC 192k stereo")

    return await _ffmpeg_process(filepath, out_path, map_args + codec_args, msg)



    """Legacy wrapper for -vt mode manual metadata."""
    out = _make_output_path(filepath, "_meta")
    meta_args = ["-map", "0", "-c", "copy"]
    if meta.get("show_title"):
        meta_args += ["-metadata", f"title={meta['show_title']}"]
    if meta.get("video_title"):
        meta_args += ["-metadata:s:v:0", f"title={meta['video_title']}"]
    if meta.get("audio_title"):
        meta_args += ["-metadata:s:a", f"title={meta['audio_title']}"]
    return await _ffmpeg_process(filepath, out, meta_args, msg)


async def _apply_full_metadata(filepath: str, meta: dict, msg: Message) -> str | None:
    """
    /setmeta wale full_metadata se apply karo.
    movie_name, video_title, audio_title, subtitle_title, comment, strip_attachments, clear_metadata.
    {audiolang}/{sublang} pehle se resolve ho chuke hain.
    """
    out = _make_output_path(filepath, "_meta")

    if meta.get("strip_attachments"):
        base_map = ["-map", "0:v?", "-map", "0:a?", "-map", "0:s?", "-c", "copy"]
    else:
        base_map = ["-map", "0", "-c", "copy"]

    meta_args = base_map[:]

    if meta.get("clear_metadata"):
        meta_args += ["-map_metadata", "-1"]

    # Global container title (MediaInfo mein "Movie name" ke roop mein dikhta hai)
    if meta.get("movie_name"):
        meta_args += ["-metadata", f"title={meta['movie_name']}"]
    if meta.get("comment"):
        meta_args += ["-metadata", f"comment={meta['comment']}"]
    if meta.get("video_title"):
        meta_args += ["-metadata:s:v:0", f"title={meta['video_title']}"]
    if meta.get("audio_title"):
        meta_args += ["-metadata:s:a", f"title={meta['audio_title']}"]
    if meta.get("subtitle_title"):
        meta_args += ["-metadata:s:s", f"title={meta['subtitle_title']}"]

    return await _ffmpeg_process(filepath, out, meta_args, msg)


async def _do_upload(bot: Client, filepath: str, message: Message, msg: Message, has_eng_sub: bool = False):
    """Upload the processed file to Telegram.
    
    upload_worker filename se caption leta hai, isliye:
    1. smart_caption() se proper caption/filename banao
    2. File ko us naam se rename karo
    3. upload_worker ko renamed filepath pass karo
    """
    import re as _re
    from ..utils.auto_caption import smart_caption
    await msg.edit("<b>📤 Uploading...</b>")
    renamed_path = None
    try:
        # URL uploads mein encoding nahi hoti — actual quality hamesha
        # ffprobe/metadata se detect karo (resolution=None).
        # DB mein saved encode resolution URL uploads pe apply NAHI hona chahiye.
        # Warna agar user ne /setres 480 set kiya hua hai toh 1080p video bhi
        # "480p" caption ke saath upload hogi — jo GALAT hai.
        caption = smart_caption(
            original_caption=os.path.basename(filepath),
            filepath=filepath,
            resolution=None,   # FIX: metadata se actual quality detect karo
            has_eng_sub=has_eng_sub,
        )
        # File ko caption ke naam se rename karo taaki upload_worker sahi naam use kare
        # Filesystem-unsafe characters remove karo
        safe_caption = _re.sub(r'[<>:"/\\|?*]', '', caption).strip()
        renamed_path = os.path.join(os.path.dirname(filepath), safe_caption)
        if renamed_path != filepath:
            os.rename(filepath, renamed_path)
            filepath = renamed_path
        
        link = await upload_worker(filepath, message, msg, resolution=None)
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
