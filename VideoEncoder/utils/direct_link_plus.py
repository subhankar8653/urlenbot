"""
direct_link_plus.py — naye / working direct-link resolvers (CBML se port, urlenbot ke liye)
=========================================================================================
Purane `direct_link_generator.py` ke bahut se hosts (zippyshare, anonfiles, antfiles ...) band ho
chuke hain. Yeh file aaj ke chalte hosts ko /url, /bot_upload mein support deti hai:

  gofile.io • buzzheavier.com (bzzhr.co/to) • mediafire.com (improved) • krakenfiles.com
  upload.ee • qiwi.gg • fuckingfast.co • tmpsend.com

Interface:
  resolve_plus(url)          -> direct URL (str)  ya  None (host pehchana nahi)
  get_extra_headers(url)     -> {header: value} jo us final URL ke download mein lagne chahiye
                                (gofile ka Cookie, tmpsend ka Referer)
  is_resolved(url)           -> True agar yeh URL humne hi resolve karke diya hai
                                (m3u8 sniff jaisi extra requests skip karne ke liye)

Sirf SINGLE-FILE links. Folder / multi-file link par saaf error message aata hai.
Blocking (requests) code hai — async code se `run_in_executor` ke through chalao.
"""
import re
import time
from hashlib import sha256
from urllib.parse import parse_qs, urlparse

import cloudscraper
import requests

try:
    from lxml.etree import HTML as _HTML
except Exception:  # pragma: no cover
    _HTML = None

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0"
_TIMEOUT = 30

_EXTRA: dict = {}        # final_url -> headers
_RESOLVED: set = set()   # humne jo final URLs banaye


class PlusLinkError(Exception):
    """Resolver ne pakka error diya (user ko dikhane layak message)."""


def get_extra_headers(url: str) -> dict:
    return dict(_EXTRA.get(url, {}))


def is_resolved(url: str) -> bool:
    return url in _RESOLVED


def _done(final: str, headers: dict = None) -> str:
    _RESOLVED.add(final)
    if headers:
        _EXTRA[final] = headers
    # memory leak se bachao
    if len(_RESOLVED) > 500:
        _RESOLVED.clear()
        _EXTRA.clear()
        _RESOLVED.add(final)
        if headers:
            _EXTRA[final] = headers
    return final


def _html(text: str):
    if _HTML is None:
        raise PlusLinkError("lxml install nahi hai (pip install lxml)")
    return _HTML(text)


# ───────────────────────────── gofile ─────────────────────────────
def _gofile_salt() -> str:
    try:
        js = requests.get("https://gofile.io/js/wt.obf.js", timeout=15).text
        js = re.sub(r"\\x([0-9a-f]{2})", lambda m: chr(int(m[1], 16)), js)
        m = re.search(r"'([0-9a-f]{14})'", js[js.index("generateWT"):])
        if m:
            return m[1]
    except Exception:
        pass
    return "12af056dacea0b"   # fallback (purana known salt)


def gofile(url: str) -> str:
    content_id = url.split("?")[0].rstrip("/").split("/")[-1]
    if not content_id:
        raise PlusLinkError("Gofile: link galat hai")
    with requests.Session() as s:
        try:
            tok = s.post("https://api.gofile.io/accounts", headers={"User-Agent": _UA},
                         timeout=_TIMEOUT).json()
            if tok.get("status") != "ok":
                raise PlusLinkError("Gofile: token nahi mila")
            token = tok["data"]["token"]
        except PlusLinkError:
            raise
        except Exception as e:
            raise PlusLinkError(f"Gofile token error: {e.__class__.__name__}")

        slot = int(time.time()) // 14400
        wt = sha256(f"{_UA}::en-US::{token}::{slot}::{_gofile_salt()}".encode()).hexdigest()
        headers = {
            "User-Agent": _UA, "Accept": "*/*",
            "Authorization": f"Bearer {token}",
            "X-Website-Token": wt, "X-BL": "en-US",
        }
        try:
            js = s.get(f"https://api.gofile.io/contents/{content_id}?cache=true",
                       headers=headers, timeout=_TIMEOUT).json()
        except Exception as e:
            raise PlusLinkError(f"Gofile contents error: {e.__class__.__name__}")

    status = js.get("status", "")
    if status == "error-notFound":
        raise PlusLinkError("Gofile: file wahan nahi mili (delete/expire ho gayi)")
    if status == "error-passwordRequired":
        raise PlusLinkError("Gofile: password-protected link abhi supported nahi")
    if status == "error-notPublic":
        raise PlusLinkError("Gofile: yeh folder public nahi hai")
    if status != "ok":
        raise PlusLinkError(f"Gofile error: {status or 'unknown'}")

    data = js["data"]
    node = data
    if "children" in data:
        files = [c for c in data["children"].values() if c.get("type") != "folder"]
        if len(files) != 1:
            raise PlusLinkError(
                f"Gofile folder mein {len(files)} files hain — single file ka link do "
                f"(folder abhi supported nahi)."
            )
        node = files[0]
    link = node.get("link")
    if not link:
        raise PlusLinkError("Gofile: direct link nahi mila")
    return _done(link, {"Cookie": f"accountToken={token}"})


# ───────────────────────────── buzzheavier ─────────────────────────────
def buzzheavier(url: str) -> str:
    if not re.match(r"^https?://(?:buzzheavier\.com|bzzhr\.(?:co|to))/[a-zA-Z0-9]+/?$", url.strip()):
        raise PlusLinkError("Buzzheavier: sirf single file link chalta hai (buzzheavier.com/<id>)")
    url = url.strip().rstrip("/")
    host = f"https://{urlparse(url).netloc}"
    try:
        from curl_cffi import requests as cr
    except Exception:
        raise PlusLinkError("curl_cffi install nahi hai (pip install curl_cffi)")
    with cr.Session(impersonate="chrome") as s:
        page = s.get(url, timeout=_TIMEOUT)
        tree = _html(page.text)
        hx = tree.xpath("//a[contains(@hx-get, 'download')]")
        if not hx:
            raise PlusLinkError("Buzzheavier: download button nahi mila (folder ya file delete?)")
        path = hx[0].attrib.get("hx-get", "").strip()
        if "/download" not in path:
            path += "/download"
        r = s.get(f"{host}{path}", allow_redirects=False, timeout=_TIMEOUT)
        loc = (r.headers.get("hx-redirect") or r.headers.get("location") or "").strip()
        if not loc:
            raise PlusLinkError("Buzzheavier: direct link nahi mila")
        return _done(loc)


# ───────────────────────────── mediafire (improved) ─────────────────────────────
def mediafire(url: str) -> str:
    if "/folder/" in url:
        raise PlusLinkError("MediaFire folder abhi supported nahi — single file link do")
    m = re.findall(r"https?://download\d+\.mediafire\.com/\S+/\S+/\S+", url)
    if m:
        return _done(m[0])
    p = urlparse(url.strip())
    cur = f"{p.scheme}://{p.netloc}{p.path}"
    sess = cloudscraper.create_scraper()
    try:
        for _ in range(3):
            try:
                html = _html(sess.get(cur, timeout=_TIMEOUT).text)
            except Exception as e:
                raise PlusLinkError(f"MediaFire: page nahi khula ({e.__class__.__name__})")
            err = html.xpath('//p[@class="notranslate"]/text()')
            if err:
                raise PlusLinkError(f"MediaFire: {err[0]}")
            if html.xpath("//div[@class='passwordPrompt']"):
                raise PlusLinkError("MediaFire: password-protected link supported nahi")
            link = html.xpath('//a[@aria-label="Download file"]/@href')
            if link:
                dl = link[0]
                if dl.startswith("//"):
                    dl = "https:" + dl
                return _done(dl)
            retry = html.xpath("//a[@class='retry']/@href") or html.xpath('//a[@id="continue-btn"]/@href')
            if not retry:
                break
            nxt = retry[0]
            cur = nxt if nxt.startswith("http") else "https://mediafire.com/" + nxt.lstrip("/")
    finally:
        sess.close()
    raise PlusLinkError("MediaFire: download link nahi mila, thodi der baad try karo")


# ───────────────────────────── krakenfiles ─────────────────────────────
def krakenfiles(url: str) -> str:
    with requests.Session() as s:
        s.headers["User-Agent"] = _UA
        try:
            res = s.get(url, timeout=_TIMEOUT)
        except Exception as e:
            raise PlusLinkError(f"KrakenFiles: {e.__class__.__name__}")
        html = _html(res.text)
        post_url = html.xpath('//form[@id="dl-form"]/@action')
        token = html.xpath('//input[@id="dl-token"]/@value')
        if not post_url or not token:
            raise PlusLinkError("KrakenFiles: download form nahi mila")
        try:
            js = s.post(f"https://krakenfiles.com{post_url[0]}", data={"token": token[0]},
                        timeout=_TIMEOUT).json()
        except Exception as e:
            raise PlusLinkError(f"KrakenFiles post error: {e.__class__.__name__}")
    if js.get("status") != "ok" or not js.get("url"):
        raise PlusLinkError("KrakenFiles: link nahi mila")
    return _done(js["url"])


# ───────────────────────────── upload.ee ─────────────────────────────
def uploadee(url: str) -> str:
    sess = cloudscraper.create_scraper()
    try:
        html = _html(sess.get(url, timeout=_TIMEOUT).text)
    except Exception as e:
        raise PlusLinkError(f"Upload.ee: {e.__class__.__name__}")
    finally:
        sess.close()
    link = html.xpath("//a[@id='d_l']/@href")
    if not link:
        raise PlusLinkError("Upload.ee: direct link nahi mila")
    return _done(link[0])


# ───────────────────────────── qiwi.gg ─────────────────────────────
def qiwi(url: str) -> str:
    file_id = url.split("?")[0].rstrip("/").split("/")[-1]
    try:
        text = requests.get(url, headers={"User-Agent": _UA}, timeout=_TIMEOUT).text
    except Exception as e:
        raise PlusLinkError(f"Qiwi: {e.__class__.__name__}")
    name = _html(text).xpath('//h1[@class="page_TextHeading__VsM7r"]/text()')
    if not name:
        raise PlusLinkError("Qiwi: file nahi mili")
    ext = name[0].split(".")[-1]
    return _done(f"https://spyderrock.com/{file_id}.{ext}")


# ───────────────────────────── fuckingfast.co ─────────────────────────────
def fuckingfast(url: str) -> str:
    try:
        text = requests.get(url.strip(), headers={"User-Agent": _UA}, timeout=_TIMEOUT).text
    except Exception as e:
        raise PlusLinkError(f"FuckingFast: {e.__class__.__name__}")
    m = re.search(r'window\.open\((["\'])(https://fuckingfast\.co/dl/[^"\']+)\1', text)
    if not m:
        raise PlusLinkError("FuckingFast: download link nahi mila")
    return _done(m.group(2))


# ───────────────────────────── tmpsend.com ─────────────────────────────
def tmpsend(url: str) -> str:
    p = urlparse(url)
    file_id = None
    if any(x in p.path for x in ("thank-you", "download")):
        file_id = (parse_qs(p.query).get("d") or [None])[0]
    else:
        file_id = p.path.strip("/") or None
    if not file_id:
        raise PlusLinkError("TmpSend: link format galat hai")
    return _done(f"https://tmpsend.com/download?d={file_id}",
                 {"Referer": f"https://tmpsend.com/thank-you?d={file_id}"})


# ───────────────────────────── dispatcher ─────────────────────────────
_TABLE = (
    (("gofile.io",), gofile),
    (("buzzheavier.com", "bzzhr.co", "bzzhr.to"), buzzheavier),
    (("mediafire.com",), mediafire),
    (("krakenfiles.com",), krakenfiles),
    (("upload.ee",), uploadee),
    (("qiwi.gg",), qiwi),
    (("fuckingfast.co",), fuckingfast),
    (("tmpsend.com",), tmpsend),
)


def resolve_plus(url: str):
    """Host pehchan ke resolve karo. Pehchana nahi to None. Pakka error -> PlusLinkError."""
    try:
        host = (urlparse(url.strip()).netloc or "").lower().split(":")[0]
    except Exception:
        return None
    for hosts, fn in _TABLE:
        if any(host == h or host.endswith("." + h) for h in hosts):
            return fn(url.strip())
    return None
