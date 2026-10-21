"""
ytdl_downloader.py  v1
======================
YouTube + 1000+ sites (Instagram, Twitter/X, Facebook, Dailymotion, Vimeo, Reddit ...) se
yt-dlp ke through download + urlenbot ke apne upload pipeline se upload.
(CBML ke /ytdlleech ka urlenbot-style port.)

Commands:
  /ytdl <link>              -> quality buttons (1080p / 720p / ... / Audio)
  /ytdl <link> 720          -> seedha 720p (360 / 480 / 720 / 1080 / 1440 / 2160)
  /ytdl <link> audio        -> seedha mp3
  (short alias: /yt)

Upload: swift_downloader._upload_one_file — /aniwatch, /abyss jaisa hi (thumbnail / cover /
custompic / caption style / auto-rename / quota / ❌ Cancel).

Env (optional):
  YTDL_COOKIES   cookies.txt ka path (YouTube "Sign in to confirm you're not a bot" ke liye)
  YTDL_PROXY     http://user:pass@host:port

Requirement: `yt-dlp` (pip) + ffmpeg (already bot mein hai).
"""

import asyncio
import glob
import os
import re
import shutil
import time
import uuid

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .. import LOGGER, download_dir
from ..utils import job_ctl
from ..utils.display_progress import _make_bar, humanbytes
from ..utils.helper import check_chat
from .swift_downloader import _new_status, _prewarm_all_custompics, _quality_from, _upload_one_file

SESSION_TIMEOUT = 1800
YT_SESSIONS: dict = {}
HEIGHTS = (2160, 1440, 1080, 720, 480, 360)
_URL_RE = re.compile(r"https?://\S+")


def _kb(rows):
    return InlineKeyboardMarkup([[InlineKeyboardButton(t, callback_data=d) for t, d in row] for row in rows])


def _prune():
    now = time.time()
    for k in [k for k, v in YT_SESSIONS.items() if now - v["created"] > SESSION_TIMEOUT]:
        YT_SESSIONS.pop(k, None)


def _clean(name: str, limit: int = 120) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name or "").strip()
    name = re.sub(r"\s+", " ", name)
    return (name[:limit].rstrip() or "video")


def _base_opts() -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "playlist_items": "1",               # playlist link par bhi sirf pehla video (slow scan nahi)
        "retries": 5,
        "fragment_retries": 10,
        "socket_timeout": 30,
        "concurrent_fragment_downloads": 8,   # DASH/HLS parts parallel — speed
        "http_chunk_size": 10 * 1024 * 1024,
        "windowsfilenames": True,
    }
    ck = os.environ.get("YTDL_COOKIES", "").strip()
    if ck and os.path.isfile(ck):
        opts["cookiefile"] = ck
    px = os.environ.get("YTDL_PROXY", "").strip()
    if px:
        opts["proxy"] = px
    return opts


def _extract_info(url: str) -> dict:
    import yt_dlp
    with yt_dlp.YoutubeDL({**_base_opts(), "skip_download": True}) as ydl:
        info = ydl.extract_info(url, download=False)
    if info and info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise RuntimeError("Playlist khali hai")
        first = entries[0]
        if not first.get("formats") and first.get("url"):
            with yt_dlp.YoutubeDL({**_base_opts(), "skip_download": True}) as ydl:
                first = ydl.extract_info(first.get("webpage_url") or first["url"], download=False)
        first["_from_playlist"] = True
        return first
    return info


def _available_heights(info: dict) -> list:
    hs = set()
    for f in info.get("formats") or []:
        h = f.get("height")
        if h and f.get("vcodec") not in (None, "none"):
            hs.add(int(h))
    if not hs:
        return []
    top = max(hs)
    out = [h for h in HEIGHTS if any(abs(x - h) <= 20 for x in hs)]   # sirf jo sach mein available hain
    if not out or top > out[0] + 40:
        out.insert(0, top)       # odd height (jaise 1000p / 240p) bhi dikhao
    return out


def _download_blocking(url: str, dl_dir: str, height, prog: dict, job) -> str:
    import yt_dlp

    def hook(d):
        if job is not None and job.cancel_ev.is_set():
            raise yt_dlp.utils.DownloadCancelled("cancelled")
        st = d.get("status")
        if st == "downloading":
            prog.update(
                phase="download",
                done=d.get("downloaded_bytes") or 0,
                total=d.get("total_bytes") or d.get("total_bytes_estimate") or 0,
                speed=d.get("speed") or 0,
                eta=d.get("eta"),
            )
        elif st == "finished":
            prog["phase"] = "merge"

    opts = _base_opts()
    opts.update({
        "outtmpl": os.path.join(dl_dir, "%(title).120B [%(id)s].%(ext)s"),
        "progress_hooks": [hook],
        "noprogress": True,
    })
    if height == "audio":
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
                                   "preferredquality": "192"}]
    else:
        h = int(height)
        opts["format"] = f"bv*[height<={h}]+ba/b[height<={h}]/bv*+ba/b"
        opts["merge_output_format"] = "mp4"
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.extract_info(url, download=True)

    files = [f for f in glob.glob(os.path.join(dl_dir, "*"))
             if os.path.isfile(f) and not f.endswith((".part", ".ytdl", ".temp"))]
    if not files:
        raise RuntimeError("Download ke baad file nahi mili")
    return max(files, key=os.path.getsize)


def _fmt_progress(prog: dict, title: str) -> str:
    if prog.get("phase") == "merge":
        return f"⚙️ **Merge / convert ho raha hai...**\n`{title}`"
    total, done = prog.get("total") or 0, prog.get("done") or 0
    pct = (done * 100 / total) if total else 0
    eta = prog.get("eta")
    return (
        f"⬇️ **Downloading...**\n`{title}`\n\n"
        f"`{_make_bar(pct)}` **{pct:.1f}%**\n\n"
        f"📦 **Size :** `{humanbytes(done)}` / `{humanbytes(total) if total else '?'}`\n"
        f"⚡ **Speed:** `{humanbytes(prog.get('speed') or 0)}/s`\n"
        f"⏳ **ETA :** `{int(eta) if eta is not None else '?'}s`"
    )


async def _run_job(client, status_msg, orig_message: Message, url: str, info: dict, height):
    title = _clean(info.get("title") or "YouTube", 55)
    job = job_ctl.Job(status_msg, orig_message.from_user.id, title=title)
    job.quota_count = 1
    state = {"ok": False}

    async def _body():
        stage = job.slot("stage", order=0)
        asyncio.ensure_future(_prewarm_all_custompics(orig_message.from_user.id))
        dl_dir = os.path.join(download_dir, f"ytdl_{uuid.uuid4().hex[:8]}_{int(time.time())}")
        os.makedirs(dl_dir, exist_ok=True)
        job.dirs.add(dl_dir)
        try:
            prog = {"phase": "download"}
            await stage.edit(f"🔍 **Download shuru...**\n`{title}`")
            fut = job_ctl.run_in_thread(_download_blocking, url, dl_dir, height, prog, job)
            while not fut.done():
                await asyncio.sleep(2)
                await stage.edit(_fmt_progress(prog, title))
            path = await fut

            await stage.edit("✅ Download ho gaya\n📤 Upload shuru...")

            if height == "audio":
                nice = _clean(info.get("title") or "audio") + ".mp3"
                new = os.path.join(dl_dir, nice)
                if path != new:
                    os.rename(path, new)
                    path = new
                await orig_message.reply_audio(
                    path, caption=f"<b>{nice}</b>", parse_mode=ParseMode.HTML,
                    title=_clean(info.get("title") or "audio"), performer=info.get("uploader") or None,
                    duration=int(info.get("duration") or 0),
                )
                state["ok"] = True
                return

            ext = os.path.splitext(path)[1] or ".mp4"
            nice = f"{_clean(info.get('title') or 'video')} {int(height)}p{ext}"
            new = os.path.join(dl_dir, nice)
            if path != new:
                os.rename(path, new)
                path = new

            q = _quality_from(os.path.basename(path))
            slot = await _new_status(orig_message, f"📤 **Uploading `{q}`...**", q)
            ok, _sent, qual = await _upload_one_file(client, orig_message, slot, path, dl_dir, False,
                                                     label_prefix="YTDL")
            if not ok:
                try:
                    await slot.edit(f"❌ Upload failed: `{qual}`")
                except Exception:
                    pass
                return
            try:
                await slot.delete()
            except Exception:
                pass
            state["ok"] = True
        finally:
            shutil.rmtree(dl_dir, ignore_errors=True)

    err = None
    try:
        await job.run(_body)
    except Exception as e:  # noqa: BLE001
        err = str(e)
        LOGGER.error(f"[YTDL] error: {e}", exc_info=True)

    if job.cancelled:
        return
    job.reset_slots(keep=("stage",))
    if state["ok"]:
        await job.finish(f"🎉 **Complete!** — {job.title}")
    else:
        job.finished = False
        msg = f"\n\n❌ `{(err or 'upload/download fail')[:200]}`"
        await job.finish(job.render() + msg)


def _parse_args(text: str):
    parts = (text or "").split()[1:]
    url, pick = None, None
    for t in parts:
        if _URL_RE.match(t):
            url = t
        elif t.lower() in ("audio", "mp3", "a"):
            pick = "audio"
        elif re.fullmatch(r"\d{3,4}p?", t.lower()):
            pick = int(t.lower().rstrip("p"))
    return url, pick


@Client.on_message(filters.command(["ytdl", "yt"]))
async def ytdl_command(client: Client, message: Message):
    c = await check_chat(message, chat="Sudo")
    if not c:
        return
    url, pick = _parse_args(message.text or "")
    if not url and message.reply_to_message:
        m = _URL_RE.search((message.reply_to_message.text or message.reply_to_message.caption or ""))
        url = m.group(0) if m else None
    if not url:
        await message.reply(
            "**Usage:**\n"
            "`/ytdl <link>` — quality buttons\n"
            "`/ytdl <link> 720` — seedha 720p (360/480/720/1080/1440/2160)\n"
            "`/ytdl <link> audio` — mp3\n\n"
            "YouTube, Instagram, Twitter/X, Facebook, Vimeo, Reddit ... 1000+ sites."
        )
        return
    try:
        import yt_dlp  # noqa: F401
    except Exception:
        await message.reply("❌ `yt-dlp` install nahi hai. `pip install -U yt-dlp` karke restart karo.")
        return

    _prune()
    status = await message.reply("🔍 Link scan ho raha hai...")
    try:
        info = await job_ctl.run_in_thread(_extract_info, url)
    except Exception as e:  # noqa: BLE001
        LOGGER.error(f"[YTDL] info error: {e}")
        txt = str(e).replace("ERROR: ", "")
        hint = ""
        if "Sign in" in txt or "not a bot" in txt:
            hint = "\n\n💡 YouTube ne bot-check lagaya. `YTDL_COOKIES=/path/cookies.txt` env set karo."
        await status.edit(f"❌ `{txt[:300]}`{hint}")
        return

    if pick is not None:
        if pick != "audio":
            hs = _available_heights(info)
            if hs and pick not in hs:
                pick = min(hs, key=lambda h: abs(h - pick))
        await status.edit(f"⏳ **{_clean(info.get('title'), 60)}** — shuru ho raha hai...")
        await _run_job(client, status, message, url, info, pick)
        return

    hs = _available_heights(info)
    sid = uuid.uuid4().hex[:8]
    YT_SESSIONS[sid] = {"url": url, "info": info, "msg0": message, "created": time.time()}
    rows, row = [], []
    for h in hs:
        row.append((f"🎞️ {h}p", f"ytd_{sid}_{h}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([("🎵 Audio (mp3)", f"ytd_{sid}_audio")])
    rows.append([("❌ Close", f"ytd_{sid}_close")])

    dur = int(info.get("duration") or 0)
    mins = f"{dur // 60}:{dur % 60:02d}" if dur else "?"
    note = "\n⚠️ Playlist hai — sirf pehla video liya." if info.get("_from_playlist") else ""
    await status.edit(
        f"🎬 **{_clean(info.get('title'), 80)}**\n"
        f"👤 {info.get('uploader') or '—'}  •  ⏱ {mins}{note}\n\n👇 Quality chuno:",
        reply_markup=_kb(rows),
    )


@Client.on_callback_query(filters.regex(r"^ytd_"))
async def ytdl_callback(client: Client, cb: CallbackQuery):
    try:
        _, sid, action = cb.data.split("_", 2)
        sess = YT_SESSIONS.get(sid)
        if not sess:
            await cb.answer("⌛ Session expire, dubara /ytdl bhejo.", show_alert=True)
            return
        orig = sess["msg0"]
        if orig.from_user and cb.from_user.id != orig.from_user.id:
            await cb.answer("❌ Yeh tumhara menu nahi hai.", show_alert=True)
            return
        if action == "close":
            YT_SESSIONS.pop(sid, None)
            await cb.answer()
            try:
                await cb.message.delete()
            except Exception:
                pass
            return
        height = "audio" if action == "audio" else int(action)
        YT_SESSIONS.pop(sid, None)
        await cb.answer("⬇️ Shuru ho raha hai...")
        try:
            await cb.message.edit("⏳ Shuru ho raha hai...")
        except Exception:
            pass
        await _run_job(client, cb.message, orig, sess["url"], sess["info"], height)
    except Exception as e:  # noqa: BLE001
        LOGGER.error(f"[YTDL] callback error: {e}", exc_info=True)
        try:
            await cb.answer("❌ Error, dubara try karo.", show_alert=True)
        except Exception:
            pass
