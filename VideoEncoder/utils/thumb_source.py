"""
thumb_source.py
================
Auto-thumbnail resolution jab user ka custom thumbnail (/setpic ya /thumb)
set NAHI hai.

Priority (sabhi upload paths mein — URL upload, mega, auto-channel-upload,
rename, save-restrict, waghera):
  1. User ka custom thumbnail (/setpic /thumb)  — already existing,
                                                   sabse pehle priority
                                                   (is module tak aata
                                                   hi nahi agar set hai)
  2. TMDB se related poster/backdrop            — NAYA (AniList fallback
                                                   hata diya gaya, sirf TMDB)
  3. Video se ffmpeg se nikala hua frame         — purana fallback, agar
                                                   TMDB pe kuch match na mile

Har case mein community band (@SBANIME, ya /community se set kiya naam)
consistent lagta hai — TMDB se aaya poster ho ya ffmpeg-frame, dono pe
same band text lagta hai.
"""

import asyncio
import hashlib
import logging
import os
import shutil
import time

from .anime_api import download_image, fetch_anime_details
from .auto_caption import extract_anime_info
from .encoding import DEFAULT_THUMB_BAND_TEXT, _add_thumb_username_band

LOGGER = logging.getLogger(__name__)

# ── SPEED NOTES ────────────────────────────────────────────────────────────────
# Pehle har file (aur swift mein 4 qualities ek saath) ke liye: TMDB search + poster
# download (original size, MBs) + PIL band + (cover ke liye) log channel pe send_photo
# — har baar, SAME anime ke liye. Ab:
#   * poster+band ek baar banta hai (master), baaki files uski chhoti copy leti hain
#   * parallel calls ek hi kaam share karti hain (in-flight dedupe)
#   * kuch na mile to 2 min tak dobara network nahi jaata (negative cache)
#   * prefetch_thumb() download ke dauran hi taiyar kar deta hai => upload ke time 0 wait
#   * cover file_id (send_photo) bhi thumb ke hash pe cache hota hai
_TTL = 1800.0          # master thumb cache
_NEG_TTL = 120.0       # "nahi mila" cache
_COVER_TTL = 6 * 3600.0
_MASTERS = {}          # key -> (ts, path | None)
_INFLIGHT = {}         # key -> Future
_COVERS = {}           # sha1 -> (ts, file_id)
_COVER_INFLIGHT = {}   # sha1 -> Future


def _style_sig():
    try:
        from .thumb_style import get_current_style
        st = get_current_style()
        return "off" if st is None else repr(st)
    except Exception:
        return "?"


def _cache_dir(dl_dir):
    # Stable dir (swift ka per-session dl_dir episode ke baad rmtree hota hai => cache mar jaata).
    try:
        from .. import download_dir as _dd
        base = os.path.join(_dd, "_thumbcache")
    except Exception:
        base = os.path.join(dl_dir or ".", ".thumb_master")
    d = os.path.join(base, "tmdb")
    os.makedirs(d, exist_ok=True)
    return d


async def _build_master(anime_name, dl_dir, band_text):
    details = await fetch_anime_details(anime_name)
    if not details or not details.get("image"):
        return None
    master = os.path.join(_cache_dir(dl_dir), f"m_{time.time_ns()}.jpg")
    ok = await download_image(details["image"], master)
    if not ok or not os.path.isfile(master) or os.path.getsize(master) == 0:
        return None
    # PIL band (CPU) event loop se bahar
    await asyncio.to_thread(_add_thumb_username_band, master, band_text or DEFAULT_THUMB_BAND_TEXT)
    LOGGER.info(f"[ThumbSource] TMDB thumbnail built for '{anime_name}' ({details.get('source')})")
    return master


async def _ensure_master(anime_name, dl_dir, band_text):
    key = (anime_name.lower(), band_text or DEFAULT_THUMB_BAND_TEXT, _style_sig())
    now = time.time()
    hit = _MASTERS.get(key)
    if hit:
        ts, path = hit
        if path is None and now - ts < _NEG_TTL:
            return None
        if path is not None and now - ts < _TTL and os.path.isfile(path):
            return path
    fut = _INFLIGHT.get(key)
    if fut is not None:
        try:
            return await asyncio.shield(fut)
        except Exception:
            return None
    fut = asyncio.get_running_loop().create_future()
    _INFLIGHT[key] = fut
    path = None
    try:
        path = await _build_master(anime_name, dl_dir, band_text)
        _MASTERS[key] = (time.time(), path)
        if len(_MASTERS) > 64:
            _MASTERS.pop(next(iter(_MASTERS)), None)
        if not fut.done():
            fut.set_result(path)
        return path
    except BaseException as e:
        if not fut.done():
            fut.set_exception(e if isinstance(e, Exception) else RuntimeError("cancelled"))
            fut.exception()
        if isinstance(e, asyncio.CancelledError):
            raise
        LOGGER.warning(f"[ThumbSource] TMDB thumbnail failed for '{anime_name}': {e}")
        return None
    finally:
        _INFLIGHT.pop(key, None)


def _anime_name_from(filepath):
    fname = os.path.basename(filepath)
    anime_name, _, _ = extract_anime_info(fname, {})
    anime_name = (anime_name or "").strip()
    return anime_name if len(anime_name) >= 2 else None


async def get_tmdb_thumbnail(filepath: str, dl_dir: str, band_text: str = None):
    """
    Filename se anime/movie ka naam nikal ke TMDB se poster/backdrop
    dhundo, download karke community band lagao.

    Kuch na mile ya koi bhi step fail ho toh None wapas — caller phir
    purane tarike se ffmpeg-frame pe fallback kare.

    Har call apni ALAG copy return karti hai (caller use ke baad delete karta hai,
    master cache mein safe rehta hai).
    """
    try:
        anime_name = _anime_name_from(filepath)
        if not anime_name:
            return None
        master = await _ensure_master(anime_name, dl_dir, band_text)
        if not master:
            return None
        os.makedirs(dl_dir, exist_ok=True)
        out_path = os.path.join(dl_dir, f"tmdb_thumb_{time.time_ns()}.jpg")
        await asyncio.to_thread(shutil.copyfile, master, out_path)
        return out_path
    except Exception as e:
        LOGGER.warning(f"[ThumbSource] TMDB thumbnail failed for '{filepath}': {e}")
        return None


def prefetch_thumb(filepath: str, dl_dir: str, band_text: str = None, cover_sender=None):
    """Fire-and-forget: download chal raha ho tab hi poster+band (aur agar cover_sender diya
    ho to cover file_id bhi) taiyar kar lo. filepath sirf naam ke liye hai."""
    async def _go():
        try:
            n = _anime_name_from(filepath)
            if n:
                master = await _ensure_master(n, dl_dir, band_text)
                if master and cover_sender is not None:
                    await cached_cover_id(master, cover_sender)
        except Exception:
            pass
    try:
        return asyncio.ensure_future(_go())
    except Exception:
        return None


# ── Cover (log channel pe send_photo => file_id) cache ─────────────────────────
def _sha1(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


async def cached_cover_id(thumb_path: str, sender):
    """
    sender: async callable(path) -> file_id  (e.g. send_photo to log channel).
    Same image (hash) dobara aaye to Telegram pe dobara upload nahi hota.
    Fail hone pe None.
    """
    try:
        key = await asyncio.to_thread(_sha1, thumb_path)
    except Exception:
        return None
    hit = _COVERS.get(key)
    if hit and time.time() - hit[0] < _COVER_TTL:
        return hit[1]
    fut = _COVER_INFLIGHT.get(key)
    if fut is not None:
        try:
            return await asyncio.shield(fut)
        except Exception:
            return None
    fut = asyncio.get_running_loop().create_future()
    _COVER_INFLIGHT[key] = fut
    try:
        fid = await sender(thumb_path)
        if fid:
            _COVERS[key] = (time.time(), fid)
            if len(_COVERS) > 128:
                _COVERS.pop(next(iter(_COVERS)), None)
        if not fut.done():
            fut.set_result(fid or None)
        return fid or None
    except BaseException as e:
        if not fut.done():
            fut.set_exception(e if isinstance(e, Exception) else RuntimeError("cancelled"))
            fut.exception()
        if isinstance(e, asyncio.CancelledError):
            raise
        LOGGER.warning(f"[ThumbSource] cover upload failed: {e!r}")
        return None
    finally:
        _COVER_INFLIGHT.pop(key, None)


# ── Custom thumb (/setpic, /thumb ka Telegram file_id) cache ────────────────────
# Har upload pe app.download_media() => Telegram se wahi chhoti image baar-baar.
# Ab ek baar download, baaki local copy (+ parallel calls ek hi download share karti hain).
_CUSTOM = {}            # file_id -> master path
_CUSTOM_INFLIGHT = {}   # file_id -> Future


async def fetch_custom_thumb(file_id: str, dest_dir: str, downloader):
    """
    downloader: async callable(file_id, path) -> path | None
    Returns apni ALAG copy ka path (caller delete kar sakta hai) ya None.
    """
    if not file_id:
        return None
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, f"cthumb_{time.time_ns()}.jpg")

    async def _copy_from(master):
        await asyncio.to_thread(shutil.copyfile, master, dest)
        return dest

    master = _CUSTOM.get(file_id)
    if master and os.path.isfile(master) and os.path.getsize(master) > 0:
        try:
            return await _copy_from(master)
        except Exception:
            pass
    fut = _CUSTOM_INFLIGHT.get(file_id)
    if fut is not None:
        try:
            master = await asyncio.shield(fut)
            return await _copy_from(master) if master else None
        except Exception:
            return None
    fut = asyncio.get_running_loop().create_future()
    _CUSTOM_INFLIGHT[file_id] = fut
    master = None
    try:
        try:
            from .. import download_dir as _dd
            keep_dir = os.path.join(_dd, "_thumbcache", "custom")
        except Exception:
            keep_dir = os.path.join(dest_dir, ".custom_master")
        os.makedirs(keep_dir, exist_ok=True)
        keep = os.path.join(keep_dir, f"c_{abs(hash(file_id))}.jpg")
        got = None
        for attempt in range(2):
            try:
                got = await asyncio.wait_for(downloader(file_id, keep), timeout=25)
                if got and str(got).endswith(".temp"):
                    fixed = str(got).replace(".temp", ".jpg")
                    try:
                        os.rename(got, fixed)
                        got = fixed
                    except Exception:
                        pass
                if got and os.path.isfile(got) and os.path.getsize(got) > 0:
                    break
                got = None
            except Exception as e:
                LOGGER.warning(f"[ThumbSource] custom thumb download {attempt + 1}/2: {e!r}")
                await asyncio.sleep(0.5)
        if got:
            master = str(got)
            _CUSTOM[file_id] = master
        if not fut.done():
            fut.set_result(master)
        return await _copy_from(master) if master else None
    except BaseException as e:
        if not fut.done():
            fut.set_exception(e if isinstance(e, Exception) else RuntimeError("cancelled"))
            fut.exception()
        if isinstance(e, asyncio.CancelledError):
            raise
        return None
    finally:
        _CUSTOM_INFLIGHT.pop(file_id, None)
