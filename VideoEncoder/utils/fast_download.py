"""
Parallel-chunk Telegram downloader.

Normal Pyrogram/kurigram message.download() uses ONE MTProto connection,
which Telegram caps at a certain per-connection speed (yahi wajah hai
2-2.5 MB/s pe atak jaana chahe network fast ho). File-to-link / leech bots
isse bypass karte hain: file ko fixed-size chunks mein todke, KAI ALAG
connections (sessions) pe EK SAATH fetch karte hain, phir har chunk seedha
uske sahi byte-offset pe disk pe likh dete hain. Isse aggregate throughput
kaafi zyada mil jata hai (network/DC allow kare utna).

Agar kisi bhi wajah se parallel path fail ho (auth issue, chhoti/unknown
size file, koi bhi exception), seedha reliable single-connection
message.download() pe fallback ho jata hai — download kabhi fail nahi
hota, sirf speed thoda kam mil sakta hai.
"""
import asyncio
import logging
import math
import os
import time

from pyrogram import Client, raw
from pyrogram.errors import AuthBytesInvalid, FloodWait
from pyrogram.file_id import FileId, FileType
from pyrogram.session import Auth, Session
from pyrogram.types import Message

log = logging.getLogger(__name__)

# 1 MB — Telegram ka standard upload.GetFile chunk limit (non-premium safe)
CHUNK_SIZE = 1024 * 1024
# Kitne parallel MTProto connections ek file pe (RAM/CPU/bandwidth ke hisaab
# se conservative default; zaroorat pade to badha sakte ho)
DEFAULT_CONNECTIONS = 6
MAX_RETRIES_PER_CHUNK = 3


def _get_location(file_id: FileId):
    if file_id.file_type == FileType.PHOTO:
        return raw.types.InputPhotoFileLocation(
            id=file_id.media_id,
            access_hash=file_id.access_hash,
            file_reference=file_id.file_reference,
            thumb_size=file_id.thumbnail_size,
        )
    return raw.types.InputDocumentFileLocation(
        id=file_id.media_id,
        access_hash=file_id.access_hash,
        file_reference=file_id.file_reference,
        thumb_size=file_id.thumbnail_size,
    )


async def _new_media_session(client: Client, dc_id: int) -> Session:
    """Har call ek NAYA, independent session banata hai (shared cached
    media_session use nahi karta) — taaki sach mein N alag connections ek
    sath chal sakein, ek connection ki speed-cap se bachne ke liye."""
    if dc_id != await client.storage.dc_id():
        session = Session(
            client, dc_id,
            await Auth(client, dc_id, await client.storage.test_mode()).create(),
            await client.storage.test_mode(), is_media=True,
        )
        await session.start()
        for _ in range(6):
            exported_auth = await client.invoke(raw.functions.auth.ExportAuthorization(dc_id=dc_id))
            try:
                await session.send(raw.functions.auth.ImportAuthorization(
                    id=exported_auth.id, bytes=exported_auth.bytes))
                break
            except AuthBytesInvalid:
                continue
        else:
            await session.stop()
            raise AuthBytesInvalid()
    else:
        session = Session(
            client, dc_id, await client.storage.auth_key(),
            await client.storage.test_mode(), is_media=True,
        )
        await session.start()
    return session


async def _download_worker(session: Session, location, fd, queue: asyncio.Queue,
                           progress_state: dict, lock: asyncio.Lock):
    """Pulls chunk indices from a SHARED queue (work-stealing) instead of a
    static pre-assigned list. This way, if one session hits a FloodWait or
    a slow patch, the other sessions simply pick up more chunks from the
    queue instead of finishing early and sitting idle — keeps all N
    connections busy right up to the last chunk, so speed doesn't taper
    off near the end of the file."""
    while True:
        try:
            idx = queue.get_nowait()
        except asyncio.QueueEmpty:
            return
        offset = idx * CHUNK_SIZE
        attempt = 0
        while True:
            try:
                r = await session.send(
                    raw.functions.upload.GetFile(location=location, offset=offset, limit=CHUNK_SIZE)
                )
                break
            except FloodWait as e:
                await asyncio.sleep(getattr(e, 'value', getattr(e, 'x', 5)))
            except Exception:
                attempt += 1
                if attempt >= MAX_RETRIES_PER_CHUNK:
                    # Put it back so another (possibly healthier) session
                    # can retry it, rather than killing the whole download.
                    await queue.put(idx)
                    raise
                await asyncio.sleep(min(0.5 * (2 ** attempt), 4))

        if isinstance(r, raw.types.upload.File) and r.bytes:
            os.pwrite(fd, r.bytes, offset)
            async with lock:
                progress_state['done'] += len(r.bytes)


async def _parallel_download(client: Client, media, file_name: str,
                             progress_callback=None, progress_args=(),
                             connections: int = DEFAULT_CONNECTIONS):
    file_id_obj = FileId.decode(media.file_id)
    file_size = media.file_size or 0
    if file_size <= 0:
        raise ValueError("Unknown file size, can't chunk it.")

    total_chunks = math.ceil(file_size / CHUNK_SIZE)
    n_conn = max(1, min(connections, total_chunks, 8))
    location = _get_location(file_id_obj)

    sessions = []
    try:
        for _ in range(n_conn):
            sessions.append(await _new_media_session(client, file_id_obj.dc_id))

        fd = os.open(file_name, os.O_CREAT | os.O_RDWR | os.O_TRUNC)
        try:
            os.ftruncate(fd, file_size)

            progress_state = {'done': 0}
            lock = asyncio.Lock()
            stop_reporting = asyncio.Event()

            async def _reporter():
                while not stop_reporting.is_set():
                    if progress_callback:
                        res = progress_callback(progress_state['done'], file_size, *progress_args)
                        if asyncio.iscoroutine(res):
                            await res
                    try:
                        await asyncio.wait_for(stop_reporting.wait(), timeout=3)
                    except asyncio.TimeoutError:
                        pass

            reporter_task = asyncio.create_task(_reporter())

            queue = asyncio.Queue()
            for i in range(total_chunks):
                queue.put_nowait(i)

            try:
                await asyncio.gather(*[
                    _download_worker(sessions[i], location, fd, queue, progress_state, lock)
                    for i in range(n_conn)
                ])
            finally:
                stop_reporting.set()
                try:
                    await reporter_task
                except Exception:
                    pass
        finally:
            os.close(fd)

        if progress_callback:
            res = progress_callback(file_size, file_size, *progress_args)
            if asyncio.iscoroutine(res):
                await res

        return file_name
    finally:
        for s in sessions:
            try:
                await s.stop()
            except Exception:
                pass


async def fast_download(client: Client, message: Message, file_name: str,
                        progress_callback=None, progress_args=(),
                        connections: int = DEFAULT_CONNECTIONS):
    """
    Public entry point (signature unchanged) — pehle multi-connection
    parallel download try karta hai, fail hone pe Pyrogram ke reliable
    single-connection download() pe fallback karta hai.
    """
    if message.video:
        media = message.video
    elif message.document:
        media = message.document
    elif message.audio:
        media = message.audio
    else:
        return None

    os.makedirs(os.path.dirname(os.path.abspath(file_name)), exist_ok=True)

    try:
        return await _parallel_download(
            client, media, file_name,
            progress_callback=progress_callback,
            progress_args=progress_args,
            connections=connections,
        )
    except Exception as e:
        log.warning(f"Parallel download failed ({e}), falling back to single-connection download.")
        try:
            if os.path.isfile(file_name):
                os.remove(file_name)
        except Exception:
            pass
        try:
            return await message.download(
                file_name=file_name,
                progress=progress_callback,
                progress_args=progress_args
            )
        except Exception as e2:
            log.error(f"Fallback download also failed: {e2}")
            return None
