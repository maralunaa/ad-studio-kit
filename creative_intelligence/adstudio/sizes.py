"""
Fit an image to another ad size without cropping the subject (T5 fan-out).

The whole image is kept, scaled to fit inside the new frame and centred; the space around it
is filled with a soft, blurred, slightly darkened extension of the same picture, so a 4:5 shot
becomes a clean 9:16 or 1:1 canvas. Templates then lay the copy out for that size.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageEnhance, ImageFilter

WIDTH = 1080
RATIOS = {"1:1": (1, 1), "4:5": (4, 5), "9:16": (9, 16)}


def fit_canvas(src: str, aspect: str, out: str) -> str:
    a, b = RATIOS[aspect]
    W, H = WIDTH, round(WIDTH * b / a)
    im = Image.open(src).convert("RGB")
    cover = max(W / im.width, H / im.height)
    bg = im.resize((round(im.width * cover), round(im.height * cover)))
    bg = bg.crop(((bg.width - W) // 2, (bg.height - H) // 2, (bg.width - W) // 2 + W, (bg.height - H) // 2 + H))
    bg = ImageEnhance.Brightness(bg.filter(ImageFilter.GaussianBlur(40))).enhance(0.8)
    fit = min(W / im.width, H / im.height)
    fg = im.resize((round(im.width * fit), round(im.height * fit)))
    bg.paste(fg, ((W - fg.width) // 2, (H - fg.height) // 2))
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    bg.save(out, "PNG")
    return out
