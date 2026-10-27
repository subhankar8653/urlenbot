import os
from PIL import Image, ImageDraw, ImageFont, ImageFilter

# ---------------------------------------------------------------- settings
SCALE = 2                      # draw at 2x, then shrink for smooth edges
WIDTH, HEIGHT = 1280, 720
MARGIN = 64                    # left edge shared by all text elements

NAVY = (42, 43, 56)
RED = (232, 32, 63)
CHIP = (240, 240, 240)
CHIP_TEXT = (138, 141, 152)
BOX = (241, 241, 241)
PILL_FILL = (252, 228, 233)
PILL_BORDER = (246, 190, 200)
DOT_PINK = (244, 122, 142)

# ------------------------------------------------------------------- fonts
FONT_DIRS = [
    "/usr/share/fonts/truetype/google-fonts/",
    "/usr/share/fonts/truetype/dejavu/",
    os.path.expanduser("~/.fonts/"),
    "fonts/",
]
FALLBACKS = {
    "Poppins-Bold.ttf": "DejaVuSans-Bold.ttf",
    "Poppins-Medium.ttf": "DejaVuSans-Bold.ttf",
    "Poppins-Regular.ttf": "DejaVuSans.ttf",
}


def get_font(name, size):
    """Load a font by file name at `size` (1x units); try the preferred
    font first, then its fallback, then PIL's default."""
    px = int(size * SCALE)
    for candidate in (name, FALLBACKS.get(name)):
        if not candidate:
            continue
        for folder in FONT_DIRS:
            path = os.path.join(folder, candidate)
            if os.path.exists(path):
                return ImageFont.truetype(path, px)
    return ImageFont.load_default()


def sp(v):
    """Convert 1x units to pixels on the 2x canvas."""
    return int(v * SCALE)


# ----------------------------------------------------------------- helpers
class Canvas:
    def __init__(self):
        self.img = Image.new("RGB", (sp(WIDTH), sp(HEIGHT)), "white")
        self.d = ImageDraw.Draw(self.img)

    def refresh(self):
        self.d = ImageDraw.Draw(self.img)

    def shadow(self, box, radius, offset, blur, strength, color=(0, 0, 0)):
        x0, y0, x1, y1 = box
        mask = Image.new("L", self.img.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            [sp(x0), sp(y0 + offset), sp(x1), sp(y1 + offset)],
            radius=sp(radius), fill=strength)
        mask = mask.filter(ImageFilter.GaussianBlur(sp(blur)))
        self.img.paste(color, mask=mask)
        self.refresh()

    def spaced_text(self, x, y, text, font, fill, gap):
        """Draw text with extra letter spacing (x, y already in pixels)."""
        for ch in text:
            self.d.text((x, y), ch, font=font, fill=fill)
            x += self.d.textlength(ch, font=font) + sp(gap)

    def x_mark(self, cx, cy, r, color, w=3.4):
        self.d.line([sp(cx - r), sp(cy - r), sp(cx + r), sp(cy + r)], fill=color, width=sp(w))
        self.d.line([sp(cx - r), sp(cy + r), sp(cx + r), sp(cy - r)], fill=color, width=sp(w))


def load_poster(width):
    """Crop the poster out of the source screenshot and resize it."""
    src = Image.open(source.POSTER_SOURCE).convert("RGB")
    fx, fy = src.width / 1280, src.height / 720
    l, t, r, b = source.POSTER_CROP
    art = src.crop((int(l * fx), int(t * fy), int(r * fx), int(b * fy)))
    height = int(width * art.height / art.width)
    return art.resize((sp(width), sp(height)), Image.LANCZOS), height


def wrap_words(c, text, highlights, fonts, max_width):
    """Split text into lines of (word, is_highlight) that fit max_width."""
    regular, medium = fonts
    space = c.d.textlength(" ", font=regular) / SCALE
    lines, cur, cur_w = [], [], 0
    for word in text.split():
        hot = word.strip(".,\u2026").lower() in highlights
        w = c.d.textlength(word, font=medium if hot else regular) / SCALE
        if cur and cur_w + space + w > max_width:
            lines.append(cur)
            cur, cur_w = [], 0
        cur_w += (space if cur else 0) + w
        cur.append((word, hot))
    lines.append(cur)
    return lines, space


# ------------------------------------------------------------ poster parts
def draw_artwork(c):
    art, h = load_poster(380)
    px, py = 832, (HEIGHT - h) // 2
    # red block behind the poster
    c.d.rectangle([sp(730), sp(py + h * 0.55), sp(1268), sp(py + h + 22)], fill=RED)
    c.shadow((px, py, px + 380, py + h), radius=18, offset=12, blur=16, strength=110)
    mask = Image.new("L", art.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, art.width - 1, art.height - 1], radius=sp(18), fill=255)
    c.img.paste(art, (sp(px), sp(py)), mask)
    c.refresh()
    # column of black dots on the red block
    for i in range(5):
        cy = py + h * 0.56 + i * 28
        c.d.ellipse([sp(1240), sp(cy), sp(1254), sp(cy + 14)], fill=(25, 25, 30))


def draw_decorations(c):
    # hamburger menu
    for i in range(3):
        c.d.rounded_rectangle([sp(MARGIN), sp(30 + i * 10), sp(MARGIN + 40), sp(34 + i * 10)],
                              radius=sp(2), fill=NAVY)
    # x marks
    for x, y, col in [(800, 95, RED), (640, 62, NAVY), (735, 655, RED), (1050, 672, NAVY)]:
        c.x_mark(x, y, 8, col)
    # zigzag
    zig = [(430 + i * 10, 668 + (-7 if i % 2 else 0)) for i in range(9)]
    c.d.line([(sp(x), sp(y)) for x, y in zig], fill=RED, width=sp(3.2), joint="curve")
    # dotted grid
    for r in range(5):
        for k in range(7):
            cx, cy = 1112 + k * 17, 638 + r * 14
            c.d.ellipse([sp(cx - 3), sp(cy - 3), sp(cx + 3), sp(cy + 3)], fill=DOT_PINK)


def draw_text_block(c, a):
    y0 = 92
    # season / episodes pill
    f = get_font("Poppins-Bold.ttf", 13)
    gap = 2.4
    text = a["season_info"]
    tw = sum(c.d.textlength(ch, font=f) / SCALE + gap for ch in text) - gap
    pw, ph = tw + 36, 34
    c.d.rounded_rectangle([sp(MARGIN), sp(y0 - 6), sp(MARGIN + pw), sp(y0 - 6 + ph)],
                          radius=sp(ph / 2), fill=PILL_FILL, outline=PILL_BORDER, width=SCALE)
    c.spaced_text(sp(MARGIN + 18), sp(y0 - 6 + ph / 2) - sp(9.5), text, f, RED, gap)

    # title (a stroke thickens Poppins Bold into a chunky display face)
    tf = get_font("Poppins-Bold.ttf", 66)
    for dy, line, col in [(32, a["title_line1"], NAVY), (104, a["title_line2"], RED)]:
        c.d.text((sp(MARGIN), sp(y0 + dy)), line, font=tf, fill=col,
                 stroke_width=sp(1.6), stroke_fill=col)

    # studio
    c.spaced_text(sp(MARGIN), sp(y0 + 192), a["studio"],
                  get_font("Poppins-Bold.ttf", 19), NAVY, 1.5)

    # genre chips
    cf = get_font("Poppins-Bold.ttf", 13)
    cx, cy = MARGIN, y0 + 232
    for tag in a["tags"]:
        w = c.d.textlength(tag, font=cf) / SCALE + 34
        c.d.rounded_rectangle([sp(cx), sp(cy), sp(cx + w), sp(cy + 36)], radius=sp(6),
                              fill=CHIP, outline=(225, 225, 225), width=SCALE)
        c.d.text((sp(cx + 17), sp(cy + 18)), tag, font=cf, fill=CHIP_TEXT, anchor="lm")
        cx += w + 12

    # synopsis box (kept narrower than the red block so they never overlap)
    regular, medium = get_font("Poppins-Regular.ttf", 16), get_font("Poppins-Medium.ttf", 16)
    box_x, box_y, box_w, pad, lh = MARGIN, cy + 54, 610, 18, 28
    hot = {w.lower() for w in a["highlight_words"]}
    lines, space = wrap_words(c, a["synopsis"], hot, (regular, medium), box_w - 2 * pad)
    box_h = len(lines) * lh + 2 * pad - 6
    c.d.rounded_rectangle([sp(box_x), sp(box_y), sp(box_x + box_w), sp(box_y + box_h)],
                          radius=sp(6), fill=BOX, outline=(226, 226, 226), width=SCALE)
    for i, line in enumerate(lines):
        x, y = box_x + pad, box_y + pad + i * lh - 2
        for word, is_hot in line:
            font = medium if is_hot else regular
            c.d.text((sp(x), sp(y)), word, font=font, fill=RED if is_hot else NAVY)
            x += c.d.textlength(word, font=font) / SCALE + space

    draw_button(c, a["button_text"], box_y + box_h + 28)


def draw_button(c, label, top):
    w, h = 214, 54
    c.shadow((MARGIN, top, MARGIN + w, top + h), radius=27, offset=5, blur=7,
             strength=45, color=RED)
    c.d.rounded_rectangle([sp(MARGIN), sp(top), sp(MARGIN + w), sp(top + h)],
                          radius=sp(27), fill=RED)
    c.d.text((sp(MARGIN + 26), sp(top + h / 2)), label,
             font=get_font("Poppins-Bold.ttf", 16), fill="white", anchor="lm")
    # white circle with an arrow
    cx, cy = MARGIN + w - 28, top + h / 2
    c.d.ellipse([sp(cx - 17), sp(cy - 17), sp(cx + 17), sp(cy + 17)], fill="white")
    for end in [(cx - 7, cy, cx + 7, cy), (cx + 1, cy - 6, cx + 7, cy), (cx + 1, cy + 6, cx + 7, cy)]:
        c.d.line([sp(end[0]), sp(end[1]), sp(end[2]), sp(end[3])], fill=RED, width=sp(2.4))


# -------------------------------------------------------------------- main
def build_poster(anime=None):
    c = Canvas()
    draw_decorations(c)
    draw_artwork(c)
    draw_text_block(c, anime or source.ANIME)
    return c.img.resize((WIDTH, HEIGHT), Image.LANCZOS)


if __name__ == "__main__":
    poster = build_poster()
    poster.save(source.OUTPUT_PNG)
    poster.save(source.OUTPUT_JPG, quality=95)
    print("Saved", source.OUTPUT_PNG, "and", source.OUTPUT_JPG)
