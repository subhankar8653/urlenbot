"""
anime_api.py
================
/add_anime ke "auto-detail" step ke liye independent anime-metadata client.

Primary source: TMDB (The Movie Database) — accurate poster, genres,
episode count, status. Requires a free API key (TMDB_API_KEY in config.env).
Anime zyaadatar TMDB par "TV" ke tarah listed hote hain, kuch (movies) "Movie"
ke tarah — dono search kiye jaate hain.

AniList fallback REMOVED — sirf TMDB use hota hai ab (TMDB_API_KEY zaroori
hai config.env mein). AniList helper functions neeche abhi bhi maujood
hain (future use ke liye), bas fetch_anime_details() unhe ab call nahi
karta.
"""

import logging
import os
import re
from difflib import SequenceMatcher
from typing import Optional

import httpx

LOGGER = logging.getLogger(__name__)

TMDB_API_KEY = os.getenv("TMDB_API_KEY", "").strip()
TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/original"

ANILIST_URL = "https://graphql.anilist.co"
ANIZIP_URL = "https://api.ani.zip/mappings"

_TITLE_MATCH_THRESHOLD = 0.55
_TIMEOUT = httpx.Timeout(15.0)

_ANILIST_FIELDS = """
    id
    title { romaji english }
    synonyms
    seasonYear
    startDate { year }
    status
    format
    episodes
    genres
    coverImage { extraLarge large }
    bannerImage
"""

_ANILIST_QUERY = """
query ($search: String) {
  Media(search: $search, type: ANIME) {
""" + _ANILIST_FIELDS + """
  }
}
"""

_client: Optional[httpx.AsyncClient] = None


def is_configured() -> bool:
    # AniList fallback removed — ab sirf TMDB, isliye key hona zaroori hai.
    return bool(TMDB_API_KEY)


async def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": "urlenbot/1.0"},
        )
    return _client


def _normalize_title(title: str) -> str:
    if not title:
        return ""
    t = title.lower().strip()
    t = re.sub(r"^\b(the|a|an)\b\s+", "", t)
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _fuzzy_ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


# ---------------------------------------------------------------------------
# TMDB
# ---------------------------------------------------------------------------

_TMDB_STATUS_MAP = {
    "Returning Series": "Ongoing",
    "Planned": "Upcoming",
    "In Production": "Upcoming",
    "Pilot": "Upcoming",
    "Ended": "Finished",
    "Canceled": "Cancelled",
    "Released": "Finished",
    "Rumored": "Upcoming",
    "Post Production": "Upcoming",
}


async def _tmdb_get(path: str, params: dict) -> Optional[dict]:
    try:
        client = await _get_client()
        params = {**params, "api_key": TMDB_API_KEY}
        resp = await client.get(f"{TMDB_BASE}{path}", params=params)
        if resp.status_code != 200:
            LOGGER.warning(f"[AnimeAPI] TMDB {path} -> HTTP {resp.status_code}")
            return None
        return resp.json()
    except Exception as e:
        LOGGER.warning(f"[AnimeAPI] TMDB {path} failed: {e}")
        return None


def _tmdb_title_match_score(query: str, result: dict) -> float:
    candidates = [
        result.get("name"), result.get("original_name"),
        result.get("title"), result.get("original_title"),
    ]
    q = _normalize_title(query)
    if not q:
        return 0.0
    best = 0.0
    for cand in candidates:
        cn = _normalize_title(cand)
        if cn:
            best = max(best, _fuzzy_ratio(q, cn))
    return best


async def _tmdb_search(query: str):
    """Pehle TV, phir Movie search karo (anime dono tarah listed ho sakte hain)."""
    tv = await _tmdb_get("/search/tv", {"query": query})
    for result in (tv or {}).get("results") or []:
        if _tmdb_title_match_score(query, result) >= _TITLE_MATCH_THRESHOLD:
            return "tv", result

    movie = await _tmdb_get("/search/movie", {"query": query})
    for result in (movie or {}).get("results") or []:
        if _tmdb_title_match_score(query, result) >= _TITLE_MATCH_THRESHOLD:
            return "movie", result

    return None, None


async def _tmdb_details(media_type: str, tmdb_id: int) -> Optional[dict]:
    return await _tmdb_get(f"/{media_type}/{tmdb_id}", {})


async def _fetch_from_tmdb(anime_name: str) -> Optional[dict]:
    if not TMDB_API_KEY:
        LOGGER.info("[AnimeAPI] TMDB_API_KEY not set, skipping TMDB")
        return None

    media_type, result = await _tmdb_search(anime_name)
    if not result:
        LOGGER.info(f"[AnimeAPI] No TMDB match for '{anime_name}'")
        return None

    details = await _tmdb_details(media_type, result.get("id")) or result

    season = None
    season_breakdown = ""

    if media_type == "tv":
        matched_name = details.get("name") or anime_name
        status = _TMDB_STATUS_MAP.get(details.get("status") or "", "")

        # ── Season-wise episode count — "Season 0" (Specials) exclude karo ──
        seasons = [
            s for s in (details.get("seasons") or [])
            if (s.get("season_number") or 0) >= 1
        ]
        seasons.sort(key=lambda s: s.get("season_number") or 0)

        if seasons:
            season_breakdown = " | ".join(
                f"S{s.get('season_number')}: {s.get('episode_count', 0)} eps"
                for s in seasons
            )
            # Default: Season 1 ke episode count se monitoring start hoti hai
            # (RTI zyaadatar S1 se hi anime add karta hai). Alag season monitor
            # karna ho toh /schedule se total_eps manually adjust kar sakte ho.
            season = seasons[0].get("season_number")
            total_eps = seasons[0].get("episode_count", 0)
        else:
            season = None
            total_eps = details.get("number_of_episodes") or 0
    else:
        matched_name = details.get("title") or anime_name
        total_eps = 1
        status = _TMDB_STATUS_MAP.get(details.get("status") or "", "")

    # ── Image: backdrop (wide/landscape banner, jaisa detail-page pe dikhta
    #    hai) — poster (portrait) sirf backdrop na milne par fallback ──
    backdrop_path = details.get("backdrop_path")
    poster_path = details.get("poster_path")
    image = ""
    if backdrop_path:
        image = f"{TMDB_IMAGE_BASE}{backdrop_path}"
    elif poster_path:
        image = f"{TMDB_IMAGE_BASE}{poster_path}"

    genres = ", ".join(g.get("name", "") for g in (details.get("genres") or []) if g.get("name"))

    return {
        "matched_name": matched_name,
        "image": image,
        "genres": genres,
        "audio": "Hindi ORG",
        "season": season,
        "season_breakdown": season_breakdown,
        "total_eps": total_eps,
        "status": status,
        "source": "TMDB",
    }


# ---------------------------------------------------------------------------
# AniList (fallback)
# ---------------------------------------------------------------------------

def _title_match_score(query: str, media: dict) -> float:
    titles = media.get("title") or {}
    candidates = [titles.get("romaji"), titles.get("english"), *(media.get("synonyms") or [])]
    q = _normalize_title(query)
    if not q:
        return 0.0
    best = 0.0
    for cand in candidates:
        cn = _normalize_title(cand)
        if cn:
            best = max(best, _fuzzy_ratio(q, cn))
    return best


async def _anilist_search(query: str) -> Optional[dict]:
    try:
        client = await _get_client()
        resp = await client.post(ANILIST_URL, json={"query": _ANILIST_QUERY, "variables": {"search": query}})
        if resp.status_code != 200:
            return None
        return ((resp.json() or {}).get("data") or {}).get("Media")
    except Exception as e:
        LOGGER.warning(f"[AnimeAPI] AniList search failed for '{query}': {e}")
        return None


async def _anizip_mappings(anilist_id: int) -> Optional[dict]:
    try:
        client = await _get_client()
        resp = await client.get(ANIZIP_URL, params={"anilist_id": anilist_id})
        if resp.status_code != 200:
            return None
        return resp.json()
    except Exception as e:
        LOGGER.warning(f"[AnimeAPI] ani.zip mappings failed for {anilist_id}: {e}")
        return None


_ANILIST_STATUS_MAP = {
    "RELEASING": "Ongoing",
    "FINISHED": "Finished",
    "NOT_YET_RELEASED": "Upcoming",
    "CANCELLED": "Cancelled",
    "HIATUS": "On Hiatus",
}


async def _fetch_from_anilist(anime_name: str) -> Optional[dict]:
    media = await _anilist_search(anime_name)
    if not media:
        LOGGER.info(f"[AnimeAPI] No AniList match for '{anime_name}'")
        return None

    score = _title_match_score(anime_name, media)
    if score < _TITLE_MATCH_THRESHOLD:
        titles = media.get("title") or {}
        LOGGER.info(
            f"[AnimeAPI] Rejecting low-confidence AniList match for '{anime_name}': "
            f"got '{titles.get('english') or titles.get('romaji')}' (score={score:.2f})"
        )
        return None

    doc = await _anizip_mappings(media.get("id")) or {}
    titles = media.get("title") or {}
    cover = media.get("coverImage") or {}

    total_eps = media.get("episodes") or 0
    if not total_eps:
        # AniList "episodes" ongoing shows ke liye null hota hai — AniZip ke
        # actual episode-mapping count se fallback lo.
        total_eps = len((doc.get("episodes") or {}))

    return {
        "matched_name": titles.get("english") or titles.get("romaji") or anime_name,
        "image": cover.get("extraLarge") or cover.get("large") or media.get("bannerImage") or "",
        "genres": ", ".join(media.get("genres") or []),
        "audio": "Hindi ORG",
        "season": None,
        "total_eps": total_eps,
        "status": _ANILIST_STATUS_MAP.get(media.get("status") or "", ""),
        "source": "AniList",
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def search_anime(query: str) -> list:
    """TMDB pe title search karo, matched media (dict, agar mila) wapas do."""
    result = await _fetch_from_tmdb(query)
    if result:
        return [result]
    return []


async def fetch_anime_details(anime_name: str):
    """
    TMDB se anime ki details nikalo, /add_anime aur auto-thumbnail ke
    liye zaroori fields ek dict mein:

      {
        "matched_name": str,
        "image":        str,   # poster/backdrop URL ("" agar nahi mila)
        "genres":       str,   # "Action, Comedy"
        "audio":        str,   # default "Hindi ORG" (baad mein
                                # /update_post_list se manually change karo)
        "season":       None,
        "total_eps":    int,   # total episode count
        "status":       str,   # "Ongoing" / "Finished" / ""
        "source":       str,   # "TMDB"
      }

    TMDB pe match na mile toh None (AniList fallback jaan-boojh kar
    hata diya gaya hai — sirf TMDB use hota hai).
    """
    return await _fetch_from_tmdb(anime_name)


async def download_image(url: str, dest_path: str) -> bool:
    """
    URL (TMDB poster/backdrop ya AniList cover) download karke dest_path
    pe save karo. Auto-thumbnail feature (thumb_source.py) ke liye.
    """
    if not url:
        return False
    try:
        client = await _get_client()
        resp = await client.get(url)
        if resp.status_code != 200:
            LOGGER.warning(f"[AnimeAPI] Image download HTTP {resp.status_code}: {url}")
            return False
        with open(dest_path, "wb") as f:
            f.write(resp.content)
        return True
    except Exception as e:
        LOGGER.warning(f"[AnimeAPI] Image download failed: {e}")
        return False
