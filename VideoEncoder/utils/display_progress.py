import math
import time

from .. import PROGRESS


# ─────────────────────────────────────────────
#  Speed smoothing — last N readings ka average
# ─────────────────────────────────────────────
_speed_cache: dict = {}   # task_id → list of (time, bytes)
_MAX_SAMPLES = 6          # kitni readings average mein leni hain
_UPDATE_EVERY = 4         # seconds mein update interval


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

async def progress_for_pyrogram(current: int, total: int, ud_type: str, message, start: float):
    now = time.time()
    elapsed = now - start
    task_id = f"{message.id}_{ud_type}"

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
async def progress_for_url(downloader, msg):
    try:
        total      = downloader.filesize if downloader.filesize else 0
        downloaded = downloader.get_dl_size()
        speed_str  = downloader.get_speed(human=True)
        eta_str    = downloader.get_eta(human=True)
        pct        = downloader.get_progress() * 100
        bar        = _make_bar(pct)
        text = (
            f"⬇️ **Downloading...**\n\n"
            f"`{bar}` **{pct:.1f}%**\n\n"
            f"📦 **Size :** `{humanbytes(downloaded)}` / `{humanbytes(total)}`\n"
            f"⚡ **Speed:** `{speed_str}`\n"
            f"⏳ **ETA :** `{eta_str}`"
        )
        await msg.edit_text(text)
    except Exception:
        pass
