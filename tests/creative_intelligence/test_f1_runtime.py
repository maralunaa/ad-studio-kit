"""F1 — runtime: start-up work runs once under several gunicorn workers, the scheduler is taken
over when its holder stops, blueprints keep every route, and /healthz checks the database and volume."""
from __future__ import annotations

import time

import pytest

from creative_intelligence.db import get_connection, init_db
from creative_intelligence.ingest import scheduler


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setenv("CI_DB_PATH", str(path))
    init_db(path)
    monkeypatch.setattr("creative_intelligence.webapp.common.db", lambda: get_connection(path))
    return path


def test_once_per_boot_brings_the_schema_up_to_date(db):
    from creative_intelligence import db as D
    from creative_intelligence.webapp import startup
    startup.once_per_boot()
    c = get_connection(db)
    assert D.schema_version(c) == max(v for v, _, _ in D.migrations())


def test_gunicorn_master_migrates_and_web_workers_start_nothing():
    import importlib.util
    spec = importlib.util.spec_from_file_location("gconf", "gunicorn.conf.py")
    conf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(conf)
    assert conf.workers == 2 and conf.worker_class == "gthread"
    assert "once_per_boot" in conf.on_starting.__code__.co_names                 # master hook, once
    assert not hasattr(conf, "post_worker_init")                                  # scheduler lives in the worker


def test_standby_scheduler_takes_over_when_the_lock_frees(tmp_path, monkeypatch):
    monkeypatch.setenv("SCHEDULER_ENABLED", "1")
    monkeypatch.setattr(scheduler, "CHECK_EVERY_S", 0.05)
    monkeypatch.setattr(scheduler, "_lock_handle", None)
    ticks = []
    monkeypatch.setattr(scheduler, "tick", lambda f: ticks.append(1))
    monkeypatch.setattr(scheduler, "backup_tick", lambda f: None)
    path = str(tmp_path / "scheduler.lock")
    other = scheduler.acquire_lock(path)                  # "another worker" holds it
    assert scheduler.start_scheduler(lambda: None, path) is True
    time.sleep(0.3)
    assert ticks == []                                     # standing by
    other.close()                                          # that worker stops
    time.sleep(0.3)
    assert ticks                                           # this one took over
    scheduler._lock_handle.close()


def test_blueprints_keep_app_small_and_every_area_registered():
    from creative_intelligence.webapp.app import app
    assert len(open("creative_intelligence/webapp/app.py").read().splitlines()) < 300
    assert {"pages", "products", "images", "compose", "aiads", "gallery", "drive", "core", "results", "ops"} <= set(app.blueprints)
    rules = {r.rule for r in app.url_map.iter_rules()}
    assert {"/adstudio", "/admin", "/api/adstudio/gallery", "/api/adstudio/product/<int:pid>/runs", "/healthz"} <= rules


def test_healthz_is_deep_and_open(db):
    from creative_intelligence.webapp.app import app
    r = app.test_client().get("/healthz")
    d = r.get_json()
    assert r.status_code == 200 and d["ok"] and d["checks"]["database"] == "ok" and d["checks"]["volume_writable"] == "ok"


def test_healthz_fails_when_the_database_does(db, monkeypatch):
    from creative_intelligence.webapp.app import app

    def broken():
        raise RuntimeError("disk gone")
    monkeypatch.setattr("creative_intelligence.webapp.common.db", broken)
    r = app.test_client().get("/healthz")
    assert r.status_code == 503 and r.get_json()["checks"]["database"] == "failed: RuntimeError"


def test_no_model_names_hard_coded_outside_config():
    import re
    from pathlib import Path
    pat = re.compile(r"[\"'](claude-(opus|sonnet|haiku)[\w.-]*|google/nano-banana[\w-]*)[\"']")
    offenders = [f"{p}:{i}" for p in Path("creative_intelligence").rglob("*.py")
                 if "legacy" not in p.parts and p.name != "config.py"
                 for i, line in enumerate(p.read_text().splitlines(), 1) if pat.search(line)]
    assert offenders == []


# ── deploy gating ─────────────────────────────────────────────────────────
@pytest.fixture
def admin(db, monkeypatch):
    from creative_intelligence.webapp.app import app
    monkeypatch.setenv("ADMIN_TOKEN", "t0k")
    return app.test_client()


def test_smoke_needs_the_token_and_checks_pages(admin, db):
    from datetime import datetime
    assert admin.get("/admin/smoke").status_code == 401
    r = admin.get("/admin/smoke", headers={"X-Admin-Token": "t0k"})
    assert r.status_code == 500 and "no check-in" in str(r.get_json()["checks"]["worker"])   # no worker yet
    c = get_connection(db)
    with c:
        now = datetime.utcnow().isoformat(timespec="seconds")
        c.execute("INSERT INTO ops_worker (id, started_at, heartbeat_at) VALUES ('w:1', ?, ?)", (now, now))
    r = admin.get("/admin/smoke", headers={"X-Admin-Token": "t0k"})
    d = r.get_json()
    assert r.status_code == 200 and d["ok"] and d["checks"]["/products"] == 200 and d["checks"]["/images/shots"] == 200 and d["checks"]["/admin"] == 200


def test_alert_shows_on_admin_until_dismissed(admin):
    assert admin.post("/admin/alert", json={"message": "x"}).status_code == 401
    aid = admin.post("/admin/alert", json={"message": "Deploy of abc1234 failed"},
                     headers={"X-Admin-Token": "t0k"}).get_json()["id"]
    assert [a["message"] for a in admin.get("/api/ops/alerts").get_json()["alerts"]] == ["Deploy of abc1234 failed"]
    admin.post(f"/api/ops/alerts/{aid}/dismiss")
    assert admin.get("/api/ops/alerts").get_json()["alerts"] == []


def _load_smoke():
    import importlib.util
    spec = importlib.util.spec_from_file_location("deploy_smoke", "scripts/deploy_smoke.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_failed_smoke_rolls_back_to_the_last_good_commit_and_alerts(monkeypatch):
    m = _load_smoke()
    monkeypatch.setenv("RAILWAY_TOKEN", "proj")
    for k in ("RAILWAY_PROJECT_ID", "RAILWAY_ENVIRONMENT_ID", "RAILWAY_SERVICE_ID"):
        monkeypatch.setenv(k, "id")
    calls, live = [], {"version": "bad0000"}

    def fake_http(method, url, body=None, headers=None, timeout=60):
        calls.append((method, url, body, headers))
        if url.endswith("/healthz"):
            return 200, {"ok": True, "version": live["version"]}
        if url.endswith("/admin/smoke"):
            return 500, {"ok": False, "checks": {"/adstudio": 500, "/admin": 200}}
        if url == m.RAILWAY_API and "deployments(" in body["query"]:
            assert headers == {"Project-Access-Token": "proj"}
            return 200, {"data": {"deployments": {"edges": [
                {"node": {"id": "d-bad", "canRollback": True, "meta": {"commitHash": "bad0000ffff"}}},
                {"node": {"id": "d-good", "canRollback": True, "meta": {"commitHash": "good000ffff"}}}]}}}
        if url == m.RAILWAY_API:
            assert body["variables"] == {"id": "d-good"}
            live["version"] = "good000"
            return 200, {"data": {"deploymentRollback": True}}
        return 200, {"id": 1}
    monkeypatch.setattr(m, "http", fake_http)
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    monkeypatch.setattr("sys.argv", ["x", "--url", "https://v2", "--sha", "bad0000ffff", "--head-sha", "bad0000ffff", "--no-dry-run"])
    assert m.main() == 1
    alert = [c for c in calls if c[1].endswith("/admin/alert")][0][2]["message"]
    assert "bad0000 failed the smoke check" in alert and '"/adstudio": 500' in alert and "Rolled back to good000." in alert


def test_dry_run_is_the_default_and_never_rolls_back(monkeypatch):
    m = _load_smoke()
    seen = []
    monkeypatch.setattr(m, "http", lambda method, url, *a, **k: (seen.append(url), (500, {}))[1])
    monkeypatch.setattr("sys.argv", ["x", "--url", "https://v2", "--sha", "bad0000", "--state", "failure"])
    assert m.main() == 1 and seen == ["https://v2/healthz"]   # no Railway call, no alert


def test_an_older_commit_is_never_rolled_back(monkeypatch):
    m = _load_smoke()
    seen = []
    monkeypatch.setattr(m, "http", lambda method, url, *a, **k: (seen.append(url), (200, {}))[1])
    monkeypatch.setattr("sys.argv", ["x", "--url", "https://v2", "--sha", "old0000", "--state", "failure",
                                     "--head-sha", "new0000", "--no-dry-run"])
    assert m.main() == 1 and seen == ["https://v2/healthz", "https://v2/admin/alert"]


def test_a_failed_build_never_rolls_back_a_healthy_app(monkeypatch):
    """2026-10-22: a build failed during Railway's outage and the rollback swapped the running (newer) app for an
    older snapshot with old variables. A failed build never goes live, so only an alert is posted."""
    m = _load_smoke()
    seen = []
    monkeypatch.setattr(m, "http", lambda method, url, body=None, *a, **k: (seen.append((url, body)),
                                                                             (200, {"ok": True, "version": "local"}))[1])
    monkeypatch.setattr("sys.argv", ["x", "--url", "https://v2", "--sha", "new0000", "--state", "failure",
                                     "--head-sha", "new0000", "--no-dry-run"])
    assert m.main() == 1
    assert [u for u, _ in seen] == ["https://v2/healthz", "https://v2/admin/alert"]
    assert "keeps running local" in seen[1][1]["message"] and seen[1][1]["level"] == "warn"


def test_a_passing_deploy_confirms_rollback_is_ready(monkeypatch, capsys):
    m = _load_smoke()
    monkeypatch.setenv("RAILWAY_TOKEN", "proj")
    for k in ("RAILWAY_PROJECT_ID", "RAILWAY_ENVIRONMENT_ID", "RAILWAY_SERVICE_ID"):
        monkeypatch.setenv(k, "id")
    calls = []

    def fake_http(method, url, body=None, headers=None, timeout=60):
        calls.append(url)
        if url.endswith("/healthz"):
            return 200, {"ok": True, "version": "new0000"}
        if url.endswith("/admin/smoke"):
            return 200, {"ok": True, "checks": {"/adstudio": 200}}
        if url == m.RAILWAY_API:
            assert "mutation" not in body["query"]                    # read-only
            return 200, {"data": {"deployments": {"edges": [
                {"node": {"id": "d1", "canRollback": True, "meta": {"commitHash": "new0000ffff"}}},
                {"node": {"id": "d0", "canRollback": True, "meta": {"commitHash": "old0000ffff"}}}]}}}
        return 200, {}
    monkeypatch.setattr(m, "http", fake_http)
    monkeypatch.setattr("sys.argv", ["x", "--url", "https://v2", "--sha", "new0000ffff", "--no-dry-run"])
    assert m.main() == 0
    assert "rollback ready: last good deployment before this one is old0000" in capsys.readouterr().out
    assert not any(c.endswith("/admin/alert") for c in calls)


def test_a_broken_token_is_flagged_after_a_passing_deploy(monkeypatch):
    m = _load_smoke()
    monkeypatch.setenv("RAILWAY_TOKEN", "bad")
    for k in ("RAILWAY_PROJECT_ID", "RAILWAY_ENVIRONMENT_ID", "RAILWAY_SERVICE_ID"):
        monkeypatch.setenv(k, "id")
    posted = []

    def fake_http(method, url, body=None, headers=None, timeout=60):
        if url.endswith("/healthz"):
            return 200, {"ok": True, "version": "new0000"}
        if url.endswith("/admin/smoke"):
            return 200, {"ok": True, "checks": {}}
        if url == m.RAILWAY_API:
            return 200, {"errors": [{"message": "Not Authorized"}]}
        posted.append(body)
        return 200, {"id": 1}
    monkeypatch.setattr(m, "http", fake_http)
    monkeypatch.setattr("sys.argv", ["x", "--url", "https://v2", "--sha", "new0000ffff", "--no-dry-run"])
    assert m.main() == 0                                              # the deploy itself is fine
    assert posted[0]["level"] == "warn" and "Not Authorized" in posted[0]["message"]


def test_requests_carry_a_named_user_agent(monkeypatch):
    """Railway's firewall answers Python's default user agent with 403 (Cloudflare 1010)."""
    m = _load_smoke()
    seen = {}

    class Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b"{}"
    def fake_urlopen(req, timeout=60):
        seen.update(req.headers)
        return Resp()
    monkeypatch.setattr(m.urllib.request, "urlopen", fake_urlopen)
    m.http("POST", m.RAILWAY_API, {"query": "{ me { name } }"}, {"Project-Access-Token": "t"})
    assert seen["User-agent"] == "ad-studio-deploy-smoke/1.0" and seen["Project-access-token"] == "t"


def test_failed_deployments_are_never_rollback_targets(monkeypatch):
    m = _load_smoke()
    monkeypatch.setenv("RAILWAY_TOKEN", "proj")
    for k in ("RAILWAY_PROJECT_ID", "RAILWAY_ENVIRONMENT_ID", "RAILWAY_SERVICE_ID"):
        monkeypatch.setenv(k, "id")
    sent = []

    def fake_http(method, url, body=None, headers=None, timeout=60):
        sent.append(body)
        if "mutation" in body["query"]:
            return 200, {"data": {"deploymentRollback": True}}
        assert "status" not in body["variables"]["input"]              # Railway rejects that filter
        return 200, {"data": {"deployments": {"edges": [
            {"node": {"id": "bad", "status": "SUCCESS", "canRollback": True, "meta": {"commitHash": "bad0000"}}},
            {"node": {"id": "crashed", "status": "CRASHED", "canRollback": True, "meta": {"commitHash": "mid0000"}}},
            {"node": {"id": "good", "status": "REMOVED", "canRollback": True, "meta": {"commitHash": "good000"}}}]}}}
    monkeypatch.setattr(m, "http", fake_http)
    assert m.rollback("bad0000") == "good000"
    assert sent[-1]["variables"] == {"id": "good"}
