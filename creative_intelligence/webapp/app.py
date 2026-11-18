"""
Ad Studio — web app.

Pages: /products, /images/<stage>, /video/<stage>, /gallery/<stage> (routes/pages.py), /admin.
Shared-password login (webapp/auth.py). Meta access is read-only.
Routes live in webapp/routes/ (one blueprint per area); shared helpers in webapp/common.py.

On Railway:  gunicorn -c gunicorn.conf.py creative_intelligence.webapp.app:app   (see startup.py)
Locally:     PYTHONPATH=. python creative_intelligence/webapp/app.py  ->  http://localhost:5555/products
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from flask import Flask, jsonify

from creative_intelligence.budget import BudgetExceeded
from creative_intelligence.webapp import auth, common, restore, retired, startup
from creative_intelligence.webapp.routes import BLUEPRINTS

startup.configure_logging()
_HERE = Path(__file__).parent
app = Flask(__name__, template_folder=str(_HERE / "templates"))
auth.register(app)
retired.register(app)
restore.register(app)        # the copy route: 404 unless ALLOW_RESTORE=1, and ADMIN_TOKEN
for _bp in BLUEPRINTS:
    app.register_blueprint(_bp)

DISK_WARN_FREE = 0.20   # warn when less than 20% of the data volume is free


def health() -> tuple[dict[str, Any], int]:
    """Deep health check: the database answers, the data volume is writable (and how full it is), and the
    knowledge files load, validate and trace to the playbook."""
    from creative_intelligence.db import get_db_path
    checks: dict[str, str] = {}
    try:
        conn = common.db()
        conn.execute("SELECT COUNT(*) FROM adstudio_products").fetchone()
        conn.close()
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"failed: {type(exc).__name__}"
    data_dir = get_db_path().parent
    try:
        with tempfile.NamedTemporaryFile(dir=data_dir, prefix=".healthz-"):
            pass
        checks["volume_writable"] = "ok"
    except Exception as exc:
        checks["volume_writable"] = f"failed: {type(exc).__name__}"
    from creative_intelligence import knowledge
    kerr = knowledge.errors()
    checks["knowledge"] = "ok" if not kerr else f"failed: {kerr[0]}" + (f" (+{len(kerr) - 1} more)" if len(kerr) > 1 else "")
    ok = all(v == "ok" for v in checks.values())
    try:
        du = shutil.disk_usage(data_dir)
        checks["volume_free"] = f"{du.free / du.total:.0%}" + (" (low)" if du.free / du.total < DISK_WARN_FREE else "")
    except OSError:
        pass
    version = (os.getenv("RAILWAY_GIT_COMMIT_SHA") or "")[:7] or "local"
    return {"ok": ok, "checks": checks, "version": version}, (200 if ok else 503)


@app.errorhandler(BudgetExceeded)
def budget_exceeded(exc: BudgetExceeded) -> Any:
    """A paid call would pass a cap (budget.py): say so plainly; nothing was spent."""
    return jsonify({"error": str(exc), "budget": True}), 402


@app.get("/healthz")
def healthz() -> Any:
    body, code = health()
    return jsonify(body), code


if __name__ == "__main__":
    import threading
    from creative_intelligence import worker
    startup.once_per_boot()
    if os.getenv("LOCAL_WORKER", "1") == "1":     # the worker's lock keeps it to one even under the reloader
        threading.Thread(target=worker.main, kwargs={"install_signals": False}, name="worker", daemon=True).start()
    # Railway injects PORT; CI_WEB_PORT used locally. Debug only when PORT is not set externally.
    port = int(os.getenv("PORT", os.getenv("CI_WEB_PORT", "5555")))
    print(f"Ad Studio starting on http://localhost:{port}/products")
    app.run(host="0.0.0.0", port=port, debug=not os.getenv("PORT"))
