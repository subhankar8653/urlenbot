import io
import math
import os
import random
import re
import urllib.request

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

W, H = 1920, 1080
FONT_DIR = os.environ.get("POSTER_FONT_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts"))
FONT_URL = "https://raw.githubusercontent.com/google/fonts/main/ofl/poppins/Poppins-{}.ttf"   # OFL licence


FONT_FILES = {   # key -> (file name, download url)   (both fonts are open-licensed)
    "Bold": ("Poppins-Bold.ttf", "https://raw.githubusercontent.com/google/fonts/main/ofl/poppins/Poppins-Bold.ttf"),
    "Medium": ("Poppins-Medium.ttf", "https://raw.githubusercontent.com/google/fonts/main/ofl/poppins/Poppins-Medium.ttf"),
    "Lilita": ("LilitaOne-Regular.ttf", "https://raw.githubusercontent.com/google/fonts/main/ofl/lilitaone/LilitaOne-Regular.ttf"),
}


def _font_path(name):
    """Font file for a key of FONT_FILES - downloaded once on first use if missing (no fonts folder needed in git)."""
    fname, url = FONT_FILES.get(name) or FONT_FILES["Bold"]
    path = os.path.join(FONT_DIR, fname)
    if not os.path.exists(path):
        try:
            os.makedirs(FONT_DIR, exist_ok=True)
            urllib.request.urlretrieve(url, path + ".part")
            os.replace(path + ".part", path)               # atomic: never leaves a half-downloaded font
        except Exception:
            pass
    return path


def _load_font(name, px):
    try:
        return ImageFont.truetype(_font_path(name), px)
    except OSError:                                          # offline first run -> still render something
        for fb in ("DejaVuSans-Bold.ttf", "DejaVuSans.ttf"):
            try:
                return ImageFont.truetype(fb, px)
            except OSError:
                pass
        return ImageFont.load_default(px)


# ======================================================================
# 1. drawing toolkit (supersampled canvas, gradients, splines)
# ======================================================================
def gradient_image(size, c1, c2, mode="h"):
    """RGBA gradient. mode: h (left->right), v (top->bottom), d (top-left -> bottom-right)."""
    w, h = max(1, size[0]), max(1, size[1])
    ramp = Image.linear_gradient("L")                        # 256x256, black top -> white bottom
    tv = ramp.resize((w, h), Image.BILINEAR)
    th = ramp.transpose(Image.ROTATE_90).resize((w, h), Image.BILINEAR)   # black left -> white right
    t = {"h": th, "v": tv, "d": ImageChops.add(th, tv, scale=2)}[mode]
    a = Image.new("RGBA", (w, h), tuple(c2[:3]) + (255,))
    b = Image.new("RGBA", (w, h), tuple(c1[:3]) + (255,))
    return Image.composite(a, b, t)


class Canvas:
    def __init__(self, w=W, h=H, bg=(255, 255, 255, 255), ss=2):
        self.w, self.h, self.ss = w, h, ss
        self.base = Image.new("RGBA", (w, h), bg)
        self._new_layer()

    def _new_layer(self):
        self.layer = Image.new("RGBA", (self.w * self.ss, self.h * self.ss), (0, 0, 0, 0))
        self.d = ImageDraw.Draw(self.layer)

    def flush(self):
        small = self.layer.resize((self.w, self.h), Image.LANCZOS)
        self.base = Image.alpha_composite(self.base, small)
        self._new_layer()

    def paste(self, img, xy, mask=None):
        self.flush()
        tmp = Image.new("RGBA", self.base.size, (0, 0, 0, 0))
        tmp.paste(img, xy, mask if mask is not None else img)
        self.base = Image.alpha_composite(self.base, tmp)

    def shadow(self, box, r, offset=(0, 18), blur=22, color=(0, 0, 0, 70)):
        self.flush()
        sh = Image.new("RGBA", self.base.size, (0, 0, 0, 0))
        x0, y0, x1, y1 = box
        ImageDraw.Draw(sh).rounded_rectangle([x0 + offset[0], y0 + offset[1], x1 + offset[0], y1 + offset[1]],
                                             radius=r, fill=color)
        self.base = Image.alpha_composite(self.base, sh.filter(ImageFilter.GaussianBlur(blur)))

    def to_image(self):
        self.flush()
        return self.base.convert("RGB")

    # ---- gradient fills through a mask ----
    def new_mask(self):
        m = Image.new("L", (self.w * self.ss, self.h * self.ss), 0)
        return m, ImageDraw.Draw(m)

    def fill_mask(self, mask, c1, c2, mode="h", region=None):
        """Paint a gradient wherever `mask` (supersampled, from new_mask) is white."""
        self.flush()
        m1 = mask.resize((self.w, self.h), Image.LANCZOS)
        box = region or m1.getbbox()
        if not box:
            return
        x0, y0, x1, y1 = box
        layer = Image.new("RGBA", self.base.size, (0, 0, 0, 0))
        layer.paste(gradient_image((x1 - x0, y1 - y0), c1, c2, mode), (x0, y0), m1.crop(box))
        self.base = Image.alpha_composite(self.base, layer)

    # ---- fonts / measuring ----
    def font(self, name, size):
        return _load_font(name, int(size * self.ss))

    def text_w(self, txt, font, tracking=0):
        s = self.ss
        if not txt:
            return 0
        return (sum(self.d.textlength(c, font=font) + tracking * s for c in txt) - tracking * s) / s

    def fit_size(self, name, lines, max_w, start=100, tracking_em=0.0, minimum=20):
        size = start
        while size > minimum:
            f = self.font(name, size)
            if max(self.text_w(l, f, tracking_em * size) for l in lines) <= max_w:
                return size
            size -= 1
        return minimum

    # ---- primitives (all 1x coords) ----
    def text(self, xy, txt, font, fill, anchor="ls", tracking=0, d=None):
        d = d or self.d
        s, (x, y) = self.ss, xy
        if tracking == 0:
            d.text((x * s, y * s), txt, font=font, fill=fill, anchor=anchor)
            return
        for c in txt:
            d.text((x * s, y * s), c, font=font, fill=fill, anchor=anchor)
            x += d.textlength(c, font=font) / s + tracking

    def gradient_text(self, xy, txt, font, c1, c2, tracking=0):
        m, md = self.new_mask()
        self.text(xy, txt, font, 255, tracking=tracking, d=md)
        self.fill_mask(m, c1, c2, "h")

    def rounded_box(self, box, r, fill=None, outline=None, width=0):
        s = self.ss
        self.d.rounded_rectangle([v * s for v in box], radius=r * s, fill=fill, outline=outline, width=int(width * s))

    def gradient_rounded_box(self, box, r, c1, c2, mode="h"):
        m, md = self.new_mask()
        md.rounded_rectangle([v * self.ss for v in box], radius=r * self.ss, fill=255)
        self.fill_mask(m, c1, c2, mode)

    def circle(self, cx, cy, r, fill=None, outline=None, width=0):
        s = self.ss
        self.d.ellipse([(cx - r) * s, (cy - r) * s, (cx + r) * s, (cy + r) * s], fill=fill, outline=outline,
                       width=int(width * s))

    def polygon(self, pts, fill):
        self.d.polygon([(x * self.ss, y * self.ss) for x, y in pts], fill=fill)

    def polyline(self, pts, color, width):
        s = self.ss
        self.d.line([(x * s, y * s) for x, y in pts], fill=color, width=int(width * s), joint="curve")

    def colored_polyline(self, pts, colors, width):
        """Open polyline where every segment has its own (RGBA) colour."""
        s = self.ss
        for (p0, p1, c) in zip(pts, pts[1:], colors):
            self.d.line([(p0[0] * s, p0[1] * s), (p1[0] * s, p1[1] * s)], fill=c, width=int(width * s))

    def star(self, cx, cy, r, color, points=4, pinch=0.26):
        pts = []
        for i in range(points * 2):
            a = math.pi / points * i - math.pi / 2
            rr = r if i % 2 == 0 else r * pinch
            pts.append((cx + rr * math.cos(a), cy + rr * math.sin(a)))
        self.polygon(pts, color)

    def arrow(self, x0, x1, y, color, width=3, head=11):
        self.polyline([(x0, y), (x1, y)], color, width)
        self.polyline([(x1 - head, y - head * .75), (x1, y), (x1 - head, y + head * .75)], color, width)


def _mix(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def smooth(points, steps=20):
    """Catmull-Rom spline through points -> dense polyline."""
    p = [points[0]] + list(points) + [points[-1]]
    out = []
    for i in range(1, len(p) - 2):
        p0, p1, p2, p3 = p[i - 1], p[i], p[i + 1], p[i + 2]
        for k in range(steps):
            t = k / steps
            out.append(tuple(0.5 * (2 * p1[j] + (-p0[j] + p2[j]) * t
                                    + (2 * p0[j] - 5 * p1[j] + 4 * p2[j] - p3[j]) * t * t
                                    + (-p0[j] + 3 * p1[j] - 3 * p2[j] + p3[j]) * t ** 3) for j in range(2)))
    out.append(p[-2])
    return out


# ======================================================================
# 2. text helpers
# ======================================================================
def clean_description(raw, limit=250):
    if not raw:
        return "No description available."
    t = re.sub(r"<br\s*/?>", " ", raw, flags=re.I)
    t = re.sub(r"<[^>]+>", "", t)
    t = re.sub(r"~!.*?!~", "", t, flags=re.S)                # anilist spoilers
    t = re.sub(r"\(Source:.*?\)|\[Written by.*?\]", "", t, flags=re.I | re.S)
    t = re.sub(r"\s+", " ", t).strip().replace("*", "")
    if len(t) <= limit:
        return t
    cut = t[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if end > limit * 0.4:
        return cut[:end + 1]
    return cut[:cut.rfind(" ")].rstrip(",;:") + "..."


def auto_highlight(text, n=None):
    """Wrap n evenly-spaced longer words in *stars* (they get the accent colour)."""
    words = text.split(" ")
    n = n or max(2, min(7, len(words) // 10))
    cand = [i for i, w in enumerate(words) if len(re.sub(r"\W", "", w)) >= 6]
    if not cand:
        return text
    step = max(1, len(cand) // n)
    for i in cand[::step][:n]:
        core = re.match(r"^(\W*)(.*?)(\W*)$", words[i])
        words[i] = f"{core.group(1)}*{core.group(2)}*{core.group(3)}"
    return " ".join(words)


def split_title(title, n_lines):
    words = title.upper().split()
    if n_lines == 1 or len(words) < 2:
        return [" ".join(words)]
    n_lines = min(n_lines, len(words))
    best, best_cost = None, 1e9

    def rec(start, left, acc):
        nonlocal best, best_cost
        if left == 1:
            cand = acc + [" ".join(words[start:])]
            cost = max(len(l) for l in cand)
            if cost < best_cost:
                best, best_cost = cand, cost
            return
        for k in range(start + 1, len(words) - left + 2):
            rec(k, left - 1, acc + [" ".join(words[start:k])])
    rec(0, n_lines, [])
    return best


# ======================================================================
# 4. shared components (used by every style, just different numbers)
# ======================================================================
def draw_title(cv, th, meta, x, max_w, anchor_y, mode="last", pitch=1.2, gradient=False,
               three_line_below=62, tracking_em=-0.035, single_line_dark=True, font="Bold"):
    """mode 'last': anchor_y = baseline of the last line;  'center': block is centred on anchor_y.
    Returns the baseline of the last line."""
    title = (meta.get("title") or "UNKNOWN").strip()
    lines = split_title(title, 2)
    size = cv.fit_size(font, lines, max_w, start=100, tracking_em=tracking_em)
    if size < three_line_below and len(title.split()) >= 4:
        lines = split_title(title, 3)
        size = cv.fit_size(font, lines, max_w, start=100, tracking_em=tracking_em)
    f, tr, step = cv.font(font, size), tracking_em * size, size * pitch
    n = len(lines)
    first = {"last": anchor_y - (n - 1) * step, "first": anchor_y}.get(mode, anchor_y - (n - 1) * step / 2)
    for i, line in enumerate(lines):
        y, last = first + i * step, i == n - 1
        if last and (n > 1 or not single_line_dark):
            if gradient:
                cv.gradient_text((x, y), line, f, th["g1"], th["g2"], tracking=tr)
            else:
                cv.text((x, y), line, f, th["title2"], tracking=tr)
        else:
            cv.text((x, y), line, f, th["title1"], tracking=tr)
    return first + (n - 1) * step


def draw_tags(cv, th, meta, x, y, size=20, h=58, pad=22, gap=16, radius=8, border=None):
    f = cv.font("Bold", size)
    border = border or th["box_border"]
    for t in [g.upper() for g in (meta.get("genres") or [])[:3]]:
        w = cv.text_w(t, f) + pad * 2
        cv.rounded_box((x, y, x + w, y + h), radius, fill=th["box_fill"], outline=border, width=2)
        cv.text((x + pad, y + h / 2), t, f, th["tag_text"], anchor="lm")
        x += w + gap
    if meta.get("score"):
        label = f"{meta['score'] / 10:.1f} / 10"
        w = cv.text_w(label, f) + pad * 2 + 30
        cv.rounded_box((x, y, x + w, y + h), radius, fill=th["box_fill"], outline=border, width=2)
        cv.star(x + pad + 8, y + h / 2, size * 0.55, th["mid"], points=5, pinch=0.42)
        cv.text((x + pad + 28, y + h / 2), label, f, th["tag_text"], anchor="lm")
    return y + h


def draw_description(cv, th, meta, x, y, w, size=22, lh=42, pad_x=25, pad_y=18, radius=8, hl=None, border=None, limit=250):
    text = auto_highlight(clean_description(meta.get("description"), limit))
    f = cv.font("Medium", size)
    s = cv.ss
    space = cv.d.textlength(" ", font=f) / s
    words, on = [], False
    for tok in text.split(" "):                    # *star* markers toggle highlighting (also across words)
        n = tok.count("*")
        words.append((tok.replace("*", ""), on or n > 0))
        on = (int(on) + n) % 2 == 1
    lines, cur = [[]], 0
    for tok, h_ in words:
        ww = cv.d.textlength(tok, font=f) / s
        if cur + ww > w - 2 * pad_x and lines[-1]:
            lines.append([]); cur = 0
        lines[-1].append((tok, h_, ww)); cur += ww + space
    box_h = len(lines) * lh + pad_y * 2
    cv.rounded_box((x, y, x + w, y + box_h), radius, fill=th["box_fill"], outline=border or th["box_border"], width=2)
    ly = y + pad_y + lh / 2
    for ln in lines:
        lx = x + pad_x
        for tok, h_, ww in ln:
            cv.text((lx, ly), tok, f, (hl or th["accent"]) if h_ else th["body"], anchor="lm")
            lx += ww + space
        ly += lh
    return box_h


def draw_button(cv, th, x, y, w, h, label, size=22, gradient=False):
    if gradient:
        cv.gradient_rounded_box((x, y, x + w, y + h), h / 2, th["g1"], th["g2"], "h")
    else:
        cv.rounded_box((x, y, x + w, y + h), h / 2, fill=th["accent"])
    white = (255, 255, 255, 255)
    cv.text((x + w * 0.12, y + h / 2), label, cv.font("Bold", size), white, anchor="lm")
    cv.arrow(x + w - w / 3, x + w - w * 0.12, y + h / 2, white, width=3)


def place_character(cv, character, cx, bottom, max_w, max_h):
    ch = character.convert("RGBA")
    bbox = ch.getchannel("A").getbbox()
    if bbox:
        ch = ch.crop(bbox)
    sc = min(max_w / ch.width, max_h / ch.height)
    ch = ch.resize((max(1, int(ch.width * sc)), max(1, int(ch.height * sc))), Image.LANCZOS)
    cv.paste(ch, (int(cx - ch.width / 2), int(bottom - ch.height)))


def place_cover_card(cv, cover, x, y, bw=430, bh=645, r=38):
    img = cover.convert("RGB")
    sc = max(bw / img.width, bh / img.height)
    img = img.resize((int(img.width * sc) + 1, int(img.height * sc) + 1), Image.LANCZOS)
    l, t = (img.width - bw) // 2, (img.height - bh) // 2
    img = img.crop((l, t, l + bw, t + bh)).convert("RGBA")
    cv.shadow((x - 8, y - 8, x + bw + 8, y + bh + 8), r, (0, 18), 22, (0, 0, 0, 80))
    cv.rounded_box((x - 8, y - 8, x + bw + 8, y + bh + 8), r, fill=(255, 255, 255, 255))
    cv.flush()
    m = Image.new("L", (bw * 4, bh * 4), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, bw * 4, bh * 4], radius=(r - 8) * 4, fill=255)
    cv.paste(img, (x, y), m.resize((bw, bh), Image.LANCZOS))


def menu_icon(cv, style="dots"):
    black = (0, 0, 0, 255)
    if style == "dots":
        for i in range(3):
            cv.rounded_box((33, 68 + i * 16, 93, 76 + i * 16), 0, fill=black)
            cv.rounded_box((100, 68 + i * 16, 108, 76 + i * 16), 0, fill=black)
    else:
        for yy in (50, 62, 74):
            cv.rounded_box((34, yy, 90, yy + 5), 0, fill=black)



BASE = dict(
    title1=(85, 85, 85, 255), body=(75, 80, 92, 255), tag_text=(160, 164, 174, 255),
    box_fill=(255, 255, 255, 255), box_border=(236, 236, 240, 255), box_border2=(232, 232, 238, 255),
    line=(50, 50, 50, 255), outline=(30, 30, 30, 255), star_white=(255, 255, 255, 255), star_blue=(118, 122, 176, 255),
)



def finish(cv):
    return cv.to_image()


def to_bytes(img, fmt="PNG"):
    buf = io.BytesIO()
    img.save(buf, fmt, **({"quality": 92} if fmt.upper() == "JPEG" else {}))
    return buf.getvalue()


# ======================================================================
# style 7: colours + layout   (light grey page, yellow line-art on the right, framed cover)
# ======================================================================
from PIL import ImageOps

GOLD, GOLD_LT, BTN, DARK = (238, 160, 0, 255), (250, 205, 90, 255), (250, 190, 20, 255), (40, 41, 52, 255)
PAGE = (241, 243, 244, 255)
THEME = dict(BASE,
    accent=(210, 136, 0, 255), mid=(210, 136, 0, 255), g1=BTN[:3], g2=GOLD[:3], title1=DARK, title2=GOLD,
    body=(60, 64, 76, 255), tag_text=(130, 134, 146, 255),
    box_fill=(228, 231, 236, 255), box_border=(208, 213, 221, 255), line_dark=(176, 108, 0))
LINES_ASSET = "lines_navy.png"       # navy line-art crop (assets/); recoloured to THEME["line_dark"] at render time


def _line_art(cv, th, here):
    """Right-hand swirl lines: the navy asset is recoloured (navy -> deep amber, page grey stays page grey)."""
    path = os.path.join(here, "assets", LINES_ASSET)
    if not os.path.exists(path):
        return
    g = Image.open(path).convert("L")
    art = ImageOps.colorize(g, black=th["line_dark"], white=PAGE[:3], blackpoint=47, whitepoint=244).convert("RGBA")
    cv.base.paste(art, (W - art.width, 0))


def _cross(cv, cx, cy, r, color, w):
    cv.polyline([(cx - r, cy - r), (cx + r, cy + r)], color, w)
    cv.polyline([(cx - r, cy + r), (cx + r, cy - r)], color, w)


def _frame(cv, cover, character, fx=1212, fy=119, fw=560, fh=842):
    cv.shadow((fx - 10, fy - 10, fx + fw + 10, fy + fh + 10), 34, (8, 14), 14, (60, 40, 0, 90))
    cv.rounded_box((fx + 22, fy + 22, fx + fw + 22, fy + fh + 22), 34, outline=GOLD, width=5)    # offset outline
    cv.rounded_box((fx - 10, fy - 10, fx + fw + 10, fy + fh + 10), 34, fill=BTN)                  # yellow border
    if cover is not None:
        img = cover.convert("RGB")
        sc = max(fw / img.width, fh / img.height)
        img = img.resize((int(img.width * sc) + 1, int(img.height * sc) + 1), Image.LANCZOS)
        l, t = (img.width - fw) // 2, (img.height - fh) // 2
        img = img.crop((l, t, l + fw, t + fh)).convert("RGBA")
    else:
        img = Image.new("RGBA", (fw, fh), (246, 246, 248, 255))
        if character is not None:
            ch = character.convert("RGBA")
            bb = ch.getchannel("A").getbbox()
            ch = ch.crop(bb) if bb else ch
            sc = min(fw * 0.85 / ch.width, fh * 0.95 / ch.height)
            ch = ch.resize((int(ch.width * sc), int(ch.height * sc)), Image.LANCZOS)
            img.paste(ch, ((fw - ch.width) // 2, fh - ch.height), ch)
    m = Image.new("L", (fw * 4, fh * 4), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, fw * 4, fh * 4], radius=26 * 4, fill=255)
    cv.paste(img, (fx, fy), m.resize((fw, fh), Image.LANCZOS))


def _draw(cv, th, meta, character, cover, button):
    X, MAXW = 140, 900
    here = os.path.dirname(os.path.abspath(__file__))

    cv.base.paste(PAGE, (0, 0, W, H))
    _line_art(cv, th, here)
    _frame(cv, cover, character)

    # --- decoration ---
    menu_icon(cv, "bars")
    for x0, y0, sz, col in [(820, -40, 110, GOLD_LT), (925, 25, 90, GOLD)]:
        cv.rounded_box((x0, y0, x0 + sz, y0 + sz), sz * 0.26, outline=col, width=3)
    cv.star(1060, 125, 20, GOLD)
    _cross(cv, 520, 110, 12, DARK, 8)
    cv.polyline([(150 + i * 17, 965 + (14 if i % 2 else 0)) for i in range(8)], GOLD, 6)
    for i in range(5):
        for j in range(4):
            cv.circle(560 + i * 20, 920 + j * 20, 3, fill=GOLD_LT)
    pts = []
    a, b, c, d = (700, 1085), (900, 930), (1000, 1090), (1150, 965)
    for i in range(121):
        u = i / 120
        v = 1 - u
        pts.append((v**3 * a[0] + 3 * v * v * u * b[0] + 3 * v * u * u * c[0] + u**3 * d[0],
                    v**3 * a[1] + 3 * v * v * u * b[1] + 3 * v * u * u * c[1] + u**3 * d[1]))
    cv.polyline(pts, DARK, 3)
    cv.circle(1110, 870, 12, outline=GOLD, width=3)
    cv.star(330, 1015, 16, GOLD_LT)

    # --- text column ---
    last = draw_title(cv, th, meta, X, MAXW, 305, mode="first", pitch=1.02, font="Lilita", tracking_em=0.0,
                      three_line_below=52)
    ty = last + 40
    draw_tags(cv, th, meta, X, ty, size=19, h=54, pad=20, gap=14, radius=10)
    by0 = ty + 54 + 28
    h = draw_description(cv, th, meta, X, by0, MAXW, size=21, lh=38, pad_x=24, pad_y=16, radius=10,
                         hl=th["accent"], limit=330)
    by = by0 + h + 32
    cv.rounded_box((X, by, X + 300, by + 76), 38, fill=BTN)
    cv.text((X + 36, by + 38), button, cv.font("Bold", 22), DARK, anchor="lm")
    cx, cy = X + 300 - 48, by + 38
    cv.circle(cx, cy, 23, fill=DARK)
    cv.arrow(cx - 10, cx + 10, cy, BTN, width=3, head=8)


def render_poster(meta, character=None, cover=None, button="WATCH NOW"):
    """Returns a 1920x1080 PIL RGB image."""
    cv = Canvas()
    _draw(cv, THEME, meta, character, cover, button)
    return finish(cv)


def render_poster_bytes(meta, character=None, cover=None, fmt="PNG"):
    return to_bytes(render_poster(meta, character, cover), fmt)


DEMO = dict(title="Alya Sometimes Hides Her Feelings in Russian", genres=["Comedy", "Romance", "Slice of Life"],
            description=("Alya is a transfer student enjoying popularity at her new high school, often sporting a cold "
                         "shoulder while earning high marks in class. She ignores her nerdy classmate, Kuze Masachika, "
                         "except for when she blurts out a flirtatious line to him in Russian. Little does she know, he "
                         "understands every word!"))

if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    art = os.path.join(here, "assets", "cover.jpg")
    cover = Image.open(art) if os.path.exists(art) else None
    out = os.path.join(here, "poster7.png")
    render_poster(DEMO, None, cover).save(out)
    print("saved", out)
