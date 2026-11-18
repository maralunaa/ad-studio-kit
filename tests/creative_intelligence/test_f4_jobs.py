"""F4 — background work goes through a SQLite jobs table to one worker process; the web starts
no threads. Covers queueing, dedupe, claiming, retries, restart recovery and the hand-offs."""
from __future__ import annotations

import json
import threading
from datetime import datetime

import pytest

from creative_intelligence import db as D
from creative_intelligence import jobs, worker


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setenv("CI_DB_PATH", str(path))
    D._UP_TO_DATE.clear()
    D.init_db(path)
    factory = lambda: D.get_connection(path)
    monkeypatch.setattr("creative_intelligence.webapp.common.db", factory)
    return factory


def test_enqueue_dedupes_while_active(db):
    c = db()
    a = jobs.enqueue(c, "backup_database", {"trigger": "scheduled"}, dedupe_key="backup_database")
    assert jobs.enqueue(c, "backup_database", {}, dedupe_key="backup_database") == a
    with c:
        c.execute("UPDATE jobs SET status='done' WHERE id=?", (a,))
    assert jobs.enqueue(c, "backup_database", {}, dedupe_key="backup_database") != a
    with pytest.raises(ValueError):
        jobs.enqueue(c, "rm_rf", {})


def test_claim_takes_oldest_due_job_once(db):
    c = db()
    first = jobs.enqueue(c, "qa_score", {"items": []})
    jobs.enqueue(c, "qa_score", {"items": []}, delay_s=3600)          # not due yet
    got = jobs.claim(c, "w:1")
    assert got["id"] == first and got["attempts"] == 1 and got["payload"] == {"items": []}
    assert jobs.claim(c, "w:2") is None


def test_failures_retry_then_fail(db):
    c = db()
    jid = jobs.enqueue(c, "meta_sync", {"run_id": 1}, max_attempts=2)
    job = jobs.claim(c, "w")
    assert jobs.fail(c, job, "boom") == "queued"
    with c:
        c.execute("UPDATE jobs SET run_after='2000-01-01' WHERE id=?", (jid,))
    job = jobs.claim(c, "w")
    assert job["attempts"] == 2 and jobs.fail(c, job, "boom again") == "failed"


def test_worker_runs_a_job_and_alerts_on_final_failure(db, monkeypatch):
    seen = []
    monkeypatch.setitem(jobs.HANDLERS, "backup_images", lambda p, f: seen.append(p) or {"sent": 3})
    monkeypatch.setitem(jobs.HANDLERS, "meta_sync", lambda p, f: (_ for _ in ()).throw(RuntimeError("Meta down")))
    c = db()
    ok = jobs.enqueue(c, "backup_images", {"trigger": "manual"})
    bad = jobs.enqueue(c, "meta_sync", {"run_id": 9})
    assert worker.run_one(jobs.claim(c, "w"), db) == "done"
    assert worker.run_one(jobs.claim(c, "w"), db) == "failed"
    rows = {r["id"]: r for r in c.execute("SELECT * FROM jobs")}
    assert rows[ok]["status"] == "done" and '"sent": 3' in rows[ok]["result_json"] and seen == [{"trigger": "manual"}]
    assert rows[bad]["status"] == "failed" and "Meta down" in rows[bad]["error"]
    assert "Meta sync (job" in c.execute("SELECT message FROM ops_alerts WHERE source='jobs'").fetchone()[0]


def test_restart_closes_runs_and_requeues_other_work(db):
    c = db()
    with c:
        c.execute("INSERT INTO adstudio_products (id, name) VALUES (1, 'Mango')")
        c.execute("INSERT INTO adstudio_runs (id, product_id, size, status) VALUES (1, 1, 8, 'running')")
    run_job = jobs.enqueue(c, "image_run", {"run_id": 1})
    sync_job = jobs.enqueue(c, "meta_sync", {"run_id": 2}, max_attempts=2)
    jobs.claim(c, "dead:1"), jobs.claim(c, "dead:1")
    out = jobs.recover(c)
    assert out == {"runs_closed": 1, "requeued": 1, "failed": 1}
    st = {r["id"]: r["status"] for r in c.execute("SELECT id, status FROM jobs")}
    assert st == {run_job: "failed", sync_job: "queued"}
    assert c.execute("SELECT status FROM adstudio_runs WHERE id=1").fetchone()[0] == "stopped"


def test_a_clip_cut_off_by_a_restart_is_made_again_once(db):
    """Feedback 2026-10-23: deploys left animations and shots 'Interrupted by an app restart'."""
    c = db()
    with c:
        c.execute("INSERT INTO adstudio_products (id, name) VALUES (1, 'Mango')")
        c.execute("INSERT INTO adstudio_videos (id, product_id, source_kind, source_id, status, created_at)"
                  " VALUES (5, 1, 'image', 1, 'running', '')")
    jobs.enqueue(c, "animate", {"video_id": 5}, dedupe_key="video:5")
    jobs.claim(c, "dead:1")
    jobs.recover(c)
    assert c.execute("SELECT status FROM adstudio_videos WHERE id=5").fetchone()[0] == "queued"
    again = c.execute("SELECT payload_json FROM jobs WHERE status='queued'").fetchone()
    assert json.loads(again[0]) == {"video_id": 5, "restarted": 1}
    with c:
        c.execute("INSERT INTO adstudio_textads (id, product_id, brief_id, kind, status) VALUES (9, 1, 0, 'variant', 'generating')")
    jobs.claim(c, "dead:2")                                    # cut off a second time: closed as before
    jobs.recover(c)
    assert c.execute("SELECT status FROM adstudio_videos WHERE id=5").fetchone()[0] == "failed"
    assert c.execute("SELECT status FROM adstudio_textads WHERE id=9").fetchone()[0] == "failed"   # never stuck


def test_web_hand_offs_queue_jobs_instead_of_threads(db, monkeypatch):
    from creative_intelligence.adstudio import runs
    from creative_intelligence.ingest import meta_sync
    from creative_intelligence.webapp import common
    started = []
    monkeypatch.setattr(threading.Thread, "start", lambda self: started.append(self.name))
    common.qa_later([("image", 4)])
    common.textcheck_later([7])
    runs.start_in_background(db, 12)
    rid, reason = meta_sync.start_sync(db, trigger="manual")
    assert meta_sync.start_sync(db, trigger="manual") == (None, "A sync is already running.")   # even from another process
    kinds = [(r["kind"], r["payload_json"]) for r in db().execute("SELECT kind, payload_json FROM jobs ORDER BY id")]
    assert kinds == [("qa_score", '{"items": [["image", 4]]}'), ("textcheck", '{"ids": [7]}'),
                     ("image_run", '{"run_id": 12}'), ("meta_sync", f'{{"run_id": {rid}}}')]
    assert started == []


def test_worker_main_recovers_then_works_the_queue(db, monkeypatch):
    done = threading.Event()
    monkeypatch.setitem(jobs.HANDLERS, "backup_database", lambda p, f: done.set())
    monkeypatch.setattr(worker, "POLL_S", 0.05)
    monkeypatch.delenv("SCHEDULER_ENABLED", raising=False)
    c = db()
    jobs.enqueue(c, "backup_database", {"trigger": "manual"})
    stop = threading.Event()
    t = threading.Thread(target=worker.main, kwargs={"stop": stop, "install_signals": False}, daemon=True)
    t.start()
    assert done.wait(5)
    import time
    for _ in range(50):                                   # the first check-in may land just after the job
        hb = c.execute("SELECT heartbeat_at FROM ops_worker").fetchone()
        if hb:
            break
        time.sleep(0.1)
    stop.set()
    t.join(5)
    assert hb and hb[0] >= datetime.utcnow().strftime("%Y-%m-%d")


def test_jobs_view_and_retry(db):
    from creative_intelligence.webapp.app import app
    client = app.test_client()
    c = db()
    jid = jobs.enqueue(c, "backup_database", {})
    jobs.fail(c, jobs.claim(c, "w"), "Drive said no")
    d = client.get("/api/ops/jobs").get_json()
    assert d["jobs"][0]["label"] == "Database backup" and d["jobs"][0]["error"] == "Drive said no"
    assert d["alive"] is False
    assert client.post(f"/api/ops/jobs/{jid}/retry").get_json()["ok"]
    assert c.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()[0] == "queued"
