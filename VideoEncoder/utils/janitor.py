"""
janitor.py — Railway ke liye auto-maintenance (storage / RAM / zombie cleanup)

Problem: Railway ka disk ephemeral hota hai. 20-25 episode ke baad
download/encode folders, chrome temp, logs aur zombie processes jama hokar
disk/RAM bhar dete the -> bot hang. Redeploy se sab wipe hota tha.

Yeh module wahi "redeploy" kaam bot ke andar khud karta hai:
  1. Stale files/folders hatata hai (download_dir, encode_dir, /tmp chrome junk)
  2. Disk kam padne par aggressive cleanup
  3. Orphan chrome/chromedriver kill + zombie reap
  4. gc + malloc_trim se RAM wapas OS ko
  5. Bot idle ho aur RAM/disk phir bhi zyada ho to khud soft-restart
     (process re-exec, Railway deploy/credits ka koi extra kharch nahi)
"""

import asyncio
import gc
import glob
import logging
import os
import shutil
import sys
import time

import psutil

from .. import app, data, download_dir, encode_dir, owner

LOGGER = logging.getLogger(__name__)

# ── Tunables (env se override ho sakte hain) ────────────────────────────
CHECK_EVERY = int(os.getenv("JANITOR_INTERVAL", "180"))            # sec
STALE_AFTER = int(os.getenv("JANITOR_STALE_MIN", "30")) * 60       # idle itne der => kachra
LOW_DISK_GB = float(os.getenv("JANITOR_LOW_DISK_GB", "2.0"))       # isse kam free => aggressive
LOW_DISK_PCT = float(os.getenv("JANITOR_LOW_DISK_PCT", "85"))
AGGRESSIVE_IDLE = 180                                              # sec
MEM_RESTART_PCT = float(os.getenv("JANITOR_MEM_RESTART_PCT", "88")) # container RAM %
MAX_UPTIME_H = float(os.getenv("JANITOR_MAX_UPTIME_H", "24"))       # idle hone par refresh
CHROME_MAX_AGE = 45 * 60
IDLE_BEFORE_RESTART = 600                                          # sec continuous idle

_idle_since = None
_started = False


# ── helpers ─────────────────────────────────────────────────────────────
def _entry_mtime(path: str) -> float:
    """Entry (file/dir) ke andar sabse naya mtime."""
    try:
        newest = os.path.getmtime(path)
        if os.path.isdir(path):
            for root, _dirs, files in os.walk(path):
                for f in files:
                    try:
                        newest = max(newest, os.path.getmtime(os.path.join(root, f)))
                    except OSError:
                        pass
        return newest
    except OSError:
        return time.time()


def _size(path: str) -> int:
    try:
        if os.path.isfile(path):
            return os.path.getsize(path)
        total = 0
        for root, _d, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        return total
    except OSError:
        return 0


def _remove(path: str) -> int:
    sz = _size(path)
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path, ignore_errors=True)
        else:
            os.remove(path)
    except OSError:
        return 0
    return sz


def _proc_running(names) -> bool:
    for p in psutil.process_iter(["name"]):
        try:
            n = (p.info["name"] or "").lower()
            if any(x in n for x in names):
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return False


def disk_stats():
    try:
        u = shutil.disk_usage(download_dir if os.path.isdir(download_dir) else ".")
        return u.free, u.used / u.total * 100
    except Exception:
        return 10**12, 0.0


def container_mem():
    """(used_bytes, limit_bytes) — Railway container ka asli limit (cgroup)."""
    for cur, lim in (("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory.max"),
                     ("/sys/fs/cgroup/memory/memory.usage_in_bytes",
                      "/sys/fs/cgroup/memory/memory.limit_in_bytes")):
        try:
            used = int(open(cur).read().strip())
            raw = open(lim).read().strip()
            limit = int(raw) if raw.isdigit() else psutil.virtual_memory().total
            limit = min(limit, psutil.virtual_memory().total)
            return used, limit
        except Exception:
            continue
    vm = psutil.virtual_memory()
    return vm.used, vm.total


def is_busy() -> bool:
    """Koi bhi task chal raha hai? (queue, auto-upload, ffmpeg)"""
    if data:
        return True
    try:
        from ..plugins.upload_control import _active
        if _active:
            return True
    except Exception:
        pass
    return _proc_running(("ffmpeg", "megadl", "megatools"))


# ── cleanup steps ───────────────────────────────────────────────────────
def clean_dirs(min_idle: int) -> int:
    """download_dir / encode_dir ki wo entries hatao jo min_idle sec se idle hain."""
    freed, now = 0, time.time()
    for base in (download_dir, encode_dir):
        if not base or not os.path.isdir(base):
            continue
        for name in os.listdir(base):
            path = os.path.join(base, name)
            if now - _entry_mtime(path) > min_idle:
                freed += _remove(path)
    return freed


def clean_tmp() -> int:
    freed, now = 0, time.time()
    patterns = ("/tmp/.org.chromium.*", "/tmp/.com.google.Chrome.*", "/tmp/scoped_dir*",
                "/tmp/chrome_*", "/tmp/tmp*", "/tmp/*.jpg", "/tmp/*.png",
                "/tmp/rust_mozprofile*", "/tmp/Crashpad*")
    for pat in patterns:
        for path in glob.glob(pat):
            if now - _entry_mtime(path) > 30 * 60:
                freed += _remove(path)
    return freed


def clean_logs():
    """Purane rotated log files hatao + bada log truncate karo (safety)."""
    for pat in ("VideoEncoder/utils/extras/logs.txt.*", "log.txt.*"):
        for p in glob.glob(pat):
            _remove(p)
    for p in ("log.txt", "VideoEncoder/utils/extras/logs.txt"):
        try:
            if os.path.exists(p) and os.path.getsize(p) > 20 * 1024 * 1024:
                open(p, "w").close()
        except OSError:
            pass


def kill_orphan_browsers() -> int:
    """Purane chrome/chromedriver kill + un-reaped chrome zombies saaf."""
    killed, now = 0, time.time()
    me = os.getpid()
    for p in psutil.process_iter(["pid", "name", "create_time", "status", "ppid"]):
        try:
            name = (p.info["name"] or "").lower()
            if not any(x in name for x in ("chrom", "crashpad")):
                continue
            if p.info["status"] == psutil.STATUS_ZOMBIE:
                if p.info["ppid"] == me:
                    try:
                        os.waitpid(p.info["pid"], os.WNOHANG)
                    except (ChildProcessError, OSError):
                        pass
                continue
            if now - p.info["create_time"] > CHROME_MAX_AGE:
                p.kill()
                killed += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return killed


def trim_memory():
    gc.collect()
    try:
        import ctypes
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass


def run_cleanup(force: bool = False) -> dict:
    """Ek poora cleanup round. force=True => /cleanup command (idle>60s sab hatao)."""
    free, pct = disk_stats()
    low = free < LOW_DISK_GB * 1024 ** 3 or pct > LOW_DISK_PCT
    idle_limit = 60 if force else (AGGRESSIVE_IDLE if low else STALE_AFTER)
    freed = clean_dirs(idle_limit) + clean_tmp()
    clean_logs()
    killed = kill_orphan_browsers()
    trim_memory()
    free2, pct2 = disk_stats()
    return {"freed": freed, "killed": killed, "free": free2, "pct": pct2, "low": low}


# ── soft restart ────────────────────────────────────────────────────────
async def soft_restart(reason: str):
    LOGGER.warning(f"[Janitor] Soft restart: {reason}")
    try:
        for uid in owner[:1]:
            await app.send_message(uid, f"<b>♻️ Auto-refresh:</b> {reason}\nBot 5 sec mein wapas aa raha hai.")
    except Exception:
        pass
    try:
        await app.stop()
    except Exception:
        pass
    # purana sab saaf — naya process fresh disk par shuru ho
    for base in (download_dir, encode_dir):
        try:
            for n in os.listdir(base):
                _remove(os.path.join(base, n))
        except OSError:
            pass
    kill_orphan_browsers()
    os.execv(sys.executable, [sys.executable, "-m", "VideoEncoder"])


# ── main loop ───────────────────────────────────────────────────────────
async def janitor_loop():
    global _idle_since
    LOGGER.info("[Janitor] started")
    while True:
        try:
            await asyncio.sleep(CHECK_EVERY)
            res = await asyncio.get_running_loop().run_in_executor(None, run_cleanup)
            if res["freed"] or res["killed"]:
                LOGGER.info(f"[Janitor] freed={res['freed']/1024**2:.0f}MB chrome_killed={res['killed']}")

            busy = is_busy()
            _idle_since = None if busy else (_idle_since or time.time())
            idle_for = 0 if _idle_since is None else time.time() - _idle_since
            if idle_for < IDLE_BEFORE_RESTART:
                continue

            used, limit = container_mem()
            mem_pct = used / limit * 100 if limit else 0
            free, pct = disk_stats()
            uptime_h = (time.time() - psutil.Process(os.getpid()).create_time()) / 3600

            if mem_pct > MEM_RESTART_PCT:
                await soft_restart(f"RAM {mem_pct:.0f}% (idle) — memory refresh")
            elif free < 1 * 1024 ** 3:
                await soft_restart("Disk almost full — cleanup restart")
            elif uptime_h > MAX_UPTIME_H:
                await soft_restart(f"{uptime_h:.0f}h uptime — routine refresh")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            LOGGER.error(f"[Janitor] loop error: {e}")


def start_janitor():
    global _started
    if _started:
        return
    _started = True
    asyncio.get_running_loop().create_task(janitor_loop())
