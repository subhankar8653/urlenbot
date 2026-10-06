"""
prefetch.py — pipeline: current file encode/upload ho rahi ho, tab tak queue ki
AGLI Telegram file background mein download ho jaye.

Pehle:  download(A) -> encode(A) -> upload(A) -> download(B) -> encode(B) ...
Ab   :  download(A) -> encode(A) -> upload(A) -> encode(B) (B pehle se ready) ...
                          \\__ download(B) isi beech chalta rehta hai __/

Safe design:
  * Sirf profile.PREFETCH=1 par (mid/big tier = VPS). Env: PREFETCH_NEXT=0/1
  * Sirf queue ka agla (data[1]) item, aur sirf Telegram video/document (/dl ya auto-encode)
  * Disk free kam ho to skip. Koi bhi error aaye to chupchap normal download pe fallback
  * Prefetch file alag folder (download_dir/pf_<msgid>/) mein, taaki naam na takraye
"""
import asyncio
import os
import shutil
import time

from .. import LOGGER, data, download_dir, profile as _prof, video_mimetype
from .fast_download import fast_download

# message.id -> dict(task, dir, path, state, name)
_JOBS = {}
PF_PREFIX = "pf_"
READY = False        # current task ka download ho chuka hai (tabhi prefetch, taaki bandwidth na bate)


def _key(message):
    return (message.chat.id, message.id)


def resolve_target(message, mode="no_reply"):
    """Download karne wala asli message + file ka naam (handle_tg_down ke saath same logic)."""
    target = message
    if message.reply_to_message and (message.reply_to_message.video or message.reply_to_message.document):
        target = message.reply_to_message
    elif message.video or message.document:
        target = message
    elif mode == "reply" and message.reply_to_message:
        target = message.reply_to_message
    else:
        return None, None
    if target.video:
        fname = target.video.file_name or f"video_{int(time.time())}.mp4"
    elif target.document:
        fname = target.document.file_name or f"file_{int(time.time())}"
    else:
        fname = f"file_{int(time.time())}"
    return target, fname


def _is_plain_tg_job(message):
    """Kya yeh queue item normal 'tg' mode (download -> encode) mein chalega?"""
    text = message.text or message.caption
    if text:
        cmd = text.split(None, 1)[0].lower()
        if "/ddl" in cmd or "/batch" in cmd or "/af" in cmd:
            return False
        if "/dl" not in cmd and not (message.video or message.document):
            return False
    if message.document and message.document.mime_type not in video_mimetype \
            and not (message.reply_to_message and (message.reply_to_message.video or message.reply_to_message.document)):
        return False
    return True


def _disk_ok(size):
    try:
        free = shutil.disk_usage(download_dir).free
    except Exception:
        return False
    # file + encode output ke liye jagah + 2GB safety
    return free > size * 2.2 + 2 * 1024 ** 3


def _rm(path):
    shutil.rmtree(path, ignore_errors=True)


async def _run(job, target, client):
    def _cb(done, total):
        job["done"], job["total"] = done, total

    try:
        os.makedirs(job["dir"], exist_ok=True)
        path = await fast_download(client=client, message=target, file_name=job["path"],
                                   progress_callback=_cb, progress_args=())
        job["ok"] = bool(path) and os.path.isfile(job["path"])
        return job["path"] if job["ok"] else None
    except asyncio.CancelledError:
        raise
    except Exception as e:
        LOGGER.warning(f"[Prefetch] fail: {e!r}")
        job["ok"] = False
        return None


def mark_downloaded():
    """Current task ka download khatam hote hi bulao."""
    global READY
    READY = True
    schedule_next()


def reset():
    global READY
    READY = False


def schedule_next():
    """Agla item (data[1]) background mein download shuru karta hai."""
    if not READY or not _prof.PREFETCH or len(data) < 2:
        return
    nxt = data[1]
    k = _key(nxt)
    if k in _JOBS or not _is_plain_tg_job(nxt):
        return
    target, fname = resolve_target(nxt)
    if not target:
        return
    media = target.video or target.document
    size = getattr(media, "file_size", 0) or 0
    if size <= 0 or not _disk_ok(size):
        return
    d = os.path.join(download_dir, f"{PF_PREFIX}{nxt.id}")
    job = {"dir": d, "path": os.path.join(d, fname), "name": fname, "done": 0, "total": size, "ok": False}
    job["task"] = asyncio.ensure_future(_run(job, target, nxt._client))
    _JOBS[k] = job
    LOGGER.info(f"[Prefetch] agli file background mein: {fname} ({size / 1024 ** 2:.0f} MB)")


async def take(message, msg, final_path):
    """Agar is message ki file prefetch ho chuki/ho rahi hai to use final_path par le aao.
    Wapas: final_path (success) ya None (normal download karo)."""
    job = _JOBS.pop(_key(message), None)
    if not job:
        return None
    try:
        last = 0
        while not job["task"].done():
            await asyncio.wait({job["task"]}, timeout=4)
            if not job["task"].done() and time.time() - last > 4:
                last = time.time()
                pct = job["done"] * 100 / max(1, job["total"])
                try:
                    await msg.edit(f"<b>⚡ Pre-download finish ho raha hai... {pct:.0f}%</b>")
                except Exception:
                    pass
        path = job["task"].result()
        if path and os.path.isfile(path):
            os.replace(path, final_path)           # same disk => instant move
            return final_path
        return None
    except Exception as e:
        LOGGER.warning(f"[Prefetch] take fail: {e!r}")
        return None
    finally:
        _rm(job["dir"])


def drop_unneeded(keep_message=None):
    """Queue clear/cancel/skip hone par jo prefetch ab kaam ka nahi use hatao."""
    keep = _key(keep_message) if keep_message is not None else None
    for k in list(_JOBS):
        if k == keep:
            continue
        job = _JOBS.pop(k)
        try:
            job["task"].cancel()
        except Exception:
            pass
        _rm(job["dir"])


def is_protected(name):
    """delete_downloads() in folders ko na chhuye (abhi chal rahe prefetch ke)."""
    return name.startswith(PF_PREFIX) and any(
        os.path.basename(j["dir"]) == name for j in _JOBS.values())
