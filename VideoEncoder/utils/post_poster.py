"""
post_poster.py
===============
Main Channel update-post ki image: /customise -> Thumbnail -> Main Channel Post Thumbnail Design mein
koi poster style (1..14) select ho to post ke time us anime ka poster auto-generate hota hai
(Anime-Poster-Bot ke templates, AniList data). Style OFF ho ya kuch fail ho to None —
caller wahi purani saved image (/update_post wali) use karta hai.
"""
import hashlib
import logging
import os
import re
import time

LOGGER = logging.getLogger(__name__)
_TTL = 6 * 3600.0


def _cache_dir() -> str:
    try:
        from .. import download_dir as _dd
        base = os.path.join(_dd, "_postercache")
    except Exception:
        base = os.path.join(".", ".postercache")
    os.makedirs(base, exist_ok=True)
    return base


async def make_post_poster(anime_name: str):
    """Local JPEG path (cache mein) ya None."""
    try:
        from . import poster_cfg
        cfg = poster_cfg.get("post")
        if not cfg.get("style") or not (anime_name or "").strip():
            return None
        from .community import get_community_tag
        brand = cfg.get("brand") or await get_community_tag()
        key = hashlib.sha1(
            (re.sub(r"[^a-z0-9]+", " ", anime_name.lower()).strip() + "|" + poster_cfg.signature("post") + "|" + str(brand)).encode()
        ).hexdigest()[:16]
        path = os.path.join(_cache_dir(), f"post_{key}.jpg")
        if os.path.isfile(path) and os.path.getsize(path) > 0 and time.time() - os.path.getmtime(path) < _TTL:
            return path
        from .poster_engine import api as _pe
        tmp = path + ".tmp"
        if await _pe.render_to_file(anime_name, cfg, tmp, brand=brand):
            os.replace(tmp, path)
            LOGGER.info(f"[PostPoster] style {cfg['style']} poster built for '{anime_name}'")
            return path
        try:
            os.remove(tmp)
        except OSError:
            pass
    except Exception as e:
        LOGGER.warning(f"[PostPoster] failed for '{anime_name}': {e}")
    return None
