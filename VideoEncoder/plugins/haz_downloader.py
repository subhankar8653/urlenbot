"""
haz_downloader.py  v4 (AZAPI captcha API)
========================
Command:
  /Haz <series_page_url>   (hindianimeszone.com ka ek anime/series post)
  /hazapi                  (captcha provider config + credits status — sudo only)

Flow (jaisa Subhankar ne screenshots + text mein bataya):
  1. Series page (static HTML, requests+BS4 se seedha scrape) se title,
     available languages ("Language: Hindi-Tamil-Telugu-English-Japanese"
     field se) aur har episode ke 5 quality-links (480p x264 / 720p x264 /
     720p x265 / 1080p x265 / 1080p HQ) nikalte hain.
  2. Bot pehle Language buttons dikhata hai (Hindi/Tamil/Telugu/...).
  3. Phir Quality buttons — sirf 3 allowed: 480p x264, 720p x265, 1080p x265
     (720p x264 aur 1080p HQ jaanbujh kar skip, jaisa bataya gaya).
  4. Phir episode list (RTI/Toono jaisa hi multi-select button menu).
  5. Chosen quality ka link (002.hindianimeszone.com/download1.php?...)
     kholne pe kabhi-kabhi Cloudflare Turnstile ka "Verify You're Human"
     gate aata hai. Cookies ki jagah ab captcha-provider API use hota hai
     (Peak.fo / NopeCHA / AZAPI): bot gate page se sitekey nikaalta hai,
     provider se token leta hai, form submit karta hai, aur gate-pass ki
     cookies ~55 min cache karta hai (ek token se poori series). Phir server-list
     page (GDFlix / MEGA / Gdshare / FilePress) se sirf MEGA link nikalta hai.
     Env vars: PEAKFO_KEY + CAPTCHA_PROXY (Peak.fo, default agar key ho),
     ya NOPECHA_KEY/NOPECHA_PROXY, ya AZAPI_KEY; CAPTCHA_PROVIDER=peakfo|nopecha|azapi.
  6. MEGA link ko mega_download.py ke existing download_mega() se download
     karte hain.
  7. url_upload.py ke existing audio/subtitle-filter helpers reuse karte
     hain — sirf chuni hui language ka audio track + English subtitle
     rakhte hain, baaki audio tracks hata dete hain (jaisa "Hindi select
     kiya to sirf Hindi audio, Tamil select kiya to sirf Tamil audio"
     bataya gaya).
  8. url_upload.py ka hi _do_upload() reuse karke Telegram pe upload.

NOTE / assumptions (live site pe test karke confirm karna padega):
  - Is site pe HAR SEASON APNA ALAG POST/URL hai (tabs nahi, jaisa RTI mein
    hota hai) — isliye is command mein in-bot "season selector" nahi hai;
    jis season ka chahiye uska URL hi /Haz ko do.
  - Server-list page pe MEGA row ka exact HTML structure is sandbox se
    (network band hai) live dekh ke confirm nahi ho paaya — isliye BS4-based
    kaafi fallback strategies + step-by-step LOGGER.info daale hain
    (jaisa /toono ke codedew step mein), taaki agar kahin miss ho to turant
    pata chale (Debug block message mein dikhega) aur ek round mein hi
    fix ho jaaye.
"""

import asyncio
import json
import os
import re
import time
import uuid
from urllib.parse import unquote_plus, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from pyrogram import Client, filters
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from .. import LOGGER, download_dir
from ..utils.helper import check_chat
from ..utils.url_processor import get_audio_streams
from .rti_downloader import _kb
from .mega_download import is_mega_link, download_mega
from .url_upload import (
    get_subtitle_streams,
    _is_english_sub_stream,
    _keep_audio_streams,
    _keep_subtitle_streams,
    _do_upload,
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# In yehi 3 qualities dikhani hain (5 available hain page pe, baaki 2 skip)
HAZ_QUALITIES = ["480p x264", "720p x265", "1080p x265"]

# Language button label -> ffprobe audio-language codes jo isse match karte hain
LANG_STREAM_CODES = {
    "hindi":    {"hin", "hi", "hindi"},
    "tamil":    {"tam", "ta", "tamil"},
    "telugu":   {"tel", "te", "telugu"},
    "english":  {"eng", "en", "english"},
    "japanese": {"jpn", "ja", "jap", "japanese"},
}

PER_PAGE = 10

HAZ_SESSIONS = {}
HAZ_SESSION_TIMEOUT = 3600

# ─────────────────────────────────────────────
#  AZAPI.ai captcha API config (env vars se)
#  Dashboard: https://app.azapi.ai  ->  "API Key" card  ->  View Key
#  Header format (AZAPI docs): Authorization: prod-xxxx  (Sand ho to sand-xxxx)
# ─────────────────────────────────────────────
def _azapi_cfg() -> dict:
    return {
        "key": (os.getenv("AZAPI_KEY") or "").strip(),
        "base": (os.getenv("AZAPI_BASE_URL") or "https://api.azapi.ai").strip().rstrip("/"),
        # NOTE: exact Turnstile endpoint path AZAPI ke Postman docs se confirm karke
        # env mein daalo. Default sirf placeholder hai.
        "path": (os.getenv("AZAPI_CAPTCHA_PATH") or "/v1/captcha/turnstile").strip(),
        # JSON body template. %URL% aur %SITEKEY% auto-replace hote hain.
        "payload": (os.getenv("AZAPI_PAYLOAD")
                    or '{"type":"turnstile","url":"%URL%","sitekey":"%SITEKEY%"}'),
    }


_TOKEN_KEYS = ("token", "cf-turnstile-response", "captcha_token", "captchaToken",
               "gRecaptchaResponse", "solution", "result", "code", "answer", "response", "text")


def _find_token(obj, depth: int = 0):
    """AZAPI response (data wrapper ke andar bhi) mein se token string dhoondo."""
    if depth > 5:
        return None
    if isinstance(obj, str):
        return obj if len(obj) >= 20 else None
    if isinstance(obj, dict):
        for k in _TOKEN_KEYS:
            if k in obj:
                t = _find_token(obj[k], depth + 1)
                if t:
                    return t
        for v in obj.values():
            if isinstance(v, (dict, list)):
                t = _find_token(v, depth + 1)
                if t:
                    return t
    if isinstance(obj, list):
        for v in obj:
            t = _find_token(v, depth + 1)
            if t:
                return t
    return None


def _azapi_solve_turnstile(page_url: str, sitekey: str, log) -> str | None:
    """AZAPI se Turnstile token lo. Fail hone par None (reason log mein)."""
    cfg = _azapi_cfg()
    if not cfg["key"]:
        log("❌ AZAPI_KEY set nahi hai (config.env / Heroku config vars mein daalo)")
        return None

    body = cfg["payload"].replace("%URL%", page_url).replace("%SITEKEY%", sitekey)
    try:
        payload = json.loads(body)
    except Exception as e:
        log(f"❌ AZAPI_PAYLOAD valid JSON nahi hai: {str(e)[:80]}")
        return None

    endpoint = cfg["base"] + (cfg["path"] if cfg["path"].startswith("/") else "/" + cfg["path"])
    headers = {"Authorization": cfg["key"], "Content-Type": "application/json"}
    try:
        r = requests.post(endpoint, json=payload, headers=headers, timeout=120)
    except Exception as e:
        log(f"❌ AZAPI request fail: {str(e).splitlines()[0][:120]}")
        return None

    try:
        data = r.json()
    except Exception:
        log(f"❌ AZAPI HTTP {r.status_code}, non-JSON reply: {r.text[:150]!r}")
        return None

    token = _find_token(data)
    if r.status_code != 200 or not token:
        # key kabhi log mein nahi jaati; sirf response ka chhota hissa
        log(f"❌ AZAPI HTTP {r.status_code} — token nahi mila. Reply: {json.dumps(data)[:200]}")
        return None

    log(f"✅ AZAPI se Turnstile token mila ({len(token)} chars)")
    return token


def _prune_haz_sessions():
    now = time.time()
    stale = [k for k, v in HAZ_SESSIONS.items() if now - v.created > HAZ_SESSION_TIMEOUT]
    for k in stale:
        HAZ_SESSIONS.pop(k, None)


# ─────────────────────────────────────────────
#  Step 1: series page -> title / languages / episodes (static HTML)
# ─────────────────────────────────────────────
def discover_haz_items(series_url: str) -> dict:
    """
    Returns:
      {"title": str, "languages": [str,...], "episodes": [
          {"num": int, "code": str, "qualities": {q: href}}, ...
      ]}
    """
    r = requests.get(series_url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    title_tag = soup.find("h1")
    title = title_tag.get_text(strip=True) if title_tag else "Unknown"

    # ── Languages: "Language: Hindi-Tamil-Telugu-English-Japanese" field ──
    page_text = soup.get_text("\n")
    languages = []
    m = re.search(r'Language\s*:\s*([A-Za-z][A-Za-z\-\s]*)', page_text)
    if m:
        raw = m.group(1).splitlines()[0].strip()
        languages = [x.strip() for x in re.split(r'[-–]', raw) if x.strip()]
    if not languages:
        languages = ["Hindi", "English"]

    # ── Episodes: har <a href="...download1.php?code=X&q=Y"> ko nearest
    #    pichhle "Episode N" text se group karo (document order, class
    #    names pe depend nahi karta) ──
    episodes = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "download1.php" not in href:
            continue
        qm = re.search(r'[?&]code=([^&]+)&q=([^&]+)', href)
        if not qm:
            continue
        code, q = qm.group(1), unquote_plus(qm.group(2))
        if q not in HAZ_QUALITIES:
            continue

        ep_marker = a.find_previous(string=re.compile(r'^\s*Episode\s+\d+\s*$', re.IGNORECASE))
        if not ep_marker:
            continue
        num_m = re.search(r'\d+', ep_marker)
        if not num_m:
            continue
        ep_num = int(num_m.group())

        ep = episodes.setdefault(ep_num, {"num": ep_num, "code": code, "qualities": {}})
        ep["qualities"][q] = href
        if not ep.get("code"):
            ep["code"] = code

    ep_list = sorted(episodes.values(), key=lambda x: x["num"])
    return {"title": title, "languages": languages, "episodes": ep_list}


# ─────────────────────────────────────────────
#  Step 2: download1.php link -> (agar aaya to) "Verify You're Human"
#  gate pass karo -> server-list page se MEGA wala link nikaalo.
#  Poori tarah Selenium/JS-driven, is sandbox mein live test nahi ho paaya
#  (network band hai) — isliye har step LOGGER.info/warning karta hai.
# ─────────────────────────────────────────────
# ─────────────────────────────────────────────
#  NopeCHA Turnstile token API (default provider)
#  Docs: https://nopecha.com/api-reference  (Token → Turnstile)
#  Env:
#    NOPECHA_KEY    (optional — khali ho to IP-based free quota use hota hai)
#    NOPECHA_PROXY  (Turnstile ke liye docs mein REQUIRED: http://user:pass@host:port)
#                   Token usi IP se use hona chahiye, isliye bot ki saari requests
#                   bhi isi proxy se jaati hain.
#    CAPTCHA_PROVIDER = nopecha (default) | azapi
# ─────────────────────────────────────────────
NOPECHA_BASE = "https://api.nopecha.com"


def _nopecha_cfg() -> dict:
    return {
        "key": (os.getenv("NOPECHA_KEY") or "").strip(),
        "proxy": _proxy_url(),
    }


def _nopecha_headers(key: str) -> dict:
    h = {"Content-Type": "application/json"}
    if key:
        h["Authorization"] = f"Basic {key}"
    return h


def _proxy_dict(proxy_url: str):
    """'http://user:pass@host:port' -> NopeCHA ka proxy object."""
    if not proxy_url:
        return None
    u = urlparse(proxy_url if "://" in proxy_url else "http://" + proxy_url)
    if not u.hostname or not u.port:
        return None
    d = {"scheme": u.scheme or "http", "host": u.hostname, "port": u.port}
    if u.username:
        d["username"] = unquote_plus(u.username)
    if u.password:
        d["password"] = unquote_plus(u.password)
    return d


def _nopecha_err(r) -> str:
    try:
        j = r.json()
        return f"HTTP {r.status_code} code={j.get('code')} msg={j.get('message')} type={j.get('type')}"
    except Exception:
        return f"HTTP {r.status_code} {r.text[:120]!r}"


def _nopecha_solve_turnstile(page_url: str, sitekey: str, log) -> str | None:
    cfg = _nopecha_cfg()
    body = {"sitekey": sitekey, "url": page_url}
    proxy = _proxy_dict(cfg["proxy"])
    if proxy:
        body["proxy"] = proxy
    else:
        log("⚠️ NOPECHA_PROXY set nahi — docs ke hisaab se Turnstile ke liye proxy required hai, "
            "bina proxy ke try kar raha hoon")
    body["useragent"] = HEADERS["User-Agent"]
    headers = _nopecha_headers(cfg["key"])

    try:
        r = requests.post(f"{NOPECHA_BASE}/v1/token/turnstile", json=body, headers=headers, timeout=30)
    except Exception as e:
        log(f"❌ NopeCHA submit fail: {str(e).splitlines()[0][:120]}")
        return None
    if r.status_code != 200:
        log(f"❌ NopeCHA submit: {_nopecha_err(r)}")
        return None
    try:
        job_id = r.json().get("data")
    except Exception:
        job_id = None
    if not job_id:
        log(f"❌ NopeCHA ne job id nahi diya: {r.text[:120]!r}")
        return None
    log(f"⏳ NopeCHA job submit ho gaya, token ka wait...")

    deadline = time.time() + 100
    while time.time() < deadline:
        time.sleep(1.5)
        try:
            g = requests.get(f"{NOPECHA_BASE}/v1/token/turnstile", params={"id": job_id},
                             headers=headers, timeout=30)
        except Exception as e:
            log(f"⚠️ NopeCHA poll error: {str(e).splitlines()[0][:80]}")
            continue
        if g.status_code == 409:      # incomplete job — dubara try
            continue
        if g.status_code != 200:
            log(f"❌ NopeCHA result: {_nopecha_err(g)}")
            return None
        try:
            token = g.json().get("data")
        except Exception:
            token = None
        if isinstance(token, str) and len(token) > 20:
            log(f"✅ NopeCHA se Turnstile token mila ({len(token)} chars)")
            return token
        log(f"❌ NopeCHA result mein token nahi: {g.text[:120]!r}")
        return None
    log("❌ NopeCHA timeout (100s) — token nahi aaya")
    return None


def _nopecha_status() -> str:
    cfg = _nopecha_cfg()
    try:
        r = requests.get(f"{NOPECHA_BASE}/v1/status", headers=_nopecha_headers(cfg["key"]), timeout=15)
        j = r.json()
        if r.status_code != 200:
            return _nopecha_err(r)
        return (f"plan={j.get('plan')} status={j.get('status')} "
                f"credit={j.get('credit')}/{j.get('quota')} reset≈{int(j.get('ttl', 0)) // 60}min")
    except Exception as e:
        return f"error: {str(e).splitlines()[0][:100]}"


# ─────────────────────────────────────────────
#  Peak.fo Turnstile solver (official SDK: pip install peakfo)
#  Env: PEAKFO_KEY (pk_...), CAPTCHA_PROXY (ya purana NOPECHA_PROXY) —
#  Turnstile ke liye proxy chahiye aur bot ki requests bhi usi proxy se jaati hain.
# ─────────────────────────────────────────────
def _proxy_url() -> str:
    return ((os.getenv("CAPTCHA_PROXY") or os.getenv("NOPECHA_PROXY") or "").strip())


def _provider() -> str:
    p = (os.getenv("CAPTCHA_PROVIDER") or "").strip().lower()
    if p in ("peakfo", "nopecha", "azapi"):
        return p
    return "peakfo" if (os.getenv("PEAKFO_KEY") or "").strip() else "nopecha"


def _peak_client():
    from peakfo import PeakClient   # lazy import: SDK na ho to clear error mile
    return PeakClient((os.getenv("PEAKFO_KEY") or "").strip())


def _peak_solve_turnstile(page_url: str, sitekey: str, log) -> str | None:
    if not (os.getenv("PEAKFO_KEY") or "").strip():
        log("❌ PEAKFO_KEY set nahi hai")
        return None
    proxy = _proxy_url()
    try:
        from peakfo import AuthenticationError, InsufficientBalanceError, SolveError
    except Exception as e:
        log(f"❌ peakfo SDK install nahi hai (requirements.txt mein `peakfo` + redeploy): {str(e)[:80]}")
        return None

    # Attempts: proxy ke saath 2 baar, phir (agar allowed) bina proxy ke 1 baar.
    attempts = [proxy, proxy] if proxy else []
    if (os.getenv("PEAKFO_NO_PROXY_FALLBACK") or "1").strip() != "0":
        attempts.append(None)
    if not attempts:
        attempts = [None]

    for n, px in enumerate(attempts, 1):
        label = "proxy ke saath" if px else "bina proxy ke"
        log(f"⏳ Peak.fo solve try {n}/{len(attempts)} ({label})...")
        try:
            kwargs = {"sitekey": sitekey, "url": page_url}
            if px:
                kwargs["proxy"] = px
            res = _peak_client().solve_turnstile(**kwargs)
        except AuthenticationError:
            log("❌ Peak.fo: API key galat hai (AuthenticationError)")
            return None
        except InsufficientBalanceError:
            log("❌ Peak.fo: balance khatam (InsufficientBalanceError)")
            return None
        except SolveError as e:
            log(f"⚠️ Peak.fo solve fail ({label}): {str(e)[:100]}")
            time.sleep(2)
            continue
        except Exception as e:
            log(f"⚠️ Peak.fo error ({label}): {type(e).__name__}: {str(e)[:100]}")
            time.sleep(2)
            continue

        token = res.get("token") if isinstance(res, dict) else None
        if isinstance(token, str) and len(token) > 20:
            log(f"✅ Peak.fo se Turnstile token mila ({len(token)} chars, {label})")
            return token
        log(f"⚠️ Peak.fo reply mein token nahi: {str(res)[:120]}")

    log("❌ Peak.fo ke saare attempts fail — proxy IP Cloudflare ne reject kiya ho sakta hai "
        "(datacenter proxy). Residential/ISP proxy try karo.")
    return None


def _peak_status() -> str:
    if not (os.getenv("PEAKFO_KEY") or "").strip():
        return "PEAKFO_KEY not set"
    try:
        b = _peak_client().get_balance()
        return f"balance=${b.get('balance')}" if isinstance(b, dict) else str(b)[:100]
    except Exception as e:
        return f"error: {type(e).__name__}: {str(e)[:100]}"


def _solve_turnstile(page_url: str, sitekey: str, log) -> str | None:
    provider = _provider()
    if provider == "azapi":
        return _azapi_solve_turnstile(page_url, sitekey, log)
    if provider == "peakfo":
        return _peak_solve_turnstile(page_url, sitekey, log)
    return _nopecha_solve_turnstile(page_url, sitekey, log)


# Gate ek baar pass ho jaye to site ~1 ghante tak dubara verify nahi maangti —
# isliye cookies cache karte hain (55 min) taaki ek token se poori series nikle.
GATE_CACHE = {"jar": None, "saved_at": 0.0}
GATE_CACHE_TTL = 55 * 60


def _is_gate(text: str) -> bool:
    low = text.lower()
    return ("not a robot" in low or "verify you" in low
            or "cf-turnstile" in low or "challenges.cloudflare.com/turnstile" in low)


def _extract_sitekey(html: str) -> str | None:
    for pat in (
        r'data-sitekey=["\']([^"\']+)["\']',
        r'sitekey["\']?\s*[:=]\s*["\']([0-9A-Za-z_\-]{16,})["\']',
        r'\b(0x[0-9A-Za-z_\-]{16,})\b',
    ):
        m = re.search(pat, html)
        if m:
            return m.group(1)
    return None


def _describe_gate(html: str, log):
    """Gate page ka structure debug mein daalo (form/inputs/ajax url)."""
    try:
        soup = BeautifulSoup(html, "html.parser")
        for f in soup.find_all("form")[:2]:
            names = [i.get("name") for i in f.find_all(["input", "button"]) if i.get("name")]
            log(f"ℹ️ gate form: action={f.get('action')!r} method={f.get('method')!r} inputs={names[:8]}")
        urls = re.findall(r"""(?:fetch|\.post|\.get|\.ajax|url\s*:)\s*\(?\s*["']([^"']+)["']""", html)
        if urls:
            log(f"ℹ️ gate JS urls: {urls[:4]}")
    except Exception:
        pass


def _submit_gate(sess, gate_resp, token: str, log):
    """Solved token ko gate form mein submit karo. Naya response (ya None) return."""
    fields = {"cf-turnstile-response": token, "g-recaptcha-response": token}
    soup = BeautifulSoup(gate_resp.text, "html.parser")
    form = None
    for f in soup.find_all("form"):
        if f.find(attrs={"name": re.compile("turnstile|recaptcha", re.I)}) or \
           f.find(class_=re.compile("turnstile", re.I)):
            form = f
            break
    if form is None:
        form = soup.find("form")

    try:
        if form is not None:
            data = {}
            for inp in form.find_all("input"):
                if inp.get("name"):
                    data[inp["name"]] = inp.get("value", "")
            data.update(fields)
            action = urljoin(gate_resp.url, form.get("action") or gate_resp.url)
            method = (form.get("method") or "post").lower()
            log(f"➡️ Gate form submit: {method.upper()} {action[:80]}")
            if method == "get":
                return sess.get(action, params=data, timeout=25, allow_redirects=True)
            return sess.post(action, data=data, timeout=25, allow_redirects=True)
        # Form nahi mila — same URL pe POST try karo
        log("➡️ Gate mein form nahi mila — same URL pe token POST kar raha hoon")
        return sess.post(gate_resp.url, data=fields, timeout=25, allow_redirects=True)
    except Exception as e:
        log(f"❌ Gate submit fail: {str(e).splitlines()[0][:120]}")
        return None


def _fetch_mega_link_with_api(download_url: str, debug: list) -> str | None:
    """
    download1.php -> (Turnstile gate aaye to) AZAPI se token solve karke gate
    pass karo -> server-list page se MEGA link nikaalo. Cookies ki zaroorat nahi;
    ek requests.Session gate-pass ke baad ki cookies khud sambhal leta hai.
    """
    def log(msg):
        debug.append(msg)
        LOGGER.info(f"[Haz] {msg}")

    sess = requests.Session()
    sess.headers.update(HEADERS)
    sess.headers["Referer"] = "https://hindianimeszone.com/"

    # Provider proxy use karta hai to token usi IP se valid hota hai — isliye
    # saari requests bhi usi proxy se bhejte hain.
    proxy_url = _proxy_url() if _provider() != "azapi" else ""
    if proxy_url:
        sess.proxies = {"http": proxy_url, "https": proxy_url}

    # Pichhle gate-pass ki cookies (55 min tak valid) reuse karo
    if GATE_CACHE["jar"] is not None and (time.time() - GATE_CACHE["saved_at"]) < GATE_CACHE_TTL:
        sess.cookies.update(GATE_CACHE["jar"])
        log("♻️ Cached gate cookies use kar raha hoon (naya token nahi lagega)")

    try:
        r = sess.get(download_url, timeout=25, allow_redirects=True)
    except Exception as e:
        log(f"❌ Page fetch fail: {str(e).splitlines()[0][:120]}")
        return None

    if _is_gate(r.text):
        GATE_CACHE["jar"] = None      # purani cookies expire — naya solve chahiye
        log("🔒 Verify gate mila — captcha provider se solve kar raha hoon...")
        sitekey = _extract_sitekey(r.text)
        if not sitekey:
            log("❌ Gate page pe Turnstile sitekey nahi mili")
            _describe_gate(r.text, log)
            return None
        log(f"🔑 sitekey: {sitekey[:14]}…")

        token = _solve_turnstile(r.url, sitekey, log)
        if not token:
            return None

        r2 = _submit_gate(sess, r, token, log)
        if r2 is not None and not _is_gate(r2.text):
            r = r2
        else:
            # Kai sites token verify karke cookie set karti hain aur page dubara
            # kholne pe hi content dikhati hain — ek baar re-GET karo.
            try:
                r3 = sess.get(download_url, timeout=25, allow_redirects=True)
            except Exception as e:
                log(f"❌ Re-fetch fail: {str(e).splitlines()[0][:120]}")
                return None
            if _is_gate(r3.text):
                log("❌ Token submit ke baad bhi gate hi mila — site ka verify flow alag ho sakta hai")
                _describe_gate(r.text, log)
                return None
            r = r3
        log(f"✅ Gate pass ho gaya (HTTP {r.status_code})")
        GATE_CACHE["jar"] = sess.cookies.copy()
        GATE_CACHE["saved_at"] = time.time()
    else:
        log(f"✅ Gate nahi aaya, seedha server-list page mila (HTTP {r.status_code})")

    text = r.text

    soup = BeautifulSoup(text, "html.parser")
    mega_url = None

    # Strategy 1: koi <a> jiske text/href mein "MEGA" ho
    for a in soup.find_all("a", href=True):
        label = a.get_text(" ", strip=True)
        href = a["href"]
        if "mega.nz" in href.lower():
            mega_url = href
            log("✅ MEGA href seedha mila")
            break
        if re.search(r'\bmega\b', label, re.IGNORECASE) and "no ads" not in label.lower():
            # Ye "MEGA" card hai lekin href khud mega.nz nahi (redirect wrapper
            # ho sakta hai) — ek hop follow karo
            try:
                r2 = sess.get(href, timeout=25, allow_redirects=True)
                m = re.search(
                    r'https?://mega\.nz/(?:file|folder)/[A-Za-z0-9_\-]+#[A-Za-z0-9_\-!]+', r2.text
                )
                if m:
                    mega_url = m.group(0)
                    log("✅ MEGA card follow karke link mila")
                    break
                if "mega.nz" in r2.url:
                    mega_url = r2.url
                    log("✅ MEGA card follow karke redirect se link mila")
                    break
            except Exception as e:
                log(f"⚠️ MEGA card follow karte waqt error: {str(e).splitlines()[0][:100]}")
                continue

    # Strategy 2: fallback — poore HTML mein seedha regex se mega.nz dhoondo
    if not mega_url:
        m = re.search(r'https?://mega\.nz/(?:file|folder)/[A-Za-z0-9_\-]+#[A-Za-z0-9_\-!]+', text)
        if m:
            mega_url = m.group(0)
            log("✅ MEGA link page HTML mein regex se mila (fallback)")

    if not mega_url:
        log("❌ MEGA link is page pe nahi mila — server-list ka structure ummeed se alag ho sakta hai")
    return mega_url


# ─────────────────────────────────────────────
#  Selector UI: Language -> Quality -> Episode list (multi-select)
# ─────────────────────────────────────────────
class HazSelector:
    def __init__(self, series_url: str, data: dict, orig_message: Message):
        self.series_url = series_url
        self.title = data["title"]
        self.languages = data["languages"]
        self.episodes = data["episodes"]
        self.orig_message = orig_message
        self.sid = None
        self.msg = None
        self.stage = "lang" if len(self.languages) > 1 else "quality"
        self.language = self.languages[0] if len(self.languages) == 1 else None
        self.quality = None
        self.page = 0
        self.multi = False
        self.selected = set()
        self.created = time.time()

    @property
    def total_pages(self):
        return max(1, (len(self.episodes) + PER_PAGE - 1) // PER_PAGE)

    def page_items(self):
        start = self.page * PER_PAGE
        return list(enumerate(self.episodes))[start:start + PER_PAGE]

    def header_text(self):
        lines = ["📂 **Select**", "━━━━━━━━━━━━━━━━━━", f"🎬 **{self.title}**", "🌐 Website: 🍥 Hindi Anime Zone"]
        if self.stage == "lang":
            lines.append("👇 Pehle Language chuno:")
        elif self.stage == "quality":
            if self.language:
                lines.append(f"🗣️ Language: **{self.language}**")
            lines.append("👇 Ab Quality chuno:")
        else:
            lines.append(f"🗣️ Language: **{self.language}** • 🎞️ Quality: **{self.quality}**")
            lines.append(f"📖 Episodes: {len(self.episodes)}")
            lines.append("━━━━━━━━━━━━━━━━━━")
            lines.append(f"👇 Tap an episode to download:  (Page {self.page + 1}/{self.total_pages})")
            if self.multi:
                lines.append(f"\n☑️ **Multi-select ON** — chosen: `{len(self.selected)}`")
        return "\n".join(lines)

    def build_markup(self):
        sid = self.sid
        rows = []

        if self.stage == "lang":
            row = []
            for lang in self.languages:
                row.append((f"🗣️ {lang}", f"haz_lang_{sid}_{lang}"))
                if len(row) == 2:
                    rows.append(row)
                    row = []
            if row:
                rows.append(row)

        elif self.stage == "quality":
            for q in HAZ_QUALITIES:
                rows.append([(f"🎞️ {q}", f"haz_qual_{sid}_{q}")])
            if len(self.languages) > 1:
                rows.append([("🔙 Back to Language", f"haz_back_{sid}")])

        else:
            row = []
            for idx, ep in self.page_items():
                mark = "✅ " if idx in self.selected else ""
                row.append((f"{mark}E{ep['num']:02d}", f"haz_ep_{sid}_{idx}"))
                if len(row) == 3:
                    rows.append(row)
                    row = []
            if row:
                rows.append(row)

            if self.total_pages > 1:
                nav = []
                if self.page > 0:
                    nav.append(("◀️ Prev", f"haz_pg_{sid}_{self.page - 1}"))
                nav.append((f"📄 {self.page + 1}/{self.total_pages}", f"haz_noop_{sid}"))
                if self.page < self.total_pages - 1:
                    nav.append(("Next ▶️", f"haz_pg_{sid}_{self.page + 1}"))
                rows.append(nav)

            if self.multi:
                rows.append([(f"⬇️ Download Selected ({len(self.selected)})", f"haz_dl_{sid}")])
                rows.append([("☑️ Multi-select: ON (tap to turn off)", f"haz_toggle_{sid}")])
            else:
                rows.append([("☑️ Select Multiple", f"haz_toggle_{sid}")])

            rows.append([("🔙 Back to Quality", f"haz_backq_{sid}")])

        rows.append([("❌ Close", f"haz_close_{sid}")])
        return _kb(rows)

    async def render(self):
        if not self.msg:
            return
        try:
            await self.msg.edit(self.header_text(), reply_markup=self.build_markup())
        except Exception as e:
            LOGGER.error(f"[Haz] selector render error: {e}")


# ─────────────────────────────────────────────
#  Audio/Subtitle filter — url_upload.py ke existing helpers reuse,
#  sirf language generic (Hindi hardcoded nahi) banaya
# ─────────────────────────────────────────────
async def _apply_language_filter(filepath: str, language: str, status_msg: Message) -> tuple[str, bool]:
    """Sirf chuni hui language ka audio + English subtitle rakhta hai.
    Returns (final_filepath, has_eng_sub)."""
    lang_key = language.strip().lower()
    codes = LANG_STREAM_CODES.get(lang_key)

    if codes:
        try:
            await status_msg.edit(f"🔄 **Sirf {language} audio rakh raha hoon...**")
        except Exception:
            pass
        audio_streams = get_audio_streams(filepath)
        keep_indices = [s["index"] for s in audio_streams if s.get("lang", "").lower().strip() in codes]
        if keep_indices:
            new_path = await _keep_audio_streams(filepath, keep_indices, status_msg)
            if new_path:
                filepath = new_path
        else:
            LOGGER.warning(f"[Haz] {language} audio nahi mila, saare audio tracks rakh rahe hain")

    has_eng_sub = False
    try:
        await status_msg.edit("🔄 **Sirf English subtitle rakh raha hoon...**")
    except Exception:
        pass
    sub_streams = get_subtitle_streams(filepath)
    eng_indices = [s["index"] for s in sub_streams if _is_english_sub_stream(s)]
    if eng_indices:
        new_path = await _keep_subtitle_streams(filepath, eng_indices, status_msg)
        if new_path:
            filepath = new_path
            has_eng_sub = True

    return filepath, has_eng_sub


# ─────────────────────────────────────────────
#  Ek episode: link nikaalo -> mega download -> filter -> upload
# ─────────────────────────────────────────────
async def _process_haz_item(client, message, ep: dict, status_msg, index: int, total: int,
                             language: str, quality: str):
    ep_label = f"E{ep['num']:02d}"
    loop = asyncio.get_event_loop()
    download_url = ep["qualities"].get(quality)
    if not download_url:
        try:
            await status_msg.edit(f"🛑 **{ep_label}** — is episode mein `{quality}` quality available nahi hai.")
        except Exception:
            pass
        return "error"

    try:
        await status_msg.edit(f"🔍 **{ep_label}** (`{index}/{total}`)\nDownload link nikal raha hoon...")
    except Exception:
        pass

    debug = []
    mega_url = await loop.run_in_executor(
        None, _fetch_mega_link_with_api, download_url, debug
    )

    if mega_url and not is_mega_link(mega_url):
        debug.append(f"⚠️ Mila hua link MEGA jaisa nahi lagta: {mega_url[:80]}")
        mega_url = None

    if not mega_url:
        debug_text = "\n".join(debug[-10:]) if debug else "(koi debug info nahi mili)"
        try:
            await status_msg.edit(
                f"🛑 **{ep_label} — MEGA Link Fail**\n\n"
                f"❌ MEGA server ka link nahi mila.\n\n"
                f"**Debug (last steps):**\n```\n{debug_text}\n```\n"
                f"⛔ Agle items **band** kar diye gaye."
            )
        except Exception:
            pass
        return "error"

    dl_dir = os.path.join(download_dir, f"haz_{uuid.uuid4().hex[:8]}")
    try:
        await status_msg.edit(f"⬇️ **{ep_label}** — MEGA se download ho raha hai...")
    except Exception:
        pass
    filepath = await download_mega(mega_url, dl_dir, status_msg)
    if not filepath:
        # download_mega() apna khud ka detailed error status_msg pe daal chuka hai
        try:
            cur_text = status_msg.text or ""
            await status_msg.edit(f"{cur_text}\n\n⛔ Agle items **band** kar diye gaye.")
        except Exception:
            pass
        return "error"

    try:
        filepath, has_eng_sub = await _apply_language_filter(filepath, language, status_msg)
    except Exception as e:
        LOGGER.error(f"[Haz] {ep_label} audio/sub filter error: {e}")
        try:
            await status_msg.edit(
                f"🛑 **{ep_label} — Filter Error**\n\n❌ `{str(e)[:150]}`\n\n"
                f"⛔ Agle items **band** kar diye gaye."
            )
        except Exception:
            pass
        return "error"

    before_text = None
    try:
        before_text = status_msg.text
    except Exception:
        pass
    await _do_upload(client, filepath, message, status_msg, has_eng_sub=has_eng_sub)
    # _do_upload apna khud ka success/fail status daal deta hai. Fail hua
    # ho to seedha pata chal jaayega (message text mein "Upload failed"),
    # is se aage single-episode flow mein hum continue kar dete hain
    # (yahi ek jagah hai jahan _do_upload khud detailed error deta hai).
    try:
        final_text = status_msg.text or ""
    except Exception:
        final_text = ""
    if "failed" in final_text.lower():
        return "error"
    return "ok"


async def _download_haz_items(client, status_msg, orig_message, sess: "HazSelector", idxs: list):
    total = len(idxs)
    provider = _provider()
    if provider == "azapi" and not _azapi_cfg()["key"]:
        await status_msg.edit("❌ **AZAPI_KEY set nahi hai.** (CAPTCHA_PROVIDER=azapi)")
        return

    all_ok = True
    for i, idx in enumerate(idxs, 1):
        ep = sess.episodes[idx]
        result = await _process_haz_item(
            client, orig_message, ep, status_msg, i, total, sess.language, sess.quality,
        )
        if result != "ok":
            all_ok = False
            break
        if i < total:
            status_msg = await orig_message.reply("⏳ Agla episode shuru ho raha hai...")
            await asyncio.sleep(1)

    if all_ok:
        try:
            await status_msg.delete()
        except Exception:
            pass


# ─────────────────────────────────────────────
#  Callback handler
# ─────────────────────────────────────────────
@Client.on_callback_query(filters.regex(r"^haz_"))
async def haz_callback_handler(client: Client, cb: CallbackQuery):
    try:
        parts = cb.data.split("_")
        action = parts[1] if len(parts) > 1 else ""
        sid = parts[2] if len(parts) > 2 else None
        sess = HAZ_SESSIONS.get(sid)

        if not sess:
            await cb.answer("⌛ Session expired, /Haz <url> dubara bhejo.", show_alert=True)
            return

        if action == "noop":
            await cb.answer()
            return

        if action == "close":
            HAZ_SESSIONS.pop(sid, None)
            await cb.answer()
            try:
                await cb.message.delete()
            except Exception:
                pass
            return

        if action == "lang":
            sess.language = "_".join(parts[3:])
            sess.stage = "quality"
            await cb.answer()
            await sess.render()
            return

        if action == "back":
            sess.stage = "lang"
            sess.quality = None
            await cb.answer()
            await sess.render()
            return

        if action == "qual":
            sess.quality = "_".join(parts[3:])
            sess.stage = "episodes"
            sess.page = 0
            sess.multi = False
            sess.selected.clear()
            await cb.answer()
            await sess.render()
            return

        if action == "backq":
            sess.stage = "quality"
            await cb.answer()
            await sess.render()
            return

        if action == "toggle":
            sess.multi = not sess.multi
            if not sess.multi:
                sess.selected.clear()
            await cb.answer("Multi-select " + ("ON" if sess.multi else "OFF"))
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

            await cb.answer("⬇️ Download shuru ho raha hai...")
            try:
                await cb.message.delete()
            except Exception:
                pass
            status_msg = await sess.orig_message.reply("⏳ Shuru ho raha hai...")
            await _download_haz_items(client, status_msg, sess.orig_message, sess, [idx])
            HAZ_SESSIONS.pop(sid, None)
            return

        if action == "dl":
            if not sess.selected:
                await cb.answer("❌ Pehle kuch episodes select karo!", show_alert=True)
                return
            idxs = sorted(sess.selected)
            await cb.answer(f"⬇️ {len(idxs)} episodes download shuru ho rahe hain...")
            try:
                await cb.message.delete()
            except Exception:
                pass
            status_msg = await sess.orig_message.reply("⏳ Shuru ho raha hai...")
            await _download_haz_items(client, status_msg, sess.orig_message, sess, idxs)
            HAZ_SESSIONS.pop(sid, None)
            return

        await cb.answer()
    except Exception as e:
        LOGGER.error(f"[Haz] callback error: {e}")
        try:
            await cb.answer("❌ Error, dubara try karo.", show_alert=True)
        except Exception:
            pass


# ─────────────────────────────────────────────
#  /Haz Command Handler
# ─────────────────────────────────────────────
@Client.on_message(filters.command("haz"))
async def haz_command(client: Client, message: Message):
    """
    /Haz <series_page_url>  -> Language menu -> Quality menu -> Episode menu
    """
    c = await check_chat(message, chat="Sudo")
    if not c:
        return

    parts = message.text.split()
    if len(parts) < 2:
        await message.reply(
            "**Usage:**\n"
            "`/Haz <series_page_url>` — Language/Quality/Episode button menu khulega\n\n"
            "**Example:**\n"
            "`/Haz https://hindianimeszone.com/liar-game-season-1-multi-audio-hindi-tamil-telugu-eng-jap-480p-720p-1080p-hd-web-dl-10bit-hevc-esub/`"
        )
        return

    series_url = parts[1].strip()
    if not series_url.startswith("http"):
        await message.reply("❌ Valid URL dalo.")
        return

    status_msg = await message.reply("🔍 Page scan ho raha hai...")

    loop = asyncio.get_event_loop()
    try:
        data = await loop.run_in_executor(None, discover_haz_items, series_url)
    except Exception as e:
        LOGGER.error(f"[Haz] discover_haz_items error: {e}")
        await status_msg.edit(f"❌ Page load nahi hua: `{str(e)[:100]}`")
        return

    if not data["episodes"]:
        await status_msg.edit("❌ Page se koi episode nahi mila. URL check karo.")
        return

    _prune_haz_sessions()
    sess = HazSelector(series_url, data, orig_message=message)
    sess.sid = uuid.uuid4().hex[:10]
    HAZ_SESSIONS[sess.sid] = sess

    try:
        await status_msg.delete()
    except Exception:
        pass

    sess.msg = await message.reply(sess.header_text(), reply_markup=sess.build_markup())


# ─────────────────────────────────────────────
#  /hazapi — captcha provider config + credits status (sudo only)
# ─────────────────────────────────────────────
def _mask(v: str) -> str:
    return (v[:4] + "…" + v[-3:]) if len(v) > 10 else ("set" if v else "❌ NOT SET")


@Client.on_message(filters.command("hazapi"))
async def hazapi_command(client: Client, message: Message):
    c = await check_chat(message, chat="Sudo")
    if not c:
        return
    provider = _provider()
    proxy_host = (_proxy_dict(_proxy_url()) or {}).get("host", "❌ NOT SET")
    loop = asyncio.get_event_loop()
    if provider == "peakfo":
        status = await loop.run_in_executor(None, _peak_status)
    elif provider == "nopecha":
        status = await loop.run_in_executor(None, _nopecha_status)
    else:
        status = "azapi (status check nahi)"
    cache_left = max(0, int(GATE_CACHE_TTL - (time.time() - GATE_CACHE["saved_at"]))) if GATE_CACHE["jar"] is not None else 0
    await message.reply(
        "**🔐 Captcha config**\n\n"
        f"• Provider: `{provider}`\n"
        f"• PEAKFO_KEY: `{_mask((os.getenv('PEAKFO_KEY') or '').strip())}`\n"
        f"• Proxy host: `{proxy_host}`\n"
        f"• Gate cookie cache: `{cache_left // 60} min left`\n\n"
        f"**{provider} status:** `{status}`"
    )
