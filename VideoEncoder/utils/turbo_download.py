"""
turbo_download.py — SmartDL ka tez, seedha replacement (sirf stdlib, naya requirement nahi)
============================================================================================

SmartDL kyun slow padta tha:
  * File ko pehle se FIXED hisson mein baant deta hai (har thread ka apna hissa). Jo thread
    slow connection pe aaya woh sabse aakhir tak chalta hai, baaki threads khali baithe rehte
    hain => end mein speed gir jaati hai.
  * Parts alag-alag temp files mein likhta hai aur end mein sabko jodta hai (COMBINE): badi
    file (200-500MB) ko disk pe DO baar likhna padta hai => download khatam hone ke baad bhi
    kuch seconds extra (slow disk pe bahut zyada), aur disk bhi 2x chahiye.
  * Har file ke apne 8-10 threads, koi global limit nahi: 4 quality ek saath = 32-40 connections.

Yeh engine:
  * File ek baar pre-allocate karta hai, har chunk seedha apni sahi offset pe pwrite hota hai
    => combine step hai hi nahi, disk 1x.
  * Chhote chunks (4MB) ki SHARED queue — koi thread slow ho to baaki zyada chunks utha lete
    hain (work-stealing), end tak saare connections busy.
  * Connection keep-alive: ek thread ek hi connection pe kai chunks leta hai (baar-baar TLS
    handshake nahi).
  * GLOBAL connection budget (profile.DL_CONN_MAX): saari files milake itne se zyada connection
    nahi => host/CDN pe load control mein, har file ko barabar hissa.
  * Chunk fail ho to usi chunk ko resume karke retry (jitna aa chuka tha utna dobara nahi).
  * SAFE: range/size ka pata na chale, ya server Range na maane, ya koi bhi dikkat ho to
    automatically pySmartDL pe fallback (purana tareeka) — download purane se kharab kabhi nahi.

API SmartDL jaisi hai (isliye purana code bina badle chalta hai):
    d = TurboDL(url, dest, request_args={"headers": {...}}, threads=8, timeout=30)
    d.start(blocking=False); d.isFinished(); d.isSuccessful(); d.get_dest(); d.get_dl_size();
    d.get_speed(human=True); d.get_eta(human=True); d.get_progress(); d.get_errors()

Env: TURBO_DL=0 (band, seedha SmartDL), DOWNLOAD_CONN_MAX, DOWNLOAD_CHUNK_MB.
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
READ_BLOCK = 256 * 1024
MAX_REDIRECTS = 8
MIN_SIZE = 8 * 1024 * 1024          # isse chhoti file => SmartDL/normal (overhead ka fayda nahi)
CHUNK_RETRIES = 6

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
    """Retry se theek nahi hoga (403/404...)."""


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


class _Job:
    """Ek file ki saari state; threads isi ko share karte hain."""

    def __init__(self, url, dest, headers, threads, timeout):
        self.url = url
        self.dest = dest
        self.headers = dict(headers or {})
        self.headers.setdefault("User-Agent", "Mozilla/5.0")
        self.headers.setdefault("Accept-Encoding", "identity")      # compressed nahi — byte offsets sahi rahein
        self.threads = max(1, int(threads or 8))
        self.timeout = timeout or 30
        self.size = 0
        self.final_url = url
        self.done_bytes = 0
        self.lock = threading.Lock()
        self.queue = deque()
        self.errors = []
        self.abort = False
        self.fd = None
        self.t0 = time.time()
        self._samples = deque(maxlen=12)       # (time, bytes) — smooth speed

    # ── probe: size + range support + final URL (redirects follow) ──
    def probe(self):
        url = self.url
        for _ in range(MAX_REDIRECTS):
            p = urllib.parse.urlparse(url)
            if p.scheme not in ("http", "https") or not p.hostname:
                raise _NoRange("unsupported url")
            conn = _connect(p, self.timeout)
            try:
                h = dict(self.headers)
                h["Range"] = "bytes=0-0"
                conn.request("GET", _path(p), headers=h)
                r = conn.getresponse()
                if r.status in (301, 302, 303, 307, 308):
                    loc = r.getheader("Location")
                    r.read()
                    if not loc:
                        raise _NoRange("redirect without location")
                    url = urllib.parse.urljoin(url, loc)
                    continue
                if r.status == 206:
                    cr = r.getheader("Content-Range", "")          # bytes 0-0/12345
                    r.read()
                    try:
                        total = int(cr.rsplit("/", 1)[1])
                    except Exception:
                        raise _NoRange("bad content-range")
                    self.size, self.final_url = total, url
                    return
                if r.status == 200:
                    raise _NoRange("server ignored Range")
                if r.status in (401, 403, 404, 410):
                    raise _NoRange(f"HTTP {r.status}")            # SmartDL/browser fallback decide karenge
                raise _NoRange(f"HTTP {r.status}")
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

    def add_bytes(self, n):
        with self.lock:
            self.done_bytes += n

    def sub_bytes(self, n):
        with self.lock:
            self.done_bytes -= n

    def next_chunk(self):
        with self.lock:
            return self.queue.popleft() if self.queue else None

    def put_back(self, idx):
        with self.lock:
            self.queue.appendleft(idx)

    # ── ek chunk fetch (resume ke saath), connection reuse ──
    def fetch_chunk(self, conn_box, idx):
        start = idx * CHUNK
        end = min(start + CHUNK, self.size) - 1
        pos = start
        got_here = 0
        attempt = 0
        while pos <= end:
            if self.abort:
                raise _Fatal("aborted")
            p = urllib.parse.urlparse(self.final_url)
            try:
                if conn_box[0] is None:
                    conn_box[0] = _connect(p, self.timeout)
                h = dict(self.headers)
                h["Range"] = f"bytes={pos}-{end}"
                conn_box[0].request("GET", _path(p), headers=h)
                r = conn_box[0].getresponse()
                if r.status in (301, 302, 303, 307, 308):          # CDN ne final URL badal diya
                    loc = r.getheader("Location")
                    r.read()
                    if loc:
                        self.final_url = urllib.parse.urljoin(self.final_url, loc)
                    conn_box[0].close()
                    conn_box[0] = None
                    continue
                if r.status != 206:
                    r.read()
                    if r.status in (401, 403, 404, 410) or r.status == 200:
                        raise _Fatal(f"HTTP {r.status}")
                    raise OSError(f"HTTP {r.status}")
                while pos <= end:
                    blk = r.read(min(READ_BLOCK, end - pos + 1))
                    if not blk:
                        raise OSError("connection closed early")
                    os.pwrite(self.fd, blk, pos)
                    pos += len(blk)
                    got_here += len(blk)
                    self.add_bytes(len(blk))
                    if self.abort:
                        raise _Fatal("aborted")
                if r.getheader("Connection", "").lower() == "close":
                    conn_box[0].close()
                    conn_box[0] = None
            except _Fatal:
                raise
            except Exception as e:
                try:
                    if conn_box[0] is not None:
                        conn_box[0].close()
                except Exception:
                    pass
                conn_box[0] = None
                attempt += 1
                if attempt >= CHUNK_RETRIES:
                    raise
                time.sleep(min(0.4 * (2 ** attempt), 5))
        return got_here

    def worker(self):
        conn_box = [None]
        try:
            while not self.abort:
                idx = self.next_chunk()
                if idx is None:
                    return
                _SLOTS.acquire()
                try:
                    self.fetch_chunk(conn_box, idx)
                except _Fatal as e:
                    self.put_back(idx)
                    self.errors.append(str(e))
                    self.abort = True
                    return
                except Exception as e:
                    # ye thread haar gaya — chunk wapas queue mein, doosre thread le lenge
                    self.put_back(idx)
                    self.errors.append(repr(e))
                    return
                finally:
                    _SLOTS.release()
        finally:
            try:
                if conn_box[0] is not None:
                    conn_box[0].close()
            except Exception:
                pass


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
        self._started = False

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
            now = time.time()
            j._samples.append((now, j.done_bytes))
            if len(j._samples) >= 2:
                t0, b0 = j._samples[0]
                if now - t0 > 0.05:
                    sp = max(0.0, (j.done_bytes - b0) / (now - t0))
            elif now - j.t0 > 0.05:
                sp = j.done_bytes / (now - j.t0)
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
        if self._fb is not None:
            try:
                self._fb.stop()
            except Exception:
                pass
        if self._job:
            self._job.abort = True

    # ── internals ──
    def _run(self):
        try:
            if not ENABLED:
                raise _NoRange("TURBO_DL=0")
            job = _Job(self.url, self.dest, self.headers, self.threads, self.timeout)
            job.probe()
            if job.size < MIN_SIZE:
                raise _NoRange(f"small file ({job.size})")
            self._job = job
            job.prepare()
            n_threads = min(job.threads, len(job.queue), _CONN_MAX)
            with _ACTIVE_LOCK:
                _ACTIVE.add(self)
            try:
                ths = [threading.Thread(target=job.worker, name=f"turbo-dl-w{i}", daemon=True)
                       for i in range(n_threads)]
                for t in ths:
                    t.start()
                for t in ths:
                    t.join()
            finally:
                with _ACTIVE_LOCK:
                    _ACTIVE.discard(self)
                job.close_fd()
            if job.queue or job.abort or job.done_bytes < job.size:
                self._errors = job.errors[-3:] or ["incomplete"]
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
        # ── fallback: purana SmartDL ──
        if self._job is not None:
            self._job.abort = True
            self._job.close_fd()
        self._job = None
        try:
            if os.path.isfile(self.dest):
                os.remove(self.dest)
        except Exception:
            pass
        LOGGER.info(f"[TurboDL] fallback to SmartDL ({reason})")
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
