import os
from PIL import Image, ImageDraw, ImageFont, ImageFilter

# ---------------------------------------------------------------- settings
S = 2                                   # draw at 2x, shrink at the end
W, H = 1280, 720
CARD_INSET, CARD_RADIUS = 10, 24

BG = (9, 9, 13)
CARD_TOP, CARD_BOTTOM = (26, 27, 35), (15, 16, 21)
PANEL = (27, 28, 37)
LINE = (52, 53, 66)
ORANGE = (240, 112, 24)
WHITE = (255, 255, 255)
GREY = (160, 163, 176)
DIM = (110, 113, 126)

# grid
LEFT_X, LEFT_W = 64, 336
MID_Y = H / 2                            # every column is centred on this line
SLOPE = 0.12                             # lean shared by the stripe and the art
ART_TOP, ART_HALF = 0, 165               # art runs from the very top; half-width at mid
INFO_X, INFO_W = 880, 336               # right column: info cards
NAV_Y = 52

# ------------------------------------------------------------------- fonts
FONT_DIRS = ["/usr/share/fonts/truetype/google-fonts/",
             "/usr/share/fonts/truetype/dejavu/",
             os.path.expanduser("~/.fonts/"), "fonts/"]
FALLBACKS = {"Poppins-Bold.ttf": "DejaVuSans-Bold.ttf",
             "Poppins-Medium.ttf": "DejaVuSans-Bold.ttf",
             "Poppins-Regular.ttf": "DejaVuSans.ttf",
             "Lora-Italic-Variable.ttf": "Poppins-MediumItalic.ttf"}


def font(name, size, weight=None):
    """Load a font at `size` (1x units). `weight` is for variable fonts (e.g. Lora)."""
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
class Canvas:
    def __init__(self):
        size = (sp(W), sp(H))
        grad = Image.linear_gradient("L").resize(size)
        self.img = Image.composite(Image.new("RGB", size, CARD_BOTTOM),
                                   Image.new("RGB", size, CARD_TOP), grad)
        self.d = ImageDraw.Draw(self.img)

    def refresh(self):
        self.d = ImageDraw.Draw(self.img)

    def width(self, text, f, gap=0):
        return self.d.textlength(text, font=f) / S + gap * len(text)

    def text(self, x, y, t, f, fill, gap=0, anchor="la"):
        """Text in 1x units; optional letter gap. Anchor 'la' or 'ra'."""
        if anchor == "ra":
            x -= self.width(t, f, gap) - gap
        if not gap:
            self.d.text((sp(x), sp(y)), t, font=f, fill=fill)
            return
        for ch in t:
            self.d.text((sp(x), sp(y)), ch, font=f, fill=fill)
            x += self.d.textlength(ch, font=f) / S + gap

    def wrap(self, text, f, max_w):
        lines, cur = [], ""
        for word in text.split():
            trial = (cur + " " + word).strip()
            if cur and self.width(trial, f) > max_w:
                lines.append(cur)
                cur = word
            else:
                cur = trial
        return lines + [cur]

    def rrect(self, box, r, fill=None, outline=None, width=1):
        x0, y0, x1, y1 = box
        self.d.rounded_rectangle([sp(x0), sp(y0), sp(x1), sp(y1)], radius=sp(r),
                                 fill=fill, outline=outline, width=sp(width))

    def shadow(self, box, r, dy=10, blur=14, alpha=120, color=(0, 0, 0)):
        x0, y0, x1, y1 = box
        m = Image.new("L", self.img.size, 0)
        ImageDraw.Draw(m).rounded_rectangle([sp(x0), sp(y0 + dy), sp(x1), sp(y1 + dy)],
                                            radius=sp(r), fill=alpha)
        self.img.paste(color, mask=m.filter(ImageFilter.GaussianBlur(sp(blur))))
        self.refresh()

    def paste_round(self, art, x, y, r):
        m = Image.new("L", art.size, 0)
        ImageDraw.Draw(m).rounded_rectangle([0, 0, art.width - 1, art.height - 1],
                                            radius=sp(r), fill=255)
        self.img.paste(art, (sp(x), sp(y)), m)
        self.refresh()


def crop_art(box, width):
    """Crop a region from the screenshot, resize to `width` (1x units)."""
    im = Image.open(source.ART_SOURCE).convert("RGB")
    rw, rh = source.ART_REF_SIZE
    fx, fy = im.width / rw, im.height / rh
    l, t, r, b = box
    art = im.crop((int(l * fx), int(t * fy), int(r * fx), int(b * fy)))
    h = width * art.height / art.width
    return art.resize((sp(width), sp(h)), Image.LANCZOS), h


# ------------------------------------------------------------------ pieces
def draw_slash(c):
    """Slanted orange stripe centred on the middle of the poster."""
    cx, slope, half = W / 2, SLOPE, 60
    pts = [(cx + slope * (MID_Y - y) + dx, y) for y, dx in ((0, -half), (0, half), (H, half), (H, -half))]
    m = Image.new("L", c.img.size, 0)
    ImageDraw.Draw(m).polygon([(sp(x), sp(y)) for x, y in pts], fill=255)
    grad = Image.linear_gradient("L").resize(c.img.size)
    fill = Image.composite(Image.new("RGB", c.img.size, (222, 92, 14)),
                           Image.new("RGB", c.img.size, (255, 138, 38)), grad)
    c.img.paste(fill, mask=m)
    c.refresh()


def draw_nav(c, a):
    logo = font("Poppins-Bold.ttf", 21)
    c.text(LEFT_X, NAV_Y - 17, a["brand"][0], logo, ORANGE)
    c.text(LEFT_X + c.width(a["brand"][0], logo), NAV_Y - 17, a["brand"][1], logo, WHITE)
    f, x = font("Poppins-Bold.ttf", 9.5), 198         # clear gap after the logo; links stay clear of the art
    for link in a["nav_links"]:
        on = link == a["nav_active"]
        c.text(x, NAV_Y - 8, link, f, WHITE if on else DIM, gap=0.8)
        w = c.width(link, f, 0.8)
        if on:
            c.rrect((x, NAV_Y + 12, x + 18, NAV_Y + 14), 1, fill=ORANGE)
        x += w + 11
    # search + menu icons (right edge aligned with the info column)
    sx = INFO_X + INFO_W - 44
    c.d.ellipse([sp(sx - 6), sp(NAV_Y - 7), sp(sx + 6), sp(NAV_Y + 5)], outline=GREY, width=sp(2))
    c.d.line([sp(sx + 5), sp(NAV_Y + 4), sp(sx + 10), sp(NAV_Y + 9)], fill=GREY, width=sp(2))
    mx = INFO_X + INFO_W - 18
    for i in range(3):
        c.rrect((mx, NAV_Y - 7 + i * 6, mx + 18, NAV_Y - 5 + i * 6), 1, fill=GREY)


def draw_left(c, a):
    tf, bf = font("Poppins-Bold.ttf", 30), font("Lora-Italic-Variable.ttf", 15.5, 500)
    title = c.wrap(a["title"].upper(), tf, LEFT_W)
    pad_x, pad_y, lh = 26, 15, 25
    syn = c.wrap(a["synopsis"], bf, LEFT_W - pad_x - 18)
    box_h = len(syn) * lh + 2 * pad_y - 2
    total = 28 + 22 + len(title) * 37 + 16 + box_h + 28 + 46
    y = MID_Y - total / 2                             # centred on the middle line
    # genre pills (fully rounded boxes)
    tg, px = font("Poppins-Bold.ttf", 10.5), LEFT_X
    for tag in a["tags"]:
        w = c.width(tag, tg, 1.4) + 30
        c.rrect((px, y, px + w, y + 28), 14, fill=(36, 37, 48), outline=(92, 70, 52), width=1)
        c.text(px + 15, y + 6.5, tag, tg, (244, 190, 150), gap=1.4)
        px += w + 10
    y += 28 + 22
    for line in title:
        c.d.text((sp(LEFT_X), sp(y)), line, font=tf, fill=WHITE,
                 stroke_width=sp(0.6), stroke_fill=WHITE)
        y += 37
    y += 16
    # synopsis box: soft panel with an orange accent bar
    c.shadow((LEFT_X, y, LEFT_X + LEFT_W, y + box_h), 14, dy=6, blur=10, alpha=90)
    c.rrect((LEFT_X, y, LEFT_X + LEFT_W, y + box_h), 14, fill=(29, 30, 40), outline=LINE, width=1)
    c.rrect((LEFT_X + 12, y + 14, LEFT_X + 15, y + box_h - 14), 1.5, fill=ORANGE)
    for i, line in enumerate(syn):
        c.text(LEFT_X + pad_x, y + pad_y - 2 + i * lh, line, bf, (214, 217, 226))
    y += box_h
    y += 28
    # watch button (soft glow) + bookmark button
    bw, bh = 176, 46
    c.shadow((LEFT_X, y, LEFT_X + bw, y + bh), 23, dy=5, blur=7, alpha=45, color=ORANGE)
    c.rrect((LEFT_X, y, LEFT_X + bw, y + bh), 23, fill=ORANGE)
    cy = y + bh / 2
    c.d.polygon([(sp(LEFT_X + 24), sp(cy - 7)), (sp(LEFT_X + 24), sp(cy + 7)),
                 (sp(LEFT_X + 35), sp(cy))], fill=WHITE)
    c.text(LEFT_X + 46, cy - 9, a["button_text"], font("Poppins-Bold.ttf", 13), WHITE, gap=1)
    bx = LEFT_X + bw + 14
    c.d.ellipse([sp(bx), sp(y), sp(bx + bh), sp(y + bh)], fill=(34, 35, 45), outline=LINE, width=sp(1.5))
    mx = bx + bh / 2
    c.d.polygon([(sp(mx - 6), sp(cy - 9)), (sp(mx + 6), sp(cy - 9)), (sp(mx + 6), sp(cy + 9)),
                 (sp(mx), sp(cy + 4)), (sp(mx - 6), sp(cy + 9))], fill=ORANGE)


def draw_glow(c):
    """Soft orange light in the lower-left corner and a faint one top-right."""
    for (cx, cy, r, k) in [(90, 700, 280, 0.16), (1180, 30, 200, 0.07)]:
        m = Image.new("L", c.img.size, 0)
        ImageDraw.Draw(m).ellipse([sp(cx - r), sp(cy - r), sp(cx + r), sp(cy + r)], fill=255)
        m = m.filter(ImageFilter.GaussianBlur(sp(r / 2.2))).point(lambda v, k=k: int(v * k))
        c.img.paste(ORANGE, mask=m)
    c.refresh()


def draw_footer(c):
    """Small finishing touches: social buttons (left) and slider controls (right)."""
    cy = 658
    c.text(LEFT_X, cy - 28, "FOLLOW US", font("Poppins-Bold.ttf", 8.5), DIM, gap=1.4)
    f = font("Poppins-Bold.ttf", 10.5)
    for i, ch in enumerate(["f", "X", "in"]):
        x0 = LEFT_X + i * 40
        c.d.ellipse([sp(x0), sp(cy - 14), sp(x0 + 30), sp(cy + 16)], fill=(34, 35, 45), outline=LINE, width=sp(1.2))
        c.d.text((sp(x0 + 15), sp(cy + 1)), ch, font=f, fill=GREY, anchor="mm")
    # slider counter + progress track
    x = INFO_X
    c.text(x, cy - 9, "01", font("Poppins-Bold.ttf", 15), WHITE)
    c.rrect((x + 32, cy - 1, x + 122, cy + 1), 1, fill=LINE)
    c.rrect((x + 32, cy - 1, x + 54, cy + 1), 1, fill=ORANGE)
    c.text(x + 132, cy - 7, "05", font("Poppins-Bold.ttf", 12), DIM)
    # prev / next arrows
    for i, (bx, d) in enumerate([(INFO_X + INFO_W - 76, -1), (INFO_X + INFO_W - 34, 1)]):
        on = d == 1
        c.d.ellipse([sp(bx), sp(cy - 17), sp(bx + 34), sp(cy + 17)],
                    fill=ORANGE if on else (34, 35, 45), outline=None if on else LINE, width=sp(1.2))
        mx, col = bx + 17, WHITE if on else GREY
        c.d.line([sp(mx - 3 * d), sp(cy - 6), sp(mx + 3 * d), sp(cy), sp(mx - 3 * d), sp(cy + 6)],
                 fill=col, width=sp(2), joint="curve")


def para(half, y0=ART_TOP, y1=H):
    """Slanted parallelogram (same lean as the stripe), centred on the poster."""
    cx = W / 2
    xa, xb = cx + SLOPE * (MID_Y - y0), cx + SLOPE * (MID_Y - y1)
    return [(xa - half, y0), (xa + half, y0), (xb + half, y1), (xb - half, y1)]


def poly_mask(c, pts):
    m = Image.new("L", c.img.size, 0)
    ImageDraw.Draw(m).polygon([(sp(x), sp(y)) for x, y in pts], fill=255)
    return m


def draw_art(c):
    """Hero art cut to a slanted panel, framed by a thin orange edge."""
    xs = [x for x, _ in para(ART_HALF)]
    bbox_w, bbox_cx = max(xs) - min(xs), (max(xs) + min(xs)) / 2
    h = H - ART_TOP
    art, ah = crop_art(source.HERO_CROP, max(bbox_w, h * (source.HERO_CROP[2] - source.HERO_CROP[0]) / (source.HERO_CROP[3] - source.HERO_CROP[1])))   # cover the whole shape
    # shadow, orange frame, then the art itself
    sh = poly_mask(c, para(ART_HALF + 7)).filter(ImageFilter.GaussianBlur(sp(18)))
    c.img.paste((0, 0, 0), mask=sh.point(lambda v: int(v * 0.7)))
    c.img.paste(ORANGE, mask=poly_mask(c, para(ART_HALF + 6)))
    layer = Image.new("RGB", c.img.size, (0, 0, 0))
    frac = getattr(source, "HERO_ALIGN", 0.0)           # 0 = left-aligned, 0.5 = centred
    left = min(xs) - (art.width / S - bbox_w) * frac
    layer.paste(art, (sp(left), sp(ART_TOP)))
    c.img.paste(layer, mask=poly_mask(c, para(ART_HALF)))
    c.refresh()


def draw_right(c, a):
    top = MID_Y - 210 + 30                  # block (label..rating card) is 420 tall, centred
    c.text(INFO_X + INFO_W, top - 30, a["side_label"], font("Poppins-Bold.ttf", 11),
           ORANGE, gap=1.2, anchor="ra")
    # character card
    cx0, cy0, ch = INFO_X, top, 190
    c.shadow((cx0, cy0, cx0 + INFO_W, cy0 + ch), 18, dy=8, blur=12, alpha=110)
    c.rrect((cx0, cy0, cx0 + INFO_W, cy0 + ch), 18, fill=PANEL, outline=LINE, width=1)
    av, avh = crop_art(source.AVATAR_CROP, 112)
    c.paste_round(av, cx0 + 20, cy0 + (ch - avh) / 2, 14)
    tx = cx0 + 20 + 112 + 18
    c.text(tx, cy0 + ch / 2 - 22, a["character_name"], font("Poppins-Bold.ttf", 16), WHITE)
    c.text(tx, cy0 + ch / 2 + 4, a["character_role"], font("Poppins-Bold.ttf", 8.5), DIM, gap=0.8)
    # pagination dots, centred under the card
    dx, dy = cx0 + INFO_W / 2, cy0 + ch + 20
    c.rrect((dx - 15, dy - 2.5, dx + 3, dy + 2.5), 2.5, fill=ORANGE)
    for off in (11, 22):
        c.d.ellipse([sp(dx + off - 2.5), sp(dy - 2.5), sp(dx + off + 2.5), sp(dy + 2.5)], fill=LINE)
    # rating card
    ry, rh = dy + 30, 150
    c.shadow((cx0, ry, cx0 + INFO_W, ry + rh), 18, dy=8, blur=12, alpha=110)
    c.rrect((cx0, ry, cx0 + INFO_W, ry + rh), 18, fill=PANEL, outline=LINE, width=1)
    c.text(cx0 + 24, ry + 16, "RATING", font("Poppins-Bold.ttf", 8.5), DIM, gap=1)
    rcx, rcy, rr = cx0 + 24 + 38, ry + 88, 38
    box = [sp(rcx - rr), sp(rcy - rr), sp(rcx + rr), sp(rcy + rr)]
    c.d.arc(box, 0, 360, fill=(48, 49, 60), width=sp(7))
    c.d.arc(box, -90, -90 + 360 * a["rating"] / 10, fill=ORANGE, width=sp(7))
    num = font("Poppins-Bold.ttf", 18)
    nw = c.width(f"{a['rating']:.2f}", num)
    c.text(rcx - nw / 2, rcy - 15, f"{a['rating']:.2f}", num, WHITE)
    c.text(rcx - 7, rcy + 9, "/10", font("Poppins-Bold.ttf", 8), DIM)
    c.d.line([sp(cx0 + 118), sp(ry + 22), sp(cx0 + 118), sp(ry + rh - 22)], fill=LINE, width=sp(1))
    chip_f = font("Poppins-Bold.ttf", 9.5)
    y = ry + 22
    for label, items in (("GENRE", a["genres"]), ("THEME", a["themes"])):
        c.text(cx0 + 134, y, label, font("Poppins-Bold.ttf", 8), DIM, gap=1)
        x, y2 = cx0 + 134, y + 17
        for item in items:
            w = c.width(item, chip_f) + 20
            c.rrect((x, y2, x + w, y2 + 22), 11, fill=(36, 37, 48), outline=LINE, width=1)
            c.text(x + 10, y2 + 4, item, chip_f, WHITE)
            x += w + 6
        y += 56


# -------------------------------------------------------------------- main
def build(a=None):
    a = a or source.ANIME
    c = Canvas()
    draw_glow(c)
    draw_slash(c)
    draw_nav(c, a)
    draw_left(c, a)
    if source.SHOW_HERO:
        draw_art(c)
    draw_right(c, a)
    draw_footer(c)
    # clip everything to a rounded card on a darker page background
    out = Image.new("RGB", c.img.size, BG)
    m = Image.new("L", c.img.size, 0)
    ImageDraw.Draw(m).rounded_rectangle(
        [sp(CARD_INSET), sp(CARD_INSET), sp(W - CARD_INSET), sp(H - CARD_INSET)],
        radius=sp(CARD_RADIUS), fill=255)
    out.paste(c.img, mask=m)
    return out.resize((W, H), Image.LANCZOS)
  
    poster = build()
    poster.save(source.OUTPUT_PNG)
    poster.save(source.OUTPUT_JPG, quality=95)
    print("Saved", source.OUTPUT_PNG, "and", source.OUTPUT_JPG)
