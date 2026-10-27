"""
adh_monitor.py  —  ADH (Anime Dub Hindi) Auto-Monitor
=====================================================
Monitor channel mein ADH ki post aati hai, kuch aisi:

    ✨Episode 21-22 Added
    ✨Complete Season Zip Pack Added

    #BluRay #Muse_Dub

    🎥 Title: Attack on Titan (Season 3)
    🎧 Audio Tracks: #Hindi | English | Japanese

RTI auto-monitor jaisa hi kaam (wahi poller / card / channel upload / update post / border /
schedule notification — auto_monitor.py ka `_episode_quality_poller` reuse hota hai), bas file ka
SOURCE alag hai: ADH series page se qualities (480p / 720p / 1080p) resolve + download hoti hain.

Logic (RTI + ADH dono ek saath chalein to):
  * Jiski quality pehle aati hai, wahi upload karta hai; baaki source us quality ko dobara upload
    nahi karta (episode_guard ka in-flight claim + channel scan).
  * ADH ka 480p aur RTI ka 360p EK HI quality tier hai (episode_guard.canon) — ek aa gaya to doosre ki
    zarurat nahi.
  * Maan lo RTI se 360p + 720p aa gaye, 1080p nahi aaya, par ADH pe 1080p aa chuka hai -> ADH wala
    sirf 1080p download karke laga dega.
  * Jo quality abhi ADH page pe nahi aayi, uske liye RTI jaise hi baar-baar refresh (poll) hota hai.

Post ko RTI handler ignore karta hai (auto_monitor.py mein is_adh_post() check).
"""

import asyncio
import difflib
import html
import os
import re
import shutil
import time
import traceback
from urllib.parse import urlparse

from pyrogram import Client, filters
from pyrogram.types import Message

from .. import LOGGER, download_dir
from ..utils import job_ctl
from ..utils import episode_guard as eg
from ..utils.track_branding import brand_file
from . import auto_monitor as am
from . import adh_downloader as adh
from .url_upload import _download_url

ADH_QUALITIES = tuple(short for _k, _f, short in adh.ADH_Q_OPTS)      # ("480p", "720p", "1080p")
MONITOR_LANGUAGE = "Hindi"                                            # channel pe sirf Hindi audio

_AUD_RE = re.compile(r"audio\s*tracks?\s*:\s*(.+)", re.IGNORECASE)
_TTL_RE = re.compile(r"title\s*:\s*(.+)", re.IGNORECASE)
_EP_RE = re.compile(r"episodes?\s*0*(\d+)(?:\s*[-\u2013\u2014]\s*0*(\d+))?", re.IGNORECASE)

_processed: dict = {}          # (chat_id, msg_id) -> ts
_recent_posts: dict = {}       # (channel_id, start, end) -> ts
_RECENT_TTL = 3 * 3600
_ACTIVE: set = set()           # (channel_id, ep) — sirf ADH ke apne (RTI ko NAHI rokte)


# ─────────────────────────────────────────────
#  Post parsing
# ─────────────────────────────────────────────
def _clean_line(v: str) -> str:
    v = re.sub(r"[*_`~#]", "", v or "")
    return re.sub(r"\s+", " ", v).strip()


def is_adh_post(message: Message, text: str = None) -> bool:
    """ADH format: 'Title:' + 'Audio Tracks:' lines. (RTI ke 'Audio:' template se alag.)"""
    t = text if text is not None else (message.text or message.caption or "")
    return bool(t and _AUD_RE.search(t) and _TTL_RE.search(t))


def parse_adh_post(message: Message, text: str):
    """-> {title, eps:(s,e), audio} ya (None, reason)."""
    aud = _AUD_RE.search(text)
    ttl = _TTL_RE.search(text)
    if not (aud and ttl):
        return None, "ADH format nahi"
    audio = _clean_line(aud.group(1))
    if "hindi" not in audio.lower():
        return None, f"Audio tracks mein Hindi nahi ({audio[:50]})"
    head = text[:ttl.start()]                      # "Episode 21-22 Added" title line se pehle hota hai
    em = _EP_RE.search(head) or _EP_RE.search(text)
    if not em:
        return None, "Episode number nahi mila (shayad sirf Zip Pack post)"
    s_ep = int(em.group(1))
    e_ep = int(em.group(2)) if em.group(2) else s_ep
    if e_ep < s_ep:
        s_ep, e_ep = e_ep, s_ep
    return {"title": _clean_line(ttl.group(1)), "eps": (s_ep, e_ep), "audio": audio}, None


def _adh_host() -> str:
    try:
        from .anime_search import ADH_SITE
        return (urlparse(ADH_SITE).netloc or "").lower().replace("www.", "")
    except Exception:
        return "animedubhindi.link"


def _is_adh_url(u: str) -> bool:
    try:
        host = (urlparse(u).netloc or "").lower().replace("www.", "")
    except Exception:
        return False
    return bool(host) and (host == _adh_host() or "animedubhindi" in host)


def _candidate_urls(message: Message, text: str) -> list:
    urls = []
    mk = message.reply_markup
    for row in (getattr(mk, "inline_keyboard", None) or []):
        for btn in row:
            u = getattr(btn, "url", None)
            if u and u.startswith("http"):
                urls.append(u)
    for e in (message.entities or message.caption_entities or []):
        u = getattr(e, "url", None)
        if u and u.startswith("http"):
            urls.append(u)
    urls += re.findall(r"https?://[^\s)]+", text or "")
    return [u for u in urls if _is_adh_url(u)]


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _pick_search_result(results: list, title: str):
    """Search results mein se title se sabse kareeb (season bhi match karna chahiye)."""
    want = _norm(title)
    want_season = am._season_hint_from_text(title)
    best, best_sc = None, 0.0
    for r in results:
        rt = r.get("title", "")
        sc = difflib.SequenceMatcher(None, want, _norm(rt)).ratio()
        if want and want in _norm(rt):
            sc = max(sc, 0.9)
        rs = am._season_hint_from_text(rt)
        if want_season and rs and want_season != rs:
            sc -= 0.4
        if sc > best_sc:
            best, best_sc = r, sc
    return best if best_sc >= 0.55 else None


async def _resolve_series_url(message: Message, text: str, title: str):
    urls = _candidate_urls(message, text)
    if urls:
        return urls[0]
    # post mein ADH ka link/button nahi — site pe title se search karo
    try:
        from .anime_search import search_adh
        results, _nx = await job_ctl.run_in_thread(search_adh, title)
        hit = _pick_search_result(results or [], title)
        if hit:
            LOGGER.info(f"[ADH-Monitor] search se series mila: {hit['title']} -> {hit['url']}")
            return hit["url"]
    except Exception as e:
        LOGGER.warning(f"[ADH-Monitor] search fail: {e!r}")
    return None


# ─────────────────────────────────────────────
#  Download source (poller ko file deta hai)
# ─────────────────────────────────────────────
class _Sink:
    """Download/filter ke 'message' jaisa object — sirf text yaad rakhta hai (card mein ek hi line banti hai)."""

    def __init__(self, ref):
        self._ref = ref
        self.text = ""
        self.id = getattr(ref, "id", 0)
        self.chat = getattr(ref, "chat", None)

    async def edit(self, text=None, *args, **kwargs):
        if text is not None:
            self.text = str(text)
        return self

    edit_text = edit

    async def delete(self, *args, **kwargs):
        return True

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._ref, name)


def _safe_name(s: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "", s).strip()


def make_source(client: Client, log_message: Message, series_url: str, episode_num: int, anime_name: str,
                season: int, channel_id: int, owner_id: int):
    """poller ke liye `source(dl_dir, on_file, prog, remaining) -> {"files": [...], "error": ...}`"""

    async def source(dl_dir: str, on_file, prog: dict, remaining: set):
        res = {"files": [], "error": None}
        cancel = prog.get("cancel")

        def _cancelled() -> bool:
            return bool(cancel is not None and cancel.is_set())

        job = job_ctl.CURRENT.get()
        dl_slot = job.slot("dl", order=10, label="download") if job is not None else None

        # 1) series page -> episode list (har poll pe fresh — nayi quality aayi ya nahi)
        try:
            data = await job_ctl.run_in_thread(adh.discover_adh_full, series_url)
        except Exception as e:
            res["error"] = f"page fetch: {str(e)[:120]}"
            LOGGER.warning(f"[ADH-Monitor] E{episode_num} page fetch fail: {e!r}")
            return res
        ep = next((e for e in data["episodes"] if e["num"] == episode_num), None)
        if ep is None:
            res["error"] = "episode abhi list mein nahi"
            return res

        # 2) kaun si qualities abhi page pe aa chuki hain (jo baaki hain unmein se)
        avail = []
        for _k, full, short in adh.ADH_Q_OPTS:
            if short in remaining and adh._ep_has_quality(ep, full) and adh._can_resolve(ep, full):
                avail.append((short, full))
        if not avail:
            return res

        # 3) channel mein pehle se? (RTI / Direct / purana upload) — 480p aur 360p ek hi tier
        try:
            present, noq, _ok = await eg.present_qualities(
                client, channel_id, owner_id, season, episode_num, [s for s, _f in avail])
            all_done = noq or eg.is_complete(channel_id, season, episode_num)
        except Exception as e:
            LOGGER.warning(f"[ADH-Monitor] channel check error (ignore): {e!r}")
            present, all_done = set(), False
        todo = []
        for short, full in avail:
            if all_done or short in present:
                prog["done_q"].add(short)               # pehle se hai — complete maano
            elif eg.is_claimed(channel_id, season, episode_num, short):
                continue                                # RTI/Direct abhi upload kar raha hai — agle poll mein dekhenge
            else:
                todo.append((short, full))
        if not todo:
            return res
        prog["expected"] = [s for s, _f in todo]
        LOGGER.info(f"[ADH-Monitor] E{episode_num}: ab download hogi {prog['expected']}")

        # 4) download + filter + branding (parallel, RAM-safe), file ready hote hi upload shuru
        sinks = {s: _Sink(log_message) for s, _f in todo}
        stage = {s: "link nikal raha hoon..." for s, _f in todo}
        sem = asyncio.Semaphore(adh._resolve_par())

        async def _status_loop():
            while True:
                await asyncio.sleep(2.5)
                try:
                    lines, live = [], ""
                    for s, _f in todo:
                        bar = am._bar_line(sinks[s].text)
                        if bar:
                            lines.append(f"⬇️ **{s}** {bar}")
                            live = live or bar
                        elif stage.get(s):
                            lines.append(f"⬇️ **{s}** — {stage[s]}")
                    if dl_slot is not None and lines:
                        await dl_slot.edit("\n".join(lines))
                    lt = prog.get("live_txt")
                    if lt is not None and live:
                        lt["dl"] = f"⬇️ **Downloading**\n{live}"
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass

        async def _one(short: str, full: str):
            sink = sinks[short]
            async with sem:
                if _cancelled():
                    return
                await _wait_ram(cancel)
                final, dbg = await job_ctl.run_in_thread(adh._resolve_job_blocking, ep, full, cancel)
                for x in list(dbg or []):
                    if str(x).startswith("__SHOT__:"):
                        try:
                            os.remove(str(x)[len("__SHOT__:"):])
                        except Exception:
                            pass
                if not final:
                    LOGGER.warning(f"[ADH-Monitor] E{episode_num} {short} link resolve fail:\n"
                                   + adh._safe_debug([x for x in dbg if not str(x).startswith("__SHOT__:")][-12:]))
                    stage[short] = "link nahi mila (agle check mein retry)"
                    return
            if _cancelled():
                return
            if eg.is_claimed(channel_id, season, episode_num, short):
                stage[short] = "koi aur upload kar chuka — skip"
                return
            stage[short] = "download ho raha hai..."
            try:
                fname = await adh._get_filename_from_url(final)
                path = await _download_url(final, fname, sink, log_message)
                stage[short] = "filter + track names..."
                path, _eng = await adh._apply_language_filter(path, MONITOR_LANGUAGE, sink)
                # MP4 + "[ Hindi ] by sbanime" audio / "[ English ] by sbanime" subtitle titles
                path = await brand_file(path, audio_fallback=MONITOR_LANGUAGE, to_mp4=True)
                canon_name = _safe_name(f"{anime_name} S{season:02d}E{episode_num:02d} [{MONITOR_LANGUAGE}] {short}.mp4")
                final_path = os.path.join(dl_dir, canon_name)
                shutil.move(path, final_path)
                stage[short] = "upload ke liye ready"
                res["files"].append(final_path)
                on_file(final_path, short)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                LOGGER.error(f"[ADH-Monitor] E{episode_num} {short} download/process error: {e!r}\n{traceback.format_exc()}")
                stage[short] = f"error: {str(e)[:60]}"
                res["error"] = str(e)[:120]

        status_task = job_ctl.track_task(asyncio.ensure_future(_status_loop()))
        try:
            await asyncio.gather(*[_one(s, f) for s, f in todo], return_exceptions=False)
        finally:
            status_task.cancel()
            try:
                await status_task
            except BaseException:
                pass
            if dl_slot is not None:
                try:
                    await dl_slot.delete()
                except Exception:
                    pass
        return res

    source.qualities = ADH_QUALITIES
    return source


async def _wait_ram(cancel):
    """Naya Chrome kholne se pehle RAM check (OOM-restart se bachao) — max ~90s."""
    t0 = time.time()
    while not (cancel is not None and cancel.is_set()):
        try:
            from ..utils import memguard as _mg
            press = _mg.PRESSURE
        except Exception:
            press = False
        low = adh._free_mem_mb() < float(os.getenv("ADH_MIN_FREE_MB") or 900)
        if not (press or low) or time.time() - t0 > 90:
            return
        await asyncio.sleep(3)


# ─────────────────────────────────────────────
#  Post handler
# ─────────────────────────────────────────────
async def _run_post(client: Client, message: Message, info: dict, matched: dict, series_url: str, text: str):
    anime_name = matched["anime_name"]
    channel_id = matched["channel_id"]
    start_ep, end_ep = info["eps"]
    total = end_ep - start_ep + 1
    oid = await am._owner_id()
    season = am._season_hint_from_text(info["title"], text) or 1

    jb = job_ctl.CURRENT.get()
    if jb is not None:
        chs = jb.meta.setdefault("channels", [])
        if channel_id not in chs:
            chs.append(channel_id)
        jb.meta["anime"] = anime_name
        try:
            jb.meta["community"] = await am.get_community_tag(all_names=True)
        except Exception:
            jb.meta["community"] = ""

    # Same post dobara (forward / repost) aaye to ignore
    pk = (channel_id, start_ep, end_ep)
    now = time.time()
    for k in [k for k, t in _recent_posts.items() if now - t > _RECENT_TTL]:
        _recent_posts.pop(k, None)
    if pk in _recent_posts:
        LOGGER.info(f"[ADH-Monitor] ♻️ duplicate post ignore: {anime_name} Ep {start_ep}-{end_ep}")
        return
    _recent_posts[pk] = now
    if jb is not None:
        jb.meta["post_key"] = pk
        jb.meta["all_ok"] = False

    update_post_sent = [False]
    season_intro_state = [False]
    any_uploaded = False
    eps_done = 0

    for i, ep_num in enumerate(range(start_ep, end_ep + 1), 1):
        _ak = (channel_id, ep_num)
        if _ak in _ACTIVE:
            LOGGER.info(f"[ADH-Monitor] Ep {ep_num} pe ADH ki doosri run chal rahi hai — skip")
            eps_done += 1
            continue
        _ACTIVE.add(_ak)
        try:
            (_m, _u, _s, is_season_cancelled, is_episode_cancelled,
             consume_season_cancel, consume_episode_cancel) = am._get_upload_control_fns()
            if is_season_cancelled(anime_name):
                consume_season_cancel(anime_name)
                await message.reply(f"🛑 **Season Upload Cancelled!**\n\nBaaki queued episodes (`{ep_num}`-`{end_ep}`) skip kar diye gaye.")
                break
            if is_episode_cancelled(anime_name, ep_num):
                consume_episode_cancel(anime_name, ep_num)
                await message.reply(f"⏭️ **Skipped** — Ep `{ep_num}` (cancel request).")
                continue

            # Download se PEHLE: channel mein poora episode pehle se hai? (480p == 360p tier)
            try:
                pres, noq, _ok = await eg.present_qualities(
                    client, channel_id, oid, season, ep_num, am.TARGET_QUALITIES)
                if set(am.TARGET_QUALITIES) <= pres or noq or eg.is_complete(channel_id, season, ep_num):
                    await job_ctl.stage_msg(message, f"⏭️ **Ep {ep_num}** (S{season:02d}) channel mein pehle se maujood hai — skip kiya.")
                    eps_done += 1
                    if jb is not None:
                        jb.done_eps.append(f"EP{ep_num:02d}⏭")
                    continue
            except Exception as ge:
                LOGGER.warning(f"[ADH-Monitor] pre-check error (ignore): {ge!r}")

            if jb is not None:
                jb.header = f"📺 **Ep {ep_num}** • `{i}/{total}`"
                jb.reset_slots(keep=("stage",))
            await job_ctl.stage_msg(message, am._WAIT_TXT)

            source = make_source(client, message, series_url, ep_num, anime_name, season, channel_id, oid)
            stats = {}
            ok = await am._episode_quality_poller(
                client, message, "", ep_num, anime_name, channel_id, oid,
                matched_entry=matched, start_ep=start_ep, end_ep=end_ep,
                update_post_sent=update_post_sent, season_intro=season_intro_state,
                season_hint=season, stats=stats, page_url=None, source=source,
            )
            real = stats.get("uploaded", 0) > 0
            if ok:
                eps_done += 1
            if real:
                any_uploaded = True
            if ok and jb is not None:
                jb.done_eps.append(f"EP{ep_num:02d}" + ("" if real else "⏭"))
            if i < total:
                await asyncio.sleep(3)
        finally:
            _ACTIVE.discard(_ak)

    if jb is not None:
        jb.meta["all_ok"] = (eps_done >= total)

    if any_uploaded:
        try:
            await am._get_schedule_fn()(client, channel_id, anime_name, end_ep)
        except Exception as e:
            LOGGER.error(f"[ADH-Monitor] schedule notification error: {e}")


async def _handle_post(client: Client, message: Message, edited: bool = False):
    text = message.text or message.caption or ""
    if not is_adh_post(message, text):
        return
    monitor_ch = await am._get_monitor_channel()
    if not monitor_ch or message.chat.id != monitor_ch:
        return                                           # sirf monitor channel
    if "━━━━━━━━━━━━━━" in text:
        return                                           # bot ka apna card

    key = (message.chat.id, message.id)
    if edited and (key in _processed or (time.time() - message.date.timestamp()) > 600):
        return
    if key in _processed:
        return

    level = await am._get_notify_level()
    info, why = parse_adh_post(message, text)
    if info is None:
        LOGGER.info(f"[ADH-Monitor] skip: {why}")
        return

    bl = am._blacklist_hit(text, await am._get_monitor_blacklist())
    if bl:
        LOGGER.info(f"[ADH-Monitor] 🚫 blacklist word '{bl}' — skip")
        return

    anime_list = await am._get_anime_list()
    matched = am._find_matching_anime(info["title"], anime_list)
    if not matched or not matched.get("channel_id"):
        LOGGER.info(f"[ADH-Monitor] anime match nahi: {info['title']}")
        if level != "off":
            cands = am._fuzzy_candidates(info["title"], anime_list)
            await am._notify_owner(
                client,
                f"⚠️ <b>ADH post detect hui, par anime match nahi hua</b>\n\n"
                f"Title: <code>{html.escape(info['title'][:90])}</code>\n"
                f"Episode: <code>{info['eps'][0]}-{info['eps'][1]}</code>\n"
                + (f"Sabse kareeb: {html.escape(', '.join(cands))}\n" if cands else "")
                + "\n/add_anime se anime add karo (naam post ke title jaisa)."
            )
        return

    _processed[key] = time.time()
    if len(_processed) > 500:
        for k in sorted(_processed, key=_processed.get)[:200]:
            _processed.pop(k, None)

    series_url = await _resolve_series_url(message, text, info["title"])
    if not series_url:
        LOGGER.warning(f"[ADH-Monitor] series URL nahi mila: {info['title']}")
        if level != "off":
            await am._notify_owner(
                client,
                f"⚠️ <b>ADH post: series page ka link nahi mila</b>\n\n"
                f"<code>{html.escape(info['title'][:90])}</code>\n"
                f"Post mein ADH ka button/link nahi tha aur site search mein bhi match nahi mila."
            )
        return

    LOGGER.info(f"[ADH-Monitor] ✅ Match: {matched['anime_name']} | Ep {info['eps'][0]}–{info['eps'][1]} | {series_url}")
    if level == "all":
        await am._notify_owner(
            client,
            f"✅ <b>ADH post detect hui</b>\n\n🎬 <b>{html.escape(matched['anime_name'])}</b> — "
            f"Ep <code>{info['eps'][0]}-{info['eps'][1]}</code>\n🔗 <code>{html.escape(series_url)}</code>"
        )

    oid = await am._owner_id()
    admin_chat = message.chat.id
    job, _ = await job_ctl.run_with_card(
        message, "AutoMonitor",
        lambda: _run_post(client, message, info, matched, series_url, text),
        owner_id=oid or 0, admin_chat=admin_chat, deletable=True,
    )
    try:   # cancel hua / poora nahi hua to dobara post karke retry kar sako
        pk = job.meta.get("post_key")
        if pk is not None and (job.cancelled or not job.meta.get("all_ok")):
            _recent_posts.pop(pk, None)
            _processed.pop(key, None)
    except Exception:
        pass


@Client.on_message(filters.channel & (filters.text | filters.caption), group=-6)
async def adh_monitor_handler(client: Client, message: Message):
    try:
        await _handle_post(client, message)
    except Exception as e:
        LOGGER.error(f"[ADH-Monitor] handler crash: {e}\n{traceback.format_exc()}")
        try:
            await am._notify_owner(
                client, f"💥 <b>ADH Monitor crash</b>\n<code>{html.escape(type(e).__name__)}: {html.escape(str(e)[:300])}</code>")
        except Exception:
            pass


@Client.on_edited_message(filters.channel & (filters.text | filters.caption), group=-6)
async def adh_monitor_edit_handler(client: Client, message: Message):
    try:
        await _handle_post(client, message, edited=True)
    except Exception as e:
        LOGGER.error(f"[ADH-Monitor] edit handler crash: {e}")
