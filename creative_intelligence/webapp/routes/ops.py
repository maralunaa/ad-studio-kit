"""Admin data: costs, prices, QA overrules, facts library, knowledge base."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from flask import Blueprint, jsonify, request

from creative_intelligence.webapp import common

bp = Blueprint("ops", __name__)
log = logging.getLogger("adstudio")
_HERE = Path(__file__).parent.parent


@bp.get("/api/ops/qa")
def ops_qa() -> Any:
    """Overrule report and the current rubric (Admin page)."""
    from creative_intelligence.adstudio import qa
    conn = common.db()
    out = {"overrules": qa.overrule_report(conn), "rubric": qa.load_rubric(),
           "scored": conn.execute("SELECT COUNT(*) AS n, SUM(passed) AS p, ROUND(SUM(cost_usd),2) AS c"
                                  " FROM qa_scores").fetchone()[0:3]}
    conn.close()
    out["scored"] = dict(zip(("scored", "passed", "cost_usd"), out["scored"]))
    return jsonify(out)


# ── Knowledge base (K3) — the playbook, its derived files, and updates from a new export ───────────────
@bp.get("/api/ops/knowledge")
def ops_knowledge() -> Any:
    from creative_intelligence.adstudio import knowledge_updates as KU
    conn = common.db()
    out = {**KU.overview(), "updates": KU.listing(conn)}
    conn.close()
    return jsonify(out)


@bp.post("/api/ops/knowledge/upload")
def ops_knowledge_upload() -> Any:
    """Body {markdown} (or a file 'export'): store the new playbook export, never live; diff it; queue proposals."""
    from creative_intelligence.adstudio import knowledge_updates as KU
    f = request.files.get("export")
    md = f.read().decode("utf-8", "replace") if f else (request.get_json(force=True, silent=True) or {}).get("markdown", "")
    conn = common.db()
    try:
        uid = KU.upload(conn, md)
    except ValueError as exc:
        conn.close()
        return jsonify({"error": str(exc)}), 400
    out = KU.get(conn, uid)
    conn.close()
    return jsonify(out)


@bp.post("/api/ops/knowledge/<int:uid>/decide")
def ops_knowledge_decide(uid: int) -> Any:
    """Body {items: [index…], approve: bool}."""
    from creative_intelligence.adstudio import knowledge_updates as KU
    d = request.get_json(force=True, silent=True) or {}
    conn = common.db()
    out = KU.decide(conn, uid, [int(i) for i in d.get("items") or []], bool(d.get("approve")))
    conn.close()
    return (jsonify(out), 400) if out.get("error") else jsonify(out)


@bp.post("/api/ops/knowledge/override/remove")
def ops_knowledge_remove() -> Any:
    """Body {key}: undo an approved value; the repo's value applies again."""
    from creative_intelligence.adstudio import knowledge_updates as KU
    key = (request.get_json(force=True, silent=True) or {}).get("key", "")
    return jsonify({"ok": KU.remove_override(key)})


@bp.get("/api/ops/knowledge/<int:uid>/export")
def ops_knowledge_export(uid: int) -> Any:
    from creative_intelligence.adstudio import knowledge_updates as KU
    conn = common.db()
    out = KU.export(conn, uid)
    conn.close()
    if out is None:
        return jsonify({"error": "not found"}), 404
    resp = jsonify(out)
    resp.headers["Content-Disposition"] = f"attachment; filename=knowledge-update-{uid}.json"
    return resp


# ── Real reviews and comments (S3) — the only source of quotes in ads ─────────────────
@bp.get("/api/ops/reviews")
def ops_reviews() -> Any:
    from creative_intelligence.adstudio import reviews
    pid = request.args.get("product_id")
    conn = common.db()
    out = {"reviews": reviews.listing(conn, int(pid) if pid else None, usable=False), "sources": reviews.SOURCES,
           "products": [dict(r) for r in conn.execute("SELECT id, name FROM adstudio_products ORDER BY name")]}
    conn.close()
    return jsonify(out)


@bp.post("/api/ops/reviews")
def ops_reviews_edit() -> Any:
    """Body: a new review {kind, text, author, rating?, source, source_url?, posted_on, product_id?} or {id, archive: true}."""
    from creative_intelligence.adstudio import reviews
    d = request.get_json(force=True, silent=True) or {}
    conn = common.db()
    if d.get("archive"):
        reviews.archive(conn, int(d.get("id") or 0))
        conn.close()
        return ops_reviews()
    rid, why = reviews.add(conn, d)
    conn.close()
    return (jsonify({"error": why}), 400) if rid is None else ops_reviews()


# ── Facts and claims library (T6) — edited on the Admin page ─────────────────
@bp.get("/api/ops/facts")
def ops_facts() -> Any:
    from creative_intelligence.adstudio.facts import CATEGORIES
    conn = common.db()
    out = {"categories": CATEGORIES,
           "facts": [dict(r) for r in conn.execute("SELECT * FROM facts WHERE status='approved' AND family='' ORDER BY product_id IS NOT NULL, product_id, category, id")],
           "banned": [dict(r) for r in conn.execute("SELECT * FROM banned_terms ORDER BY term")],
           "products": [dict(r) for r in conn.execute("SELECT id, name FROM adstudio_products ORDER BY name")]}
    conn.close()
    return jsonify(out)


@bp.post("/api/ops/facts")
def ops_facts_save() -> Any:
    """Body: {kind: fact|banned, id?, delete?, product_id?, category?, text?, source?, term?, reason?}."""
    from creative_intelligence.adstudio.facts import CATEGORIES
    d = request.get_json(force=True, silent=True) or {}
    kind, fid = d.get("kind"), d.get("id")
    table = {"fact": "facts", "banned": "banned_terms"}.get(kind)
    if not table:
        return jsonify({"error": "kind must be fact or banned"}), 400
    pid = int(d["product_id"]) if str(d.get("product_id") or "").isdigit() else None
    text, term = (d.get("text") or "").strip(), (d.get("term") or "").strip()
    cat = d.get("category") if d.get("category") in CATEGORIES else "other"
    source = (d.get("source") or "").strip()
    if not d.get("delete"):                       # validate before touching the database
        if kind == "fact" and not text:
            return jsonify({"error": "Write the fact first."}), 400
        if kind == "fact" and cat == "nutrition" and not source:
            return jsonify({"error": "Nutrition facts need a source."}), 400
        if kind == "banned" and not term:
            return jsonify({"error": "Write the word or claim first."}), 400
    conn = common.db()
    with conn:
        if d.get("delete") and fid:
            conn.execute(f"DELETE FROM {table} WHERE id=?", (int(fid),))
        elif kind == "fact" and fid:
            conn.execute("UPDATE facts SET product_id=?, category=?, text=?, source=? WHERE id=?",
                         (pid, cat, text, source, int(fid)))
        elif kind == "fact":
            conn.execute("INSERT INTO facts (product_id, category, text, source) VALUES (?,?,?,?)",
                         (pid, cat, text, source))
        else:
            conn.execute("INSERT INTO banned_terms (product_id, term, reason) VALUES (?,?,?)",
                         (pid, term, (d.get("reason") or "").strip()))
    conn.close()
    return ops_facts()


@bp.get("/api/ops/costs")
def ops_costs() -> Any:
    from creative_intelligence import budget, providers
    from creative_intelligence.adstudio import costs
    conn = common.db()
    costs.seed_prices(conn)
    b = budget.overview(conn)
    out = {"today_usd": costs.summary(conn)["today_usd"],
           "caps": {"run_usd": b["caps"]["generation_run"], "day_usd": b["caps"]["generation_day"]},
           "budget": b, "models": providers.registry(conn),
           "by_kind": [dict(r) for r in conn.execute(
               "SELECT kind, model, COUNT(*) AS calls, ROUND(SUM(cost_usd),3) AS usd FROM model_calls"
               " WHERE at >= date('now','-7 day') GROUP BY kind, model ORDER BY usd DESC")],
           "prices": [dict(r) for r in conn.execute("SELECT * FROM model_prices ORDER BY model")],
           "runs": [dict(r) for r in conn.execute(
               "SELECT r.id, p.name AS product, r.size, r.status, r.delivered, ROUND(r.spent_usd,2) AS spent_usd,"
               " r.message, r.created_at FROM adstudio_runs r LEFT JOIN adstudio_products p ON p.id=r.product_id"
               " ORDER BY r.id DESC LIMIT 10")],
           "syncs": [dict(r) for r in conn.execute(
               "SELECT id, trigger, status, started_at, finished_at, n_ads, n_patterns, message FROM meta_sync_runs"
               " ORDER BY id DESC LIMIT 8")]}
    conn.close()
    return jsonify(out)


@bp.post("/api/ops/caps")
def ops_caps_save() -> Any:
    """{key, usd}: change a spending cap (budget.py). Takes effect on the next paid call."""
    from creative_intelligence import budget
    d = request.get_json(force=True, silent=True) or {}
    try:
        usd = float(d.get("usd"))
        conn = common.db()
        budget.set_cap(conn, d.get("key", ""), usd)
        caps = budget.caps(conn)
        conn.close()
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc) if "cap" in str(exc) else "Enter an amount in dollars."}), 400
    return jsonify({"ok": True, "caps": caps})


@bp.post("/api/ops/prices")
def ops_prices_save() -> Any:
    """Body: {model, usd} — record a checked price (e.g. from the Replicate bill)."""
    from datetime import date as _date
    d = request.get_json(force=True, silent=True) or {}
    try:
        usd = float(d.get("usd"))
    except (TypeError, ValueError):
        return jsonify({"error": "Price must be a number."}), 400
    from creative_intelligence.adstudio import costs
    conn = common.db()
    costs.seed_prices(conn)
    with conn:
        cur = conn.execute("UPDATE model_prices SET usd=?, source='checked in Admin', checked_at=? WHERE model=?",
                           (usd, _date.today().isoformat(), d.get("model", "")))
    conn.close()
    return (jsonify({"ok": True}) if cur.rowcount else (jsonify({"error": "unknown model"}), 404))


# ── Deploy gating (F1): smoke check after each deploy, and alerts on Admin ──
SMOKE_PAGES = ["/products", "/images/create", "/images/review", "/images/compose", "/images/shots", "/video/create",
               "/video/shots", "/video/edit", "/video/review", "/gallery/assets", "/gallery/performance", "/ideas/bank", "/ideas/market", "/ideas/voice", "/admin", "/api/adstudio/config", "/api/adstudio/products",
               "/api/adstudio/results", "/api/adstudio/ideas", "/api/ops/costs", "/api/ops/alerts", "/api/ops/jobs", "/api/meta/sync"]


@bp.get("/admin/smoke")
def admin_smoke() -> Any:
    """Post-deploy smoke (ADMIN_TOKEN): loads the main pages and APIs inside the app as a signed-in
    user, and checks the worker has checked in. Read-only, no paid calls. 200 when all is well."""
    from flask import current_app
    if not common.admin_token_ok():
        return jsonify({"error": "unauthorized"}), 401
    client = current_app.test_client()
    with client.session_transaction() as sess:
        sess["auth"] = True
    pages = list(SMOKE_PAGES)
    conn = common.db()
    first = conn.execute("SELECT id FROM adstudio_products ORDER BY id LIMIT 1").fetchone()
    conn.close()
    if first:
        pages += [f"/api/adstudio/product/{first[0]}", f"/api/adstudio/gallery?product_id={first[0]}",
                  f"/api/adstudio/product/{first[0]}/runs"]
    checks = {}
    for page in pages:
        try:
            checks[page] = client.get(page).status_code
        except Exception as exc:
            checks[page] = f"error: {type(exc).__name__}"
    conn = common.db()
    w = worker_status(conn)
    conn.close()
    checks["worker"] = 200 if w["alive"] else f"no check-in for {w['heartbeat_age_s'] or 'ever'} s"
    ok = all(v == 200 for v in checks.values())
    return jsonify({"ok": ok, "checks": checks}), (200 if ok else 500)


@bp.post("/admin/alert")
def admin_alert() -> Any:
    """Record an alert for the Admin banner (ADMIN_TOKEN). Used by the deploy workflow."""
    if not common.admin_token_ok():
        return jsonify({"error": "unauthorized"}), 401
    d = request.get_json(force=True, silent=True) or {}
    if not (d.get("message") or "").strip():
        return jsonify({"error": "message required"}), 400
    return jsonify({"id": common.add_alert(d.get("source") or "deploy", d["message"].strip(), d.get("level") or "error")})


@bp.get("/api/ops/alerts")
def ops_alerts() -> Any:
    conn = common.db()
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM ops_alerts WHERE resolved_at IS NULL ORDER BY id DESC LIMIT 20")]
    conn.close()
    return jsonify({"alerts": rows})


@bp.post("/api/ops/alerts/<int:aid>/dismiss")
def ops_alert_dismiss(aid: int) -> Any:
    from datetime import datetime
    conn = common.db()
    with conn:
        conn.execute("UPDATE ops_alerts SET resolved_at=? WHERE id=?", (datetime.utcnow().isoformat(timespec="seconds"), aid))
    conn.close()
    return jsonify({"ok": True})


# ── Backups (F2) ──
@bp.get("/api/ops/backups")
def ops_backups() -> Any:
    from creative_intelligence import backup
    conn = common.db()
    rows = [dict(r) for r in conn.execute("SELECT * FROM ops_backups ORDER BY id DESC LIMIT 10")]
    conn.close()
    return jsonify({"backups": rows, "folder_set": bool(backup.folder_id()), "keep": backup.KEEP,
                    "images_weekday": backup.images_weekday(),
                    "hour_utc": backup.backup_hour(), "railway_volume_backups": "daily (kept 6 days) and weekly (kept 27 days)"})


@bp.post("/api/ops/backups/run")
def ops_backup_run() -> Any:
    """Queue a backup now: {"kind": "database"} (default) or {"kind": "images"}."""
    from creative_intelligence import backup, jobs
    if not backup.folder_id():
        return jsonify({"error": "Set BACKUP_DRIVE_FOLDER_ID (the Drive “Backups” folder) first."}), 400
    kind = "images" if (request.get_json(force=True, silent=True) or {}).get("kind") == "images" else "database"
    conn = common.db()
    job_id = jobs.enqueue(conn, f"backup_{kind}", {"trigger": "manual"}, dedupe_key=f"backup_{kind}")
    conn.close()
    return jsonify({"started": True, "kind": kind, "job_id": job_id})


@bp.get("/api/ops/storage")
def ops_storage() -> Any:
    from creative_intelligence.storage import storage
    return jsonify(storage().usage())


# ── Jobs (F4): what the worker is doing ──
WORKER_STALE_S = 120


def worker_status(conn) -> dict[str, Any]:
    from datetime import datetime
    from creative_intelligence import jobs
    sm = jobs.summary(conn)
    w = sm["worker"]
    age = None
    if w:
        age = (datetime.utcnow() - datetime.fromisoformat(w["heartbeat_at"])).total_seconds()
    return {**sm, "alive": age is not None and age < WORKER_STALE_S, "heartbeat_age_s": age}


@bp.get("/api/ops/jobs")
def ops_jobs() -> Any:
    from creative_intelligence import jobs
    conn = common.db()
    rows = [dict(r) for r in conn.execute(
        "SELECT id, kind, status, attempts, max_attempts, created_at, started_at, finished_at, error"
        " FROM jobs ORDER BY id DESC LIMIT 40")]
    st = worker_status(conn)
    conn.close()
    for r in rows:
        r["label"] = jobs.LABELS.get(r["kind"], r["kind"])
        r["error"] = (r["error"] or "").split("\n")[0][:300]
    return jsonify({"jobs": rows, **st})


@bp.post("/api/ops/jobs/<int:jid>/retry")
def ops_job_retry(jid: int) -> Any:
    from datetime import datetime
    conn = common.db()
    with conn:
        n = conn.execute("UPDATE jobs SET status='queued', attempts=0, run_after=?, error=NULL, finished_at=NULL"
                         " WHERE id=? AND status='failed' AND kind != 'image_run'",
                         (datetime.utcnow().isoformat(timespec="seconds"), jid)).rowcount
    conn.close()
    if not n:
        return jsonify({"error": "Only failed jobs can be retried (start a new run instead of retrying one)."}), 400
    return jsonify({"ok": True})


RETRY_SKIP = ("image_run", "ig_sync")     # a run is started anew; Instagram fails until the token is connected


@bp.post("/api/ops/jobs/retry-failed")
def ops_jobs_retry_failed() -> Any:
    """Every job that failed in the last 24 h back in the queue (feedback 2026-10-30: "rerun the failed jobs …
    there should also be a button for that")."""
    from datetime import datetime, timedelta
    now = datetime.utcnow()
    conn = common.db()
    with conn:
        n = conn.execute(f"UPDATE jobs SET status='queued', attempts=0, run_after=?, error=NULL, finished_at=NULL"
                         f" WHERE status='failed' AND finished_at >= ? AND kind NOT IN ({','.join('?' * len(RETRY_SKIP))})",
                         (now.isoformat(timespec="seconds"), (now - timedelta(days=1)).isoformat(timespec="seconds"),
                          *RETRY_SKIP)).rowcount
    conn.close()
    return jsonify({"ok": True, "queued": n})


# ── Dashboard (F8): the operating picture on one screen ──
def _provider_of(model: str) -> str:
    return "Anthropic (Claude)" if (model or "").startswith("claude") else "Replicate (images, video)"


@bp.get("/api/ops/dashboard")
def ops_dashboard() -> Any:
    from datetime import datetime, timedelta
    from creative_intelligence import budget, jobs
    from creative_intelligence.storage import storage
    conn = common.db()
    now = datetime.utcnow()
    periods = {"today": budget.day_start_utc(), "7 days": (now - timedelta(days=7)).isoformat(),
               "30 days": (now - timedelta(days=30)).isoformat()}
    spend: dict[str, dict[str, float]] = {}
    for label, since in periods.items():
        for r in conn.execute("SELECT model, SUM(cost_usd) FROM model_calls WHERE at >= ? GROUP BY model", (since,)):
            p = spend.setdefault(_provider_of(r[0]), {k: 0.0 for k in periods})
            p[label] = round(p[label] + (r[1] or 0), 2)
    totals = {k: round(sum(p[k] for p in spend.values()), 2) for k in periods}

    from creative_intelligence.adstudio.runs import spent_on_run
    runs = []
    for r in conn.execute("SELECT r.id, p.name AS product, r.size, r.status, r.delivered, r.spent_usd, r.created_at,"
                          " r.finished_at FROM adstudio_runs r LEFT JOIN adstudio_products p ON p.id=r.product_id"
                          " ORDER BY r.id DESC LIMIT 10"):
        r = dict(r)
        spent = r["spent_usd"] if r["finished_at"] else spent_on_run(conn, r["id"])
        delivered = r["delivered"] or 0
        r.update(spent_usd=round(spent or 0, 2),
                 per_image=round(spent / delivered, 3) if delivered and spent else None)
        runs.append(r)
    week = conn.execute("SELECT COALESCE(SUM(spent_usd),0), COALESCE(SUM(delivered),0), COUNT(*) FROM adstudio_runs"
                        " WHERE finished_at >= ? AND delivered > 0", ((now - timedelta(days=7)).isoformat(),)).fetchone()
    per_image_7d = round(week[0] / week[1], 3) if week[1] else None

    since_day = (now - timedelta(days=1)).isoformat(timespec="seconds")
    failed = [dict(r) for r in conn.execute(
        "SELECT id, kind, finished_at, error FROM jobs WHERE status='failed' AND finished_at >= ? ORDER BY id DESC", (since_day,))]
    for f in failed:
        f["label"], f["error"] = jobs.LABELS.get(f["kind"], f["kind"]), (f["error"] or "").split("\n")[0][:200]
    sync = conn.execute("SELECT status, trigger, started_at, finished_at, message FROM meta_sync_runs ORDER BY id DESC LIMIT 1").fetchone()
    backups = {k: (dict(r) if r else None) for k, r in (
        (k, conn.execute("SELECT status, started_at, finished_at, size_bytes, message FROM ops_backups WHERE kind=?"
                         " ORDER BY id DESC LIMIT 1", (k,)).fetchone()) for k in ("database", "images"))}
    out = {"spend": spend, "spend_total": totals, "periods": list(periods),
           "runs": runs, "per_image_7d": per_image_7d, "runs_7d": week[2],
           "budget": budget.overview(conn), "volume": storage().usage(detail=False),
           "failed_jobs": failed, "worker": worker_status(conn),
           "last_sync": dict(sync) if sync else None, "last_backup": backups,
           "alerts": conn.execute("SELECT COUNT(*) FROM ops_alerts WHERE resolved_at IS NULL").fetchone()[0]}
    conn.close()
    return jsonify(out)


@bp.get("/api/ops/video")
def ops_video() -> Any:
    """Video Studio for Admin: cost per finished video by style (first render vs each extra version) and how much
    the shot library is reused."""
    conn = common.db()
    by_style = []
    for r in conn.execute(
            "SELECT v.style, COUNT(DISTINCT v.id) AS videos, COUNT(r.id) AS renders FROM adstudio_videos v"
            " JOIN adstudio_video_renders r ON r.video_id=v.id AND r.status='done' WHERE v.status='done' GROUP BY v.style"):
        ids = [x[0] for x in conn.execute("SELECT DISTINCT v.id FROM adstudio_videos v JOIN adstudio_video_renders r ON"
                                          " r.video_id=v.id WHERE v.status='done' AND v.style=?", (r["style"],))]
        spend = conn.execute(f"SELECT COALESCE(SUM(cost_usd),0) FROM model_calls WHERE source='video' AND source_id IN"
                             f" ({','.join('?' * len(ids))})", ids).fetchone()[0] if ids else 0.0
        by_style.append({"style": r["style"], "videos": r["videos"], "renders": r["renders"], "spend_usd": round(spend, 2),
                         "per_video_usd": round(spend / r["videos"], 2) if r["videos"] else None,
                         "per_render_usd": round(spend / r["renders"], 3) if r["renders"] else None})
    s = conn.execute("SELECT COUNT(*) AS made, SUM(CASE WHEN q.passed=1 THEN 1 ELSE 0 END) AS passing,"
                     " COALESCE(SUM(v.reuse_count),0) AS reuses FROM adstudio_videos v LEFT JOIN qa_scores q ON"
                     " q.source='video' AND q.source_id=v.id WHERE v.style='shot' AND v.status='done'").fetchone()
    shot_spend = conn.execute("SELECT COALESCE(SUM(m.cost_usd),0) FROM model_calls m JOIN adstudio_videos v ON"
                              " m.source='video' AND m.source_id=v.id WHERE v.style='shot'").fetchone()[0]
    out = {"by_style": by_style, "shots": {"made": s["made"], "passing": s["passing"] or 0, "reuses": s["reuses"],
           "reuse_rate": round(s["reuses"] / s["passing"], 2) if s["passing"] else None, "spend_usd": round(shot_spend, 2),
           "per_use_usd": round(shot_spend / s["reuses"], 2) if s["reuses"] else None}}
    conn.close()
    return jsonify(out)
