"""Ad Pack (Part C): the request form, its sentence reader and templates, and the saved packs. adstudio/adpack.py."""
from __future__ import annotations

import logging
from typing import Any

from flask import Blueprint, jsonify, request

from creative_intelligence.webapp import common

bp = Blueprint("adpack", __name__)
log = logging.getLogger("adstudio")


@bp.get("/api/adstudio/adpack/options")
def options() -> Any:
    """What the form offers: products, video styles (in order, proven first), sizes, approved ideas, defaults,
    templates and the saved packs."""
    from creative_intelligence.adstudio import adpack
    conn = common.db()
    try:
        return jsonify({"products": [dict(r) for r in conn.execute(
                            "SELECT id, name FROM adstudio_products WHERE COALESCE(archived,0)=0 ORDER BY name")],
                        "video_styles": list(adpack.video_styles().items()), "sizes": adpack.SIZES, "default": adpack.normalize({}),
                        "ideas": [dict(r) for r in conn.execute(
                            "SELECT id, title FROM ideas WHERE status='approved' ORDER BY id DESC LIMIT 50")],
                        "templates": adpack.templates(conn), "packs": adpack.listing(conn),
                        "limits": {"statics": adpack.MAX_STATICS, "videos": adpack.MAX_VIDEOS},
                        **adpack.cast_options(conn)})
    finally:
        conn.close()


@bp.post("/api/adstudio/adpack/parse")
def parse() -> Any:
    from creative_intelligence.adstudio import adpack
    conn = common.db()
    try:
        return jsonify(adpack.parse(conn, (request.get_json(silent=True) or {}).get("text", "")))
    except Exception as exc:                            # noqa: BLE001 — the form still works without the reader
        log.warning("adpack parse failed: %s", exc)
        return jsonify({"error": "Couldn't read that just now: fill the form instead."})
    finally:
        conn.close()


@bp.post("/api/adstudio/adpack/templates")
def save_template() -> Any:
    from creative_intelligence.adstudio import adpack
    b = request.get_json(silent=True) or {}
    conn = common.db()
    try:
        err = adpack.save_template(conn, b.get("name", ""), b.get("request") or {})
        return jsonify({"error": err} if err else {"ok": True, "templates": adpack.templates(conn)})
    finally:
        conn.close()


@bp.post("/api/adstudio/adpack")
def create() -> Any:
    from creative_intelligence.adstudio import adpack
    conn = common.db()
    try:
        pid, err = adpack.create(conn, (request.get_json(silent=True) or {}).get("request") or {})
        return jsonify({"error": err} if err else {"id": pid})
    finally:
        conn.close()


@bp.get("/api/adstudio/adpack/<int:pack_id>")
def get_pack(pack_id: int) -> Any:
    from creative_intelligence.adstudio import adpack
    conn = common.db()
    try:
        p = adpack.get(conn, pack_id)
        return (jsonify(p), 200) if p else (jsonify({"error": "not found"}), 404)
    finally:
        conn.close()


@bp.post("/api/adstudio/adpack/<int:pack_id>/plan")
def make_plan(pack_id: int) -> Any:
    """Work out (again) what the pack makes and costs. Nothing is generated."""
    from creative_intelligence.adstudio import adpack
    conn = common.db()
    try:
        out = adpack.plan(conn, pack_id)
        return (jsonify(out), 404) if out.get("error") else jsonify(out)
    finally:
        conn.close()


@bp.post("/api/adstudio/adpack/<int:pack_id>/produce")
def produce(pack_id: int) -> Any:
    """Approve the plan: start the Bulk and video batches it lists."""
    from creative_intelligence.adstudio import adpack
    conn = common.db()
    try:
        out = adpack.produce(conn, pack_id)
        return (jsonify(out), 400) if out.get("error") else jsonify(out)
    finally:
        conn.close()


@bp.get("/api/adstudio/adpack/<int:pack_id>/progress")
def progress(pack_id: int) -> Any:
    from creative_intelligence.adstudio import adpack
    conn = common.db()
    try:
        p = adpack.get(conn, pack_id)
        return jsonify({**p, "progress": adpack.progress(conn, p)}) if p else (jsonify({"error": "not found"}), 404)
    finally:
        conn.close()
