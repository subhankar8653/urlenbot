"""
light_dl.py — kam-RAM / kam-connection download engine (All-Url-Uploader jaisa seedha stream)
==============================================================================================
Ek hi connection, 512KB chunks, seedha disk pe write, progress har ~3s. Memory/thread-pool ka
koi bojh nahi. RAM pressure (memguard) ya LIGHT_DL=1 par _download_url yahi use karta hai.
Beech mein connection toot jaye to Range se wahin se RESUME (0% se nahi).
"""
import asyncio
import os
import time

import aiohttp

from .. import LOGGER

CHUNK = 512 * 1024
TRIES = 4


def _h(n):
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.2f} {u}"
        n /= 1024


def _t(s):
    s = int(max(0, s))
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h}h {m}m {s}s" if h else (f"{m}m {s}s" if m else f"{s}s")


async def light_download(url: str, filepath: str, msg, headers: dict | None = None, label: str = "Light") -> str:
    headers = dict(headers or {})
    timeout = aiohttp.ClientTimeout(total=None, connect=30, sock_read=90)
    total = 0
    started = time.time()
    last_edit = 0.0
    last_err = None
    done = 0
    if os.path.exists(filepath):
        os.remove(filepath)

    for attempt in range(1, TRIES + 1):
        hdr = dict(headers)
        if done:
            hdr["Range"] = f"bytes={done}-"
        try:
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.get(url, headers=hdr, allow_redirects=True) as r:
                    if done and r.status != 206:      # server resume nahi karta => shuru se
                        done = 0
                        if os.path.exists(filepath):
                            os.remove(filepath)
                    r.raise_for_status()
                    if not total:
                        cl = int(r.headers.get("Content-Length", "0") or 0)
                        total = cl + done if r.status == 206 else cl
                    with open(filepath, "ab" if done else "wb") as f:
                        async for chunk in r.content.iter_chunked(CHUNK):
                            f.write(chunk)
                            done += len(chunk)
                            now = time.time()
                            if now - last_edit >= 3:
                                last_edit = now
                                el = max(now - started, 1e-6)
                                sp = done / el
                                pct = (done / total * 100) if total else 0
                                bar = "█" * int(pct / 100 * 15) + "░" * (15 - int(pct / 100 * 15))
                                eta = _t((total - done) / sp) if total and sp else "-"
                                try:
                                    await msg.edit(
                                        f"<b>⬇️ Downloading...</b>\n\n{bar} {pct:.1f}%\n\n"
                                        f"📦 Size : {_h(done)} / {_h(total)}\n"
                                        f"⚡ Speed: {_h(sp)}/s\n⏳ ETA : {eta}\n🔧 Engine: {label} (1 conn)")
                                except Exception:
                                    pass
                            await asyncio.sleep(0)    # event loop ko saans do (bot atke nahi)
            if total and done < total:
                raise aiohttp.ClientPayloadError(f"incomplete {done}/{total}")
            return filepath
        except asyncio.CancelledError:
            raise
        except Exception as e:
            last_err = e
            LOGGER.warning(f"[LightDL] attempt {attempt}/{TRIES} fail at {_h(done)}: {e!r}")
            await asyncio.sleep(min(2 * attempt, 8))
    try:
        os.remove(filepath)
    except Exception:
        pass
    raise RuntimeError(f"Light download failed: {last_err}")
