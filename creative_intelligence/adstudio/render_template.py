"""
Draw a template (templates.py) onto a text-free image with PIL.

    img, drawn = render(image_path, "benefit_stack", {"headline": ..., "sub": ..., "cta": ..., "price": ...})

`drawn` maps each field to the exact string that was drawn, so callers (and tests) can
prove the copy on the ad equals the brief / Shopify. Text is only ever shrunk to fit,
never cut. "upper" changes letter case for display only.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from creative_intelligence import knowledge
from creative_intelligence.adstudio.templates import COLORS, safe_area, template_for

FONT_DIR = Path(__file__).resolve().parent / "fonts"
WEIGHTS = {"regular": "Poppins-Regular.ttf", "medium": "Poppins-Medium.ttf",
           "semibold": "Poppins-SemiBold.ttf", "bold": "Poppins-Bold.ttf",
           "hand": "Caveat.ttf"}                     # handwritten letter (SIL OFL, fonts/Caveat-OFL.txt)


def _font(weight: str, px: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_DIR / WEIGHTS.get(weight, WEIGHTS["regular"])), max(10, int(px)))


def aspect_key(w: int, h: int) -> str:
    """Closest supported aspect ratio ('9:16', '4:5' or '1:1')."""
    def ratio(k: str) -> float:
        a, b = k.split(":")
        return int(a) / int(b)
    return min(safe_area(), key=lambda k: abs(w / h - ratio(k)))


def _area(w: int, h: int) -> tuple[float, float, float, float]:
    top, bottom, side = safe_area()[aspect_key(w, h)]
    return side * w, top * h, w - 2 * side * w, (bottom - top) * h


def _px(box: list[float], w: int, h: int, bleed: bool = False) -> tuple[float, float, float, float]:
    ax, ay, aw, ah = _area(w, h)
    x, y, bw, bh = box
    x1, y1, x2, y2 = ax + x * aw, ay + y * ah, ax + (x + bw) * aw, ay + (y + bh) * ah
    if bleed:
        x1, x2 = 0, w
        if y <= 0.001:
            y1 = 0
        if y + bh >= 0.999:
            y2 = h
    return x1, y1, x2, y2


# Text below this contrast (WCAG ratio, as measured for QA legibility) is redrawn to read:
# 4.5 is AA for body text and where legibility_score starts giving 4 of 5.
MIN_CONTRAST = 4.5
LIGHT, DARK = COLORS["white"], COLORS["ink"]


def _pick(name: str, bg) -> tuple[int, int, int]:
    """The template's colour when it reads on `bg`; otherwise (and for "auto") white or ink,
    whichever contrasts more. Fixed colours are preferences, so one layout works on any photo."""
    from creative_intelligence.adstudio.qa import contrast
    if name != "auto":
        pref = COLORS.get(name, LIGHT)
        if contrast(pref, bg) >= MIN_CONTRAST:
            return pref
    return max((LIGHT, DARK), key=lambda c: contrast(c, bg))


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_w: float, tracking: float) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if _width(draw, trial, font, tracking) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _width(draw, text: str, font, tracking: float) -> float:
    return draw.textlength(text, font=font) + tracking * font.size * max(0, len(text) - 1)


def _draw_line(draw, xy, text: str, font, fill, tracking: float) -> None:
    if not tracking:
        draw.text(xy, text, font=font, fill=fill)
        return
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += draw.textlength(ch, font=font) + tracking * font.size


def _fit(draw, text: str, el: dict[str, Any], box, w: int) -> tuple[Any, list[str], float]:
    """Largest size (<= the template's) at which the text fits the box in max_lines."""
    x1, y1, x2, y2 = box
    size = el.get("size", 0.05) * w
    tracking = el.get("tracking", 0.0)
    # a headline never shrinks below the readable minimum (24 px at a 360 px phone width): it takes one more
    # line instead (feedback 2026-10-30: "text is tiny and can't be read")
    floor = max(11.0, w * min_text_share()) if el.get("field") == "headline" else 11.0
    size = max(size, floor)
    while True:
        font = _font(el.get("weight", "regular"), size)
        lines = _wrap(draw, text, font, x2 - x1, tracking)
        line_h = size * 1.18
        if len(lines) <= el.get("max_lines", 3) and len(lines) * line_h <= (y2 - y1):
            return font, lines, line_h
        if size * 0.92 < floor:
            return font, lines, line_h          # at the floor: an extra line rather than smaller words
        size *= 0.92


def min_text_share() -> float:
    """The playbook's smallest caption size as a share of the frame width."""
    c = knowledge.load("rules")["captions"]
    return c["min_px_at_phone"] / c["phone_width_px"]


def _mean_rgb(img: Image.Image, box) -> tuple[float, float, float]:
    x1, y1, x2, y2 = (int(max(0, v)) for v in box)
    region = img.crop((x1, y1, max(x1 + 1, x2), max(y1 + 1, y2))).resize((16, 16))
    px = list(region.getdata())
    return tuple(sum(p[i] for p in px) / len(px) for i in range(3))


def _record(metrics: dict | None, field: str, box, value: str = "") -> None:
    """Where a piece of copy was drawn and how many words it has (safe-zone, word-count and CTA checks)."""
    if metrics is None:
        return
    metrics.setdefault("boxes", []).append([round(v, 1) for v in box] + [field])
    if value:
        metrics.setdefault("words", {})[field] = len(value.split())
        if field == "cta":
            metrics["cta"] = value


MASK_W = 64


def subject_mask(img: Image.Image) -> Image.Image:
    """A small black/white map of where the product is (white). A pixel counts when it is strongly coloured, far from
    the background colour (the median of the frame's edge) or busy (seeds, skin texture, a knife). Pale fruit flesh
    on a grey backdrop is caught by the last two, which a colour test alone misses (feedback 2026-10-22)."""
    w, h = img.size
    small = img.convert("RGB").resize((MASK_W, max(1, round(MASK_W * h / w))))
    sw, sh = small.size
    px = small.load()
    edge = [px[x, 0] for x in range(sw)] + [px[x, sh - 1] for x in range(sw)] + \
           [px[0, y] for y in range(sh)] + [px[sw - 1, y] for y in range(sh)]
    bg = tuple(sorted(p[i] for p in edge)[len(edge) // 2] for i in range(3))
    busy = small.convert("L").filter(ImageFilter.FIND_EDGES).load()
    hsv = small.convert("HSV").load()
    mask = Image.new("L", (sw, sh), 0)
    m = mask.load()
    for y in range(sh):
        for x in range(sw):
            p, (_, s, v) = px[x, y], hsv[x, y]
            far = sum((p[i] - bg[i]) ** 2 for i in range(3)) ** 0.5 > 70
            edge_px = x in (0, sw - 1) or y in (0, sh - 1)        # the edge filter always fires on the frame
            if far or (busy[x, y] > 60 and not edge_px) or (s > 115 and v > 75):
                m[x, y] = 255
    return mask.filter(ImageFilter.MaxFilter(3))          # grow a little: text shouldn't kiss the fruit either


def subject_share(img: Image.Image, box, mask: Image.Image | None = None) -> float:
    """Share of a box that covers the product (subject_mask). The check behind 'text never on the fruit'."""
    mask = mask or subject_mask(img)
    w, h = img.size
    sx, sy = mask.size[0] / w, mask.size[1] / h
    x1, y1, x2, y2 = box
    x1, x2 = sorted((int(max(0, x1 * sx)), int(min(mask.size[0], max(x1 * sx + 1, x2 * sx)))))
    y1, y2 = sorted((int(max(0, y1 * sy)), int(min(mask.size[1], max(y1 * sy + 1, y2 * sy)))))
    region = list(mask.crop((x1, y1, max(x1 + 1, x2), max(y1 + 1, y2))).getdata())
    return round(sum(1 for v in region if v) / max(1, len(region)), 2)


MOVABLE = {"text", "button", "badge", "stars"}
# Colour themes a bulk batch rotates through (feedback 2026-10-22: the ads all looked the same). Panels, buttons and
# highlight bars take the theme's colour; text colour is still chosen against what is behind it. Lo-fi paper stays.
PALETTES = {"": {}, "dark": {"orange": "espresso", "cream": "espresso", "navy": "espresso", "white": "espresso"},
            "navy": {"orange": "navy", "espresso": "navy", "cream": "navy"},
            "light": {"orange": "cream", "navy": "cream", "espresso": "cream"}}
SHIFTS = (0.0, -0.1, 0.1, -0.2, 0.2, -0.3, 0.3, -0.45, 0.45)


def photo_led(template: str) -> bool:
    """Copy straight on the photo (no panel, paper or cards of its own): the layouts whose words must avoid the fruit."""
    t = template_for(template)
    return not t.get("lofi") and all(e["type"] in MOVABLE | {"scrim"} for e in t.get("elements", []))


def best_shifts(img: Image.Image, template: str) -> dict[int, float]:
    """For a photo-led layout: how far to move each piece of copy up or down (a share of the safe area) so it covers
    the least fruit, without two pieces overlapping. {element index: shift}. Panelled layouts keep their design."""
    if not photo_led(template):
        return {}
    els = template_for(template).get("elements", [])
    w, h = img.size
    mask = subject_mask(img)
    placed: list[tuple[float, float]] = []
    out: dict[int, float] = {}
    for i, e in enumerate(els):
        if e["type"] not in MOVABLE:
            continue
        x, y, bw, bh = e["box"]
        best, best_cover = 0.0, None
        for d in SHIFTS:
            if y + d < 0 or y + d + bh > 1 or any(y + d < b and y + d + bh > a for a, b in placed):
                continue
            cover = subject_share(img, _px([x, y + d, bw, bh], w, h), mask)
            if best_cover is None or cover < best_cover - 0.05:        # move only for a clear gain
                best, best_cover = d, cover
        out[i] = best
        placed.append((y + best, y + best + bh))
    return out


def _backdrop(img: Image.Image, canvas: Image.Image | None, box) -> tuple[float, float, float]:
    """Mean colour behind `box` as the viewer sees it: on a video layer, the frame with the
    layer's scrims and panels laid over it."""
    if canvas is None:
        return _mean_rgb(img, box)
    x1, y1, x2, y2 = (int(max(0, v)) for v in box)
    crop = (x1, y1, max(x1 + 1, x2), max(y1 + 1, y2))
    seen = img.crop(crop).convert("RGBA")
    seen.alpha_composite(canvas.crop(crop))
    return _mean_rgb(seen.convert("RGB"), (0, 0, *seen.size))


def _shade(img, canvas, box, fill, bg, pad: float):
    """Darken a soft patch behind the text until `fill` reaches MIN_CONTRAST (mid-tone photos,
    where neither white nor ink reads). Returns the new backdrop colour."""
    from creative_intelligence.adstudio.qa import _lum, contrast
    w, h = img.size
    x1, y1, x2, y2 = (int(max(0, box[0] - pad)), int(max(0, box[1] - pad)),
                      int(min(w, box[2] + pad)), int(min(h, box[3] + pad)))
    target = (_lum(fill) + 0.05) / MIN_CONTRAST - 0.05
    for _ in range(4):
        if contrast(fill, bg) >= MIN_CONTRAST:
            break
        alpha = min(0.9, max(0.1, 1 - (target / max(_lum(bg), 1e-4)) ** (1 / 2.2) + 0.05))
        mask = Image.new("L", (x2 - x1, y2 - y1), 0)
        ImageDraw.Draw(mask).rectangle([pad / 2, pad / 2, x2 - x1 - pad / 2, y2 - y1 - pad / 2],
                                       fill=int(255 * alpha))
        mask = mask.filter(ImageFilter.GaussianBlur(pad / 3))
        if canvas is not None:
            patch = Image.new("RGBA", mask.size, (0, 0, 0, 255))
            patch.putalpha(mask)
            canvas.alpha_composite(patch, (x1, y1))
        else:
            img.paste((0, 0, 0), (x1, y1, x2, y2), mask)
        bg = _backdrop(img, canvas, box)
    return bg


def _measure(metrics: dict | None, fill, bg, font_px: float, w: int) -> None:
    """Record the worst text contrast and smallest text size (at 360px phone width)."""
    if metrics is None:
        return
    from creative_intelligence.adstudio.qa import contrast
    metrics["min_contrast"] = min(contrast(fill, bg), metrics.get("min_contrast", 99))
    metrics["min_text_px_at_phone"] = round(min(font_px * 360 / w, metrics.get("min_text_px_at_phone", 99)), 1)


def _text(img, draw, el, value: str, w: int, h: int, metrics: dict | None = None,
          canvas: Image.Image | None = None) -> None:
    box = _px(el["box"], w, h)
    shown = value.upper() if el.get("upper") else value
    if el.get("quote"):
        shown = f"“{shown}”"
    shown = el.get("prefix", "") + shown
    font, lines, line_h = _fit(draw, shown, el, box, w)
    x1, y1, x2, y2 = box
    total = len(lines) * line_h
    y = y2 - total if (el["box"][1] + el["box"][3]) >= 0.95 else y1   # bottom-anchored boxes grow upward
    tbox = (x1, y, x2, y + total)
    tracking = el.get("tracking", 0.0)
    if el.get("highlight"):                              # text sits on its own highlight bar
        bg = COLORS[el["highlight"]]
        fill = _pick(el.get("color", "auto"), bg)
    else:
        bg = _backdrop(img, canvas, tbox)
        fill = _pick(el.get("color", "auto"), bg)
        from creative_intelligence.adstudio.qa import contrast
        if contrast(fill, bg) < MIN_CONTRAST:            # mid-tone: shade behind light text
            pref = COLORS.get(el.get("color"), LIGHT)
            fill = pref if sum(pref) > 600 else LIGHT
            bg = _shade(img, canvas, tbox, fill, bg, font.size * 0.8)
    _measure(metrics, fill, bg, font.size, w)
    widest = max(_width(draw, ln, font, tracking) for ln in lines)
    left = {"center": x1 + (x2 - x1 - widest) / 2, "right": x2 - widest}.get(el.get("align", "left"), x1)
    _record(metrics, el["field"], (left, y, left + widest, y + total), value)
    for ln in lines:
        lw = _width(draw, ln, font, tracking)
        x = {"center": x1 + (x2 - x1 - lw) / 2, "right": x2 - lw}.get(el.get("align", "left"), x1)
        if el.get("highlight"):
            pad = font.size * 0.12
            draw.rectangle([x - pad, y + font.size * 0.08, x + lw + pad, y + line_h * 0.98],
                           fill=COLORS[el["highlight"]])
        _draw_line(draw, (x, y), ln, font, fill, tracking)
        y += line_h


def _button(img, draw, el, value: str, w: int, h: int, metrics: dict | None = None) -> None:
    value = knowledge.cta_label(value)
    x1, y1, x2, y2 = _px(el["box"], w, h)
    font = _font("semibold", el.get("size", 0.04) * w)
    tw = draw.textlength(value, font=font)
    pad_x, pad_y = font.size * 1.0, font.size * 0.6
    bw, bh = tw + 2 * pad_x, font.size + 2 * pad_y
    bx = {"left": x1, "right": x2 - bw}.get(el.get("align", "center"), x1 + (x2 - x1 - bw) / 2)
    by = y1 + (y2 - y1 - bh) / 2
    fill = COLORS.get(el.get("fill", "orange"), COLORS["orange"])
    txt = COLORS["ink"] if sum(fill) > 450 or el.get("fill") == "orange" else COLORS["white"]
    draw.rounded_rectangle([bx, by, bx + bw, by + bh], radius=bh / 2, fill=fill)
    draw.text((bx + pad_x, by + pad_y - font.size * 0.12), value, font=font, fill=txt)
    _record(metrics, el["field"], (bx, by, bx + bw, by + bh), value)
    if metrics is not None:
        from creative_intelligence.adstudio.qa import contrast
        metrics["min_contrast"] = min(contrast(txt, fill), metrics.get("min_contrast", 99))


def _lines_fit(draw, items: list[str], box, size: float, weight: str, indent: float, gap: float):
    """Largest size (<= size) at which every item wraps inside the box; (font, wrapped items, line height)."""
    x1, y1, x2, y2 = box
    while True:
        font = _font(weight, size)
        wrapped = [_wrap(draw, it, font, x2 - x1 - indent, 0.0) for it in items]
        lh = size * 1.25
        if sum(len(ws) * lh for ws in wrapped) + gap * size * (len(items) - 1) <= (y2 - y1) or size <= 12:
            return font, wrapped, lh
        size *= 0.92


def _list(img, draw, el, items: list[str], w: int, h: int, metrics: dict | None, canvas=None) -> None:
    """The brand's own lines as a list (notes app, post-it): bulleted or numbered, shrunk until all fit."""
    box = _px(el["box"], w, h)
    items = [i.strip() for i in items if i and i.strip()][:el.get("max_items", 6)]
    marked = el.get("numbered") or el.get("bullet", "•") != ""      # a letter's lines are plain paragraphs
    indent = 1.1 if marked else 0.0
    font, wrapped, lh = _lines_fit(draw, items, box, el.get("size", 0.05) * w, el.get("weight", "regular"),
                                   el.get("size", 0.05) * w * indent, 0.35)
    fill = _pick(el.get("color", "auto"), _backdrop(img, canvas, box))
    _measure(metrics, fill, _backdrop(img, canvas, box), font.size, w)
    x1, y = box[0], box[1]
    for n, lines in enumerate(wrapped, 1):
        if marked:
            draw.text((x1, y), f"{n}." if el.get("numbered") else el.get("bullet", "•"), font=font, fill=fill)
        for ln in lines:
            draw.text((x1 + font.size * indent, y), ln, font=font, fill=fill)
            y += lh
        y += font.size * 0.35
    _record(metrics, el["field"], (box[0], box[1], box[2], y), " ".join(items))


def _bubbles(img, draw, el, items: list[str], w: int, h: int, metrics: dict | None) -> None:
    """Messages from the brand, right-aligned bubbles (its own voice; never a made-up customer)."""
    x1, y1, x2, y2 = _px(el["box"], w, h)
    items = [i.strip() for i in items if i and i.strip()][:el.get("max_items", 4)]
    fill = COLORS.get(el.get("fill", "orange"), COLORS["orange"])
    txt = COLORS["ink"] if sum(fill) > 450 or el.get("fill") == "orange" else COLORS["white"]
    max_w = (x2 - x1) * 0.8
    size = el.get("size", 0.045) * w
    while True:
        font = _font("regular", size)
        wrapped = [_wrap(draw, it, font, max_w - size * 1.4, 0.0) for it in items]
        heights = [len(ws) * size * 1.25 + size * 0.9 for ws in wrapped]
        if sum(heights) + size * 0.5 * (len(items) - 1) <= y2 - y1 or size <= 12:
            break
        size *= 0.92
    y = y1
    for lines, bh in zip(wrapped, heights):
        bw = max(_width(draw, ln, font, 0.0) for ln in lines) + size * 1.4
        bx = x2 - bw
        draw.rounded_rectangle([bx, y, x2, y + bh], radius=size * 0.8, fill=fill)
        ty = y + size * 0.45
        for ln in lines:
            draw.text((bx + size * 0.7, ty), ln, font=font, fill=txt)
            ty += size * 1.25
        _record(metrics, el["field"], (bx, y, x2, y + bh))
        y += bh + size * 0.5
    _measure(metrics, txt, fill, font.size, w)
    if metrics is not None:
        metrics.setdefault("words", {})[el["field"]] = sum(len(i.split()) for i in items)


def _cards(img, draw, el, reviews: list[dict], w: int, h: int, metrics: dict | None) -> list[dict]:
    """Real reviews or comments from the library, each on its own card: stars (if rated), the exact words,
    then who, where and when. A quote that can't fit at a readable size is left out, never cut."""
    from creative_intelligence.adstudio.qa import contrast
    x1, y1, x2, y2 = _px(el["box"], w, h)
    size = el.get("size", 0.042) * w
    small = _font("regular", size * 0.72)
    pad, gap = size * 0.8, size * 0.6
    shown, y = [], y1
    for r in reviews[:el.get("max_items", 3)]:
        font = _font("medium", size)
        lines = _wrap(draw, f"“{r['text']}”", font, x2 - x1 - 2 * pad, 0.0)
        stars_h = size * 1.1 if r.get("rating") else 0
        ch = pad * 2 + stars_h + len(lines) * size * 1.25 + size * 1.0
        if y + ch > y2:
            continue                                   # doesn't fit: leave it out rather than shorten it
        draw.rounded_rectangle([x1, y, x2, y + ch], radius=size * 0.6, fill=COLORS["cream"])
        ty = y + pad
        if r.get("rating"):
            _stars(draw, {"box": [0, 0, 0, 0]}, w, h, at=(x1 + pad, ty, size * 0.45), n=int(r["rating"]))
            ty += stars_h
        for ln in lines:
            draw.text((x1 + pad, ty), ln, font=font, fill=COLORS["ink"])
            ty += size * 1.25
        draw.text((x1 + pad, ty + size * 0.15), f"{r['author']} · {r['source']} · {r['posted_on']}", font=small,
                  fill=(92, 80, 70))
        _record(metrics, "review", (x1, y, x2, y + ch), r["text"])
        if metrics is not None:
            metrics["min_contrast"] = min(contrast(COLORS["ink"], COLORS["cream"]), metrics.get("min_contrast", 99))
            metrics["min_text_px_at_phone"] = round(min(small.size * 360 / w, metrics.get("min_text_px_at_phone", 99)), 1)
        shown.append(r)
        y += ch + gap
    if metrics is not None:
        metrics.setdefault("words", {}).pop("review", None)
        metrics["words"]["review"] = sum(len(r["text"].split()) for r in shown)    # real quotes: never counted
    return shown


def _stars(draw, el, w: int, h: int, at: tuple[float, float, float] | None = None, n: int = 5) -> None:
    """n stars (a real review's own rating), in the element's box or at (x, y, radius)."""
    if at:
        x1, y1, r = at
    else:
        x1, y1, x2, y2 = _px(el["box"], w, h)
        r = (y2 - y1) / 2
    for i in range(n):
        cx, cy = x1 + r + i * r * 2.3, y1 + r
        pts = [(cx + (r if k % 2 == 0 else r * 0.45) * math.sin(k * math.pi / 5),
                cy - (r if k % 2 == 0 else r * 0.45) * math.cos(k * math.pi / 5)) for k in range(10)]
        draw.polygon(pts, fill=COLORS["orange"])


def _scrim(img: Image.Image, el, w: int, h: int) -> Image.Image:
    x1, y1, x2, y2 = _px(el["box"], w, h, bleed=True)
    if el.get("from") == "top":
        y1 = 0
    else:
        y2 = h
    dark = True
    grad = Image.new("L", (1, 256))
    for i in range(256):
        t = i / 255 if el.get("from") != "top" else 1 - i / 255
        grad.putpixel((0, i), int(255 * el.get("strength", 0.5) * (t ** 1.4)))
    mask = grad.resize((int(x2 - x1), max(1, int(y2 - y1))))
    layer = Image.new("RGB", mask.size, (0, 0, 0) if dark else (255, 255, 255))
    base = img.copy()
    base.paste(layer, (int(x1), int(y1)), mask)
    return base


def _scrim_layer(canvas: Image.Image, el, w: int, h: int) -> None:
    """The scrim as see-through black on a transparent layer (for video, where the picture moves)."""
    x1, y1, x2, y2 = _px(el["box"], w, h, bleed=True)
    if el.get("from") == "top":
        y1 = 0
    else:
        y2 = h
    grad = Image.new("L", (1, 256))
    for i in range(256):
        t = i / 255 if el.get("from") != "top" else 1 - i / 255
        grad.putpixel((0, i), int(255 * el.get("strength", 0.5) * (t ** 1.4)))
    mask = grad.resize((int(x2 - x1), max(1, int(y2 - y1))))
    shade = Image.new("RGBA", mask.size, (0, 0, 0, 255))
    shade.putalpha(mask)
    canvas.alpha_composite(shade, (int(x1), int(y1)))


def render(image_path: str, template: str, copy: dict[str, str],
           metrics: dict | None = None, layer: bool = False) -> tuple[Image.Image, dict[str, str]]:
    """Draw the template onto the picture. With layer=True the picture is only used to choose
    colours and measure contrast, and the drawing goes on a transparent layer of the same size
    (for laying over a video, Video Studio)."""
    img = Image.open(image_path).convert("RGB")
    w, h = img.size
    plain = img.copy()                                # the picture before any copy, for the subject check
    canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0)) if layer else None
    if metrics is not None:
        metrics["frame"] = [w, h]
        if template_for(template).get("lofi"):
            metrics["lofi"] = True                        # text by design: the lo-fi word limit applies
    drawn: dict[str, str] = {}
    shifts = best_shifts(plain, template) if not layer else {}       # copy goes where the fruit isn't
    pal = PALETTES.get(copy.get("palette") or "", {})
    for n, el in enumerate(template_for(template)["elements"]):
        if pal and (el.get("fill") in pal or el.get("highlight") in pal):
            el = {**el, **{k: pal.get(el[k], el[k]) for k in ("fill", "highlight") if el.get(k)}}
        if shifts.get(n):
            el = {**el, "box": [el["box"][0], el["box"][1] + shifts[n], el["box"][2], el["box"][3]]}
        kind = el["type"]
        raw = copy.get(el.get("field", ""))
        if kind in ("list", "bubbles", "cards"):
            if raw:
                draw = ImageDraw.Draw(canvas if layer else img)
                if kind == "list":
                    _list(img, draw, el, list(raw), w, h, metrics, canvas)
                elif kind == "bubbles":
                    _bubbles(img, draw, el, list(raw), w, h, metrics)
                else:
                    shown = _cards(img, draw, el, list(raw), w, h, metrics)
                    if metrics is not None:
                        metrics["reviews_shown"] = [r["id"] for r in shown]
                drawn[el["field"]] = raw
            continue
        value = el.get("static") or ((raw or "").strip() if isinstance(raw, str) else "")
        if kind == "scrim":
            if layer:
                _scrim_layer(canvas, el, w, h)
            else:
                img = _scrim(img, el, w, h)
            continue
        draw = ImageDraw.Draw(canvas if layer else img)
        if kind == "block":
            x1, y1, x2, y2 = _px(el["box"], w, h, bleed=el.get("bleed", False))
            r = el.get("radius", 0) * w
            draw.rounded_rectangle([x1, y1, x2, y2], radius=r, fill=COLORS[el["fill"]]) if r else \
                draw.rectangle([x1, y1, x2, y2], fill=COLORS[el["fill"]])
        elif kind == "stars":
            rating = copy.get("rating")
            if rating:                                    # only a real review's own rating; never assumed
                _stars(draw, el, w, h, n=int(rating))
        elif not value:
            continue                                  # optional field left empty: draw nothing
        elif kind == "text":
            _text(img, draw, el, value, w, h, metrics, canvas)
            if not el.get("static"):                      # a fixed label (the thread's sender) isn't copy
                drawn[el["field"]] = value
        elif kind == "button":
            _button(img, draw, el, value, w, h, metrics)
            drawn[el["field"]] = value
        elif kind == "badge":
            x1, y1, x2, y2 = _px(el["box"], w, h)
            font = _font("semibold", el.get("size", 0.03) * w)
            tw = draw.textlength(value, font=font)
            draw.rounded_rectangle([x1, y1, x1 + tw + font.size * 1.6, y1 + font.size * 1.9],
                                   radius=font.size, fill=COLORS["cream"])
            draw.text((x1 + font.size * 0.8, y1 + font.size * 0.32), value, font=font, fill=COLORS["ink"])
            _record(metrics, el["field"], (x1, y1, x1 + tw + font.size * 1.6, y1 + font.size * 1.9), value)
            drawn[el["field"]] = value
    if metrics is not None and metrics.get("boxes"):
        mask = subject_mask(plain)
        metrics["over_subject"] = max(subject_share(plain, b[:4], mask) for b in metrics["boxes"])
    return (canvas if layer else img), drawn


def text_regions(template: str, w: int, h: int) -> list[tuple[float, float, float, float]]:
    """Pixel boxes of a template's text, buttons and panels at this frame size (for keeping them off the product)."""
    out = []
    for el in template_for(template)["elements"]:
        if el["type"] in ("text", "button", "block", "badge"):
            out.append(_px(el["box"], w, h, bleed=el.get("bleed", False)))
    return out


def render_layer_to(frame_path: str, out_path: str, template: str, copy: dict[str, str],
                    metrics: dict | None = None) -> dict[str, str]:
    """Transparent PNG text layer for a video, colours chosen from `frame_path` (a frame of the clip)."""
    canvas, drawn = render(frame_path, template, copy, metrics, layer=True)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path, "PNG")
    return drawn


def render_to(image_path: str, out_path: str, template: str, copy: dict[str, str],
              metrics: dict | None = None) -> dict[str, str]:
    img, drawn = render(image_path, template, copy, metrics)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "PNG")
    return drawn
