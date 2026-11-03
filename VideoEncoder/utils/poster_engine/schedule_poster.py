"""
schedule_poster.py
===================
Hindi anime schedule ki image (NEW style) — poster 11 jaisa design:
  • upar-left : hamburger + community username(s)  (2 username => dono SIDE-BY-SIDE, chhote, telegram icon ke saath)
  • pill      : DIN · aaj ki date
  • heading   : HINDI ANIME / SCHEDULE
  • list      : har anime ka  TIME | naam | S04 • E23   (max 7 row, baaki "+N more")
  • right side: aaj ke aane wale anime mein se random ek ka CHARACTER art (fanart.tv transparent PNG;
                na mile to AniList character ka cut-out) — red block + dots ke saath. COVER CARD kabhi nahi.

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


# ------------------------------------------------------------------ community handles (top-left, side by side)
def _plane(c: _Canvas, x, cy, size, color):
    """chhota telegram paper-plane icon (x = left, cy = vertical centre, 1x px)"""
    w, h = size, size * 0.86
    pts = [(0.0, 0.46), (1.0, 0.0), (0.80, 1.0), (0.50, 0.68), (0.31, 0.92), (0.30, 0.62)]
    top = cy - h / 2
    c.d.polygon([(sp(x + px * w), sp(top + py * h)) for px, py in pts], fill=color)


def _brand_row(c: _Canvas, brand: str):
    """@A | @B  — dono community ek line mein side-by-side, chhote (aankhon mein na chubhe). 1 ho to sirf ek."""
    toks = [t for t in (brand or "").split() if t.startswith("@") and len(t) > 1][:2]
    if not toks:
        t = chname.clean(brand)
        toks = [t] if t else []
    if not toks:
        return
    x0, cy = MARGIN + 62, 42
    size, gap, icon = 14.5, 8, 15
    max_total = 470
    while True:
        f = _font("Poppins-Bold.ttf", size)
        widths = [c.d.textlength(t, font=f) / SCALE for t in toks]
        total = sum(icon + gap + w for w in widths) + (26 if len(toks) > 1 else 0)
        if total <= max_total or size <= 11:
            break
        size -= 0.5
    if total > max_total:                                   # bahut lambe naam: ellipsis
        room = (max_total - (26 if len(toks) > 1 else 0)) / len(toks) - icon - gap
        toks = [c.fit_text(t, f, room) for t in toks]
    x = x0
    for i, t in enumerate(toks):
        if i:
            x += 10
            c.d.line([sp(x), sp(cy - 9), sp(x), sp(cy + 9)], fill=(190, 192, 200), width=SCALE)
            x += 16
        _plane(c, x, cy, icon, RED)
        x += icon + gap
        c.d.text((sp(x), sp(cy)), t, font=f, fill=NAVY, anchor="lm")
        x += c.d.textlength(t, font=f) / SCALE


# ------------------------------------------------------------------ art (right side)
def _grid_rows(n: int) -> list:
    """Kitne character kis row mein: 2 => upar 1 neeche 1; 3 => 2+1; 4 => 2+2; 5 => 3+2; 6 => 3+3."""
    return {1: [1], 2: [1, 1], 3: [2, 1], 4: [2, 2], 5: [3, 2], 6: [3, 3], 7: [3, 2, 2],
            8: [3, 3, 2]}.get(n, [3, 3, 3])


def _group_art(c: _Canvas, chars: list, bx0, by0, bx1, by1):
    """Aaj ke har anime ka ek character — poore right column mein grid (1:ek bada, 4:2x2, 5-6:3+3, 7-9:3 ki rows).
    Jitne zyada character, utne chhote — sab aaram se fit."""
    imgs = []
    for im in chars:
        im = im.convert("RGBA")
        bb = im.getchannel("A").getbbox()
        imgs.append(im.crop(bb) if bb else im)
    n = len(imgs)
    if not n:
        return
    rows = _grid_rows(n)
    L, R, T, B = 738, 1256, 22, 682                           # pura right column (upar se neeche tak)
    W, H = R - L, B - T
    if len(rows) == 1:
        T = B - 640 if n == 1 else B - 600                    # 1-2 character: ek hi bada row
        H = B - T
    rh = H / len(rows)
    cw = W / max(rows)
    base = c.img.convert("RGBA")
    # halo
    cx, cy = (L + R) / 2, (T + B) / 2 + 40
    r = 215
    halo = Image.new("RGBA", base.size, (0, 0, 0, 0))
    ImageDraw.Draw(halo).ellipse([sp(cx - r), sp(cy - r), sp(cx + r), sp(cy + r)], fill=PILL_FILL + (255,))
    base = Image.alpha_composite(base, halo)
    rd = ImageDraw.Draw(base)
    rd.rectangle([sp(730), sp(372), sp(1268), sp(676)], fill=RED)           # red block halo ke upar bhi solid
    it = iter(imgs)
    for ri, k in enumerate(rows):
        row_bottom = T + rh * (ri + 1) - (4 if ri < len(rows) - 1 else 0)
        if len(rows) > 1 and ri == len(rows) - 1:
            row_bottom = min(B - 2, 676 + 4)
        x0 = L + (W - k * cw) / 2
        for ci in range(k):
            im = next(it)
            sc = min((rh - 6) / im.height, (cw * 1.12) / im.width)
            w, h = max(1, int(im.width * sc)), max(1, int(im.height * sc))
            ch = im.resize((sp(w), sp(h)), Image.LANCZOS)
            px, py = sp(x0 + cw * (ci + 0.5) - w / 2), sp(row_bottom - h)
            sh = Image.new("RGBA", base.size, (0, 0, 0, 0))
            sh.paste((0, 0, 0, 110), (px + sp(5), py + sp(5)), ch.getchannel("A"))
            sh = sh.filter(ImageFilter.GaussianBlur(sp(7)))
            base = Image.alpha_composite(base, sh)
            base.alpha_composite(ch, (px, py))
    c.img = base.convert("RGB")
    c.refresh()


def _avatar_group(c: _Canvas, imgs: list, bx0, by0, bx1, by1):
    """Cut-out na bane to: character ke gol (circle) avatar, saaf-suthre, ek doosre se atakte nahi."""
    n = len(imgs)
    rows = 1 if n <= 3 else 2
    per = [n] if rows == 1 else [(n + 1) // 2, n // 2]
    top, bot = by0 + 16, by1 - 52                            # neeche label pill ki jagah
    avail_w = (bx1 - 40) - (bx0 + 30)
    gap = 14
    d = min(230, (bot - top - gap * (rows - 1)) / rows,
            min((avail_w - gap * (k - 1)) / k for k in per))
    d = int(d)
    base = c.img.convert("RGBA")
    y = top + ((bot - top) - (rows * d + gap * (rows - 1))) / 2
    it = iter(imgs)
    ring = 5
    for k in per:
        row_w = k * d + gap * (k - 1)
        x = bx0 + 30 + (avail_w - row_w) / 2
        for _ in range(k):
            im = next(it).convert("RGBA")
            bb = im.getchannel("A").getbbox()
            if bb:
                im = im.crop(bb)
            side = min(im.width, int(im.height * 0.82)) if im.height > im.width else im.width
            side = max(1, min(side, im.width))
            left = (im.width - side) // 2
            im = im.crop((left, 0, left + side, min(im.height, side)))        # chehra upar-beech se
            big = sp(d)
            av = im.resize((big, big), Image.LANCZOS)
            bg = Image.new("RGBA", (big, big), (255, 255, 255, 255))
            bg.alpha_composite(av)
            mask = Image.new("L", (big, big), 0)
            ImageDraw.Draw(mask).ellipse([0, 0, big - 1, big - 1], fill=255)
            px, py = sp(x), sp(y)
            sh = Image.new("RGBA", base.size, (0, 0, 0, 0))
            ImageDraw.Draw(sh).ellipse([px + sp(4), py + sp(6), px + big + sp(4), py + big + sp(6)], fill=(0, 0, 0, 110))
            sh = sh.filter(ImageFilter.GaussianBlur(sp(7)))
            base = Image.alpha_composite(base, sh)
            rd = Image.new("RGBA", base.size, (0, 0, 0, 0))
            ImageDraw.Draw(rd).ellipse([px - sp(ring), py - sp(ring), px + big + sp(ring), py + big + sp(ring)],
                                       fill=(255, 255, 255, 255))
            base = Image.alpha_composite(base, rd)
            base.paste(bg, (px, py), mask)
            x += d + gap
        y += d + gap
    c.img = base.convert("RGB")
    c.refresh()


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
    if kind == "avatars":
        _avatar_group(c, img, bx0, by0, bx1, by1)
    elif kind == "chars":
        _group_art(c, img, bx0, by0, bx1, by1)
    elif kind == "char":
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
    if label and not label.startswith("TODAY"):
        f = _font("Poppins-Bold.ttf", 12.5)
        txt = c.fit_text(("" if label.startswith("TODAY") else "FEATURING · ") + label.upper(), f, 330)
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
    _brand_row(c, brand)                                  # logo spot: 2 usernames side by side (chhote)
    img = c.img.resize((WIDTH, HEIGHT), Image.LANCZOS)
    return img.convert("RGB")


# ------------------------------------------------------------------ art picking (fanart.tv / AniList character)
def _flood_cutout(img: Image.Image):
    """Flat background wale AniList character portrait ka background hata do (corners se flood-fill).
    Safe check fail ho to None (galat kata hua character nahi chahiye)."""
    try:
        im = img.convert("RGB")
        w, h = im.size
        if w < 60 or h < 60:
            return None
        work = im.filter(ImageFilter.GaussianBlur(1.2))
        mark = (255, 0, 255)
        for xy in [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1), (w // 2, 0), (0, h // 2), (w - 1, h // 2)]:
            if work.getpixel(xy) != mark:
                ImageDraw.floodfill(work, xy, mark, thresh=34)
        mask = Image.new("L", (w, h), 255)
        px_w, px_m = work.load(), mask.load()
        bg = 0
        for yy in range(h):
            for xx in range(w):
                if px_w[xx, yy] == mark:
                    px_m[xx, yy] = 0
                    bg += 1
        frac = bg / float(w * h)
        if not (0.10 <= frac <= 0.72) or mask.getpixel((w // 2, int(h * 0.55))) < 200:
            return None
        mask = mask.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.GaussianBlur(1.1))
        out = im.convert("RGBA")
        out.putalpha(mask)
        return out
    except Exception as e:
        LOGGER.warning(f"[SchedulePoster] flood cutout failed: {e!r}")
        return None


def _grabcut_cutout(img: Image.Image):
    """OpenCV GrabCut (optional: pip install opencv-python-headless). Safe check fail ho to None."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None
    try:
        im = _upscale(img.convert("RGB"), 600)
        arr = np.array(im)[:, :, ::-1].copy()
        h, w = arr.shape[:2]
        mask = np.zeros((h, w), np.uint8)
        rect = (int(w * 0.03), int(h * 0.02), int(w * 0.94), int(h * 0.97))
        bgd, fgd = np.zeros((1, 65), np.float64), np.zeros((1, 65), np.float64)
        cv2.grabCut(arr, mask, rect, bgd, fgd, 5, cv2.GC_INIT_WITH_RECT)
        fg = np.where((mask == 1) | (mask == 3), 255, 0).astype("uint8")
        k = np.ones((5, 5), np.uint8)
        fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, k)
        fg = cv2.morphologyEx(fg, cv2.MORPH_CLOSE, k)
        n, lab, stats, _ = cv2.connectedComponentsWithStats(fg)
        if n > 1:                                          # sabse bada hissa hi character
            big = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
            fg = np.where(lab == big, 255, 0).astype("uint8")
        frac = float((fg > 0).sum()) / float(w * h)
        if not (0.18 <= frac <= 0.86):
            return None
        alpha = Image.fromarray(fg).filter(ImageFilter.GaussianBlur(1.3))
        out = im.convert("RGBA")
        out.putalpha(alpha)
        return out
    except Exception as e:
        LOGGER.warning(f"[SchedulePoster] grabcut failed: {e!r}")
        return None


def _feathered(img: Image.Image) -> Image.Image:
    """Cut-out na bane to character portrait ko upar/side se soft fade karo — cover card jaisa dabba nahi dikhega."""
    im = img.convert("RGBA")
    w, h = im.size
    mask = Image.new("L", (w, h), 255)
    px = mask.load()
    fx, fy = max(4, int(w * 0.18)), max(4, int(h * 0.22))
    for yy in range(h):
        ay = min(1.0, yy / fy)
        for xx in range(w):
            ax = min(1.0, xx / fx, (w - 1 - xx) / fx)
            px[xx, yy] = int(255 * max(0.0, min(ax, ay)) ** 1.3)
    im.putalpha(mask)
    return im


async def _portrait_candidates(a) -> list:
    """AniList ke main characters ki portrait images (RGB)."""
    import httpx
    from . import dataset
    urls = [(((e.get("node") or {}).get("image") or {}).get("large"))
            for e in ((a.raw.get("characters") or {}).get("edges") or [])]
    urls = [u for u in urls if u][:3]
    out = []
    if not urls:
        return out
    async with httpx.AsyncClient(headers=dataset.HEADERS, timeout=20, follow_redirects=True) as client:
        for u in urls:
            try:
                r = await client.get(u)
                r.raise_for_status()
                im = Image.open(io.BytesIO(r.content))
                im.load()
                out.append(im.convert("RGB"))
            except Exception as e:
                LOGGER.warning(f"[SchedulePoster] portrait fetch failed: {e!r}")
    return out


def _upscale(im: Image.Image, min_h: int = 900) -> Image.Image:
    if im.height >= min_h:
        return im
    k = min_h / im.height
    return im.resize((int(im.width * k), min_h), Image.LANCZOS)


async def _fanart_character(a, kinds=("characterart",)):
    """fanart.tv transparent image (kinds: 'characterart' = asli character; 'hdclearart'/'clearart' = logo + characters)."""
    import httpx
    from . import dataset
    src = dataset._sources()
    async with httpx.AsyncClient(headers=dataset.HEADERS, timeout=25, follow_redirects=True) as client:
        data = await dataset._fanart_data(client, a)
        urls = dataset._fanart_urls(data, kinds)[:6]
        random.shuffle(urls)
        for u in urls:
            img = await src.fetch_image(client, u, rgba=True)
            if img is not None and src.has_transparency(img):
                return img
    return None


_REMBG = {"sess": None, "tried": False}
_REMBG_LOCK = asyncio.Lock()


async def _rembg_cut(im: Image.Image):
    """rembg (optional: pip install rembg onnxruntime). Session ek baar load, ek waqt mein ek hi kaam (memory)."""
    async with _REMBG_LOCK:
        if not _REMBG["tried"]:
            _REMBG["tried"] = True
            try:
                import os
                from rembg import new_session
                _REMBG["sess"] = await asyncio.to_thread(new_session, os.environ.get("REMBG_MODEL", "isnet-anime"))
            except Exception as e:
                LOGGER.info(f"[SchedulePoster] rembg nahi hai/load fail ({e!r}) — GrabCut/flood-fill use hoga")
        if _REMBG["sess"] is None:
            return None
        try:
            from rembg import remove
            sess = _REMBG["sess"]
            return await asyncio.to_thread(lambda: remove(im, session=sess).convert("RGBA"))
        except Exception as e:
            LOGGER.warning(f"[SchedulePoster] rembg cut failed: {e!r}")
            return None


async def _cutout_from_portraits(a):
    """AniList character portrait -> saaf cut-out (rembg > GrabCut > flood-fill). Na bane to None (dabba/photo kabhi nahi)."""
    for im in await _portrait_candidates(a):
        for fn in (None, _grabcut_cutout, _flood_cutout):
            cut = await _rembg_cut(im) if fn is None else await asyncio.to_thread(fn, im)
            if cut is not None and cut.getchannel("A").getbbox():
                return "cut", await asyncio.to_thread(_upscale, cut)
    LOGGER.info(f"[SchedulePoster] '{a.title}': character cut-out nahi bana (fanart.tv mein bhi nahi) — skip")
    return None


MAX_CHARS = 9


async def _pick_art(titles: list):
    """(('chars', [RGBA, ...]) | None, label). Aaj ke HAR anime ka ek character (max 9), sab right side mein.
    Har anime: fanart.tv characterart -> AniList character cut-out. Cover / logo kabhi nahi."""
    from . import api
    pool = [t for t in dict.fromkeys(titles) if t]
    if len(pool) > MAX_CHARS:                                   # zyada anime: random 9 (time ka order same)
        keep = set(random.sample(range(len(pool)), MAX_CHARS))
        pool = [t for i, t in enumerate(pool) if i in keep]
    sem = asyncio.Semaphore(3)

    async def one(t):
        async with sem:
            try:
                a = await api.find_anime(t)
                if a is None:
                    return None
                img = None
                try:
                    img = await _fanart_character(a)
                except Exception as e:
                    LOGGER.warning(f"[SchedulePoster] fanart for '{t}' failed: {e!r}")
                if img is not None:
                    return "cut", img
                got = await _cutout_from_portraits(a)          # rembg / GrabCut / flood-fill cut-out
                if got is not None:
                    return got
                try:                                           # last: pehle jaisa fanart.tv clearart (logo + characters)
                    img = await _fanart_character(a, ("hdclearart", "clearart"))
                except Exception as e:
                    LOGGER.warning(f"[SchedulePoster] clearart for '{t}' failed: {e!r}")
                    img = None
                return ("cut", img) if img is not None else None
            except Exception as e:
                LOGGER.warning(f"[SchedulePoster] art for '{t}' failed: {e!r}")
                return None

    got = [x for x in await asyncio.gather(*[one(t) for t in pool]) if x is not None]
    if not got:
        return None, ""
    imgs = [im for _k, im in got]
    return ("chars", imgs), ("TODAY'S LINEUP" if len(imgs) > 1 else pool[0])


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
