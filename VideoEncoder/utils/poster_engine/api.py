"""
poster_engine/api.py
=====================
Bot ke liye chhota wrapper — Anime-Poster-Bot ke engine (dataset.py) ke upar:

    anime = await find_anime("Psyren")                # AniList se dhundo (cache ke saath)
    r     = await render(anime, 7, button="WATCH NOW", brand="@SBANIME")
    ok    = await render_to_file("Psyren", cfg, "/path/x.jpg", brand="@SBANIME")

Yeh sirf BOT ke liye hai (auto-thumbnail / update-post poster / /customise preview).
"""
from __future__ import annotations

import asyncio
import logging
import re
import time

from . import config, dataset

LOGGER = logging.getLogger(__name__)

POSTERS = dataset.POSTERS                      # {1..14: PosterStyle}
STYLE_IDS = list(POSTERS)
BUTTON_PRESETS = dataset.BUTTON_PRESETS
NoArt = dataset.NoArt

_TTL = 3600.0
_ANIME: dict = {}                              # key -> (ts, Anime | None)
_INFLIGHT: dict = {}                           # key -> Future
_NEG_TTL = 180.0


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


async def _load_by_query(name: str):
    res = await dataset.search_anime(name, limit=3)
    if not res and re.search(r"\b(season|s\d+|part|cour)\b.*$", name, re.I):
        res = await dataset.search_anime(re.sub(r"\b(season|s\d+|part|cour)\b.*$", "", name, flags=re.I).strip(), limit=3)
    if not res:
        return None
    return await dataset.load_anime(res[0]["id"])


async def find_anime(name: str):
    """Naam se AniList anime (dataset.Anime) — 1 ghante tak cache; parallel calls ek hi kaam share karti hain."""
    k = _key(name)
    if not k:
        return None
    now = time.time()
    hit = _ANIME.get(k)
    if hit:
        ts, a = hit
        if (a is not None and now - ts < _TTL) or (a is None and now - ts < _NEG_TTL):
            return a
    fut = _INFLIGHT.get(k)
    if fut is not None:
        try:
            return await asyncio.shield(fut)
        except Exception:
            return None
    fut = asyncio.get_running_loop().create_future()
    _INFLIGHT[k] = fut
    a = None
    try:
        a = await _load_by_query(name)
        _ANIME[k] = (time.time(), a)
        if len(_ANIME) > 48:
            _ANIME.pop(next(iter(_ANIME)), None)
        if not fut.done():
            fut.set_result(a)
        return a
    except BaseException as e:
        if not fut.done():
            fut.set_exception(e if isinstance(e, Exception) else RuntimeError("cancelled"))
            fut.exception()
        if isinstance(e, asyncio.CancelledError):
            raise
        LOGGER.warning(f"[Poster] anime lookup failed for '{name}': {e}")
        return None
    finally:
        _INFLIGHT.pop(k, None)


async def load_by_id(anilist_id: int):
    k = f"id:{anilist_id}"
    hit = _ANIME.get(k)
    if hit and time.time() - hit[0] < _TTL and hit[1] is not None:
        return hit[1]
    a = await dataset.load_anime(int(anilist_id))
    _ANIME[k] = (time.time(), a)
    return a


async def search(query: str, limit: int = 6) -> list:
    """/customise preview ke liye: [{id, title, year, format}, ...]"""
    try:
        return await dataset.search_anime(query, limit=limit)
    except Exception as e:
        LOGGER.warning(f"[Poster] search failed: {e}")
        return []


_fonts_ready = False
_fonts_lock = None


async def _prepare():
    """Pehli baar: poster fonts (Google Fonts se) download — blocking kaam thread mein, sirf ek baar."""
    global _fonts_ready, _fonts_lock
    if _fonts_ready:
        return
    if _fonts_lock is None:
        _fonts_lock = asyncio.Lock()
    async with _fonts_lock:
        if _fonts_ready:
            return
        try:
            missing = await asyncio.to_thread(dataset.ensure_fonts)
            if missing:
                LOGGER.warning(f"[Poster] fonts missing (DejaVu fallback): {', '.join(missing)}")
        except Exception as e:
            LOGGER.warning(f"[Poster] ensure_fonts failed: {e}")
        _fonts_ready = True


def default_options(**kw) -> dict:
    o = dict(button=BUTTON_PRESETS[0], pic=0, char=0, chname="")
    o.update({k: v for k, v in kw.items() if v is not None})
    return o


async def render(anime, style_id: int, button: str = None, brand: str = None, pic: int = 0, char: int = 0):
    """dataset.Rendered (jpg / png bytes + size). NoArt raise kar sakta hai (banner/poster nahi mila)."""
    await _prepare()
    o = default_options(button=button, chname=brand or "", pic=pic, char=char)
    return await dataset.make_poster(int(style_id), anime, o)


async def render_to_file(name: str, cfg: dict, dest: str, brand: str = None, timeout: float = 60.0) -> bool:
    """Naam se anime dhundo, cfg['style'] se poster banao, JPEG dest pe likho. Fail pe False (caller purane tareeke pe jaaye)."""
    try:
        style = int((cfg or {}).get("style") or 0)
        if style not in POSTERS:
            return False

        async def _go():
            a = await find_anime(name)
            if a is None:
                return False
            r = await render(a, style, button=(cfg or {}).get("button"), brand=brand)
            with open(dest, "wb") as fh:
                fh.write(r.jpg)
            return True

        return await asyncio.wait_for(_go(), timeout)
    except NoArt as e:
        LOGGER.info(f"[Poster] style {cfg.get('style')} ke liye '{name}' ka {e.args[0] if e.args else 'art'} nahi mila")
    except asyncio.TimeoutError:
        LOGGER.warning(f"[Poster] render timeout for '{name}'")
    except Exception as e:
        LOGGER.warning(f"[Poster] render failed for '{name}': {e!r}")
    return False
