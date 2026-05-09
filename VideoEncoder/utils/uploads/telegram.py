

import os
import re
import time

from pyrogram.enums import ParseMode
from ... import app, download_dir, log
from ..database.access_db import db
from ..display_progress import progress_for_pyrogram
from ..encoding import get_duration, get_thumbnail, get_width_height


def build_caption(original_caption, new_filename, resolution):
    """Build smart caption - replace quality tag and apply swap rules"""
    if not original_caption:
        return new_filename

    caption = original_caption

    # Step 1: Replace resolution in caption
    res_map = {
        '2160': '2160p', '1080': '1080p', '720': '720p',
        '576': '576p', '480': '480p', 'OG': None
    }
    quality_patterns = [
        r'2160p', r'1080p', r'720p', r'576p', r'480p',
        r'4K', r'FHD', r'HD', r'SD'
    ]
    new_res = res_map.get(resolution, None)
    if new_res:
        replaced = False
        for pat in quality_patterns:
            if re.search(pat, caption, re.IGNORECASE):
                caption = re.sub(pat, new_res, caption, flags=re.IGNORECASE, count=1)
                replaced = True
                break
        if not replaced:
            # No quality tag found, just use new filename
            pass

    # Step 2: Replace file extension
    caption = re.sub(r'\.mkv$|\.avi$|\.mov$|\.flv$|\.wmv$', '.mp4', caption, flags=re.IGNORECASE)

    # Step 3: Remove source tags like HEVC, x265, 10bit etc (optional cleanup)
    # caption = re.sub(r'HEVC|x265|x264|10bit|8bit', '', caption, flags=re.IGNORECASE).strip()

    return caption.strip()


async def apply_swap(caption, user_id):
    """Apply user-defined swap rules to caption"""
    swap_rules = await db.get_swap(user_id)
    if not swap_rules:
        return caption
    for old, new in swap_rules.items():
        caption = caption.replace(old, new)
    return caption


async def upload_to_tg(new_file, message, msg, resolution='480'):
    # Variables
    c_time = time.time()
    filename = os.path.basename(new_file)

    # Build smart caption
    original_caption = message.caption or message.text or filename
    caption = build_caption(original_caption, filename, resolution)
    caption = await apply_swap(caption, message.from_user.id)
    caption = f'<b>{caption}</b>'

    duration = get_duration(new_file)

    # Thumbnail Logic
    custom_thumb = await db.get_thumbnail(message.from_user.id)
    if custom_thumb:
        thumb = await app.download_media(custom_thumb, file_name=os.path.join(download_dir, str(time.time()) + ".jpg"))
    else:
        thumb = get_thumbnail(new_file, download_dir, duration / 4)

    width, height = get_width_height(new_file)
    # Handle Upload
    if await db.get_upload_as_doc(message.from_user.id) is True:
        link = await upload_doc(message, msg, c_time, caption, new_file)
    else:
        link = await upload_video(message, msg, new_file, caption,
                                  c_time, thumb, duration, width, height)

    # Cleanup custom thumb
    if custom_thumb and thumb and os.path.isfile(thumb):
        try:
            os.remove(thumb)
        except Exception:
            pass

    return link


async def upload_video(message, msg, new_file, caption, c_time, thumb, duration, width, height):
    resp = await message.reply_video(
        new_file,
        supports_streaming=True,
        parse_mode=ParseMode.HTML,
        caption=caption,
        thumb=thumb,
        duration=duration,
        width=width,
        height=height,
        progress=progress_for_pyrogram,
        progress_args=("Uploading ...", msg, c_time)
    )
    if resp:
        await app.send_video(log, resp.video.file_id, thumb=thumb,
                             caption=caption, duration=duration,
                             width=width, height=height, parse_mode=ParseMode.HTML)

    return resp.link


async def upload_doc(message, msg, c_time, caption, new_file):
    resp = await message.reply_document(
        new_file,
        parse_mode=ParseMode.HTML,
        caption=caption,
        progress=progress_for_pyrogram,
        progress_args=("Uploading ...", msg, c_time)
    )

    if resp:
        await app.send_document(log, resp.document.file_id, caption=caption, parse_mode=ParseMode.HTML)

    return resp.link
