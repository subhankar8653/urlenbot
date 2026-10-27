from __future__ import annotations

import io
import math
import os
import textwrap
from dataclasses import dataclass, field

import requests
from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

FONT_DIR = os.path.join(os.path.dirname(__file__), "fonts")

CANVAS_W, CANVAS_H = 1600, 900

COLOR_WHITE = (255, 255, 255)
COLOR_OFFWHITE = (225, 227, 230)
COLOR_GRAY = (150, 154, 160)
COLOR_DIM = (120, 124, 130)
COLOR_RED = (214, 40, 57)          # brand red ("ANIME", accents)
COLOR_RED_SOFT = (214, 40, 57, 235)
GLASS_STRENGTH = 0.2                 # 1.0 = heavy frosted glass; 0.2 = as light as the GENRES box (fill 14, outline 40)
COLOR_RED_LIGHT = (255, 112, 124)    # brighter red for small labels on the glass card

_font_cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


def font(name: str, size: int, weight: int | None = None) -> ImageFont.FreeTypeFont:
    """
    Load (and cache) a font by filename from /fonts. If `weight` is given and
    the font is a variable font, set its weight axis (any other axes, e.g.
    optical size, are left at a sensible default) — e.g. Noto Sans JP, Inter.
    """
    key = (name, size, weight)
    if key not in _font_cache:
        path = os.path.join(FONT_DIR, name)
        f = ImageFont.truetype(path, size)
        if weight is not None:
            try:
                axes = f.get_variation_axes()
                values = []
                for ax in axes:
                    if ax["name"] == b"Weight":
                        values.append(weight)
                    else:
                        values.append(ax["default"])
                f.set_variation_by_axes(values)
            except Exception:
                pass
        _font_cache[key] = f
    return _font_cache[key]


# Font roles -----------------------------------------------------------
# NOTE: "Anicrox" is the brand *name* (rendered as text), not a font.
# The brand wordmark + big display headings use a bold condensed display
# face standing in for "Horizon" (a paid/non-Google font) - swap the file
# below for the real Horizon.ttf/otf if you own a license.
F_DISPLAY = "Anton-Regular.ttf"          # bold display / headline face ("Horizon"-style)
# Heading (movie title): Bebas Neue in capitals, lightly letter-spaced, with a vertical
# white -> lavender gradient and a soft shadow (same look as the streaming-style poster 9).
F_TITLE = "BebasNeue-Regular.ttf"
TITLE_WEIGHT = None                      # variable-font weight (None for static fonts)
TITLE_SCALE = 1.28                       # Bebas is narrow/short -> render it larger than Anton
TITLE_TRACKING = 0.03                    # letter-spacing in em
TITLE_UPPER = True
TITLE_GRAD = ((255, 255, 255), (190, 202, 228))   # top colour -> bottom colour
F_BODY = "Poppins-Regular.ttf"
F_BODY_MED = "Poppins-SemiBold.ttf"
F_BODY_BOLD = "Poppins-Bold.ttf"
F_NATIVE = "NotoSansJP-Variable.ttf"     # covers CJK glyphs for native titles
F_SYNOPSIS = "Lora-Variable.ttf"         # elegant serif for the synopsis (pairs with the italic romaji line)
SYN_WEIGHT = 500                         # variable-font weight (None for static fonts)
SYN_SCALE = 1.02                         # size nudge when you swap F_SYNOPSIS (serifs usually need ~1.05)
F_ROMAJI = "DMSerifDisplay-Italic.ttf"   # elegant italic serif for the romaji line under the Japanese title
ROMAJI_WEIGHT = None                     # set e.g. 500 if you switch F_ROMAJI to a variable font
ROMAJI_SCALE = 1.15                      # serif italics look smaller than Poppins, so nudge the size up


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass
class PosterMeta:
    title_english: str
    title_native: str = ""
    title_romaji: str = ""
    synopsis: str = ""
    rating: str = "N/A"
    year: str = "—"
    episodes: str = "—"
    studio: str = "—"
    genres: str = "—"
    image_url: str | None = None
    brand_top: str = "ANI"
    brand_bottom: str = "CROX"
    brand_tagline: str = "アニメ  •  TRAILERS  •  NEWS  •  UPDATES"
    brand_small: str = "ANICROX"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _download_image(url: str, timeout: int = 20) -> Image.Image | None:
    try:
        r = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        return Image.open(io.BytesIO(r.content)).convert("RGB")
    except Exception:
        return None


def _cover_fit(img: Image.Image, w: int, h: int) -> Image.Image:
    """Resize+crop `img` to exactly (w, h), like CSS `background-size: cover`."""
    src_w, src_h = img.size
    scale = max(w / src_w, h / src_h)
    new_w, new_h = math.ceil(src_w * scale), math.ceil(src_h * scale)
    img = img.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - w) // 2
    top = (new_h - h) // 2
    return img.crop((left, top, left + w, top + h))


def _placeholder_background(w: int, h: int) -> Image.Image:
    """
    Original, generated key-art-style background used only as a fallback
    when no AniList image is available (or for local demos where the
    AniList CDN isn't reachable). This is procedurally painted - not a
    copy of any existing artwork - so it's always safe to ship as a
    default. In real use `image_url` from AniList replaces this entirely.
    """
    import random
    rng = random.Random(7)

    # dusky sky gradient
    img = Image.new("RGB", (w, h), (18, 20, 30))
    top = (46, 42, 74)
    mid = (92, 58, 74)
    bottom = (16, 15, 22)
    px = img.load()
    for y in range(h):
        t = y / h
        if t < 0.55:
            tt = t / 0.55
            r = int(top[0] + (mid[0] - top[0]) * tt)
            g = int(top[1] + (mid[1] - top[1]) * tt)
            b = int(top[2] + (mid[2] - top[2]) * tt)
        else:
            tt = (t - 0.55) / 0.45
            r = int(mid[0] + (bottom[0] - mid[0]) * tt)
            g = int(mid[1] + (bottom[1] - mid[1]) * tt)
            b = int(mid[2] + (bottom[2] - mid[2]) * tt)
        for x in range(0, w, 3):
            for dx in range(3):
                if x + dx < w:
                    px[x + dx, y] = (r, g, b)

    draw = ImageDraw.Draw(img, "RGBA")

    # soft moon
    moon_r = int(h * 0.11)
    mx, my = int(w * 0.78), int(h * 0.22)
    for i in range(6, 0, -1):
        a = int(10 * i)
        draw.ellipse([mx - moon_r - i * 6, my - moon_r - i * 6,
                      mx + moon_r + i * 6, my + moon_r + i * 6],
                     fill=(255, 235, 210, a))
    draw.ellipse([mx - moon_r, my - moon_r, mx + moon_r, my + moon_r],
                 fill=(255, 244, 224, 235))

    # layered mountain silhouettes
    layers = [
        (0.62, (26, 24, 40, 255)),
        (0.72, (18, 17, 28, 255)),
        (0.85, (10, 10, 16, 255)),
    ]
    for base_t, color in layers:
        pts = [(0, h)]
        n = 9
        for i in range(n + 1):
            x = w * i / n
            y = h * (base_t + rng.uniform(-0.05, 0.05))
            pts.append((x, y))
        pts.append((w, h))
        draw.polygon(pts, fill=color)

    # drifting petal/leaf accents (echoes the reference's floating petals)
    petal_colors = [(178, 46, 58, 150), (206, 64, 74, 120), (150, 40, 52, 100)]
    for _ in range(26):
        x = rng.uniform(0, w)
        y = rng.uniform(0, h * 0.75)
        r = rng.uniform(4, 14)
        c = rng.choice(petal_colors)
        draw.ellipse([x - r, y - r * 0.6, x + r, y + r * 0.6], fill=c)

    # abstract foreground silhouette (generic figure-ish shape, not a
    # copy of any specific character) to give the frame some depth
    fig_color = (8, 8, 12, 255)
    fx = w * 0.83
    draw.ellipse([fx - w * 0.09, h * 0.30, fx + w * 0.09, h * 0.30 + w * 0.18], fill=fig_color)
    draw.polygon([
        (fx - w * 0.16, h * 1.05),
        (fx - w * 0.12, h * 0.45),
        (fx + w * 0.12, h * 0.45),
        (fx + w * 0.18, h * 1.05),
    ], fill=fig_color)

    img = img.filter(ImageFilter.GaussianBlur(0.6))
    return img


def _wrap_by_width(draw: ImageDraw.ImageDraw, text: str, fnt: ImageFont.FreeTypeFont,
                    max_width: int) -> list[str]:
    """Greedy word-wrap using actual glyph measurements."""
    words = text.split()
    lines: list[str] = []
    cur = ""
    for word in words:
        trial = (cur + " " + word).strip()
        if draw.textlength(trial, font=fnt) <= max_width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def _truncate_paragraph(draw: ImageDraw.ImageDraw, text: str, fnt: ImageFont.FreeTypeFont,
                         max_width: int, max_lines: int) -> list[str]:
    lines = _wrap_by_width(draw, text, fnt, max_width)
    if len(lines) <= max_lines:
        return lines
    lines = lines[:max_lines]
    last = lines[-1]
    while draw.textlength(last + "...", font=fnt) > max_width and " " in last:
        last = last.rsplit(" ", 1)[0]
    lines[-1] = last.rstrip(".") + "..."
    return lines


def _tracked_text(draw: ImageDraw.ImageDraw, xy, text: str, fnt: ImageFont.FreeTypeFont,
                   fill, tracking: int = 0):
    """Draw text with manual letter-spacing (Pillow has no native tracking)."""
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=fnt, fill=fill)
        x += draw.textlength(ch, font=fnt) + tracking
    return x


def _tracked_text_width(draw: ImageDraw.ImageDraw, text: str, fnt: ImageFont.FreeTypeFont,
                         tracking: int = 0) -> int:
    w = 0
    for ch in text:
        w += draw.textlength(ch, font=fnt) + tracking
    return w - tracking if text else 0


def _rounded_rect(draw: ImageDraw.ImageDraw, box, radius, outline=None, width=1, fill=None):
    draw.rounded_rectangle(box, radius=radius, outline=outline, width=width, fill=fill)


# ---- simple line icons -----------------------------------------------

def _icon_hamburger(size: int, color) -> Image.Image:
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    stroke = max(2, size // 12)
    pad = size * 0.15
    ys = [size * 0.28, size * 0.5, size * 0.72]
    for y in ys:
        d.line([(pad, y), (size - pad, y)], fill=color, width=stroke)
    return im


def _icon_home(size: int, color) -> Image.Image:
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    stroke = max(2, size // 12)
    cx = size / 2
    roof_top = (cx, size * 0.12)
    roof_l = (size * 0.14, size * 0.5)
    roof_r = (size * 0.86, size * 0.5)
    d.line([roof_l, roof_top], fill=color, width=stroke, joint="curve")
    d.line([roof_top, roof_r], fill=color, width=stroke, joint="curve")
    body = [size * 0.24, size * 0.48, size * 0.76, size * 0.88]
    d.line([(body[0], body[1]), (body[0], body[3])], fill=color, width=stroke)
    d.line([(body[2], body[1]), (body[2], body[3])], fill=color, width=stroke)
    d.line([(body[0], body[3]), (body[2], body[3])], fill=color, width=stroke)
    door_w = size * 0.16
    d.rectangle([cx - door_w / 2, size * 0.62, cx + door_w / 2, body[3]], outline=color, width=max(2, stroke - 1))
    return im


def _icon_star(size: int, color) -> Image.Image:
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    stroke = max(2, size // 12)
    cx, cy = size / 2, size / 2
    outer, inner = size * 0.46, size * 0.19
    pts = []
    for i in range(10):
        ang = -math.pi / 2 + i * math.pi / 5
        r = outer if i % 2 == 0 else inner
        pts.append((cx + r * math.cos(ang), cy + r * math.sin(ang)))
    d.line(pts + [pts[0]], fill=color, width=stroke, joint="curve")
    return im


def _icon_mylist(size: int, color) -> Image.Image:
    """Bookmark-style icon for 'My List'."""
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    stroke = max(2, size // 12)
    left = size * 0.24
    right = size * 0.76
    top = size * 0.12
    bottom = size * 0.88
    notch = size * 0.5
    pts = [
        (left, top), (right, top), (right, bottom),
        (notch, bottom - size * 0.18), (left, bottom), (left, top),
    ]
    d.line(pts, fill=color, width=stroke, joint="curve")
    return im



# ---- liquid-glass panel -------------------------------------------------

def _glass_panel(canvas: Image.Image, box, radius: int = 12) -> None:
    """
    Frosted "liquid glass" card painted straight onto `canvas` (in place):
    blurred + slightly brightened backdrop, soft white tint (lighter at the top),
    top sheen, a gradient hairline border and a soft drop shadow.
    """
    x0, y0, x1, y1 = [int(v) for v in box]
    bw, bh = x1 - x0, y1 - y0
    m = 70                                                   # margin for shadow / blur
    left, top = max(0, x0 - m), max(0, y0 - m)
    right, bottom = min(canvas.width, x1 + m), min(canvas.height, y1 + m)
    ox, oy = x0 - left, y0 - top                             # panel origin inside the crop
    region = canvas.crop((left, top, right, bottom)).convert("RGBA")
    size = region.size

    def rr_mask(inset=0, outline=0):
        big = Image.new("L", (size[0] * 3, size[1] * 3), 0)
        d = ImageDraw.Draw(big)
        d.rounded_rectangle([(ox + inset) * 3, (oy + inset) * 3, (ox + bw - inset) * 3, (oy + bh - inset) * 3],
                            radius=max(1, (radius - inset) * 3), fill=255)
        if outline:
            d.rounded_rectangle([(ox + inset + outline) * 3, (oy + inset + outline) * 3,
                                 (ox + bw - inset - outline) * 3, (oy + bh - inset - outline) * 3],
                                radius=max(1, (radius - inset - outline) * 3), fill=0)
        return big.resize(size, Image.LANCZOS)

    def vgrad(top_val, bottom_val):
        g = Image.linear_gradient("L").resize((size[0], bh)).point(lambda v: int(top_val + (bottom_val - top_val) * v / 255))
        full = Image.new("L", size, 0)
        full.paste(g, (0, oy))
        return full

    panel = rr_mask()

    # drop shadow (outside the glass only)
    shadow = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle([ox, oy + 12, ox + bw, oy + bh + 12], radius=radius,
                                             fill=(0, 0, 0, int(120 * min(1.0, GLASS_STRENGTH * 1.2))))
    shadow = shadow.filter(ImageFilter.GaussianBlur(16))

    # frosted backdrop
    g = GLASS_STRENGTH
    frost = region.filter(ImageFilter.GaussianBlur(14))
    frost = ImageEnhance.Brightness(frost).enhance(1 + 0.25 * g)
    frost = ImageEnhance.Color(frost).enhance(1 + 0.2 * g)

    out = Image.alpha_composite(region, shadow)
    out.paste(frost, (0, 0), panel)

    # white tint, stronger at the top
    tint_alpha = ImageChops.multiply(vgrad(70 * g, 45 * g), panel)
    out = Image.alpha_composite(out, Image.merge("RGBA", (*Image.new("RGB", size, (255, 255, 255)).split(), tint_alpha)))

    # top sheen (glossy highlight over the upper third)
    sheen_h = int(bh * 0.38)
    sheen = Image.linear_gradient("L").rotate(180).resize((size[0], sheen_h)).point(lambda v: int(v * 0.30 * g))
    sheen_full = Image.new("L", size, 0)
    sheen_full.paste(sheen, (0, oy))
    sheen_alpha = ImageChops.multiply(sheen_full, rr_mask(inset=2))
    out = Image.alpha_composite(out, Image.merge("RGBA", (*Image.new("RGB", size, (255, 255, 255)).split(), sheen_alpha)))

    # gradient hairline border (bright at the top-left, faint at the bottom)
    border = ImageChops.multiply(rr_mask(outline=2), vgrad(200 * g, 150 * g))
    out = Image.alpha_composite(out, Image.merge("RGBA", (*Image.new("RGB", size, (255, 255, 255)).split(), border)))
    inner = ImageChops.multiply(rr_mask(inset=3, outline=1), vgrad(60 * g, 10 * g))
    out = Image.alpha_composite(out, Image.merge("RGBA", (*Image.new("RGB", size, (255, 255, 255)).split(), inner)))

    canvas.paste(out.convert("RGB"), (left, top))

# --------------------------------------------------------------------------
# Main render
# --------------------------------------------------------------------------

def render_poster(meta: PosterMeta, out_path: str,
                   w: int = CANVAS_W, h: int = CANVAS_H) -> str:
    # ---- 1. background -----------------------------------------------
    bg_src = _download_image(meta.image_url) if meta.image_url else None
    bg = _cover_fit(bg_src, w, h) if bg_src else _placeholder_background(w, h)

    canvas = bg.convert("RGB")

    # Darken overall so white text always reads.
    dark = Image.new("RGB", (w, h), (0, 0, 0))
    canvas = Image.blend(canvas, dark, 0.28)

    # Left-to-right + bottom vignette (a soft radial/linear multiply), like
    # the reference's dark scrim behind the text column.
    overlay = Image.new("L", (w, h), 0)
    od = ImageDraw.Draw(overlay)
    for x in range(w):
        # darker on the left where text sits, fades out toward the right
        t = x / w
        alpha = int(210 * max(0, 1 - t * 1.15) ** 1.4)
        od.line([(x, 0), (x, h)], fill=alpha)
    bottom = Image.new("L", (w, h), 0)
    bd = ImageDraw.Draw(bottom)
    for y in range(h):
        t = y / h
        alpha = int(190 * max(0, (t - 0.35) / 0.65) ** 1.2) if t > 0.35 else 0
        bd.line([(0, y), (w, y)], fill=alpha)
    combined = Image.new("L", (w, h), 0)
    combined.paste(overlay, (0, 0))
    combined = Image.eval(combined, lambda p: p)
    import PIL.ImageChops as ImageChops
    combined = ImageChops.lighter(overlay, bottom)

    black = Image.new("RGB", (w, h), (5, 5, 8))
    canvas = Image.composite(black, canvas, combined)

    draw = ImageDraw.Draw(canvas, "RGBA")

    pad_x = int(w * 0.05)

    # ---- 2. top-left brand lockup (single straight line) ----------------
    brand_fnt = font(F_DISPLAY, int(h * 0.052))
    # Tagline may contain Japanese characters (e.g. "アニメ"); Noto Sans JP
    # covers both those glyphs and plain Latin text so nothing tofu-boxes.
    tagline_fnt = font(F_NATIVE, int(h * 0.0135), weight=600)

    ty = int(h * 0.038)
    draw.text((pad_x, ty), meta.brand_top, font=brand_fnt, fill=COLOR_WHITE)
    top_w = draw.textlength(meta.brand_top, font=brand_fnt)
    draw.text((pad_x + top_w, ty), meta.brand_bottom, font=brand_fnt, fill=COLOR_RED)
    ty += int(h * 0.052 * 1.22)
    _tracked_text(draw, (pad_x, ty), meta.brand_tagline, tagline_fnt, COLOR_GRAY, tracking=2)

    # ---- 3. top-right nav: just a clean, clearly-visible hamburger icon --
    ham_size = int(h * 0.05)
    ham_x = w - pad_x - ham_size
    ham_y = int(h * 0.045)
    ham = _icon_hamburger(ham_size, COLOR_WHITE)
    canvas.paste(ham, (ham_x, ham_y), ham)

    # ---- 4+5. title / native / romaji / synopsis -------------------------
    # Everything is measured first (real glyph heights, baseline anchored) and then
    # stacked top-to-bottom, so long English / Japanese text can never overlap and the
    # block always ends above the stat strip.
    stat_y_top = int(h * 0.80)
    block_bottom = stat_y_top - int(h * 0.035)      # synopsis must end above this
    block_top_min = int(h * 0.17)                   # ...and start below the brand lockup
    block_top_pref = int(h * 0.42)                  # where the original design starts
    max_w = int(w * 0.56)

    def _fit_single_line(text, fname, base_size, max_width, min_size, weight=None):
        """Shrink one line until it fits; as a last resort cut it with an ellipsis."""
        size = base_size
        f = font(fname, size, weight)
        while draw.textlength(text, font=f) > max_width and size > min_size:
            size -= 1
            f = font(fname, size, weight)
        if draw.textlength(text, font=f) > max_width:
            while len(text) > 1 and draw.textlength(text + "…", font=f) > max_width:
                text = text[:-1]
            text += "…"
        return text, f

    title_text = meta.title_english.upper() if TITLE_UPPER else meta.title_english

    def _tlen(text, fnt):                           # width of tracked title text
        return draw.textlength(text, font=fnt) + fnt.size * TITLE_TRACKING * max(0, len(text) - 1)

    def _wrap_title(text, fnt, max_width):
        lines, cur = [], ""
        for word in text.split():
            trial = (cur + " " + word).strip()
            if _tlen(trial, fnt) <= max_width or not cur:
                cur = trial
            else:
                lines.append(cur)
                cur = word
        if cur:
            lines.append(cur)
        return lines

    def _asc_desc(fnt, text):
        bb = fnt.getbbox(text, anchor="ls")
        return -bb[1], bb[3]                        # pixels above / below the baseline

    plan = None
    for syn_max in (3, 2):
        for scale in (1.0, 0.92, 0.85, 0.78, 0.70, 0.62):
            # English title: at most 2 lines, shrink if it needs more
            t_size = int(h * 0.075 * TITLE_SCALE * scale)
            t_min = int(h * 0.04)
            while True:
                t_fnt = font(F_TITLE, t_size, TITLE_WEIGHT)
                t_lines = _wrap_title(title_text, t_fnt, max_w)
                if len(t_lines) <= 2 or t_size <= t_min:
                    break
                t_size -= 2
            if len(t_lines) > 2:                    # still too long -> cut with ellipsis
                t_lines = t_lines[:2]
                while _tlen(t_lines[1] + "…", t_fnt) > max_w and " " in t_lines[1]:
                    t_lines[1] = t_lines[1].rsplit(" ", 1)[0]
                t_lines[1] += "…"
            t_asc, t_desc = _asc_desc(t_fnt, "HAEO")          # capitals only
            t_gap = int(t_size * 0.24)
            t_pitch = t_asc + t_desc + t_gap
            title_h = t_asc + (len(t_lines) - 1) * t_pitch + t_desc

            native = romaji = None
            native_h = romaji_h = 0
            if meta.title_native:
                native, n_fnt = _fit_single_line(meta.title_native, F_NATIVE, int(h * 0.028 * scale),
                                                 int(w * 0.62), int(h * 0.018), weight=700)
                n_asc, n_desc = _asc_desc(n_fnt, native)
                native_h = n_asc + n_desc
            if meta.title_romaji:
                romaji, r_fnt = _fit_single_line(meta.title_romaji, F_ROMAJI, int(h * 0.024 * ROMAJI_SCALE * scale),
                                                 int(w * 0.62), int(h * 0.016), weight=ROMAJI_WEIGHT)
                r_asc, r_desc = _asc_desc(r_fnt, romaji)
                romaji_h = r_asc + r_desc

            syn_label_fnt = font(F_BODY_BOLD, int(h * 0.0155))
            glass_pad_x = 0                                  # no glass card any more -> text sits on the poster
            glass_pad_y = 0
            div_h = int(h * 0.034)                           # divider line + breathing room above SYNOPSIS
            syn_body_fnt = font(F_SYNOPSIS, int(h * 0.021 * SYN_SCALE * max(scale, 0.85)), weight=SYN_WEIGHT)
            syn_lines = _truncate_paragraph(draw, meta.synopsis, syn_body_fnt, max_w - 2 * glass_pad_x,
                                            max_lines=syn_max)
            syn_pitch = int(h * 0.034 * max(scale, 0.85))
            syn_h = div_h + int(h * 0.036) + (len(syn_lines) - 1) * syn_pitch + int(h * 0.026)

            gap_title = int(h * 0.032)
            gap_native = int(h * 0.016)
            gap_syn = int(h * 0.030)
            total = (title_h + (gap_title + native_h if native else 0)
                     + (gap_native + romaji_h if romaji else 0) + gap_syn + syn_h)
            top = min(block_top_pref, block_bottom - total)
            plan = dict(top=top, t_fnt=t_fnt, t_lines=t_lines, t_asc=t_asc, t_pitch=t_pitch, title_h=title_h,
                        native=native, native_fnt=n_fnt if native else None, n_asc=n_asc if native else 0,
                        native_h=native_h, romaji=romaji, romaji_fnt=r_fnt if romaji else None,
                        r_asc=r_asc if romaji else 0, romaji_h=romaji_h, gap_title=gap_title,
                        gap_native=gap_native, gap_syn=gap_syn, syn_label_fnt=syn_label_fnt,
                        syn_body_fnt=syn_body_fnt, syn_lines=syn_lines, syn_pitch=syn_pitch, syn_h=syn_h,
                        glass_pad_x=glass_pad_x, glass_pad_y=glass_pad_y, div_h=div_h, max_w=max_w)
            if top >= block_top_min:
                break
        else:
            continue
        break

    p = plan
    y = p["top"]
    # title: draw into a mask, add a soft shadow, then fill with a vertical gradient
    tmask = Image.new("L", (w, h), 0)
    tmd = ImageDraw.Draw(tmask)
    tf = p["t_fnt"]
    for i, line in enumerate(p["t_lines"]):
        cx_, base = pad_x, y + p["t_asc"] + i * p["t_pitch"]
        for ch in line:
            tmd.text((cx_, base), ch, font=tf, fill=255, anchor="ls")
            cx_ += tmd.textlength(ch, font=tf) + tf.size * TITLE_TRACKING
    shadow = tmask.filter(ImageFilter.GaussianBlur(max(2, int(h * 0.004)))).point(lambda v: int(v * 0.85))
    canvas.paste((0, 0, 0), (int(h * 0.0015), int(h * 0.003)), shadow)
    bb = tmask.getbbox()
    if bb:
        gw, gh = bb[2] - bb[0], bb[3] - bb[1]
        col = Image.new("RGB", (1, gh))
        px = col.load()
        for yy in range(gh):
            k = yy / max(1, gh - 1)
            px[0, yy] = tuple(int(c0 + (c1 - c0) * k) for c0, c1 in zip(*TITLE_GRAD))
        canvas.paste(col.resize((gw, gh)), (bb[0], bb[1]), tmask.crop(bb))
    y += p["title_h"]
    if p["native"]:
        y += p["gap_title"]
        draw.text((pad_x, y + p["n_asc"]), p["native"], font=p["native_fnt"], fill=COLOR_RED, anchor="ls")
        y += p["native_h"]
    if p["romaji"]:
        y += p["gap_native"]
        draw.text((pad_x, y + p["r_asc"]), p["romaji"], font=p["romaji_fnt"], fill=COLOR_OFFWHITE, anchor="ls")
        y += p["romaji_h"]

    # divider line between the heading block and the synopsis (short red accent + faint full-width rule)
    box_top = y + p["gap_syn"]
    draw.line([(pad_x, box_top), (pad_x + p["max_w"], box_top)], fill=(255, 255, 255, 60), width=1)
    draw.line([(pad_x, box_top), (pad_x + int(w * 0.05), box_top)], fill=COLOR_RED, width=3)

    # synopsis: plain text on the poster (no panel)
    tx = pad_x
    syn_y = box_top + p["div_h"]
    _tracked_text(draw, (tx, syn_y), "SYNOPSIS", p["syn_label_fnt"], COLOR_RED_LIGHT, tracking=3)
    ly2 = syn_y + int(h * 0.036)
    for line in p["syn_lines"]:
        draw.text((tx, ly2), line, font=p["syn_body_fnt"], fill=(244, 245, 247))
        ly2 += p["syn_pitch"]

    # ---- 6. stat strip (RATING / YEAR / EPISODES / STUDIO) ---------------
    stats = [
        ("RATING", meta.rating),
        ("YEAR", meta.year),
        ("EPISODES", meta.episodes),
        ("STUDIO", meta.studio),
    ]
    stat_label_fnt = font(F_BODY_MED, int(h * 0.012))
    stat_value_fnt = font(F_DISPLAY, int(h * 0.026))

    stat_y = int(h * 0.80)
    col_widths = [int(w * 0.10), int(w * 0.09), int(w * 0.115), int(w * 0.16)]
    cx = pad_x
    for (label, value), col_w in zip(stats, col_widths):
        _tracked_text(draw, (cx, stat_y), label, stat_label_fnt, COLOR_GRAY, tracking=1)
        draw.text((cx, stat_y + int(h * 0.022)), value, font=stat_value_fnt, fill=COLOR_WHITE)
        divider_x = cx + col_w
        draw.line([(divider_x, stat_y - int(h * 0.006)), (divider_x, stat_y + int(h * 0.065))],
                   fill=(120, 40, 45), width=2)
        cx = divider_x + int(w * 0.025)

    # ---- 7. genres pill -----------------------------------------------
    genre_y = int(h * 0.905)
    genre_label_fnt = font(F_BODY_MED, int(h * 0.012))
    genre_value_fnt = font(F_BODY_BOLD, int(h * 0.019))

    text_w = max(
        draw.textlength(meta.genres, font=genre_value_fnt),
        _tracked_text_width(draw, "GENRES", genre_label_fnt, 1),
    )
    gx = pad_x                                   # plain text, left-aligned with everything else (no box)
    _tracked_text(draw, (gx, genre_y + int(h * 0.012)),
                  "GENRES", genre_label_fnt, COLOR_GRAY, tracking=1)
    draw.text((gx, genre_y + int(h * 0.034)), meta.genres,
               font=genre_value_fnt, fill=COLOR_WHITE)

    # ---- 8. thin brand accent bar (top hairline, like the ref crop) -----
    draw.line([(0, 2), (w, 2)], fill=(255, 255, 255, 18), width=2)

    canvas.save(out_path, quality=95)
    return out_path
