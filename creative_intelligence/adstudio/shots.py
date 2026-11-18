"""
Video Studio V2: the shot library. Reusable clips per product and shot type, each from a recipe
card (prompts/shots/<type>.vN.yaml), generated once and used again by later videos.

  library()      every card for a product: its best passing shot (QA, reuse count, cost) or what's missing
  passing()      the library-first check: a passing, not retired shot of this product and type, or None
  create()       library first; otherwise queue a "shot" job (estimate and caps checked first)
  process()      in the worker:
      1. the start still (9:16, never cropped): an approved image of a type the card lists
         (from_images), extended like Animate; otherwise generated from the card's locks / freedom /
         composition / lighting plus the product type's reference lock
      2. the clip through the card's route (Kling / Veo / Seedance, fallbacks in config), with the
         card's sound setting and negative prompt
      3. video QA; cards marked strict_qa (multi-fruit, busy scenes) need the same fruit first to last
         exactly and very little warping; one quiet retake, as for Animate
  looks          a batch video's own shots (feedback 2026-10-23: every format reused the same clips, so the videos
                 looked alike): its opening and two beats, made for it in its format's look (framing, light, camera),
                 tagged "+look:<tag>:<style>+" in prompt_version. The shared library never offers them to other videos.
  retire()       take a shot out of use (kept for the record); a new one can then be generated
  use()          count a reuse (timelines call this when they take a shot from the library)
  upload_real()  the brand's own footage (brief S4): customer reactions, interviews, testimonials, the founder story.
                 Tagged real (shot_type "real:<kind>"), never QA'd as AI, never offered to AI styles or mixed
                 with AI people; who filmed it, when, and the on-camera consent are kept with it.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from creative_intelligence import budget, prompts
from creative_intelligence.adstudio import animate, costs

RECOVERY = {   # banana-claude's failure routing, as retake instructions
    "consistency": "Keep exactly the same fruits: same number, shape and size from the first frame to the last.",
    "motion_artefacts": "Slower, simpler motion; nothing melts, morphs or flickers.",
    "fruit_accuracy": "Match the reference photos exactly: skin colour, markings, proportions, flesh.",
    "hands": "One natural adult hand, five fingers, slow movement.",
}


LOOKS: dict[str, dict[str, str]] = {        # format → how its own shots are framed, lit and moved
    "fruit_story": {"composition": "Wide establishing frame with room around the fruit, eye level.",
                    "lighting": "Soft golden late-afternoon light.", "camera": "a slow, steady dolly in"},
    "talking_head": {"composition": "Clean medium frame, the fruit off-centre to leave room for captions.",
                     "lighting": "Even, bright daylight.", "camera": "a gentle locked-off frame with a slight drift"},
    "sensory": {"composition": "Extreme close-up in phone macro mode, the texture filling the frame.",
                "lighting": "Low-key side light that rakes across the surface; dark background.",
                "camera": "a very slow macro push-in"},
    "pov": {"composition": "First-person point of view: the viewer's own hands in frame, top-down at a table.",
            "lighting": "Natural home window light.", "camera": "a handheld first-person move"},
    "us_vs_supermarket": {"composition": "Straight-on frame on a plain surface, the fruit centred like a product shot.",
                          "lighting": "Bright, clean daylight.", "camera": "a slow orbit around the fruit"},
    "kinetic": {"composition": "Graphic top-down flat lay on a single bold colour surface, lots of clean space.",
                "lighting": "Flat, even daylight.", "camera": "a quick, punchy push-in"},
    "fast_cut_montage": {"composition": "Tight, dynamic angle, low and close.",
                         "lighting": "Contrasty, saturated light.", "camera": "a fast handheld whip-in"},
    "how_to_eat": {"composition": "Overhead on a cutting board, hands clearly visible.",
                   "lighting": "Bright kitchen window light.", "camera": "a steady overhead frame"},
}
DEFAULT_LOOK = {"composition": "A fresh angle, different from a straight-on product shot.",
                "lighting": "Natural light.", "camera": "a slow push-in"}
_LOOK = re.compile(r"\+look:([a-z0-9.]+):([a-z_]+)\+")


def look_of(prompt_version: str | None) -> tuple[str, str] | None:
    """(tag, style) of a batch video's own shot, or None for a library shot."""
    m = _LOOK.search(prompt_version or "")
    return (m[1], m[2]) if m else None


def with_look(c: dict[str, Any], style: str) -> dict[str, Any]:
    """The card with the format's framing, light and camera in place of its own (locks, pitfalls and QA stay)."""
    lk = LOOKS.get(style, DEFAULT_LOOK)
    return {**c, "still": {**c["still"], "composition": lk["composition"], "lighting": lk["lighting"]},
            "motion": {**c["motion"], "camera": lk["camera"]}}


def own_ok(shot_type: str) -> bool:
    """Shot types a video may have its own take of: not the character, clay, ripening or supermarket cards."""
    c = prompts.shot_cards().get(shot_type) or {}
    return bool(c) and not (c.get("character") or c.get("cast") or c.get("styles"))


def applies(c: dict[str, Any], product: dict[str, Any] | None) -> bool:
    """Cards say which product types they are for (default fruit); a new product of that type gets them all, except
    the fruits a card names in not_for (2026-11-11: hands can't pull a mango apart, so it came out whole with the cut
    cubes on top); a retired card applies to nothing (the juice shots, "always some syrup")."""
    from creative_intelligence.adstudio.profiles import normalize_type
    name = ((product or {}).get("name") or "").lower()
    if c.get("retired") or any(w.lower() in name for w in c.get("not_for") or []):
        return False
    return normalize_type((product or {}).get("product_type")) in (c.get("product_types") or ["fruit"])


def card(shot_type: str) -> dict[str, Any]:
    cards = prompts.shot_cards()
    if shot_type not in cards:
        raise ValueError(f"No recipe card for {shot_type!r}.")
    return cards[shot_type]


def motion_text(c: dict[str, Any]) -> str:
    s = f"{c['motion']['camera']}; {c['motion']['change']}"
    return s[:1].upper() + s[1:]


def still_prompt(product: dict[str, Any], c: dict[str, Any], facts_text: str = "") -> str:
    from creative_intelligence.adstudio.profiles import profile
    s = c["still"]
    name = product.get("name", "the product")
    look = (product.get("physical_desc") or "").strip()
    stylized = bool(c.get("character") or c.get("styles"))     # the character, clay and ripening keep their look
    return (f"A vertical 9:16 still for a video shot of {name}. "
            f"{profile(product)['reference_lock']}\n"
            + (f"WHAT IT LOOKS LIKE (from the product setup): {look}\n" if look else "")
            + (f"APPROVED FACTS TO MATCH EXACTLY: {facts_text}\n" if facts_text else "")
            + f"\nLOCKS (must not change): {s['locks']}\n"
            f"FREEDOM (may vary): {s['freedom']}\nCOMPOSITION: {s['composition']}\nLIGHTING: {s['lighting']}\n"
            + ("" if stylized else realism("hands") + "\n"
               # 2026-11-04 model test: stills with the box, whole fruit, cut halves and hands all at once made every
               # model add or lose fruit; one clear subject keeps the count
               "Keep the frame simple: one clear subject, only what LOCKS and FREEDOM name; no extra boxes, fruit, "
               "cut pieces or props.\n")
            + "No text, logos, captions or watermarks.")


# 2026-11-05: a fruit-character clip came back with Chinese subtitles burned in; the video models add on-screen text
# unless told not to, so every clip's negatives name it (Seedance, without a negative field, gets it as a sentence)
NO_TEXT = "on-screen text, subtitles, captions, letters, watermark, a third hand, more than two hands"   # 2026-11-11: three hands


def no_text(negative: str = "") -> str:
    return ", ".join(x for x in (negative.strip(" ,."), NO_TEXT) if x)


def realism(who: str = "") -> str:
    """The phone-camera realism every real-world video still asks for (feedback 2026-11-04)."""
    people = {"hands": " Any hands look real: natural skin texture, short nails, natural asymmetry. At most two hands, "
                       "both from one person (2026-11-11: a satsuma clip had three).",
              "person": " Realistic skin texture with visible pores, no airbrushing, natural asymmetry in the face."}
    return prompts.text("realism-phone", people=people.get(who, ""))


def _character_tag(conn, product_id: int, shot_type: str) -> tuple[str, str | None]:
    """Character cards: a shot belongs to the character it was made for (stamped "+character:<id>+" in its
    prompt_version), so picking another character never reuses the old one's shot. ("", None) for other cards;
    (tag, None) when the product has no character in use."""
    if not (prompts.shot_cards().get(shot_type) or {}).get("character"):
        return "", None
    from creative_intelligence.adstudio import characters
    ch = characters.for_product(conn, product_id)
    return ("character", f"+character:{ch['id']}+") if ch else ("character", None)


def passing(conn, product_id: int, shot_type: str, look: str = "") -> dict[str, Any] | None:
    """look: a batch video's own take (its tag); without one, only library shots (never another video's own)."""
    kind, tag = _character_tag(conn, product_id, shot_type)
    if kind and tag is None:
        return None
    lk = f"+look:{look}:" if look else "+look:"
    r = conn.execute(
        "SELECT v.* FROM adstudio_videos v JOIN qa_scores q ON q.source='video' AND q.source_id=v.id"
        " WHERE v.product_id=? AND v.style='shot' AND v.shot_type=? AND v.status='done' AND v.retired=0"
        " AND q.passed=1 AND instr(COALESCE(v.prompt_version, ''), '+cmp:')=0"
        + (" AND instr(v.prompt_version, ?) > 0" if tag else "") +
        f" AND instr(COALESCE(v.prompt_version, ''), ?) {'>' if look else '='} 0"
        # several takes of a type (Fill the library): the least used first, so videos don't all get the same one
        " ORDER BY v.reuse_count ASC, q.total DESC, v.id DESC LIMIT 1", (product_id, shot_type, *([tag] if tag else []), lk)).fetchone()
    return dict(r) if r else None


def best_take(conn, product_id: int, shot_type: str, look: str) -> dict[str, Any] | None:
    """A video's own take of a type with a clip, passing or not, the best first: the model test keeps one model in a
    video even when its take failed QA (feedback 2026-11-04: "i want to see actual video with sounds")."""
    r = conn.execute(
        "SELECT v.* FROM adstudio_videos v LEFT JOIN qa_scores q ON q.source='video' AND q.source_id=v.id"
        " WHERE v.product_id=? AND v.style='shot' AND v.shot_type=? AND v.status='done' AND v.clip_path IS NOT NULL"
        " AND instr(COALESCE(v.prompt_version, ''), ?) > 0 ORDER BY COALESCE(q.passed, 0) DESC, COALESCE(q.total, 0) DESC"
        " LIMIT 1", (product_id, shot_type, f"+look:{look}:")).fetchone()
    return dict(r) if r else None


def passing_takes(conn, product_id: int, shot_type: str) -> list[dict[str, Any]]:
    """Every passing library take of a type, the least used first."""
    if _character_tag(conn, product_id, shot_type)[0]:
        one = passing(conn, product_id, shot_type)
        return [one] if one else []
    return [dict(r) for r in conn.execute(
        "SELECT v.* FROM adstudio_videos v JOIN qa_scores q ON q.source='video' AND q.source_id=v.id"
        " WHERE v.product_id=? AND v.style='shot' AND v.shot_type=? AND v.status='done' AND v.retired=0 AND q.passed=1"
        " AND instr(COALESCE(v.prompt_version, ''), '+cmp:')=0 AND instr(COALESCE(v.prompt_version, ''), '+look:')=0"
        " ORDER BY v.reuse_count ASC, q.total DESC, v.id DESC", (product_id, shot_type))]


KEEP = 3          # Fill the library: passing takes of each shot type (2026-11-02: "2-3 more shots of each type")
GIVE_UP = 2       # takes of the current recipe that failed QA, none passing: not made again until the recipe changes


def takes(conn, product_id: int, shot_type: str) -> dict[str, int]:
    """Library takes of one type: passing (any recipe version), failed and being made (the current recipe)."""
    c = card(shot_type)
    rows = conn.execute(
        "SELECT v.status, v.card_version, v.retired, q.passed, q.total FROM adstudio_videos v LEFT JOIN qa_scores q"
        " ON q.source='video' AND q.source_id=v.id WHERE v.product_id=? AND v.style='shot' AND v.shot_type=?"
        " AND instr(COALESCE(v.prompt_version, ''), '+look:')=0 AND instr(COALESCE(v.prompt_version, ''), '+cmp:')=0",
        (product_id, shot_type)).fetchall()
    return {"passing": sum(1 for r in rows if r["status"] == "done" and r["passed"] and not r["retired"]),
            "failed": sum(1 for r in rows if r["card_version"] == c["version"] and r["status"] in ("done", "failed")
                          and not r["passed"]),
            "busy": sum(1 for r in rows if r["status"] in ("queued", "running"))}


def keeps_failing(conn, product_id: int, shot_type: str) -> bool:
    """2026-11-02: knife cut failed QA four times for Cherry Mango ($8): after GIVE_UP failed takes of the current
    recipe and none passing, videos use another passing shot instead of paying for another try."""
    try:
        t = takes(conn, product_id, shot_type)
    except ValueError:
        return False
    return t["passing"] == 0 and t["failed"] >= GIVE_UP


def fill_plan(conn, product_id: int, per_type: int = KEEP) -> dict[str, Any]:
    """What Fill the library would make: per shot type, the takes still needed to have `per_type` passing ones."""
    from creative_intelligence.webapp.common import _product_dict
    product = _product_dict(conn, product_id) or {}
    out, skipped, usd = [], [], 0.0
    for key, c in prompts.shot_cards().items():
        if not applies(c, product) or c.get("character"):
            continue                                  # a character's scene is made from its look sheet
        t = takes(conn, product_id, key)
        if keeps_failing(conn, product_id, key):
            skipped.append({"type": key, "label": c["label"], "why": f"failed QA {t['failed']} times: its recipe needs work"})
            continue
        n = max(0, per_type - t["passing"] - t["busy"])
        if n:
            each = estimate(conn, c, True)
            out.append({"type": key, "label": c["label"], "have": t["passing"], "make": n, "usd": round(each * n, 2)})
            usd += each * n
    return {"make": out, "skipped": skipped, "takes": sum(x["make"] for x in out), "usd": round(usd, 2)}


def fill(conn, product_id: int, per_type: int = KEEP) -> dict[str, Any]:
    """Queue the takes fill_plan lists, each from its own new still so the takes differ."""
    plan = fill_plan(conn, product_id, per_type)
    try:
        budget.check(conn, "video", plan["usd"])
    except budget.BudgetExceeded as exc:
        return {**plan, "error": str(exc)}
    queued = 0
    for x in plan["make"]:
        for _ in range(x["make"]):
            vid, why, _ = create(conn, product_id, x["type"], extra=True)
            if vid is None:
                return {**plan, "queued": queued, "error": why}
            queued += 1
    return {**plan, "queued": queued}


def approved_still(conn, product_id: int, c: dict[str, Any]) -> dict[str, Any] | None:
    """The best approved image of a type the card can start from (QA passed, best score first)."""
    kinds = c.get("from_images") or []
    if not kinds:
        return None
    r = conn.execute(
        f"SELECT i.* FROM adstudio_images i LEFT JOIN qa_scores q ON q.source='image' AND q.source_id=i.id"
        f" WHERE i.product_id=? AND i.review_status='approved' AND i.image_path IS NOT NULL AND i.archived=0"
        f" AND i.archetype IN ({','.join('?' * len(kinds))}) AND COALESCE(q.passed, 1)=1"
        f" ORDER BY COALESCE(q.total, 0) DESC, i.id DESC LIMIT 1", (product_id, *kinds)).fetchone()
    return dict(r) if r else None


def estimate(conn, c: dict[str, Any], needs_still: bool) -> float:
    from creative_intelligence.providers import model, replicate
    if c.get("timelapse_stages"):           # stills only: the first plus one edit per later stage, no video model
        return round(costs.price(conn, model("image")) * (1 + len(c["timelapse_stages"])) + animate.QA_ESTIMATE_USD, 2)
    m = replicate.route_model(c["route"])
    still = costs.price(conn, model("image")) if needs_still else 0.0
    if c.get("end_from_images"):
        still += costs.price(conn, model("image"))         # the end photo is extended to 9:16 too
    secs = min((4, 6, 8), key=lambda d: abs(d - c["duration_s"])) if "veo" in m else c["duration_s"]
    return round(still + costs.price(conn, replicate.price_key(m, c.get("audio") == "ambient",
                                                                 c["route"] == replicate.PRO)) * secs
                 + animate.QA_ESTIMATE_USD, 2)


def library(conn, product_id: int) -> list[dict[str, Any]]:
    from creative_intelligence.adstudio import qa
    from creative_intelligence.webapp.common import _product_dict
    product = _product_dict(conn, product_id) or {}
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM adstudio_videos WHERE product_id=? AND style='shot' AND instr(COALESCE(prompt_version, ''),"
        " '+look:')=0 AND instr(COALESCE(prompt_version, ''), '+cmp:')=0 ORDER BY id DESC", (product_id,))]
    scores = qa.scores_for(conn, "video", [r["id"] for r in rows])
    out = []
    for key, c in prompts.shot_cards().items():
        if not applies(c, product):
            continue
        mine = [r for r in rows if r["shot_type"] == key]
        best = passing(conn, product_id, key)
        latest = mine[0] if mine else None
        shown = best or latest
        src = approved_still(conn, product_id, c)
        end = approved_still(conn, product_id, {"from_images": c.get("end_from_images") or []})
        out.append({
            "type": key, "label": c["label"], "summary": c["summary"], "use": c["use"], "route": c["route"],
            "audio": c.get("audio", "none"), "strict_qa": bool(c.get("strict_qa")), "card_version": c["version"],
            "duration_s": c["duration_s"], "from_image": (src or {}).get("title"),
            "end_on": c.get("end_from_images") or [], "end_image": (end or {}).get("title"),
            "estimate": estimate(conn, c, not (src and animate._is_916(src["image_path"]))),
            "status": "passing" if best else (latest["status"] if latest else "missing"),
            "shot": None if not shown else {
                "id": shown["id"], "status": shown["status"], "step": shown["step"], "message": shown["message"],
                "model": shown["model"], "reuse_count": shown["reuse_count"], "retired": shown["retired"],
                "qa": scores.get(shown["id"]), "spent_usd": budget.spent_on_video(conn, shown["id"]),
                "created_at": shown["created_at"], "has_audio": shown["has_audio"]},
            "attempts_total": len(mine), "spent_total": round(sum(budget.spent_on_video(conn, r["id"]) for r in mine), 2),
        })
    return out


def prompts_for(conn, product_id: int, shot_type: str, style: str = "") -> dict[str, str]:
    """The picture and motion prompts a video's own take of this type is made from (feedback 2026-11-04: "see the
    prompt and edit"), in the format's look."""
    from creative_intelligence.webapp.common import _product_dict
    product = _product_dict(conn, product_id) or {}
    c = with_look(card(shot_type), style) if style and own_ok(shot_type) else card(shot_type)
    return {"still": still_prompt(product, c),
            "motion": prompts.text("animate-motion-rules", motion=motion_text(c), product=animate.product_line(product))}


def create(conn, product_id: int, shot_type: str, look: str = "", style: str = "",
           extra: bool = False, engine: str = "", still: str = "",
           prompt: dict[str, str] | None = None) -> tuple[int | None, str, bool]:
    """Library first. Returns (video_id, reason, reused): an existing passing shot is returned as is.
    look/style: a batch video's own take in its format's look, always from a new still (see LOOKS).
    extra: one more library take even when there are passing ones (Fill the library), from its own new still.
    engine/still: the model test (replicate.ENGINES): the take is made on that model only, from `still` when given (the
    same still the current model's take started from, so only the model differs).
    prompt: {still?, motion?} written by the person (the storyboard or Edit), in place of the card's."""
    from creative_intelligence import jobs
    from creative_intelligence.adstudio.profiles import prompt_stamp
    from creative_intelligence.providers import replicate
    from creative_intelligence.webapp.common import _product_dict
    try:
        c = card(shot_type)
    except ValueError as exc:
        return None, str(exc), False
    tag = ""
    if c.get("character"):
        from creative_intelligence.adstudio import characters
        try:
            tag = f"+character:{characters.require(conn, product_id)['id']}+"
        except ValueError as exc:
            return None, str(exc), False
    if look:
        if not own_ok(shot_type):
            return None, f"The {c['label']} shot has no per-video take.", False
        c, tag = with_look(c, style), f"{tag}+look:{look}:{style}+"
    if engine:
        c, tag = {**c, "route": engine}, f"{tag}+engine:{engine}+"
    have = None if extra else passing(conn, product_id, shot_type, look)
    if have:
        return have["id"], "", True
    busy = conn.execute("SELECT id FROM adstudio_videos WHERE product_id=? AND style='shot' AND shot_type=?"
                        " AND status IN ('queued','running') AND instr(COALESCE(prompt_version, ''), ?) > 0"
                        + ("" if look else " AND instr(COALESCE(prompt_version, ''), '+look:')=0")
                        + " AND instr(COALESCE(prompt_version, ''), '+cmp:')=0",
                        (product_id, shot_type, tag)).fetchone()
    if busy and not extra:
        return busy["id"], "", False
    product = _product_dict(conn, product_id) or {}
    if not applies(c, product):
        return None, f"The {c['label']} shot isn't made for this product type.", False
    src = None if look or extra else approved_still(conn, product_id, c)   # a look or an extra take: its own new still
    end = approved_still(conn, product_id, {"from_images": c.get("end_from_images") or []})
    if src and end and src["id"] == end["id"]:
        src = None                                       # never start and end on the same photo
    est = estimate(conn, c, not (src and animate._is_916(src["image_path"])))
    try:
        if est > budget.caps(conn)["video_job"]:
            raise budget.BudgetExceeded(f"This shot would cost about ${est:.2f}, over the video job cap.")
        budget.check(conn, "video", est)
    except budget.BudgetExceeded as exc:
        return None, str(exc), False
    motion = ((prompt or {}).get("motion") or "").strip() or prompts.text(
        "animate-motion-rules", motion=motion_text(c), product=animate.product_line(product))
    own_still = ((prompt or {}).get("still") or "").strip()
    stamp = f"shot:{shot_type}@v{c['version']}{tag or '+'}" + ("+edited+" if prompt else "") + prompts.stamp("animate-motion-rules", "video-qa-system") + \
        "+" + prompt_stamp(product, "image")
    with conn:
        vid = conn.execute(
            "INSERT INTO adstudio_videos (product_id, source_kind, source_id, style, motion_preset, motion_prompt,"
            " duration_s, still_strategy, model, prompt_version, shot_type, card_version, route, has_audio, created_at,"
            " still_path, copy_json) VALUES (?,?,?, 'shot', ?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (product_id, "image" if src and not own_still else "card", src["id"] if src and not own_still else 0,
             shot_type, motion, c["duration_s"],
             animate.strategy_default() if src and not own_still else "generated", replicate.route_model(c["route"]),
             stamp, shot_type, c["version"], c["route"], int(c.get("audio") == "ambient"),
             datetime.utcnow().isoformat(timespec="seconds"), still or None,
             json.dumps({"still_prompt": own_still} if own_still else {}))).lastrowid
    jobs.enqueue(conn, "shot", {"video_id": vid}, dedupe_key=f"video:{vid}")
    return vid, "", False


def _generated_still(conn, video: dict[str, Any], product: dict[str, Any], c: dict[str, Any], refs: list[str],
                     tag: str = "") -> str:
    from creative_intelligence.providers import model, replicate
    from creative_intelligence.storage import storage
    animate.check_stop(conn, video["id"])
    budget.check_video_job(conn, video["id"], costs.price(conn, model("image")))
    facts_text = ""
    if c.get("facts_category"):
        from creative_intelligence.adstudio import facts
        facts_text = " ".join(f["text"] for f in facts.list_facts(conn, product.get("id"), facts._family(product))
                              if f["category"] == c["facts_category"])
    mine = (json.loads(video.get("copy_json") or "{}") or {}).get("still_prompt")     # written by the person
    url = replicate.generate_image(mine or still_prompt(product, c, facts_text), refs, "9:16", bucket="video")
    ref = storage().put_url("videos", f"still_v{video['id']}_card{tag}.png", url)
    costs.log_image(conn, model("image"), "video", video["id"], kind="video-still")
    if not animate._is_916(ref):
        ref = animate._pad_still(ref, Path(ref).with_name(Path(ref).stem + "_916.png"))
    animate._set(conn, video["id"], still_path=ref)
    return ref


def _edited_end(conn, video: dict[str, Any], still: str, end_src: dict[str, Any]) -> str:
    """The end frame as an edit of the start still (cards with end_edit): the same board, knife, light and angle,
    the fruit now cut open with the inside of the real cut photo. A start and end from different shoots make the
    video models switch shots mid-clip (Kling's guide; split panels and a second knife in v4)."""
    from creative_intelligence.adstudio.store import to_data_uri
    from creative_intelligence.providers import model, replicate
    from creative_intelligence.storage import storage
    budget.check_video_job(conn, video["id"], costs.price(conn, model("image")))
    url = replicate.generate_image(
        (video.get("_end_edit_prompt") or
         "Edit the first image. Keep everything exactly the same: the board, the knife, the background, the light and "
         "the camera angle. Only change the fruit: it is now cut cleanly in half, the cut faces turned toward the "
         "camera, and its inside looks exactly like the fruit in the second image. One fruit, one knife. No text."),
        [to_data_uri(still), to_data_uri(end_src["image_path"])], "9:16", bucket="video")
    ref = storage().put_url("videos", f"still_v{video['id']}_end_edit.png", url)
    costs.log_image(conn, model("image"), "video", video["id"], kind="video-still")
    if not animate._is_916(ref):
        ref = animate._pad_still(ref, Path(ref).with_name(Path(ref).stem + "_916.png"))
    return ref


def _stage_still(conn, video: dict[str, Any], still: str, stage: str, facts_text: str, n: int) -> str:
    """One later ripeness stage as an edit of the first still: the same fruit, spot, light and angle, so the stills
    play back as a real time-lapse (a video model asked to "ripen over days" drifted or never changed)."""
    from creative_intelligence.adstudio.store import to_data_uri
    from creative_intelligence.providers import model, replicate
    from creative_intelligence.storage import storage
    animate.check_stop(conn, video["id"])
    budget.check_video_job(conn, video["id"], costs.price(conn, model("image")))
    url = replicate.generate_image(
        "Edit the image. Keep everything exactly the same: the counter, the background, the light, the camera angle, "
        "the fruit's position, size and shape, and its variety's own markings (its blush, speckles, pattern and the "
        "colours that make it this variety). Only change how ripe the fruit looks, the way this variety ripens: it is "
        f"now {stage}"
        + (f", as this ripening guide describes: {facts_text}" if facts_text else "") + ". One fruit. No text.",
        [to_data_uri(still)], "9:16", bucket="video")
    ref = storage().put_url("videos", f"still_v{video['id']}_stage{n}.png", url)
    costs.log_image(conn, model("image"), "video", video["id"], kind="video-still")
    if not animate._is_916(ref):
        ref = animate._pad_still(ref, Path(ref).with_name(Path(ref).stem + "_916.png"))
    return ref


STILL_TRIES = 3                     # a still that fails the check is made again, at most twice
STILL_CHECKS = {"fruit_accuracy": 4, "artefacts": 4, "never_include": 3}   # what a still already shows: a wrong fruit
# or a warped object only gets worse once it moves (2026-11-02: half of all shots failed QA after the clip was paid for)


def check_still(conn, video_id: int, still: str, refs: list[str], product: dict[str, Any],
                client: Any = None) -> tuple[bool, float]:
    """Score the start still on what it already shows, before a clip is paid for. (passed, total); an error passes."""
    import copy
    from creative_intelligence.adstudio import qa
    rubric = copy.deepcopy(qa.load_rubric())
    rubric["criteria"] = [c for c in rubric["criteria"] if c["key"] in STILL_CHECKS]
    try:
        reading, usage = qa.ask_claude(still, refs, product, rubric, "images", client)
        qa._log_cost(conn, usage, "video", video_id)
    except Exception:                       # the check must never block a shot: the clip's own QA still runs
        return True, 0.0
    got = reading.get("scores") or {}
    total = sum(got.get(k, 0) for k in STILL_CHECKS)
    return all(got.get(k, 0) >= m for k, m in STILL_CHECKS.items()), total


def timelapse_clip(stills: list[str], out: Path, hold: float = 0.7, fade: float = 0.6) -> Path:
    """The stills blended one into the next with a slow push-in, like a locked-off time-lapse."""
    from creative_intelligence.adstudio import videotools
    W, H = videotools.SIZES["9:16"]
    each = hold + fade
    args: list[str] = []
    for s in stills:
        args += ["-loop", "1", "-t", f"{each + fade:.2f}", "-i", str(s)]
    graph = [f"[{k}:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,fps=30[s{k}]"
             for k in range(len(stills))]
    last = "[s0]"
    for k in range(1, len(stills)):
        graph.append(f"{last}[s{k}]xfade=transition=fade:duration={fade}:offset={k * each:.2f}[x{k}]")
        last = f"[x{k}]"
    total = len(stills) * each + fade
    graph.append(f"{last}zoompan=z='min(1+0.0006*on,1.08)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={W}x{H}:fps=30,"
                 "format=yuv420p[v]")
    videotools.run([*args, "-filter_complex", ";".join(graph), "-map", "[v]", "-t", f"{total:.2f}", "-an",
                    *videotools._x264(), str(out)])
    return out


def _inputs(conn, video: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """A shot's card (with its look), its product and the reference pictures it is made and judged against."""
    from creative_intelligence.webapp.common import _primary_photo_uris, _product_dict
    c = card(video["shot_type"])
    if lk := look_of(video.get("prompt_version")):
        c = with_look(c, lk[1])
    product = _product_dict(conn, video["product_id"]) or {}
    refs = _primary_photo_uris(conn, video["product_id"], limit=3)
    if c.get("character"):                   # the approved look sheet leads: shots and QA stay on-model
        from creative_intelligence.adstudio import characters
        from creative_intelligence.adstudio.store import to_data_uri
        m = re.search(r"\+character:(\d+)\+", video.get("prompt_version") or "")
        ch = characters.approved(conn, int(m[1])) if m else characters.for_product(conn, video["product_id"])
        if ch is None:
            raise RuntimeError("The product's fruit character is no longer approved.")
        refs = [to_data_uri(ch["sheet_path"])] + refs[:2]
        product = {**product, "physical_desc": f"{product.get('physical_desc') or ''} The character: {ch['name']}, "
                                               f"{ch['personality']}.".strip()}
    return c, product, refs


def unscored(conn, product_id: int) -> list[int]:
    """Finished shots whose clip was made but never scored (2026-11-10: the day's text cap stopped the scoring)."""
    out = []
    for r in conn.execute("SELECT id FROM adstudio_videos WHERE product_id=? AND style='shot' AND status='done'"
                          " AND shot_type IS NOT NULL AND shot_type<>'' AND clip_path IS NOT NULL AND clip_path<>''"
                          " AND message LIKE 'not scored%'", (product_id,)):
        out.append(r[0])
    return out


def rescore(video_id: int, conn_factory: Callable[[], Any]) -> dict[str, Any]:
    """Score a finished shot's clip again, nothing made again."""
    from creative_intelligence.adstudio import video_qa
    conn = conn_factory()
    try:
        video = dict(conn.execute("SELECT * FROM adstudio_videos WHERE id=?", (video_id,)).fetchone())
        c, product, refs = _inputs(conn, video)
        q = video_qa.score(conn, video_id, video["clip_path"], refs, product, strict_qa=bool(c.get("strict_qa")),
                           count_check=c.get("count_check", True), skip=c.get("qa_skip"),
                           note=" ".join(x for x in (c.get("qa_note", ""), video_qa.STYLIZED_NOTE
                                                     if c.get("character") or c.get("cast") else "") if x))
        verdict = "passed QA" if q.get("passed") else "did not pass QA" if q.get("total") is not None else "not scored"
        animate._set(conn, video_id, message=f"{verdict} (scored again)")
        return {"video_id": video_id, "passed": q.get("passed")}
    finally:
        conn.close()


def process(video_id: int, conn_factory: Callable[[], Any]) -> dict[str, Any]:
    from creative_intelligence.adstudio import video_qa
    from creative_intelligence.webapp.common import _primary_photo_uris, _product_dict
    conn = conn_factory()
    try:
        video = dict(conn.execute("SELECT * FROM adstudio_videos WHERE id=?", (video_id,)).fetchone())
        if video["status"] == "stopping":
            animate._set(conn, video_id, status="failed", step="", message="Stopped before it started; nothing was spent.")
            return {"stopped": True}
        if video["status"] != "queued":
            return {"skipped": video["status"]}
        animate._set(conn, video_id, status="running", step="Preparing the still")
        c, product, refs = _inputs(conn, video)
        cmp = _CMP.search(video.get("prompt_version") or "")
        if eng := _ENGINE.search(video.get("prompt_version") or ""):
            c = {**c, "route": eng[1]}           # the model test: this model only, no fallback
        if cmp:                                  # model comparison: every model starts from the same still
            c = {**c, "route": cmp[1]}
            still = video["still_path"]
        elif eng and video.get("still_path"):    # the model test: the current model's take's still
            still = video["still_path"]
        elif video["source_kind"] == "image":
            still = animate.make_still(conn, video, animate.source_row(conn, "image", video["source_id"]), refs)
        else:
            still = _generated_still(conn, video, product, c, refs)
            best = (-1.0, still)
            for n in range(1, STILL_TRIES + 1):
                animate._set(conn, video_id, step="Checking the still" if n == 1 else "Making a better still")
                ok, total = check_still(conn, video_id, still, refs, product)
                if ok:
                    best = (total, still)
                    break
                best = max(best, (total, still))
                if n < STILL_TRIES:
                    still = _generated_still(conn, video, product, c, refs, f"_{n + 1}")
            still = best[1]
            animate._set(conn, video_id, still_path=still)
        end_still = None
        end_src = approved_still(conn, video["product_id"], {"from_images": c.get("end_from_images") or []})
        if end_src and not (video["source_kind"] == "image" and end_src["id"] == video["source_id"]):
            # e.g. a real cut photo, so the model can't invent the flesh; extended to 9:16 like the start
            # still (padding it made the clip morph into blurred bars)
            end_still = (_edited_end(conn, {**video, "_end_edit_prompt": c.get("end_edit_prompt", "")}, still, end_src)
                         if c.get("end_edit") else
                         animate.make_still(conn, dict(video, still_strategy="outpaint"), end_src, refs, role="end"))
        corrections, q, attempt = "", {}, 0
        stages = c.get("timelapse_stages") or []
        for attempt in range(1, (1 if cmp else animate.MAX_ATTEMPTS) + 1):     # a comparison: first take only
            animate._set(conn, video_id, step="Making the shot" if attempt == 1 else "Making a better shot", attempts=attempt)
            if stages:                           # a real time-lapse from stills (ripening, feedback 2026-10-26)
                from creative_intelligence.adstudio import facts
                from creative_intelligence.storage import storage
                facts_text = " ".join(f["text"] for f in facts.list_facts(conn, product.get("id"), facts._family(product))
                                      if f["category"] == c.get("facts_category"))
                frames = [still] + [_stage_still(conn, video, still, st, facts_text, n)
                                    for n, st in enumerate(stages, 1)]
                clip = str(timelapse_clip(frames, storage().area_dir("videos") / f"shot_v{video_id}_a{attempt}.mp4"))
            else:
                clip = animate.make_clip(conn, video, still, attempt, corrections, route=c["route"],
                                         audio=c.get("audio") == "ambient", negative=no_text(c.get("negative", "")),
                                         end_still=end_still)
            animate._set(conn, video_id, clip_path=clip, step="Checking the shot")
            q = video_qa.score(conn, video_id, clip, refs, product, strict_qa=bool(c.get("strict_qa")),
                               count_check=c.get("count_check", True), skip=c.get("qa_skip"),
                               note=" ".join(x for x in (c.get("qa_note", ""), video_qa.STYLIZED_NOTE
                                                         if c.get("character") or c.get("cast") else "") if x))
            if q.get("total") is None or q.get("passed"):
                break
            corrections = retake_note(q.get("failures") or [])     # the fix only: naming the defect invites it
        spent = budget.spent_on_video(conn, video_id)
        verdict = "passed QA" if q.get("passed") else "did not pass QA" if q.get("total") is not None else "not scored"
        animate._set(conn, video_id, status="done", step="", finished_at=datetime.utcnow().isoformat(timespec="seconds"),
                     message=f"{verdict} after {attempt} clip(s); ${spent:.2f}")
        return {"video_id": video_id, "passed": q.get("passed"), "spent": spent}
    except animate.Stopped:
        animate._set(conn, video_id, status="failed", step="", message="Stopped; nothing more was spent.")
        return {"video_id": video_id, "stopped": True}
    except Exception as exc:
        animate._set(conn, video_id, status="failed", step="", message=f"{type(exc).__name__}: {exc}"[:400])
        raise
    finally:
        conn.close()


RECOVERY_NAMES = {"consistency": "Same fruit first to last", "motion_artefacts": "Warping or morphing",
                  "fruit_accuracy": "Fruit accuracy", "hands": "Hands"}


def retake_note(failures: list[str]) -> str:
    """QA failure lines ('<criterion name> 2 is below the minimum 3') to the fix the card's retake asks for."""
    fixes = [RECOVERY[k] for k, name in RECOVERY_NAMES.items() if any(f.startswith(name) for f in failures)]
    return " ".join(fixes) or "; ".join(failures)


REAL_PREFIX = "real:"
REAL_MAX_BYTES = 500 * 1024 * 1024


def real_kinds() -> dict[str, str]:
    from creative_intelligence import knowledge
    return {i["key"]: i["name"] for i in knowledge.load("styles")["real_footage_only"]["items"]}


def upload_real(conn, product_id: int, filename: str, data: bytes, kind: str, note: str = "", filmed_by: str = "",
                filmed_on: str = "", consent: bool = False) -> tuple[int | None, str]:
    """Store real footage in the shot library. Everyone on camera must have agreed to appear in ads."""
    from creative_intelligence.adstudio import videotools
    from creative_intelligence.storage import storage
    if kind not in real_kinds():
        return None, "Pick what kind of footage this is."
    if not consent:
        return None, f"Confirm that everyone on camera agreed to appear in {_brand_name()} ads."
    if not filmed_by.strip():
        return None, "Say who filmed it (it's kept as the footage's source)."
    if not data or len(data) > REAL_MAX_BYTES:
        return None, "Upload a video file up to 500 MB."
    ext = Path(filename or "").suffix.lower()
    if ext not in (".mp4", ".mov", ".m4v"):
        return None, "Upload an MP4 or MOV file."
    now = datetime.utcnow()
    path = storage().put_bytes("videos", f"real_p{product_id}_{now:%Y%m%d%H%M%S}_{kind}{ext}", data)
    try:
        info = videotools.probe(path)
    except videotools.VideoToolError as exc:
        Path(path).unlink(missing_ok=True)
        return None, f"That file isn't a video ffmpeg can read: {exc}"
    meta = {"kind": kind, "note": note.strip(), "filmed_by": filmed_by.strip(), "filmed_on": filmed_on.strip(),
            "consent": True, "file": filename, "width": info["width"], "height": info["height"]}
    with conn:
        vid = conn.execute(
            "INSERT INTO adstudio_videos (product_id, source_kind, source_id, style, duration_s, clip_path, model,"
            " status, step, ai_person, shot_type, real, real_json, created_at, finished_at)"
            " VALUES (?, 'real', 0, 'shot', ?, ?, 'real footage', 'done', 'Uploaded', 0, ?, 1, ?, ?, ?)",
            (product_id, round(info["duration"]), path, REAL_PREFIX + kind, json.dumps(meta),
             now.isoformat(timespec="seconds"), now.isoformat(timespec="seconds"))).lastrowid
    return vid, ""


def real_footage(conn, product_id: int) -> list[dict[str, Any]]:
    kinds = real_kinds()
    out = []
    for r in conn.execute("SELECT * FROM adstudio_videos WHERE product_id=? AND real=1 AND retired=0 ORDER BY id DESC",
                          (product_id,)):
        meta = json.loads(r["real_json"] or "{}")
        out.append({"id": r["id"], "kind": meta.get("kind"), "kind_name": kinds.get(meta.get("kind"), "Real footage"),
                    "duration_s": r["duration_s"], "note": meta.get("note", ""), "filmed_by": meta.get("filmed_by", ""),
                    "filmed_on": meta.get("filmed_on", ""), "created_at": r["created_at"]})
    return out


# ── Which model makes which shot best (feedback 2026-10-26: "confirm what model is better for each variant") ──
COMPARE_ROUTES = ("kling", "veo", "seedance")
_CMP = re.compile(r"\+cmp:([a-z_]+)\+")
_ENGINE = re.compile(r"\+engine:([a-z0-9_]+)\+")


def compare_estimate(conn, shot_types: list[str], routes: tuple[str, ...] = COMPARE_ROUTES) -> float:
    from creative_intelligence.providers import model
    total = 0.0
    for t in shot_types:
        c = card(t)
        total += costs.price(conn, model("image")) + sum(estimate(conn, {**c, "route": r}, False) for r in routes)
    return round(total, 2)


def compare(db: Callable[[], Any], product_id: int, shot_type: str, routes: tuple[str, ...] = COMPARE_ROUTES) -> dict[str, Any]:
    """The shot_compare job: one still from the card, then one take per model from that same still, QA-scored the
    same way. The takes stay out of the library until one is promoted."""
    from creative_intelligence import jobs
    from creative_intelligence.adstudio.profiles import prompt_stamp
    from creative_intelligence.providers import model, replicate
    from creative_intelligence.storage import storage
    from creative_intelligence.webapp.common import _primary_photo_uris, _product_dict
    conn = db()
    try:
        c, product = card(shot_type), _product_dict(conn, product_id) or {}
        budget.check(conn, "video", compare_estimate(conn, [shot_type], routes))
        url = replicate.generate_image(still_prompt(product, c), _primary_photo_uris(conn, product_id, limit=3), "9:16",
                                       bucket="video")
        still = storage().put_url("videos", f"cmp_p{product_id}_{shot_type}_{datetime.utcnow():%Y%m%d%H%M%S}.png", url)
        costs.log_image(conn, model("image"), "video", 0, kind="video-still")
        if not animate._is_916(still):
            still = animate._pad_still(still, Path(still).with_name(Path(still).stem + "_916.png"))
        made = []
        for r in routes:
            rc = {**c, "route": r}
            motion = prompts.text("animate-motion-rules", motion=motion_text(rc), product=animate.product_line(product))
            stamp = (f"shot:{shot_type}@v{c['version']}+cmp:{r}+" + prompts.stamp("animate-motion-rules", "video-qa-system")
                     + "+" + prompt_stamp(product, "image"))
            with conn:
                vid = conn.execute(
                    "INSERT INTO adstudio_videos (product_id, source_kind, source_id, style, motion_preset, motion_prompt,"
                    " duration_s, still_strategy, still_path, model, prompt_version, shot_type, card_version, route,"
                    " has_audio, created_at) VALUES (?, 'card', 0, 'shot', ?,?,?, 'generated', ?,?,?,?,?,?,0,?)",
                    (product_id, shot_type, motion, c["duration_s"], still, replicate.route_model(r), stamp, shot_type,
                     c["version"], r, datetime.utcnow().isoformat(timespec="seconds"))).lastrowid
            jobs.enqueue(conn, "shot", {"video_id": vid}, dedupe_key=f"video:{vid}")
            made.append(vid)
        return {"shot_type": shot_type, "videos": made}
    finally:
        conn.close()


def compare_results(conn, product_id: int) -> list[dict[str, Any]]:
    """Every comparison take of the product, newest round first: per shot type, each model's result."""
    from creative_intelligence.adstudio import qa
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM adstudio_videos WHERE product_id=? AND style='shot' AND instr(COALESCE(prompt_version, ''),"
        " '+cmp:') > 0 ORDER BY id DESC", (product_id,))]
    scores = qa.scores_for(conn, "video", [r["id"] for r in rows])
    out = []
    for r in rows:
        q = scores.get(r["id"]) or {}
        took = None
        if r.get("finished_at") and r.get("created_at"):
            took = round((datetime.fromisoformat(r["finished_at"]) - datetime.fromisoformat(r["created_at"])).total_seconds())
        out.append({"id": r["id"], "shot_type": r["shot_type"], "route": _CMP.search(r["prompt_version"])[1],
                    "model": r["model"], "status": r["status"], "message": r["message"], "qa_total": q.get("total"),
                    "passed": q.get("passed"), "failures": q.get("failures") or [], "spent_usd": budget.spent_on_video(conn, r["id"]),
                    "seconds_taken": took, "promoted": False})
    return out


def promote(conn, video_id: int) -> str:
    """Make a comparison take the product's library shot of its type (it must have passed QA)."""
    r = conn.execute("SELECT prompt_version FROM adstudio_videos WHERE id=?", (video_id,)).fetchone()
    if r is None or "+cmp:" not in (r[0] or ""):
        return "That isn't a comparison take."
    from creative_intelligence.adstudio import qa
    if not (qa.scores_for(conn, "video", [video_id]).get(video_id) or {}).get("passed"):
        return "Only a take that passed QA can go in the library."
    with conn:
        conn.execute("UPDATE adstudio_videos SET prompt_version=? WHERE id=?", (_CMP.sub("+", r[0]), video_id))
    return ""


def retire(conn, video_id: int) -> bool:
    with conn:
        return conn.execute("UPDATE adstudio_videos SET retired=1 WHERE id=? AND style='shot'", (video_id,)).rowcount == 1


def use(conn, video_id: int) -> None:
    with conn:
        conn.execute("UPDATE adstudio_videos SET reuse_count=reuse_count+1 WHERE id=?", (video_id,))


def _brand_name() -> str:
    from creative_intelligence import brand
    return brand.name()
