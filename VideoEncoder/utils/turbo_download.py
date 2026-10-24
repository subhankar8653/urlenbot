"""
turbo_download.py — SmartDL ka tez, seedha replacement (sirf stdlib, naya requirement nahi)
============================================================================================

V2 (speed-decay fix) — "shuru mein tez, phir dheere dheere speed gir jaati hai" ka ilaaj:

  PURANI BUGS (jo ab theek hain):
  * Worker thread lagatar 5 fail ke baad HAMESHA ke liye mar jaata tha => parallel connections
    ghatte rehte the => speed gir-ti jaati thi. Ab thread kabhi nahi marta; fail pe backoff + retry.
  * Server 429/503/403 (too many connections) de to purana code poora download abort karke
    pySmartDL pe RESTART kar deta tha (0% se, aur woh bhi dheema). Ab AUTO-THROTTLE: connections
    ghata deta hai (cap), 20s shant rahe to dheere dheere wapas badhata hai.
  * Beech mein koi connection atak jaye (0 bytes) => 60s stall watchdog sab connections recycle
    karke SAME file pe resume karta hai (done chunks dobara nahi).
  * Slow connection detection: jo connection baaki ke muqable 4x se zyada slow ho, use turant
    badal diya jata hai (naya connection = full speed).
  * Cancel (stop) karne par ab SmartDL fallback start nahi hota.
  * Speed/ETA ab 1s sampling se (10s window) — jhatke nahi, ETA sahi.
  * Progress msg me Engine line: "Turbo 12/16" ya "SmartDL fallback (reason)" => pata chal jaata hai
    kaunsa engine chal raha hai.

Design (pehle jaisa):
  * File ek baar pre-allocate, har chunk seedha apni offset pe pwrite => combine step nahi.
  * 4MB chunks ki SHARED queue (work-stealing), connection keep-alive.
  * GLOBAL connection budget (profile.DL_CONN_MAX) saari files ke liye.
  * Range/size ka pata na chale to pySmartDL pe fallback (purana tareeka).

API SmartDL jaisi hai:
    d = TurboDL(url, dest, request_args={"headers": {...}}, threads=8, timeout=30)
    d.start(blocking=False); d.isFinished(); d.isSuccessful(); d.get_dest(); d.get_dl_size();
    d.get_speed(human=True); d.get_eta(human=True); d.get_progress(); d.get_errors(); d.get_info()

Env: TURBO_DL=0 (band, seedha SmartDL), DOWNLOAD_CONN_MAX, DOWNLOAD_CHUNK_MB, TURBO_STALL_SEC.
"""
import http.client
import logging
import os
import socket
import ssl
import threading
import time
import urllib.parse
from collections import deque

LOGGER = logging.getLogger(__name__)

ENABLED = os.getenv("TURBO_DL", "1") != "0"
CHUNK = max(1, int(os.getenv("DOWNLOAD_CHUNK_MB", "4") or 4)) * 1024 * 1024
READ_BLOCK = 512 * 1024             # chhota block => slow-connection check jaldi hota hai
MAX_CHUNKS_PER_CONN = 8             # itne chunk ke baad connection refresh (per-connection throttle se bachav)
SLOW_RATIO = 0.5                    # chunk speed fresh-connection speed ke is hisse se neeche => naya connection
MAX_REDIRECTS = 8
MIN_SIZE = 8 * 1024 * 1024          # isse chhoti file => SmartDL/normal
CHUNK_RETRIES = 6
STALL_SEC = max(15, int(os.getenv("TURBO_STALL_SEC", "60") or 60))   # itni der 0 bytes => connections recycle
MAX_ROUNDS = 8                      # bina progress ke itne recycle ke baad hi fallback
RAMP_AFTER = 20                     # itne sec shant rahe to cap +1
MIN_CAP = 2                         # auto-throttle itne se neeche nahi jaayega
SLOW_CHECK_AFTER = 6.0              # connection ko itne sec do, phir baaki se compare
SLOW_FACTOR = 0.25                  # conn speed < is hisse * avg-per-conn => badlo
CONN_IDLE_SEC = 12.0                # ek connection pe itni der 0 bytes (baaki chal rahe hon) => band karke retry

try:
    from .. import profile as _prof
    _CONN_MAX = int(os.getenv("DOWNLOAD_CONN_MAX", "") or getattr(_prof, "DL_CONN_MAX", 32))
except Exception:                                  # profile import na ho paaye
    _CONN_MAX = int(os.getenv("DOWNLOAD_CONN_MAX", "32") or 32)
_CONN_MAX = max(2, _CONN_MAX)

_SLOTS = threading.BoundedSemaphore(_CONN_MAX)     # saari files ke liye global connection budget
_ACTIVE = set()
_ACTIVE_LOCK = threading.Lock()
_SSL_CTX = ssl.create_default_context()


class _NoRange(Exception):
    """Server Range support nahi karta / size unknown — normal path pe jao."""


class _Fatal(Exception):
    """Retry se theek nahi hoga (404/410/...)."""


class _Throttle(Exception):
    """Server bol raha hai 'dheere' (429/503/403 beech me) — connections ghatao, thoda ruko."""

    def __init__(self, msg, retry_after=None):
        super().__init__(msg)
        try:
            self.retry_after = min(30.0, float(retry_after)) if retry_after else None
        except Exception:
            self.retry_after = None


class _Recycle(Exception):
    """Round khatam / stop — chunk wapas queue mein rakho aur thread band karo."""


class _Slow(Exception):
    """Yeh connection baaki se bahut slow — naya connection lo (fail nahi gina jaata)."""


def _human_size(n):
    n = float(n)
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.1f} {u}" if u != "B" else f"{int(n)} B"
        n /= 1024


def _human_time(sec):
    sec = int(max(0, sec))
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}h {m}m {s}s" if h else (f"{m}m {s}s" if m else f"{s}s")


def _connect(parsed, timeout):
    host = parsed.hostname
    port = parsed.port
    if parsed.scheme == "https":
        return http.client.HTTPSConnection(host, port or 443, timeout=timeout, context=_SSL_CTX)
    return http.client.HTTPConnection(host, port or 80, timeout=timeout)


def _path(parsed):
    p = parsed.path or "/"
    return p + ("?" + parsed.query if parsed.query else "")


def _kill(conn):
    """Doosre thread mein recv() pe atke connection ko jagao: pehle shutdown(), phir close()."""
    try:
        sk = getattr(conn, "sock", None)
        if sk is not None:
            try:
                sk.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _drop(conn_box):
    try:
        if conn_box[0] is not None:
            conn_box[0].close()
    except Exception:
        pass
    conn_box[0] = None


class _Job:
    """Ek file ki saari state; threads isi ko share karte hain."""

    def __init__(self, url, dest, headers, threads, timeout):
        self.url = url
        self.dest = dest
        self.headers = dict(headers or {})
        self.headers.setdefault("User-Agent", "Mozilla/5.0")
        self.headers.setdefault("Accept-Encoding", "identity")      # compressed nahi — byte offsets sahi rahein
        self.threads = max(1, int(threads or 8))
        self.timeout = min(max(10, timeout or 30), 30)
        self.size = 0
        self.final_url = url
        self.done_bytes = 0
        self.lock = threading.Lock()
        self.queue = deque()
        self.inflight = 0
        self.errors = deque(maxlen=20)
        self.abort = False             # hard stop (user stop / fatal)
        self.recycle = False           # round khatam — workers chhodo, naye round mein resume
        self.fd = None
        self.t0 = time.time()
        self.cap = self.threads        # abhi kitne workers kaam kar sakte hain (auto-throttle)
        self.boxes = []
        self.last_progress = time.time()
        self.last_throttle = 0.0
        self.last_ramp = 0.0
        self.fail_times = deque(maxlen=8)
        self.consec403 = 0
        self.speed = 0.0
        self._samples = deque(maxlen=10)       # (time, bytes) — 1s ke gap pe, supervisor bharta hai

    # ── probe: size + range support + final URL (redirects follow) ──
    def probe(self):
        last = None
        for attempt in range(3):
            try:
                return self._probe_once()
            except _NoRange:
                raise
            except Exception as e:                       # network hiccup — thoda ruk ke dobara
                last = e
                time.sleep(1.0 * (attempt + 1))
        raise _NoRange(f"probe failed: {last!r}")

    def _probe_once(self):
        url = self.url
        use_range = True
        for _ in range(MAX_REDIRECTS + 2):
            p = urllib.parse.urlparse(url)
            if p.scheme not in ("http", "https") or not p.hostname:
                raise _NoRange("unsupported url")
            conn = _connect(p, self.timeout)
            try:
                h = dict(self.headers)
                if use_range:
                    h["Range"] = "bytes=0-0"
                else:
                    h.pop("Range", None)
                conn.request("GET", _path(p), headers=h)
                r = conn.getresponse()
                st = r.status
                if st in (301, 302, 303, 307, 308):
                    loc = r.getheader("Location")
                    try:
                        r.read()
                    except Exception:
                        pass
                    if not loc:
                        raise _NoRange("redirect without location")
                    url = urllib.parse.urljoin(url, loc)
                    continue
                if st == 206:
                    cr = r.getheader("Content-Range", "")          # bytes 0-0/12345
                    try:
                        r.read()                                   # 1 byte body — kuch servers bheje bina conn band kar dete hain
                    except Exception:
                        pass                                       # header mil gaya, body ki zarurat nahi
                    try:
                        total = int(cr.rsplit("/", 1)[1])
                    except Exception:
                        raise _NoRange("bad content-range")
                    if total <= 0:
                        raise _NoRange("unknown size")
                    self.size, self.final_url = total, url
                    return
                if st == 200:
                    # Range ignore kiya ya probe ke bina bheja gaya — Accept-Ranges + length se bharosa karo
                    ar = (r.getheader("Accept-Ranges") or "").lower()
                    cl = r.getheader("Content-Length") or ""
                    if "bytes" in ar and cl.isdigit() and int(cl) > 0:
                        self.size, self.final_url = int(cl), url
                        return
                    raise _NoRange("server ignored Range")
                if use_range and st in (400, 403, 405, 416, 501):
                    use_range = False                              # kuch servers Range probe pe bigadte hain
                    continue
                raise _NoRange(f"HTTP {st}")
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        raise _NoRange("too many redirects")

    def prepare(self):
        d = os.path.dirname(os.path.abspath(self.dest))
        os.makedirs(d, exist_ok=True)
        self.fd = os.open(self.dest, os.O_CREAT | os.O_RDWR | os.O_TRUNC, 0o644)
        os.ftruncate(self.fd, self.size)
        n = (self.size + CHUNK - 1) // CHUNK
        self.queue = deque(range(n))

    def close_fd(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except Exception:
                pass
            self.fd = None

    # ── shared state helpers ──
    def err(self, msg):
        self.errors.append(msg)

    def add_bytes(self, n):
        with self.lock:
            self.done_bytes += n
            self.last_progress = time.time()

    def sub_bytes(self, n):
        with self.lock:
            self.done_bytes -= n

    def next_chunk(self):
        with self.lock:
            if self.queue:
                self.inflight += 1
                return self.queue.popleft()
            return None

    def finish_chunk(self):
        with self.lock:
            self.inflight -= 1

    def put_back(self, idx):
        with self.lock:
            self.queue.appendleft(idx)
            self.inflight -= 1

    def all_done(self):
        with self.lock:
            return not self.queue and self.inflight == 0

    def remaining(self):
        with self.lock:
            return len(self.queue) + self.inflight

    def kill_conns(self):
        with self.lock:
            boxes = list(self.boxes)
        for b in boxes:
            try:
                if b[0] is not None:
                    _kill(b[0])
            except Exception:
                pass

    def _kill_idle_conns(self, now_wall):
        """Ek connection atak gaya (baaki chal rahe hain) => sirf use band karo, worker naye conn pe retry karega."""
        now = time.monotonic()
        with self.lock:
            boxes = list(self.boxes)
        for b in boxes:
            try:
                if b[4] and b[0] is not None and now - b[3] > CONN_IDLE_SEC:
                    LOGGER.info("[TurboDL] atka hua connection band kiya — naya connection")
                    b[3] = now
                    _kill(b[0])
            except Exception:
                pass

    def _sleep(self, sec):
        end = time.time() + sec
        while time.time() < end and not self.abort and not self.recycle:
            time.sleep(0.2)

    # ── auto-throttle ──
    def throttle(self, why):
        with self.lock:
            now = time.time()
            if now - self.last_throttle < 4:
                return
            self.last_throttle = now
            new = max(MIN_CAP, int(self.cap * 0.75))
            if new < self.cap:
                LOGGER.warning(f"[TurboDL] server throttle ({why}) — connections {self.cap} -> {new}")
                self.cap = new

    def note_fail(self):
        now = time.time()
        self.fail_times.append(now)
        if len(self.fail_times) >= 4 and now - self.fail_times[-4] < 10:
            self.throttle("bahut saare connection errors")

    # ── ek chunk fetch (resume ke saath), connection reuse ──
    def fetch_chunk(self, conn_box, idx):
        got = [0]
        conn_box[3] = time.monotonic()
        conn_box[4] = True
        try:
            return self._fetch_chunk(conn_box, idx, got)
        except BaseException:
            self.sub_bytes(got[0])        # chunk wapas queue mein jayega — dobara gino mat
            raise
        finally:
            conn_box[4] = False

    def _fetch_chunk(self, conn_box, idx, got):
        start = idx * CHUNK
        end = min(start + CHUNK, self.size) - 1
        pos = start
        attempt = 0
        slow_hits = 0
        t_chunk = time.monotonic()
        while pos <= end:
            if self.abort or self.recycle:
                raise _Recycle()
            p = urllib.parse.urlparse(self.final_url)
            try:
                if conn_box[0] is None:
                    conn_box[0] = _connect(p, self.timeout)
                    conn_box[1], conn_box[2] = 0.0, 0
                h = dict(self.headers)
                h["Range"] = f"bytes={pos}-{end}"
                conn_box[3] = time.monotonic()
                conn_box[0].request("GET", _path(p), headers=h)
                r = conn_box[0].getresponse()
                conn_box[3] = time.monotonic()
                st = r.status
                if st in (301, 302, 303, 307, 308):               # CDN ne final URL badal diya
                    loc = r.getheader("Location")
                    r.read()
                    if loc:
                        self.final_url = urllib.parse.urljoin(self.final_url, loc)
                    _drop(conn_box)
                    continue
                if st != 206:
                    ra = r.getheader("Retry-After")
                    _drop(conn_box)                                # body mat padho (200 = poori file!)
                    if st in (429, 503, 509):
                        raise _Throttle(f"HTTP {st}", ra)
                    if st == 403:
                        if self.done_bytes > 0 and self.consec403 < 15:
                            self.consec403 += 1
                            raise _Throttle("HTTP 403", ra)
                        raise _Fatal("HTTP 403")
                    if st in (200, 401, 404, 410, 416):
                        raise _Fatal(f"HTTP {st}")
                    raise OSError(f"HTTP {st}")
                self.consec403 = 0
                t_req = time.monotonic()
                got_req = 0
                while pos <= end:
                    blk = r.read(min(READ_BLOCK, end - pos + 1))
                    if not blk:
                        raise OSError("connection closed early")
                    os.pwrite(self.fd, blk, pos)
                    n = len(blk)
                    pos += n
                    got[0] += n
                    got_req += n
                    self.add_bytes(n)
                    conn_box[3] = time.monotonic()
                    if self.abort or self.recycle:
                        raise _Recycle()
                    if pos <= end and slow_hits < 3:
                        el = time.monotonic() - t_req
                        if el >= SLOW_CHECK_AFTER:
                            ref = self.speed / max(1, self.inflight)      # baaki connections ki avg speed
                            if ref > 256 * 1024 and (got_req / el) < SLOW_FACTOR * ref:
                                slow_hits += 1
                                raise _Slow()
                if (r.getheader("Connection", "") or "").lower() == "close":
                    _drop(conn_box)
            except (_Fatal, _Recycle, _Throttle):
                raise
            except _Slow:
                _drop(conn_box)               # naya connection, attempt nahi gina
                continue
            except Exception as e:
                _drop(conn_box)
                if self.abort or self.recycle:
                    raise _Recycle()
                attempt += 1
                self.note_fail()
                if attempt >= CHUNK_RETRIES:
                    raise
                time.sleep(min(0.4 * (2 ** attempt), 5))

        # ── STABLE SPEED: connection health ──
        # Kai server/CDN ek hi connection pe kuch MB ke baad throttle kar dete hain (burst phir slow).
        try:
            dt = max(time.monotonic() - t_chunk, 0.001)
            rate = got[0] / dt
            if attempt == 0 and got[0] >= CHUNK // 2:
                conn_box[2] += 1
                if conn_box[1] <= 0:
                    conn_box[1] = rate
                elif rate < SLOW_RATIO * conn_box[1] or conn_box[2] >= MAX_CHUNKS_PER_CONN:
                    _drop(conn_box)
                    conn_box[1] = 0.0
                    conn_box[2] = 0
        except Exception:
            pass
        return got[0]

    def worker(self, wid):
        conn_box = [None, 0.0, 0, 0.0, False]   # [connection, fresh-conn chunk speed, chunks on conn, last activity, busy]
        with self.lock:
            self.boxes.append(conn_box)
        fails = 0
        try:
            while not self.abort and not self.recycle:
                if wid >= self.cap:                    # auto-throttle: is worker ko abhi aaram
                    if self.all_done():
                        return
                    time.sleep(0.5)
                    continue
                idx = self.next_chunk()
                if idx is None:
                    if self.all_done():
                        return
                    time.sleep(0.2)                    # koi aur ka chunk fail hua to main utha lunga
                    continue
                got_slot = False
                while not got_slot:
                    got_slot = _SLOTS.acquire(timeout=1)
                    if not got_slot and (self.abort or self.recycle):
                        self.put_back(idx)
                        return
                wait = 0.0
                try:
                    self.fetch_chunk(conn_box, idx)
                    self.finish_chunk()
                    fails = 0
                except _Recycle:
                    self.put_back(idx)
                    return
                except _Fatal as e:
                    self.put_back(idx)
                    self.err(str(e))
                    self.abort = True
                    return
                except _Throttle as e:
                    self.put_back(idx)
                    self.err(str(e))
                    self.throttle(str(e))
                    fails += 1
                    wait = e.retry_after or min(2.0 + fails * 2, 15.0)
                except Exception as e:
                    # Thread KABHI nahi marta — pehle 5 fail pe mar jaata tha => speed girti thi.
                    self.put_back(idx)
                    self.err(repr(e))
                    fails += 1
                    _drop(conn_box)
                    wait = min(1.0 * fails, 8.0)
                finally:
                    _SLOTS.release()
                if wait:
                    self._sleep(wait)
        finally:
            _drop(conn_box)

    # ── supervisor: workers chalao, speed sample karo, stall pe recycle, throttle ke baad ramp-up ──
    def run(self):
        rounds = 0
        while True:
            rounds += 1
            before = self.done_bytes
            self.recycle = False
            n = max(1, min(self.threads, self.remaining(), _CONN_MAX))
            self.cap = min(self.cap, n)
            self.cap = max(self.cap, min(n, MIN_CAP))
            with self.lock:
                self.boxes = []
            ths = [threading.Thread(target=self.worker, args=(i,), name=f"turbo-dl-w{i}", daemon=True)
                   for i in range(n)]
            self.last_progress = time.time()
            for t in ths:
                t.start()
            last_sample = 0.0
            while any(t.is_alive() for t in ths):
                time.sleep(0.25)
                now = time.time()
                if now - last_sample >= 1.0:
                    last_sample = now
                    self._samples.append((now, self.done_bytes))
                    if len(self._samples) >= 2:
                        t0, b0 = self._samples[0]
                        if now - t0 > 0.5:
                            self.speed = max(0.0, (self.done_bytes - b0) / (now - t0))
                if (self.cap < n and now - max(self.last_throttle, self.last_ramp) > RAMP_AFTER):
                    self.cap += 1
                    self.last_ramp = now
                if not self.abort and not self.recycle and now - self.last_progress > STALL_SEC \
                        and not self.all_done():
                    LOGGER.warning(f"[TurboDL] {STALL_SEC}s se koi byte nahi aaya — connections recycle")
                    self.err("stalled")
                    self.recycle = True
                    self.kill_conns()
                if self.abort:
                    self.kill_conns()
                else:
                    self._kill_idle_conns(now)
            for t in ths:
                t.join()
            if self.abort or self.all_done():
                return
            if self.done_bytes > before:
                rounds = 0                                   # progress hua — recycle ginti reset
            if rounds >= MAX_ROUNDS:
                return
            # URL/redirect refresh (token wale CDN ke liye) — best effort
            try:
                old = self.size
                self.probe()
                if self.size != old:
                    self.err("file size badal gaya")
                    self.abort = True
                    return
            except Exception:
                pass
            self.recycle = False


class TurboDL:
    """SmartDL-compatible facade. start(blocking=False) ke baad isFinished()/isSuccessful() poll karo."""

    def __init__(self, url, dest, progress_bar=False, threads=8, request_args=None, timeout=30, **_ignored):
        self.url = url
        self.dest = dest
        self.threads = threads
        self.timeout = timeout
        self.request_args = request_args or {}
        self.headers = (self.request_args.get("headers") or {})
        self._job = None
        self._thread = None
        self._finished = threading.Event()
        self._ok = False
        self._errors = []
        self._fb = None                     # pySmartDL fallback object
        self._fb_reason = ""
        self._started = False
        self._stopped = False

    # ── public API ──
    def start(self, blocking=True):
        if self._started:
            return
        self._started = True
        self._thread = threading.Thread(target=self._run, name="turbo-dl", daemon=True)
        self._thread.start()
        if blocking:
            self.wait()

    def wait(self, raise_exceptions=False):
        while not self.isFinished():
            time.sleep(0.1)
        if raise_exceptions and not self.isSuccessful():
            raise RuntimeError(f"download failed: {self.get_errors()}")

    def isFinished(self):
        if self._fb is not None:
            return self._fb.isFinished()
        return self._finished.is_set()

    def isSuccessful(self):
        if self._fb is not None:
            return self._fb.isSuccessful()
        return self._finished.is_set() and self._ok

    def get_dest(self):
        return self.dest

    @property
    def filesize(self):
        """SmartDL.filesize jaisa — progress_for_url() isi ko padhta hai."""
        if self._fb is not None:
            return getattr(self._fb, "filesize", 0) or 0
        return self._job.size if self._job else 0

    def get_errors(self):
        if self._fb is not None:
            try:
                return self._fb.get_errors()
            except Exception:
                pass
        return list(self._errors)

    def get_info(self):
        """Progress msg ke liye: kaunsa engine + kitne connection."""
        if self._fb is not None:
            r = (self._fb_reason or "")[:45]
            return f"SmartDL fallback ({r})" if r else "SmartDL fallback"
        j = self._job
        if j:
            act = min(j.inflight, j.cap)
            return f"Turbo {act}/{j.cap} conn" + (" ⚠️throttled" if j.cap < j.threads else "")
        return "Turbo (starting)"

    def get_dl_size(self, human=False):
        if self._fb is not None:
            return self._fb.get_dl_size(human=human)
        v = self._job.done_bytes if self._job else 0
        return _human_size(v) if human else v

    def get_final_filesize(self, human=False):
        if self._fb is not None:
            return self._fb.get_final_filesize(human=human)
        v = self._job.size if self._job else 0
        return _human_size(v) if human else v

    def get_progress(self):
        if self._fb is not None:
            return self._fb.get_progress()
        j = self._job
        if not j or not j.size:
            return 0.0
        return min(1.0, max(0.0, j.done_bytes / j.size))

    def get_speed(self, human=False):
        if self._fb is not None:
            return self._fb.get_speed(human=human)
        j = self._job
        sp = 0.0
        if j:
            sp = j.speed
            if sp <= 0:                                     # shuru ke 1-2 sec: simple average
                el = time.time() - j.t0
                if el > 0.2:
                    sp = j.done_bytes / el
        return f"{_human_size(sp)}/s" if human else sp

    def get_eta(self, human=False):
        if self._fb is not None:
            return self._fb.get_eta(human=human)
        j = self._job
        eta = 0
        if j and j.size:
            sp = self.get_speed()
            eta = int((j.size - j.done_bytes) / sp) if sp > 0 else 0
        return _human_time(eta) if human else eta

    def get_progress_bar(self, length=20):
        p = self.get_progress()
        n = int(p * length)
        return "[" + "#" * n + "-" * (length - n) + "]"

    def stop(self):
        self._stopped = True
        if self._fb is not None:
            try:
                self._fb.stop()
            except Exception:
                pass
        if self._job:
            self._job.abort = True
            self._job.kill_conns()

    # ── internals ──
    def _run(self):
        job = None
        try:
            if not ENABLED:
                raise _NoRange("TURBO_DL=0")
            job = _Job(self.url, self.dest, self.headers, self.threads, self.timeout)
            job.probe()
            if job.size < MIN_SIZE:
                raise _NoRange(f"small file ({job.size})")
            self._job = job
            job.prepare()
            with _ACTIVE_LOCK:
                _ACTIVE.add(self)
            try:
                job.run()
            finally:
                with _ACTIVE_LOCK:
                    _ACTIVE.discard(self)
                job.close_fd()
            if self._stopped:                                  # user ne cancel kiya — fallback nahi
                self._errors = ["stopped"]
                self._ok = False
                self._finished.set()
                return
            if not job.all_done() or job.abort or job.done_bytes < job.size:
                self._errors = list(job.errors)[-3:] or ["incomplete"]
                raise _NoRange("incomplete: " + "; ".join(self._errors))
            try:
                if os.path.getsize(self.dest) != job.size:
                    raise _NoRange("size mismatch")
            except OSError as e:
                raise _NoRange(str(e))
            dt = max(time.time() - job.t0, 0.001)
            LOGGER.info(f"[TurboDL] {os.path.basename(self.dest)} {job.size / 1048576:.0f}MB in "
                        f"{dt:.1f}s ({job.size / 1048576 / dt:.1f} MB/s)")
            self._ok = True
            self._finished.set()
            return
        except _NoRange as e:
            reason = str(e)
        except Exception as e:                                       # noqa: BLE001
            reason = repr(e)
        if self._stopped:
            self._finished.set()
            return
        # ── fallback: purana SmartDL ──
        if job is not None:
            job.abort = True
            job.kill_conns()
            job.close_fd()
        self._job = None
        try:
            if os.path.isfile(self.dest):
                os.remove(self.dest)
        except Exception:
            pass
        self._fb_reason = reason
        LOGGER.warning(f"[TurboDL] fallback to SmartDL ({reason}) url={self.url[:120]}")
        try:
            from pySmartDL import SmartDL
            fb = SmartDL(self.url, self.dest, progress_bar=False, threads=self.threads,
                         request_args=self.request_args, timeout=self.timeout)
            fb.start(blocking=False)
            self._fb = fb
        except Exception as e:                                       # noqa: BLE001
            self._errors = [f"turbo: {reason}", f"smartdl: {e!r}"]
            self._ok = False
            self._finished.set()


def stats():
    with _ACTIVE_LOCK:
        files = len(_ACTIVE)
    return {"enabled": ENABLED, "conn_max": _CONN_MAX, "active_files": files}
