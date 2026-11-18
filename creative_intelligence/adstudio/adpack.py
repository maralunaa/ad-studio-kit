"""
Ad Pack (Part C): say what creatives you need, approve one plan with its cost, download one pack ready for Meta.

  request   the form (products, statics per product, videos per product by style, sizes, angles, mix, copy, notes),
            pre-filled with defaults; a sentence ("15 statics and 5 videos for each of these 3, 2 talking heads and 3
            that worked before") fills it, and the form is always shown before anything runs. Saved templates.
  plan      (next step) per-product table, diversity check, cost and readiness against the caps.
  produce   only the existing jobs (Bulk, video batches, QA, claims, caps); no new generation path.
  pack      review grid, quality gate, one zip with the launch sheet.

"proven" videos resolve to our decoded winners (and, once Part B exists, graduated concepts); when there aren't
enough, the plan says so and fills with the best-evidence ideas, labelled as such.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

log = logging.getLogger("adstudio")

SIZES = ("9:16", "4:5", "1:1")
PROVEN = "proven"
DEFAULT = {"product_ids": [], "statics": 10, "videos": {PROVEN: 3}, "sizes": ["9:16", "4:5"], "angles": "auto",
           "idea_ids": [], "mix_new": 70, "primary_texts": 3, "headlines": 3, "name": "", "notes": "", "direction": "",
           "voice": "rotate", "presenter_id": "rotate"}
ROTATE = "rotate"     # every voice and presenter in turn, the least used first (feedback 2026-11-09: "equal usage of all")
BUILT_IN = {
    "Weekly pack": {"statics": 10, "videos": {PROVEN: 3}},
    "Gifting push": {"statics": 12, "videos": {"pov": 2, PROVEN: 2},
                     "direction": "A gift: who it's for and the occasion, the unboxing moment, the reaction when it's "
                                  "opened. Warm, generous, special."},
    "New product launch": {"statics": 15, "videos": {"fruit_story": 2, "sensory": 1, "talking_head": 1},
                           "direction": "Introduce it: what it is, what it looks like inside, how it tastes. "
                                        "Curious and new, as if seeing it for the first time."},
}
MAX_STATICS, MAX_VIDEOS = 40, 12
# Claude for the words, rough (2026-11-10: three packs ran out the daily text cap and the videos never got scored):
# a video's script with its read-aloud pass, claims check and repairs; a static's brief share and claims check;
# writing hooks when there are too few.
TEXT_USD = {"video": 0.35, "static": 0.03, "hooks": 0.3}


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def video_styles() -> dict[str, str]:
    """What a request can ask for: 'proven' plus every video format a batch can make."""
    from creative_intelligence.adstudio import video_batch
    return {PROVEN: "Proven (our winners)", **video_batch.batch_styles()}


def normalize(req: dict[str, Any]) -> dict[str, Any]:
    """Coerce a request (the form, a template or Claude's reading of a sentence) to its stored shape, in limits."""
    styles = video_styles()
    r = {**DEFAULT, **{k: v for k, v in (req or {}).items() if k in DEFAULT}}
    r["product_ids"] = list(dict.fromkeys(int(p) for p in r["product_ids"] if str(p).isdigit()))[:10]
    r["statics"] = max(0, min(MAX_STATICS, int(r["statics"] or 0)))
    r["videos"] = {k: max(0, min(MAX_VIDEOS, int(v or 0))) for k, v in (r["videos"] or {}).items() if k in styles}
    r["videos"] = {k: v for k, v in r["videos"].items() if v}
    r["sizes"] = [s for s in SIZES if s in (r["sizes"] or [])] or list(DEFAULT["sizes"])
    r["angles"] = "ideas" if r["angles"] == "ideas" and r["idea_ids"] else "auto"
    r["idea_ids"] = [int(i) for i in r["idea_ids"] or [] if str(i).isdigit()][:20]
    r["mix_new"] = max(0, min(100, int(r["mix_new"] or 0)))
    r["primary_texts"] = max(1, min(5, int(r["primary_texts"] or 3)))
    r["headlines"] = max(1, min(5, int(r["headlines"] or 3)))
    r["name"], r["notes"] = str(r["name"] or "").strip()[:80], str(r["notes"] or "").strip()[:500]
    r["direction"] = str(r["direction"] or "").strip()[:600]
    from creative_intelligence import config
    r["voice"] = r["voice"] if r["voice"] in {v["name"] for v in config.HOUSE_VOICES} else ROTATE
    r["presenter_id"] = int(r["presenter_id"]) if str(r["presenter_id"]).isdigit() else ROTATE
    return r


def cast_options(conn) -> dict[str, list]:
    """What the form offers for who speaks: the narrator voices and the approved presenters."""
    from creative_intelligence.adstudio import video_batch
    return {"voices": video_batch.narrator_voices(),
            "presenters": [dict(r) for r in conn.execute(
                "SELECT id, name FROM presenters WHERE status='approved' ORDER BY id")]}


PARSE_SCHEMA = {"type": "object", "additionalProperties": False,
                "required": ["product_names", "statics", "videos", "sizes", "notes", "name", "direction"],
                "properties": {"product_names": {"type": "array", "items": {"type": "string"}},
                               "statics": {"type": "integer"},
                               "videos": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                                                       "required": ["style", "count"],
                                                                       "properties": {"style": {"type": "string"},
                                                                                      "count": {"type": "integer"}}}},
                               "sizes": {"type": "array", "items": {"type": "string"}},
                               "notes": {"type": "string"}, "name": {"type": "string"},
                               "direction": {"type": "string"}}}


def parse(conn, text: str, client: Any = None) -> dict[str, Any]:
    """Claude reads a sentence into the form. Products are matched to the catalogue by name; anything it can't
    match is listed so the operator fixes it in the form before anything runs."""
    from creative_intelligence import config
    from creative_intelligence.adstudio import costs
    from creative_intelligence.adstudio.chat import _client
    text = (text or "").strip()
    if not text:
        return {"error": "Say what you need, e.g. '10 statics and 3 videos for Sunrise Mango'."}
    products = {r[0]: r[1] for r in conn.execute("SELECT id, name FROM adstudio_products WHERE COALESCE(archived,0)=0")}
    styles = video_styles()
    ask = (f"REQUEST: {text}\n\nPRODUCTS (use these exact names): {'; '.join(products.values())}\n\n"
           f"VIDEO STYLES (use these keys): {json.dumps(styles)}\n'proven' means ads that worked before.\n"
           f"SIZES: {', '.join(SIZES)}. Fill only what the request says; 0 or [] otherwise. direction: how the ads should "
           "feel or what they're about, in a sentence the image and script writers can follow (season, occasion, mood, "
           "audience, e.g. 'Christmas: warm, festive table, gift-giving'); '' if none. notes: deadlines or anything for "
           "the team. name: a short pack name if one is implied (e.g. 'Mother's Day').")
    client = client or _client()
    resp = client.beta.messages.create(model=config.CI_CLAUDE_CHECK_MODEL, max_tokens=1200,
                                       system="You turn a request for ad creatives into a form. Never invent products.",
                                       output_config={"format": {"type": "json_schema", "schema": PARSE_SCHEMA}},
                                       messages=[{"role": "user", "content": ask}])
    costs.log_tokens(conn, config.CI_CLAUDE_CHECK_MODEL, "copy", resp.usage, "adpack_parse")
    if resp.stop_reason == "refusal":
        return {"error": "Claude couldn't read that: fill the form instead."}
    got = json.loads(next(b.text for b in resp.content if b.type == "text"))
    by_name = {v.lower(): k for k, v in products.items()}
    ids, unknown = [], []
    for n in got["product_names"]:
        pid = by_name.get(n.strip().lower()) or next((k for k, v in products.items() if n.strip().lower() in v.lower()), None)
        (ids.append(pid) if pid else unknown.append(n))
    req = normalize({"product_ids": ids, "statics": got["statics"] or DEFAULT["statics"],
                     "videos": {v["style"]: v["count"] for v in got["videos"]} or DEFAULT["videos"],
                     "sizes": got["sizes"] or DEFAULT["sizes"], "notes": got["notes"], "name": got["name"],
                     "direction": got.get("direction", "")})
    return {"request": req, "unknown_products": unknown}


def templates(conn) -> list[dict[str, Any]]:
    saved = [{"name": r[0], "request": normalize(json.loads(r[1])), "saved": True}
             for r in conn.execute("SELECT name, request_json FROM ad_pack_templates ORDER BY name")]
    return [{"name": k, "request": normalize(v), "saved": False} for k, v in BUILT_IN.items()] + saved


def save_template(conn, name: str, req: dict[str, Any]) -> str:
    name = (name or "").strip()[:60]
    if not name:
        return "Name the template."
    if name in BUILT_IN:
        return "That name is a built-in template."
    r = normalize(req)
    r["product_ids"] = []                               # a template is the shape of a pack, not its products
    with conn:
        conn.execute("INSERT INTO ad_pack_templates (name, request_json, created_at) VALUES (?,?,?)"
                     " ON CONFLICT(name) DO UPDATE SET request_json=excluded.request_json", (name, json.dumps(r), _now()))
    return ""


def create(conn, req: dict[str, Any]) -> tuple[int | None, str]:
    """Save a request as a new pack (status 'request'); the plan is made from it next."""
    r = normalize(req)
    if not r["product_ids"]:
        return None, "Pick at least one product."
    if not r["statics"] and not r["videos"]:
        return None, "Ask for some statics or videos."
    name = r["name"] or f"{datetime.utcnow():%Y-%m-%d} pack"
    with conn:
        pid = conn.execute("INSERT INTO ad_packs (name, request_json, created_at) VALUES (?,?,?)",
                           (name, json.dumps(r), _now())).lastrowid
    return pid, ""


def listing(conn) -> list[dict[str, Any]]:
    return [{**dict(r), "request": json.loads(r["request_json"])}
            for r in conn.execute("SELECT id, name, status, total_usd, created_at, finished_at, request_json"
                                  " FROM ad_packs ORDER BY id DESC LIMIT 50")]


# ── C3 · the plan: what gets made per product, what it costs, what's missing (nothing is spent here) ──
MIN_HOOKS = 3                  # fewer usable hooks than this and the statics repeat their words
PER_FORMAT = 3                 # a video batch makes at most 3 of a format per product
DRAFT_USD = {"presenter": 2.5, "voice": 0.3}      # one draft before the shots, rough: presenter clips cost the most


def _video_estimate(conn, pid: int, style: str) -> float:
    """About what one video of this format costs to its approved draft: its own shots (opening + 2 beats), the
    format's own cards not in the library yet, and the draft. Finals come later and are priced then."""
    from creative_intelligence.adstudio import shots, styles, video_batch
    if style == video_batch.ANIMATE:
        from creative_intelligence.adstudio import animate
        return animate.estimate(conn, 5, True)
    card = shots.estimate(conn, shots.card("whole_fruit_hero"), True)
    own = video_batch.OWN_SHOTS * card
    cat = styles.catalog().get(style) or {}
    missing = sum(shots.estimate(conn, shots.card(t), True) for t in cat.get("shot_types") or []
                  if not shots.passing(conn, pid, t))
    return round(own + missing + DRAFT_USD["presenter" if cat.get("presenter") else "voice"], 2)


# Feedback 2026-11-09 ("the ad pack should be quicker ... it should fill the product in the same run ... now it's
# saying to approve images, should auto do it"): the pack fills an empty product profile itself, and the animated
# still uses the best images we have (approved for it) or makes some first; a format that still can't be made is
# skipped and named, never a stop for the whole pack.
PACK_IMAGES = 8                # a product with no images to animate gets one run of this many first
PACK_IMAGE_ASPECT = "9:16"


def fill_needed(conn, product: dict[str, Any]) -> list[str]:
    """The parts of the product profile the auto-fill would make (the same checks as hub.fill)."""
    from creative_intelligence.adstudio import hub
    pid = product["id"]
    photos = conn.execute("SELECT 1 FROM adstudio_photos WHERE product_id=? LIMIT 1", (pid,)).fetchone()
    out = []
    if not photos and product.get("origin") == "shopify":
        out.append("photos from the shop")
    if not (product.get("physical_desc") or "").strip() and (photos or out):
        out.append("what it looks like")
    if not conn.execute("SELECT 1 FROM product_research WHERE product_id=? AND status='done'", (pid,)).fetchone():
        out.append("research")
    if not hub._usable_hooks(conn, pid):
        out.append("hooks")
    return out


def unreviewed_images(conn, pid: int) -> list[int]:
    """Finished product images nobody reviewed yet that passed QA, best first: the pack approves these to animate."""
    return [r[0] for r in conn.execute(
        "SELECT i.id FROM adstudio_images i JOIN qa_scores q ON q.source='image' AND q.source_id=i.id AND q.passed=1"
        " WHERE i.product_id=? AND i.status='done' AND i.image_path IS NOT NULL AND i.archived=0"
        " AND COALESCE(i.review_status, '') NOT IN ('approved', 'rejected')"
        " GROUP BY i.id ORDER BY MAX(q.total) DESC, i.id DESC", (pid,))]


def _images_usd(conn) -> float:
    from creative_intelligence.adstudio import costs, runs
    from creative_intelligence.adstudio.generate import MODEL
    return round(PACK_IMAGES * (costs.price(conn, MODEL) + runs.QA_ESTIMATE_USD), 2)


def _video_row(conn, product: dict[str, Any], style: str, n: int) -> dict[str, Any]:
    from creative_intelligence.adstudio import ideas, styles, video_batch
    pid, why, note = product["id"], "", ""
    asked = style
    extra = 0.0
    if style == video_batch.ANIMATE and not video_batch._picks(conn, pid, style):
        have = len(unreviewed_images(conn, pid))
        if have:
            note = f"Uses the {min(have, max(n, 3))} best images we have (QA passed), approved for you."
        else:
            note = f"No images to animate yet: makes {PACK_IMAGES} first and approves the ones that pass QA."
            extra = _images_usd(conn)
    if style == PROVEN:                                       # our decoded winners; best-evidence ideas fill the rest
        have = len(video_batch._picks(conn, pid, "template"))
        style = "template"
        if have < n:
            note = (f"{have} decoded winner{'s' if have != 1 else ''}: the other {n - have} come from the best-evidence "
                    "approved ideas, labelled as such." if have else
                    "No decoded winners yet: these come from the best-evidence approved ideas, labelled as such.")
    elif style in video_batch.NO_PICK and style != video_batch.ANIMATE and not video_batch._picks(conn, pid, style):
        why = video_batch.NO_PICK[style]
    elif style in styles.catalog() and (gaps := styles.missing_facts(conn, product, style)):
        from creative_intelligence.adstudio import facts
        why = f"Add the approved {', '.join(facts.CATEGORIES.get(g, g) for g in gaps)} facts first."
    usd = _video_estimate(conn, pid, style if style in styles.catalog() or style == video_batch.ANIMATE else "fruit_story")
    label = {PROVEN: "Proven (our winners)"}.get(asked) or video_styles().get(asked, asked)
    return {"style": asked, "label": label, "count": n, "usd": round(usd * n + extra, 2), "ready": not why,
            "why": f"Skipped: {why}" if why else "", "note": note}


def plan(conn, pack_id: int) -> dict[str, Any]:
    """The plan for a saved request: per product, the statics and videos, their cost, and what isn't ready; the
    totals against today's caps. Saved on the pack (status 'planned'); production is the next step."""
    from creative_intelligence import budget
    from creative_intelligence.adstudio import batch, hub
    from creative_intelligence.webapp.common import _product_dict
    row = conn.execute("SELECT * FROM ad_packs WHERE id=?", (pack_id,)).fetchone()
    if row is None:
        return {"error": "pack not found"}
    req = normalize(json.loads(row["request_json"]))
    ai_usd = batch.ai_price(conn)
    products, total_img, total_vid, total_txt = [], 0.0, 0.0, 0.0
    for pid in req["product_ids"]:
        prod = _product_dict(conn, pid)
        if prod is None:
            products.append({"id": pid, "name": f"#{pid}", "error": "Product not found."})
            continue
        hooks = batch._hooks(conn, pid)
        photos = conn.execute("SELECT COUNT(*) FROM adstudio_photos WHERE product_id=?", (pid,)).fetchone()[0]
        n_static = req["statics"] * len(req["sizes"])
        warn, notes = [], []
        if req["statics"] and len(hooks) < MIN_HOOKS:          # feedback 2026-10-26: no hooks stopped the whole pack
            notes.append(f"{len(hooks) or 'No'} usable hook{'s' if len(hooks) != 1 else ''}: Make writes "
                         f"{hub.AUTO_HOOKS} more first (Hook University, claims-checked like hooks saved by hand).")
        fill = fill_needed(conn, prod)
        fill_usd = 0.0
        if fill:
            from creative_intelligence.adstudio import hub
            fill_usd = hub.fill_estimate(conn)
            notes.append(f"Fills the product first, in the same run: {', '.join(fill)} (about ${fill_usd:.2f}).")
        if not photos and "photos from the shop" not in fill:
            warn.append("No product photos: add them in the Product Hub.")
        videos = [{**_video_row(conn, prod, s, min(n, PER_FORMAT)),
                   **({"note": f"At most {PER_FORMAT} of a format per product in one pack."} if n > PER_FORMAT else {})}
                  for s, n in req["videos"].items()]
        img_usd = round(n_static * ai_usd + fill_usd, 2)
        vid_usd = round(sum(v["usd"] for v in videos if v["ready"]), 2)
        total_img += img_usd
        total_vid += vid_usd
        total_txt += (sum(v["count"] for v in videos if v["ready"]) * TEXT_USD["video"] + n_static * TEXT_USD["static"]
                      + (TEXT_USD["hooks"] if req["statics"] and len(hooks) < MIN_HOOKS else 0))
        products.append({"id": pid, "name": prod["name"], "statics": req["statics"], "static_files": n_static,
                         "static_usd": img_usd, "hooks": len(hooks), "photos": photos, "videos": videos,
                         "video_usd": vid_usd, "warnings": warn, "notes": notes,
                         "ready": not warn and (bool(req["statics"]) or any(v["ready"] for v in videos))})
    cap = budget.caps(conn)
    left = {"images": round(cap["generation_day"] - budget.spent_today(conn, "generation"), 2),
            "video": round(cap["video_day"] - budget.spent_today(conn, "video"), 2),
            "text": round(cap["text_day"] - budget.spent_today(conn, "text"), 2)}
    out = {"pack_id": pack_id, "name": row["name"], "request": req, "products": products,
           "total_usd": round(total_img + total_vid, 2), "images_usd": round(total_img, 2),
           "video_usd": round(total_vid, 2), "left_today": left,
           "text_usd": round(total_txt, 2),
           "over_cap": [k for k, usd in (("images", total_img), ("video", total_vid), ("text", total_txt)) if usd > left[k]],
           "ready": all(p.get("ready") for p in products)}
    with conn:
        conn.execute("UPDATE ad_packs SET plan_json=?, total_usd=?, status=CASE WHEN status IN ('request','planned')"
                     " THEN 'planned' ELSE status END WHERE id=?", (json.dumps(out), out["total_usd"], pack_id))
    return out


def get(conn, pack_id: int) -> dict[str, Any] | None:
    r = conn.execute("SELECT * FROM ad_packs WHERE id=?", (pack_id,)).fetchone()
    if r is None:
        return None
    return {**{k: r[k] for k in ("id", "name", "status", "total_usd", "created_at", "message")},
            "request": json.loads(r["request_json"]), "plan": json.loads(r["plan_json"] or "{}") or None}


# ── C4 · produce: only the existing jobs (Bulk and the video batches), one plan approval for everything ──
CHECK_S = 30


def _formats(conn, pid: int, videos: dict[str, int]) -> dict[str, int]:
    """The request's video formats as batch styles: Proven → our decoded winners, the rest from the best-evidence
    approved video idea's style (Fruit Story when none has one)."""
    from creative_intelligence.adstudio import ideas, video_batch
    out: dict[str, int] = {}
    for style, n in videos.items():
        if style == PROVEN:
            have = min(n, len(video_batch._picks(conn, pid, "template")))
            if have:
                out["template"] = out.get("template", 0) + have
            if n > have:
                pick = next((i["style"] for i in ideas.eligible(conn, pid, "video")
                             if i["style"] in video_batch.batch_styles() and i["style"] != "template"), "fruit_story")
                out[pick] = out.get(pick, 0) + n - have
        else:
            out[style] = out.get(style, 0) + n
    return {k: min(PER_FORMAT, v) for k, v in out.items()}


def produce(conn, pack_id: int) -> dict[str, Any]:
    """Approve the plan: per product one all-AI Bulk batch and one video batch; a pack job approves the storyboards
    as soon as they're written (the plan's cost was approved here) and marks the pack for review when all is made.
    A product whose profile is empty is filled first (its batches start when the fill is done); an animated still
    with nothing approved to animate approves the best images we have, or waits for a run of new ones."""
    from creative_intelligence import jobs
    from creative_intelligence.adstudio import hub
    from creative_intelligence.webapp.common import _product_dict
    p = get(conn, pack_id)
    if p is None:
        return {"error": "pack not found"}
    if p["status"] not in ("planned",):
        return {"error": "Make the plan first." if p["status"] == "request" else "This pack is already being made."}
    plan_ = p["plan"] or {}
    if not plan_.get("ready"):
        return {"error": "Some products aren't ready: fix what the plan lists, then make the plan again."}
    req = p["request"]
    made: dict[str, Any] = {"bulk": {}, "video": {}, "errors": [], "filling": [], "pending": [], "animate": {}}
    for pid in req["product_ids"]:                  # the click only queues; the pack job starts everything (quicker)
        prod = _product_dict(conn, pid)
        if prod is not None and fill_needed(conn, prod) and (
                hub.queue_fill(conn, pid) is not None or prod.get("fill_status") in ("queued", "running")):
            made["filling"].append(pid)
        else:
            made["pending"].append(pid)
    with conn:
        conn.execute("UPDATE ad_packs SET status='producing', approved_at=?, plan_json=?, message='' WHERE id=?",
                     (_now(), json.dumps({**plan_, "made": made}), pack_id))
    jobs.enqueue(conn, "adpack", {"pack_id": pack_id}, dedupe_key=f"adpack:{pack_id}")
    return {"started": len(req["product_ids"]), "errors": [], "filling": made["filling"]}


def _approve_images(conn, ids: list[int]) -> None:
    with conn:
        conn.executemany("UPDATE adstudio_images SET review_status='approved' WHERE id=?", [(i,) for i in ids])


def _video_request(conn, pack_id: int, pid: int, req: dict[str, Any], styles_: dict[str, int]) -> dict[str, Any]:
    from creative_intelligence.adstudio import styles
    return {"product_ids": [pid], "styles": styles_, "voice": req.get("voice") or ROTATE,
            "presenter_id": req.get("presenter_id") or ROTATE,      # the batch gives each video its own in turn
            "method": styles.PICKED_TALKING,             # the presenters the owner picked (Seedance), not the old default
            "aspects": [s for s in req["sizes"] if s in ("9:16", "4:5")] or ["9:16"],
            "direction": req["direction"], "pack_id": pack_id}


def _start_product(conn, pack_id: int, pid: int, req: dict[str, Any], made: dict[str, Any]) -> int:
    """Start one product's statics and videos; returns how many batches started. An animated still with nothing to
    animate waits for its image run (made["animate"]) and goes into a batch of its own when that run is done."""
    from creative_intelligence import jobs
    from creative_intelligence.adstudio import batch, hub, runs, styles as styles_mod, video_batch
    from creative_intelligence.webapp.common import _product_dict
    n = 0
    if req["statics"] and len(batch._hooks(conn, pid)) < MIN_HOOKS:    # too few hooks: write some first
        try:
            hub.auto_hooks(conn, _product_dict(conn, pid))
        except Exception as exc:
            log.exception("pack %s: hooks for product %s", pack_id, pid)
            made["errors"].append(f"Hooks #{pid}: {str(exc)[:120]}")
    if req["statics"]:
        opts = batch.options(conn, pid)
        body = {"images": [], "types": [t["key"] for t in opts["types"] if not t["why"]],
                "hook_ids": [h["id"] for h in opts["hooks"] if not h["flagged"]], "sizes": req["sizes"],
                "ai": min(batch.MAX_AI, req["statics"] * len(req["sizes"])), "direction": req["direction"]}
        bid, why = batch.create(conn, pid, body)
        if bid:
            made["bulk"][str(pid)] = bid
            n += 1
        else:
            made["errors"].append(f"Statics #{pid}: {why}")
    styles_ = _formats(conn, pid, req["videos"])
    prod = _product_dict(conn, pid) or {}
    for st in list(styles_):                         # what still can't be made is skipped, never a stop
        if st in video_batch.NO_PICK and st != video_batch.ANIMATE and not video_batch._picks(conn, pid, st):
            styles_.pop(st)
        elif st in styles_mod.catalog() and styles_mod.missing_facts(conn, prod, st):
            styles_.pop(st)
    want = styles_.get(video_batch.ANIMATE, 0)
    if want and not video_batch._picks(conn, pid, video_batch.ANIMATE):
        have = unreviewed_images(conn, pid)
        if have:
            _approve_images(conn, have[:max(want, 3)])
        else:
            styles_.pop(video_batch.ANIMATE)
            rid, why = runs.create_run(conn, pid, PACK_IMAGES, PACK_IMAGE_ASPECT)
            if rid:
                jobs.enqueue(conn, "image_run", {"run_id": rid}, dedupe_key=f"run:{rid}")
                made["animate"][str(pid)] = {"count": want, "run_id": rid, "batch": None}
                n += 1
            else:
                made["errors"].append(f"Images to animate #{pid}: {why}")
    if styles_:
        vid, why = video_batch.create(conn, _video_request(conn, pack_id, pid, req, styles_))
        if vid:
            made["video"][str(pid)] = vid
            n += 1
        else:
            made["errors"].append(f"Videos #{pid}: {why}")
    return n


def _next_steps(conn, pack_id: int, p: dict[str, Any]) -> None:
    """The pack job's own steps: start the products whose fill is done, and the animated stills whose images are."""
    from creative_intelligence.adstudio import runs, video_batch
    plan_ = p["plan"] or {}
    made = plan_.get("made") or {}
    changed = False
    while made.get("pending"):
        _start_product(conn, pack_id, made["pending"].pop(0), p["request"], made)
        changed = True
        with conn:                                   # saved after each product: a restart never starts one twice
            conn.execute("UPDATE ad_packs SET plan_json=? WHERE id=?", (json.dumps({**plan_, "made": made}), pack_id))
    for pid in list(made.get("filling") or []):
        st = conn.execute("SELECT fill_status FROM adstudio_products WHERE id=?", (pid,)).fetchone()
        if st and st[0] in ("queued", "running"):
            continue
        made["filling"].remove(pid)
        if st and st[0] == "failed":
            err = conn.execute("SELECT fill_error FROM adstudio_products WHERE id=?", (pid,)).fetchone()[0]
            made["errors"].append(f"Fill #{pid} (made what it could): {(err or '')[:120]}")
        _start_product(conn, pack_id, pid, p["request"], made)
        changed = True
    for key, a in (made.get("animate") or {}).items():
        if a.get("batch") or a.get("done"):
            continue
        r = runs.status(conn, a["run_id"])
        if r and r["status"] in ("queued", "running"):
            continue
        ids = unreviewed_images(conn, int(key))
        a["done"] = True
        changed = True
        if not ids:
            made["errors"].append(f"Animated still #{key}: none of the new images passed QA.")
            continue
        _approve_images(conn, ids[:max(a["count"], 3)])
        vid, why = video_batch.create(conn, _video_request(conn, pack_id, int(key), p["request"],
                                                           {video_batch.ANIMATE: a["count"]}))
        if vid:
            a["batch"] = vid
        else:
            made["errors"].append(f"Animated still #{key}: {why}")
    if changed:
        with conn:
            conn.execute("UPDATE ad_packs SET plan_json=?, message=? WHERE id=?",
                         (json.dumps({**plan_, "made": made}), "; ".join(made["errors"]), pack_id))
        p["plan"] = {**plan_, "made": made}


def progress(conn, p: dict[str, Any]) -> list[dict[str, Any]]:
    """Per product: the statics batch and the video batch, where each is."""
    from creative_intelligence.adstudio import batch, video_batch
    made = (p.get("plan") or {}).get("made") or {}
    out = []
    for pid in p["request"]["product_ids"]:
        b = batch.status(conn, made.get("bulk", {}).get(str(pid))) if made.get("bulk", {}).get(str(pid)) else None
        v = video_batch.status(conn, made.get("video", {}).get(str(pid))) if made.get("video", {}).get(str(pid)) else None
        a = (made.get("animate") or {}).get(str(pid)) or {}
        av = video_batch.status(conn, a["batch"]) if a.get("batch") else None
        name = conn.execute("SELECT name FROM adstudio_products WHERE id=?", (pid,)).fetchone()
        out.append({"product_id": pid, "name": name[0] if name else f"#{pid}",
                    "filling": pid in (made.get("filling") or []), "starting": pid in (made.get("pending") or []),
                    "animate": ({"status": "making images"} if a and not a.get("batch") and not a.get("done") else
                                {"id": av["id"], "status": av["status"]} if av else None),
                    "statics": {k: b[k] for k in ("id", "status", "done", "failed", "total")} if b else None,
                    "videos": {"id": v["id"], "status": v["status"],
                               "drafts": sum(1 for i in v["items"] if (i.get("draft") or {}).get("status") == "done"),
                               "total": sum(1 for i in v["items"] if i.get("keep")),
                               "failed": [{"style": i.get("label") or i.get("style"), "why": i["error"]}
                                          for i in v["items"] if i.get("error")]} if v else None})
    return out


def advance(db: Any, pack_id: int) -> dict[str, Any]:
    """The pack job: approve storyboards once written; 'review' when the statics are made and the drafts are in."""
    from creative_intelligence import jobs
    from creative_intelligence.adstudio import video_batch
    conn = db()
    try:
        p = get(conn, pack_id)
        if p is None or p["status"] != "producing":
            return {"status": (p or {}).get("status")}
        _next_steps(conn, pack_id, p)
        made = (p["plan"] or {}).get("made") or {}
        for vid in [*made.get("video", {}).values(),
                    *[a["batch"] for a in (made.get("animate") or {}).values() if a.get("batch")]]:
            st = video_batch.status(conn, vid)
            if st and st["status"] == "planned":
                r = video_batch.approve(conn, vid)
                if r.get("error"):
                    with conn:
                        conn.execute("UPDATE ad_packs SET message=? WHERE id=?", (f"Videos: {r['error']}", pack_id))
        rows = progress(conn, p)
        done = not ((p["plan"] or {}).get("made") or {}).get("pending") and all(not r["filling"]
                   and (not r["statics"] or r["statics"]["status"] in ("done", "cancelled", "failed"))
                   and (not r["videos"] or r["videos"]["status"] in ("drafts", "finals", "failed")
                        or (r["videos"]["status"] == "planned" and not r["videos"]["total"]))    # nothing to make
                   and (not r["animate"] or r["animate"]["status"] in ("drafts", "finals", "failed")) for r in rows)
        if done:
            with conn:
                conn.execute("UPDATE ad_packs SET status='review' WHERE id=?", (pack_id,))
            return {"status": "review"}
        jobs.enqueue(conn, "adpack", {"pack_id": pack_id}, delay_s=CHECK_S)
        return {"status": "producing"}
    finally:
        conn.close()
