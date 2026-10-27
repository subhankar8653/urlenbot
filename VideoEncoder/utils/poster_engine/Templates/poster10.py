import asyncio
import html
import math
import os
import re
import urllib.request
from functools import lru_cache
from io import BytesIO

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

# ------------------------------------------------------------ fonts (poster10 only)
FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
_BASE = "https://raw.githubusercontent.com/google/fonts/main/ofl/"
FONT_FILES = {
    "title": ("anton", "Anton-Regular.ttf"),
    "heavy": ("poppins", "Poppins-ExtraBold.ttf"),
    "semibold": ("poppins", "Poppins-SemiBold.ttf"),
    "medium": ("poppins", "Poppins-Medium.ttf"),
    "italic": ("poppins", "Poppins-LightItalic.ttf"),
}
_FALLBACKS = {"title": "DejaVuSans-Bold.ttf", "heavy": "DejaVuSans-Bold.ttf", "semibold": "DejaVuSans-Bold.ttf"}


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


# ------------------------------------------------------------ images / text cleanup
async def fetch_bytes(url: str, retries: int = 3) -> bytes:
    """Download an image with retries and validate it is a real image."""
    import aiohttp   # only needed by this helper, which the bot does not use
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
BG = (255, 255, 255)
NAVY = (222, 230, 255)         # pale blue disc behind the art
TEXT = (14, 18, 58)            # dark navy text
MUTED = (78, 86, 122)
BODY = (58, 66, 102)             # synopsis text
PINK_TXT = (240, 72, 120)        # pink used for text on white
CHIP = (232, 236, 247)
CHIP_LINE = (212, 218, 236)
PINK = (255, 112, 150)
PINK_TOP, PINK_BOT = (255, 138, 174), (255, 98, 140)
INK = (255, 255, 255)           # glyph / pill-text colour on pink


def S(v):
    return int(round(v * K))


# ------------------------------------------------------------ helpers
def _smooth(t):
    t = max(0.0, min(1.0, t))
    return t * t * (3 - 2 * t)


def _tw(d, text, font, tr=0):
    return sum(d.textlength(c, font=font) for c in text) + tr * max(0, len(text) - 1)


def _ctt(d, cx, y, text, font, fill, tr=0, stroke=0, stroke_fill=None):
    """Centred, letter-spaced text on a baseline."""
    x = cx - _tw(d, text, font, tr) / 2
    for c in text:
        d.text((x, y), c, font=font, fill=fill, anchor="ls", stroke_width=stroke, stroke_fill=stroke_fill)
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


def _glow(canvas, cx, cy, r, color, alpha):
    g = ImageOps.invert(Image.radial_gradient("L")).resize((2 * r, 2 * r), Image.BICUBIC)
    g = g.point(lambda v: int(alpha * _smooth(v / 255.0) ** 1.5))
    canvas.paste(color + (255,), (cx - r, cy - r), g)


def _gradient_rect(canvas, box, c0, c1, radius):
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    col = Image.new("RGB", (1, h))
    px = col.load()
    for y in range(h):
        k = y / max(1, h - 1)
        px[0, y] = tuple(int(a + (b - a) * k) for a, b in zip(c0, c1))
    m = Image.new("L", (w, h), 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, w - 1, h - 1), radius=radius, fill=255)
    canvas.paste(col.resize((w, h)), (x0, y0), m)


def _burst(d, cx, cy, r, fill, points=8):
    """Small star-burst icon."""
    pts = []
    for i in range(points * 2):
        a = -math.pi / 2 + i * math.pi / points
        rad = r if i % 2 == 0 else r * 0.42
        pts.append((cx + rad * math.cos(a), cy + rad * math.sin(a)))
    d.polygon(pts, fill=fill)


def _star(d, cx, cy, r, fill):
    _burst(d, cx, cy, r, fill, points=5)


def _cap_h(font):
    b = font.getbbox("H")
    return b[3] - b[1]


def _fit_title(d, title, max_w, tr, max_h):
    """Biggest title that fits the width and max height: 1 line -> 2 -> 3 -> '...'."""
    def block_h(f, n):
        return (n - 1) * int(f.size * 1.1) + _cap_h(f)

    for n, sizes in ((1, range(96, 51, -2)), (2, range(84, 45, -2)), (3, range(60, 33, -1))):
        for size in sizes:
            f = get_font("title", S(size))
            lines = [title] if n == 1 else _balanced(d, title, f, tr, n, max_w)
            if len(lines) <= n and all(_tw(d, l, f, tr) <= max_w for l in lines) and block_h(f, len(lines)) <= max_h:
                return f, lines, block_h(f, len(lines))
    f = get_font("title", S(34))
    lines = _balanced(d, title, f, tr, 3, max_w)[:3]
    last = lines[-1]
    while len(last) > 1 and _tw(d, last + "...", f, tr) > max_w:
        last = last[:-1].rstrip()
    lines[-1] = last + "..."
    return f, lines, block_h(f, len(lines))


def _tt(d, xy, text, font, fill, tr=0):
    x, y = xy
    for c in text:
        d.text((x, y), c, font=font, fill=fill, anchor="ls")
        x += d.textlength(c, font=font) + tr


def _sparkle(d, cx, cy, r, fill):
    k = 0.22
    d.polygon([(cx, cy - r), (cx + r * k, cy - r * k), (cx + r, cy), (cx + r * k, cy + r * k), (cx, cy + r),
               (cx - r * k, cy + r * k), (cx - r, cy), (cx - r * k, cy - r * k)], fill=fill)


def _cross(d, cx, cy, r, fill, w):
    d.line((cx - r, cy - r, cx + r, cy + r), fill=fill, width=w)
    d.line((cx - r, cy + r, cx + r, cy - r), fill=fill, width=w)


def _zigzag(d, x, y, n, step, amp, fill, w):
    pts = [(x + i * step, y + (amp if i % 2 else -amp)) for i in range(n)]
    d.line(pts, fill=fill, width=w, joint="curve")


def _curve(d, p0, p1, p2, fill, w, steps=48):
    pts = []
    for i in range(steps + 1):
        t = i / steps
        pts.append(((1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * p1[0] + t * t * p2[0],
                    (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * p1[1] + t * t * p2[1]))
    d.line(pts, fill=fill, width=w, joint="curve")


def _title_segments(lines):
    """Two-tone title: first half dark, second half pink (split on a line boundary when there are
    2+ lines, otherwise on the middle word / letter)."""
    words = [l.split() for l in lines]
    if len(lines) >= 2:
        cut = (len(lines) + 1) // 2
        return [[(w, TEXT if li < cut else PINK_TXT, True) for w in ws] for li, ws in enumerate(words)]
    ws = words[0]
    if len(ws) >= 2:
        cut = (len(ws) + 1) // 2
        return [[(w, TEXT if i < cut else PINK_TXT, True) for i, w in enumerate(ws)]]
    w = ws[0]
    cut = max(1, (len(w) + 1) // 2)
    return [[(w[:cut], TEXT, False), (w[cut:], PINK_TXT, False)]]


def _draw_segs(d, x, y, segs, font, tr):
    sp = d.textlength(" ", font=font) + tr
    for text, col, gap in segs:
        for c in text:
            d.text((x, y), c, font=font, fill=col, anchor="ls")
            x += d.textlength(c, font=font) + tr
        if gap:
            x += sp


def _highlight_idx(words):
    """Pick up to 5 long words, spaced apart, to colour pink in the synopsis."""
    idx, last = set(), -8
    for i, w in enumerate(words):
        if len(re.sub(r"[^A-Za-z]", "", w)) >= 7 and i - last >= 7 and len(idx) < 5:
            idx.add(i)
            last = i
    return idx


# ------------------------------------------------------------ main
def create_poster(meta: dict, cover_bytes: bytes = None, banner_bytes: bytes = None,
                  button_text: str = None) -> BytesIO:
    cover, banner = _open(cover_bytes), _open(banner_bytes)
    if cover is None:
        cover = banner if banner is not None else _placeholder(meta)

    t = meta.get("title") or {}
    title = (t.get("english") or t.get("romaji") or t.get("native") or "Unknown").upper()
    score = meta.get("averageScore") or meta.get("meanScore")
    studios = ((meta.get("studios") or {}).get("nodes")) or []
    studio = studios[0]["name"].upper() if studios else None
    fmt = (meta.get("format") or "").replace("_", " ").upper()
    status = (meta.get("status") or "").replace("_", " ").upper()
    eps = meta.get("episodes")
    duration = meta.get("duration")
    genres = [g.upper() for g in (meta.get("genres") or [])[:3]]
    desc = clean_description(meta.get("description"))
    # the pill is now a call-to-action button (was the season, e.g. "SUMMER 2024")
    pill_text = (button_text or "").strip().upper() or "WATCH NOW"
    pill_text = pill_text[:22]

    canvas = Image.new("RGBA", (S(W), S(H)), BG + (255,))
    d = ImageDraw.Draw(canvas)

    # ---- decorative elements (behind everything)
    d.rounded_rectangle((S(800), S(-30), S(884), S(52)), radius=S(18), outline=(255, 206, 222), width=int(S(2.5)))
    d.rounded_rectangle((S(858), S(6), S(934), S(80)), radius=S(18), outline=PINK, width=int(S(3)))
    _sparkle(d, S(992), S(84), S(18), PINK)
    _cross(d, S(716), S(86), S(9), TEXT, int(S(4.5)))
    for i in range(3):
        for j in range(3):
            d.ellipse((S(1204 + j * 17) - S(2.6), S(92 + i * 17) - S(2.6), S(1204 + j * 17) + S(2.6),
                       S(92 + i * 17) + S(2.6)), fill=PINK)
    d.ellipse((S(1232) - S(8), S(220) - S(8), S(1232) + S(8), S(220) + S(8)), outline=PINK, width=int(S(3)))
    _sparkle(d, S(1214), S(560), S(14), (255, 170, 196))
    _zigzag(d, S(660), S(694), 7, S(10), S(5), PINK, int(S(3.2)))
    _sparkle(d, S(770), S(692), S(12), PINK)
    _curve(d, (S(880), S(718)), (S(1030), S(688)), (S(1185), S(676)), TEXT, int(S(1.8)))

    # ---- art: half / more-than-half circle (left part runs off the poster) + pink lining
    acx, acy, adia = S(150), S(360), S(800)
    R = adia // 2
    art = ImageOps.fit(cover, (adia, adia), Image.LANCZOS, centering=(0.5, 0.2)).filter(
        ImageFilter.UnsharpMask(radius=S(1.3), percent=60, threshold=3))
    m = Image.new("L", (adia, adia), 0)
    ImageDraw.Draw(m).ellipse((0, 0, adia - 1, adia - 1), fill=255)
    canvas.paste(art, (acx - R, acy - R), m)
    d = ImageDraw.Draw(canvas)
    d.ellipse((acx - R - S(9), acy - R - S(9), acx + R + S(9), acy + R + S(9)), outline=PINK, width=int(S(9)))
    for i in range(6):
        d.ellipse((S(214 + i * 19) - S(2.6), S(676) - S(2.6), S(214 + i * 19) + S(2.6), S(676) + S(2.6)),
                  fill=(255, 255, 255))

    tl = Image.new("RGBA", canvas.size, (0, 0, 0, 0))     # text layer
    td = ImageDraw.Draw(tl)

    # ---- right column (left aligned) ---------------------------------------------
    x0, col_w = S(650), S(570)
    tr = S(2)
    f_title, lines, title_h = _fit_title(d, title, col_w, tr, S(215))
    step = int(f_title.size * 1.1)

    f_chip = get_font("semibold", S(14))
    chips = [(g, None) for g in genres]
    if score:
        chips.append((f"{score / 10:.1f} / 10", "star"))
    chip_h = S(38)

    f_desc = get_font("italic", S(15))
    inner = col_w - S(44)
    all_lines = _wrap(d, desc, f_desc, inner)
    dl = all_lines[:4]
    if len(all_lines) > 4:
        last = dl[-1].split()
        while last and d.textlength(" ".join(last) + "...", font=f_desc) > inner:
            last.pop()
        dl[-1] = " ".join(last) + "..."
    line_pitch = S(25)
    box_h = len(dl) * line_pitch + S(28)
    btn_h = S(54)
    top_h = S(26)
    total = top_h + S(24) + title_h + S(24) + (chip_h + S(20) if chips else 0) + box_h + S(24) + btn_h
    y = S(70) + max(0, (S(540) - total) // 2)

    # studio
    top_txt = studio or fmt or "ANIME"
    f_top = get_font("heavy", S(17))
    _burst(d, x0 + S(12), y + S(13), S(12), PINK)
    d.ellipse((x0 + S(8), y + S(9), x0 + S(16), y + S(17)), fill=BG)
    _tt(td, (x0 + S(34), y + S(19)), top_txt, f_top, TEXT, S(1.2))
    y += top_h + S(24)

    # two-tone title
    y += _cap_h(f_title)
    for i, segs in enumerate(_title_segments(lines)):
        _draw_segs(td, x0, y + i * step, segs, f_title, tr)
    y += (len(lines) - 1) * step + S(24)

    # chips: genres + rating
    if chips:
        cx_ = x0
        for txt, kind in chips:
            wtxt = _tw(td, txt, f_chip, S(0.6))
            cw_ = int(wtxt + S(36) + (S(24) if kind else 0))
            if cx_ + cw_ > x0 + col_w:
                break
            d.rounded_rectangle((cx_, y, cx_ + cw_, y + chip_h), radius=S(10), fill=CHIP, outline=CHIP_LINE,
                                width=int(S(1.5)))
            tx = cx_ + S(18)
            if kind == "star":
                _star(d, tx + S(8), y + chip_h // 2, S(9), PINK_TXT)
                tx += S(24)
            _tt(td, (tx, y + chip_h // 2 + S(5)), txt, f_chip, MUTED, S(0.6))
            cx_ += cw_ + S(12)
        y += chip_h + S(20)

    # transparent synopsis box + pink highlighted words
    bx0, bx1 = x0, x0 + col_w
    layer = Image.new("RGBA", (bx1 - bx0, box_h), (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle((0, 0, bx1 - bx0 - 1, box_h - 1), radius=S(14),
                                            fill=(196, 208, 240, 74), outline=(184, 196, 232, 190),
                                            width=int(S(1.5)))
    canvas.alpha_composite(layer, (bx0, y))
    d = ImageDraw.Draw(canvas)
    flat = [w for line in dl for w in line.split()]
    hl = _highlight_idx(flat)
    gi = 0
    sp = td.textlength(" ", font=f_desc)
    for li, line in enumerate(dl):
        xx = bx0 + S(22)
        base = y + S(14) + li * line_pitch + S(17)
        for w in line.split():
            col = PINK_TXT if gi in hl else BODY
            td.text((xx, base), w, font=f_desc, fill=col, anchor="ls")
            xx += td.textlength(w, font=f_desc) + sp
            gi += 1
    y += box_h + S(24)

    # watch button: pink pill, dark text, dark circle with a pink arrow
    f_btn = get_font("heavy", S(17))
    dia, padl, gap, padr = S(40), S(32), S(36), S(7)
    bw = int(padl + _tw(td, pill_text, f_btn, S(1)) + gap + dia + padr)
    bw = min(bw, col_w)
    _gradient_rect(canvas, (x0, y, x0 + bw, y + btn_h), PINK_TOP, PINK_BOT, btn_h // 2)
    d = ImageDraw.Draw(canvas)
    _tt(td, (x0 + padl, y + btn_h // 2 + S(6)), pill_text, f_btn, TEXT, S(1))
    ccx, ccy = x0 + bw - padr - dia // 2, y + btn_h // 2
    d.ellipse((ccx - dia // 2, ccy - dia // 2, ccx + dia // 2, ccy + dia // 2), fill=TEXT)
    aw = int(S(2.8))
    d.line((ccx - S(8), ccy, ccx + S(8), ccy), fill=PINK_TOP, width=aw)
    d.line([(ccx + S(2), ccy - S(7)), (ccx + S(8.5), ccy), (ccx + S(2), ccy + S(7))], fill=PINK_TOP, width=aw,
           joint="curve")

    # ---- stat row (bottom) ---------------------------------------------------------
    items = []
    if eps:
        items.append(("play", f"{eps} EPISODES"))
    if duration:
        items.append(("dot", f"{duration} MIN"))
    if fmt:
        items.append(("dot", fmt + (f"  \u2022  {status}" if status else "")))
    if items:
        ytxt = S(650)
        d.line((x0, S(622), x0 + col_w, S(622)), fill=(214, 220, 240), width=int(S(1.3)))
        f_stat = get_font("semibold", S(13.5))
        slot = col_w / len(items)
        for i, (kind, txt) in enumerate(items):
            icx = x0 + slot * (i + 0.5)
            txt = _ellipsize(td, txt, f_stat, slot - S(52))
            wt = _tw(td, txt, f_stat, S(1))
            xs = icx - (S(32) + wt) / 2
            d.ellipse((xs, ytxt - S(12), xs + S(24), ytxt + S(12)), fill=PINK)
            gx, gy = xs + S(12), ytxt
            if kind == "play":
                d.polygon([(gx - S(3), gy - S(5.5)), (gx - S(3), gy + S(5.5)), (gx + S(5.5), gy)], fill=INK)
            else:
                d.ellipse((gx - S(4), gy - S(4), gx + S(4), gy + S(4)), fill=INK)
            _tt(td, (xs + S(32), ytxt + S(4.8)), txt, f_stat, TEXT, S(1))

    canvas.alpha_composite(tl)

    out = canvas.convert("RGB").resize(OUT, Image.LANCZOS)
    buf = BytesIO()
    out.save(buf, "JPEG", quality=97, subsampling=0, optimize=True)
    buf.name = "poster10.jpg"
    buf.seek(0)
    return buf
