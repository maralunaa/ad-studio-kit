"""
The worker process: does the background work the web queues (creative_intelligence/jobs.py) and
runs the scheduler (daily Meta sync, nightly and weekly backups, volume check).

    python -m creative_intelligence.worker

On Railway it runs beside gunicorn in the same service (honcho + Procfile); locally `python
creative_intelligence/webapp/app.py` runs one in a thread. Start-up:
  1. take the worker lock next to the database, so only one worker uses a data volume
  2. recover jobs a stopped worker left running (runs are closed as interrupted)
  3. start the scheduler, then work through the queue with JOB_CONCURRENCY threads,
     checking in (ops_worker) every 30 s so Admin and the deploy smoke can see it is alive.
"""
from __future__ import annotations

import logging
import os
import signal
import threading
import time
import traceback
from datetime import datetime
from typing import Any, Callable

from creative_intelligence import jobs

log = logging.getLogger("adstudio.worker")

CONCURRENCY = int(os.getenv("JOB_CONCURRENCY", "10"))    # shots and videos run side by side (was 5; most of a
                                                           # job is waiting on Replicate, renders are capped apart)
POLL_S = 2
HEARTBEAT_S = 30
ALERT_KINDS = {"image_run", "meta_sync", "backup_database", "backup_images", "animate", "shot",
               "shopify_sync", "research", "fill", "story", "decode", "presenter_look", "talking", "styled"}


def _db() -> Any:
    from creative_intelligence.webapp import common
    return common.db()


def run_one(job: dict[str, Any], db: Callable[[], Any] = _db) -> str:
    """Run a claimed job; record done / retry / failed. Returns the final status."""
    t0 = time.time()
    try:
        result = jobs.HANDLERS[job["kind"]](job["payload"], db)
    except Exception as exc:
        conn = db()
        try:
            status = jobs.fail(conn, job, f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=3)}")
            if status == "failed" and job["kind"] in ALERT_KINDS:
                from creative_intelligence.alerts import raise_alert
                raise_alert(conn, "jobs", f"{jobs.LABELS.get(job['kind'], job['kind'])} (job {job['id']}) failed: {exc}"[:500])
        finally:
            conn.close()
        log.exception("job %s %s %s", job["id"], job["kind"], status)
        return status
    conn = db()
    try:
        jobs.finish(conn, job["id"], result)
    finally:
        conn.close()
    log.info("job %s %s done in %.0fs", job["id"], job["kind"], time.time() - t0)
    return "done"


def work_loop(stop: threading.Event, wid: str, db: Callable[[], Any] = _db) -> None:
    while not stop.is_set():
        try:
            conn = db()
            try:
                job = jobs.claim(conn, wid)
            finally:
                conn.close()
        except Exception:
            log.exception("could not claim a job")
            job = None
        if job is None:
            stop.wait(POLL_S)
            continue
        run_one(job, db)


def heartbeat(stop: threading.Event, wid: str, started: str, db: Callable[[], Any] = _db) -> None:
    while not stop.is_set():
        try:
            conn = db()
            with conn:
                conn.execute("INSERT INTO ops_worker (id, started_at, heartbeat_at) VALUES (?,?,?) ON CONFLICT(id)"
                             " DO UPDATE SET heartbeat_at=excluded.heartbeat_at",
                             (wid, started, datetime.utcnow().isoformat(timespec="seconds")))
            conn.close()
        except Exception:
            log.exception("heartbeat failed")
        stop.wait(HEARTBEAT_S)


def main(stop: threading.Event | None = None, install_signals: bool = True) -> int:
    from creative_intelligence.db import get_db_path, init_db
    from creative_intelligence.ingest.scheduler import acquire_lock, start_scheduler
    from creative_intelligence.webapp.startup import configure_logging
    configure_logging()
    stop = stop or threading.Event()
    if install_signals:
        signal.signal(signal.SIGTERM, lambda *a: stop.set())
    init_db()
    lock_path = str(get_db_path().parent / "worker.lock")
    lock = acquire_lock(lock_path)
    if lock is None:
        log.info("another worker holds %s; waiting", lock_path)
    while lock is None and not stop.wait(10):
        lock = acquire_lock(lock_path)
    if lock is None:
        return 0
    wid, started = jobs.worker_id(), datetime.utcnow().isoformat(timespec="seconds")
    conn = _db()
    try:
        rec = jobs.recover(conn)
        with conn:                                   # forget workers that stopped more than a day ago
            conn.execute("DELETE FROM ops_worker WHERE heartbeat_at < datetime('now', '-1 day')")
    finally:
        conn.close()
    log.info("worker %s started: %s interrupted run(s) closed, %s job(s) requeued, %s marked failed",
             wid, rec["runs_closed"], rec["requeued"], rec["failed"])
    start_scheduler(_db, str(get_db_path().parent / "scheduler.lock"))
    threads = [threading.Thread(target=heartbeat, args=(stop, wid, started), name="heartbeat", daemon=True)]
    threads += [threading.Thread(target=work_loop, args=(stop, wid), name=f"jobs-{i}", daemon=True)
                for i in range(CONCURRENCY)]
    for t in threads:
        t.start()
    stop.wait()
    log.info("worker %s stopping", wid)
    lock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
