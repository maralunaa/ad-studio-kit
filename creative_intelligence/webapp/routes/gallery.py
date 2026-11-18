"""Gallery: images, Compose ads, AI ads and Core shots in one list."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from flask import Blueprint, jsonify, request, send_file

from creative_intelligence.adstudio import prelaunch
from creative_intelligence.webapp import common
from creative_intelligence.webapp.common import _EXPORTED_SQL, _attach_exports, _bulk_apply, _product_dict

bp = Blueprint("gallery", __name__)
log = logging.getLogger("adstudio")
_HERE = Path(__file__).parent.parent


# ── One merged gallery: images, Compose ads, AI ads and Core shots ─────────────
GALLERY_KINDS = {"image": "Images", "compose": "Compose ads", "ai": "AI ads", "core": "Core shots", "video": "Videos"}


def _gallery_items(conn, pid: int, include_archived: bool = False) -> list[dict[str, Any]]:
    """Every finished picture for a product, in one shape, newest first."""
    from creative_intelligence.adstudio import assets, facts, qa
    arch = "" if include_archived else " AND archived=0"
    items: list[dict[str, Any]] = []
    for r in conn.execute(
            "SELECT id, title, archetype, review_status, is_favorite, archived, aspect_ratio, created_at, run_id"
            " FROM adstudio_images WHERE product_id=? AND image_path IS NOT NULL AND core_id IS NULL"
            f" AND parent_image_id IS NULL{arch}", (pid,)):
        items.append({"kind": "image", "id": r["id"], "title": r["title"] or "", "sub": r["archetype"] or "",
                      "review_status": r["review_status"] or "", "is_ready": 0, "is_favorite": r["is_favorite"],
                      "archived": r["archived"], "aspect_ratio": r["aspect_ratio"] or "4:5",
                      "created_at": r["created_at"], "src": f"/api/adstudio/image-file/{r['id']}"})
    for r in conn.execute(
            "SELECT c.id, c.headline, c.subhead, c.cta, c.review_status, c.is_ready, c.review_notes, c.is_favorite,"
            " c.archived, c.created_at, c.hook_id, i.aspect_ratio FROM adstudio_composites c"
            " LEFT JOIN adstudio_images i ON i.id=c.image_id WHERE c.product_id=? AND c.output_path IS NOT NULL"
            + arch.replace("archived", "c.archived"), (pid,)):
        items.append({"kind": "compose", "id": r["id"], "title": r["headline"] or "", "sub": r["subhead"] or "",
                      "cta": r["cta"] or "", "review_status": r["review_status"] or "", "is_ready": r["is_ready"],
                      "review_notes": r["review_notes"] or "", "is_favorite": r["is_favorite"], "archived": r["archived"],
                      "aspect_ratio": r["aspect_ratio"] or "4:5", "created_at": r["created_at"],
                      "src": f"/api/adstudio/composite/{r['id']}/image"})
    for r in conn.execute(
            "SELECT t.id, t.kind AS tkind, t.variant, t.title, t.headline, t.subhead, t.cta, t.review_status, t.is_ready,"
            " t.text_checked, t.review_notes, t.is_favorite, t.archived, t.aspect_ratio, t.created_at,"
            " t.textcheck_status, t.textcheck_json, EXISTS (" + _EXPORTED_SQL + ") AS exported"
            " FROM adstudio_textads t WHERE t.product_id=? AND t.status='done' AND t.image_path IS NOT NULL"
            + arch.replace("archived", "t.archived"), (pid,)):
        items.append({"kind": "ai", "id": r["id"], "title": r["headline"] or r["title"] or "", "headline": r["headline"] or "",
                      "sub": r["subhead"] or "", "subhead": r["subhead"] or "",
                      "cta": r["cta"] or "", "format": r["title"] or r["variant"], "ad_kind": r["tkind"],
                      "review_status": r["review_status"] or "", "is_ready": r["is_ready"],
                      "text_checked": r["text_checked"], "review_notes": r["review_notes"] or "",
                      "is_favorite": r["is_favorite"], "archived": r["archived"], "aspect_ratio": r["aspect_ratio"] or "9:16",
                      "created_at": r["created_at"], "textcheck_status": r["textcheck_status"] or "",
                      "textcheck_json": r["textcheck_json"] or "", "exported": bool(r["exported"]),
                      "src": f"/api/adstudio/textad/{r['id']}/image"})
    for r in conn.execute("SELECT id, kind, is_favorite, created_at, aspect_ratio FROM adstudio_core"
                          " WHERE product_id=? AND image_path IS NOT NULL", (pid,)):
        items.append({"kind": "core", "id": r["id"], "title": r["kind"].replace("info_", "infographic ").replace("core", "Core "),
                      "sub": "", "review_status": "", "is_ready": 0, "is_favorite": r["is_favorite"], "archived": 0,
                      "aspect_ratio": r["aspect_ratio"] or "1:1", "created_at": r["created_at"],
                      "src": f"/api/adstudio/core/{r['id']}/image"})
    _video_items(conn, pid, items, include_archived)
    src = {"image": "image", "compose": "composite", "ai": "textad", "core": "core"}
    for kind, source in src.items():
        ids = [i["id"] for i in items if i["kind"] == kind]
        q, a = qa.scores_for(conn, source, ids), assets.assets_for(conn, source, ids) if kind != "image" else {}
        ck = facts.checks_for(conn, source if kind != "ai" else "textad", ids)
        for i in items:
            if i["kind"] == kind:
                i["qa"], i["asset"], i["claims_check"] = q.get(i["id"]), a.get(i["id"]), ck.get(i["id"])
    _attach_exports(conn, [i for i in items if i["kind"] == "ai"])
    prelaunch.attach(conn, items)
    items.sort(key=lambda i: i["created_at"] or "", reverse=True)
    return items


def _video_items(conn, pid: int, items: list[dict[str, Any]], include_archived: bool) -> None:
    """Finished videos (each size/headline version is one item) and clips still being made or failed."""
    from creative_intelligence.adstudio import assets, qa
    arch = "" if include_archived else " AND r.archived=0"
    rows = [dict(r) for r in conn.execute(
        "SELECT r.*, r.duration_s AS render_s, v.style, v.duration_s AS video_s, v.motion_preset, v.ai_person, v.source_kind,"
        " v.source_id, json_extract(v.copy_json, '$.mode') AS mode"
        " FROM adstudio_video_renders r JOIN adstudio_videos v ON v.id=r.video_id"
        f" WHERE r.product_id=? AND r.status='done' AND r.output_path IS NOT NULL{arch}", (pid,))]
    q = qa.scores_for(conn, "video", sorted({r["video_id"] for r in rows}))
    a = assets.assets_for(conn, "video_render", [r["id"] for r in rows])
    for r in rows:
        items.append({"kind": "video", "id": r["id"], "video_id": r["video_id"],
                      "title": r["headline"] or f"{r['style'].title()} · {r['motion_preset'].replace('_', ' ') or 'custom motion'}",
                      "sub": r["subhead"], "cta": r["cta"],
                      "format": f"{_style_label(r['style'], r['mode'])} · {r['aspect']} · {round(r['render_s'] or r['video_s'] or 0)} s",
                      "mode": r["mode"] or "",
                      "style": r["style"], "length_s": round(r["render_s"] or r["video_s"] or 0), "ai_person": bool(r["ai_person"]),
                      "render_note": r["note"],
                      "review_status": r["review_status"], "is_ready": r["is_ready"], "review_notes": r["review_notes"],
                      "is_favorite": r["is_favorite"], "archived": r["archived"], "aspect_ratio": r["aspect"],
                      "created_at": r["created_at"], "src": f"/api/adstudio/video-render/{r['id']}/file",
                      "poster": f"/api/adstudio/video-render/{r['id']}/poster",
                      "qa": q.get(r["video_id"]), "asset": a.get(r["id"]), "claims_check": None})
    done = {r["video_id"] for r in rows}
    usual = typical_minutes(conn)
    for v in conn.execute("SELECT id, status, step, message, created_at, motion_preset, style,"
                          " ROUND((julianday('now') - julianday(created_at)) * 1440) AS so_far FROM adstudio_videos"
                          " WHERE product_id=? AND (status IN ('queued','running') OR (status='failed' AND"
                          " created_at >= datetime('now','-2 day')))", (pid,)):
        if v["id"] in done:
            continue
        items.append({"kind": "video", "id": -v["id"], "video_id": v["id"], "pending": v["status"] != "failed",
                      "failed": v["status"] == "failed", "title": v["step"] or v["message"] or "Queued",
                      "sub": v["message"] if v["status"] == "failed" else "", "format": "animate",
                      "review_status": "", "is_ready": 0, "is_favorite": 0, "archived": 0, "aspect_ratio": "9:16",
                      "created_at": v["created_at"], "src": "", "qa": None, "asset": None, "claims_check": None,
                      "usual_min": usual.get(v["style"]), "so_far_min": int(v["so_far"] or 0)})


def typical_minutes(conn) -> dict[str, int]:
    """{style: minutes} the last 10 finished videos of each format took, start to finish (new shots included): the
    median, shown while one is being made (feedback 2026-11-02: "usually 2–4 min is not true")."""
    times: dict[str, list[float]] = {}
    for r in conn.execute("SELECT style, (julianday(finished_at) - julianday(created_at)) * 1440 AS m FROM adstudio_videos"
                          " WHERE status='done' AND finished_at IS NOT NULL AND style IS NOT NULL ORDER BY id DESC"):
        if len(t := times.setdefault(r["style"], [])) < 10 and r["m"] is not None and r["m"] >= 0:
            t.append(r["m"])
    return {k: max(1, round(sorted(t)[len(t) // 2])) for k, t in times.items() if t}


STAGES = {"draft": "Draft", "review": "In review", "approved": "Approved", "exported": "Exported", "live": "Live",
          "rejected": "Rejected"}


def stage(i: dict[str, Any]) -> str:
    """The one status path every asset shows: Draft → In review → Approved → Exported → Live (or Rejected).
    Draft = still being made or not yet quality-scored; Live = linked to a Meta ad."""
    if (i.get("asset") or {}).get("meta_ad_id"):
        return "live"
    if i.get("review_status") == "rejected":
        return "rejected"
    if i.get("exported") or i.get("exports"):
        return "exported"
    if i.get("is_ready") or i.get("review_status") == "approved":
        return "approved"
    if i.get("pending") or i.get("failed") or (i["kind"] in ("image", "compose", "ai", "video") and not i.get("qa")):
        return "draft"
    return "review"


LENGTH_BANDS = ("15", "30", "long")


def length_band(i: dict[str, Any]) -> str:
    """Video length band for the filter: up to 15 s, 16–30 s, longer."""
    secs = float(i.get("length_s") or 0)
    return "15" if secs <= 16 else "30" if secs <= 32 else "long"


def _gallery_status(i: dict[str, Any]) -> str:
    if i["is_ready"]:
        return "ready"
    return i["review_status"] or "unreviewed"


def _products_with_work(conn) -> list[int]:
    return [r[0] for r in conn.execute(
        "SELECT product_id FROM adstudio_images WHERE image_path IS NOT NULL UNION SELECT product_id FROM"
        " adstudio_composites UNION SELECT product_id FROM adstudio_textads UNION SELECT product_id FROM adstudio_core"
        " UNION SELECT product_id FROM adstudio_video_renders UNION SELECT product_id FROM adstudio_videos")]


@bp.get("/api/adstudio/stage-counts")
def adstudio_stage_counts() -> Any:
    """What is waiting in each stage for the product in the top bar (the counts on the stage strip)."""
    pid = int(request.args.get("product_id") or 0)
    conn = common.db()
    out = {
        "images.review": conn.execute(
            "SELECT COUNT(*) FROM adstudio_images WHERE product_id=? AND image_path IS NOT NULL AND archived=0"
            " AND core_id IS NULL AND parent_image_id IS NULL AND COALESCE(review_status,'')=''", (pid,)).fetchone()[0],
        "video.review": conn.execute(
            "SELECT COUNT(*) FROM adstudio_video_renders WHERE product_id=? AND status='done' AND archived=0"
            " AND COALESCE(review_status,'')='' AND is_ready=0", (pid,)).fetchone()[0],
    }
    conn.close()
    return jsonify(out)


@bp.get("/api/adstudio/gallery")
def adstudio_gallery() -> Any:
    """?product_id &type=all|image|compose|ai|core &status=all|unreviewed|approved|ready|rejected|notexported
    &score=all|passed|qa_rejected|4plus &sort=newest|score &include_archived=1"""
    from creative_intelligence.adstudio import assets
    pid = int(request.args.get("product_id") or 0)      # 0 / missing: every product
    kind, status = request.args.get("type", "all"), request.args.get("status", "all")
    vstyle, vlen = request.args.get("style", "all"), request.args.get("length", "all")
    score, sort = request.args.get("score", "all"), request.args.get("sort", "newest")
    conn = common.db()
    pids = [pid] if pid else _products_with_work(conn)
    names = {r[0]: r[1] for r in conn.execute("SELECT id, name FROM adstudio_products")}
    items = []
    for p in pids:
        assets.ensure_assets(conn, p)
        for i in _gallery_items(conn, p, request.args.get("include_archived") == "1"):
            i["product_id"], i["product"] = p, names.get(p, "")
            items.append(i)
    conn.close()
    items.sort(key=lambda i: i["created_at"] or "", reverse=True)
    for i in items:
        i["stage"] = stage(i)

    def qa_failed(i):
        return bool(i["qa"] and i["qa"]["total"] is not None and not i["qa"]["passed"])

    counts = {"type": {k: 0 for k in ["all", *GALLERY_KINDS]}, "status": {}, "score": {},
              "launch_kit": sum(1 for i in items if i["kind"] in ("compose", "ai", "video") and i["is_ready"])}
    for i in items:
        counts["type"]["all"] += 1
        counts["type"][i["kind"]] += 1
    pool = [i for i in items if kind in ("all", i["kind"])]
    vids = [i for i in pool if i["kind"] == "video" and i["id"] > 0]
    counts["style"] = {"all": len(vids), **{k: sum(1 for i in vids if i.get("style") == k) for k in sorted({i.get("style") for i in vids if i.get("style")})}}
    counts["length"] = {"all": len(vids), **{k: sum(1 for i in vids if length_band(i) == k) for k in LENGTH_BANDS}}
    if vstyle != "all":
        pool = [i for i in pool if i["kind"] == "video" and i.get("style") == vstyle]
    if vlen != "all":
        pool = [i for i in pool if i["kind"] == "video" and length_band(i) == vlen]
    by_status = {"all": pool, "notexported": [i for i in pool if i["kind"] == "ai" and not i["exported"]]}
    for st in ("unreviewed", "approved", "ready", "rejected"):
        by_status[st] = [i for i in pool if _gallery_status(i) == st or (st == "approved" and i["is_ready"])]
    counts["status"] = {k: len(v) for k, v in by_status.items()}
    pool = by_status.get(status, pool)
    # every video shows, QA-rejected ones too, with their QA reason (feedback 2026-11-02: "show all videos")
    by_score = {"all": [i for i in pool if not qa_failed(i) or i["kind"] == "video"], "passed": [i for i in pool if i["qa"] and i["qa"]["passed"]],
                "4plus": [i for i in pool if i["qa"] and (i["qa"]["total"] or 0) >= 4],
                "qa_rejected": [i for i in pool if qa_failed(i)]}
    counts["score"] = {k: len(v) for k, v in by_score.items()}
    out = by_score.get(score, by_score["all"])
    if sort == "score":
        out = sorted(out, key=lambda i: -((i["qa"] or {}).get("total") or 0))
    return jsonify({"items": out[:600], "counts": counts, "kinds": GALLERY_KINDS, "stages": STAGES})


@bp.get("/api/adstudio/export-gallery")
def adstudio_export_gallery() -> Any:
    """?product_id &ready=1 -> launch kit: every ready Compose ad, AI ad and video, named by Asset ID, with
    manifest.csv. Without ready=1: everything finished in the gallery."""
    import io, zipfile
    from creative_intelligence.adstudio import launchkit
    pid = int(request.args.get("product_id") or 0)
    ready = request.args.get("ready") == "1"
    keys = [k for k in (request.args.get("keys") or "").split(",") if k]   # the selected ones (2026-10-30)
    conn = common.db()
    if keys and not pid:                     # all products: each selected file by its own row
        kit = []
        for k in keys:
            kind, _, i = k.partition(":")
            if kind not in launchkit.PATHS or not i.isdigit():
                continue
            table, col = launchkit.PATHS[kind]
            row = conn.execute(f"SELECT {col} FROM {table} WHERE id=?", (int(i),)).fetchone()
            if row and row[0] and Path(row[0]).exists():
                kit.append({"path": row[0], "name": f"{kind}_{i}{Path(row[0]).suffix or '.png'}", "key": k, "row": []})
    else:
        kit = [f for f in launchkit.files(conn, pid, ready and not keys) if not keys or f["key"] in set(keys)]
    prod = _product_dict(conn, pid) or {"name": "product"}
    conn.close()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in kit:
            z.write(f["path"], arcname=f["name"])
        if any(f["row"] for f in kit):
            z.writestr("manifest.csv", launchkit.manifest([f["row"] for f in kit if f["row"]]))
    buf.seek(0)
    import re as _re
    slug = _re.sub(r"[^a-z0-9]+", "-", prod["name"].lower()).strip("-") or "product"
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"{slug}_{'selected' if keys else 'launch_kit' if ready else 'gallery'}.zip")


@bp.post("/api/adstudio/drive-kit")
def adstudio_drive_kit() -> Any:
    """{product_id, date} -> the launch kit (statics and videos) and its launch sheet go to a new Drive folder,
    in the worker. Answers {job_id, n}; follow it with GET /api/adstudio/drive-kit/<job_id>."""
    import re as _re
    from datetime import date as _date
    from creative_intelligence import jobs
    from creative_intelligence.adstudio import launchkit
    d = request.get_json(force=True, silent=True) or {}
    pid = int(d.get("product_id") or 0)
    day = d.get("date") if _re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(d.get("date", ""))) else _date.today().isoformat()
    conn = common.db()
    try:
        if _product_dict(conn, pid) is None:
            return jsonify({"error": "product not found"}), 404
        if not launchkit.connected(conn):
            return jsonify({"error": "Connect Google Drive first."}), 400
        n = len(launchkit.files(conn, pid))
        if not n:
            return jsonify({"error": "Nothing is marked ready for this product."}), 400
        jid = jobs.enqueue(conn, "drive_kit", {"product_id": pid, "date": day}, dedupe_key=f"drive_kit:{pid}")
    finally:
        conn.close()
    return jsonify({"job_id": jid, "n": n})


@bp.get("/api/adstudio/drive-kit/<int:jid>")
def adstudio_drive_kit_status(jid: int) -> Any:
    conn = common.db()
    r = conn.execute("SELECT status, error, result_json FROM jobs WHERE id=? AND kind='drive_kit'", (jid,)).fetchone()
    conn.close()
    if r is None:
        return jsonify({"error": "not found"}), 404
    return jsonify({"status": r["status"], "error": (r["error"] or "").split("\n")[0][:300],
                    "result": json.loads(r["result_json"]) if r["result_json"] else None})


def _style_label(style: str, mode: str | None) -> str:
    """'Fruit Story · narrated' for styled videos; the raw style for animate and story clips."""
    from creative_intelligence.adstudio import styles
    return styles.label(style, mode) if style in styles.catalog() else style


def _meta_button(cta: str) -> str:
    """The Meta CTA button that says the same as the on-image CTA (playbook: they must match)."""
    from creative_intelligence import knowledge
    from creative_intelligence.adstudio.qa import cta_family
    fam = cta_family(cta)
    return knowledge.load("rules")["cta"]["allowed"][fam]["label"] if fam else ""


def _check_list(i: dict[str, Any], status: str) -> str:
    """The checklist lines with this status, for the launch kit's manifest."""
    return "; ".join(x["text"] + (f" ({x['note']})" if x.get("note") else "")
                     for x in (i.get("prelaunch") or {}).get("items", []) if x["status"] == status)


GALLERY_TABLES = {"image": "adstudio_images", "compose": "adstudio_composites", "ai": "adstudio_textads",
                  "video": "adstudio_video_renders"}


CORE_BULK = {"favorite": "UPDATE adstudio_core SET is_favorite=1", "unfavorite": "UPDATE adstudio_core SET is_favorite=0",
             "delete": "DELETE FROM adstudio_core"}


@bp.post("/api/adstudio/gallery/bulk")
def adstudio_gallery_bulk() -> Any:
    """{action, items: [{kind, id}]} -> one action across kinds. Actions a kind can't take are skipped
    (images can't be 'ready'; Core shots only take favorite, unfavorite and delete)."""
    d = request.get_json(force=True, silent=True) or {}
    action, by_kind = d.get("action", ""), {}
    for it in d.get("items", []):
        by_kind.setdefault(it.get("kind"), []).append(it.get("id"))
    affected = skipped = 0
    for kind, ids in by_kind.items():
        if kind == "core":
            ids = [int(x) for x in ids if str(x).isdigit()]
            if action not in CORE_BULK or not ids:
                skipped += len(ids)
                continue
            conn = common.db()
            with conn:
                affected += conn.execute(f"{CORE_BULK[action]} WHERE id IN ({','.join('?' * len(ids))})", ids).rowcount
            conn.close()
        elif kind in GALLERY_TABLES:
            if kind == "image" and action == "ready":
                skipped += len(ids)
                continue
            try:
                n = _bulk_apply(GALLERY_TABLES[kind], action, ids)
            except ValueError as exc:
                return jsonify({"error": str(exc)}), 400
            affected += n
            skipped += len(ids) - n if action != "delete" else 0
        else:
            skipped += len(ids)
    return jsonify({"affected": affected, "skipped": skipped})
