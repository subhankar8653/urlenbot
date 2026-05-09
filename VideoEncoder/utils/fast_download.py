"""
Optimized Multi-threaded Telegram file downloader
"""
import asyncio
import os
import time
import math
import aiohttp
import aiofiles

from pyrogram import Client
from pyrogram.types import Message

# Config
CHUNK_SIZE = 512 * 1024   # 512KB per chunk
MAX_WORKERS = 8            # 8 parallel download workers
MIN_SIZE_FOR_MULTI = 5 * 1024 * 1024  # 5MB+ ke liye multi-thread


async def fast_download(client: Client, message: Message, file_name: str,
                         progress_callback=None, progress_args=()):
    """
    8-thread parallel Telegram downloader
    """
    # Media determine karo
    if message.video:
        media = message.video
    elif message.document:
        media = message.document
    else:
        return await _fallback_download(message, file_name, progress_callback, progress_args)

    file_size = getattr(media, 'file_size', 0) or 0

    # Chhoti files — normal download
    if file_size < MIN_SIZE_FOR_MULTI:
        return await _fallback_download(message, file_name, progress_callback, progress_args)

    os.makedirs(os.path.dirname(os.path.abspath(file_name)), exist_ok=True)

    # Chunks divide karo
    num_workers = min(MAX_WORKERS, max(2, file_size // (10 * 1024 * 1024) + 1))
    chunk_size = math.ceil(file_size / num_workers)

    downloaded_bytes = [0] * num_workers
    start_time = time.time()
    chunk_files = [f"{file_name}.part{i}" for i in range(num_workers)]
    success = [False] * num_workers

    async def update_progress():
        """Progress bar update karo"""
        while True:
            await asyncio.sleep(2)
            if progress_callback:
                total = sum(downloaded_bytes)
                try:
                    await progress_callback(total, file_size, *progress_args)
                except Exception:
                    pass
            if all(success):
                break

    async def download_worker(idx, offset, size):
        """Single worker — ek chunk download karta hai"""
        try:
            chunk_path = chunk_files[idx]
            byte_count = 0

            async with aiofiles.open(chunk_path, 'wb') as f:
                async for chunk in client.stream_media(media, offset=offset, limit=size):
                    await f.write(chunk)
                    byte_count += len(chunk)
                    downloaded_bytes[idx] = byte_count

            if byte_count > 0:
                success[idx] = True
        except Exception as e:
            success[idx] = False

    # Workers launch karo
    tasks = []
    for i in range(num_workers):
        offset = i * chunk_size
        size = min(chunk_size, file_size - offset)
        if size > 0:
            tasks.append(download_worker(i, offset, size))

    progress_task = asyncio.create_task(update_progress())

    await asyncio.gather(*tasks, return_exceptions=True)
    progress_task.cancel()

    # Sab chunks successful?
    all_ok = all(
        success[i] and os.path.exists(chunk_files[i]) and os.path.getsize(chunk_files[i]) > 0
        for i in range(len(tasks))
    )

    if all_ok:
        # Merge karo
        async with aiofiles.open(file_name, 'wb') as outfile:
            for i in range(len(tasks)):
                async with aiofiles.open(chunk_files[i], 'rb') as infile:
                    await outfile.write(await infile.read())
                os.remove(chunk_files[i])

        # Final progress
        if progress_callback:
            try:
                await progress_callback(file_size, file_size, *progress_args)
            except Exception:
                pass

        return file_name

    # Cleanup aur fallback
    for f in chunk_files:
        if os.path.exists(f):
            os.remove(f)

    return await _fallback_download(message, file_name, progress_callback, progress_args)


async def _fallback_download(message, file_name, progress_callback, progress_args):
    """Normal pyrogram download fallback"""
    c_time = time.time()
    path = await message.download(
        file_name=file_name,
        progress=progress_callback,
        progress_args=progress_args
    )
    return path
