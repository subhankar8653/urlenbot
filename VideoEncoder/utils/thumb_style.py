"""
thumb_style.py
================
Auto-generated thumbnails ke bottom-band ("@SBANIME" wala red box) ke
liye 10 STRUCTURALLY ALAG professional layouts + ek "disable" option +
custom color override — sab GLOBAL (bot-wide), community branding ki
tarah. Har style ka SHAPE/POSITION alag hai (sirf color badal ke naya
style nahi banaya gaya) — full band, corner tag, floating pill, bracket
frame, angled cut, side tab, ribbon, cinematic fade, glass pill, outline.

/setpic_style command (plugins/setpic_style.py) se style select/preview/
apply/disable hota hai. Yeh module sirf engine hai:
  - STYLES        -> 10 preset definitions
  - render_band() -> kisi bhi image pe koi bhi style draw karna
  - generate_preview() -> sample card banake style ka preview dena
  - load_style_cache() / get_current_style() -> DB-backed, in-memory
    cache (taaki har thumbnail banate waqt DB hit na karni pade — bot
    start pe ek baar load hota hai, /setpic_style se change hone par
    turant refresh ho jaata hai)
"""

import logging
import os
import time

LOGGER = logging.getLogger(__name__)

DEFAULT_THUMB_BAND_TEXT = "@SBANIME"

# ─────────────────────────────────────────────
#  10 Style Presets + "none" (disable)
# ─────────────────────────────────────────────
# kind (har ek ka layout/shape/position alag hai):
#   "band"       -> classic full-width solid bottom band
#   "fade"       -> koi box nahi, bottom pe smooth dark-to-transparent
#                   cinematic gradient fade, text seedha uspe (Netflix/
#                   movie-poster style)
#   "pill"       -> chhota floating rounded "badge" bottom-center mein,
#                   edges ko touch nahi karta
#   "corner_tag" -> chhota rounded box TOP-LEFT corner mein (channel-bug
#                   jaisa, bottom band nahi)
#   "bracket"    -> 4 corners pe viewfinder-style L brackets + chhota
#                   text-pill niche center mein — minimal cinematic
#   "glass_pill" -> translucent (glass) rounded pill BOTTOM-RIGHT corner
#   "angled"     -> bottom band, lekin seedhi rectangle nahi — slanted/
#                   diagonal-cut top edge (parallelogram silhouette)
#   "side_tab"   -> LEFT edge pe vertical strip, text rotated (top se
#                   bottom padhte hue)
#   "ribbon"     -> diagonal corner ribbon (top-right), full-width nahi
#   "outline"    -> koi box nahi, sirf outlined/shadowed text
#
# color override (custom color pick) primary target:
#   sabhi filled shapes (band/pill/corner_tag/bracket/glass_pill/
#   angled/side_tab/ribbon)  -> bg_color/accent badalta hai
#   fade                     -> gradient tint color badalta hai
#   outline                  -> text_color badalta hai

STYLES = {
    "classic_band": {
        "name": "🔴 Classic Band",
        "kind": "band",
        "bg_color": (220, 20, 20),
        "text_color": (255, 255, 255),
    },
    "cinematic_fade": {
        "name": "🌆 Cinematic Fade",
        "kind": "fade",
        "bg_color": (0, 0, 0),
        "text_color": (255, 255, 255),
    },
    "center_pill": {
        "name": "💊 Center Pill Badge",
        "kind": "pill",
        "bg_color": (25, 55, 180),
        "text_color": (255, 255, 255),
    },
    "corner_tag": {
        "name": "🏷️ Corner Tag",
        "kind": "corner_tag",
        "bg_color": (14, 120, 70),
        "text_color": (255, 255, 255),
    },
    "bracket_frame": {
        "name": "📐 Bracket Frame",
        "kind": "bracket",
        "bg_color": (255, 205, 60),
        "text_color": (20, 20, 20),
    },
    "glass_pill": {
        "name": "🪟 Glass Pill",
        "kind": "glass_pill",
        "bg_color": (10, 10, 10),
        "text_color": (255, 255, 255),
        "alpha": 180,
    },
    "angled_band": {
        "name": "🔻 Angled Cut Band",
        "kind": "angled",
        "bg_color": (110, 25, 170),
        "text_color": (255, 255, 255),
    },
    "side_tab": {
        "name": "📎 Side Tab",
        "kind": "side_tab",
        "bg_color": (200, 20, 20),
        "text_color": (255, 255, 255),
    },
    "corner_ribbon": {
        "name": "🎗️ Corner Ribbon",
        "kind": "ribbon",
        "bg_color": (220, 20, 20),
        "text_color": (255, 255, 255),
    },
    "minimal_outline": {
        "name": "🪶 Minimal Outline",
        "kind": "outline",
        "text_color": (255, 255, 255),
        "outline_color": (0, 0, 0),
    },
}

STYLE_ORDER = [
    "classic_band", "cinematic_fade", "center_pill", "corner_tag",
    "bracket_frame", "glass_pill", "angled_band", "side_tab",
    "corner_ribbon", "minimal_outline",
]

DEFAULT_STYLE_ID = "classic_band"

# Color-picker preset swatches (color override) — (label, hex)
COLOR_PRESETS = [
    ("🔴 Red", "#DC1414"),
    ("⚫ Black", "#121212"),
    ("🔵 Blue", "#1937B4"),
    ("🟢 Green", "#0E7846"),
    ("🟣 Purple", "#6E19AA"),
    ("🟠 Orange", "#FF8C00"),
    ("🟡 Gold", "#FFCD3C"),
    ("🩷 Pink", "#E4318C"),
    ("⚪ White", "#F2F2F2"),
    ("🩵 Cyan", "#00C8D9"),
]


def hex_to_rgb(hex_color: str):
    h = (hex_color or "").strip().lstrip("#")
    if len(h) != 6:
        return None
    try:
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return None


# ─────────────────────────────────────────────
#  In-memory cache (DB-backed) — thumbnail generation is hot-path,
#  isliye har frame pe DB call nahi karte.
# ─────────────────────────────────────────────
_cache_style_id = DEFAULT_STYLE_ID
_cache_color = None  # hex string ya None (style ka default color)


async def load_style_cache():
    """Bot startup pe ek baar call karo — DB se current style/color load karta hai."""
    global _cache_style_id, _cache_color
    try:
        from .database.access_db import db
        style_id = await db.get_thumb_style()
        color = await db.get_thumb_color()
        _cache_style_id = style_id or DEFAULT_STYLE_ID
        _cache_color = color
        LOGGER.info(f"[ThumbStyle] Loaded: style={_cache_style_id} color={_cache_color}")
    except Exception as e:
        LOGGER.warning(f"[ThumbStyle] Cache load failed, using default: {e}")


async def set_style(style_id: str):
    global _cache_style_id
    from .database.access_db import db
    await db.set_thumb_style(style_id)
    _cache_style_id = style_id


async def set_color(hex_color):
    global _cache_color
    from .database.access_db import db
    await db.set_thumb_color(hex_color)
    _cache_color = hex_color


def get_current_style_id() -> str:
    return _cache_style_id


def get_current_color():
    return _cache_color


def get_current_style() -> dict:
    """(style_id, color_hex) resolved into a ready-to-draw style dict, ya None agar 'none'/disabled hai."""
    if _cache_style_id == "none":
        return None
    return resolve_style(_cache_style_id, _cache_color)


def resolve_style(style_id: str, color_hex=None) -> dict:
    """STYLES preset + optional color override ko ek final dict mein merge karta hai."""
    if style_id == "none":
        return None
    preset = STYLES.get(style_id, STYLES[DEFAULT_STYLE_ID])
    style = dict(preset)
    style["id"] = style_id
    rgb = hex_to_rgb(color_hex) if color_hex else None
    if rgb:
        if style["kind"] == "outline":
            style["text_color"] = rgb
        else:
            style["bg_color"] = rgb
    return style


# ─────────────────────────────────────────────
#  Rendering
# ─────────────────────────────────────────────
def _get_font(size: int):
    from PIL import ImageFont
    for fp in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ):
        if os.path.isfile(fp):
            return ImageFont.truetype(fp, size)
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _text_size(draw, text, font):
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1], bbox


def render_band(img, text: str, style: dict):
    """
    PIL Image (RGB) pe diya gaya style draw karta hai — in-place modify
    karke wapas img return karta hai. style=None -> kuch nahi karta
    (disabled).
    """
    if style is None:
        return img

    kind = style["kind"]
    dispatch = {
        "band": _render_band,
        "fade": _render_fade,
        "pill": _render_pill,
        "corner_tag": _render_corner_tag,
        "bracket": _render_bracket,
        "glass_pill": _render_glass_pill,
        "angled": _render_angled_band,
        "side_tab": _render_side_tab,
        "ribbon": _render_ribbon,
        "outline": _render_outline,
    }
    fn = dispatch.get(kind, _render_band)
    return fn(img, text, style)


def _render_band(img, text, style):
    from PIL import ImageDraw
    w, h = img.size
    band_h = max(int(h * 0.09), 34)
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, h - band_h, w, h], fill=style["bg_color"])
    font = _get_font(int(band_h * 0.55))
    tw, th, bbox = _text_size(draw, text, font)
    tx = (w - tw) / 2 - bbox[0]
    ty = h - band_h + (band_h - th) / 2 - bbox[1]
    draw.text((tx, ty), text, font=font, fill=style["text_color"])
    return img


def _render_fade(img, text, style):
    """Bottom se upar ki taraf transparent->tint-color gradient (cinematic poster look), text bina box ke."""
    from PIL import Image, ImageDraw
    w, h = img.size
    fade_h = max(int(h * 0.32), 90)
    tint = style["bg_color"]

    overlay = Image.new("RGBA", (w, fade_h), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    for row in range(fade_h):
        t = row / max(fade_h - 1, 1)  # 0 top -> 1 bottom
        alpha = int(200 * (t ** 1.4))
        od.line([(0, row), (w, row)], fill=(*tint, alpha))

    base = img.convert("RGBA")
    base.alpha_composite(overlay, (0, h - fade_h))
    img.paste(base.convert("RGB"))

    draw = ImageDraw.Draw(img)
    font = _get_font(max(int(h * 0.05), 24))
    tw, th, bbox = _text_size(draw, text, font)
    tx = (w - tw) / 2 - bbox[0]
    ty = h - th - int(h * 0.045) - bbox[1]
    # soft shadow for readability on top of fade
    draw.text((tx + 2, ty + 2), text, font=font, fill=(0, 0, 0))
    draw.text((tx, ty), text, font=font, fill=style["text_color"])
    return img


def _render_pill(img, text, style):
    """Floating rounded badge, bottom-center, edges ko touch nahi karta."""
    from PIL import ImageDraw
    w, h = img.size
    draw = ImageDraw.Draw(img)
    font = _get_font(max(int(h * 0.045), 20))
    tw, th, bbox = _text_size(draw, text, font)

    pad_x, pad_y = int(th * 0.9), int(th * 0.55)
    pill_w, pill_h = tw + pad_x * 2, th + pad_y * 2
    cx = w / 2
    bottom_margin = int(h * 0.06)
    y1 = h - bottom_margin - pill_h
    x1 = cx - pill_w / 2

    draw.rounded_rectangle(
        [x1, y1, x1 + pill_w, y1 + pill_h], radius=pill_h / 2, fill=style["bg_color"]
    )
    tx = x1 + pad_x - bbox[0]
    ty = y1 + pad_y - bbox[1]
    draw.text((tx, ty), text, font=font, fill=style["text_color"])
    return img


def _render_corner_tag(img, text, style):
    """Chhota rounded tag TOP-LEFT corner mein (bottom band nahi)."""
    from PIL import ImageDraw
    w, h = img.size
    draw = ImageDraw.Draw(img)
    font = _get_font(max(int(h * 0.04), 18))
    tw, th, bbox = _text_size(draw, text, font)

    pad_x, pad_y = int(th * 0.7), int(th * 0.45)
    tag_w, tag_h = tw + pad_x * 2, th + pad_y * 2
    margin = int(min(w, h) * 0.035)

    draw.rounded_rectangle(
        [margin, margin, margin + tag_w, margin + tag_h],
        radius=int(tag_h * 0.28), fill=style["bg_color"],
    )
    tx = margin + pad_x - bbox[0]
    ty = margin + pad_y - bbox[1]
    draw.text((tx, ty), text, font=font, fill=style["text_color"])
    return img


def _render_bracket(img, text, style):
    """4 corners pe viewfinder-style L brackets + chhota text-pill bottom-center."""
    from PIL import ImageDraw
    w, h = img.size
    draw = ImageDraw.Draw(img)
    accent = style["bg_color"]

    arm = int(min(w, h) * 0.07)
    thick = max(int(min(w, h) * 0.008), 3)
    m = int(min(w, h) * 0.035)

    corners = [
        ((m, m), (1, 1)),            # top-left
        ((w - m, m), (-1, 1)),       # top-right
        ((m, h - m), (1, -1)),       # bottom-left
        ((w - m, h - m), (-1, -1)),  # bottom-right
    ]
    for (cx, cy), (dx, dy) in corners:
        draw.line([(cx, cy), (cx + dx * arm, cy)], fill=accent, width=thick)
        draw.line([(cx, cy), (cx, cy + dy * arm)], fill=accent, width=thick)

    font = _get_font(max(int(h * 0.04), 18))
    tw, th, bbox = _text_size(draw, text, font)
    pad_x, pad_y = int(th * 0.7), int(th * 0.4)
    pill_w, pill_h = tw + pad_x * 2, th + pad_y * 2
    x1 = (w - pill_w) / 2
    y1 = h - m - pill_h
    draw.rounded_rectangle([x1, y1, x1 + pill_w, y1 + pill_h], radius=pill_h / 2, fill=accent)
    draw.text((x1 + pad_x - bbox[0], y1 + pad_y - bbox[1]), text, font=font, fill=style["text_color"])
    return img


def _render_glass_pill(img, text, style):
    """Translucent rounded pill, BOTTOM-RIGHT corner."""
    from PIL import Image, ImageDraw
    w, h = img.size
    tmp_draw = ImageDraw.Draw(img)
    font = _get_font(max(int(h * 0.042), 18))
    tw, th, bbox = _text_size(tmp_draw, text, font)

    pad_x, pad_y = int(th * 0.85), int(th * 0.5)
    pill_w, pill_h = tw + pad_x * 2, th + pad_y * 2
    margin = int(min(w, h) * 0.04)
    x1 = w - margin - pill_w
    y1 = h - margin - pill_h

    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.rounded_rectangle(
        [x1, y1, x1 + pill_w, y1 + pill_h], radius=pill_h / 2,
        fill=(*style["bg_color"], style.get("alpha", 180)),
    )
    od.text((x1 + pad_x - bbox[0], y1 + pad_y - bbox[1]), text, font=font, fill=style["text_color"])

    base = img.convert("RGBA")
    base.alpha_composite(overlay)
    img.paste(base.convert("RGB"))
    return img


def _render_angled_band(img, text, style):
    """Bottom band, lekin slanted/diagonal-cut top edge (parallelogram)."""
    from PIL import ImageDraw
    w, h = img.size
    band_h = max(int(h * 0.11), 40)
    slant = int(band_h * 0.7)
    draw = ImageDraw.Draw(img)
    draw.polygon(
        [(0, h - band_h + slant), (w, h - band_h), (w, h), (0, h)],
        fill=style["bg_color"],
    )
    font = _get_font(int(band_h * 0.5))
    tw, th, bbox = _text_size(draw, text, font)
    tx = (w - tw) / 2 - bbox[0]
    ty = h - band_h + slant / 2 + (band_h - slant / 2 - th) / 2 - bbox[1]
    draw.text((tx, ty), text, font=font, fill=style["text_color"])
    return img


def _render_side_tab(img, text, style):
    """LEFT edge pe vertical strip, text rotated (top-se-bottom padhte hue)."""
    from PIL import Image, ImageDraw
    w, h = img.size
    tab_w = max(int(w * 0.09), 44)
    tab_h = int(h * 0.46)
    y0 = int((h - tab_h) / 2)

    draw = ImageDraw.Draw(img)
    draw.rectangle([0, y0, tab_w, y0 + tab_h], fill=style["bg_color"])

    # Font ko shrink karte jao jab tak text (rotate se pehle horizontal
    # width) tab_h ke andar fit na ho jaaye (padding ke saath) — warna
    # lamba text tab se upar-neeche overflow ho jaata hai.
    max_text_len = tab_h - int(tab_h * 0.12)
    size = max(int(tab_w * 0.55), 20)
    tmp = Image.new("RGBA", (10, 10))
    td = ImageDraw.Draw(tmp)
    font = _get_font(size)
    tbbox = td.textbbox((0, 0), text, font=font)
    tw = tbbox[2] - tbbox[0]
    while tw > max_text_len and size > 10:
        size -= 2
        font = _get_font(size)
        tbbox = td.textbbox((0, 0), text, font=font)
        tw = tbbox[2] - tbbox[0]
    th = tbbox[3] - tbbox[1]

    txt_img = Image.new("RGBA", (tab_h, tab_w), (0, 0, 0, 0))
    td = ImageDraw.Draw(txt_img)
    td.text(((tab_h - tw) / 2 - tbbox[0], (tab_w - th) / 2 - tbbox[1]),
            text, font=font, fill=style["text_color"])
    rotated = txt_img.rotate(90, expand=True)

    rx = int((tab_w - rotated.width) / 2)
    ry = y0 + int((tab_h - rotated.height) / 2)
    img.paste(rotated, (rx, ry), rotated)
    return img


def _render_ribbon(img, text, style):
    from PIL import Image, ImageDraw

    w, h = img.size
    strip_w = max(int(w * 0.55), 220)
    strip_h = max(int(h * 0.10), 36)

    strip = Image.new("RGBA", (strip_w, strip_h), (0, 0, 0, 0))
    d = ImageDraw.Draw(strip)
    d.rectangle([0, 0, strip_w, strip_h], fill=(*style["bg_color"], 255))
    font = _get_font(int(strip_h * 0.55))
    tw, th, bbox = _text_size(d, text, font)
    d.text(
        ((strip_w - tw) / 2 - bbox[0], (strip_h - th) / 2 - bbox[1]),
        text, font=font, fill=style["text_color"],
    )

    rotated = strip.rotate(-32, expand=True, resample=Image.BICUBIC)
    px, py = w - rotated.width + int(rotated.width * 0.18), -int(rotated.height * 0.18)
    img.paste(rotated, (px, py), rotated)
    return img


def _render_outline(img, text, style):
    from PIL import ImageDraw
    w, h = img.size
    draw = ImageDraw.Draw(img)
    font = _get_font(max(int(h * 0.05), 22))
    tw, th, bbox = _text_size(draw, text, font)
    tx = (w - tw) / 2 - bbox[0]
    ty = h - th - int(h * 0.05) - bbox[1]
    draw.text(
        (tx, ty), text, font=font, fill=style["text_color"],
        stroke_width=max(2, int(h * 0.006)), stroke_fill=style["outline_color"],
    )
    return img


def generate_preview(style_id: str, color_hex=None, text: str = None, dest_dir: str = "/tmp") -> str:
    """
    Sample placeholder card (16:9) banake diye gaye style/color ka
    preview render karta hai, JPEG path return karta hai.
    """
    from PIL import Image, ImageDraw

    text = text or DEFAULT_THUMB_BAND_TEXT
    w, h = 960, 540
    img = Image.new("RGB", (w, h), (40, 42, 48))
    draw = ImageDraw.Draw(img)
    # simple placeholder "video frame" gradient background
    for row in range(h):
        t = row / (h - 1)
        c = (int(35 + 25 * t), int(38 + 20 * t), int(50 + 35 * t))
        draw.line([(0, row), (w, row)], fill=c)
    ph_font = _get_font(28)
    ph_text = "PREVIEW"
    bbox = draw.textbbox((0, 0), ph_text, font=ph_font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((w - tw) / 2, h / 2 - th), ph_text, font=ph_font, fill=(120, 122, 130))

    style = resolve_style(style_id, color_hex)
    render_band(img, text, style)

    out_path = os.path.join(dest_dir, f"style_preview_{int(time.time() * 1000)}.jpg")
    img.save(out_path, "JPEG", quality=90)
    return out_path
