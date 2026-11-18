"""
Video QA (Video Studio): score a clip with the QA rubric (config/qa_rubric.yaml, criteria that
apply to "videos") from sampled frames.

Claude sees the reference photos, a numbered contact sheet (one frame per second, last frame at
the end), then the first and last frames. Besides the 1–5 scores it counts the whole fruits in
the first and last frame; a different count fails the clip whatever the scores (drift check).
Stored in qa_scores as source "video", like image scores, so badges and overrules work the same.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from creative_intelligence import prompts
from creative_intelligence.adstudio import qa, videotools


BOX = {"type": "array", "items": {"type": "number"},
       "description": "The smallest box around the fruit or product itself (NOT hands, arms or props) in BOTH the "
                      "first and the last frame together: [left, top, right, bottom] as fractions of the frame width "
                      "and height, 0 to 1."}


def _schema(keys: list[str]) -> dict[str, Any]:
    return {"type": "object", "additionalProperties": False,
            "required": ["scores", "fruit_count_first", "fruit_count_last", "subject_box", "reasoning"],
            "properties": {
                "scores": {"type": "object", "additionalProperties": False, "required": keys,
                           "properties": {k: {"type": "integer", "enum": [1, 2, 3, 4, 5]} for k in keys}},
                "fruit_count_first": {"type": "integer", "description": "whole fruits visible in the first frame"},
                "fruit_count_last": {"type": "integer", "description": "whole fruits visible in the last frame"},
                "subject_box": BOX,
                "reasoning": {"type": "string", "description": "One or two sentences: the main problems, if any."}}}


PRESENTER_NOTE = ("This is a TALKING-HEAD clip of an AI presenter; the reference photo is the presenter's approved face. "
                  "Judge: does the mouth move in time with the speech (lip-sync), is it the same face as the reference from "
                  "first to last frame, and are hands and teeth natural. Score 'consistency' on face identity.")
# 2026-11-02: every Claymation and cartoon video failed "never-include rules" (score 1) for being clay or cartoon, the
# fruit's never-include list bans cartoon looks for normal ads. For the formats that are clay or cartoon by design:
STYLIZED_NOTE = ("This video is DELIBERATELY claymation or animated-cartoon (the format), not a photograph: a clay or "
                 "cartoon look, a clay box or props, and an animated fruit character with a face are INTENDED and never "
                 "break the never-include rules (judge those rules only on everything else: other fruit, claims, text, "
                 "people). Judge fruit accuracy as: clearly the same kind of fruit, same colours and markings, in clay or "
                 "as a cartoon. A real product photo with a call to action as the final end card is expected.")
STYLIZED_STYLES = ("claymation", "fruit_characters")
# 2026-11-03: presenter videos lost points for the presenter "competing with" the fruit or "being the focal point".
# In these formats a person talking to camera IS the format; the fruit is what they show and talk about.
ON_CAMERA_NOTE = ("This video is DELIBERATELY presenter-led (the format): a person talks to camera about the fruit, on "
                  "screen the whole video or cut out in a corner over the product shots. The presenter being on screen, "
                  "large or the focal point is INTENDED and is never a fault for appeal, product focus or never-include "
                  "rules; judge the fruit wherever it is shown, and judge the presenter on natural lip-sync, a steady face "
                  "and natural hands. Cuts between takes of the same presenter are expected.")
ON_CAMERA_STYLES = ("talking_head", "presenter_table")


def note_for(style: str) -> str:
    """The format's own note for the finished video's quality check."""
    return STYLIZED_NOTE if style in STYLIZED_STYLES else ON_CAMERA_NOTE if style in ON_CAMERA_STYLES else ""
MONTAGE_NOTE = ("This video is a MONTAGE of separate shots cut together (a story). Judge each shot on its own; "
                "different fruit counts, angles or scenes between shots are expected, not a fault.")


def montage(rubric: dict[str, Any]) -> dict[str, Any]:
    """Stories cut between library shots (each already QA-passed): no first-to-last continuity criterion."""
    import copy
    r = copy.deepcopy(rubric)
    r["criteria"] = [c for c in r["criteria"] if c["key"] != "consistency"]
    return r


def ask_claude(clip_path: str, ref_uris: list[str], product: dict[str, Any], rubric: dict[str, Any],
               client: Any = None, note: str = "") -> tuple[dict[str, Any], Any]:
    from creative_intelligence.adstudio.chat import _client, _image_blocks, _product_line
    from creative_intelligence.adstudio.profiles import profile
    from creative_intelligence.adstudio.textcheck import _image_block
    crit = [c for c in rubric["criteria"] if qa.applies(c, "video") and not c.get("measured")]
    with tempfile.TemporaryDirectory() as tmp:
        frames = videotools.sample_frames(clip_path, Path(tmp) / "f", every_s=1.0, width=480)
        sheet = videotools.contact_sheet(frames, Path(tmp) / "sheet.jpg")
        images = _image_blocks(ref_uris[:3]) + [_image_block(str(sheet)), _image_block(str(frames[0])),
                                                 _image_block(str(frames[-1]))]
    lines = "\n".join(f"- {c['key']} ({c['name']}): {c['question']}" for c in crit)
    ask = (f"{_product_line(product)}\n\n" + (note + "\n\n" if note else "") + f"NEVER-INCLUDE RULES for this product type:\n{profile(product)['negative']}\n\n"
           f"Images, in order: {len(ref_uris[:3])} reference photo(s); the contact sheet ({len(frames)} frames); "
           f"the first frame; the last frame.\n\nScore the CLIP 1–5 on each criterion:\n{lines}")
    client = client or _client(log=False, kind="qa")
    resp = client.beta.messages.create(
        model=qa.MODEL, max_tokens=4000, betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        system=prompts.text("video-qa-system"),
        output_config={"format": {"type": "json_schema", "schema": _schema([c["key"] for c in crit])}},
        messages=[{"role": "user", "content": images + [{"type": "text", "text": ask}]}])
    if resp.stop_reason == "refusal":
        raise RuntimeError("Claude declined to score this clip")
    return json.loads(next(b.text for b in resp.content if b.type == "text")), resp.usage


def clean_box(b: Any) -> list[float] | None:
    """[x1, y1, x2, y2] in 0..1, ordered, at least a little area; else None."""
    try:
        x1, y1, x2, y2 = (min(1.0, max(0.0, float(v))) for v in b)
    except (TypeError, ValueError):
        return None
    x1, x2, y1, y2 = min(x1, x2), max(x1, x2), min(y1, y2), max(y1, y2)
    return [round(x1, 3), round(y1, 3), round(x2, 3), round(y2, 3)] if (x2 - x1) * (y2 - y1) > 0.005 else None


def subject_box(conn, video_id: int, clip_path: str, client: Any = None) -> list[float] | None:
    """Locate the product in a clip that has no box yet (one small Claude call). Stored on the video."""
    from creative_intelligence.adstudio.chat import _client
    from creative_intelligence.adstudio.textcheck import _image_block
    with tempfile.TemporaryDirectory() as tmp:
        frames = videotools.sample_frames(clip_path, Path(tmp) / "f", every_s=2.0, width=480)
        images = [_image_block(str(frames[0])), _image_block(str(frames[-1]))]
    client = client or _client(log=False, kind="qa")
    resp = client.beta.messages.create(
        model=qa.MODEL, max_tokens=1000, betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        output_config={"format": {"type": "json_schema", "schema": {
            "type": "object", "additionalProperties": False, "required": ["subject_box"], "properties": {"subject_box": BOX}}}},
        messages=[{"role": "user", "content": images + [{"type": "text", "text":
                   "These are the first and last frames of one product video. Give the box around the product."}]}])
    qa._log_cost(conn, resp.usage, "video", video_id)
    box = clean_box(json.loads(next(b.text for b in resp.content if b.type == "text")).get("subject_box"))
    if box:
        with conn:
            conn.execute("UPDATE adstudio_videos SET subject_box=? WHERE id=?", (json.dumps(box), video_id))
    return box


def strict(rubric: dict[str, Any]) -> dict[str, Any]:
    """Multi-fruit or busy shots (shot library): the same fruit first to last exactly, and very little warping."""
    import copy
    r = copy.deepcopy(rubric)
    for c in r["criteria"]:
        if c["key"] == "consistency":
            c["min"] = 5
        elif c["key"] == "motion_artefacts":
            c["min"] = 4
    return r


def score(conn, video_id: int, clip_path: str, ref_uris: list[str], product: dict[str, Any],
          client: Any = None, strict_qa: bool = False, count_check: bool = True,
          is_montage: bool = False, presenter: bool = False, skip: list[str] | None = None,
          note: str = "") -> dict[str, Any]:
    """Score one clip and store it (qa_scores, source 'video'). Never raises. A montage (a story cut from
    library shots) has no continuity criterion and no fruit-count check."""
    rubric = strict(qa.load_rubric()) if strict_qa else qa.load_rubric()
    if strict_qa and not count_check:         # the fruit changes on purpose (cut, torn, a slice taken): "same fruit"
        for c in rubric["criteria"]:          # can't be a perfect 5 (plated scored 4.29 and failed, 2026-10-26)
            if c["key"] == "consistency":
                c["min"] = 4
    if is_montage:
        rubric, count_check = montage(rubric), False
    if skip:                                  # a shot card that deliberately differs (e.g. the supermarket fruit)
        import copy
        rubric = copy.deepcopy(rubric)
        rubric["criteria"] = [c for c in rubric["criteria"] if c["key"] not in skip]
    try:
        reading, usage = ask_claude(clip_path, ref_uris, product, rubric, client,
                                    " ".join(x for x in (MONTAGE_NOTE if is_montage else PRESENTER_NOTE if presenter else "",
                                                         note) if x))
        cost = qa._log_cost(conn, usage, "video", video_id)
        scores = dict(reading["scores"])
        first, last = reading.get("fruit_count_first"), reading.get("fruit_count_last")
        note = reading.get("reasoning", "")
        if count_check and first is not None and last is not None and first != last:
            scores["consistency"] = 1                   # fruit appeared or vanished: always a fail
            note = f"Fruit count changed from {first} to {last}. " + note
        row = qa.save(conn, "video", video_id, rubric, scores, note, cost)
        box = clean_box(reading.get("subject_box"))
        if box:
            with conn:
                conn.execute("UPDATE adstudio_videos SET subject_box=? WHERE id=?", (json.dumps(box), video_id))
        row.update(fruit_count_first=first, fruit_count_last=last, subject_box=box)
        return row
    except Exception as exc:
        return qa.save(conn, "video", video_id, rubric, {}, "", error=str(exc)[:200])
