import asyncio
import html
import math
import os
import re
import urllib.request
from functools import lru_cache
from io import BytesIO

import aiohttp
from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps


# ------------------------------------------------------------ fonts
FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
_BASE = "https://raw.githubusercontent.com/google/fonts/main/ofl/"
# style -> (google-fonts folder, file)
FONT_FILES = {
    "title": ("bebasneue", "BebasNeue-Regular.ttf"),   # cinematic title
    "bold": ("poppins", "Poppins-Bold.ttf"),
    "semibold": ("poppins", "Poppins-SemiBold.ttf"),
    "medium": ("poppins", "Poppins-Medium.ttf"),
    "regular": ("poppins", "Poppins-Regular.ttf"),
    "body": ("barlow", "Barlow-Regular.ttf"),           # description text
    "bodymed": ("barlow", "Barlow-Medium.ttf"),
    "synopsis": ("spectral", "Spectral-MediumItalic.ttf"),      # synopsis text (stylish italic serif)
    "btn": ("poppins", "Poppins-ExtraBold.ttf"),                # PLAY NOW / MY LIST
    "label": ("kanit", "Kanit-Medium.ttf"),                     # SYNOPSIS heading
}
_FALLBACKS = {
    "title": "DejaVuSans-Bold.ttf",
    "bold": "DejaVuSans-Bold.ttf",
    "semibold": "DejaVuSans-Bold.ttf",
    "btn": "DejaVuSans-Bold.ttf",
    "label": "DejaVuSans-Bold.ttf",
}


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


# ------------------------------------------------------------ images / text helpers
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
            Image.open(io.BytesIO(data)).verify()
            return data
        except Exception as e:  # noqa
            last = e
            await asyncio.sleep(1 + i)
    raise last


async def load_images(meta: dict):
    """Returns (cover_bytes|None, banner_bytes|None). Tries every size, never raises."""
    cover = banner = None
    ci = meta.get("coverImage") or {}
    for key in ("extraLarge", "large", "medium"):
        if ci.get(key):
            try:
                cover = await fetch_bytes(ci[key])
                break
            except Exception as e:  # noqa
                print(f"[img] cover {key} failed: {e}")
    if meta.get("bannerImage"):
        try:
            banner = await fetch_bytes(meta["bannerImage"])
        except Exception as e:  # noqa
            print(f"[img] banner failed: {e}")
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


W, H = 1280, 720          # design grid
K = 3                     # render scale (supersampling)
OUT = (1920, 1080)        # final size
LEFT = 67
GREEN = (70, 211, 105)
RED = (229, 9, 20)          # Netflix logo only
ACCENT = (176, 32, 44)      # softer red for button / accents


def S(v):
    return int(round(v * K))


# ------------------------------------------------------------ masks
def _smooth(t):
    t = max(0.0, min(1.0, t))
    return t * t * (3 - 2 * t)


def _hmask(w, h, left=0, right=0):
    row = Image.new("L", (w, 1), 255)
    px = row.load()
    for x in range(w):
        v = 1.0
        if left and x < left:
            v = min(v, _smooth(x / left))
        if right and x >= w - right:
            v = min(v, _smooth((w - 1 - x) / right))
        px[x, 0] = int(255 * v)
    return row.resize((w, h), Image.NEAREST)


def _ramp(w, h, axis, a0, a1):
    n = w if axis == "x" else h
    line = Image.new("L", (n, 1))
    px = line.load()
    for i in range(n):
        px[i, 0] = int(a0 + (a1 - a0) * _smooth(i / max(1, n - 1)))
    if axis == "x":
        return line.resize((w, h), Image.NEAREST)
    return line.rotate(-90, expand=True).resize((w, h), Image.NEAREST)


def _shade(canvas, x, y, w, h, axis, a0, a1):
    canvas.paste((0, 0, 0, 255), (x, y, x + w, y + h), _ramp(w, h, axis, a0, a1))


# ------------------------------------------------------------ text
def _tw(d, text, font, tr=0):
    return sum(d.textlength(c, font=font) for c in text) + tr * max(0, len(text) - 1)


def _tt(d, xy, text, font, fill, tr=0):
    """Tracked (letter-spaced) text on a baseline."""
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
    """Wrap into <= n lines with the most even line lengths."""
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


def _fit_title(d, title, max_w, tr):
    """Long names: shrink -> balanced 2 lines -> balanced 3 lines -> '...' as a last resort."""
    plans = [(range(104, 63, -4), 1), (range(92, 55, -4), 2), (range(68, 43, -4), 3)]
    for sizes, n in plans:
        for size in sizes:
            f = get_font("title", S(size))
            lines = [title] if n == 1 else _balanced(d, title, f, tr, n, max_w)
            if len(lines) <= n and all(_tw(d, l, f, tr) <= max_w for l in lines):
                return f, lines
    f = get_font("title", S(44))
    lines = _balanced(d, title, f, tr, 3, max_w)
    if len(lines) > 3:
        lines = lines[:3]
        last = lines[-1]
        while len(last) > 1 and _tw(d, last + "...", f, tr) > max_w:
            last = last[:-1].rstrip()
        lines[-1] = last + "..."
    return f, lines


def _badge(d, td, x, cy, text, font, tr):
    tw = _tw(td, text, font, tr)
    pad, h = S(7), S(19)
    d.rounded_rectangle((x, cy - h // 2, x + tw + 2 * pad, cy + h // 2), radius=S(3),
                        outline=(180, 180, 180), width=S(1))
    _tt(td, (x + pad, cy + S(3.8)), text, font, (205, 205, 205), tr)
    return x + tw + 2 * pad


# ------------------------------------------------------------ shapes
def _netflix_n(d, x, y):
    w, h, b = S(33), S(40), S(9)
    d.polygon([(x, y), (x + b, y), (x + w, y + h), (x + w - b, y + h)], fill=(150, 5, 14))
    d.rectangle((x, y, x + b, y + h), fill=RED)
    d.rectangle((x + w - b, y, x + w, y + h), fill=RED)


def _star(d, cx, cy, r):
    pts = []
    for i in range(10):
        a = -math.pi / 2 + i * math.pi / 5
        rad = r if i % 2 == 0 else r * 0.45
        pts.append((cx + rad * math.cos(a), cy + rad * math.sin(a)))
    d.polygon(pts, fill=(255, 197, 24))



def _glass_box(canvas, box, radius):
    """Smooth frosted-glass rounded panel (blurred backdrop + tint + hairline border + soft shadow)."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    # soft shadow
    pad = S(24)
    sh = Image.new("L", (w + 2 * pad, h + 2 * pad), 0)
    ImageDraw.Draw(sh).rounded_rectangle((pad, pad + S(5), pad + w, pad + h + S(5)), radius=radius, fill=120)
    sh = sh.filter(ImageFilter.GaussianBlur(S(10)))
    canvas.paste((0, 0, 0, 255), (x0 - pad, y0 - pad), sh)
    # frosted backdrop
    region = canvas.crop(box).filter(ImageFilter.GaussianBlur(S(12)))
    region = ImageEnhance.Brightness(region).enhance(0.5)
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius=radius, fill=255)
    canvas.paste(region, (x0, y0), mask)
    # glass tint + border
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle((0, 0, w - 1, h - 1), radius=radius,
                                            fill=(255, 255, 255, 14), outline=(255, 255, 255, 60), width=S(1))
    canvas.alpha_composite(layer, (x0, y0))


def _glow(canvas, box, color, radius, alpha=150, blur=8, dy=5):
    """Soft coloured shadow under a pill button."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    pad = S(blur * 3)
    m = Image.new("L", (w + 2 * pad, h + 2 * pad), 0)
    ImageDraw.Draw(m).rounded_rectangle((pad, pad + S(dy), pad + w, pad + h + S(dy)), radius=radius, fill=alpha)
    m = m.filter(ImageFilter.GaussianBlur(S(blur)))
    canvas.paste(color + (255,), (x0 - pad, y0 - pad), m)

# ------------------------------------------------------------ images
def _placeholder(meta):
    """Used only if every image download failed."""
    col = ((meta.get("coverImage") or {}).get("color") or "#2a3550").lstrip("#")
    try:
        c = tuple(int(col[i:i + 2], 16) for i in (0, 2, 4))
    except Exception:
        c = (42, 53, 80)
    im = Image.new("RGB", (460, 650), c)
    return ImageOps.colorize(ImageOps.grayscale(im).filter(ImageFilter.GaussianBlur(2)), (0, 0, 0), c)


def _open(b):
    return Image.open(BytesIO(b)).convert("RGB") if b else None


def _build_hero(cover, banner, use_banner, zw, zh):
    if use_banner and banner is not None:
        return ImageOps.fit(banner, (zw, zh), Image.LANCZOS)
    bg = ImageOps.fit(cover, (zw, zh), Image.LANCZOS).filter(ImageFilter.GaussianBlur(S(30)))
    bg = ImageEnhance.Brightness(bg).enhance(0.45)
    fg = ImageOps.contain(cover, (zw - S(120), zh), Image.LANCZOS)
    fg = fg.filter(ImageFilter.UnsharpMask(radius=S(1.2), percent=70, threshold=3))
    bg.paste(fg, ((zw - fg.width) // 2, (zh - fg.height) // 2), _hmask(fg.width, fg.height, S(60), S(60)))
    return bg


# ------------------------------------------------------------ main
def create_poster(meta: dict, cover_bytes: bytes = None, banner_bytes: bytes = None,
                  use_banner: bool = False) -> BytesIO:
    cover, banner = _open(cover_bytes), _open(banner_bytes)
    if cover is None:                                   # fallbacks: banner -> placeholder
        cover = banner if banner is not None else _placeholder(meta)

    t = meta.get("title") or {}
    title = (t.get("english") or t.get("romaji") or t.get("native") or "Unknown").upper()
    year = meta.get("seasonYear") or (meta.get("startDate") or {}).get("year") or ""
    score = meta.get("averageScore") or meta.get("meanScore")
    genres = [g.upper() for g in (meta.get("genres") or [])[:3]]
    desc = clean_description(meta.get("description"))
    age = "TV-MA" if meta.get("isAdult") else "TV-14"
    eps = meta.get("episodes")

    canvas = Image.new("RGBA", (S(W), S(H)), (0, 0, 0, 255))

    # hero art (right side) faded into black
    zx, zw = S(430), S(760)
    hero = _build_hero(cover, banner, use_banner, zw, S(H))
    canvas.paste(hero, (zx, 0), _hmask(zw, S(H), S(240), S(290)))
    _shade(canvas, 0, 0, S(720), S(H), "x", 235, 0)           # left text shade
    _shade(canvas, 0, S(H - 280), S(W), S(280), "y", 0, 215)  # bottom shade
    _shade(canvas, 0, 0, S(W), S(130), "y", 170, 0)           # top vignette

    d = ImageDraw.Draw(canvas)                                   # shapes
    tl = Image.new("RGBA", canvas.size, (0, 0, 0, 0))            # light text (gets a soft shadow)
    td = ImageDraw.Draw(tl)
    tm = Image.new("L", canvas.size, 0)                          # title mask (gets a gradient fill)
    tmd = ImageDraw.Draw(tm)

    _netflix_n(d, S(57), S(47))

    # ---- title: Bebas Neue, letter-spaced, bottom anchored
    tr = S(2.5)
    f_title, lines = _fit_title(d, title, S(640), tr)
    base = S(414)
    step = int(f_title.size * 0.98)
    for i, line in enumerate(reversed(lines)):
        _tt(tmd, (S(LEFT), base - i * step), line, f_title, 255, tr)

    # ---- meta row: match / year / badges / episodes
    f_match = get_font("semibold", S(17))
    f_year = get_font("medium", S(17))
    f_tag = get_font("semibold", S(10.5))
    x, cy = S(LEFT), S(448)
    if score:
        txt = f"{score}% Match"
        _tt(td, (x, cy + S(6)), txt, f_match, GREEN, S(0.4))
        x += _tw(td, txt, f_match, S(0.4)) + S(22)
    if year:
        _tt(td, (x, cy + S(6)), str(year), f_year, (215, 215, 215), S(0.5))
        x += _tw(td, str(year), f_year, S(0.5)) + S(14)
    x = _badge(d, td, x, cy, age, f_tag, S(0.9)) + S(8)
    x = _badge(d, td, x, cy, "HD", f_tag, S(0.9)) + S(16)
    if eps:
        _tt(td, (x, cy + S(4)), f"{eps} EPISODES", get_font("medium", S(11)), (160, 160, 160), S(1.4))

    # ---- genres: tracked caps with red dot separators
    f_gen = get_font("medium", S(11.5))
    gx, gy = S(LEFT), S(479)
    for i, g in enumerate(genres):
        if i:
            d.ellipse((gx + S(9) - S(2), gy - S(2), gx + S(9) + S(2), gy + S(2)), fill=ACCENT)
            gx += S(18)
        _tt(td, (gx, gy + S(4.2)), g, f_gen, (178, 178, 178), S(1.6))
        gx += _tw(td, g, f_gen, S(1.6))

    # ---- synopsis in a smooth glass box
    bx0, by0, bx1, by1 = S(LEFT), S(496), S(LEFT + 452), S(622)
    _glass_box(canvas, (bx0, by0, bx1, by1), S(12))
    d = ImageDraw.Draw(canvas)

    hx, hy = bx0 + S(16), by0 + S(19)
    d.rounded_rectangle((hx, hy - S(6.5), hx + S(3), hy + S(6.5)), radius=S(1.5), fill=ACCENT)
    _tt(td, (hx + S(11), hy + S(4.5)), "SYNOPSIS", get_font("label", S(11.5)), (240, 240, 240), S(2.4))

    f_desc = get_font("synopsis", S(15.5))
    max_w, max_lines = S(452 - 32), 4
    dl = _wrap(d, desc, f_desc, max_w)
    if len(dl) > max_lines:
        dl = dl[:max_lines]
        last = dl[-1].split()
        while last and d.textlength(" ".join(last) + "...", font=f_desc) > max_w:
            last.pop()
        dl[-1] = " ".join(last) + "..."
    for i, line in enumerate(dl):
        td.text((hx, by0 + S(45 + i * 19.5)), line, font=f_desc, fill=(226, 226, 226), anchor="lm")

    # ---- buttons: pill + text + circle icon (red PLAY NOW / glass MY LIST), rating beside them
    f_btn = get_font("btn", S(14.5))
    trb = S(0.9)
    x0, top, bh = S(LEFT), S(634), S(42)
    cyb = top + bh // 2
    dia, padl, padr, gap = S(28), S(22), S(7), S(16)
    base_y = cyb + S(5.1)

    # PLAY NOW  (red pill, white circle with red play icon)
    tw = _tw(d, "PLAY NOW", f_btn, trb)
    pw = int(padl + tw + gap + dia + padr)
    _glow(canvas, (x0, top, x0 + pw, top + bh), ACCENT, bh // 2, alpha=75)
    d = ImageDraw.Draw(canvas)
    d.rounded_rectangle((x0, top, x0 + pw, top + bh), radius=bh // 2, fill=ACCENT)
    _tt(d, (x0 + padl, base_y), "PLAY NOW", f_btn, (255, 255, 255), trb)
    ccx, ccy = x0 + pw - padr - dia // 2, cyb
    d.ellipse((ccx - dia // 2, ccy - dia // 2, ccx + dia // 2, ccy + dia // 2), fill=(255, 255, 255))
    d.polygon([(ccx - S(3.5), ccy - S(6)), (ccx - S(3.5), ccy + S(6)), (ccx + S(6.5), ccy)], fill=ACCENT)

    # MY LIST  (glass pill, white circle with dark plus)
    mx = x0 + pw + S(14)
    tw2 = _tw(d, "MY LIST", f_btn, trb)
    mw = int(padl + tw2 + gap + dia + padr)
    _glass_box(canvas, (mx, top, mx + mw, top + bh), bh // 2)
    d = ImageDraw.Draw(canvas)
    _tt(td, (mx + padl, base_y), "MY LIST", f_btn, (245, 245, 245), trb)
    ccx = mx + mw - padr - dia // 2
    d.ellipse((ccx - dia // 2, ccy - dia // 2, ccx + dia // 2, ccy + dia // 2), fill=(255, 255, 255))
    lw = int(S(2.6))
    d.line((ccx - S(6), ccy, ccx + S(6), ccy), fill=(20, 20, 24), width=lw)
    d.line((ccx, ccy - S(6), ccx, ccy + S(6)), fill=(20, 20, 24), width=lw)

    if score:
        sx = mx + mw + S(36)
        _star(d, sx, cyb, S(13))
        f_sc = get_font("bold", S(22))
        sc = f"{score / 10:.1f}"
        td.text((sx + S(22), cyb + S(1)), sc, font=f_sc, fill=(255, 255, 255), anchor="lm")
        td.text((sx + S(22) + td.textlength(sc, font=f_sc) + S(3), cyb + S(5)), "/10",
                font=get_font("medium", S(12)), fill=(150, 150, 150), anchor="lm")

    # ---- text pass: soft shadow -> light text -> gradient title
    alpha = ImageChops.lighter(tl.split()[3], tm)
    shadow = alpha.filter(ImageFilter.GaussianBlur(S(3.5))).point(lambda v: int(v * 0.9))
    canvas.paste((0, 0, 0, 255), (S(1.5), S(3)), shadow)
    canvas.alpha_composite(tl)
    bb = tm.getbbox()
    if bb:
        gw, gh = bb[2] - bb[0], bb[3] - bb[1]
        col = Image.new("RGB", (1, gh))
        px = col.load()
        for y in range(gh):
            k = y / max(1, gh - 1)
            px[0, y] = tuple(int(a + (b - a) * k) for a, b in zip((255, 255, 255), (190, 202, 228)))
        canvas.paste(col.resize((gw, gh)), (bb[0], bb[1]), tm.crop(bb))

    out = canvas.convert("RGB").resize(OUT, Image.LANCZOS)
    buf = BytesIO()
    out.save(buf, "JPEG", quality=97, subsampling=0, optimize=True)
    buf.name = "poster.jpg"
    buf.seek(0)
    return buf
