import asyncio
import html
import math
import os
import re
import urllib.request
from functools import lru_cache
from io import BytesIO

import aiohttp
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

# ------------------------------------------------------------ fonts (poster3 only)
FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
_BASE = "https://raw.githubusercontent.com/google/fonts/main/ofl/"
FONT_FILES = {
    "title": ("kanit", "Kanit-ExtraBold.ttf"),
    "heavy": ("poppins", "Poppins-ExtraBold.ttf"),
    "bold": ("poppins", "Poppins-Bold.ttf"),
    "semibold": ("poppins", "Poppins-SemiBold.ttf"),
    "synopsis": ("kanit", "Kanit-Italic.ttf"),          # synopsis (italic, not a regular look)
    "jp": ("zenmarugothic", "ZenMaruGothic-Bold.ttf"),
}
_FALLBACKS = {"title": "DejaVuSans-Bold.ttf", "heavy": "DejaVuSans-Bold.ttf", "bold": "DejaVuSans-Bold.ttf",
              "semibold": "DejaVuSans-Bold.ttf"}


def ensure_fonts():
    """Download fonts once (blocking - call with asyncio.to_thread)."""
    os.makedirs(FONT_DIR, exist_ok=True)
    for folder, name in FONT_FILES.values():
        path = os.path.join(FONT_DIR, name)
        if os.path.exists(path) and os.path.getsize(path) > 10_000:
            continue
        try:
            urllib.request.urlretrieve(f"{_BASE}{folder}/{name}", path)
        except Exception as e:  # noqa
            print(f"[fonts] could not download {name}: {e}")


@lru_cache(maxsize=128)
def get_font(style: str, size: int):
    path = os.path.join(FONT_DIR, FONT_FILES[style][1])
    if not os.path.exists(path):
        ensure_fonts()
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        try:
            return ImageFont.truetype(_FALLBACKS.get(style, "DejaVuSans.ttf"), size)
        except Exception:
            return ImageFont.load_default()


def _jp_ok():
    p = os.path.join(FONT_DIR, FONT_FILES["jp"][1])
    return os.path.exists(p) and os.path.getsize(p) > 10_000


# ------------------------------------------------------------ images / text cleanup
async def fetch_bytes(url: str, retries: int = 3) -> bytes:
    """Download an image with retries and validate it is a real image."""
    headers = {"User-Agent": "Mozilla/5.0 (compatible; PosterBot/1.0)"}
    last = None
    for i in range(retries):
        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as r:
                    r.raise_for_status()
                    data = await r.read()
            Image.open(BytesIO(data)).verify()
            return data
        except Exception as e:  # noqa
            last = e
            await asyncio.sleep(1 + i)
    raise last


async def _try(url):
    try:
        return await fetch_bytes(url, retries=2)
    except Exception as e:  # noqa
        print(f"[img] {url[-40:]} failed: {e}")
        return None


async def load_images(meta: dict):
    """Returns (cover_bytes|None, banner_bytes|None); never raises."""
    cover = banner = None
    ci = meta.get("coverImage") or {}
    for key in ("extraLarge", "large", "medium"):
        if ci.get(key):
            cover = await _try(ci[key])
            if cover:
                break
    if meta.get("bannerImage"):
        banner = await _try(meta["bannerImage"])
    return cover, banner


def clean_description(text: str) -> str:
    if not text:
        return "No description available."
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"\(Source:.*?\)", "", text, flags=re.I | re.S)
    text = re.sub(r"\[Written by.*?\]", "", text, flags=re.I | re.S)
    return re.sub(r"\s+", " ", text).strip()


def _open(b):
    return Image.open(BytesIO(b)).convert("RGB") if b else None


def _placeholder(meta):
    col = ((meta.get("coverImage") or {}).get("color") or "#2a3550").lstrip("#")
    try:
        c = tuple(int(col[i:i + 2], 16) for i in (0, 2, 4))
    except Exception:
        c = (42, 53, 80)
    return Image.new("RGB", (460, 650), c)


# ------------------------------------------------------------ constants
W, H = 1280, 720
K = 3
OUT = (1920, 1080)
VIOLET = (124, 92, 255)
VIOLET_TOP, VIOLET_BOT = (150, 122, 255), (104, 74, 236)
CYAN = (96, 214, 238)
GOLD = (255, 196, 32)
LEFT = 64


def S(v):
    return int(round(v * K))


# ------------------------------------------------------------ helpers
def _smooth(t):
    t = max(0.0, min(1.0, t))
    return t * t * (3 - 2 * t)


def _ramp(w, h, axis, a0, a1):
    n = w if axis == "x" else h
    line = Image.new("L", (n, 1))
    px = line.load()
    for i in range(n):
        px[i, 0] = int(a0 + (a1 - a0) * _smooth(i / max(1, n - 1)))
    if axis == "x":
        return line.resize((w, h), Image.NEAREST)
    return line.rotate(-90, expand=True).resize((w, h), Image.NEAREST)


def _tint(canvas, x, y, w, h, axis, a0, a1, color=(6, 8, 16)):
    canvas.paste(color + (255,), (x, y, x + w, y + h), _ramp(w, h, axis, a0, a1))


def _glow(canvas, cx, cy, r, color, alpha):
    g = ImageOps.invert(Image.radial_gradient("L")).resize((2 * r, 2 * r), Image.BICUBIC)
    g = g.point(lambda v: int(alpha * _smooth(v / 255.0) ** 1.5))
    canvas.paste(color + (255,), (cx - r, cy - r), g)


def _fmt_count(n):
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _tw(d, text, font, tr=0):
    return sum(d.textlength(c, font=font) for c in text) + tr * max(0, len(text) - 1)


def _tt(d, xy, text, font, fill, tr=0):
    x, y = xy
    for c in text:
        d.text((x, y), c, font=font, fill=fill, anchor="ls")
        x += d.textlength(c, font=font) + tr


def _wrap(d, text, font, max_w, tr=0):
    lines, cur = [], ""
    for word in text.split():
        t = (cur + " " + word).strip()
        if _tw(d, t, font, tr) <= max_w:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def _balanced(d, text, font, tr, n, max_w):
    lo = int(max(_tw(d, w, font, tr) for w in text.split()))
    hi = int(max_w)
    if lo > hi:
        return _wrap(d, text, font, hi, tr)
    for _ in range(14):
        mid = (lo + hi) // 2
        if len(_wrap(d, text, font, mid, tr)) <= n:
            hi = mid
        else:
            lo = mid + 1
    return _wrap(d, text, font, hi, tr)


def _ellipsize(d, text, font, max_w):
    if d.textlength(text, font=font) <= max_w:
        return text
    while len(text) > 1 and d.textlength(text + "...", font=font) > max_w:
        text = text[:-1]
    return text.rstrip() + "..."


def _glass(canvas, box, radius, tint=12, border=44, shadow=80):
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    if shadow:
        pad = S(22)
        sh = Image.new("L", (w + 2 * pad, h + 2 * pad), 0)
        ImageDraw.Draw(sh).rounded_rectangle((pad, pad + S(4), pad + w, pad + h + S(4)), radius=radius, fill=shadow)
        canvas.paste((0, 0, 0, 255), (x0 - pad, y0 - pad), sh.filter(ImageFilter.GaussianBlur(S(9))))
    region = ImageEnhance.Brightness(canvas.crop(box).filter(ImageFilter.GaussianBlur(S(12)))).enhance(0.55)
    m = Image.new("L", (w, h), 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, w - 1, h - 1), radius=radius, fill=255)
    canvas.paste(region, (x0, y0), m)
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle((0, 0, w - 1, h - 1), radius=radius, fill=(255, 255, 255, tint),
                                            outline=(255, 255, 255, border), width=max(1, S(1)))
    canvas.alpha_composite(layer, (x0, y0))


def _gradient_rect(canvas, box, c0, c1, radius, horizontal=False):
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    col = Image.new("RGB", (w, 1) if horizontal else (1, h))
    px = col.load()
    for i in range(w if horizontal else h):
        k = i / max(1, (w if horizontal else h) - 1)
        c = tuple(int(a + (b - a) * k) for a, b in zip(c0, c1))
        px[(i, 0) if horizontal else (0, i)] = c
    m = Image.new("L", (w, h), 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, w - 1, h - 1), radius=radius, fill=255)
    canvas.paste(col.resize((w, h)), (x0, y0), m)


def _star(d, cx, cy, r, fill):
    pts = []
    for i in range(10):
        a = -math.pi / 2 + i * math.pi / 5
        rad = r if i % 2 == 0 else r * 0.48
        pts.append((cx + rad * math.cos(a), cy + rad * math.sin(a)))
    d.polygon(pts, fill=fill)


def _fill(src, zw, zh, blur=0, bright=1.0):
    im = ImageOps.fit(src, (zw, zh), Image.LANCZOS, centering=(0.5, 0.3))
    if blur:
        im = im.filter(ImageFilter.GaussianBlur(blur))
    if bright != 1.0:
        im = ImageEnhance.Brightness(im).enhance(bright)
    return im


def _cover_card(canvas, cover, right, y, w, h, radius):
    x = right - w
    im = ImageOps.fit(cover, (w, h), Image.LANCZOS, centering=(0.5, 0.28)).filter(
        ImageFilter.UnsharpMask(radius=S(1.2), percent=60, threshold=3))
    pad = S(44)
    sh = Image.new("L", (w + 2 * pad, h + 2 * pad), 0)
    ImageDraw.Draw(sh).rounded_rectangle((pad, pad + S(12), pad + w, pad + h + S(12)), radius=radius, fill=165)
    canvas.paste((0, 0, 0, 255), (x - pad, y - pad), sh.filter(ImageFilter.GaussianBlur(S(18))))
    m = Image.new("L", (w, h), 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, w - 1, h - 1), radius=radius, fill=255)
    canvas.paste(im, (x, y), m)
    edge = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(edge).rounded_rectangle((0, 0, w - 1, h - 1), radius=radius, outline=(255, 255, 255, 70),
                                           width=max(1, S(1)))
    canvas.alpha_composite(edge, (x, y))


def _fit_title(d, title, max_w, tr):
    plans = [(range(66, 39, -2), 1), (range(56, 33, -2), 2), (range(40, 27, -1), 3)]
    for sizes, n in plans:
        for size in sizes:
            f = get_font("title", S(size))
            lines = [title] if n == 1 else _balanced(d, title, f, tr, n, max_w)
            if len(lines) <= n and all(_tw(d, l, f, tr) <= max_w for l in lines):
                return f, lines
    f = get_font("title", S(28))
    lines = _balanced(d, title, f, tr, 3, max_w)[:3]
    last = lines[-1]
    while len(last) > 1 and _tw(d, last + "...", f, tr) > max_w:
        last = last[:-1].rstrip()
    lines[-1] = last + "..."
    return f, lines


def _chip(canvas, td, x, cy, text, font, tr, h=30, pad=15):
    w = int(_tw(td, text, font, tr) + 2 * S(pad))
    _glass(canvas, (x, cy - S(h) // 2, x + w, cy + S(h) // 2), S(h) // 2, shadow=0)
    _tt(td, (x + S(pad), cy + S(4)), text, font, (236, 238, 246), tr)
    return x + w


# ------------------------------------------------------------ main
def create_poster(meta: dict, cover_bytes: bytes = None, banner_bytes: bytes = None) -> BytesIO:
    cover, banner = _open(cover_bytes), _open(banner_bytes)
    if cover is None:
        cover = banner if banner is not None else _placeholder(meta)

    t = meta.get("title") or {}
    title = (t.get("english") or t.get("romaji") or t.get("native") or "Unknown").upper()
    native = t.get("native") if (t.get("native") and _jp_ok() and (t.get("english") or t.get("romaji"))) else None
    score = meta.get("averageScore") or meta.get("meanScore")
    year = meta.get("seasonYear") or (meta.get("startDate") or {}).get("year")
    studios = ((meta.get("studios") or {}).get("nodes")) or []
    studio = studios[0]["name"].upper() if studios else None
    genres = [g.upper() for g in (meta.get("genres") or [])[:4]]
    desc = clean_description(meta.get("description"))
    fmt = (meta.get("format") or "").replace("_", " ").upper()
    status = (meta.get("status") or "").replace("_", " ").upper()
    eps = meta.get("episodes")
    top_label = "  \u2022  ".join(x for x in (fmt, status) if x)

    pop = meta.get("popularity")
    season = (meta.get("season") or "").title()
    duration = meta.get("duration")

    canvas = Image.new("RGBA", (S(W), S(H)), (6, 8, 16, 255))

    # ---- background: banner (or blurred cover) + violet glow + soft dark gradients
    if banner is not None:
        canvas.paste(_fill(banner, S(W), S(H), bright=0.9), (0, 0))
    else:
        canvas.paste(_fill(cover, S(W), S(H), blur=S(26), bright=0.8), (0, 0))
    _tint(canvas, 0, 0, S(W), S(H), "x", 232, 60)
    _tint(canvas, 0, S(420), S(W), S(300), "y", 0, 140)
    _glow(canvas, S(1130), S(40), S(520), VIOLET, 70)

    d = ImageDraw.Draw(canvas)
    tl = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    td = ImageDraw.Draw(tl)

    # ---- top-right label
    if top_label:
        f_top = get_font("bold", S(11.5))
        tw = _tw(td, top_label, f_top, S(1.6))
        x1 = S(W - LEFT)
        x0 = int(x1 - tw - 2 * S(18))
        _glass(canvas, (x0, S(30), x1, S(60)), S(15), shadow=0)
        _tt(td, (x0 + S(18), S(45) + S(4.2)), top_label, f_top, (232, 234, 244), S(1.6))
        d = ImageDraw.Draw(canvas)

    # ---- left stack (vertically centred): header row, native title, title, synopsis, tags
    tr = S(1.2)
    f_title, lines = _fit_title(d, title, S(600), tr)
    step = int(f_title.size * 1.02)
    cap = int(f_title.size * 0.72)
    title_h = (len(lines) - 1) * step + cap
    hdr_h, card_h, chip_h = S(30), S(146), S(30)
    gaps = S(16) + (S(34) if native else 0) + S(40) + S(22)
    total = hdr_h + title_h + card_h + chip_h + gaps
    y = max(S(86), (S(H) - total) // 2 + S(8))

    # header: studio pill + rating + year
    x = S(LEFT)
    cyh = y + hdr_h // 2
    if studio:
        f_st = get_font("heavy", S(11.5))
        sw = int(_tw(td, studio, f_st, S(0.9)) + 2 * S(13))
        _gradient_rect(canvas, (x, cyh - S(14), x + sw, cyh + S(14)), VIOLET_TOP, VIOLET_BOT, S(8))
        d = ImageDraw.Draw(canvas)
        _tt(td, (x + S(13), cyh + S(4.2)), studio, f_st, (255, 255, 255), S(0.9))
        x += sw + S(16)
    if score:
        _star(d, x + S(9), cyh, S(9.5), GOLD)
        sc = f"{score / 10:.1f}"
        f_sc = get_font("heavy", S(19))
        td.text((x + S(24), cyh + S(1)), sc, font=f_sc, fill=GOLD, anchor="lm")
        x += S(24) + td.textlength(sc, font=f_sc) + S(16)
    if year:
        td.text((x, cyh + S(1)), str(year), font=get_font("semibold", S(15)), fill=(170, 176, 192), anchor="lm")
    y += hdr_h + S(16)

    # native (Japanese) title
    if native:
        _tt(td, (S(LEFT), y + S(18)), native, get_font("jp", S(20)), CYAN, S(1.6))
        y += S(34)

    # big title
    y += cap
    for i, line in enumerate(lines):
        _tt(td, (S(LEFT), y + i * step), line, f_title, (255, 255, 255), tr)
    base_last = y + (len(lines) - 1) * step
    _gradient_rect(canvas, (S(LEFT), base_last + S(14), S(LEFT + 72), base_last + S(18)), VIOLET, CYAN, S(2),
                   horizontal=True)                                   # line under the heading
    d = ImageDraw.Draw(canvas)
    y = base_last + S(40)

    # synopsis glass card with a gradient accent edge
    cx0, cx1 = S(LEFT), S(LEFT + 570)
    _glass(canvas, (cx0, y, cx1, y + card_h), S(14))
    d = ImageDraw.Draw(canvas)
    f_desc = get_font("synopsis", S(15.5))
    max_w = S(570 - 48)
    dl = _wrap(d, desc, f_desc, max_w)
    if len(dl) > 4:
        dl = dl[:4]
        last = dl[-1].split()
        while last and d.textlength(" ".join(last) + "...", font=f_desc) > max_w:
            last.pop()
        dl[-1] = " ".join(last) + "..."
    block = (len(dl) - 1) * S(24.5)
    ty = y + (card_h - block) // 2
    for i, line in enumerate(dl):
        td.text((cx0 + S(24), ty + i * S(24.5)), line, font=f_desc, fill=(228, 232, 242), anchor="lm")
    y += card_h + S(22)

    # genre tags
    cxm = y + chip_h // 2
    xx = S(LEFT)
    f_chip = get_font("bold", S(11))
    for g in genres:
        if xx + _tw(td, g, f_chip, S(1.1)) + S(30) > cx1:
            break
        xx = _chip(canvas, td, xx, cxm, g, f_chip, S(1.1)) + S(10)
    d = ImageDraw.Draw(canvas)

    # ---- right side: "At a glance" stat tiles (or a cover card if there is almost no data)
    tiles = []
    if score:
        tiles.append(("AVERAGE SCORE", f"{score / 10:.1f}", "/10", score))
    if pop:
        tiles.append(("POPULARITY", _fmt_count(pop), "users", None))
    if eps:
        tiles.append(("EPISODES", str(eps), "", None))
    if duration:
        tiles.append(("PER EPISODE", str(duration), "min", None))
    if season and year:
        tiles.append(("SEASON", f"{season} {year}", "", None))
    elif year:
        tiles.append(("RELEASED", str(year), "", None))
    if fmt:
        tiles.append(("FORMAT", fmt, "", None))
    tiles = tiles[:6]
    if len(tiles) >= 2:
        px0, pw = S(776), S(440)
        rows = (len(tiles) + 1) // 2
        cell_h, gap = S(74), S(14)
        grid_h = rows * cell_h + (rows - 1) * gap
        head_h = S(44)
        py = (S(H) - (head_h + grid_h)) // 2 + S(6)
        _tt(td, (px0, py + S(14)), "AT A GLANCE", get_font("heavy", S(12.5)), (186, 194, 214), S(2.4))
        _gradient_rect(canvas, (px0, py + S(26), px0 + pw, py + S(29)), VIOLET, (20, 24, 42), S(1.5), horizontal=True)
        d = ImageDraw.Draw(canvas)
        gy = py + head_h
        cw = (pw - gap) // 2
        f_lab, f_val, f_unit = get_font("semibold", S(9.5)), get_font("title", S(27)), get_font("semibold", S(11))
        for i, (lab, val, unit, ring) in enumerate(tiles):
            cx = px0 + (i % 2) * (cw + gap)
            cy0 = gy + (i // 2) * (cell_h + gap)
            _glass(canvas, (cx, cy0, cx + cw, cy0 + cell_h), S(18), shadow=70)
            d = ImageDraw.Draw(canvas)
            d.rounded_rectangle((cx + S(16), cy0 + S(20), cx + S(19), cy0 + cell_h - S(20)), radius=S(1.5), fill=VIOLET)
            tx = cx + S(30)
            _tt(td, (tx, cy0 + S(27)), lab, f_lab, (150, 158, 180), S(1.5))
            vmax = cw - S(30) - S(16) - (S(54) if ring else 0)
            vtxt = _ellipsize(td, val, f_val, vmax)
            td.text((tx, cy0 + S(58)), vtxt, font=f_val, fill=(255, 255, 255), anchor="ls")
            if unit:
                td.text((tx + td.textlength(vtxt, font=f_val) + S(6), cy0 + S(58)), unit, font=f_unit,
                        fill=(150, 158, 180), anchor="ls")
            if ring:                                    # score ring
                rcx, rcy, rr = cx + cw - S(18) - S(21), cy0 + cell_h // 2, S(21)
                d.ellipse((rcx - rr, rcy - rr, rcx + rr, rcy + rr), outline=(62, 68, 92), width=int(S(5)))
                d.arc((rcx - rr, rcy - rr, rcx + rr, rcy + rr), -90, -90 + 360 * ring / 100.0, fill=GOLD,
                      width=int(S(5)))
        d = ImageDraw.Draw(canvas)
    else:
        _cover_card(canvas, cover, S(W - 70), S(110), S(380), S(500), S(16))

    # ---- text pass: soft shadow -> text
    shadow = tl.split()[3].filter(ImageFilter.GaussianBlur(S(3))).point(lambda v: int(v * 0.7))
    canvas.paste((0, 0, 0, 255), (S(1), S(2.5)), shadow)
    canvas.alpha_composite(tl)

    out = canvas.convert("RGB").resize(OUT, Image.LANCZOS)
    buf = BytesIO()
    out.save(buf, "JPEG", quality=97, subsampling=0, optimize=True)
    buf.name = "poster3.jpg"
    buf.seek(0)
    return buf
