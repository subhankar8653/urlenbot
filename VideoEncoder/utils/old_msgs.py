"""
old_msgs.py
===========
Naya episode aate hi PICHHLE episode ke schedule msg ("Next episode upload on ..." / "END") aur
end messages channel se hata do — restart / redeploy ke baad bhi.

Do tarike (dono chalte hain):

  1) DB IDs   : schedule_notify jo message IDs DB mein save karta hai (last_ep_msgs_<channel>)
                unhe delete karo.  DB mein hone ki wajah se restart/redeploy se farak nahi padta.

  2) CHANNEL SCAN : channel ke sabse neeche (latest) ke messages padho (owner ke user-session se,
                bot token se history aksar allowed nahi hoti). Jo message "schedule" type ka ya
                saved "end message" jaisa (sticker / photo / text ...) mile, usse delete karo.
                Pehla episode-post (video / document) milte hi scan ruk jaata hai, isliye episode
                files kabhi nahi chhoo jaati.

Fail-open: koi bhi error aaye to upload kabhi nahi rukta.
"""
import asyncio
import re
import time
import unicodedata

from .. import LOGGER

# "Next episode upload on 4th June" / "END" / "More episodes comming soon..."
_SCHED_RE = re.compile(
    r"^\s*(next\s+episode\s+upload\s+on\s+.+|end|more\s+episodes?\s+com+ing\s+soon\.*)\s*$",
    re.IGNORECASE | re.DOTALL,
)

_SCAN_LIMIT = 30          # channel ke neeche ke itne messages dekho
_SCAN_DEBOUNCE = 45       # sec — ek channel ka scan itni jaldi dobara nahi
_SIGS_CACHE_TTL = 300     # sec — saved end message signatures ka memory cache
_STORED_SIGS_MAX = 300

_LOCKS: dict = {}
_LAST_SCAN: dict = {}
_SIGS_CACHE = {"ts": 0.0, "sigs": set()}


# ─────────────────────────────────────────────
#  Signature — message "kaisa type ka hai"
# ─────────────────────────────────────────────
def _norm_text(t: str) -> str:
    t = unicodedata.normalize("NFKC", t or "").lower()
    return re.sub(r"\s+", " ", t).strip()


def sig_of(m) -> str | None:
    """Pyrogram Message -> signature string (None = pehchaan nahi sakte)."""
    try:
        if getattr(m, "sticker", None):
            return f"st:{m.sticker.file_unique_id}"
        if getattr(m, "photo", None):
            return f"ph:{m.photo.file_unique_id}"
        if getattr(m, "animation", None):
            return f"an:{m.animation.file_unique_id}"
        if getattr(m, "video", None):
            return f"vd:{m.video.file_unique_id}"
        if getattr(m, "document", None):
            return f"dc:{m.document.file_unique_id}"
        if getattr(m, "audio", None):
            return f"au:{m.audio.file_unique_id}"
        if getattr(m, "text", None):
            t = _norm_text(m.text)
            return f"tx:{t}" if t else None
    except Exception:
        return None
    return None


def _is_media(m) -> bool:
    return bool(getattr(m, "video", None) or getattr(m, "document", None)
                or getattr(m, "audio", None) or getattr(m, "photo", None)
                or getattr(m, "animation", None))


def is_schedule_text(m) -> bool:
    t = getattr(m, "text", None)
    if not t:
        return False
    return bool(_SCHED_RE.match(unicodedata.normalize("NFKC", t)))


# ─────────────────────────────────────────────
#  Stored signatures (DB) — end message bhejte waqt save hote hain
# ─────────────────────────────────────────────
async def _owner():
    from ..plugins.schedule_notify import _owner_id
    return await _owner_id()


async def remember_sent(sent_msgs) -> None:
    """End messages bhejne ke baad unki signatures DB mein save karo (restart-proof)."""
    try:
        sigs = [s for s in (sig_of(m) for m in sent_msgs if m) if s]
        if not sigs:
            return
        from .database.access_db import db
        oid = await _owner()
        if not oid:
            return
        user = await db._get_user(oid)
        cur = list(user.get("end_msg_sigs", []) or [])
        for s in sigs:
            if s in cur:
                cur.remove(s)
            cur.append(s)
        cur = cur[-_STORED_SIGS_MAX:]
        await db.col.update_one({"id": oid}, {"$set": {"end_msg_sigs": cur}}, upsert=True)
        _SIGS_CACHE["ts"] = 0.0
    except Exception as e:  # noqa: BLE001
        LOGGER.warning(f"[OldMsgs] remember_sent fail: {e!r}")


async def _end_sigs() -> set:
    """Saved end messages ki signatures: DB mein stored + abhi set kiye hue messages se nikali hui."""
    if time.time() - _SIGS_CACHE["ts"] < _SIGS_CACHE_TTL:
        return _SIGS_CACHE["sigs"]
    sigs: set = set()
    try:
        from .database.access_db import db
        from .. import app
        oid = await _owner()
        if oid:
            user = await db._get_user(oid)
            sigs.update(user.get("end_msg_sigs", []) or [])
            end_map = user.get("end_messages_map", {}) or {}
            for items in end_map.values():
                for item in items or []:
                    t = item.get("type")
                    try:
                        if t in ("copy_ref", "forward"):
                            src = await app.get_messages(item["from_chat_id"], item["message_id"])
                            s = sig_of(src) if src and not getattr(src, "empty", False) else None
                            if s:
                                sigs.add(s)
                        elif t == "text" and item.get("text"):
                            sigs.add(f"tx:{_norm_text(item['text'])}")
                    except Exception:
                        continue
    except Exception as e:  # noqa: BLE001
        LOGGER.warning(f"[OldMsgs] end sigs build fail: {e!r}")
    _SIGS_CACHE["ts"] = time.time()
    _SIGS_CACHE["sigs"] = sigs
    return sigs


# ─────────────────────────────────────────────
#  Delete helper
# ─────────────────────────────────────────────
async def _delete(client, ch: int, ids) -> int:
    ids = sorted({int(i) for i in ids if i})
    if not ids:
        return 0
    done = 0
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        try:
            await client.delete_messages(ch, chunk)
            done += len(chunk)
        except Exception:
            for mid in chunk:
                try:
                    await client.delete_messages(ch, mid)
                    done += 1
                    await asyncio.sleep(0.2)
                except Exception:
                    pass
    return done


# ─────────────────────────────────────────────
#  Method 2 — channel scan
# ─────────────────────────────────────────────
async def _scan_tail(src, channel_id: int, end_sigs: set) -> list:
    """Channel ke neeche se upar chalo; match hone wale message IDs return karo."""
    hits = []
    seen = 0
    async for m in src.get_chat_history(channel_id, limit=_SCAN_LIMIT):
        seen += 1
        if is_schedule_text(m):
            hits.append(m.id)
            continue
        s = sig_of(m)
        if s and s in end_sigs:
            hits.append(m.id)
            continue
        # Episode-post (ya koi aur media jo end message nahi) mila -> yahi ruk jao
        if _is_media(m) and not getattr(m, "sticker", None):
            break
        # text / sticker jo match nahi hua (border, season sticker ...) -> chhod ke aage badho
    return hits


async def _scan_and_delete(client, channel_id: int, owner_id) -> int:
    end_sigs = await _end_sigs()
    uc = None
    srcs = []
    if owner_id:
        try:
            from .uploads.telegram import _make_uploader_client
            uc = await asyncio.wait_for(_make_uploader_client(owner_id, max_transmissions=4), timeout=30)
            srcs.append((uc, "user"))
        except Exception as e:  # noqa: BLE001
            LOGGER.warning(f"[OldMsgs] user client nahi mila: {e!r}")
    srcs.append((client, "bot"))
    try:
        for src, label in srcs:
            try:
                hits = await asyncio.wait_for(_scan_tail(src, channel_id, end_sigs), timeout=60)
            except Exception as e:  # noqa: BLE001
                LOGGER.warning(f"[OldMsgs] scan fail ({label}, {channel_id}): {e!r}")
                continue
            if not hits:
                return 0
            n = await _delete(client, channel_id, hits)
            if n == 0 and src is not client:
                n = await _delete(src, channel_id, hits)
            LOGGER.info(f"[OldMsgs] scan ({label}) {channel_id}: {n}/{len(hits)} purane msgs delete")
            return n
    finally:
        if uc is not None:
            try:
                await uc.disconnect()   # shared client — sirf release
            except Exception:
                pass
    return 0


# ─────────────────────────────────────────────
#  PUBLIC — har upload path yehi call kare
# ─────────────────────────────────────────────
async def purge_old_bot_msgs(client, channel_id: int, scan: bool = True) -> int:
    """
    Channel se pichhle episode ke schedule/end messages hatao (DB ids + channel scan).
    Kitne delete hue wo return karta hai. Kabhi exception nahi uthata.
    """
    try:
        channel_id = int(channel_id)
    except Exception:
        return 0
    lock = _LOCKS.setdefault(channel_id, asyncio.Lock())
    total = 0
    async with lock:
        try:
            from ..plugins.schedule_notify import get_last_posted_msg_ids, clear_last_posted_msg_ids
            ids = await get_last_posted_msg_ids(channel_id)
            if ids:
                total += await _delete(client, channel_id, ids)
                await clear_last_posted_msg_ids(channel_id)
                LOGGER.info(f"[OldMsgs] DB ids se {channel_id}: {len(ids)} msgs delete try kiye")
        except Exception as e:  # noqa: BLE001
            LOGGER.warning(f"[OldMsgs] DB-ids delete error: {e!r}")

        if scan:
            now = time.time()
            if now - _LAST_SCAN.get(channel_id, 0) >= _SCAN_DEBOUNCE:
                _LAST_SCAN[channel_id] = now
                try:
                    owner_id = await _owner()
                    total += await _scan_and_delete(client, channel_id, owner_id)
                except Exception as e:  # noqa: BLE001
                    LOGGER.warning(f"[OldMsgs] scan error: {e!r}")
    return total
