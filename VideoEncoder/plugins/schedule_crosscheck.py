"""
schedule_crosscheck.py
======================
Hindi schedule ke liye DOOSRA source (https://deadtoons.sbs/schedule) + dono sources ka matching/merge.

Pehle:  sirf hindianimeszone.com/schedule.html  (source A)
Ab   :  A (primary)  +  deadtoons.sbs/schedule (source B, double-check)

Merge ke niyam (merge_items):
  • A ka Hindi + NON-complete anime = base list.
  • A + B dono mein hai          -> "confirmed" (A ka time/episode rehta hai; fark ho to report mein likha jaata hai;
                                     A mein episode TBA ho to B ka episode liya jaata hai).
  • Sirf A mein hai              -> rehta hai (A primary hai; B har daily anime nahi dikhata).
  • Sirf B mein hai (A ke aaj ke tab mein kisi bhi roop mein nahi) -> ADD (A abhi update nahi hua / naya anime aaya).
  • B mein hai par A usse COMPLETE / dusre din / non-Hindi bata raha hai -> ADD NAHI (A ki baat maani).
  • B load / parse na ho            -> kuch nahi badlega (sirf A), report mein likha jaata hai.

B ka page JS se banta hai aur time "aapke local time" mein dikhata hai, isliye Chrome ka timezone Asia/Kolkata
(IST) set karke scrape hota hai.
"""

import re
import shutil
import tempfile
import time as _time
from difflib import SequenceMatcher

DEADTOONS_URL = "https://deadtoons.sbs/schedule"
MAX_B_ONLY = 8

_EP_LINE = re.compile(r"^S\s*(\d{1,2})\s*[·•|:\-–.]\s*(?:Episodes?|Ep\.?)\s*(\d+)(?:\s*[-–&,]\s*(\d+))?\s*$", re.IGNORECASE)
_TIME_LINE = re.compile(r"^(\d{1,2}:\d{2})\s*([AP]M)$", re.IGNORECASE)
_START_MARK = re.compile(r"release schedule", re.IGNORECASE)
_END_MARK = re.compile(r"follow on social media|trending now", re.IGNORECASE)


# ───────────────────────── parsing (source B) ─────────────────────────
def _clean_time(h_m: str, ap: str) -> str:
    h, m = h_m.split(":")
    return f"{int(h):02d}:{m} {ap.upper()}"


def parse_deadtoons(body: str) -> list:
    """
    Page ke innerText se aaj ke entries nikalo. Har entry 3 line ka hota hai:
        <Title>            (kabhi 'Title…' kata hua)
        S3 · Episode 9
        7:30 AM
    Return: [{title, season, ep, ep_end, time, truncated}]
    """
    lines = [l.strip() for l in (body or "").splitlines()]
    lines = [l for l in lines if l]
    # sirf "Release Schedule" ke baad aur "Follow On Social Media" se pehle ka hissa
    start, end = 0, len(lines)
    for i, l in enumerate(lines):
        if _START_MARK.search(l):
            start = i + 1
            break
    for i in range(start, len(lines)):
        if _END_MARK.search(lines[i]):
            end = i
            break
    seg = lines[start:end]

    out = []
    for i, l in enumerate(seg):
        m = _EP_LINE.match(l)
        if not m:
            continue
        # title = is line se pehle ki sabse nazdeek 'asli' line
        title = ""
        for j in range(i - 1, max(-1, i - 3), -1):
            cand = seg[j]
            if _TIME_LINE.match(cand) or _EP_LINE.match(cand) or re.fullmatch(r"\d{1,2}|today|sun|mon|tue|wed|thu|fri|sat", cand, re.I):
                continue
            title = cand
            break
        if not title:
            continue
        tm = None
        for j in range(i + 1, min(len(seg), i + 3)):
            mt = _TIME_LINE.match(seg[j])
            if mt:
                tm = _clean_time(mt.group(1), mt.group(2))
                break
        truncated = bool(re.search(r"(…|\.\.\.)\s*$", title))
        out.append({
            "title": re.sub(r"(…|\.\.\.)\s*$", "", title).strip(),
            "season": int(m.group(1)),
            "ep": int(m.group(2)),
            "ep_end": int(m.group(3)) if m.group(3) else None,
            "time": tm,
            "truncated": truncated,
        })
    # duplicate hatao
    seen, uniq = set(), []
    for e in out:
        k = (e["title"].lower(), e["season"], e["ep"])
        if k not in seen:
            seen.add(k)
            uniq.append(e)
    return uniq


def scrape_deadtoons_sync() -> dict:
    """Chrome (IST timezone) se deadtoons schedule ka aaj ka tab padho. Return {ok, body, items, error}."""
    try:
        from .rti_downloader import SELENIUM_OK, _kill_driver_tree
        if not SELENIUM_OK:
            return {"ok": False, "body": "", "items": [], "error": "Selenium install nahi hai"}
        from .swift_downloader import _make_driver
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "body": "", "items": [], "error": f"import: {e}"}

    dl = tempfile.mkdtemp(prefix="hsx_")
    driver = None
    try:
        driver = _make_driver(dl)
        try:                                           # time "local" dikhata hai -> IST force
            driver.execute_cdp_cmd("Emulation.setTimezoneOverride", {"timezoneId": "Asia/Kolkata"})
        except Exception:
            pass
        driver.get(DEADTOONS_URL)
        body = ""
        deadline = _time.time() + 40
        while _time.time() < deadline:
            body = driver.execute_script("return document.body ? document.body.innerText : ''") or ""
            if re.search(r"\bEpisode\s*\d+", body) and _START_MARK.search(body):
                _time.sleep(1.5)                       # list poori render ho jaye
                body = driver.execute_script("return document.body ? document.body.innerText : ''") or body
                break
            _time.sleep(1.5)
        items = parse_deadtoons(body)
        return {"ok": bool(items) or bool(_START_MARK.search(body)), "body": body, "items": items,
                "error": "" if body else "page khali"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "body": "", "items": [], "error": f"{type(e).__name__}: {e}"}
    finally:
        try:
            _kill_driver_tree(driver)
        except Exception:
            pass
        shutil.rmtree(dl, ignore_errors=True)


# ───────────────────────── matching + merge ─────────────────────────
def _key(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"\b(the\s+)?final\s+season\b|\bseason\s*\d+\b|\b\d+(?:st|nd|rd|th)\s+season\b|"
               r"\bpart\s*\d+\b|\bcour\s*\d+\b|\bs\d+\b|\b(?:hindi|dubbed|dub|sub|tv)\b", " ", s)
    return re.sub(r"[^a-z0-9]", "", s)


def same_title(a_title: str, b_title: str, b_truncated: bool, score_fn=None) -> bool:
    """A ka title (poora) vs B ka title (kabhi kata hua '…')."""
    ka, kb = _key(a_title), _key(b_title)
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    if b_truncated and len(kb) >= 8 and ka.startswith(kb):
        return True
    if score_fn is not None:
        try:
            if score_fn(a_title, b_title) >= 0.88:
                return True
        except Exception:
            pass
    return SequenceMatcher(None, ka, kb).ratio() >= 0.9


def _mk_item(b: dict, today) -> dict:
    return {"title": b["title"], "season": b["season"], "langs": ["HINDI"], "freq": "", "time": b["time"],
            "per_release": 1, "cur": None, "total": None, "next": b["ep"], "next_end": b.get("ep_end"),
            "next_date": today, "next_time": b["time"], "upcoming": False, "complete": False,
            "raw": "[deadtoons]", "src": "B"}


def merge_items(a_all: list, a_items: list, b_items: list, today, score_fn=None) -> tuple:
    """
    a_all   : A ke saare parsed cards (sab language, complete bhi) — sirf matching ke liye
    a_items : A ka filtered (Hindi, non-complete, aaj) list — post ka base
    b_items : parse_deadtoons() ka result (khali/None => merge nahi hoga)
    Return (merged_items, report)
    """
    report = {"b_ok": bool(b_items), "confirmed": [], "only_a": [], "added_from_b": [], "skipped_b": [],
              "ep_diff": [], "time_diff": []}
    if not b_items:
        return list(a_items), report

    merged = [dict(i) for i in a_items]
    used_a = set()
    for b in b_items:
        hit = None
        for idx, a in enumerate(a_all):
            if same_title(a["title"], b["title"], b.get("truncated", False), score_fn):
                hit = a
                break
        if hit is not None:
            # A ke filtered list mein hai?
            m_item = next((m for m in merged if m["title"] == hit["title"]), None)
            if m_item is not None:
                used_a.add(hit["title"])
                report["confirmed"].append(hit["title"])
                if m_item.get("next") is None and b["ep"]:            # A mein ep TBA -> B se lo
                    m_item["next"], m_item["next_end"] = b["ep"], b.get("ep_end")
                    m_item["upcoming"] = False
                elif m_item.get("next") is not None and b["ep"] and m_item["next"] != b["ep"]:
                    report["ep_diff"].append((hit["title"], m_item["next"], b["ep"]))
                if b["time"] and m_item.get("time") and _norm_t(m_item.get("next_time") or m_item["time"]) != _norm_t(b["time"]):
                    report["time_diff"].append((hit["title"], m_item.get("next_time") or m_item["time"], b["time"]))
            else:
                why = "complete" if hit.get("complete") else "A ke hisaab se aaj nahi / Hindi nahi"
                report["skipped_b"].append((b["title"], why))
            continue
        # B-only
        if b.get("truncated"):                       # poora naam nahi pata — adhoora naam channel mein nahi daalna
            report["skipped_b"].append((b["title"], "naam kata hua"))
            continue
        if len(report["added_from_b"]) >= MAX_B_ONLY:
            report["skipped_b"].append((b["title"], "limit"))
            continue
        merged.append(_mk_item(b, today))
        report["added_from_b"].append(b["title"])

    for a in a_items:
        if a["title"] not in used_a:
            report["only_a"].append(a["title"])
    return merged, report


def _norm_t(t: str) -> str:
    m = re.match(r"(\d{1,2}):(\d{2})\s*([AP]M)", t or "", re.IGNORECASE)
    return f"{int(m.group(1)):02d}:{m.group(2)} {m.group(3).upper()}" if m else (t or "")


def report_text(rep: dict) -> str:
    """Owner ke liye chhota summary."""
    if not rep.get("b_ok"):
        return "🔎 Double-check: deadtoons load/parse nahi hua — sirf hindianimeszone use hua."
    lines = [f"🔎 Double-check (2 sources): ✅ {len(rep['confirmed'])} match"]
    if rep["added_from_b"]:
        lines.append("➕ deadtoons se add: " + ", ".join(rep["added_from_b"]))
    if rep["only_a"]:
        lines.append("ℹ️ Sirf hindianimeszone mein: " + ", ".join(rep["only_a"][:6]))
    for t, a, b in rep["ep_diff"][:5]:
        lines.append(f"⚠️ Episode alag: {t} — A:E{a} vs B:E{b} (A rakha)")
    for t, a, b in rep["time_diff"][:5]:
        lines.append(f"🕒 Time alag: {t} — A:{a} vs B:{b} (A rakha)")
    for t, why in rep["skipped_b"][:5]:
        lines.append(f"🚫 B se nahi liya: {t} ({why})")
    return "\n".join(lines)
