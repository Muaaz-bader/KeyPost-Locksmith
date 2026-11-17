"""Generate the KeyPost brand assets (icon, tray logo, wordmark preview).

Run:  python tools/make_brand_assets.py
Writes assets/app.ico, assets/app-logo.png, assets/logo-wordmark.png and
web/static/img/logo.svg. The SVG and the bitmaps share the same geometry.
"""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
STEEL = '#1c2733'
BRASS = '#d9a441'
BRASS_DARK = '#a8761f'
CANVAS = 512  # design grid

# Keyhole geometry on the 512 grid.
HOLE_CENTER, HOLE_R = (256, 222), 50
HOLE_SLOT = [(240, 236), (272, 236), (294, 352), (218, 352)]

SVG = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" role="img" aria-label="KeyPost">
  <rect width="512" height="512" rx="112" fill="{STEEL}"/>
  <circle cx="256" cy="256" r="176" fill="{BRASS}"/>
  <circle cx="256" cy="256" r="146" fill="none" stroke="{BRASS_DARK}" stroke-width="12"/>
  <circle cx="{HOLE_CENTER[0]}" cy="{HOLE_CENTER[1]}" r="{HOLE_R}" fill="{STEEL}"/>
  <polygon points="{' '.join(f'{x},{y}' for x, y in HOLE_SLOT)}" fill="{STEEL}" stroke="{STEEL}" stroke-width="10" stroke-linejoin="round"/>
</svg>
'''


def draw_icon(size):
    scale = 4  # supersample, then downscale for clean edges
    px = size * scale
    k = px / CANVAS
    img = Image.new('RGBA', (px, px), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, px - 1, px - 1), radius=112 * k, fill=STEEL)
    d.ellipse([(256 - 176) * k, (256 - 176) * k, (256 + 176) * k, (256 + 176) * k], fill=BRASS)
    if size >= 32:
        w = 12 * k
        r = 146 * k
        c = 256 * k
        d.ellipse([c - r - w / 2, c - r - w / 2, c + r + w / 2, c + r + w / 2], outline=BRASS_DARK, width=round(w))
    cx, cy = HOLE_CENTER
    d.ellipse([(cx - HOLE_R) * k, (cy - HOLE_R) * k, (cx + HOLE_R) * k, (cy + HOLE_R) * k], fill=STEEL)
    d.polygon([(x * k, y * k) for x, y in HOLE_SLOT], fill=STEEL)
    return img.resize((size, size), Image.LANCZOS)


def font(names, size):
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def draw_wordmark():
    icon = draw_icon(220)
    img = Image.new('RGBA', (900, 280), (255, 255, 255, 255))
    img.paste(icon, (30, 30), icon)
    d = ImageDraw.Draw(img)
    bold = font(['C:/Windows/Fonts/segoeuib.ttf', 'arialbd.ttf', 'DejaVuSans-Bold.ttf'], 110)
    small = font(['C:/Windows/Fonts/segoeui.ttf', 'arial.ttf', 'DejaVuSans.ttf'], 34)
    d.text((285, 48), 'Key', font=bold, fill=STEEL)
    key_w = d.textlength('Key', font=bold)
    d.text((285 + key_w, 48), 'Post', font=bold, fill=BRASS_DARK)
    d.text((290, 190), 'LOCKSMITH AUTOMATION', font=small, fill='#5b6775')
    return img


def main():
    assets = ROOT / 'assets'
    static_img = ROOT / 'web/static/img'
    static_img.mkdir(parents=True, exist_ok=True)
    (static_img / 'logo.svg').write_text(SVG, encoding='utf-8')
    sizes = [16, 24, 32, 48, 64, 128, 256]
    big = draw_icon(256)
    big.save(assets / 'app.ico', sizes=[(s, s) for s in sizes],
             append_images=[draw_icon(s) for s in sizes[:-1]])
    big.save(assets / 'app-logo.png')
    big.save(static_img / 'logo-256.png')
    draw_wordmark().save(assets / 'logo-wordmark.png')
    print('Brand assets written.')


if __name__ == '__main__':
    main()
