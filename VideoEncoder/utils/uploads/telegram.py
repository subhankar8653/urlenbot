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
#  Strategy: user client se "Saved Messages" mein
#  upload karo (always works), phir bot se
#  us file_id ko group mein forward karo.
#  Isse user bandwidth use hoti hai (fast upload)
#  aur CHANNEL_INVALID error bhi nahi aata.
# ─────────────────────────────────────────────
async def _upload_via_user_then_forward(
    uc, message, msg, new_file, send_kwargs, media_type="video"
):
    """
    1. User client se apne Saved Messages (self) mein upload karo
    2. Bot se woh message group mein forward karo
    3. Saved Messages wala message delete karo (cleanup)
    """
    try:
        # Step 1: User ke "me" (Saved Messages) mein upload
        if media_type == "video":
            saved = await uc.send_video(
                chat_id="me",
                video=new_file,
                **{k: v for k, v in send_kwargs.items()
                   if k not in ("reply_to_message_id",)},
            )
        else:
            saved = await uc.send_document(
                chat_id="me",
                document=new_file,
                **{k: v for k, v in send_kwargs.items()
                   if k not in ("reply_to_message_id",)},
            )

        # Step 2: Bot se group mein forward karo (file_id se — instant, no bandwidth)
        if media_type == "video" and saved.video:
            resp = await app.send_video(
                chat_id=message.chat.id,
                video=saved.video.file_id,
                caption=send_kwargs.get("caption", ""),
                duration=send_kwargs.get("duration"),
                width=send_kwargs.get("width"),
                height=send_kwargs.get("height"),
                thumb=send_kwargs.get("thumb"),
                supports_streaming=True,
                parse_mode=ParseMode.HTML,
                reply_to_message_id=message.id,
            )
        else:
            resp = await app.send_document(
                chat_id=message.chat.id,
                document=saved.document.file_id,
                caption=send_kwargs.get("caption", ""),
                file_name=send_kwargs.get("file_name"),
                parse_mode=ParseMode.HTML,
                reply_to_message_id=message.id,
            )

        # Step 3: Saved Messages se delete karo
        try:
            await uc.delete_messages("me", saved.id)
        except Exception:
            pass

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
#  upload_to_tg
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
            # User se "Saved Messages" mein upload → bot se group mein forward
            resp = await _upload_via_user_then_forward(
                uploader_client, message, msg, new_file, send_kwargs, media_type="video"
            )
        except Exception as e:
            # Koi bhi error aaye — bot se fallback
            resp = None

    if resp is None:
        # Bot fallback
        resp = await message.reply_video(new_file, **send_kwargs)

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
        # Bot fallback
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
