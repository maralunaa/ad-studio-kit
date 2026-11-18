"""R5: the Monday research brief. Computed sections (new long-running competitor ads, angles gaining or fading, new
customer themes, our winners and fatigued ads by the playbook rule), then 6 to 10 idea-shaped suggestions from Claude
that approve into the bank in one click, and an optional Slack post. Claude and Slack are faked."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from creative_intelligence import knowledge
from creative_intelligence.adstudio import ideas, research
from creative_intelligence.db import get_connection, init_db


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("CI_DB_PATH", str(tmp_path / "data" / "t.db"))
    monkeypatch.chdir(tmp_path)
    init_db(tmp_path / "data" / "t.db")
    c = get_connection(tmp_path / "data" / "t.db")
    with c:
        c.execute("INSERT INTO adstudio_products (id, name, product_type) VALUES (1, 'Lychee', 'fruit')")
    monkeypatch.setattr("creative_intelligence.adstudio.costs.log_tokens", lambda *a, **k: 0.2)
    yield c
    c.close()


class _Claude:
    def __init__(self, body):
        self.body, self.sent = body, []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.sent.append(kw)
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(self.body))],
                               usage=SimpleNamespace(input_tokens=1, output_tokens=1, cache_read_input_tokens=0))


def _day(n: int) -> str:
    return (date(2026, 10, 19) - timedelta(days=n)).isoformat()


def _insights(conn, ad, name, days, ctr_early, ctr_late, cpa_early, cpa_late):
    with conn:
        for d in range(days, -1, -1):
            late = d < 7
            imp, ctr, cpa = 1000, ctr_late if late else ctr_early, cpa_late if late else cpa_early
            purchases = 2
            conn.execute("INSERT INTO ad_daily_insights (ad_id, date, ad_name, spend, impressions, clicks, purchases, revenue)"
                         " VALUES (?,?,?,?,?,?,?,?)", (ad, _day(d), name, cpa * purchases, imp, round(imp * ctr / 100),
                                                     purchases, 100.0))


def _idea_body(**kw):
    return {"title": "Arrives firm, ripe in 2 to 3 days", "angle": "tree_ripened_quality", "hook": "worry, then fact",
            "hook_type": knowledge.hook_type_keys()[0], "style": "ripening_timelapse", "products": ["Lychee"],
            "why": "88 reviews ask about ripeness", "customer_terms": ["ripe"], **kw}


def test_sections_find_long_runners_trends_themes_winners_and_fatigue(conn):
    research.add_manual(conn, {"page_name": "Coastal Fruit Co", "body": "Rare fruit", "started_on": (date.today() - timedelta(days=32)).isoformat()})
    research.add_manual(conn, {"page_name": "Kumo Berry", "body": "Berries", "started_on": (date.today() - timedelta(days=3)).isoformat()})
    with conn:
        conn.execute("UPDATE competitor_ads SET tagged_at='now', angle='gifting'")
        conn.execute("INSERT INTO research_briefs (created_at, brief_json) VALUES (?, ?)",
                     ((datetime.utcnow() - timedelta(days=7)).isoformat(timespec="seconds"),
                      json.dumps({"summary": "", "suggestions": [], "snapshot": {"angles": {"gifting": 0, "health": 3}}})))
        conn.execute("INSERT INTO voice_runs (kind, started_at, finished_at, status) VALUES ('extract', ?, ?, 'done')",
                     (datetime.utcnow().isoformat(), datetime.utcnow().isoformat()))
        conn.execute("INSERT INTO voice_themes (run_id, product_id, kind, theme, mentions) VALUES (1, 1, 'objection', 'Will it be ripe', 88)")
    _insights(conn, "w1", "Winner ad", 20, 2.0, 2.0, 20, 20)        # steady, ROAS 2.5
    _insights(conn, "f1", "Tired ad", 30, 2.0, 1.2, 20, 30)         # CTR -40%, CPA +50%: two trips
    s = research.brief_sections(conn)
    assert [a["brand"] for a in s["long_runners"]] == ["Coastal Fruit Co"]
    assert s["angles"]["gaining"][0]["angle"] == "gifting" and s["angles"]["fading"][0]["angle"] == "health"
    assert s["themes"][0]["theme"] == "Will it be ripe"
    assert {w["name"] for w in s["winners"]} == {"Winner ad", "Tired ad"}
    assert [f["name"] for f in s["fatigued"]] == ["Tired ad"] and len(s["fatigued"][0]["trips"]) == 2


def test_brief_writes_idea_shaped_suggestions_and_posts_to_slack(conn, monkeypatch):
    posted = []
    monkeypatch.setattr("creative_intelligence.alerts.notify_slack", lambda src, msg, level="error": posted.append((src, msg, level)) or True)
    fake = _Claude({"summary": "Ripeness is the gap.", "suggestions": [_idea_body(), _idea_body(title="Bad", style="nope")]})
    b = research.weekly_brief(conn, client=fake)
    assert [s["title"] for s in b["suggestions"]] == ["Arrives firm, ripe in 2 to 3 days"]
    assert b["sections"]["winners"] == [] and "snapshot" in research.latest_brief(conn)
    props = fake.sent[0]["output_config"]["format"]["schema"]["properties"]["suggestions"]["items"]["properties"]
    assert props["products"]["items"]["enum"] == ["Lychee"] and "farm_origin" not in props["angle"]["enum"]
    assert "never say where a fruit comes from" in fake.sent[0]["system"]
    assert posted[0][0] == "Monday research brief" and posted[0][2] == "info" and "Arrives firm" in posted[0][1]
    iid, _ = ideas.from_suggestion(conn, b["suggestions"][0], b["id"])
    i = ideas.get(conn, iid)
    assert i["status"] == "approved" and i["product_ids"] == [1] and i["evidence"]["customer_terms"] == ["ripe"]
    assert ideas.from_suggestion(conn, b["suggestions"][0], b["id"])[1] == "Already in the bank."
    monkeypatch.setenv("BRIEF_SLACK", "0")
    research.weekly_brief(conn, client=_Claude({"summary": "x", "suggestions": []}))
    assert len(posted) == 1


def test_due_on_mondays_when_there_is_something_to_read(conn):
    monday = datetime(2026, 10, 19, 7, 0)
    assert not research.brief_due(conn, now=monday)                          # nothing to read yet
    research.add_manual(conn, {"page_name": "Fruit Co", "body": "x"})
    with conn:
        conn.execute("UPDATE competitor_ads SET tagged_at='now'")
    assert research.brief_due(conn, now=monday)
    assert not research.brief_due(conn, now=monday + timedelta(days=1))      # Tuesday
    with conn:
        conn.execute("INSERT INTO research_briefs (created_at, brief_json) VALUES ('2026-10-19T06:00:00', '{}')")
    assert not research.brief_due(conn, now=monday)
    assert research.brief_due(conn, now=monday + timedelta(days=7))
