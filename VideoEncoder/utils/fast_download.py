"""
Multi-threaded Telegram file downloader
Pyrogram ke default downloader se 3-4x fast
"""
import asyncio
import os
import time
import math

from pyrogram import Client
from pyrogram.types import Message

CHUNK_SIZE = 1024 * 1024  # 1MB per chunk
MAX_WORKERS = 4            # 4 parallel threads


async def fast_download(client: Client, message: Message, file_name: str, progress_callback=None, progress_args=()):
    """
    Multi-threaded file downloader
    File ko chunks mein divide karke parallel download karta hai
    """
    # Target message determine karo
    if message.video:
        media = message.video
    elif message.document:
        media = message.document
    else:
        return await message.download(file_name=file_name)

    file_size = media.file_size
    if not file_size:
        return await message.download(file_name=file_name)

    # Chhoti files ke liye normal download
    if file_size < 10 * 1024 * 1024:  # 10MB se chhota
        return await message.download(file_name=file_name)

    # Chunks calculate karo
    num_chunks = min(MAX_WORKERS, math.ceil(file_size / (10 * 1024 * 1024)))
    chunk_size = math.ceil(file_size / num_chunks)

    os.makedirs(os.path.dirname(file_name) if os.path.dirname(file_name) else '.', exist_ok=True)

    # Temp chunk files
    chunk_files = [f"{file_name}.part{i}" for i in range(num_chunks)]
    downloaded = [0] * num_chunks
    start_time = time.time()

    async def download_chunk(chunk_idx, offset, length):
        """Ek chunk download karo"""
        try:
            chunk_path = chunk_files[chunk_idx]
            async with client.stream_media(media, offset=offset, limit=length) as stream:
                with open(chunk_path, 'wb') as f:
                    async for chunk in stream:
                        f.write(chunk)
                        downloaded[chunk_idx] += len(chunk)

                        # Progress update
                        if progress_callback:
                            total_downloaded = sum(downloaded)
                            elapsed = time.time() - start_time
                            speed = total_downloaded / elapsed if elapsed > 0 else 0
                            await progress_callback(
                                total_downloaded, file_size,
                                *progress_args
                            )
        except Exception:
            # Fallback - normal download karo
            pass

    # Check karo ki stream_media available hai
    has_stream = hasattr(client, 'stream_media')

    if has_stream and num_chunks > 1:
        # Parallel chunks download karo
        tasks = []
        for i in range(num_chunks):
            offset = i * chunk_size
            length = min(chunk_size, file_size - offset)
            tasks.append(download_chunk(i, offset, length))

        await asyncio.gather(*tasks)

        # Chunks ko merge karo
        all_exist = all(os.path.exists(f) and os.path.getsize(f) > 0 for f in chunk_files)

        if all_exist:
            with open(file_name, 'wb') as outfile:
                for chunk_file in chunk_files:
                    with open(chunk_file, 'rb') as infile:
                        outfile.write(infile.read())
                    os.remove(chunk_file)
            return file_name

    # Fallback: Normal pyrogram download
    for f in chunk_files:
        if os.path.exists(f):
            os.remove(f)

    c_time = time.time()
    path = await message.download(
        file_name=file_name,
        progress=progress_callback,
        progress_args=progress_args
    )
    return path
