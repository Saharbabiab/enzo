"""
tools/icon_candidates.py - four different directions for the Enzo icon, side by side (writes tools/out/candidates.png).
Pick one, then it gets wired into tools/make_icon.py. Needs Pillow only.
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
SS = 1024
FONTS = Path("C:/Windows/Fonts")


def vgrad(top, bottom):
    g = Image.new("RGB", (1, SS))
    for y in range(SS):
        t = y / (SS - 1)
        g.putpixel((0, y), tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    return g.resize((SS, SS))


def tile(top, bottom, glow):
    bg = vgrad(top, bottom).convert("RGBA")
    spot = Image.new("L", (SS, SS), 0)
    ImageDraw.Draw(spot).ellipse([SS * 0.15, SS * 0.1, SS * 0.85, SS * 0.72], fill=50)
    spot = spot.filter(ImageFilter.GaussianBlur(SS * 0.14))
    bg = Image.alpha_composite(bg, Image.merge("RGBA", (*Image.new("RGB", (SS, SS), glow).split(), spot)))
    mask = Image.new("L", (SS, SS), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, SS - 1, SS - 1], radius=int(SS * 0.225), fill=255)
    out = Image.new("RGBA", (SS, SS), (0, 0, 0, 0))
    out.paste(bg, (0, 0), mask)
    return out


def centerline(radius=17.0, n=300, a0=35, a1=-145):
    """Two joined arcs = a smooth geometric S, centred on (50, 50) in a 100-unit box."""
    c1, c2 = (50.0, 50.0 - radius), (50.0, 50.0 + radius)
    pts, half = [], n // 2
    for i in range(half):
        a = math.radians(a0 + (270 - a0) * i / half)
        pts.append((c1[0] + radius * math.cos(a), c1[1] - radius * math.sin(a)))
    for i in range(half + 1):
        a = math.radians(90 - (90 - a1) * i / half)
        pts.append((c2[0] + radius * math.cos(a), c2[1] - radius * math.sin(a)))
    return pts


def ribbon_mask(width_fn, radius=17.0, a0=35, a1=-145, scale=0.9, round_caps=False):
    pts = centerline(radius, a0=a0, a1=a1)
    n = len(pts) - 1
    u = SS / 100 * scale
    off = SS / 2 - 50 * u
    P = lambda p: (off + p[0] * u, off + p[1] * u)
    left, right = [], []
    for i, (x, y) in enumerate(pts):
        j0, j1 = max(0, i - 1), min(n, i + 1)
        dx, dy = pts[j1][0] - pts[j0][0], pts[j1][1] - pts[j0][1]
        l = math.hypot(dx, dy) or 1
        nx, ny = -dy / l, dx / l
        w = width_fn(i / n) / 2
        left.append((x + nx * w, y + ny * w)); right.append((x - nx * w, y - ny * w))
    poly = left + right[::-1]
    xs, ys = [p[0] for p in poly], [p[1] for p in poly]
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    poly = [(x - cx + 50, y - cy + 50) for x, y in poly]
    m = Image.new("L", (SS, SS), 0)
    d = ImageDraw.Draw(m)
    d.polygon([P(p) for p in poly], fill=255)
    if round_caps:
        for idx in (0, n):
            x, y = pts[idx]
            x, y = x - cx + 50, y - cy + 50
            r = width_fn(idx / n) / 2 * u
            px, py = P((x, y))
            d.ellipse([px - r, py - r, px + r, py + r], fill=255)
    return m


def glyph_mask(font_file, size_frac=0.62):
    f = ImageFont.truetype(str(FONTS / font_file), int(SS * 1.0))
    bbox = f.getbbox("S")
    h = bbox[3] - bbox[1]
    scale = SS * size_frac / h
    f = ImageFont.truetype(str(FONTS / font_file), int(SS * scale))
    bbox = f.getbbox("S")
    m = Image.new("L", (SS, SS), 0)
    ImageDraw.Draw(m).text(((SS - (bbox[2] - bbox[0])) / 2 - bbox[0], (SS - (bbox[3] - bbox[1])) / 2 - bbox[1]), "S", font=f, fill=255)
    return m


def paint(mask, top, bottom, glow_color, glow=0.5):
    layer = Image.new("RGBA", (SS, SS), (0, 0, 0, 0))
    layer.paste(vgrad(top, bottom).convert("RGBA"), (0, 0), mask)
    g = layer.filter(ImageFilter.GaussianBlur(SS * 0.018))
    tint = Image.new("RGBA", (SS, SS), glow_color + (0,))
    tint.putalpha(g.split()[3].point(lambda a: int(a * glow)))
    return Image.alpha_composite(tint, layer)


def hairline(img, color, alpha=90, r=0.42):
    d = ImageDraw.Draw(img)
    d.ellipse([SS * (0.5 - r), SS * (0.5 - r), SS * (0.5 + r), SS * (0.5 + r)], outline=color + (alpha,), width=3)


def make(variant):
    if variant == "A":      # champagne gold monoline S on charcoal
        t = tile((30, 30, 34), (8, 8, 10), (230, 200, 130))
        m = ribbon_mask(lambda x: 7.4, radius=17.5, scale=0.95, round_caps=True)
        art = paint(m, (250, 236, 196), (205, 168, 100), (230, 200, 130), 0.35)
        hairline(art, (222, 190, 120), 70, 0.43)
    elif variant == "B":    # classic serif S, ivory on deep navy
        t = tile((20, 33, 62), (5, 9, 20), (190, 205, 235))
        art = paint(glyph_mask("georgiab.ttf", 0.60), (255, 252, 245), (214, 205, 188), (200, 215, 245), 0.3)
    elif variant == "C":    # soft ribbon S, mint to white on deep teal
        t = tile((12, 46, 48), (2, 14, 16), (110, 230, 200))
        m = ribbon_mask(lambda x: 4.2 + 8.2 * math.sin(math.pi * x) ** 0.8, radius=17.0, scale=0.95, round_caps=True)
        art = paint(m, (236, 255, 247), (120, 226, 186), (110, 230, 200), 0.4)
        hairline(art, (140, 235, 205), 70, 0.43)
    else:                   # D: rose-gold thin monoline S on plum black
        t = tile((44, 24, 48), (10, 6, 14), (255, 190, 170))
        m = ribbon_mask(lambda x: 5.2, radius=18.5, a0=40, a1=-140, scale=0.95, round_caps=True)
        art = paint(m, (255, 226, 212), (226, 160, 140), (255, 190, 170), 0.35)
    return Image.alpha_composite(t, art)


def main():
    out = ROOT / "tools" / "out"
    out.mkdir(exist_ok=True)
    sheet = Image.new("RGBA", (4 * 300 + 5 * 28, 300 + 2 * 28 + 20), (24, 26, 34, 255))
    for i, v in enumerate("ABCD"):
        sheet.alpha_composite(make(v).resize((300, 300), Image.LANCZOS), (28 + i * 328, 28))
        make(v).resize((512, 512), Image.LANCZOS).save(out / f"candidate-{v}.png")
    sheet.save(out / "candidates.png")
    print("ok")


if __name__ == "__main__":
    main()
