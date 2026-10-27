"""
track_branding.py
=================
Audio / Subtitle tracks ke naam pe community ka naam lagata hai, taaki MX Player (ya koi bhi
player) mein track list kuch aisi dikhe:

    [ Hindi ] by sbanime        <- audio track
    [ English ] by sbanime      <- subtitle track

* Community naam automatic aata hai (utils/community.py — /community se set kiya hua,
  kuch set nahi to default 'sbanime').
* Har stream ki apni language (ffprobe 'language' tag) se naam banta hai. Language tag na ho
  to `audio_fallback` (jaise user ne jo language chuni) / subtitle ke liye 'English' use hota hai.
* MP4 mein per-track `title` nahi likha jaata, player `handler_name` padhta hai — isliye dono
  (title + handler_name) same likhe jaate hain.

Do tarike:
  brand_args(filepath, community, ...)      -> ffmpeg ke -metadata:s:... args (sync)
  brand_file(filepath, ..., to_mp4=False)   -> file ko rewrite karke naya path (async)
"""

import asyncio
import json
import os
import subprocess

from .. import LOGGER

# ISO 639-2 (ffmpeg/MX Player ke liye) + dikhne wala naam
_LANGS = {
    "hin": ("hin", "Hindi"), "hi": ("hin", "Hindi"), "hindi": ("hin", "Hindi"),
    "eng": ("eng", "English"), "en": ("eng", "English"), "english": ("eng", "English"),
    "jpn": ("jpn", "Japanese"), "ja": ("jpn", "Japanese"), "jap": ("jpn", "Japanese"),
    "japanese": ("jpn", "Japanese"),
    "tam": ("tam", "Tamil"), "ta": ("tam", "Tamil"), "tamil": ("tam", "Tamil"),
    "tel": ("tel", "Telugu"), "te": ("tel", "Telugu"), "telugu": ("tel", "Telugu"),
    "mal": ("mal", "Malayalam"), "ml": ("mal", "Malayalam"), "malayalam": ("mal", "Malayalam"),
    "kan": ("kan", "Kannada"), "kn": ("kan", "Kannada"), "kannada": ("kan", "Kannada"),
    "ben": ("ben", "Bengali"), "bn": ("ben", "Bengali"), "bengali": ("ben", "Bengali"),
    "mar": ("mar", "Marathi"), "mr": ("mar", "Marathi"), "marathi": ("mar", "Marathi"),
    "kor": ("kor", "Korean"), "ko": ("kor", "Korean"), "korean": ("kor", "Korean"),
    "chi": ("zho", "Chinese"), "zho": ("zho", "Chinese"), "zh": ("zho", "Chinese"),
    "chinese": ("zho", "Chinese"),
    "spa": ("spa", "Spanish"), "es": ("spa", "Spanish"), "spanish": ("spa", "Spanish"),
    "fre": ("fra", "French"), "fra": ("fra", "French"), "fr": ("fra", "French"),
    "ger": ("deu", "German"), "deu": ("deu", "German"), "de": ("deu", "German"),
    "ara": ("ara", "Arabic"), "ar": ("ara", "Arabic"), "arabic": ("ara", "Arabic"),
    "ind": ("ind", "Indonesian"), "id": ("ind", "Indonesian"),
    "por": ("por", "Portuguese"), "pt": ("por", "Portuguese"),
    "rus": ("rus", "Russian"), "ru": ("rus", "Russian"),
}


def lang_info(code, fallback: str = ""):
    """('hin', 'Hindi') — code pehchaan mein na aaye to fallback naam se, warna (None, None)."""
    c = (code or "").strip().lower()
    if c in _LANGS:
        return _LANGS[c]
    fb = (fallback or "").strip().lower()
    if fb in _LANGS:
        return _LANGS[fb]
    return (None, None)


def make_title(lang_name: str, community: str) -> str:
    return f"[ {lang_name} ] by {community}"


async def get_community() -> str:
    """Community naam (lowercase, @ ke bina). Default 'sbanime'."""
    try:
        from .community import get_community_name
        return (await get_community_name()) or "sbanime"
    except Exception:
        return "sbanime"


def _probe_streams(filepath: str) -> list:
    try:
        out = subprocess.check_output(
            ["ffprobe", "-hide_banner", "-v", "quiet", "-print_format", "json", "-show_streams", filepath],
            stderr=subprocess.DEVNULL, timeout=60,
        )
        return json.loads(out.decode()).get("streams", [])
    except Exception as e:
        LOGGER.warning(f"[Brand] ffprobe fail: {e!r}")
        return []


def _lang_tag(stream: dict) -> str:
    tags = stream.get("tags") or {}
    return (tags.get("language") or tags.get("LANGUAGE") or "").strip().lower()


def brand_args(filepath: str, community: str, audio_fallback: str = "", sub_fallback: str = "English",
               streams: list = None) -> list:
    """
    ffmpeg ke args: har audio stream (-metadata:s:a:N) aur har subtitle stream (-metadata:s:s:N) ka
    title + handler_name + language. Video stream ko haath nahi lagate.
    """
    streams = streams if streams is not None else _probe_streams(filepath)
    args = []
    ai = si = 0
    for s in streams:
        ctype = s.get("codec_type")
        if ctype == "audio":
            iso, name = lang_info(_lang_tag(s), audio_fallback)
            if name:
                title = make_title(name, community)
                args += [f"-metadata:s:a:{ai}", f"title={title}",
                         f"-metadata:s:a:{ai}", f"handler_name={title}",
                         f"-metadata:s:a:{ai}", f"language={iso}"]
            ai += 1
        elif ctype == "subtitle":
            iso, name = lang_info(_lang_tag(s), sub_fallback)
            if name:
                title = make_title(name, community)
                args += [f"-metadata:s:s:{si}", f"title={title}",
                         f"-metadata:s:s:{si}", f"handler_name={title}",
                         f"-metadata:s:s:{si}", f"language={iso}"]
            si += 1
    return args


async def _run(cmd: list, timeout: int = 3600) -> bool:
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            from . import job_ctl
            job_ctl.register_proc(proc)          # ❌ Cancel pe ffmpeg bhi kill
        except Exception:
            pass
        _, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        if proc.returncode != 0:
            LOGGER.warning(f"[Brand] ffmpeg fail: {(err or b'').decode(errors='replace')[-300:]}")
            return False
        return True
    except asyncio.CancelledError:
        raise
    except Exception as e:
        LOGGER.warning(f"[Brand] ffmpeg crash: {e!r}")
        return False


async def brand_file(filepath: str, audio_fallback: str = "", sub_fallback: str = "English",
                     to_mp4: bool = False, community: str = None) -> str:
    """
    File ke audio + subtitle tracks ka naam '[ Lang ] by community' kar do (stream copy — re-encode nahi).
    to_mp4=True: MP4 banao (subtitles -> mov_text, attachments/fonts hata ke) — channel upload ke liye.
    Returns final path. Kuch bhi fail ho to original path (upload na ruke).
    """
    if not filepath or not os.path.isfile(filepath):
        return filepath
    community = community or await get_community()
    loop = asyncio.get_event_loop()
    streams = await loop.run_in_executor(None, _probe_streams, filepath)
    meta = brand_args(filepath, community, audio_fallback, sub_fallback, streams=streams)
    if not meta and not to_mp4:
        return filepath

    base, ext = os.path.splitext(filepath)
    out = base + ("_brand.mp4" if to_mp4 else f"_brand{ext}")
    has_sub = any(s.get("codec_type") == "subtitle" for s in streams)

    if to_mp4:
        head = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", filepath,
                "-map", "0:v:0", "-map", "0:a?", "-map", "0:s?"]
        tail = meta + ["-movflags", "+faststart", out]
        attempts = [
            head + ["-c:v", "copy", "-c:a", "copy"] + (["-c:s", "mov_text"] if has_sub else []) + tail,
            head + ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ac", "2"]
            + (["-c:s", "mov_text"] if has_sub else []) + tail,
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", filepath,
             "-map", "0:v:0", "-map", "0:a?", "-sn", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ac", "2"]
            + [a for i in range(0, len(meta), 2) if "s:s:" not in meta[i] for a in meta[i:i + 2]]
            + ["-movflags", "+faststart", out],
        ]
    else:
        attempts = [["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", filepath,
                     "-map", "0", "-c", "copy"] + meta + [out]]

    for cmd in attempts:
        if await _run(cmd):
            if os.path.isfile(out) and os.path.getsize(out) > 0:
                try:
                    os.remove(filepath)
                except Exception:
                    pass
                return out
        try:
            if os.path.exists(out):
                os.remove(out)
        except Exception:
            pass
    LOGGER.warning("[Brand] branding nahi ho paayi — original file hi use hogi")
    return filepath
