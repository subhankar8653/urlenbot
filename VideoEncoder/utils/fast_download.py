"""
Optimized Telegram file downloader
- Pyrogram ka native download use karta hai (most reliable)
- Progress bar sahi se dikhata hai
- Chunked multi-thread Pyrogram mein properly supported nahi hai,
  isliye fast native download use karo
"""
import asyncio
import os
import time

from pyrogram import Client
from pyrogram.types import Message


async def fast_download(client: Client, message: Message, file_name: str,
                        progress_callback=None, progress_args=()):
    """
    Pyrogram native download — sabse reliable aur fast method.
    stream_media offset/limit Pyrogram mein unreliable hai,
    isliye direct message.download() use karo jo internally optimized hai.
    """
    # Media check karo
    if message.video:
        media = message.video
    elif message.document:
        media = message.document
    elif message.audio:
        media = message.audio
    else:
        return None

    os.makedirs(os.path.dirname(os.path.abspath(file_name)), exist_ok=True)

    c_time = time.time()

    # Pyrogram ka native download — internally multipart handle karta hai
    path = await message.download(
        file_name=file_name,
        progress=progress_callback,
        progress_args=progress_args
    )

    return path
