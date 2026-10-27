import asyncio
import io
import logging
import os
import re
from dataclasses import dataclass, field

import httpx
from PIL import Image

log = logging.getLogger("sources")

TMDB_KEY = os.environ.get("TMDB_API_KEY", "2c1bcd8ab35fb0102d0bbd2320688f03")
FANART_KEY = os.environ.get("FANART_API_KEY", "77d5cb67eb122c30f984882fe063856b")
HEADERS = {"User-Agent": "anime-poster-bot/1.0"}


class NotFound(Exception):
    pass


@dataclass
class Inputs:
    meta: dict
    cover: Image.Image | None = None
    character: Image.Image | None = None
    notes: list = field(default_factory=list)      # debug info: which source was used


# ======================================================================
# 1. metadata: AniList first
# ======================================================================
ANILIST_QUERY = """
query ($search: String) {
  Media(search: $search, type: ANIME, sort: SEARCH_MATCH) {
    id idMal
    title { romaji english native }
    description(asHtml: false)
    genres averageScore seasonYear format episodes status
    coverImage { extraLarge large color }
    bannerImage
    studios(isMain: true) { nodes { name } }
    characters(sort: [ROLE, FAVOURITES_DESC], perPage: 4) { nodes { name { full } image { large } } }
  }
}"""


async def search_anilist(client: httpx.AsyncClient, name: str) -> dict:
    r = await client.post("https://graphql.anilist.co",
                          json={"query": ANILIST_QUERY, "variables": {"search": name}})
    if r.status_code == 404:                          # AniList answers 404 when nothing matches
        raise NotFound(name)
    r.raise_for_status()
    m = (r.json().get("data") or {}).get("Media")
    if not m:
        raise NotFound(name)
    t = m["title"]
    return dict(
        source="anilist", anilist_id=m["id"], mal_id=m.get("idMal"),
        title=t.get("english") or t.get("romaji") or t.get("native"),
        title_romaji=t.get("romaji"), year=m.get("seasonYear"), format=m.get("format"),
        episodes=m.get("episodes"), status=m.get("status"),
        description=m.get("description") or "", genres=m.get("genres") or [],
        score=m.get("averageScore"),                  # 0-100
        color=(m.get("coverImage") or {}).get("color"),
        cover_url=(m.get("coverImage") or {}).get("extraLarge") or (m.get("coverImage") or {}).get("large"),
        banner_url=m.get("bannerImage"),
        studio=((m.get("studios") or {}).get("nodes") or [{}])[0].get("name"),
        characters=[dict(name=c["name"]["full"], image=(c.get("image") or {}).get("large"))
                    for c in (m.get("characters") or {}).get("nodes", [])],
    )


# ======================================================================
# 2. metadata fallback: TMDB
# ======================================================================
async def _tmdb(client, path, **params):
    if not TMDB_KEY:
        raise NotFound("TMDB_API_KEY not set")
    headers = {}
    if len(TMDB_KEY) > 40:                            # v4 read-access token
        headers["Authorization"] = f"Bearer {TMDB_KEY}"
    else:
        params["api_key"] = TMDB_KEY
    r = await client.get(f"https://api.themoviedb.org/3{path}", params=params, headers=headers)
    r.raise_for_status()
    return r.json()


def _year(date):
    return int(date[:4]) if date and date[:4].isdigit() else None


async def search_tmdb(client: httpx.AsyncClient, name: str) -> dict:
    for kind in ("tv", "movie"):
        res = (await _tmdb(client, f"/search/{kind}", query=name)).get("results") or []
        if not res:
            continue
        hit = res[0]
        d = await _tmdb(client, f"/{kind}/{hit['id']}", append_to_response="external_ids")
        img = "https://image.tmdb.org/t/p/"
        return dict(
            source="tmdb", tmdb_id=d["id"], tmdb_kind=kind,
            tvdb_id=(d.get("external_ids") or {}).get("tvdb_id"),
            title=d.get("name") or d.get("title"), year=_year(d.get("first_air_date") or d.get("release_date")),
            description=d.get("overview") or "", genres=[g["name"] for g in d.get("genres", [])],
            score=round((d.get("vote_average") or 0) * 10) or None, color=None,
            cover_url=f"{img}w780{d['poster_path']}" if d.get("poster_path") else None,
            banner_url=f"{img}w1280{d['backdrop_path']}" if d.get("backdrop_path") else None,
            characters=[],
        )
    raise NotFound(name)


async def tmdb_ids_for(client, meta) -> dict:
    """AniList meta has no TVDB id (fanart.tv needs it) -> look the show up on TMDB."""
    if not TMDB_KEY:
        return {}
    try:
        res = (await _tmdb(client, "/search/tv", query=meta.get("title_romaji") or meta["title"])).get("results") or []
        if meta.get("year"):
            near = [x for x in res if _year(x.get("first_air_date")) and abs(_year(x["first_air_date"]) - meta["year"]) <= 1]
            res = near or res
        if not res:
            return {}
        d = await _tmdb(client, f"/tv/{res[0]['id']}/external_ids")
        return dict(tmdb_id=res[0]["id"], tvdb_id=d.get("tvdb_id"))
    except Exception as e:
        log.warning("tmdb id lookup failed: %s", e)
        return {}


# ======================================================================
# 3. image helpers
# ======================================================================
async def fetch_image(client: httpx.AsyncClient, url: str | None, rgba=False) -> Image.Image | None:
    if not url:
        return None
    try:
        r = await client.get(url)
        r.raise_for_status()
        img = Image.open(io.BytesIO(r.content))
        img.load()
        return img.convert("RGBA" if rgba else "RGB")
    except Exception as e:
        log.warning("image fetch failed %s: %s", url, e)
        return None


def has_transparency(img: Image.Image) -> bool:
    return img.mode == "RGBA" and img.getchannel("A").getextrema()[0] < 250


# ======================================================================
# 4. character-PNG providers (tried in order; first hit wins)
#    each:  async def provider(client, meta) -> PIL RGBA | None
# ======================================================================
async def fanart_png(client, meta):
    """fanart.tv  -> characterart / hdclearart / clearart (transparent PNGs)."""
    if not FANART_KEY:
        return None
    ids = {k: meta.get(k) for k in ("tvdb_id", "tmdb_id")}
    if not ids["tvdb_id"]:
        ids.update(await tmdb_ids_for(client, meta))
        meta.update({k: v for k, v in ids.items() if v})
    if not ids.get("tvdb_id"):
        return None
    r = await client.get(f"https://webservice.fanart.tv/v3/tv/{ids['tvdb_id']}", params={"api_key": FANART_KEY})
    if r.status_code != 200:
        return None
    data = r.json()
    for key in ("characterart", "hdclearart", "clearart"):
        items = sorted(data.get(key) or [], key=lambda i: int(i.get("likes", 0)), reverse=True)
        for it in items[:3]:
            img = await fetch_image(client, it["url"], rgba=True)
            if img is not None and has_transparency(img):
                return img
    return None


async def anilist_cutout_png(client, meta):
    """2nd provider: take the main character picture from AniList and cut the background out.
    Optional - needs `pip install rembg onnxruntime`; silently skipped if not installed.
    Replace/add your own site here (see 'adding a provider' in README)."""
    try:
        from rembg import new_session, remove
    except ImportError:
        return None
    chars = [c for c in meta.get("characters", []) if c.get("image")]
    if not chars:
        return None
    img = await fetch_image(client, chars[0]["image"])
    if img is None:
        return None
    model = os.environ.get("REMBG_MODEL", "isnet-anime")
    session = await asyncio.to_thread(new_session, model)
    return await asyncio.to_thread(lambda: remove(img, session=session).convert("RGBA"))


PNG_PROVIDERS = [fanart_png, anilist_cutout_png]       # <- add / reorder providers here


# ======================================================================
# 5. the one function the bot calls
# ======================================================================
async def get_inputs(name: str, client: httpx.AsyncClient | None = None) -> Inputs:
    own = client is None
    client = client or httpx.AsyncClient(headers=HEADERS, timeout=20, follow_redirects=True)
    try:
        notes = []
        try:
            meta = await search_anilist(client, name)
            notes.append("meta: anilist")
        except (NotFound, httpx.HTTPError) as e:
            log.info("anilist failed (%s) -> tmdb", e)
            meta = await search_tmdb(client, name)          # raises NotFound if this fails too
            notes.append("meta: tmdb")

        cover_task = asyncio.create_task(fetch_image(client, meta.get("cover_url")))
        character = None
        for provider in PNG_PROVIDERS:
            try:
                character = await provider(client, meta)
            except Exception as e:                           # one broken site must not kill the poster
                log.warning("%s failed: %s", provider.__name__, e)
                character = None
            if character is not None:
                notes.append(f"character: {provider.__name__}")
                break
        cover = await cover_task
        return Inputs(meta=meta, cover=cover, character=character, notes=notes)
    finally:
        if own:
            await client.aclose()
