"""F8 — Admin overview (spend by provider and period, cost per delivered image per run and over
7 days, caps vs usage, volume, failed jobs, worker, last sync and backups), alerts (banner on every
page, optional Slack webhook) and structured logs. No live API."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

import pytest

from creative_intelligence import alerts
from creative_intelligence import db as D


@pytest.fixture
def env(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setenv("CI_DB_PATH", str(path))
    D._UP_TO_DATE.clear()
    D.init_db(path)
    factory = lambda: D.get_connection(path)
    monkeypatch.setattr("creative_intelligence.webapp.common.db", factory)
    from creative_intelligence.webapp.app import app
    return app.test_client(), factory


def test_dashboard_figures(env):
    client, factory = env
    c = factory()
    now = datetime.utcnow()
    with c:
        c.execute("INSERT INTO adstudio_products (id, name) VALUES (1, 'Mango')")
        for at, model, kind, usd in [(now, "google/nano-banana-pro", "image", 1.80), (now, "claude-opus-5-5", "qa-score", 0.30),
                                     (now - timedelta(days=5), "google/nano-banana-pro", "image", 2.00),
                                     (now - timedelta(days=20), "claude-sonnet-4-5", "copy", 0.50)]:
            c.execute("INSERT INTO model_calls (at, model, kind, units, cost_usd) VALUES (?,?,?,1,?)", (at.isoformat(), model, kind, usd))
        c.execute("INSERT INTO adstudio_runs (id, product_id, size, status, delivered, spent_usd, created_at, finished_at)"
                  " VALUES (1, 1, 8, 'done', 7, 2.38, ?, ?)", ((now - timedelta(days=2)).isoformat(), (now - timedelta(days=2)).isoformat()))
        c.execute("INSERT INTO adstudio_runs (id, product_id, size, status, delivered, spent_usd, created_at, finished_at)"
                  " VALUES (2, 1, 12, 'done', 10, 4.00, ?, ?)", (now.isoformat(), now.isoformat()))
        c.execute("INSERT INTO jobs (kind, status, run_after, created_at, finished_at, error) VALUES"
                  " ('backup_database', 'failed', ?, ?, ?, 'DriveError: folder missing')", (now.isoformat(),) * 3)
    d = client.get("/api/ops/dashboard").get_json()
    rep = d["spend"]["Replicate (images, video)"]
    assert rep["7 days"] == 3.8 and rep["30 days"] == 3.8
    assert d["spend"]["Anthropic (Claude)"]["30 days"] == 0.8 and d["spend_total"]["30 days"] == 4.6
    assert [r["per_image"] for r in d["runs"]] == [0.4, 0.34]
    assert d["per_image_7d"] == round(6.38 / 17, 3) and d["runs_7d"] == 2
    assert d["failed_jobs"][0]["label"] == "Database backup" and "folder missing" in d["failed_jobs"][0]["error"]
    assert d["budget"]["caps"]["text_day"] == 15 and "used_share" in d["volume"] and d["worker"]["alive"] is False


def test_alerts_go_to_slack_only_when_configured(env, monkeypatch):
    _, factory = env
    sent = []

    class R:
        status_code = 200
    import requests
    monkeypatch.setattr(requests, "post", lambda url, json=None, timeout=None: (sent.append((url, json)), R())[1])
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    alerts.raise_alert(factory(), "backup", "Nightly database backup failed")
    assert sent == []
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.test/T0/B0/x")
    alerts.raise_alert(factory(), "storage", "The data volume is 85% full", level="warn")
    assert sent[0][0] == "https://hooks.slack.test/T0/B0/x" and "storage: The data volume is 85% full" in sent[0][1]["text"]
    assert factory().execute("SELECT COUNT(*) FROM ops_alerts").fetchone()[0] == 2


def test_slack_failure_never_breaks_the_caller(env, monkeypatch):
    _, factory = env
    import requests
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.test/x")
    monkeypatch.setattr(requests, "post", lambda *a, **k: (_ for _ in ()).throw(OSError("offline")))
    assert alerts.raise_alert(factory(), "jobs", "Meta sync failed") > 0


def test_every_page_but_admin_carries_the_alert_banner(env):
    client, _ = env
    assert "alert-strip" in client.get("/products").get_data(as_text=True)
    assert "alert-strip" in client.get("/images/shots").get_data(as_text=True)
    assert "alert-strip" not in client.get("/admin").get_data(as_text=True).split("<main")[0]


def test_json_logs():
    from creative_intelligence.webapp.startup import JsonFormatter
    rec = logging.LogRecord("adstudio.worker", logging.INFO, __file__, 1, "job %s done", (7,), None)
    line = json.loads(JsonFormatter().format(rec))
    assert line["msg"] == "job 7 done" and line["level"] == "info" and line["logger"] == "adstudio.worker" and line["pid"]
