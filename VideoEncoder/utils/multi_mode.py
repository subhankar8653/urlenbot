"""
multi_mode.py
=============
/multi_mode — multiple update channels ke liye alag-alag link + alag-alag buttons.

  OFF (default):  bot bilkul pehle jaisa — 1 invite link, 1 button-set, sab channels pe same post.
  ON:             • /add_anime mein 2+ links (Link 1 → Channel 01, Link 2 → Channel 02 ...)
                  • har update channel ka apna button-set (naam / URL / colour / delete)
                  • /update_channel <id> <N>  → naya channel Channel N ka clone

Data:
  db.col2 {'id': 'multi_mode'}            → {'enabled': bool}
  post_map entry                           → 'invite_links': [link1, link2, ...]  (invite_link == link1)
  update_channels record                   → 'link_idx': int (0-based), 'buttons': {watch/kaise/backup: {...}}
"""

from .database.access_db import db


async def is_multi() -> bool:
    try:
        doc = await db.col2.find_one({'id': 'multi_mode'})
    except Exception:
        return False
    return bool(doc and doc.get('enabled'))


async def set_multi(enabled: bool):
    await db.col2.update_one(
        {'id': 'multi_mode'},
        {'$set': {'enabled': bool(enabled)}},
        upsert=True,
    )


def link_for(entry: dict, idx) -> str:
    """Channel ke link_idx (0-based) ka Watch & Download link. Na mile to Link 1."""
    entry = entry or {}
    links = entry.get('invite_links') or []
    if isinstance(idx, int) and 0 <= idx < len(links) and links[idx]:
        return links[idx]
    if links and links[0]:
        return links[0]
    return entry.get('invite_link') or ''


def set_link(entry: dict, n: int, url: str):
    """Link number n (1-based) set karo; invite_link hamesha Link 1 ke saath sync."""
    links = list(entry.get('invite_links') or [])
    if not links:
        links = [entry.get('invite_link') or '']
    while len(links) < n:
        links.append('')
    links[n - 1] = url
    while len(links) > 1 and not links[-1]:       # peeche ke khali links hata do
        links.pop()
    entry['invite_links'] = links
    entry['invite_link'] = links[0]


def ch_link_idx(ch: dict, pos: int) -> int:
    """Channel ka effective link index (explicit link_idx, warna list position)."""
    v = (ch or {}).get('link_idx')
    return v if isinstance(v, int) and v >= 0 else pos
