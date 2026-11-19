#!/usr/bin/env python3
"""Draw the GlitchCam launcher icon: a torn aperture with RGB ghosting."""

import math
import sys

from PIL import Image, ImageChops, ImageDraw, ImageFilter

S = 400
C = S / 2


def polar(r, deg):
    a = math.radians(deg)
    return (C + r * math.cos(a), C + r * math.sin(a))


def aperture(fill_fn, outline):
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    R, r, n = 160, 64, 7
    step = 360 / n
    for k in range(n):
        a = k * step - 90
        pts = [polar(R, a), polar(R, a + step + 4), polar(r, a + step + 52), polar(r * 0.92, a + 40)]
        d.polygon(pts, fill=fill_fn(k), outline=outline, width=9)
    return im


def main(out_png, size):
    def blade(k):
        t = (k * 3 % 7) / 6
        return (int(30 + 140 * t), int(150 + 75 * t), int(215 + 30 * t), 255)

    base = aperture(blade, (8, 18, 40, 255))
    # chrome highlight: soft white glow toward the top-left
    glow = Image.new("L", (S, S), 0)
    ImageDraw.Draw(glow).ellipse([30, 20, 250, 220], fill=70)
    glow = glow.filter(ImageFilter.GaussianBlur(50))
    white = Image.new("RGBA", (S, S), (255, 255, 255, 0))
    white.putalpha(ImageChops.multiply(glow, base.getchannel("A")))
    base = Image.alpha_composite(base, white)

    # chromatic ghosts
    a = base.getchannel("A")
    mag = Image.new("RGBA", (S, S), (224, 36, 90, 0))
    mag.putalpha(a.point(lambda v: v * 0.9))
    blu = Image.new("RGBA", (S, S), (48, 184, 224, 0))
    blu.putalpha(a.point(lambda v: v * 0.9))
    canvas = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    canvas.alpha_composite(mag, (-14, 0))
    canvas.alpha_composite(blu, (14, 0))
    canvas.alpha_composite(base)

    # tear: shift a few horizontal bands sideways
    torn = canvas.copy()
    for y0, y1, dx in ((96, 128, 34), (210, 226, -26), (288, 300, 18)):
        band = canvas.crop((0, y0, S, y1))
        torn.paste((0, 0, 0, 0), (0, y0, S, y1))
        torn.alpha_composite(band, (dx, y0))
    torn.resize((size, size), Image.LANCZOS).save(out_png)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "icon.png",
         int(sys.argv[2]) if len(sys.argv) > 2 else 100)
