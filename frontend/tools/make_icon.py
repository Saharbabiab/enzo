"""Draws the Enzo icon ("Nearly zero": an open blue ring with a brown oopsie dot in the gap, on a #0b0d12 tile)
at every size the app needs. Run from the repo root: python tools/make_icon.py   (needs Pillow).
Geometry matches public/icon-source.svg and the ring in src/App.tsx (100-unit box)."""
import math
from PIL import Image, ImageDraw

TILE = (11, 13, 18)          # #0b0d12
BLUE = (95, 140, 224)        # #5f8ce0
BROWN = (176, 138, 104)      # #b08a68
R, STROKE, DOT_R = 26, 9, 5  # ring radius, stroke width, dot radius (of 100)
START, SWEEP = -40, 360 * .84   # SVG: rotate(-40), stroke-dasharray 84 16 -> the gap sits at the upper right
DOT = (61, 26.4)
RES = 'android/app/src/main/res'
DENSITIES = {'mdpi': 1, 'hdpi': 1.5, 'xhdpi': 2, 'xxhdpi': 3, 'xxxhdpi': 4}


def ring(d: int, blue=BLUE, brown=BROWN) -> Image.Image:
    """The ring alone, on a transparent d x d square (the 100-unit box scaled to d)."""
    D = d * 4                      # draw large, then shrink: smooth edges
    u = D / 100
    im = Image.new('RGBA', (D, D), (0, 0, 0, 0))
    g = ImageDraw.Draw(im)
    c, r, w = 50 * u, R * u, STROKE * u
    g.arc([c - r - w / 2, c - r - w / 2, c + r + w / 2, c + r + w / 2], START, START + SWEEP, fill=blue, width=round(w))
    for a in (START, START + SWEEP):   # round line caps
        x, y = c + r * math.cos(math.radians(a)), c + r * math.sin(math.radians(a))
        g.ellipse([x - w / 2, y - w / 2, x + w / 2, y + w / 2], fill=blue)
    x, y, dr = DOT[0] * u, DOT[1] * u, DOT_R * u
    g.ellipse([x - dr, y - dr, x + dr, y + dr], fill=brown)
    return im.resize((d, d), Image.LANCZOS)


def icon(size: int, scale=1.0, shape='rounded') -> Image.Image:
    """size x size: the tile (rounded 22.5%, square, circle or none) with the ring; scale = ring box / icon size."""
    S = size * 4
    bg = Image.new('RGBA', (S, S), (0, 0, 0, 0))
    g = ImageDraw.Draw(bg)
    if shape == 'rounded':
        g.rounded_rectangle([0, 0, S - 1, S - 1], radius=S * .225, fill=TILE + (255,))
    elif shape == 'square':
        g.rectangle([0, 0, S, S], fill=TILE + (255,))
    elif shape == 'circle':
        g.ellipse([0, 0, S - 1, S - 1], fill=TILE + (255,))
    out = bg.resize((size, size), Image.LANCZOS)
    d = round(size * scale)
    out.alpha_composite(ring(d), ((size - d) // 2, (size - d) // 2))
    return out


def monochrome(size: int, scale: float) -> Image.Image:
    """Android 13 themed icon: only the alpha counts (the system tints it)."""
    out = Image.new('RGBA', (size, size), (255, 255, 255, 0))
    d = round(size * scale)
    out.alpha_composite(ring(d, (255, 255, 255), (255, 255, 255)), ((size - d) // 2, (size - d) // 2))
    return out


for name, f in DENSITIES.items():
    icon(round(48 * f)).save(f'{RES}/mipmap-{name}/ic_launcher.png')
    icon(round(48 * f), shape='circle').save(f'{RES}/mipmap-{name}/ic_launcher_round.png')
    # adaptive layers are 108 dp; launchers show the middle 72 dp (ring box 66% of 108 = the 72 dp tile)
    icon(round(108 * f), .66, shape=None).save(f'{RES}/mipmap-{name}/ic_launcher_foreground.png')
    monochrome(round(108 * f), .66).save(f'{RES}/mipmap-{name}/ic_launcher_monochrome.png')

icon(192).save('public/icons/icon-192.png')
icon(512).save('public/icons/icon-512.png')
icon(512, .8, shape='square').save('public/icons/maskable-512.png')   # maskable: the ring stays well inside the 80% safe circle
icon(180, shape='square').save('public/icons/apple-touch-icon.png')  # iOS rounds the corners itself
icon(64).save('public/icons/favicon-64.png')
open('public/icon-source.svg', 'w', encoding='utf-8').write('''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
  <!-- Enzo icon, "Nearly zero". The PNGs come from tools/make_icon.py. -->
  <rect width="100" height="100" rx="22.5" fill="#0b0d12"/>
  <circle cx="50" cy="50" r="26" fill="none" stroke="#5f8ce0" stroke-width="9" stroke-linecap="round" pathLength="100" stroke-dasharray="84 16" transform="rotate(-40 50 50)"/>
  <circle cx="61" cy="26.4" r="5" fill="#b08a68"/>
</svg>
''')
print('icons written')
