"""Meta sync, Asset IDs and results."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from flask import Blueprint, jsonify, request

from creative_intelligence.webapp import common

bp = Blueprint("results", __name__)
log = logging.getLogger("adstudio")
_HERE = Path(__file__).parent.parent


@bp.post("/api/meta/sync")
def meta_sync_start() -> Any:
    """Pull fresh results from Meta (read-only GETs) and rebuild winning patterns."""
    from creative_intelligence.ingest.meta_sync import start_sync
    run_id, reason = start_sync(common.db)
    if run_id is None:
        return jsonify({"error": reason}), 409
    return jsonify({"run_id": run_id})


@bp.post("/admin/meta-sync")
def admin_meta_sync() -> Any:
    """Same sync, for an external cron. Needs ADMIN_TOKEN in the X-Admin-Token header."""
    from creative_intelligence.ingest.meta_sync import start_sync
    if not common.admin_token_ok():
        return jsonify({"error": "unauthorized"}), 401
    run_id, reason = start_sync(common.db, trigger="cron")
    if run_id is None:
        return jsonify({"error": reason}), 409
    return jsonify({"run_id": run_id})


# ── Assets: IDs, Meta links, results (T2) ───────────────────────────────────
@bp.get("/api/adstudio/asset/<asset_id>")
def adstudio_asset_detail(asset_id: str) -> Any:
    from creative_intelligence.adstudio import assets
    conn = common.db()
    r = conn.execute("SELECT * FROM adstudio_assets WHERE asset_id=?", (asset_id,)).fetchone()
    if r is None:
        conn.close(); return jsonify({"error": "not found"}), 404
    a = dict(r)
    a["results"] = assets.results(conn, a["meta_ad_id"]) if a["meta_ad_id"] else None
    if a["results"]:
        from creative_intelligence.adstudio import diagnosis
        length = conn.execute("SELECT duration_s FROM adstudio_video_renders WHERE id=?", (a["source_id"],)).fetchone() \
            if a["source"] == "video_render" else None
        a["results"]["diagnosis"] = diagnosis.hint(a["results"], length[0] if length else None)
    if a["meta_ad_id"]:
        m = conn.execute("SELECT ad_name FROM ad_daily_insights WHERE ad_id=? ORDER BY date DESC LIMIT 1",
                         (a["meta_ad_id"],)).fetchone() or conn.execute(
            "SELECT ad_name FROM creatives WHERE ad_id=? LIMIT 1", (a["meta_ad_id"],)).fetchone()
        a["meta_ad_name"] = m["ad_name"] if m else None
    conn.close()
    return jsonify(a)


@bp.post("/api/adstudio/asset/<asset_id>/link")
def adstudio_asset_link(asset_id: str) -> Any:
    """Body: {meta_ad_id} to link by hand, or {meta_ad_id: null} to unlink. Read-only on Meta."""
    import re as _re
    from creative_intelligence.adstudio import assets
    d = request.get_json(force=True, silent=True) or {}
    meta = str(d.get("meta_ad_id") or "").strip() or None
    if meta and not _re.fullmatch(r"\d{5,25}", meta):
        return jsonify({"error": "A Meta ad ID is a long number, e.g. 120212345678901234."}), 400
    conn = common.db()
    ok = assets.link_manual(conn, asset_id, meta)
    conn.close()
    return (jsonify({"ok": True}) if ok else (jsonify({"error": "not found"}), 404))


@bp.get("/api/adstudio/meta-ads")
def adstudio_meta_ads_search() -> Any:
    """Synced Meta ads matching ?q= (name or ID), for the manual link picker."""
    q = (request.args.get("q") or "").strip()
    conn = common.db()
    rows = [dict(r) for r in conn.execute(
        "SELECT ad_id, ad_name, MAX(date) AS last_day, ROUND(SUM(spend),2) AS spend FROM ad_daily_insights"
        " WHERE ad_name LIKE ? OR ad_id LIKE ? GROUP BY ad_id ORDER BY last_day DESC, spend DESC LIMIT 20",
        (f"%{q}%", f"%{q}%")).fetchall()]
    conn.close()
    return jsonify({"ads": rows})


@bp.get("/api/adstudio/results")
def adstudio_results() -> Any:
    """?group=angle|hook_type|format|style|product [&product_id] — results of linked ads, grouped."""
    from creative_intelligence.adstudio import assets, costs
    group = request.args.get("group", "angle")
    pid = request.args.get("product_id")
    conn = common.db()
    assets.ensure_assets(conn)
    try:
        rows = assets.aggregate(conn, group, int(pid) if pid else None)
    except ValueError as exc:
        conn.close(); return jsonify({"error": str(exc)}), 400
    totals = conn.execute("SELECT COUNT(*) AS assets, COUNT(meta_ad_id) AS linked FROM adstudio_assets"
                          + (" WHERE product_id=?" if pid else ""), ([int(pid)] if pid else [])).fetchone()
    out = {"group": group, "rows": rows, "assets": totals["assets"], "linked": totals["linked"],
           "cost": costs.summary(conn)}
    conn.close()
    return jsonify(out)


@bp.get("/api/meta/sync")
def meta_sync_status() -> Any:
    from creative_intelligence.ingest.meta_sync import latest_run
    conn = common.db()
    run = latest_run(conn)
    conn.close()
    return jsonify({"run": run})


# ── Phase 2a research (brief R) ─────────────────────────────────────────────
@bp.get("/api/adstudio/research")
def research_overview() -> Any:
    from creative_intelligence import budget
    from creative_intelligence.adstudio import research
    conn = common.db()
    out = {"ads": research.listing(conn), "patterns": research.patterns(conn), "brief": research.latest_brief(conn),
           "spent_month": budget.research_spent_this_month(conn), "cap_month": budget.caps(conn)["research_month"]}
    conn.close()
    return jsonify(out)


@bp.post("/api/adstudio/research/ads")
def research_add_ad() -> Any:
    """Body {page_name, body, headline?, url?, seen_on?, notes?}: the manual "Add ad" (no scraper)."""
    from creative_intelligence.adstudio import research
    conn = common.db()
    rid, why = research.add_manual(conn, request.get_json(force=True, silent=True) or {})
    conn.close()
    return (jsonify({"error": why}), 400) if rid is None else jsonify({"id": rid})


@bp.post("/api/adstudio/research/library")
def research_library() -> Any:
    """Body {search_terms, countries?}: read-only Meta Ad Library search (EU/UK delivery)."""
    from creative_intelligence.adstudio import research
    d = request.get_json(force=True, silent=True) or {}
    conn = common.db()
    out = research.fetch_library(conn, d.get("search_terms", ""), d.get("countries") or None)
    conn.close()
    return (jsonify(out), 400) if out.get("error") else jsonify(out)


@bp.post("/api/adstudio/research/run")
def research_run() -> Any:
    """Queue tagging plus a new research brief now (research cap applies)."""
    from creative_intelligence import jobs
    conn = common.db()
    jid = jobs.enqueue(conn, "research_brief", {"trigger": "manual"}, dedupe_key="research_brief")
    conn.close()
    return jsonify({"job_id": jid})


@bp.post("/api/adstudio/research/comments")
def research_comments() -> Any:
    """Import real comments from the brand's Page (read-only; needs the Page token, decision j)."""
    from creative_intelligence.adstudio import research
    conn = common.db()
    out = research.import_page_comments(conn)
    conn.close()
    return (jsonify(out), 400) if out.get("error") else jsonify(out)
