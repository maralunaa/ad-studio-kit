"""
Video Studio V1: animate an approved still (an Images-tab image or a Core shot).

  create()          checks the source, the caps and the inputs; queues an "animate" job
  process()         in the worker:
      1. a 9:16 start still, never cropped: kept as is if it already is 9:16, else extended
         ("outpaint": the square/4:5 photo continued above and below) or regenerated at 9:16
         from the image's own prompt ("native"). ANIMATE_STILL_STRATEGY picks the default.
      2. a 5–8 s clip from it (Replicate video model, CI_VIDEO_MODEL, image-to-video)
      3. video QA (video_qa.py); a failing clip is quietly made once more with QA's reasons
      4. the finished versions: 9:16 and 4:5 (padded, not cropped), each with the copy drawn by
         code (the Compose template as a transparent layer laid over the clip by ffmpeg)
  render_outputs()  more versions of a finished clip (other headlines) without new generation

Every paid step is checked first against the video job cap and the daily video cap (budget.py).
Costs are logged against source "video" + the video's id, kinds "video" and "video-still".
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from creative_intelligence import budget, prompts
from creative_intelligence.adstudio import costs, prelaunch, videotools

# Motion presets per product type. "safe" ones keep the fruit still (camera moves only) or show one
# hero fruit; the others change the scene and get the same QA, but drift is more likely.
PRESETS: dict[str, list[dict[str, Any]]] = {
    "fruit": [
        {"key": "push_in", "label": "Gentle push-in", "safe": True,
         "motion": "a very slight, slow camera push-in, no more than about 10% closer by the end; the fruit itself "
                   "stays perfectly still and keeps its exact size, shape and position in the frame"},
        {"key": "orbit", "label": "Slow orbit", "safe": True,
         "motion": "the camera slowly orbits about 15 degrees around the fruit at the same height and distance; "
                   "the fruit stays still and the same size"},
        {"key": "hand_turn", "label": "Hand turning the fruit", "safe": False,
         "motion": "one hand slowly turns the fruit a quarter turn to show its skin; the hand has five natural fingers"},
        {"key": "slow_cut", "label": "Slow cut", "safe": False,
         "motion": "a knife slowly finishes one clean cut through the fruit and the half falls open to reveal the flesh; "
                   "the camera is still"},
        {"key": "juice_drip", "label": "Juice drip", "safe": False,
         "motion": "a single drop of juice slowly runs down the cut flesh, macro view, the camera is still"},
        {"key": "box_lid", "label": "Box lid lifting", "safe": False,
         "motion": "the lid of the plain white box lifts slowly to reveal the fruit inside; the camera is still"},
    ],
    "candle": [
        {"key": "push_in", "label": "Gentle push-in", "safe": True,
         "motion": "a slow, steady camera push-in toward the product; the product stays perfectly still"},
        {"key": "flame", "label": "Flame flicker", "safe": True,
         "motion": "the candle flame flickers softly and the light moves gently across the jar; nothing else moves"},
    ],
    "other": [
        {"key": "push_in", "label": "Gentle push-in", "safe": True,
         "motion": "a slow, steady camera push-in toward the product; the product stays perfectly still"},
        {"key": "orbit", "label": "Slow orbit", "safe": True,
         "motion": "the camera slowly orbits about 15 degrees around the product; the product stays still"},
    ],
}
DURATIONS = (5, 6, 7, 8)
ASPECTS = ("9:16", "4:5")
MAX_ATTEMPTS = 2                 # the first clip and at most one quiet replacement
QA_ESTIMATE_USD = 0.05


def strategy_default() -> str:
    s = os.getenv("ANIMATE_STILL_STRATEGY", "outpaint")
    return s if s in ("outpaint", "native") else "outpaint"


def presets_for(product: dict[str, Any] | None) -> list[dict[str, Any]]:
    from creative_intelligence.adstudio.profiles import normalize_type
    return PRESETS.get(normalize_type((product or {}).get("product_type")), PRESETS["other"])


def product_line(product: dict[str, Any] | None) -> str:
    """Who the product is, from its own setup (any product: name, look whole and cut, varieties)."""
    p = product or {}
    bits = [p.get("name") or "the product"]
    for k in ("physical_desc", "varieties"):
        if (p.get(k) or "").strip():
            bits.append(p[k].strip())
    return ". ".join(bits)


def motion_prompt(product: dict[str, Any] | None, preset: str, custom: str = "") -> str:
    motion = custom.strip() or next((p["motion"] for p in presets_for(product) if p["key"] == preset), "")
    if not motion:
        raise ValueError("Pick a motion or describe your own.")
    return prompts.text("animate-motion-rules", motion=motion, product=product_line(product))


# ── sources ─────────────────────────────────────────────────────────────────
def source_row(conn, kind: str, sid: int) -> dict[str, Any] | None:
    table = {"image": "adstudio_images", "core": "adstudio_core"}.get(kind)
    if not table:
        return None
    r = conn.execute(f"SELECT * FROM {table} WHERE id=?", (sid,)).fetchone()
    return dict(r) if r else None


def check_source(row: dict[str, Any] | None, kind: str) -> str:
    """'' if this still may be animated, else the reason it may not."""
    if not row or not row.get("image_path") or not Path(row["image_path"]).exists():
        return "That picture isn't there any more."
    if kind == "image" and row.get("review_status") != "approved":
        return "Approve the image first: only approved images are animated."
    if kind == "core" and row.get("status") not in (None, "done"):
        return "That Core shot isn't finished yet."
    return ""


def estimate(conn, duration: int, needs_still: bool) -> float:
    from creative_intelligence.providers import model
    still = costs.price(conn, model("image")) if needs_still else 0.0
    return round(still + costs.price(conn, model("video")) * duration + QA_ESTIMATE_USD, 2)


def _is_916(path: str) -> bool:
    """A missing or unreadable file counts as 'not 9:16' (it would need outpainting), so one lost image never breaks
    a whole listing."""
    from PIL import Image
    try:
        w, h = Image.open(path).size
    except (OSError, ValueError):
        return False
    return abs(w / h - 9 / 16) < 0.02


def create(conn, product_id: int, source_kind: str, source_id: int, preset: str = "push_in", custom: str = "",
           duration: int = 5, copy: dict[str, Any] | None = None, strategy: str | None = None) -> tuple[int | None, str]:
    """Queue an animation. Returns (video_id, '') or (None, reason)."""
    from creative_intelligence import jobs
    from creative_intelligence.adstudio.profiles import prompt_stamp
    from creative_intelligence.providers import model
    from creative_intelligence.webapp.common import _product_dict
    row = source_row(conn, source_kind, source_id)
    why = check_source(row, source_kind)
    if why:
        return None, why
    if row["product_id"] != product_id:
        return None, "That picture belongs to another product."
    if duration not in DURATIONS:
        return None, f"Pick a length of {', '.join(map(str, DURATIONS))} seconds."
    product = _product_dict(conn, product_id) or {}
    try:
        motion = motion_prompt(product, preset, custom)
    except ValueError as exc:
        return None, str(exc)
    strategy = strategy if strategy in ("outpaint", "native") else strategy_default()
    est = estimate(conn, duration, not _is_916(row["image_path"]))
    try:
        if est > budget.caps(conn)["video_job"]:
            raise budget.BudgetExceeded(f"This clip would cost about ${est:.2f}, over the video job cap.")
        budget.check(conn, "video", est)
    except budget.BudgetExceeded as exc:
        return None, str(exc)
    stamp = prompts.stamp("animate-motion-rules", "still-outpaint" if strategy == "outpaint" else "still-headroom",
                          "video-qa-system") + "+" + prompt_stamp(product, "image")
    with conn:
        vid = conn.execute(
            "INSERT INTO adstudio_videos (product_id, source_kind, source_id, motion_preset, motion_prompt, duration_s,"
            " still_strategy, model, copy_json, prompt_version, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (product_id, source_kind, source_id, "" if custom.strip() else preset, motion, duration, strategy,
             model("video"), json.dumps(copy or {}), stamp, datetime.utcnow().isoformat(timespec="seconds"))).lastrowid
    jobs.enqueue(conn, "animate", {"video_id": vid}, dedupe_key=f"video:{vid}")
    return vid, ""


# ── the steps (worker) ──────────────────────────────────────────────────────
class Stopped(Exception):
    """Someone pressed Stop: the job ends before its next paid step."""


def check_stop(conn, vid: int) -> None:
    r = conn.execute("SELECT status FROM adstudio_videos WHERE id=?", (vid,)).fetchone()
    if r and r[0] == "stopping":
        raise Stopped()


def request_stop(conn, vid: int) -> bool:
    with conn:
        return conn.execute("UPDATE adstudio_videos SET status='stopping', step='Stopping' WHERE id=?"
                            " AND status IN ('queued','running')", (vid,)).rowcount == 1


def _set(conn, vid: int, **fields: Any) -> None:
    cols = ", ".join(f"{k}=?" for k in fields)
    with conn:
        conn.execute(f"UPDATE adstudio_videos SET {cols} WHERE id=?", (*fields.values(), vid))


def _pad_still(path: str, out: Path) -> str:
    """A still that came back in another shape: fit it into 9:16 with a blurred fill (no crop)."""
    from PIL import Image, ImageEnhance, ImageFilter
    im = Image.open(path).convert("RGB")
    W, H = 1080, 1920
    bg = im.resize((W, int(W * im.height / im.width)) if im.width / im.height < W / H else (int(H * im.width / im.height), H))
    bg = bg.crop(((bg.width - W) // 2, (bg.height - H) // 2, (bg.width - W) // 2 + W, (bg.height - H) // 2 + H))
    bg = ImageEnhance.Brightness(bg.filter(ImageFilter.GaussianBlur(40))).enhance(0.85)
    fg = im.copy()
    fg.thumbnail((W, H))
    bg.paste(fg, ((W - fg.width) // 2, (H - fg.height) // 2))
    bg.save(out, "PNG")
    return str(out)


def _headroom_canvas(photo: str, out: Path) -> Path:
    """A 1080x1920 canvas with the approved photo at the bottom and an empty grey band on top for
    the model to fill: the product ends up low, with room for a headline above it."""
    from PIL import Image
    im = Image.open(photo).convert("RGB")
    im = im.resize((1080, round(1080 * im.height / im.width)))
    if im.height > 1920:                                        # taller than 9:16 already: no band needed
        im = im.resize((round(1920 * im.width / im.height), 1920))
    canvas = Image.new("RGB", (1080, 1920), (128, 128, 128))
    canvas.paste(im, ((1080 - im.width) // 2, 1920 - im.height))
    canvas.save(out, "PNG")
    return out


def _restore_photo(still: str, photo: str) -> str:
    """Put the approved photo back over the bottom of the filled still (soft 60px seam), so the model
    can only have changed the new band, never the product."""
    from PIL import Image
    base = Image.open(still).convert("RGB").resize((1080, 1920))
    im = Image.open(photo).convert("RGB")
    im = im.resize((1080, round(1080 * im.height / im.width)))
    if im.height > 1920:
        return still
    top = 1920 - im.height
    mask = Image.new("L", im.size, 255)
    for y in range(min(60, im.height)):
        for x in range(im.width):
            mask.putpixel((x, y), int(255 * y / 60))
    base.paste(im, (0, top), mask)
    out = Path(still).with_name(Path(still).stem + "_kept.png")
    base.save(out, "PNG")
    return str(out)


def make_still(conn, video: dict[str, Any], src: dict[str, Any], refs: list[str], role: str = "start") -> str:
    """A 9:16 still for the clip (never a crop of the product): the start, or with role='end' an end
    frame (same treatment, not recorded as the video's still)."""
    from creative_intelligence.adstudio.store import to_data_uri
    from creative_intelligence.providers import model, replicate
    from creative_intelligence.storage import storage
    if _is_916(src["image_path"]):
        if role == "start":
            _set(conn, video["id"], still_strategy="as_is", still_path=src["image_path"])
        return src["image_path"]
    check_stop(conn, video["id"])
    budget.check_video_job(conn, video["id"], costs.price(conn, model("image")))
    vdir = storage().area_dir("videos")
    if video["still_strategy"] == "native" and src.get("prompt"):
        prompt = re.sub(r"\b(4:5|1:1|2:3|3:4|16:9)\b", "9:16", src["prompt"]) + "\n\n" + prompts.text("still-headroom")
        url = replicate.generate_image(prompt, refs, "9:16", bucket="video")
    else:
        canvas = _headroom_canvas(src["image_path"], vdir / f"canvas_v{video['id']}.png")
        prompt = prompts.text("still-outpaint", aspect="9:16 (tall, vertical)")
        url = replicate.generate_image(prompt, [to_data_uri(str(canvas), max_dim=1920)] + refs[:2], "9:16",
                                       bucket="video")
    ref = storage().put_url("videos", f"{'still' if role == 'start' else 'end'}_v{video['id']}_{video['still_strategy']}.png", url)
    costs.log_image(conn, model("image"), "video", video["id"], kind="video-still")
    if not _is_916(ref):
        ref = _pad_still(ref, Path(ref).with_name(Path(ref).stem + "_916.png"))
    if video["still_strategy"] != "native":
        ref = _restore_photo(ref, src["image_path"])    # the approved photo itself, pixel for pixel
    if role == "start":
        _set(conn, video["id"], still_path=ref)
    return ref


def veo_still(path: str) -> str:
    """The 9:16 still at exactly 720 x 1280 for Veo (kept beside the original)."""
    from PIL import Image
    out = Path(path).with_name(Path(path).stem + "_720.png")
    if not out.exists():
        with Image.open(path) as im:
            im.convert("RGB").resize((720, 1280), Image.LANCZOS).save(out, "PNG")
    return str(out)


def make_clip(conn, video: dict[str, Any], still: str, attempt: int, corrections: str = "",
              route: str = "kling", audio: bool = False, negative: str = "", end_still: str | None = None) -> str:
    from creative_intelligence.adstudio.store import to_data_uri
    from creative_intelligence.providers import replicate
    from creative_intelligence.storage import storage
    first = replicate.route_model(route)
    check_stop(conn, video["id"])
    budget.check_video_job(conn, video["id"], costs.price(conn, replicate.price_key(first, audio)) * video["duration_s"])
    prompt = video["motion_prompt"] + (f"\nFix from the previous attempt: {corrections}" if corrections else "")
    if "veo" in first:                          # Veo's guide: a 9:16 input image of exactly 720 x 1280
        still = veo_still(still)
    url, used, secs, sound = replicate.generate_clip(
        prompt, to_data_uri(still, max_dim=1536), duration=video["duration_s"], aspect_ratio="9:16", route=route,
        audio=audio, negative=negative, end_image=to_data_uri(end_still, max_dim=1536) if end_still else None)
    ref = storage().put_url("videos", f"clip_v{video['id']}_a{attempt}.mp4", url, min_bytes=10_000)
    costs.log_video(conn, used, secs, "video", video["id"], audio=sound)
    _set(conn, video["id"], model=used, has_audio=int(sound))
    info = videotools.probe(ref)
    if abs(info["width"] / info["height"] - 9 / 16) > 0.02:     # the model kept another shape: pad, don't crop
        ref = str(videotools.fit(ref, Path(ref).with_name(Path(ref).stem + "_916.mp4"), "9:16"))
    return ref


# A layout whose text would sit on the product swaps to its counterpart on the other side of the frame.
OPPOSITE = {"bold_type": "benefit_stack", "direct_response": "benefit_stack", "scene": "benefit_stack",
            "scroll_stopping": "benefit_stack", "seasonal_drop": "benefit_stack", "premium": "lifestyle_tagline",
            "supermarket_contrast": "origin_story", "benefit_stack": "bold_type", "lifestyle_tagline": "premium",
            "origin_story": "supermarket_contrast", "social_proof": "lifestyle_tagline"}
OVERLAP_MAX = 0.12          # share of the product box the text may cover
# Animations (feedback 2026-10-30: "these animations need the cta and text better placed"): when the chosen layout's
# words would sit on the fruit, the copy goes as one stack (headline, line, button) above or below the fruit, on a
# soft shade, whichever covers it least. Only for video: the stills keep their layouts.
VIDEO_STACKS = {
    "top": {"name": "Video stack (top)", "elements": [
        {"type": "scrim", "from": "top", "box": [0, 0.0, 1, 0.5], "strength": 0.5},
        {"type": "text", "field": "headline", "box": [0, 0.0, 1, 0.24], "size": 0.1, "weight": "bold",
         "align": "left", "color": "auto", "max_lines": 3, "upper": True},
        {"type": "text", "field": "sub", "box": [0, 0.25, 1, 0.08], "size": 0.045, "weight": "medium",
         "align": "left", "color": "auto", "max_lines": 2},
        {"type": "button", "field": "cta", "box": [0, 0.34, 1, 0.09], "size": 0.045, "fill": "orange", "align": "left"}]},
    "bottom": {"name": "Video stack (bottom)", "elements": [
        {"type": "scrim", "from": "bottom", "box": [0, 0.5, 1, 0.5], "strength": 0.55},
        {"type": "text", "field": "headline", "box": [0, 0.57, 1, 0.24], "size": 0.1, "weight": "bold",
         "align": "left", "color": "auto", "max_lines": 3, "upper": True},
        {"type": "text", "field": "sub", "box": [0, 0.81, 1, 0.08], "size": 0.045, "weight": "medium",
         "align": "left", "color": "auto", "max_lines": 2},
        {"type": "button", "field": "cta", "box": [0, 0.90, 1, 0.09], "size": 0.045, "fill": "orange", "align": "left"}]},
}


def _box_in(box: list[float] | None, framing: dict) -> list[float] | None:
    """The product box in the output frame's coordinates (a crop shifts it, padding shrinks and centres it)."""
    if not box:
        return None
    if framing.get("mode") == "crop":
        t, h = framing["top"], framing["height"]
        return [box[0], (box[1] - t) / h, box[2], (box[3] - t) / h]
    if framing.get("mode") == "pad" and "sx" in framing:
        sx, sy, ox, oy = framing["sx"], framing["sy"], framing["ox"], framing["oy"]
        return [ox + box[0] * sx, oy + box[1] * sy, ox + box[2] * sx, oy + box[3] * sy]
    return box


def overlap(template: str, box: list[float] | None, w: int, h: int) -> float:
    """Share of the product box covered by the template's text and panels (0 if unknown)."""
    from creative_intelligence.adstudio.render_template import text_regions
    if not box:
        return 0.0
    bx1, by1, bx2, by2 = box[0] * w, box[1] * h, box[2] * w, box[3] * h
    area = max(1.0, (bx2 - bx1) * (by2 - by1))
    covered = 0.0
    for x1, y1, x2, y2 in text_regions(template, w, h):
        covered += max(0.0, min(x2, bx2) - max(x1, bx1)) * max(0.0, min(y2, by2) - max(y1, by1))
    return min(1.0, covered / area)


def choose_layout(template: str, box: list[float] | None, w: int, h: int) -> tuple[str, str]:
    """(template to use, note). Keeps the chosen layout unless its text covers the product and the
    counterpart covers less."""
    mine = overlap(template, box, w, h)
    if mine <= OVERLAP_MAX:
        return template, ""
    alt = OPPOSITE.get(template)
    options = ([(overlap(alt, box, w, h), alt, f"Text moved to keep the product clear ({template} → {alt})")]
               if alt else []) + [(overlap(st, box, w, h), st, f"Text stacked {'above' if side == 'top' else 'below'} the product")
                                  for side, st in VIDEO_STACKS.items()]
    best = min(options, key=lambda o: o[0])
    if best[0] < mine:
        return best[1], best[2]
    return template, "Text overlaps the product"


def render_outputs(conn, video_id: int, copies: list[dict[str, Any]], aspects: tuple[str, ...] = ASPECTS) -> list[int]:
    """Finished versions of a clip: each copy × each size. No generation, only ffmpeg/PIL work.
    The 4:5 version is framed around the product (background cropped, product never); text layouts
    that would cover the product switch to their counterpart on the other side."""
    from creative_intelligence.adstudio import video_qa
    from creative_intelligence.adstudio.render_template import render_layer_to
    from creative_intelligence.storage import storage
    v = dict(conn.execute("SELECT * FROM adstudio_videos WHERE id=?", (video_id,)).fetchone())
    box = video_qa.clean_box(json.loads(v["subject_box"])) if v.get("subject_box") else None
    if box is None and any(c.get("template") for c in copies or []) or (box is None and "4:5" in aspects):
        try:
            box = video_qa.subject_box(conn, video_id, v["clip_path"])
        except Exception:
            box = None                                   # unknown: pad, and keep the chosen layout
    vdir = storage().area_dir("videos")
    made = []
    for aspect in aspects:
        base = vdir / f"base_v{video_id}_{aspect.replace(':', 'x')}.mp4"
        framing_file = base.with_suffix(".json")
        if base.exists() and framing_file.exists():
            framing = json.loads(framing_file.read_text())
        else:
            if aspect == "9:16":
                videotools.fit(v["clip_path"], base, aspect)
                framing = {"mode": "same"}
            else:
                _, framing = videotools.frame(v["clip_path"], base, aspect, box)
            framing_file.write_text(json.dumps(framing))
        out_box = _box_in(box, framing)
        W, H = videotools.SIZES[aspect]
        for copy in copies or [{}]:
            now = datetime.utcnow().isoformat(timespec="seconds")
            with conn:
                rid = conn.execute(
                    "INSERT INTO adstudio_video_renders (video_id, product_id, aspect, template, headline, subhead, cta,"
                    " hook_id, status, created_at) VALUES (?,?,?,?,?,?,?,?, 'rendering', ?)",
                    (video_id, v["product_id"], aspect, copy.get("template", ""), copy.get("headline", ""),
                     copy.get("sub", ""), copy.get("cta", ""), copy.get("hook_id"), now)).lastrowid
            out = vdir / f"video_v{video_id}_r{rid}_{aspect.replace(':', 'x')}.mp4"
            note = ""
            measured: dict = {}
            if copy.get("template") and (copy.get("headline") or copy.get("sub") or copy.get("cta")):
                template, note = choose_layout(copy["template"], out_box, W, H)
                with tempfile.TemporaryDirectory() as tmp:
                    frame = videotools.frame_at(base, Path(tmp) / "frame.png", 1.0)
                    layer = Path(tmp) / "layer.png"
                    render_layer_to(str(frame), str(layer), template,
                                    {"headline": copy.get("headline", ""), "sub": copy.get("sub", ""),
                                     "cta": copy.get("cta", "")}, measured)
                    videotools.overlay(base, layer, out)
            else:
                import shutil
                shutil.copy2(base, out)
            export = videotools.ensure_export(out)
            poster = videotools.frame_at(out, out.with_suffix(".jpg"), 1.0, 540)
            prelaunch.record_video(conn, rid, measured, export)
            with conn:
                conn.execute("UPDATE adstudio_video_renders SET output_path=?, poster_path=?, status='done', note=?,"
                             " template=COALESCE(NULLIF(?, ''), template) WHERE id=?",
                             (str(out), str(poster), note, template if note.startswith("Text moved") and
                              isinstance(template, str) else "", rid))
            made.append(rid)
    return made


def process(video_id: int, conn_factory: Callable[[], Any]) -> dict[str, Any]:
    """The animate job, end to end (worker). Records failures on the video instead of raising,
    except a budget stop, which also fails the job so it shows on Admin."""
    from creative_intelligence.adstudio import video_qa
    from creative_intelligence.webapp.common import _primary_photo_uris, _product_dict
    conn = conn_factory()
    try:
        video = dict(conn.execute("SELECT * FROM adstudio_videos WHERE id=?", (video_id,)).fetchone())
        if video["status"] == "stopping":
            _set(conn, video_id, status="failed", step="", message="Stopped before it started; nothing was spent.")
            return {"stopped": True}
        if video["status"] not in ("queued",):
            return {"skipped": video["status"]}
        _set(conn, video_id, status="running", step="Preparing the still")
        src = source_row(conn, video["source_kind"], video["source_id"])
        product = _product_dict(conn, video["product_id"]) or {}
        refs = _primary_photo_uris(conn, video["product_id"], limit=3)
        still = make_still(conn, video, src, refs)
        corrections, q = "", {}
        for attempt in range(1, MAX_ATTEMPTS + 1):
            _set(conn, video_id, step="Making the clip" if attempt == 1 else "Making a better clip", attempts=attempt)
            clip = make_clip(conn, video, still, attempt, corrections)
            _set(conn, video_id, clip_path=clip, step="Checking the clip")
            q = video_qa.score(conn, video_id, clip, refs, product)
            if q.get("total") is None or q.get("passed"):
                break                                   # passed, or QA couldn't run: keep it, unscored
            corrections = "; ".join(q.get("failures") or []) + ". " + (q.get("reasoning") or "")
        _set(conn, video_id, step="Adding the text")
        copy = json.loads(video["copy_json"] or "{}")
        render_outputs(conn, video_id, [copy] if copy else [])
        spent = budget.spent_on_video(conn, video_id)
        verdict = ("passed QA" if q.get("passed") else "did not pass QA" if q.get("total") is not None
                   else "not scored")
        _set(conn, video_id, status="done", step="", finished_at=datetime.utcnow().isoformat(timespec="seconds"),
             message=f"{verdict} after {attempt} clip(s); ${spent:.2f}")
        return {"video_id": video_id, "passed": q.get("passed"), "spent": spent}
    except Stopped:
        _set(conn, video_id, status="failed", step="", message="Stopped; nothing more was spent.")
        return {"video_id": video_id, "stopped": True}
    except budget.BudgetExceeded as exc:
        _set(conn, video_id, status="failed", step="", message=str(exc))
        raise
    except Exception as exc:
        _set(conn, video_id, status="failed", step="", message=f"{type(exc).__name__}: {exc}"[:400])
        raise
    finally:
        conn.close()
