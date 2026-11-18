"""
Background jobs: a SQLite table the web enqueues into and the worker process works through.

  enqueue(conn, kind, payload, dedupe_key=…)   from a web request: returns the job id at once
  claim / finish / fail                        used by the worker (creative_intelligence/worker.py)
  HANDLERS                                     what each kind of job does

A job with a dedupe_key is not queued twice while one with the same key is queued or running
(one Meta sync, one backup, one run per run id). Failed jobs retry while attempts < max_attempts.
"""
from __future__ import annotations

import json
import os
import socket
from datetime import datetime, timedelta
from typing import Any, Callable

ACTIVE = ("queued", "running")


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def enqueue(conn, kind: str, payload: dict[str, Any] | None = None, dedupe_key: str | None = None,
            max_attempts: int = 1, delay_s: int = 0) -> int:
    if kind not in HANDLERS:
        raise ValueError(f"unknown job kind {kind!r}")
    with conn:
        if dedupe_key:
            row = conn.execute(f"SELECT id FROM jobs WHERE dedupe_key=? AND status IN {ACTIVE}", (dedupe_key,)).fetchone()
            if row:
                return row[0]
        run_after = (datetime.utcnow() + timedelta(seconds=delay_s)).isoformat(timespec="seconds")
        return conn.execute("INSERT INTO jobs (kind, payload_json, dedupe_key, max_attempts, run_after, created_at)"
                            " VALUES (?,?,?,?,?,?)", (kind, json.dumps(payload or {}), dedupe_key, max_attempts,
                                                      run_after, _now())).lastrowid


def worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def claim(conn, worker: str) -> dict[str, Any] | None:
    """Take the oldest due job (atomically). None when nothing is waiting."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT * FROM jobs WHERE status='queued' AND run_after <= ? ORDER BY id LIMIT 1",
                           (_now(),)).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        conn.execute("UPDATE jobs SET status='running', attempts=attempts+1, started_at=?, worker=?, error=NULL"
                     " WHERE id=?", (_now(), worker, row["id"]))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    job = dict(row)
    job["attempts"] += 1
    job["payload"] = json.loads(job["payload_json"] or "{}")
    return job


def finish(conn, job_id: int, result: Any = None) -> None:
    with conn:
        conn.execute("UPDATE jobs SET status='done', finished_at=?, result_json=? WHERE id=?",
                     (_now(), json.dumps(result, default=str)[:4000] if result is not None else None, job_id))


def fail(conn, job: dict[str, Any], error: str) -> str:
    """Retry later if attempts remain, else mark failed. Returns the new status."""
    retry = job["attempts"] < job["max_attempts"]
    with conn:
        if retry:
            after = (datetime.utcnow() + timedelta(seconds=60 * job["attempts"])).isoformat(timespec="seconds")
            conn.execute("UPDATE jobs SET status='queued', run_after=?, error=? WHERE id=?", (after, error[:1000], job["id"]))
        else:
            conn.execute("UPDATE jobs SET status='failed', finished_at=?, error=? WHERE id=?",
                         (_now(), error[:1000], job["id"]))
    return "queued" if retry else "failed"


RESTART_KINDS = ("animate", "shot", "styled", "free_video")   # clips cut off by a deploy: made again once (feedback 2026-10-23)


def _restart_clips(conn, stale: list[dict[str, Any]]) -> set[int]:
    """Animations, library shots and styled videos a restart cut off go back in the queue once (a second restart
    closes them as before). Returns the ids of the jobs handled here."""
    done = set()
    for job in stale:
        p = json.loads(job["payload_json"] or "{}")
        if job["kind"] not in RESTART_KINDS or p.get("restarted") or not p.get("video_id"):
            continue
        with conn:
            conn.execute("UPDATE jobs SET status='failed', finished_at=?, error='interrupted by a restart; queued again'"
                         " WHERE id=?", (_now(), job["id"]))
            conn.execute("UPDATE adstudio_videos SET status='queued', step='', message='Restarted after an app update.'"
                         " WHERE id=? AND status IN ('queued','running')", (p["video_id"],))
        enqueue(conn, job["kind"], {**p, "restarted": 1}, dedupe_key=job["dedupe_key"], max_attempts=job["max_attempts"])
        done.add(job["id"])
    return done


def recover(conn) -> dict[str, int]:
    """Worker start-up: jobs left 'running' by a stopped worker. Runs are closed as interrupted
    (their slots were half-made); other kinds go back in the queue if they may retry."""
    from creative_intelligence.adstudio.runs import mark_interrupted
    stale = [dict(r) for r in conn.execute("SELECT * FROM jobs WHERE status='running'")]
    restarted = _restart_clips(conn, stale)
    stale = [j for j in stale if j["id"] not in restarted]
    closed = mark_interrupted(conn)
    with conn:                                       # half-made clips: closed, like runs (they spend money)
        closed += conn.execute("UPDATE adstudio_videos SET status='failed', step='',"
                               " message='Interrupted by an app restart; start it again.'"
                               " WHERE status IN ('queued','running') AND id IN (SELECT json_extract(payload_json,"
                               " '$.video_id') FROM jobs WHERE status='running' AND kind IN ('animate','shot','story','talking','styled'))").rowcount
        closed += conn.execute("UPDATE adstudio_textads SET status='failed', error_message='Interrupted by an app"
                               " restart; make it again.' WHERE status IN ('queued','generating')").rowcount   # AI ads
        closed += conn.execute("UPDATE product_research SET status='failed', error='Interrupted by an app restart;"
                               " start it again.' WHERE status IN ('queued','running') AND id IN (SELECT"
                               " json_extract(payload_json, '$.research_id') FROM jobs WHERE status='running'"
                               " AND kind='research')").rowcount
        closed += conn.execute("UPDATE adstudio_products SET fill_status='failed', fill_step='', fill_error='Interrupted"
                               " by an app restart; fill it again.' WHERE fill_status IN ('queued','running') AND id IN"
                               " (SELECT json_extract(payload_json, '$.product_id') FROM jobs WHERE status='running'"
                               " AND kind='fill')").rowcount
        closed += conn.execute("UPDATE video_templates SET status='failed', step='', error='Interrupted by an app restart.'"
                               " WHERE status IN ('queued','running') AND id IN (SELECT json_extract(payload_json,"
                               " '$.template_id') FROM jobs WHERE status='running' AND kind='decode')").rowcount
        closed += conn.execute("UPDATE presenter_looks SET status='failed', error='Interrupted by an app restart.'"
                               " WHERE status='queued' AND id IN (SELECT j.value FROM jobs, json_each(jobs.payload_json,"
                               " '$.look_ids') AS j WHERE jobs.status='running' AND jobs.kind='presenter_look')").rowcount
        closed += conn.execute("UPDATE character_sheets SET status='failed', error='Interrupted by an app restart.'"
                               " WHERE status='queued' AND id IN (SELECT j.value FROM jobs, json_each(jobs.payload_json,"
                               " '$.sheet_ids') AS j WHERE jobs.status='running' AND jobs.kind='character_sheet')").rowcount
    out = {"runs_closed": closed, "requeued": 0, "failed": 0}
    for job in stale:
        if job["kind"] not in ("image_run", "animate", "shot", "research", "fill", "story", "decode", "presenter_look", "character_sheet", "talking", "styled") and job["attempts"] < job["max_attempts"]:
            with conn:
                conn.execute("UPDATE jobs SET status='queued', run_after=?, error='worker restarted' WHERE id=?",
                             (_now(), job["id"]))
            out["requeued"] += 1
        else:
            with conn:
                conn.execute("UPDATE jobs SET status='failed', finished_at=?, error='interrupted by a restart' WHERE id=?",
                             (_now(), job["id"]))
            out["failed"] += 1
    return out


def summary(conn) -> dict[str, Any]:
    since = (datetime.utcnow() - timedelta(days=1)).isoformat(timespec="seconds")
    counts = {r[0]: r[1] for r in conn.execute(
        "SELECT status, COUNT(*) FROM jobs WHERE status IN ('queued','running') OR created_at >= ? GROUP BY status", (since,))}
    w = conn.execute("SELECT * FROM ops_worker ORDER BY heartbeat_at DESC LIMIT 1").fetchone()
    return {"counts": counts, "worker": dict(w) if w else None}


# ── what each kind does (run in the worker) ─────────────────────────────────
def _image_run(p: dict, db: Callable) -> Any:
    from creative_intelligence.adstudio import runs
    from creative_intelligence.webapp.common import run_deps
    return runs.process_run(db, p["run_id"], **run_deps())


def _qa_score(p: dict, db: Callable) -> Any:
    from creative_intelligence.webapp.common import qa_run
    qa_run([tuple(x) for x in p["items"]])


def _textcheck(p: dict, db: Callable) -> Any:
    from creative_intelligence.webapp.common import textcheck_run
    textcheck_run(p["ids"])


def _meta_sync(p: dict, db: Callable) -> Any:
    from creative_intelligence.ingest import meta_sync
    meta_sync.run_sync(db, p["run_id"])


def _animate(p: dict, db: Callable) -> Any:
    from creative_intelligence.adstudio import animate
    return animate.process(p["video_id"], db)


def _shot(p: dict, db: Callable) -> Any:
    from creative_intelligence.adstudio import shots
    return shots.process(p["video_id"], db)


def _shot_rescore(p: dict, db: Callable) -> Any:
    from creative_intelligence.adstudio import shots
    return shots.rescore(p["video_id"], db)


def _story(p: dict, db: Callable) -> Any:
    from creative_intelligence.adstudio import story
    return story.process(p["video_id"], db)


def _video_render(p: dict, db: Callable) -> Any:
    from creative_intelligence.adstudio import animate
    conn = db()
    try:
        return {"renders": animate.render_outputs(conn, p["video_id"], p.get("copies") or [],
                                                  tuple(p.get("aspects") or animate.ASPECTS))}
    finally:
        conn.close()


def _backup_database(p: dict, db: Callable) -> Any:
    from creative_intelligence import backup
    return backup.backup_database(db, p.get("trigger", "manual"))


def _backup_images(p: dict, db: Callable) -> Any:
    from creative_intelligence import backup
    return backup.backup_images(db, p.get("trigger", "manual"))


def _shopify_sync(p: dict, db: Callable) -> Any:
    from creative_intelligence.adstudio import catalog
    conn = db()
    try:
        return catalog.sync(conn, p.get("trigger", "manual"))
    finally:
        conn.close()


def _styled(p: dict, db: Callable) -> Any:
    from creative_intelligence.adstudio import styles
    return styles.process(p["video_id"], db)


def _talking(p: dict, db: Callable) -> Any:
    from creative_intelligence.adstudio import talking
    return talking.process(p["video_id"], db)


def _presenter_look(p: dict, db: Callable) -> Any:
    from creative_intelligence.adstudio import presenters
    return presenters.process(p["look_ids"], db)


def _decode(p: dict, db: Callable) -> Any:
    from creative_intelligence.adstudio import winners
    return winners.decode(db, p["template_id"])


def _compose_batch(p: dict, db: Callable) -> Any:
    from creative_intelligence.adstudio import batch
    return batch.process(p["batch_id"], db)


def _research(p: dict, db: Callable) -> Any:
    from creative_intelligence.adstudio import hub
    return hub.run_research(db, p["research_id"])


def _fill(p: dict, db: Callable) -> Any:
    from creative_intelligence.adstudio import hub
    return hub.fill(db, p["product_id"])


def _character_sheet(p: dict, db: Callable) -> Any:
    from creative_intelligence.adstudio import characters
    return characters.process(p["sheet_ids"], db)


def _research_brief(p: dict, db: Callable) -> Any:
    from creative_intelligence.adstudio import research
    conn = db()
    try:
        research.tag(conn)                          # tag new competitor ads first, then the brief
        return research.weekly_brief(conn, trigger=p.get("trigger", "scheduled"))
    finally:
        conn.close()


def _research_scan(p: dict, db: Callable) -> Any:
    from creative_intelligence.adstudio import market
    return market.refresh(db, trigger=p.get("trigger", "scheduled"))


def _ig_sync(p: dict, db: Callable) -> Any:
    from creative_intelligence.adstudio import instagram
    conn = db()
    try:
        return instagram.sync(conn)
    finally:
        conn.close()


def _voice(p: dict, db: Callable) -> Any:
    """Customer voice (R3): one import, one extraction, or the weekly refresh."""
    from creative_intelligence.adstudio import voice
    if p.get("step") == "refresh":
        return voice.refresh(db)
    conn = db()
    try:
        if p.get("step") == "extract":
            return voice.extract(conn, p.get("product_id"))
        if p.get("source") == "fera":
            return voice.import_fera(conn)
        return voice.import_web(conn, p["source"])
    finally:
        conn.close()


def _taste(p: dict, db: Callable) -> Any:
    from creative_intelligence.adstudio import swipe
    conn = db()
    try:
        return swipe.propose(conn, trigger=p.get("trigger", "scheduled"))
    finally:
        conn.close()


def _drive_kit(p: dict, db: Callable) -> Any:
    from creative_intelligence.adstudio import launchkit
    return launchkit.to_drive(db, p["product_id"], p["date"])


def _video_batch(p: dict, db: Callable) -> Any:
    from creative_intelligence.adstudio import video_batch
    if p.get("step") == "advance":
        return video_batch.advance(db, p["batch_id"])
    return video_batch.plan(db, p["batch_id"])


def _shot_compare(p: dict, db: Callable) -> Any:
    from creative_intelligence.adstudio import shots
    return shots.compare(db, p["product_id"], p["shot_type"], tuple(p.get("routes") or shots.COMPARE_ROUTES))


def _free_video(p: dict, db: Callable) -> Any:
    from creative_intelligence.adstudio import free_video
    return free_video.process(p["video_id"], db)


def _hook_recheck(p: dict, db: Callable) -> Any:
    from creative_intelligence.webapp.routes.products import recheck_hooks
    conn = db()
    try:
        return recheck_hooks(conn, p["product_id"])
    finally:
        conn.close()


def _adpack(p: dict, db: Callable) -> Any:
    from creative_intelligence.adstudio import adpack
    return adpack.advance(db, p["pack_id"])


def _read_screens(p: dict, db: Callable) -> Any:
    from creative_intelligence.adstudio import market
    return market.read_screens(db, p["paths"])


def _core_shots(p: dict, db: Callable) -> Any:
    from creative_intelligence.webapp.routes.core import run_core
    return [run_core(i) for i in p["ids"]]


def _like_this(p: dict, db: Callable) -> Any:
    from creative_intelligence.webapp.routes.aiads import run_textad
    return [run_textad(i) for i in p["ids"]]


def _knowledge_update(p: dict, db: Callable) -> Any:
    from creative_intelligence.adstudio import knowledge_updates
    return knowledge_updates.propose(db, p["update_id"])


HANDLERS: dict[str, Callable[[dict, Callable], Any]] = {
    "image_run": _image_run, "qa_score": _qa_score, "textcheck": _textcheck, "meta_sync": _meta_sync,
    "backup_database": _backup_database, "backup_images": _backup_images,
    "animate": _animate, "video_render": _video_render, "shot": _shot, "shot_rescore": _shot_rescore,
    "shopify_sync": _shopify_sync, "research": _research, "fill": _fill, "story": _story, "decode": _decode,
    "presenter_look": _presenter_look, "talking": _talking, "styled": _styled,
    "compose_batch": _compose_batch, "knowledge_update": _knowledge_update,
    "character_sheet": _character_sheet, "research_brief": _research_brief, "research_scan": _research_scan, "voice": _voice, "taste": _taste,
    "drive_kit": _drive_kit, "video_batch": _video_batch, "adpack": _adpack, "like_this": _like_this, "read_screens": _read_screens, "hook_recheck": _hook_recheck, "free_video": _free_video, "shot_compare": _shot_compare, "core_shots": _core_shots,
    "ig_sync": _ig_sync,
}
LABELS = {"adpack": "Ad Pack", "free_video": "Free prompt video", "shot_compare": "Compare video models on one shot", "hook_recheck": "Recheck a product's hooks", "image_run": "Image run", "qa_score": "QA scoring", "textcheck": "Text check", "meta_sync": "Meta sync",
          "backup_database": "Database backup", "backup_images": "Image backup",
          "animate": "Animate a still", "video_render": "Video versions", "shot": "Library shot", "shot_rescore": "Score a shot again",
          "shopify_sync": "Shopify product sync", "research": "Product research",
          "fill": "Fill a product profile", "story": "Storytelling video",
          "decode": "Decode a winning video ad", "presenter_look": "Presenter looks",
          "talking": "Talking-head clip", "styled": "Styled video and variants",
          "compose_batch": "Bulk static ads", "knowledge_update": "Playbook update proposals",
          "character_sheet": "Fruit character sheets", "research_brief": "Weekly research brief",
          "research_scan": "Monthly market refresh",
          "voice": "Customer voice",
          "taste": "Taste rubric proposals",
          "drive_kit": "Launch kit to Drive",
          "video_batch": "Video batch (storyboards, then drafts)",
          "like_this": "Make ours like this", "read_screens": "Read competitor screenshots",
          "core_shots": "Product shots", "ig_sync": "Our Instagram posts"}
