"""AI Ads: briefs, formats and scenes, text check."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from flask import Blueprint, jsonify, request, send_file

from creative_intelligence.adstudio.profiles import prompt_stamp
from creative_intelligence.webapp import common
from creative_intelligence.webapp.common import _EXPORTED_SQL, _REVIEW_SQL, _asset_ids, _attach_assets, _attach_exports, _bulk_apply, _claims, _log_image_cost, _log_reviews, _primary_photo_uris, _product_dict, _qa_view, _textfree

bp = Blueprint("aiads", __name__)
log = logging.getLogger("adstudio")
_HERE = Path(__file__).parent.parent


def _compose_textad(conn, ad_id: int, out_path: str | None = None) -> dict[str, str]:
    """Draw an AI ad's copy (from its row + brief) onto its clean image with its format's template.

    Text drawn by code is exact by construction, so the ad is marked textcheck 'code'.
    """
    from creative_intelligence.adstudio.render_template import render_to
    from creative_intelligence.adstudio.store import textads_dir
    row = dict(conn.execute("SELECT * FROM adstudio_textads WHERE id=?", (ad_id,)).fetchone())
    b = _brief_row(conn, row["brief_id"]) or {"review_text": "", "review_author": ""}
    out = out_path or row.get("image_path") or str(textads_dir() / f"ad{ad_id}_p{row['product_id']}.png")
    copy = {"headline": row["headline"] or "", "sub": row["subhead"] or "", "cta": row["cta"] or "",
            "review": b.get("review_text") or "", "author": b.get("review_author") or "",
            "rating": (b.get("brief") or {}).get("review_rating")}
    metrics: dict[str, Any] = {}
    drawn = render_to(row["base_path"], out, "scene" if row["kind"] == "scene" else row["variant"], copy, metrics)
    with conn:
        # drawn by code from the brief: exact by construction, so the text counts as checked
        conn.execute("UPDATE adstudio_textads SET image_path=?, textcheck_status='code', textcheck_json=?,"
                     " text_checked=1, textcheck_at=datetime('now') WHERE id=?",
                     (out, json.dumps({"status": "code", "drawn": drawn, "problems": [], "notes": [],
                                       "legibility": metrics}), ad_id))
    from creative_intelligence.adstudio import qa
    if qa.scores_for(conn, "textad", [ad_id]):           # re-drawn copy: update its measured legibility
        qa.set_legibility(conn, "textad", ad_id, metrics)
    return drawn


def _brief_items(brief: dict[str, Any]) -> dict[str, str]:
    items = {"angle": brief.get("angle", ""), "hook": brief.get("hook", ""), "cta": brief.get("cta", "")}
    for i, t in enumerate(brief.get("primary_texts") or [], 1):
        items[f"primary text {i}"] = t
    for k, v in (brief.get("variants") or {}).items():
        items[f"{k} headline"] = (v or {}).get("headline", "")
        items[f"{k} sub"] = (v or {}).get("sub", "")
    return items


# ── AI Ads: finished ads with the copy drawn in (brief → 12 variants → scenes) ──
def _brief_row(conn, bid: int) -> dict[str, Any] | None:
    r = conn.execute("SELECT * FROM adstudio_briefs WHERE id=?", (bid,)).fetchone()
    if r is None:
        return None
    b = dict(r)
    b["brief"] = json.loads(b.pop("brief_json") or "{}")
    b["pattern_ids"] = json.loads(b.get("pattern_ids") or "[]")
    if b.get("idea_id"):
        t = conn.execute("SELECT title FROM ideas WHERE id=?", (b["idea_id"],)).fetchone()
        b["idea_title"] = t["title"] if t else ""
    from creative_intelligence.adstudio.facts import checks_for
    b["claims_check"] = checks_for(conn, "brief", [bid]).get(bid)
    return b


def _brief_patterns(conn, b: dict[str, Any]) -> list[dict[str, Any]]:
    """The patterns a brief was written from (re-read for its scenes)."""
    from creative_intelligence.adstudio.winning import get_pattern
    ids = [b["pattern_id"]] if b.get("pattern_id") else b.get("pattern_ids") or []
    return [p for p in (get_pattern(conn, int(i)) for i in ids) if p]


@bp.get("/api/adstudio/product/<int:pid>/briefs")
def adstudio_list_briefs(pid: int) -> Any:
    conn = common.db()
    rows = conn.execute(
        "SELECT b.id, b.created_at, b.brief_json, b.idea,"
        " (SELECT COUNT(*) FROM adstudio_textads t WHERE t.brief_id=b.id) AS n_ads"
        " FROM adstudio_briefs b WHERE b.product_id=? ORDER BY b.id DESC", (pid,)).fetchall()
    conn.close()
    out = [{"id": r["id"], "created_at": r["created_at"], "idea": r["idea"], "n_ads": r["n_ads"],
            "angle": json.loads(r["brief_json"] or "{}").get("angle", "")} for r in rows]
    return jsonify({"briefs": out})


def _library_review(conn, pid: int, review_id: Any) -> tuple[str, str, int | None, int | None]:
    """(text, author line, id, rating) of a real review from the library; nothing for anything else. Typed-in
    review text is never used (decision i: real reviews only)."""
    from creative_intelligence.adstudio import reviews
    if not str(review_id or "").isdigit():
        return "", "", None, None
    got = reviews.resolve(conn, [int(review_id)], pid, "review")
    if not got:
        return "", "", None, None
    r = got[0]
    return r["text"], f"{r['author']} · {r['source']}", r["id"], r["rating"]


@bp.post("/api/adstudio/product/<int:pid>/brief/draft")
def adstudio_draft_brief(pid: int) -> Any:
    """Body: {pattern: auto|none|<id>, idea?, idea_id?, review_id?}. Saves the draft. The Social Proof review is a
    real one from the library (review_id), never typed in. Without a typed or picked idea, the idea bank steers its
    share of briefs (rules.ideas_mix)."""
    from creative_intelligence.adstudio import ideas
    from creative_intelligence.adstudio.textads import draft_brief
    from creative_intelligence.adstudio.winning import current_patterns, get_pattern
    d = request.get_json(force=True, silent=True) or {}
    mode = str(d.get("pattern", "auto"))
    conn = common.db()
    idea, idea_id = ideas.pick_one(conn, pid, "aiads", (d.get("idea") or "").strip(), d.get("idea_id"))
    review, author, review_id, rating = _library_review(conn, pid, d.get("review_id"))
    prod = _product_dict(conn, pid)
    uris = _primary_photo_uris(conn, pid)
    if mode == "none":
        patterns = []
    elif mode.isdigit():
        one = get_pattern(conn, int(mode))
        patterns = [one] if one else []
    else:
        patterns = current_patterns(conn, limit=4)
    conn.close()
    if prod is None:
        return jsonify({"error": "product not found"}), 404
    if not patterns and not idea:
        return jsonify({"error": "No winning patterns to build on — sync from Meta, or type an idea to test."}), 400
    try:
        brief = draft_brief(prod, patterns, idea, bool(review), uris)
    except Exception as exc:
        log.exception("draft brief failed")
        return jsonify({"error": str(exc)[:300]}), 500
    if not brief["variants"]:
        return jsonify({"error": "Claude returned no usable brief — try again."}), 500
    labels = {p["id"]: p["label"] for p in patterns}
    brief["pattern_label"] = labels.get(brief["pattern_id"], "")
    brief["review_id"], brief["review_rating"] = review_id, rating
    conn = common.db()
    with conn:
        cur = conn.execute(
            "INSERT INTO adstudio_briefs (product_id, pattern_id, pattern_ids, idea, review_text,"
            " review_author, brief_json, idea_id) VALUES (?,?,?,?,?,?,?,?)",
            (pid, brief["pattern_id"], json.dumps([p["id"] for p in patterns]), idea, review,
             author, json.dumps(brief), idea_id))
    _claims(conn, "brief", cur.lastrowid, pid, _brief_items(brief))
    b = _brief_row(conn, cur.lastrowid)
    conn.close()
    return jsonify(b)


@bp.get("/api/adstudio/brief/<int:bid>")
def adstudio_get_brief(bid: int) -> Any:
    conn = common.db()
    b = _brief_row(conn, bid)
    conn.close()
    return jsonify(b) if b else (jsonify({"error": "not found"}), 404)


@bp.post("/api/adstudio/brief/<int:bid>/update")
def adstudio_update_brief(bid: int) -> Any:
    """Body: {brief: {...}, review_id?} — the operator's edits (the review is picked from the library)."""
    from creative_intelligence.adstudio.textads import normalize_brief
    d = request.get_json(force=True, silent=True) or {}
    conn = common.db()
    b = _brief_row(conn, bid)
    if b is None:
        conn.close(); return jsonify({"error": "not found"}), 404
    merged = {**b["brief"], **(d.get("brief") or {})}
    nb = normalize_brief(merged)
    nb["pattern_id"] = b["brief"].get("pattern_id")
    nb["pattern_label"] = b["brief"].get("pattern_label", "")
    if "review_id" in d:
        review, author, nb["review_id"], nb["review_rating"] = _library_review(conn, b["product_id"], d.get("review_id"))
    else:
        review, author = b["review_text"], b["review_author"]
        nb["review_id"], nb["review_rating"] = b["brief"].get("review_id"), b["brief"].get("review_rating")
    with conn:
        conn.execute("UPDATE adstudio_briefs SET brief_json=?, review_text=?, review_author=? WHERE id=?",
                     (json.dumps(nb), review, author, bid))
    _claims(conn, "brief", bid, b["product_id"], _brief_items(nb))
    out = _brief_row(conn, bid)
    conn.close()
    return jsonify(out)


@bp.post("/api/adstudio/brief/<int:bid>/delete")
def adstudio_delete_brief(bid: int) -> Any:
    conn = common.db()
    with conn:
        conn.execute("DELETE FROM adstudio_textads WHERE brief_id=?", (bid,))
        conn.execute("DELETE FROM adstudio_briefs WHERE id=?", (bid,))
    conn.close()
    return jsonify({"ok": True})


@bp.post("/api/adstudio/brief/<int:bid>/variants")
def adstudio_queue_variants(bid: int) -> Any:
    """Queue the fixed formats for this brief (one ad each). Body: {aspect_ratio?}."""
    from creative_intelligence.adstudio.textads import DEFAULT_ASPECT, VARIANTS, variants_for
    from creative_intelligence.adstudio.generate import MODEL as IMG_MODEL
    d = request.get_json(force=True, silent=True) or {}
    aspect = d.get("aspect_ratio") or DEFAULT_ASPECT
    conn = common.db()
    b = _brief_row(conn, bid)
    if b is None:
        conn.close(); return jsonify({"error": "not found"}), 404
    prod = _product_dict(conn, b["product_id"])
    br = b["brief"]
    texts = br.get("primary_texts") or [""]
    run = conn.execute("SELECT COUNT(*) FROM adstudio_textads WHERE brief_id=? AND kind='variant'",
                       (bid,)).fetchone()[0]
    keys = variants_for(prod or {}, bool(b["review_text"]))
    queued = []
    with conn:
        for i, k in enumerate(keys):
            v = br.get("variants", {}).get(k, {})
            fields = VARIANTS[k]["fields"]
            cur = conn.execute(
                "INSERT INTO adstudio_textads (product_id, brief_id, kind, variant, title, headline,"
                " subhead, cta, body, style_seed, aspect_ratio, model, status)"
                " VALUES (?,?,'variant',?,?,?,?,?,?,?,?,?,'queued')",
                (b["product_id"], bid, k, VARIANTS[k]["name"],
                 v.get("headline", "") if "headline" in fields else "",
                 v.get("sub", "") if "sub" in fields else "",
                 br.get("cta", "") if "cta" in fields else "",
                 texts[i % len(texts)], bid * 31 + run + i, aspect, IMG_MODEL))
            conn.execute("UPDATE adstudio_textads SET prompt_version=? WHERE id=?",
                         (prompt_stamp(prod, "aiad"), cur.lastrowid))
            queued.append({"id": cur.lastrowid, "variant": k})
    conn.close()
    return jsonify({"queued": queued})


@bp.post("/api/adstudio/brief/<int:bid>/scenes")
def adstudio_queue_scenes(bid: int) -> Any:
    """Claude invents N new scenes, each with copy written for it. Body: {n, aspect_ratio?}."""
    from creative_intelligence.adstudio.textads import DEFAULT_ASPECT, invent_scenes
    from creative_intelligence.adstudio.generate import MODEL as IMG_MODEL
    d = request.get_json(force=True, silent=True) or {}
    n = max(1, min(int(d.get("n", 4)), 12))
    aspect = d.get("aspect_ratio") or DEFAULT_ASPECT
    conn = common.db()
    b = _brief_row(conn, bid)
    if b is None:
        conn.close(); return jsonify({"error": "not found"}), 404
    pid = b["product_id"]
    prod = _product_dict(conn, pid)
    uris = _primary_photo_uris(conn, pid)
    patterns = _brief_patterns(conn, b)
    prior = [dict(r) for r in conn.execute(
        "SELECT archetype, title, concept, created_at FROM ("
        " SELECT variant AS archetype, title, concept, created_at, id FROM adstudio_textads"
        "  WHERE product_id=? AND kind='scene'"
        " UNION ALL SELECT archetype, title, concept, created_at, id FROM adstudio_images"
        "  WHERE product_id=? AND core_id IS NULL)"
        " ORDER BY created_at DESC, id DESC LIMIT 60", (pid, pid)).fetchall()]
    conn.close()
    try:
        scenes = invent_scenes(prod, b["brief"], n, prior, patterns, b["idea"], uris)
    except Exception as exc:
        log.exception("invent scenes failed")
        return jsonify({"error": str(exc)[:300]}), 500
    if not scenes:
        return jsonify({"error": "no concepts returned"}), 500
    conn = common.db()
    queued = []
    with conn:
        for sc in scenes:
            cur = conn.execute(
                "INSERT INTO adstudio_textads (product_id, brief_id, kind, variant, title, concept,"
                " text_layout, headline, subhead, cta, body, aspect_ratio, model, status)"
                " VALUES (?,?,'scene',?,?,?,?,?,?,?,?,?,?,'queued')",
                (pid, bid, sc["archetype"], sc["title"], sc["scene"], sc["text_layout"],
                 sc["headline"], sc["sub"], sc["cta"], sc["body"], aspect, IMG_MODEL))
            conn.execute("UPDATE adstudio_textads SET prompt_version=? WHERE id=?",
                         (prompt_stamp(prod, "aiad"), cur.lastrowid))
            queued.append({"id": cur.lastrowid, "title": sc["title"]})
    conn.close()
    return jsonify({"queued": queued})


def _textad_prompt(conn, row: dict[str, Any]) -> str:
    """Build the prompt from the row's CURRENT copy + fix note (so edits apply)."""
    from creative_intelligence.adstudio import textads as T
    prod = _product_dict(conn, row["product_id"]) or {}
    if _textfree(row):
        b = _brief_row(conn, row["brief_id"]) or {"brief": {}}
        return T.build_textfree_prompt(prod, b["brief"], row["variant"], row["aspect_ratio"], row["fix"] or "",
                                       scene=row["concept"] if row["kind"] == "scene" else "")
    if row["kind"] == "scene":
        return T.build_scene_prompt(prod, row["concept"], row["headline"], row["subhead"],
                                    row["cta"], row["text_layout"], row["aspect_ratio"], row["fix"] or "")
    b = _brief_row(conn, row["brief_id"]) or {"brief": {}, "review_text": "", "review_author": ""}
    return T.build_variant_prompt(prod, b["brief"], row["variant"], row["headline"], row["subhead"],
                                  row["cta"], b["review_text"], b["review_author"], row["style_seed"],
                                  row["aspect_ratio"], row["fix"] or "")


def run_textad(ad_id: int) -> dict[str, Any]:
    """Generate one queued AI ad: {id, status} or {id, status: failed, error}. Used by the route and by bulk batches."""
    from creative_intelligence.adstudio.generate import generate_ad, AdGenError
    from creative_intelligence.adstudio.store import save_generated_image, textads_dir
    conn = common.db()
    row = conn.execute("SELECT * FROM adstudio_textads WHERE id=?", (ad_id,)).fetchone()
    if row is None:
        conn.close(); return {"id": ad_id, "status": "missing", "error": "not found"}
    row = dict(row)
    prompt = _textad_prompt(conn, row)
    uris = _primary_photo_uris(conn, row["product_id"], limit=3)
    if row.get("ref_path") and Path(row["ref_path"]).exists():      # "Make ours like this": the reference goes last
        from creative_intelligence.adstudio.store import to_data_uri
        from creative_intelligence.adstudio.visuals import LIKE_RULE
        uris = uris + [to_data_uri(row["ref_path"])]
        prompt = f"{prompt}\n\n{LIKE_RULE}"
    with conn:
        conn.execute("UPDATE adstudio_textads SET status='generating', prompt=? WHERE id=?",
                     (prompt, ad_id))
    conn.close()
    try:
        url = generate_ad(prompt, uris, row["aspect_ratio"])
        saved = save_generated_image(url, row["product_id"], ad_id, textads_dir())
        conn = common.db()
        if _textfree(row):
            base = str(Path(saved).with_name(Path(saved).stem + "_base.png"))
            Path(saved).replace(base)                    # keep the clean image; the ad is drawn on top
            with conn:
                conn.execute("UPDATE adstudio_textads SET base_path=? WHERE id=?", (base, ad_id))
            _compose_textad(conn, ad_id, saved)
        else:
            with conn:
                conn.execute("UPDATE adstudio_textads SET image_path=? WHERE id=?", (saved, ad_id))
        with conn:
            conn.execute("UPDATE adstudio_textads SET status='done', error_message=NULL WHERE id=?", (ad_id,))
        _log_image_cost(conn, "textad", ad_id)
        conn.close()
        if not _textfree(row):
            common.textcheck_later([ad_id])
        common.qa_later([("textad", ad_id)])
        return {"id": ad_id, "status": "done"}
    except (AdGenError, Exception) as exc:
        msg = str(exc)[:400]
        conn = common.db()
        with conn:
            conn.execute("UPDATE adstudio_textads SET status='failed', error_message=? WHERE id=?",
                         (msg, ad_id))
        conn.close()
        return {"id": ad_id, "status": "failed", "error": msg}


@bp.post("/api/adstudio/textad/<int:ad_id>/run")
def adstudio_run_textad(ad_id: int) -> Any:
    """Generate one queued AI ad (the frontend calls these one at a time)."""
    r = run_textad(ad_id)
    if r["status"] == "missing":
        return jsonify({"error": "not found"}), 404
    if r["status"] == "failed":
        code = 429 if "429" in r["error"] or "throttled" in r["error"].lower() else 500
        return jsonify(r), code
    return jsonify(r)


@bp.post("/api/adstudio/textad/<int:ad_id>/regenerate")
def adstudio_regen_textad(ad_id: int) -> Any:
    """
    Body: {fix?, headline?, subhead?, cta?, run?}. Saves the fix note and any
    copy edits, clears the old review (it's a new image), then re-runs unless
    run=false.
    """
    d = request.get_json(force=True, silent=True) or {}
    conn = common.db()
    if conn.execute("SELECT 1 FROM adstudio_textads WHERE id=?", (ad_id,)).fetchone() is None:
        conn.close(); return jsonify({"error": "not found"}), 404
    sets, vals = ["status='queued'", "error_message=NULL", "review_status=''", "is_ready=0",
                  "text_checked=0"], []
    for k in ("fix", "headline", "subhead", "cta"):
        if k in d:
            sets.append(f"{k}=?"); vals.append((d.get(k) or "").strip())
    row = conn.execute("SELECT base_path FROM adstudio_textads WHERE id=?", (ad_id,)).fetchone()
    if row["base_path"] and Path(row["base_path"]).exists() and not (d.get("fix") or "").strip():
        # text-free ad + copy-only edit: re-draw the copy, no new image (instant, free)
        sets = [x for x in sets if x != "status='queued'"]
        with conn:
            conn.execute(f"UPDATE adstudio_textads SET {', '.join(sets)} WHERE id=?", (*vals, ad_id))
        drawn = _compose_textad(conn, ad_id)
        conn.close()
        return jsonify({"id": ad_id, "status": "done", "redrawn": True, "drawn": drawn})
    with conn:
        conn.execute(f"UPDATE adstudio_textads SET {', '.join(sets)} WHERE id=?", (*vals, ad_id))
    conn.close()
    if d.get("run", True) is False:
        return jsonify({"id": ad_id, "status": "queued"})
    return adstudio_run_textad(ad_id)


_TEXTAD_COLS = ("t.id, t.product_id, t.brief_id, t.kind, t.variant, t.title, t.headline, t.subhead,"
                " t.cta, t.body, t.fix, t.status, t.error_message, t.is_favorite, t.archived,"
                " t.review_status, t.is_ready, t.text_checked, t.review_notes, t.aspect_ratio,"
                " t.created_at, t.image_path IS NOT NULL AS has_image,"
                " t.textcheck_status, t.textcheck_json")


@bp.post("/api/adstudio/textads/textcheck")
def adstudio_textcheck() -> Any:
    """Body: {ids} or {product_id, unchecked: true}. Starts the text check in the background."""
    d = request.get_json(force=True, silent=True) or {}
    conn = common.db()
    if d.get("ids"):
        ids = [int(x) for x in d["ids"] if str(x).isdigit()]
    else:
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM adstudio_textads WHERE product_id=? AND image_path IS NOT NULL AND archived=0"
            " AND COALESCE(textcheck_status,'') IN ('', 'error')", (int(d.get("product_id") or 0),))]
    conn.close()
    if ids:
        common.textcheck_later(ids)
    return jsonify({"started": len(ids)})


@bp.get("/api/adstudio/textads")
def adstudio_list_textads() -> Any:
    """?product_id [&brief_id] [&done=1 (gallery)] [&review=…] [&include_archived=1]."""
    pid = request.args.get("product_id")
    bid = request.args.get("brief_id")
    done = request.args.get("done") == "1"
    include_archived = request.args.get("include_archived") == "1"
    where, params = ["1=1"], []
    if pid:
        where.append("t.product_id=?"); params.append(int(pid))
    if bid:
        where.append("t.brief_id=?"); params.append(int(bid))
    if done:
        where.append("t.status='done' AND t.image_path IS NOT NULL")
    if not include_archived:
        where.append("t.archived=0")
    base = " AND ".join(where)
    review_where = {"unreviewed": " AND COALESCE(t.review_status,'')=''",
                    "approved": " AND t.review_status='approved'",
                    "ready": " AND t.is_ready=1",
                    "rejected": " AND t.review_status='rejected'",
                    "notexported": f" AND NOT EXISTS ({_EXPORTED_SQL})"}
    conn = common.db()
    rows = [dict(r) for r in conn.execute(
        f"SELECT {_TEXTAD_COLS}, b.brief_json FROM adstudio_textads t"
        f" LEFT JOIN adstudio_briefs b ON b.id=t.brief_id WHERE {base}"
        + review_where.get(request.args.get("review", ""), "")
        + " ORDER BY t.id DESC LIMIT 400", params).fetchall()]
    for r in rows:
        bj = json.loads(r.pop("brief_json") or "{}")
        r["angle"], r["pattern_label"] = bj.get("angle", ""), bj.get("pattern_label", "")
    _attach_exports(conn, rows)
    if done:
        _attach_assets(conn, "textad", rows, int(pid) if pid else None)
        rows, n_failed = _qa_view(conn, "textad", rows, request.args.get("review", ""), request.args.get("sort", ""))
    else:
        n_failed = 0
    counts = {"all": 0, "unreviewed": 0, "approved": 0, "ready": 0, "rejected": 0, "notexported": 0}
    for r in conn.execute(f"SELECT COALESCE(t.review_status,'') AS s, t.is_ready,"
                          f" EXISTS ({_EXPORTED_SQL}) AS exported FROM adstudio_textads t"
                          f" WHERE {base}", params).fetchall():
        counts["all"] += 1
        counts[r["s"] or "unreviewed"] = counts.get(r["s"] or "unreviewed", 0) + 1
        counts["ready"] += 1 if r["is_ready"] else 0
        counts["notexported"] += 0 if r["exported"] else 1
    counts["qa_rejected"] = n_failed
    conn.close()
    return jsonify({"ads": rows, "counts": counts})


@bp.get("/api/adstudio/textad/<int:ad_id>/image")
def adstudio_textad_image(ad_id: int) -> Any:
    conn = common.db()
    row = conn.execute("SELECT image_path FROM adstudio_textads WHERE id=?", (ad_id,)).fetchone()
    conn.close()
    if row is None or not row["image_path"] or not Path(row["image_path"]).exists():
        return ("", 404)
    return send_file(row["image_path"], mimetype="image/png", conditional=True)


@bp.post("/api/adstudio/textad/<int:ad_id>/favorite")
def adstudio_fav_textad(ad_id: int) -> Any:
    conn = common.db()
    row = conn.execute("SELECT is_favorite FROM adstudio_textads WHERE id=?", (ad_id,)).fetchone()
    if row is None:
        conn.close(); return jsonify({"error": "not found"}), 404
    nv = 0 if row["is_favorite"] else 1
    with conn:
        conn.execute("UPDATE adstudio_textads SET is_favorite=? WHERE id=?", (nv, ad_id))
    conn.close()
    return jsonify({"id": ad_id, "is_favorite": nv})


@bp.post("/api/adstudio/textad/<int:ad_id>/delete")
def adstudio_del_textad(ad_id: int) -> Any:
    conn = common.db()
    with conn:
        conn.execute("DELETE FROM adstudio_textads WHERE id=?", (ad_id,))
    conn.close()
    return jsonify({"ok": True})


@bp.post("/api/adstudio/textad/<int:ad_id>/review")
def adstudio_review_textad(ad_id: int) -> Any:
    """
    Body: {action?: approve|reject|ready|clear, notes?, text_checked?}. Same
    toggles as the composite Gallery, plus: an ad can only be marked ready once
    its text has been checked (spelling, price, claims) — the AI drew it.
    """
    from datetime import datetime as _dt
    d = request.get_json(force=True, silent=True) or {}
    action = d.get("action", "")
    conn = common.db()
    row = conn.execute("SELECT review_status, is_ready, text_checked FROM adstudio_textads WHERE id=?",
                       (ad_id,)).fetchone()
    if row is None:
        conn.close(); return jsonify({"error": "not found"}), 404
    checked = bool(d["text_checked"]) if "text_checked" in d else bool(row["text_checked"])
    if action == "approve" and row["review_status"] == "approved" and not row["is_ready"]:
        action = "clear"
    elif action == "reject" and row["review_status"] == "rejected":
        action = "clear"
    elif action == "ready" and row["is_ready"]:
        action = "unready"
    if action and action not in _REVIEW_SQL:
        conn.close(); return jsonify({"error": f"bad action {action!r}"}), 400
    if action == "ready" and not checked:
        conn.close()
        return jsonify({"error": "Check the text first — tick “text checked” once spelling, "
                                 "prices and claims are right."}), 400
    if action:
        _log_reviews(conn, "adstudio_textads", [ad_id], action)
    with conn:
        if "text_checked" in d:
            conn.execute("UPDATE adstudio_textads SET text_checked=?" + ("" if checked else ", is_ready=0")
                         + " WHERE id=?", (int(checked), ad_id))
        if action:
            conn.execute(f"UPDATE adstudio_textads SET {_REVIEW_SQL[action]}, reviewed_at=? WHERE id=?",
                         (_dt.utcnow().isoformat(), ad_id))
        if "notes" in d:
            conn.execute("UPDATE adstudio_textads SET review_notes=? WHERE id=?",
                         ((d.get("notes") or "").strip(), ad_id))
    r = dict(conn.execute("SELECT id, review_status, is_ready, text_checked, review_notes"
                          " FROM adstudio_textads WHERE id=?", (ad_id,)).fetchone())
    conn.close()
    return jsonify(r)


@bp.post("/api/adstudio/textads/bulk")
def adstudio_textads_bulk() -> Any:
    d = request.get_json(force=True, silent=True) or {}
    try:
        n = _bulk_apply("adstudio_textads", d.get("action", ""), d.get("ids", []))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"affected": n})


@bp.get("/api/adstudio/export-textads")
def adstudio_export_textads() -> Any:
    """
    Zip a product's AI ads. ?ready=1 → launch kit: ready ads only, plus
    manifest.csv (file, on-image copy, primary text, format, angle, pattern,
    notes) for Ads Manager.
    """
    import csv, io, re as _re, zipfile
    pid = request.args.get("product_id")
    ready = request.args.get("ready") == "1"
    conn = common.db()
    rows = [dict(r) for r in conn.execute(
        "SELECT t.id, t.image_path, t.kind, t.variant, t.title, t.headline, t.subhead, t.cta,"
        " t.body, t.review_notes, t.aspect_ratio, b.brief_json"
        " FROM adstudio_textads t LEFT JOIN adstudio_briefs b ON b.id=t.brief_id"
        " WHERE t.product_id=? AND t.image_path IS NOT NULL"
        + (" AND t.is_ready=1" if ready else "") + " ORDER BY t.id",
        (int(pid),)).fetchall()] if pid else []
    prod = _product_dict(conn, int(pid)) if pid else None
    aid = _asset_ids(conn, "textad", [r["id"] for r in rows])
    conn.close()
    slug = _re.sub(r"[^a-z0-9]+", "-", ((prod or {}).get("name") or "product").lower()).strip("-")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        manifest = io.StringIO()
        w = csv.writer(manifest)
        w.writerow(["file", "meta_ad_name", "format", "headline", "subhead", "cta", "primary_text", "angle",
                    "pattern", "aspect_ratio", "ai_generated", "notes"])
        for r in rows:
            if not Path(r["image_path"]).exists():
                continue
            ad_name = aid.get(r["id"]) or f"{slug}_ai_{r['id']}"
            name = f"{ad_name}.png"
            z.write(r["image_path"], arcname=name)
            bj = json.loads(r["brief_json"] or "{}")
            fmt = r["title"] if r["kind"] == "variant" else f"scene: {r['variant']} — {r['title']}"
            w.writerow([name, ad_name, fmt, r["headline"], r["subhead"], r["cta"], r["body"] or "",
                        bj.get("angle", ""), bj.get("pattern_label", ""), r["aspect_ratio"],
                        "yes", r["review_notes"] or ""])
        if ready:
            z.writestr("manifest.csv", manifest.getvalue())
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"{slug}_{'ai_launch_kit' if ready else 'ai_ads'}.zip")
