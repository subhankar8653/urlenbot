import asyncio
import html
import os
import time
from datetime import datetime
from urllib.parse import unquote_plus

from pyrogram.errors.exceptions.bad_request_400 import (MessageIdInvalid, MessageNotModified)
from pyrogram.types import Message

from .. import LOGGER, data, download_dir, video_mimetype
from .database.access_db import db
from .direct_link_generator import direct_link_generator
from .display_progress import progress_for_pyrogram
from .helper import delete_downloads, get_zip_folder, handle_encode, handle_extract, handle_url
from .uploads.drive import _get_file_id
from .uploads.drive.download import Downloader
from .encoding import get_media_streams
from ..video_utils.audio_selector import AudioSelect
from .fast_download import fast_download


async def on_task_complete():
    delete_downloads()
    if not data:
        return
    del data[0]
    if not len(data) > 0:
        return
    message = data[0]

    text_content = message.text or message.caption

    if text_content:
        text = text_content.split(None, 1)
        command = text[0].lower()
        if '/ddl' in command:
            await handle_tasks(message, 'url')
        elif '/batch' in command:
            await handle_tasks(message, 'batch')
        elif '/dl' in command:
            await handle_tasks(message, 'tg')
        elif '/af' in command:
            await handle_tasks(message, 'af')
        else:
            if message.document or message.video:
                if message.document and not message.document.mime_type in video_mimetype:
                    await on_task_complete()
                    return
                await handle_tasks(message, 'tg')
    else:
        if message.document:
            if not message.document.mime_type in video_mimetype:
                await on_task_complete()
                return
        await handle_tasks(message, 'tg')


async def handle_tasks(message, mode):
    try:
        msg = await message.reply_text("<b>💠 Downloading...</b>")
        if mode == 'tg':
            await tg_task(message, msg)
        elif mode == 'url':
            await url_task(message, msg)
        elif mode == 'af':
            await af_task(message, msg)
        else:
            await batch_task(message, msg)
    except MessageNotModified:
        pass
    except IndexError:
        return
    except MessageIdInvalid:
        await msg.edit('Download Cancelled!')
    except FileNotFoundError:
        LOGGER.error('[FileNotFoundError]: Maybe due to cancel')
        import traceback
        LOGGER.error(traceback.format_exc())
    except Exception as e:
        import traceback
        LOGGER.error(traceback.format_exc())
        await message.reply(text=f"Error! <code>{e}</code>")
    finally:
        await on_task_complete()


async def tg_task(message, msg):
    filepath = await handle_tg_down(message, msg)
    if not filepath:
        await msg.edit("Download failed or no file found.")
        return
    await msg.edit('Encoding...')
    await handle_encode(filepath, message, msg)


async def af_task(message, msg):
    filepath = await handle_tg_down(message, msg)
    if not filepath:
        await msg.edit("Download failed or no file found.")
        return

    streams = get_media_streams(filepath)
    if not streams:
        await msg.edit("Could not retrieve media streams.")
        return

    selector = AudioSelect(message._client, message)
    await msg.delete()
    audio_map, _ = await selector.get_buttons(streams)

    if audio_map == -1:
        return

    msg = await message.reply("Encoding with new audio arrangement...")
    await handle_encode(filepath, message, msg, audio_map)


async def url_task(message, msg):
    filepath = await handle_download_url(message, msg, False)
    if not filepath:
        return
    await msg.edit_text("Encoding...")
    await handle_encode(filepath, message, msg)


async def batch_task(message, msg):
    if message.reply_to_message:
        filepath = await handle_tg_down(message, msg, mode='reply')
    else:
        filepath = await handle_download_url(message, msg, True)
    if not filepath:
        await msg.edit('NO ZIP FOUND!')
    if os.path.isfile(filepath):
        path = await get_zip_folder(filepath)
        await handle_extract(filepath)
        if not os.path.isdir(path):
            await msg.edit('extract failed!')
            return
        filepath = path
    if os.path.isdir(filepath):
        path = filepath
    else:
        await msg.edit('Something went wrong!')
        return
    await msg.edit('<b>📕 Encode Started!</b>')
    sentfiles = []
    for dirpath, subdir, files_ in sorted(os.walk(path)):
        for i in sorted(files_):
            msg_ = await message.reply('Encoding')
            filepath = os.path.join(dirpath, i)
            await msg.edit('Encoding: <code>{}</code>'.format(i))
            try:
                url = await handle_encode(filepath, message, msg_)
            except Exception as e:
                await msg_.edit(str(e) + '\n\n Continuing...')
                continue
            else:
                sentfiles.append((i, url))
    text = '✨ <b>#EncodedFiles:</b> \n\n'
    quote = None
    first_index = None
    all_amount = 1
    for filename, filelink in sentfiles:
        if filelink:
            atext = f'- <a href="{filelink}">{html.escape(filename)}</a>'
        else:
            atext = f'- {html.escape(filename)} (empty)'
        atext += '\n'
        futtext = text + atext
        if all_amount > 100:
            thing = await message.reply_text(text, quote=quote, disable_web_page_preview=True)
            if first_index is None:
                first_index = thing
            quote = False
            futtext = atext
            all_amount = 1
            await asyncio.sleep(3)
        all_amount += 1
        text = futtext
    if not sentfiles:
        text = 'Files: None'
    thing = await message.reply_text(text, quote=quote, disable_web_page_preview=True)
    if first_index is None:
        first_index = thing
    await msg.edit('Encoded Files! Links: {}'.format(first_index.link), disable_web_page_preview=True)


async def handle_download_url(message, msg, batch):
    url = message.text.split(None, 1)[1].strip()
    if 'drive.google.com' in url:
        file_id = _get_file_id(url)
        n = Downloader()
        custom_file_name = n.name(file_id)
    else:
        custom_file_name = unquote_plus(os.path.basename(url))

    if "|" in url and not batch:
        url, c_file_name = url.split("|", maxsplit=1)
        url = url.strip()
        if c_file_name:
            custom_file_name = c_file_name.strip()
    elif " " in url and not batch:
        parts = url.split()
        if len(parts) > 1:
            url = parts[0]
            custom_file_name = " ".join(parts[1:])

    direct = direct_link_generator(url)
    if direct:
        url = direct

    if not custom_file_name:
        custom_file_name = "downloaded_file"

    path = os.path.join(download_dir, custom_file_name)
    filepath = path
    if 'drive.google.com' in url:
        await n.handle_drive(msg, url, custom_file_name, batch)
    else:
        await handle_url(url, filepath, msg)
    return filepath


async def handle_tg_down(message, msg, mode='no_reply'):
    """Multi-threaded fast download"""
    c_time = time.time()

    # Target message determine karo
    target_msg = message
    if message.reply_to_message and (message.reply_to_message.video or message.reply_to_message.document):
        target_msg = message.reply_to_message
    elif message.video or message.document:
        target_msg = message
    elif mode == 'reply' and message.reply_to_message:
        target_msg = message.reply_to_message
    else:
        if not (message.reply_to_message and (message.reply_to_message.video or message.reply_to_message.document)):
            return None
        target_msg = message.reply_to_message

    # Filename determine karo
    if target_msg.video:
        fname = target_msg.video.file_name or f"video_{int(time.time())}.mp4"
    elif target_msg.document:
        fname = target_msg.document.file_name or f"file_{int(time.time())}"
    else:
        fname = f"file_{int(time.time())}"

    file_path = os.path.join(download_dir, fname)

    # Fast multi-threaded download use karo
    path = await fast_download(
        client=message._client,
        message=target_msg,
        file_name=file_path,
        progress_callback=progress_for_pyrogram,
        progress_args=("⚡ Downloading...", msg, c_time)
    )

    return path
