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

import logging
import os
import time

from .anime_api import download_image, fetch_anime_details
from .auto_caption import extract_anime_info
from .encoding import DEFAULT_THUMB_BAND_TEXT, _add_thumb_username_band

LOGGER = logging.getLogger(__name__)


async def get_tmdb_thumbnail(filepath: str, dl_dir: str, band_text: str = None):
    """
    Filename se anime/movie ka naam nikal ke TMDB se poster/backdrop
    dhundo, download karke community band lagao.

    Kuch na mile ya koi bhi step fail ho toh None wapas — caller phir
    purane tarike se ffmpeg-frame pe fallback kare.
    """
    try:
        fname = os.path.basename(filepath)
        anime_name, _, _ = extract_anime_info(fname, {})
        anime_name = (anime_name or "").strip()
        if len(anime_name) < 2:
            return None

        details = await fetch_anime_details(anime_name)
        if not details or not details.get("image"):
            return None

        os.makedirs(dl_dir, exist_ok=True)
        out_path = os.path.join(dl_dir, f"tmdb_thumb_{int(time.time() * 1000)}.jpg")

        ok = await download_image(details["image"], out_path)
        if not ok or not os.path.isfile(out_path) or os.path.getsize(out_path) == 0:
            return None

        _add_thumb_username_band(out_path, text=band_text or DEFAULT_THUMB_BAND_TEXT)
        LOGGER.info(f"[ThumbSource] TMDB thumbnail used for '{anime_name}' ({details.get('source')})")
        return out_path
    except Exception as e:
        LOGGER.warning(f"[ThumbSource] TMDB thumbnail failed for '{filepath}': {e}")
        return None
