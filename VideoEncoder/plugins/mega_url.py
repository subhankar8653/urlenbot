"""
mega_url.py — Mega / DLBeta(Pixeldrain) / HubCloud link -> /url jaisa flow -> Telegram upload (TEST)
===================================================================================================
Do jagah se use hota hai:

 1. /megaurl <mega_link> [-vt] [| name]     (seedha command)
 2. /rtimega ... (rti_mega.py)              -> links nikalne ke baad har source se download + upload
                                               (`run_source_upload`), taaki real upload test ho sake.

Flow (sab /url jaisa): ❌ Cancel -> uploader prewarm -> download (progress) -> /urlpreset auto-settings
-> smart caption -> Telegram upload.
"""

import asyncio
import os
import re
import shutil
import time
from urllib.parse import urljoin, urlparse

from pyrogram import Client, filters
from pyrogram.types import Message

from .. import LOGGER, download_dir
from ..utils import job_ctl
from ..utils.database.add_user import AddUserToDatabase
from ..utils.helper import check_chat
from .mega_download import download_mega, is_mega_link
from .url_upload import (
    _auto_process_and_upload,
    _download_url,
    _get_filename_from_url,
    _parse_url_args,
    _safe_filename,
    _show_url_options,
    _url_sessions,
)

_MEGA_URL_RE = re.compile(r"https?://mega\.(?:nz|co\.nz)/\S+", re.I)
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _bar(done: int, total: int, width: int = 12) -> str:
    pct = (done / total) if total else 0
    fill = int(pct * width)
    return "▓" * fill + "░" * (width - fill) + f" {pct * 100:.1f}%"


# ─────────────────────────────────────────────
#  Resolvers: source link -> direct file URL
# ─────────────────────────────────────────────

def _pixeldrain_direct(url: str):
    """pixeldrain.net / .com /u/<id> -> API file url (direct bytes)."""
    m = re.search(r"pixeldrain\.[a-z]+/(?:u|l)/([A-Za-z0-9]+)", url)
    if not m:
        return None
    host = urlparse(url).netloc or "pixeldrain.net"
    return f"https://{host}/api/file/{m.group(1)}?download"


def _is_html(resp) -> bool:
    return "text/html" in (resp.headers.get("content-type") or "").lower()


def _hubcloud_direct(url: str):
    """BEST-EFFORT: hubcloud.*/drive/<id> -> direct download URL ya (None, html_text_for_debug).
    Page structure badalta rehta hai, isliye 2 hop tak follow karke 'download' buttons dhoondte hain."""
    import requests
    from bs4 import BeautifulSoup

    s = requests.Session()
    s.headers.update({"User-Agent": _UA})
    last_html = ""
    cur = url
    prefer = ("pixel", "fsl", "10gbps", "download file", "direct")
    for _hop in range(3):
        r = s.get(cur, timeout=30, allow_redirects=True)
        last_html = r.text
        if not _is_html(r):
            return cur, None                       # seedha file nikli
        soup = BeautifulSoup(r.text, "html.parser")
        cands = []
        for a in soup.find_all("a", href=True):
            href = urljoin(r.url, a["href"].strip())
            text = a.get_text(" ", strip=True).lower()
            if not href.startswith("http") or href.startswith("javascript"):
                continue
            score = None
            for i, p in enumerate(prefer):
                if p in text:
                    score = i
                    break
            if score is None and re.search(r"hubcloud\.php|gamerxyt|/dl/|\.mkv|\.mp4", href, re.I):
                score = 50
            if score is not None:
                cands.append((score, href))
        if not cands:
            break
        cands.sort(key=lambda x: x[0])
        for _sc, href in cands[:6]:
            try:
                h = s.head(href, timeout=20, allow_redirects=True)
                if h.status_code < 400 and not _is_html(h):
                    return h.url, None
            except Exception:
                pass
        cur = cands[0][1]                          # agle hop pe wahi page kholo
    return None, last_html


async def _download_any(url: str, kind: str, dest_dir: str, msg, message, notes: list):
    """kind: mega | dlbeta | hubcloud | other -> downloaded filepath ya None."""
    if is_mega_link(url):
        return await _download_mega_progress(url, dest_dir, msg)

    direct = url
    if "pixeldrain" in url:
        direct = _pixeldrain_direct(url) or url
    elif kind == "hubcloud" or "hubcloud" in url:
        await msg.edit("<b>🔎 HubCloud page se direct link dhoondh raha hoon...</b>")
        direct, html = await asyncio.get_event_loop().run_in_executor(None, _hubcloud_direct, url)
        if not direct:
            notes.append(("hubcloud_page.html", html or ""))
            await msg.edit("❌ <b>HubCloud:</b> direct download link auto nahi mila "
                           "(JS / captcha / naya layout). Page HTML debug ke liye bhej raha hoon.")
            return None

    LOGGER.info(f"[MegaURL] {kind} direct: {direct[:120]}")
    name = _safe_filename(await _get_filename_from_url(direct))
    return await _download_url(direct, name, msg, message)      # download_dir mein save


async def _download_mega_progress(url: str, dest_dir: str, msg) -> str | None:
    """File link -> pure-Python downloader (live progress). Folder / unexpected error -> megatools fallback."""
    from ..utils.mega_py import MegaError, download_public_file, parse_link

    try:
        parse_link(url)
    except MegaError:
        return await download_mega(url, dest_dir, msg)

    last = [0.0]

    async def _prog(done, total, speed, name):
        if time.time() - last[0] < 4:
            return
        last[0] = time.time()
        try:
            await msg.edit(
                f"<b>📥 Mega se download ho raha hai</b>\n"
                f"<code>{name[:60]}</code>\n"
                f"{_bar(done, total)}\n"
                f"📦 {done / 1048576:.0f} / {total / 1048576:.0f} MB • ⚡ {speed / 1048576:.1f} MB/s"
            )
        except Exception:
            pass

    try:
        return await download_public_file(url, dest_dir, progress=_prog)
    except asyncio.CancelledError:
        raise
    except MegaError as e:
        await msg.edit(f"❌ <b>Mega download failed!</b>\n<code>{str(e)[:300]}</code>")
        return None
    except Exception as e:  # noqa: BLE001
        LOGGER.warning(f"[MegaURL] python downloader fail ({e!r}) — megatools try")
        return await download_mega(url, dest_dir, msg)


# ─────────────────────────────────────────────
#  Core pipeline: download -> /url flow -> upload
# ─────────────────────────────────────────────

async def run_pipeline(bot: Client, message: Message, msg, url: str, kind: str = "mega",
                       custom_name: str | None = None, show_buttons: bool = False) -> bool:
    """msg = status message (CancelMsg proxy ho sakta hai). True = upload tak pahunch gaya."""
    try:
        from ..utils.uploads.telegram import prewarm_uploader
        asyncio.ensure_future(prewarm_uploader(message.from_user.id))
    except Exception:
        pass

    dest = os.path.join(download_dir, f"megaurl_{message.from_user.id}_{int(time.time())}")
    os.makedirs(dest, exist_ok=True)
    notes: list = []
    try:
        filepath = await _download_any(url, kind, dest, msg, message, notes)
        if not filepath or not os.path.isfile(filepath):
            for fname, content in notes:                    # debug attachments (hubcloud html)
                try:
                    p = os.path.join(dest, fname)
                    with open(p, "w", encoding="utf-8") as f:
                        f.write(content)
                    await message.reply_document(p, caption=f"📄 {kind} debug")
                except Exception:
                    pass
            return False

        # Mega file dest_dir mein aati hai; /url jaisa download_dir mein le aao (+ optional custom name)
        final_name = _safe_filename(custom_name or os.path.basename(filepath))
        final_path = os.path.join(download_dir, final_name)
        if os.path.abspath(final_path) != os.path.abspath(filepath):
            if os.path.exists(final_path):
                final_name = f"{int(time.time())}_{final_name}"
                final_path = os.path.join(download_dir, final_name)
            try:
                os.replace(filepath, final_path)
                filepath = final_path
            except Exception as e:  # noqa: BLE001
                LOGGER.warning(f"[MegaURL] move fail ({e!r}) — original path se hi chalate hain")

        user_id = message.from_user.id
        _url_sessions[user_id] = {
            "filepath": filepath,
            "msg": msg,
            "orig_name": os.path.basename(filepath),
            "message": message,
        }
        size_mb = os.path.getsize(filepath) / 1048576
        await msg.edit(f"<b>✅ Downloaded</b> • {size_mb:.1f} MB\n<code>{os.path.basename(filepath)[:80]}</code>")

        if show_buttons:
            await _show_url_options(msg, user_id, filepath)
        else:
            await _auto_process_and_upload(bot, user_id, msg, message)
        return True
    finally:
        shutil.rmtree(dest, ignore_errors=True)


async def run_source_upload(bot: Client, message: Message, kind: str, url: str) -> bool:
    """/rtimega se: ek resolved source (mega/dlbeta/hubcloud) ko alag status msg + Cancel button ke saath
    download karke Telegram pe upload karo."""
    msg = await message.reply(f"<b>🧪 [{kind}] download → upload test</b>\n<code>{url[:150]}</code>")
    ok = {"v": False}

    async def _body(pmsg):
        ok["v"] = await run_pipeline(bot, message, pmsg, url, kind=kind)

    try:
        await job_ctl.run_cancelable(msg, message.from_user.id, f"RTI test: {kind}", _body)
    except Exception as e:  # noqa: BLE001
        LOGGER.error(f"[MegaURL] {kind} test error: {e!r}")
        try:
            await msg.edit(f"❌ <b>[{kind}]</b> <code>{type(e).__name__}: {str(e)[:250]}</code>")
        except Exception:
            pass
    return ok["v"]


# ─────────────────────────────────────────────
#  /megaurl command
# ─────────────────────────────────────────────

@Client.on_message(filters.command("megaurl"))
async def megaurl_cmd(bot: Client, message: Message):
    c = await check_chat(message, chat="Both")
    if not c:
        return
    await AddUserToDatabase(bot, message)

    raw = message.text.split(None, 1)[1].strip() if len(message.command) > 1 else ""
    if not raw and message.reply_to_message and message.reply_to_message.text:
        m = _MEGA_URL_RE.search(message.reply_to_message.text)
        raw = m.group(0) if m else ""

    url, custom_name, _auth, show_buttons, _extract = (
        _parse_url_args(raw) if raw else ("", None, None, False, False))

    if not url or not is_mega_link(url):
        await message.reply(
            "<b>Usage:</b>\n"
            "• <code>/megaurl &lt;mega_link&gt;</code> – Auto-process with saved settings + upload\n"
            "• <code>/megaurl &lt;mega_link&gt; -vt</code> – Manual option buttons\n"
            "• <code>/megaurl &lt;mega_link&gt; | &lt;filename&gt;</code> – Custom filename\n"
            "• Mega link pe reply karke bhi chalega\n\n"
            "OG upload chahiye? <code>/mega</code> • Upload + encode? <code>/meganow</code>"
        )
        return

    from ..utils.cooldown import check as _cd_check
    _wait = _cd_check(message.from_user.id, exempt=bool(await check_chat(message, chat="Sudo")))
    if _wait:
        await message.reply(f"⏳ Thoda ruko — agla link <b>{max(1, round(_wait / 60))} min</b> baad bhej sakte ho.")
        return

    msg = await message.reply("<b>🔍 Mega link check ho raha hai...</b>")

    async def _body(pmsg):
        await run_pipeline(bot, message, pmsg, url, kind="mega",
                           custom_name=custom_name, show_buttons=show_buttons)

    await job_ctl.run_cancelable(msg, message.from_user.id, "Mega URL Upload", _body)
