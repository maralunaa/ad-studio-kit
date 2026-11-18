"""K2d: Gallery and Results show the playbook's diagnosis hints (knowledge/metrics.yaml); hold rate uses true
15-second views when the read-only sync has them. Meta is never called: the fetch is mocked."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from creative_intelligence.adstudio import assets, diagnosis

ENOUGH = {"days": 9, "impressions": 12000}


def _r(**kw):
    return {**ENOUGH, "spend": 300, "purchases": 10, "cpa": 30, "roas": 4.0, "ctr": 1.2, **kw}


def test_each_pattern_gets_its_fix():
    h = diagnosis.hint(_r(hook_rate=12, hold_rate=8, hold_basis="15s"))
    assert h["key"] == "low_hook_low_hold" and "first 1–3 s" in h["hint"]
    h = diagnosis.hint(_r(hook_rate=38, hold_rate=8, hold_basis="15s"))
    assert h["hint"].startswith("High hook, low hold: tighten seconds 3–15")
    assert diagnosis.hint(_r(hook_rate=38, hold_rate=22, hold_basis="15s", ctr=0.6))["key"] == "good_hold_low_ctr"
    assert diagnosis.hint(_r(ctr=1.6, cpa=60))["key"] == "good_ctr_low_conversion"          # a static: no video signals
    assert diagnosis.hint(_r(hook_rate=38, hold_rate=22, hold_basis="15s", roas=2.1))["key"] == "all_strong_weak_roas"
    assert diagnosis.hint(_r(hook_rate=38, hold_rate=22, hold_basis="15s")) is None          # nothing to fix


def test_no_hint_before_the_minimum_read():
    assert diagnosis.hint({**_r(hook_rate=12, hold_rate=8, hold_basis="15s"), "days": 2}) is None
    assert diagnosis.hint({**_r(hook_rate=12, hold_rate=8, hold_basis="15s"), "impressions": 900}) is None
    assert diagnosis.hint(None) is None


def test_thruplay_hold_is_skipped_for_short_videos():
    r = _r(hook_rate=38, hold_rate=8, hold_basis="thruplay")
    assert "hold" not in diagnosis.levels(r, duration_s=12)            # ThruPlay = whole video when under 15 s
    assert diagnosis.levels(r, duration_s=30)["hold"] == "low"
    assert diagnosis.hint(r, 30)["key"] == "high_hook_low_hold" and diagnosis.hint(r, 12) is None


def test_hold_rate_uses_fifteen_second_views_when_there_are_some():
    m = assets.metrics({"impressions": 1000, "v3": 400, "thru": 200, "v15": 100})
    assert m["hold_rate"] == 25.0 and m["hold_basis"] == "15s" and m["hook_rate"] == 40.0
    m = assets.metrics({"impressions": 1000, "v3": 400, "thru": 200})
    assert m["hold_rate"] == 50.0 and m["hold_basis"] == "thruplay"


def test_sync_parses_fifteen_second_views():
    from creative_intelligence.ingest import daily_insights
    row = daily_insights.parse_row({"ad_id": "1", "date_start": "2026-10-19", "impressions": "100",
                                    "video_15_sec_watched_actions": [{"action_type": "video_view", "value": "42"}]})
    assert row["video_15s_views"] == 42 and "video_15s_views" in daily_insights.COLUMNS


def test_sync_asks_for_fifteen_second_views_and_carries_on_without_them(monkeypatch, tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    import fetch_insights
    asked = []

    def fake(url, params, label=""):
        asked.append(params["fields"])
        if "video_15_sec_watched_actions" in params["fields"]:
            raise SystemExit(1)                                        # api._get exits on an API error
        return [{"ad_id": "1"}]
    monkeypatch.setattr(fetch_insights, "paginated_get", fake)
    monkeypatch.setattr(fetch_insights, "base_url", lambda env, ep: "https://example.invalid/insights")
    monkeypatch.setenv("CI_RAW_DATA_DIR", str(tmp_path))
    fetch_insights.fetch_recent_daily({"META_ACCESS_TOKEN": "test"}, 3)
    assert len(asked) == 2 and "video_15_sec_watched_actions" in asked[0] and "video_15_sec" not in asked[1]
    assert list(tmp_path.glob("insights_daily_*.json"))                 # raw response saved, timestamped


@pytest.fixture
def conn(tmp_path, monkeypatch):
    from creative_intelligence.db import get_connection, init_db
    monkeypatch.setenv("CI_DB_PATH", str(tmp_path / "t.db"))
    init_db(tmp_path / "t.db")
    c = get_connection(tmp_path / "t.db")
    yield c
    c.close()


def test_results_groups_and_assets_carry_hints(conn):
    with conn:
        conn.execute("INSERT INTO adstudio_products (id, name, product_type) VALUES (1, 'Mango', 'fruit')")
        conn.execute("INSERT INTO adstudio_assets (asset_id, concept_id, product_id, source, source_id, angle, meta_ad_id)"
                     " VALUES ('EXB_A', 'H1', 1, 'composite', 3, 'gift', 'ad1')")
        for d in range(9):
            conn.execute("INSERT INTO ad_daily_insights (ad_id, date, spend, impressions, clicks, purchases, revenue,"
                         " video_3s_views, thruplays, video_15s_views) VALUES ('ad1', ?, 40, 2000, 30, 1, 120, 800, 300, 60)",
                         (f"2026-10-{d + 1:02d}",))
    row = assets.aggregate(conn, "angle", 1)[0]
    assert row["key"] == "gift" and row["hold_basis"] == "15s" and row["days"] == 9
    assert row["diagnosis"]["key"] == "high_hook_low_hold"                # hook 40%, hold 7.5%
    a = assets.assets_for(conn, "composite", [3])[3]
    assert a["results"]["diagnosis"]["key"] == "high_hook_low_hold"
