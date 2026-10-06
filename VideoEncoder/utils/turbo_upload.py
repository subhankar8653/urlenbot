"""
turbo_upload.py — saari uploads ke liye EK shared, host-aware, self-tuning engine
=================================================================================

Pehle kya problem thi (jab kai file ek saath upload hoti thi):
  * Har file apna alag set of sessions x workers kholti thi (4 x 8 = 32 parts) aur
    koi bhi kisi ko nahi dekhta tha. 4 file ek saath = 128 parts (64MB) ek saath
    uplink mein => link bhar jaata, har part ka jawab late aata, Session.invoke ke
    andar hi timeout + retry (default 10 baar!) hota, wahi bytes dobara bhejte =>
    speed 5-10x gir jaati (500 KB/s jaisi). Bytes bhejne ki jagah retry mein waqt jaata.
  * Har file ke liye naya Client + naye sessions (connect mein seconds jaate).
  * Same session_string se kai Client ek saath => Telegram reject => bot fallback (slow).
  * Part file se event-loop ke andar sync read hota tha (slow disk pe poora bot atakta).

Ab:
  1. GOVERNOR — process mein ek hi, saari uploads (user client + bot client) ka total
     "in-flight parts" control karta hai. Part ke "extra intezaar" (queue delay) se
     window upar/neeche hota hai + timeout/FloodWait pe 30% kam (AIMD): link ki asli
     capacity khud pata chalti hai. Host profile.py se sirf range (UP_START..UP_MAX) milti hai.
     Fairness: jis file ke kam parts in-flight, usko pehle slot (max-min fair) =>
     1 file = poori speed, 4 file = barabar hissa, total speed gir ti nahi.
  2. SESSION POOL — har client ke liye sessions ek baar bante hain aur saari files
     reuse karti hain (parallel connect). Idle hone par band (RAM bachti hai).
  3. Part-reads thread mein (pread) — event loop kabhi disk pe nahi atakta.
  4. Session.invoke ke andar chhupa hua retry band (retries=1) — retry ab yahin hota
     hai jahan se window ko pata chalta hai (congestion signal).
  5. SAFE: koi bhi dikkat => original pyrogram save_file pe fallback (circuit breaker).

Install: Client.save_file class-level patch hota hai (import par apne aap), isliye
/url, auto-monitor, swift, bot-fallback, channel upload — sab ko ek saath milta hai.

Env: TURBO_UPLOAD=0 (band), UPLOAD_INFLIGHT_START / UPLOAD_INFLIGHT_MAX (profile.py).
"""
import asyncio
import inspect
import logging
import math
import os
import random
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

from .. import profile as _prof

LOGGER = logging.getLogger(__name__)

PART_SIZE = 512 * 1024              # Telegram big-file part
MIN_SIZE = 10 * 1024 * 1024         # isse chhoti file => normal path
MAX_SIZE = 1900 * 1024 * 1024       # isse badi => original (wahi limit-error dega)
PER_SESSION = 6                     # window ke har itne parts pe ek session (per-connection speed-cap se bachne ke liye)
PART_TIMEOUT = 30                   # ek part ka max intezaar (sec)
MAX_ATTEMPTS = 8

ENABLED = (os.getenv("TURBO_UPLOAD", "1") != "0"
           and os.getenv("FAST_UPLOAD", "1") != "0")

_READ_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="turbo-read")


class _Unsupported(Exception):
    """Installed pyrogram/pyrofork ka Session API samajh nahi aaya — turbo band, original chalu."""


# ════════════════════════════════════════════════════════════════════════
#  GOVERNOR — global window + fairness
# ════════════════════════════════════════════════════════════════════════
class _Up:
    __slots__ = ("name", "size", "inflight", "waiters", "sent", "t0")

    def __init__(self, name, size):
        self.name = name
        self.size = size
        self.inflight = 0
        self.waiters = deque()
        self.sent = 0
        self.t0 = time.monotonic()


class Governor:
    # Window ka control "extra intezaar" (queue delay) se: part ka RTT us waqt ke sabse
    # chhote RTT se kitna zyada hai. Link khali => extra ~0. Link bhar gaya => parts
    # queue mein baithte hain => extra badhta hai. Isse host ki asli bandwidth khud pata
    # chalti hai (slow Render free se lekar fast VPS tak), koi fixed number guess nahi.
    LOW_QD = 0.6     # sec: isse kam extra intezaar => window badhao
    HIGH_QD = 2.0    # sec: isse zyada => window ghatao (timeout se bahut door)
    TARGET_QD = 1.0  # ghatate waqt yahan tak laane ki koshish

    def __init__(self):
        self.max = float(_prof.UP_MAX)
        self.min = float(max(2, _prof.UP_PER_FILE_MIN))
        # Shuru chhote window se (slow-start har RTT mein double karta hai) — dheemi link pe
        # pehla burst hi timeout na kar de. UP_START = tab tak ka expected "typical" level.
        self.limit = float(min(self.max, max(self.min, min(_prof.UP_START, 8))))
        self.inflight = 0
        self.uploads = []
        self.slow_start = True
        self.rtt = None
        self._bk_cur = None
        self._bk_prev = None
        self._bk_t = time.monotonic()
        self._acks = 0
        self._last_loss = 0.0
        self.losses = 0
        self._samples = deque()          # (t, cumulative_bytes) — speed ke liye
        self.total_bytes = 0

    # ── registration ──
    def register(self, up):
        self.uploads.append(up)

    def unregister(self, up):
        try:
            self.uploads.remove(up)
        except ValueError:
            pass
        while up.waiters:
            fut = up.waiters.popleft()
            if not fut.done():
                fut.cancel()
        self._dispatch()

    # ── slot grant (max-min fair: sabse kam in-flight wali upload ko pehle) ──
    def _dispatch(self):
        cap = int(self.limit)
        while self.inflight < cap:
            best = None
            for u in self.uploads:
                while u.waiters and u.waiters[0].done():   # cancelled futures hatao
                    u.waiters.popleft()
                if u.waiters and (best is None or u.inflight < best.inflight):
                    best = u
            if best is None:
                return
            fut = best.waiters.popleft()
            self.inflight += 1
            best.inflight += 1
            fut.set_result(None)

    async def acquire(self, up):
        fut = asyncio.get_running_loop().create_future()
        up.waiters.append(fut)
        self._dispatch()
        try:
            await fut
        except asyncio.CancelledError:
            if fut.done() and not fut.cancelled():
                # slot mil chuka tha par task cancel ho gaya — slot wapas
                self.inflight -= 1
                up.inflight -= 1
                self._dispatch()
            raise

    def release(self, up, rtt=None, congested=False):
        self.inflight = max(0, self.inflight - 1)
        up.inflight = max(0, up.inflight - 1)
        if congested:
            self._on_loss()
        elif rtt is not None:
            self._on_ack(rtt)
        self._dispatch()

    # ── congestion control ──
    def _base_rtt(self, rtt, now):
        if now - self._bk_t > 30:                       # 30s ke do bucket ka rolling min
            self._bk_prev, self._bk_cur, self._bk_t = self._bk_cur, None, now
        if self._bk_cur is None or rtt < self._bk_cur:
            self._bk_cur = rtt
        cands = [x for x in (self._bk_cur, self._bk_prev) if x is not None]
        return min(cands)

    def _on_ack(self, rtt):
        now = time.monotonic()
        base = self._base_rtt(rtt, now)
        self.rtt = rtt if self.rtt is None else 0.85 * self.rtt + 0.15 * rtt
        qd = max(0.0, self.rtt - base)                   # extra intezaar (sec)

        if self.slow_start:
            if qd >= self.LOW_QD:
                self.slow_start = False
            else:
                self.limit = min(self.max, self.limit + 1.0)    # har ack +1 => har RTT mein double
            return

        self._acks += 1
        if self._acks < max(1, int(self.limit)):                # ~ek RTT mein ek baar decide
            return
        self._acks = 0
        if qd < self.LOW_QD:
            self.limit = min(self.max, self.limit + 1.0)
        elif qd > self.HIGH_QD:
            self.limit = max(self.min, self.limit * max(0.6, self.TARGET_QD / qd))

    def _on_loss(self):
        now = time.monotonic()
        self.slow_start = False
        if now - self._last_loss < max(2.0, 2.0 * (self.rtt or 1.0)):
            return                                                # ek burst ko ek hi baar gino
        self._last_loss = now
        self.losses += 1
        self.limit = max(self.min, self.limit * 0.7)
        self._acks = 0

    # ── speed meter ──
    def add_bytes(self, n):
        now = time.monotonic()
        self.total_bytes += n
        self._samples.append((now, self.total_bytes))
        while self._samples and now - self._samples[0][0] > 10:
            self._samples.popleft()

    def speed(self):
        if len(self._samples) < 2:
            return 0.0
        (t0, b0), (t1, b1) = self._samples[0], self._samples[-1]
        return (b1 - b0) / (t1 - t0) if t1 > t0 else 0.0


GOV = Governor()


# ════════════════════════════════════════════════════════════════════════
#  Circuit breaker — turbo fail ho to original pe, baar-baar try nahi
# ════════════════════════════════════════════════════════════════════════
class _Breaker:
    def __init__(self):
        self.fails = 0
        self.until = 0.0
        self.permanent = False

    def allowed(self):
        return ENABLED and not self.permanent and time.monotonic() >= self.until

    def success(self):
        self.fails = 0

    def failure(self, permanent=False):
        if permanent:
            self.permanent = True
            LOGGER.error("[Turbo] band — is pyrogram version ka Session API support nahi; original upload chalega")
            return
        self.fails += 1
        if self.fails >= 2:
            self.until = time.monotonic() + 600
            self.fails = 0
            LOGGER.warning("[Turbo] 2 baar fail — 10 min ke liye original upload pe")


BREAKER = _Breaker()


# ════════════════════════════════════════════════════════════════════════
#  Session pool
# ════════════════════════════════════════════════════════════════════════
class _Sess:
    __slots__ = ("s", "load", "fails", "dead")

    def __init__(self, s):
        self.s = s
        self.load = 0
        self.fails = 0
        self.dead = False


_SESSION_MAX = max(int(_prof.UPLOAD_SESSIONS), math.ceil(_prof.UP_MAX / PER_SESSION))


def _session_kwargs(Session, dc_id, auth_key, test_mode, client):
    """Session.__init__ ka signature dekh ke sahi kwargs banao (version badalne par bhi crash nahi)."""
    params = inspect.signature(Session.__init__).parameters
    have = dict(client=client, dc_id=dc_id, auth_key=auth_key, test_mode=test_mode, is_media=True)
    kw = {k: v for k, v in have.items() if k in params}
    for name, p in params.items():
        if name == "self" or name in kw:
            continue
        if p.default is inspect.Parameter.empty and p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY):
            if name in ("server_address", "port"):
                try:
                    from pyrogram.session.internals import DataCenter
                    dc = None
                    for args in ((dc_id, test_mode, getattr(client, "ipv6", False), False, True),
                                 (dc_id, test_mode, getattr(client, "ipv6", False), True)):
                        try:
                            dc = DataCenter(*args)
                            break
                        except TypeError:
                            continue
                    if dc is None:
                        raise _Unsupported("DataCenter")
                    kw["server_address"], kw["port"] = dc
                    continue
                except _Unsupported:
                    raise
                except Exception as e:
                    raise _Unsupported(f"server_address/port: {e!r}")
            raise _Unsupported(f"Session needs unknown arg {name!r}")
    return kw


async def _new_session(client):
    try:
        from pyrogram.session import Session
    except Exception as e:
        raise _Unsupported(f"import Session: {e!r}")
    dc_id = await client.storage.dc_id()
    auth_key = await client.storage.auth_key()
    test_mode = await client.storage.test_mode()
    kw = _session_kwargs(Session, dc_id, auth_key, test_mode, client)
    s = Session(**kw)
    await s.start()
    return _Sess(s)


def _invoke_kwargs():
    try:
        from pyrogram.session import Session
        p = inspect.signature(Session.invoke).parameters
    except Exception:
        return {}
    kw = {}
    if "retries" in p:
        kw["retries"] = 1          # chhupa hua 10x retry band — retry ab hum karte hain (signal ke saath)
    if "timeout" in p:
        kw["timeout"] = PART_TIMEOUT
    return kw


_INVOKE_KW = None


async def _invoke(sess, rpc):
    global _INVOKE_KW
    if _INVOKE_KW is None:
        _INVOKE_KW = _invoke_kwargs()
    fn = getattr(sess, "invoke", None) or sess.send
    return await fn(rpc, **_INVOKE_KW)


class _Pool:
    def __init__(self, client):
        self.client = client
        self.sessions = []
        self.lock = None
        self.active = 0
        self.idle_handle = None
        self.growing = False

    def want(self):
        n = math.ceil(GOV.limit / PER_SESSION)
        return max(min(2, _SESSION_MAX), min(_SESSION_MAX, n))

    def live(self):
        return [x for x in self.sessions if not x.dead]

    async def ensure(self, n=None):
        n = n or self.want()
        if self.lock is None:
            self.lock = asyncio.Lock()
        async with self.lock:
            self.sessions = self.live()
            missing = n - len(self.sessions)
            if missing <= 0:
                return
            res = await asyncio.gather(*[_new_session(self.client) for _ in range(missing)],
                                       return_exceptions=True)
            last = None
            for r in res:
                if isinstance(r, _Sess):
                    self.sessions.append(r)
                else:
                    last = r
            if not self.sessions:
                raise last or RuntimeError("koi media session nahi bana")
            if last is not None and isinstance(last, _Unsupported):
                raise last

    def pick(self):
        live = self.live()
        return min(live, key=lambda x: x.load) if live else None

    def maybe_grow(self):
        if self.growing or len(self.live()) >= self.want():
            return
        self.growing = True

        async def _g():
            try:
                await self.ensure()
            except Exception:
                pass
            finally:
                self.growing = False
        asyncio.ensure_future(_g())

    def kill(self, s):
        if s.dead:
            return
        s.dead = True
        asyncio.ensure_future(self._stop(s))

    @staticmethod
    async def _stop(s):
        try:
            await s.s.stop()
        except Exception:
            pass

    async def close(self):
        ss, self.sessions = self.sessions, []
        await asyncio.gather(*[self._stop(s) for s in ss], return_exceptions=True)

    def begin(self):
        self.active += 1
        if self.idle_handle:
            self.idle_handle.cancel()
            self.idle_handle = None

    def end(self):
        self.active = max(0, self.active - 1)
        if self.active == 0 and not self.idle_handle:
            loop = asyncio.get_running_loop()
            self.idle_handle = loop.call_later(
                _prof.UP_IDLE_CLOSE, lambda: asyncio.ensure_future(self._idle_close()))

    async def _idle_close(self):
        self.idle_handle = None
        if self.active == 0:
            await self.close()


def _pool_for(client):
    pool = client.__dict__.get("_turbo_pool")
    if pool is None:
        pool = _Pool(client)
        client.__dict__["_turbo_pool"] = pool
    return pool


async def close_pool(client):
    """Client disconnect karne se pehle bulao — uske media sessions band."""
    pool = getattr(client, "__dict__", {}).pop("_turbo_pool", None)
    if pool is not None:
        if pool.idle_handle:
            pool.idle_handle.cancel()
        await pool.close()


# ════════════════════════════════════════════════════════════════════════
#  Core upload
# ════════════════════════════════════════════════════════════════════════
def _pread(fd, idx):
    off = idx * PART_SIZE
    buf = os.pread(fd, PART_SIZE, off)
    while len(buf) < PART_SIZE:
        more = os.pread(fd, PART_SIZE - len(buf), off + len(buf))
        if not more:
            break
        buf += more
    return buf


def _retryable(e):
    if isinstance(e, (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError, EOFError)):
        return True
    return type(e).__name__ in ("InternalServerError", "ServiceUnavailable", "Timeout",
                                "TimeoutError", "BadMsgNotification")


async def _send_part(pool, up, fd, file_id, total, idx):
    from pyrogram import raw
    from pyrogram.errors import FloodWait

    loop = asyncio.get_running_loop()
    data = None
    floods = 0
    last_err = None

    for attempt in range(MAX_ATTEMPTS):
        await GOV.acquire(up)
        rel = {}
        wait = 0.0
        s = None
        try:
            if data is None:
                data = await loop.run_in_executor(_READ_POOL, _pread, fd, idx)
                if not data:
                    raise OSError(f"part {idx}: file se 0 bytes mile (file badal gayi?)")
            s = pool.pick()                  # slot milne ke BAAD — tab load sahi dikhta hai
            if s is None:
                await pool.ensure(1)
                s = pool.pick()
            pool.maybe_grow()
            s.load += 1
            try:
                t0 = time.monotonic()
                await _invoke(s.s, raw.functions.upload.SaveBigFilePart(
                    file_id=file_id, file_part=idx, file_total_parts=total, bytes=data))
                rel["rtt"] = time.monotonic() - t0
            finally:
                s.load -= 1
        except asyncio.CancelledError:
            raise
        except FloodWait as e:
            last_err = e
            rel["congested"] = True
            wait = float(getattr(e, "value", None) or getattr(e, "x", None) or 5)
        except Exception as e:
            last_err = e
            if not _retryable(e):
                raise
            rel["congested"] = True
            if s is not None:
                s.fails += 1
                if s.fails >= 3:
                    pool.kill(s)
        else:
            s.fails = 0
            return len(data)
        finally:
            GOV.release(up, **rel)

        if wait:
            floods += 1
            if floods > 6:
                raise last_err
            await asyncio.sleep(min(wait, 60) + 0.5)
        else:
            await asyncio.sleep(min(0.4 * (2 ** attempt), 6))

    raise last_err or RuntimeError(f"part {idx} bhej nahi paye")


async def turbo_save_file(client, path, progress=None, progress_args=()):
    """Multi-session, window-controlled upload. Fail hone par exception (caller fallback karta hai)."""
    from pyrogram import raw
    try:
        from pyrogram import StopTransmission
    except Exception:                                    # pragma: no cover
        class StopTransmission(Exception):
            pass

    path = os.fspath(path)
    size = os.path.getsize(path)
    total = (size + PART_SIZE - 1) // PART_SIZE
    file_id = random.getrandbits(62)
    pool = _pool_for(client)
    up = _Up(os.path.basename(path), size)
    t_start = time.monotonic()

    GOV.register(up)
    pool.begin()
    fd = None
    tasks = []
    rep_task = None
    stop_evt = asyncio.Event()
    stop_exc = []

    def _abort(exc):
        stop_exc.append(exc)
        for t in tasks:
            t.cancel()

    async def _reporter():
        last = -1
        while True:
            try:
                await asyncio.wait_for(stop_evt.wait(), 0.5)
            except asyncio.TimeoutError:
                pass
            cur = min(up.sent, size)
            if progress and cur != last:
                last = cur
                try:
                    r = progress(cur, size, *progress_args)
                    if inspect.isawaitable(r):
                        await r
                except StopTransmission as e:
                    _abort(e)
                    return
                except Exception:
                    pass
            if stop_evt.is_set():
                return

    try:
        await pool.ensure()
        fd = os.open(path, os.O_RDONLY)
        counter = iter(range(total))

        async def worker():
            for idx in counter:                          # shared iterator: await ke beech kabhi nahi tootta
                n = await _send_part(pool, up, fd, file_id, total, idx)
                up.sent += n
                GOV.add_bytes(n)

        for _ in range(min(total, int(GOV.max))):
            tasks.append(asyncio.ensure_future(worker()))
        rep_task = asyncio.ensure_future(_reporter())

        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        if stop_exc:
            raise stop_exc[0]
        for t in done:
            if not t.cancelled() and t.exception():
                raise t.exception()

        stop_evt.set()
        await rep_task
        if stop_exc:
            raise stop_exc[0]
        if progress:
            try:
                r = progress(size, size, *progress_args)
                if inspect.isawaitable(r):
                    await r
            except StopTransmission:
                raise
            except Exception:
                pass

        dt = max(time.monotonic() - t_start, 0.01)
        LOGGER.info(f"[Turbo] {size / 1048576:.0f}MB in {dt:.1f}s ({size / 1048576 / dt:.2f} MB/s) | "
                    f"window {GOV.limit:.0f} | uploads {len(GOV.uploads)} | "
                    f"sessions {len(pool.live())} | losses {GOV.losses}")
        return raw.types.InputFileBig(id=file_id, parts=total, name=os.path.basename(path))
    finally:
        stop_evt.set()
        for t in tasks:
            if not t.done():
                t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if rep_task is not None and not rep_task.done():
            rep_task.cancel()
            await asyncio.gather(rep_task, return_exceptions=True)
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        GOV.unregister(up)
        pool.end()


# ════════════════════════════════════════════════════════════════════════
#  Client.save_file patch (class-level — saare clients ke liye)
# ════════════════════════════════════════════════════════════════════════
_ORIG_SAVE = None


async def _patched_save_file(self, path, *args, **kwargs):
    if (BREAKER.allowed() and not args
            and kwargs.get("file_id") is None and not kwargs.get("file_part")
            and isinstance(path, (str, os.PathLike))):
        try:
            p = os.fspath(path)
            ok = os.path.isfile(p) and MIN_SIZE <= os.path.getsize(p) <= MAX_SIZE
        except OSError:
            ok = False
        if ok:
            try:
                res = await turbo_save_file(self, p, kwargs.get("progress"),
                                            kwargs.get("progress_args", ()))
                BREAKER.success()
                return res
            except asyncio.CancelledError:
                raise
            except Exception as e:
                try:
                    from pyrogram import StopTransmission
                    if isinstance(e, StopTransmission):
                        raise
                except ImportError:
                    pass
                BREAKER.failure(permanent=isinstance(e, _Unsupported))
                LOGGER.warning(f"[Turbo] fail ({e!r}) — original upload pe fallback")
    return await _ORIG_SAVE(self, path, *args, **kwargs)


def install():
    """Idempotent. pyrogram.Client.save_file ko turbo-wrapper se badal do."""
    global _ORIG_SAVE
    try:
        import pyrogram
    except Exception:
        return False
    if getattr(pyrogram.Client.save_file, "_turbo", False):
        return True
    try:
        import tgcrypto  # noqa: F401
    except Exception:
        LOGGER.warning("[Turbo] TgCrypto nahi mila — encryption pure-python pe hogi, upload CPU-bound "
                       "aur bahut slow rahega. requirements mein TgCrypto-pyrofork install karo.")
    _ORIG_SAVE = pyrogram.Client.save_file
    _patched_save_file._turbo = True
    pyrogram.Client.save_file = _patched_save_file
    LOGGER.info(f"[Turbo] installed | window {_prof.UP_START}-{_prof.UP_MAX} parts | "
                f"max sessions {_SESSION_MAX} | enabled={ENABLED}")
    return True


def stats():
    return {
        "enabled": BREAKER.allowed(),
        "window": GOV.limit,
        "max": GOV.max,
        "inflight": GOV.inflight,
        "uploads": len(GOV.uploads),
        "speed": GOV.speed(),
        "rtt": GOV.rtt,
        "losses": GOV.losses,
    }


install()
