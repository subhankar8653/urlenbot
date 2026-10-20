# Ported from AniwatchTvdl (Cantarella bots) — urlenbot /aniwatch ke liye
"""
anikototv.to scraper.

Pipeline
--------
  /search?keyword=            -> <a class="name d-title" href=".../watch/{slug}/ep-N">
  watch page                  -> #watch-main[data-id][data-url]   internal anime id
  /ajax/episode/list/{id}     -> <a data-ids data-id data-num ...> one per episode
  /ajax/server/list?servers=  -> <li data-link-id data-sv-id>      HD-1 / Vidstream-1 ...
  /ajax/server?get={link-id}  -> megaplay embed url
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

BASE = "https://anikototv.to"

_EP_ID_RE = re.compile(r"[?&]ep=(\d+)")
_EP_NUM_RE = re.compile(r"/ep-(\d+)")
_SLUG_RE = re.compile(r"/watch/([^/?#]+)")
_WATCH_MAIN_RE = re.compile(
    r'id="watch-main"[^>]*data-id="(\d+)"[^>]*data-url="([^"]+)"'
)
_FALLBACK_META_RE = re.compile(r'data-id="(\d+)"[^>]*data-url="([^"]+)"')


class AnikotoClient:
    """Thin, non-interactive client for anikototv.to."""

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
        """Normalise anything (slug, /watch/..., full url, .../ep-N) to a watch url."""
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
        resp = self.get(f"{BASE}/search", params={"keyword": query})
        resp.raise_for_status()
        html = resp.text

        anchors = list(re.finditer(
            r'<a\b([^>]*\bname d-title\b[^>]*)>(.*?)</a>', html, re.S | re.I))
        if not anchors:
            anchors = list(re.finditer(
                r'<a\b([^>]*\bd-title\b[^>]*)>(.*?)</a>', html, re.S | re.I))

        out, seen = [], set()
        for match in anchors:
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
                "jp": clean_text(data.get("data-jp") or ""),
                "url": series,
                "slug": self.slug_of(series),
                "type": _type_near(html, match.start()),
            })
            if len(out) >= limit:
                break
        return out

    def watch_meta(self, url: str) -> dict:
        series = self.series_url(url)
        resp = self.get(series)
        resp.raise_for_status()
        html = resp.text

        match = _WATCH_MAIN_RE.search(html) or _FALLBACK_META_RE.search(html)
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
        if payload.get("status") != 200:
            raise RuntimeError(f"episode list failed: {payload}")
        html = payload.get("result") or payload.get("html") or ""

        episodes = []
        for anchor in re.finditer(r"<a\b([^>]*\bdata-ids=[^>]*)>", html):
            data = attrs_of(anchor.group(1))
            try:
                number = int(data.get("data-num") or data.get("data-slug") or 0)
            except ValueError:
                number = 0

            # the human title lives on the enclosing <li title="..."> or the
            # <span class="d-title"> inside the anchor
            li_start = html.rfind("<li", 0, anchor.start())
            li_tag = html[li_start:anchor.start()] if li_start != -1 else ""
            li_title = re.search(r'title="([^"]*)"', li_tag)
            inner_end = html.find("</a>", anchor.end())
            inner = html[anchor.end():inner_end] if inner_end != -1 else ""
            span = re.search(r'<span[^>]*class="[^"]*d-title[^"]*"[^>]*>(.*?)</span>',
                             inner, re.S | re.I)

            title = ""
            if li_title:
                title = clean_text(li_title.group(1))
            elif span:
                title = clean_text(span.group(1))

            episodes.append({
                "num": number,
                "ep_id": data.get("data-id", ""),
                "slug": data.get("data-slug", ""),
                "mal": data.get("data-mal", ""),
                "timestamp": data.get("data-timestamp", ""),
                "sub": data.get("data-sub") == "1",
                "dub": data.get("data-dub") == "1",
                "ids": data.get("data-ids", ""),
                "title": title or f"Episode {number}",
            })

        episodes.sort(key=lambda item: item["num"])
        return episodes

    def servers(self, ids: str, kind: str = None) -> list:
        url = f"{BASE}/ajax/server/list?servers=" + urllib.parse.quote(ids or "", safe="")
        resp = self._ajax(url)
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("status") != 200:
            raise RuntimeError(f"server list failed: {payload}")
        html = payload.get("result") or payload.get("html") or ""

        # the list is grouped into <div class="type" data-type="sub|dub"> blocks
        markers = [(m.start(), m.group(1))
                   for m in re.finditer(r'data-type="(sub|dub)"', html)]

        out = []
        for match in re.finditer(r"<li\b([^>]*)>", html):
            data = attrs_of(match.group(1))
            if "data-link-id" not in data:
                continue
            end = html.find("</li>", match.end())
            label = clean_text(html[match.end():end] if end != -1 else "")
            current = next((kind_name for pos, kind_name in reversed(markers)
                            if pos < match.start()), "sub")
            out.append({
                "name": label or data.get("data-sv-id", "?"),
                "type": current,
                "server_id": data.get("data-sv-id", ""),
                "link_id": data["data-link-id"],
            })

        if kind:
            filtered = [srv for srv in out if srv["type"] == kind]
            return filtered
        return out

    def embed(self, link_id: str) -> str:
        url = f"{BASE}/ajax/server?get=" + urllib.parse.quote(link_id or "", safe="")
        resp = self._ajax(url)
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("status") != 200:
            raise RuntimeError(f"server resolve failed: {payload}")
        result = payload.get("result") or {}
        link = result.get("url") if isinstance(result, dict) else None
        if not link:
            raise RuntimeError(f"server resolve returned no url: {payload}")
        return link

    def sources(self, embed_url: str) -> dict:
        return resolve_megaplay(
            self.session, embed_url,
            referer=f"{BASE}/", timeout=self.timeout,
            impersonate=self.impersonate,
        )

    def latest_widget(self, name: str = "latest-episode") -> list:
        """Home-page widgets expose /ajax/home/widget/<name>.

        These are what the site's own JS fills in; the markup is identical to
        the 'trending' widget. Returns [] when the widget has no data yet.
        """
        try:
            resp = self._ajax(f"{BASE}/ajax/home/widget/{name}")
            payload = resp.json()
        except Exception as exc:
            print(f"anikoto widget '{name}' failed: {exc}", flush=True)
            return []
        html = payload.get("result") or ""
        out, seen = [], set()
        for block in re.finditer(r'<div class="item">(.*?)</div>\s*</div>', html, re.S):
            chunk = block.group(0)
            link = re.search(r'href="([^"]*/watch/[^"]+)"', chunk)
            if not link:
                continue
            href = link.group(1)
            series = self.series_url(href)
            if series in seen:
                continue
            seen.add(series)
            name_tag = re.search(r'class="name d-title"[^>]*>(.*?)</a>', chunk, re.S)
            num = _EP_NUM_RE.search(href)
            out.append({
                "id": self.slug_of(series),
                "title": clean_text(name_tag.group(1)) if name_tag else self.slug_of(series),
                "url": series,
                "ep_num": int(num.group(1)) if num else None,
            })
        return out


# ──────────────────────────────────────────────────────────────────────────────
#  helpers
# ──────────────────────────────────────────────────────────────────────────────

def _page_title(html: str, fallback: str) -> str:
    """Prefer the page's own <h1>; the og:title is heavily SEO-decorated."""
    for pattern in (
        r'<h1[^>]*class="[^"]*d-title[^"]*"[^>]*>(.*?)</h1>',
        r'<h1[^>]*>(.*?)</h1>',
    ):
        match = re.search(pattern, html, re.S | re.I)
        if match:
            title = clean_text(match.group(1))
            if title:
                return title

    match = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"',
                      html, re.S | re.I)
    if match:
        title = clean_text(match.group(1))
        title = re.sub(r"^Watch\s+", "", title, flags=re.I)
        title = re.sub(r"\s+Episode\s+\d+.*$", "", title, flags=re.I | re.S)
        title = re.sub(r"\s+(English\s+)?(Sub|Dub|SUB|DUB).*$", "", title,
                       flags=re.I | re.S)
        title = re.sub(r"\s+(online\s+)?free\s+on\s+.*$", "", title,
                       flags=re.I | re.S)
        if title.strip():
            return title.strip()

    return fallback.replace("-", " ").title()


def _type_near(html: str, position: int) -> str:
    window = html[max(0, position - 700):position + 700]
    match = re.search(r'<div class="right">\s*([A-Z]{2,7})\s*</div>', window)
    return match.group(1) if match else "ANIME"


# ──────────────────────────────────────────────────────────────────────────────
#  scraper
# ──────────────────────────────────────────────────────────────────────────────

class AnikotoScraper(BaseScraper):
    """Bot-facing scraper for anikototv.to (the 'animetsu' slot)."""

    SOURCE = "anikoto"
    BASE = BASE
    impersonate = "chrome124"

    def __init__(self, download_path="anime_downloads", progress_queue=None,
                 client: AnikotoClient = None):
        super().__init__(download_path, progress_queue)
        self.client = client or AnikotoClient(impersonate=self.impersonate,
                                              proxy=self.proxy)
        self.session = self.client.session

    # ── search ───────────────────────────────────────────────────────────────

    def search_anime(self, query: str, limit: int = 24) -> list:
        # a pasted anikoto link resolves straight to its series entry - the bot's
        # text handler can't tell URLs from queries, so handle it here
        if is_url(query) and detect_source(query) == "anikoto":
            try:
                meta = self.client.watch_meta(query)
            except Exception as exc:
                print(f"anikoto url resolve failed: {exc}", flush=True)
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
                "url": f"{BASE}/watch/{meta['slug']}?ep={episode['ep_id']}",
                "ep_number": str(episode["num"]),
                "ep_id": episode["ep_id"],
                "has_sub": episode.get("sub", False),
                "has_dub": episode.get("dub", False),
            })
        # keep the series name reachable for callers that want it
        for entry in out:
            entry["series_title"] = meta["title"]
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
        """anikototv.to publishes no schedule endpoint - nothing to report."""
        return []

    # ── download recipe ──────────────────────────────────────────────────────

    def resolve_episode(self, url: str) -> dict:
        ep_id_match = _EP_ID_RE.search(url or "")
        ep_num_match = _EP_NUM_RE.search(url or "")
        wanted_id = ep_id_match.group(1) if ep_id_match else None
        wanted_num = int(ep_num_match.group(1)) if ep_num_match else None

        series = self.client.series_url(url)
        meta = self.client.watch_meta(series)
        episodes = self.client.episodes(meta["anime_id"])
        if not episodes:
            raise RuntimeError("no episodes listed for this title")

        episode = None
        if wanted_id:
            episode = next((e for e in episodes if e["ep_id"] == wanted_id), None)
        if episode is None and wanted_num is not None:
            episode = next((e for e in episodes if e["num"] == wanted_num), None)
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
            source = self._first_working_source(episode["ids"], kind)
            if source:
                recipe[kind] = source

        return recipe

    def _first_working_source(self, ids: str, kind: str) -> dict:
        """Walk this episode's servers until one yields a playable m3u8."""
        try:
            servers = self.client.servers(ids, kind)
        except Exception as exc:
            print(f"anikoto {kind} server list failed: {exc}", flush=True)
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
