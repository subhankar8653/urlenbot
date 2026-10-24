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
import statistics
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


# ════════════════════════════════════════════════════════════════════════
#  Session pool — per (client, DC)
#  ─────────────────────────────
#  PEHLE: har download pe N naye sessions bante the, aur DC alag ho to HAR session ke
#  liye alag auth.ExportAuthorization (+ 6x retry loop). Telegram ExportAuthorization
#  pe bahut jaldi FloodWait deta hai (10s -> 240s...), isse:
#    * download start hone mein 10+ sec lagte the (FloodWait ka chhupa hua sleep)
#    * baad mein koi bhi doosra cross-DC kaam (thumbnail download_media wagairah)
#      "420 FLOOD_WAIT auth.ExportAuthorization" se fail ho jata tha.
#  AB:
#    * Har DC ke liye ExportAuthorization SIRF EK BAAR (process mein), auth_key cache.
#      Baaki saare sessions wahi authorized auth_key share karte hain (MTProto mein
#      ek auth_key pe kai session allowed hain — home DC pe bhi yahi ho raha tha).
#    * Sessions download ke baad band nahi hote — agli file/download pe turant reuse
#      (idle IDLE_CLOSE sec baad band, RAM bachti hai).
#    * Download pehla session ready hote hi shuru; baaki sessions chalte-chalte judte hain.
# ════════════════════════════════════════════════════════════════════════
IDLE_CLOSE = int(os.getenv('TGDL_IDLE_CLOSE', '600'))   # sec — idle pool ke sessions itni der baad band
EXPORT_FLOOD_MAX_WAIT = 300


class _DCPool:
    def __init__(self, client: Client, dc_id: int):
        self.client = client
        self.dc_id = dc_id
        self.sessions = []        # live Session objects
        self.dead = set()
        self.auth_key = None
        self._spare = None        # export ke time bana session — pehle creator ko mil jata hai
        self._lock = asyncio.Lock()
        self.refs = 0
        self._idle = None

    # ── auth ──
    async def _ensure_key(self):
        if self.auth_key is not None:
            return self.auth_key
        async with self._lock:
            if self.auth_key is not None:
                return self.auth_key
            client, dc_id = self.client, self.dc_id
            test_mode = await client.storage.test_mode()
            if dc_id == await client.storage.dc_id():
                self.auth_key = await client.storage.auth_key()
                return self.auth_key

            # Library ne is DC ka media session pehle hi bana rakha ho to uska authorized key reuse
            try:
                lib = getattr(client, "media_sessions", None)
                ls = lib.get(dc_id) if isinstance(lib, dict) else None
                k = getattr(ls, "auth_key", None)
                if k:
                    self.auth_key = k
                    return k
            except Exception:
                pass

            key = await Auth(client, dc_id, test_mode).create()
            tmp = _build_session(client, dc_id, key, test_mode)
            await tmp.start()
            try:
                floods = 0
                bad = 0
                while True:
                    try:
                        exported = await client.invoke(raw.functions.auth.ExportAuthorization(dc_id=dc_id))
                        await tmp.send(raw.functions.auth.ImportAuthorization(
                            id=exported.id, bytes=exported.bytes))
                        break
                    except AuthBytesInvalid:
                        bad += 1
                        if bad >= 3:
                            raise
                    except FloodWait as e:
                        wait = getattr(e, "value", getattr(e, "x", 0)) or 0
                        floods += 1
                        if wait > EXPORT_FLOOD_MAX_WAIT or floods > 2:
                            raise
                        log.warning(f"[TGDL] ExportAuthorization FloodWait {wait}s (DC{dc_id}) — wait karke retry")
                        await asyncio.sleep(wait + 1)
            except BaseException:
                try:
                    await tmp.stop()
                except Exception:
                    pass
                raise
            self.auth_key = key
            self._spare = tmp
            return key

    async def new_session(self) -> Session:
        key = await self._ensure_key()
        if self._spare is not None:
            s, self._spare = self._spare, None
            return s
        s = _build_session(self.client, self.dc_id, key, await self.client.storage.test_mode())
        await s.start()
        return s

    # ── pool state ──
    def live(self):
        return [s for s in self.sessions if s not in self.dead]

    def add(self, s):
        self.sessions.append(s)
        if self.refs == 0:
            self._schedule_idle()

    def drop(self, s):
        if s in self.dead:
            return
        self.dead.add(s)
        # Turant stop() karne se in-flight GetFile hang ho jaate the (download aakhir mein atak jata tha)
        try:
            asyncio.get_running_loop().call_later(DROP_STOP_DELAY, lambda: asyncio.ensure_future(self._stop(s)))
        except RuntimeError:
            asyncio.ensure_future(self._stop(s))

    @staticmethod
    async def _stop(s):
        try:
            await s.stop()
        except Exception:
            pass

    def begin(self):
        self.refs += 1
        if self._idle:
            self._idle.cancel()
            self._idle = None

    def end(self):
        self.refs = max(0, self.refs - 1)
        if self.refs == 0:
            self._schedule_idle()

    def _schedule_idle(self):
        if self._idle:
            self._idle.cancel()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._idle = loop.call_later(IDLE_CLOSE, lambda: asyncio.ensure_future(self.close(only_idle=True)))

    async def close(self, only_idle=False):
        if only_idle and self.refs > 0:
            return
        self._idle = None
        ss = self.live()
        self.sessions = []
        self.dead.clear()
        spare, self._spare = self._spare, None
        if spare is not None:
            ss.append(spare)
        await asyncio.gather(*[self._stop(s) for s in ss], return_exceptions=True)


def _pool_for(client: Client, dc_id: int) -> _DCPool:
    pools = client.__dict__.setdefault("_fd_pools", {})
    p = pools.get(dc_id)
    if p is None:
        p = pools[dc_id] = _DCPool(client, dc_id)
    return p


async def close_pools(client: Client):
    """Client disconnect karne se pehle bulao (short-lived user client ke liye)."""
    pools = client.__dict__.pop("_fd_pools", None) or {}
    for p in pools.values():
        if p._idle:
            p._idle.cancel()
        await p.close()


REQ_TIMEOUT = 25          # sec — ek GetFile ka max intezaar; hang hua request retry hota hai (end pe atakna nahi)
DROP_STOP_DELAY = 30      # sec — drop kiye session ko in-flight requests nipatne ke baad hi band karo


async def _download_worker(pool: _DCPool, session: Session, location, fd, queue: asyncio.Queue,
                           progress_state: dict, lock: asyncio.Lock):
    """Shared queue se chunk uthata hai (work-stealing). Ek session par PIPELINE
    workers chalte hain => ek hi connection par kai GetFile request ek saath
    in-flight rehte hain, isse network latency (RTT) ka wait nahi hota.

    ENDGAME: queue khali hone par idle worker band nahi hota — jo chunks abhi doosre (slow)
    sessions par in-flight hain unhe woh bhi fetch karta hai; jo pehle aaye uska data use hota hai.
    Isse aakhri % pe ek slow connection poori download ko roke nahi rakh sakta."""
    inflight = progress_state['inflight']
    chunks = progress_state['chunks']
    while True:
        if session in pool.dead:
            return                      # session toota — baaki sessions kaam sambhal lenge
        try:
            idx = queue.get_nowait()
        except asyncio.QueueEmpty:
            idx = None
            for i, c in inflight.items():
                if c < 2 and i not in chunks:
                    idx = i
                    break
            if idx is None:
                return
        if idx in chunks:
            continue
        inflight[idx] = inflight.get(idx, 0) + 1
        offset = idx * CHUNK_SIZE
        attempt = 0
        floods = 0
        r = None
        try:
            while True:
                try:
                    r = await asyncio.wait_for(
                        session.send(raw.functions.upload.GetFile(
                            location=location, offset=offset, limit=CHUNK_SIZE)),
                        REQ_TIMEOUT)
                    break
                except FloodWait as e:
                    floods += 1
                    if floods > 5:
                        if idx not in chunks and inflight.get(idx, 0) <= 1:
                            queue.put_nowait(idx)
                        raise
                    await asyncio.sleep(getattr(e, 'value', getattr(e, 'x', 5)))
                except Exception:
                    if idx in chunks:
                        break               # doosre worker ne chunk la diya — kaam khatam
                    attempt += 1
                    if attempt >= MAX_RETRIES_PER_CHUNK:
                        # Chunk wapas queue mein, taaki doosra (healthy) session retry kare
                        if inflight.get(idx, 0) <= 1:
                            queue.put_nowait(idx)
                        pool.drop(session)
                        raise
                    await asyncio.sleep(min(0.5 * (2 ** attempt), 4))

            if idx not in chunks and isinstance(r, raw.types.upload.File) and r.bytes:
                # disk write thread mein — event loop (Telegram I/O) kabhi block nahi hota
                await asyncio.to_thread(os.pwrite, fd, r.bytes, offset)
                if idx not in chunks:
                    async with lock:
                        progress_state['done'] += len(r.bytes)
                        chunks.add(idx)
                        if len(chunks) >= progress_state['total']:
                            progress_state['evt'].set()
                        sb = progress_state['sess_bytes']
                        sb[session] = sb.get(session, 0) + len(r.bytes)
        finally:
            n = inflight.get(idx, 1) - 1
            if n <= 0:
                inflight.pop(idx, None)
            else:
                inflight[idx] = n


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

    pool = _pool_for(client, file_id_obj.dc_id)
    pool.begin()
    try:
        fd = os.open(file_name, os.O_CREAT | os.O_RDWR | os.O_TRUNC)
        try:
            os.ftruncate(fd, file_size)

            progress_state = {'done': 0, 'chunks': set(), 'sess_bytes': {}, 'inflight': {}, 'total': total_chunks, 'evt': asyncio.Event()}
            lock = asyncio.Lock()
            stop_reporting = asyncio.Event()

            async def _reporter():
                while not stop_reporting.is_set():
                    if progress_callback:
                        res = progress_callback(progress_state['done'], file_size, *progress_args)
                        if asyncio.iscoroutine(res):
                            await res
                    try:
                        await asyncio.wait_for(stop_reporting.wait(), timeout=1.0)
                    except asyncio.TimeoutError:
                        pass

            # Reporter PEHLE — sessions ban rahe hain tab bhi user ko 0% progress turant dikhe
            reporter_task = asyncio.create_task(_reporter())

            queue = asyncio.Queue()
            for i in range(total_chunks):
                queue.put_nowait(i)

            workers = []
            join_errors = []
            closed = [False]
            heal = [0]

            def _spawn(sess):
                for _ in range(PIPELINE):
                    workers.append(asyncio.ensure_future(
                        _download_worker(pool, sess, location, fd, queue, progress_state, lock)))

            async def _join():
                try:
                    s = await pool.new_session()
                except Exception as e:
                    join_errors.append(e)
                    return
                pool.add(s)
                if not closed[0] and not queue.empty():
                    _spawn(s)

            try:
                # Pool mein jo sessions pehle se ready hain unse TURANT shuru (0s wait)
                existing = pool.live()[:n_conn]
                for s in existing:
                    _spawn(s)
                joins = [asyncio.ensure_future(_join()) for _ in range(n_conn - len(existing))]

                # ── STABLE SPEED monitor ──
                # Download ke beech mein kuch sessions/connections slow ho jaate hain (Telegram per-connection
                # throttle / toota hua route) aur total speed dheere dheere gir jaati hai. Har 4s check:
                #   (a) koi session baaki sessions ke median se <25% speed pe, lagatar 12s => use hata ke NAYA session
                #   (b) total speed peak ke 50% se neeche 12s+ => saare purane sessions refresh (naye connection
                #       shuru mein full speed dete hain)
                # Naya session pool ke cached auth_key se banta hai (ExportAuthorization dobara nahi).
                async def _monitor():
                    t_prev = time.monotonic()
                    t_start = t_prev
                    b_prev = {}
                    tot_prev = 0
                    peak = 0.0
                    low_since = None
                    last_refresh = t_prev
                    strikes = {}
                    while not stop_reporting.is_set():
                        try:
                            await asyncio.wait_for(stop_reporting.wait(), timeout=4)
                            return
                        except asyncio.TimeoutError:
                            pass
                        now = time.monotonic()
                        dt = max(now - t_prev, 0.1)
                        sb = progress_state['sess_bytes']
                        tot = progress_state['done']
                        agg = (tot - tot_prev) / dt
                        rates = {s: (sb.get(s, 0) - b_prev.get(s, 0)) / dt
                                 for s in list(sb) if s not in pool.dead}
                        b_prev = dict(sb)
                        tot_prev, t_prev = tot, now
                        remaining = total_chunks - len(progress_state['chunks'])
                        if queue.empty() or now - t_start < 8 or remaining < max(24, total_chunks * 0.2):
                            strikes.clear()
                            continue      # aakhri 20% mein koi refresh nahi — wahan stability zaroori hai
                        peak = max(peak * 0.995, agg)
                        victims = []
                        if len(rates) >= 3:
                            med = statistics.median(rates.values())
                            if med > 0:
                                flagged = [s for s, r in rates.items() if r < 0.25 * med]
                                for s_ in list(strikes):
                                    if s_ not in flagged:
                                        strikes.pop(s_, None)
                                for s_ in flagged:
                                    strikes[s_] = strikes.get(s_, 0) + 1
                                victims = [s_ for s_ in flagged if strikes[s_] >= 3][:2]   # lagatar 3 window (12s) slow
                        if peak > 1.5 * 1048576 and agg < 0.5 * peak:
                            low_since = low_since or now
                            if now - low_since >= 16 and now - last_refresh >= 30:
                                victims = list(rates)
                                last_refresh = now
                                low_since = None
                        else:
                            low_since = None
                        for s in victims[:max(1, n_conn)]:
                            log.info(f"[TGDL] slow session refresh (agg {agg / 1048576:.1f} MB/s, peak {peak / 1048576:.1f})")
                            pool.drop(s)
                            joins.append(asyncio.ensure_future(_join()))
                        if victims:
                            peak = agg   # naya baseline

                monitor_task = asyncio.ensure_future(_monitor())

                # Agar koi ready session nahi tha, to pehla session ban-ne tak ruko — baaki chalte-chalte judenge
                while not workers and any(not t.done() for t in joins):
                    await asyncio.wait([t for t in joins if not t.done()], return_when=asyncio.FIRST_COMPLETED)
                if not workers:
                    raise RuntimeError(f"media sessions nahi ban paaye: {join_errors[:1]!r}")

                evt_task = asyncio.ensure_future(progress_state['evt'].wait())
                while True:
                    if len(progress_state['chunks']) >= total_chunks:
                        break       # poori file aa gayi — atke hue (hung) request ka wait nahi
                    pend_w = [t for t in workers if not t.done()]
                    pend_j = [t for t in joins if not t.done()]
                    if not pend_w and (queue.empty() or not pend_j):
                        # Saare workers mar gaye (session drop/refresh) par chunks baaki => naya session
                        if not queue.empty() and heal[0] < 3:
                            heal[0] += 1
                            joins.append(asyncio.ensure_future(_join()))
                            await asyncio.sleep(0)
                            continue
                        break
                    await asyncio.wait(pend_w + pend_j + [evt_task], return_when=asyncio.FIRST_COMPLETED)

                # Koi worker mara bhi ho to baaki ne uska chunk utha liya hota hai; yahan
                # sirf poori file ka verify karo, adhoori file kabhi "success" na maani jaye.
                if len(progress_state['chunks']) < total_chunks:
                    errs = [t.exception() for t in workers
                            if t.done() and not t.cancelled() and t.exception()]
                    raise RuntimeError(
                        f"{total_chunks - len(progress_state['chunks'])} chunk reh gaye: {(errs or join_errors)[:1]!r}")
            finally:
                closed[0] = True
                stop_reporting.set()
                try:
                    evt_task.cancel()
                except Exception:
                    pass
                try:
                    monitor_task.cancel()
                except Exception:
                    pass
                for t in workers:
                    if not t.done():
                        t.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
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
        pool.end()     # sessions band nahi — agli download reuse karegi (idle pe band)


async def prewarm(client: Client, message=None, dc_id: int = None, connections: int = DEFAULT_CONNECTIONS):
    """Download shuru hone se PEHLE sessions taiyar (command aate hi bulao). Chupchap fail ho jaata hai.
    message do (file ka DC khud nikal lenge) ya seedha dc_id."""
    try:
        if dc_id is None and message is not None:
            media = message.video or message.document or message.audio
            if not media:
                return
            dc_id = FileId.decode(media.file_id).dc_id
        if dc_id is None:
            dc_id = await client.storage.dc_id()
        pool = _pool_for(client, dc_id)
        n = max(1, min(connections, HARD_MAX_CONNECTIONS)) - len(pool.live())
        if n <= 0:
            return
        pool.begin()
        try:
            res = await asyncio.gather(*[pool.new_session() for _ in range(n)], return_exceptions=True)
            for r in res:
                if isinstance(r, Session):
                    pool.add(r)
        finally:
            pool.end()
    except Exception as e:
        log.debug(f"[TGDL] prewarm skipped: {e!r}")



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
