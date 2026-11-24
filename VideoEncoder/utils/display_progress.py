import asyncio
import math
import time

from .. import PROGRESS


# ─────────────────────────────────────────────
#  Speed smoothing — last N readings ka average
# ─────────────────────────────────────────────
_speed_cache: dict = {}   # task_id → list of (time, bytes)
_MAX_SAMPLES = 6          # kitni readings average mein leni hain
_UPDATE_EVERY = 3         # seconds mein update interval (3s: bar zyada responsive, flood-safe)


def _smooth_speed(task_id: str, current: int, now: float) -> float:
    if task_id not in _speed_cache:
        _speed_cache[task_id] = []
    samples = _speed_cache[task_id]
    samples.append((now, current))
    if len(samples) > _MAX_SAMPLES:
        samples.pop(0)
    if len(samples) < 2:
        return 0.0
    t0, b0 = samples[0]
    t1, b1 = samples[-1]
    dt = t1 - t0
    if dt <= 0:
        return 0.0
    return (b1 - b0) / dt


def _cleanup_speed(task_id: str):
    _speed_cache.pop(task_id, None)


# ─────────────────────────────────────────────
#  Human-readable helpers
# ─────────────────────────────────────────────
def humanbytes(size: float) -> str:
    if not size:
        return "0 B"
    power = 1024
    n = 0
    labels = {0: "B", 1: "KB", 2: "MB", 3: "GB", 4: "TB"}
    while size >= power and n < 4:
        size /= power
        n += 1
    return f"{size:.2f} {labels[n]}"


def TimeFormatter(seconds: float) -> str:
    seconds = int(seconds)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    parts = []
    if days:    parts.append(f"{days}d")
    if hours:   parts.append(f"{hours}h")
    if minutes: parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)


# ─────────────────────────────────────────────
#  Progress bar — 15 blocks
# ─────────────────────────────────────────────
_BAR_LEN = 15

def _make_bar(pct: float) -> str:
    filled = int(_BAR_LEN * pct / 100)
    return "█" * filled + "░" * (_BAR_LEN - filled)


# ─────────────────────────────────────────────
#  Main progress callback (Pyrogram-compatible)
# ─────────────────────────────────────────────
_last_update: dict = {}

async def _progress_for_pyrogram_impl(current: int, total: int, ud_type: str, message, start: float):
    now = time.time()
    elapsed = now - start
    task_id = f"{message.id}_{ud_type}"
    if message.id in _DONE_MSGS:          # upload khatam / status message finish ho chuka — purana edit ab nahi
        return

    # Throttle updates
    if current != total:
        if now - _last_update.get(task_id, 0) < _UPDATE_EVERY:
            return
    _last_update[task_id] = now

    try:
        if total == 0:
            return

        pct   = current * 100 / total
        speed = _smooth_speed(task_id, current, now)
        eta   = (total - current) / speed if speed > 0 else 0
        bar   = _make_bar(pct)

        speed_mbps = speed / (1024 * 1024)
        if speed_mbps >= 5:
            speed_icon = "🚀"
        elif speed_mbps >= 2:
            speed_icon = "⚡"
        elif speed_mbps >= 0.5:
            speed_icon = "📶"
        else:
            speed_icon = "🐢"

        text = (
            f"**{ud_type}**\n\n"
            f"`{bar}` **{pct:.1f}%**\n\n"
            f"📦 **Size :** `{humanbytes(current)}` / `{humanbytes(total)}`\n"
            f"{speed_icon} **Speed:** `{humanbytes(speed)}/s`\n"
            f"⏱ **Elapsed:** `{TimeFormatter(elapsed)}`\n"
            f"⏳ **ETA :** `{TimeFormatter(eta) if speed > 0 else 'Calculating...'}`"
        )

        if current == total:
            # 100% ho gaya — Telegram server pe finalize hogi file
            # User ko dikhao ki processing chal rahi hai (stuck nahi hai)
            _cleanup_speed(task_id)
            _last_update.pop(task_id, None)
            try:
                finalizing_text = (
                    f"**{ud_type}**\n\n"
                    f"`{'█' * 15}` **100.0%**\n\n"
                    f"📦 **Size :** `{humanbytes(total)}` / `{humanbytes(total)}`\n"
                    f"⏳ **Finalizing...** (Telegram processing)"
                )
                await message.edit(text=finalizing_text)
            except Exception:
                pass
        else:
            await message.edit(text=text)

    except Exception:
        pass


# ─────────────────────────────────────────────
#  URL downloader progress
# ─────────────────────────────────────────────
_URL_LAST: dict = {}     # msg id -> last edit time


async def progress_for_url(downloader, msg):
    # THROTTLE: callers `while not isFinished(): await progress_for_url(...)` tight loop chalate hain —
    # pehle har iteration pe Telegram edit hota tha (FloodWait + faltu CPU). Ab 2.5s mein ek edit,
    # beech mein halka sleep.
    try:
        _k = getattr(msg, "id", None) or id(msg)
        _now = time.time()
        if _now - _URL_LAST.get(_k, 0) < 2.5:
            await asyncio.sleep(0.5)
            return
        _URL_LAST[_k] = _now
        if len(_URL_LAST) > 200:
            for _o in sorted(_URL_LAST, key=_URL_LAST.get)[:100]:
                _URL_LAST.pop(_o, None)
    except Exception:
        pass
    try:
        total      = downloader.filesize if downloader.filesize else 0
        downloaded = downloader.get_dl_size()
        speed_str  = downloader.get_speed(human=True)
        eta_str    = downloader.get_eta(human=True)
        pct        = downloader.get_progress() * 100
        bar        = _make_bar(pct)
        engine_line = ""
        try:                                   # TurboDL: "Turbo 12/16 conn" ya "SmartDL fallback (reason)"
            _gi = getattr(downloader, "get_info", None)
            if _gi:
                engine_line = f"\n🔧 **Engine:** `{_gi()}`"
        except Exception:
            pass
        text = (
            f"⬇️ **Downloading...**\n\n"
            f"`{bar}` **{pct:.1f}%**\n\n"
            f"📦 **Size :** `{humanbytes(downloaded)}` / `{humanbytes(total)}`\n"
            f"⚡ **Speed:** `{speed_str}`\n"
            f"⏳ **ETA :** `{eta_str}`"
            f"{engine_line}"
        )
        await msg.edit_text(text)
    except Exception:
        pass


# ─────────────────────────────────────────────
#  SPEED FIX: progress_for_pyrogram ab NON-BLOCKING hai
#  Pehle upload/download ka data-pump har status edit (FloodWait / slow edit) ka
#  intezaar karta tha — isse speed shuru mein tez aur baad mein dheemi lagti thi.
#  Ab throttle check turant hota hai aur edit alag task mein jaata hai
#  (ek time pe ek hi per message). /url, /rename, /save, /upload — sab pe lagta hai.
# ─────────────────────────────────────────────
_PG_LATEST: dict = {}     # task_id -> sabse naya (current, total, ud_type, message, start)
_PG_TASK: dict = {}       # task_id -> chalta hua runner task
_DONE_MSGS: dict = {}     # message.id -> time  (finish_progress_msg ke baad koi edit nahi)


async def progress_for_pyrogram(current: int, total: int, ud_type: str, message, start: float):
    """FIX (bar atka rehta tha / 80% pe ruk jaata tha):
       • naya edit hamesha sabse taza state ke saath (latest-wins) — purana der se pahunch kar naye ko overwrite nahi karta
       • 100% / final turant (throttle nahi)
       • finish_progress_msg() ke baad us message pe koi edit nahi
       • upload data-pump kabhi nahi rukta (edit alag task mein)"""
    try:
        mid = getattr(message, "id", None)
        if mid in _DONE_MSGS:
            return
        now = time.time()
        task_id = f"{mid}_{ud_type}"
        final = bool(total) and current >= total
        if not final and now - _last_update.get(task_id, 0) < _UPDATE_EVERY:
            return
    except Exception:
        return

    _PG_LATEST[task_id] = (current, total, ud_type, message, start)
    t = _PG_TASK.get(task_id)
    if t is not None and not t.done():
        return                      # chalta runner edit khatam hote hi sabse naya state utha lega

    async def _runner():
        try:
            while True:
                args = _PG_LATEST.pop(task_id, None)
                if args is None or args[3].id in _DONE_MSGS:
                    break
                try:
                    await _progress_for_pyrogram_impl(*args)
                except Exception:
                    pass
        finally:
            _PG_TASK.pop(task_id, None)

    _PG_TASK[task_id] = asyncio.ensure_future(_runner())


async def finish_progress_msg(message, delete: bool = True, text: str = None) -> bool:
    """Upload ke baad progress/status message ko pakka band karo:
       1) us message pe koi aur progress-edit nahi (late '80%' edit overwrite nahi karega)
       2) chalta edit khatam hone do
       3) delete (FloodWait pe 3 baar retry) — fail ho to 'complete' text se purana bar hata do."""
    import re as _re
    mid = getattr(message, "id", None)
    if mid is None:
        return False
    _DONE_MSGS[mid] = time.time()
    if len(_DONE_MSGS) > 500:
        for _k in sorted(_DONE_MSGS, key=_DONE_MSGS.get)[:250]:
            _DONE_MSGS.pop(_k, None)
    pref = f"{mid}_"
    for k in [k for k in list(_PG_TASK) if k.startswith(pref)]:
        t = _PG_TASK.get(k)
        if t is not None and not t.done():
            try:
                await asyncio.wait_for(asyncio.shield(t), 5)
            except Exception:
                t.cancel()
    for k in [k for k in list(_PG_LATEST) if k.startswith(pref)]:
        _PG_LATEST.pop(k, None)
    for k in [k for k in list(_speed_cache) if k.startswith(pref)]:
        _speed_cache.pop(k, None)
    for k in [k for k in list(_last_update) if k.startswith(pref)]:
        _last_update.pop(k, None)

    if delete:
        for _ in range(3):
            try:
                await message.delete()
                return True
            except Exception as e:
                es = str(e)
                if "MESSAGE_ID_INVALID" in es or "not found" in es.lower():
                    return True
                m = _re.search(r"wait of (\d+)", es)
                await asyncio.sleep(min(int(m.group(1)), 15) + 1 if m else 1.5)
        try:
            await message.edit("✅ Upload complete")
        except Exception:
            pass
        return False
    if text:
        try:
            await message.edit(text)
        except Exception:
            pass
    return True
