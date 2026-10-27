"""
episode_guard.py
================
Auto-monitor ke liye DUPLICATE UPLOAD se bachao.

Problem: ek hi post kabhi-kabhi do baar aa jaata hai (ya do alag posts same episode ke), jisse
usi episode ka upload do baar shuru ho jaata tha. Ab upload SE PEHLE upload-channel ko scan karke
dekha jaata hai ki (season, episode, quality) wahan pehle se hai ya nahi.

Scan kaise hota hai
-------------------
  * Owner ke user-session (uploader client) se channel ki recent history padhi jaati hai
    (bot token se get_chat_history aksar allowed nahi hota); user client na mile to bot client try.
  * Sirf MEDIA posts dekhe jaate hain (video / document / audio / animation, aur buttons wali photo
    — bot-mode post). "Next episode upload on ..." jaise plain text messages ignore hote hain,
    taaki galti se false-positive na ho.
  * Filename aur caption DONO parse hote hain — bot ke SAARE formats:

      filename : "Naruto S01E05 in Hindi 720p [@SBANIME].mp4",  "Naruto E05 in Hindi ..."
      caption  : "Season 01 Episode 05 Hindi"            (default)
                 "Episode : 05 / Season : 01"             (style 1, 5)
                 "◈ Episode: 05" + "(S - 01)"             (style 2)
                 "│‣ Season - 01 / Episode - 05"          (style 3)
                 "◈ (S - 01) Episode: 05"                 (style 4)
                 "𝗦01𝗘05" , "𝐒𝐞𝐚𝐬𝐨𝐧: 01 / 𝐄𝐩𝐢𝐬𝐨𝐝𝐞: 05"    (fancy unicode — NFKC se normal ho jaata hai)
                 "S01 • E05" , "[S01-E05]"
                 "「 Season 01 ⋄ Episode 05 」", "(Season 01 — Episode 05)", "Season 01 | Episode 05"

  * Season kahin na mile to bot ki tarah season = 1 maana jaata hai (caption mein `season or 1`).

Scan fail ho (user session nahi / permission nahi) to `None` milta hai — upload KABHI nahi rokta
(fail-open); tab sirf chhota in-process registry kaam aata hai.
"""
import asyncio
import re
import time
import unicodedata

from .. import LOGGER

# "Season 01 Episode 05" / "S01E05" / "S01 • E05" / "(S - 01) Episode: 05" / "[S01-E05]"
_SE = re.compile(
    r'(?<![a-z0-9])(?:season|s)\s*[-:.]?\s*0*(\d{1,2})\s*[^a-z0-9]{0,8}\s*'
    r'(?:episode|ep|e)\s*[-:.]?\s*0*(\d{1,4})(?!\d)'
)
_EP = re.compile(r'(?<![a-z0-9])(?:episode|ep|e)\s*[-:.]?\s*0*(\d{1,4})(?!\d)')
_SN = re.compile(r'(?<![a-z0-9])(?:season|s)\s*[-:.]?\s*0*(\d{1,2})(?![a-z0-9])')
_Q = re.compile(r'(?<![0-9])(2160|1440|1080|720|576|480|360|240|144)\s*p(?![a-z0-9])')



def canon(q: str) -> str:
    """Quality tier: ADH ka 480p aur RTI ka 360p EK HI tier hai (dono 'sabse chhoti quality').
    360p aa gaya to 480p ki zarurat nahi, aur 480p aa gaya to 360p ki nahi."""
    return "360p" if q == "480p" else q


_TTL = 40            # scan cache (sec)
_CACHE: dict = {}    # channel_id -> (ts, {(season, ep): {quality, ...}})
_LOCKS: dict = {}
_REG: dict = {}      # (channel_id, season, ep, quality) -> ts   (scan fail hone par backup)
_REG_TTL = 12 * 3600


def norm(text: str) -> str:
    """NFKC: 𝗦/𝐄𝐩𝐢𝐬𝐨𝐝𝐞 jaise math-bold letters normal ASCII ban jaate hain."""
    return unicodedata.normalize("NFKC", text or "").lower()


def parse(text: str):
    """text -> (season|None, episode|None)"""
    t = norm(text)
    m = _SE.search(t)
    if m:
        return int(m.group(1)), int(m.group(2))
    me = _EP.search(t)
    if not me:
        return None, None
    ms = _SN.search(t)
    return (int(ms.group(1)) if ms else None), int(me.group(1))


def _qualities(*texts) -> set:
    out = set()
    for t in texts:
        for q in _Q.findall(norm(t)):
            out.add(canon(f"{q}p"))
    return out


def msg_entry(m):
    """Pyrogram message -> (season, episode, {qualities}) ya None (agar episode-post nahi)."""
    media = m.video or m.document or m.audio or getattr(m, "animation", None)
    is_post = bool(media) or bool(m.photo and m.reply_markup)
    if not is_post:
        return None
    fname = getattr(media, "file_name", None) if media else None
    cap = m.caption or ""
    fs, fe = parse(fname or "")
    cs, ce = parse(cap)
    ep = fe or ce
    if not ep:
        return None
    season = fs or cs or 1          # bot caption mein bhi `season or 1`
    return season, ep, _qualities(fname, cap)


async def _scan(client, channel_id: int, limit: int) -> dict:
    data: dict = {}
    async for m in client.get_chat_history(channel_id, limit=limit):
        try:
            e = msg_entry(m)
        except Exception:
            continue
        if e:
            season, ep, qs = e
            data.setdefault((season, ep), set()).update(qs)
    return data


async def get_existing(client, channel_id: int, owner_id: int = None, limit: int = 500, force: bool = False):
    """
    {(season, ep): {"360p", ...}} ya None (scan nahi ho paya).
    Quality set khali ho sakta hai (message mein quality likhi hi nahi thi) — tab bhi episode "maujood" hai.
    """
    c = _CACHE.get(channel_id)
    if c and not force and time.time() - c[0] < _TTL:
        return c[1]
    lock = _LOCKS.setdefault(channel_id, asyncio.Lock())
    async with lock:
        c = _CACHE.get(channel_id)
        if c and not force and time.time() - c[0] < _TTL:
            return c[1]
        uc = None
        data = None
        if owner_id:
            try:
                from .uploads.telegram import _make_uploader_client
                uc = await asyncio.wait_for(_make_uploader_client(owner_id, max_transmissions=4), timeout=30)
            except Exception as e:  # noqa: BLE001
                LOGGER.warning(f"[EpGuard] user client nahi mila: {e!r}")
                uc = None
        try:
            for src, label in ((uc, "user"), (client, "bot")):
                if src is None:
                    continue
                try:
                    data = await asyncio.wait_for(_scan(src, channel_id, limit), timeout=90)
                    LOGGER.info(f"[EpGuard] {channel_id}: {sum(len(v) or 1 for v in data.values())} "
                                f"episode-entries scanned ({label} client)")
                    break
                except Exception as e:  # noqa: BLE001
                    LOGGER.warning(f"[EpGuard] {label} scan fail ({channel_id}): {e!r}")
        finally:
            if uc is not None:
                try:
                    await uc.disconnect()      # sirf release (shared client)
                except Exception:
                    pass
        if data is None:
            return None
        _CACHE[channel_id] = (time.time(), data)
        return data


def _reg_has(channel_id, season, ep, quality) -> bool:
    now = time.time()
    for k in [k for k, t in _REG.items() if now - t > _REG_TTL]:
        _REG.pop(k, None)
    return (channel_id, season, ep, canon(quality)) in _REG


def record_upload(channel_id: int, season, ep: int, quality: str):
    """Upload ke baad turant cache + registry mein daalo (taaki agla check fresh scan ka wait na kare)."""
    season = season or 1
    quality = canon(quality)
    _REG[(channel_id, season, ep, quality)] = time.time()
    c = _CACHE.get(channel_id)
    if c:
        c[1].setdefault((season, ep), set()).add(quality)


def forget_channel(channel_id: int):
    """Delete ke baad: cache + registry saaf (taaki dobara upload ho sake)."""
    _CACHE.pop(channel_id, None)
    for k in [k for k in _REG if k[0] == channel_id]:
        _REG.pop(k, None)
    for k in [k for k in _INFLIGHT if k[0] == channel_id]:
        _INFLIGHT.pop(k, None)
    for k in [k for k in _COMPLETE if k[0] == channel_id]:
        _COMPLETE.pop(k, None)


# ─────────────────────────────────────────────────────────────────────────────
#  IN-FLIGHT CLAIM  +  "EPISODE COMPLETE" flag
#  -------------------------------------------
#  Auto-monitor (RTI) aur Direct-File channel DONO ek hi episode ki same quality
#  ek saath upload na kar dein, isliye upload shuru karne se pehle quality "claim"
#  hoti hai. Jo pehle claim kare wahi upload karega, doosra skip.
#  Claim ko TTL hai — kisi crash/exception se atak bhi jaye to khud expire ho jaata hai.
# ─────────────────────────────────────────────────────────────────────────────
_INFLIGHT: dict = {}       # (channel_id, season, ep, quality) -> ts
_INFLIGHT_TTL = 45 * 60
_COMPLETE: dict = {}       # (channel_id, season, ep) -> ts   (Direct channel ne poora episode finalize kar diya)
_COMPLETE_TTL = 24 * 3600


def claim(channel_id: int, season, ep: int, quality: str) -> bool:
    """True = ab ye quality tumhari hai (upload karo). False = koi aur already upload kar raha hai."""
    now = time.time()
    for k in [k for k, t in _INFLIGHT.items() if now - t > _INFLIGHT_TTL]:
        _INFLIGHT.pop(k, None)
    key = (channel_id, season or 1, ep, canon(quality))
    if key in _INFLIGHT:
        return False
    _INFLIGHT[key] = now
    return True


def release(channel_id: int, season, ep: int, quality: str):
    _INFLIGHT.pop((channel_id, season or 1, ep, canon(quality)), None)


def is_claimed(channel_id: int, season, ep: int, quality: str) -> bool:
    t = _INFLIGHT.get((channel_id, season or 1, ep, canon(quality)))
    return bool(t and time.time() - t <= _INFLIGHT_TTL)


def mark_complete(channel_id: int, season, ep: int):
    _COMPLETE[(channel_id, season or 1, ep)] = time.time()


def clear_complete(channel_id: int, season, ep: int):
    _COMPLETE.pop((channel_id, season or 1, ep), None)


def is_complete(channel_id: int, season, ep: int) -> bool:
    t = _COMPLETE.get((channel_id, season or 1, ep))
    if not t:
        return False
    if time.time() - t > _COMPLETE_TTL:
        _COMPLETE.pop((channel_id, season or 1, ep), None)
        return False
    return True


async def present_qualities(client, channel_id: int, owner_id, season, ep: int, wanted=()):
    """
    Is (season, ep) ki kaun si qualities channel mein pehle se hain.
    Returns (set_of_wanted_qualities_present, episode_exists_without_quality_info, scan_ok)
      - scan_ok False => scan nahi hua; sirf in-process registry ka jawab (fail-open).
    """
    season = season or 1
    data = await get_existing(client, channel_id, owner_id)
    present = set()
    any_no_q = False
    if data is not None:
        qs = data.get((season, ep))
        if qs is not None:
            present = {q for q in wanted if canon(q) in qs}
            if not qs:
                any_no_q = True
    for q in wanted:
        if _reg_has(channel_id, season, ep, q):
            present.add(q)
    return present, any_no_q, data is not None


# ─────────────────────────────────────────────────────────────────────────────
#  ONCE — ek hi cheez (jaise "update post") RTI aur ADH dono monitor se do baar na jaye
# ─────────────────────────────────────────────────────────────────────────────
_ONCE: dict = {}
_ONCE_TTL = 6 * 3600


def once(key) -> bool:
    """True = pehli baar (karo). False = koi aur source pehle hi kar chuka."""
    now = time.time()
    for k in [k for k, t in _ONCE.items() if now - t > _ONCE_TTL]:
        _ONCE.pop(k, None)
    if key in _ONCE:
        return False
    _ONCE[key] = now
    return True
