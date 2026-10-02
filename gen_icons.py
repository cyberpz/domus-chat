#!/usr/bin/env python3
"""Genera le icone PWA di DomusChat — stile phosphor terminal."""
from PIL import Image, ImageDraw

BG = (0, 10, 0)
GREEN = (0, 255, 65)


def icon(size):
    s = 4  # supersampling
    n = size * s
    img = Image.new("RGB", (n, n), BG)
    d = ImageDraw.Draw(img)
    m = int(n * 0.14)
    d.rounded_rectangle([m, m, n - m, n - m], radius=int(n * 0.16),
                        outline=GREEN, width=max(2, int(n * 0.035)))
    # prompt ">_" — chevron + underscore
    cx, cy = n * 0.34, n * 0.42
    r = n * 0.13
    w = max(2, int(n * 0.05))
    d.line([(cx - r * 0.7, cy - r), (cx + r * 0.7, cy), (cx - r * 0.7, cy + r)],
           fill=GREEN, width=w, joint="curve")
    y = cy + r * 1.45
    d.line([(cx + r * 1.2, y), (cx + r * 2.3, y)], fill=GREEN, width=w)
    return img.resize((size, size), Image.LANCZOS)


for sz in (192, 512):
    icon(sz).save(f"static/icon-{sz}.png")
    print(f"icon-{sz}.png")
mask = icon(512).convert("RGBA")
mask.save("static/icon-512-maskable.png")
print("icon-512-maskable.png")