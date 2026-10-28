"""
rti_bypass.py — RTI ke baaki sources (HubCloud / WatchNow / DLBeta / Mega) ka REAL upload test
===========================================================================================
rti_mega.py zipper se destination tak pahuncha deta hai. Ye plugin agla hop karta hai aur file
Telegram tak upload karwa ke dikhata hai:

  mega      mega.nz link                         -> /mega jaisa download -> upload
  hubcloud  hubcloud.ist/drive/ID -> (5s) "Generate Direct Download Link" -> 10Gbps / FSL / PixelServer / Buzz
            -> best server ka direct link        -> /url jaisa download -> upload
  watchnow  codedew.com/streambeta -> "Download v1..v3" -> zipper -> final link -> /url jaisa upload
  dlbeta    pixeldrain.net/u/ID?download -> https://pixeldrain.dev/api/file/ID?download -> /url jaisa upload

Commands:
  /rtiup <rti_page_url> <ep> <mega|hubcloud|watchnow|dlbeta|all> [debug]
        resolve + download + upload (saara process trace mein + upload progress alag message mein)
  /bypass <hubcloud_drive_url>      (alias: /b)
        HubCloud ke saare download servers ke destination links ki list
        (pixeldrain link dene par uska direct API link)
"""

import asyncio
import html as _html
import os
import re
import shutil
import time
import traceback
import uuid
from urllib.parse import urlparse

import requests
from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import Message

from .. import LOGGER, download_dir
from ..utils import job_ctl
from ..utils.helper import check_chat
from . import rti_downloader as rd
from . import rti_mega as rm

SERVER_PRIORITY = ("10gbps", "fsl", "pixel", "buzz")          # kaun sa server pehle try ho
_SKIP_SERVER = ("telegram", "android", "watch online", "tutorial", "account", "login")

_JS_LINKS = """
return Array.from(document.querySelectorAll('a')).map(function(a){
  var r = a.getBoundingClientRect();
  return {text: (a.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 80), href: a.href || '',
          vis: (r.width > 0 && r.height > 0)};
}).filter(function(x){ return x.href && x.text; });
"""

_UA = {"User-Agent": rd.HEADERS.get("User-Agent", "Mozilla/5.0")}


# ─────────────────────────────────────────────
#  pixeldrain
# ─────────────────────────────────────────────
def pixeldrain_direct(url: str):
    m = re.search(r"pixeldrain\.(?:net|dev|com)/(?:u|api/file)/([A-Za-z0-9]+)", url or "", re.I)
    return f"https://pixeldrain.dev/api/file/{m.group(1)}?download" if m else None


# ─────────────────────────────────────────────
#  HubCloud: drive page -> servers
# ─────────────────────────────────────────────
def _close_other_tabs(driver, main: str):
    try:
        for h in list(driver.window_handles):
            if h != main:
                driver.switch_to.window(h)
                driver.close()
        driver.switch_to.window(main)
    except Exception:
        pass


def hubcloud_servers(driver, drive_url: str, tr: rm.Trace) -> list:
    """drive page kholo -> loading (~5s) -> 'Generate Direct Download Link' click -> server buttons.
    Returns [{name, href, downloadable}]"""
    t0 = time.time()
    try:
        driver.get(drive_url)
    except Exception as e:
        tr.add(f"(hubcloud page load warning: {type(e).__name__})")
    main = driver.current_window_handle
    tr.add(f"HUBCLOUD open: {drive_url[:80]}")
    deadline = time.time() + 100
    last_click, last_sig, clicks = 0.0, None, 0
    while time.time() < deadline:
        _close_other_tabs(driver, main)
        st = rm._page_state(driver)
        sig = (st["url"], st["head"][:60], len(st["btns"]))
        if sig != last_sig:
            last_sig = sig
            tr.add(f"HUB STATE '{st['title'][:40]}' | btns: "
                   + (" | ".join(b["text"] for b in st["btns"] if b["text"])[:130] or "-"))
            tr.shot(driver, "hubcloud state")
        try:
            body = driver.execute_script("return document.body ? document.body.innerText : ''") or ""
        except Exception:
            body = ""
        links = []
        try:
            links = driver.execute_script(_JS_LINKS) or []
        except Exception:
            pass
        servers = [l for l in links if re.match(r"^\W*download\b", l["text"], re.I) and "[" in l["text"]
                   or "watch online" in l["text"].lower() or "from telegram" in l["text"].lower()
                   or "android app" in l["text"].lower()]
        if "download link generated" in body.lower() and servers:
            out = []
            for l in servers:
                dl = not any(x in l["text"].lower() for x in _SKIP_SERVER)
                out.append({"name": l["text"], "href": l["href"], "downloadable": dl})
            tr.add(f"✅ HUBCLOUD servers ({time.time() - t0:.1f}s): "
                   + ", ".join(f"{o['name']}{'' if o['downloadable'] else ' (skip)'}" for o in out))
            tr.shot(driver, "hubcloud servers", force=True)
            return out
        # Generate button
        btn = None
        for b in st["btns"]:
            t = (b["text"] or "").lower()
            if "generate" in t and "link" in t and not b["dis"]:
                btn = b
                break
        if btn and time.time() - last_click > 4:
            last_click = time.time()
            clicks += 1
            tr.add(f"👆 CLICK #{clicks}: '{btn['text']}'")
            try:
                el = driver.find_element(rd.By.CSS_SELECTOR, f'[data-bx="{btn["i"]}"]')
                try:
                    el.click()
                except Exception:
                    driver.execute_script("arguments[0].click();", el)
            except Exception as e:
                tr.add(f"    ❌ click fail: {type(e).__name__}")
            time.sleep(0.2)
            continue
        time.sleep(rm.TICK)
    tr.add("⏰ HubCloud servers nahi mile")
    tr.shot(driver, "hubcloud TIMEOUT", force=True)
    try:
        with open(os.path.join(tr.workdir, "last_page_hubcloud.html"), "w", encoding="utf-8") as f:
            f.write(driver.page_source or "")
    except Exception:
        pass
    return []


def inspect_link(href: str, tr: rm.Trace, referer: str = "") -> dict:
    """Server button ka link follow karo (redirects) — final URL + kya wo file hai? (body download nahi)"""
    info = {"href": href, "final": href, "is_file": False, "ctype": "", "size": 0, "status": 0, "name": ""}
    try:
        hdr = dict(_UA)
        if referer:
            hdr["Referer"] = referer
        r = requests.get(href, headers=hdr, allow_redirects=True, stream=True, timeout=20)
        info["status"] = r.status_code
        info["final"] = r.url
        info["ctype"] = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        info["size"] = int(r.headers.get("Content-Length") or 0)
        cd = r.headers.get("Content-Disposition") or ""
        m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', cd, re.I)
        info["name"] = m.group(1) if m else ""
        is_html = info["ctype"] in ("text/html", "application/xhtml+xml", "")
        info["is_file"] = r.status_code == 200 and (not is_html or "attachment" in cd.lower()) \
            and (info["size"] > 5_000_000 or "video" in info["ctype"] or "octet" in info["ctype"]
                 or "attachment" in cd.lower() or "matroska" in info["ctype"])
        if is_html and r.status_code == 200 and not info["is_file"]:
            # HTML page — andar direct/pixeldrain link ho sakta hai
            body = r.text[:200000] if hasattr(r, "text") else ""
            px = pixeldrain_direct(body) or None
            vm = rm._VIDEO_URL_RE.search(body)
            if px:
                info["final"], info["is_file"] = px, True
            elif vm:
                info["final"], info["is_file"] = vm.group(0), True
        r.close()
    except Exception as e:
        tr.add(f"    inspect fail {type(e).__name__}: {str(e)[:80]}")
    return info


def pick_best(servers: list, tr: rm.Trace, referer: str = ""):
    """Priority order mein har downloadable server inspect karo, pehla jo file de wahi."""
    cand = [s for s in servers if s["downloadable"]]
    cand.sort(key=lambda s: next((i for i, k in enumerate(SERVER_PRIORITY) if k in s["name"].lower()), 99))
    for s in cand:
        info = inspect_link(s["href"], tr, referer)
        s["info"] = info
        tr.add(f"  • {s['name']}: status={info['status']} file={'✅' if info['is_file'] else '❌'} "
               f"{info['ctype'] or '?'} {info['size'] / 1048576:.0f}MB -> {info['final'][:90]}")
        if info["is_file"]:
            return s, info
    return None, None


# ─────────────────────────────────────────────
#  Final-hop chain (kisi bhi destination se direct link tak)
# ─────────────────────────────────────────────
def finalize(url: str, driver, tr: rm.Trace):
    """url -> ("mega", url) | ("direct", url) | (None, None)"""
    if not url:
        return None, None
    if rm._mega_in(url):
        return "mega", rm._mega_in(url)
    px = pixeldrain_direct(url)
    if px:
        tr.add(f"pixeldrain -> {px}")
        return "direct", px
    if rm._HUBDRIVE_RE.search(url):
        if driver is None:
            tr.add("❌ hubcloud ke liye Chrome chahiye")
            return None, None
        servers = hubcloud_servers(driver, url, tr)
        s, info = pick_best(servers, tr, referer=url)
        if s:
            return "direct", info["final"]
        return None, None
    return "direct", url


def watchnow_second_hop(driver, streambeta_url: str, tr: rm.Trace):
    """streambeta page: Download v1 -> (v2 -> v3) click -> zipper steps -> final URL."""
    for label in ("download v1", "download v2", "download v3"):
        tr.add(f"WATCHNOW: '{label}' try")
        st = rm._page_state(driver)
        btn = next((b for b in st["btns"] if label in (b["text"] or "").lower() and not b["dis"]), None)
        if not btn:
            tr.add(f"    '{label}' button nahi dikha — btns: "
                   + (" | ".join(b["text"] for b in st["btns"] if b["text"])[:150] or "-"))
            time.sleep(1.0)
            st = rm._page_state(driver)
            btn = next((b for b in st["btns"] if label in (b["text"] or "").lower() and not b["dis"]), None)
            if not btn:
                continue
        tr.add(f"👆 CLICK '{btn['text']}' href={btn['href'][:60] or '-'}")
        try:
            el = driver.find_element(rd.By.CSS_SELECTOR, f'[data-bx="{btn["i"]}"]')
            try:
                el.click()
            except Exception:
                driver.execute_script("arguments[0].click();", el)
        except Exception as e:
            tr.add(f"    ❌ click fail: {type(e).__name__}")
            continue
        time.sleep(0.5)
        res = rm.zipper_flow(driver, None, "generic", tr, navigate=False)
        if res:
            return res
        try:      # fail hua — streambeta page pe wapas, agla version
            driver.get(streambeta_url)
        except Exception:
            pass
    return None


def post_hop(kind: str, dest: str, driver, tr: rm.Trace):
    """rti_mega.resolve_blocking ka `post`: destination -> (type, final_url) tuple."""
    if kind == "watchnow":
        if driver is None:
            return None
        nxt = watchnow_second_hop(driver, dest, tr)
        if not nxt:
            return None
        dest = nxt
    ftype, furl = finalize(dest, driver, tr)
    if furl:
        tr.add(f"🎯 {kind}: FINAL ({ftype}) {furl}")
        return (ftype, furl)
    return None


# ─────────────────────────────────────────────
#  Upload (same pipeline jo /url aur /mega use karte hain)
# ─────────────────────────────────────────────
async def upload_final(client: Client, message: Message, kind: str, ftype: str, furl: str):
    from .url_upload import (_auto_process_and_upload, _download_url, _get_filename_from_url,
                             _safe_filename, _url_sessions)
    uid = message.from_user.id
    um = await message.reply(f"<b>💠 [{kind}] Downloading...</b>", parse_mode=ParseMode.HTML)

    async def _body(pm):
        try:
            if ftype == "mega":
                from .mega_download import download_mega
                dest = os.path.join(download_dir, f"rtiup_{int(time.time())}_{kind}")
                filepath = await download_mega(furl, dest, pm)
            else:
                name = _safe_filename(await _get_filename_from_url(furl))
                filepath = await _download_url(furl, name, pm, message)
        except Exception as e:
            await pm.edit(f"❌ [{kind}] Download failed: <code>{_html.escape(str(e)[:200])}</code>")
            return False
        if not filepath or not os.path.isfile(filepath):
            await pm.edit(f"❌ [{kind}] Download failed or file not found.")
            return False
        _url_sessions[uid] = {"filepath": filepath, "msg": pm, "orig_name": os.path.basename(filepath),
                              "message": message}
        await _auto_process_and_upload(client, uid, pm, message)
        return True

    try:
        _job, res = await job_ctl.run_cancelable(um, uid, f"RTI {kind} test", _body)
        return bool(res)
    except Exception as e:
        LOGGER.error(f"[RTI-Up] {kind}: {traceback.format_exc()}")
        try:
            await um.edit(f"❌ [{kind}] error: <code>{_html.escape(str(e)[:200])}</code>")
        except Exception:
            pass
        return False


# ─────────────────────────────────────────────
#  /rtiup
# ─────────────────────────────────────────────
@Client.on_message(filters.command("rtiup"))
async def rtiup_command(client: Client, message: Message):
    c = await check_chat(message, chat="Sudo")
    if not c:
        return
    if not rd.SELENIUM_OK:
        await message.reply("❌ Selenium install nahi hai!")
        return
    parts = message.text.split()[1:]
    debug = any(p.lower() == "debug" for p in parts)
    parts = [p for p in parts if p.lower() != "debug"]
    if len(parts) < 2 or not parts[1].isdigit():
        await message.reply(
            "**Usage:** `/rtiup <rti_page_url> <ep> <mega|hubcloud|watchnow|dlbeta|all> [debug]`\n\n"
            "Resolve → download → upload (urlpreset ke saath, jaise /url). `all` = chaaron ek ke baad ek.\n"
            "HubCloud ke saare servers ki list: `/bypass <hubcloud_drive_url>`"
        )
        return
    page, ep = parts[0], int(parts[1])
    kind = parts[2].lower() if len(parts) > 2 else "mega"
    kinds = ["mega", "hubcloud", "watchnow", "dlbeta"] if kind == "all" else [kind]
    if any(k not in ("mega", "hubcloud", "watchnow", "dlbeta") for k in kinds):
        await message.reply("Kind: `mega` `hubcloud` `watchnow` `dlbeta` ya `all`")
        return

    workdir = os.path.join(download_dir, "_rtiup", uuid.uuid4().hex[:8])
    tr = rm.Trace(workdir, debug=debug)
    msg = await message.reply("🧪 **RTI → upload test shuru...**")
    fut = job_ctl.run_in_thread(rm.resolve_blocking, page, ep, kinds, tr, post_hop)

    async def _live():
        last = ""
        while True:
            await asyncio.sleep(2.5)
            t = tr.tail(14)
            if t != last:
                last = t
                try:
                    await msg.edit(f"🧪 **Resolve ho raha hai...**\n<pre>{_html.escape(t[-3300:])}</pre>",
                                   parse_mode=ParseMode.HTML)
                except Exception:
                    pass

    live = asyncio.ensure_future(_live())
    results: dict = {}
    try:
        results = await asyncio.wait_for(fut, timeout=rm.ZIPPER_TOTAL_TIMEOUT * 3 * len(kinds) + 120)
    except asyncio.TimeoutError:
        tr.add("⏰ resolve timeout")
    except Exception as e:
        tr.add(f"💥 {type(e).__name__}: {e}")
    finally:
        live.cancel()

    lines = []
    for k in kinds:
        v = results.get(k)
        if isinstance(v, tuple):
            lines.append(f"✅ <b>{k}</b> → <code>{_html.escape(v[1][:150])}</code>")
        else:
            lines.append(f"❌ <b>{k}</b> → final link nahi mila")
    try:
        await msg.edit("\n".join(lines) + f"\n\n<pre>{_html.escape(tr.tail(10)[-2200:])}</pre>",
                       parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception:
        pass

    try:      # trace + (fail pe) html/screens
        tp = os.path.join(workdir, "trace.txt")
        with open(tp, "w", encoding="utf-8") as f:
            f.write(f"PAGE: {page} EP: {ep} KINDS: {kinds}\nRESULT: {results}\n\n{tr.full()}\n")
        await message.reply_document(tp, caption="🧾 Resolve trace")
        if any(not isinstance(results.get(k), tuple) for k in kinds):
            for name in sorted(os.listdir(workdir)):
                if name.endswith(".html"):
                    await message.reply_document(os.path.join(workdir, name), caption=f"📄 {name}")
        if tr.shots:
            from pyrogram.types import InputMediaPhoto
            try:
                await message.reply_media_group([InputMediaPhoto(p, caption=(cp or "")[:200]) for p, cp in tr.shots[:10]])
            except Exception:
                pass
    except Exception as e:
        LOGGER.warning(f"[RTI-Up] trace bhejne mein error: {e!r}")

    # ── Upload: ek ke baad ek ──
    summary = []
    for k in kinds:
        v = results.get(k)
        if not isinstance(v, tuple):
            summary.append(f"❌ {k}: resolve fail")
            continue
        t_up = time.time()
        ok = await upload_final(client, message, k, v[0], v[1])
        summary.append(f"{'✅' if ok else '❌'} {k}: upload {'ho gaya' if ok else 'fail'} ({time.time() - t_up:.0f}s)")
    await message.reply("📊 <b>RTI upload test</b>\n" + "\n".join(summary), parse_mode=ParseMode.HTML)
    shutil.rmtree(workdir, ignore_errors=True)


# ─────────────────────────────────────────────
#  /bypass  (alias /b)
# ─────────────────────────────────────────────
@Client.on_message(filters.command(["bypass", "b"]))
async def bypass_command(client: Client, message: Message):
    c = await check_chat(message, chat="Sudo")
    if not c:
        return
    parts = message.text.split()
    url = next((p for p in parts[1:] if p.startswith("http")), None)
    debug = any(p.lower() == "debug" for p in parts)
    if not url:
        await message.reply(
            "**Usage:** `/bypass https://hubcloud.ist/drive/xxxx`  (alias `/b`)\n"
            "HubCloud ke saare download servers ke destination links deta hai.\n"
            "Pixeldrain link dene par direct API link milta hai."
        )
        return

    px = pixeldrain_direct(url)
    if px:
        await message.reply(f"✅ <b>Pixeldrain direct</b>\n<code>{_html.escape(px)}</code>\n\nUpload: <code>/url {_html.escape(px)}</code>",
                            parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        return
    if not rm._HUBDRIVE_RE.search(url):
        await message.reply("❌ Abhi sirf HubCloud drive link (`hubcloud.*/drive/ID`) ya pixeldrain link supported hai.")
        return
    if not rd.SELENIUM_OK:
        await message.reply("❌ Selenium install nahi hai!")
        return

    workdir = os.path.join(download_dir, "_bypass", uuid.uuid4().hex[:8])
    tr = rm.Trace(workdir, debug=debug)
    msg = await message.reply("🔓 **HubCloud bypass shuru...**")

    def _work():
        dfut = rm._DriverFuture(tr)
        try:
            drv = dfut.get()
            servers = hubcloud_servers(drv, url, tr)
            for s in servers:
                if s["downloadable"]:
                    s["info"] = inspect_link(s["href"], tr, referer=url)
            return servers
        finally:
            dfut.kill()

    fut = job_ctl.run_in_thread(_work)

    async def _live():
        last = ""
        while True:
            await asyncio.sleep(2.5)
            t = tr.tail(10)
            if t != last:
                last = t
                try:
                    await msg.edit(f"🔓 **HubCloud bypass chal raha hai...**\n<pre>{_html.escape(t[-3000:])}</pre>",
                                   parse_mode=ParseMode.HTML)
                except Exception:
                    pass

    live = asyncio.ensure_future(_live())
    servers = []
    try:
        servers = await asyncio.wait_for(fut, timeout=240)
    except Exception as e:
        tr.add(f"💥 {type(e).__name__}: {e}")
    finally:
        live.cancel()

    if not servers:
        await msg.edit("❌ <b>Servers nahi mile.</b> Trace neeche — wahi bhej do.", parse_mode=ParseMode.HTML)
    else:
        out = ["✅ <b>HubCloud servers</b>\n"]
        for s in servers:
            if not s["downloadable"]:
                out.append(f"⏭ {_html.escape(s['name'])} <i>(download link nahi)</i>")
                continue
            i = s.get("info") or {}
            tag = "✅ file" if i.get("is_file") else "⚠️ page/redirect"
            size = f" • {i['size'] / 1048576:.0f} MB" if i.get("size") else ""
            out.append(f"{tag} <b>{_html.escape(s['name'])}</b>{size}\n<code>{_html.escape(i.get('final') or s['href'])}</code>")
        out.append("\nUpload: <code>/url &lt;link&gt;</code>")
        await msg.edit("\n\n".join(out)[:4000], parse_mode=ParseMode.HTML, disable_web_page_preview=True)

    try:
        tp = os.path.join(workdir, "trace.txt")
        with open(tp, "w", encoding="utf-8") as f:
            f.write(f"URL: {url}\n\n{tr.full()}\n")
        await message.reply_document(tp, caption="🧾 Trace")
        if not servers:
            for name in sorted(os.listdir(workdir)):
                if name.endswith(".html"):
                    await message.reply_document(os.path.join(workdir, name), caption=f"📄 {name}")
        if tr.shots:
            from pyrogram.types import InputMediaPhoto
            try:
                await message.reply_media_group([InputMediaPhoto(p, caption=(cp or "")[:200]) for p, cp in tr.shots[:10]])
            except Exception:
                pass
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
