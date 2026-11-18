"""F5 — provider adapters, model registry, and a budget check before every paid call:
generation (images + video) per run and per day, Claude text per day, tagging never blocked but
alerted. Days start 00:00 London. No live API: clients are faked."""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from creative_intelligence import budget
from creative_intelligence import db as D


@pytest.fixture
def conn(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setenv("CI_DB_PATH", str(path))
    D._UP_TO_DATE.clear()
    D.init_db(path)
    monkeypatch.setattr("creative_intelligence.webapp.common.db", lambda: D.get_connection(path))
    c = D.get_connection(path)
    yield c
    c.close()


def spend(c, kind, usd, at=None):
    with c:
        c.execute("INSERT INTO model_calls (at, model, kind, units, cost_usd) VALUES (?, 'm', ?, 1, ?)",
                  (at or datetime.utcnow().isoformat(), kind, usd))


def test_day_starts_at_midnight_lisbon():
    assert budget.day_start_utc(datetime(2026, 10, 16, 12, 0)) == "2026-10-15T23:00:00"   # summer time, UTC+1
    assert budget.day_start_utc(datetime(2026, 12, 16, 12, 0)) == "2026-12-16T00:00:00"   # winter, UTC+0
    assert budget.day_start_utc(datetime(2026, 10, 15, 23, 30)) == "2026-10-15T23:00:00"  # already "tomorrow" in London (sample brand timezone)


def test_spend_is_split_into_buckets(conn):
    spend(conn, "image", 1.0)
    spend(conn, "video", 0.5)
    spend(conn, "qa-score", 0.2)
    spend(conn, "copy", 0.1)
    spend(conn, "tagging", 3.0)
    spend(conn, "image", 9.0, at="2020-01-01T00:00:00")           # another day
    assert {b: budget.spent_today(conn, b) for b in ("generation", "video", "text", "tagging")} == \
        {"generation": 1.0, "video": 0.5, "text": 0.3, "tagging": 3.0}           # video has its own caps (Phase 2b)


def test_caps_default_from_config_and_are_editable(conn):
    assert budget.caps(conn) == {"generation_run": 10.0, "generation_day": 50.0, "video_job": 25.0, "video_day": 60.0,
                                 "text_day": 15.0, "tagging_alert_day": 5.0, "research_month": 10.0}   # + Phase 2a cap
    budget.set_cap(conn, "text_day", 20)
    assert budget.caps(conn)["text_day"] == 20
    with pytest.raises(ValueError):
        budget.set_cap(conn, "text_day", -1)
    with pytest.raises(ValueError):
        budget.set_cap(conn, "everything", 1)


def test_check_blocks_text_and_generation_but_never_tagging(conn):
    spend(conn, "copy", 14.99)
    budget.check(conn, "text")
    with pytest.raises(budget.BudgetExceeded, match="Daily cap reached — resets at 00:00 London. Admin can raise it."):
        budget.check(conn, "text", estimate=0.02)
    spend(conn, "image", 49.9)
    with pytest.raises(budget.BudgetExceeded):
        budget.check(conn, "generation", estimate=0.18)
    spend(conn, "tagging", 500)
    budget.check(conn, "tagging", estimate=100)                    # never blocked


class FakeMessages:
    def __init__(self):
        self.calls = 0

    def create(self, **kw):
        self.calls += 1
        return SimpleNamespace(usage=SimpleNamespace(input_tokens=1000, output_tokens=100))


def test_claude_calls_stop_at_the_text_cap_before_reaching_claude(conn):
    from creative_intelligence.adstudio.costs import LoggedClient
    inner = SimpleNamespace(messages=FakeMessages(), beta=SimpleNamespace(messages=FakeMessages()))
    client = LoggedClient(inner, "copy", log=False)
    client.beta.messages.create(model="claude-opus-5-5")
    spend(conn, "copy", 15.0)
    with pytest.raises(budget.BudgetExceeded):
        client.beta.messages.create(model="claude-opus-5-5")
    assert inner.beta.messages.calls == 1                          # the second call never went out


def test_tagging_is_logged_never_blocked_and_alerts_once_a_day(conn):
    from creative_intelligence.adstudio.costs import LoggedClient
    spend(conn, "tagging", 5.5)
    inner = SimpleNamespace(messages=FakeMessages(), beta=SimpleNamespace(messages=FakeMessages()))
    client = LoggedClient(inner, "tagging")
    client.beta.messages.create(model="claude-opus-5")
    client.beta.messages.create(model="claude-opus-5")
    assert inner.beta.messages.calls == 2
    alerts = conn.execute("SELECT message FROM ops_alerts WHERE source='tagging'").fetchall()
    assert len(alerts) == 1 and "above the $5.00 alert level" in alerts[0][0]


def test_image_generation_stops_before_calling_replicate(conn, monkeypatch):
    import sys
    from creative_intelligence import config
    monkeypatch.setattr(config, "CI_REPLICATE_API_KEY", "r8_test")
    called = []
    monkeypatch.setitem(sys.modules, "replicate", SimpleNamespace(Client=lambda **k: called.append(1)))
    spend(conn, "image", 49.95)
    from creative_intelligence.providers import replicate
    with pytest.raises(budget.BudgetExceeded):
        replicate.generate_image("a mango", [], "4:5")
    assert called == []


def test_api_answers_402_with_the_cap_message(conn):
    from creative_intelligence.webapp import app as app_module
    with app_module.app.test_request_context("/api/adstudio/image/1/run"):
        body, code = app_module.budget_exceeded(budget.BudgetExceeded(budget.CAP_MESSAGE))
    assert code == 402 and body.get_json() == {"error": budget.CAP_MESSAGE, "budget": True}
    assert app_module.app.error_handler_spec[None][None][budget.BudgetExceeded] is app_module.budget_exceeded


def test_caps_and_models_on_admin(conn):
    from creative_intelligence.webapp.app import app
    client = app.test_client()
    assert client.post("/api/ops/caps", json={"key": "generation_day", "usd": "75"}).get_json()["caps"]["generation_day"] == 75
    assert client.post("/api/ops/caps", json={"key": "generation_day", "usd": "lots"}).status_code == 400
    d = client.get("/api/ops/costs").get_json()
    assert d["budget"]["caps"]["generation_day"] == 75 and d["caps"]["day_usd"] == 75
    roles = {m["role"]: m for m in d["models"]}
    assert roles["video"]["model"] == "kwaivgi/kling-v3-video" and roles["video"]["price_checked_at"] is None
    assert roles["tag"]["bucket"] == "tagging" and roles["image"]["bucket"] == "generation"


def test_video_proof_is_a_dry_run_by_default(conn, capsys):
    from creative_intelligence.providers import video_proof
    with conn:
        conn.execute("INSERT INTO adstudio_products (id, name, product_type) VALUES (1, 'Honey Mango', 'fruit')")
    assert video_proof.main(["--product", "1"]) == 0
    out = capsys.readouterr().out
    assert "estimated $0.84" in out and "[dry run] nothing generated" in out
