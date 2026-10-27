from __future__ import annotations

import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from . import config

# emoji / pictographs / variation selectors: a poster font cannot draw them
_NOISE = re.compile("[\U0001F000-\U0001FFFF\u2600-\u27BF\u2B00-\u2BFF\uFE00-\uFE0F\u200B-\u200F\u202A-\u202E\u2060\U000E0000-\U000E007F]")

LIMIT = 24                       # longest branding text


def clean(name: str | None, limit: int = LIMIT) -> str:
    """printable branding text for a poster ('' when nothing is left)"""
    name = _NOISE.sub("", name or "")
    name = re.sub(r"\s+", " ", name).strip()
    return name[:limit].strip()


def _word_cut(words: list[str]) -> int:
    """index that splits the words into two halves of about the same length"""
    total, best, cut = len(" ".join(words)), 10 ** 9, 1
    for i in range(1, len(words)):
        run = len(" ".join(words[:i]))
        if abs(run - total / 2) < best:
            best, cut = abs(run - total / 2), i
    return cut


def split_brand(name: str) -> tuple[str, str]:
    """'Anime World Zone' -> ('ANIME', 'WORLD ZONE') - two halves of about the same length, 12 letters each at most
    (the lockup of styles 8 and 13)"""
    words = clean(name, LIMIT).upper().split()
    if not words:
        return " ", " "
    if len(words) == 1:
        w = words[0]
        cut = max(1, len(w) // 2)
        return w[:cut][:12], (w[cut:] or " ")[:12]
    cut = _word_cut(words)
    return " ".join(words[:cut])[:11] + " ", " ".join(words[cut:])[:12]     # the lockup glues the halves: keep the space


def _halves(name: str, upper: bool) -> tuple[str, str]:
    """two-tone split: between words when there are several, in the middle of a single word (like ANI|CROX)"""
    name = clean(name)
    words = (name.upper() if upper else name).split()
    if len(words) >= 2:
        cut = _word_cut(words)
        return " ".join(words[:cut]), " ".join(words[cut:])
    w = words[0] if words else ""
    cut = max(1, len(w) // 2) if len(w) > 3 else len(w)
    return w[:cut], w[cut:]


# --------------------------------------------------------------------------- the look of each poster
# at = where the text starts (native pixels of that poster; the logo spot)  ·  align: "l" or "r" (grows to the left)
# size / track = font size and letter spacing in px  ·  ink / accent = that poster's own colours
SPECS: dict[int, dict] = {
    1: dict(font="Poppins-Bold.ttf", size=38, track=3.0, case="upper", at=(132, 88), ink=(85, 85, 85), accent=(224, 124, 134), maxw=700, ref=1920),
    2: dict(font="Poppins-Bold.ttf", size=32, track=3.0, case="upper", at=(114, 65), ink=(85, 85, 85), accent=(176, 80, 160), maxw=700, ref=1920),
    3: dict(font="Poppins-Bold.ttf", size=32, track=3.0, case="upper", at=(268, 65), ink=(85, 85, 85), accent=(220, 78, 130), maxw=560, ref=1920),
    4: dict(font="LilitaOne-Regular.ttf", size=36, track=1.5, case="upper", at=(114, 65), ink=(40, 41, 52), accent=(232, 38, 66), maxw=290, ref=1920),
    5: dict(font="LilitaOne-Regular.ttf", size=38, track=1.5, case="upper", at=(830, 172), ink=(40, 41, 52), accent=(158, 75, 87), maxw=620, ref=1920),
    7: dict(font="LilitaOne-Regular.ttf", size=36, track=1.5, case="upper", at=(114, 65), ink=(40, 41, 52), accent=(238, 160, 0), maxw=700, ref=1920),
    9: dict(font="BebasNeue-Regular.ttf", size=60, track=5.0, case="upper", at=(158, 101), ink=(255, 255, 255), accent=(229, 9, 20), maxw=700, ref=1920),
    10: dict(font="Anton-Regular.ttf", size=46, track=3.0, case="upper", at=(1860, 62), align="r", ink=(14, 18, 58), accent=(240, 72, 120), maxw=380, ref=1920),
    11: dict(font="Poppins-Bold.ttf", size=26, track=2.0, case="upper", at=(126, 43), ink=(42, 43, 56), accent=(232, 32, 63), maxw=500, ref=1280),
    12: dict(font="Kanit-ExtraBold.ttf", size=44, track=3.0, case="upper", at=(72, 78), ink=(255, 255, 255), accent=(150, 120, 255), maxw=700, ref=1920),
    14: dict(font="Lora-Italic-Variable.ttf", size=34, track=0.6, case="keep", at=(640, 668), ink=(17, 55, 69), accent=(178, 96, 70), maxw=420, ref=1280),
}
OWN_BRAND = (6, 8, 13)           # these three draw the branding themselves (template header / lockup)


def _font_path(name: str) -> Path | None:
    for d in (config.FONT_DIR, config.BASE_DIR / "Templates" / "fonts"):
        if (Path(d) / name).exists():
            return Path(d) / name
    return None


def _font(spec: dict, text: str, size: int):
    latin = all(ord(ch) < 0x250 for ch in text)
    names = [spec["font"]] if latin else ["NotoSansJP-Variable.ttf"]
    names += ["Poppins-Bold.ttf", "NotoSansJP-Variable.ttf"]
    for n in names:
        p = _font_path(n)
        if p is None:
            continue
        try:
            f = ImageFont.truetype(str(p), size)
        except OSError:
            continue
        if "Variable" in n:                             # variable font: ask for the bold weight
            try:
                f.set_variation_by_axes([700])
            except Exception:
                pass
        return f
    try:
        return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", size)
    except OSError:
        return ImageFont.load_default(size)


def _run(draw, font, text, x, y, fill, track, measure=False) -> float:
    """text with letter spacing, left aligned on the middle line; returns the x after the last letter"""
    for ch in text:
        if not measure:
            draw.text((x, y), ch, font=font, fill=fill, anchor="lm")
        x += draw.textlength(ch, font=font) + track
    return x


def draw_brand(img: Image.Image, name: str, style_id: int) -> Image.Image:
    """Write the branding in this poster's own typography, colours and logo spot. Styles without a spec are left alone."""
    spec = SPECS.get(style_id)
    name = clean(name)
    if not spec or not name:
        return img
    base = img.convert("RGBA")
    k = base.width / spec["ref"]                         # the specs are written for the template's native size
    size = max(12, round(spec["size"] * k))
    track = spec["track"] * k
    upper = spec["case"] == "upper"
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    maxw = spec["maxw"] * k
    floor = max(14, round(size * 0.65))                    # never smaller than this: still readable

    def parts_of(text):
        a, b = _halves(text, upper)
        return a, (" " if (" " in text.strip() and b) else ""), b

    def width(parts, f) -> float:
        return sum(_run(d, f, t, 0, 0, None, track, measure=True) for t in parts)

    font = _font(spec, name, size)
    a, joiner, b = parts_of(name)
    while size > floor and width([a, joiner, b], font) > maxw:      # long name -> a bit smaller ...
        size -= 2
        font = _font(spec, name, size)
    while len(name) > 4 and width([a, joiner, b], font) > maxw:     # ... then shortened with an ellipsis, never over the artwork
        core = name[:-3] if name.endswith("...") else name          # "..." not "…": poster fonts often lack that glyph
        name = core[:-2].rstrip() + "..."
        a, joiner, b = parts_of(name)
    total = width([a, joiner, b], font)
    x, y = spec["at"][0] * k, spec["at"][1] * k
    if spec.get("align") == "r":
        x -= total
    x = _run(d, font, a, x, y, spec["ink"] + (255,), track)
    x = _run(d, font, joiner, x, y, spec["ink"] + (255,), track)
    _run(d, font, b, x, y, spec["accent"] + (255,), track)
    return Image.alpha_composite(base, layer).convert("RGB")


def text_width(font_name: str, size: int, text: str) -> float:
    """pixel width of text in one of the poster fonts (used to see whether a brand still fits a template's header)"""
    path = _font_path(font_name)
    if path is None:
        return len(text) * size * 0.62
    return ImageFont.truetype(str(path), size).getlength(text)
