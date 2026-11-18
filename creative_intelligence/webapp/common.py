"""Helpers shared by the Ad Studio blueprints.

Seams the tests replace: db(), qa_later() and textcheck_later()."""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

from flask import request

_HERE = Path(__file__).parent
log = logging.getLogger("adstudio")


def db() -> sqlite3.Connection:
    """Open creative intelligence DB (initialises schema if needed)."""
    from creative_intelligence.db import get_connection, init_db
    init_db()  # no-op if tables already exist
    return get_connection()


# ── Claude: reference-aware chat + structured hook suggestions ────────────
def _product_dict(conn, pid: int) -> dict[str, Any] | None:
    r = conn.execute("SELECT * FROM adstudio_products WHERE id=?", (pid,)).fetchone()
    return dict(r) if r else None


def _primary_photo_uris(conn, pid: int, limit: int = 3) -> list[str]:
    from creative_intelligence.adstudio.store import to_data_uri
    rows = conn.execute(
        "SELECT path FROM adstudio_photos WHERE product_id=? ORDER BY is_hero DESC, is_primary DESC, id"
        " LIMIT ?", (pid, limit)).fetchall()
    uris = []
    for r in rows:
        try:
            uris.append(to_data_uri(r["path"]))
        except Exception:
            pass
    return uris


def _log_image_cost(conn, source: str, source_id: int) -> None:
    """Record one Nano Banana Pro image in the cost log; never fails the generation."""
    from creative_intelligence.adstudio import costs
    from creative_intelligence.adstudio.generate import MODEL
    try:
        costs.log_image(conn, MODEL, source, source_id)
    except Exception:
        log.exception("cost log failed")


def textcheck_later(ids: list[int]) -> None:
    """Queue the AI Ads text check (Claude reads each image back); the worker runs it."""
    from creative_intelligence import jobs
    conn = db()
    try:
        jobs.enqueue(conn, "textcheck", {"ids": list(ids)})
    finally:
        conn.close()


def textcheck_run(ids: list[int]) -> None:
    """The text check itself (in the worker)."""
    from creative_intelligence.adstudio import textcheck
    conn = db()
    try:
        for i in ids:
            r = conn.execute("SELECT * FROM adstudio_textads WHERE id=? AND image_path IS NOT NULL", (i,)).fetchone()
            if r:
                with conn:
                    conn.execute("UPDATE adstudio_textads SET textcheck_status='checking' WHERE id=?", (i,))
                textcheck.check_textad(conn, dict(r))
                again = conn.execute("SELECT textcheck_status, textcheck_json, text_retries FROM adstudio_textads"
                                     " WHERE id=?", (i,)).fetchone()
                if again["textcheck_status"] == "fail" and again["text_retries"] < 1:
                    _retry_painted(conn, i, json.loads(again["textcheck_json"] or "{}"))
    finally:
        conn.close()


def _retry_painted(conn, ad_id: int, result: dict[str, Any]) -> None:
    """A painted ad whose words came out wrong is made once more, told exactly what to fix, then read back again."""
    from creative_intelligence.webapp.routes.aiads import run_textad
    fix = "The words on the last try were wrong: " + "; ".join(result.get("problems") or [])[:400] + \
          ". Spell every word exactly as given; no other text."
    with conn:
        conn.execute("UPDATE adstudio_textads SET text_retries=text_retries+1, fix=?, review_status=NULL, text_checked=0"
                     " WHERE id=?", (fix, ad_id))
    if run_textad(ad_id).get("status") == "done":
        from creative_intelligence.adstudio import textcheck
        r = conn.execute("SELECT * FROM adstudio_textads WHERE id=?", (ad_id,)).fetchone()
        textcheck.check_textad(conn, dict(r))


def _textfree(row: dict[str, Any] | None = None) -> bool:
    """An AI ad's words drawn by code on a text-free scene (T3), or painted by the image model (AI-designed, the
    original way, read back by the text check). The row's text_mode decides; the env flag is the old default."""
    mode = (row or {}).get("text_mode") or ""
    if mode in ("code", "painted"):
        return mode == "code"
    return os.getenv("TEXTFREE_AI_ADS", "1") == "1"


def qa_later(items: list[tuple[str, int]]) -> None:
    """Queue quality scoring (T4); the worker runs it. items = [(source, id)], source image|textad|core."""
    from creative_intelligence import jobs
    conn = db()
    try:
        jobs.enqueue(conn, "qa_score", {"items": [list(x) for x in items]})
    finally:
        conn.close()


def qa_run(items: list[tuple[str, int]]) -> None:
    """Quality scoring itself (in the worker)."""
    from creative_intelligence.adstudio import qa
    conn = db()
    try:
        for source, sid in items:
            table = {"image": "adstudio_images", "textad": "adstudio_textads", "core": "adstudio_core"}[source]
            r = conn.execute(f"SELECT * FROM {table} WHERE id=?", (sid,)).fetchone()
            if r is None or not r["image_path"]:
                continue
            r = dict(r)
            path, leg = r["image_path"], None
            if source == "textad" and r.get("base_path"):
                path = r["base_path"]                     # score the picture; legibility is measured
                leg = (json.loads(r.get("textcheck_json") or "{}") or {}).get("legibility")
            qa.score(conn, source, sid, path, _primary_photo_uris(conn, r["product_id"], limit=3),
                     _product_dict(conn, r["product_id"]) or {}, leg)
            if source == "image":
                _qa_refresh_composites(conn, sid)
    finally:
        conn.close()


def _claims(conn, source: str, source_id: int, product_id: int, items: dict[str, str]) -> dict[str, Any] | None:
    """T6: check copy against the facts library and store the result (FACTS_CHECK, on by default)."""
    if os.getenv("FACTS_CHECK", "1") != "1":
        return None
    from creative_intelligence.adstudio import facts
    res = facts.check_copy(conn, _product_dict(conn, product_id) or {}, items, source=source, source_id=source_id)
    facts.store(conn, source, source_id, res)
    return res


def _qa_refresh_composites(conn, image_id: int) -> None:
    """Re-inherit the (now scored) base image's scores into its composites, keeping their legibility."""
    from creative_intelligence.adstudio import qa
    for c in conn.execute("SELECT id FROM adstudio_composites WHERE image_id=? OR image_id IN"
                          " (SELECT id FROM adstudio_images WHERE parent_image_id=?)", (image_id, image_id)).fetchall():
        cur = qa.scores_for(conn, "composite", [c["id"]]).get(c["id"])
        leg = (cur or {}).get("scores", {}).get("legibility")
        qa.inherit(conn, "composite", c["id"], "image", image_id, None, leg_value=leg)


def _qa_view(conn, source: str, rows: list[dict[str, Any]], view: str = "", sort: str = "") -> tuple[list, int]:
    """Attach QA scores; hide QA-failed rows unless view == 'qa_rejected'; optional sort by score.

    Returns (rows, number of QA-rejected rows among those given)."""
    from creative_intelligence.adstudio import qa
    by = qa.scores_for(conn, source, [r["id"] for r in rows])
    for r in rows:
        r["qa"] = by.get(r["id"])
    failed = [r for r in rows if r["qa"] and r["qa"]["total"] is not None and not r["qa"]["passed"]]
    if view == "qa_rejected":
        rows = failed
    elif view != "all_qa":
        ids = {r["id"] for r in failed}
        rows = [r for r in rows if r["id"] not in ids]
    if sort == "score":
        rows = sorted(rows, key=lambda r: -((r["qa"] or {}).get("total") or 0))
    return rows, len(failed)


def _bulk_apply(table: str, action: str, ids: list[int]) -> int:
    """Apply a bulk action to rows of a table; returns affected count."""
    from datetime import datetime as _dt
    ids = [int(x) for x in ids if str(x).isdigit()]
    if not ids:
        return 0
    ph = ",".join("?" * len(ids))
    if table in ("adstudio_composites", "adstudio_textads", "adstudio_video_renders") and action in _REVIEW_SQL:
        # AI ads only go ready once their text has been checked
        gate = " AND text_checked=1" if table == "adstudio_textads" and action == "ready" else ""
        conn = db()
        with conn:
            cur = conn.execute(f"UPDATE {table} SET {_REVIEW_SQL[action]},"
                               f" reviewed_at=? WHERE id IN ({ph}){gate}", [_dt.utcnow().isoformat(), *ids])
        _log_reviews(conn, table, ids, action)
        conn.close()
        return cur.rowcount
    if table == "adstudio_images" and action in ("approve", "reject", "clear"):
        conn = db()
        with conn:
            cur = conn.execute(f"UPDATE adstudio_images SET review_status=? WHERE id IN ({ph})",
                               [{"approve": "approved", "reject": "rejected", "clear": ""}[action], *ids])
        _log_reviews(conn, table, ids, action)
        conn.close()
        return cur.rowcount
    ops = {
        "favorite":   f"UPDATE {table} SET is_favorite=1 WHERE id IN ({ph})",
        "unfavorite": f"UPDATE {table} SET is_favorite=0 WHERE id IN ({ph})",
        "archive":    f"UPDATE {table} SET archived=1 WHERE id IN ({ph})",
        "unarchive":  f"UPDATE {table} SET archived=0 WHERE id IN ({ph})",
        "delete":     f"DELETE FROM {table} WHERE id IN ({ph})",
    }
    if action not in ops:
        raise ValueError(f"bad action {action!r}")
    conn = db()
    with conn:
        cur = conn.execute(ops[action], ids)
    conn.close()
    return cur.rowcount


def _log_reviews(conn, table: str, ids: list[int], action: str) -> None:
    """Log human reviews of QA-scored items, so overrules can be counted (T4)."""
    from creative_intelligence.adstudio import qa
    source = {"adstudio_images": "image", "adstudio_textads": "textad",
              "adstudio_composites": "composite", "adstudio_core": "core",
              "adstudio_video_renders": "video_render"}[table]
    for i in ids:
        qa.log_review(conn, source, i, action)


# Gallery review states. Ready implies approved; rejecting clears ready.
_REVIEW_SQL = {
    "approve": "review_status='approved'",
    "reject":  "review_status='rejected', is_ready=0",
    "ready":   "review_status='approved', is_ready=1",
    "unready": "is_ready=0",
    "clear":   "review_status='', is_ready=0",
}


_EXPORTED_SQL = ("SELECT 1 FROM adstudio_drive_export_items i JOIN adstudio_drive_exports e"
                 " ON e.id=i.export_id WHERE i.textad_id=t.id")


def _attach_exports(conn, rows: list[dict[str, Any]]) -> None:
    """Each ad's Drive exports, newest first: [{folder_name, folder_url, at}]."""
    ids = [r["id"] for r in rows]
    by: dict[int, list] = {i: [] for i in ids}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for x in conn.execute(
                "SELECT i.textad_id, e.folder_name, e.folder_url, i.uploaded_at FROM adstudio_drive_export_items i"
                " JOIN adstudio_drive_exports e ON e.id=i.export_id"
                f" WHERE i.textad_id IN ({','.join('?' * len(chunk))}) ORDER BY i.id DESC", chunk):
            by[x["textad_id"]].append({"folder_name": x["folder_name"], "folder_url": x["folder_url"],
                                       "at": x["uploaded_at"]})
    for r in rows:
        r["exports"] = by.get(r["id"], [])


def _attach_assets(conn, source: str, rows: list[dict[str, Any]], product_id: int | None) -> None:
    """Give each finished ad its Asset ID, tags and (when linked) its Meta results."""
    from creative_intelligence.adstudio import assets
    assets.ensure_assets(conn, product_id)
    by = assets.assets_for(conn, source, [r["id"] for r in rows])
    for r in rows:
        r["asset"] = by.get(r["id"])


def _asset_ids(conn, source: str, ids: list[int]) -> dict[int, str]:
    from creative_intelligence.adstudio import assets
    assets.ensure_assets(conn)
    return {k: v["asset_id"] for k, v in assets.assets_for(conn, source, ids).items()}


def _uri(path: str) -> str | None:
    from creative_intelligence.adstudio.store import to_data_uri
    try:
        return to_data_uri(path)
    except Exception:
        return None


def admin_token_ok() -> bool:
    """For /admin/* endpoints called by cron and the deploy smoke: ADMIN_TOKEN in the X-Admin-Token header."""
    import hmac
    token = os.getenv("ADMIN_TOKEN", "")
    return bool(token) and hmac.compare_digest(request.headers.get("X-Admin-Token", "").encode(), token.encode())


def add_alert(source: str, message: str, level: str = "error") -> int:
    from creative_intelligence.alerts import raise_alert
    conn = db()
    try:
        return raise_alert(conn, source, message, level)
    finally:
        conn.close()


def run_deps() -> dict[str, Any]:
    """The real planner, image maker and scorer for an image run (used by the worker)."""
    from creative_intelligence.adstudio import costs, qa, runs
    from creative_intelligence.adstudio.generate import MODEL, generate_ad
    from creative_intelligence.adstudio.store import save_generated_image

    def make_image(c, row, prompt, ref_uris, aspect, run_id):
        path = save_generated_image(generate_ad(prompt, ref_uris, aspect), row["product_id"], row["id"])
        costs.log_image(c, MODEL, "image", row["id"], run_id=str(run_id))
        return path

    return {
        "plan": lambda c, prod, n, prior, refs, run_id: runs.plan_run(prod, n, prior, refs, c, run_id),
        "make_image": make_image,
        "score": lambda c, img_id, path, refs, prod, run_id: qa.score(c, "image", img_id, path, refs, prod, run_id=run_id),
        "refs": lambda c, pid: _primary_photo_uris(c, pid, limit=3),
        "product": lambda c, pid: _product_dict(c, pid) or {},
    }
