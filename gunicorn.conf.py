"""gunicorn settings for Ad Studio on Railway: `gunicorn -c gunicorn.conf.py creative_intelligence.webapp.app:app`."""
import os

bind = f"0.0.0.0:{os.getenv('PORT', '5555')}"
workers = int(os.getenv("WEB_CONCURRENCY", "2"))
worker_class = "gthread"
threads = int(os.getenv("WEB_THREADS", "8"))
timeout = 300            # one image or AI ad is made inside a request and can take a few minutes
graceful_timeout = 30
accesslog = "-"
errorlog = "-"
# One JSON line per request, like the app's own logs (webapp/startup.py).
access_log_format = '{"ts":"%(t)s","kind":"access","method":"%(m)s","path":"%(U)s","status":%(s)s,"ms":%(M)s,"bytes":"%(B)s"}'


def on_starting(server):
    """Master process, before any worker exists: bring the schema up to date, once per boot.
    Background work and the scheduler run in the worker process (creative_intelligence/worker.py)."""
    from creative_intelligence.webapp.startup import once_per_boot
    once_per_boot()
