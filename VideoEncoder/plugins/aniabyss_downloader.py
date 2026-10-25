"""
aniabyss_downloader.py
======================
AniAbyss (aniabyss.xyz) — Hindi Dub, 4K tak.

Site ka khud ka JSON API hai (probe se confirm hua):
  search : GET  {SITE}/api/anime/search?q=<naam>   -> {"success":true,"data":[{...}]}
  detail : GET  {SITE}/api/anime/<slug>            -> {"data":{"title":{...},"downloads":[...]}}
  downloads[] : {episodeNumber, seasonNumber, quality ("4K UHD"/"1080p FHD"/...),
                 isBatch, batchRange, directUrl (https://dl.aniabyss.xyz/dl/<id>),
                 fileSize, format, audio ("Hindi, Japanese"), mirrorName}

Flow (site ke Batch Download page jaisa):
  anime chuno -> Quality buttons -> Episode buttons (multi-select + Download All)
  -> har episode: directUrl se download -> Telegram upload (url_upload ka _download_url/_do_upload).

Commands:
  /abyss <aniabyss anime link ya slug>   (naam se search ke liye /anime use karo)
"""

import asyncio
import os
import re
import time
import uuid
from urllib.parse import quote_plus

import requests
from pyrogram import Client, filters
from pyrogram.types import Message, CallbackQuery

from .. import LOGGER, download_dir
from ..utils.helper import check_chat
from .rti_downloader import _kb
from .url_upload import _download_url, _do_upload, _safe_filename
import shutil
from ..utils import job_ctl
from .swift_downloader import _new_status, _quality_from, _upload_one_file, _prewarm_all_custompics

SITE = os.getenv("ABYSS_SITE", "https://www.aniabyss.xyz").rstrip("/")
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36"
    ),
    "Referer": SITE + "/",
    "Accept": "application/json, text/plain, */*",
}

PER_PAGE = 12
ABY_SESSIONS = {}
ABY_SESSION_TIMEOUT = 3600


# ───────────────────────── API helpers (blocking) ─────────────────────────
def _title_of(d: dict) -> str:
    t = d.get("title") or {}
    if isinstance(t, str):
        return t
    return t.get("english") or t.get("romaji") or t.get("native") or d.get("customSlug") or "Anime"


def _slugify(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (t or "").lower()).strip("-")


def _get_json(path: str):
    r = requests.get(SITE + path, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def search_abyss(query: str):
    """-> (results, None). results = [{"title","url"(slug),"label"}]"""
    js = _get_json(f"/api/anime/search?q={quote_plus(query)}")
    out = []
    for d in js.get("data") or []:
        title = _title_of(d)
        slug = d.get("customSlug") or d.get("slug") or _slugify(title)
        extra = []
        if d.get("dubType"):
            extra.append(str(d["dubType"]))
        if d.get("episodes"):
            extra.append(f"{d['episodes']} ep")
        label = title + (f" • {' • '.join(extra)}" if extra else "")
        out.append({"title": title, "url": slug, "label": label,
                    "_alt": [str(d.get("_id") or ""), str(d.get("anilistId") or "")]})
    return out, None


def slug_from_input(text: str) -> str:
    text = (text or "").strip()
    m = re.search(r"/anime/([^/?#\s]+)", text)
    return m.group(1) if m else text.strip("/ ")


def fetch_detail(slug: str, alts=()):
    """Detail JSON ka data dict. Slug fail ho to _id / anilistId bhi try karta hai."""
    last = None
    for key in [slug, *[a for a in alts if a]]:
        try:
            js = _get_json(f"/api/anime/{quote_plus(key)}")
            d = js.get("data")
            if d and d.get("downloads") is not None:
                return d
        except Exception as e:  # noqa: BLE001
            last = e
    raise RuntimeError(f"Anime detail nahi mila ({slug}): {last}")


_Q_RANK = (("4k", 0), ("2160", 0), ("1080", 1), ("720", 2), ("480", 3), ("360", 4))


def _q_rank(q: str) -> int:
    ql = (q or "").lower()
    for k, r in _Q_RANK:
        if k in ql:
            return r
    return 9


def build_data(detail: dict) -> dict:
    """downloads[] -> {title, audio, qualities[], by_q{quality: [episode dicts]}}"""
    title = _title_of(detail)
    by_q = {}
    for dl in detail.get("downloads") or []:
        if dl.get("isBatch") or not dl.get("directUrl"):
            continue                       # ZIP/batch entries skip — episode wise download
        q_raw = (dl.get("quality") or "Unknown").strip()
        q = _q_key(q_raw)          # "4K UHD" -> "2160p", "1080p FHD" -> "1080p"
        key = (int(dl.get("seasonNumber") or 1), int(dl.get("episodeNumber") or 0))
        eps = by_q.setdefault(q, {})
        prev = eps.get(key)
        fast = "high" in (dl.get("mirrorName") or "").lower()
        if prev is None or (fast and not prev["fast"]):
            eps[key] = {
                "season": key[0], "num": key[1], "url": dl["directUrl"],
                "size": dl.get("fileSize") or "", "fmt": (dl.get("format") or "MKV").lower(),
                "audio": dl.get("audio") or detail.get("audioLanguages") or "", "fast": fast,
                "quality": q_raw,
            }
    qualities = sorted(by_q, key=lambda q: (_q_rank(q), q))
    return {
        "title": title,
        "dub": detail.get("dubType") or "",
        "qualities": qualities,
        "by_q": {q: [by_q[q][k] for k in sorted(by_q[q])] for q in qualities},
    }


def _q_key(q: str) -> str:
    """Quality ka normalized key: '4K UHD' -> '2160p', '720p HD' -> '720p', warna raw."""
    ql = (q or "").lower()
    if "4k" in ql or "2160" in ql:
        return "2160p"
    m = re.search(r"(\d{3,4})\s*p?", ql)
    return f"{m.group(1)}p" if m else (q or "Unknown")


def _q_label(key: str) -> str:
    return "4K" if key == "2160p" else key


def _with_dl(url: str) -> str:
    """dl.aniabyss.xyz ke link pe ?download=1 lagata hai taaki 'Download Now' HTML page skip ho
    aur seedha file mile."""
    if "dl.aniabyss.xyz" in url and "download=" not in url:
        url += ("&" if "?" in url else "?") + "download=1"
    return url


def resolve_direct(url: str) -> str:
    """directUrl redirect / JSON-wrapper ho to final file URL nikalta hai; warna wahi URL.
    Sirf pakke errors (HTTP>=400, HTML page, JSON bina link) par exception uthata hai."""
    url = _with_dl(url)
    try:
        r = requests.get(url, headers=HEADERS, stream=True, timeout=30, allow_redirects=True)
    except requests.RequestException as e:
        LOGGER.warning(f"[Abyss] resolve_direct network issue, original URL try hoga: {e}")
        return url
    try:
        ctype = (r.headers.get("content-type") or "").lower()
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code} — link expire/blocked ho sakta hai")
        if "json" in ctype:
            js = r.json()
            node = js.get("data", js) if isinstance(js, dict) else {}
            for k in ("url", "directUrl", "link", "downloadUrl", "download_url", "file"):
                if isinstance(node, dict) and isinstance(node.get(k), str) and node[k].startswith("http"):
                    return node[k]
            raise RuntimeError(f"JSON mila par link nahi: {str(js)[:150]}")
        if "text/html" in ctype:
            raise RuntimeError("HTML page mila (direct file nahi)")
        return r.url or url
    finally:
        r.close()


# ───────────────────────── session / UI ─────────────────────────
def _prune_aby_sessions():
    now = time.time()
    for k in [k for k, v in ABY_SESSIONS.items() if now - v.created > ABY_SESSION_TIMEOUT]:
        ABY_SESSIONS.pop(k, None)


class AbyssSelector:
    def __init__(self, data: dict, orig_message: Message):
        self.data = data
        self.title = data["title"]
        self.qualities = data["qualities"]             # normalized keys: 480p, 720p, 1080p, 2160p ...
        self.orig_message = orig_message
        self.sid = uuid.uuid4().hex[:10]
        self.msg = None
        # default: 720p agar hai, warna sabse pehli available
        self.quality = "720p" if "720p" in self.qualities else (self.qualities[0] if self.qualities else None)
        self.page = 0
        self.multi = False
        self.selected = set()
        self.created = time.time()

    @property
    def episodes(self):
        if self.quality == "all":
            union = {}
            for q in self.qualities:
                for ep in self.data["by_q"].get(q, []):
                    u = union.setdefault((ep["season"], ep["num"]),
                                         {"season": ep["season"], "num": ep["num"], "by": {}, "size": ""})
                    u["by"][q] = ep
            return [union[k] for k in sorted(union)]
        return self.data["by_q"].get(self.quality, [])

    def jobs(self, idxs: list) -> list:
        """Chuni hui episode indexes -> download karne wale ep dicts (All mein har quality ka alag)."""
        eps = self.episodes
        out = []
        for i in idxs:
            ep = eps[i]
            if self.quality == "all":
                out.extend(ep["by"][q] for q in reversed(self.qualities) if q in ep["by"])
            else:
                out.append(ep)
        return out

    @property
    def total_pages(self):
        return max(1, (len(self.episodes) + PER_PAGE - 1) // PER_PAGE)

    def _ep_label(self, ep):
        multi_season = len({e["season"] for e in self.episodes}) > 1
        lab = f"S{ep['season']}E{ep['num']:02d}" if multi_season else f"E{ep['num']:02d}"
        if self.quality == "all" and ep.get("quality"):
            lab += f" {_q_label(_q_key(ep['quality']))}"
        return lab

    def header_text(self):
        lines = ["📂 **Select Episode**", "━━━━━━━━━━━━━━━━━━", f"🎬 **{self.title}**",
                 "🌐 Website: 🌀 AniAbyss"]
        if self.data.get("dub"):
            lines.append(f"🗣️ {self.data['dub']}")
        if not self.qualities:
            lines.append("\n❌ Is anime ke liye koi download link nahi mila.")
            return "\n".join(lines)
        eps = self.episodes
        size = eps[0].get("size", "") if eps else ""
        qlabel = "All" if self.quality == "all" else _q_label(self.quality)
        lines.append(f"📖 Episodes: {len(eps)} • 🎚 Quality: {qlabel}" + (f" • ~{size}/ep" if size else ""))
        lines.append("━━━━━━━━━━━━━━━━━━")
        lines.append(f"👇 Tap an episode to download:  (Page {self.page + 1}/{self.total_pages})")
        if self.multi:
            lines.append(f"\n☑️ **Multi-select ON** — chosen: `{len(self.selected)}`")
        return "\n".join(lines)

    def build_markup(self):
        sid = self.sid
        rows = []
        eps = self.episodes
        start = self.page * PER_PAGE
        row = []
        for idx in range(start, min(start + PER_PAGE, len(eps))):
            mark = "✅ " if idx in self.selected else ""
            lab = f"S{eps[idx]['season']}E{eps[idx]['num']:02d}" \
                if len({e["season"] for e in eps}) > 1 else f"E{eps[idx]['num']:02d}"
            row.append((f"{mark}{lab}", f"aby_e_{sid}_{idx}"))
            if len(row) == 3:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        if self.total_pages > 1:
            nav = []
            if self.page > 0:
                nav.append(("◀️ Prev", f"aby_pg_{sid}_{self.page - 1}"))
            nav.append((f"📄 {self.page + 1}/{self.total_pages}", f"aby_noop_{sid}"))
            if self.page < self.total_pages - 1:
                nav.append(("Next ▶️", f"aby_pg_{sid}_{self.page + 1}"))
            rows.append(nav)

        # Quality row: jo qualities is anime ke liye available hain (480p / 720p / 1080p / 4K ...) + All
        qrow = [((("✅ " if self.quality == q else "") + _q_label(q)), f"aby_q_{sid}_{q}")
                for q in reversed(self.qualities)]          # chhoti -> badi (480p ... 4K)
        if len(self.qualities) > 1:
            qrow.append(((("✅ " if self.quality == "all" else "") + "All"), f"aby_q_{sid}_all"))
        for i in range(0, len(qrow), 5):
            rows.append(qrow[i:i + 5])

        rows.append([(f"📦 Download All Episodes ({len(eps)})", f"aby_all_{sid}")])
        if self.multi:
            rows.append([(f"⬇️ Download Selected ({len(self.selected)})", f"aby_dl_{sid}")])
            rows.append([("☑️ Multi-select: ON (tap to turn off)", f"aby_toggle_{sid}")])
        else:
            rows.append([("☑️ Select Multiple", f"aby_toggle_{sid}")])
        rows.append([("❌ Close", f"aby_close_{sid}")])
        return _kb(rows)

    async def render(self):
        if not self.msg:
            return
        try:
            await self.msg.edit(self.header_text(), reply_markup=self.build_markup())
        except Exception as e:  # noqa: BLE001
            if "MESSAGE_NOT_MODIFIED" not in str(e):
                LOGGER.error(f"[Abyss] render error: {e}")


def make_selector(detail: dict, orig_message: Message) -> AbyssSelector:
    sel = AbyssSelector(build_data(detail), orig_message)
    _prune_aby_sessions()
    ABY_SESSIONS[sel.sid] = sel
    return sel


# ───────────────────────── download pipeline ─────────────────────────
def _q_tag(q: str) -> str:
    """'4K' -> '2160p', '1080p'/'1080' -> '1080p', warna khali."""
    ql = (q or "").lower()
    if "4k" in ql or "2160" in ql:
        return "2160p"
    m = re.search(r"(\d{3,4})", ql)
    return f"{m.group(1)}p" if m else ""


def _make_filename(title: str, ep: dict) -> str:
    # Naam mein [Hindi] + quality rakhte hain taaki auto-rename "in Hindi 720p [@SBANIME]" bana sake
    audio = re.sub(r"\s*,\s*", "+", (ep.get("audio") or "").strip()) or "Hindi"
    name = f"{title} S{ep['season']:02d}E{ep['num']:02d} [{audio}]"
    qt = _q_tag(ep.get("quality"))
    if qt:
        name += f" {qt}"
    name = re.sub(r'[<>:"/\\|?*]', "", name).strip()
    return _safe_filename(f"{name}.{ep.get('fmt') or 'mkv'}")


async def _fast_download(url: str, dest: str, stage):
    """Seedha multi-connection download (TurboDL) — koi link-check / m3u8 sniff nahi."""
    from ..utils.turbo_download import TurboDL
    from ..utils.display_progress import progress_for_url
    from .. import profile as _prof

    headers = dict(HEADERS)
    headers["Accept"] = "*/*"
    dl = TurboDL(url, dest, progress_bar=False, threads=_prof.DOWNLOAD_THREADS,
                 request_args={"headers": headers}, timeout=30)
    job = job_ctl.CURRENT.get()
    if job is not None:
        job.progresses.append({"phase": "download", "dls": {"abyss": dl}})
    dl.start(blocking=False)
    while not dl.isFinished():
        await progress_for_url(dl, stage)
        await asyncio.sleep(1.5)
    if dl.isSuccessful():
        return dest
    raise RuntimeError(f"Download fail: {dl.get_errors()}")


async def _process_item(client, orig_message, sel: AbyssSelector, ep: dict, stage, i: int, total: int):
    label = f"{sel._ep_label(ep)}"
    dl_dir = os.path.join(download_dir, f"aby_{sel.sid}_{ep['season']}_{ep['num']}_{int(time.time())}")
    os.makedirs(dl_dir, exist_ok=True)
    job = job_ctl.CURRENT.get()
    if job is not None:
        job.dirs.add(dl_dir)          # cancel / finish pe folder saaf

    try:
        filename = _make_filename(sel.title, ep)
        dest = os.path.join(dl_dir, filename)
        await stage.edit(f"⬇️ **{label}** (`{i}/{total}`) — download shuru...\n`{filename}`")

        # ?download=1 -> "Download Now" HTML page skip, seedha file. Koi link-check nahi.
        try:
            filepath = await _fast_download(_with_dl(ep["url"]), dest, stage)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            LOGGER.warning(f"[Abyss] fast download fail ({e}), purana downloader try hoga")
            tmp = await _download_url(_with_dl(ep["url"]), filename, stage, orig_message)
            if tmp and os.path.exists(tmp):
                shutil.move(tmp, dest)
            filepath = dest

        if not filepath or not os.path.exists(filepath):
            await stage.edit(f"🛑 **{label}** — file nahi bani.\n⛔ Agle episodes band kar diye gaye.")
            return "error"

        await stage.edit(f"✅ **{label}** (`{i}/{total}`) — download ho gaya\n📤 Upload shuru...")

        # /aniwatch wala hi upload: thumbnail / cover / custompic / caption style / auto-rename / quota
        q = _quality_from(os.path.basename(filepath))
        slot = await _new_status(orig_message, f"📤 **Uploading `{q}`...**", q)
        success, _sent, _quality = await _upload_one_file(
            client, orig_message, slot, filepath, dl_dir, False, label_prefix=label
        )
        if not success:
            try:
                await slot.edit(f"❌ Upload failed: `{_quality}`")
            except Exception:
                pass
            await stage.edit(f"🛑 **{label} — Upload Fail**\n⛔ Agle episodes band kar diye gaye.")
            return "error"
        try:
            await slot.delete()
        except Exception:
            pass
        return "ok"
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        LOGGER.error(f"[Abyss] {label} error: {e}", exc_info=True)
        try:
            await stage.edit(f"🛑 **{label} — Error**\n`{str(e)[:150]}`\n⛔ Agle episodes band kar diye gaye.")
        except Exception:
            pass
        return "error"
    finally:
        shutil.rmtree(dl_dir, ignore_errors=True)


async def _download_items(client, status_msg, sel: AbyssSelector, idxs: list):
    """Poora process EK card message mein + ❌ Cancel button (job_ctl) — /aniwatch jaisa."""
    eps = sel.jobs(idxs)
    total = len(eps)
    orig_message = sel.orig_message
    title = (sel.title or "AniAbyss")[:55]
    job = job_ctl.Job(status_msg, orig_message.from_user.id, title=title)
    job.quota_count = total
    state = {"ok": 0, "last": "ok"}

    async def _body():
        stage = job.slot("stage", order=0)
        asyncio.ensure_future(_prewarm_all_custompics(orig_message.from_user.id))
        for n, ep in enumerate(eps, 1):
            label = sel._ep_label(ep)
            job.header = f"📺 **{label}** • `{n}/{total}`"
            job.reset_slots(keep=("stage",))
            result = await _process_item(client, orig_message, sel, ep, stage, n, total)
            state["last"] = result
            if result != "ok":
                break
            state["ok"] += 1
            job.done_eps.append(label)
            if n < total:
                await asyncio.sleep(1)

    try:
        await job.run(_body)
    except Exception as e:  # noqa: BLE001
        LOGGER.error(f"[Abyss] _download_items error: {e}")
        state["last"] = "error"
        job.slot("stage", order=0).text = f"❌ `{str(e)[:150]}`"

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


# ───────────────────────── callbacks ─────────────────────────
@Client.on_callback_query(filters.regex(r"^aby_"))
async def aby_callback(client: Client, cb: CallbackQuery):
    try:
        parts = cb.data.split("_")
        action = parts[1]
        sid = parts[2] if len(parts) > 2 else None
        sel = ABY_SESSIONS.get(sid)
        if not sel:
            await cb.answer("⌛ Session expire, dubara /anime ya /abyss bhejo.", show_alert=True)
            return
        if sel.orig_message.from_user and cb.from_user.id != sel.orig_message.from_user.id:
            await cb.answer("❌ Yeh tumhara menu nahi hai.", show_alert=True)
            return

        if action == "noop":
            await cb.answer()
            return

        if action == "close":
            ABY_SESSIONS.pop(sid, None)
            await cb.answer()
            try:
                await cb.message.delete()
            except Exception:
                pass
            return

        if action == "q":
            key = parts[3] if len(parts) > 3 else ""
            if key == "all" or key in sel.qualities:
                sel.quality = key
                sel.page, sel.multi = 0, False
                sel.selected.clear()
            await cb.answer(f"Quality: {'All' if sel.quality == 'all' else _q_label(sel.quality)}")
            sel.msg = cb.message
            await sel.render()
            return

        if action == "toggle":
            sel.multi = not sel.multi
            if not sel.multi:
                sel.selected.clear()
            await cb.answer("Multi-select " + ("ON" if sel.multi else "OFF"))
            sel.msg = cb.message
            await sel.render()
            return

        if action == "pg":
            sel.page = max(0, min(int(parts[3]), sel.total_pages - 1))
            await cb.answer()
            sel.msg = cb.message
            await sel.render()
            return

        if action == "e":
            idx = int(parts[3])
            if sel.multi:
                sel.selected.symmetric_difference_update({idx})
                await cb.answer()
                sel.msg = cb.message
                await sel.render()
                return
            idxs = [idx]
        elif action == "dl":
            if not sel.selected:
                await cb.answer("❌ Pehle kuch episodes select karo!", show_alert=True)
                return
            idxs = sorted(sel.selected)
        elif action == "all":
            idxs = list(range(len(sel.episodes)))
            if not idxs:
                await cb.answer("❌ Koi episode nahi.", show_alert=True)
                return
        else:
            await cb.answer()
            return

        await cb.answer(f"⬇️ {len(idxs)} episode(s) shuru ho rahe hain...")
        try:
            await cb.message.delete()
        except Exception:
            pass
        status_msg = await sel.orig_message.reply("⏳ Shuru ho raha hai...")
        await _download_items(client, status_msg, sel, idxs)
        ABY_SESSIONS.pop(sid, None)
    except Exception as e:  # noqa: BLE001
        LOGGER.error(f"[Abyss] callback error: {e}")
        try:
            await cb.answer("❌ Error, dubara try karo.", show_alert=True)
        except Exception:
            pass


# ───────────────────────── /abyss ─────────────────────────
@Client.on_message(filters.command("abyss"))
async def abyss_command(client: Client, message: Message):
    c = await check_chat(message, chat="Sudo")
    if not c:
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply(
            "**Usage:** `/abyss <aniabyss anime link ya slug>`\n"
            "**Example:** `/abyss https://www.aniabyss.xyz/anime/sakamoto-days`\n\n"
            "Naam se dhundhna ho to `/anime <naam>` use karo aur AniAbyss chuno."
        )
        return
    slug = slug_from_input(parts[1])
    status = await message.reply("🔍 AniAbyss se data le raha hu...")
    loop = asyncio.get_event_loop()
    try:
        detail = await loop.run_in_executor(None, fetch_detail, slug, ())
    except Exception as e:  # noqa: BLE001
        await status.edit(f"❌ {str(e)[:300]}")
        return
    sel = make_selector(detail, message)
    sel.msg = status
    await status.edit(sel.header_text(), reply_markup=sel.build_markup())
