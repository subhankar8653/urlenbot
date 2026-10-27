"""
poster_cfg.py
==============
Anime-Poster-Bot ke 14 styles ki GLOBAL (bot-wide) settings — do alag jagah ke liye:

  "video" -> upload hone wali video ka auto-thumbnail (pehle TMDB poster + band lagta tha)
  "post"  -> Main Channel pe jaane wale update post ki image

Har kind ka config:  {"style": 0 (OFF = purana tareeka) ya 1..14, "button": "WATCH NOW", "brand": "" (khali = community naam)}

/customise -> Thumbnail se set hota hai (plugins/poster_panel.py). Hot-path (thumbnail banana / post bhejna) ke liye
in-memory cache hai; bot start pe ek baar DB se load hota hai (__main__.py).
"""
import logging

LOGGER = logging.getLogger(__name__)

KINDS = ("video", "post")
_DB_KEY = "poster_cfg"
_cache: dict = {k: {} for k in KINDS}
_loaded = False


def _norm(d) -> dict:
    d = d if isinstance(d, dict) else {}
    try:
        style = int(d.get("style") or 0)
    except (TypeError, ValueError):
        style = 0
    return {
        "style": style if 1 <= style <= 14 else 0,
        "button": str(d.get("button") or "").strip()[:24],
        "brand": str(d.get("brand") or "").strip()[:24],
    }


async def _owner_id():
    from .. import owner
    return owner[0] if owner else None


async def load_cache():
    """Bot startup pe call karo."""
    global _loaded
    try:
        from .database.access_db import db
        oid = await _owner_id()
        if oid:
            user = await db._get_user(oid)
            raw = user.get(_DB_KEY, {}) or {}
            for k in KINDS:
                _cache[k] = _norm(raw.get(k))
        _loaded = True
        LOGGER.info(f"[PosterCfg] loaded: {_cache}")
    except Exception as e:
        LOGGER.warning(f"[PosterCfg] load failed: {e}")


def get(kind: str) -> dict:
    """Sync (cached) — hot path ke liye."""
    return dict(_cache.get(kind) or _norm({}))


def style_of(kind: str) -> int:
    return int((_cache.get(kind) or {}).get("style") or 0)


def signature(kind: str) -> str:
    c = _cache.get(kind) or {}
    return f"{c.get('style', 0)}|{c.get('button', '')}|{c.get('brand', '')}"


async def save(kind: str, **changes) -> dict:
    from .database.access_db import db
    oid = await _owner_id()
    cur = _norm({**(_cache.get(kind) or {}), **changes})
    _cache[kind] = cur
    if oid:
        await db.col.update_one({'id': oid}, {'$set': {f'{_DB_KEY}.{kind}': cur}}, upsert=True)
    return dict(cur)
