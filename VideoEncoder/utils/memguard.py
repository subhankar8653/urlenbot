"""
memguard.py — RAM watchdog + "bot kyun restart hua" ka sabut
=============================================================
Problem: download/upload ke beech bot bina warning restart ho jata tha. Railway/Docker
OOM-kill ya crash mein process ko SIGKILL milta hai — koi log nahi, koi message nahi.

Yeh module:
  1. Har 5s container RAM (anon + shmem) dekhta hai. > MEMGUARD_HIGH% (default 80) => PRESSURE=True:
       - gc + malloc_trim + purane/orphan Chrome kill
       - naye downloads light engine (single-connection stream) pe jaate hain
       - /adh prefetch Chrome nahi kholta
     Wapas MEMGUARD_LOW% (default 65) se neeche aane par PRESSURE=False.
  2. Busy hone par har 30s log mein RAM + (bot / chrome / ffmpeg) breakdown print karta hai.
  3. State file (/tmp) mein "running" likhta hai, normal exit/restart par "exited". Naye start par agar
     "running" mila => pichla exit UNCLEAN tha (OOM-kill/crash) — owner ko last RAM reading ke saath
     Telegram message jaata hai. "exited" => clean (redeploy / /restart / janitor), koi spam nahi.
Env: MEMGUARD=0 (band), MEMGUARD_HIGH, MEMGUARD_LOW, LIGHT_DL=1 (hamesha light engine).
"""
import asyncio
import atexit
import json
import logging
import os
import time

LOGGER = logging.getLogger(__name__)

ENABLED = (os.getenv("MEMGUARD", "1").strip() != "0")
HIGH = float(os.getenv("MEMGUARD_HIGH", "80") or 80)
LOW = float(os.getenv("MEMGUARD_LOW", "65") or 65)
STATE_FILE = os.path.join(os.getenv("TMPDIR", "/tmp"), "suhani_memguard.json")

PRESSURE = False
_last = {"pct": 0.0, "used_mb": 0, "limit_mb": 0}
_peak = {"pct": 0.0, "at": 0.0}


def light_forced() -> bool:
    return (os.getenv("LIGHT_DL") or "").strip().lower() in ("1", "true", "yes", "on")


def use_light_engine() -> bool:
    return light_forced() or PRESSURE


def _usage():
    from . import janitor
    used, limit = janitor.container_mem()
    try:
        sh = janitor._stat("shmem", "total_shmem")   # tmpfs / /dev/shm bhi RAM hi hai
        if sh > 0:
            used += sh
    except Exception:
        pass
    return used, limit


def _write_state(state: str, extra: dict | None = None):
    try:
        d = {"state": state, "ts": time.time(), "peak_pct": round(_peak["pct"], 1),
             "peak_at": _peak["at"], **_last}
        if extra:
            d.update(extra)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f)
        os.replace(tmp, STATE_FILE)
    except Exception:
        pass


def mark_exit(reason: str = "normal"):
    """Planned exit/restart (execv bhi) — isse 'unclean restart' alert nahi jaata."""
    _write_state("exited", {"reason": reason})


atexit.register(mark_exit, "atexit")


def _relieve():
    from . import janitor
    try:
        janitor.trim_memory()
    except Exception:
        pass
    try:
        k = janitor.kill_orphan_browsers(120)
        if k:
            LOGGER.warning(f"[MemGuard] {k} purane chrome kill kiye")
    except Exception:
        pass


async def check_previous(app, owners):
    """Start par: pichla exit unclean tha to owner ko batao."""
    try:
        with open(STATE_FILE) as f:
            d = json.load(f)
    except Exception:
        return
    if d.get("state") != "running":
        return
    age = time.time() - float(d.get("ts") or 0)
    if age > 6 * 3600:
        return
    txt = (f"⚠️ <b>Bot unclean restart hua</b> (koi shutdown signal nahi mila — OOM-kill / crash jaisa)\n\n"
           f"Last reading ({int(age)}s pehle): RAM <b>{d.get('pct', 0):.0f}%</b> "
           f"({d.get('used_mb', 0)}/{d.get('limit_mb', 0)} MB)\n"
           f"Peak: <b>{d.get('peak_pct', 0):.0f}%</b> | busy: {d.get('busy')}\n"
           f"Breakdown: <code>{d.get('breakdown', '-')}</code>\n\n"
           f"Peak 90%+ ho to RAM hi wajah hai (Railway plan/limit dekho). Peak kam ho to Railway logs mein crash trace dekho.")
    for uid in owners[:1]:
        try:
            await app.send_message(uid, txt)
        except Exception:
            pass


async def _loop():
    global PRESSURE
    from . import janitor
    last_log = 0.0
    loop = asyncio.get_running_loop()
    while True:
        try:
            await asyncio.sleep(5)
            used, limit = await loop.run_in_executor(None, _usage)
            pct = used / limit * 100 if limit else 0.0
            _last.update(pct=round(pct, 1), used_mb=int(used / 1048576), limit_mb=int(limit / 1048576))
            if pct > _peak["pct"] or time.time() - _peak["at"] > 600:
                _peak.update(pct=pct, at=time.time())
            busy = janitor.is_busy()
            extra = {"busy": bool(busy)}
            now = time.time()
            if busy and now - last_log >= 30:
                last_log = now
                try:
                    bd = await loop.run_in_executor(None, janitor.mem_breakdown)
                except Exception:
                    bd = {}
                extra["breakdown"] = str(bd)[:300]
                LOGGER.info(f"[MemGuard] RAM {pct:.0f}% ({_last['used_mb']}/{_last['limit_mb']}MB) {bd}")
            _write_state("running", extra)
            if not PRESSURE and pct >= HIGH:
                PRESSURE = True
                LOGGER.warning(f"[MemGuard] RAM {pct:.0f}% >= {HIGH:.0f}% — light mode ON")
                await loop.run_in_executor(None, _relieve)
            elif PRESSURE and pct <= LOW:
                PRESSURE = False
                LOGGER.info(f"[MemGuard] RAM {pct:.0f}% — light mode OFF")
            elif PRESSURE and pct >= HIGH + 8:
                await loop.run_in_executor(None, _relieve)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            LOGGER.debug(f"[MemGuard] loop err: {e!r}")


def start():
    if not ENABLED:
        return
    asyncio.get_running_loop().create_task(_loop())
