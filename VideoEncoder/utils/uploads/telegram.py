import os
import re
import time

from pyrogram import Client
from pyrogram.enums import ParseMode
from ... import app, download_dir, log, api_id, api_hash
from ..database.access_db import db
from ..auto_caption import smart_caption
from ..display_progress import progress_for_pyrogram
from ..encoding import get_duration, get_thumbnail, get_width_height


# ─────────────────────────────────────────────
#  User session DB helpers
# ─────────────────────────────────────────────
async def _get_user_session(user_id: int):
    """save_restrict mein jo session save hua tha woh laao"""
    user = await db._get_user(user_id)
    return user.get("user_session", None)


async def _make_uploader_client(user_id: int):
    """
    User ka saved session hai toh uska Client banao — nahi hai toh None.
    Caller ko connect() aur disconnect() khud karna hoga.
    """
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
            max_concurrent_transmissions=20,
            workers=32,
            sleep_threshold=60,
        )
        await uc.connect()
        return uc
    except Exception:
        return None


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
#  Chunk size patch
# ─────────────────────────────────────────────
def _patch_chunk_size():
    try:
        import pyrogram.utils as pu
        if hasattr(pu, 'MIN_CHUNK_SIZE'):
            pu.MIN_CHUNK_SIZE = 4 * 1024 * 1024
        if hasattr(pu, 'MAX_CHUNK_SIZE'):
            pu.MAX_CHUNK_SIZE = 4 * 1024 * 1024
    except Exception:
        pass

_patch_chunk_size()


# ─────────────────────────────────────────────
#  upload_to_tg — encode/mega/swift ke baad call
# ─────────────────────────────────────────────
async def upload_to_tg(new_file, message, msg, resolution='480'):
    c_time = time.time()
    filename = os.path.basename(new_file)

    original_caption = message.caption or message.text or os.path.splitext(filename)[0]
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

    # ── User client banao — milta hai toh usse upload hoga ──
    uc = await _make_uploader_client(message.from_user.id)

    try:
        if await db.get_upload_as_doc(message.from_user.id) is True:
            link = await upload_doc(
                message, msg, c_time, bold_caption, new_file, new_filename,
                uploader_client=uc
            )
        else:
            link = await upload_video(
                message, msg, new_file, bold_caption,
                c_time, thumb, duration, width, height, new_filename, custom_thumb,
                uploader_client=uc
            )
    finally:
        # User client disconnect
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

    return link


# ─────────────────────────────────────────────
#  upload_video
#  uploader_client = user account client (fast)
#                  = None means bot (fallback)
# ─────────────────────────────────────────────
async def upload_video(message, msg, new_file, caption, c_time, thumb,
                       duration, width, height, file_name=None, cover=None,
                       uploader_client=None):

    send_kwargs = dict(
        supports_streaming=True,
        parse_mode=ParseMode.HTML,
        caption=caption,
        thumb=thumb,
        duration=duration,
        width=width,
        height=height,
        file_name=file_name,
        progress=progress_for_pyrogram,
        progress_args=("📤 Uploading...", msg, c_time),
    )
    if cover:
        send_kwargs['cover'] = cover

    if uploader_client:
        # ── User account se upload — FAST ──
        resp = await uploader_client.send_video(
            chat_id=message.chat.id,
            video=new_file,
            reply_to_message_id=message.id,
            **send_kwargs,
        )
    else:
        # ── Fallback: bot se upload ──
        resp = await message.reply_video(new_file, **send_kwargs)

    if resp:
        log_kwargs = dict(
            thumb=thumb, caption=caption,
            duration=duration, width=width,
            height=height, parse_mode=ParseMode.HTML,
        )
        if cover:
            log_kwargs['cover'] = cover
        # Log channel mein file_id se bhejo — bandwidth zero
        try:
            await app.send_video(log, resp.video.file_id, **log_kwargs)
        except Exception:
            pass

    return resp.link


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

    if uploader_client:
        # ── User account se upload — FAST ──
        resp = await uploader_client.send_document(
            chat_id=message.chat.id,
            document=new_file,
            reply_to_message_id=message.id,
            **send_kwargs,
        )
    else:
        # ── Fallback: bot se upload ──
        resp = await message.reply_document(new_file, **send_kwargs)

    if resp:
        try:
            await app.send_document(
                log, resp.document.file_id,
                caption=caption, parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

    return resp.link
