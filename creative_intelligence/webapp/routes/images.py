"""Images: generation, runs, fan-out, review and QA."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from flask import Blueprint, jsonify, request, send_file

from creative_intelligence import config
from creative_intelligence.adstudio.profiles import prompt_stamp
from creative_intelligence.webapp import common
from creative_intelligence.webapp.common import _bulk_apply, _log_image_cost, _primary_photo_uris, _product_dict, _qa_view

bp = Blueprint("images", __name__)
log = logging.getLogger("adstudio")
_HERE = Path(__file__).parent.parent


# ── Prompt preview (free) ────────────────────────────────────────────────


# ── Queue + run generation ───────────────────────────────────────────────


# ── Gallery ──────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────
# AD STUDIO v2 — diversity-seed image library + programmatic overlay
# ─────────────────────────────────────────────

@bp.post("/api/adstudio/product/<int:pid>/gen-images")
def adstudio_gen_images(pid: int) -> Any:
    """Claude invents N distinct text-free concepts (avoiding prior ones); queue them."""
    from creative_intelligence.adstudio.concepts import generate_concepts, build_image_prompt
    from creative_intelligence.adstudio.formats import DEFAULT_ASPECT
    d = request.get_json(force=True, silent=True) or {}
    n = max(1, min(int(d.get("n", 4)), 12))
    aspect = d.get("aspect_ratio", DEFAULT_ASPECT)

    conn = common.db()
    prod = _product_dict(conn, pid)
    if prod is None:
        conn.close(); return jsonify({"error": "product not found"}), 404
    uris = _primary_photo_uris(conn, pid)
    prior = [dict(r) for r in conn.execute(
        "SELECT archetype, title, concept FROM adstudio_images WHERE product_id=?"
        " ORDER BY id DESC LIMIT 60", (pid,)).fetchall()]
    conn.close()

    try:
        concepts = generate_concepts(prod, n, prior, uris)
    except Exception as exc:
        log.exception("gen-images concepts failed")
        return jsonify({"error": str(exc)[:300]}), 500
    if not concepts:
        return jsonify({"error": "no concepts returned"}), 500

    conn = common.db()
    created = []
    with conn:
        for cpt in concepts:
            prompt = build_image_prompt(prod, cpt["scene"], aspect)
            cur = conn.execute(
                "INSERT INTO adstudio_images (product_id, archetype, title, concept,"
                " prompt, aspect_ratio, model, status) VALUES (?,?,?,?,?,?,?, 'queued')",
                (pid, cpt["archetype"], cpt["title"], cpt["scene"], prompt, aspect, config.CI_IMAGE_MODEL))
            conn.execute("UPDATE adstudio_images SET prompt_version=? WHERE id=?",
                         (prompt_stamp(prod, "image"), cur.lastrowid))
            created.append({"id": cur.lastrowid, "title": cpt["title"],
                            "archetype": cpt["archetype"]})
    conn.close()
    return jsonify({"queued": created})


@bp.post("/api/adstudio/image/<int:img_id>/run")
def adstudio_run_image(img_id: int) -> Any:
    """Generate one queued text-free image (frontend calls these sequentially)."""
    from creative_intelligence.adstudio.generate import generate_ad, AdGenError
    from creative_intelligence.adstudio.store import save_generated_image
    conn = common.db()
    row = conn.execute("SELECT * FROM adstudio_images WHERE id=?", (img_id,)).fetchone()
    if row is None:
        conn.close(); return jsonify({"error": "not found"}), 404
    row = dict(row)
    uris = _primary_photo_uris(conn, row["product_id"], limit=3)
    with conn:
        conn.execute("UPDATE adstudio_images SET status='generating' WHERE id=?", (img_id,))
    conn.close()
    try:
        url = generate_ad(row["prompt"], uris, row["aspect_ratio"])
        saved = save_generated_image(url, row["product_id"], img_id)
        conn = common.db()
        with conn:
            conn.execute("UPDATE adstudio_images SET status='done', image_path=?,"
                         " error_message=NULL WHERE id=?", (saved, img_id))
        _log_image_cost(conn, "image", img_id)
        conn.close()
        common.qa_later([("image", img_id)])
        return jsonify({"id": img_id, "status": "done"})
    except (AdGenError, Exception) as exc:
        msg = str(exc)[:400]
        conn = common.db()
        with conn:
            conn.execute("UPDATE adstudio_images SET status='failed', error_message=? WHERE id=?",
                         (msg, img_id))
        conn.close()
        code = 429 if "429" in msg or "throttled" in msg.lower() else 500
        return jsonify({"id": img_id, "status": "failed", "error": msg}), code


@bp.post("/api/adstudio/image/<int:img_id>/regenerate")
def adstudio_regen_image(img_id: int) -> Any:
    """Re-run a concept, optionally with operator feedback ('fix') folded in.

    Rebuilds the prompt from the stored concept with the CURRENT builders, so
    prompt improvements (label lock, ingredient accuracy) and the fix note both
    apply — instead of blindly re-running the stale stored prompt.
    """
    from creative_intelligence.adstudio.concepts import build_image_prompt
    d = request.get_json(force=True, silent=True) or {}
    fix = (d.get("fix") or "").strip()

    conn = common.db()
    row = conn.execute("SELECT * FROM adstudio_images WHERE id=?", (img_id,)).fetchone()
    if row is None:
        conn.close(); return jsonify({"error": "not found"}), 404
    row = dict(row)
    prod = _product_dict(conn, row["product_id"])
    new_prompt = row["prompt"]
    if prod and row.get("concept"):
        try:
            new_prompt = build_image_prompt(prod, row["concept"], row["aspect_ratio"], fix)
        except Exception:
            pass
    with conn:
        conn.execute("UPDATE adstudio_images SET status='queued', error_message=NULL,"
                     " prompt=? WHERE id=?", (new_prompt, img_id))
    conn.close()
    return adstudio_run_image(img_id)


@bp.get("/api/adstudio/images")
def adstudio_list_images() -> Any:
    pid = request.args.get("product_id")
    include_archived = request.args.get("include_archived") == "1"
    conn = common.db()
    sql = ("SELECT id, product_id, archetype, title, status, is_favorite, archived, review_status,"
           " aspect_ratio, error_message, created_at, image_path IS NOT NULL AS has_image"
           " FROM adstudio_images WHERE core_id IS NULL AND parent_image_id IS NULL")
    params: list[Any] = []
    if pid:
        sql += " AND product_id=?"; params.append(int(pid))
    if not include_archived:
        sql += " AND archived=0"
    sql += " ORDER BY id DESC LIMIT 400"
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    rows, n_failed = _qa_view(conn, "image", rows, request.args.get("view", "all_qa"), request.args.get("sort", ""))
    conn.close()
    return jsonify({"images": rows, "qa_rejected": n_failed})


@bp.post("/api/adstudio/image/<int:img_id>/review")
def adstudio_review_image(img_id: int) -> Any:
    """Body: {action: approve|reject} — toggles like the galleries."""
    d = request.get_json(force=True, silent=True) or {}
    action = d.get("action", "")
    conn = common.db()
    row = conn.execute("SELECT review_status FROM adstudio_images WHERE id=?", (img_id,)).fetchone()
    conn.close()
    if row is None:
        return jsonify({"error": "not found"}), 404
    if (action, row["review_status"]) in (("approve", "approved"), ("reject", "rejected")):
        action = "clear"
    if action not in ("approve", "reject", "clear"):
        return jsonify({"error": f"bad action {action!r}"}), 400
    _bulk_apply("adstudio_images", action, [img_id])
    return jsonify({"id": img_id, "review_status": {"approve": "approved", "reject": "rejected", "clear": ""}[action]})


@bp.post("/api/adstudio/qa/score")
def adstudio_qa_backfill() -> Any:
    """Body: {product_id}. Scores this product's finished images that have no score yet (background)."""
    d = request.get_json(force=True, silent=True) or {}
    pid = int(d.get("product_id") or 0)
    conn = common.db()
    jobs = []
    for source, table in (("image", "adstudio_images"), ("textad", "adstudio_textads"), ("core", "adstudio_core")):
        jobs += [(source, r[0]) for r in conn.execute(
            f"SELECT id FROM {table} t WHERE product_id=? AND image_path IS NOT NULL"
            f" AND NOT EXISTS (SELECT 1 FROM qa_scores q WHERE q.source=? AND q.source_id=t.id AND q.error='')",
            (pid, source))]
    conn.close()
    if jobs:
        common.qa_later(jobs)
    return jsonify({"started": len(jobs)})


# ── Runs: 8 / 12 / 16 / 20 finished images in one pass (T5) ─────────────────
@bp.post("/api/adstudio/product/<int:pid>/runs")
def adstudio_start_run(pid: int) -> Any:
    """Body: {n: 8|12|16|20, aspect_ratio}. Plans and makes the run on the server."""
    from creative_intelligence.adstudio import runs
    d = request.get_json(force=True, silent=True) or {}
    conn = common.db()
    if _product_dict(conn, pid) is None:
        conn.close(); return jsonify({"error": "product not found"}), 404
    if conn.execute("SELECT 1 FROM adstudio_runs WHERE product_id=? AND status IN ('queued','planning','running')",
                    (pid,)).fetchone():
        conn.close(); return jsonify({"error": "A run for this product is already in progress."}), 409
    run_id, reason = runs.create_run(conn, pid, int(d.get("n") or 0), d.get("aspect_ratio") or "4:5")
    conn.close()
    if run_id is None:
        return jsonify({"error": reason}), 400
    runs.start_in_background(common.db, run_id)
    return jsonify({"run_id": run_id})


@bp.post("/api/adstudio/run/<int:run_id>/cancel")
def adstudio_run_cancel(run_id: int) -> Any:
    from creative_intelligence.adstudio import runs
    conn = common.db()
    ok = runs.cancel(conn, run_id)
    conn.close()
    return jsonify({"ok": True}) if ok else (jsonify({"error": "This run isn't running."}), 400)


@bp.get("/api/adstudio/run/<int:run_id>")
def adstudio_run_status(run_id: int) -> Any:
    from creative_intelligence.adstudio import runs
    conn = common.db()
    st = runs.status(conn, run_id)
    conn.close()
    return jsonify(st) if st else (jsonify({"error": "not found"}), 404)


@bp.get("/api/adstudio/product/<int:pid>/runs")
def adstudio_list_runs(pid: int) -> Any:
    conn = common.db()
    rows = [dict(r) for r in conn.execute(
        "SELECT id, size, aspect_ratio, status, delivered, spent_usd, budget_usd, message, created_at, finished_at"
        " FROM adstudio_runs WHERE product_id=? ORDER BY id DESC LIMIT 10", (pid,))]
    conn.close()
    return jsonify({"runs": rows})


# ── Fan-out: one image -> headlines x layouts x sizes (T5) ───────────────────
FANOUT_TEMPLATES = ["direct_response", "bold_type", "lifestyle_tagline"]


FANOUT_SIZES = ["1:1", "4:5", "9:16"]


@bp.post("/api/adstudio/image/<int:img_id>/fanout")
def adstudio_fanout(img_id: int) -> Any:
    """Body: {hook_ids?: up to 3 (default: the 3 newest hooks), templates?: 2-3}. Makes every
    headline x layout x size (1:1, 4:5, 9:16); layouts adapt to each size, the photo is never cropped."""
    from creative_intelligence.adstudio import qa
    from creative_intelligence.adstudio.overlay import compose
    from creative_intelligence.adstudio.sizes import fit_canvas
    from creative_intelligence.adstudio.store import composites_dir
    from creative_intelligence.adstudio.templates import TEMPLATES
    d = request.get_json(force=True, silent=True) or {}
    conn = common.db()
    img = conn.execute("SELECT * FROM adstudio_images WHERE id=?", (img_id,)).fetchone()
    if img is None or not img["image_path"] or not Path(img["image_path"]).exists():
        conn.close(); return jsonify({"error": "image not found"}), 404
    img = dict(img)
    hook_ids = [int(x) for x in (d.get("hook_ids") or [])][:3]
    q = "SELECT * FROM adstudio_hooks WHERE product_id=?" + (
        f" AND id IN ({','.join('?' * len(hook_ids))})" if hook_ids else "") + " ORDER BY id DESC LIMIT 3"
    hooks = [dict(r) for r in conn.execute(q, [img["product_id"], *hook_ids])]
    templates = [t for t in (d.get("templates") or FANOUT_TEMPLATES) if t in TEMPLATES
                 and not {"items", "reviews", "review"} & {e.get("field") for e in TEMPLATES[t]["elements"]}][:3]
    if not hooks:
        conn.close(); return jsonify({"error": "Save some hooks first (Hooks tab)."}), 400
    if len(templates) < 2:
        conn.close(); return jsonify({"error": "Pick 2 or 3 layouts."}), 400
    made = []
    for size in FANOUT_SIZES:
        with conn:
            sid = conn.execute(
                "INSERT INTO adstudio_images (product_id, archetype, title, concept, prompt, aspect_ratio, model,"
                " status, parent_image_id) VALUES (?,?,?,?,'', ?, ?, 'done', ?)",
                (img["product_id"], img["archetype"], f"{img['title']} · {size}", img["concept"], size,
                 img["model"], img_id)).lastrowid
            conn.execute("UPDATE adstudio_images SET prompt_version=? WHERE id=?", (img["prompt_version"], sid))
        sized = fit_canvas(img["image_path"], size, str(composites_dir() / f"img{img_id}_{size.replace(':', 'x')}.png"))
        with conn:
            conn.execute("UPDATE adstudio_images SET image_path=? WHERE id=?", (sized, sid))
        for h in hooks:
            for t in templates:
                layout = {"template": t}
                with conn:
                    cid = conn.execute(
                        "INSERT INTO adstudio_composites (product_id, image_id, hook_id, headline, subhead, cta,"
                        " layout_json) VALUES (?,?,?,?,?,?,?)",
                        (img["product_id"], sid, h["id"], h["headline"] or h["hook_text"], h["subhead"] or "",
                         h["cta"] or "", json.dumps(layout))).lastrowid
                out = str(composites_dir() / f"comp{cid}.png")
                metrics: dict[str, Any] = {}
                compose(sized, out, headline=h["headline"] or h["hook_text"], subhead=h["subhead"] or "",
                        cta=h["cta"] or "", layout=layout, metrics=metrics)
                with conn:
                    conn.execute("UPDATE adstudio_composites SET output_path=? WHERE id=?", (out, cid))
                qa.inherit(conn, "composite", cid, "image", img_id, metrics)   # scores of the original photo
                made.append(cid)
    conn.close()
    return jsonify({"made": len(made), "sizes": FANOUT_SIZES, "templates": templates, "hooks": len(hooks)})


@bp.post("/api/adstudio/images/bulk")
def adstudio_images_bulk() -> Any:
    d = request.get_json(force=True, silent=True) or {}
    try:
        n = _bulk_apply("adstudio_images", d.get("action", ""), d.get("ids", []))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"affected": n})


@bp.get("/api/adstudio/image-file/<int:img_id>")
def adstudio_image_file(img_id: int) -> Any:
    conn = common.db()
    row = conn.execute("SELECT image_path FROM adstudio_images WHERE id=?", (img_id,)).fetchone()
    conn.close()
    if row is None or not row["image_path"] or not Path(row["image_path"]).exists():
        return ("", 404)
    return send_file(row["image_path"], mimetype="image/png", conditional=True)


@bp.post("/api/adstudio/image/<int:img_id>/favorite")
def adstudio_fav_image(img_id: int) -> Any:
    conn = common.db()
    row = conn.execute("SELECT is_favorite FROM adstudio_images WHERE id=?", (img_id,)).fetchone()
    if row is None:
        conn.close(); return jsonify({"error": "not found"}), 404
    nv = 0 if row["is_favorite"] else 1
    with conn:
        conn.execute("UPDATE adstudio_images SET is_favorite=? WHERE id=?", (nv, img_id))
    conn.close()
    return jsonify({"id": img_id, "is_favorite": nv})


@bp.post("/api/adstudio/image/<int:img_id>/delete")
def adstudio_del_image(img_id: int) -> Any:
    conn = common.db()
    with conn:
        conn.execute("DELETE FROM adstudio_images WHERE id=?", (img_id,))
    conn.close()
    return jsonify({"ok": True})
