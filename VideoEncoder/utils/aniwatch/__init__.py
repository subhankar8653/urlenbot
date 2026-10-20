"""
utils/aniwatch — Aniwatch/Animetsu scrapers (AniwatchTvdl / Cantarella se port kiye hue).

Do sites, dono megaplay player ke peeche:

    "aniwatch" -> animesalt.to   (AnimeSaltScraper)
    "animetsu" -> anikototv.to   (AnikotoScraper)

/aniwatch (plugins/aniwatch_downloader.py) sirf yahi se import karta hai.
"""

from .anikoto import AnikotoClient, AnikotoScraper
from .animesalt import AnimeSaltClient, AnimeSaltScraper
from .base import detect_source, get_binary_path, is_url, make_progress_bar

# bot-facing naam  ->  asli scraper key
SOURCE_ALIASES = {
    "animetsu": "anikoto",
    "anikoto": "anikoto",
    "anikototv": "anikoto",
    "anikototv.to": "anikoto",
    "aniwatch": "animesalt",
    "aniwatchtv": "animesalt",
    "aniwatchtv.to": "animesalt",
    "animesalt": "animesalt",
    "animesalt.to": "animesalt",
}

# scraper key -> button/label text
SOURCE_LABELS = {
    "animesalt": "Aniwatch",
    "anikoto": "Animetsu",
}

DEFAULT_SOURCE = "animesalt"


def resolve_source(source: str = None) -> str:
    """Map a bot setting / user string onto a real scraper key."""
    return SOURCE_ALIASES.get((source or "").strip().lower(), DEFAULT_SOURCE)


def get_scraper(source: str = None, **kwargs):
    """Build the scraper for a source key (kwargs -> scraper constructor)."""
    if resolve_source(source) == "anikoto":
        return AnikotoScraper(**kwargs)
    return AnimeSaltScraper(**kwargs)


__all__ = [
    "AnikotoClient", "AnikotoScraper", "AnimeSaltClient", "AnimeSaltScraper",
    "DEFAULT_SOURCE", "SOURCE_ALIASES", "SOURCE_LABELS",
    "detect_source", "get_binary_path", "get_scraper", "is_url",
    "make_progress_bar", "resolve_source",
]
