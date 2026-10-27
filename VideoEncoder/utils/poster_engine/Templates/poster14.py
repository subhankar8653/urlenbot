import math
import os
from PIL import Image, ImageChops, ImageDraw, ImageFont

# ---------------------------------------------------------------- settings
S = 2                                    # draw at 2x, shrink at the end
W, H = 1280, 720

WHITE = (255, 255, 255)
PEACH = (248, 221, 211)
TEAL = (17, 55, 69)                      # title colour
SAGE = (104, 128, 118)                   # bar + brand
TAN = (196, 150, 112)
INK = (28, 28, 34)
TERRA = (178, 96, 70)                    # warm accent: title line 2, highlights
BTN1, BTN2 = (206, 134, 96), (166, 88, 64)           # button gradient (light -> deep)
CHIP, CHIP_LINE, CHIP_TEXT = (252, 238, 231), (238, 208, 196), (98, 118, 108)
BOX, BOX_LINE = (253, 243, 238), (238, 208, 196)
BODY = (30, 60, 72)

ART_X, ART_W = 58, 510                   # art panel (broader frame; height follows the crop)
TEXT_X = 640                             # one left edge for every text element
BOX_W = 500

# ------------------------------------------------------------------- fonts
FONT_DIRS = ["/usr/share/fonts/truetype/google-fonts/",
             "/usr/share/fonts/truetype/dejavu/",
             os.path.expanduser("~/.fonts/"), "fonts/"]
FALLBACKS = {"Poppins-BoldItalic.ttf": "DejaVuSans-BoldOblique.ttf",
             "Poppins-Regular.ttf": "DejaVuSans.ttf",
             "Poppins-Medium.ttf": "DejaVuSans-Bold.ttf",
             "Poppins-Bold.ttf": "DejaVuSans-Bold.ttf",
             "Lora-Italic-Variable.ttf": "DejaVuSerif-Italic.ttf"}


def font(name, size, weight=None):
    """Load a font at `size` (1x units); `weight` is for variable fonts."""
    for cand in (name, FALLBACKS.get(name)):
        for folder in FONT_DIRS:
            path = os.path.join(folder, cand or "")
            if cand and os.path.exists(path):
                f = ImageFont.truetype(path, int(size * S))
                if weight and "Variable" in cand:
                    try:
                        f.set_variation_by_axes([weight])
                    except Exception:
                        pass
                return f
    return ImageFont.load_default()


def sp(v):
    return int(v * S)


# ----------------------------------------------------------------- helpers
def line(d, pts, color, width):
    d.line([(sp(x), sp(y)) for x, y in pts], fill=color, width=sp(width), joint="curve")


def rect(d, box, fill, radius=0):
    x0, y0, x1, y1 = box
    if radius:
        d.rounded_rectangle([sp(x0), sp(y0), sp(x1), sp(y1)], radius=sp(radius), fill=fill)
    else:
        d.rectangle([sp(x0), sp(y0), sp(x1), sp(y1)], fill=fill)


def spaced(d, x, y, text, f, fill, gap):
    for ch in text:
        d.text((sp(x), sp(y)), ch, font=f, fill=fill)
        x += d.textlength(ch, font=f) / S + gap


def art_panel():
    """Crop the artwork; arched top-left corner, softly rounded others."""
    src = Image.open(source.ART_SOURCE).convert("RGB").crop(source.ART_CROP)
    h = ART_W * src.height / src.width
    art = src.resize((sp(ART_W), sp(h)), Image.LANCZOS)
    soft = Image.new("L", art.size, 0)
    ImageDraw.Draw(soft).rounded_rectangle([0, 0, art.width - 1, art.height - 1], radius=sp(24), fill=255)
    arch = Image.new("L", art.size, 0)
    ImageDraw.Draw(arch).rounded_rectangle([0, 0, art.width - 1, art.height - 1], radius=sp(110),
                                           fill=255, corners=(True, False, False, False))
    return art, ImageChops.multiply(soft, arch), h


# ------------------------------------------------------------------ pieces
def star(d, cx, cy, r, color):
    """Four-point sparkle."""
    k = 0.24
    pts = [(0, -r), (k * r, -k * r), (r, 0), (k * r, k * r), (0, r), (-k * r, k * r), (-r, 0), (-k * r, -k * r)]
    d.polygon([(sp(cx + x), sp(cy + y)) for x, y in pts], fill=color)


def burst(d, cx, cy, r, color, rays=8):
    for i in range(rays):
        ang = 2 * math.pi * i / rays
        line(d, [(cx + 0.38 * r * math.cos(ang), cy + 0.38 * r * math.sin(ang)),
                 (cx + r * math.cos(ang), cy + r * math.sin(ang))], color, 3.2)


def dots(d, x, y, cols, rows, color, gap=14):
    for r_ in range(rows):
        for c_ in range(cols):
            cx, cy = x + c_ * gap, y + r_ * gap
            d.ellipse([sp(cx - 2.3), sp(cy - 2.3), sp(cx + 2.3), sp(cy + 2.3)], fill=color)


def ring(d, cx, cy, r, color, w=2):
    d.ellipse([sp(cx - r), sp(cy - r), sp(cx + r), sp(cy + r)], outline=color, width=sp(w))


def tile(d, x, y, size, color, w=2):
    d.rounded_rectangle([sp(x), sp(y), sp(x + size), sp(y + size)], radius=sp(size * 0.28), outline=color, width=sp(w))


def draw_background(d):
    rect(d, (0, 0, 600, 460), PEACH)                                 # peach block (ends before the text column)
    d.ellipse([sp(930), sp(520), sp(1430), sp(1020)], fill=PEACH)    # big soft circle, bottom right


def draw_decor(d):
    """Small accents in the poster's own colours, kept clear of text and art."""
    dots(d, 60, 26, 4, 5, TAN)
    star(d, 200, 46, 14, SAGE)
    star(d, 300, 28, 8, TERRA)
    ring(d, 395, 52, 7, TAN)
    star(d, 760, 52, 14, TERRA)
    burst(d, 880, 74, 28, TAN)
    star(d, 1030, 40, 9, SAGE)
    dots(d, 1090, 118, 5, 4, TAN)
    tile(d, 1160, 190, 44, TERRA)
    star(d, 1232, 360, 10, TAN)
    star(d, 1214, 440, 7, SAGE)
    star(d, 120, 676, 12, SAGE)
    ring(d, 230, 688, 8, TAN)
    tile(d, 300, 654, 38, TERRA)
    dots(d, 400, 664, 5, 3, TAN)
    ring(d, 1090, 610, 8, SAGE)


def draw_header(d):
    # menu icon (three bars with a dot)
    for y in (42, 54, 66):
        rect(d, (1208, y, 1244, y + 6), INK)
        rect(d, (1250, y, 1256, y + 6), INK)


def wrap(d, text, f, max_w):
    lines, cur = [], ""
    for word in text.split():
        trial = (cur + " " + word).strip()
        if cur and d.textlength(trial, font=f) / S > max_w:
            lines.append(cur)
            cur = word
        else:
            cur = trial
    return lines + [cur]


def gradient_pill(img, box, c1, c2):
    """Horizontal gradient inside a fully rounded pill."""
    x0, y0, x1, y1 = [sp(v) for v in box]
    size = (x1 - x0, y1 - y0)
    grad = Image.linear_gradient("L").rotate(90, expand=True).resize(size)
    fill = Image.composite(Image.new("RGB", size, c2), Image.new("RGB", size, c1), grad)
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size[0] - 1, size[1] - 1], radius=size[1] // 2, fill=255)
    img.paste(fill, (x0, y0), mask)


def draw_content(img, d, a, cy):
    """Title, chips, synopsis box and button, stacked and centred on the art."""
    tf = font("Lora-Italic-Variable.ttf", 76, 700)
    cf = font("Poppins-Bold.ttf", 12.5)
    rf, mf = font("Poppins-Regular.ttf", 14.5), font("Poppins-Medium.ttf", 14.5)
    pad, lh = 18, 24
    hot = {w.lower() for w in a["highlight_words"]}
    lines = wrap(d, a["synopsis"], rf, BOX_W - 2 * pad)
    box_h = len(lines) * lh + 2 * pad - 6
    title_h, chip_h, btn_h = 88 * len(a["title_lines"]) - 10, 34, 44
    total = title_h + 28 + chip_h + 24 + box_h + 26 + btn_h
    y = cy - total / 2 - 22                              # centred on the art panel
    # title: first line dark, last line in the accent colour
    for i, text in enumerate(a["title_lines"]):
        col = TEAL if i < len(a["title_lines"]) - 1 else TERRA
        d.text((sp(TEXT_X), sp(y)), text, font=tf, fill=col, stroke_width=sp(0.5), stroke_fill=col)
        y += 88
    y += 28 - 10 + 22
    # genre chips
    x = TEXT_X
    for tag in a["tags"]:
        w = d.textlength(tag, font=cf) / S + 30
        d.rounded_rectangle([sp(x), sp(y), sp(x + w), sp(y + chip_h)], radius=sp(6),
                            fill=CHIP, outline=CHIP_LINE, width=S)
        d.text((sp(x + 15), sp(y + chip_h / 2)), tag, font=cf, fill=CHIP_TEXT, anchor="lm")
        x += w + 10
    y += chip_h + 24
    # synopsis box with highlighted words
    d.rounded_rectangle([sp(TEXT_X), sp(y), sp(TEXT_X + BOX_W), sp(y + box_h)], radius=sp(6),
                        fill=BOX, outline=BOX_LINE, width=S)
    sw = d.textlength(" ", font=rf) / S
    for i, ln in enumerate(lines):
        wx, wy = TEXT_X + pad, y + pad - 2 + i * lh
        for word in ln.split():
            h = word.strip(".,").lower() in hot
            f = mf if h else rf
            d.text((sp(wx), sp(wy)), word, font=f, fill=TERRA if h else BODY)
            wx += d.textlength(word, font=f) / S + sw
    y += box_h + 26
    # watch-now button: gradient pill, label, long arrow
    bw = 184
    gradient_pill(img, (TEXT_X, y, TEXT_X + bw, y + btn_h), BTN1, BTN2)
    d.text((sp(TEXT_X + 22), sp(y + btn_h / 2)), a["button_text"], font=font("Poppins-Bold.ttf", 13),
           fill=WHITE, anchor="lm")
    ax, ay = TEXT_X + bw - 24, y + btn_h / 2
    line(d, [(ax - 38, ay), (ax, ay)], WHITE, 1.6)
    line(d, [(ax - 6, ay - 5), (ax, ay), (ax - 6, ay + 5)], WHITE, 1.6)


# -------------------------------------------------------------------- main
def build(a=None):
    a = a or source.ANIME
    img = Image.new("RGB", (sp(W), sp(H)), WHITE)
    d = ImageDraw.Draw(img)
    draw_background(d)
    draw_decor(d)
    art, mask, h = art_panel()
    art_y = (H - h) / 2                                    # art centred top-to-bottom
    img.paste(art, (sp(ART_X), sp(art_y)), mask)
    d = ImageDraw.Draw(img)
    draw_header(d)
    draw_content(img, d, a, art_y + h / 2)
    return img.resize((W, H), Image.LANCZOS)
    poster = build()
    poster.save(source.OUTPUT_PNG)
    poster.save(source.OUTPUT_JPG, quality=95)
    print("Saved", source.OUTPUT_PNG, "and", source.OUTPUT_JPG)
