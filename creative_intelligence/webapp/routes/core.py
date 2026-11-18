"""Core product-page photos and infographics."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from flask import Blueprint, jsonify, request, send_file

from creative_intelligence.adstudio.profiles import prompt_stamp
from creative_intelligence.webapp import common
from creative_intelligence.webapp.common import _asset_ids, _log_image_cost, _product_dict, _qa_view, _uri

bp = Blueprint("core", __name__)
log = logging.getLogger("adstudio")
_HERE = Path(__file__).parent.parent


# ── Core photos (core1-3) + box-size infographics ───────────────────────────
def _core_row(conn, cid: int) -> dict[str, Any] | None:
    r = conn.execute("SELECT * FROM adstudio_core WHERE id=?", (cid,)).fetchone()
    return dict(r) if r else None


def _core_refs(conn, pid: int, kind: str) -> tuple[list[str], list[str]]:
    """
    (layout_uris, fruit_uris) for a core shot. Layout = this product's photo
    tagged with the same role, else up to 2 from other products (house
    examples). Fruit = this product's remaining real photos. Infographics and
    other-fruit photos are never sent.
    """
    rows = [dict(r) for r in conn.execute(
        "SELECT id, path, role FROM adstudio_photos WHERE product_id=?"
        " ORDER BY is_primary DESC, id", (pid,)).fetchall()]
    rows = [r for r in rows if (r["role"] or "") not in ("infographic", "other")]
    own = [r for r in rows if r["role"] == kind][:1]
    # borrowed only from a product of the same type (2026-11-13: product shots for candles and other products; a
    # candle must never copy a fruit box's layout)
    from creative_intelligence.adstudio.core import product_type
    types = {r["id"]: product_type(dict(r)) for r in conn.execute("SELECT id, product_type FROM adstudio_products")}
    mine = types.get(pid, "fruit")
    layout = own or [dict(r) for r in conn.execute(
        "SELECT MIN(id) AS id, path, product_id FROM adstudio_photos WHERE role=? AND product_id!=?"
        " GROUP BY product_id ORDER BY id", (kind, pid)).fetchall() if types.get(r["product_id"]) == mine][:2]
    used = {r["id"] for r in own}
    order = {"core1": 0, "core2": 1, "core3": 2, "": 3}
    fruit = sorted((r for r in rows if r["id"] not in used),
                   key=lambda r: order.get(r["role"] or "", 3))[:max(0, 5 - len(layout))]
    to = lambda rs: [u for u in (_uri(r["path"]) for r in rs) if u]
    return to(layout), to(fruit)


@bp.post("/api/adstudio/product/<int:pid>/core/queue")
def adstudio_core_queue(pid: int) -> Any:
    """
    Queue core shots: {"kinds": ["core1","core2","core3"], "n": 1}. Claude writes
    a shot brief from the layout + fruit references (one per kind; one per
    lifestyle setup for core3) before anything reaches the image model.
    """
    from creative_intelligence.adstudio.core import (
        CORE_KINDS, DEFAULT_ASPECT, build_core_prompt, write_shot_brief)
    d = request.get_json(force=True, silent=True) or {}
    kinds = [k for k in (d.get("kinds") or list(CORE_KINDS)) if k in CORE_KINDS]
    n = max(1, min(int(d.get("n", 1)), 6))
    conn = common.db()
    prod = _product_dict(conn, pid)
    if prod is None:
        conn.close(); return jsonify({"error": "product not found"}), 404
    queued = []
    for k in kinds:
        layout, fruit = _core_refs(conn, pid, k)
        base = conn.execute("SELECT COUNT(*) FROM adstudio_core WHERE product_id=? AND kind=?",
                            (pid, k)).fetchone()[0]
        briefs: dict[int, str] = {}
        for i in range(n):
            variant = base + i
            key = variant % 5 if k == "core3" else 0
            if key not in briefs:
                try:
                    briefs[key] = write_shot_brief(prod, k, variant, layout, fruit)
                except Exception:
                    log.exception("shot brief failed (%s)", k)
                    briefs[key] = ""
            prompt = build_core_prompt(prod, k, variant, brief=briefs[key],
                                       n_layout=len(layout), n_fruit=len(fruit))
            with conn:
                cur = conn.execute(
                    "INSERT INTO adstudio_core (product_id, kind, variant, brief, prompt, aspect_ratio)"
                    " VALUES (?,?,?,?,?,?)", (pid, k, variant, briefs[key], prompt, DEFAULT_ASPECT))
                conn.execute("UPDATE adstudio_core SET prompt_version=? WHERE id=?",
                             (prompt_stamp(prod, "core"), cur.lastrowid))
            queued.append({"id": cur.lastrowid, "kind": k})
    if queued:                                       # made in the worker: leaving the page never stalls them
        from creative_intelligence import jobs
        jobs.enqueue(conn, "core_shots", {"ids": [q["id"] for q in queued]})
    conn.close()
    return jsonify({"queued": queued})


@bp.post("/api/adstudio/core/<int:cid>/run")
def adstudio_core_run(cid: int) -> Any:
    out = run_core(cid)
    if out.get("status") == "missing":
        return jsonify({"error": "not found"}), 404
    if out["status"] == "failed":
        code = 429 if "429" in out["error"] or "throttled" in out["error"].lower() else 500
        return jsonify(out), code
    return jsonify(out)


def run_core(cid: int) -> dict[str, Any]:
    """Make one product shot (worker job core_shots, or the route above): {id, status[, error]}."""
    from creative_intelligence.adstudio.core import build_core_prompt
    from creative_intelligence.adstudio.generate import generate_ad, AdGenError
    from creative_intelligence.adstudio.store import save_generated_image, core_dir
    conn = common.db()
    row = _core_row(conn, cid)
    if row is None or row["kind"].startswith("info_"):
        conn.close(); return {"id": cid, "status": "missing"}
    # rebuilt at run time so the image labels always match what is actually sent
    layout, fruit = _core_refs(conn, row["product_id"], row["kind"])
    prompt = build_core_prompt(_product_dict(conn, row["product_id"]) or {}, row["kind"],
                               row["variant"], row.get("fix") or "", row.get("brief") or "",
                               len(layout), len(fruit))
    with conn:
        conn.execute("UPDATE adstudio_core SET status='generating', prompt=? WHERE id=?",
                     (prompt, cid))
    conn.close()
    try:
        url = generate_ad(prompt, layout + fruit, row["aspect_ratio"])
        saved = save_generated_image(url, row["product_id"], cid)
        dest = core_dir() / f"{row['kind']}_{cid}_p{row['product_id']}.png"
        Path(saved).replace(dest)
        conn = common.db()
        with conn:
            conn.execute("UPDATE adstudio_core SET status='done', image_path=?, error_message=NULL"
                         " WHERE id=?", (str(dest), cid))
        _log_image_cost(conn, "core", cid)
        conn.close()
        common.qa_later([("core", cid)])
        return {"id": cid, "status": "done"}
    except (AdGenError, Exception) as exc:
        msg = str(exc)[:400]
        conn = common.db()
        with conn:
            conn.execute("UPDATE adstudio_core SET status='failed', error_message=? WHERE id=?",
                         (msg, cid))
        conn.close()
        return {"id": cid, "status": "failed", "error": msg}


@bp.post("/api/adstudio/core/<int:cid>/regenerate")
def adstudio_core_regen(cid: int) -> Any:
    """Re-run a core shot with an operator fix note (kept for later reruns)."""
    d = request.get_json(force=True, silent=True) or {}
    conn = common.db()
    row = _core_row(conn, cid)
    if row is None or row["kind"].startswith("info_"):
        conn.close(); return jsonify({"error": "not found"}), 404
    with conn:
        conn.execute("UPDATE adstudio_core SET status='queued', error_message=NULL, fix=?"
                     " WHERE id=?", ((d.get("fix") or "").strip(), cid))
    from creative_intelligence import jobs
    jobs.enqueue(conn, "core_shots", {"ids": [cid]})
    conn.close()
    return jsonify({"id": cid, "status": "queued"})


@bp.post("/api/adstudio/product/<int:pid>/infographic")
def adstudio_infographic(pid: int) -> Any:
    """
    Render a box-size infographic. Body: {"kind": "pdp"|"bab", "info": {...},
    "thumb": "core:<id>" | "photo:<id>" | ""}. `info` is saved on the product.
    """
    from creative_intelligence.adstudio.infographic import render_pdp, render_bab
    from creative_intelligence.adstudio.store import core_dir
    d = request.get_json(force=True, silent=True) or {}
    kind = d.get("kind")
    if kind not in ("pdp", "bab"):
        return jsonify({"error": "kind must be pdp or bab"}), 400
    info = d.get("info") or {}
    conn = common.db()
    if _product_dict(conn, pid) is None:
        conn.close(); return jsonify({"error": "product not found"}), 404
    with conn:
        conn.execute("UPDATE adstudio_products SET info_json=? WHERE id=?", (json.dumps(info), pid))

    thumb_path = None
    src, _, sid = (d.get("thumb") or "").partition(":")
    if src == "core" and sid.isdigit():
        r = conn.execute("SELECT image_path FROM adstudio_core WHERE id=?", (int(sid),)).fetchone()
        thumb_path = r["image_path"] if r else None
    elif src == "photo" and sid.isdigit():
        r = conn.execute("SELECT path FROM adstudio_photos WHERE id=?", (int(sid),)).fetchone()
        thumb_path = r["path"] if r else None

    cur = None
    with conn:
        cur = conn.execute("INSERT INTO adstudio_core (product_id, kind, status) VALUES (?,?, 'queued')",
                           (pid, f"info_{kind}"))
    cid = cur.lastrowid
    out = str(core_dir() / f"info_{kind}_{cid}_p{pid}.png")
    name = info.get("name") or ""
    estimate = info.get("estimate", "pieces")
    try:
        if kind == "pdp":
            rows = [r for r in info.get("rows", []) if r.get("on", True)]
            render_pdp(name, rows, out, thumb_path=thumb_path, estimate=estimate)
        else:
            render_bab(name, info.get("bab") or {}, out, thumb_path=thumb_path, estimate=estimate)
    except Exception as exc:
        with conn:
            conn.execute("DELETE FROM adstudio_core WHERE id=?", (cid,))
        conn.close()
        return jsonify({"error": str(exc)[:300]}), 400
    with conn:
        conn.execute("UPDATE adstudio_core SET status='done', image_path=? WHERE id=?", (out, cid))
    conn.close()
    return jsonify({"id": cid})


@bp.get("/api/adstudio/core")
def adstudio_core_list() -> Any:
    pid = request.args.get("product_id")
    conn = common.db()
    rows = [dict(r) for r in conn.execute(
        "SELECT id, kind, variant, status, is_favorite, error_message, created_at, brief, fix,"
        " image_path IS NOT NULL AS has_image FROM adstudio_core WHERE product_id=?"
        " ORDER BY id DESC LIMIT 400", (int(pid or 0),)).fetchall()]
    rows, _ = _qa_view(conn, "core", rows, "all_qa")
    conn.close()
    return jsonify({"items": rows})


@bp.get("/api/adstudio/core/<int:cid>/image")
def adstudio_core_image(cid: int) -> Any:
    conn = common.db()
    row = conn.execute("SELECT image_path FROM adstudio_core WHERE id=?", (cid,)).fetchone()
    conn.close()
    if row is None or not row["image_path"] or not Path(row["image_path"]).exists():
        return ("", 404)
    return send_file(row["image_path"], conditional=True)


@bp.post("/api/adstudio/core/<int:cid>/favorite")
def adstudio_core_fav(cid: int) -> Any:
    conn = common.db()
    with conn:
        conn.execute("UPDATE adstudio_core SET is_favorite=1-is_favorite WHERE id=?", (cid,))
    conn.close()
    return jsonify({"ok": True})


@bp.post("/api/adstudio/core/<int:cid>/delete")
def adstudio_core_delete(cid: int) -> Any:
    conn = common.db()
    with conn:
        conn.execute("DELETE FROM adstudio_core WHERE id=?", (cid,))
    conn.close()
    return jsonify({"ok": True})


@bp.get("/api/adstudio/export-core")
def adstudio_export_core() -> Any:
    """Zip a product's core set: favorites if any, else everything done."""
    import io, re as _re, zipfile
    pid = int(request.args.get("product_id") or 0)
    conn = common.db()
    prod = _product_dict(conn, pid) or {"name": "product"}
    rows = conn.execute("SELECT id, kind, is_favorite, image_path FROM adstudio_core"
                        " WHERE product_id=? AND image_path IS NOT NULL ORDER BY kind, id",
                        (pid,)).fetchall()
    aid = _asset_ids(conn, "core", [r["id"] for r in rows])
    conn.close()
    if any(r["is_favorite"] for r in rows):
        rows = [r for r in rows if r["is_favorite"]]
    slug = _re.sub(r"[^a-z0-9]+", "-", prod["name"].lower()).strip("-") or "product"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for r in rows:
            if Path(r["image_path"]).exists():
                z.write(r["image_path"], arcname=f"{aid.get(r['id']) or slug + '_' + r['kind'] + '_' + str(r['id'])}.png")
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"{slug}_core.zip")
