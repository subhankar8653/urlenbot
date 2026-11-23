"""
hindi_schedule.py
=================
Hindi anime schedule ko channel mein roz auto-post karo.

  Source : https://hindianimeszone.com/schedule.html   (JavaScript se bana page —
           isliye bot ka headless Chrome use hota hai, aaj ke din (IST) ka tab chunta hai)
  Filter : sirf HINDI dub wale anime
  Post   : professional list (time ke hisaab se sorted) — set kiye channel mein, roz set time pe

Commands (owner/sudo):
  /add_hindi_anime_schedule <channel_id>   — schedule channel ADD karo (multiple ho sakte hain)
  /hindi_schedule_time <time>              — roz kab post ho (IST), jaise 08:00 / 8:30 PM
  /hindi_schedule [on|off]                 — status / chalu / band
  /hindi_schedule_now [mon..sun]           — abhi post karo (aaj ka, ya koi din)
  /hindi_schedule_debug                    — page ka raw text file (parser tune karne ke liye)
  /hindi_schedule_check                    — dono sites (hindianimeszone + deadtoons) ka comparison, post kiye bina

  /hindi_schedule_style [new|old]          — post ka style: NEW (image + clickable anime links) / OLD (purana list)

/customise → ⏰ Cleanup & Schedule → 📅 Hindi Anime Schedule  (sab buttons se bhi)

NEW style : upar stylish image (poster-11 design: aaj ki date, anime list + time, community ke 1/2 username,
            aaj ke anime mein se kisi ek ka character) + neeche caption list — har anime ka naam us anime ke
            channel ka join link hota hai (SIRF agar anime /add_anime se add hai; warna plain text).
OLD style : purana banner + box wali list (koi badlav nahi).

Data: db.col2 {'id': 'hindi_schedule_cfg'} → {'cfg': {channel_id, channel_title, enabled,
      time 'HH:MM' (IST 24h), last_date 'YYYY-MM-DD', last_try ISO}}
"""

import asyncio
import html as _html
import io
import os
import re
import shutil
import tempfile
import time as _time
from difflib import SequenceMatcher
from datetime import datetime, timedelta, timezone

from pyrogram import Client, filters, StopPropagation, ContinuePropagation
from pyrogram.enums import ParseMode
from pyrogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Message,
)

from .. import LOGGER, owner, sudo_users
from ..utils.database.access_db import db

SCHEDULE_URL = "https://hindianimeszone.com/schedule.html"
IST = timezone(timedelta(hours=5, minutes=30))
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
DAY_FULL = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
DEFAULT_TIME = "08:00"
CATCHUP_MIN = 180          # bot restart ho gaya ho to post-time ke itne minute tak late post kar do
RETRY_GAP_MIN = 20         # fail hone par dobara try karne se pehle gap
MAX_MSG_LEN = 3800
RECHECK_HOURS = float(os.getenv("HS_RECHECK_HOURS", "3"))     # post ke itne ghante baad dobara check (0 = band)
RECHECK_RETRY_MIN = 30                                         # recheck fail ho to itne minute baad phir (max 3 baar)
CROSSCHECK = os.getenv("HS_CROSSCHECK", "1") != "0"            # deadtoons.sbs se double-check

BUSY = False               # janitor (auto-refresh) is flag ko dekhta hai
_SESS: dict = {}           # user_id → {"mode": "channel" | "time"}
_LOCK = asyncio.Lock()


# ───────────────────────── config ─────────────────────────
async def _cfg() -> dict:
    doc = await db.col2.find_one({'id': 'hindi_schedule_cfg'})
    return dict((doc or {}).get('cfg') or {})


async def _save(cfg: dict):
    await db.col2.update_one({'id': 'hindi_schedule_cfg'}, {'$set': {'cfg': cfg}}, upsert=True)


def _is_auth(uid: int) -> bool:
    return uid in owner or uid in sudo_users


def clear_session(uid: int):
    _SESS.pop(uid, None)


# ───────────────────────── time helpers ─────────────────────────
def parse_time(s: str):
    """'08:00' / '8:30 pm' / '8pm' / '2030' -> (h, m) 24h, ya None."""
    s = (s or "").strip().lower().replace(".", ":")
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*([ap]m)?", s)
    if not m:
        m2 = re.fullmatch(r"(\d{2})(\d{2})", s)
        if not m2:
            return None
        h, mi, ap = int(m2.group(1)), int(m2.group(2)), None
    else:
        h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap:
        if not 1 <= h <= 12:
            return None
        h = (h % 12) + (12 if ap == "pm" else 0)
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return None
    return h, mi


def fmt12(h: int, m: int) -> str:
    ap = "AM" if h < 12 else "PM"
    return f"{(h % 12) or 12}:{m:02d} {ap}"


def _hhmm(s: str):
    return parse_time(s or DEFAULT_TIME) or parse_time(DEFAULT_TIME)


def should_post(now: datetime, cfg: dict) -> bool:
    """Scheduler ka faisla (pure function): abhi post karna hai?"""
    if not cfg.get("channel_id") or not cfg.get("enabled", True):
        return False
    h, m = _hhmm(cfg.get("time"))
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if cfg.get("last_date") == now.date().isoformat():
        return False
    if not (target <= now <= target + timedelta(minutes=CATCHUP_MIN)):
        return False
    lt = cfg.get("last_try")
    if lt:
        try:
            if now - datetime.fromisoformat(lt).replace(tzinfo=IST) < timedelta(minutes=RETRY_GAP_MIN):
                return False
        except Exception:
            pass
    return True


# ───────────────────────── scraping (Selenium) ─────────────────────────
_JS_CLICK_DAY = """
const label = arguments[0];
const c = [...document.querySelectorAll('button,a,div,span,li')].filter(e =>
  e.children.length <= 2 && e.innerText && e.innerText.trim() === label && e.offsetParent !== null);
if (!c.length) return false;
c[0].click(); return true;
"""

_JS_SCROLL = """
window.scrollTo(0, document.body.scrollHeight);
[...document.querySelectorAll('button,a')].filter(e =>
  /load more|show more|view more|see more/i.test(e.innerText || '') && e.offsetParent !== null
).forEach(e => { try { e.click(); } catch (x) {} });
return document.body ? document.body.innerText : '';
"""

_TAB_LABELS = {"All days", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"}


def cards_from_body(body: str) -> list:
    """
    Page ke body innerText ko cards mein todo. Har card countdown ('NEXT RELEASE',
    'dd DAYS', 'hh HOURS', 'mm MINS', 'ss SECS') pe khatam hota hai, aur uske baad kabhi
    'Watch Now' button ka text aata hai. DOM/class names pe koi dependency nahi.
    """
    lines = [l.rstrip() for l in (body or "").splitlines()]
    start = 0
    for i, l in enumerate(lines):          # day-tabs ('All days'...'Sun') ke baad se cards shuru
        if l.strip() == "Sun" and any(x.strip() == "All days" for x in lines[max(0, i - 8):i]):
            start = i + 1
            break
    cards, cur = [], []
    skip_watch = False
    for l in lines[start:]:
        t = l.strip()
        if not t:
            continue
        if skip_watch:
            skip_watch = False
            if t.lower() == "watch now":
                continue
        cur.append(t)
        if t.upper() == "SECS":             # countdown ka aakhri label = card poora
            cards.append("\n".join(cur))
            cur = []
            skip_watch = True
    if cur and any("per release" in x.lower() for x in cur):   # countdown ke bina aakhri card
        cards.append("\n".join(cur))
    return cards


def _card_key(card: str) -> str:
    for l in card.splitlines():
        if l.strip() and l.strip().lower() not in ("watch now", "i"):
            return l.strip().lower()
    return card[:40]


def _scrape_sync(day_idx):
    """Page render karo, day_idx (0=Mon) ka tab chuno, scroll karke saare cards load karo."""
    from .rti_downloader import SELENIUM_OK, _kill_driver_tree
    if not SELENIUM_OK:
        raise RuntimeError("Selenium install nahi hai")
    from .swift_downloader import _make_driver

    dl = tempfile.mkdtemp(prefix="hs_")
    driver = None
    try:
        driver = _make_driver(dl)
        driver.get(SCHEDULE_URL)
        deadline = _time.time() + 45
        while _time.time() < deadline:
            txt = driver.execute_script("return document.body ? document.body.innerText : ''") or ""
            if re.search(r"per release", txt, re.IGNORECASE):
                break
            _time.sleep(1.5)
        clicked = False
        if day_idx is not None:
            clicked = bool(driver.execute_script(_JS_CLICK_DAY, DAYS[day_idx]))
            _time.sleep(2.5)

        # Cards lazy-load hote hain (32 scheduled, par shuru mein 5 hi dikhte hain):
        # neeche tak scroll karte raho, har step ke cards jama karo (virtual list ho to bhi chale)
        found: dict = {}
        body = ""
        expected = 0
        stable = 0
        for _ in range(60):
            body = driver.execute_script(_JS_SCROLL) or ""
            m = re.search(r"(\d+)\s+Anime Scheduled", body)
            if m:
                expected = int(m.group(1))
            before = len(found)
            for c in cards_from_body(body):
                found.setdefault(_card_key(c), c)
            if expected and len(found) >= expected:
                break
            stable = stable + 1 if len(found) == before else 0
            if stable >= 4:
                break
            _time.sleep(1.2)

        try:
            res = driver.execute_script(
                "return performance.getEntriesByType('resource').map(e => e.name)") or []
        except Exception:
            res = []
        return {"cards": list(found.values()), "body": body, "resources": res,
                "clicked": clicked, "expected": expected}
    finally:
        try:
            _kill_driver_tree(driver)
        except Exception:
            pass
        shutil.rmtree(dl, ignore_errors=True)


# ───────────────────────── parsing ─────────────────────────
_LANGS = {"HINDI", "TAMIL", "TELUGU", "ENGLISH", "JAPANESE", "MALAYALAM", "KANNADA",
          "BENGALI", "KOREAN", "CHINESE", "MARATHI", "URDU", "PUNJABI", "SPANISH"}
_SKIP_LINES = {"i", "DAILY", "WEEKLY", "NEXT RELEASE", "DAYS", "HOURS", "MINS", "SECS", "NEW",
               "ONGOING", "WATCH NOW", "STARTING SOON...", "COMPLETE", "COMPLETED"}
_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], 1)}


def _clean_time(t: str) -> str:
    t = re.sub(r"\s+", " ", (t or "").strip().upper())
    m = re.match(r"0?(\d{1,2}:\d{2}) ([AP]M)", t)
    return f"{m.group(1)} {m.group(2)}" if m else t


def parse_card(text: str) -> dict:
    """Ek card ke text se fields nikalo (class names pe nahi, text pattern pe)."""
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    d = {"title": "", "season": None, "langs": [], "freq": "", "time": None,
         "per_release": 1, "cur": None, "total": None, "next": None, "next_end": None,
         "next_date": None, "next_time": None, "upcoming": False, "complete": False, "raw": text}
    for l in lines:
        if l.upper() in _SKIP_LINES or re.fullmatch(r"\d{1,2}", l) or len(l) < 2:
            continue
        if re.match(r"^Season\s+\d+", l, re.IGNORECASE) or l.upper() in _LANGS:
            continue
        if "@" in l or "per release" in l.lower() or l.startswith(("📺", "📅", "🔁", "🎙")):
            continue
        d["title"] = l
        break
    for l in lines:
        m = re.match(r"^Season\s+(\d+)", l, re.IGNORECASE)
        if m and d["season"] is None:
            d["season"] = int(m.group(1))
        if l.upper() in _LANGS and l.upper() not in d["langs"]:
            d["langs"].append(l.upper())
        if "@" in l:
            mt = re.search(r"@\s*(\d{1,2}:\d{2}\s*[AP]M)", l, re.IGNORECASE)
            if mt and not d["time"]:
                d["time"] = _clean_time(mt.group(1))
            if re.search(r"every day|daily", l, re.IGNORECASE):
                d["freq"] = "Daily"
            else:
                mw = re.search(r"(?:Weekly\s*·\s*|every\s+)([A-Za-z]+)", l, re.IGNORECASE)
                if mw and mw.group(1).lower() not in ("day", "weekly"):
                    d["freq"] = f"Every {mw.group(1).capitalize()}"
        mp = re.search(r"(\d+)\s*episode\(s\)\s*per release", l, re.IGNORECASE)
        if mp:
            d["per_release"] = int(mp.group(1))
        me = re.search(r"Episode\s+(\d+)\s*/\s*(\d+)", l, re.IGNORECASE)
        if me and d["cur"] is None:
            d["cur"], d["total"] = int(me.group(1)), int(me.group(2))
        mn = re.search(r"Next:\s*Episodes?\s*(\d+)(?:\s*[–\-]\s*(\d+))?", l, re.IGNORECASE)
        if mn:
            d["next"] = int(mn.group(1))
            d["next_end"] = int(mn.group(2)) if mn.group(2) else None
        md = re.search(r"([A-Za-z]+),\s*([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})\s+at\s+(\d{1,2}:\d{2}\s*[AP]M)", l)
        if md and md.group(2).lower() in _MONTHS:
            try:
                d["next_date"] = datetime(int(md.group(4)), _MONTHS[md.group(2).lower()],
                                          int(md.group(3))).date()
                d["next_time"] = _clean_time(md.group(5))
            except ValueError:
                pass
    if re.search(r"UPCOMING DUB|Starting soon", text or "", re.IGNORECASE):
        d["upcoming"] = True
    # FIX: COMPLETE badge / "Complete Series! All 39 episodes aired." wala anime ab aane wala nahi hai —
    # pehle (jaise JoJo Golden Wind) ye bhi "daily" maan ke roz schedule mein aa jaata tha.
    if (re.search(r"(?im)^\W*COMPLETED?\W*$", text or "")
            or re.search(r"complete\s+series|all\s+\d+\s+episodes?\s+(?:have\s+)?aired", text or "", re.IGNORECASE)):
        d["complete"] = True
    return d


def _time_key(t):
    if not t:
        return (99, 99)
    m = re.match(r"(\d{1,2}):(\d{2})\s*([AP]M)", t, re.IGNORECASE)
    if not m:
        return (99, 99)
    h = int(m.group(1)) % 12 + (12 if m.group(3).upper() == "PM" else 0)
    return (h, int(m.group(2)))


def filter_hindi(items: list) -> list:
    out = [i for i in items if i["title"] and not i.get("complete")
           and ("HINDI" in i["langs"] or not i["langs"])]
    return sorted(out, key=lambda i: (_time_key(i["time"]), i["title"].lower()))


# ───────────────────────── message format ─────────────────────────
_NUM = ["①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩"]


def _lang_line(langs):
    return " • ".join(l.capitalize() for l in langs) if langs else "Hindi"


_INDIAN_LANGS = ("HINDI", "TAMIL", "TELUGU", "MALAYALAM", "KANNADA", "BENGALI",
                 "MARATHI", "PUNJABI", "URDU")

# Din ke naam ka stylish font (jaise ᗯᗴᗪᑎᗴՏᗪᗩY)
_FANCY = {
    "A": "ᗩ", "B": "ᗷ", "C": "ᑕ", "D": "ᗪ", "E": "ᗴ", "F": "ᖴ", "G": "Ǥ", "H": "ᕼ",
    "I": "I", "J": "ᒍ", "K": "K", "L": "ᒪ", "M": "ᗰ", "N": "ᑎ", "O": "O", "P": "ᑭ",
    "Q": "ᑫ", "R": "ᖇ", "S": "Տ", "T": "T", "U": "ᑌ", "V": "ᐯ", "W": "ᗯ", "X": "᙭",
    "Y": "Y", "Z": "ᘔ",
}


def fancy_day(name: str) -> str:
    return "".join(_FANCY.get(c, c) for c in name.upper())


def _time12(t) -> str:
    """'7:40 PM' -> '07:40 PM' (do-digit hour)."""
    m = re.match(r"(\d{1,2}):(\d{2})\s*([AP]M)", t or "", re.IGNORECASE)
    return f"{int(m.group(1)):02d}:{m.group(2)} {m.group(3).upper()}" if m else (t or "TBA")


def season_of(it) -> int:
    """Season number — site ke card se; na ho to title se ('Season 2', '2nd Season', 'Part 3'); phir bhi na mile to 1."""
    if it.get("season"):
        return int(it["season"])
    t = it.get("title") or ""
    for pat in (r"\bseason\s*(\d{1,2})\b", r"\b(\d{1,2})(?:st|nd|rd|th)\s+season\b", r"\bS(\d{1,2})\b"):
        m = re.search(pat, t, re.IGNORECASE)
        if m and 1 <= int(m.group(1)) <= 40:
            return int(m.group(1))
    return 1


def _ep_code(it) -> str:
    """'S04 E13' / 'S01 E361–362' / 'Upcoming Dub'. Season HAMESHA dikhta hai (card mein na ho to S01)."""
    if it["next"] is None:
        return "Upcoming Dub" if it["upcoming"] else "TBA"
    def f(n):
        return f"{n:02d}" if n < 100 else str(n)
    ep = f"E{f(it['next'])}"
    if it["next_end"] and it["next_end"] != it["next"]:
        ep += f"–{f(it['next_end'])}"
    elif it["per_release"] and it["per_release"] > 1:
        ep += "+"
    return f"S{season_of(it):02d} {ep}"


def _lang_tags(langs) -> str:
    tags = [f"#{l.capitalize()}" for l in langs if l in _INDIAN_LANGS]
    return " ".join(tags) if tags else "#Hindi"


def _is_today(it, today) -> bool:
    return it["next_date"] is None or it["next_date"] == today


def build_posts(day_idx: int, now: datetime, items: list, footer: str = "") -> list:
    """
    Items (Hindi, sorted) -> 1+ HTML messages (har ek <= MAX_MSG_LEN).

    Style (poora BOLD):
        ⟣──⟢ / DIN KA NAAM / ⟣──⟢          (banner)
        <1 khali line>
        ⫷ Title ⫸ + ┃ box                 (har anime; beech mein 1 khali line)
        <khali line nahi>
        ❝ HINDI ANIME SCHEDULE / date ❞    (QUOTE block)
        <khali line nahi>
        ⟣━━━━━━━━━━━━━━━━━━━━⟢
    Sirf wo anime jinka agla release AAJ hai (site ke countdown ke hisaab se).
    """
    E = lambda s: _html.escape(str(s), quote=False)
    B = lambda t: f"<b>{t}</b>"
    today = now.date()
    todays = [i for i in items if _is_today(i, today)]

    name = fancy_day(DAY_FULL[day_idx])
    pad = " " * max(0, int(20 * (20 - 1.3 * len(name)) / (20 - 1.3 * 9)))
    line = "⟣────────────────────⟢"
    banner = f"{line}\n{pad}{name}\n{line}"

    quote = (
        "<blockquote>"
        + B("𝐇𝐈𝐍𝐃𝐈 𝐀𝐍𝐈𝐌𝐄 𝐒𝐂𝐇𝐄𝐃𝐔𝐋𝐄\n" + f"{DAY_FULL[day_idx]} • {now.strftime('%d %b %Y')}")
        + "</blockquote>"
    )
    closing = quote + "\n" + B("⟣━━━━━━━━━━━━━━━━━━━━⟢")
    if footer:
        closing += "\n\n" + B(f"💜 Powered By : {E(footer)}")

    if not todays:
        return [B(banner + "\n\n😴 Aaj koi Hindi anime nahi aa raha.") + "\n" + closing]

    blocks = []
    last = len(todays) - 1
    for n, it in enumerate(todays):
        t = it["next_time"] if it["next_date"] == today and it["next_time"] else it["time"]
        end = "╰───────────────────" if n == last else "┗━━━━━━━━━━━━━━━━━"
        blocks.append(
            f"⫷ {E(it['title'])} ⫸\n"
            f"┃ 🎬 Episode: {E(_ep_code(it))}\n"
            f"┃🕗 Time: {E(_time12(t))}\n"
            f"┃ 🔊 {E(_lang_tags(it['langs']))}\n"
            f"{end}"
        )

    # Chunking: pehle chunk mein banner; aakhri mein quote + closing line
    msgs, cur, first = [], "", True
    limit = MAX_MSG_LEN - len(closing) - 20
    for b in blocks:
        head = (banner + "\n\n") if first and not cur else ""
        add = head + b
        if cur and len(cur) + len(add) + 2 > limit:
            msgs.append(B(cur))
            cur, first = "", False
            add = b
        cur = (cur + "\n\n" + add) if cur else (head + b)
        first = False
    msgs.append(B(cur + "\n") + closing)
    return msgs



# ───────────────────────── NEW style (image + clickable titles) ─────────────────────────
TIME_ICON = "✦"             # time se pehle ka icon (clock emoji hata diya) — theme ke hisaab se: ✦ ◈ ❖ ➤ ✧
CAPTION_LIMIT = 1000        # Telegram photo-caption limit 1024 (visible text) — thoda margin

_BOLD_SANS = str.maketrans(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz",
    "𝗔𝗕𝗖𝗗𝗘𝗙𝗚𝗛𝗜𝗝𝗞𝗟𝗠𝗡𝗢𝗣𝗤𝗥𝗦𝗧𝗨𝗩𝗪𝗫𝗬𝗭𝗮𝗯𝗰𝗱𝗲𝗳𝗴𝗵𝗶𝗷𝗸𝗹𝗺𝗻𝗼𝗽𝗾𝗿𝘀𝘁𝘂𝘃𝘄𝘅𝘆𝘇")
_SMALLCAPS = str.maketrans("abcdefghijklmnopqrstuvwxyz", "ᴀʙᴄᴅᴇғɢʜɪᴊᴋʟᴍɴᴏᴘǫʀsᴛᴜᴠᴡxʏᴢ")


def style_of(cfg: dict) -> str:
    return "old" if (cfg or {}).get("style") == "old" else "new"


def footer_on(cfg: dict) -> bool:
    """Powered-By line: NEW style mein default ON, OLD style mein default OFF (toggle se badal sakte ho)."""
    if "footer" in (cfg or {}):
        return bool(cfg["footer"])
    return style_of(cfg) == "new"


def _visible_len(html_text: str) -> int:
    return len(_html.unescape(re.sub(r"<[^>]+>", "", html_text)))


def _title_key(s: str) -> str:
    """Anime naam ko compare karne layak banao (season/part/dub jaise shabd hata ke)."""
    s = (s or "").lower()
    s = re.sub(r"\b(the\s+)?final\s+season\b|\bseason\s*\d+\b|\b\d+(?:st|nd|rd|th)\s+season\b|"
               r"\bpart\s*\d+\b|\bcour\s*\d+\b|\bs\d+\b|\b(?:hindi|dubbed|dub|sub|tv)\b", " ", s)
    return re.sub(r"[^a-z0-9]", "", s)


def _tokens(s: str) -> set:
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", (s or "").lower())
    s = re.sub(r"\b(the\s+)?final\s+season\b|\bseason\s*\d+\b|\b\d+(?:st|nd|rd|th)\s+season\b|"
               r"\bpart\s*\d+\b|\bcour\s*\d+\b|\bs\d+\b|\b(?:hindi|dubbed|dub|sub|tv|the|a|an|of|no|wa|ni|to)\b", " ", s)
    return {w for w in re.findall(r"[a-z0-9]+", s) if w}


def _match_score(title: str, name: str) -> float:
    """0..1 — kitna milta hai. Exact / ek doosre mein shamil / shabdon ka overlap / spelling similarity."""
    a, b = _title_key(title), _title_key(name)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    best = SequenceMatcher(None, a, b).ratio()
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) >= 8 and short in long_ and len(short) / len(long_) >= 0.6:
        best = max(best, 0.93)                      # 'jojosbizarreadventuregoldenwind' vs '... goldenwind2018'
    ta, tb = _tokens(title), _tokens(name)
    if ta and tb:
        inter = len(ta & tb)
        jac = inter / len(ta | tb)
        if jac >= 0.75:
            best = max(best, 0.9 + 0.1 * jac)
        if inter >= 3 and inter / min(len(ta), len(tb)) >= 0.99:   # chhota naam poora bade mein hai
            best = max(best, 0.9)
    # subtitle match: 'Show: Subtitle' — dono taraf subtitle same ho
    sa, sb = (title.split(":", 1) + [""])[:2], (name.split(":", 1) + [""])[:2]
    if sa[1].strip() and sb[1].strip() and _title_key(sa[1]) == _title_key(sb[1]) and len(_title_key(sa[1])) >= 6:
        best = max(best, 0.92)
    return best


def match_link(title: str, pairs: list):
    """pairs = [(anime_name, link)] (/add_anime se add anime). Title match ho to us anime ka channel link, warna None."""
    best, best_link, best_name = 0.0, None, ""
    for name, link in pairs:
        if not link:
            continue
        score = _match_score(title, name)
        if score > best:
            best, best_link, best_name = score, link, name
    if best >= 0.88:
        return best_link
    if best_name:
        LOGGER.info(f"[HindiSchedule] link nahi laga: '{title}' (sabse close: '{best_name}' {best:.2f})")
    else:
        LOGGER.info(f"[HindiSchedule] link nahi laga: '{title}' (add_anime list mein koi naam nahi mila)")
    return None


def _http(link: str) -> str:
    link = (link or "").strip()
    if link.startswith(("http://", "https://")):
        return link
    if link.startswith(("t.me/", "telegram.me/")):
        return "https://" + link
    return ""


async def load_anime_links(link_idx: int = 0) -> list:
    """/add_anime se add har anime ka [(naam, channel join link)].
    link_idx: Multi mode mein kaun sa Link (0 = Link 1 / Channel 01, 1 = Link 2 ...). Multi mode OFF ho to hamesha Link 1.
    Link nahi => list mein nahi => link nahi lagega."""
    from ..utils import multi_mode as _mm
    pairs = []
    try:
        doc = await db.col2.find_one({"id": "update_post_map"})
        for key, v in ((doc or {}).get("map") or {}).items():
            if not isinstance(v, dict):
                continue
            link = _http(_mm.link_for(v, link_idx) or v.get("invite_link") or "")
            if link:
                pairs.append((v.get("display_name") or key, link))
    except Exception as e:
        LOGGER.warning(f"[HindiSchedule] update_post_map read failed: {e}")
    if not link_idx:        # monitor list mein sirf Link 1 saved hota hai
        try:
            if owner:
                user = await db._get_user(owner[0])
                for e in (user.get("anime_monitor_list") or []):
                    link = _http(e.get("channel_link") or "")
                    if e.get("anime_name") and link:
                        pairs.append((e["anime_name"], link))
        except Exception as e:
            LOGGER.warning(f"[HindiSchedule] anime_monitor_list read failed: {e}")
    return pairs


def _channels(cfg: dict) -> list:
    """Schedule channels: [{id, title, link_ch}]. Purani single-channel config bhi chalti hai."""
    chs = cfg.get("channels")
    if isinstance(chs, list) and chs:
        return [dict(c) for c in chs if isinstance(c, dict) and c.get("id")]
    if cfg.get("channel_id"):
        return [{"id": cfg["channel_id"], "title": cfg.get("channel_title") or "Unknown",
                 "link_ch": cfg.get("link_ch")}]
    return []


def _store_channels(cfg: dict, chs: list):
    """channels list save karo; channel_id/channel_title = pehla channel (purane checks ke liye)."""
    for k in ("link_ch", "link_ch_title"):
        cfg.pop(k, None)
    if chs:
        cfg["channels"] = chs
        cfg["channel_id"], cfg["channel_title"] = chs[0]["id"], chs[0]["title"]
    else:
        for k in ("channels", "channel_id", "channel_title", "last_date", "last_try"):
            cfg.pop(k, None)


async def link_info(ch: dict = None) -> dict:
    """Ek schedule channel ke liye anime-links ka Link number kaise tay hoga.
    kind:
      'off'  — Multi mode OFF ya channel nahi => Link 1
      'auto' — ye channel khud ek Update Channel hai => usi channel ka link (AUTOMATIC)
      'pick' — ye channel Update Channels mein NAHI hai => user ko chuna padega (ch['link_ch'])
    """
    out = {"kind": "off", "idx": 0, "label": "Link 1 (default)", "short": "Link 1"}
    try:
        from ..utils.multi_mode import is_multi, ch_link_idx
        if not ch or not await is_multi():
            return out
        from .update_channel import _get_update_channels
        chans = await _get_update_channels()
        for pos, uc in enumerate(chans):
            if uc.get("channel_id") == ch.get("id"):
                idx = ch_link_idx(uc, pos)
                return {"kind": "auto", "idx": idx, "short": f"Link {idx + 1} (auto)",
                        "label": f"Auto → Channel {pos + 1:02d} (Link {idx + 1})"}
        out["kind"] = "pick"                      # unknown channel — chuna hua link_ch dekho
        out.update(short="⚠️ link chuno", label="⚠️ Chuna nahi — abhi Link 1 lagega")
        for pos, uc in enumerate(chans):
            if ch.get("link_ch") and uc.get("channel_id") == ch["link_ch"]:
                idx = ch_link_idx(uc, pos)
                out.update(idx=idx, short=f"Link {idx + 1}",
                           label=f"Channel {pos + 1:02d} (Link {idx + 1}) — tumne chuna")
                return out
    except Exception as e:
        LOGGER.warning(f"[HindiSchedule] link channel resolve failed: {e}")
    return out


def _ep_code_new(it) -> str:
    """'S06 • E07' / 'S04 • E05—E08' / 'E361' (season na ho) / 'Upcoming Dub'  (+ pehla episode => [ɴᴇᴡ])"""
    if it["next"] is None:
        return "Upcoming Dub" if it["upcoming"] else "TBA"

    def f(n):
        return f"{n:02d}" if n < 100 else str(n)
    ep = f"E{f(it['next'])}"
    if it["next_end"] and it["next_end"] != it["next"]:
        ep += f"—E{f(it['next_end'])}"
    elif it["per_release"] and it["per_release"] > 1:
        ep += "+"
    return f"S{season_of(it):02d} • {ep}"


def _item_time(it, today) -> str:
    t = it["next_time"] if it["next_date"] == today and it["next_time"] else it["time"]
    return _time12(t)


def schedule_rows(items: list, now: datetime) -> list:
    """Aaj ke anime -> [{title, ep, time, new}] (image + caption dono isse bante hain)."""
    today = now.date()
    out = []
    for it in items:
        if not _is_today(it, today):
            continue
        out.append({"title": it["title"], "ep": _ep_code_new(it), "time": _item_time(it, today),
                    "new": it["next"] == 1})
    return out


def build_new_posts(day_idx: int, now: datetime, rows: list, links: list, handles: str = "") -> list:
    """
    NEW style caption(s) (HTML). msgs[0] image ke saath caption banta hai (<= CAPTION_LIMIT), baaki text msgs.

        ⟣──────────⟢ / 𝗙𝗥𝗜𝗗𝗔𝗬 / ⟣──────────⟢          (sab kuch BOLD)
        ‣ <a href=channel-link>Anime</a>          ← link SIRF /add_anime wale anime pe
          ↳ S06 • E07 [𝗡𝗘𝗪]  ✦ 07:40 PM          (season hamesha; clock emoji nahi)
        ⟣────────⟢
        ❝ ᴘᴏᴡᴇʀᴇᴅ ʙʏ : @A | @B ❞
    """
    E = lambda s: _html.escape(str(s), quote=False)
    A = lambda s: _html.escape(str(s), quote=True)
    line = "⟣────────────────────⟢"
    name = DAY_FULL[day_idx].upper().translate(_BOLD_SANS)
    pad = " " * max(0, int(20 * (20 - 1.3 * len(name)) / (20 - 1.3 * 9)))
    banner = f"<b>{line}\n{pad}{name}\n{line}</b>"
    closing = "<b>⟣────────────⟢</b>"
    if handles:
        closing += "\n<blockquote><b>" + "ᴘᴏᴡᴇʀᴇᴅ ʙʏ".translate(_SMALLCAPS) + f" : {E(handles)}</b></blockquote>"

    if not rows:
        return [banner + "\n\n<b>😴 Aaj koi Hindi anime nahi aa raha.</b>\n" + closing]

    blocks = []
    for r in rows:
        link = match_link(r["title"], links)
        title = f'<a href="{A(link)}">{E(r["title"])}</a>' if link else E(r["title"])
        tag = " [𝗡𝗘𝗪]" if r["new"] else ""
        # poora block BOLD (link wala naam bhi)
        blocks.append(f"<b>‣ {title}\n  ↳ {E(r['ep'])}{tag}  {TIME_ICON} {E(r['time'])}</b>")

    msgs, cur = [], banner
    limit_first = CAPTION_LIMIT - _visible_len(closing) - 4
    for b in blocks:
        lim = limit_first if not msgs else MAX_MSG_LEN - len(closing) - 20
        if cur != banner and _visible_len(cur) + _visible_len(b) + 2 > lim and cur.strip():
            msgs.append(cur)
            cur = ""
        cur = (cur + ("\n" if cur == banner else "\n\n") + b) if cur else b
    msgs.append(cur + "\n" + closing)
    return msgs


# ───────────────────────── posting ─────────────────────────
async def _owner_dm(client: Client, text: str):
    for oid in dict.fromkeys(list(owner)):
        try:
            await client.send_message(oid, text, parse_mode=ParseMode.DISABLED)
        except Exception:
            pass


async def _compose_new(cfg: dict, day_idx: int, now: datetime, items: list, link_idx: int = 0, make_image: bool = True):
    """NEW style ka poora content banao: (msgs, img_path, rows). Post aur edit (recheck) dono yahin se."""
    from ..utils.community import get_community_tag
    try:
        handles_list = await get_community_tag(all_names=True)          # '@A @B' — community mein jitne set hain
    except Exception:
        handles_list = ""
    branding = footer_on(cfg)           # Branding OFF => na link, na community (image + text dono mein)
    if not branding:
        handles_list = ""
    handles = " | ".join(handles_list.split())
    rows = schedule_rows(items, now)
    links = await load_anime_links(link_idx) if branding else []
    msgs = build_new_posts(day_idx, now, rows, links, handles)

    img_path = None
    if make_image and rows and cfg.get("image", True):
        try:
            from ..utils.poster_engine import schedule_poster
            fd, img_path = tempfile.mkstemp(prefix="hs_img_", suffix=".jpg")
            os.close(fd)
            date_str = now.strftime("%d %b %Y")
            ok = await schedule_poster.render_schedule_to_file(
                rows, DAY_FULL[day_idx], date_str, handles_list, img_path)
            if not ok:
                img_path = None
        except Exception as e:
            LOGGER.warning(f"[HindiSchedule] schedule image fail (text post hoga): {e}")
            img_path = None
    return msgs, img_path, rows


async def _post_new_style(client: Client, ch, cfg: dict, day_idx: int, now: datetime, items: list,
                          link_idx: int = 0) -> list:
    """NEW style: stylish image + caption (clickable anime names). Image fail/OFF => sirf text post.
    Return: bheje gaye messages [{"id", "photo"}] (3-ghante wale recheck mein edit karne ke liye)."""
    msgs, img_path, _rows = await _compose_new(cfg, day_idx, now, items, link_idx)
    sent = []
    try:
        for i, text in enumerate(msgs):
            if i == 0 and img_path:
                m = await client.send_photo(ch, img_path, caption=text, parse_mode=ParseMode.HTML)
                sent.append({"id": m.id, "photo": True})
            else:
                m = await client.send_message(ch, text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
                sent.append({"id": m.id, "photo": False})
            if i < len(msgs) - 1:
                await asyncio.sleep(2)
    finally:
        if img_path:
            try:
                os.remove(img_path)
            except OSError:
                pass
    return sent


async def _footer_old(cfg: dict) -> str:
    if style_of(cfg) == "new" or not footer_on(cfg):
        return ""
    try:
        from ..utils.community import get_community_tag
        from ..utils.caption_style import _handles_brackets
        return _handles_brackets(await get_community_tag(all_names=True), spaced=True)
    except Exception:
        return ""


def items_sig(items: list, now: datetime) -> list:
    """Post mein jo dikhta hai uska signature (title, episode, time) — recheck mein change pakadne ke liye."""
    return sorted(f"{r['title'].strip().lower()}|{r['ep']}|{r['time']}" for r in schedule_rows(items, now))


async def collect_items(day_idx: int, now: datetime):
    """
    Dono sources se aaj ka final anime list.  Return (items, report_text, error)
      1) hindianimeszone (Selenium) -> Hindi + NON-complete
      2) (sirf aaj ke din) deadtoons.sbs/schedule -> match / naye add / complete wale skip
    """
    from ..utils import job_ctl
    try:
        data = await job_ctl.run_in_thread(_scrape_sync, day_idx)
    except Exception as e:
        LOGGER.error(f"[HindiSchedule] scrape failed: {e}")
        return None, "", f"Page load/scrape fail: {type(e).__name__}: {e}"
    if not data["cards"]:
        return None, "", ("Page par koi anime card nahi mila (page badal gaya ya load nahi hua). "
                          "/hindi_schedule_debug se raw text bhejo.")
    if not data["clicked"]:
        LOGGER.warning(f"[HindiSchedule] day tab '{DAYS[day_idx]}' click nahi hua — page ka default view use hua")

    parsed = [parse_card(c) for c in data["cards"]]
    items = filter_hindi(parsed)
    done_names = [p["title"] for p in parsed if p.get("complete")]
    report = ""
    if done_names:
        report += "🚫 Complete (hata diye): " + ", ".join(done_names[:6]) + "\n"

    if CROSSCHECK and day_idx == now.weekday():
        try:
            from . import schedule_crosscheck as _cx
            b = await asyncio.wait_for(job_ctl.run_in_thread(_cx.scrape_deadtoons_sync), 90)
            items, rep = _cx.merge_items(parsed, items, b.get("items") or [], now.date(), _match_score)
            if not b.get("ok") and b.get("error"):
                LOGGER.warning(f"[HindiSchedule] deadtoons: {b['error']}")
            report += _cx.report_text(rep)
            items = sorted(items, key=lambda i: (_time_key(i.get("next_time") if i.get("next_date") == now.date() and i.get("next_time") else i["time"]),
                                                 i["title"].lower()))
        except Exception as e:
            LOGGER.warning(f"[HindiSchedule] cross-check fail (sirf A use hua): {e}")
            report += f"🔎 Double-check fail ({type(e).__name__}) — sirf hindianimeszone use hua."
    return items, report.strip(), None


async def post_schedule(client: Client, day_idx: int = None, manual: bool = False):
    """Schedule scrape karke channel mein post karo. Returns (ok, info_text)."""
    global BUSY
    async with _LOCK:
        BUSY = True
        try:
            cfg = await _cfg()
            chs = _channels(cfg)
            if not chs:
                return False, "Schedule channel set nahi hai. /add_hindi_anime_schedule <channel_id>"
            now = datetime.now(IST)
            if day_idx is None:
                day_idx = now.weekday()

            items, report, err = await collect_items(day_idx, now)
            if err:
                return False, err

            footer = await _footer_old(cfg)
            ok_n, fails, rec_channels = 0, [], {}
            for c in chs:                       # har channel mein post — apne link ke saath
                ch = c["id"]
                try:
                    li = await link_info(c)
                    if style_of(cfg) == "new":
                        sent = await _post_new_style(client, ch, cfg, day_idx, now, items, li["idx"])
                    else:
                        sent = []
                        msgs = build_posts(day_idx, now, items, footer)
                        for i, text in enumerate(msgs):
                            m = await client.send_message(ch, text, parse_mode=ParseMode.HTML,
                                                          disable_web_page_preview=True)
                            sent.append({"id": m.id, "photo": False})
                            if i < len(msgs) - 1:
                                await asyncio.sleep(2)
                    rec_channels[str(ch)] = {"msgs": sent, "link_idx": li["idx"]}
                    ok_n += 1
                    LOGGER.info(f"[HindiSchedule] posted {len(items)} anime ({DAYS[day_idx]}) to {ch}")
                except Exception as e:
                    LOGGER.error(f"[HindiSchedule] post to {ch} failed: {e}")
                    fails.append(f"{c.get('title') or ch}: {type(e).__name__}: {e}")
                if len(chs) > 1:
                    await asyncio.sleep(2)
            if not ok_n:
                return False, "Post fail: " + " | ".join(fails)

            cfg = await _cfg()
            if not manual:
                cfg["last_date"] = now.date().isoformat()
            # 3 ghante baad dobara check (sirf aaj ke din ki post ke liye)
            if RECHECK_HOURS > 0 and day_idx == now.weekday():
                cfg["recheck"] = {"date": now.date().isoformat(), "day_idx": day_idx, "tries": 0,
                                  "due": (now + timedelta(hours=RECHECK_HOURS)).replace(tzinfo=None).isoformat(),
                                  "sig": items_sig(items, now), "channels": rec_channels}
            await _save(cfg)
            msg = f"{len(items)} Hindi anime ka {DAY_FULL[day_idx]} schedule {ok_n}/{len(chs)} channel mein post ho gaya."
            if fails:
                msg += "\n⚠️ Fail: " + " | ".join(fails)
            if report:
                msg += "\n\n" + report
            if cfg.get("recheck"):
                msg += f"\n⏱ {RECHECK_HOURS:g} ghante baad dobara check hoga (kuch badla to post edit ho jayegi)."
            return True, msg
        except Exception as e:
            LOGGER.error(f"[HindiSchedule] post failed: {e}")
            return False, f"Post fail: {type(e).__name__}: {e}"
        finally:
            BUSY = False


# ───────────────────────── 3-ghante baad recheck + post edit ─────────────────────────
async def _edit_channel_posts(client: Client, ch: int, rec: dict, cfg: dict, day_idx: int, now: datetime, items: list):
    """Pehle se bheji post(s) ko naye list ke hisaab se edit karo. Message kam/zyada ho to delete / naya bhejo."""
    old = list(rec.get("msgs") or [])
    link_idx = rec.get("link_idx", 0)
    if style_of(cfg) == "new":
        msgs, img_path, rows = await _compose_new(cfg, day_idx, now, items, link_idx)
    else:
        msgs, img_path, rows = build_posts(day_idx, now, items, await _footer_old(cfg)), None, []
    new_sent = []
    try:
        for i, text in enumerate(msgs):
            if i < len(old):
                mid, is_photo = old[i]["id"], old[i].get("photo", False)
                if is_photo:
                    done = False
                    if img_path:                                   # list badli => image bhi naya
                        try:
                            await client.edit_message_media(
                                ch, mid, InputMediaPhoto(img_path, caption=text, parse_mode=ParseMode.HTML))
                            done = True
                        except Exception as e:
                            if "MESSAGE_NOT_MODIFIED" in str(e).upper():
                                done = True
                            else:
                                LOGGER.warning(f"[HindiSchedule] edit media fail ({e}) — caption edit try")
                    if not done:
                        try:
                            await client.edit_message_caption(ch, mid, text, parse_mode=ParseMode.HTML)
                        except Exception as e:
                            if "MESSAGE_NOT_MODIFIED" not in str(e).upper():
                                raise
                    new_sent.append({"id": mid, "photo": True})
                else:
                    try:
                        await client.edit_message_text(ch, mid, text, parse_mode=ParseMode.HTML,
                                                       disable_web_page_preview=True)
                    except Exception as e:
                        if "MESSAGE_NOT_MODIFIED" not in str(e).upper():
                            raise
                    new_sent.append({"id": mid, "photo": False})
            else:
                m = await client.send_message(ch, text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
                new_sent.append({"id": m.id, "photo": False})
            await asyncio.sleep(1)
        extra = [o["id"] for o in old[len(msgs):]]
        if extra:
            try:
                await client.delete_messages(ch, extra)
            except Exception as e:
                LOGGER.warning(f"[HindiSchedule] extra msg delete fail: {e}")
    finally:
        if img_path:
            try:
                os.remove(img_path)
            except OSError:
                pass
    rec["msgs"] = new_sent


def _sig_diff(old: list, new: list) -> str:
    def titles(sig):
        return {x.split("|")[0]: x for x in sig}
    o, n = titles(old), titles(new)
    out = []
    for t in n:
        if t not in o:
            out.append(f"➕ {t.title()} ({n[t].split('|')[1]}, {n[t].split('|')[2]})")
    for t in o:
        if t not in n:
            out.append(f"➖ {t.title()}")
    for t in n:
        if t in o and o[t] != n[t]:
            out.append(f"✏️ {t.title()}: {o[t].split('|', 1)[1]} → {n[t].split('|', 1)[1]}")
    return "\n".join(out) or "(chhota badlav)"


async def maybe_recheck(client: Client):
    """Loop se har ~45s: post ke RECHECK_HOURS baad dobara scrape (dono sources) — badla ho to post edit."""
    cfg = await _cfg()
    rc = cfg.get("recheck")
    if not rc:
        return
    now = datetime.now(IST)
    if rc.get("date") != now.date().isoformat():                # din badal gaya — purani post ko ab chhedna nahi
        cfg.pop("recheck", None)
        await _save(cfg)
        return
    try:
        due = datetime.fromisoformat(rc["due"]).replace(tzinfo=IST)
    except Exception:
        cfg.pop("recheck", None)
        await _save(cfg)
        return
    if now < due:
        return
    global BUSY
    async with _LOCK:
        BUSY = True
        try:
            day_idx = rc.get("day_idx", now.weekday())
            items, report, err = await collect_items(day_idx, now)
            cfg = await _cfg()
            rc = cfg.get("recheck") or rc
            if err:
                rc["tries"] = rc.get("tries", 0) + 1
                if rc["tries"] >= 3:
                    cfg.pop("recheck", None)
                    await _owner_dm(client, f"⚠️ Hindi schedule recheck 3 baar fail hua:\n{err}")
                else:
                    rc["due"] = (now + timedelta(minutes=RECHECK_RETRY_MIN)).replace(tzinfo=None).isoformat()
                    cfg["recheck"] = rc
                await _save(cfg)
                return
            new_sig = items_sig(items, now)
            if new_sig == rc.get("sig"):
                LOGGER.info("[HindiSchedule] recheck: koi badlav nahi")
                cfg.pop("recheck", None)
                await _save(cfg)
                return
            diff = _sig_diff(rc.get("sig") or [], new_sig)
            ok_n, fails = 0, []
            for chid, rec in (rc.get("channels") or {}).items():
                try:
                    await _edit_channel_posts(client, int(chid), rec, cfg, day_idx, now, items)
                    ok_n += 1
                except Exception as e:
                    LOGGER.error(f"[HindiSchedule] recheck edit {chid} fail: {e}")
                    fails.append(f"{chid}: {type(e).__name__}: {e}")
            cfg.pop("recheck", None)
            await _save(cfg)
            txt = f"✏️ Hindi schedule update hua ({RECHECK_HOURS:g}h recheck) — {ok_n} channel mein post edit:\n{diff}"
            if report:
                txt += "\n\n" + report
            if fails:
                txt += "\n⚠️ Edit fail: " + " | ".join(fails)
            await _owner_dm(client, txt)
        except Exception as e:
            LOGGER.error(f"[HindiSchedule] recheck error: {e}")
        finally:
            BUSY = False


async def schedule_loop():
    """Roz set time (IST) pe auto-post. janitor.start_janitor() se start hota hai."""
    from .. import app
    await asyncio.sleep(90)
    while True:
        try:
            cfg = await _cfg()
            now = datetime.now(IST)
            if should_post(now, cfg):
                cfg["last_try"] = now.replace(tzinfo=None).isoformat()
                await _save(cfg)
                ok, info = await post_schedule(app, manual=False)
                if not ok:
                    await _owner_dm(app, f"⚠️ Hindi schedule auto-post fail:\n{info}\n\n"
                                         f"{RETRY_GAP_MIN} min baad dobara try hoga.")
            else:
                await maybe_recheck(app)
        except Exception as e:
            LOGGER.error(f"[HindiSchedule] loop error: {e}")
        await asyncio.sleep(45)


# ───────────────────────── channel validation ─────────────────────────
async def _validate_channel(client: Client, channel_id: int):
    """(title, error|None) — bot admin + Post Messages check."""
    try:
        chat = await client.get_chat(channel_id)
        title = chat.title
    except Exception as e:
        return None, f"Channel nahi mila: {e}"
    try:
        me = await client.get_me()
        member = await client.get_chat_member(channel_id, me.id)
        if member.status.name not in ("ADMINISTRATOR", "OWNER"):
            return None, f"Bot {title} mein admin nahi hai!"
        from .update_channel import _can_post
        if not _can_post(member):
            return None, f"Bot {title} mein admin hai, lekin 'Post Messages' permission nahi hai!"
    except Exception as e:
        return None, f"Admin check fail: {e}"
    return title, None


async def _set_channel(client: Client, channel_id: int):
    """Channel ADD karta hai (pehle wale hatte nahi). Already ho to title refresh."""
    title, err = await _validate_channel(client, channel_id)
    if err:
        return None, err
    cfg = await _cfg()
    chs = _channels(cfg)
    for c in chs:
        if c["id"] == channel_id:
            c["title"] = title
            break
    else:
        chs.append({"id": channel_id, "title": title})
    _store_channels(cfg, chs)
    cfg.setdefault("enabled", True)
    cfg.setdefault("time", DEFAULT_TIME)
    await _save(cfg)
    return title, None


# ───────────────────────── commands ─────────────────────────
@Client.on_message(filters.command("add_hindi_anime_schedule") & filters.private)
async def cmd_add_channel(client: Client, message: Message):
    if not _is_auth(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) < 2:
        await message.reply(
            "📅 **Hindi Anime Schedule Channel**\n\n"
            "Usage: `/add_hindi_anime_schedule -1001234567890`\n\n"
            "Roz set time par aaj ka Hindi anime schedule is channel mein jayega. "
            "Time badalne ke liye `/hindi_schedule_time 8:30 PM`."
        )
        return
    try:
        cid = int(parts[1])
    except ValueError:
        await message.reply("❌ Valid channel ID do. Format: `-100xxxxxxxxxx`")
        return
    title, err = await _set_channel(client, cid)
    if err:
        await message.reply(f"❌ {err}")
        return
    cfg = await _cfg()
    h, m = _hhmm(cfg.get("time"))
    await message.reply(
        f"✅ **Hindi Schedule channel set!**\n\n📢 **{title}**\n🆔 `{cid}`\n"
        f"⏰ Roz **{fmt12(h, m)} IST** par post hoga\n\n"
        "• Time: `/hindi_schedule_time 9:00 AM`\n• Abhi test: `/hindi_schedule_now`\n"
        "• Sab buttons se: `/customise` → Cleanup & Schedule"
    )


@Client.on_message(filters.command("hindi_schedule_time") & filters.private)
async def cmd_time(client: Client, message: Message):
    if not _is_auth(message.from_user.id):
        return
    parts = message.text.split(None, 1)
    if len(parts) < 2 or not parse_time(parts[1]):
        await message.reply("Usage: `/hindi_schedule_time 08:00` ya `/hindi_schedule_time 8:30 PM` (IST)")
        return
    h, m = parse_time(parts[1])
    cfg = await _cfg()
    cfg["time"] = f"{h:02d}:{m:02d}"
    cfg.pop("last_try", None)
    await _save(cfg)
    await message.reply(f"✅ Roz **{fmt12(h, m)} IST** par Hindi schedule post hoga.")


@Client.on_message(filters.command("hindi_schedule") & filters.private)
async def cmd_status(client: Client, message: Message):
    if not _is_auth(message.from_user.id):
        return
    parts = message.text.split()
    cfg = await _cfg()
    if len(parts) > 1 and parts[1].lower() in ("on", "off"):
        cfg["enabled"] = parts[1].lower() == "on"
        await _save(cfg)
        await message.reply("🟢 Hindi schedule auto-post **ON**" if cfg["enabled"] else "🔴 Hindi schedule auto-post **OFF**")
        return
    await message.reply(_status_text(cfg, await _labels(cfg)) + "\n\nCommands: `/hindi_schedule on|off`, `/hindi_schedule_time`, "
                        "`/hindi_schedule_now`, `/hindi_schedule_style`, `/add_hindi_anime_schedule`")


@Client.on_message(filters.command("hindi_schedule_style") & filters.private)
async def cmd_style(client: Client, message: Message):
    if not _is_auth(message.from_user.id):
        return
    parts = message.text.split()
    cfg = await _cfg()
    if len(parts) < 2 or parts[1].lower() not in ("new", "old"):
        await message.reply(
            f"🎨 Abhi style: **{style_of(cfg).upper()}**\n\n"
            "`/hindi_schedule_style new` — image + clickable anime links\n"
            "`/hindi_schedule_style old` — purana list style")
        return
    cfg["style"] = parts[1].lower()
    await _save(cfg)
    await message.reply("✨ **NEW** style set — image + anime links." if cfg["style"] == "new"
                        else "📜 **OLD** style set — purana list.")


@Client.on_message(filters.command("hindi_schedule_now") & filters.private)
async def cmd_now(client: Client, message: Message):
    if not _is_auth(message.from_user.id):
        return
    parts = message.text.split()
    day_idx = None
    if len(parts) > 1:
        key = parts[1][:3].capitalize()
        if key not in DAYS:
            await message.reply("Din galat. Example: `/hindi_schedule_now wed`")
            return
        day_idx = DAYS.index(key)
    wait = await message.reply("⏳ Schedule page load ho raha hai (~30-60 sec)...")
    ok, info = await post_schedule(client, day_idx, manual=True)
    await wait.edit(("✅ " if ok else "❌ ") + info)


@Client.on_message(filters.command("hindi_schedule_debug") & filters.private)
async def cmd_debug(client: Client, message: Message):
    if not _is_auth(message.from_user.id):
        return
    await _send_debug(client, message.chat.id)


@Client.on_message(filters.command("hindi_schedule_check") & filters.private)
async def cmd_check(client: Client, message: Message):
    """Dono sources (hindianimeszone + deadtoons) ka aaj ka comparison — post kiye bina. B ka raw text file mein."""
    if not _is_auth(message.from_user.id):
        return
    wait = await message.reply("⏳ Dono sites check ho rahi hain (~1-2 min)...")
    try:
        from ..utils import job_ctl
        from . import schedule_crosscheck as _cx
        now = datetime.now(IST)
        day_idx = now.weekday()
        data = await job_ctl.run_in_thread(_scrape_sync, day_idx)
        parsed = [parse_card(c) for c in data["cards"]]
        a_items = filter_hindi(parsed)
        b = await job_ctl.run_in_thread(_cx.scrape_deadtoons_sync)
        merged, rep = _cx.merge_items(parsed, a_items, b.get("items") or [], now.date(), _match_score)
        lines = [f"📅 {DAY_FULL[day_idx]} {now.date()}",
                 f"A hindianimeszone: {len(parsed)} card • Hindi non-complete: {len(a_items)} • complete: "
                 f"{sum(1 for p in parsed if p.get('complete'))}",
                 f"B deadtoons: {'OK' if b.get('ok') else 'FAIL'} • {len(b.get('items') or [])} entries"
                 + (f" • {b.get('error')}" if b.get("error") else ""), "",
                 "A (final base):"]
        lines += [f"  • {i['title']} — {_ep_code_new(i)} {_item_time(i, now.date())}" for i in a_items] or ["  (khali)"]
        lines += ["", "B entries:"]
        lines += [f"  • {e['title']} — S{e['season']}E{e['ep']} {e['time']}{' [kata]' if e['truncated'] else ''}"
                  for e in (b.get("items") or [])] or ["  (khali)"]
        lines += ["", "FINAL (post mein ye jayega):"]
        lines += [f"  • {i['title']} — {_ep_code_new(i)} {_item_time(i, now.date())}{'  [B se]' if i.get('src') == 'B' else ''}"
                  for i in merged if _is_today(i, now.date())] or ["  (khali)"]
        lines += ["", _cx.report_text(rep)]
        await wait.edit("\n".join(lines)[:4000], parse_mode=ParseMode.DISABLED)
        buf = io.BytesIO((b.get("body") or "(khali)").encode("utf-8"))
        buf.name = "deadtoons_raw.txt"
        await client.send_document(message.chat.id, buf, caption="🧪 deadtoons raw page text (parser tune karne ke liye)")
    except Exception as e:
        await wait.edit(f"❌ Check fail: {type(e).__name__}: {e}", parse_mode=ParseMode.DISABLED)


async def _send_debug(client: Client, chat_id: int):
    wait = await client.send_message(chat_id, "⏳ Debug data nikal raha hoon...")
    try:
        from ..utils import job_ctl
        day_idx = datetime.now(IST).weekday()
        data = await job_ctl.run_in_thread(_scrape_sync, day_idx)
    except Exception as e:
        await wait.edit(f"❌ Scrape fail: {type(e).__name__}: {e}")
        return
    parts = [f"clicked_day_tab: {data['clicked']}  (day={DAYS[day_idx]})",
             f"cards: {len(data['cards'])}  (page ke hisaab se scheduled: {data.get('expected')})", ""]
    for i, c in enumerate(data["cards"], 1):
        parts += [f"===== CARD {i} =====", c, f"--> parsed: { {k: v for k, v in parse_card(c).items() if k != 'raw'} }", ""]
    parts += ["===== RESOURCES (network) =====", *data["resources"][:80], "", "===== BODY TEXT =====", data["body"][:6000]]
    buf = io.BytesIO("\n".join(parts).encode("utf-8"))
    buf.name = "hindi_schedule_debug.txt"
    await client.send_document(chat_id, buf, caption="🧪 Hindi schedule debug")
    await wait.delete()


def _status_text(cfg: dict, labels: dict = None) -> str:
    h, m = _hhmm(cfg.get("time"))
    labels = labels or {}
    chs = _channels(cfg)
    if chs:
        ch = "\n".join(f"  {i + 1}. **{c['title']}** — `{c['id']}`\n      🔗 {labels.get(c['id'], 'Link 1 (default)')}"
                       for i, c in enumerate(chs))
        ch = f"({len(chs)})\n{ch}"
    else:
        ch = "❌ Set nahi hai"
    today = DAY_FULL[datetime.now(IST).weekday()]
    return (
        "📅 **Hindi Anime Schedule**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📢 Channels: {ch}\n"
        f"⏰ Roz post: **{fmt12(h, m)} IST**\n"
        f"🔔 Auto-post: **{'🟢 ON' if cfg.get('enabled', True) else '🔴 OFF'}**\n"
        f"🎨 Style: **{'✨ NEW (image + links)' if style_of(cfg) == 'new' else '📜 OLD (classic list)'}**\n"
        f"🖼 Image: **{'🟢 ON' if cfg.get('image', True) else '🔴 OFF'}** _(NEW style)_\n"
        f"💜 Branding (links + community): **{'🟢 ON' if footer_on(cfg) else '🔴 OFF'}**\n"
        f"🗓 Aaj: {today}\n"
        f"📤 Last post: {cfg.get('last_date') or '—'}"
    )


async def _labels(cfg: dict) -> dict:
    return {c["id"]: (await link_info(c))["label"] for c in _channels(cfg)}


# ───────────────────────── /customise panel ─────────────────────────
def _back(label="⬅️ Schedule"):
    return [InlineKeyboardButton(label, callback_data="cmz:open:hindi_schedule"),
            InlineKeyboardButton("🏠 Menu", callback_data="cmz:menu")]


async def _edit(client, cb, text, kb):
    from .customise import _safe_edit
    await _safe_edit(client, cb, text, kb)


async def render(client: Client, cb: CallbackQuery):
    from .customise import _CUR_SECTION, _back_row
    _CUR_SECTION.set("hindi_schedule")
    clear_session(cb.from_user.id)
    cfg = await _cfg()
    on = cfg.get("enabled", True)
    chs = _channels(cfg)
    rows = []
    for i, c in enumerate(chs):          # har channel ek button — tap karke link / hatao
        li = await link_info(c)
        rows.append([InlineKeyboardButton(f"📢 {i + 1}. {(c['title'] or 'Channel')[:22]} · {li['short']}",
                                          callback_data=f"cmz:hs:c:{i}")])
    rows += [
        [InlineKeyboardButton("➕ Channel add karo", callback_data="cmz:hs:chan"),
         InlineKeyboardButton("⏰ Post Time", callback_data="cmz:hs:time")],
        [InlineKeyboardButton("🟢 Auto-post ON — OFF karo" if on else "🔴 Auto-post OFF — ON karo",
                              callback_data="cmz:hs:tog")],
        [InlineKeyboardButton("🎨 Style: ✨ NEW — OLD karo" if style_of(cfg) == "new"
                              else "🎨 Style: 📜 OLD — NEW karo", callback_data="cmz:hs:style")],
        [InlineKeyboardButton("🖼 Image: ON — OFF karo" if cfg.get("image", True)
                              else "🖼 Image: OFF — ON karo", callback_data="cmz:hs:img")],
        [InlineKeyboardButton("💜 Branding: ON — OFF karo" if footer_on(cfg)
                              else "💜 Branding: OFF — ON karo", callback_data="cmz:hs:foot")],
        [InlineKeyboardButton("📤 Aaj ka schedule abhi post karo", callback_data="cmz:hs:now")],
        [InlineKeyboardButton("🧪 Debug file", callback_data="cmz:hs:dbg")],
    ]
    rows.append(_back_row())
    await _edit(client, cb, _status_text(cfg, await _labels(cfg)), InlineKeyboardMarkup(rows))


_PRESETS = ["0600", "0700", "0800", "0900", "1000", "1200", "1800", "2100"]


async def handle(client: Client, cb: CallbackQuery, args: list):
    sub = args[0] if args else ""
    uid = cb.from_user.id

    # ── ek schedule channel ka screen ──
    if sub == "c" and len(args) > 1:
        cfg = await _cfg()
        chs = _channels(cfg)
        i = int(args[1]) if args[1].isdigit() else -1
        if not 0 <= i < len(chs):
            await cb.answer("Channel nahi mila", show_alert=True)
            await render(client, cb)
            return
        c = chs[i]
        li = await link_info(c)
        rows = []
        if li["kind"] == "pick":
            rows.append([InlineKeyboardButton("🔗 Link channel chuno", callback_data=f"cmz:hs:lnk:{i}")])
        rows.append([InlineKeyboardButton("🗑 Ye channel hatao", callback_data=f"cmz:hs:rm:{i}")])
        rows.append(_back())
        note = ("Ye channel Update Channels mein hai, isliye link **automatic**."
                if li["kind"] == "auto" else
                "Ye channel Update Channels mein nahi hai — upar button se chuno kis channel ka link lage."
                if li["kind"] == "pick" else "Multi mode OFF — Link 1 lagega.")
        await cb.answer()
        await _edit(client, cb,
                    f"📢 **{c['title']}**\n🆔 `{c['id']}`\n━━━━━━━━━━━━━━━━━━━━\n\n🔗 Anime links: **{li['label']}**\n{note}",
                    InlineKeyboardMarkup(rows))
        return

    if sub == "lnk" and len(args) > 1:
        from ..utils.multi_mode import ch_link_idx
        from .update_channel import _get_update_channels
        await cb.answer()
        cfg = await _cfg()
        chs = _channels(cfg)
        i = int(args[1]) if args[1].isdigit() else -1
        if not 0 <= i < len(chs):
            await render(client, cb)
            return
        c = chs[i]
        li = await link_info(c)
        if li["kind"] != "pick":
            await _edit(client, cb, f"🔗 Link **automatic** hai: {li['label']}.\nKuch chunne ki zarurat nahi.",
                        InlineKeyboardMarkup([_back()]))
            return
        cur = c.get("link_ch")
        rows = [[InlineKeyboardButton(("✅ " if not cur else "") + "Link 1 (default)",
                                      callback_data=f"cmz:hs:lset:{i}:0")]]
        for pos, uc in enumerate(await _get_update_channels()):
            title = (uc.get("channel_title") or "Channel")[:24]
            rows.append([InlineKeyboardButton(
                ("✅ " if cur == uc.get("channel_id") else "") + f"Channel {pos + 1:02d} · {title} (Link {ch_link_idx(uc, pos) + 1})",
                callback_data=f"cmz:hs:lset:{i}:{pos + 1}")])
        rows.append(_back())
        await _edit(client, cb,
                    f"**🔗 {c['title']} — kaunsa link lage?**\n━━━━━━━━━━━━━━━━━━━━\n\n"
                    "Ye channel Update Channels mein nahi hai, isliye chuno ki kis channel ka Watch link lage.",
                    InlineKeyboardMarkup(rows))
        return

    if sub == "lset" and len(args) > 2:
        from .update_channel import _get_update_channels
        cfg = await _cfg()
        chs = _channels(cfg)
        try:
            i, n = int(args[1]), int(args[2])
        except ValueError:
            i, n = -1, 0
        if 0 <= i < len(chs):
            ups = await _get_update_channels()
            if n and 1 <= n <= len(ups):
                chs[i]["link_ch"] = ups[n - 1].get("channel_id")
                await cb.answer(f"✅ Channel {n:02d} ke links")
            else:
                chs[i].pop("link_ch", None)
                await cb.answer("✅ Link 1 (default)")
            _store_channels(cfg, chs)
            await _save(cfg)
        await render(client, cb)
        return

    if sub == "chan":
        _SESS[uid] = {"mode": "channel"}
        await cb.answer()
        await _edit(client, cb,
                    "**📢 Schedule channel add karo**\n━━━━━━━━━━━━━━━━━━━━\n\n"
                    "Channel ka **ID** bhejo, jaise `-1001234567890`\n_(ya us channel ki koi post forward karo)_\n\n"
                    "Bot us channel mein **admin** (Post Messages ke saath) hona chahiye.",
                    InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cmz:open:hindi_schedule")]]))
        return

    if sub == "time":
        await cb.answer()
        cfg = await _cfg()
        h, m = _hhmm(cfg.get("time"))
        rows, row = [], []
        for p in _PRESETS:
            hh, mm = int(p[:2]), int(p[2:])
            row.append(InlineKeyboardButton(("✅ " if (hh, mm) == (h, m) else "") + fmt12(hh, mm),
                                            callback_data=f"cmz:hs:tset:{p}"))
            if len(row) == 2:
                rows.append(row)
                row = []
        rows.append([InlineKeyboardButton("✏️ Custom time", callback_data="cmz:hs:tcustom")])
        rows.append(_back())
        await _edit(client, cb,
                    f"**⏰ Roz kab post ho?** (IST)\n━━━━━━━━━━━━━━━━━━━━\n\nAbhi: **{fmt12(h, m)}**\n\n"
                    "Time choose karo ya Custom dabao.", InlineKeyboardMarkup(rows))
        return

    if sub == "tset" and len(args) > 1:
        t = parse_time(args[1])
        if t:
            cfg = await _cfg()
            cfg["time"] = f"{t[0]:02d}:{t[1]:02d}"
            cfg.pop("last_try", None)
            await _save(cfg)
            await cb.answer(f"✅ {fmt12(*t)} IST")
        await render(client, cb)
        return

    if sub == "tcustom":
        _SESS[uid] = {"mode": "time"}
        await cb.answer()
        await _edit(client, cb,
                    "**✏️ Custom time bhejo** (IST)\n━━━━━━━━━━━━━━━━━━━━\n\n"
                    "Jaise `08:00`, `8:30 PM`, `21:15`",
                    InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cmz:hs:time")]]))
        return

    if sub == "tog":
        cfg = await _cfg()
        cfg["enabled"] = not cfg.get("enabled", True)
        await _save(cfg)
        await cb.answer("🟢 ON" if cfg["enabled"] else "🔴 OFF")
        await render(client, cb)
        return

    if sub == "foot":
        cfg = await _cfg()
        cfg["footer"] = not footer_on(cfg)
        await _save(cfg)
        await cb.answer("💜 Branding ON" if cfg["footer"] else "Branding OFF — links/community nahi lagenge")
        await render(client, cb)
        return

    if sub == "style":
        cfg = await _cfg()
        cfg["style"] = "old" if style_of(cfg) == "new" else "new"
        await _save(cfg)
        await cb.answer("✨ NEW style" if cfg["style"] == "new" else "📜 OLD style")
        await render(client, cb)
        return

    if sub == "img":
        cfg = await _cfg()
        cfg["image"] = not cfg.get("image", True)
        await _save(cfg)
        await cb.answer("🖼 Image ON" if cfg["image"] else "🖼 Image OFF")
        await render(client, cb)
        return

    if sub == "now":
        cfg = await _cfg()
        if not _channels(cfg):
            await cb.answer("Pehle channel set karo.", show_alert=True)
            return
        await cb.answer("⏳ Post ho raha hai (30-60 sec)...", show_alert=True)
        ok, info = await post_schedule(client, None, manual=True)
        try:
            await client.send_message(cb.message.chat.id, ("✅ " if ok else "❌ ") + info)
        except Exception:
            pass
        return

    if sub == "dbg":
        await cb.answer("⏳ Debug data nikal raha hoon...")
        await _send_debug(client, cb.message.chat.id)
        return

    if sub == "rm" and len(args) > 1:
        await cb.answer()
        await _edit(client, cb, "🗑 **Ye schedule channel hata du?**\n\nIs channel mein auto-post band ho jayega (baaki channels chalte rahenge).",
                    InlineKeyboardMarkup([[InlineKeyboardButton("✅ Haan, hatao", callback_data=f"cmz:hs:rmok:{args[1]}"),
                                           InlineKeyboardButton("❌ Nahi", callback_data=f"cmz:hs:c:{args[1]}")]]))
        return

    if sub == "rmok" and len(args) > 1:
        cfg = await _cfg()
        chs = _channels(cfg)
        i = int(args[1]) if args[1].isdigit() else -1
        if 0 <= i < len(chs):
            chs.pop(i)
            _store_channels(cfg, chs)
            await _save(cfg)
        await cb.answer("🗑 Hata diya")
        await render(client, cb)
        return

    await cb.answer()


# group=-3: sirf tab consume karta hai jab user is panel ke input mode mein ho
@Client.on_message((filters.text | filters.forwarded) & filters.private, group=-8)
async def hs_input(client: Client, message: Message):
    if not message.from_user:
        raise ContinuePropagation
    uid = message.from_user.id
    sess = _SESS.get(uid)
    if not sess or not _is_auth(uid):
        raise ContinuePropagation
    raw = (message.text or "").strip()
    if raw.startswith("/"):
        _SESS.pop(uid, None)
        raise ContinuePropagation
    back_kb = InlineKeyboardMarkup([_back()])

    if sess["mode"] == "time":
        t = parse_time(raw)
        if not t:
            await message.reply("⚠️ Time samajh nahi aaya. Jaise `08:00` ya `8:30 PM` bhejo.")
            raise StopPropagation
        cfg = await _cfg()
        cfg["time"] = f"{t[0]:02d}:{t[1]:02d}"
        cfg.pop("last_try", None)
        await _save(cfg)
        _SESS.pop(uid, None)
        await message.reply(f"✅ Roz **{fmt12(*t)} IST** par post hoga.", reply_markup=back_kb)
        raise StopPropagation

    # mode == channel
    cid = None
    fchat = getattr(message, "forward_from_chat", None)
    if fchat is None:
        origin = getattr(message, "forward_origin", None)
        fchat = getattr(origin, "chat", None) if origin is not None else None
    if fchat is not None:
        cid = fchat.id
    else:
        try:
            cid = int(raw)
        except ValueError:
            await message.reply("❌ Valid channel ID bhejo (`-100xxxxxxxxx`) ya channel ki post forward karo.")
            raise StopPropagation
    if cid > 0:
        await message.reply("❌ Channel ID `-100` se shuru hoti hai. Dobara bhejo.")
        raise StopPropagation
    title, err = await _set_channel(client, cid)
    if err:
        await message.reply(f"❌ {err}\n\nSahi karke ID dobara bhejo.")
        raise StopPropagation
    _SESS.pop(uid, None)
    cfg = await _cfg()
    chs = _channels(cfg)
    cur = next((c for c in chs if c["id"] == cid), None)
    li = await link_info(cur)
    extra, rows = f"\n\n🔗 Anime links: **{li['label']}**", []
    if li["kind"] == "pick":
        extra = ("\n\n🔗 Ye channel Update Channels mein nahi hai — "
                 "neeche se chuno kis channel ka link lage 👇")
        rows.append([InlineKeyboardButton("🔗 Link channel chuno", callback_data=f"cmz:hs:lnk:{chs.index(cur)}")])
    rows.append([InlineKeyboardButton("➕ Ek aur channel add karo", callback_data="cmz:hs:chan")])
    rows.append(_back())
    await message.reply(f"✅ **Schedule channel add ho gaya!** (total {len(chs)})\n\n📢 **{title}**\n🆔 `{cid}`{extra}",
                        reply_markup=InlineKeyboardMarkup(rows))
    raise StopPropagation
