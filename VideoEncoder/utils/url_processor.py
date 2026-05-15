"""
URL Processor Utility
Helper functions used by url_upload.py plugin:
  - apply_name_swap
  - get_audio_streams
  - build_metadata_ffmpeg_args
  - process_url_file
"""

import json
import os
import re
import subprocess

from .. import LOGGER


def get_audio_streams(filepath: str) -> list[dict]:
    """
    ffprobe se saare audio streams ka info lo.
    Returns list of dicts:
      { 'index': int, 'lang': str, 'title': str, 'codec': str }
    """
    try:
        cmd = [
            "ffprobe", "-hide_banner", "-print_format", "json",
            "-show_streams", "-select_streams", "a",
            filepath
        ]
        output = subprocess.check_output(cmd, stderr=subprocess.DEVNULL)
        streams_raw = json.loads(output.decode()).get("streams", [])
        result = []
        for s in streams_raw:
            tags = s.get("tags", {})
            result.append({
                "index": s.get("index", 0),
                "lang": tags.get("language", tags.get("LANGUAGE", "")),
                "title": tags.get("title", tags.get("TITLE", tags.get("handler_name", ""))),
                "codec": s.get("codec_name", ""),
            })
        return result
    except Exception as e:
        LOGGER.error(f"get_audio_streams failed: {e}")
        return []


def apply_name_swap(filename: str, rules: dict) -> str:
    """
    filename mein rules apply karo (case-insensitive replace).
    rules = { 'toonweb': 'sbanime', 'ToonWeb': 'sbanime', ... }
    
    Example:
      filename = "[ToonWeb] Naruto S01E01.mkv"
      rules    = {"toonweb": "sbanime"}
      result   = "[sbanime] Naruto S01E01.mkv"
    """
    result = filename
    for from_text, to_text in rules.items():
        # Case-insensitive replace karo
        pattern = re.compile(re.escape(from_text), re.IGNORECASE)
        result = pattern.sub(to_text, result)
    return result


def build_metadata_ffmpeg_args(meta: dict) -> list[str]:
    """
    Metadata dict se ffmpeg -metadata args banao.
    meta = {
        'video_title': str,   # -metadata:s:v:0 title=...
        'audio_title': str,   # -metadata:s:a title=...
        'show_title':  str,   # -metadata title=...
    }
    """
    args = []
    if meta.get("show_title"):
        args += ["-metadata", f"title={meta['show_title']}"]
    if meta.get("video_title"):
        args += ["-metadata:s:v:0", f"title={meta['video_title']}"]
    if meta.get("audio_title"):
        args += ["-metadata:s:a", f"title={meta['audio_title']}"]
    return args


def process_url_file():
    """Placeholder – actual processing is in url_upload.py plugin callbacks."""
    pass
