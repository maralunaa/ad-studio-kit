"""
The timeline engine (Video Studio V3): a video is a JSON timeline, rendered with Python + ffmpeg.

    {"version": 1, "aspect": "9:16", "fps": 30, "transition": 0.3,
     "picture":  [{"src": clip.mp4, "dur": 4.2, "shot_id": 12, "label": "Whole-fruit hero", "box": [..]}],
     "voice":    {"src": vo.mp3, "words": [{"word", "start", "end"}]},
     "captions": {"max_words": 3},
     "text":     [{"template": "bold_type", "headline": "...", "start": 0, "end": 3}],
     "sfx":      [{"src": hit.wav, "start": 1.2, "gain_db": -6}],
     "music":    {"src": track.mp3, "gain_db": -16} | null,
     "end_card": {"dur": 3.0, "image": hero.jpg, "headline": "...", "cta": "Shop now", "price": "" }}

Rendering: every picture segment is framed to the size (products never cropped: background-only crop
around the product box, else padded), stretched a little or held on its last frame to fill its time,
then joined with short crossfades; the end card is a still drawn with PIL. Captions are burned in from
the voice-over's word timings (PIL text, a few words at a time), text layers come from the Compose
templates, and music is ducked under the voice (sidechain compression). All text is drawn by code, inside
the playbook's safe zone for the size (templates.safe_area); render() reports where it went and how big
(`measured`) and checks the finished file against the export spec (`export`).
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFilter

from creative_intelligence.adstudio import videotools
from creative_intelligence.adstudio.render_template import _font, render_layer_to
from creative_intelligence.adstudio.templates import safe_area

from creative_intelligence import brand as _brand

MAX_STRETCH = 1.35            # a clip may play this much slower; past that it plays again from the start
TRANSITION = 0.3
END_CARD_S = 3.0
ACCENT = _brand.rgb("accent")          # the brand accent (brand.yaml › colors)
ACCENT_INK = _brand.rgb("accent_ink")


# ── planning ────────────────────────────────────────────────────────────────
def line_spans(lines: list[str], words: list[dict[str, Any]], total: float) -> list[tuple[float, float]]:
    """When each script line is spoken: from its first word to the next line's first word."""
    from creative_intelligence.adstudio.voices import norm
    counts = [len([w for w in ln.split() if norm(w)]) for ln in lines]
    starts, i = [], 0
    for n in counts:
        starts.append(words[min(i, len(words) - 1)]["start"] if words else 0.0)
        i += n
    starts[0] = 0.0
    ends = starts[1:] + [total]
    return [(round(a, 3), round(b, 3)) for a, b in zip(starts, ends)]


def picture_plan(spans: list[tuple[float, float]], clips: list[dict[str, Any]], t: float = TRANSITION) -> list[dict[str, Any]]:
    """One picture segment per line, long enough that the crossfades leave each line on its own shot."""
    out = []
    for k, ((a, b), c) in enumerate(zip(spans, clips)):
        dur = (b - a) + (t if k < len(spans) - 1 else 0.0)
        out.append({**c, "dur": round(max(0.8, dur), 3)})
    return out


def caption_chunks(words: list[dict[str, Any]], max_words: int = 3, max_gap: float = 0.6,
                   min_s: float = 0.7, hold_s: float = 1.2) -> list[dict[str, Any]]:
    """Words grouped a few at a time, breaking at punctuation and pauses; each shown until the next starts.
    2026-11-03 (the Claymation captions were choppy): a chunk shorter than min_s takes the next word or two (up to
    max_words + 2), and a caption stays up through a pause of up to hold_s instead of blinking off."""
    chunks, cur = [], []
    for i, w in enumerate(words):
        cur.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        brk = (len(cur) >= max_words or w["word"][-1:] in ".,:;!?" or nxt is None or nxt["start"] - w["end"] > max_gap)
        if brk and nxt is not None and w["end"] - cur[0]["start"] < min_s and len(cur) < max_words + 2 \
                and w["word"][-1:] not in ".!?" and nxt["start"] - w["end"] <= max_gap:
            brk = False
        if brk:
            chunks.append({"text": " ".join(x["word"] for x in cur), "start": cur[0]["start"], "end": cur[-1]["end"]})
            cur = []
    for a, b in zip(chunks, chunks[1:]):
        a["end"] = b["start"] if b["start"] - a["end"] <= hold_s else max(a["end"], a["end"] + 0.5)
    if chunks:
        chunks[-1]["end"] += 0.3
    return chunks


def duration(tl: dict[str, Any]) -> float:
    segs = tl["picture"]
    t = tl.get("transition", TRANSITION)
    body = sum(s["dur"] for s in segs) - t * max(0, len(segs) - 1)
    return round(body + ((tl.get("end_card") or {}).get("dur", 0) or 0), 3)


def validate(tl: dict[str, Any]) -> list[str]:
    errs = []
    if tl.get("aspect") not in videotools.SIZES:
        errs.append("unknown aspect")
    if not tl.get("picture"):
        errs.append("no picture")
    for i, s in enumerate(tl.get("picture") or []):
        if not s.get("src") or not Path(s["src"]).exists():
            errs.append(f"picture {i + 1}: file missing")
        if (s.get("dur") or 0) <= 0:
            errs.append(f"picture {i + 1}: no duration")
        if s.get("split") and not Path(s["split"]).exists():
            errs.append(f"picture {i + 1}: split clip missing")
        if s.get("pip") and not Path(s["pip"]).exists():
            errs.append(f"picture {i + 1}: presenter clip missing")
    v = tl.get("voice")
    if v and not Path(v.get("src", "")).exists():
        errs.append("voice file missing")
    m = tl.get("music")
    if m and not Path(m.get("src", "")).exists():
        errs.append("music file missing")
    return errs


# ── drawing (PIL) ───────────────────────────────────────────────────────────
CAPTION_STYLES = ("bold", "boxed")


def _note(m: dict | None, field: str, box, px: float, W: int, contrast: float | None = None) -> None:
    """Record a drawn text block for the checks: its box, its size at phone width and its contrast."""
    if m is None:
        return
    m.setdefault("boxes", []).append([round(v, 1) for v in box] + [field])
    at_phone = round(px * 360 / W, 1)
    m["min_text_px_at_phone"] = min(at_phone, m.get("min_text_px_at_phone", 99))
    if field == "caption":
        m["caption_px_at_phone"] = min(at_phone, m.get("caption_px_at_phone", 99))
    if contrast is not None:
        m["min_contrast"] = round(min(contrast, m.get("min_contrast", 99)), 2)


def hook_place(clip: dict[str, Any]) -> str:
    """Where the hook words go so they never sit on the face or the fruit (feedback 2026-10-26): under a presenter's
    face, else the half of the frame the subject isn't in."""
    if clip.get("pip") or clip.get("character"):   # the presenter's head has the bottom corner; a fruit character
        return "top"                               # fills the middle and its face is there (2026-11-03)
    box = clip.get("box")
    return "low" if clip.get("label") == "presenter" or (box and (box[1] + box[3]) / 2 < 0.5) else "top"


# Caption looks (feedback 2026-10-26: every format had the same white-outline captions). Each: font weight, size
# factor, text colour, box colour (None = outline only), outline colour, lower-case.
CAPTION_STYLES = {
    "bold":   ("bold", 1.0, (255, 255, 255), None, (16, 12, 9), False),           # white, dark outline
    "boxed":  ("bold", 1.0, (255, 255, 255), (16, 12, 9, 205), None, False),      # white on a dark box
    "hand":   ("hand", 1.35, (255, 244, 214), None, (40, 26, 12), False),         # handwritten, warm cream
    "bubble": ("semibold", 0.9, (24, 20, 17), (255, 255, 255, 240), None, False), # dark on a white speech bubble
    "casual": ("medium", 0.85, (255, 255, 255), (0, 0, 0, 220), None, True),      # lower-case on black, phone-native
    "accent": ("bold", 0.95, ACCENT_INK, (*ACCENT, 245), None, False),      # accent ink on the brand accent
    # Behind the farm (2026-11-04, from the user's best video): small white sentence fragments, low, thin outline
    "quiet":  ("medium", 0.62, (255, 255, 255), None, (16, 12, 9), False),
}


def caption_png(text: str, W: int, H: int, out: Path, aspect: str, style: str = "bold",
                metrics: dict | None = None, place: str = "low", size: float = 0.078, field: str = "caption") -> Path:
    """One caption, centred in the lower part of the frame and ending at the bottom of the safe zone (9:16:
    above the bottom 35%), or with place="top" starting at the top of the zone, drawn in one of CAPTION_STYLES."""
    from creative_intelligence.adstudio.qa import contrast
    weight, scale, ink, box, outline, lower = CAPTION_STYLES.get(style, CAPTION_STYLES["bold"])
    text = text.lower() if lower else text
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    px = round(W * size * scale)
    font = _font(weight, px)
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if d.textlength(trial, font=font) > W * 0.84 and cur:
            lines.append(cur)
            cur = w
        else:
            cur = trial
    lines.append(cur)
    bottom = safe_area()[aspect][1] * H - px * 0.4              # the last line's descenders stay in the zone
    if place == "top":
        bottom = safe_area()[aspect][0] * H + px * 0.3 + px * 1.18 * len(lines)
    y = bottom - px * 1.18 * len(lines)
    widest = max(d.textlength(ln, font=font) for ln in lines)
    _note(metrics, field, ((W - widest) / 2, y, (W + widest) / 2, bottom), px, W,
          contrast(ink, (box or outline)[:3]))
    if box:
        pad = px * 0.35
        d.rounded_rectangle([(W - widest) / 2 - pad, y - pad * 0.6, (W + widest) / 2 + pad, y + px * 1.18 * len(lines) + pad * 0.2],
                            radius=px * (0.6 if style == "bubble" else 0.3), fill=box)
    for ln in lines:
        tw = d.textlength(ln, font=font)
        if box:
            d.text(((W - tw) / 2, y), ln, font=font, fill=ink + (255,))
        else:
            d.text(((W - tw) / 2, y), ln, font=font, fill=ink + (255,),
                   stroke_width=max(4, px // 10), stroke_fill=outline + (235,))
        y += px * 1.18
    img.save(out, "PNG")
    return out


PIP_SIZE = 0.36       # the presenter's head: a circle this share of the frame width


def pip_box(W: int, H: int, aspect: str) -> tuple[int, int, int]:
    """(x, y, diameter) of the presenter's head: bottom left, its lowest point on the bottom of the safe zone."""
    top, bottom, side = safe_area()[aspect]
    size = round(W * PIP_SIZE) // 2 * 2
    return round(W * side), round(bottom * H) - size, size


CUT_W = 0.5          # the cut-out presenter: this share of the frame width, flush with the bottom-left corner
CUT_KEEP = 0.62      # head and shoulders: the top of the presenter clip kept, its bottom edge on the frame's edge
GREEN_KEY = "chromakey=0x00FF00:0.22:0.06,despill=type=green"


def cut_box(W: int, H: int) -> tuple[int, int, int, int]:
    """(x, y, w, h) of the cut-out presenter in the frame, for a 9:16 clip."""
    w = round(W * CUT_W) // 2 * 2
    h = round(w * 16 / 9 * CUT_KEEP) // 2 * 2
    return 0, H - h, w, h


def pip_masks(size: int, tmp: Path, i: int) -> tuple[Path, Path]:
    """The circle the head is cut to, and its thin ring in the brand accent."""
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, size - 1, size - 1], fill=255)
    ring = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(ring).ellipse([1, 1, size - 2, size - 2], outline=ACCENT + (255,), width=max(4, size // 60))
    mask.save(tmp / f"pipmask{i}.png")
    ring.save(tmp / f"pipring{i}.png")
    return tmp / f"pipmask{i}.png", tmp / f"pipring{i}.png"


def split_labels_png(labels: list[str], W: int, H: int, aspect: str, out: Path, metrics: dict | None = None) -> Path:
    """The two halves of a split screen named in small pills, each near the top of its half and inside the safe zone."""
    from creative_intelligence.adstudio.qa import contrast
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    top, bottom, side = safe_area()[aspect]
    px = round(W * 0.042)
    font = _font("semibold", px)
    for label, y in zip(labels, (max(top * H, H * 0.02) + px * 0.4, max(top * H, H / 2) + px * 0.4)):
        tw = d.textlength(label, font=font)
        x0 = side * W + px * 0.4
        box = (x0, y, x0 + tw + px * 1.4, y + px * 1.8)
        d.rounded_rectangle(box, radius=px * 0.9, fill=(16, 12, 9, 215))
        d.text((x0 + px * 0.7, y + px * 0.32), label, font=font, fill=(255, 255, 255, 255))
        _note(metrics, "split_label", box, px, W, contrast((255, 255, 255), (16, 12, 9)))
    img.save(out, "PNG")
    return out


def comment_sticker_png(text: str, W: int, H: int, aspect: str, out: Path, metrics: dict | None = None,
                        label: str = "A customer asked", place: str = "top") -> Path:
    """A real comment (or a real question people ask) as a sticker near the top of the safe zone: the words exactly as
    written, no name."""
    from creative_intelligence.adstudio.qa import contrast
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    top, bottom, side = safe_area()[aspect]
    px = round(W * 0.042)
    while True:
        font = _font("medium", px)
        lines = _wrap(d, f"“{text}”", font, W * (1 - 2 * side) - px * 2)
        if len(lines) <= 5 or px <= 22:
            break
        px -= 2
    small = _font("semibold", round(px * 0.72))
    hgt = px * 1.3 + len(lines) * px * 1.25 + px * 1.0
    x0, y0 = side * W, (top * H + px * 0.3) if place == "top" else (bottom * H - hgt)   # low: under a presenter's face
    d.rounded_rectangle([x0, y0, W - side * W, y0 + hgt], radius=px * 0.7, fill=(255, 249, 236, 245))
    d.text((x0 + px, y0 + px * 0.5), label, font=small, fill=(92, 80, 70, 255))
    y = y0 + px * 1.3
    for ln in lines:
        d.text((x0 + px, y), ln, font=font, fill=(24, 20, 17, 255))
        y += px * 1.25
    _note(metrics, "comment_sticker", (x0, y0, W - side * W, y0 + hgt), small.size, W, contrast((24, 20, 17), (255, 249, 236)))
    img.save(out, "PNG")
    return out


def kinetic_png(text: str, W: int, H: int, out: Path, metrics: dict | None = None) -> Path:
    """Kinetic type: a few big words in the middle of the frame, the last one in the brand accent."""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    px = round(W * 0.115)
    font = _font("bold", px)
    lines = _wrap(d, text.upper(), font, W * 0.86)
    y = H * 0.42 - px * 1.1 * len(lines) / 2
    widest = max((d.textlength(ln, font=font) for ln in lines), default=0)
    _note(metrics, "kinetic", ((W - widest) / 2, y, (W + widest) / 2, y + px * 1.1 * len(lines)), px, W)
    for i, ln in enumerate(lines):
        tw = d.textlength(ln, font=font)
        fill = ACCENT + (255,) if i == len(lines) - 1 else (255, 255, 255, 255)
        d.text(((W - tw) / 2, y), ln, font=font, fill=fill, stroke_width=max(5, px // 12), stroke_fill=(16, 12, 9, 230))
        y += px * 1.1
    img.save(out, "PNG")
    return out


def label_png(text: str, W: int, H: int, aspect: str, out: Path, metrics: dict | None = None) -> Path:
    """Fast-cut montage: a short upper-case label in a brand-accent tab, bottom left of the safe zone."""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    top, bottom, side = safe_area()[aspect]
    px = round(W * 0.06)
    font = _font("bold", px)
    words = text.upper()
    tw = d.textlength(words, font=font)
    pad = px * 0.45
    x0, y1 = side * W, bottom * H
    y0 = y1 - px * 1.35 - pad
    d.rectangle([x0, y0, x0 + tw + pad * 2, y1], fill=ACCENT + (255,))
    d.text((x0 + pad, y0 + pad * 0.55), words, font=font, fill=ACCENT_INK + (255,))
    _note(metrics, "label", (x0, y0, x0 + tw + pad * 2, y1), px, W)
    img.save(out, "PNG")
    return out


def whisper_png(text: str, W: int, H: int, aspect: str, out: Path, metrics: dict | None = None) -> Path:
    """Sensory: small, quiet lower-case words with a soft shadow, low in the safe zone; the picture does the work."""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    top, bottom, side = safe_area()[aspect]
    px = round(W * 0.05)
    font = _font("regular", px)
    words = text.lower()
    d = ImageDraw.Draw(img)
    tw = d.textlength(words, font=font)
    x, y = (W - tw) / 2, bottom * H - px * 1.6
    ImageDraw.Draw(shadow).text((x, y + 2), words, font=font, fill=(0, 0, 0, 170))
    img = Image.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(6)), img)
    ImageDraw.Draw(img).text((x, y), words, font=font, fill=(255, 255, 255, 240))
    _note(metrics, "whisper", (x, y, x + tw, y + px * 1.2), px, W)
    img.save(out, "PNG")
    return out


def day_png(text: str, W: int, H: int, aspect: str, out: Path, metrics: dict | None = None) -> Path:
    """Ripening time-lapse: the stage ("Day 3: ready") as a chip, top left of the safe zone; a longer stage wraps
    inside the frame (live test 2026-10-27: one-line chips ran off the edge)."""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    top, bottom, side = safe_area()[aspect]
    px = round(W * 0.055)
    font = _font("semibold", px)
    pad = px * 0.5
    lines = _wrap(d, text, font, W * (1 - 2 * side) - pad * 2)
    widest = max((d.textlength(ln, font=font) for ln in lines), default=0)
    lh = px * 1.25
    x0, y0 = side * W, top * H + px * 0.4
    box = (x0, y0, x0 + widest + pad * 2, y0 + lh * len(lines) + pad)
    d.rounded_rectangle(box, radius=px * 0.35, fill=(16, 12, 9, 200))
    for i, ln in enumerate(lines):
        d.text((x0 + pad, y0 + pad * 0.5 + i * lh), ln, font=font, fill=(255, 255, 255, 255))
    _note(metrics, "day", box, px, W)
    img.save(out, "PNG")
    return out


TEXT_LOOKS = {"label": label_png, "whisper": whisper_png, "day": day_png}


def end_card_png(card: dict[str, Any], W: int, H: int, out: Path, metrics: dict | None = None) -> Path:
    """The last few seconds: the product on a blurred copy of itself, then the headline, price and CTA (no logo:
    it sat in an odd spot, feedback 2026-10-26). The copy sits inside the safe zone for the size (9:16: the product
    may fill the top, but every word ends above the bottom 35%)."""
    from creative_intelligence.adstudio.render_template import aspect_key
    from creative_intelligence.adstudio.qa import contrast
    top, bottom, side = safe_area()[aspect_key(W, H)]
    band_bottom = bottom * H
    bg = Image.new("RGB", (W, H), (21, 18, 15))
    d = ImageDraw.Draw(bg)
    hf = _font("bold", round(W * 0.075))
    head = _wrap(d, card.get("headline", ""), hf, W * (1 - 2 * max(side, 0.07)))
    head_lh = round(W * 0.075 * 1.15)
    cf = _font("semibold", round(W * 0.05))
    bh = W * 0.11
    gap = W * 0.03
    block = len(head) * head_lh + (W * 0.06 if card.get("price") else 0) + (gap + bh if card.get("cta") else 0)
    text_top = band_bottom - block                                  # the copy ends at the bottom of the zone
    if card.get("image") and Path(card["image"]).exists():
        photo = Image.open(card["image"]).convert("RGB")
        fill = photo.copy()
        fill = fill.resize((W, round(W * fill.height / fill.width))) if fill.width / fill.height < W / H else \
            fill.resize((round(H * fill.width / fill.height), H))
        fill = fill.crop(((fill.width - W) // 2, (fill.height - H) // 2, (fill.width - W) // 2 + W, (fill.height - H) // 2 + H))
        bg = Image.blend(fill.filter(ImageFilter.GaussianBlur(40)), Image.new("RGB", (W, H), (12, 10, 8)), 0.45)
        d = ImageDraw.Draw(bg)
        photo_top = round(H * 0.05)
        photo.thumbnail((int(W * 0.78), int(max(W * 0.3, text_top - gap - photo_top))))
        bg.paste(photo, ((W - photo.width) // 2, max(photo_top, round(text_top - gap - photo.height))))
    y = text_top
    for ln in head:
        tw = d.textlength(ln, font=hf)
        d.text(((W - tw) / 2, y), ln, font=hf, fill=(239, 234, 227))
        _note(metrics, "end_card_headline", ((W - tw) / 2, y, (W + tw) / 2, y + head_lh), hf.size, W,
              contrast((239, 234, 227), (21, 18, 15)))
        y += head_lh
    if card.get("price"):
        pf = _font("medium", round(W * 0.045))
        tw = d.textlength(card["price"], font=pf)
        d.text(((W - tw) / 2, y + 10), card["price"], font=pf, fill=(201, 193, 183))
        _note(metrics, "end_card_price", ((W - tw) / 2, y, (W + tw) / 2, y + W * 0.06), pf.size, W)
        y += round(W * 0.06)
    if card.get("cta"):
        tw = d.textlength(card["cta"], font=cf)
        bw = tw + W * 0.12
        x0, y0 = (W - bw) / 2, y + gap
        d.rounded_rectangle([x0, y0, x0 + bw, y0 + bh], radius=bh / 2, fill=ACCENT)
        d.text(((W - tw) / 2, y0 + (bh - W * 0.05) / 2 - W * 0.008), card["cta"], font=cf, fill=ACCENT_INK)
        _note(metrics, "end_card_cta", (x0, y0, x0 + bw, y0 + bh), cf.size, W, contrast(ACCENT_INK, ACCENT))
        if metrics is not None:
            metrics["cta"] = card["cta"]
    bg.save(out, "PNG")
    return out


def _wrap(d, text: str, font, max_w: float) -> list[str]:
    lines, cur = [], ""
    for w in (text or "").split():
        trial = (cur + " " + w).strip()
        if d.textlength(trial, font=font) > max_w and cur:
            lines.append(cur)
            cur = w
        else:
            cur = trial
    return lines + ([cur] if cur else [])


# ── rendering (ffmpeg) ──────────────────────────────────────────────────────
def _segment(seg: dict[str, Any], aspect: str, fps: int, out: Path, tmp: Path, i: int,
             measured: dict | None = None) -> Path:
    """One picture segment framed to `aspect`, exactly seg['dur'] seconds, no audio."""
    W, H = videotools.SIZES[aspect]
    d = seg["dur"]
    if seg.get("split"):                       # Us vs supermarket: the comparison on top, ours below, both full width
        top, bottom = tmp / f"top{i}.mp4", tmp / f"bottom{i}.mp4"
        videotools.fit_to(seg["split"], top, W, H // 2)
        videotools.fit_to(seg["src"], bottom, W, H - H // 2)
        hold = f"tpad=stop_mode=clone:stop_duration={d:.3f},fps={fps}"
        labels = split_labels_png(seg.get("split_labels") or [], W, H, aspect, tmp / f"split{i}.png", measured)
        videotools.run(["-i", str(top), "-i", str(bottom), "-loop", "1", "-i", str(labels), "-filter_complex",
                        f"[0:v]{hold}[a];[1:v]{hold}[b];[a][b]vstack=inputs=2,scale={W}:{H},setsar=1[s];"   # 675 halves round to 674:
                        f"[s][2:v]overlay=0:0,format=yuv420p[v]",                                             # back to 1350 exact
                        "-map", "[v]", "-t", f"{d:.3f}", "-an", *videotools._x264(), str(out)])
        return out
    if seg.get("image"):
        videotools.run(["-loop", "1", "-t", f"{d:.3f}", "-i", str(seg["src"]), "-vf",
                        f"scale={W}:{H},fps={fps},format=yuv420p", "-an", *videotools._x264(), str(out)])
        return out
    framed = tmp / f"framed{i}.mp4"
    videotools.frame(seg["src"], framed, aspect, seg.get("box"))
    full = videotools.probe(framed)["duration"]
    if float(seg.get("trim_in", 0) or 0) > full - 0.5:      # played to its end on earlier lines: start it again
        seg = {**seg, "trim_in": 0}
    have = full - float(seg.get("trim_in", 0) or 0)
    slow = min(MAX_STRETCH, max(1.0, d / max(0.1, have)))
    if seg.get("stage"):                       # a time-lapse over n lines: this line plays its k-th part, slowed to fit
        k, n = seg["stage"]                    # (feedback 2026-11-03: "a simple, slow time-lapse", day after day)
        seg = {**seg, "trim_in": round(full * k / n, 3)}
        have = full / n
        slow = max(1.0, d / max(0.1, have))
    step = f"fps={int(seg['stop_motion_fps'])}," if seg.get("stop_motion_fps") else ""     # stop-motion: held frames
    # a shot too short for its line plays again instead of freezing on its last frame (feedback 2026-10-30: "the
    # video stops and we keep hearing voiceover"); a talking clip is as long as its line, so it never loops
    loop = seg.get("label") != "presenter" and not seg.get("stage") and have * MAX_STRETCH < d - 0.05
    if loop:
        vf = f"{step}fps={fps},format=yuv420p"
    else:
        hold = max(0.0, d - have * slow) + 0.1
        vf = f"setpts={slow:.4f}*PTS,tpad=stop_mode=clone:stop_duration={hold:.3f},{step}fps={fps},format=yuv420p"
    ss = (["-stream_loop", "-1"] if loop else []) + (["-ss", str(seg["trim_in"])] if seg.get("trim_in") else [])
    if seg.get("sticker"):                     # Comment reply: the real comment, drawn into the picture
        png = comment_sticker_png(seg["sticker"], W, H, aspect, tmp / f"sticker{i}.png", measured,
                                  seg.get("sticker_label") or "A customer asked", hook_place(seg))
        videotools.run([*ss, "-i", str(framed), "-loop", "1", "-i", str(png), "-filter_complex",
                        f"[0:v]{vf}[b];[b][1:v]overlay=0:0,format=yuv420p[v]", "-map", "[v]", "-t", f"{d:.3f}", "-an",
                        *videotools._x264(), str(out)])
        return out
    if seg.get("pip") and seg.get("pip_cut"):  # Talking head: the presenter, cut out, talks from the bottom-left corner
        x, y, w, h = cut_box(W, H)
        if measured is not None:
            measured.setdefault("boxes", []).append([x, y, x + w, y + h, "presenter"])
        videotools.run([*ss, "-i", str(framed), "-i", str(seg["pip"]), "-filter_complex",
                        f"[0:v]{vf}[b];[1:v]{GREEN_KEY},crop=iw:ih*{CUT_KEEP}:0:0,scale={w}:{h},setsar=1,format=rgba,"
                        f"tpad=stop_mode=clone:stop_duration={d:.3f},fps={fps}[p];"
                        f"[b][p]overlay={x}:{y}:shortest=0,format=yuv420p[v]",
                        "-map", "[v]", "-t", f"{d:.3f}", "-an", *videotools._x264(), str(out)])
        return out
    if seg.get("pip"):                         # older pieces (not made on green): the head in a circle
        x, y, size = pip_box(W, H, aspect)
        mask, ring = pip_masks(size, tmp, i)
        if measured is not None:
            measured.setdefault("boxes", []).append([x, y, x + size, y + size, "presenter"])
        sq = "min(iw\\,ih)"                    # a square from the top of a portrait clip: head and shoulders
        videotools.run([*ss, "-i", str(framed), "-i", str(seg["pip"]), "-loop", "1", "-i", str(mask),
                        "-loop", "1", "-i", str(ring), "-filter_complex",
                        f"[0:v]{vf}[b];[1:v]crop={sq}:{sq}:(iw-{sq})/2:if(gt(ih\\,iw)\\,ih*0.03\\,0),"
                        f"scale={size}:{size},setsar=1,tpad=stop_mode=clone:stop_duration={d:.3f},fps={fps},format=rgba[f];"
                        f"[2:v]format=gray,scale={size}:{size}[m];[f][m]alphamerge[p];"
                        f"[b][p]overlay={x}:{y}:shortest=0[bp];[bp][3:v]overlay={x}:{y},format=yuv420p[v]",
                        "-map", "[v]", "-t", f"{d:.3f}", "-an", *videotools._x264(), str(out)])
        return out
    videotools.run([*ss, "-i", str(framed), "-vf", vf, "-t", f"{d:.3f}", "-an", *videotools._x264(), str(out)])
    return out


def overlay_track(overlays: list[tuple[Path, float, float]], W: int, H: int, total: float, tmp: Path) -> Path:
    """An ffconcat list that shows each layer from its start to its end over a transparent blank."""
    blank = tmp / "blank.png"
    Image.new("RGBA", (W, H), (0, 0, 0, 0)).save(blank, "PNG")
    items, t = [], 0.0
    for png, a, b in sorted(overlays, key=lambda o: o[1]):
        a, b = max(a, t), min(max(b, a), total)
        if b - a < 0.04:
            continue
        if a - t >= 0.04:
            items.append((blank, a - t))
        items.append((png, b - a))
        t = b
    if total - t >= 0.04 or not items:
        items.append((blank, max(0.04, total - t)))
    lines = ["ffconcat version 1.0"]
    for f, d in items:
        lines += [f"file '{f}'", f"duration {d:.3f}"]
    lines.append(f"file '{items[-1][0]}'")                    # the concat demuxer needs the last file again
    lst = tmp / "overlay.ffconcat"
    lst.write_text("\n".join(lines) + "\n")
    return lst


# Renders at once across the app: each is CPU-heavy ffmpeg, so more side by side only slows them all down.
RENDERS = threading.BoundedSemaphore(int(os.getenv("RENDERS_AT_ONCE", "3")))


def render(tl: dict[str, Any], out: str | Path) -> dict[str, Any]:
    """Render a timeline to an mp4. Returns {path, duration, captions}."""
    with RENDERS:
        return _render(tl, out)


def _render(tl: dict[str, Any], out: str | Path) -> dict[str, Any]:
    errs = validate(tl)
    if errs:
        raise videotools.VideoToolError("; ".join(errs))
    aspect, fps = tl["aspect"], int(tl.get("fps", 30))
    W, H = videotools.SIZES[aspect]
    t = float(tl.get("transition", TRANSITION))
    out = Path(out)
    measured: dict[str, Any] = {"frame": [W, H]}
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        segs = [dict(s) for s in tl["picture"]]
        card = tl.get("end_card")
        if card:
            png = end_card_png(card, W, H, tmp / "endcard.png", measured)
            segs[-1]["dur"] = round(segs[-1]["dur"] + t, 3)            # the last shot fades into the card
            segs.append({"src": str(png), "image": True, "dur": float(card.get("dur", END_CARD_S))})
        files = [_segment(s, aspect, fps, tmp / f"seg{i}.mp4", tmp, i, measured) for i, s in enumerate(segs)]
        total = round(sum(s["dur"] for s in segs) - t * (len(segs) - 1), 3)

        args: list[str] = []
        for f in files:
            args += ["-i", str(f)]
        # one pixel shape for every segment: the split screen came out tagged SAR 1:1 and the others not, and xfade
        # refuses to join them ("Error reinitializing filters"; every 4:5 Us vs supermarket failed, 2026-10-26)
        graph, last, acc = [f"[{k}:v]setsar=1[s{k}]" for k in range(len(files))], "[s0]", 0.0
        if t <= 0 and len(files) > 1:          # hard cuts (Fast-cut montage)
            graph.append("".join(f"[s{k}]" for k in range(len(files))) + f"concat=n={len(files)}:v=1:a=0[xc]")
            last = "[xc]"
        for k in range(1, len(files) if t > 0 else 1):
            acc += segs[k - 1]["dur"] - t
            graph.append(f"{last}[s{k}]xfade=transition=fade:duration={t}:offset={acc:.3f}[x{k}]")
            last = f"[x{k}]"
        n = len(files)

        overlays: list[tuple[Path, float, float]] = []
        busy: list[tuple[float, float]] = []
        for j, item in enumerate(tl.get("text") or []):
            layer = tmp / f"text{j}.png"
            if item.get("template") == "kinetic":
                kinetic_png(item.get("headline", ""), W, H, layer, measured)
            elif item.get("template") in TEXT_LOOKS:   # each text-led format its own type (feedback 2026-10-26)
                TEXT_LOOKS[item["template"]](item.get("headline", ""), W, H, aspect, layer, measured)
            elif item.get("template") == "hook":      # clear of the face and the fruit (feedback 2026-10-26)
                caption_png(item.get("headline", ""), W, H, layer, aspect, item.get("style", "bold"), measured,
                            item.get("place", "low"), 0.09, "hook")
            else:
                frame = videotools.frame_at(files[0], tmp / f"tframe{j}.png", 0.5)
                lm: dict[str, Any] = {}
                render_layer_to(str(frame), str(layer), item.get("template", "bold_type"),
                                {"headline": item.get("headline", ""), "sub": item.get("sub", ""), "cta": item.get("cta", "")}, lm)
                measured.setdefault("boxes", []).extend(lm.get("boxes", []))
                if lm.get("min_contrast") is not None:
                    measured["min_contrast"] = round(min(lm["min_contrast"], measured.get("min_contrast", 99)), 2)
            overlays.append((layer, float(item.get("start", 0)), float(item.get("end", 3))))
            if not item.get("keep_captions"):        # a step chip sits beside the spoken captions
                busy.append((float(item.get("start", 0)), float(item.get("end", 3))))
        cap = tl.get("captions") or {}
        chunks = caption_chunks((tl.get("voice") or {}).get("words") or [], int(cap.get("max_words", 3)))
        if segs and segs[0].get("sticker") and hook_place(segs[0]) == "low":   # the sticker has the captions' place
            busy.append((0.0, segs[0]["dur"]))
        for j, c in enumerate(chunks):
            if any(c["start"] < b and c["end"] > a for a, b in busy):   # on-screen text speaks for itself
                continue
            overlays.append((caption_png(c["text"], W, H, tmp / f"cap{j}.png", aspect, cap.get("style", "bold"), measured,
                                         cap.get("place", "low")), c["start"], c["end"]))
        n = len(files)
        if overlays:
            # every text layer in ONE input: a list of still frames with durations (blank between them),
            # so a 30 s story with 30 captions opens 1 overlay stream, not 30
            args += ["-f", "concat", "-safe", "0", "-i", str(overlay_track(overlays, W, H, total, tmp))]
            graph.append(f"[{n}:v]fps={fps},format=rgba[ovl]")
            graph.append(f"{last}[ovl]overlay=0:0:eof_action=pass,format=yuv420p[vout]")
            n += 1
        else:
            graph.append(f"{last}format=yuv420p[vout]")

        # audio: voice, ducked music, sound effects; silence if there is none
        a_in = n
        mixes: list[str] = []
        voice = tl.get("voice")
        if voice:
            args += ["-i", str(voice["src"])]
            vi = a_in
            a_in += 1
            graph.append(f"[{vi}:a]adelay={int(float(voice.get('start', 0)) * 1000)}:all=1,aresample=48000,asplit=2[vo][vkey]")
            mixes.append("[vo]")
        music = tl.get("music")
        if music:
            args += ["-stream_loop", "-1", "-i", str(music["src"])]
            mi = a_in
            a_in += 1
            graph.append(f"[{mi}:a]aresample=48000,volume={float(music.get('gain_db', -16))}dB[mraw]")
            if voice:
                # the key must last the whole video: sidechaincompress stops when either input ends, which
                # silenced the music under the end card once the voice-over finished
                graph.append(f"[vkey]apad=whole_dur={total:.3f}[vkeyp]")
                graph.append("[mraw][vkeyp]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=400[mduck]")
                mixes.append("[mduck]")
            else:
                mixes.append("[mraw]")
        elif voice:
            graph.append("[vkey]anullsink")
        for k, fx in enumerate(tl.get("sfx") or []):
            args += ["-i", str(fx["src"])]
            graph.append(f"[{a_in}:a]adelay={int(float(fx.get('start', 0)) * 1000)}:all=1,"
                         f"volume={float(fx.get('gain_db', -6))}dB,aresample=48000[fx{k}]")
            mixes.append(f"[fx{k}]")
            a_in += 1
        if mixes:
            graph.append(f"{''.join(mixes)}amix=inputs={len(mixes)}:duration=longest:normalize=0,"
                         f"atrim=0:{total:.3f},apad=whole_dur={total:.3f}[aout]")
        else:
            args += ["-f", "lavfi", "-t", f"{total:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
            graph.append(f"[{a_in}:a]anull[aout]")
        videotools.run([*args, "-filter_complex", ";".join(graph), "-map", "[vout]", "-map", "[aout]",
                        "-t", f"{total:.3f}", *videotools._x264(final=True), "-c:a", "aac", "-b:a", "160k",
                        "-movflags", "+faststart", str(out)], timeout=1800)
    return {"path": str(out), "duration": total, "captions": len(chunks), "measured": measured,
            "export": videotools.ensure_export(out)}


def dumps(tl: dict[str, Any]) -> str:
    return json.dumps(tl, indent=1)
