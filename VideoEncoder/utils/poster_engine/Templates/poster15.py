"""
poster15.py — "Hex collage" style (dark honeycomb background, anime hex-collage on the right,
big title / genres / synopsis + DOWNLOAD / JOIN NOW buttons on the left).

Accent colour (title 2nd line, genres, buttons, solid hexagons, faint honeycomb lines) is a parameter,
so the same design works in blue / orange / red / green ...

render_poster(meta, tiles, accent="#2F4BFF", brand="ANIME FLICKER", button1="DOWNLOAD", button2="JOIN NOW") -> PIL.Image (1920x1080 RGB)
    meta  : dict(title, genres=[...], synopsis)
    tiles : list of PIL images; ISME SE EK artwork chuna jaata hai (proper backdrop > poster) jo saare hexagons ke
            peeche ek hi bada picture ban kar dikhta hai (hexagon = us picture ki khidkiyan).
"""
from __future__ import annotations

import math
import os
import re
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

OUT_W, OUT_H = 1920, 1080
K = 2                                   # supersampling
GW, GH = 1080.0, 607.5                  # design grid (the layout numbers below are in this grid)
F = OUT_W / GW * K                      # grid unit -> pixel on the supersampled canvas

DEFAULT_ACCENT = "#2F4BFF"
ACCENT_PRESETS = [                      # (label, hex)
    ("🔵 Blue", "#2F4BFF"),
    ("🟠 Orange", "#FF6A00"),
    ("🔴 Red", "#E5262E"),
    ("🟢 Green", "#19C37D"),
    ("🟣 Purple", "#8B3DFF"),
    ("🩷 Pink", "#FF3D8B"),
    ("🩵 Cyan", "#00C8D9"),
    ("🟡 Gold", "#FFC53D"),
]

BG = (13, 14, 18)


def S(v: float) -> int:
    return int(round(v * F))


# ----------------------------------------------------------------------------- fonts
_FONT_DIRS = [Path(os.environ.get("POSTER_FONT_DIR", "")), Path(__file__).resolve().parent / "fonts",
              Path(__file__).resolve().parent.parent / "fonts"]
_FALL = ["DejaVuSans-Bold.ttf", "DejaVuSans.ttf"]


def font(name: str, size: int):
    for d in _FONT_DIRS:
        p = d / name
        if d and p.is_file():
            try:
                return ImageFont.truetype(str(p), size)
            except Exception:
                pass
    for f in _FALL:
        try:
            return ImageFont.truetype(f, size)
        except Exception:
            continue
    return ImageFont.load_default()


# ----------------------------------------------------------------------------- colour helpers
def parse_hex(c, default=DEFAULT_ACCENT):
    s = str(c or "").strip().lstrip("#")
    if re.fullmatch(r"[0-9a-fA-F]{3}", s):
        s = "".join(ch * 2 for ch in s)
    if not re.fullmatch(r"[0-9a-fA-F]{6}", s):
        s = default.lstrip("#")
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))


def mix(a, b, t):
    return tuple(int(round(a[i] * (1 - t) + b[i] * t)) for i in range(3))


def lum(c):
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def on_dark(c):
    """Accent must stay visible on the near-black background (black / dark picks get lifted)."""
    if lum(c) < 70:
        c = mix(c, (255, 255, 255), min(0.75, (70 - lum(c)) / 70 * 0.8 + 0.2))
    return c


# ----------------------------------------------------------------------------- geometry
def hex_pts(cx, cy, r, rot=0.0):
    """Flat-top hexagon (points left / right)."""
    return [(cx + r * math.cos(math.radians(60 * i + rot)), cy + r * math.sin(math.radians(60 * i + rot)))
            for i in range(6)]


def spaced(draw, xy, text, fnt, fill, tracking=0.0):
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=fnt, fill=fill)
        x += fnt.getlength(ch) + tracking
    return x


def text_w(text, fnt, tracking=0.0):
    return sum(fnt.getlength(c) + tracking for c in text) - (tracking if text else 0)


# ----------------------------------------------------------------------------- background
def _background(accent):
    img = Image.new("RGB", (OUT_W * K, OUT_H * K), BG)
    # soft lighter glow behind the collage (right side)
    glow = Image.new("L", img.size, 0)
    gd = ImageDraw.Draw(glow)
    gd.ellipse((S(560), S(-120), S(1240), S(720)), fill=95)
    glow = glow.filter(ImageFilter.GaussianBlur(S(120)))
    tint = mix(BG, accent, 0.20)
    img = Image.composite(Image.new("RGB", img.size, tint), img, glow)

    # honeycomb lines (fainter on the left, stronger on the right)
    r = 30.0
    line_l = mix(BG, accent, 0.07)
    line_r = mix(BG, accent, 0.17)
    layer = Image.new("RGB", img.size, BG)
    d = ImageDraw.Draw(layer)
    dx, dy = 1.5 * r, math.sqrt(3) * r
    ncol = int(GW / dx) + 3
    nrow = int(GH / dy) + 3
    for c in range(-1, ncol):
        for rr in range(-1, nrow):
            cx = c * dx
            cy = rr * dy + (dy / 2 if c % 2 else 0)
            t = max(0.0, min(1.0, (cx - 250) / 600))
            col = mix(line_l, line_r, t)
            pts = [(x * F, y * F) for x, y in hex_pts(cx, cy, r - 0.6)]
            d.line(pts + [pts[0]], fill=col, width=max(1, S(0.9)))
    # blend: pattern only slightly visible
    img = Image.blend(img, ImageChops.lighter(img, layer), 0.9)
    return img


# ----------------------------------------------------------------------------- tiles
def _crop_fill(src: Image.Image, bw: int, bh: int, fx=0.5, fy=0.5, zoom=1.0) -> Image.Image:
    src = src.convert("RGB")
    sc = max(bw / src.width, bh / src.height) * max(1.0, zoom)
    w, h = max(bw, int(src.width * sc)), max(bh, int(src.height * sc))
    big = src.resize((w, h), Image.LANCZOS)
    l = int(max(0, min(w - bw, fx * w - bw / 2)))
    t = int(max(0, min(h - bh, fy * h - bh / 2)))
    return big.crop((l, t, l + bw, t + bh))


def _hex_mask(size, cx, cy, r):
    m = Image.new("L", size, 0)
    ImageDraw.Draw(m).polygon(hex_pts(cx, cy, r), fill=255)
    return m


R_TILE = 104.0
GAP = 3.2


def _layout():
    """Exact flat-top honeycomb (columns 1.5R apart, odd columns half a row lower), grid units.
    kind: 'img' (ek hi bade artwork ka hissa) | 'solid' (accent colour hexagon)."""
    dy = math.sqrt(3) * R_TILE
    xs = [640.0 + 1.5 * R_TILE * k for k in range(4)]

    def pos(k, j):                                    # column k, row j
        return xs[k], 120.0 + dy * (j + (0.5 if k % 2 else 0.0))

    cells = [
        # column 0 (x=640)
        ((0, -1), "solid"), ((0, 0), "img"), ((0, 1), "img"), ((0, 2), "solid"),
        # column 1 (x=796)
        ((1, -1), "img"), ((1, 0), "img"), ((1, 1), "img"), ((1, 2), "img"),
        # column 2 (x=952)
        ((2, -2), "img"), ((2, -1), "img"), ((2, 0), "img"), ((2, 1), "img"), ((2, 2), "img"),
        # column 3 (x=1108)
        ((3, -1), "solid"), ((3, 0), "img"), ((3, 1), "img"), ((3, 2), "solid"),
    ]
    return [(*pos(k, j), kind) for (k, j), kind in cells]


# ----------------------------------------------------------------------------- one artwork behind all hexagons
def _focus(img: Image.Image):
    """Artwork ka 'dilchasp' hissa (edges + colour) — crop wahin center ho. Return (fx, fy) 0..1."""
    sm = img.convert("RGB").resize((64, 64), Image.BILINEAR)
    edges = sm.convert("L").filter(ImageFilter.FIND_EDGES)
    sat = sm.convert("HSV").split()[1]
    e, sa = list(edges.getdata()), list(sat.getdata())
    sw = sx = sy = 0.0
    for i in range(64 * 64):
        w = (0.75 * e[i] + 0.25 * sa[i]) ** 2
        sw += w
        sx += w * (i % 64)
        sy += w * (i // 64)
    if sw <= 0:
        return 0.5, 0.45
    return min(0.7, max(0.3, sx / sw / 63.0)), min(0.65, max(0.3, sy / sw / 63.0))


def _pick_art(tiles: list) -> Image.Image:
    """Poore collage ke liye EK artwork: proper backdrop (aspect <= 2.3) sabse behtar; ultra-wide banner ho to poster."""
    for t in tiles:
        r = t.width / max(t.height, 1)
        if 0.6 <= r <= 2.3 and min(t.size) >= 400:
            return t
    for t in tiles:
        if t.width / max(t.height, 1) <= 2.3:
            return t
    return tiles[0]


def _collage(canvas: Image.Image, art: Image.Image, accent):
    """Ek hi artwork canvas ke right hisse pe bichhao; usko hexagon 'khidkiyon' se dikhao
    (har hexagon us bade artwork ka hi tukda hai — alag-alag seen nahi)."""
    W, H = canvas.size
    x0 = S(520)
    rw, rh = W - x0, H
    fx, fy = _focus(art)
    big = _crop_fill(art, rw, rh, fx, fy, 1.0)

    acc_hi = mix(accent, (255, 255, 255), 0.22)
    acc_lo = mix(accent, (0, 0, 0), 0.30)
    for cx, cy, kind in _layout():
        X, Y, R = cx * F, cy * F, (R_TILE - GAP) * F
        bb = (int(X - R), int(Y - R * math.sqrt(3) / 2), int(X + R) + 1, int(Y + R * math.sqrt(3) / 2) + 1)
        bw, bh = bb[2] - bb[0], bb[3] - bb[1]
        if bb[2] < 0 or bb[0] > W or bb[3] < 0 or bb[1] > H:
            continue
        m3 = _hex_mask((bw * 3, bh * 3), (X - bb[0]) * 3, (Y - bb[1]) * 3, R * 3)      # 3x supersampled edges
        mask = m3.resize((bw, bh), Image.LANCZOS)
        if kind == "img":
            tile = big.crop((bb[0] - x0, bb[1], bb[2] - x0, bb[3]))
            canvas.paste(tile, (bb[0], bb[1]), mask)
        else:
            grad = Image.linear_gradient("L").resize((bw, bh))
            canvas.paste(Image.composite(Image.new("RGB", (bw, bh), acc_lo), Image.new("RGB", (bw, bh), acc_hi), grad),
                         (bb[0], bb[1]), mask)
    return canvas


# ----------------------------------------------------------------------------- text blocks
def _wrap(text, fnt, max_w, tracking=0.0):
    lines, cur = [], ""
    for w in text.split():
        t = (cur + " " + w).strip()
        if cur and text_w(t, fnt, tracking) > max_w:
            lines.append(cur)
            cur = w
        else:
            cur = t
    if cur:
        lines.append(cur)
    return lines


def _fit_ellipsis(text, fnt, max_w, tracking=0.0, suffix="..."):
    if text_w(text, fnt, tracking) <= max_w:
        return text
    while text and text_w(text + suffix, fnt, tracking) > max_w:
        text = text[:-1]
    return text.rstrip(" ,.-:") + suffix


def _title_lines(title, fnt, max_w, tracking):
    title = " ".join((title or "").upper().split()) or "UNKNOWN"
    lines = _wrap(title, fnt, max_w, tracking)
    if len(lines) <= 2:
        return [_fit_ellipsis(l, fnt, max_w, tracking) for l in lines]
    rest = " ".join(lines[1:])
    return [lines[0], _fit_ellipsis(rest, fnt, max_w, tracking)]


def _logo(d, x, y, s, col):
    """small 'A' triangle emblem (brand mark)."""
    w = max(2, int(s * 0.12))
    d.line([(x, y + s), (x + s / 2, y), (x + s, y + s)], fill=col, width=w, joint="curve")
    d.line([(x + s * 0.28, y + s * 0.78), (x + s * 0.72, y + s * 0.78)], fill=col, width=w)
    d.line([(x + s * 0.5, y + s * 0.34), (x + s * 0.5, y + s * 0.6)], fill=col, width=w)


def _button(d, poly, text, fnt, fill, outline, text_col):
    d.polygon([(x * F, y * F) for x, y in poly], fill=fill)
    if outline:
        pts = [(x * F, y * F) for x, y in poly]
        d.line(pts + [pts[0]], fill=outline, width=max(2, S(1.5)), joint="curve")
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    cx, cy = (min(xs) + max(xs)) / 2 * F, (min(ys) + max(ys)) / 2 * F
    tw = text_w(text, fnt, S(0.8))
    spaced(d, (cx - tw / 2, cy - fnt.size * 0.62), text, fnt, text_col, S(0.8))


# ----------------------------------------------------------------------------- main
def render_poster(meta: dict, tiles: list, accent: str = DEFAULT_ACCENT, brand: str = "ANIME FLICKER",
                  button1: str = "DOWNLOAD", button2: str = "JOIN NOW") -> Image.Image:
    acc = on_dark(parse_hex(accent))
    canvas = _background(acc)
    tiles = [t for t in (tiles or []) if t is not None]
    if tiles:
        canvas = _collage(canvas, _pick_art(tiles), acc)
    d = ImageDraw.Draw(canvas)

    # ── brand row ──
    _logo(d, S(58), S(44), S(24), (255, 255, 255))
    bsize = 22
    bf = font("Poppins-Bold.ttf", S(bsize))
    brand = (brand or "ANIME FLICKER").strip()
    while bsize > 11 and text_w(brand, bf, S(0.6)) > S(420):
        bsize -= 1
        bf = font("Poppins-Bold.ttf", S(bsize))
    spaced(d, (S(100), S(57) - bf.size * 0.62), brand, bf, (255, 255, 255), S(0.6))

    # ── title (line 1 white, line 2 accent) ──
    tf = font("Poppins-ExtraBold.ttf", S(43))
    trk = S(0.2)
    lines = _title_lines(meta.get("title"), tf, S(480), trk)
    y = 160.0
    for i, ln in enumerate(lines):
        spaced(d, (S(46), S(y)), ln, tf, (255, 255, 255) if i == 0 else acc, trk)
        y += 48
    y += 4

    # ── genres ──
    gf = font("Poppins-Bold.ttf", S(16.5))
    genres = [g.upper() for g in (meta.get("genres") or [])[:3]] or ["ANIME"]
    spaced(d, (S(46), S(y)), "  ".join(genres), gf, acc, S(0.7))
    y += 31

    # ── synopsis ──
    sf = font("Poppins-Medium.ttf", S(14.2))
    syn = " ".join((meta.get("synopsis") or "").split()) or "No description available."
    max_w, max_lines, line_h = S(385), 5, 20.5
    lines = _wrap(syn, sf, max_w)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        tail = "...read more"
        last = lines[-1]
        while last and text_w(last + tail, sf) > max_w:
            last = last[:-1]
        lines[-1] = last.rstrip(" ,.-:") + tail
    for ln in lines:
        d.text((S(46), S(y)), ln, font=sf, fill=(226, 228, 236))
        y += line_h

    # ── buttons ──
    btf = font("Poppins-Bold.ttf", S(14.5))
    by0, by1 = 437.0, 477.0
    _button(d, [(32, by0), (225, by0), (208, by1), (32, by1)], (button1 or "DOWNLOAD").upper(), btf,
            fill=mix(BG, acc, 0.22), outline=acc, text_col=(255, 255, 255))
    _button(d, [(238, by0), (422, by0), (422, by1), (221, by1)], (button2 or "JOIN NOW").upper(), btf,
            fill=acc, outline=None, text_col=(255, 255, 255) if lum(acc) < 190 else (20, 20, 24))

    out = canvas.resize((OUT_W, OUT_H), Image.LANCZOS)
    return out.convert("RGB")
