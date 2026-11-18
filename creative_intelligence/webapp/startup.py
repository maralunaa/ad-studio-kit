"""
Start-up work for the web, safe with several gunicorn workers.

  once_per_boot()  runs in the gunicorn master before any worker starts (gunicorn.conf.py
                   on_starting): brings the database schema up to date, once per boot.

Background work (image runs, scoring, Meta sync, backups) and the scheduler live in the worker
process, creative_intelligence/worker.py, which also closes runs a restart interrupted.
Locally (python app.py) the web runs a worker in a thread.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger("adstudio")


class JsonFormatter(logging.Formatter):
    """One JSON object per line (ts, level, logger, pid, msg, exc), so Railway's log search can filter on fields."""

    def format(self, record: logging.LogRecord) -> str:
        import json
        out = {"ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"), "level": record.levelname.lower(),
               "logger": record.name, "pid": record.process, "msg": record.getMessage()}
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, ensure_ascii=False)


def configure_logging() -> None:
    """Structured (JSON) lines on Railway or with LOG_FORMAT=json; readable text locally."""
    root = logging.getLogger()
    if root.handlers:
        return
    handler = logging.StreamHandler()
    fmt = os.getenv("LOG_FORMAT") or ("json" if os.getenv("RAILWAY_ENVIRONMENT") else "text")
    handler.setFormatter(JsonFormatter() if fmt == "json" else
                         logging.Formatter("%(asctime)s %(levelname)s %(name)s pid=%(process)d %(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def once_per_boot() -> list[int]:
    configure_logging()
    from creative_intelligence.db import get_connection, migrate
    conn = get_connection()
    try:
        done = migrate(conn)
    finally:
        conn.close()
    log.info("start-up: schema up to date in pid %s (applied now: %s)", os.getpid(), done or "none")
    return done
