# Ported from AniwatchTvdl (Cantarella bots) — urlenbot /aniwatch ke liye
"""
animesalt.to scraper.

Pipeline
--------
  /filter?keyword=            -> <a class="dynamic-name" href=".../watch/{slug}/ep-N">
  watch page                  -> #wrapper[data-id]              internal anime id
  /ajax/episode/list/{id}     -> <a class="item ep-item" data-number data-id>
  /ajax/server/list?servers=  -> <div class="item server-item" data-type data-id>
  /ajax/server?get={id}       -> megaplay embed url
  megaplay getSourcesNew      -> {"enc": "<AES-256-CBC b64url>"}
  decrypt(enc)                -> {"file": ".../master.m3u8"}
  N_m3u8DL-RE + ffmpeg        -> final .mp4 (see .base)
"""

from __future__ import annotations

import re
import urllib.parse

from .base import (
    BaseScraper,
    attrs_of,
    clean_text,
    detect_source,
    http_get,
    is_url,
    make_session,
    resolve_megaplay,
    season_of,
)

BASE = "https://animesalt.to"

_EP_ID_RE = re.compile(r"[?&]ep=([^&#]+)")
_EP_NUM_RE = re.compile(r"/ep-(\d+)")
_SLUG_RE = re.compile(r"/watch/([^/?#]+)")
_WRAPPER_RE = re.compile(r'id="wrapper"[^>]*data-id="([^"]+)"')

# generic labels animesalt puts in the episode <a title="..."> slot
_GENERIC_TITLES = {"full", "hd", "sd", "sub", "dub", "episode", ""}


class AnimeSaltClient:
    """Thin, non-interactive client for animesalt.to."""

    BASE = BASE
    impersonate = "chrome124"

    def __init__(self, impersonate: str = "chrome124", timeout: int = 40,
                 proxy: str = None):
        self.impersonate = impersonate
        self.timeout = timeout
        self.proxy = proxy
        self.session = make_session(impersonate, proxy)

    # ── transport ────────────────────────────────────────────────────────────

    def get(self, url, **kwargs):
        kwargs.setdefault("timeout", self.timeout)
        kwargs.setdefault("impersonate", self.impersonate)
        return http_get(self.session, url, **kwargs)

    def _ajax(self, url, **kwargs):
        headers = {"x-requested-with": "XMLHttpRequest"}
        headers.update(kwargs.pop("headers", {}) or {})
        return self.get(url, headers=headers, **kwargs)

    # ── URL helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def series_url(value: str) -> str:
        raw = (value or "").strip().strip("\"'")
        if not raw:
            return ""
        match = _SLUG_RE.search(raw)
        if match:
            return f"{BASE}/watch/{match.group(1)}"
        slug = raw.split("?")[0].rstrip("/").split("/")[-1]
        return f"{BASE}/watch/{slug}"

    @staticmethod
    def slug_of(value: str) -> str:
        match = _SLUG_RE.search(value or "")
        if match:
            return match.group(1)
        return (value or "").split("?")[0].rstrip("/").split("/")[-1]

    # ── reads ────────────────────────────────────────────────────────────────

    def search(self, query: str, limit: int = 24) -> list:
        resp = self.get(f"{BASE}/filter", params={"keyword": query})
        resp.raise_for_status()
        html = resp.text

        out, seen = [], set()
        for match in re.finditer(
                r"<a\b([^>]*\bdynamic-name\b[^>]*)>(.*?)</a>", html, re.S | re.I):
            data = attrs_of(match.group(1))
            href = data.get("href") or ""
            if "/watch/" not in href:
                continue
            series = self.series_url(href)
            if series in seen:
                continue
            seen.add(series)
            out.append({
                "title": clean_text(data.get("title") or match.group(2)),
                "jp": clean_text(data.get("data-jname") or ""),
                "url": series,
                "slug": self.slug_of(series),
                "type": "ANIME",
            })
            if len(out) >= limit:
                break
        return out

    def watch_meta(self, url: str) -> dict:
        series = self.series_url(url)
        resp = self.get(series)
        resp.raise_for_status()
        html = resp.text

        match = _WRAPPER_RE.search(html)
        if not match:
            raise RuntimeError("could not find the anime id on that page")

        return {
            "anime_id": match.group(1),
            "base_url": series,
            "slug": self.slug_of(series),
            "title": _page_title(html, self.slug_of(series)),
        }

    def episodes(self, anime_id: str) -> list:
        resp = self._ajax(f"{BASE}/ajax/episode/list/{anime_id}")
        resp.raise_for_status()
        payload = resp.json()
        html = payload.get("html") or payload.get("result") or ""

        episodes = []
        for match in re.finditer(r"<a\b([^>]*\bep-item\b[^>]*)>", html):
            data = attrs_of(match.group(1))
            if "data-id" not in data:
                continue
            try:
                number = int(data.get("data-number") or 0)
            except ValueError:
                number = 0

            title = clean_text(data.get("title") or "")
            if title.lower() in _GENERIC_TITLES:
                title = ""

            episodes.append({
                "num": number,
                "ep_id": data["data-id"],
                "title": title or f"Episode {number}",
            })

        episodes.sort(key=lambda item: item["num"])
        return episodes

    def servers(self, ep_id: str, kind: str = None) -> list:
        url = f"{BASE}/ajax/server/list?servers=" + urllib.parse.quote(ep_id or "", safe="")
        resp = self._ajax(url)
        resp.raise_for_status()
        payload = resp.json()
        html = payload.get("html") or payload.get("result") or ""

        out = []
        for match in re.finditer(r'<div[^>]*class="[^"]*server-item[^"]*"([^>]*)>', html):
            data = attrs_of(match.group(1))
            if "data-id" not in data:
                continue
            # the visible name is the first anchor after the opening div
            window = html[match.end():match.end() + 400]
            label = re.search(r"<a\b[^>]*>(.*?)</a>", window, re.S)
            out.append({
                "name": clean_text(label.group(1)) if label else data.get("data-server-id", "?"),
                "type": data.get("data-type", "sub"),
                "server_id": data.get("data-server-id", ""),
                "link_id": data["data-id"],
            })

        if kind:
            return [srv for srv in out if srv["type"] == kind]
        return out

    def embed(self, link_id: str) -> str:
        url = f"{BASE}/ajax/server?get=" + urllib.parse.quote(link_id or "", safe="")
        resp = self._ajax(url)
        resp.raise_for_status()
        payload = resp.json()
        link = payload.get("link")
        if not link:
            result = payload.get("result")
            link = result.get("url") if isinstance(result, dict) else None
        if not link:
            raise RuntimeError(f"server resolve returned no link: {payload}")
        return link

    def sources(self, embed_url: str) -> dict:
        return resolve_megaplay(
            self.session, embed_url,
            referer=f"{BASE}/", timeout=self.timeout,
            impersonate=self.impersonate,
        )

    def latest_widget(self, path: str = "latest-episode") -> list:
        """Same widget family as the other site; empty-safe."""
        try:
            resp = self._ajax(f"{BASE}/ajax/home/widget/{path}")
            payload = resp.json()
        except Exception as exc:
            print(f"animesalt widget '{path}' failed: {exc}", flush=True)
            return []
        html = payload.get("result") or payload.get("html") or ""
        out, seen = [], set()
        for match in re.finditer(r'<a\b([^>]*\bdynamic-name\b[^>]*)>', html, re.S | re.I):
            data = attrs_of(match.group(1))
            href = data.get("href") or ""
            if "/watch/" not in href:
                continue
            series = self.series_url(href)
            if series in seen:
                continue
            seen.add(series)
            num = _EP_NUM_RE.search(href)
            out.append({
                "id": self.slug_of(series),
                "title": clean_text(data.get("title") or ""),
                "url": series,
                "ep_num": int(num.group(1)) if num else None,
            })
        return out


# ──────────────────────────────────────────────────────────────────────────────
#  helpers
# ──────────────────────────────────────────────────────────────────────────────

def _page_title(html: str, fallback: str) -> str:
    """animesalt only exposes the series name inside <title>.

    Observed shapes::

        Watch Frieren: Beyond Journey's End Season 2 Online in HD - AnimeSalt
        Watch Free! Road of Naruto Online in HD - AnimeSalt
        Watch Naruto Anime Online | AnimeSalt
        Watch One Piece Online with SUB/DUB - AnimeSalt
    """
    match = re.search(r"<title>(.*?)</title>", html, re.S | re.I)
    if match:
        title = clean_text(match.group(1))
        title = re.sub(r"\s*[-|]\s*AnimeSalt.*$", "", title, flags=re.I)
        title = re.sub(r"\s+Online\b.*$", "", title, flags=re.I | re.S)
        title = re.sub(r"^Watch\s+(?:Free!\s*)?", "", title, flags=re.I)
        title = re.sub(r"\s+Anime\s*$", "", title, flags=re.I)
        if title.strip():
            return title.strip()
    for pattern in (r'<h1[^>]*>(.*?)</h1>',
                    r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"'):
        match = re.search(pattern, html, re.S | re.I)
        if match:
            title = clean_text(match.group(1))
            if title:
                return title
    return fallback.replace("-", " ").title()


# ──────────────────────────────────────────────────────────────────────────────
#  scraper
# ──────────────────────────────────────────────────────────────────────────────

class AnimeSaltScraper(BaseScraper):
    """Bot-facing scraper for animesalt.to (the 'aniwatch' slot)."""

    SOURCE = "animesalt"
    BASE = BASE
    impersonate = "chrome124"

    def __init__(self, download_path="anime_downloads", progress_queue=None,
                 client: AnimeSaltClient = None):
        super().__init__(download_path, progress_queue)
        self.client = client or AnimeSaltClient(impersonate=self.impersonate,
                                                proxy=self.proxy)
        self.session = self.client.session

    # ── search ───────────────────────────────────────────────────────────────

    def search_anime(self, query: str, limit: int = 24) -> list:
        if is_url(query) and detect_source(query) == "animesalt":
            try:
                meta = self.client.watch_meta(query)
            except Exception as exc:
                print(f"animesalt url resolve failed: {exc}", flush=True)
                return []
            return [{
                "title": meta["title"],
                "id": meta["slug"],
                "type": "ANIME",
                "url": meta["base_url"],
            }]

        return [{
            "title": item["title"],
            "id": item["slug"],
            "type": item.get("type") or "ANIME",
            "url": item["url"],
        } for item in self.client.search(query, limit=limit)]

    # ── episodes ─────────────────────────────────────────────────────────────

    def list_episodes(self, value: str) -> list:
        series = self.client.series_url(value)
        meta = self.client.watch_meta(series)
        episodes = self.client.episodes(meta["anime_id"])

        out = []
        for episode in episodes:
            out.append({
                "title": episode.get("title") or f"Episode {episode['num']}",
                "url": f"{BASE}/watch/{meta['slug']}/ep-{episode['num']}",
                "ep_number": str(episode["num"]),
                "ep_id": episode["ep_id"],
                "series_title": meta["title"],
            })
        return out

    def get_anime_info(self, anime_id) -> dict:
        try:
            return self.client.watch_meta(str(anime_id))
        except Exception:
            return {}

    def get_episode_servers(self, value: str, kind: str = "sub") -> list:
        try:
            return self.client.servers(value, kind)
        except Exception:
            return []

    def get_home_sections(self) -> dict:
        return {"latest": self.client.latest_widget("latest-episode")}

    def fetch_recently_updated(self, page: int = 1, per_page: int = 20) -> list:
        items = self.client.latest_widget("latest-episode")
        if not items:
            items = self.client.latest_widget("recent")
        return items[:per_page] if per_page else items

    def get_schedule(self, date_str: str = None) -> list:
        """animesalt.to publishes no schedule endpoint - nothing to report."""
        return []

    # ── download recipe ──────────────────────────────────────────────────────

    def resolve_episode(self, url: str) -> dict:
        num_match = _EP_NUM_RE.search(url or "")
        id_match = _EP_ID_RE.search(url or "")
        wanted_num = int(num_match.group(1)) if num_match else None
        wanted_id = id_match.group(1) if id_match else None

        series = self.client.series_url(url)
        meta = self.client.watch_meta(series)
        episodes = self.client.episodes(meta["anime_id"])
        if not episodes:
            raise RuntimeError("no episodes listed for this title")

        episode = None
        if wanted_num is not None:
            episode = next((e for e in episodes if e["num"] == wanted_num), None)
        if episode is None and wanted_id:
            episode = next((e for e in episodes if e["ep_id"] == wanted_id), None)
        if episode is None:
            episode = episodes[0]

        recipe = {
            "series": meta["title"],
            "slug": meta["slug"],
            "season": season_of(meta["title"]),
            "episode": str(episode["num"]),
            "ep_title": episode.get("title") or f"Episode {episode['num']}",
            "referer": f"{BASE}/",
            "sub": None,
            "dub": None,
        }

        for kind in ("sub", "dub"):
            source = self._first_working_source(episode["ep_id"], kind)
            if source:
                recipe[kind] = source

        return recipe

    def _first_working_source(self, ep_id: str, kind: str) -> dict:
        try:
            servers = self.client.servers(ep_id, kind)
        except Exception as exc:
            print(f"animesalt {kind} server list failed: {exc}", flush=True)
            return None

        for server in servers:
            if server.get("type") != kind:
                continue
            try:
                print(f"   trying [{kind}] {server['name']}...", flush=True)
                embed = self.client.embed(server["link_id"])
                source = self.client.sources(embed)
                if source.get("file"):
                    print(f"   -> ok on {server['name']}", flush=True)
                    return source
            except Exception as exc:
                print(f"   [{kind}] {server.get('name')} failed: {exc}", flush=True)
        return None
