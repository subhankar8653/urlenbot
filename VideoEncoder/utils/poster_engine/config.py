"""poster_engine settings (original Anime-Poster-Bot config.py ka poster wala hissa)."""
from __future__ import annotations

import os
import re
from pathlib import Path


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _int(name: str, default: int) -> int:
    try:
        return int(_get(name, str(default)))
    except ValueError:
        return default


BASE_DIR = Path(__file__).resolve().parent

# ---------------------------------------------------------------- posters
BRAND_TOP = _get("BRAND_TOP", "ANI")                        # brand on styles 8 and 13
BRAND_BOTTOM = _get("BRAND_BOTTOM", "CROX")
USE_CHARACTER_PNG = _get("USE_CHARACTER_PNG", "1") != "0"   # styles 1, 2, 4, 5: try transparent fanart.tv PNGs
USE_ANILIST_CUTOUT = _get("USE_ANILIST_CUTOUT", "0") == "1"  # AniList character cut-out (rembg). off = fanart.tv only
FANART_CANDIDATES = _int("FANART_CANDIDATES", 15)
POSTER_CHOICES = _int("POSTER_CHOICES", 12)
BANNER_CHOICES = _int("BANNER_CHOICES", 12)

CHAR_MIN_HEIGHT = _int("CHAR_MIN_HEIGHT", 1400)
COVER_MIN_HEIGHT = _int("COVER_MIN_HEIGHT", 1350)
BANNER_MIN_WIDTH = _int("BANNER_MIN_WIDTH", 1920)
try:
    CHAR_MEDIUM_FILL = min(1.0, max(0.3, float(_get("CHAR_MEDIUM_FILL", "0.75"))))
    CHAR_WIDE_STRETCH = max(1.0, float(_get("CHAR_WIDE_STRETCH", "1.35")))
except ValueError:
    CHAR_MEDIUM_FILL, CHAR_WIDE_STRETCH = 0.75, 1.35
try:
    MAX_UPSCALE = max(1.0, float(_get("MAX_UPSCALE", "4")))
except ValueError:
    MAX_UPSCALE = 4.0
RENDER_WORKERS = _int("POSTER_RENDER_WORKERS", 2)
SAMPLE_ANIME_ID = _int("SAMPLE_ANIME_ID", 1535)             # AniList id jo /customise previews mein default dikhta hai

# ---------------------------------------------------------------- character art keys (optional)
FANART_API_KEY = _get("FANART_API_KEY", "")
TMDB_API_KEY = _get("TMDB_API_KEY", "")
REMBG_MODEL = _get("REMBG_MODEL")

# ---------------------------------------------------------------- fonts
FONT_DIR = BASE_DIR / "fonts"

# ---------------------------------------------------------------- values the (unmodified) templates read
os.environ.setdefault("POSTER_FONT_DIR", str(FONT_DIR))
for _k, _v in (("FANART_API_KEY", FANART_API_KEY), ("TMDB_API_KEY", TMDB_API_KEY), ("REMBG_MODEL", REMBG_MODEL)):
    if _v:
        os.environ[_k] = _v
