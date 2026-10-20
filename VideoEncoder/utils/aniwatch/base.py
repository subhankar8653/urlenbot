"""
Ported into urlenbot from AniwatchTvdl (Cantarella bots) — `cantarella/scraper/base.py`.

Changes vs the original (sirf plumbing, scraping logic same):
  * cantarella.core.proxy dependency hata di  -> env ANIWATCH_PROXY (optional)
  * config.FORMAT hata diya                   -> env ANIWATCH_FORMAT, default urlenbot ke
                                                 auto-caption ke hisaab se ("Title S01E05 [Audio] [720p]")
  * N_m3u8DL-RE ko cancel_event milta hai     -> /aniwatch ka ❌ Cancel subprocess bhi maar deta hai
  * DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1   -> libicu ke bina bhi binary chale
  * audio label "JP/EN" -> "Japanese/English/Dual Audio" (auto_caption inhe language maanta hai)

Source-agnostic plumbing shared by every scraper in this package.

Nothing in here knows about anikoto or animesalt. It holds the pieces that used
to be copy-pasted between the old site modules:

  * N_m3u8DL-RE discovery + a wrapper that streams its progress into the
    ``Queue`` that VideoEncoder/plugins/aniwatch_downloader.py drains
  * the progress-queue protocol (status / percent / finished / error)
  * the ffmpeg mux step (video + optional dub audio + optional subtitle tracks)
  * the filename builder driven by ``config.FORMAT``
  * the megaplay player: AES-256-CBC source decryption and the
    ``getSourcesNew`` dance, since both supported sites sit behind it
"""

from __future__ import annotations

import base64
import html as html_lib
import json
import re
import shutil
import subprocess
import time
import urllib.parse
from pathlib import Path
from queue import Queue
from typing import Iterable, Optional

try:
    from curl_cffi import requests as c_requests
except ImportError:  # pragma: no cover - hard dependency of the bot
    c_requests = None

try:
    from Crypto.Cipher import AES
except ImportError:  # pragma: no cover
    AES = None


# ──────────────────────────────────────────────────────────────────────────────
#  constants
# ──────────────────────────────────────────────────────────────────────────────

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Lifted from megaplay's e1-player.min.js. Padded to 32/16 bytes.
MEGA_KEY_SRC = "i?LMTAx0Q6,:}50U"
MEGA_IV_SRC = "W0;27ToaUpl_P%'c"

# Host fragments used to route an arbitrary URL/id to the right scraper.
#
# The legacy domains are still here on purpose: the rest of the bot (search
# handler, callbacks, ongoing) still builds "animetsu.live" / "aniwatchtv.to"
# URLs by hand, so those strings have to keep landing on the right site.
HOST_MARKERS = {
    "anikoto": ("anikototv.to", "anikoto.to", "anikoto",
                "animetsu.live", "animetsu.bz", "animetsu"),
    "animesalt": ("animesalt.to", "animesalt", "aniwatchtv.to", "aniwatch"),
}

# urlenbot ka auto_caption leading "[...]" ko group-tag samajh ke hata deta hai, isliye
# S/E marker naam ke BAAD rakha hai: "Frieren S02E05 [Japanese] [720p]"
DEFAULT_FORMAT = "{title} S{season:0>2}E{episode:0>2} [{audio}] [{quality}]"

_BINARY_CANDIDATES = (
    Path("binary") / "N_m3u8DL-RE",            # linux, repo-local
    Path("binary") / "N_m3u8DL-RE.exe",        # windows, repo-local
    Path("/usr/local/bin/N_m3u8DL-RE"),        # docker / heroku image
)

_ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def _pad(raw: str, size: int) -> bytes:
    b = raw.encode("utf-8")
    return b[:size].ljust(size, b"\x00")


MEGA_KEY = _pad(MEGA_KEY_SRC, 32)
MEGA_IV = _pad(MEGA_IV_SRC, 16)


# ──────────────────────────────────────────────────────────────────────────────
#  small helpers
# ──────────────────────────────────────────────────────────────────────────────

def detect_source(value: str, default: Optional[str] = None) -> Optional[str]:
    """Guess which site a URL / slug / anime id belongs to."""
    text = (value or "").lower()
    for key, markers in HOST_MARKERS.items():
        if any(marker in text for marker in markers):
            return key
    return default


_URL_RE = re.compile(r"^(?:https?://|www\.)", re.I)


def is_url(value: str) -> bool:
    """True when the user pasted something link-shaped rather than a query."""
    return bool(_URL_RE.match((value or "").strip().strip("\"'")))


def sanitize(name: str) -> str:
    """Strip the characters that blow up on Windows (WinError 123)."""
    return re.sub(r'[\\/*?:"<>|]', "", name or "").strip()


def slugify(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]+', "", name or "").strip().rstrip(".")


def clean_text(value: str) -> str:
    return html_lib.unescape(re.sub(r"<[^>]+>", "", value or "")).strip()


def lang_tag(label: str) -> str:
    """'Portuguese (- Portugues(Brasil))' -> 'portuguese_brasil' (file safe)."""
    tag = (label or "und").lower()
    m = re.search(r"\(([^)]*)\)", tag)
    if m:
        base = re.split(r"[-(]", tag)[0].strip()
        tag = f"{base}_{m.group(1)}"
    return re.sub(r"[^a-z0-9]+", "_", tag).strip("_") or "und"


def attrs_of(tag: str) -> dict:
    """Parse ``key="value"`` pairs out of a raw HTML tag body."""
    return dict(re.findall(r'([\w:-]+)="([^"]*)"', tag or ""))


def build_basename(fmt: str, *, season, episode, title, quality, audio) -> str:
    """Apply ``config.FORMAT`` and make the result filesystem-safe."""
    template = fmt or DEFAULT_FORMAT
    try:
        rendered = template.format(
            season=season, episode=episode, title=title,
            quality=quality, audio=audio,
        )
    except (KeyError, IndexError, ValueError):
        rendered = DEFAULT_FORMAT.format(
            season=season, episode=episode, title=title,
            quality=quality, audio=audio,
        )
    return sanitize(rendered) or "episode"


def config_format() -> str:
    """Filename template. Override: env ANIWATCH_FORMAT."""
    import os
    return os.environ.get("ANIWATCH_FORMAT") or DEFAULT_FORMAT


def season_of(title: str) -> str:
    """Best-effort season number from a series title (defaults to '1')."""
    title = title or ""
    for pattern in (
        r"\bSeason\s+(\d+)\b",
        r"\b(\d+)(?:st|nd|rd|th)\s+Season\b",
        r"\bPart\s+(\d+)\b",
        r"\b(\d+)(?:st|nd|rd|th)\s+Part\b",
    ):
        match = re.search(pattern, title, re.I)
        if match:
            return match.group(1)
    match = re.search(r"\s(\d{1,2})$", title.strip())
    if match and 1 <= int(match.group(1)) <= 20:
        return match.group(1)
    return "1"


def make_progress_bar(percent: float, length: int = 10) -> str:
    try:
        percent = max(0.0, min(100.0, float(percent)))
    except (TypeError, ValueError):
        percent = 0.0
    filled = int(length * percent / 100)
    return f"[{'●' * filled}{'○' * (length - filled)}] {percent:.1f}%"


def site_proxy() -> Optional[str]:
    """Optional proxy (env ANIWATCH_PROXY, e.g. http://user:pass@host:port). None = direct."""
    import os
    return (os.environ.get("ANIWATCH_PROXY") or "").strip() or None


def _proxy_map(proxy: Optional[str]) -> dict:
    if not proxy:
        return {}
    return {"http": proxy, "https": proxy}


def make_session(impersonate: str = "chrome124", proxy: Optional[str] = None):
    """A curl_cffi session with browser impersonation + optional proxy."""
    if c_requests is None:
        raise RuntimeError("curl_cffi is not installed")
    try:
        session = c_requests.Session(impersonate=impersonate)
    except TypeError:
        # older curl_cffi: impersonation is per-request
        session = c_requests.Session()
        session._impersonate = impersonate  # type: ignore[attr-defined]
    session.headers.update({
        "user-agent": USER_AGENT,
        "accept-language": "en-US,en;q=0.9",
    })
    proxy_map = _proxy_map(proxy)
    if proxy_map:
        session.proxies.update(proxy_map)
    return session


def http_get(session, url, *, tries: int = 4, base_delay: float = 1.5,
             **kwargs):
    """GET with backoff.

    Both sites drop connections under load (curl error 56) and hand out the odd
    502, so a single failure should never kill a download. 4xx responses are
    returned as-is - those are real answers, not hiccups.
    """
    last = None
    delay = base_delay
    for attempt in range(tries):
        try:
            response = session.get(url, **kwargs)
            if response.status_code < 500 and response.status_code != 429:
                return response
            last = f"HTTP {response.status_code}"
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
        if attempt + 1 < tries:
            time.sleep(delay)
            delay = min(delay * 1.8, 20)
    raise RuntimeError(f"giving up on {url} ({last})")


# ──────────────────────────────────────────────────────────────────────────────
#  progress queue protocol
# ──────────────────────────────────────────────────────────────────────────────

class Reporter:
    """Thin wrapper over the queue that ``telegram.download`` polls.

    Payload shapes it understands (do not change them - the consumer is fixed):

      {'status': str}
      {'percent': '12.3%', 'speed': '1.2 MB/s', 'downloaded': '..',
       'total': '..', 'type': 'sub'|'dub', 'title': str}
      {'finished': True, 'filename': str, 'title': str}
      {'error': str}
    """

    def __init__(self, queue: Optional[Queue] = None):
        self.queue = queue if queue is not None else Queue()

    def put(self, payload: dict) -> None:
        if self.queue is None:
            return
        try:
            self.queue.put(payload)
        except Exception:
            pass

    def status(self, text: str) -> None:
        self.put({"status": text})

    def error(self, text: str) -> None:
        self.put({"error": str(text)})

    def finished(self, filename, title: str) -> None:
        self.put({"finished": True, "filename": str(filename), "title": title})

    def progress(self, *, percent, speed="0 MB/s", downloaded="0 MB",
                 total="0 MB", kind="sub", title="") -> None:
        self.put({
            "percent": percent,
            "speed": speed,
            "downloaded": downloaded,
            "total": total,
            "type": kind,
            "title": title,
        })


# ──────────────────────────────────────────────────────────────────────────────
#  N_m3u8DL-RE
# ──────────────────────────────────────────────────────────────────────────────

_binary_cache: Optional[Path] = None


def get_binary_path() -> Optional[Path]:
    """Locate N_m3u8DL-RE (repo-local binary folder first, then PATH)."""
    global _binary_cache
    if _binary_cache is not None and _binary_cache.exists():
        return _binary_cache
    for candidate in _BINARY_CANDIDATES:
        if candidate.exists():
            _binary_cache = candidate
            return candidate
    which = shutil.which("N_m3u8DL-RE")
    if which:
        _binary_cache = Path(which)
        return _binary_cache
    _binary_cache = None
    return None


def _parse_progress_line(line: str) -> Optional[dict]:
    """Pull percent / speed / progress out of one N_m3u8DL-RE output line.

    A live line looks like::

        Vid 1920x1080 | 2657 Kbps | 23.974 ------ 1/175 0.57% - 1.70MBps 00:03:22

    i.e. segment counts, not byte totals. Some builds do print ``5.2MB/42MB``,
    so both shapes are handled and the segment form is labelled as such.
    """
    percent = re.search(r"(\d+(?:\.\d+)?)%", line)
    if not percent:
        return None

    # the real speed sits *after* the percentage, so look there first
    tail = re.split(r"\d+(?:\.\d+)?%", line)[-1]
    speed = (
        re.search(r"(\d+(?:\.\d+)?\s*[MKG]?i?(?:B/s|bps|b/s|bit/s))", tail, re.I)
        or re.search(r"(\d+(?:\.\d+)?\s*\S+/(?:s|sec))", tail, re.I)
        or re.search(r"(\d+(?:\.\d+)?\s*[MKG]?i?(?:B/s|bps|b/s|bit/s))", line, re.I)
        or re.search(r"(\d+(?:\.\d+)?\s*\S+/(?:s|sec))", line, re.I)
    )

    sizes = re.search(
        r"(\d+(?:\.\d+)?\s*[KMG]i?B)\s*/\s*(\d+(?:\.\d+)?\s*[KMG]i?B)", line, re.I)
    if sizes:
        downloaded, total = sizes.group(1), sizes.group(2)
    else:
        segments = re.search(r"(?<![\d.])(\d+)\s*/\s*(\d+)(?![\d.])", line)
        if segments:
            downloaded = f"{segments.group(1)} seg"
            total = f"{segments.group(2)} seg"
        else:
            downloaded = total = "0 MB"

    return {
        "percent": f"{percent.group(1)}%",
        "speed": speed.group(1) if speed else "0 MB/s",
        "downloaded": downloaded,
        "total": total,
    }


def run_n_m3u8dl(url: str, save_name: str, task_dir, *, kind: str = "sub",
                 reporter: Optional[Reporter] = None, title: str = "",
                 quality: str = "auto", referer: Optional[str] = None,
                 extra_headers: Iterable[str] = (), proxy: Optional[str] = None,
                 threads: int = 50, retries: int = 5,
                 binary: Optional[Path] = None,
                 cancel_event=None) -> bool:
    """Run N_m3u8DL-RE and mirror its progress into the reporter."""
    reporter = reporter or Reporter()
    binary = binary or get_binary_path()
    if not binary or not Path(binary).is_file():
        print(f"N_m3u8DL-RE binary not found ({binary})", flush=True)
        return False

    # --tmp-dir keeps N_m3u8DL-RE's segment scratch folder inside the task dir
    # (default is CWD, which litters the project root with "{save_name}_sub/"
    # folders full of .ts files); --no-log stops it writing a Logs/ tree next to
    # the binary. Both get wiped by _cleanup() once the episode is muxed.
    tmp_dir = Path(task_dir) / "_tmp"
    try:
        tmp_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    cmd = [
        str(binary), url,
        "--save-dir", str(task_dir),
        "--save-name", save_name,
        "--tmp-dir", str(tmp_dir),
        "--no-log",
        "-H", f"User-Agent: {USER_AGENT}",
        "--check-segments-count", "False",
        "-mt",
        "--thread-count", str(threads),
        "--download-retry-count", str(retries),
    ]
    if referer:
        cmd += ["-H", f"Referer: {referer}"]
    for header in extra_headers or ():
        cmd += ["-H", header]
    if proxy:
        cmd += ["--custom-proxy", proxy]

    if str(quality) in ("360", "480", "720", "1080"):
        cmd += ["-sv", f"res='{quality}':for=best"]
    else:
        cmd += ["--auto-select"]

    import os
    import threading
    env = dict(os.environ)
    # libicu install na ho tab bhi .NET binary chale
    env.setdefault("DOTNET_SYSTEM_GLOBALIZATION_INVARIANT", "1")
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0, env=env,
        )
    except (FileNotFoundError, PermissionError, OSError) as exc:
        print(f"could not launch N_m3u8DL-RE: {exc}", flush=True)
        return False

    # ❌ Cancel: job.cancel_ev set hote hi N_m3u8DL-RE ko maar do
    if cancel_event is not None:
        def _watch():
            while proc.poll() is None:
                if cancel_event.wait(0.5):
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    return
        threading.Thread(target=_watch, daemon=True).start()

    buf = b""
    tail: list[str] = []
    while True:
        char = proc.stdout.read(1)
        if not char:
            break
        if char in (b"\r", b"\n"):
            line = _ANSI_RE.sub("", buf.decode("utf-8", errors="replace")).strip()
            buf = b""
            if not line:
                continue
            print(f"[{kind.upper()}] {line}", flush=True)
            tail.append(line)
            del tail[:-5]
            parsed = _parse_progress_line(line)
            if parsed:
                reporter.progress(kind=kind, title=title, **parsed)
        else:
            buf += char

    proc.wait()
    if proc.returncode != 0:
        detail = "\n".join(tail) or "no output captured"
        print(f"[{kind.upper()}] exited {proc.returncode}:\n{detail}", flush=True)
    return proc.returncode == 0


def collect_output(task_dir, save_name: str) -> Optional[Path]:
    """N_m3u8DL-RE picks its own container - find whatever it wrote."""
    directory = Path(task_dir)
    for ext in (".mp4", ".mkv", ".ts", ".m4a", ".m4v", ".webm"):
        candidate = directory / f"{save_name}{ext}"
        if candidate.exists():
            return candidate
    for candidate in sorted(directory.glob(f"{save_name}.*")):
        if candidate.is_file() and candidate.suffix != ".vtt":
            return candidate
    return None


# ──────────────────────────────────────────────────────────────────────────────
#  master-playlist handling
#
#  megaplay frequently publishes a single variant (e.g. only 1920x1080). Handing
#  N_m3u8DL-RE `-sv res='360':for=best` then aborts with "No stream found to
#  download", which used to kill every 360p/720p job. So we read the master
#  ourselves, pick the closest variant that actually exists, and hand over a
#  playlist that is guaranteed to contain something.
# ──────────────────────────────────────────────────────────────────────────────

def parse_variants(master_text: str, master_url: str) -> list:
    """Real (non I-frame) variants from a master playlist."""
    lines = [line.strip() for line in (master_text or "").splitlines()]
    out = []
    for index, line in enumerate(lines):
        # NB: #EXT-X-I-FRAME-STREAM-INF does not start with #EXT-X-STREAM-INF,
        # so scrub-only I-frame playlists are skipped for free
        if not line.startswith("#EXT-X-STREAM-INF"):
            continue
        if index + 1 >= len(lines) or lines[index + 1].startswith("#"):
            continue
        resolution = re.search(r"RESOLUTION=(\d+)x(\d+)", line)
        bandwidth = re.search(r"BANDWIDTH=(\d+)", line)
        height = int(resolution.group(2)) if resolution else 0
        out.append({
            "height": height,
            "label": f"{height}p" if height else "auto",
            "bandwidth": int(bandwidth.group(1)) if bandwidth else 0,
            "url": urllib.parse.urljoin(master_url, lines[index + 1]),
        })
    return out


def choose_variant(variants: list, quality) -> Optional[dict]:
    """Exact height if present, else the nearest one below, else the best."""
    if not variants:
        return None
    try:
        want = int(str(quality).replace("p", ""))
    except (TypeError, ValueError):
        want = None
    if want is None:
        return max(variants, key=lambda v: (v["bandwidth"], v["height"]))
    exact = [v for v in variants if v["height"] == want]
    if exact:
        return max(exact, key=lambda v: v["bandwidth"])
    below = [v for v in variants if v["height"] and v["height"] < want]
    if below:
        return max(below, key=lambda v: v["height"])
    return max(variants, key=lambda v: (v["bandwidth"], v["height"]))


def prepare_stream(session, m3u8: str, *, referer: Optional[str] = None,
                   quality="auto", impersonate: Optional[str] = None,
                   timeout: int = 40) -> dict:
    """Return ``{'url': ..., 'quality': ..., 'label': ...}`` for a download.

    ``label`` is the resolution that will actually be fetched, so filenames and
    the bot's ``[1080p]`` caption tag tell the truth even when the requested
    quality does not exist on the CDN.
    """
    fallback = {
        "url": m3u8,
        "quality": quality,
        "label": f"{quality}p" if str(quality) in ("360", "480", "720", "1080") else "auto",
    }
    if not m3u8:
        return fallback

    kwargs = {"impersonate": impersonate} if impersonate else {}
    try:
        response = http_get(session, m3u8, headers={"referer": referer or ""},
                            timeout=timeout, **kwargs)
        master = response.text
    except Exception as exc:
        print(f"could not read the master playlist ({exc}); using it as-is", flush=True)
        return fallback

    if "EXT-X-STREAM-INF" not in master:
        # already a media playlist - one stream, nothing to choose
        return {"url": m3u8, "quality": "auto", "label": fallback["label"]}

    variants = parse_variants(master, m3u8)
    chosen = choose_variant(variants, quality)
    if not chosen:
        return fallback

    # a master with separate audio/subtitle renditions has to stay the master,
    # otherwise the extra tracks are lost - so filter by the exact resolution
    if re.search(r"#EXT-X-MEDIA:", master):
        return {"url": m3u8, "quality": str(chosen["height"]) if chosen["height"] else quality,
                "label": chosen["label"]}

    return {"url": chosen["url"], "quality": "auto", "label": chosen["label"]}


# ──────────────────────────────────────────────────────────────────────────────
#  ffmpeg mux
# ──────────────────────────────────────────────────────────────────────────────

_LANG_ISO = {
    "english": "eng", "arabic": "ara", "french": "fre", "german": "ger",
    "italian": "ita", "portuguese": "por", "russian": "rus", "spanish": "spa",
    "japanese": "jpn", "korean": "kor", "chinese": "chi", "hindi": "hin",
    "tamil": "tam", "telugu": "tel", "indonesian": "ind", "malay": "may",
    "thai": "tha", "vietnamese": "vie", "turkish": "tur", "polish": "pol",
    "dutch": "dut", "swedish": "swe", "danish": "dan", "finnish": "fin",
    "norwegian": "nor", "greek": "gre", "hebrew": "heb", "czech": "cze",
    "hungarian": "hun", "romanian": "rum", "ukrainian": "ukr", "filipino": "fil",
    "burmese": "bur", "khmer": "khm", "urdu": "urd", "bengali": "ben",
    "persian": "per", "malayalam": "mal", "kannada": "kan",
}


def subtitle_meta(tag: str) -> tuple:
    """``'spanish_latin_america'`` -> ``('spa', 'Spanish Latin America')``.

    Without this every subtitle track shows up as an unnamed "Track 1/2/3" in
    the player, because ffmpeg does not infer a language from a .vtt file.
    """
    words = [word for word in (tag or "").split("_") if word]
    iso = _LANG_ISO.get(words[0] if words else "", "und")

    # "portuguese_portuguese_brazil" -> "Portuguese Brazil"
    deduped = []
    for word in words:
        if not deduped or deduped[-1] != word:
            deduped.append(word)

    label = " ".join(word.capitalize() for word in deduped) or "Unknown"
    return iso, label


def _promote(src, dst) -> Path:
    """Move a finished download into place, keeping its real container.

    N_m3u8DL-RE writes .mp4 for a plain muxed stream; renaming that to .mkv
    would hand Telegram an MP4 body under an MKV name, so the suffix follows
    the source whenever there was no ffmpeg pass.
    """
    src, dst = Path(src), Path(dst)
    if src.suffix and src.suffix.lower() != dst.suffix.lower():
        dst = dst.with_suffix(src.suffix)
    try:
        if dst.exists():
            dst.unlink()
        src.replace(dst)
    except Exception as exc:
        print(f"could not move {src.name} -> {dst.name}: {exc}", flush=True)
    return dst


def merge_tracks(video_path, final_path, *, dub_audio=None, subtitles=(),
                 audio_lang: str = "jpn", reporter: Optional[Reporter] = None,
                 title: str = "") -> Optional[Path]:
    """Mux video (+ second audio + subtitles) into the final file.

    Runs whenever ffmpeg is available, even for a plain single-audio episode:
    a ``-c copy`` remux costs a second or two and is the only way to stamp the
    audio/subtitle languages, which otherwise show up as unnamed "Track 1/2/3"
    in the player. Without ffmpeg (or if the mux fails) the raw download is
    promoted instead, keeping its real container, so the user always gets
    *something*. Returns the path that actually exists on disk.
    """
    reporter = reporter or Reporter()
    video_path = Path(video_path)
    final_path = Path(final_path)
    if not video_path.exists():
        reporter.error("video file vanished before the mux step")
        return None

    dub = Path(dub_audio) if dub_audio else None
    if dub is not None and not dub.exists():
        dub = None

    subs = [(Path(p), tag) for p, tag in (subtitles or ()) if Path(p).exists()]

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return _promote(video_path, final_path)

    reporter.status(f"🎬 **Merging tracks: {title}**\nPlease wait...")

    def _build(use_subs: bool, use_dub: bool) -> list:
        sub_list = subs if use_subs else []
        dub_file = dub if use_dub else None
        cmd = [ffmpeg, "-y", "-i", str(video_path)]
        if dub_file:
            cmd += ["-i", str(dub_file)]
        for sub_path, _ in sub_list:
            cmd += ["-i", str(sub_path)]

        cmd += ["-map", "0:v", "-map", "0:a"]
        if dub_file:
            cmd += ["-map", "1:a:0"]
        offset = 2 if dub_file else 1
        for index in range(len(sub_list)):
            cmd += ["-map", f"{index + offset}:s"]

        # MP4 + mov_text: urlenbot ka _auto_rename / upload pipeline hamesha .mp4
        # banata hai (ffmpeg -c copy), aur MP4 mein SRT/ASS copy nahi hota — isliye
        # subtitles yahin mov_text mein convert hote hain.
        cmd += ["-c", "copy"]
        if sub_list:
            cmd += ["-c:s", "mov_text"]

        primary_title = "Japanese" if audio_lang == "jpn" else "English"
        cmd += ["-metadata:s:a:0", f"language={audio_lang}",
                "-metadata:s:a:0", f"title={primary_title}"]
        if dub_file:
            cmd += ["-metadata:s:a:1", "language=eng", "-metadata:s:a:1", "title=English"]

        for index, (_, tag) in enumerate(sub_list):
            iso, label = subtitle_meta(tag)
            cmd += [f"-metadata:s:s:{index}", f"language={iso}",
                    f"-metadata:s:s:{index}", f"title={label}"]

        if sub_list:
            cmd += ["-disposition:s:0", "default"]
        cmd += ["-movflags", "+faststart", str(final_path)]
        return cmd

    # pehle sab kuch (dub + subs), phir sirf dub, phir bina subs ke — jo bhi chal jaye
    attempts = [(True, True)]
    if subs:
        attempts.append((False, True))
    if dub and subs:
        attempts.append((False, False))
    for use_subs, use_dub in attempts:
        try:
            subprocess.run(_build(use_subs, use_dub), check=True, capture_output=True)
            return final_path
        except Exception as exc:
            detail = ""
            err = getattr(exc, "stderr", None)
            if err:
                detail = err.decode("utf-8", errors="replace").strip().splitlines()[-1:]  # last line
            print(f"ffmpeg merge failed (subs={use_subs}, dub={use_dub}): {exc} {detail}", flush=True)
            try:
                if final_path.exists():
                    final_path.unlink()
            except Exception:
                pass

    print("ffmpeg merge failed everywhere; keeping the raw video", flush=True)
    return _promote(video_path, final_path)


# ──────────────────────────────────────────────────────────────────────────────
#  megaplay player
# ──────────────────────────────────────────────────────────────────────────────

def mega_decrypt(enc: str) -> dict:
    """AES-256-CBC / PKCS#7 / base64url -> megaplay sources dict."""
    if AES is None:
        raise RuntimeError("pycryptodome is not installed")
    payload = enc.replace("-", "+").replace("_", "/")
    payload += "=" * (-len(payload) % 4)
    plain = AES.new(MEGA_KEY, AES.MODE_CBC, MEGA_IV).decrypt(base64.b64decode(payload))
    return json.loads(plain[:-plain[-1]].decode("utf-8"))


def resolve_megaplay(session, embed_url: str, *, referer: Optional[str] = None,
                     timeout: int = 40, impersonate: Optional[str] = None) -> dict:
    """Turn a megaplay embed URL into ``{file, tracks, origin, ...}``.

    The CDN hotlink-checks ``Referer`` and it has to be the bare player origin
    (``https://megaplay.buzz/``) - the site's own domain or a deep path gets a
    403. The embed's query string must never be forwarded either: some sites
    tack on ``?s=tcdn`` which flips the backend onto an ad-slideshow playlist
    whose "segments" are all .png files.
    """
    split = urllib.parse.urlsplit(embed_url)
    origin = f"{split.scheme}://{split.netloc}"

    kwargs = {}
    if impersonate:
        kwargs["impersonate"] = impersonate

    page = session.get(embed_url, headers={"referer": referer or f"{origin}/"},
                       timeout=timeout, **kwargs)
    page.raise_for_status()
    match = re.search(r'data-id="(\d+)"', page.text)
    if not match:
        raise RuntimeError("no data-id on the megaplay embed page")
    data_id = match.group(1)

    last = None
    for path in ("stream/getSourcesNew", "stream/getSources"):
        resp = session.get(
            f"{origin}/{path}?id={data_id}",
            headers={
                "referer": embed_url,
                "x-requested-with": "XMLHttpRequest",
                "accept": "*/*",
            },
            timeout=timeout,
            **kwargs,
        )
        resp.raise_for_status()
        payload = resp.json()

        if "enc" in payload:
            data = mega_decrypt(payload["enc"])
        elif payload.get("sources"):
            raw = payload["sources"]
            data = raw if isinstance(raw, dict) else json.loads(raw)
        else:
            last = payload
            continue

        file_url = data.get("file") or data.get("url")
        if not file_url:
            last = data
            continue

        return {
            "file": file_url,
            "tracks": payload.get("tracks") or [],
            "intro": payload.get("intro"),
            "outro": payload.get("outro"),
            "embed": embed_url,
            "origin": origin,
            "referer": f"{origin}/",
            "raw": data,
        }

    raise RuntimeError(f"no playable sources in the getSources response: {last}")


def fetch_subtitles(session, tracks, out_dir, base_name: str, *, referer: str,
                    langs: Optional[list] = None, timeout: int = 20,
                    impersonate: Optional[str] = None) -> list:
    """Download the caption tracks and return ``[(path, lang_tag), ...]``."""
    saved: list = []
    kwargs = {"impersonate": impersonate} if impersonate else {}
    for index, track in enumerate(tracks or []):
        url = track.get("file") or track.get("url")
        if not url:
            continue
        kind = (track.get("kind") or "").lower()
        if kind and kind not in ("captions", "subtitles"):
            continue
        label = track.get("label") or f"sub_{index}"
        if langs and not any(needle in label.lower() for needle in langs):
            continue
        dest = Path(out_dir) / f"{base_name}_{lang_tag(label)}.vtt"
        try:
            resp = session.get(url, headers={"referer": referer},
                               timeout=timeout, **kwargs)
            if resp.status_code == 200 and resp.content:
                dest.write_bytes(resp.content)
                saved.append((dest, lang_tag(label)))
                print(f"   subs: {dest.name}", flush=True)
        except Exception as exc:
            print(f"   subtitle failed ({label}): {exc}", flush=True)
    return saved


# ──────────────────────────────────────────────────────────────────────────────
#  BaseScraper - the download pipeline every site inherits
# ──────────────────────────────────────────────────────────────────────────────

class BaseScraper:
    """Common behaviour for a site scraper.

    Subclasses implement the site-specific reads (``search_anime``,
    ``list_episodes``, ``resolve_episode``); everything downstream of that -
    quality loops, retries, subtitle capture, muxing, progress reporting - is
    handled here so both sites behave identically to the rest of the bot.
    """

    SOURCE = "base"
    BASE = ""

    # ── lifecycle ────────────────────────────────────────────────────────────

    impersonate = "chrome124"
    # threading.Event — set hone par running download band (plugin job.cancel_ev yahin set karta hai)
    cancel_event = None

    def _cancelled(self) -> bool:
        ev = self.cancel_event
        return bool(ev is not None and ev.is_set())

    def __init__(self, download_path="anime_downloads", progress_queue=None):
        self.download_path = Path(download_path)
        self.download_path.mkdir(parents=True, exist_ok=True)
        self.reporter = Reporter(progress_queue)
        self.proxy = site_proxy()
        self.binary_path = get_binary_path()
        # subclasses point this at their site client's session
        self.session = None

    # ── subclass contract ────────────────────────────────────────────────────

    def search_anime(self, query: str, limit: int = 24) -> list:
        raise NotImplementedError

    def list_episodes(self, value: str) -> list:
        raise NotImplementedError

    def resolve_episode(self, url: str) -> dict:
        """Return the download recipe for one episode.

        Required keys::

            {
              'series': str, 'season': str, 'episode': str, 'ep_title': str,
              'referer': str,
              'sub': {'file': m3u8, 'tracks': [...]} | None,
              'dub': {'file': m3u8, 'tracks': [...]} | None,
            }
        """
        raise NotImplementedError

    # ── optional, subclass may override ──────────────────────────────────────

    def get_episode_info(self, url: str) -> tuple:
        """``(anime_name, ep_num, ep_title, season)`` - legacy helper."""
        try:
            recipe = self.resolve_episode(url)
            return (recipe.get("series", "Anime"),
                    str(recipe.get("episode", "0")),
                    recipe.get("ep_title", "Unknown"),
                    str(recipe.get("season", "1")))
        except Exception:
            return ("Anime", "0", "Unknown", "1")

    def get_anime_info(self, anime_id):
        raise NotImplementedError

    def get_episode_servers(self, *args, **kwargs):
        return []

    def get_schedule(self, date_str: str = None) -> list:
        return []

    def get_home_sections(self) -> dict:
        return {}

    def fetch_recently_updated(self, page: int = 1, per_page: int = 20) -> list:
        return []

    # ── download entry points (mirror the legacy downloader API) ─────────────

    def download_episode(self, url, quality="auto", name_override=None,
                         season_override=None, ep_num_override=None) -> bool:
        # 'all' means "every quality the CDN actually has". The scrapers only
        # publish one variant per title more often than not, so the same file
        # would otherwise be fetched three times under three different names.
        produced = set() if quality == "all" else None
        wanted = ("360", "720", "1080") if quality == "all" else (quality,)

        overall = False
        for target in wanted:
            label = self._download_with_retry(url, target, name_override,
                                              season_override, ep_num_override,
                                              produced=produced)
            if label:
                overall = True
        return overall

    def download_all_episodes(self, url, quality="auto") -> bool:
        for entry in self.list_episodes(url):
            self.download_episode(entry.get("url", url), quality=quality)
        return True

    def download_range(self, url, start, end, quality="auto") -> bool:
        for entry in self.list_episodes(url):
            try:
                number = int(entry.get("ep_number") or 0)
            except (TypeError, ValueError):
                continue
            if start <= number <= end:
                self.download_episode(entry.get("url", url), quality=quality)
        return True

    # ── internals ────────────────────────────────────────────────────────────

    def _download_with_retry(self, url, quality, name_override, season_override,
                             ep_num_override, attempts: int = 3,
                             produced: Optional[set] = None):
        """Returns the resolution label that was written, or None on failure."""
        for attempt in range(attempts):
            if self._cancelled():
                return None
            try:
                label = self._download_single(url, quality, name_override,
                                              season_override, ep_num_override,
                                              produced=produced)
                if label:
                    return label
                print(f"download attempt {attempt + 1} failed, retrying...", flush=True)
            except Exception as exc:
                print(f"download error on attempt {attempt + 1}: {exc}", flush=True)
            if attempt + 1 < attempts and not self._cancelled():
                time.sleep(5)
        return None

    def _download_single(self, url, quality, name_override, season_override,
                         ep_num_override, produced: Optional[set] = None):
        recipe = self.resolve_episode(url)
        if not recipe:
            self.reporter.error("could not resolve this episode")
            return None

        sub = recipe.get("sub")
        dub = recipe.get("dub")
        if not sub and not dub:
            self.reporter.error("no video source found for this episode")
            return None

        series = name_override or recipe.get("series") or "Anime"
        season = str(season_override or recipe.get("season") or "1")
        episode = str(ep_num_override or recipe.get("episode") or "0")
        ep_title = recipe.get("ep_title") or f"Episode {episode}"

        primary = sub or dub
        referer = recipe.get("referer") or primary.get("referer") or self.BASE + "/"

        # work out which stream we will really get, so the filename and the
        # bot's [1080p] tag never lie about the quality
        stream = self._prepare(primary, referer, quality)
        qual_label = stream["label"]

        base_name = build_basename(
            config_format(),
            season=season, episode=episode, title=series,
            quality=qual_label,
            audio="Dual Audio" if (sub and dub) else ("English" if dub and not sub else "Japanese"),
        )

        if produced is not None and base_name in produced:
            print(f"{qual_label} is the only variant available - already fetched, skipping",
                  flush=True)
            return qual_label

        task_dir = self.download_path / f"{self.SOURCE}_{abs(hash(base_name)):x}_{qual_label}"
        task_dir.mkdir(parents=True, exist_ok=True)

        final_file = self.download_path / f"{base_name}.mp4"

        try:
            self.reporter.status(
                f"📥 **Downloading: {series} [{qual_label}]**\nPlease wait..."
            )

            # ── dub runs alongside the main download ──
            dub_state = {"path": None}
            dub_thread = None
            if sub and dub and dub.get("file"):
                import threading

                def fetch_dub():
                    prepared = self._prepare(dub, referer, quality)
                    if run_n_m3u8dl(
                        prepared["url"], f"{base_name}_dub", task_dir, kind="dub",
                        reporter=self.reporter, title=ep_title,
                        quality=prepared["quality"],
                        referer=dub.get("referer") or referer,
                        proxy=self.proxy,
                        binary=self.binary_path,
                        cancel_event=self.cancel_event,
                    ):
                        found = collect_output(task_dir, f"{base_name}_dub")
                        if found:
                            dub_state["path"] = found
                    else:
                        self.reporter.status(
                            "⚠️ **Dub download failed**\nProceeding with Japanese only."
                        )

                dub_thread = threading.Thread(target=fetch_dub, daemon=True)
                dub_thread.start()

            # ── main (sub) download ──
            ok = run_n_m3u8dl(
                stream["url"], f"{base_name}_sub", task_dir, kind="sub",
                reporter=self.reporter, title=ep_title, quality=stream["quality"],
                referer=primary.get("referer") or referer,
                proxy=self.proxy,
                binary=self.binary_path,
                cancel_event=self.cancel_event,
            )

            if dub_thread:
                dub_thread.join()

            if not ok:
                self.reporter.error("video download failed")
                return None

            # keep whatever container N_m3u8DL-RE chose - ffmpeg probes the
            # real format, and renaming .mp4 to .mkv only confuses Telegram
            video_path = collect_output(task_dir, f"{base_name}_sub")
            if not video_path:
                self.reporter.error("downloader reported success but wrote no file")
                return None

            # ── subtitles (from the sub source; dub sources rarely carry any) ──
            sub_files = []
            if self.session is not None:
                sub_files = fetch_subtitles(
                    self.session, (primary.get("tracks") or []), task_dir, base_name,
                    referer=primary.get("referer") or referer,
                    impersonate=self.impersonate,
                )

            merge_tracks(
                video_path, final_file,
                dub_audio=dub_state["path"],
                subtitles=sub_files,
                audio_lang="jpn" if sub else "eng",
                reporter=self.reporter,
                title=ep_title,
            )
        finally:
            _cleanup(task_dir)

        # merge_tracks may have kept the source container instead of the
        # requested .mp4, so re-resolve whatever actually landed on disk
        produced_path = final_file
        if not produced_path.exists():
            candidates = sorted(
                self.download_path.glob(f"{base_name}.*"),
                key=lambda p: p.stat().st_mtime if p.exists() else 0,
                reverse=True,
            )
            candidates = [p for p in candidates if p.suffix.lower() != ".vtt"]
            produced_path = candidates[0] if candidates else None

        if not produced_path or not produced_path.exists():
            self.reporter.error("could not produce the final file")
            return None

        if produced is not None:
            produced.add(base_name)

        self.reporter.finished(produced_path, base_name)
        return qual_label

    def _prepare(self, source: dict, referer: str, quality) -> dict:
        """Resolve a source's master playlist to a concrete, downloadable URL."""
        if self.session is None or not source.get("file"):
            return {
                "url": source.get("file"),
                "quality": quality,
                "label": f"{quality}p" if str(quality) in ("360", "480", "720", "1080") else "auto",
            }
        return prepare_stream(
            self.session, source["file"],
            referer=source.get("referer") or referer,
            quality=quality, impersonate=self.impersonate,
        )


def _cleanup(directory) -> None:
    """Best-effort removal of a task dir.

    Windows keeps ffmpeg/N_m3u8DL-RE handles alive for a moment after the
    process exits, so a single rmtree can fail on a 300 MB temp file and - with
    ignore_errors - silently leave it behind. Retry, then delete piecemeal.
    """
    path = Path(directory)
    for _ in range(5):
        if not path.exists():
            return
        try:
            shutil.rmtree(path)
            return
        except Exception:
            time.sleep(0.6)

    for item in sorted(path.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        try:
            item.unlink() if item.is_file() else item.rmdir()
        except Exception:
            pass
    try:
        path.rmdir()
    except Exception:
        pass
