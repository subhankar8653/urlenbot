"""
quota.py
========
Upload QUOTA system — ek saath kitne uploads chal sakte hain.

Rules (SIRF manual uploads: /Rtic, /rti, /toono, /swift, /aniwatch ...).
Auto-monitor channel uploads pe quota NAHI lagta (na wait, na slot consume):

  • SINGLE job  = sirf 1 episode / 1 file-set upload karna hai.
  • MULTI  job  = 2 ya zyada episodes (range / season / multi-select).

  - Ek saath max 2 SINGLE jobs chal sakti hain  (UPLOAD_QUOTA_SINGLE se badal sakte ho).
  - MULTI job hamesha AKELI chalti hai — jab tak woh chal rahi hai koi aur
    job (single ya multi) shuru nahi hoti, aur jab tak koi single chal raha
    hai tab tak multi shuru nahi hoti.
  - Jo job quota mein fit nahi hoti woh QUEUE mein chali jaati hai (FIFO,
    strict order — taaki multi job bhookhi na rahe) aur jaise hi jagah
    milti hai apne aap shuru ho jaati hai. Card mein "Quota mein hai" dikhta hai.

Examples:
  Naruto Ep1 (single) + Boruto Ep1 (single)      -> dono ek saath
  + teesra single                                 -> queue
  Naruto Ep1-3 (multi) chal raha + Boruto Ep1     -> Boruto queue mein
  Boruto Ep1 (single) chal raha + Naruto Ep1-3    -> Naruto queue mein
"""

import asyncio
import os
from collections import deque

from .. import LOGGER

try:
    MAX_SINGLE = max(1, int(os.environ.get("UPLOAD_QUOTA_SINGLE", "2")))
except ValueError:
    MAX_SINGLE = 2

_running = {"single": 0, "multi": 0}
_waiters: deque = deque()


class Ticket:
    __slots__ = ("kind", "fut", "cb", "started", "released", "pos", "waited")

    def __init__(self, kind, cb=None):
        self.kind = kind
        self.cb = cb
        self.fut = None
        self.started = False
        self.released = False
        self.pos = 0
        self.waited = False


def kind_for(count) -> str:
    """1 episode -> 'single', 2+ -> 'multi'."""
    try:
        return "multi" if int(count or 1) > 1 else "single"
    except (TypeError, ValueError):
        return "single"


def _can_start(kind: str) -> bool:
    if kind == "multi":
        return _running["single"] == 0 and _running["multi"] == 0
    return _running["multi"] == 0 and _running["single"] < MAX_SINGLE


def _reason(kind: str) -> str:
    if kind == "multi":
        return "Multi-episode upload akela chalta hai — baaki uploads khatam hone ka wait."
    if _running["multi"]:
        return "Abhi ek multi-episode upload chal raha hai — uske baad shuru hoga."
    return f"Ek saath max {MAX_SINGLE} single uploads chal sakte hain — jagah banne ka wait."


async def _call(t: Ticket):
    try:
        await t.cb(t.pos, _reason(t.kind))
    except Exception as e:
        LOGGER.debug(f"[Quota] wait callback error: {e!r}")


def _notify(t: Ticket):
    if t.cb is not None:
        asyncio.ensure_future(_call(t))


def _start(t: Ticket):
    _running[t.kind] += 1
    t.started = True
    if t.fut is not None and not t.fut.done():
        t.fut.set_result(True)


def _pump():
    while _waiters and _can_start(_waiters[0].kind):
        _start(_waiters.popleft())
    for i, t in enumerate(_waiters, 1):
        if t.pos != i:
            t.pos = i
            _notify(t)


async def acquire(kind: str, on_wait=None) -> Ticket:
    """
    Quota slot lo. Jagah ho to turant, warna queue mein wait.
    on_wait: async fn(position:int, reason:str) — queue position badalne par bulaya jaata hai.
    Cancel hone par (CancelledError) queue se nikal jaata hai.
    """
    t = Ticket(kind, on_wait)
    t.fut = asyncio.get_event_loop().create_future()
    if not _waiters and _can_start(kind):
        _start(t)
        LOGGER.info(f"[Quota] {kind} started (running: {dict(_running)})")
        return t

    t.waited = True
    _waiters.append(t)
    t.pos = len(_waiters)
    LOGGER.info(f"[Quota] {kind} queued at #{t.pos} (running: {dict(_running)})")
    _notify(t)
    try:
        await t.fut
    except asyncio.CancelledError:
        if t.started:
            release(t)
        else:
            try:
                _waiters.remove(t)
            except ValueError:
                pass
            _pump()
        raise
    LOGGER.info(f"[Quota] {kind} started after wait (running: {dict(_running)})")
    return t


def release(t: Ticket):
    if t is None or t.released or not t.started:
        return
    t.released = True
    _running[t.kind] = max(0, _running[t.kind] - 1)
    LOGGER.info(f"[Quota] {t.kind} released (running: {dict(_running)}, queued: {len(_waiters)})")
    _pump()


def status() -> dict:
    return {
        "single": _running["single"],
        "multi": _running["multi"],
        "queued": len(_waiters),
        "queued_kinds": [w.kind for w in _waiters],
        "max_single": MAX_SINGLE,
    }
