"""
rti_downloader.py  v3
======================
Commands:
  /rti <url>                -> Latest episode auto-detect + download
  /rti <url> <start> <end>  -> Episode range download
  /rti <url> 5 5            -> Sirf episode 5
  /rti <url> 0 0            -> MOVIE mode — episode number dhundhta hi
                                nahi, seedha page pe jo WatchMultQuality
                                link mile use utha leta hai
"""

import asyncio
import base64
import os
import re
import shutil
import time
import uuid

import requests
from urllib.parse import unquote as _unquote, urljoin as _urljoin
from bs4 import BeautifulSoup
from pyrogram import Client, filters
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from .. import LOGGER, download_dir, app
from ..utils.helper import check_chat
from ..utils import job_ctl
from ..utils.database.access_db import db

try:
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    SELENIUM_OK = True
except ImportError:
    SELENIUM_OK = False

ARGON_DOMAIN = "argon.razorshell.space/embed"
SWIFT_BASE   = "https://argon.razorshell.space/downlead/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

AUDIO_PRIORITY = ["hindi", "dual", "multi", "english", "japanese", "sub", "unknown"]

# ─────────────────────────────────────────────
#  NEW: Button-based episode selector (/rti <url>)
# ─────────────────────────────────────────────
# sid -> RTISelector. In-memory hai, redeploy pe clear ho jaata hai —
# usse zyada purani sessions bhi _prune_sessions() clean kar deta hai.
RTI_SESSIONS = {}
RTI_SESSION_TIMEOUT = 3600  # 1 hour
PER_PAGE = 10  # screenshot jaisa hi — 10 episode buttons per page

# OVA/Special ko pehle check karo taaki generic "Episode" pattern
# "OVA Episode 1" jaisa text galti se EP na bana de.
# OVA wala pattern number ke around optional "( )" bhi allow karta hai
# (e.g. "S2 OVA (1) – ...") — pehle sirf "OVA1"/"OVA 1" match hota tha,
# "OVA (1)" (bracket wala) skip ho jaata tha isliye uska button hi
# nahi banta tha.
ITEM_PATTERNS = [
    (re.compile(r"\bOVA\s*\(?\s*(\d+)\s*\)?", re.IGNORECASE), "OVA"),
    (re.compile(r"\bSpecial(?:\s*Episode)?\s*\(?\s*(\d+)\s*\)?", re.IGNORECASE), "SP"),
    (re.compile(r"\bEpisode\s*\(?\s*(\d+)\s*\)?", re.IGNORECASE), "EP"),
]


# ─────────────────────────────────────────────
#  NEW: RTI ka apna "Channel Upload" toggle — per user save hota hai.
#  ON hone pe har successful upload us anime ke /add_anime wale channel pe (pehle /addchannel se)
#  add kiye gaye channel pe bhi copy ho jaata hai. Default OFF.
#
#  ("Update Post" toggle iske bilkul alag hai — woh RTISelector session
#  mein hi local rehta hai (in-memory, DB mein save nahi hota), taaki
#  usko ek /rti request mein ON/OFF karne se global /updatechannel ya
#  auto_monitor jaisi kisi bhi doosri cheez pe koi asar na pade.)
# ─────────────────────────────────────────────
async def _get_rti_channel_upload(user_id: int) -> bool:
    user = await db._get_user(user_id)
    return user.get("rti_channel_upload", False)


async def _set_rti_channel_upload(user_id: int, enabled: bool):
    await db.col.update_one({"id": int(user_id)}, {"$set": {"rti_channel_upload": enabled}}, upsert=True)


# ─────────────────────────────────────────────
#  Audio detection helpers
# ─────────────────────────────────────────────
# ── FIX: "Hindi Sub – [WatchMultiQuality]" ko pehle "hindi" maan liya jaata tha ──
# Keyword loop mein "hindi" "sub" se pehle check hota tha, isliye Hindi-SUB wali row
# bhi Hindi-DUB samajh ke download/upload ho jaati thi. Ab SUB sabse pehle check hota hai
# aur har link ka label uski APNI line (<br> tak) se padha jaata hai.
_SUB_WORD_RE = re.compile(r"\bsubs?\b|subbed|subtitles?|\besubs?\b", re.I)
_OTHER_LANG_RE = re.compile(
    r"\b(tamil|telugu|bengali|malayalam|kannada|marathi|punjabi|urdu|gujarati|"
    r"bangla|odia|tagalog|indonesian|spanish|french|arabic|korean|chinese|thai|"
    r"portuguese|russian|german)\b", re.I)
_KW_LABELS = [("hindi", "hindi"), ("dual", "dual"), ("multi", "multi"),
              ("english", "english"), ("japanese", "japanese")]


def _kw_label(text: str):
    """Text se audio label (SUB sabse pehle). Kuch nahi mila to None."""
    t = (text or "").lower()
    if not t.strip():
        return None
    if _SUB_WORD_RE.search(t):
        return "sub"
    for kw, label in _KW_LABELS:
        if kw in t:
            return label
    if _OTHER_LANG_RE.search(t):
        return "other"
    return None


def _label_before(a) -> str:
    """Link se pehle ki apni line ka text (\"Hindi Sub – [\"). <br> par ruk jaata hai; link kisi
    <span>/<strong> ke andar ho to 3 level upar tak jaata hai."""
    node = a
    for _ in range(4):
        parts = []
        for sib in node.previous_siblings:
            if getattr(sib, "name", None) == "br":
                return " ".join(reversed(parts)).strip().lower()
            parts.append(sib.get_text(" ", strip=True) if hasattr(sib, "get_text") else str(sib))
        txt = " ".join(reversed(parts)).strip().lower()
        if txt:
            return txt
        node = node.parent
        if node is None or getattr(node, "name", None) in (None, "[document]", "body", "html"):
            break
    return ""


def _detect_audio(link_el, context_text: str) -> str:
    # 1) Link ki apni line ka label — sabse bharosemand ("Hindi Sub –" => sub)
    lab = _kw_label(_label_before(link_el))
    if lab:
        return lab

    # 2) Purane fallbacks (ab SUB pehle check hota hai)
    parent = link_el.find_parent()
    if parent:
        pt = parent.get_text(" ", strip=True).lower()
        lt = link_el.get_text(strip=True).lower()
        if lt and lt in pt:
            lab = _kw_label(pt.split(lt)[0][-80:])
            if lab:
                return lab

    ct = (context_text or "").lower()
    for pat, label in [
        (r"hindi\s*[-\u2013\u2014]\s*\[?watch", "hindi"),
        (r"english\s*[-\u2013\u2014]\s*\[?watch", "english"),
        (r"japanese\s*[-\u2013\u2014]\s*\[?watch", "japanese"),
        (r"dual\s*audio\s*[-\u2013\u2014]\s*\[?watch", "dual"),
    ]:
        if re.search(pat, ct):
            return label

    return _kw_label(ct) or "unknown"


def _absolutize(soup, base: str):
    """Page ke saare <a href> ko poora URL bana do. Naye episode ka link relative ("/go/abc") ya
    protocol-relative ("//site/abc") ho sakta hai — requests/Chrome ko poora URL chahiye, warna Argon step fail."""
    try:
        for a in soup.find_all("a", href=True):
            h = (a.get("href") or "").strip()
            if h and not h.lower().startswith(("http://", "https://", "javascript:", "mailto:", "#")):
                a["href"] = _urljoin(base, h)
    except Exception:
        pass
    return soup


# ── WatchMultiQuality / WatchMultQuality link pehchaan — "chhota-mota" naam badalne par bhi chale ──
# Site kabhi "WatchMultiQuality" likhti hai, kabhi "WatchMultQuality" (ya space / alag spelling / chhupe
# characters). Isliye ab exact naam nahi, FUZZY match: purana naam, naya naam, space ya symbol, chhoti
# spelling galti, zero-width characters, homoglyph — sab chalte hain. Aur agar phir bhi naam na mile to
# "line ka pehla anchor jo HubCloud/WatchNow/DLBeta/Mega nahi hai" ko WMQ maan liya jaata hai.
import difflib as _difflib
import unicodedata as _ud

_INVIS_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff\u00ad]")
_HOMOGLYPH = str.maketrans({"а": "a", "е": "e", "о": "o", "с": "c", "р": "p", "і": "i", "ѕ": "s",
                            "ј": "j", "ԁ": "d", "һ": "h", "ɡ": "g", "ⅼ": "l", "ι": "i", "ο": "o"})
_WMQ_REF = "watchmultiquality"
_OTHER_SERVERS = ("hubcloud", "watchnow", "dlbeta", "dl-beta", "mega", "telegram", "download", "gdrive",
                  "drive", "mediafire", "pixeldrain", "gofile", "buzzheavier", "join", "click")


def _label_key(t: str) -> str:
    t = _ud.normalize("NFKC", t or "")
    t = _INVIS_RE.sub("", t).lower().translate(_HOMOGLYPH)
    return re.sub(r"[^a-z]", "", t)


def is_wmq(text: str, href: str = "") -> bool:
    """True agar anchor WatchMultiQuality / WatchMultQuality (ya uska koi chhota-mota badla roop) hai."""
    n = _label_key(text)
    h = (href or "").lower()
    if any(k in h for k in ("watchmult", "multiquality", "multquality", "watch-mult", "multi-quality")):
        return True
    if not n or any(o.replace("-", "") in n for o in _OTHER_SERVERS if o not in ("mega",)) and "mult" not in n:
        return False
    if "multiquality" in n or "multquality" in n or "watchmult" in n:
        return True
    if "watch" in n and ("mult" in n or "qual" in n):
        return True
    if n.endswith("quality") and len(n) <= 24 and "mult" in n:
        return True
    if len(n) >= 8 and _difflib.SequenceMatcher(None, n, _WMQ_REF).ratio() >= 0.74 and "now" not in n:
        return True
    return False


def _first_in_line(a) -> bool:
    """Is anchor se pehle (apni line mein, <br> tak) koi aur link nahi."""
    for sib in a.previous_siblings:
        nm = getattr(sib, "name", None)
        if nm == "br":
            return True
        if nm == "a" or (nm and hasattr(sib, "find") and sib.find("a")):
            return False
    return True


def _find_wmq_links(element) -> list:
    links = []
    ctx = element.get_text(" ", strip=True)
    anchors = element.find_all("a", href=True)
    for a in anchors:
        if is_wmq(a.get_text(strip=True), a.get("href", "")):
            links.append({"href": a.get("href", ""), "audio": _detect_audio(a, ctx)})
    if links:
        return links
    # Fallback: naam bilkul alag ho gaya — har language-line ka pehla "anjaana" link WMQ maano
    for a in anchors:
        key = _label_key(a.get_text(strip=True))
        if not key or any(o.replace("-", "") in key for o in _OTHER_SERVERS):
            continue
        if _first_in_line(a) and _label_before(a):
            links.append({"href": a.get("href", ""), "audio": _detect_audio(a, ctx)})
    if links:
        LOGGER.warning(f"[RTI] WMQ naam pehchana nahi gaya — position se {len(links)} link liye "
                       f"(text: {[x for x in (a.get_text(strip=True) for a in anchors)][:4]})")
    return links


# ── Episode ka block sirf <p> mein hi ho zaruri nahi (naye / edit kiye episode ka heading <h3>, <div>, <strong>,
#    <span> mein bhi ho sakta hai) — isliye <p> wale tareeke ke baad ye "kisi bhi tag" wala tareeka bhi chalta hai ──
_EP_NUM_RE = re.compile(r"Episode\s*\(?\s*0*(\d+)", re.IGNORECASE)


def _ep_in(text: str):
    m = _EP_NUM_RE.search(text or "")
    return int(m.group(1)) if m else None


def _episode_blocks(soup, episode_num: int) -> list:
    """Wo sabse bade elements jinme SIRF is episode ka heading ho (doosre episode ka nahi)."""
    blocks = []
    for el in soup.find_all(True):
        if el.name in ("html", "body", "head", "script", "style", "a"):
            continue
        txt = el.get_text(" ", strip=True)
        if _ep_in(txt) != episode_num:
            continue
        # sabse chhota element jo khud heading rakhta ho (bachcha bhi match kare to bachche tak jao)
        if any(_ep_in(c.get_text(" ", strip=True)) == episode_num for c in el.find_all(True, recursive=False)):
            continue
        blk = el
        while blk.parent is not None and getattr(blk.parent, "name", None) not in (None, "[document]", "body", "html"):
            ptxt = blk.parent.get_text(" ", strip=True)
            others = {int(x) for x in _EP_NUM_RE.findall(ptxt)} - {episode_num}
            if others:
                break
            blk = blk.parent
        if blk not in blocks:
            blocks.append(blk)
    return blocks


def _episode_links_any(soup, episode_num: int) -> tuple:
    """(links, episode_block_mila) — kisi bhi tag mein episode ka block + uske baad ke siblings se WMQ links."""
    links, seen = [], False
    for blk in _episode_blocks(soup, episode_num):
        seen = True
        links.extend(_find_wmq_links(blk))
        for idx, sib in enumerate(blk.find_next_siblings()):
            if idx > 8:
                break
            n = _ep_in(sib.get_text(" ", strip=True))
            if n is not None and n != episode_num:
                break
            links.extend(_find_wmq_links(sib))
    return links, seen


def diagnose_episode(page_url: str, episode_num: int) -> str:
    """Fail hone par 1-2 line ki jaankari: page pe is episode ka block mila ya nahi, aur uske links kaunse the."""
    try:
        r = requests.get(page_url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.content, "html.parser")
        blocks = _episode_blocks(soup, episode_num)
        if not blocks:
            seen = sorted({int(x) for x in _EP_NUM_RE.findall(soup.get_text(" ", strip=True))})[-8:]
            return f"page pe Episode {episode_num} ka block hi nahi mila (page pe episodes: {seen})"
        anchors = []
        for blk in blocks[:2]:
            for a in blk.find_all("a", href=True)[:8]:
                anchors.append(repr(a.get_text(strip=True))[:26])
            for sib in list(blk.find_next_siblings())[:2]:
                for a in sib.find_all("a", href=True)[:8]:
                    anchors.append(repr(a.get_text(strip=True))[:26])
        return f"Episode {episode_num} block mila ({blocks[0].name}), links: {', '.join(anchors[:10]) or 'koi link nahi'}"
    except Exception as e:
        return f"diagnose error: {e}"


def _best_link(links: list):
    for priority in AUDIO_PRIORITY:
        for lnk in links:
            if lnk["audio"] == priority:
                return lnk
    return links[0] if links else None


# ── Generic fallback: "Erased S01E01 Fandub" jaise link-text wale pages ──
# (jahan <p> heading + WatchMultQuality link ka standard layout nahi hota, seedha
#  episode ke naam wale links ki list hoti hai: "Anime S01E05 Fandub", "1x05", "E05"...)
_SXXEYY_RE = re.compile(
    r"\bS(?:eason)?\s*0*(\d{1,2})\s*[-_. ]?\s*E(?:p(?:isode)?)?\s*\.?\s*0*(\d{1,3})\b",
    re.IGNORECASE,
)
_NXN_RE = re.compile(r"\b0*(\d{1,2})x0*(\d{1,3})\b", re.IGNORECASE)
_EP_SHORT_RE = re.compile(r"(?<![A-Za-z])E(?:p\.?)?\s*0*(\d{1,3})\b", re.IGNORECASE)


def _match_item_ext(text: str):
    """(kind, num, season|None). Pehle purane patterns (OVA/Special/Episode N), phir
    S01E05 / 1x05 / E05 jaise short formats."""
    kind, num = _match_item(text)
    if kind:
        return kind, num, None
    m = _SXXEYY_RE.search(text)
    if m:
        return "EP", int(m.group(2)), int(m.group(1))
    m = _NXN_RE.search(text)
    if m:
        return "EP", int(m.group(2)), int(m.group(1))
    m = _EP_SHORT_RE.search(text)
    if m:
        return "EP", int(m.group(1)), None
    return None, None, None


def _audio_from_text(text: str) -> str:
    t = (text or "").lower()
    if re.search(r"fan\s*dub|dubbed|\bdub\b|hindi", t):
        return "hindi"
    if "dual" in t:
        return "dual"
    if "multi" in t:
        return "multi"
    if re.search(r"english|\beng\b", t):
        return "english"
    if "japanese" in t:
        return "japanese"
    if re.search(r"\bsubs?\b", t):
        return "sub"
    return "unknown"


def _generic_items(soup):
    """
    Page ke saare <a> links scan karo; jis link ke text (ya chhote parent text) mein
    episode ka pattern ho, usse (kind, num) → [{href, audio}] banao.
    Returns (found_dict, season_hint|None).
    """
    found = {}
    seasons = []
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        text = a.get_text(" ", strip=True) or (a.get("title") or "")
        kind, num, season = _match_item_ext(text)
        ctx = text
        if not kind:
            par = a.find_parent()
            ptxt = par.get_text(" ", strip=True) if par else ""
            if ptxt and len(ptxt) <= 160:
                kind, num, season = _match_item_ext(ptxt)
                ctx = ptxt
        if not kind:
            continue
        audio = _audio_from_text(text)
        if audio == "unknown":
            audio = _audio_from_text(ctx)
        found.setdefault((kind, num), []).append({"href": href, "audio": audio})
        if season:
            seasons.append(season)
    hint = max(set(seasons), key=seasons.count) if seasons else None
    return found, hint


def _page_title(soup) -> str:
    t = soup.find("h1", class_="entry-title") or soup.find("h1") or soup.find("h2")
    if t and t.get_text(strip=True):
        return t.get_text(" ", strip=True)
    og = soup.find("meta", attrs={"property": "og:title"})
    if og and og.get("content"):
        return og["content"].strip()
    if soup.title and soup.title.get_text(strip=True):
        return soup.title.get_text(strip=True)
    return "Unknown Anime"


def _match_item(text: str):
    """Text mein Episode/OVA/Special number dhundo. Returns (kind, num) ya (None, None)."""
    for pattern, kind in ITEM_PATTERNS:
        m = pattern.search(text)
        if m:
            return kind, int(m.group(1))
    return None, None


# ─────────────────────────────────────────────
#  NEW: Poore page ka ek-hi-baar scan — saare Episode/OVA/Special
#  items aur unke WatchMultQuality (Hindi priority) links nikaal lo.
#  Isse har episode ke liye baar baar page fetch nahi karna padta.
# ─────────────────────────────────────────────
def discover_items(page_url: str):
    """
    Returns dict:
      {
        "title": str, "season": int, "mode": "episodes" | "movie",
        "items": [{"kind": "EP"/"OVA"/"SP"/"MOVIE", "num": int,
                    "label": str, "wmq_link": str}, ...]
      }
    """
    r = requests.get(page_url, headers=HEADERS, timeout=15)
    r.raise_for_status()
    soup = _absolutize(BeautifulSoup(r.content, "html.parser"), page_url)

    title = _page_title(soup)

    season_match = re.search(r"season\s*(\d+)", title, re.IGNORECASE)
    season = int(season_match.group(1)) if season_match else 1

    # Saare candidate <p> blocks jisme Episode/OVA/Special text ho, DOM order mein
    blocks = []
    for p in soup.find_all("p"):
        text = p.get_text(" ", strip=True)
        kind, num = _match_item(text)
        if kind:
            blocks.append((p, kind, num))

    seen = set()
    items = []
    for p, kind, num in blocks:
        key = (kind, num)
        if key in seen:
            # Same episode ke duplicate headings (e.g. "Untouched CR (Multi
            # Audio)" wala dusra block) — pehla wala hi rakho.
            continue

        all_links = list(_find_wmq_links(p))
        for sib_idx, sibling in enumerate(p.find_next_siblings()):
            if sib_idx > 8:
                break
            sib_text = sibling.get_text(" ", strip=True)
            sib_kind, sib_num = _match_item(sib_text)
            if sib_kind and (sib_kind, sib_num) != key:
                break
            all_links.extend(_find_wmq_links(sibling))

        best = _best_link(all_links)
        if not best:
            continue

        seen.add(key)
        items.append({"kind": kind, "num": num, "wmq_link": best["href"]})

    if not items:
        # ── Generic fallback ── "Erased S01E01 Fandub" jaise link-text wale pages
        found, gseason = _generic_items(soup)
        for (kind, num), links in found.items():
            best = _best_link(links)
            if best:
                items.append({"kind": kind, "num": num, "wmq_link": best["href"]})
        if items and not season_match and gseason:
            season = gseason

    if not items:
        # ── MOVIE MODE ── Episode/OVA/Special jaisa kuch nahi mila,
        # seedha poore page pe best (Hindi priority) WatchMultQuality
        # link utha lo.
        all_links = _find_wmq_links(soup)
        best = _best_link(all_links)
        if not best:
            return {"title": title, "season": season, "mode": "movie", "items": []}
        return {
            "title": title,
            "season": season,
            "mode": "movie",
            "items": [{"kind": "MOVIE", "num": 0, "label": "Movie", "wmq_link": best["href"]}],
        }

    kind_order = {"EP": 0, "OVA": 1, "SP": 2}
    items.sort(key=lambda x: (kind_order.get(x["kind"], 9), x["num"]))

    for it in items:
        if it["kind"] == "EP":
            it["label"] = f"EP{it['num']:02d}"
        elif it["kind"] == "OVA":
            it["label"] = f"OVA{it['num']}"
        else:
            it["label"] = f"SP{it['num']}"

    return {"title": title, "season": season, "mode": "episodes", "items": items}


def _kb(rows):
    """rows = list of list of (text, callback_data) tuples"""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(t, callback_data=d) for t, d in row] for row in rows]
    )


def _prune_sessions():
    now = time.time()
    stale = [k for k, v in RTI_SESSIONS.items() if now - v.created > RTI_SESSION_TIMEOUT]
    for k in stale:
        RTI_SESSIONS.pop(k, None)


# Quality row (Raretoon pe 360p / 720p / 1080p milta hai; "All" = jaisa pehle tha, saari qualities)
RTI_QUALITIES = [("360", "360p"), ("720", "720p"), ("1080", "1080p"), ("all", "All")]
_RTI_Q_KEYS = {k for k, _ in RTI_QUALITIES}


class RTISelector:
    """
    /rti <url> ke liye interactive button menu:
    Season header, EPxx grid (paginated), single-tap download,
    ya "Select Multiple" ON karke checkmark se multi-episode select
    karke ek saath download.
    """

    def __init__(self, page_url: str, data: dict, orig_message: Message):
        self.page_url = page_url
        self.title = data["title"]
        self.season = data["season"]
        self.mode = data["mode"]
        self.items = data["items"]
        self.orig_message = orig_message  # asli command bhejne wala user message
        self.sid = None
        self.msg = None  # selector card Message
        self.page = 0
        self.multi = False
        self.selected = set()
        self.created = time.time()
        # Cached toggle states — populate_toggles() se load hote hain
        # session banate waqt, taaki build_markup() sync reh sake.
        self.update_toggle = False
        self.channel_toggle = False
        self.quality = "all"      # Quality row: 360 | 720 | 1080 | all (default = purana behaviour)

    @property
    def quality_filter(self):
        """_run_swift ke liye: None = saari qualities, warna '720p' jaisa."""
        return None if self.quality == "all" else f"{self.quality}p"

    @property
    def quality_label(self):
        return dict(RTI_QUALITIES).get(self.quality, self.quality)

    async def populate_toggles(self):
        # "Update Post" toggle ab poori tarah session-local hai (default OFF,
        # __init__ mein already set) — global /updatechannel toggle se na
        # padhta hai, na likhta hai. Sirf "Channel Upload" (chtog) hi per-user
        # persisted hai (rti_channel_upload), woh already independent tha.
        self.channel_toggle = await _get_rti_channel_upload(self.orig_message.from_user.id)

    @property
    def total_pages(self):
        return max(1, (len(self.items) + PER_PAGE - 1) // PER_PAGE)

    def page_items(self):
        start = self.page * PER_PAGE
        return list(enumerate(self.items))[start:start + PER_PAGE]

    def header_text(self):
        counts = {"EP": 0, "OVA": 0, "SP": 0}
        for it in self.items:
            if it["kind"] in counts:
                counts[it["kind"]] += 1

        lines = [
            "📂 **Select Episode**",
            "━━━━━━━━━━━━━━━━━━",
            f"🎬 **{self.title}**",
            "🌐 Website: 🎭 RareAnimes",
        ]

        if self.mode == "movie":
            lines.append("🍿 Type: Movie")
        else:
            info = f"🏝️ Season: {self.season} • 📖 Episodes: {counts['EP']}"
            if counts["OVA"]:
                info += f" • 🎞 OVA: {counts['OVA']}"
            if counts["SP"]:
                info += f" • ✨ Special: {counts['SP']}"
            lines.append(info)

        if self.channel_toggle:
            lines.append("🎚 Quality: All  _(Channel Upload ON — saari qualities)_")
        else:
            lines.append(f"🎚 Quality: {self.quality_label}")
        lines.append("━━━━━━━━━━━━━━━━━━")
        if self.mode == "movie":
            lines.append("👇 Tap the button to download:")
        else:
            lines.append(f"👇 Tap an episode to download:  (Page {self.page + 1}/{self.total_pages})")

        if self.multi:
            lines.append(f"\n☑️ **Multi-select ON** — chosen: `{len(self.selected)}`")

        return "\n".join(lines)

    def build_markup(self):
        sid = self.sid
        rows = []

        if self.mode == "movie":
            mark = "✅ " if 0 in self.selected else "🎬 "
            rows.append([(f"{mark}Movie", f"rti_ep_{sid}_0")])
            rows.append(self._quality_row())
        else:
            ep_count = sum(1 for it in self.items if it.get("kind") == "EP") or len(self.items)
            rows.append([(f"🏝️ SEASON {self.season:02d} • ⬇️ Download All ({ep_count})", f"rti_season_{sid}")])

            row = []
            for idx, item in self.page_items():
                mark = "✅ " if idx in self.selected else ""
                row.append((f"{mark}{item['label']}", f"rti_ep_{sid}_{idx}"))
                if len(row) == 3:
                    rows.append(row)
                    row = []
            if row:
                rows.append(row)

            if self.total_pages > 1:
                nav = []
                if self.page > 0:
                    nav.append(("◀️ Prev", f"rti_pg_{sid}_{self.page - 1}"))
                nav.append((f"📄 {self.page + 1}/{self.total_pages}", f"rti_noop_{sid}"))
                if self.page < self.total_pages - 1:
                    nav.append(("Next ▶️", f"rti_pg_{sid}_{self.page + 1}"))
                rows.append(nav)

            rows.append(self._quality_row())

            if self.multi:
                rows.append([(f"⬇️ Download Selected ({len(self.selected)})", f"rti_dl_{sid}")])
                rows.append([("☑️ Multi-select: ON (tap to turn off)", f"rti_toggle_{sid}")])
            else:
                rows.append([("☑️ Select Multiple", f"rti_toggle_{sid}")])

        rows.append([
            (f"📢 Update Post: {'ON' if self.update_toggle else 'OFF'}", f"rti_uptog_{sid}"),
        ])
        rows.append([
            (f"📤 Channel Upload: {'ON' if self.channel_toggle else 'OFF'}", f"rti_chtog_{sid}"),
        ])
        rows.append([("❌ Close", f"rti_close_{sid}")])
        return _kb(rows)

    def _quality_row(self):
        return [
            ((("✅ " if self.quality == k else "") + label), f"rti_q_{self.sid}_{k}")
            for k, label in RTI_QUALITIES
        ]

    async def render(self):
        if not self.msg:
            return
        try:
            await self.msg.edit(self.header_text(), reply_markup=self.build_markup())
        except Exception as e:
            if "MESSAGE_NOT_MODIFIED" not in str(e):
                LOGGER.error(f"[RTI] selector render error: {e}")


# ─────────────────────────────────────────────
#  NEW: Latest episode number nikalo
# ─────────────────────────────────────────────
def get_latest_episode(page_url: str):
    """
    Page ke sabse latest (highest number) episode detect karo.
    Returns: (latest_ep_num, anime_title) or (None, None)
    """
    try:
        r = requests.get(page_url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.content, "html.parser")

        title_tag = soup.find("h1", class_="entry-title")
        anime_title = title_tag.text.strip() if title_tag else "Unknown Anime"

        # Saare episode numbers collect karo
        ep_numbers = []
        for p in soup.find_all("p"):
            text = p.get_text(" ", strip=True)
            match = re.search(r"Episode\s*(\d+)", text, re.IGNORECASE)
            if match:
                ep_numbers.append(int(match.group(1)))

        if not ep_numbers:
            LOGGER.warning("[RTI] No episodes found on page")
            return None, None

        latest = max(ep_numbers)
        LOGGER.info(f"[RTI] Latest episode detected: {latest}")
        return latest, anime_title

    except Exception as e:
        LOGGER.error(f"[RTI] get_latest_episode error: {e}")
        return None, None


# ─────────────────────────────────────────────
#  Step 1: Page → WatchMultQuality link
# ─────────────────────────────────────────────
def get_watchmult_link(page_url: str, episode_num: int):
    try:
        r = requests.get(page_url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        soup = _absolutize(BeautifulSoup(r.content, "html.parser"), page_url)

        anime_title = _page_title(soup)

        # ── MOVIE MODE (episode_num == 0) ──
        # Movies ke page pe "Episode N" jaisa koi text hota hi nahi
        # (seedha "Hindi – Download" ke baad "WatchMultiQuality" heading
        # aa jaati hai). Aise pages ke liye episode number dhundhne ki
        # koshish karna hi galat hai — /rti <url> 0 0 use karo, tab
        # seedha poore page pe jo bhi WatchMultQuality link mile
        # (audio-priority ke hisaab se best) utha lo, episode text
        # match kiye bina.
        if episode_num == 0:
            all_links = _find_wmq_links(soup)
            best = _best_link(all_links)
            if best:
                return best["href"], anime_title
            return None, None

        for p in soup.find_all("p"):
            text = p.get_text(" ", strip=True)
            match = re.search(r"Episode\s*(\d+)", text, re.IGNORECASE)
            if match and int(match.group(1)) == episode_num:
                all_links = list(_find_wmq_links(p))
                for idx, sibling in enumerate(p.find_next_siblings()):
                    if idx > 8:
                        break
                    sib_text = sibling.get_text(" ", strip=True)
                    ep_match = re.search(r"Episode\s*(\d+)", sib_text, re.IGNORECASE)
                    if ep_match and int(ep_match.group(1)) != episode_num:
                        break
                    all_links.extend(_find_wmq_links(sibling))

                best = _best_link(all_links)
                if best:
                    return best["href"], anime_title

        # Dusra tareeka: episode ka heading <p> ke bahar (h3 / div / strong / span ...) ho to
        any_links, _seen = _episode_links_any(soup, episode_num)
        best = _best_link(any_links)
        if best:
            LOGGER.info(f"[RTI] Ep {episode_num}: link <p> ke bahar ke block se mila")
            return best["href"], anime_title

        # Generic fallback — "Anime S01E05 Fandub" jaise link-text wale pages
        found, _ = _generic_items(soup)
        best = _best_link(found.get(("EP", episode_num), []))
        if best:
            return best["href"], anime_title

        return None, None
    except Exception as e:
        LOGGER.error(f"[RTI] Page scrape error: {e}")
        return None, None


# ─────────────────────────────────────────────
#  AUTO-MONITOR ke liye STRICT finder
#  Sirf Hindi DUB (env AM_AUDIO_ALLOWED, default: hindi,dual,multi) ka link deta hai.
#  Page pe abhi sirf "Hindi Sub" / "Eng Sub" / koi aur language ho to link NAHI deta,
#  status="wrong_audio" deta hai — caller 30s baad dobara check karta hai.
#    status: ok | wrong_audio | no_link | no_episode | error
# ─────────────────────────────────────────────
AM_AUDIO_ALLOWED = [a.strip().lower() for a in
                    os.getenv("AM_AUDIO_ALLOWED", "hindi,dual,multi").split(",") if a.strip()] \
                   or ["hindi", "dual", "multi"]


def _pick_allowed(links: list, title: str, allowed: list):
    for pr in AUDIO_PRIORITY:
        if pr not in allowed and not (pr == "unknown" and "unknown" in allowed):
            continue
        for lnk in links:
            if lnk["audio"] == pr:
                return lnk
    # label-less link: sirf tab jab page title mein Sub na ho
    if not _SUB_WORD_RE.search(title or ""):
        for lnk in links:
            if lnk["audio"] == "unknown":
                return lnk
    return None


def get_watchmult_link_checked(page_url: str, episode_num: int, allowed: list = None) -> dict:
    allowed = [a.lower() for a in (allowed or AM_AUDIO_ALLOWED)]
    out = {"link": None, "title": None, "status": "no_episode", "audios": []}
    try:
        r = requests.get(page_url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        soup = _absolutize(BeautifulSoup(r.content, "html.parser"), page_url)
    except Exception as e:
        LOGGER.error(f"[RTI] checked scrape error: {e}")
        out["status"] = "error"
        return out

    title = _page_title(soup)
    out["title"] = title
    links, ep_seen = [], False

    if episode_num == 0:                                   # movie mode
        links = _find_wmq_links(soup)
        ep_seen = bool(links)
    else:
        for p in soup.find_all("p"):
            text = p.get_text(" ", strip=True)
            m = re.search(r"Episode\s*\(?\s*(\d+)", text, re.IGNORECASE)
            if not (m and int(m.group(1)) == episode_num):
                continue
            ep_seen = True
            links.extend(_find_wmq_links(p))
            for idx, sib in enumerate(p.find_next_siblings()):
                if idx > 8:
                    break
                em = re.search(r"Episode\s*\(?\s*(\d+)", sib.get_text(" ", strip=True), re.IGNORECASE)
                if em and int(em.group(1)) != episode_num:
                    break
                links.extend(_find_wmq_links(sib))
        if not links:                                      # episode ka heading <p> ke bahar (h3/div/strong...)
            _al, _sn = _episode_links_any(soup, episode_num)
            if _sn:
                ep_seen = True
            links = _al
        if not links:                                      # generic "S01E05 Fandub" pages
            found, _ = _generic_items(soup)
            g = found.get(("EP", episode_num), [])
            if g:
                ep_seen = True
                links = g

    out["audios"] = sorted({l["audio"] for l in links})
    best = _pick_allowed(links, title, allowed)
    if best:
        out["link"], out["status"] = best["href"], "ok"
    elif links:
        out["status"] = "wrong_audio"
    elif ep_seen:
        out["status"] = "no_link"
    LOGGER.info(f"[RTI] ep{episode_num} checked: status={out['status']} audios={out['audios']}")
    return out


# ─────────────────────────────────────────────
#  Step 2: WatchMultQuality -> Argon embed link
# ─────────────────────────────────────────────
def _make_selenium_driver():
    # ── Har session ka apna disposable profile dir ──
    # Chrome ko --user-data-dir diye bina chalane pe woh khud /tmp mein
    # ek random profile folder banata hai. Normal case mein quit() pe
    # khud clean kar deta hai, lekin agar Railway ke constrained container
    # mein renderer crash ho jaaye ya OOM aa jaaye, profile folder /tmp
    # mein hi reh jaata hai. get_argon_link 1 episode ke 30 retry attempts
    # tak baar-baar call hota hai — thodi si bhi leak rate ke saath yeh
    # kuch hi episodes mein /tmp/storage bhar deta hai aur bot stuck ho
    # jaata hai jab tak redeploy na ho.
    # Fix: apna hi disposable dir do (download_dir ke andar) taaki humein
    # pata ho iska exact path — aur cleanup (_kill_driver_tree) mein
    # explicitly delete kar dein, Chrome ke bharose na rahe.
    profile_dir = os.path.join(download_dir, "_chrome_tmp", f"rti_{uuid.uuid4().hex}")
    os.makedirs(profile_dir, exist_ok=True)

    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument(f"user-agent={HEADERS['User-Agent']}")
    options.add_argument(f"--user-data-dir={profile_dir}")
    options.add_argument("--disable-crash-reporter")
    options.add_experimental_option("prefs", {
        "profile.managed_default_content_settings.images": 2
    })
    options.page_load_strategy = "eager"
    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(40)
    # Cleanup ke liye profile_dir ko driver pe hi attach kar do
    driver._suhani_profile_dir = profile_dir
    job_ctl.register_driver(driver)      # cancel pe band ho sake
    return driver


def _kill_driver_tree(driver):
    """
    driver.quit() bhaunsa/fail ho jaaye (renderer crash, hung tab, etc.)
    to bhi chromedriver + chrome + unke saare child processes ko
    forcefully kill karo, aur uska disposable profile dir bhi delete
    karo. Warna zombie chrome processes RAM/fd jama karte rehte hain
    aur kuch episodes ke baad bot response dena band kar deta hai —
    sirf redeploy (fresh process) se hi theek hota hai.
    """
    if not driver:
        return

    pid = None
    try:
        pid = driver.service.process.pid
    except Exception:
        pid = None

    try:
        driver.quit()
    except Exception:
        pass

    if pid:
        try:
            import psutil
            if psutil.pid_exists(pid):
                proc = psutil.Process(pid)
                for child in proc.children(recursive=True):
                    try:
                        child.kill()
                    except Exception:
                        pass
                try:
                    proc.kill()
                except Exception:
                    pass
        except Exception:
            pass

    profile_dir = getattr(driver, "_suhani_profile_dir", None)
    if profile_dir:
        shutil.rmtree(profile_dir, ignore_errors=True)


def _extract_argon_from_iframes(driver, _depth: int = 0):
    """Page + (nested) iframes ke andar tak Argon embed dhundo."""
    try:
        src_html = driver.page_source or ""
        soup = BeautifulSoup(src_html, "html.parser")
        for iframe in soup.find_all("iframe"):
            src = iframe.get("src", "") or iframe.get("data-src", "")
            if ARGON_DOMAIN in src or "razorshell.space" in src:
                return _find_argon(src) or src

        hit = _find_argon(src_html)
        if hit:
            return hit

        if _depth < 2:                                     # wrapper iframe ke andar jaao
            frames = driver.find_elements(By.TAG_NAME, "iframe")
            for i in range(min(len(frames), 6)):
                try:
                    driver.switch_to.frame(i)
                    found = _extract_argon_from_iframes(driver, _depth + 1)
                    driver.switch_to.parent_frame()
                    if found:
                        return found
                except Exception:
                    try:
                        driver.switch_to.default_content()
                    except Exception:
                        pass
                    break
    except Exception as e:
        LOGGER.error(f"[RTI] Argon extract error: {e}")
    return None


def _close_popups(driver, main_window):
    try:
        if len(driver.window_handles) > 1:
            for handle in driver.window_handles:
                if handle != main_window:
                    driver.switch_to.window(handle)
                    driver.close()
            driver.switch_to.window(main_window)
    except Exception:
        pass


_ARGON_RE = re.compile(r'https?://argon\.razorshell\.space/embed/[A-Za-z0-9_-]+')
# FIX: Argon link ab sirf seedhe "https://argon.razorshell.space/embed/ID" text mein nahi, in roopon mein bhi pakda jaata hai:
#   https:\/\/argon...\/embed\/ID (JS/JSON escaped) · https%3A%2F%2F... (URL-encoded) · &#x2F; (HTML entity) ·
#   //argon.razorshell.space/embed/ID (protocol-relative) · atob("...") base64 mein chhupa · razorshell ka koi bhi subdomain
_ARGON_ANY_RE = re.compile(r'(?:https?:)?//([a-z0-9.-]*razorshell\.space)/(?:embed|downlead|download|d|e)/([A-Za-z0-9_-]{5,})', re.I)
_B64_RE = re.compile(r'[A-Za-z0-9+/_-]{28,}={0,2}')


def _find_argon(text: str):
    """Kisi bhi text/HTML mein se Argon embed URL nikalo (escaped / encoded / base64 roop mein bhi). Na mile to None."""
    if not text:
        return None
    variants = [text]
    t2 = text.replace("\\/", "/").replace("&#x2F;", "/").replace("&#47;", "/").replace("&amp;", "&")
    variants.append(t2)
    try:
        variants.append(_unquote(t2))
    except Exception:
        pass
    for v in variants:
        m = _ARGON_ANY_RE.search(v)
        if m:
            return f"https://{m.group(1)}/embed/{m.group(2)}"
    # base64 mein chhupa ho sakta hai (atob / data-attribute)
    if "razorshell" not in text.lower():
        for cand in _B64_RE.findall(text)[:40]:
            try:
                dec = base64.b64decode(cand + "=" * (-len(cand) % 4), altchars=b"-_", validate=False).decode("utf-8", "ignore")
            except Exception:
                continue
            if "razorshell" in dec.lower():
                m = _ARGON_ANY_RE.search(dec.replace("\\/", "/"))
                if m:
                    return f"https://{m.group(1)}/embed/{m.group(2)}"
    return None


def _argon_via_requests(watchmult_url: str):
    """Chrome kholne se pehle (3-8s bachta hai) plain HTTP se argon embed dhundo.
    Seedha page mein na mile to uske iframe / link ke andar (1 level) bhi dekhta hai."""
    try:
        r = requests.get(watchmult_url, headers=HEADERS, timeout=10)
        if r.status_code != 200:
            return None
        hit = _find_argon(r.text)
        if hit:
            return hit
        soup = BeautifulSoup(r.content, "html.parser")
        nxt = []
        for tag, attr in (("iframe", "src"), ("iframe", "data-src"), ("embed", "src"), ("a", "href")):
            for el in soup.find_all(tag):
                u = (el.get(attr) or "").strip()
                if u and not u.lower().startswith(("javascript:", "mailto:", "#")):
                    u = _urljoin(watchmult_url, u)
                    if u != watchmult_url and u not in nxt and not re.search(r"hubcloud|telegram|t\.me|facebook|twitter", u, re.I):
                        nxt.append(u)
        for u in nxt[:4]:
            try:
                r2 = requests.get(u, headers={**HEADERS, "Referer": watchmult_url}, timeout=8)
                hit = _find_argon(r2.text) if r2.status_code == 200 else None
                if hit:
                    return hit
            except Exception:
                continue
    except Exception:
        pass
    return None


def diagnose_wmq_page(watchmult_url: str) -> str:
    """Argon link na milne par: WatchMultQuality page asal mein kaisa dikh raha hai (1 line)."""
    try:
        r = requests.get(watchmult_url, headers=HEADERS, timeout=10, allow_redirects=True)
        soup = BeautifulSoup(r.content, "html.parser")
        ifr = [(i.get("src") or i.get("data-src") or "")[:45] for i in soup.find_all("iframe")][:3]
        low = r.text.lower()
        flags = [k for k in ("cloudflare", "just a moment", "captcha", "not found", "expired", "processing", "please wait",
                             "coming soon", "razorshell") if k in low]
        title = (soup.title.get_text(strip=True) if soup.title else "")[:40]
        return (f"WMQ page: HTTP {r.status_code}, {len(r.text)} chars, title={title!r}, "
                f"iframes={ifr or 'nahi'}, scripts={len(soup.find_all('script'))}, flags={flags or '-'}")
    except Exception as e:
        return f"WMQ page open nahi hua: {type(e).__name__}"


def get_argon_link(watchmult_url: str):
    # Fast path: static HTML mein hi iframe/link ho to Chrome ki zaroorat nahi
    _quick = _argon_via_requests(watchmult_url)
    if _quick:
        LOGGER.info("[RTI] argon link requests se mila (Chrome skip)")
        return _quick
    driver = None
    try:
        driver = _make_selenium_driver()
        driver.get(watchmult_url)
        main = driver.current_window_handle
        # Fixed 2s sleep ki jagah: iframe aate hi turant nikal lo (0.25s polling, max 4s)
        argon = None
        for _ in range(48):                                   # ~12s (naye episode ka iframe der se aata hai)
            try:
                argon = _find_argon(driver.page_source or "")
                if argon:
                    break
            except Exception:
                pass
            time.sleep(0.25)
        try:
            driver.execute_script("window.stop();")
        except Exception:
            pass
        _close_popups(driver, main)

        argon = argon or _extract_argon_from_iframes(driver)
        if argon:
            return argon

        try:
            wait = WebDriverWait(driver, 8)
            for btn_text in ["Get Download Link", "Download", "Get Link", "Click Here"]:
                try:
                    btn = wait.until(EC.element_to_be_clickable(
                        (By.XPATH, f"//a[contains(translate(text(),'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ'), '{btn_text.upper()}')]")
                    ))
                    btn.click()
                    time.sleep(2)
                    _close_popups(driver, main)
                    argon = _extract_argon_from_iframes(driver)
                    if argon:
                        return argon
                    break
                except Exception:
                    continue
        except Exception:
            pass

        return None
    except Exception as e:
        LOGGER.error(f"[RTI] get_argon_link error: {e}")
        return None
    finally:
        _kill_driver_tree(driver)


# ─────────────────────────────────────────────
#  Step 3: Argon -> Swift URL
# ─────────────────────────────────────────────
def argon_to_swift(argon_url: str):
    try:
        parts = [p for p in argon_url.rstrip("/").split("/") if p]
        unique_code = parts[-1]
        if len(unique_code) < 5:
            return None
        return SWIFT_BASE + unique_code
    except Exception as e:
        LOGGER.error(f"[RTI] argon_to_swift error: {e}")
        return None


# ─────────────────────────────────────────────
#  Step 4: Download + Sequential upload
# ─────────────────────────────────────────────
async def _run_rti_swift(client, message: Message, swift_url: str, status_msg, ep_num: int, total_eps: int,
                          sess: "RTISelector" = None, page_url: str = ""):
    from .swift_downloader import _run_swift
    ep_label = "Movie" if ep_num == 0 else f"Ep {ep_num}/{total_eps}"
    # /swift wala exact flow use karo — download + queued messages + sequential upload
    # show_url=False: DM mein swift_url kabhi nahi dikhega, sirf status (downloading/
    # uploading/quality/episode) — episode_label se pata chalega kaunsa episode chal raha hai
    _lbl = None if job_ctl.CURRENT.get() is not None else ep_label
    _qf = sess.quality_filter if sess is not None else None     # Quality row ka chuna hua
    _fb_page = page_url or (getattr(sess, "page_url", "") if sess is not None else "")
    uploaded_results = await _run_swift(
        client, message, swift_url, encode=False, episode_label=_lbl, show_url=False,
        quality_filter=_qf,
        fb_page=_fb_page or None, fb_ep=ep_num or None,       # 1080p Swift pe na ho to mirror servers
    )
    await _forward_to_channel_if_enabled(client, message, uploaded_results, sess=sess, page_url=page_url)
    await _send_update_post_if_enabled(client, sess, ep_num, uploaded_results)
    return True


async def _send_update_post_if_enabled(client, sess: "RTISelector", ep_num: int, uploaded_results):
    """
    Sess ka "Update Post" toggle ON hai to us anime/episode ka post
    update channel pe bhejo — force=True se global /updatechannel toggle
    bypass hota hai, taaki yeh sirf isi /rti session tak seemit rahe.
    Range-mode (/rti <url> <start> <end>) mein sess None hota hai, wahan
    yeh koi effect nahi karta — jaisa pehle tha.
    """
    if not sess or not sess.update_toggle or not uploaded_results:
        return
    try:
        from .update_channel import send_update_post
        # RareAnimes ka page-title ("... Hindi Dubbed Episodes Download HD") update_post_map
        # ke naam se match nahi hota tha — isliye /add_anime list wala asli naam use karo.
        _post_name = sess.title
        try:
            _ent, _ = await _find_add_anime_channel(sess.title, sess.page_url)
            if _ent and _ent.get("anime_name"):
                _post_name = _ent["anime_name"]
        except Exception:
            pass
        await send_update_post(
            client,
            anime_name=_post_name,
            season=sess.season,
            episode=ep_num if ep_num else None,
            force=True,
        )
    except Exception as e:
        LOGGER.error(f"[RTI] Update post error: {e}")


async def _find_add_anime_channel(title: str, page_url: str = ""):
    """
    /add_anime list mein se is anime ka channel dhundo (Rtic jaisa hi matching).
    -> (anime_entry | None, anime_list)
    """
    from .auto_monitor import _get_anime_list, _find_matching_anime
    anime_list = await _get_anime_list()
    if not anime_list:
        return None, []
    return _find_matching_anime(f"{title} {page_url}", anime_list), anime_list


async def _forward_to_channel_if_enabled(client, message: Message, uploaded_results, sess: "RTISelector" = None,
                                         page_url: str = ""):
    """
    "📤 Channel Upload" toggle ON hai to har uploaded quality ko /add_anime se
    add kiye gaye us anime ke channel pe copy kar do (anime ka naam match karke).
    Bot chat mein upload waise hi normal rehta hai — yeh sirf ek extra copy hai.
    """
    if not uploaded_results:
        return
    try:
        user_id = message.from_user.id
        enabled = await _get_rti_channel_upload(user_id)
        if not enabled:
            return
        m_title = sess.title if sess else ""
        m_url = sess.page_url if sess else page_url
        if not (m_title or m_url):
            return
        entry, anime_list = await _find_add_anime_channel(m_title, m_url)
        if not entry or not entry.get("channel_id"):
            try:
                await message.reply(
                    f"⚠️ **Channel upload skip** — `{(m_title or m_url)[:60]}`\n"
                    f"Ye anime `/add_anime` list mein nahi mila, isliye channel pe nahi bheja.\n"
                    f"Pehle `/add_anime` se add karo."
                )
            except Exception:
                pass
            return
        target_id = entry["channel_id"]
        for _quality, sent_msg in uploaded_results:
            try:
                await client.copy_message(
                    chat_id=target_id, from_chat_id=sent_msg.chat.id, message_id=sent_msg.id
                )
            except Exception as e:
                LOGGER.error(f"[RTI] Channel copy error: {e}")
                try:
                    await message.reply(f"❌ Channel copy fail (`{target_id}`): `{str(e)[:150]}`")
                except Exception:
                    pass
    except Exception as e:
        LOGGER.error(f"[RTI] _forward_to_channel_if_enabled error: {e}")


async def _process_episode(client, message, page_url, episode_num, total_episodes, status_msg):
    """
    Ek episode ke liye Swift URL nikalo aur download+upload karo.

    Return values:
      "ok"       → success, agle episode pe jao
      "retry"    → sabhi retries ke baad bhi Swift URL nahi mila → BAND karo
      "error"    → download/upload mein exception → BAND karo
    """
    loop = asyncio.get_event_loop()

    # Movie mode (episode_num == 0) mein status messages "Ep 0" ki jagah
    # "Movie" dikhayenge — zyada readable hai
    ep_label = "Movie" if episode_num == 0 else f"Ep {episode_num}"

    # ── Swift URL nikalne ke liye retry constants ──
    SWIFT_MAX_ATTEMPTS   = 5    # 5 baar try karo
    SWIFT_RETRY_INTERVAL = 60   # har attempt ke beech 60s wait

    swift_url = None

    for attempt in range(1, SWIFT_MAX_ATTEMPTS + 1):
        # Step 1: WatchMultQuality link
        try:
            await status_msg.edit(
                f"🔍 **{ep_label}/{total_episodes}**\n"
                f"Attempt `{attempt}/{SWIFT_MAX_ATTEMPTS}` — WatchMultQuality link dhundh raha hoon..."
            )
        except Exception:
            pass

        wmq_link, _ = await job_ctl.run_in_thread(get_watchmult_link, page_url, episode_num)

        if wmq_link:
            # Step 2: Argon link
            try:
                await status_msg.edit(
                    f"🔍 **{ep_label}/{total_episodes}**\n"
                    f"Attempt `{attempt}/{SWIFT_MAX_ATTEMPTS}` — Argon link extract ho raha hai..."
                )
            except Exception:
                pass

            argon_link = await job_ctl.run_in_thread(get_argon_link, wmq_link)

            if argon_link:
                # Step 3: Swift URL
                swift_url = argon_to_swift(argon_link)
                if swift_url:
                    break  # ✅ Swift URL mil gaya — loop se bahar

        # Yahan tak matlab kuch na kuch fail hua — kaunsa step? (user ko dikhao)
        _why = "WatchMultQuality link page pe nahi mila"
        if wmq_link:
            _why = "WMQ link mil gaya par Argon link nahi nikla"
            if argon_link:
                _why = "Argon link mila par Swift code nahi ban paya"
        if wmq_link and not argon_link:
            _dg = await job_ctl.run_in_thread(diagnose_wmq_page, wmq_link)
            _dg = re.sub(r"[`*_\[\]]", "", str(_dg))[:330]
            _why += f"\n🔎 {_dg}"
        if not wmq_link:
            _dg = await job_ctl.run_in_thread(diagnose_episode, page_url, episode_num)
            _dg = re.sub(r"[`*_\[\]]", "", str(_dg))[:330]          # Telegram markdown na toote
            _why += f"\n🔎 {_dg}"
            LOGGER.warning(f"[RTI] Ep {episode_num} attempt {attempt}: {_why}")
        else:
            LOGGER.warning(f"[RTI] Ep {episode_num} attempt {attempt}: {_why} ({wmq_link})")
        if attempt == SWIFT_MAX_ATTEMPTS:
            # Sabhi attempts khatam — HARD STOP signal bhejo
            try:
                await status_msg.edit(
                    f"🛑 **{ep_label} — Complete Fail**\n\n"
                    f"❌ `{SWIFT_MAX_ATTEMPTS}` attempts ke baad bhi Swift URL nahi mila.\n"
                    f"📌 Wajah: {_why[:420]}\n"
                    f"⛔ Agle episodes **band** kar diye gaye.\n"
                    f"RTI pe manually check karo."
                )
            except Exception:
                pass
            return "retry"

        # Retry message
        remaining = SWIFT_MAX_ATTEMPTS - attempt
        try:
            await status_msg.edit(
                f"⏳ **{ep_label}/{total_episodes}**\n\n"
                f"🔄 Attempt `{attempt}/{SWIFT_MAX_ATTEMPTS}` fail — {_why[:420]}\n"
                f"⏰ `{SWIFT_RETRY_INTERVAL}s` baad retry... (`{remaining}` attempts baki)"
            )
        except Exception:
            pass

        await asyncio.sleep(SWIFT_RETRY_INTERVAL)

    # ── Swift URL mil gaya — download + upload ──
    try:
        await status_msg.edit(
            f"✅ **{ep_label}/{total_episodes} — Link mil gaya**\n\n"
            f"⬇️ Ab download shuru ho raha hai..."
        )
        await _run_rti_swift(client, message, swift_url, status_msg, ep_num=episode_num, total_eps=total_episodes, page_url=page_url)
        return "ok"
    except Exception as e:
        LOGGER.error(f"[RTI] {ep_label} download/upload error: {e}")
        try:
            await status_msg.edit(
                f"🛑 **{ep_label} — Upload Error**\n\n"
                f"❌ `{str(e)[:120]}`\n"
                f"⛔ Agle episodes **band** kar diye gaye."
            )
        except Exception:
            pass
        return "error"


# ─────────────────────────────────────────────
#  NEW: Selector se cached WatchMultQuality link ke saath download
#  (page dubara scrape nahi karna — link already discover_items() se
#  mil chuka hai, seedha argon → swift step se shuru karo)
# ─────────────────────────────────────────────
async def _process_cached_item(client, message, item, status_msg, index, total, sess: "RTISelector" = None):
    loop = asyncio.get_event_loop()
    ep_label = item["label"]
    wmq_link = item["wmq_link"]

    SWIFT_MAX_ATTEMPTS = 5
    SWIFT_RETRY_INTERVAL = 60
    swift_url = None

    for attempt in range(1, SWIFT_MAX_ATTEMPTS + 1):
        try:
            await status_msg.edit(
                f"🔍 **{ep_label}** (`{index}/{total}`)\n"
                f"Attempt `{attempt}/{SWIFT_MAX_ATTEMPTS}` — Argon link extract ho raha hai..."
            )
        except Exception:
            pass

        argon_link = await job_ctl.run_in_thread(get_argon_link, wmq_link)
        _why = "WMQ link mil gaya par Argon link nahi nikla"
        if argon_link:
            swift_url = argon_to_swift(argon_link)
            if swift_url:
                break
            _why = "Argon link mila par Swift code nahi ban paya"
        else:
            _dg = await job_ctl.run_in_thread(diagnose_wmq_page, wmq_link)
            _why += "\n🔎 " + re.sub(r"[`*_\[\]]", "", str(_dg))[:330]
        LOGGER.warning(f"[RTI] {ep_label} attempt {attempt}: {_why} ({wmq_link})")

        if attempt == SWIFT_MAX_ATTEMPTS:
            try:
                await status_msg.edit(
                    f"🛑 **{ep_label} — Complete Fail**\n\n"
                    f"❌ `{SWIFT_MAX_ATTEMPTS}` attempts ke baad bhi Swift URL nahi mila.\n"
                    f"⛔ Agle items **band** kar diye gaye.\n"
                    f"RTI pe manually check karo."
                )
            except Exception:
                pass
            return "retry"

        remaining = SWIFT_MAX_ATTEMPTS - attempt
        try:
            await status_msg.edit(
                f"⏳ **{ep_label}** (`{index}/{total}`)\n\n"
                f"🔄 Attempt `{attempt}/{SWIFT_MAX_ATTEMPTS}` fail — {_why}\n"
                f"⏰ `{SWIFT_RETRY_INTERVAL}s` baad retry... (`{remaining}` attempts baki)"
            )
        except Exception:
            pass

        await asyncio.sleep(SWIFT_RETRY_INTERVAL)

    try:
        await status_msg.edit(
            f"✅ **{ep_label}** (`{index}/{total}`) — Link mil gaya\n\n"
            f"⬇️ Ab download shuru ho raha hai..."
        )
        ep_num_for_swift = 0 if item["kind"] == "MOVIE" else item["num"]
        await _run_rti_swift(client, message, swift_url, status_msg, ep_num=ep_num_for_swift, total_eps=total, sess=sess)
        return "ok"
    except Exception as e:
        LOGGER.error(f"[RTI] {ep_label} download/upload error: {e}")
        try:
            await status_msg.edit(
                f"🛑 **{ep_label} — Upload Error**\n\n"
                f"❌ `{str(e)[:120]}`\n"
                f"⛔ Agle items **band** kar diye gaye."
            )
        except Exception:
            pass
        return "error"


async def _download_items(client, status_msg, orig_message, sess: "RTISelector", idxs: list):
    """
    Selected items ko line by line download+upload karo.
    Poora process EK hi card message mein dikhta hai (status_msg), uske neeche
    "❌ Cancel" button — dabane par sab ruk jata hai aur cache delete ho jata hai.
    """
    total = len(idxs)
    job = job_ctl.Job(status_msg, orig_message.from_user.id, title=(sess.title or "RTI")[:55])
    job.quota_count = total
    state = {"ok": 0, "last": "ok"}

    # ── Channel Upload ON → bilkul /Rtic wala engine ──
    # (channel pe seedha upload, cover/thumbnail, bot-mode post, update post,
    #  schedule notification). Anime /add_anime list mein milna zaruri hai.
    rtic_entry = None
    if sess.channel_toggle:
        try:
            rtic_entry, _al = await _find_add_anime_channel(sess.title, sess.page_url)
        except Exception as e:
            LOGGER.error(f"[RTI] add_anime lookup error: {e}")
        if rtic_entry and not rtic_entry.get("channel_id"):
            rtic_entry = None
        if not rtic_entry:
            try:
                await orig_message.reply(
                    "⚠️ **Channel upload skip** — ye anime `/add_anime` list mein nahi mila, "
                    "isliye normal (bot chat) upload hoga."
                )
            except Exception:
                pass

    async def _rtic_body():
        from .auto_monitor import (
            _episode_quality_poller, _get_swift_url_for_episode,
            _get_schedule_fn, _owner_id,
        )
        stage = job.slot("stage", order=0)
        anime_name = rtic_entry["anime_name"]
        channel_id = rtic_entry["channel_id"]
        oid = (await _owner_id()) or orig_message.from_user.id
        nums = [0 if sess.items[i]["kind"] == "MOVIE" else (sess.items[i]["num"] or 0) for i in idxs]
        start_ep, end_ep = nums[0], max(nums)
        update_post_sent = [False]
        # Season intro sirf tab jab selection Ep 1 (EP kind) se shuru ho
        _first = sess.items[idxs[0]]
        season_intro_state = [not (_first["kind"] == "EP" and nums[0] == 1)]
        up_mode = "force" if sess.update_toggle else "off"
        any_uploaded = False

        FAST, SLOW = 10, 20
        MAX_ATT = FAST + SLOW
        for i, idx in enumerate(idxs, 1):
            item = sess.items[idx]
            ep_num = nums[i - 1]
            job.header = f"📺 **{item['label']}** • `{i}/{total}`"
            job.reset_slots(keep=("stage",))
            await stage.edit(f"🎌 **{item['label']}**\n\n🔍 Swift URL nikaal raha hoon...")

            swift_url = None
            for att in range(1, MAX_ATT + 1):
                swift_url = await _get_swift_url_for_episode(
                    sess.page_url, ep_num, stage, wmq_link=item["wmq_link"]
                )
                if swift_url:
                    break
                if att == MAX_ATT:
                    await stage.edit(
                        f"❌ **{item['label']}**\n\n"
                        f"⏱️ {MAX_ATT} attempts (~25 min) ke baad bhi link nahi mila."
                    )
                    break
                interval = 30 if att <= FAST else 60
                await stage.edit(
                    f"⏳ **{item['label']}**\n\n"
                    f"🔄 Attempt `{att}/{MAX_ATT}` — link nahi mila\n"
                    f"⏰ `{interval}s` baad retry... ({MAX_ATT - att} attempts left)"
                )
                await asyncio.sleep(interval)

            if not swift_url:
                state["last"] = "retry"
                break

            ok = await _episode_quality_poller(
                client, orig_message, swift_url,
                ep_num, anime_name, channel_id, oid,
                matched_entry=rtic_entry,
                start_ep=start_ep, end_ep=end_ep,
                update_post_sent=update_post_sent,
                update_post_mode=up_mode,
                season_intro=season_intro_state,
                season_hint=sess.season,
                fb_page_url=sess.page_url,                 # 1080p Swift pe na ho to mirror servers
            )
            if not ok:
                state["last"] = "error"
                break
            any_uploaded = True
            state["ok"] += 1
            job.done_eps.append(item["label"])
            if i < total:
                await asyncio.sleep(3)

        if any_uploaded:
            try:
                await _get_schedule_fn()(client, channel_id, anime_name, end_ep)
            except Exception as e:
                LOGGER.error(f"[RTI] Schedule notification error: {e}")

    async def _body():
        if rtic_entry:
            await _rtic_body()
            return
        stage = job.slot("stage", order=0)
        for i, idx in enumerate(idxs, 1):
            item = sess.items[idx]
            job.header = f"📺 **{item['label']}** • `{i}/{total}`"
            job.reset_slots(keep=("stage",))
            await stage.edit(f"🔍 `{item['label']}` ka link nikal raha hoon...")
            result = await _process_cached_item(client, orig_message, item, stage, i, total, sess=sess)
            state["last"] = result
            if result != "ok":
                break
            state["ok"] += 1
            job.done_eps.append(item["label"])
            if i < total:
                await asyncio.sleep(3)

    try:
        await job.run(_body)
    except Exception as e:
        LOGGER.error(f"[RTI] _download_items error: {e}")
        state["last"] = "error"
        stage = job.slot("stage", order=0)
        stage.text = f"❌ `{str(e)[:150]}`"

    if job.cancelled:
        return                      # cancel() ne card final kar diya
    job.reset_slots(keep=("stage",))
    if state["last"] == "ok":
        await job.finish(
            f"🎉 **Complete!** — {job.title}\n\n"
            f"✅ `{state['ok']}/{total}` upload ho gaye\n"
            f"📋 {' '.join(job.done_eps[-12:])}"
        )
    else:
        job.finished = False
        body = job.render()
        await job.finish(body + f"\n\n⛔ Process band ho gaya (`{state['ok']}/{total}` complete).")


# ─────────────────────────────────────────────
#  NEW: Episode selector ke buttons ka callback handler
# ─────────────────────────────────────────────
@Client.on_callback_query(filters.regex(r"^rti_"))
async def rti_callback_handler(client: Client, cb: CallbackQuery):
    try:
        parts = cb.data.split("_")
        action = parts[1] if len(parts) > 1 else ""
        sid = parts[2] if len(parts) > 2 else None
        sess = RTI_SESSIONS.get(sid)

        if not sess:
            await cb.answer("⌛ Session expired, /rti <url> dubara bhejo.", show_alert=True)
            return

        if action == "noop":
            await cb.answer()
            return

        if action == "season":
            # SEASON button: us season ke saare episode apne aap select ho kar
            # line by line (ek ke baad ek) download+upload honge — bilkul
            # "/rti <url> 1 N" ki tarah.
            idxs = [i for i, it in enumerate(sess.items) if it.get("kind") == "EP"]
            if not idxs:
                idxs = list(range(len(sess.items)))
            idxs.sort(key=lambda i: (sess.items[i].get("num") or 0, i))
            if not idxs:
                await cb.answer("Is season mein koi episode nahi mila.", show_alert=True)
                return
            RTI_SESSIONS.pop(sid, None)
            await cb.answer(f"⬇️ Season {sess.season}: {len(idxs)} episodes shuru...")
            status_msg = cb.message
            try:
                await status_msg.edit(
                    f"🎌 **Season {sess.season:02d}** — `{len(idxs)}` episodes line by line "
                    f"(Ep {sess.items[idxs[0]].get('num')} → Ep {sess.items[idxs[-1]].get('num')})..."
                )
            except Exception:
                pass
            await _download_items(client, status_msg, sess.orig_message, sess, idxs)
            return

        if action == "close":
            RTI_SESSIONS.pop(sid, None)
            await cb.answer()
            try:
                await cb.message.delete()
            except Exception:
                pass
            return

        if action == "q":
            key = parts[3] if len(parts) > 3 else ""
            if key in _RTI_Q_KEYS:
                sess.quality = key
            if sess.channel_toggle and sess.quality != "all":
                await cb.answer("ℹ️ Channel Upload ON hai — channel flow mein saari qualities upload hoti hain. "
                                "Sirf ye quality chahiye to Channel Upload OFF karo.", show_alert=True)
            else:
                await cb.answer(f"Quality: {sess.quality_label}")
            await sess.render()
            return

        if action == "toggle":
            sess.multi = not sess.multi
            if not sess.multi:
                sess.selected.clear()
            await cb.answer("Multi-select " + ("ON" if sess.multi else "OFF"))
            await sess.render()
            return

        if action == "uptog":
            # Session-local only — global /updatechannel toggle ko na
            # padhte hain na likhte hain, isliye yeh sirf isi /rti request
            # ke uploads pe asar karega, kahin aur nahi.
            new_val = not sess.update_toggle
            sess.update_toggle = new_val
            await cb.answer("📢 Update Post " + ("ON" if new_val else "OFF"))
            await sess.render()
            return

        if action == "chtog":
            new_val = not sess.channel_toggle
            if new_val:
                try:
                    entry, anime_list = await _find_add_anime_channel(sess.title, sess.page_url)
                except Exception as e:
                    LOGGER.error(f"[RTI] add_anime lookup error: {e}")
                    entry, anime_list = None, []
                if not anime_list:
                    await cb.answer(
                        "❌ Pehle /add_anime se anime + channel add karo!", show_alert=True
                    )
                    return
                if not entry:
                    await cb.answer(
                        f"❌ '{sess.title[:50]}' /add_anime list mein nahi hai.\n"
                        f"Pehle /add_anime se is anime ko add karo.",
                        show_alert=True,
                    )
                    return
            await _set_rti_channel_upload(sess.orig_message.from_user.id, new_val)
            sess.channel_toggle = new_val
            await cb.answer("📤 Channel Upload " + ("ON" if new_val else "OFF"))
            await sess.render()
            return

        if action == "pg":
            try:
                new_page = int(parts[3])
            except (IndexError, ValueError):
                await cb.answer()
                return
            sess.page = max(0, min(new_page, sess.total_pages - 1))
            await cb.answer()
            await sess.render()
            return

        if action == "ep":
            try:
                idx = int(parts[3])
            except (IndexError, ValueError):
                await cb.answer()
                return

            if sess.multi:
                if idx in sess.selected:
                    sess.selected.discard(idx)
                else:
                    sess.selected.add(idx)
                await cb.answer()
                await sess.render()
            else:
                await cb.answer()
                RTI_SESSIONS.pop(sid, None)
                status_msg = cb.message
                try:
                    await status_msg.edit(f"🎌 **RTI** — Starting `{sess.items[idx]['label']}`...")
                except Exception:
                    pass
                await _download_items(client, status_msg, sess.orig_message, sess, [idx])
            return

        if action == "dl":
            if not sess.selected:
                await cb.answer("Pehle kuch episode select karo!", show_alert=True)
                return
            idxs = sorted(sess.selected)
            RTI_SESSIONS.pop(sid, None)
            await cb.answer()
            status_msg = cb.message
            try:
                await status_msg.edit(f"🎌 **RTI** — Starting `{len(idxs)}` selected item(s)...")
            except Exception:
                pass
            await _download_items(client, status_msg, sess.orig_message, sess, idxs)
            return

        await cb.answer()
    except Exception as e:
        LOGGER.error(f"[RTI] callback error: {e}")
        try:
            await cb.answer("❌ Error, dubara try karo.", show_alert=True)
        except Exception:
            pass


# ─────────────────────────────────────────────
#  /rti Command Handler
# ─────────────────────────────────────────────
@Client.on_message(filters.command("rti"))
async def rti_command(client: Client, message: Message):
    """
    /rti <url>               -> Latest episode auto-download
    /rti <url> <start> <end> -> Episode range
    /rti <url> 5 5           -> Sirf episode 5
    """
    c = await check_chat(message, chat="Sudo")
    if not c:
        return

    if not SELENIUM_OK:
        await message.reply("❌ Selenium install nahi hai! `pip install selenium`")
        return

    parts = message.text.split()

    # Minimum: /rti <url>
    if len(parts) < 2:
        await message.reply(
            "**Usage:**\n"
            "`/rti <url>` — Button menu khulega (season/episode select karo)\n"
            "`/rti <url> <start> <end>` — Episode range (bina button ke, direct)\n"
            "`/rti <url> 0 0` — Movie mode (no episode number on page)\n\n"
            "**Examples:**\n"
            "`/rti https://rareanimes.buzz/wistoria/` — Button menu\n"
            "`/rti https://rareanimes.buzz/wistoria/ 01 10` — Ep 1 to 10\n"
            "`/rti https://rareanimes.buzz/wistoria/ 5 5` — Sirf Ep 5\n"
            "`/rti https://rareanimes.mov/a-magnificent-life/ 0 0` — Movie"
        )
        return

    page_url = parts[1].strip()

    if not page_url.startswith("http"):
        await message.reply("❌ Valid URL dalo.")
        return

    # ── BUTTON MENU MODE: sirf URL diya, koi number nahi ──
    if len(parts) == 2:
        status_msg = await message.reply("🔍 Page scan ho raha hai...")

        loop = asyncio.get_event_loop()
        try:
            data = await loop.run_in_executor(None, discover_items, page_url)
        except Exception as e:
            LOGGER.error(f"[RTI] discover_items error: {e}")
            await status_msg.edit(f"❌ Page load nahi hua: `{str(e)[:100]}`")
            return

        if not data["items"]:
            await status_msg.edit("❌ Page se koi episode/movie link nahi mila. URL check karo.")
            return

        _prune_sessions()
        sess = RTISelector(page_url, data, orig_message=message)
        sess.sid = uuid.uuid4().hex[:10]
        await sess.populate_toggles()
        RTI_SESSIONS[sess.sid] = sess

        try:
            await status_msg.delete()
        except Exception:
            pass

        sess.msg = await message.reply(sess.header_text(), reply_markup=sess.build_markup())
        return

    # ── RANGE MODE: /rti <url> <start> <end> ──
    if len(parts) < 4:
        await message.reply(
            "❌ Range ke liye do numbers chahiye.\n"
            "Example: `/rti <url> 1 10`"
        )
        return

    try:
        start_ep = int(parts[2])
        end_ep   = int(parts[3])
    except ValueError:
        await message.reply("❌ Episode number valid nahi.\nExample: `/rti <url> 1 10`")
        return

    if start_ep > end_ep:
        await message.reply("❌ Start > End nahi ho sakta.")
        return

    if end_ep - start_ep > 50:
        await message.reply("❌ Max 50 episodes ek baar mein.")
        return

    total_eps    = end_ep - start_ep + 1
    episode_list = list(range(start_ep, end_ep + 1))

    status_msg = await message.reply(
        f"🎌 **RTI** — Ep `{start_ep}` to `{end_ep}` (Total: `{total_eps}`)\n"
        f"⏳ Starting..."
    )

    job = job_ctl.Job(status_msg, message.from_user.id, title=f"RTI Ep {start_ep}-{end_ep}")
    job.quota_count = total_eps
    state = {"ok": 0, "last": "ok"}

    async def _range_body():
        stage = job.slot("stage", order=0)
        for i, ep_num in enumerate(episode_list, 1):
            job.header = f"📺 **Ep {ep_num}** • `{i}/{total_eps}`"
            job.reset_slots(keep=("stage",))
            result = await _process_episode(
                client, message, page_url,
                episode_num=ep_num,
                total_episodes=total_eps,
                status_msg=stage,
            )
            state["last"] = result
            if result != "ok":
                # "retry" ya "error" — dono cases mein HARD STOP
                LOGGER.warning(f"[RTI] Ep {ep_num} failed ({result}). Stopping range.")
                break
            state["ok"] += 1
            job.done_eps.append(f"EP{ep_num:02d}")
            if i < total_eps:
                await asyncio.sleep(3)

    try:
        await job.run(_range_body)
    except Exception as e:
        LOGGER.error(f"[RTI] range error: {e}")
        state["last"] = "error"
        job.slot("stage", order=0).text = f"❌ `{str(e)[:150]}`"

    if job.cancelled:
        return
    job.reset_slots(keep=("stage",))
    if state["last"] == "ok":
        await job.finish(
            f"🎉 **Complete!** — Ep {start_ep}-{end_ep}\n\n"
            f"✅ `{state['ok']}/{total_eps}` upload ho gaye"
        )
    else:
        job.finished = False
        await job.finish(job.render() + f"\n\n⛔ Process band ho gaya (`{state['ok']}/{total_eps}` complete).")
