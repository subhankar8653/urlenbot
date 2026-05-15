import os
import re
import time

from pyrogram import Client
from pyrogram.enums import ParseMode
from pyrogram.errors import ChannelInvalid, ChannelPrivate, ChatIdInvalid, PeerIdInvalid
from ... import app, download_dir, log, api_id, api_hash
from ..database.access_db import db
from ..auto_caption import smart_caption
from ..display_progress import progress_for_pyrogram
from ..encoding import get_duration, get_thumbnail, get_width_height


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
            max_concurrent_transmissions=20,
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
    uc, message, msg, new_file, send_kwargs, media_type="video"
):
    """
    1. User client → log channel mein upload (user admin hai)
    2. Bot → log channel se target chat mein forward_messages
    3. No cleanup needed — log channel mein rehna chahiye file
    """
    # Strip progress & reply_to from kwargs — log channel ko nahi chahiye
    safe_kwargs = {k: v for k, v in send_kwargs.items()
                   if k not in ("reply_to_message_id", "progress", "progress_args")}

    try:
        # ── Step 1: User client se LOG_CHANNEL mein upload ──
        if media_type == "video":
            saved = await uc.send_video(
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

        # ── Step 2: Bot se LOG_CHANNEL → target chat forward ──
        resp = await app.forward_messages(
            chat_id=message.chat.id,
            from_chat_id=log,
            message_ids=saved.id,
        )

        # Log channel mein message already hai — alag se send karne ki zaroorat nahi
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
            link = await upload_video(
                message, msg, new_file, bold_caption,
                c_time, thumb, duration, width, height, new_filename, custom_thumb,
                uploader_client=uc
            )
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

    return link


# ─────────────────────────────────────────────
#  upload_video
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

    resp = None

    if uploader_client:
        try:
            # User → log channel upload, bot → log channel se forward
            resp = await _upload_via_user_then_forward(
                uploader_client, message, msg, new_file, send_kwargs, media_type="video"
            )
        except Exception:
            resp = None

    if resp is None:
        # Bot fallback — directly upload
        resp = await message.reply_video(new_file, **send_kwargs)

        # Log channel mein bhi bhejo (bot fallback case mein, cover bhi include)
        if resp:
            log_kwargs = dict(
                thumb=thumb, caption=caption,
                duration=duration, width=width,
                height=height, parse_mode=ParseMode.HTML,
            )
            if cover:
                log_kwargs['cover'] = cover
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
