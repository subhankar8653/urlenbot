"""
thumb_style.py
================
Auto-generated thumbnails ke bottom-band ("@SBANIME" wala red box) ke
liye 10 professional style presets + ek "disable" option + custom color
override — sab GLOBAL (bot-wide), community branding ki tarah.

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
# kind:
#   "band"      -> solid-color full-width bottom band
#   "gradient"  -> top->bottom gradient full-width bottom band
#   "bordered"  -> solid band + thin accent border line on top edge
#   "glass"     -> semi-transparent (alpha-blended) band
#   "ribbon"    -> small diagonal corner ribbon (top-right), not full-width
#   "outline"   -> no box at all, sirf outlined/shadowed text
#
# color override (custom color pick) primary target:
#   band/gradient/bordered/glass/ribbon -> bg_color badalta hai
#   outline                              -> text_color badalta hai

STYLES = {
    "classic_red": {
        "name": "🔴 Classic Red",
        "kind": "band",
        "bg_color": (220, 20, 20),
        "text_color": (255, 255, 255),
    },
    "midnight_black": {
        "name": "⚫ Midnight Black",
        "kind": "band",
        "bg_color": (18, 18, 18),
        "text_color": (255, 215, 0),
    },
    "royal_blue": {
        "name": "🔵 Royal Blue",
        "kind": "band",
        "bg_color": (25, 55, 180),
        "text_color": (255, 255, 255),
    },
    "emerald_green": {
        "name": "🟢 Emerald Green",
        "kind": "band",
        "bg_color": (14, 120, 70),
        "text_color": (255, 255, 255),
    },
    "sunset_gradient": {
        "name": "🌅 Sunset Gradient",
        "kind": "gradient",
        "bg_color": (255, 140, 0),
        "bg_color2": (200, 20, 20),
        "text_color": (255, 255, 255),
    },
    "neon_purple": {
        "name": "🟣 Neon Purple",
        "kind": "bordered",
        "bg_color": (110, 25, 170),
        "text_color": (255, 255, 255),
        "border_color": (0, 230, 255),
    },
    "golden_luxury": {
        "name": "✨ Golden Luxury",
        "kind": "bordered",
        "bg_color": (15, 15, 15),
        "text_color": (255, 205, 60),
        "border_color": (255, 205, 60),
    },
    "dark_glass": {
        "name": "🖤 Dark Glass",
        "kind": "glass",
        "bg_color": (10, 10, 10),
        "text_color": (255, 255, 255),
        "alpha": 175,
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
    "classic_red", "midnight_black", "royal_blue", "emerald_green",
    "sunset_gradient", "neon_purple", "golden_luxury", "dark_glass",
    "corner_ribbon", "minimal_outline",
]

DEFAULT_STYLE_ID = "classic_red"

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
            # gradient ka 2nd stop bhi thoda dark shift kar do taaki gradient banaa rahe
            if style["kind"] == "gradient":
                style["bg_color2"] = tuple(max(0, c - 70) for c in rgb)
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


def render_band(img, text: str, style: dict):
    """
    PIL Image (RGB) pe diya gaya style draw karta hai — in-place modify
    karke wapas img return karta hai. style=None -> kuch nahi karta
    (disabled).
    """
    if style is None:
        return img

    from PIL import Image, ImageDraw

    w, h = img.size
    kind = style["kind"]

    if kind == "ribbon":
        return _render_ribbon(img, text, style)

    if kind == "outline":
        draw = ImageDraw.Draw(img)
        font = _get_font(max(int(h * 0.05), 22))
        bbox = draw.textbbox((0, 0), text, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        tx = (w - tw) / 2 - bbox[0]
        ty = h - th - int(h * 0.05) - bbox[1]
        draw.text(
            (tx, ty), text, font=font, fill=style["text_color"],
            stroke_width=max(2, int(h * 0.006)), stroke_fill=style["outline_color"],
        )
        return img

    band_h = max(int(h * 0.09), 34)
    font = _get_font(int(band_h * 0.55))

    if kind == "gradient":
        band = Image.new("RGB", (w, band_h))
        c1, c2 = style["bg_color"], style["bg_color2"]
        for row in range(band_h):
            t = row / max(band_h - 1, 1)
            color = tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3))
            for x in range(w):
                band.putpixel((x, row), color)
        img.paste(band, (0, h - band_h))
        draw = ImageDraw.Draw(img)
    elif kind == "glass":
        overlay = Image.new("RGBA", (w, band_h), (*style["bg_color"], style.get("alpha", 175)))
        base = img.convert("RGBA")
        base.paste(Image.alpha_composite(
            base.crop((0, h - band_h, w, h)).convert("RGBA"), overlay
        ), (0, h - band_h))
        img.paste(base.convert("RGB"))
        draw = ImageDraw.Draw(img)
    else:
        draw = ImageDraw.Draw(img)
        draw.rectangle([0, h - band_h, w, h], fill=style["bg_color"])
        if kind == "bordered":
            border_h = max(int(band_h * 0.08), 3)
            draw.rectangle([0, h - band_h, w, h - band_h + border_h], fill=style["border_color"])

    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    tx = (w - tw) / 2 - bbox[0]
    ty = h - band_h + (band_h - th) / 2 - bbox[1]
    draw.text((tx, ty), text, font=font, fill=style["text_color"])
    return img


def _render_ribbon(img, text: str, style: dict):
    from PIL import Image, ImageDraw

    w, h = img.size
    strip_w = max(int(w * 0.55), 220)
    strip_h = max(int(h * 0.10), 36)

    strip = Image.new("RGBA", (strip_w, strip_h), (0, 0, 0, 0))
    d = ImageDraw.Draw(strip)
    d.rectangle([0, 0, strip_w, strip_h], fill=(*style["bg_color"], 255))
    font = _get_font(int(strip_h * 0.55))
    bbox = d.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    d.text(
        ((strip_w - tw) / 2 - bbox[0], (strip_h - th) / 2 - bbox[1]),
        text, font=font, fill=style["text_color"],
    )

    rotated = strip.rotate(-32, expand=True, resample=Image.BICUBIC)
    px, py = w - rotated.width + int(rotated.width * 0.18), -int(rotated.height * 0.18)
    img.paste(rotated, (px, py), rotated)
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
