import asyncio
import os
import re
import subprocess
import time

from pyrogram import Client
from pyrogram.enums import ParseMode
from pyrogram.errors import ChannelInvalid, ChannelPrivate, ChatIdInvalid, PeerIdInvalid
from ... import app, download_dir, log, api_id, api_hash, LOGGER
from ..database.access_db import db
from ..auto_caption import smart_caption
from ..display_progress import progress_for_pyrogram
from ..encoding import get_duration, get_thumbnail, get_width_height


# ─────────────────────────────────────────────
#  cover-safe send wrapper
#  ───────────────────────
#  Kuch kurigram/pyrogram versions (e.g. kurigram==2.2.22) ke
#  send_video()/reply_video() mein 'cover' kwarg support nahi hota
#  (TypeError: got an unexpected keyword argument 'cover').
#  Yeh wrapper pehle 'cover' ke saath try karta hai, aur agar wahi
#  specific TypeError aaye to automatically 'cover' hata ke retry
#  karta hai — kabhi bhi crash nahi hoga, chahe library 'cover'
#  support kare ya na kare.
# ─────────────────────────────────────────────
async def _send_video_cover_safe(send_fn, **kwargs):
    # kurigram 2.2.22 ke send_video/reply_video mein 'file_name' aur 'cover'
    # kwargs support nahi hote — pehle with all kwargs try karo,
    # TypeError aane pe sirf wahi kwarg strip karo jo error mein mention hai.
    _maybe_unsupported = ("file_name", "cover")
    try:
        return await send_fn(**kwargs)
    except TypeError as e:
        err_str = str(e)
        stripped_any = False
        for kw in _maybe_unsupported:
            if kw in kwargs and kw in err_str:
                LOGGER.warning(f"[Upload] '{kw}' kwarg unsupported, hata ke retry: {e}")
                kwargs.pop(kw)
                stripped_any = True
        if stripped_any:
            try:
                return await send_fn(**kwargs)
            except TypeError as e2:
                # Ek aur pass — koi aur kwarg bhi unsupported ho sakta hai
                err_str2 = str(e2)
                for kw in _maybe_unsupported:
                    if kw in kwargs and kw in err_str2:
                        kwargs.pop(kw)
                return await send_fn(**kwargs)
        raise


# ─────────────────────────────────────────────
#  User session DB helpers
# ─────────────────────────────────────────────
async def _get_user_session(user_id: int):
    user = await db._get_user(user_id)
    return user.get("user_session", None)


async def _make_uploader_client(user_id: int):
    session_str = await _get_user_session(user_id)
    if not session_str:
        return None
    try:
        uc = Client(
            "uploader_user",
            session_string=session_str,
            api_id=api_id,
            api_hash=api_hash,
            in_memory=True,
            max_concurrent_transmissions=4,  # FIX: 20 → 4 (app client mein yeh already fix tha, yahan reh gaya tha — isi se deadlock + pyrogram_patch.py ki zaroorat padi thi)
            workers=32,
            sleep_threshold=60,
        )
        await uc.connect()
        return uc
    except Exception:
        return None


# ─────────────────────────────────────────────
#  User client se upload karo —
#  Strategy:
#    1. User client → LOG_CHANNEL mein upload karo
#       (user already admin hai wahan)
#    2. Bot → LOG_CHANNEL se target chat mein forward karo
#       (instant, no re-upload, no bandwidth)
#    3. LOG_CHANNEL ka message rehne do — wahi log bhi hai!
#       (alag se log send karne ki zaroorat nahi)
# ─────────────────────────────────────────────
async def _upload_via_user_then_forward(
    uc, message, msg, new_file, send_kwargs, media_type="video",
    skip_forward=False
):
    """
    1. User client → log channel mein upload (user admin hai)
    2. Bot → log channel se target chat mein forward_messages
       (skip_forward=True hoga to forward skip hoga — bot_mode ke liye)
    3. No cleanup needed — log channel mein rehna chahiye file
    """
    # Strip progress, reply_to aur file_name (kurigram unsupported) — cover RAKHNA hai!
    # cover = Telegram video player background image, forward mein preserve nahi hoti
    # isliye user client upload mein cover pass karna zaroori hai
    _strip_keys = ("reply_to_message_id", "progress", "progress_args", "file_name")
    safe_kwargs = {k: v for k, v in send_kwargs.items() if k not in _strip_keys}
    try:
        # ── Step 1: User client se LOG_CHANNEL mein upload ──
        if media_type == "video":
            saved = await _send_video_cover_safe(
                uc.send_video,
                chat_id=log,
                video=new_file,
                progress=send_kwargs.get("progress"),
                progress_args=send_kwargs.get("progress_args"),
                **safe_kwargs,
            )
        else:
            saved = await uc.send_document(
                chat_id=log,
                document=new_file,
                progress=send_kwargs.get("progress"),
                progress_args=send_kwargs.get("progress_args"),
                **safe_kwargs,
            )

        # ── Step 2: Bot se LOG_CHANNEL → target chat copy ──
        # forward_messages nahi — cover metadata forward mein preserve nahi hoti!
        # copy_message use karo — cover bhi sahi se copy hoti hai
        if skip_forward:
            return saved  # log channel ka message return karo (link ke liye)

        resp = await app.copy_message(
            chat_id=message.chat.id,
            from_chat_id=log,
            message_id=saved.id,
        )

        # copy_message single Message object return karta hai (list nahi)
        return resp

    except Exception as e:
        raise e


# ─────────────────────────────────────────────
#  Caption helpers
# ─────────────────────────────────────────────
def build_caption(original_caption, filename, resolution):
    if not original_caption:
        return os.path.splitext(filename)[0] + ".mp4"
    caption = original_caption
    res_label = {
        'OG': None, '2160': '2160p', '1080': '1080p',
        '720': '720p', '576': '576p', '480': '480p'
    }.get(str(resolution), '480p')
    if res_label:
        for pat in ['2160p', '1080p', '720p', '576p', '480p', '4K', 'FHD', 'HD']:
            if re.search(pat, caption, re.IGNORECASE):
                caption = re.sub(pat, res_label, caption, count=1, flags=re.IGNORECASE)
                break
    caption = re.sub(r'\.(mkv|avi|mov|flv|wmv|ts|m4v)$', '.mp4', caption, flags=re.IGNORECASE)
    return caption.strip()


def build_filename(caption):
    name = re.sub(r'[<>:"/\\|?*]', '', caption).strip()
    if not name.endswith('.mp4'):
        name = os.path.splitext(name)[0] + '.mp4'
    return name


async def apply_swap(caption, user_id):
    swap_rules = await db.get_swap(user_id)
    if not swap_rules:
        return caption
    for old, new in swap_rules.items():
        caption = caption.replace(old, new)
    return caption


# ─────────────────────────────────────────────
#  upload_to_tg
# ─────────────────────────────────────────────
async def upload_to_tg(new_file, message, msg, resolution='480'):
    c_time = time.time()
    filename = os.path.basename(new_file)

    # /url, /ddl, /dl jaisi commands caption mein nahi aani chahiye
    # Agar message.text mein command hai toh sirf filename use karo
    raw_text = message.caption or message.text or ''
    if raw_text.strip().startswith('/'):
        # Command message hai — filename se caption banao
        original_caption = os.path.splitext(filename)[0]
    else:
        original_caption = raw_text or os.path.splitext(filename)[0]

    caption = smart_caption(original_caption, new_file, resolution)
    caption = await apply_swap(caption, message.from_user.id)
    bold_caption = f'<b>{caption}</b>'

    new_filename = build_filename(caption)
    new_path = os.path.join(os.path.dirname(new_file), new_filename)
    try:
        if new_file != new_path:
            os.rename(new_file, new_path)
            new_file = new_path
    except Exception:
        pass

    duration = get_duration(new_file)

    custom_thumb = await db.get_thumbnail(message.from_user.id)
    if custom_thumb:
        thumb = await app.download_media(
            custom_thumb,
            file_name=os.path.join(download_dir, str(time.time()) + ".jpg")
        )
    else:
        thumb = get_thumbnail(new_file, download_dir, duration / 4)

    width, height = get_width_height(new_file)

    uc = await _make_uploader_client(message.from_user.id)

    try:
        if await db.get_upload_as_doc(message.from_user.id) is True:
            link = await upload_doc(
                message, msg, c_time, bold_caption, new_file, new_filename,
                uploader_client=uc
            )
        else:
            # cover = custom_thumb FILE_ID (DB se mila Telegram file_id) —
            # Local path nahi, file_id chahiye cover ke liye (aiogram bot ki tarah)
            # thumb (local path) = gallery preview; cover (file_id) = video player background
            _cover = custom_thumb if custom_thumb else None
            _resp = await upload_video(
                message, msg, new_file, bold_caption,
                c_time, thumb, duration, width, height,
                file_name=new_filename,
                cover=_cover,
                uploader_client=uc
            )
            link = _resp.link if _resp else None
    finally:
        if uc:
            try:
                await uc.disconnect()
            except Exception:
                pass

    if custom_thumb and thumb and os.path.isfile(thumb):
        try:
            os.remove(thumb)
        except Exception:
            pass

    # ── Auto Channel Upload ──
    # User ke linked channels check karo aur file wahan bhi bhejo
    if link:
        try:
            await _upload_to_user_channels(
                user_id=message.from_user.id,
                new_file=new_file,
                caption=bold_caption,
                user_message=link,
                duration=duration,
                width=width,
                height=height,
                filename=new_filename,
            )
        except Exception as e:
            # Channel upload fail ho toh main upload ko affect na kare
            print(f"[Channel Upload Error] {e}")

    return link


# ─────────────────────────────────────────────
#  upload_video
# ─────────────────────────────────────────────
async def upload_video(message, msg, new_file, caption, c_time, thumb,
                       duration, width, height, file_name=None, cover=None,
                       uploader_client=None, progress=None, progress_args=None,
                       skip_forward=False):
    # progress/progress_args override — caller custom progress callback de sakta hai
    # (e.g. swift_downloader 50% staggered upload ke liye)
    _progress_fn   = progress      if progress      is not None else progress_for_pyrogram
    _progress_args = progress_args if progress_args is not None else ("📤 Uploading...", msg, c_time)

    send_kwargs = dict(
        supports_streaming=True,
        parse_mode=ParseMode.HTML,
        caption=caption,
        thumb=thumb,
        duration=duration,
        width=width,
        height=height,
        file_name=file_name,
        progress=_progress_fn,
        progress_args=_progress_args,
    )
    if cover:
        send_kwargs['cover'] = cover

    resp = None

    if uploader_client:
        try:
            # User → log channel upload, bot → log channel se forward
            resp = await _upload_via_user_then_forward(
                uploader_client, message, msg, new_file, send_kwargs, media_type="video",
                skip_forward=skip_forward
            )
        except Exception:
            resp = None

    if resp is None:
        if skip_forward:
            # Bot mode fallback — seedha log channel pe upload karo
            log_kwargs = {k: v for k, v in send_kwargs.items()
                          if k not in ("progress", "progress_args")}
            resp = await _send_video_cover_safe(app.send_video, chat_id=log, video=new_file, **log_kwargs)
        else:
            # Normal fallback — target chat pe upload
            resp = await _send_video_cover_safe(message.reply_video, video=new_file, **send_kwargs)

            # Log channel mein bhi bhejo (bot fallback case mein)
            if resp:
                log_kwargs = dict(
                    thumb=thumb, caption=caption,
                    duration=duration, width=width,
                    height=height, parse_mode=ParseMode.HTML,
                )
                if cover:
                    log_kwargs['cover'] = cover
                try:
                    await _send_video_cover_safe(app.send_video, chat_id=log, video=resp.video.file_id, **log_kwargs)
                except Exception:
                    pass

    return resp  # Message object return karo (swift ke liye .id chahiye)


# ─────────────────────────────────────────────
#  upload_doc
# ─────────────────────────────────────────────
async def upload_doc(message, msg, c_time, caption, new_file, file_name=None,
                     uploader_client=None):

    send_kwargs = dict(
        file_name=file_name,
        caption=caption,
        parse_mode=ParseMode.HTML,
        progress=progress_for_pyrogram,
        progress_args=("📤 Uploading...", msg, c_time),
    )

    resp = None

    if uploader_client:
        try:
            resp = await _upload_via_user_then_forward(
                uploader_client, message, msg, new_file, send_kwargs, media_type="doc"
            )
        except Exception:
            resp = None

    if resp is None:
        # Bot fallback — directly upload
        resp = await message.reply_document(new_file, **send_kwargs)

        # Log channel mein bhi bhejo (bot fallback case mein)
        if resp:
            try:
                await app.send_document(
                    log, resp.document.file_id,
                    caption=caption, parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass

    return resp.link


# ─────────────────────────────────────────────
#  Audio Filter using FFmpeg
#  Input: file path + "Hindi" ya "Hindi+Tamil" ya "All"
#  Output: filtered file path (naya file)
# ─────────────────────────────────────────────
async def _filter_audio_tracks(input_path: str, languages: str, output_dir: str) -> str:
    """
    Specified language tracks hi rakhta hai, baaki remove karta hai.
    languages = "Hindi" / "Hindi+Tamil" / "All"
    Returns: output file path (agar filter hua) ya original path (agar All/fail)
    """
    if not languages or languages.strip().lower() == "all":
        return input_path

    # Language name → ISO 639-2 code mapping
    lang_to_iso = {
        'hindi': 'hin', 'english': 'eng', 'tamil': 'tam',
        'telugu': 'tel', 'japanese': 'jpn', 'korean': 'kor',
        'chinese': 'chi', 'bengali': 'ben', 'marathi': 'mar',
        # Direct ISO codes bhi accept karo
        'hin': 'hin', 'eng': 'eng', 'tam': 'tam', 'tel': 'tel',
        'jpn': 'jpn', 'kor': 'kor', 'chi': 'chi', 'ben': 'ben',
    }

    iso_to_name = {v: k.capitalize() for k, v in lang_to_iso.items()}

    # Requested languages parse karo
    req_langs = [l.strip().lower() for l in languages.replace('+', ',').split(',')]
    req_iso = set()
    for r in req_langs:
        if r in lang_to_iso:
            req_iso.add(lang_to_iso[r])

    if not req_iso:
        return input_path

    try:
        # FFprobe se audio streams lo
        result = subprocess.run(
            ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_streams', input_path],
            capture_output=True, text=True, timeout=30
        )
        import json
        data_j = json.loads(result.stdout)

        audio_tracks = []
        for stream in data_j.get('streams', []):
            if stream.get('codec_type') == 'audio':
                tags = stream.get('tags', {})
                lang_code = tags.get('language', tags.get('LANGUAGE', 'und')).lower().strip()
                audio_tracks.append({
                    'index': stream['index'],
                    'lang': lang_code,
                })

        if not audio_tracks:
            return input_path

        # Match karo
        keep_indices = [t['index'] for t in audio_tracks if t['lang'] in req_iso]

        if not keep_indices:
            # Koi match nahi mila — pehla track rakho
            keep_indices = [audio_tracks[0]['index']]

        if len(keep_indices) >= len(audio_tracks):
            # Sab tracks match kiye — filter karne ki zaroorat nahi
            return input_path

        # FFmpeg command banao
        base = os.path.splitext(os.path.basename(input_path))[0]
        output_path = os.path.join(output_dir, f"ch_filtered_{int(time.time())}_{base}.mkv")

        cmd = ['ffmpeg', '-y', '-i', input_path, '-map', '0:v:0']
        for idx in keep_indices:
            cmd.extend(['-map', f'0:{idx}'])
        cmd.extend(['-map', '0:s?', '-c', 'copy', output_path])

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE
        )
        await proc.communicate()

        if os.path.exists(output_path) and os.path.getsize(output_path) > 1024 * 1024:
            return output_path

        return input_path

    except Exception as e:
        print(f"[Audio Filter Error] {e}")
        return input_path


# ─────────────────────────────────────────────
#  Auto Channel Upload
#  User ke saare linked channels mein file bhejo
# ─────────────────────────────────────────────
async def _upload_to_user_channels(
    user_id: int,
    new_file: str,
    caption: str,
    user_message,          # upload_to_tg ka return value (message link string)
    duration: int,
    width: int,
    height: int,
    filename: str,
):
    """
    User ke linked channels mein file upload karo.
    - No audio filter → Direct forward (fast)
    - Audio filter hai → FFmpeg filter → Re-upload
    """
    channels = await db.get_channels(user_id)
    if not channels:
        return

    # File abhi disk pe honi chahiye
    if not os.path.isfile(new_file):
        print("[Channel Upload] File not found on disk, skipping.")
        return

    for ch in channels:
        channel_id = ch.get('channel_id')
        languages = ch.get('languages', 'All')
        channel_title = ch.get('channel_title', str(channel_id))

        if not channel_id:
            continue

        try:
            # Custom thumbnail check karo
            custom_thumb = await db.get_thumbnail(user_id)
            thumb = None
            if custom_thumb:
                thumb = await app.download_media(
                    custom_thumb,
                    file_name=os.path.join(download_dir, f"ch_thumb_{int(time.time())}.jpg")
                )
            else:
                thumb = get_thumbnail(new_file, download_dir, duration / 4)

            needs_filter = languages.strip().lower() != 'all'

            if needs_filter:
                # ── Audio filter lagao aur direct upload karo ──
                print(f"[Channel Upload] Filtering audio ({languages}) for {channel_title}")
                filtered_file = await _filter_audio_tracks(
                    new_file, languages, os.path.dirname(new_file)
                )

                await app.send_video(
                    chat_id=channel_id,
                    video=filtered_file,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    duration=duration,
                    width=width,
                    height=height,
                    thumb=thumb,
                    supports_streaming=True,
                )

                # Filtered file cleanup karo (original nahi)
                if filtered_file != new_file and os.path.isfile(filtered_file):
                    try:
                        os.remove(filtered_file)
                    except Exception:
                        pass

            else:
                # ── No filter → Log channel se forward karo (fast) ──
                print(f"[Channel Upload] Forwarding to {channel_title}")
                try:
                    # Log channel mein message dhundho aur forward karo
                    # Pehle direct upload try karo (safe method)
                    await app.send_video(
                        chat_id=channel_id,
                        video=new_file,
                        caption=caption,
                        parse_mode=ParseMode.HTML,
                        duration=duration,
                        width=width,
                        height=height,
                        thumb=thumb,
                        supports_streaming=True,
                    )
                except Exception as e:
                    print(f"[Channel Upload] Direct upload failed for {channel_title}: {e}")

            # Thumb cleanup
            if thumb and os.path.isfile(thumb) and 'ch_thumb_' in thumb:
                try:
                    os.remove(thumb)
                except Exception:
                    pass

            print(f"[Channel Upload] ✅ Done: {channel_title}")
            await asyncio.sleep(2)  # Flood wait se bachne ke liye

        except (ChannelInvalid, ChannelPrivate, ChatIdInvalid, PeerIdInvalid) as e:
            print(f"[Channel Upload] ❌ Invalid channel {channel_title}: {e}")
        except Exception as e:
            print(f"[Channel Upload] ❌ Error for {channel_title}: {e}")
