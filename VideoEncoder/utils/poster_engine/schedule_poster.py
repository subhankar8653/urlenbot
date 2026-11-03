"""
schedule_poster.py
===================
Hindi anime schedule ki image (NEW style) — poster 11 jaisa design:
  • upar-left : hamburger + community username(s)  (2 username => dono, chhote karke stacked)
  • pill      : DIN · aaj ki date
  • heading   : HINDI ANIME / SCHEDULE
  • list      : har anime ka  TIME | naam | S04 • E23   (max 7 row, baaki "+N more")
  • right side: aaj ke aane wale anime mein se random ek ka CHARACTER art (fanart.tv transparent PNG;
                na mile to AniList cover card) — red block + dots ke saath

Use:
    ok = await render_schedule_to_file(items, "Friday", "03 Nov 2026", "@A @B", "/tmp/x.jpg")
    items = [{"title": "...", "ep": "S04 • E23", "time": "07:40 PM"}, ...]   (time ke hisaab se sorted)
"""
from __future__ import annotations

import asyncio
import io
import logging
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from . import chname, config

LOGGER = logging.getLogger(__name__)

SCALE = 2
WIDTH, HEIGHT = 1280, 720
MARGIN = 64
LIST_RIGHT = 700                 # list rows end here (the red art block starts at 730)
MAX_ROWS = 7

NAVY = (42, 43, 56)
RED = (232, 32, 63)
CHIP = (240, 240, 240)
CHIP_TEXT = (138, 141, 152)
BOX = (245, 245, 247)
BOX_LINE = (226, 226, 230)
PILL_FILL = (252, 228, 233)
PILL_BORDER = (246, 190, 200)
DOT_PINK = (244, 122, 142)

_SYS_FONT_DIRS = ["/usr/share/fonts/truetype/google-fonts/", "/usr/share/fonts/truetype/dejavu/",
                  str(Path("~/.fonts").expanduser()) + "/"]
_FALLBACK = {"Poppins-Bold.ttf": "DejaVuSans-Bold.ttf", "Poppins-Medium.ttf": "DejaVuSans-Bold.ttf",
             "Poppins-Regular.ttf": "DejaVuSans.ttf"}


def _font(name: str, size: float):
    px = int(size * SCALE)
    p = chname._font_path(name)
    if p is not None:
        try:
            return ImageFont.truetype(str(p), px)
        except OSError:
            pass
    for cand in (name, _FALLBACK.get(name)):
        if not cand:
            continue
        for d in _SYS_FONT_DIRS:
            fp = Path(d) / cand
            if fp.exists():
                try:
                    return ImageFont.truetype(str(fp), px)
                except OSError:
                    pass
    return ImageFont.load_default()


def sp(v) -> int:
    return int(v * SCALE)


class _Canvas:
    def __init__(self):
        self.img = Image.new("RGB", (sp(WIDTH), sp(HEIGHT)), "white")
        self.d = ImageDraw.Draw(self.img)

    def refresh(self):
        self.d = ImageDraw.Draw(self.img)

    def shadow(self, box, radius, offset, blur, strength, color=(0, 0, 0)):
        x0, y0, x1, y1 = box
        mask = Image.new("L", self.img.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle([sp(x0), sp(y0 + offset), sp(x1), sp(y1 + offset)],
                                               radius=sp(radius), fill=strength)
        mask = mask.filter(ImageFilter.GaussianBlur(sp(blur)))
        self.img.paste(color, mask=mask)
        self.refresh()

    def spaced(self, x, y, text, font, fill, gap, anchor=None):
        for ch in text:
            self.d.text((x, y), ch, font=font, fill=fill, anchor=anchor)
            x += self.d.textlength(ch, font=font) + sp(gap)
        return x

    def spaced_w(self, text, font, gap) -> float:
        return sum(self.d.textlength(ch, font=font) / SCALE + gap for ch in text) - (gap if text else 0)

    def x_mark(self, cx, cy, r, color, w=3.4):
        self.d.line([sp(cx - r), sp(cy - r), sp(cx + r), sp(cy + r)], fill=color, width=sp(w))
        self.d.line([sp(cx - r), sp(cy + r), sp(cx + r), sp(cy - r)], fill=color, width=sp(w))

    def fit_text(self, text, font, max_w) -> str:
        """text ko max_w (1x px) mein fit karo, zarurat ho to '...' laga ke"""
        if self.d.textlength(text, font=font) / SCALE <= max_w:
            return text
        while len(text) > 3 and self.d.textlength(text + "...", font=font) / SCALE > max_w:
            text = text[:-1]
        return text.rstrip(" :-,") + "..."


# ------------------------------------------------------------------ decorations
def _decorations(c: _Canvas):
    for i in range(3):                                    # hamburger
        c.d.rounded_rectangle([sp(MARGIN), sp(30 + i * 10), sp(MARGIN + 40), sp(34 + i * 10)],
                              radius=sp(2), fill=NAVY)
    for x, y, col in [(800, 95, RED), (640, 62, NAVY), (735, 690, RED), (1050, 700, NAVY)]:
        c.x_mark(x, y, 8, col)
    zig = [(330 + i * 10, 698 + (-7 if i % 2 else 0)) for i in range(9)]
    c.d.line([(sp(x), sp(y)) for x, y in zig], fill=RED, width=sp(3.2), joint="curve")
    for r in range(5):                                    # dotted grid
        for k in range(7):
            cx, cy = 1112 + k * 17, 650 + r * 14
            c.d.ellipse([sp(cx - 3), sp(cy - 3), sp(cx + 3), sp(cy + 3)], fill=DOT_PINK)


# ------------------------------------------------------------------ art (right side)
def _art_block(c: _Canvas, art, label: str):
    """red block + pink circle + character (RGBA png) ya cover card (RGB)"""
    bx0, by0, bx1, by1 = 730, 372, 1268, 676
    c.d.rectangle([sp(bx0), sp(by0), sp(bx1), sp(by1)], fill=RED)
    for i in range(5):                                    # black dots column on the red block
        cy = by0 + 22 + i * 28
        c.d.ellipse([sp(1240), sp(cy), sp(1254), sp(cy + 14)], fill=(25, 25, 30))
    if art is None:
        return
    kind, img = art
    if kind == "char":
        ch = img.convert("RGBA")
        bbox = ch.getchannel("A").getbbox()
        if bbox:
            ch = ch.crop(bbox)
        max_h, max_w = 640, 520
        sc = min(max_h / ch.height, max_w / ch.width)
        w, h = max(1, int(ch.width * sc)), max(1, int(ch.height * sc))
        ch = ch.resize((sp(w), sp(h)), Image.LANCZOS)
        cx, bottom = 990, 676                             # stands on the bottom edge of the red block
        # soft pink circle behind the character (stylish halo)
        r = 215
        c.d.ellipse([sp(cx - r), sp(bottom - h * 0.55 - r), sp(cx + r), sp(bottom - h * 0.55 + r)], fill=PILL_FILL)
        c.d.rectangle([sp(bx0), sp(by0), sp(bx1), sp(by1)], fill=RED)       # keep the block solid under the halo
        # shadow
        sh = Image.new("RGBA", c.img.size, (0, 0, 0, 0))
        sh.paste((0, 0, 0, 120), (sp(cx - w / 2) + sp(8), sp(bottom - h) + sp(8)), ch.getchannel("A"))
        sh = sh.filter(ImageFilter.GaussianBlur(sp(9)))
        base = c.img.convert("RGBA")
        base = Image.alpha_composite(base, sh)
        base.alpha_composite(ch, (sp(cx - w / 2), sp(bottom - h)))
        c.img = base.convert("RGB")
        c.refresh()
    else:
        w = 340
        im = img.convert("RGB")
        h = int(w * im.height / im.width)
        h = min(h, 560)
        im = im.resize((sp(w), sp(int(w * img.height / img.width))), Image.LANCZOS).crop((0, 0, sp(w), sp(h)))
        px, py = 832 - 10, 120 + 0
        py = max(60, 676 - h - 40)
        c.shadow((px, py, px + w, py + h), radius=18, offset=12, blur=16, strength=110)
        mask = Image.new("L", im.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, im.width - 1, im.height - 1], radius=sp(18), fill=255)
        c.img.paste(im, (sp(px), sp(py)), mask)
        c.refresh()
    # "FEATURING" label (small, on the red block)
    if label:
        f = _font("Poppins-Bold.ttf", 12.5)
        txt = c.fit_text("FEATURING · " + label.upper(), f, 330)
        tw = c.spaced_w(txt, f, 1.2)
        px0, py0 = bx1 - 24 - tw - 28, by1 - 14 - 30
        c.d.rounded_rectangle([sp(px0), sp(py0), sp(bx1 - 24), sp(py0 + 30)], radius=sp(15), fill="white")
        c.spaced(sp(px0 + 14), sp(py0 + 15), txt, f, NAVY, 1.2, anchor="lm")


# ------------------------------------------------------------------ text (left side)
def _text_block(c: _Canvas, items: list, day: str, date_str: str):
    y0 = 92
    f = _font("Poppins-Bold.ttf", 13)
    gap = 2.4
    text = f"{day.upper()} · {date_str.upper()}"
    tw = c.spaced_w(text, f, gap)
    pw, ph = tw + 36, 34
    c.d.rounded_rectangle([sp(MARGIN), sp(y0 - 6), sp(MARGIN + pw), sp(y0 - 6 + ph)], radius=sp(ph / 2),
                          fill=PILL_FILL, outline=PILL_BORDER, width=SCALE)
    c.spaced(sp(MARGIN + 18), sp(y0 - 6 + ph / 2) - sp(9.5), text, f, RED, gap)

    tf = _font("Poppins-Bold.ttf", 54)
    for dy, line, col in [(30, "HINDI ANIME", NAVY), (92, "SCHEDULE", RED)]:
        c.d.text((sp(MARGIN), sp(y0 + dy)), line, font=tf, fill=col, stroke_width=sp(1.4), stroke_fill=col)

    top, pitch, rh = 266, 55, 46
    shown = items if len(items) <= MAX_ROWS else items[:MAX_ROWS - 1]
    tf_time = _font("Poppins-Bold.ttf", 14.5)
    tf_name = _font("Poppins-Bold.ttf", 17)
    tf_ep = _font("Poppins-Bold.ttf", 12.5)
    for i, it in enumerate(shown):
        y = top + i * pitch
        c.d.rounded_rectangle([sp(MARGIN), sp(y), sp(LIST_RIGHT), sp(y + rh)], radius=sp(8),
                              fill=BOX, outline=BOX_LINE, width=SCALE)
        c.d.rounded_rectangle([sp(MARGIN), sp(y), sp(MARGIN + 6), sp(y + rh)], radius=sp(3), fill=RED)
        c.d.text((sp(MARGIN + 22), sp(y + rh / 2)), it.get("time") or "TBA", font=tf_time, fill=RED, anchor="lm")
        c.d.line([sp(MARGIN + 124), sp(y + 10), sp(MARGIN + 124), sp(y + rh - 10)], fill=BOX_LINE, width=SCALE)
        ep = it.get("ep") or ""
        ep_w = (c.d.textlength(ep, font=tf_ep) / SCALE + 22) if ep else 0
        if ep:
            ex1 = LIST_RIGHT - 10
            c.d.rounded_rectangle([sp(ex1 - ep_w), sp(y + 9), sp(ex1), sp(y + rh - 9)], radius=sp(10),
                                  fill=CHIP, outline=(225, 225, 225), width=SCALE)
            c.d.text((sp(ex1 - ep_w / 2), sp(y + rh / 2)), ep, font=tf_ep, fill=CHIP_TEXT, anchor="mm")
        name = c.fit_text(it.get("title") or "", tf_name, LIST_RIGHT - 10 - (MARGIN + 140) - ep_w - 10)
        c.d.text((sp(MARGIN + 140), sp(y + rh / 2)), name, font=tf_name, fill=NAVY, anchor="lm")
    if len(items) > len(shown):
        y = top + len(shown) * pitch
        more = f"+ {len(items) - len(shown)} more anime today"
        c.d.text((sp(MARGIN + 6), sp(y + rh / 2)), more, font=_font("Poppins-Bold.ttf", 14), fill=RED, anchor="lm")


# ------------------------------------------------------------------ build
def build_schedule_image(items: list, day: str, date_str: str, brand: str, art=None, art_label: str = "") -> Image.Image:
    c = _Canvas()
    _decorations(c)
    _art_block(c, art, art_label)
    _text_block(c, items, day, date_str)
    img = c.img.resize((WIDTH, HEIGHT), Image.LANCZOS)
    if brand:
        img = chname.draw_brand(img, brand, 11)           # same logo spot as poster 11 (2 usernames -> stacked)
    return img.convert("RGB")


# ------------------------------------------------------------------ art picking (AniList / fanart.tv)
async def _pick_art(titles: list):
    """(('char', RGBA) | ('cover', RGB) | None, anime title). Aaj ke anime mein se random ek."""
    from . import api, dataset
    pool = [t for t in titles if t]
    random.shuffle(pool)
    for t in pool[:4]:
        try:
            a = await api.find_anime(t)
            if a is None:
                continue
            await api._prepare()
            await dataset._ensure_character(a)
            if a.characters:
                _label, img = random.choice(a.characters)
                return ("char", img), a.title or t
            await dataset._need_image(a, "cover")
            cov = a.cover
            if cov is not None:
                return ("cover", cov), a.title or t
        except Exception as e:
            LOGGER.warning(f"[SchedulePoster] art for '{t}' failed: {e!r}")
    return None, ""


async def render_schedule_to_file(items: list, day: str, date_str: str, brand: str, dest: str,
                                  timeout: float = 75.0) -> bool:
    """Schedule image banao, JPEG dest pe likho. Fail pe False (caller sirf text post kare)."""
    try:
        from . import api
        await api._prepare()
        try:
            art, label = await asyncio.wait_for(_pick_art([i.get("title") for i in items]), timeout - 20)
        except asyncio.TimeoutError:
            art, label = None, ""
        img = await asyncio.to_thread(build_schedule_image, items, day, date_str, brand, art, label)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=93, optimize=True)
        with open(dest, "wb") as fh:
            fh.write(buf.getvalue())
        return True
    except Exception as e:
        LOGGER.warning(f"[SchedulePoster] render failed: {e!r}")
        return False
