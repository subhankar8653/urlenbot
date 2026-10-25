"""
direct_channel.py
=================
DIRECT FILE CHANNEL  —  auto-monitor jaisa channel, bas isme POST nahi, seedha FILE aati hai.

Setup
-----
  /set_direct -100xxxxxxxxxx     (ya channel se post forward karke reply mein /set_direct)
  /set_direct off                (band karne ke liye)
  /set_direct                    (abhi kaun sa channel set hai dekho)
  Bot is channel mein ADMIN hona chahiye.

Flow  (file pehle se Telegram pe hai => download / upload KUCH nahi, file_id se seedha send)
----
  1. Direct channel mein file aati hai, e.g. "Naruto S01E01 480p.mkv"
  2. Filename + caption se  anime (/add_anime list se match) + season + episode + quality nikalta hai
  3. Anime ke channel mein CHECK: ye (season, episode, quality) pehle se hai? -> hai to skip
  4. Nahi hai to anime ke channel mein bhejta hai:
        - cover pic  (custompic keyword / default /setpic — jo auto-monitor mein lagti hai)
        - caption    (/caption_style wala wahi caption)
     Episode 1 ki PEHLI file se pehle: season intro (details post -> border -> season sticker)
     Pichhle episode ke end/schedule messages: naye episode ki pehli file se pehle delete
  5. 480p upload ke baad kuch nahi hota — jab tak 720p aur 1080p na aa jayein.
  6. 480p + 720p + 1080p teeno aate hi (khaaskar 1080p) auto-monitor jaisa sab kuch:
        update post  ->  border line  ->  schedule / end messages
  7. Beech mein / baad mein 4K (2160p) aa jaye:
        border + end/schedule messages DELETE  ->  4K upload (1080p ke BAAD)  ->
        phir border + end/schedule messages dobara
        (update post dobara nahi jaata)
  8. Ek episode ki ek quality kabhi dobara nahi: channel scan + in-flight claim
     (auto-monitor aur direct channel dono isi guard (utils/episode_guard.py) se guzarte hain).

Order
-----
  480p -> 720p -> 1080p -> 2160p.  Agar koi file order se pehle aa gayi (jaise 720p, 480p se pehle),
  to ORDER_GRACE seconds tak neeche wali quality ka wait hota hai, phir bhi na aaye to upload ho jaati hai.
"""

import asyncio
import html
import os
import re
import time

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import Message

from .. import LOGGER, owner, sudo_users
from ..utils import episode_guard as eg
from ..utils.database.access_db import db

# ─────────────────────────────────────────────
#  Constants
# ─────────────────────────────────────────────
QUAL_ORDER = ["144p", "240p", "360p", "480p", "576p", "720p", "1080p", "1440p", "2160p"]   # chhoti -> badi
BASE_ORDER = ["480p", "720p", "1080p", "2160p"]                           # direct channel ka order
REQUIRED = ("480p", "720p", "1080p")                                      # inke aate hi episode "complete"
ORDER_GRACE = int(os.getenv("DIRECT_ORDER_GRACE", "120"))                 # seconds


def _rank(q: str) -> int:
    return QUAL_ORDER.index(q) if q in QUAL_ORDER else 99


def _is_authorized(user_id: int) -> bool:
    return user_id in owner or user_id in sudo_users


def _norm(t: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (t or "").lower())


# ─────────────────────────────────────────────
#  Direct channel id  (owner ke user doc mein)
# ─────────────────────────────────────────────
_DC_CACHE = {"ts": 0.0, "val": None}


async def _owner_id():
    return owner[0] if owner else None


async def get_direct_channel() -> int | None:
    now = time.time()
    if now - _DC_CACHE["ts"] < 20:
        return _DC_CACHE["val"]
    val = None
    oid = await _owner_id()
    if oid:
        try:
            user = await db._get_user(oid)
            v = user.get("direct_channel_id")
            val = int(v) if v else None
        except Exception as e:
            LOGGER.warning(f"[Direct] channel id read fail: {e!r}")
            return _DC_CACHE["val"]
    _DC_CACHE.update(ts=now, val=val)
    return val


async def _save_direct_channel(channel_id: int | None):
    oid = await _owner_id()
    if not oid:
        return
    if channel_id is None:
        await db.col.update_one({"id": oid}, {"$unset": {"direct_channel_id": ""}}, upsert=True)
    else:
        await db.col.update_one({"id": oid}, {"$set": {"direct_channel_id": channel_id}}, upsert=True)
    _DC_CACHE.update(ts=time.time(), val=channel_id)


# ─────────────────────────────────────────────
#  /set_direct
# ─────────────────────────────────────────────
@Client.on_message(filters.command("set_direct") & filters.private)
async def cmd_set_direct(client: Client, message: Message):
    if not _is_authorized(message.from_user.id):
        return
    from .auto_monitor import _fwd_chat

    parts = (message.text or "").split()
    if len(parts) >= 2 and parts[1].lower() in ("off", "remove", "none", "clear"):
        await _save_direct_channel(None)
        await message.reply("🛑 **Direct File Channel band kar diya.**")
        return

    channel_id, title = None, None
    fc = _fwd_chat(message.reply_to_message)
    if fc:
        channel_id, title = fc.id, fc.title
    elif len(parts) >= 2:
        try:
            channel_id = int(parts[1])
            chat = await client.get_chat(channel_id)
            title = chat.title
        except Exception as e:
            await message.reply(f"❌ Channel nahi mila: `{e}`")
            return

    if channel_id is None:
        cur = await get_direct_channel()
        if cur:
            try:
                ch = await client.get_chat(cur)
                cur_text = f"✅ **Current:** {ch.title} (`{cur}`)"
            except Exception:
                cur_text = f"✅ **Current ID:** `{cur}`"
        else:
            cur_text = "❌ Abhi set nahi hai"
        await message.reply(
            f"📥 **Direct File Channel**\n\n{cur_text}\n\n"
            f"**Kaise set karein:**\n"
            f"• Channel se koi post forward karo, reply mein `/set_direct`\n"
            f"• ya `/set_direct -100xxxxxxxxxx`\n"
            f"• band karne ke liye `/set_direct off`\n\n"
            f"Is channel mein sirf FILE bhejo (e.g. `Naruto S01E01 480p.mkv`). "
            f"Bot ko channel mein admin banana mat bhulna."
        )
        return

    await _save_direct_channel(channel_id)
    from .auto_monitor import _bot_access_line
    acc = await _bot_access_line(client, channel_id, need_post=False)
    await message.reply(
        f"✅ **Direct File Channel Set!**\n\n📢 **{title}**\n🆔 `{channel_id}`\n\n{acc}\n\n"
        f"Ab is channel mein file bhejoge to bot anime ke channel mein upload kar dega. 🚀"
    )


# ─────────────────────────────────────────────
#  Parsing
# ─────────────────────────────────────────────
_4K_RE = re.compile(r"(?<![a-z0-9])4\s*k(?![a-z0-9])", re.I)


def _quality_of(fname: str, cap: str, height: int | None) -> str | None:
    for t in (fname, cap):
        qs = eg._qualities(t)
        if qs:
            return max(qs, key=_rank)
        if t and _4K_RE.search(t):
            return "2160p"
    if height:
        for lim, q in ((1800, "2160p"), (900, "1080p"), (600, "720p"), (420, "480p"), (300, "360p")):
            if height >= lim:
                return q
    return None


def _parse_file(m: Message) -> dict | None:
    """Direct channel ki file -> info dict (ya None agar video nahi)."""
    media = m.video or m.document
    if media is None:
        return None
    kind = "video"
    if m.document and not m.video:
        mime = (m.document.mime_type or "").lower()
        name = (m.document.file_name or "").lower()
        if not (mime.startswith("video/") or name.endswith((".mkv", ".mp4", ".webm", ".avi", ".mov", ".m4v", ".ts"))):
            return None
        kind = "document"
    fname = getattr(media, "file_name", None) or ""
    cap = m.caption or ""
    fs, fe = eg.parse(fname)
    cs, ce = eg.parse(cap)
    ep = fe or ce
    season = fs or cs
    height = getattr(media, "height", None)
    return {
        "kind": kind, "file_id": media.file_id, "fname": fname, "cap": cap,
        "season": season, "ep": ep, "q": _quality_of(fname, cap, height),
        "msg_id": m.id, "chat_id": m.chat.id,
    }


# ─────────────────────────────────────────────
#  Per-episode state
# ─────────────────────────────────────────────
class _Ep:
    def __init__(self, key, anime: str, entry: dict):
        self.key = key                      # (channel_id, season, ep)
        self.anime = anime
        self.entry = entry
        self.lock = asyncio.Lock()
        self.pending: dict = {}             # quality -> info
        self.uploaded: set = set()
        self.finalized = False
        self.update_sent = False
        self.border_ids: list = []
        self.end_ids: list = []
        self.first_done = False
        self.timer = None
        self.touched = time.time()


_STATES: dict = {}
_SL = None                                  # lazy asyncio.Lock (state creation)
_INTRO_SENT: set = set()                    # (channel_id, season)
_SEEN: dict = {}                            # (chat, msg) -> ts


def _pid(key) -> str:
    return f"dstate_{key[0]}_{key[1]}_{key[2]}"


async def _persist(st: _Ep):
    try:
        await db.col2.update_one(
            {"id": _pid(st.key)},
            {"$set": {"finalized": st.finalized, "update_sent": st.update_sent,
                      "border_ids": st.border_ids, "end_ids": st.end_ids, "ts": time.time()}},
            upsert=True,
        )
    except Exception as e:
        LOGGER.warning(f"[Direct] state persist fail: {e!r}")


async def _get_state(key, anime: str, entry: dict) -> _Ep:
    global _SL
    if _SL is None:
        _SL = asyncio.Lock()
    async with _SL:
        now = time.time()
        for k in [k for k, s in _STATES.items() if now - s.touched > 24 * 3600 and not s.pending]:
            _STATES.pop(k, None)
        st = _STATES.get(key)
        if st is None:
            st = _Ep(key, anime, entry)
            try:   # restart ke baad bhi 4K aaye to border/end msgs ka hisaab mile
                d = await db.col2.find_one({"id": _pid(key)}) or {}
                st.finalized = bool(d.get("finalized"))
                st.update_sent = bool(d.get("update_sent"))
                st.border_ids = list(d.get("border_ids") or [])
                st.end_ids = list(d.get("end_ids") or [])
                if st.finalized:
                    eg.mark_complete(*key)
            except Exception:
                pass
            _STATES[key] = st
        st.touched = now
        return st


# ─────────────────────────────────────────────
#  Notify owner (DM)
# ─────────────────────────────────────────────
async def _notify(client, text: str, important: bool = True):
    """important=True -> hamesha DM (fail / match nahi hua). False -> sirf notify level 'all' mein."""
    try:
        from .auto_monitor import _notify_owner, _get_notify_level
        if not important and (await _get_notify_level()) != "all":
            return
        await _notify_owner(client, text)
    except Exception as e:
        LOGGER.warning(f"[Direct] notify fail: {e!r}")


# ─────────────────────────────────────────────
#  Entry point  (handler)
# ─────────────────────────────────────────────
@Client.on_message(filters.channel & (filters.video | filters.document), group=-6)
async def direct_channel_handler(client: Client, message: Message):
    try:
        dc = await get_direct_channel()
        if not dc or message.chat.id != dc:
            return
        k = (message.chat.id, message.id)
        now = time.time()
        if k in _SEEN:
            return
        _SEEN[k] = now
        if len(_SEEN) > 400:
            for kk in sorted(_SEEN, key=_SEEN.get)[:200]:
                _SEEN.pop(kk, None)
        await _on_file(client, message)
    except Exception as e:
        import traceback
        LOGGER.error(f"[Direct] handler crash: {e}\n{traceback.format_exc()}")
        await _notify(client, f"💥 <b>Direct channel error</b>\n<code>{html.escape(type(e).__name__)}: "
                              f"{html.escape(str(e)[:300])}</code>")


async def _on_file(client: Client, m: Message):
    info = _parse_file(m)
    if info is None:
        return
    from .auto_monitor import _get_anime_list, _find_matching_anime, _season_hint_from_text

    fname, cap = info["fname"], info["cap"]
    label = html.escape(fname or cap[:60] or "file")

    if not info["ep"]:
        await _notify(client, f"⚠️ <b>Direct channel</b>: episode number nahi mila\n<code>{label}</code>")
        return
    if not info["q"]:
        await _notify(client, f"⚠️ <b>Direct channel</b>: quality nahi mili (480p/720p/1080p/4K)\n<code>{label}</code>")
        return

    anime_list = await _get_anime_list()
    entry = _find_matching_anime(f"{fname} {cap}", anime_list)
    if not entry:
        await _notify(client, f"⚠️ <b>Direct channel</b>: <code>/add_anime</code> list mein match nahi mila\n<code>{label}</code>")
        return

    season = info["season"] or _season_hint_from_text(fname, cap) or 1
    ep, q = int(info["ep"]), info["q"]
    target = int(entry["channel_id"])
    st = await _get_state((target, season, ep), entry["anime_name"], entry)

    if q in st.pending or q in st.uploaded:
        LOGGER.info(f"[Direct] S{season:02d}E{ep:02d} {q}: dobara aayi — skip")
        await _notify(client, f"♻️ Direct: <b>{html.escape(entry['anime_name'])}</b> S{season:02d}E{ep:02d} "
                              f"<code>{q}</code> pehle hi aa chuki hai — skip", important=False)
        return

    LOGGER.info(f"[Direct] ✅ {entry['anime_name']} S{season:02d}E{ep:02d} {q} -> channel {target}")
    st.pending[q] = info
    await _drain(client, st)


# ─────────────────────────────────────────────
#  Drain: ready files upload -> phir finalize
# ─────────────────────────────────────────────
def _ready(q: str, have: set, force: bool) -> bool:
    if force:
        return True
    return all(b in have for b in BASE_ORDER if _rank(b) < _rank(q))


async def _present(client, st: _Ep, wanted) -> set:
    ch, season, ep = st.key
    try:
        oid = await _owner_id()
        pres, _noq, _ok = await eg.present_qualities(client, ch, oid, season, ep, list(wanted))
        return set(pres)
    except Exception as e:
        LOGGER.warning(f"[Direct] present check error (fail-open): {e!r}")
        return set()


async def _drain(client, st: _Ep, force: bool = False):
    async with st.lock:
        if st.timer is not None:
            try:
                st.timer.cancel()
            except Exception:
                pass
            st.timer = None
        while st.pending:
            have = st.uploaded | await _present(client, st, BASE_ORDER)
            q = min(st.pending, key=_rank)
            if not _ready(q, have, force):
                break
            info = st.pending.pop(q)
            await _upload_item(client, st, q, info)
        await _maybe_finalize(client, st)
        if st.pending and not force:
            loop = asyncio.get_event_loop()
            st.timer = loop.call_later(
                ORDER_GRACE, lambda: asyncio.ensure_future(_drain(client, st, force=True)))


# ─────────────────────────────────────────────
#  Upload one
# ─────────────────────────────────────────────
async def _delete_ids(client, ch: int, ids: list):
    ids = [i for i in ids if i]
    if not ids:
        return
    try:
        await client.delete_messages(ch, ids)
    except Exception:
        for i in ids:
            try:
                await client.delete_messages(ch, i)
                await asyncio.sleep(0.2)
            except Exception:
                pass


async def _delete_old_bot_msgs(client, ch: int):
    """Pichhle episode ke end/schedule messages (naye episode ki pehli file se pehle)."""
    try:
        from .schedule_notify import get_last_posted_msg_ids, clear_last_posted_msg_ids
        ids = await get_last_posted_msg_ids(ch)
        if not ids:
            return
        await _delete_ids(client, ch, ids)
        await clear_last_posted_msg_ids(ch)
    except Exception as e:
        LOGGER.warning(f"[Direct] old msgs delete error: {e!r}")


async def _make_caption(st: _Ep, info: dict, q: str) -> str:
    from ..utils import caption_style
    from ..utils.auto_caption import build_auto_caption, get_caption_data
    from ..utils.community import get_community_tag

    ch, season, ep = st.key
    uid = (await _owner_id()) or 0
    fname = info["fname"] or f"{st.anime} S{season:02d}E{ep:02d} {q}.mp4"
    tag = await get_community_tag(uid, all_names=True)

    def _default():
        cap = build_auto_caption(fname, resolution=q.replace("p", ""), channel=tag)
        proper = re.sub(r'[<>:"/\\|?*]', "", cap).strip()
        return f"<b>{html.escape(proper)}</b>"

    style_id = caption_style.get_current_style_id()
    if style_id == caption_style.DEFAULT_STYLE_ID:
        return _default()
    try:
        data = get_caption_data(None, fname, channel=tag)
        data["quality"] = q
        data["season"], data["episode"] = season, ep
        nm = data.get("anime_name") or ""
        if not nm or (_norm(st.anime) not in _norm(nm) and _norm(nm) not in _norm(st.anime)):
            data["anime_name"] = st.anime
        data["genres"] = await caption_style.lookup_genres(data["anime_name"])
        return caption_style.render_caption_html(style_id, data)
    except Exception as e:
        LOGGER.warning(f"[Direct] styled caption fail, default use: {e!r}")
        return _default()


async def _cover_for(info: dict):
    """custompic keyword (naam se) -> warna default /setpic. Photo file_id (instant, upload nahi)."""
    try:
        from .custompic import get_custompic_for_file
        uid = (await _owner_id()) or 0
        text = f"{info['fname']} {info['cap']}".strip()
        pic = await get_custompic_for_file(uid, text)
        if pic:
            return pic
        return await db.get_thumbnail(uid)
    except Exception as e:
        LOGGER.warning(f"[Direct] cover lookup fail: {e!r}")
        return None


async def _send(client, ch: int, info: dict, caption: str, cover):
    from pyrogram.errors import FloodWait
    for attempt in (1, 2):
        try:
            if info["kind"] == "video":
                from ..utils.uploads.telegram import _send_video_cover_safe
                kw = dict(chat_id=ch, video=info["file_id"], caption=caption,
                          parse_mode=ParseMode.HTML, supports_streaming=True)
                if cover:
                    kw["cover"] = cover
                try:
                    return await _send_video_cover_safe(client.send_video, **kw)
                except FloodWait:
                    raise
                except Exception as e:
                    if cover:      # cover problem ho to bina cover bhej do — upload nahi rukna chahiye
                        LOGGER.warning(f"[Direct] cover ke saath send fail ({e!r}) — bina cover retry")
                        kw.pop("cover", None)
                        return await _send_video_cover_safe(client.send_video, **kw)
                    raise
            return await client.send_document(ch, info["file_id"], caption=caption, parse_mode=ParseMode.HTML)
        except FloodWait as fw:
            if attempt == 2:
                raise
            await asyncio.sleep(int(getattr(fw, "value", 5)) + 1)
        except Exception as e:
            LOGGER.warning(f"[Direct] send fail ({e!r}) — copy_message fallback")
            return await client.copy_message(ch, info["chat_id"], info["msg_id"],
                                             caption=caption, parse_mode=ParseMode.HTML)


async def _upload_item(client, st: _Ep, q: str, info: dict):
    ch, season, ep = st.key
    tag = f"{st.anime} S{season:02d}E{ep:02d} {q}"

    # 1) auto-monitor isi quality ko abhi upload kar raha ho to skip
    if not eg.claim(ch, season, ep, q):
        LOGGER.info(f"[Direct] ⏭️ {tag}: koi aur upload kar raha hai — skip")
        await _notify(client, f"⏭️ Direct: <b>{html.escape(st.anime)}</b> S{season:02d}E{ep:02d} "
                              f"<code>{q}</code> abhi doosre process se upload ho rahi hai — skip", important=False)
        return
    try:
        # 2) channel mein pehle se hai?
        pres = await _present(client, st, [q])
        if q in pres:
            LOGGER.info(f"[Direct] ⏭️ {tag}: channel mein pehle se hai — skip")
            await _notify(client, f"⏭️ Direct: <b>{html.escape(st.anime)}</b> S{season:02d}E{ep:02d} "
                                  f"<code>{q}</code> channel mein pehle se hai — skip", important=False)
            return

        # 3) Naya upload aaya aur episode "finalized" tha (4K / late file) -> border + end msgs hatao
        if st.finalized:
            await _undo_finalize(client, st)

        # 4) Episode ki PEHLI file: purane end msgs hatao + (Ep 1) season intro
        if not st.first_done:
            others = await _present(client, st, [b for b in BASE_ORDER if b != q])
            if not st.uploaded and not others:
                await _delete_old_bot_msgs(client, ch)
                if ep == 1 and (ch, season) not in _INTRO_SENT:
                    _INTRO_SENT.add((ch, season))
                    try:
                        from .auto_monitor import _send_season_intro
                        await _send_season_intro(client, ch, st.anime, season)
                    except Exception as e:
                        LOGGER.error(f"[Direct] season intro error: {e!r}")
            st.first_done = True

        # 5) caption + cover, phir send
        caption, cover = await asyncio.gather(_make_caption(st, info, q), _cover_for(info))
        sent = await _send(client, ch, info, caption, cover)
        if sent is None:
            raise RuntimeError("send returned None")
        st.uploaded.add(q)
        eg.record_upload(ch, season, ep, q)
        LOGGER.info(f"[Direct] 📤 {tag} uploaded (msg {getattr(sent, 'id', '?')})")
    except Exception as e:
        LOGGER.error(f"[Direct] upload fail {tag}: {e!r}")
        await _notify(client, f"❌ <b>Direct upload fail</b>\n🎬 {html.escape(st.anime)} S{season:02d}E{ep:02d} "
                              f"<code>{q}</code>\n<code>{html.escape(str(e)[:200])}</code>\n\n"
                              f"Bot ko anime channel mein admin + Post permission chahiye.")
    finally:
        eg.release(ch, season, ep, q)


# ─────────────────────────────────────────────
#  Finalize / undo
# ─────────────────────────────────────────────
async def _maybe_finalize(client, st: _Ep):
    if st.finalized or st.pending or not st.uploaded:
        return
    have = st.uploaded | await _present(client, st, BASE_ORDER)
    if not all(q in have for q in REQUIRED):
        return
    await _finalize(client, st)


async def _finalize(client, st: _Ep):
    ch, season, ep = st.key
    # 0) auto-monitor ko signal: is episode ko ab chhodo
    eg.mark_complete(ch, season, ep)

    # 1) update post (sirf ek baar — 4K re-finalize mein dobara nahi)
    if not st.update_sent:
        try:
            from .update_channel import send_update_post
            await send_update_post(client, anime_name=st.anime, season=season,
                                   episode_start=ep, episode_end=ep)
        except Exception as e:
            LOGGER.error(f"[Direct] update post error: {e!r}")
        st.update_sent = True

    # 2) border line
    st.border_ids = []
    try:
        from .auto_monitor import _get_deco, _send_deco_item
        border, _ = await _get_deco()
        if border:
            bm = await _send_deco_item(client, ch, border)
            if bm is not None:
                st.border_ids = [bm.id]
    except Exception as e:
        LOGGER.error(f"[Direct] border error: {e!r}")

    # 3) schedule / end messages
    st.end_ids = []
    try:
        from .schedule_notify import send_schedule_notification, get_last_posted_msg_ids
        await send_schedule_notification(client, ch, st.anime, ep)
        st.end_ids = list(await get_last_posted_msg_ids(ch) or [])
    except Exception as e:
        LOGGER.error(f"[Direct] schedule/end msg error: {e!r}")

    st.finalized = True
    await _persist(st)
    LOGGER.info(f"[Direct] 🎉 {st.anime} S{season:02d}E{ep:02d} complete")
    await _notify(client, f"🎉 Direct: <b>{html.escape(st.anime)}</b> S{season:02d}E{ep:02d} complete "
                          f"({' → '.join(sorted(st.uploaded, key=_rank))})", important=False)


async def _undo_finalize(client, st: _Ep):
    ch, season, ep = st.key
    await _delete_ids(client, ch, list(st.border_ids) + list(st.end_ids))
    try:   # DB mein sirf tab saaf karo jab wahi ids saved hon (naye episode ke end msgs na ud jayein)
        from .schedule_notify import get_last_posted_msg_ids, clear_last_posted_msg_ids
        cur = await get_last_posted_msg_ids(ch)
        if cur and (not st.end_ids or set(cur) == set(st.end_ids)):
            await clear_last_posted_msg_ids(ch)
    except Exception as e:
        LOGGER.warning(f"[Direct] undo clear fail: {e!r}")
    st.border_ids, st.end_ids, st.finalized = [], [], False
    await _persist(st)
    LOGGER.info(f"[Direct] ↩️ {st.anime} S{season:02d}E{ep:02d}: border + end msgs hata diye (naya upload aa raha hai)")
