"""Non-owner users ke liye optional cooldown (env URL_COOLDOWN_SEC, default 0 = band)."""
import os
import time

_last: dict = {}


def check(user_id: int, exempt: bool) -> int:
    """Return remaining seconds (0 = allowed)."""
    try:
        sec = int(os.getenv("URL_COOLDOWN_SEC", "0") or 0)
    except ValueError:
        sec = 0
    if sec <= 0 or exempt:
        return 0
    now = time.time()
    prev = _last.get(user_id)
    _last[user_id] = now
    if prev is None or now - prev >= sec:
        return 0
    return int(sec - (now - prev))
