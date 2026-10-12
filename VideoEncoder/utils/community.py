"""
community.py
=============
GLOBAL (bot-wide) "community" branding.

Pehle "sbanime" hardcoded default tha — thumbnail band, auto-caption tag
([@SBANIME]) aur full-metadata video title mein. /community <name> se koi
bhi authorized (owner/sudo) account naam set kar sakta hai, aur woh naam
sab jagah — manual uploads AUR auto-monitor (automated channel upload)
dono mein — consistent use hota hai. Kuch set na kare toh 'sbanime' hi
default rehta hai.

NOTE: Yeh jaan-boojh kar GLOBAL hai, per-user NAHI — auto-monitor ka
upload pipeline internally sirf owner ID use karta hai, isliye per-user
setting hoti toh sudo account se set kiya naam auto-monitor tak kabhi
nahi pahunchta. `user_id` parameter sirf backward-compat ke liye accept
hota hai (purane call-sites tootne na paayein), lekin ignore hota hai.

    from ..utils.community import get_community_tag
    channel = await get_community_tag()   # '@SBANIME' ya '@BOBANIME'
"""

from .database.access_db import db

DEFAULT_COMMUNITY = "sbanime"


MAX_COMMUNITY_NAMES = 2


def parse_names(raw) -> list:
    """\"@a, B\" / \"a b\" -> ['a', 'b'] (lowercase, @ hata ke, unique, max 2)."""
    out = []
    for tok in (raw or "").replace(",", " ").split():
        tok = tok.strip().lstrip("@").lower()
        if tok and tok not in out:
            out.append(tok)
    return out[:MAX_COMMUNITY_NAMES]


async def get_community_names(user_id=None) -> list:
    """1 ya 2 community naam (lowercase list). Kuch set nahi to ['sbanime']."""
    try:
        raw = await db.get_community_raw()
    except Exception:
        raw = None
    return parse_names(raw) or [DEFAULT_COMMUNITY]


async def get_community_name(user_id=None) -> str:
    """PEHLA community naam, lowercase. Default: 'sbanime'. (Global — user_id ignored.)"""
    return (await get_community_names())[0]


async def get_community_tag(user_id=None, all_names: bool = False) -> str:
    """@-prefixed uppercase tag — caption/thumbnail band ke liye. e.g. '@BOBANIME'.

    all_names=False (default): sirf PEHLA naam (thumbnail band, metadata ke liye).
    all_names=True: saare naam space se jode hue — '@A @B' (caption / post ke
    "Main Channel" / "Powered By" line ke liye: 1 set hai to 1, 2 hain to 2)."""
    names = await get_community_names()
    if not all_names:
        names = names[:1]
    return " ".join(f"@{n.upper()}" for n in names)


async def get_community_title(user_id=None) -> str:
    """Title-case naam — metadata title ke liye. e.g. 'Bobanime'."""
    name = await get_community_name()
    return name.capitalize()
