import os
import re
import time

from pyrogram.enums import ParseMode
from ... import app, download_dir, log
from ..database.access_db import db
from ..auto_caption import smart_caption
from ..display_progress import progress_for_pyrogram
from ..encoding import get_duration, get_thumbnail, get_width_height


def build_caption(original_caption, filename, resolution):
    """Smart caption - quality replace karo, extension fix karo"""
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
    """
    Caption se filename banao.
    Spaces aur [] brackets rakho - sirf illegal chars hatao.
    Example: 'Naruto S01E01 in Hindi 1080p [@SBANIME].mp4'
             → 'Naruto S01E01 in Hindi 1080p [@SBANIME].mp4'  (same, clean)
    """
    name = caption
    name = re.sub(r'[<>:"/\\|?*]', '', name)
    name = name.strip()
    if not name.endswith('.mp4'):
        name = os.path.splitext(name)[0] + '.mp4'
    return name


async def apply_swap(caption, user_id):
    """User-defined swap rules apply karo"""
    swap_rules = await db.get_swap(user_id)
    if not swap_rules:
        return caption
    for old, new in swap_rules.items():
        caption = caption.replace(old, new)
    return caption


async def upload_to_tg(new_file, message, msg, resolution='480'):
    c_time = time.time()
    filename = os.path.basename(new_file)

    original_caption = message.caption or message.text or os.path.splitext(filename)[0]
    caption = smart_caption(original_caption, new_file, resolution)
    caption = await apply_swap(caption, message.from_user.id)
    bold_caption = f'<b>{caption}</b>'

    # Clean filename - spaces aur [] sab sahi rahega
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

    if await db.get_upload_as_doc(message.from_user.id) is True:
        link = await upload_doc(message, msg, c_time, bold_caption, new_file, new_filename)
    else:
        link = await upload_video(
            message, msg, new_file, bold_caption,
            c_time, thumb, duration, width, height, new_filename, custom_thumb
        )

    if custom_thumb and thumb and os.path.isfile(thumb):
        try:
            os.remove(thumb)
        except Exception:
            pass

    return link


async def upload_video(message, msg, new_file, caption, c_time, thumb, duration, width, height, file_name=None, cover=None):
    send_kwargs = dict(
        supports_streaming=True,
        parse_mode=ParseMode.HTML,
        caption=caption,
        thumb=thumb,
        duration=duration,
        width=width,
        height=height,
        # file_name explicitly pass karo - Telegram will use this as-is
        # Spaces aur [] brackets preserve honge
        file_name=file_name,
        progress=progress_for_pyrogram,
        progress_args=("Uploading ...", msg, c_time)
    )
    if cover:
        send_kwargs['cover'] = cover

    resp = await message.reply_video(new_file, **send_kwargs)

    if resp:
        log_kwargs = dict(
            thumb=thumb, caption=caption,
            duration=duration, width=width,
            height=height, parse_mode=ParseMode.HTML
        )
        if cover:
            log_kwargs['cover'] = cover
        await app.send_video(log, resp.video.file_id, **log_kwargs)

    return resp.link


async def upload_doc(message, msg, c_time, caption, new_file, file_name=None):
    resp = await message.reply_document(
        new_file,
        file_name=file_name,
        caption=caption,
        parse_mode=ParseMode.HTML,
        progress=progress_for_pyrogram,
        progress_args=("Uploading ...", msg, c_time)
    )
    if resp:
        await app.send_document(
            log, resp.document.file_id,
            caption=caption, parse_mode=ParseMode.HTML
        )
    return resp.link
