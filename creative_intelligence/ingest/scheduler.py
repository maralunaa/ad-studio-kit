"""
Daily automatic Meta sync, inside the web app.

  SCHEDULER_ENABLED=1   turn it on (off by default, so local runs and tests never sync)
  SYNC_HOUR_UTC=5       earliest hour (UTC) the daily sync may start

A background thread wakes every few minutes and starts the existing sync once a
day. Only one scheduler runs even if the app has several processes (gunicorn
workers, Flask's debug reloader): the one holding an exclusive file lock next to the
DB schedules; the others keep trying the lock, so one takes over if the holder stops.

One scheduled attempt per day. If it fails, it is not retried until the next
day; the Hooks tab shows the failure and the Sync button still works.

It runs in the worker process (creative_intelligence/worker.py). Each tick also queues the
nightly database backup and the weekly image backup (creative_intelligence/backup.py) when due,
and checks how full the volume is.
"""
from __future__ import annotations

import fcntl
import logging
import os
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Callable, IO

log = logging.getLogger(__name__)

CHECK_EVERY_S = 300
_lock_handle: IO | None = None  # kept open for the process lifetime


def enabled() -> bool:
    return os.getenv("SCHEDULER_ENABLED") == "1"


def sync_hour() -> int:
    try:
        return max(0, min(23, int(os.getenv("SYNC_HOUR_UTC", "5"))))
    except ValueError:
        return 5


def is_due(conn: Any, now: datetime | None = None) -> bool:
    """True if today's scheduled sync should start now."""
    now = now or datetime.utcnow()
    if now.hour < sync_hour():
        return False
    today = now.strftime("%Y-%m-%d")
    tried_today = conn.execute(
        "SELECT 1 FROM meta_sync_runs WHERE trigger='scheduled' AND started_at >= ? LIMIT 1",
        (today,)).fetchone()
    if tried_today:
        return False
    # A sync (any trigger) still running or finished in the last 20h already covers today.
    # A 'running' row older than 2h is a run killed by a restart — ignore it.
    recent = conn.execute(
        "SELECT 1 FROM meta_sync_runs WHERE (status='running' AND started_at >= ?)"
        " OR (status='done' AND started_at >= ?) LIMIT 1",
        ((now - timedelta(hours=2)).isoformat(), (now - timedelta(hours=20)).isoformat())).fetchone()
    return not recent


def acquire_lock(path: str) -> IO | None:
    """Non-blocking exclusive lock. Returns the open handle, or None if another process has it."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fh = open(path, "a")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def tick(conn_factory: Callable[[], Any],
         start: Callable[..., tuple[int | None, str]] | None = None) -> int | None:
    """One check: start the scheduled sync if due. Returns the run id if one started."""
    from creative_intelligence.ingest import meta_sync
    start = start or meta_sync.start_sync
    conn = conn_factory()
    try:
        due = is_due(conn)
    finally:
        conn.close()
    if not due:
        return None
    run_id, reason = start(conn_factory, trigger="scheduled")
    if run_id is None:
        log.info("scheduled sync skipped: %s", reason)
    else:
        log.info("scheduled sync started: run %s", run_id)
    return run_id


def backup_tick(conn_factory: Callable[[], Any]) -> list[int]:
    """Queue the nightly database backup, the weekly image backup and the daily Shopify product sync
    when due; check the volume. Returns the ids of jobs queued."""
    from creative_intelligence import backup, jobs
    from creative_intelligence.adstudio import catalog
    queued = []
    conn = conn_factory()
    try:
        if catalog.is_due(conn):
            queued.append(catalog.queue(conn, "scheduled"))
        if backup.is_due(conn):
            queued.append(jobs.enqueue(conn, "backup_database", {"trigger": "scheduled"}, dedupe_key="backup_database"))
        if backup.images_due(conn):
            queued.append(jobs.enqueue(conn, "backup_images", {"trigger": "scheduled"}, dedupe_key="backup_images"))
        from creative_intelligence.adstudio import research
        if research.brief_due(conn):                 # Phase 2a: the weekly research brief
            queued.append(jobs.enqueue(conn, "research_brief", {"trigger": "scheduled"}, dedupe_key="research_brief"))
        from creative_intelligence.adstudio import market
        if market.scan_due(conn):                    # Phase 2a R1: the monthly market refresh
            queued.append(jobs.enqueue(conn, "research_scan", {"trigger": "scheduled"}, dedupe_key="research_scan"))
        from creative_intelligence.adstudio import voice
        if voice.refresh_due(conn):                  # Phase 2a R3: new reviews weekly, themes when enough are new
            queued.append(jobs.enqueue(conn, "voice", {"step": "refresh"}, dedupe_key="voice:refresh"))
        from creative_intelligence.adstudio import instagram
        if instagram.due(conn):                      # our own Instagram posts, daily (read only)
            queued.append(jobs.enqueue(conn, "ig_sync", {"trigger": "scheduled"}, dedupe_key="ig_sync"))
        from creative_intelligence.adstudio import swipe
        if swipe.propose_due(conn):                  # Phase 2a R6: monthly taste rubric proposals
            queued.append(jobs.enqueue(conn, "taste", {"trigger": "scheduled"}, dedupe_key="taste"))
    finally:
        conn.close()
    storage_check(conn_factory)
    return queued


def storage_check(conn_factory: Callable[[], Any]) -> bool:
    """Post one Admin alert when the volume is 80% full (until it is dismissed). True if posted."""
    from creative_intelligence.storage import storage
    u = storage().usage(detail=False)
    if not u["warn"]:
        return False
    conn = conn_factory()
    try:
        if conn.execute("SELECT 1 FROM ops_alerts WHERE source='storage' AND resolved_at IS NULL").fetchone():
            return False
        from creative_intelligence.alerts import raise_alert
        raise_alert(conn, "storage", f"The data volume is {u['used_share']:.0%} full ({u['free_bytes'] / 1e9:.1f} GB free). "
                    "Grow the volume in Railway or clear old images.", level="warn")
        return True
    finally:
        conn.close()


def start_scheduler(conn_factory: Callable[[], Any], lock_path: str) -> bool:
    """Start the scheduler thread if enabled. It only schedules (sync, backup) while this process holds the lock."""
    if not enabled():
        return False

    def loop() -> None:
        global _lock_handle
        waiting_logged = False
        while True:
            try:
                if _lock_handle is None:
                    _lock_handle = acquire_lock(lock_path)
                    if _lock_handle is not None:
                        log.info("Meta sync scheduler on in pid %s (daily from %02d:00 UTC)", os.getpid(), sync_hour())
                    elif not waiting_logged:
                        log.info("Meta sync scheduler standing by in pid %s: another process holds the lock", os.getpid())
                        waiting_logged = True
                if _lock_handle is not None:
                    tick(conn_factory)
                    backup_tick(conn_factory)
            except Exception:  # never let the thread die
                log.exception("scheduler tick failed")
            time.sleep(CHECK_EVERY_S)

    threading.Thread(target=loop, name="meta-sync-scheduler", daemon=True).start()
    return True
