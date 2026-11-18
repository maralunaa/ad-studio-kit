"""Compose: finished ads drawn by code from an image and a hook."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from flask import Blueprint, jsonify, request, send_file

from creative_intelligence.webapp import common
from creative_intelligence.webapp.common import _REVIEW_SQL, _asset_ids, _attach_assets, _bulk_apply, _claims, _log_reviews, _product_dict, _qa_view

bp = Blueprint("compose", __name__)
log = logging.getLogger("adstudio")
_HERE = Path(__file__).parent.parent


def _resolve_copy_lists(conn, product_id: int, layout: dict[str, Any]) -> None:
    """Lo-fi and review layouts (brief S3). The brand's own lines come as `items` (short, claims-checked like any copy);
    quotes only ever come from the real reviews library by id (`review_ids`), resolved here with author, source
    and date. Typed-in review text is never drawn."""
    from creative_intelligence.adstudio import reviews
    from creative_intelligence.adstudio.templates import template_for
    els = template_for(layout.get("template") or "").get("elements", []) if layout.get("template") else []
    fields = {e.get("field") for e in els}
    layout.pop("review", None), layout.pop("author", None), layout.pop("rating", None)
    if "items" in fields:
        items = [str(x).strip()[:90] for x in (layout.get("items") or []) if str(x).strip()][:6]
        if not items:
            raise ValueError("Add a few short lines for this layout (one per line).")
        layout["items"] = items
    want = next((e.get("kind") for e in els if e.get("type") == "cards"), None)
    if "reviews" in fields or "review" in fields:
        ids = [int(i) for i in (layout.get("review_ids") or []) if str(i).isdigit()]
        picked = reviews.resolve(conn, ids, product_id, want)
        if not picked:
            raise ValueError("Pick at least one real " + ("comment" if want == "comment" else "review")
                             + " from the library (Admin › Real reviews and comments).")
        layout["review_ids"] = [r["id"] for r in picked]
        if "reviews" in fields:
            layout["reviews"] = picked
        else:                                            # the single review card (Social Proof)
            first = picked[0]
            layout.update(review=first["text"], author=f"{first['author']} · {first['source']}", rating=first["rating"])


def _resolve_price(conn, product_id: int, layout: dict[str, Any]) -> dict[str, Any]:
    """If the layout shows a price, read it live from Shopify (never from Claude).

    Sets layout['price'] and layout['price_source']; falls back to the price stored at
    import (marked as such) if Shopify can't be reached. No Shopify product -> no price.
    """
    if not layout.get("show_price"):
        layout.pop("price", None)
        return layout
    from creative_intelligence.adstudio import core as C
    prod = _product_dict(conn, product_id) or {}
    url, want = prod.get("shopify_url") or "", layout.get("price_variant") or ""
    if not url:
        raise ValueError("This product has no Shopify link, so there is no price to show.")
    try:
        variants = C.shopify_prices(url)
        v = next((x for x in variants if x["title"] == want), None) or \
            next((x for x in variants if x["available"]), None) or variants[0]
        layout.update(price=v["price"], price_variant=v["title"], price_source="shopify live")
    except Exception:
        rows = json.loads(prod.get("info_json") or "{}").get("rows", [])
        r = next((x for x in rows if x.get("variant_title") == want and x.get("price")), None) or \
            next((x for x in rows if x.get("price")), None)
        if not r:
            raise ValueError("Couldn't read the price from Shopify, and none was stored at import.")
        layout.update(price=r["price"], price_variant=r.get("variant_title", ""), price_source="shopify at import")
    return layout


@bp.post("/api/adstudio/composite/<int:comp_id>/review")
def adstudio_review_composite(comp_id: int) -> Any:
    """
    Body: {action: approve|reject|ready|clear, notes?}. approve / reject /
    ready toggle: repeating the current state clears it (as in Render).
    """
    from datetime import datetime as _dt
    d = request.get_json(force=True, silent=True) or {}
    action = d.get("action", "")
    conn = common.db()
    row = conn.execute("SELECT review_status, is_ready FROM adstudio_composites WHERE id=?",
                       (comp_id,)).fetchone()
    if row is None:
        conn.close(); return jsonify({"error": "not found"}), 404
    if action == "approve" and row["review_status"] == "approved" and not row["is_ready"]:
        action = "clear"
    elif action == "reject" and row["review_status"] == "rejected":
        action = "clear"
    elif action == "ready" and row["is_ready"]:
        action = "unready"
    if action and action not in _REVIEW_SQL:
        conn.close(); return jsonify({"error": f"bad action {action!r}"}), 400
    if action:
        _log_reviews(conn, "adstudio_composites", [comp_id], action)
    with conn:
        if action:
            conn.execute(f"UPDATE adstudio_composites SET {_REVIEW_SQL[action]}, reviewed_at=?"
                         " WHERE id=?", (_dt.utcnow().isoformat(), comp_id))
        if "notes" in d:
            conn.execute("UPDATE adstudio_composites SET review_notes=? WHERE id=?",
                         ((d.get("notes") or "").strip(), comp_id))
    r = dict(conn.execute("SELECT id, review_status, is_ready, review_notes FROM adstudio_composites"
                          " WHERE id=?", (comp_id,)).fetchone())
    conn.close()
    return jsonify(r)


@bp.post("/api/adstudio/composites/bulk")
def adstudio_composites_bulk() -> Any:
    d = request.get_json(force=True, silent=True) or {}
    try:
        n = _bulk_apply("adstudio_composites", d.get("action", ""), d.get("ids", []))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"affected": n})


# ── Compose: draw copy onto a chosen text-free image ─────────────────────
def _core_as_image(conn, core_id: int) -> int | None:
    """
    The adstudio_images id standing in for a finished Core shot, created on
    first use, so Compose / Gallery / exports treat it like any base image.
    Mirror rows are hidden from the Images tab (core_id IS NOT NULL).
    """
    from creative_intelligence.adstudio.core import CORE_KINDS
    hit = conn.execute("SELECT id FROM adstudio_images WHERE core_id=?", (core_id,)).fetchone()
    if hit:
        return hit["id"]
    c = conn.execute("SELECT * FROM adstudio_core WHERE id=? AND image_path IS NOT NULL",
                     (core_id,)).fetchone()
    if c is None or c["kind"] not in CORE_KINDS:
        return None
    with conn:
        cur = conn.execute(
            "INSERT INTO adstudio_images (product_id, archetype, title, concept, prompt,"
            " aspect_ratio, model, status, image_path, core_id)"
            " VALUES (?, 'core', ?, '', '', ?, 'core', 'done', ?, ?)",
            (c["product_id"], f"{CORE_KINDS[c['kind']]} #{core_id}", c["aspect_ratio"],
             c["image_path"], core_id))
        conn.execute("UPDATE adstudio_images SET prompt_version=? WHERE id=?", (c["prompt_version"], cur.lastrowid))
    return cur.lastrowid


@bp.post("/api/adstudio/compose")
def adstudio_compose() -> Any:
    """Body: {image_id | core_id, headline, subhead, cta, hook_id?, layout}."""
    from creative_intelligence.adstudio.overlay import compose
    from creative_intelligence.adstudio.store import composites_dir
    import json as _json
    d = request.get_json(force=True, silent=True) or {}
    conn = common.db()
    image_id = int(d.get("image_id") or 0)
    if d.get("core_id"):
        image_id = _core_as_image(conn, int(d["core_id"])) or 0
    img = conn.execute("SELECT * FROM adstudio_images WHERE id=?", (image_id,)).fetchone()
    if img is None or not img["image_path"] or not Path(img["image_path"]).exists():
        conn.close(); return jsonify({"error": "base image not found"}), 404
    img = dict(img)

    # copy: explicit fields override; else pull from a hook
    headline = d.get("headline", ""); subhead = d.get("subhead", ""); cta = d.get("cta", "")
    hook_id = d.get("hook_id")
    if hook_id and not (headline or subhead or cta):
        h = conn.execute("SELECT headline, hook_text, subhead, cta FROM adstudio_hooks WHERE id=?",
                         (hook_id,)).fetchone()
        if h:
            headline = h["headline"] or h["hook_text"] or ""
            subhead = h["subhead"] or ""; cta = h["cta"] or ""
    layout = d.get("layout") or {}
    try:
        _resolve_price(conn, img["product_id"], layout)
        _resolve_copy_lists(conn, img["product_id"], layout)
    except ValueError as exc:
        conn.close(); return jsonify({"error": str(exc)}), 400

    with conn:
        cur = conn.execute(
            "INSERT INTO adstudio_composites (product_id, image_id, hook_id, headline,"
            " subhead, cta, layout_json) VALUES (?,?,?,?,?,?,?)",
            (img["product_id"], image_id, hook_id, headline, subhead, cta, _json.dumps(layout)))
    comp_id = cur.lastrowid
    try:
        out = str(composites_dir() / f"comp{comp_id}.png")
        metrics: dict[str, Any] = {}
        compose(img["image_path"], out, headline=headline, subhead=subhead, cta=cta, layout=layout, metrics=metrics)
        with conn:
            conn.execute("UPDATE adstudio_composites SET output_path=? WHERE id=?", (out, comp_id))
        from creative_intelligence.adstudio import qa
        qa.inherit(conn, "composite", comp_id, "image", image_id, metrics)
        _claims(conn, "composite", comp_id, img["product_id"], {"headline": headline, "subhead": subhead, "cta": cta,
                                                                "items": " · ".join(layout.get("items") or [])})
        conn.close()
        return jsonify({"id": comp_id})
    except Exception as exc:
        log.exception("compose failed")
        conn.close()
        return jsonify({"error": str(exc)[:300]}), 500


@bp.post("/api/adstudio/compose-bulk")
def adstudio_compose_bulk() -> Any:
    """Compose copy onto many images at once.

    Body: {product_id, layout, image_ids?(default: all done images),
           core_ids?[Core shots to include], copies?[{headline,subhead,cta,hook_id}]
           OR hook_ids?[...] OR single headline/subhead/cta}. Every copy is
           drawn onto every image.
    """
    from creative_intelligence.adstudio.overlay import compose
    from creative_intelligence.adstudio.store import composites_dir
    import json as _json
    d = request.get_json(force=True, silent=True) or {}
    pid = int(d.get("product_id", 0))
    layout = d.get("layout") or {}
    conn = common.db()

    core_img_ids = [i for i in (_core_as_image(conn, int(c)) for c in d.get("core_ids") or []) if i]
    if d.get("image_ids") or core_img_ids:
        ids = [int(x) for x in d.get("image_ids") or []] + core_img_ids
        q = ("SELECT id, image_path FROM adstudio_images WHERE image_path IS NOT NULL"
             " AND id IN (%s)" % ",".join("?" * len(ids)))
        imgs = [dict(r) for r in conn.execute(q, ids).fetchall()]
    else:
        imgs = [dict(r) for r in conn.execute(
            "SELECT id, image_path FROM adstudio_images WHERE product_id=?"
            " AND image_path IS NOT NULL AND core_id IS NULL", (pid,)).fetchall()]

    copies = d.get("copies")
    if not copies:
        if d.get("hook_ids"):
            hooks = {r["id"]: dict(r) for r in conn.execute(
                "SELECT * FROM adstudio_hooks WHERE product_id=?", (pid,)).fetchall()}
            copies = [{
                "hook_id": hid,
                "headline": hooks[hid].get("headline") or hooks[hid].get("hook_text") or "",
                "subhead": hooks[hid].get("subhead", ""),
                "cta": hooks[hid].get("cta", ""),
            } for hid in d["hook_ids"] if hid in hooks]
        else:
            copies = [{"headline": d.get("headline", ""), "subhead": d.get("subhead", ""),
                       "cta": d.get("cta", "")}]

    total = len(imgs) * len(copies)
    if total == 0:
        conn.close(); return jsonify({"error": "nothing to compose"}), 400
    if total > 300:
        conn.close(); return jsonify({"error": f"{total} composites is too many at once (max 300)"}), 400
    try:
        _resolve_price(conn, int(d.get("product_id") or 0), layout)
        _resolve_copy_lists(conn, int(d.get("product_id") or 0), layout)     # quotes only from the library
    except ValueError as exc:
        conn.close(); return jsonify({"error": str(exc)}), 400

    made = 0
    for im in imgs:
        if not Path(im["image_path"]).exists():
            continue
        for cp in copies:
            with conn:
                cur = conn.execute(
                    "INSERT INTO adstudio_composites (product_id, image_id, hook_id,"
                    " headline, subhead, cta, layout_json) VALUES (?,?,?,?,?,?,?)",
                    (pid, im["id"], cp.get("hook_id"), cp.get("headline", ""),
                     cp.get("subhead", ""), cp.get("cta", ""), _json.dumps(layout)))
            cid = cur.lastrowid
            out = str(composites_dir() / f"comp{cid}.png")
            try:
                metrics: dict[str, Any] = {}
                compose(im["image_path"], out, headline=cp.get("headline", ""),
                        subhead=cp.get("subhead", ""), cta=cp.get("cta", ""), layout=layout, metrics=metrics)
                with conn:
                    conn.execute("UPDATE adstudio_composites SET output_path=? WHERE id=?",
                                 (out, cid))
                from creative_intelligence.adstudio import qa
                qa.inherit(conn, "composite", cid, "image", im["id"], metrics)
                _claims(conn, "composite", cid, pid, {k: cp.get(k, "") for k in ("headline", "subhead", "cta")})
                made += 1
            except Exception:
                log.exception("bulk compose item failed")
    conn.close()
    return jsonify({"made": made, "total": total})


@bp.get("/api/adstudio/composites")
def adstudio_list_composites() -> Any:
    pid = request.args.get("product_id")
    include_archived = request.args.get("include_archived") == "1"
    conn = common.db()
    review = request.args.get("review", "")
    sql = ("SELECT c.id, c.product_id, c.image_id, c.hook_id, c.headline, c.subhead, c.cta,"
           " c.is_favorite, c.archived, c.created_at, c.review_status, c.is_ready, c.review_notes,"
           " c.output_path IS NOT NULL AS has_image, h.source AS hook_source,"
           " h.archetype AS hook_archetype, h.pattern_id, i.core_id"
           " FROM adstudio_composites c LEFT JOIN adstudio_hooks h ON h.id = c.hook_id"
           " LEFT JOIN adstudio_images i ON i.id = c.image_id WHERE 1=1")
    params: list[Any] = []
    if pid:
        sql += " AND c.product_id=?"; params.append(int(pid))
    if not include_archived:
        sql += " AND c.archived=0"
    review_where = {"unreviewed": " AND COALESCE(c.review_status,'')=''",
                    "approved": " AND c.review_status='approved'",
                    "ready": " AND c.is_ready=1",
                    "rejected": " AND c.review_status='rejected'"}
    sql += review_where.get(review, "")
    sql += " ORDER BY c.id DESC LIMIT 400"
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    _attach_assets(conn, "composite", rows, int(pid) if pid else None)
    rows, n_failed = _qa_view(conn, "composite", rows, request.args.get("review", ""), request.args.get("sort", ""))
    from creative_intelligence.adstudio.facts import checks_for
    ck = checks_for(conn, "composite", [r["id"] for r in rows])
    for r in rows:
        r["claims_check"] = ck.get(r["id"])
    csql = ("SELECT COALESCE(review_status,'') AS s, is_ready FROM adstudio_composites"
            " WHERE (? IS NULL OR product_id=?)" + ("" if include_archived else " AND archived=0"))
    counts = {"all": 0, "unreviewed": 0, "approved": 0, "ready": 0, "rejected": 0}
    for r in conn.execute(csql, (pid, int(pid) if pid else None)).fetchall():
        counts["all"] += 1
        counts[r["s"] or "unreviewed"] = counts.get(r["s"] or "unreviewed", 0) + 1
        counts["ready"] += 1 if r["is_ready"] else 0
    counts["qa_rejected"] = n_failed
    conn.close()
    return jsonify({"composites": rows, "counts": counts})


@bp.get("/api/adstudio/composite/<int:comp_id>/image")
def adstudio_composite_image(comp_id: int) -> Any:
    conn = common.db()
    row = conn.execute("SELECT output_path FROM adstudio_composites WHERE id=?", (comp_id,)).fetchone()
    conn.close()
    if row is None or not row["output_path"] or not Path(row["output_path"]).exists():
        return ("", 404)
    return send_file(row["output_path"], mimetype="image/png", conditional=True)


@bp.post("/api/adstudio/composite/<int:comp_id>/favorite")
def adstudio_fav_composite(comp_id: int) -> Any:
    conn = common.db()
    row = conn.execute("SELECT is_favorite FROM adstudio_composites WHERE id=?", (comp_id,)).fetchone()
    if row is None:
        conn.close(); return jsonify({"error": "not found"}), 404
    nv = 0 if row["is_favorite"] else 1
    with conn:
        conn.execute("UPDATE adstudio_composites SET is_favorite=? WHERE id=?", (nv, comp_id))
    conn.close()
    return jsonify({"id": comp_id, "is_favorite": nv})


@bp.post("/api/adstudio/composite/<int:comp_id>/delete")
def adstudio_del_composite(comp_id: int) -> Any:
    conn = common.db()
    with conn:
        conn.execute("DELETE FROM adstudio_composites WHERE id=?", (comp_id,))
    conn.close()
    return jsonify({"ok": True})


@bp.get("/api/adstudio/export-composites")
def adstudio_export_composites() -> Any:
    """
    Zip a product's finished ads. ?ready=1 → launch kit: only ads marked
    ready, plus manifest.csv (file, copy, primary text, pattern, notes) for
    uploading to Ads Manager.
    """
    import csv, io, re as _re, zipfile
    pid = request.args.get("product_id")
    ready = request.args.get("ready") == "1"
    conn = common.db()
    rows = [dict(r) for r in conn.execute(
        "SELECT c.id, c.output_path, c.headline, c.subhead, c.cta, c.review_notes,"
        " h.body, h.hook_text, h.archetype, h.source, i.core_id"
        " FROM adstudio_composites c LEFT JOIN adstudio_hooks h ON h.id = c.hook_id"
        " LEFT JOIN adstudio_images i ON i.id = c.image_id"
        " WHERE c.product_id=? AND c.output_path IS NOT NULL"
        + (" AND c.is_ready=1" if ready else "") + " ORDER BY c.id",
        (int(pid),)).fetchall()] if pid else []
    prod = _product_dict(conn, int(pid)) if pid else None
    aid = _asset_ids(conn, "composite", [r["id"] for r in rows])
    conn.close()
    slug = _re.sub(r"[^a-z0-9]+", "-", ((prod or {}).get("name") or "product").lower()).strip("-")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        manifest = io.StringIO()
        w = csv.writer(manifest)
        w.writerow(["file", "meta_ad_name", "headline", "subhead", "cta", "primary_text", "hook",
                    "pattern", "base", "ai_generated", "notes"])
        for r in rows:
            if not Path(r["output_path"]).exists():
                continue
            ad_name = aid.get(r["id"]) or f"{slug}_ad_{r['id']}"
            name = f"{ad_name}.png"
            z.write(r["output_path"], arcname=name)
            pattern = r["archetype"] if (r["source"] or "").startswith("pattern") else ""
            w.writerow([name, ad_name, r["headline"], r["subhead"], r["cta"], r["body"] or "",
                        r["hook_text"] or "", pattern, "core shot" if r["core_id"] else "image",
                        "yes", r["review_notes"] or ""])
        if ready:
            z.writestr("manifest.csv", manifest.getvalue())
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"{slug}_{'launch_kit' if ready else 'ads'}.zip")


# ── Compose → Bulk: approved images × ad types × hooks × sizes, made in the background ──
@bp.get("/api/adstudio/product/<int:pid>/bulk")
def adstudio_bulk_options(pid: int) -> Any:
    from creative_intelligence.adstudio import batch
    conn = common.db()
    out = {**batch.options(conn, pid), "batches": batch.recent(conn, pid)}
    conn.close()
    return jsonify(out)


@bp.post("/api/adstudio/product/<int:pid>/hooks/auto")
def adstudio_auto_hooks(pid: int) -> Any:
    """Bulk with no usable hooks (feedback 2026-10-26: every hook was flagged, Make ads did nothing): write Hook
    University hooks now, claims-checked like hooks saved by hand."""
    from creative_intelligence import budget
    from creative_intelligence.adstudio import hub
    conn = common.db()
    try:
        prod = _product_dict(conn, pid)
        if prod is None:
            return jsonify({"error": "product not found"}), 404
        try:
            return jsonify({"written": hub.auto_hooks(conn, prod)})
        except budget.BudgetExceeded as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            log.exception("auto hooks for product %s", pid)
            return jsonify({"error": f"Couldn't write hooks: {str(exc)[:160]}"}), 500
    finally:
        conn.close()


@bp.post("/api/adstudio/product/<int:pid>/bulk/estimate")
def adstudio_bulk_estimate(pid: int) -> Any:
    from creative_intelligence.adstudio import batch
    conn = common.db()
    out = batch.preview(conn, pid, request.get_json(force=True, silent=True) or {})
    conn.close()
    return jsonify(out), 400 if "error" in out else 200


@bp.post("/api/adstudio/product/<int:pid>/bulk")
def adstudio_bulk_start(pid: int) -> Any:
    from creative_intelligence.adstudio import batch
    conn = common.db()
    bid, why = batch.create(conn, pid, request.get_json(force=True, silent=True) or {})
    out = batch.status(conn, bid) if bid else None
    conn.close()
    return (jsonify({"batch": out}), 200) if bid else (jsonify({"error": why}), 400)


@bp.post("/api/adstudio/bulk/<int:bid>/cancel")
def adstudio_bulk_cancel(bid: int) -> Any:
    from creative_intelligence.adstudio import batch
    conn = common.db()
    ok = batch.cancel(conn, bid)
    out = batch.status(conn, bid)
    conn.close()
    return (jsonify({"batch": out}), 200) if ok else (jsonify({"error": "This batch isn't running."}), 400)


@bp.get("/api/adstudio/bulk/<int:bid>")
def adstudio_bulk_status(bid: int) -> Any:
    from creative_intelligence.adstudio import batch
    conn = common.db()
    out = batch.status(conn, bid)
    conn.close()
    return (jsonify({"batch": out}), 200) if out else (jsonify({"error": "not found"}), 404)
