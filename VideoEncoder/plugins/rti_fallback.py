"""
rti_fallback.py — Missing quality (1080p) ko RTI ke 4 mirror servers se nikaalna
================================================================================
Swift page pe 1080p abhi nahi aaya (360p/720p aa chuke) to RTI episode page ke
Mega / HubCloud / WatchNow / DLBeta buttons se 1080p file download karke wapas
deta hai. Upload ka kaam caller (auto_monitor poller / manual /rti pipeline) hi karta hai.

CHAIN (sabse tez pehle — tumhare test ke bypass + download/upload time se):
    mega      9.9s  + 48s  = ~58s
    hubcloud  38.2s + 46s  = ~84s
    watchnow  31.7s + 77s  = ~109s
    dlbeta    24.7s + 86s  = ~111s
Pehla server fail ho (resolve / download / quota) to turant agla, phir agla — jab tak
koi ek file na de de. Jo server fail hua use agle kuch minute ke liye list mein sabse
peeche kar diya jaata hai (taaki har episode pe wahi fail na dohrae).

ENV:
    RTI_FALLBACK=0                      poora fallback band
    RTI_FALLBACK_ORDER=mega,hubcloud,watchnow,dlbeta     order apne hisab se
    RTI_FALLBACK_QUALITIES=1080p        kin qualities ke liye (default sirf 1080p)

Command (Sudo): /rtiorder  -> abhi ka order + live timings
"""

import asyncio
import os
import re
import shutil
import subprocess
import time
import traceback
import uuid

from pyrogram import Client, filters
from pyrogram.enums import ParseMode

from .. import LOGGER, download_dir
from .. import profile as _prof
from ..utils import job_ctl
from ..utils.helper import check_chat

KINDS_ALL = ("mega", "hubcloud", "watchnow", "dlbeta")

# Tumhare /rtimega test se (resolve + download/upload seconds). Chhota = tez = pehle.
_SEED = {
    "mega":     9.9 + 48.0,
    "hubcloud": 38.2 + 46.0,
    "watchnow": 31.7 + 77.0,
    "dlbeta":   24.7 + 86.0,
}

_RESOLVE_TIMEOUT = 300          # ek server ka bypass max (sec)
_COOLDOWN_RESOLVE = 600         # resolve fail -> itni der server list mein peeche
_COOLDOWN_DOWNLOAD = 1200       # download fail (quota etc.) -> itni der peeche
_RETRY_AFTER_FAIL = 240         # poora chain fail -> same (page, ep, quality) pe itni der baad hi dobara
_WRONG_QUALITY_BLOCK = 3600     # mirror ki file 1080p nikli hi nahi -> 1 ghanta dobara try nahi

_VIDEO_EXTS = (".mkv", ".mp4", ".avi", ".mov", ".webm", ".m4v", ".ts")
_Q_TOKEN_RE = re.compile(r"[\s._-]*\b(?:360p|480p|720p|1080p|2160p)\b", re.I)

_FAIL_UNTIL: dict = {}          # kind -> ts (tab tak list mein peeche)
_STATS: dict = {}               # kind -> {"ok", "fail", "last_resolve", "last_dl"}
_NEG: dict = {}                 # (page, ep, quality) -> ts (tab tak dobara try nahi)
_RUNNING: set = set()           # (page, ep, quality) jo abhi chal rahe hain


class _Cancelled(Exception):
    pass


class _WrongQuality(Exception):
    def __init__(self, got: str):
        super().__init__(got)
        self.got = got


# ─────────────────────────────────────────────
#  Config helpers
# ─────────────────────────────────────────────
def enabled() -> bool:
    if os.getenv("RTI_FALLBACK", "1").strip() == "0":
        return False
    try:
        from . import rti_downloader as rd
        return bool(rd.SELENIUM_OK)
    except Exception:
        return False


def fallback_qualities() -> tuple:
    raw = os.getenv("RTI_FALLBACK_QUALITIES", "1080p")
    qs = tuple(q.strip().lower() for q in raw.split(",") if q.strip())
    return qs or ("1080p",)


def ordered_kinds() -> list:
    """Abhi ka try-order: (env ya seed) order, par jo server haal mein fail hua woh sabse peeche."""
    env = os.getenv("RTI_FALLBACK_ORDER", "")
    base = [k.strip().lower() for k in env.split(",") if k.strip().lower() in KINDS_ALL]
    for k in sorted(KINDS_ALL, key=lambda x: _SEED.get(x, 999)):
        if k not in base:
            base.append(k)
    now = time.time()
    good = [k for k in base if _FAIL_UNTIL.get(k, 0) <= now]
    cooling = [k for k in base if k not in good]
    return good + cooling


def _stat(kind: str) -> dict:
    return _STATS.setdefault(kind, {"ok": 0, "fail": 0, "last_resolve": None, "last_dl": None})


def _mark_ok(kind: str, t_resolve: float, t_dl: float):
    s = _stat(kind)
    s["ok"] += 1
    s["last_resolve"], s["last_dl"] = t_resolve, t_dl
    _FAIL_UNTIL.pop(kind, None)
    LOGGER.info(f"[RTI-FB] ✅ {kind}: bypass {t_resolve:.1f}s + download {t_dl:.1f}s = {t_resolve + t_dl:.1f}s")


def _mark_fail(kind: str, why: str, cooldown: int):
    s = _stat(kind)
    s["fail"] += 1
    _FAIL_UNTIL[kind] = time.time() + cooldown
    LOGGER.warning(f"[RTI-FB] ❌ {kind} fail ({why}) — {cooldown // 60} min ke liye list mein peeche")


# ─────────────────────────────────────────────
#  Progress shim (poller ke download bar ke liye — mega ke liye)
# ─────────────────────────────────────────────
class _DlShim:
    """TurboDL/SmartDL jaisa chhota object, taaki progress bar mein Mega download bhi dikhe."""

    def __init__(self):
        self.done = 0
        self.total = 0
        self.fin = False
        self.ok = False
        self.dest = None

    def get_dl_size(self, human=False):
        return self.done

    def get_final_filesize(self, human=False):
        return self.total

    def isFinished(self):
        return self.fin

    def isSuccessful(self):
        return self.ok

    def get_dest(self):
        return self.dest

    def stop(self):
        pass


def _cancelled(prog) -> bool:
    try:
        ev = (prog or {}).get("cancel")
        return bool(ev is not None and ev.is_set())
    except Exception:
        return False


# ─────────────────────────────────────────────
#  Quality check (ffprobe) + naam
# ─────────────────────────────────────────────
def _probe_quality(path: str):
    """Video ki asli resolution -> '1080p' / '720p' / ... ya None (ffprobe na ho / fail)."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", path],
            capture_output=True, text=True, timeout=40,
        )
        m = re.search(r"(\d+)x(\d+)", out.stdout or "")
        if not m:
            return None
        w, h = int(m.group(1)), int(m.group(2))
    except Exception:
        return None
    if w >= 3000 or h >= 2000:
        return "2160p"
    if w >= 1700 or h >= 1000:
        return "1080p"
    if w >= 1200 or h >= 680:
        return "720p"
    if w >= 800 or h >= 440:
        return "480p"
    return "360p"


def _finalize_file(path: str, wanted: str, dest_dir: str) -> str:
    """Quality verify -> naam mein quality tag -> dest_dir mein le aao. Galat quality pe _WrongQuality."""
    got = _probe_quality(path)
    if got and got != wanted:
        raise _WrongQuality(got)
    base, ext = os.path.splitext(os.path.basename(path))
    if ext.lower() not in _VIDEO_EXTS:
        ext = ".mkv"
    base = _Q_TOKEN_RE.sub("", base).strip(" ._-") or "video"
    final = os.path.join(dest_dir, f"{base} {wanted}{ext}")
    if os.path.exists(final):
        final = os.path.join(dest_dir, f"{base} {wanted} {int(time.time())}{ext}")
    os.replace(path, final)
    return final


# ─────────────────────────────────────────────
#  Download (mega / direct)
# ─────────────────────────────────────────────
async def _download_mega(url: str, work: str, quality: str, prog) -> str | None:
    from ..utils.mega_py import MegaError, download_public_file
    shim = _DlShim()
    if prog is not None:
        prog.setdefault("dls", {})[quality] = shim

    async def _cb(done, total, speed, name):
        shim.done, shim.total = int(done), int(total)

    try:
        path = await download_public_file(url, work, progress=_cb, cancel=(prog or {}).get("cancel"))
    except MegaError as e:
        if _cancelled(prog):
            raise _Cancelled()
        LOGGER.warning(f"[RTI-FB] mega download fail: {str(e)[:150]}")
        return None
    finally:
        shim.fin = True
    if path and os.path.isfile(path) and os.path.getsize(path) > 0:
        shim.ok = True
        return path
    return None


async def _download_direct(url: str, work: str, quality: str, prog) -> str | None:
    from ..utils.turbo_download import TurboDL
    from .mega_url import _best_filename
    from .url_upload import _safe_filename
    from . import rti_downloader as rd

    try:
        name = _safe_filename(await _best_filename(url))
    except Exception:
        name = "downloaded_file"
    dest = os.path.join(work, name)
    headers = {"User-Agent": rd.HEADERS.get("User-Agent", "Mozilla/5.0")}
    dl = TurboDL(url, dest, progress_bar=False, threads=_prof.DOWNLOAD_THREADS,
                 request_args={"headers": headers}, timeout=30)
    dl.start(blocking=False)
    if prog is not None:
        prog.setdefault("dls", {})[quality] = dl
    try:
        while not dl.isFinished():
            if _cancelled(prog):
                dl.stop()
                raise _Cancelled()
            await asyncio.sleep(0.5)
    except asyncio.CancelledError:
        dl.stop()
        raise
    if dl.isSuccessful():
        real = dl.get_dest() or dest
        if os.path.isfile(real) and os.path.getsize(real) > 0:
            return real
    LOGGER.warning(f"[RTI-FB] direct download fail: {str(dl.get_errors())[:150]} | {url[:80]}")
    return None


# ─────────────────────────────────────────────
#  Ek server: bypass -> download
# ─────────────────────────────────────────────
async def _try_kind(kind: str, page_url: str, ep: int, quality: str, work: str, tr, prog):
    """Returns downloaded filepath ya None. _Cancelled / _WrongQuality bahar aate hain."""
    from . import rti_bypass as rb
    from . import rti_mega as rm

    t0 = time.time()
    LOGGER.info(f"[RTI-FB] ▶ {kind}: bypass shuru (Ep {ep})")
    try:
        res = await asyncio.wait_for(
            job_ctl.run_in_thread(rm.resolve_blocking, page_url, ep, [kind], tr, rb.post_hop),
            timeout=_RESOLVE_TIMEOUT,
        )
    except asyncio.TimeoutError:
        _mark_fail(kind, "bypass timeout", _COOLDOWN_RESOLVE)
        return None
    except Exception as e:  # noqa: BLE001
        LOGGER.error(f"[RTI-FB] {kind} resolve error: {e!r}\n{traceback.format_exc()}")
        _mark_fail(kind, f"bypass error {type(e).__name__}", _COOLDOWN_RESOLVE)
        return None
    t_resolve = time.time() - t0

    val = (res or {}).get(kind)
    if not isinstance(val, tuple):
        _mark_fail(kind, "final link nahi mila", _COOLDOWN_RESOLVE)
        return None
    ftype, furl = val
    if _cancelled(prog):
        raise _Cancelled()

    cands = [furl] + [a for a in rb._ALT.get(furl, []) if a != furl]     # hubcloud ke backup servers
    t1 = time.time()
    for i, u in enumerate(cands):
        if i:
            LOGGER.info(f"[RTI-FB] {kind}: backup link #{i} try")
        try:
            if ftype == "mega" or "mega.nz" in u or "mega.co.nz" in u:
                path = await _download_mega(u, work, quality, prog)
            else:
                path = await _download_direct(u, work, quality, prog)
        except _Cancelled:
            raise
        except Exception as e:  # noqa: BLE001
            LOGGER.error(f"[RTI-FB] {kind} download error: {e!r}")
            path = None
        if path:
            _mark_ok(kind, t_resolve, time.time() - t1)
            return path
        if _cancelled(prog):
            raise _Cancelled()
    _mark_fail(kind, "download fail", _COOLDOWN_DOWNLOAD)
    return None


# ─────────────────────────────────────────────
#  PUBLIC: missing quality nikaalo
# ─────────────────────────────────────────────
async def fetch_missing(page_url: str, ep: int, quality: str, dest_dir: str,
                        prog: dict = None, note=None) -> str | None:
    """
    RTI page ke mirrors se `quality` ki file download karo (speed-order mein, fail pe agla).
    prog: poller/pipeline ka progress dict ("cancel" padha jaata hai, "dls" mein progress likha jaata hai).
    note(text): optional status callback.
    Returns: dest_dir mein final filepath (naam mein quality tag ke saath) ya None.
    """
    key = (page_url, int(ep), quality)
    now = time.time()
    if _NEG.get(key, 0) > now:
        LOGGER.info(f"[RTI-FB] Ep {ep} {quality}: {int(_NEG[key] - now)}s cooldown baaki — skip")
        return None
    if key in _RUNNING:
        LOGGER.info(f"[RTI-FB] Ep {ep} {quality}: already chal raha hai — skip")
        return None
    _RUNNING.add(key)

    from . import rti_mega as rm
    os.makedirs(dest_dir, exist_ok=True)
    work = os.path.join(dest_dir, f"_fb_{uuid.uuid4().hex[:6]}")      # sub-folder: swift ka done-scan ise nahi dekhta
    os.makedirs(work, exist_ok=True)
    tr = rm.Trace(os.path.join(download_dir, "_rtifb", uuid.uuid4().hex[:8]))
    order = ordered_kinds()
    LOGGER.info(f"[RTI-FB] Ep {ep} {quality} Swift pe nahi — mirror order: {' → '.join(order)}")
    t_all = time.time()
    try:
        for n, kind in enumerate(order, 1):
            if _cancelled(prog):
                return None
            if note:
                try:
                    note(f"🔁 {quality}: {kind} try ({n}/{len(order)})")
                except Exception:
                    pass
            try:
                path = await _try_kind(kind, page_url, ep, quality, work, tr, prog)
                if not path:
                    continue
                final = _finalize_file(path, quality, dest_dir)
            except _Cancelled:
                return None
            except _WrongQuality as wq:
                _NEG[key] = time.time() + _WRONG_QUALITY_BLOCK
                LOGGER.warning(f"[RTI-FB] ⚠️ {kind} ki file {wq.got} nikli ({quality} chahiye) — "
                               f"mirrors same file dete hain, chain band (1h skip)")
                return None
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                LOGGER.error(f"[RTI-FB] {kind} finalize error: {e!r}\n{traceback.format_exc()}")
                _mark_fail(kind, "finalize error", _COOLDOWN_DOWNLOAD)
                continue
            _NEG.pop(key, None)
            LOGGER.info(f"[RTI-FB] 🎯 Ep {ep} {quality} mila via {kind} ({time.time() - t_all:.0f}s): "
                        f"{os.path.basename(final)}")
            return final
        _NEG[key] = time.time() + _RETRY_AFTER_FAIL
        LOGGER.warning(f"[RTI-FB] Ep {ep} {quality}: koi bhi mirror se nahi mila ({time.time() - t_all:.0f}s)")
        return None
    finally:
        _RUNNING.discard(key)
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(tr.workdir, ignore_errors=True)


# ─────────────────────────────────────────────
#  /rtiorder
# ─────────────────────────────────────────────
@Client.on_message(filters.command("rtiorder"))
async def rtiorder_command(client: Client, message):
    c = await check_chat(message, chat="Sudo")
    if not c:
        return
    now = time.time()
    lines = ["<b>🔁 RTI mirror fallback</b>",
             f"Status: <code>{'ON' if enabled() else 'OFF'}</code> • Qualities: <code>{', '.join(fallback_qualities())}</code>",
             "", "<b>Try-order (tez → dheema):</b>"]
    for i, k in enumerate(ordered_kinds(), 1):
        s = _STATS.get(k) or {}
        cool = max(0, int(_FAIL_UNTIL.get(k, 0) - now))
        seed = _SEED.get(k, 0)
        live = ""
        if s.get("last_resolve") is not None:
            live = f" • live: bypass {s['last_resolve']:.0f}s + dl {s['last_dl']:.0f}s"
        lines.append(f"{i}. <b>{k}</b> — seed {seed:.0f}s{live} • ✅{s.get('ok', 0)} ❌{s.get('fail', 0)}"
                     + (f" • ⏸ {cool // 60}m peeche" if cool else ""))
    lines.append("\nOrder badalna ho: env <code>RTI_FALLBACK_ORDER=mega,hubcloud,watchnow,dlbeta</code>")
    await message.reply("\n".join(lines), parse_mode=ParseMode.HTML)
