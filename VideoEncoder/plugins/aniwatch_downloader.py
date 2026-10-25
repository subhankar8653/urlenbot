"""
aniwatch_downloader.py  v1
==========================
Aniwatch / Animetsu se seedha download + urlenbot ke apne upload pipeline se upload
(AniwatchTvdl / Cantarella bot ke scrapers ka port — VideoEncoder/utils/aniwatch/).

Commands:
  /aniwatch <anime ka naam>            -> search -> anime chuno -> episode buttons
  /aniwatch <series_url>               -> seedha episode buttons
  /aniwatch <series_url> 3-8           -> ep 3 se 8 turant download+upload (default quality)
  /aniwatch <series_url> 3-8 1080      -> same, 1080p mein   (360 / 720 / 1080 / all)
  (short alias: /aniw)

Do sources (dono megaplay player ke peeche):
  🍥 Aniwatch -> animesalt.to      (default)
  🍥 Animetsu -> anikototv.to      (search ke card pe "Source" button se badlo)

Flow (toono_downloader.py jaisa hi, taaki sab kuch familiar rahe):
  - Episode buttons: single tap = turant download, "Select Multiple" = multi-select
  - Quality: 360p / 720p / 1080p / All  (jo CDN pe actually hai wahi milta hai —
    kisi site pe sirf 1080p ho to 360p maangne par nearest quality aati hai, aur
    filename/caption mein asli quality likhi hoti hai)
  - Download: N_m3u8DL-RE + ffmpeg mux (sub + dub + subtitles -> MP4/mov_text)
  - Upload: swift_downloader._upload_one_file (wahi thumbnail / custompic / caption
    style / auto-rename / quota / ❌ Cancel jo /swift, /toono mein hai)
  - "Update Post" aur "Channel Upload" toggles — /toono jaise, apne alag field ke saath

Env (sab optional):
  ANIWATCH_SOURCE   aniwatch | animetsu   (default aniwatch)
  ANIWATCH_QUALITY  360 | 720 | 1080 | all (default 720)
  ANIWATCH_PROXY    http://user:pass@host:port
  ANIWATCH_FORMAT   filename template, default "{title} S{season:0>2}E{episode:0>2} [{audio}] [{quality}]"

NOTE: N_m3u8DL-RE binary chahiye (Dockerfile mein install hota hai). Scrapers live
sites ke HTML/ajax pe depend karte hain — site ka markup badla to sirf
VideoEncoder/utils/aniwatch/{animesalt,anikoto}.py theek karne padenge.
"""

import asyncio
import os
import queue as _queue
import re
import shutil
import time
import uuid

from pyrogram import Client, filters
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from .. import LOGGER, download_dir
from ..utils import job_ctl
from ..utils.aniwatch import (
    SOURCE_LABELS,
    detect_source,
    get_binary_path,
    get_scraper,
    is_url,
    make_progress_bar,
    resolve_source,
)
from ..utils.aniwatch.base import season_of
from ..utils.database.access_db import db
from ..utils.helper import check_chat
from .swift_downloader import (
    _new_status,
    _prewarm_all_custompics,
    _quality_from,
    _sort_by_size,
    _upload_one_file,
)

PER_PAGE = 30           # ek page pe episode buttons
COLS = 5                # ek row mein episode buttons
SEARCH_LIMIT = 15       # search results (1 button / row)
SESSION_TIMEOUT = 3600  # 1 hour (in-memory, redeploy pe clear)

_DEFAULT_SOURCE = resolve_source(os.getenv("ANIWATCH_SOURCE", "aniwatch"))
_DEFAULT_QUALITY = os.getenv("ANIWATCH_QUALITY", "720").lower().replace("p", "")
if _DEFAULT_QUALITY not in ("360", "720", "1080", "all"):
    _DEFAULT_QUALITY = "720"

QUALITIES = [("360", "360p"), ("720", "720p"), ("1080", "1080p"), ("all", "All")]
_QUALITY_KEYS = {k for k, _ in QUALITIES}

# Language row (dual eng-jap sites): jpn = sirf Japanese (sub), eng = sirf English (dub),
# all = dono audio ek file mein (Dual Audio) — purana default behaviour.
LANGUAGES = [("jpn", "Japanese"), ("eng", "English"), ("all", "All")]
_LANG_KEYS = {k for k, _ in LANGUAGES}
_LANG_LONG = {"jpn": "Japanese (Sub)", "eng": "English (Dub)", "all": "All (Dual Audio)"}

ANIW_SESSIONS = {}      # sid -> AniwSession


# ─────────────────────────────────────────────
#  Small helpers
# ─────────────────────────────────────────────
def _kb(rows):
    """rows = list of list of (text, callback_data)"""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(t, callback_data=d) for t, d in row] for row in rows]
    )


def _md_safe(text: str) -> str:
    """Telegram markdown todne wale chars hata do (title header mein jaata hai)."""
    return re.sub(r"[*_`~\[\]]", "", text or "").strip()


def _clip(text: str, n: int) -> str:
    text = text or ""
    return text if len(text) <= n else text[: n - 1] + "…"


def _prune_sessions():
    now = time.time()
    for k in [k for k, v in ANIW_SESSIONS.items() if now - v.created > SESSION_TIMEOUT]:
        ANIW_SESSIONS.pop(k, None)


def _other_source(source: str) -> str:
    return "anikoto" if resolve_source(source) == "animesalt" else "animesalt"


def split_title_season(title: str):
    """
    'Frieren: Beyond Journey's End Season 2' -> ("Frieren: Beyond Journey's End", "2")
    '2nd Season' / 'Part 2' / trailing ' 2' bhi. Season na mile to ("<title>", "1").

    Filename mein season alag (S02E05) jaata hai, isliye naam se season-token hatana
    zaroori hai — warna urlenbot ka auto-caption "… Season 2 S02E05" bana dega.
    """
    title = (title or "").strip()
    season = season_of(title)
    if season == "1":
        return title, "1"
    patterns = (
        r"\s*\bSeason\s+{n}\b",
        r"\s*\b{n}(?:st|nd|rd|th)\s+Season\b",
        r"\s*\bPart\s+{n}\b",
        r"\s*\b{n}(?:st|nd|rd|th)\s+Part\b",
        r"\s+{n}$",
    )
    for pat in patterns:
        new = re.sub(pat.format(n=re.escape(season)), "", title, count=1, flags=re.IGNORECASE)
        if new != title:
            title = new
            break
    return title.strip(" -:–") or title, season


def _ep_int(item: dict) -> int:
    try:
        return int(item.get("ep_number") or 0)
    except (TypeError, ValueError):
        return 0


# ─────────────────────────────────────────────
#  "Channel Upload" toggle — per user (toono ke toono_channel_upload jaisa,
#  alag field taaki Toono/RTI/Aniwatch ka state independent rahe)
# ─────────────────────────────────────────────
async def _get_channel_upload(user_id: int) -> bool:
    user = await db._get_user(user_id)
    return user.get("aniwatch_channel_upload", False)


async def _set_channel_upload(user_id: int, enabled: bool):
    await db.col.update_one(
        {"id": int(user_id)}, {"$set": {"aniwatch_channel_upload": enabled}}, upsert=True
    )


# ─────────────────────────────────────────────
#  Blocking scraper calls (thread mein chalte hain)
# ─────────────────────────────────────────────
def _search_blocking(source: str, query: str) -> list:
    return get_scraper(source, download_path=download_dir).search_anime(query, limit=SEARCH_LIMIT)


def _episodes_blocking(source: str, url: str) -> list:
    return get_scraper(source, download_path=download_dir).list_episodes(url)


# ─────────────────────────────────────────────
#  Session — search results view  ->  episode view
# ─────────────────────────────────────────────
class AniwSession:
    def __init__(self, orig_message: Message, source: str, query: str = ""):
        self.orig_message = orig_message
        self.source = resolve_source(source)
        self.query = query
        self.sid = uuid.uuid4().hex[:10]
        self.msg = None
        self.created = time.time()

        self.view = "results"            # "results" | "episodes"
        self.results = []                # [{"title","id","type","url"}]
        self.series_title = ""
        self.series_url = ""
        self.episodes = []               # list_episodes() items
        self.page = 0
        self.multi = False
        self.selected = set()
        self.quality = _DEFAULT_QUALITY
        self.audio = "all"               # Language row: jpn | eng | all
        self.busy = False                # search / episode-load chal raha hai
        self.update_toggle = False       # session-local, /updatechannel se independent
        self.channel_toggle = False

    # ── derived ──
    @property
    def source_label(self):
        return SOURCE_LABELS.get(self.source, self.source)

    @property
    def total_pages(self):
        return max(1, (len(self.episodes) + PER_PAGE - 1) // PER_PAGE)

    def page_items(self):
        start = self.page * PER_PAGE
        return list(enumerate(self.episodes))[start:start + PER_PAGE]

    @property
    def quality_label(self):
        return dict(QUALITIES).get(self.quality, self.quality)

    @property
    def audio_label(self):
        return _LANG_LONG.get(self.audio, self.audio)

    async def populate_toggles(self):
        try:
            self.channel_toggle = await _get_channel_upload(self.orig_message.from_user.id)
        except Exception as e:
            LOGGER.warning(f"[Aniwatch] toggle load failed: {e}")

    # ── text ──
    def header_text(self):
        if self.view == "results":
            lines = [
                "🔎 **Aniwatch Search**",
                "━━━━━━━━━━━━━━━━━━",
                f"🔍 Query: `{_md_safe(_clip(self.query, 60))}`",
                f"🌐 Source: 🍥 {self.source_label}",
            ]
            if self.busy:
                lines.append("\n⏳ Search ho raha hai...")
            elif self.results:
                lines.append(f"📚 Results: {len(self.results)}")
                lines.append("━━━━━━━━━━━━━━━━━━")
                lines.append("👇 Anime chuno:")
            else:
                lines.append("━━━━━━━━━━━━━━━━━━")
                lines.append("❌ Kuch nahi mila. Spelling badlo ya Source button se doosri site try karo.")
            return "\n".join(lines)

        lines = [
            "📂 **Select Episode**",
            "━━━━━━━━━━━━━━━━━━",
            f"🎬 **{_md_safe(self.series_title)}**",
            f"🌐 Source: 🍥 {self.source_label}",
            f"📖 Episodes: {len(self.episodes)} • 🎚 Quality: {self.quality_label}",
            f"🗣 Language: {self.audio_label}",
            "━━━━━━━━━━━━━━━━━━",
            f"👇 Tap an episode to download:  (Page {self.page + 1}/{self.total_pages})",
        ]
        if self.multi:
            lines.append(f"\n☑️ **Multi-select ON** — chosen: `{len(self.selected)}`")
        return "\n".join(lines)

    # ── buttons ──
    def build_markup(self):
        sid = self.sid
        rows = []

        if self.view == "results":
            if not self.busy:
                for i, r in enumerate(self.results):
                    kind = f" • {r['type']}" if r.get("type") and r["type"] != "ANIME" else ""
                    rows.append([(_clip(r["title"], 52) + kind, f"aniw_pick_{sid}_{i}")])
            if self.query:
                other = SOURCE_LABELS[_other_source(self.source)]
                rows.append([(f"🌐 Source: {self.source_label} ⇄ {other}", f"aniw_src_{sid}")])
            rows.append([("❌ Close", f"aniw_close_{sid}")])
            return _kb(rows)

        # episode view
        row = []
        for idx, item in self.page_items():
            mark = "✅" if idx in self.selected else ""
            row.append((f"{mark}{_ep_int(item):02d}", f"aniw_ep_{sid}_{idx}"))
            if len(row) == COLS:
                rows.append(row)
                row = []
        if row:
            rows.append(row)

        if self.total_pages > 1:
            nav = []
            if self.page > 0:
                nav.append(("◀️ Prev", f"aniw_pg_{sid}_{self.page - 1}"))
            nav.append((f"📄 {self.page + 1}/{self.total_pages}", f"aniw_noop_{sid}"))
            if self.page < self.total_pages - 1:
                nav.append(("Next ▶️", f"aniw_pg_{sid}_{self.page + 1}"))
            rows.append(nav)

        rows.append([
            ((("✅ " if self.quality == k else "") + label), f"aniw_q_{sid}_{k}")
            for k, label in QUALITIES
        ])
        rows.append([
            ((("✅ " if self.audio == k else "") + label), f"aniw_lg_{sid}_{k}")
            for k, label in LANGUAGES
        ])

        if self.multi:
            rows.append([(f"⬇️ Download Selected ({len(self.selected)})", f"aniw_dl_{sid}")])
            rows.append([
                ("📄 Select Page", f"aniw_selpage_{sid}"),
                ("📚 Select All", f"aniw_selall_{sid}"),
                ("🧹 Clear", f"aniw_selclr_{sid}"),
            ])
            rows.append([("☑️ Multi-select: ON (tap to turn off)", f"aniw_toggle_{sid}")])
        else:
            rows.append([("☑️ Select Multiple", f"aniw_toggle_{sid}")])

        if self.results:
            rows.append([("🔙 Back to Results", f"aniw_back_{sid}")])

        rows.append([(f"📢 Update Post: {'ON' if self.update_toggle else 'OFF'}", f"aniw_uptog_{sid}")])
        rows.append([(f"📤 Channel Upload: {'ON' if self.channel_toggle else 'OFF'}", f"aniw_chtog_{sid}")])
        rows.append([("❌ Close", f"aniw_close_{sid}")])
        return _kb(rows)

    async def render(self):
        if not self.msg:
            return
        try:
            await self.msg.edit(self.header_text(), reply_markup=self.build_markup())
        except Exception as e:
            if "MESSAGE_NOT_MODIFIED" not in str(e):
                LOGGER.error(f"[Aniwatch] render error: {e}")


# ─────────────────────────────────────────────
#  Channel forward + update post (toono jaisa)
# ─────────────────────────────────────────────
async def _forward_to_channel_if_enabled(client, message: Message, uploaded_results):
    if not uploaded_results:
        return
    try:
        user_id = message.from_user.id
        if not await _get_channel_upload(user_id):
            return
        channels = await db.get_channels(user_id)
        if not channels:
            return
        target_id = channels[0].get("channel_id")
        if not target_id:
            return
        for _quality, sent_msg in uploaded_results:
            try:
                await client.copy_message(
                    chat_id=target_id, from_chat_id=sent_msg.chat.id, message_id=sent_msg.id
                )
            except Exception as e:
                LOGGER.error(f"[Aniwatch] Channel copy error: {e}")
    except Exception as e:
        LOGGER.error(f"[Aniwatch] _forward_to_channel_if_enabled error: {e}")


async def _send_update_post_if_enabled(client, sess: AniwSession, item: dict, uploaded_results):
    if not sess or not sess.update_toggle or not uploaded_results:
        return
    try:
        from .update_channel import send_update_post
        name, season = split_title_season(sess.series_title)
        await send_update_post(
            client,
            anime_name=name,
            season=int(season),
            episode=_ep_int(item),
            force=True,
        )
    except Exception as e:
        LOGGER.error(f"[Aniwatch] Update post error: {e}")


# ─────────────────────────────────────────────
#  Ek episode: scrape + download (thread) -> files
# ─────────────────────────────────────────────
async def _download_episode_files(sess: AniwSession, item: dict, dl_dir: str, stage,
                                  ep_label: str, index: int, total: int):
    """
    Returns (files: list[str], error: str | None).
    Scraper blocking hai -> thread; progress queue har ~3s drain hoti hai aur
    card ke 'stage' slot mein bar dikhta hai.
    """
    job = job_ctl.CURRENT.get()
    pq = _queue.Queue()
    name, season = split_title_season(sess.series_title)

    scraper = get_scraper(sess.source, download_path=dl_dir, progress_queue=pq)
    if job is not None:
        scraper.cancel_event = job.cancel_ev       # ❌ Cancel -> N_m3u8DL-RE kill

    box = {"files": [], "err": None, "status": "", "prog": None}

    def _drain():
        while True:
            try:
                p = pq.get_nowait()
            except _queue.Empty:
                return
            if p.get("finished"):
                box["files"].append(p.get("filename"))
            elif p.get("error"):
                box["err"] = p["error"]
            elif p.get("status"):
                box["status"] = p["status"]
            elif p.get("percent") is not None:
                box["prog"] = p

    def _text():
        head = f"⬇️ **{ep_label}** (`{index}/{total}`)"
        prog = box["prog"]
        if prog:
            try:
                pct = float(str(prog.get("percent", "0")).rstrip("%"))
            except ValueError:
                pct = 0.0
            track = "Dub" if prog.get("type") == "dub" else "Sub"
            return (
                f"{head} • {track}\n"
                f"{make_progress_bar(pct)}\n"
                f"🚀 {prog.get('speed', '0 MB/s')} • {prog.get('downloaded', '')}/{prog.get('total', '')}"
            )
        status = re.sub(r"\*\*", "", box["status"]).strip()
        return f"{head}\n{status or 'Link nikal raha hoon...'}"

    await stage.edit(_text())
    fut = job_ctl.run_in_thread(
        scraper.download_episode,
        item["url"],
        quality=sess.quality,
        name_override=name,
        season_override=season,
        ep_num_override=str(item.get("ep_number") or ""),
        audio=sess.audio,
    )

    ok = False
    try:
        while not fut.done():
            await asyncio.wait({fut}, timeout=3)
            _drain()
            await stage.edit(_text())
        ok = bool(fut.result())
    except asyncio.CancelledError:
        raise
    except Exception as e:
        box["err"] = box["err"] or f"{type(e).__name__}: {e}"
    finally:
        _drain()

    files = [f for f in box["files"] if f and os.path.isfile(f)]
    if not files:
        return [], box["err"] or ("download fail" if not ok else "file nahi bani")
    return files, None


# ─────────────────────────────────────────────
#  Ek episode: download + upload
# ─────────────────────────────────────────────
async def _process_item(client, message, sess: AniwSession, item: dict, stage, index: int, total: int):
    ep_label = f"E{_ep_int(item):02d}"
    dl_dir = os.path.join(download_dir, f"aniw_{sess.sid}_{_ep_int(item)}_{int(time.time())}")
    os.makedirs(dl_dir, exist_ok=True)

    job = job_ctl.CURRENT.get()
    if job is not None:
        job.dirs.add(dl_dir)          # cancel / finish pe folder saaf

    try:
        files, err = await _download_episode_files(sess, item, dl_dir, stage, ep_label, index, total)
        if not files:
            await stage.edit(
                f"🛑 **{ep_label} — Download Fail**\n"
                f"❌ `{_clip(err or 'unknown', 150)}`\n"
                f"⛔ Agle episodes **band** kar diye gaye."
            )
            return "error"

        files = _sort_by_size(files)     # chhoti quality pehle
        await stage.edit(f"✅ **{ep_label}** (`{index}/{total}`) — download ho gaya\n📤 Upload shuru...")

        uploaded = []
        for fp in files:
            q = _quality_from(os.path.basename(fp))
            slot = await _new_status(message, f"📤 **Uploading `{q}`...**", q)
            success, sent_msg, quality = await _upload_one_file(
                client, message, slot, fp, dl_dir, False, label_prefix=ep_label
            )
            if success:
                uploaded.append((quality, sent_msg))
                try:
                    await slot.delete()
                except Exception:
                    pass
            else:
                try:
                    await slot.edit(f"❌ Upload failed: `{quality}`")
                except Exception:
                    pass

        if not uploaded:
            await stage.edit(f"🛑 **{ep_label} — Upload Fail**\n⛔ Agle episodes **band** kar diye gaye.")
            return "error"

        uploaded_with_msgs = [(q, m) for q, m in uploaded if m is not None]
        await _forward_to_channel_if_enabled(client, message, uploaded_with_msgs)
        await _send_update_post_if_enabled(client, sess, item, uploaded_with_msgs or uploaded)
        return "ok"

    except asyncio.CancelledError:
        raise
    except Exception as e:
        LOGGER.error(f"[Aniwatch] {ep_label} error: {e}", exc_info=True)
        try:
            await stage.edit(
                f"🛑 **{ep_label} — Error**\n❌ `{_clip(str(e), 150)}`\n⛔ Agle episodes **band** kar diye gaye."
            )
        except Exception:
            pass
        return "error"
    finally:
        shutil.rmtree(dl_dir, ignore_errors=True)


async def _download_items(client, status_msg, orig_message: Message, sess: AniwSession, idxs: list):
    """Poora process EK card message mein + ❌ Cancel button (job_ctl)."""
    items = [sess.episodes[i] for i in idxs]
    total = len(items)
    title = _clip(_md_safe(sess.series_title) or "Aniwatch", 55)
    job = job_ctl.Job(status_msg, orig_message.from_user.id, title=title)
    job.quota_count = total
    state = {"ok": 0, "last": "ok"}

    async def _body():
        stage = job.slot("stage", order=0)
        asyncio.ensure_future(_prewarm_all_custompics(orig_message.from_user.id))
        for i, item in enumerate(items, 1):
            label = f"E{_ep_int(item):02d}"
            job.header = f"📺 **{label}** • `{i}/{total}`"
            job.reset_slots(keep=("stage",))
            result = await _process_item(client, orig_message, sess, item, stage, i, total)
            state["last"] = result
            if result != "ok":
                break
            state["ok"] += 1
            job.done_eps.append(label)
            if i < total:
                await asyncio.sleep(1)

    try:
        await job.run(_body)
    except Exception as e:
        LOGGER.error(f"[Aniwatch] _download_items error: {e}")
        state["last"] = "error"
        job.slot("stage", order=0).text = f"❌ `{_clip(str(e), 150)}`"

    if job.cancelled:
        return
    job.reset_slots(keep=("stage",))
    if state["last"] == "ok":
        await job.finish(
            f"🎉 **Complete!** — {job.title}\n\n"
            f"✅ `{state['ok']}/{total}` upload ho gaye\n"
            f"📋 {' '.join(job.done_eps[-12:])}"
        )
    else:
        job.finished = False
        await job.finish(job.render() + f"\n\n⛔ Process band ho gaya (`{state['ok']}/{total}` complete).")


# ─────────────────────────────────────────────
#  Loaders (search / episodes) — session ko update karte hain
# ─────────────────────────────────────────────
async def _do_search(sess: AniwSession):
    sess.busy = True
    await sess.render()
    try:
        sess.results = await job_ctl.run_in_thread(_search_blocking, sess.source, sess.query) or []
    except Exception as e:
        LOGGER.error(f"[Aniwatch] search error ({sess.source}): {e}")
        sess.results = []
    finally:
        sess.busy = False
    sess.view = "results"
    await sess.render()


async def _load_episodes(sess: AniwSession, series_url: str, title: str = ""):
    """Returns (ok: bool, error_text | None). ok=True -> view 'episodes' pe aa gaya."""
    try:
        episodes = await job_ctl.run_in_thread(_episodes_blocking, sess.source, series_url)
    except Exception as e:
        LOGGER.error(f"[Aniwatch] list_episodes error: {e}")
        return False, f"{type(e).__name__}: {e}"
    if not episodes:
        return False, "Is anime ke episodes nahi mile."
    sess.episodes = episodes
    sess.series_url = series_url
    sess.series_title = episodes[0].get("series_title") or title or "Anime"
    sess.view = "episodes"
    sess.page = 0
    sess.multi = False
    sess.selected.clear()
    return True, None


# ─────────────────────────────────────────────
#  Callback handler
# ─────────────────────────────────────────────
@Client.on_callback_query(filters.regex(r"^aniw_"))
async def aniwatch_callback_handler(client: Client, cb: CallbackQuery):
    try:
        parts = cb.data.split("_")
        action = parts[1] if len(parts) > 1 else ""
        sid = parts[2] if len(parts) > 2 else None
        sess = ANIW_SESSIONS.get(sid)

        if not sess:
            await cb.answer("⌛ Session expired, /aniwatch dubara bhejo.", show_alert=True)
            return

        if cb.from_user.id != sess.orig_message.from_user.id:
            await cb.answer("Yeh tumhara session nahi hai.", show_alert=True)
            return

        if action == "noop":
            await cb.answer()
            return

        if action == "close":
            ANIW_SESSIONS.pop(sid, None)
            await cb.answer()
            try:
                await cb.message.delete()
            except Exception:
                pass
            return

        if sess.busy:
            await cb.answer("⏳ Ruko, kaam chal raha hai...")
            return

        if action == "src":
            sess.source = _other_source(sess.source)
            await cb.answer(f"Source: {sess.source_label}")
            await _do_search(sess)
            return

        if action == "pick":
            try:
                r = sess.results[int(parts[3])]
            except (IndexError, ValueError):
                await cb.answer()
                return
            await cb.answer("📖 Episodes la raha hoon...")
            sess.busy = True
            try:
                await cb.message.edit("📖 **Episodes load ho rahe hain...**")
            except Exception:
                pass
            try:
                ok, err = await _load_episodes(sess, r["url"], r["title"])
            finally:
                sess.busy = False
            if not ok:
                await sess.render()
                await cb.message.reply(f"❌ {err}")
                return
            await sess.render()
            return

        if action == "back":
            sess.view = "results"
            sess.multi = False
            sess.selected.clear()
            await cb.answer()
            await sess.render()
            return

        if action == "q":
            key = parts[3] if len(parts) > 3 else ""
            if key in _QUALITY_KEYS:
                sess.quality = key
            await cb.answer(f"Quality: {sess.quality_label}")
            await sess.render()
            return

        if action == "lg":
            key = parts[3] if len(parts) > 3 else ""
            if key in _LANG_KEYS:
                sess.audio = key
            await cb.answer(f"Language: {sess.audio_label}")
            await sess.render()
            return

        if action == "toggle":
            sess.multi = not sess.multi
            if not sess.multi:
                sess.selected.clear()
            await cb.answer("Multi-select " + ("ON" if sess.multi else "OFF"))
            await sess.render()
            return

        if action == "selpage":
            for idx, _ in sess.page_items():
                sess.selected.add(idx)
            await cb.answer()
            await sess.render()
            return

        if action == "selall":
            sess.selected = set(range(len(sess.episodes)))
            await cb.answer(f"{len(sess.selected)} episodes select")
            await sess.render()
            return

        if action == "selclr":
            sess.selected.clear()
            await cb.answer()
            await sess.render()
            return

        if action == "uptog":
            sess.update_toggle = not sess.update_toggle
            await cb.answer("📢 Update Post " + ("ON" if sess.update_toggle else "OFF"))
            await sess.render()
            return

        if action == "chtog":
            new_val = not sess.channel_toggle
            if new_val:
                try:
                    channels = await db.get_channels(sess.orig_message.from_user.id)
                except Exception as e:
                    LOGGER.error(f"[Aniwatch] channel lookup error: {e}")
                    channels = []
                if not channels:
                    await cb.answer("❌ Pehle /addchannel se ek channel add karo!", show_alert=True)
                    return
            await _set_channel_upload(sess.orig_message.from_user.id, new_val)
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
                sess.episodes[idx]
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
                return

            await _start_download(client, cb, sess, [idx])
            return

        if action == "dl":
            if not sess.selected:
                await cb.answer("❌ Pehle kuch episodes select karo!", show_alert=True)
                return
            await _start_download(client, cb, sess, sorted(sess.selected))
            return

        await cb.answer()
    except Exception as e:
        LOGGER.error(f"[Aniwatch] callback error: {e}", exc_info=True)
        try:
            await cb.answer("❌ Error, dubara try karo.", show_alert=True)
        except Exception:
            pass


async def _start_download(client, cb: CallbackQuery, sess: AniwSession, idxs: list):
    await cb.answer(f"⬇️ {len(idxs)} episode(s) download shuru...")
    try:
        await cb.message.delete()
    except Exception:
        pass
    status_msg = await sess.orig_message.reply("⏳ Shuru ho raha hai...")
    ANIW_SESSIONS.pop(sess.sid, None)
    await _download_items(client, status_msg, sess.orig_message, sess, idxs)


# ─────────────────────────────────────────────
#  /aniwatch command
# ─────────────────────────────────────────────
_RANGE_RE = re.compile(r"^(\d+)(?:\s*[-–]\s*(\d+))?$")
_Q_RE = re.compile(r"^(360|720|1080|all)p?$", re.IGNORECASE)

USAGE = (
    "**Usage:**\n"
    "`/aniwatch <anime ka naam>` — search karke buttons se chuno\n"
    "`/aniwatch <series_url>` — seedha episode buttons\n"
    "`/aniwatch <series_url> 3-8` — ep 3 se 8 turant\n"
    "`/aniwatch <series_url> 3-8 1080` — quality bhi (360 / 720 / 1080 / all)\n\n"
    "**Sites:** animesalt.to (Aniwatch) • anikototv.to (Animetsu)\n\n"
    "**Example:**\n"
    "`/aniwatch frieren`\n"
    "`/aniwatch https://animesalt.to/watch/frieren-beyond-journeys-end-season-2 1-5 720`"
)


@Client.on_message(filters.command(["aniwatch", "aniw"]))
async def aniwatch_command(client: Client, message: Message):
    c = await check_chat(message, chat="Sudo")
    if not c:
        return

    import asyncio as _asyncio
    # blocking (auto-download ho sakta hai) -> thread mein chalao
    if not await _asyncio.to_thread(get_binary_path):
        await message.reply(
            "❌ `N_m3u8DL-RE` binary nahi mili aur auto-download bhi fail hua.\n"
            "Server ka internet / GitHub access check karo, ya binary `binary/N_m3u8DL-RE` "
            "(ya `/usr/local/bin/`) mein rakho. Logs mein exact error hai."
        )
        return

    tokens = (message.text or "").split()[1:]
    if not tokens:
        await message.reply(USAGE)
        return

    _prune_sessions()

    # ── link diya hai? ──
    if is_url(tokens[0]):
        url = tokens[0].strip()
        source = detect_source(url)
        if not source:
            await message.reply("❌ Sirf animesalt.to / anikototv.to ke links chalte hain.")
            return

        ep_range, quality = None, None
        for t in tokens[1:]:
            q = _Q_RE.match(t)
            r = _RANGE_RE.match(t)
            if q:
                quality = q.group(1).lower()
            elif r:
                a = int(r.group(1))
                b = int(r.group(2)) if r.group(2) else a
                ep_range = (min(a, b), max(a, b))

        status_msg = await message.reply("🔍 Link scan ho raha hai...")
        sess = AniwSession(message, source)
        try:
            found = await job_ctl.run_in_thread(_search_blocking, sess.source, url)
        except Exception as e:
            LOGGER.error(f"[Aniwatch] url resolve error: {e}")
            found = []
        if not found:
            await status_msg.edit("❌ Link se anime nahi mila. URL check karo.")
            return

        ok, err = await _load_episodes(sess, found[0]["url"], found[0]["title"])
        if not ok:
            await status_msg.edit(f"❌ {err}")
            return

        if quality:
            sess.quality = quality

        # range diya hai -> menu skip, seedha download
        if ep_range:
            lo, hi = ep_range
            idxs = [i for i, it in enumerate(sess.episodes) if lo <= _ep_int(it) <= hi]
            if not idxs:
                await status_msg.edit(
                    f"❌ Episode {lo}-{hi} nahi mile. Available: "
                    f"`{_ep_int(sess.episodes[0])}-{_ep_int(sess.episodes[-1])}`"
                )
                return
            await status_msg.edit(
                f"⏳ **{_md_safe(sess.series_title)}** — {len(idxs)} episode(s), {sess.quality_label}..."
            )
            await _download_items(client, status_msg, message, sess, idxs)
            return

        await sess.populate_toggles()
        ANIW_SESSIONS[sess.sid] = sess
        sess.msg = status_msg
        await sess.render()
        return

    # ── naam diya hai -> search ──
    query = " ".join(tokens).strip()
    sess = AniwSession(message, _DEFAULT_SOURCE, query=query)
    await sess.populate_toggles()
    ANIW_SESSIONS[sess.sid] = sess
    sess.msg = await message.reply("🔍 Search ho raha hai...")
    await _do_search(sess)
