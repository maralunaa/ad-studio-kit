"""T1 — automatic Meta sync: daily per-ad insights, scheduler, cron endpoint, freshness badge.

No live API: Meta rows are fixtures and the sync starter is faked.
"""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from creative_intelligence.adstudio import winning
from creative_intelligence.db import get_connection, init_db
from creative_intelligence.ingest import daily_insights as di
from creative_intelligence.ingest import meta_sync, scheduler


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "t.db"
    init_db(path)
    return lambda: get_connection(path)


def _row(ad="a1", day="2026-10-13", spend="50", purchases="2", revenue="180", **extra):
    r = {
        "ad_id": ad, "date_start": day, "date_stop": day, "campaign_id": "c", "adset_id": "s",
        "ad_name": "EXB mango", "spend": spend, "impressions": "1000", "clicks": "30", "ctr": "3.0",
        "actions": [{"action_type": "omni_purchase", "value": purchases},
                    {"action_type": "video_view", "value": "400"}],
        "action_values": [{"action_type": "omni_purchase", "value": revenue}],
        "video_thruplay_watched_actions": [{"action_type": "video_view", "value": "100"}],
    }
    r.update(extra)
    return r


# ── daily insights ───────────────────────────────────────────────────────
class TestDailyInsights:
    def test_parse_metrics(self):
        p = di.parse_row(_row())
        assert p["purchases"] == 2 and p["cpa"] == 25 and p["roas"] == pytest.approx(3.6)
        assert p["video_3s_views"] == 400 and p["thruplays"] == 100
        assert di.rates(p) == {"hook_rate": 40.0, "hold_rate": 25.0}

    def test_no_purchases_no_spend(self):
        p = di.parse_row(_row(spend="0", purchases="0", revenue="0"))
        assert p["cpa"] is None and p["roas"] is None

    def test_rerun_updates_instead_of_duplicating(self, db):
        conn = db()
        di.upsert(conn, [_row(day="2026-10-12"), _row(day="2026-10-13")])
        di.upsert(conn, [_row(day="2026-10-13", purchases="3"), _row(day="2026-10-14")])
        rows = conn.execute("SELECT date, purchases FROM ad_daily_insights ORDER BY date").fetchall()
        assert [(r["date"], r["purchases"]) for r in rows] == [
            ("2026-10-12", 2), ("2026-10-13", 3), ("2026-10-14", 2)]

    def test_ingest_uses_newest_raw_file(self, db, tmp_path):
        raw = tmp_path / "raw"
        raw.mkdir()
        (raw / "insights_daily_2026-10-13_050000.json").write_text(json.dumps([_row(purchases="1")]))
        (raw / "insights_daily_2026-10-14_050000.json").write_text(json.dumps([_row(purchases="4")]))
        conn = db()
        assert di.ingest_latest(conn, raw) == 1
        assert conn.execute("SELECT purchases FROM ad_daily_insights").fetchone()[0] == 4

    def test_sync_includes_daily_steps(self):
        argvs = [a for _, a in meta_sync.STEPS]
        assert ["src/fetch_insights.py", "--recent-daily", "3"] in argvs
        assert ["-m", "creative_intelligence.ingest.daily_insights"] in argvs
        assert argvs[-1] == ["-m", "creative_intelligence.adstudio.assets", "link"]


# ── scheduler ────────────────────────────────────────────────────────────
def _run(conn, started, status="done", trigger="manual"):
    with conn:
        conn.execute("INSERT INTO meta_sync_runs (started_at, finished_at, status, trigger)"
                     " VALUES (?,?,?,?)", (started, started, status, trigger))


class TestScheduler:
    NOW = datetime(2026, 10, 15, 6, 0)

    def test_not_before_sync_hour(self, db, monkeypatch):
        monkeypatch.setenv("SYNC_HOUR_UTC", "7")
        assert not scheduler.is_due(db(), self.NOW)

    def test_due_when_nothing_today(self, db):
        _run(db(), "2026-10-14T05:00:00")
        assert scheduler.is_due(db(), self.NOW)

    def test_once_per_day_even_after_failure(self, db):
        _run(db(), "2026-10-15T05:01:00", status="failed", trigger="scheduled")
        assert not scheduler.is_due(db(), self.NOW)

    def test_recent_manual_sync_counts(self, db):
        _run(db(), "2026-10-15T01:00:00")
        assert not scheduler.is_due(db(), self.NOW)

    def test_stuck_running_row_is_ignored(self, db):
        _run(db(), "2026-10-14T23:00:00", status="running")
        assert scheduler.is_due(db(), self.NOW)

    def test_tick_starts_scheduled_sync(self, db, monkeypatch):
        monkeypatch.setattr(scheduler, "is_due", lambda conn: True)
        calls = []
        rid = scheduler.tick(db, start=lambda f, trigger: (calls.append(trigger) or 7, ""))
        assert rid == 7 and calls == ["scheduled"]

    def test_disabled_by_default(self, db, monkeypatch, tmp_path):
        monkeypatch.delenv("SCHEDULER_ENABLED", raising=False)
        assert scheduler.start_scheduler(db, str(tmp_path / "l")) is False

    def test_lock_allows_only_one(self, tmp_path):
        path = str(tmp_path / "scheduler.lock")
        first = scheduler.acquire_lock(path)
        assert first is not None
        # flock is per open file description, so a second open in-process also fails
        assert scheduler.acquire_lock(path) is None
        first.close()
        assert scheduler.acquire_lock(path) is not None


# ── freshness badge ──────────────────────────────────────────────────────
class TestFreshness:
    NOW = datetime(2026, 10, 15, 12, 0)

    def test_fresh(self):
        f = winning.sync_freshness({"finished_at": "2026-10-15T05:10:00"},
                                   {"status": "done"}, self.NOW)
        assert f["sync_warning"] == "" and f["last_synced_at"] == "2026-10-15T05:10:00"

    def test_stale_after_7_days(self):
        f = winning.sync_freshness({"finished_at": "2026-10-07T05:00:00"},
                                   {"status": "done"}, self.NOW)
        assert "8 days ago" in f["sync_warning"]

    def test_failed_and_never(self):
        f = winning.sync_freshness(None, {"status": "failed"}, self.NOW)
        assert f["sync_warning"] == "never synced; last sync failed"

    def test_status_route_has_freshness(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CI_DB_PATH", str(tmp_path / "w.db"))
        from creative_intelligence.webapp import app as webapp
        st = webapp.app.test_client().get("/api/adstudio/patterns").get_json()["status"]
        assert st["sync_warning"] == "never synced"
        assert {"last_synced_at", "scheduler"} <= set(st)


# ── cron endpoint ────────────────────────────────────────────────────────
class TestCronEndpoint:
    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CI_DB_PATH", str(tmp_path / "w.db"))
        monkeypatch.setenv("ADMIN_TOKEN", "tok")
        monkeypatch.setenv("ADSTUDIO_PASSWORD", "pw")  # login on: endpoint must still work by token
        from creative_intelligence.webapp import app as webapp
        return webapp.app.test_client()

    def test_needs_token(self, client):
        assert client.post("/admin/meta-sync").status_code == 401
        assert client.post("/admin/meta-sync", headers={"X-Admin-Token": "no"}).status_code == 401

    def test_starts_cron_sync(self, client, monkeypatch):
        seen = {}
        monkeypatch.setattr(meta_sync, "start_sync",
                            lambda f, trigger="manual": (seen.setdefault("t", trigger) and 5, ""))
        r = client.post("/admin/meta-sync", headers={"X-Admin-Token": "tok"})
        assert r.status_code == 200 and r.get_json()["run_id"] == 5 and seen["t"] == "cron"
