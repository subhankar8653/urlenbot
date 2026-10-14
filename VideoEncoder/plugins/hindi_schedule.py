"""
hindi_schedule.py
=================
Hindi anime schedule ko channel mein roz auto-post karo.

  Source : https://hindianimeszone.com/schedule.html   (JavaScript se bana page —
           isliye bot ka headless Chrome use hota hai, aaj ke din (IST) ka tab chunta hai)
  Filter : sirf HINDI dub wale anime
  Post   : professional list (time ke hisaab se sorted) — set kiye channel mein, roz set time pe

Commands (owner/sudo):
  /add_hindi_anime_schedule <channel_id>   — schedule channel set karo
  /hindi_schedule_time <time>              — roz kab post ho (IST), jaise 08:00 / 8:30 PM
  /hindi_schedule [on|off]                 — status / chalu / band
  /hindi_schedule_now [mon..sun]           — abhi post karo (aaj ka, ya koi din)
  /hindi_schedule_debug                    — page ka raw text file (parser tune karne ke liye)

/customise → ⏰ Cleanup & Schedule → 📅 Hindi Anime Schedule  (sab buttons se bhi)

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
from datetime import datetime, timedelta, timezone

from pyrogram import Client, filters, StopPropagation
from pyrogram.enums import ParseMode
from pyrogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
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
               "ONGOING", "WATCH NOW", "STARTING SOON..."}
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
         "next_date": None, "next_time": None, "upcoming": False, "raw": text}
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
    out = [i for i in items if i["title"] and ("HINDI" in i["langs"] or not i["langs"])]
    return sorted(out, key=lambda i: (_time_key(i["time"]), i["title"].lower()))


# ───────────────────────── message format ─────────────────────────
_NUM = ["①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩"]


def _lang_line(langs):
    return " • ".join(l.capitalize() for l in langs) if langs else "Hindi"


def _ep_label(it) -> str:
    if it["next"] is None:
        return ""
    if it["next_end"] and it["next_end"] != it["next"]:
        ep = f"Ep {it['next']}–{it['next_end']}"
    elif it["per_release"] and it["per_release"] > 1:
        ep = f"Ep {it['next']}+ ({it['per_release']} eps)"
    else:
        ep = f"Ep {it['next']}"
    return ep


def _is_today(it, today) -> bool:
    return it["next_date"] is None or it["next_date"] == today


def build_posts(day_idx: int, now: datetime, items: list, footer: str = "") -> list:
    """
    Items (Hindi, sorted) -> 1+ HTML messages (har ek <= MAX_MSG_LEN).
    Jinka agla release aaj hai wo main list mein; baaki (site ke hisaab se aage ki date)
    neeche "Aane wale" section mein date ke saath.
    """
    E = lambda s: _html.escape(str(s), quote=False)
    today = now.date()
    todays = [i for i in items if _is_today(i, today)]
    later = sorted((i for i in items if not _is_today(i, today)),
                   key=lambda i: (i["next_date"], _time_key(i["next_time"] or i["time"])))
    head = (
        "📅 <b>HINDI ANIME SCHEDULE</b>\n"
        f"🗓 <b>{DAY_FULL[day_idx]}</b> • {now.strftime('%d %b %Y')}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
    )

    blocks = []
    for n, it in enumerate(todays, 1):
        num = _NUM[n - 1] if n <= len(_NUM) else f"{n}."
        title = f"{num} <b>{E(it['title'])}</b>"
        if it["season"]:
            title += f" <i>(S{it['season']})</i>"
        if it["upcoming"] and it["next"] is None:
            title += " 🎙 <i>Upcoming Dub</i>"
        l2 = []
        t = it["next_time"] if it["next_date"] == today and it["next_time"] else it["time"]
        if t:
            l2.append(f"⏰ <b>{E(t)}</b> IST")
        ep = _ep_label(it)
        if ep:
            if it["total"]:
                ep += f" <i>({it['cur'] if it['cur'] is not None else '?'}/{it['total']})</i>"
            l2.append(f"▶️ {ep}")
        l3 = [f"🎙 {E(_lang_line(it['langs']))}"]
        if it["freq"]:
            l3.append(f"🔁 {E(it['freq'])}")
        blocks.append(f"{title}\n   {'  •  '.join(l2) if l2 else '⏰ Time TBA'}\n   {'  •  '.join(l3)}\n")

    later_lines = []
    for it in later:
        when = it["next_date"].strftime("%d %b")
        tm = it["next_time"] or it["time"] or ""
        ep = _ep_label(it)
        s = f"• <b>{E(it['title'])}</b>"
        if it["season"]:
            s += f" <i>(S{it['season']})</i>"
        s += f" — {when}{(', ' + E(tm)) if tm else ''}"
        if ep:
            s += f" • {ep}"
        elif it["upcoming"]:
            s += " • 🎙 Upcoming Dub"
        later_lines.append(s)

    foot = "━━━━━━━━━━━━━━━━━━━━\n⏰ Saare time IST mein hain"
    if footer:
        foot += f"\n💜 <b>Powered By</b> : {E(footer)}"
    later_block = ""
    if later_lines:
        later_block = "\n🔜 <b>AAGE AANE WALE</b>\n" + "\n".join(later_lines) + "\n\n"

    if not blocks and not later_lines:
        return [head + "\n😴 Aaj koi Hindi anime schedule nahi hai.\n" + foot]
    if not blocks:
        blocks = ["😴 Aaj in anime ka koi naya episode nahi aa raha.\n"]

    msgs, cur = [], head + "\n"
    for b in blocks:
        if len(cur) + len(b) + 1 > MAX_MSG_LEN - len(foot):
            msgs.append(cur.rstrip())
            cur = ""
        cur += b + "\n"
    tail = later_block
    if len(cur) + len(tail) + len(foot) > MAX_MSG_LEN:
        msgs.append(cur.rstrip())
        cur = ""
        # aane wale section bhi lamba ho to todo
        chunk = ""
        for ln in later_lines:
            if len(chunk) + len(ln) + 40 > MAX_MSG_LEN - len(foot):
                msgs.append(("🔜 <b>AAGE AANE WALE</b>\n" + chunk).rstrip())
                chunk = ""
            chunk += ln + "\n"
        cur = ("🔜 <b>AAGE AANE WALE</b>\n" + chunk + "\n") if chunk else ""
        tail = ""
    msgs.append(cur + tail + foot)
    return msgs


# ───────────────────────── posting ─────────────────────────
async def _owner_dm(client: Client, text: str):
    for oid in dict.fromkeys(list(owner)):
        try:
            await client.send_message(oid, text, parse_mode=ParseMode.DISABLED)
        except Exception:
            pass


async def post_schedule(client: Client, day_idx: int = None, manual: bool = False):
    """Schedule scrape karke channel mein post karo. Returns (ok, info_text)."""
    global BUSY
    async with _LOCK:
        BUSY = True
        try:
            cfg = await _cfg()
            ch = cfg.get("channel_id")
            if not ch:
                return False, "Schedule channel set nahi hai. /add_hindi_anime_schedule <channel_id>"
            now = datetime.now(IST)
            if day_idx is None:
                day_idx = now.weekday()

            from ..utils import job_ctl
            try:
                data = await job_ctl.run_in_thread(_scrape_sync, day_idx)
            except Exception as e:
                LOGGER.error(f"[HindiSchedule] scrape failed: {e}")
                return False, f"Page load/scrape fail: {type(e).__name__}: {e}"

            parsed = [parse_card(c) for c in data["cards"]]
            items = filter_hindi(parsed)
            if not data["cards"]:
                return False, ("Page par koi anime card nahi mila (page badal gaya ya load nahi hua). "
                               "/hindi_schedule_debug se raw text bhejo.")
            if not data["clicked"]:
                LOGGER.warning(f"[HindiSchedule] day tab '{DAYS[day_idx]}' click nahi hua — page ka default view use hua")

            try:
                from ..utils.community import get_community_tag
                from ..utils.caption_style import _handles_brackets
                footer = _handles_brackets(await get_community_tag(all_names=True), spaced=True)
            except Exception:
                footer = ""

            msgs = build_posts(day_idx, now, items, footer)
            for i, text in enumerate(msgs):
                await client.send_message(ch, text, parse_mode=ParseMode.HTML,
                                          disable_web_page_preview=True)
                if i < len(msgs) - 1:
                    await asyncio.sleep(2)
            LOGGER.info(f"[HindiSchedule] posted {len(items)} anime ({DAYS[day_idx]}) to {ch}")
            if not manual:
                cfg = await _cfg()
                cfg["last_date"] = now.date().isoformat()
                await _save(cfg)
            return True, f"{len(items)} Hindi anime ka {DAY_FULL[day_idx]} schedule post ho gaya."
        except Exception as e:
            LOGGER.error(f"[HindiSchedule] post failed: {e}")
            return False, f"Post fail: {type(e).__name__}: {e}"
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
    title, err = await _validate_channel(client, channel_id)
    if err:
        return None, err
    cfg = await _cfg()
    cfg.update({"channel_id": channel_id, "channel_title": title})
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
    await message.reply(_status_text(cfg) + "\n\nCommands: `/hindi_schedule on|off`, `/hindi_schedule_time`, "
                        "`/hindi_schedule_now`, `/add_hindi_anime_schedule`")


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


def _status_text(cfg: dict) -> str:
    h, m = _hhmm(cfg.get("time"))
    ch = f"**{cfg.get('channel_title', 'Unknown')}**\n🆔 `{cfg['channel_id']}`" if cfg.get("channel_id") else "❌ Set nahi hai"
    today = DAY_FULL[datetime.now(IST).weekday()]
    return (
        "📅 **Hindi Anime Schedule**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📢 Channel: {ch}\n"
        f"⏰ Roz post: **{fmt12(h, m)} IST**\n"
        f"🔔 Auto-post: **{'🟢 ON' if cfg.get('enabled', True) else '🔴 OFF'}**\n"
        f"🗓 Aaj: {today}\n"
        f"📤 Last post: {cfg.get('last_date') or '—'}"
    )


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
    rows = [
        [InlineKeyboardButton("✏️ Channel badlo" if cfg.get("channel_id") else "➕ Channel set karo",
                              callback_data="cmz:hs:chan"),
         InlineKeyboardButton("⏰ Post Time", callback_data="cmz:hs:time")],
        [InlineKeyboardButton("🟢 Auto-post ON — OFF karo" if on else "🔴 Auto-post OFF — ON karo",
                              callback_data="cmz:hs:tog")],
        [InlineKeyboardButton("📤 Aaj ka schedule abhi post karo", callback_data="cmz:hs:now")],
        [InlineKeyboardButton("🧪 Debug file", callback_data="cmz:hs:dbg")],
    ]
    if cfg.get("channel_id"):
        rows.append([InlineKeyboardButton("🗑 Channel hatao", callback_data="cmz:hs:rm")])
    rows.append(_back_row())
    await _edit(client, cb, _status_text(cfg), InlineKeyboardMarkup(rows))


_PRESETS = ["0600", "0700", "0800", "0900", "1000", "1200", "1800", "2100"]


async def handle(client: Client, cb: CallbackQuery, args: list):
    sub = args[0] if args else ""
    uid = cb.from_user.id

    if sub == "chan":
        _SESS[uid] = {"mode": "channel"}
        await cb.answer()
        await _edit(client, cb,
                    "**📢 Schedule channel set karo**\n━━━━━━━━━━━━━━━━━━━━\n\n"
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

    if sub == "now":
        cfg = await _cfg()
        if not cfg.get("channel_id"):
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

    if sub == "rm":
        await cb.answer()
        await _edit(client, cb, "🗑 **Schedule channel hata du?**\n\nAuto-post band ho jayega.",
                    InlineKeyboardMarkup([[InlineKeyboardButton("✅ Haan, hatao", callback_data="cmz:hs:rmok"),
                                           InlineKeyboardButton("❌ Nahi", callback_data="cmz:open:hindi_schedule")]]))
        return

    if sub == "rmok":
        cfg = await _cfg()
        for k in ("channel_id", "channel_title", "last_date", "last_try"):
            cfg.pop(k, None)
        await _save(cfg)
        await cb.answer("🗑 Hata diya")
        await render(client, cb)
        return

    await cb.answer()


# group=-3: sirf tab consume karta hai jab user is panel ke input mode mein ho
@Client.on_message((filters.text | filters.forwarded) & filters.private, group=-3)
async def hs_input(client: Client, message: Message):
    if not message.from_user:
        return
    uid = message.from_user.id
    sess = _SESS.get(uid)
    if not sess or not _is_auth(uid):
        return
    raw = (message.text or "").strip()
    if raw.startswith("/"):
        _SESS.pop(uid, None)
        return
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
    await message.reply(f"✅ **Schedule channel set!**\n\n📢 **{title}**\n🆔 `{cid}`", reply_markup=back_kb)
    raise StopPropagation
