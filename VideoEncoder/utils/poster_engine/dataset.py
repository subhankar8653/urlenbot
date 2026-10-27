from __future__ import annotations

import asyncio
import html
import importlib.util
import io
import logging
import os
import re
import sys
import tempfile
import threading
import types
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from PIL import Image, ImageFilter

from . import chname
from . import config

log = logging.getLogger("dataset")

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "Templates"
FONT_DIR = config.FONT_DIR

# poster1-7 download Poppins here on first use; poster11/13/14 look in "fonts/"
FONT_DIR.mkdir(exist_ok=True)
(TEMPLATES_DIR / "fonts").mkdir(exist_ok=True)         # poster9/10/12 hard-code this folder

HEADERS = {"User-Agent": "anime-poster-bot/1.0"}
ANILIST_URL = "https://graphql.anilist.co"

BRAND_TOP = config.BRAND_TOP          # used by poster 8 and 13
BRAND_BOTTOM = config.BRAND_BOTTOM
USE_CHARACTER_PNG = config.USE_CHARACTER_PNG   # posters 1-7

BUTTON_PRESETS = ["WATCH NOW", "WATCH TRAILER", "STREAM NOW", "PLAY NOW", "DOWNLOAD"]


# ======================================================================
# 0. fonts  (downloaded once; posters 8/11/13/14 do not fetch their own)
# ======================================================================
_GF = "https://raw.githubusercontent.com/google/fonts/main/ofl/"
# (google-fonts folder, remote file name, local file name, folder(s) it must exist in)
_FONTS = [
    ("poppins", f"Poppins-{w}.ttf", f"Poppins-{w}.ttf", "both")
    for w in ("Regular", "Medium", "SemiBold", "Bold", "ExtraBold", "BoldItalic", "MediumItalic")
] + [
    ("lora", "Lora-Italic[wght].ttf", "Lora-Italic-Variable.ttf", "root"),      # poster 13/14
    ("lora", "Lora[wght].ttf", "Lora-Variable.ttf", "tpl"),                      # poster 8
    ("poppins", "Poppins-LightItalic.ttf", "Poppins-LightItalic.ttf", "tpl"),    # style 10 synopsis
    ("anton", "Anton-Regular.ttf", "Anton-Regular.ttf", "tpl"),                  # poster 8
    ("bebasneue", "BebasNeue-Regular.ttf", "BebasNeue-Regular.ttf", "tpl"),     # poster 8 heading
    ("notosansjp", "NotoSansJP[wght].ttf", "NotoSansJP-Variable.ttf", "tpl"),    # poster 8 (native title)
    ("dmserifdisplay", "DMSerifDisplay-Italic.ttf", "DMSerifDisplay-Italic.ttf", "tpl"),
]


def ensure_fonts() -> list[str]:
    """Download missing fonts. Blocking; returns the list of files that could not be fetched."""
    import urllib.parse
    import urllib.request
    missing = []
    for folder, remote, local, where in _FONTS:
        dests = []
        if where in ("root", "both"):
            dests.append(FONT_DIR / local)
        if where in ("tpl", "both"):
            dests.append(TEMPLATES_DIR / "fonts" / local)
        for dest in dests:
            if dest.exists() and dest.stat().st_size > 10_000:
                continue
            try:
                url = _GF + folder + "/" + urllib.parse.quote(remote, safe="")
                urllib.request.urlretrieve(url, dest)
            except Exception as e:
                log.warning("font %s not downloaded: %s", local, e)
                missing.append(local)
    return missing


# ======================================================================
# 0b. missing art assets
# ======================================================================
def _spline(points, steps=60):
    """Catmull-Rom through points -> dense polyline (same sample count for every curve)."""
    pts = [points[0]] + list(points) + [points[-1]]
    out = []
    for i in range(1, len(pts) - 2):
        p0, p1, p2, p3 = pts[i - 1], pts[i], pts[i + 1], pts[i + 2]
        for k in range(steps):
            t = k / steps
            t2, t3 = t * t, t * t * t
            out.append(tuple(0.5 * ((2 * p1[j]) + (-p0[j] + p2[j]) * t + (2 * p0[j] - 5 * p1[j] + 4 * p2[j] - p3[j]) * t2
                                    + (-p0[j] + 3 * p1[j] - 3 * p2[j] + p3[j]) * t3) for j in (0, 1)))
    out.append(points[-1])
    return out


def ensure_assets():
    """poster7 draws gold swirl lines from Templates/assets/lines_navy.png, which is not in the repo.
    If the file is missing a look-alike is generated (a drop-in file of your own always wins)."""
    path = TEMPLATES_DIR / "assets" / "lines_navy.png"
    if path.exists():
        return
    from PIL import ImageDraw
    path.parent.mkdir(exist_ok=True)
    W, H, SS = 780, 1080, 6                                    # pasted at the right edge of the 1920x1080 page
    img = Image.new("L", (W * SS, H * SS), 244)                # 244 = page colour, 47 = line colour (what the template expects)
    d = ImageDraw.Draw(img)

    def blend(e1, e2, n):
        a, b = _spline(e1), _spline(e2)
        for k in range(n):
            t = k / (n - 1)
            pts = [((1 - t) * p[0] + t * q[0], (1 - t) * p[1] + t * q[1]) for p, q in zip(a, b)]
            d.line([(x * SS, y * SS) for x, y in pts], fill=47, width=int(2.0 * SS), joint="curve")

    # top-right ribbon: fans in from the top edge, pinches behind the card, opens again to the right edge
    blend([(540, -30), (585, 100), (625, 200), (655, 280), (720, 318), (800, 338)],
          [(800, -30), (730, 60), (665, 185), (655, 280), (735, 385), (800, 450)], 24)
    # middle-right fan leaving the card
    blend([(600, 560), (690, 575), (755, 620), (800, 672)],
          [(600, 700), (688, 735), (752, 825), (800, 925)], 20)
    # bottom fan under the card
    blend([(150, 955), (153, 1015), (158, 1075), (162, 1120)],
          [(580, 955), (610, 1010), (660, 1072), (715, 1120)], 22)
    img.resize((W, H), Image.LANCZOS).save(path)
    log.info("generated %s", path)


# ======================================================================
# 1. poster registry  (add / rename styles here, the bot builds its menus from it)
# ======================================================================
@dataclass(frozen=True)
class PosterStyle:
    id: int
    label: str                       # shown on the inline button
    blurb: str                       # one line shown in the style list
    button: bool = False             # supports a custom button text
    brand: bool = False              # supports custom brand name (TOP + BOTTOM)
    character: bool = False          # uses transparent fanart.tv PNG (choose which in Options, next + back)


POSTERS: dict[int, PosterStyle] = {p.id: p for p in [
    PosterStyle(1, "Style 1", "1920x1080 · fanart.tv PNG (poster if none)", button=True, character=True),
    PosterStyle(2, "Style 2", "1920x1080 · fanart.tv PNG (poster if none)", button=True, character=True),
    PosterStyle(3, "Style 3", "1920x1080 · AniList poster", button=True),
    PosterStyle(4, "Style 4", "1920x1080 · fanart.tv PNG (poster if none)", button=True, character=True),
    PosterStyle(5, "Style 5", "1920x1080 · fanart.tv PNG (poster if none)", button=True, character=True),
    PosterStyle(6, "Style 6", "1920x1080 · AniList poster", button=True),
    PosterStyle(7, "Style 7", "1920x1080 · AniList poster", button=True),
    PosterStyle(8, "Style 8", "Brand lockup + info row, AniList banner", brand=True),
    PosterStyle(9, "Style 9", "Streaming-home look, AniList poster + match %"),
    PosterStyle(10, "Style 10", "White + pink, big half-circle frame, AniList poster", button=True),
    PosterStyle(11, "Style 11", "Light 1280x720, red block + AniList poster", button=True),
    PosterStyle(12, "Style 12", "Dark violet glow, AniList banner background"),
    PosterStyle(13, "Style 13", "Orange slash, AniList poster + rating cards", button=True, brand=True),
    PosterStyle(14, "Style 14", "Peach/teal, AniList poster in an arched panel", button=True),
]}


# ======================================================================
# 2. AniList
# ======================================================================
SEARCH_QUERY = """
query ($search: String, $perPage: Int) {
  Page(perPage: $perPage) {
    media(search: $search, type: ANIME, sort: SEARCH_MATCH) {
      id
      title { romaji english native }
      seasonYear
      format
    }
  }
}"""

MEDIA_QUERY = """
query ($id: Int) {
  Media(id: $id, type: ANIME) {
    id idMal
    title { romaji english native }
    description(asHtml: false)
    genres
    tags { name isMediaSpoiler rank }
    averageScore meanScore popularity
    seasonYear season startDate { year }
    format episodes duration status isAdult
    coverImage { extraLarge large color }
    bannerImage
    studios(isMain: true) { nodes { name } }
    characters(sort: [ROLE, FAVOURITES_DESC], perPage: 4) {
      edges { role node { name { full } image { large } } }
    }
  }
}"""


class NotFound(Exception):
    pass


class NoArt(Exception):
    """The style needs a banner / poster and none exists for this anime. args[0] = 'banner' | 'poster'."""


# every extra image AniList itself has for a show: the covers / banners of its related entries (seasons, source manga ...),
# the trailer thumbnail and the episode thumbnails. Asked for only when the user taps next / back.
ART_QUERY = """
query ($id: Int) {
  Media(id: $id, type: ANIME) {
    relations { edges { relationType(version: 2)
      node { id type title { romaji english } coverImage { extraLarge large } bannerImage } } }
    trailer { site thumbnail }
    streamingEpisodes { title thumbnail }
  }
}
"""


async def _anilist(client: httpx.AsyncClient, query: str, variables: dict) -> dict:
    last = None
    for attempt in range(3):
        try:
            r = await client.post(ANILIST_URL, json={"query": query, "variables": variables})
            if r.status_code == 404:
                raise NotFound(str(variables))
            if r.status_code == 429:                       # rate limited -> wait and retry
                await asyncio.sleep(int(r.headers.get("Retry-After", "3")))
                continue
            r.raise_for_status()
            return r.json().get("data") or {}
        except NotFound:
            raise
        except httpx.HTTPError as e:
            last = e
            await asyncio.sleep(1 + attempt)
    raise last or RuntimeError("AniList unreachable")


def display_title(m: dict) -> str:
    t = m.get("title") or {}
    return t.get("english") or t.get("romaji") or t.get("native") or "Unknown"


async def search_anime(query: str, limit: int = 6) -> list[dict]:
    """-> [{id, title, year, format}, ...]  (empty list = nothing found)"""
    async with httpx.AsyncClient(headers=HEADERS, timeout=20) as client:
        try:
            data = await _anilist(client, SEARCH_QUERY, {"search": query, "perPage": limit})
        except NotFound:
            return []
    out = []
    for m in (data.get("Page") or {}).get("media") or []:
        out.append(dict(id=m["id"], title=display_title(m), year=m.get("seasonYear"),
                        format=(m.get("format") or "").replace("_", " ")))
    return out


# ======================================================================
# 3. one loaded anime (raw AniList record + downloaded images)
# ======================================================================
@dataclass
class Anime:
    raw: dict
    cover_bytes: bytes | None = None
    banner_bytes: bytes | None = None
    characters: list = field(default_factory=list)   # [(label, RGBA image)] for posters 1-7; [0] = default
    character_tried: bool = False
    cover_tried: bool = False                        # images are loaded lazily, only the one a style needs
    banner_tried: bool = False
    posters: list = field(default_factory=list)      # [(label, url)] poster choices, filled by list_art()
    banners: list = field(default_factory=list)      # [(label, url)] banner choices, filled by list_art()
    posters_listed: bool = False
    banners_listed: bool = False
    img_cache: dict = field(default_factory=dict)    # url -> bytes (already sized up)
    _cache: dict = field(default_factory=dict)

    @property
    def title(self) -> str:
        return display_title(self.raw)

    @property
    def main_name(self) -> str | None:
        edges = (self.raw.get("characters") or {}).get("edges") or []
        return (((edges[0].get("node") or {}).get("name") or {}).get("full")) if edges else None

    @property
    def cover(self) -> Image.Image | None:
        return _pil(self.cover_bytes)

    @property
    def banner(self) -> Image.Image | None:
        return _pil(self.banner_bytes)


def _pil(data: bytes | None) -> Image.Image | None:
    if not data:
        return None
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        return img.convert("RGB")
    except Exception:
        return None


async def _get_bytes(client: httpx.AsyncClient, url: str | None) -> bytes | None:
    if not url:
        return None
    try:
        r = await client.get(url)
        r.raise_for_status()
        return r.content
    except Exception as e:
        log.warning("image download failed %s: %s", url, e)
        return None


# ----------------------------------------------------------------------
# small art -> medium size (aspect ratio is always kept, nothing is ever shrunk)
# ----------------------------------------------------------------------
_SHARPEN = ImageFilter.UnsharpMask(radius=1.4, percent=55, threshold=2)     # takes the softness out of the enlarge


def _grow(img: Image.Image, min_w: int = 0, min_h: int = 0) -> Image.Image:
    """Enlarge img (same ratio) until it reaches min_w / min_h. Returns img itself when it is already big enough.
    Capped at config.MAX_UPSCALE so a tiny file is not blown up into mush."""
    w, h = img.size
    f = max(1.0, min_w / w if min_w else 1.0, min_h / h if min_h else 1.0)
    f = min(f, config.MAX_UPSCALE)
    if f < 1.05:                                        # already medium or close enough
        return img
    big = img.resize((round(w * f), round(h * f)), Image.LANCZOS)   # RGBA is resized premultiplied by Pillow
    if big.mode == "RGBA":                              # sharpen the colour only, keep the cut-out edge as it is
        r, g, b, al = big.split()
        r, g, b = Image.merge("RGB", (r, g, b)).filter(_SHARPEN).split()
        return Image.merge("RGBA", (r, g, b, al))
    return big.convert("RGB").filter(_SHARPEN)


def _grow_character(img: Image.Image) -> Image.Image:
    """Transparent character PNG (styles 1-7): short / small -> at least CHAR_MIN_HEIGHT tall."""
    return _grow(img, min_h=config.CHAR_MIN_HEIGHT)


def _grow_art(img: Image.Image) -> Image.Image:
    """Banner (landscape) -> at least BANNER_MIN_WIDTH wide, poster / cover (portrait) -> at least COVER_MIN_HEIGHT tall."""
    if img.width >= img.height:
        return _grow(img, min_w=config.BANNER_MIN_WIDTH)
    return _grow(img, min_h=config.COVER_MIN_HEIGHT)


def _grow_bytes(data: bytes | None) -> bytes | None:
    """bytes in -> bytes out (JPEG q95 when it had to be enlarged, the original bytes when it did not)."""
    if not data:
        return data
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        img = img.convert("RGB")
        big = _grow_art(img)
        if big is img:
            return data
        out = io.BytesIO()
        big.save(out, "JPEG", quality=95, subsampling=0)
        return out.getvalue()
    except Exception as e:
        log.warning("upscale failed, using the original: %s", e)
        return data


async def load_anime(anilist_id: int) -> Anime:
    """Metadata only. The images are fetched later, per style, by ensure_art() - only the one that style needs."""
    async with httpx.AsyncClient(headers=HEADERS, timeout=25, follow_redirects=True) as client:
        data = await _anilist(client, MEDIA_QUERY, {"id": int(anilist_id)})
    media = data.get("Media")
    if not media:
        raise NotFound(str(anilist_id))
    return Anime(raw=media)


# ----------------------------------------------------------------------
# which image each style uses - and ONLY that one gets loaded
#   1, 2, 4, 5                      fanart.tv transparent PNG   (no PNG found -> AniList poster)
#   3, 6, 7, 9, 10, 11, 13, 14      AniList poster
#   8, 12                           AniList banner              (no banner -> AniList poster)
# ----------------------------------------------------------------------
PNG_STYLES = {1, 2, 4, 5}
BANNER_STYLES = {8, 12}


def art_kind(style_id: int) -> str:
    return "png" if style_id in PNG_STYLES else "banner" if style_id in BANNER_STYLES else "poster"


async def _need_image(a: Anime, which: str) -> None:
    """Download + size up the AniList poster ('cover') or banner, once."""
    if getattr(a, f"{which}_tried"):
        return
    setattr(a, f"{which}_tried", True)
    if which == "cover":
        ci = a.raw.get("coverImage") or {}
        url = ci.get("extraLarge") or ci.get("large")
    else:
        url = a.raw.get("bannerImage")
    if not url:
        return
    async with httpx.AsyncClient(headers=HEADERS, timeout=25, follow_redirects=True) as client:
        data = await _get_bytes(client, url)
    data = await asyncio.to_thread(_grow_bytes, data)          # small poster / banner -> medium size, same ratio
    setattr(a, f"{which}_bytes", data)


def _base_items(a: Anime, kind: str) -> list:
    """What AniList itself has: one poster, one banner. Nothing else is touched until the user taps next / back."""
    if kind == "poster":
        ci = a.raw.get("coverImage") or {}
        u = ci.get("extraLarge") or ci.get("large")
        return [("AniList poster", u)] if u else []
    u = a.raw.get("bannerImage")
    return [("AniList banner", u)] if u else []


def _art_items(a: Anime, kind: str) -> list:
    return getattr(a, f"{kind}s") if getattr(a, f"{kind}s_listed") else _base_items(a, kind)


async def list_art(a: Anime, kind: str) -> None:
    """Look up the other AniList posters / banners (one small AniList call) so next / back has something to cycle through.
    AniList only - nothing from fanart.tv or anywhere else.
    posters: this anime's poster, then the posters of its related entries (sequel, prequel, source manga ...).
    banners: this anime's banner, then related entries' banners, the trailer thumbnail and episode thumbnails."""
    if getattr(a, f"{kind}s_listed"):
        return
    items = list(_base_items(a, kind))
    try:
        async with httpx.AsyncClient(headers=HEADERS, timeout=25, follow_redirects=True) as client:
            media = (await _anilist(client, ART_QUERY, {"id": int(a.raw["id"])})).get("Media") or {}
        for e in (media.get("relations") or {}).get("edges") or []:
            n = e.get("node") or {}
            ci = n.get("coverImage") or {}
            url = (ci.get("extraLarge") or ci.get("large")) if kind == "poster" else n.get("bannerImage")
            if not url:
                continue
            t = n.get("title") or {}
            rel = str(e.get("relationType") or "related").replace("_", " ").title()
            items.append((f"AniList · {rel}: {t.get('english') or t.get('romaji') or '?'}", url))
        if kind == "banner":
            tr = media.get("trailer") or {}
            if tr.get("thumbnail"):
                items.append(("AniList · trailer thumbnail", tr["thumbnail"]))
            for i, ep in enumerate(media.get("streamingEpisodes") or [], 1):
                if ep.get("thumbnail"):
                    items.append((f"AniList · episode {ep.get('title') or i}", ep["thumbnail"]))
    except Exception as e:
        log.warning("listing %s choices failed: %s", kind, e)
    seen, out = set(), []
    for label, u in items:
        if u and u not in seen:
            seen.add(u)
            out.append((label, u))
    limit = config.POSTER_CHOICES if kind == "poster" else config.BANNER_CHOICES
    setattr(a, f"{kind}s", out[:max(1, limit)])
    setattr(a, f"{kind}s_listed", True)


def art_count(a: Anime, style_id: int) -> int:
    return len(_art_items(a, art_kind(style_id)))


def art_state(a: Anime, style_id: int, idx: int | None) -> tuple[int, str, int, bool]:
    """-> (how many, label of the chosen one, its index, whether the full list was looked up yet)"""
    kind = art_kind(style_id)
    items = _art_items(a, kind)
    listed = getattr(a, f"{kind}s_listed")
    if not items:
        return 0, "none", 0, listed
    i = (idx or 0) % len(items)
    return len(items), items[i][0], i, listed


async def _download_art(a: Anime, url: str) -> None:
    if url in a.img_cache:
        return
    async with httpx.AsyncClient(headers=HEADERS, timeout=25, follow_redirects=True) as client:
        data = await _get_bytes(client, url)
    if not data:
        return
    a.img_cache[url] = await asyncio.to_thread(_grow_bytes, data)     # small image -> medium size, same ratio


async def ensure_art(a: Anime, style_id: int, opts: dict | None = None) -> None:
    """Load only the one image this style draws.  Raises NoArt when a banner / poster style has nothing to draw."""
    o = opts or {}
    kind = art_kind(style_id)
    if kind == "png":
        await _ensure_character(a)                              # fanart.tv only
        idx = o.get("char", 0)
        if not (0 <= idx < len(a.characters)):                  # no PNG (or "cover card" picked) -> AniList poster
            await _need_image(a, "cover")
        return
    items = _art_items(a, kind)
    if not items and not getattr(a, f"{kind}s_listed"):         # e.g. AniList has no banner -> look further
        await list_art(a, kind)
        items = _art_items(a, kind)
    if not items:
        raise NoArt(kind)
    url = items[(o.get("pic") or 0) % len(items)][1]
    await _download_art(a, url)
    if url not in a.img_cache:
        raise NoArt(kind)


def _art_bytes(a: Anime, style_id: int, o: dict) -> bytes | None:
    """The chosen poster (poster styles) or banner (banner styles)."""
    items = _art_items(a, art_kind(style_id))
    if not items:
        return None
    return a.img_cache.get(items[(o.get("pic") or 0) % len(items)][1])


# ======================================================================
# 4. text helpers
# ======================================================================
def clean_text(raw: str | None, limit: int = 260) -> str:
    s = html.unescape(raw or "")
    s = re.sub(r"<br\s*/?>", " ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"\(Source:[^)]*\)|\[Written by[^\]]*\]", "", s, flags=re.I)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) <= limit:
        return s
    cut = s[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if end > limit * 0.55:
        return cut[:end + 1]
    return cut.rsplit(" ", 1)[0].rstrip(",;:-") + "…"


def split_lines(title: str, n: int) -> list[str]:
    """Balanced split of a title into at most n lines."""
    words = title.split()
    if n <= 1 or len(words) <= 1:
        return [title]
    n = min(n, len(words))
    total = len(title)
    lines, cur = [], []
    for w in words:
        cur.append(w)
        if len(lines) < n - 1 and len(" ".join(cur)) >= total / n:
            lines.append(" ".join(cur))
            cur = []
    if cur:
        lines.append(" ".join(cur))
    return lines


def wrap_to_width(title: str, measure, max_w: float, max_lines: int) -> list[str]:
    """Greedy wrap so every line is <= max_w (measure(text) -> width); extra text becomes '…'.
    A title that fits on one line is split in two so a two-colour title still looks right."""
    lines, cur = [], ""
    for w in title.split():
        trial = (cur + " " + w).strip()
        if cur and measure(trial) > max_w:
            lines.append(cur)
            cur = w
        else:
            cur = trial
    lines.append(cur)
    if len(lines) > max_lines:
        last = " ".join(lines[max_lines - 1:])
        while measure(last + "…") > max_w and " " in last:
            last = last.rsplit(" ", 1)[0]
        lines = lines[:max_lines - 1] + [last + "…"]
    elif len(lines) == 1 and " " in title and max_lines > 1:
        lines = split_lines(title, 2)
    return lines


_STOP = {"which", "their", "there", "about", "would", "could", "while", "after", "before", "where",
         "other", "these", "those", "being", "become", "because", "himself", "herself", "something"}


def pick_highlights(text: str, n: int = 4) -> list[str]:
    """Names (capitalised mid-sentence) first, then the longest plain words."""
    words = [w.strip(".,;:!?\"'()…") for w in text.split()]
    names, longs = [], []
    for i, w in enumerate(words):
        if len(w) < 4 or w.lower() in _STOP:
            continue
        prev = text.split()[i - 1] if i else "."
        if w[0].isupper() and not prev.endswith((".", "!", "?")):
            names.append(w.lower())
        elif len(w) >= 7:
            longs.append(w.lower())
    out = []
    for w in names + sorted(set(longs), key=len, reverse=True):
        if w not in out:
            out.append(w)
    return out[:n]


def _score(raw: dict) -> int | None:
    return raw.get("averageScore") or raw.get("meanScore")


def _studio(raw: dict) -> str | None:
    nodes = ((raw.get("studios") or {}).get("nodes")) or []
    return nodes[0]["name"] if nodes else None


def _year(raw: dict):
    return raw.get("seasonYear") or (raw.get("startDate") or {}).get("year")


def _center_crop(img: Image.Image, aspect: float, top_bias: float = 0.0) -> tuple[int, int, int, int]:
    """Box (l,t,r,b) of the largest region with width/height == aspect.
    top_bias 0 = centred vertically, 1 = glued to the top."""
    w, h = img.size
    if w / h > aspect:
        cw, ch = int(h * aspect), h
    else:
        cw, ch = w, int(w / aspect)
    l = (w - cw) // 2
    t = int((h - ch) * (0.5 - 0.5 * top_bias))
    return l, t, l + cw, t + ch


# ======================================================================
# 5. template loading
# ======================================================================
class _Source(types.SimpleNamespace):
    """Stand-in for the missing `source` module used by poster11/13/14."""


_SOURCE = _Source()
_SOURCE_LOCK = threading.Lock()                 # the shim is global -> one render at a time for 11/13/14
_MODULES: dict[int, types.ModuleType] = {}


def _place_character_medium(cv, character, cx, bottom, max_w, max_h):
    """Same job as the templates' place_character(), but a short png is not left small.

    The templates fit the art into a max_w x max_h box. A wide / low png (a mecha with a long weapon, a lying pose...)
    hits max_w first and ends up tiny, with empty space above it. Here it keeps its ratio and grows until it reaches a
    medium height (CHAR_MEDIUM_FILL of max_h), using up to CHAR_WIDE_STRETCH x the width, never leaving the canvas."""
    ch = character.convert("RGBA")
    bbox = ch.getchannel("A").getbbox()
    if bbox:
        ch = ch.crop(bbox)
    sc = min(max_w / ch.width, max_h / ch.height)
    medium_h = max_h * config.CHAR_MEDIUM_FILL
    if ch.height * sc < medium_h:
        room_w = min(max_w * config.CHAR_WIDE_STRETCH, 2 * (min(cx, cv.w - cx) - 12))
        sc = max(sc, min(medium_h / ch.height, room_w / ch.width, max_h / ch.height))
    ch = ch.resize((max(1, int(ch.width * sc)), max(1, int(ch.height * sc))), Image.LANCZOS)
    cv.paste(ch, (int(cx - ch.width / 2), int(bottom - ch.height)))


def _template(n: int) -> types.ModuleType:
    if n in _MODULES:
        return _MODULES[n]
    path = TEMPLATES_DIR / f"poster{n}.py"
    code = path.read_text(encoding="utf-8")
    code = re.sub(r"^\s*import\s+\.source\s*$", "", code, flags=re.M)      # poster11 typo
    mod = types.ModuleType(f"poster_tpl_{n}")
    mod.__file__ = str(path)
    mod.source = _SOURCE
    sys.modules[mod.__name__] = mod                  # dataclasses (poster8) look the module up here
    exec(compile(code, str(path), "exec"), mod.__dict__)
    if n in PNG_STYLES and hasattr(mod, "place_character"):
        mod.place_character = _place_character_medium
    _MODULES[n] = mod
    return mod


_sources_mod = None


def _sources():
    """Templates/sources.py (character-PNG providers) loaded by path."""
    global _sources_mod
    if _sources_mod is None:
        spec = importlib.util.spec_from_file_location("poster_sources", TEMPLATES_DIR / "sources.py")
        _sources_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_sources_mod)
    return _sources_mod


# ======================================================================
# 6. adapters  (AniList record -> what each template wants)
# ======================================================================
def _simple_meta(a: Anime) -> dict:
    """poster1-7"""
    r = a.raw
    return dict(title=a.title, genres=r.get("genres") or [], description=r.get("description") or "",
                score=_score(r))


def _sources_meta(a: Anime) -> dict:
    """shape produced by Templates/sources.py, needed by the character-PNG providers"""
    r, t = a.raw, a.raw.get("title") or {}
    chars = [dict(name=e["node"]["name"]["full"], image=(e["node"].get("image") or {}).get("large"))
             for e in ((r.get("characters") or {}).get("edges") or [])]
    return dict(source="anilist", anilist_id=r["id"], mal_id=r.get("idMal"), title=a.title,
                title_romaji=t.get("romaji"), year=_year(r), characters=chars)


# ---- AniList id -> TVDB id (fanart.tv is indexed by TVDB id). Free, no key. TMDB is the fallback. ----
_MAP_URL = "https://raw.githubusercontent.com/Fribb/anime-lists/master/anime-list-full.json"
_MAP_FILE = BASE_DIR / ".cache" / "anilist_tvdb.json"
_MAP_MAX_AGE = 7 * 86400
_tvdb_map: dict[int, int] | None = None


async def _tvdb_id_for(client: httpx.AsyncClient, anilist_id: int) -> int | None:
    global _tvdb_map
    if _tvdb_map is None:
        import json
        import time
        fresh = _MAP_FILE.exists() and time.time() - _MAP_FILE.stat().st_mtime < _MAP_MAX_AGE
        if fresh:
            try:
                _tvdb_map = {int(k): v for k, v in json.loads(_MAP_FILE.read_text()).items()}
            except Exception:
                fresh = False
        if not fresh:
            try:
                r = await client.get(_MAP_URL, timeout=90)
                r.raise_for_status()
                _tvdb_map = {int(x["anilist_id"]): int(x["tvdb_id"]) for x in r.json()
                             if x.get("anilist_id") and x.get("tvdb_id")}
                _MAP_FILE.parent.mkdir(exist_ok=True)
                _MAP_FILE.write_text(json.dumps(_tvdb_map))
                log.info("anilist->tvdb map loaded: %d entries", len(_tvdb_map))
            except Exception as e:
                log.warning("anilist->tvdb map download failed: %s", e)
                if _MAP_FILE.exists():                      # stale cache is better than nothing
                    try:
                        _tvdb_map = {int(k): v for k, v in json.loads(_MAP_FILE.read_text()).items()}
                    except Exception:
                        pass
                if _tvdb_map is None:
                    return None
    return _tvdb_map.get(int(anilist_id))


FANART_CANDIDATES = config.FANART_CANDIDATES


async def _fanart_data(client, a: Anime) -> dict:
    """fanart.tv record of this show (all image kinds). Fetched once per anime; {} when nothing is known."""
    if "fanart" in a._cache:
        return a._cache["fanart"]
    data: dict = {}
    try:
        src = _sources()
        if src.FANART_KEY:
            meta = _sources_meta(a)
            meta["tvdb_id"] = await _tvdb_id_for(client, a.raw["id"])
            if not meta.get("tvdb_id"):
                meta.update({k: v for k, v in (await src.tmdb_ids_for(client, meta)).items() if v})   # TMDB fallback
            if meta.get("tvdb_id"):
                r = await client.get(f"https://webservice.fanart.tv/v3/tv/{meta['tvdb_id']}",
                                     params={"api_key": src.FANART_KEY})
                if r.status_code == 200:
                    data = r.json()
    except Exception as e:
        log.warning("fanart.tv lookup failed: %s", e)
    a._cache["fanart"] = data
    return data


def _fanart_urls(data: dict, keys: tuple) -> list:
    """image urls of the given fanart.tv kinds, most-liked first"""
    urls: list = []
    for key in keys:
        for it in sorted(data.get(key) or [], key=lambda i: int(i.get("likes", 0) or 0), reverse=True):
            if it.get("url") and it["url"] not in urls:
                urls.append(it["url"])
    return urls


async def _fanart_candidates(client, a: Anime, limit: int) -> list:
    """Several transparent images from fanart.tv, most-liked first.
    fanart.tv does not say WHICH character is on an image, so the user picks in Options."""
    urls = _fanart_urls(await _fanart_data(client, a), ("characterart", "hdclearart", "clearart"))[:limit]
    src = _sources()
    imgs = await asyncio.gather(*[src.fetch_image(client, u, rgba=True) for u in urls])
    return [img for img in imgs if img is not None and src.has_transparency(img)]


async def _ensure_character(a: Anime):
    """Fill a.characters with fanart.tv transparent PNGs (+ optional AniList cut-out when USE_ANILIST_CUTOUT=1)."""
    if a.character_tried:
        return
    a.character_tried = True
    if not USE_CHARACTER_PNG:
        log.info("character art: disabled (USE_CHARACTER_PNG=0) -> cover card")
        return
    try:
        src = _sources()
        log.info("character art for %r: FANART_API_KEY=%s TMDB_API_KEY=%s", a.title,
                 "set" if src.FANART_KEY else "MISSING", "set" if src.TMDB_KEY else "not set (fallback off)")
        async with httpx.AsyncClient(headers=HEADERS, timeout=25, follow_redirects=True) as client:
            meta = _sources_meta(a)
            meta["tvdb_id"] = await _tvdb_id_for(client, a.raw["id"])   # if None -> TMDB fallback
            cut = None
            if config.USE_ANILIST_CUTOUT:
                try:
                    cut = await src.anilist_cutout_png(client, meta)    # main character, needs rembg
                except Exception as e:
                    log.warning("character art: cutout crashed: %s", e)
            if cut is not None:
                cut = await asyncio.to_thread(_grow_character, cut)     # short / small png -> medium size
                a.characters.append((f"main character: {a.main_name or '?'}", cut))
            try:
                for i, img in enumerate(await _fanart_candidates(client, a, FANART_CANDIDATES), 1):
                    img = await asyncio.to_thread(_grow_character, img)     # short / small png -> medium size
                    a.characters.append((f"fanart.tv #{i}", img))
            except Exception as e:
                log.warning("character art: fanart crashed: %s", e)
        log.info("character art: %d candidate(s): %s", len(a.characters), [l for l, _ in a.characters])
    except Exception as e:
        log.warning("character lookup failed: %s", e)


def char_state(a: Anime, idx: int) -> tuple[int, str]:
    """-> (number of candidates, label for the chosen index)  idx -1 = cover card"""
    n = len(a.characters)
    if 0 <= idx < n:
        return n, a.characters[idx][0]
    return n, "cover card"


def _render_1_7(n, a, o):
    if n == 7:
        ensure_assets()
    mod = _template(n)
    if n in PNG_STYLES:
        idx = o.get("char", 0)
        art = a.characters[idx][1] if 0 <= idx < len(a.characters) else None  # None -> AniList poster card
        cover = a.cover
    else:
        art = None                                                            # styles 3, 6, 7: the chosen poster only
        cover = _pil(_art_bytes(a, n, o))
    return mod.render_poster(_simple_meta(a), art, cover, button=o["button"])


def _render_8(n, a, o):
    mod = _template(n)
    r = a.raw
    t = r.get("title") or {}
    score = _score(r)
    url_img = _pil(_art_bytes(a, n, o))                  # the chosen banner
    meta = mod.PosterMeta(
        title_english=a.title,
        title_native=t.get("native") or "",
        title_romaji=t.get("romaji") or "",
        synopsis=clean_text(r.get("description"), 420),
        rating=f"{score / 10:.1f}" if score else "N/A",
        year=str(_year(r) or "—"),
        episodes=str(r.get("episodes") or "—"),
        studio=_studio(r) or "—",
        genres=", ".join((r.get("genres") or [])[:4]) or "—",
        image_url=None,
        brand_top=o["brand"][0], brand_bottom=o["brand"][1],
        brand_small=(o["brand"][0] + o["brand"][1]),
    )
    # the template downloads image_url itself; hand it the bytes we already have instead
    meta.image_url = "memory"
    with _SOURCE_LOCK:                                   # we swap a module function -> one at a time
        mod_download = mod._download_image
        mod._download_image = lambda url, timeout=20: url_img
        try:
            with tempfile.TemporaryDirectory() as tmp:
                out = mod.render_poster(meta, os.path.join(tmp, "p8.png"))
                img = Image.open(out)
                img.load()
                return img.convert("RGB")
        finally:
            mod._download_image = mod_download


def _render_aniraw(n, a, o):
    """poster9 / 10 / 12 take the raw AniList dict + image bytes and return BytesIO"""
    mod = _template(n)
    sel = _art_bytes(a, n, o)
    if n == 12:                                           # banner only: it is the background (the template reuses it up front)
        buf = mod.create_poster(a.raw, None, sel)
    else:                                                  # style 9: the chosen poster = hero art
        buf = mod.create_poster(a.raw, sel, None)
    img = Image.open(buf)
    img.load()
    return img.convert("RGB")


def _render_10(n, a, o):
    """Style 10 = Templates/poster10.py (white + pink half-circle frame); takes a custom button text."""
    mod = _template(10)
    buf = mod.create_poster(a.raw, _art_bytes(a, 10, o), None, button_text=o["button"])
    img = Image.open(buf)
    img.load()
    return img.convert("RGB")


def _save_tmp(img: Image.Image, tmp: str, name: str) -> str:
    p = os.path.join(tmp, name)
    img.save(p, "PNG")
    return p


def _tags_upper(raw, n=3):
    return [g.upper() for g in (raw.get("genres") or [])[:n]]


def _render_11(n, a, o):
    mod, r = _template(n), a.raw
    cover = _pil(_art_bytes(a, n, o)) or Image.new("RGB", (460, 650), (60, 60, 70))
    syn = clean_text(r.get("description"), 250)
    eps = r.get("episodes")
    season = " ".join(x for x in ((r.get("season") or "").upper(), str(_year(r) or "")) if x)
    info = " · ".join(x for x in (season, f"{eps} EPISODES" if eps else "") if x) or "ANIME"
    tf = mod.get_font("Poppins-Bold.ttf", 66)                 # same font the template draws with
    l1, l2 = (wrap_to_width(a.title, lambda t: tf.getlength(t) / mod.SCALE + 4, 735, 2) + [""])[:2]
    anime = dict(season_info=info, title_line1=l1, title_line2=l2,
                 studio=(_studio(r) or "ANIME").upper(), tags=_tags_upper(r, 3) or ["ANIME"],
                 highlight_words=pick_highlights(syn), synopsis=syn, button_text=o["button"])
    with _SOURCE_LOCK, tempfile.TemporaryDirectory() as tmp:
        _SOURCE.POSTER_SOURCE = _save_tmp(cover, tmp, "cover.png")
        _SOURCE.POSTER_CROP = (0, 0, 1280, 720)               # whole image (coords are relative)
        _SOURCE.ANIME = anime
        return mod.build_poster(anime).convert("RGB")


def _render_13(n, a, o):
    mod, r = _template(n), a.raw
    cover = _pil(_art_bytes(a, n, o)) or Image.new("RGB", (460, 650), (60, 60, 70))
    syn = clean_text(r.get("description"), 230)
    edges = (r.get("characters") or {}).get("edges") or []
    ch = edges[0] if edges else {}
    ch_name = ((ch.get("node") or {}).get("name") or {}).get("full") or "Main Cast"
    tags = [t["name"] for t in (r.get("tags") or []) if not t.get("isMediaSpoiler")][:3]
    score = _score(r)
    nav = ["HOME", "ANIME", "TRENDING", "NEWS"]
    room = 198 - getattr(mod, "LEFT_X", 40) - 14                 # the nav links start at a fixed x: a long brand must not run into them
    if chname.text_width("Poppins-Bold.ttf", 21, "".join(o["brand"])) > room:
        nav = []
    anime = dict(brand=o["brand"], nav_links=nav, nav_active="ANIME",
                 title=a.title, synopsis=syn, tags=_tags_upper(r, 3) or ["ANIME"],
                 button_text=o["button"], side_label="FEATURED CHARACTER",
                 character_name=ch_name[:22], character_role=(ch.get("role") or "MAIN").upper(),
                 rating=(score or 0) / 10, genres=(r.get("genres") or ["Anime"])[:3],
                 themes=tags or ["Anime"])
    # hero = whole-cover crop for the slanted panel, avatar = square from the top of the cover
    hero_aspect = 0.62
    with _SOURCE_LOCK, tempfile.TemporaryDirectory() as tmp:
        _SOURCE.ART_SOURCE = _save_tmp(cover, tmp, "cover.png")
        _SOURCE.ART_REF_SIZE = cover.size                       # 1:1 pixel coordinates
        _SOURCE.HERO_CROP = _center_crop(cover, hero_aspect)
        _SOURCE.AVATAR_CROP = _center_crop(cover, 1.0, top_bias=1.0)
        _SOURCE.SHOW_HERO = True
        _SOURCE.HERO_ALIGN = 0.5
        _SOURCE.ANIME = anime
        return mod.build(anime).convert("RGB")


def _render_14(n, a, o):
    mod, r = _template(n), a.raw
    cover = _pil(_art_bytes(a, n, o)) or Image.new("RGB", (460, 650), (60, 60, 70))
    syn = clean_text(r.get("description"), 200)
    tf = mod.font("Lora-Italic-Variable.ttf", 76, 700)
    title_lines = wrap_to_width(a.title, lambda t: tf.getlength(t) / mod.S, 580, 3)
    anime = dict(title_lines=title_lines, tags=_tags_upper(r, 3) or ["ANIME"],
                 highlight_words=pick_highlights(syn), synopsis=syn, button_text=o["button"])
    with _SOURCE_LOCK, tempfile.TemporaryDirectory() as tmp:
        _SOURCE.ART_SOURCE = _save_tmp(cover, tmp, "cover.png")
        _SOURCE.ART_CROP = _center_crop(cover, 0.85, top_bias=0.6)
        _SOURCE.ANIME = anime
        return mod.build(anime).convert("RGB")


_RENDERERS = {**{i: _render_1_7 for i in range(1, 8)}, 8: _render_8, 9: _render_aniraw, 10: _render_10,
              11: _render_11, 12: _render_aniraw, 13: _render_13, 14: _render_14}


# ======================================================================
# 7. the one call the bot uses
# ======================================================================
@dataclass
class Rendered:
    jpg: bytes                  # for send_photo   (always < 10 MB)
    png: bytes                  # for send_document (lossless)
    size: tuple[int, int]


_RENDER_SLOTS = asyncio.Semaphore(config.RENDER_WORKERS)


def default_options(style_id: int) -> dict:
    return dict(button=BUTTON_PRESETS[0], brand=(BRAND_TOP, BRAND_BOTTOM), char=0, pic=0)


_BRAND6_LOCK = threading.Lock()          # style 6 gets its header text by swapping a constant of the shared template


def _render_sync(style_id: int, a: Anime, opts: dict) -> Rendered:
    o = {**default_options(style_id), **(opts or {})}
    name = chname.clean(o.get("chname"))                 # the user's channel branding - on EVERY poster
    if name and style_id in (8, 13):                     # the template's brand lockup carries it
        o["brand"] = chname.split_brand(name)
    if name and style_id == 6:                           # the template's header text carries it
        mod = _template(6)
        with _BRAND6_LOCK:
            old = mod.BRAND
            mod.BRAND = name.upper()
            try:
                img = _RENDERERS[style_id](style_id, a, o)
            finally:
                mod.BRAND = old
    else:
        img = _RENDERERS[style_id](style_id, a, o)
    img = img.convert("RGB")
    if name and style_id not in chname.OWN_BRAND:        # every other style: written in that poster's own look
        img = chname.draw_brand(img, name, style_id)
    jpg, png = io.BytesIO(), io.BytesIO()
    img.save(jpg, "JPEG", quality=93, optimize=True)
    img.save(png, "PNG", optimize=False)
    return Rendered(jpg=jpg.getvalue(), png=png.getvalue(), size=img.size)


async def make_poster(style_id: int, a: Anime, opts: dict | None = None) -> Rendered:
    if style_id not in POSTERS:
        raise ValueError(f"unknown poster style {style_id}")
    await ensure_art(a, style_id, opts)                  # loads only what THIS style uses
    async with _RENDER_SLOTS:
        return await asyncio.to_thread(_render_sync, style_id, a, opts or {})
