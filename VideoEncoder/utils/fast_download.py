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
import inspect
import logging
import math
import os
import time
import traceback

from pyrogram import Client, raw
from pyrogram.errors import AuthBytesInvalid, FloodWait
from pyrogram.file_id import FileId, FileType
from pyrogram.session import Auth, Session
from pyrogram.types import Message

from .. import profile as _prof

log = logging.getLogger(__name__)

# 1 MB — Telegram ka standard upload.GetFile chunk limit (non-premium safe)
CHUNK_SIZE = 1024 * 1024
# Kitne parallel MTProto connections ek file pe (RAM/CPU/bandwidth ke hisaab
# se conservative default; zaroorat pade to badha sakte ho)
DEFAULT_CONNECTIONS = _prof.TG_DL_CONN     # tier ke hisaab se (profile.py / env TG_DL_CONNECTIONS)
PIPELINE = _prof.TG_DL_PIPE                # har connection par in-flight requests
HARD_MAX_CONNECTIONS = 32
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


def _dc_address(dc_id: int, test_mode: bool):
    """Naye pyrogram/kurigram Session ko (server_address, port) chahiye hota hai.
    DataCenter ka signature bhi version ke saath badalta hai, isliye introspection."""
    from pyrogram.session.internals import DataCenter
    try:
        params = inspect.signature(DataCenter).parameters
    except (TypeError, ValueError):
        params = {}
    cand = dict(dc_id=dc_id, test_mode=test_mode, ipv6=False, alt=False, media=True)
    kw = {k: v for k, v in cand.items() if k in params}
    res = DataCenter(**kw) if kw else DataCenter(dc_id, test_mode, False, False, True)
    return res[0], res[1]


def _build_session(client, dc_id, auth_key, test_mode):
    """Session(...) ko hamesha sahi keyword args se banao, chahe library ka
    constructor jaisa bhi ho (purana: client,dc_id,auth_key,test_mode,is_media |
    naya: + server_address, port)."""
    params = inspect.signature(Session.__init__).parameters
    cand = dict(client=client, dc_id=dc_id, auth_key=auth_key, test_mode=test_mode,
                is_media=True, is_cdn=False)
    kw = {k: v for k, v in cand.items() if k in params}
    if "server_address" in params or "port" in params:
        addr, port = _dc_address(dc_id, test_mode)
        if "server_address" in params:
            kw["server_address"] = addr
        if "port" in params:
            kw["port"] = port
    return Session(**kw)


async def _new_media_session(client: Client, dc_id: int) -> Session:
    """Har call ek NAYA, independent session banata hai (shared cached
    media_session use nahi karta) — taaki sach mein N alag connections ek
    sath chal sakein, ek connection ki speed-cap se bachne ke liye."""
    if dc_id != await client.storage.dc_id():
        session = _build_session(
            client, dc_id,
            await Auth(client, dc_id, await client.storage.test_mode()).create(),
            await client.storage.test_mode(),
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
        session = _build_session(
            client, dc_id, await client.storage.auth_key(),
            await client.storage.test_mode(),
        )
        await session.start()
    return session


async def _download_worker(session: Session, location, fd, queue: asyncio.Queue,
                           progress_state: dict, lock: asyncio.Lock):
    """Shared queue se chunk uthata hai (work-stealing). Ek session par PIPELINE
    workers chalte hain => ek hi connection par kai GetFile request ek saath
    in-flight rehte hain, isse network latency (RTT) ka wait nahi hota."""
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
                    # Chunk wapas queue mein, taaki doosra (healthy) worker retry kare
                    queue.put_nowait(idx)
                    raise
                await asyncio.sleep(min(0.5 * (2 ** attempt), 4))

        if isinstance(r, raw.types.upload.File) and r.bytes:
            # disk write thread mein — event loop (Telegram I/O) kabhi block nahi hota
            await asyncio.to_thread(os.pwrite, fd, r.bytes, offset)
            async with lock:
                progress_state['done'] += len(r.bytes)
                progress_state['chunks'].add(idx)


async def _parallel_download(client: Client, media, file_name: str,
                             progress_callback=None, progress_args=(),
                             connections: int = DEFAULT_CONNECTIONS):
    file_id_obj = FileId.decode(media.file_id)
    file_size = media.file_size or 0
    if file_size <= 0:
        raise ValueError("Unknown file size, can't chunk it.")

    total_chunks = math.ceil(file_size / CHUNK_SIZE)
    n_conn = max(1, min(connections, total_chunks, HARD_MAX_CONNECTIONS))
    location = _get_location(file_id_obj)

    sessions = []
    try:
        # Saare sessions EK SAATH banao (pehle ek-ek karke bante the => start mein 5-15 sec waste)
        made = await asyncio.gather(
            *[_new_media_session(client, file_id_obj.dc_id) for _ in range(n_conn)],
            return_exceptions=True)
        sessions = [m for m in made if isinstance(m, Session)]
        if not sessions:
            raise RuntimeError(f"media sessions nahi ban paaye: {made[0]!r}")
        n_conn = len(sessions)

        fd = os.open(file_name, os.O_CREAT | os.O_RDWR | os.O_TRUNC)
        try:
            os.ftruncate(fd, file_size)

            progress_state = {'done': 0, 'chunks': set()}
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
                results = await asyncio.gather(*[
                    _download_worker(sessions[i % n_conn], location, fd, queue, progress_state, lock)
                    for i in range(n_conn * PIPELINE)
                ], return_exceptions=True)
                # Koi worker mara bhi ho to baaki ne uska chunk utha liya hota hai; yahan
                # sirf poori file ka verify karo, adhoori file kabhi "success" na maani jaye.
                if len(progress_state['chunks']) < total_chunks:
                    errs = [r for r in results if isinstance(r, Exception)]
                    raise RuntimeError(
                        f"{total_chunks - len(progress_state['chunks'])} chunk reh gaye: {errs[:1]!r}")
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


def _labeled(progress_args, label):
    """Progress message ke title ko badal do, taaki chat mein dikhe kaunsa mode chal raha hai."""
    if progress_args and isinstance(progress_args[0], str):
        return (label,) + tuple(progress_args[1:])
    return progress_args


async def _range_download(client: Client, media, file_name: str,
                          progress_callback=None, progress_args=(), connections=8):
    """Tier-2: library ka apna get_file() kai ranges par EK SAATH (library ke apne
    version-matched sessions). Custom Session ki zaroorat nahi."""
    gp = inspect.signature(client.get_file).parameters
    if "offset" not in gp or "limit" not in gp:
        raise RuntimeError("client.get_file mein offset/limit nahi hai")
    fid = FileId.decode(media.file_id)
    size = media.file_size or 0
    if size <= 0:
        raise ValueError("Unknown file size")
    total_chunks = math.ceil(size / CHUNK_SIZE)
    n = max(1, min(connections, total_chunks))
    per = math.ceil(total_chunks / n)
    fd = os.open(file_name, os.O_CREAT | os.O_RDWR | os.O_TRUNC)
    state = {"done": 0}
    try:
        os.ftruncate(fd, size)

        async def part(i):
            start = i * per
            lim = min(per, total_chunks - start)
            if lim <= 0:
                return
            pos = start * CHUNK_SIZE
            want = min(size, (start + lim) * CHUNK_SIZE) - pos
            got = 0
            async for chunk in client.get_file(fid, size, lim, start):
                await asyncio.to_thread(os.pwrite, fd, chunk, pos + got)
                got += len(chunk)
                state["done"] += len(chunk)
            if got < want:
                raise RuntimeError(f"range {i}: {got}/{want} bytes")

        stop = asyncio.Event()

        async def rep():
            while not stop.is_set():
                if progress_callback:
                    r = progress_callback(state["done"], size, *progress_args)
                    if asyncio.iscoroutine(r):
                        await r
                try:
                    await asyncio.wait_for(stop.wait(), timeout=3)
                except asyncio.TimeoutError:
                    pass

        rt = asyncio.create_task(rep())
        try:
            res = await asyncio.gather(*[part(i) for i in range(n)], return_exceptions=True)
            bad = [r for r in res if isinstance(r, Exception)]
            if bad or state["done"] < size:
                raise RuntimeError(f"range download adhura: {bad[:1]!r}")
        finally:
            stop.set()
            try:
                await rt
            except Exception:
                pass
    finally:
        os.close(fd)
    if progress_callback:
        r = progress_callback(size, size, *progress_args)
        if asyncio.iscoroutine(r):
            await r
    return file_name


def _rm(path):
    try:
        if os.path.isfile(path):
            os.remove(path)
    except Exception:
        pass


async def fast_download(client: Client, message: Message, file_name: str,
                        progress_callback=None, progress_args=(),
                        connections: int = DEFAULT_CONNECTIONS):
    """
    3 layer: (1) apne N sessions x pipeline  (2) library get_file() ranges parallel
    (3) normal message.download(). Har layer ka fail-reason ERROR log mein jata hai,
    aur progress title mein dikhta hai kaunsa mode chal raha hai.
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
    size = getattr(media, "file_size", 0) or 0
    t0 = time.time()

    def _done(mode):
        dt = max(0.1, time.time() - t0)
        log.info(f"[TGDL] {mode} OK | {size / 1048576:.1f} MB in {dt:.1f}s = {size / 1048576 / dt:.2f} MB/s")

    try:
        r = await _parallel_download(
            client, media, file_name, progress_callback=progress_callback,
            progress_args=_labeled(progress_args, f"⚡ Turbo ({connections}x{PIPELINE}) Downloading..."),
            connections=connections)
        _done("turbo-sessions")
        return r
    except Exception as e:
        log.error(f"[TGDL] Layer-1 (turbo sessions) FAIL: {e!r}\n{traceback.format_exc()}")
        _rm(file_name)

    try:
        r = await _range_download(
            client, media, file_name, progress_callback=progress_callback,
            progress_args=_labeled(progress_args, f"⚡ Parallel ({connections}) Downloading..."),
            connections=connections)
        _done("range-parallel")
        return r
    except Exception as e:
        log.error(f"[TGDL] Layer-2 (range parallel) FAIL: {e!r}\n{traceback.format_exc()}")
        _rm(file_name)

    try:
        r = await message.download(
            file_name=file_name, progress=progress_callback,
            progress_args=_labeled(progress_args, "🐢 Normal Downloading..."))
        _done("single-fallback")
        return r
    except Exception as e2:
        log.error(f"[TGDL] Layer-3 (normal) also FAIL: {e2!r}")
        return None
