"""R: Phase 2a research. Competitor ads from the Meta Ad Library (read-only, mocked) or added by hand, tagged with our
own taxonomy, patterns and gaps, the weekly research brief (styles, not only angles) under a monthly research cap,
and the Page comments import into the real reviews library. No live API: Meta and Claude are faked."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from creative_intelligence import budget
from creative_intelligence.adstudio import research, reviews
from creative_intelligence.db import get_connection, init_db


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("CI_DB_PATH", str(tmp_path / "data" / "t.db"))
    monkeypatch.setenv("CI_RAW_DATA_DIR", str(tmp_path / "raw"))
    monkeypatch.chdir(tmp_path)                                  # outputs/api.log lands here
    init_db(tmp_path / "data" / "t.db")
    c = get_connection(tmp_path / "data" / "t.db")
    with c:
        c.execute("INSERT INTO adstudio_products (id, name, product_type) VALUES (1, 'Lychee', 'fruit')")
    yield c
    c.close()


class _Http:
    def __init__(self, body):
        self.body, self.calls = body, []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        return SimpleNamespace(json=lambda: self.body)


class _Claude:
    def __init__(self, body):
        self.body, self.sent = body, []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.sent.append(kw)
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(self.body))],
                               usage=SimpleNamespace(input_tokens=1000, output_tokens=200, cache_read_input_tokens=0))


def test_ad_library_is_read_only_logged_and_saved(conn, tmp_path):
    http = _Http({"data": [{"id": "111", "page_name": "Fruit Co", "ad_creative_bodies": ["Mangoes picked ripe."],
                            "ad_creative_link_titles": ["Shop mangoes"], "ad_delivery_start_time": "2026-09-01T00:00:00",
                            "ad_snapshot_url": "https://example.invalid/snap", "publisher_platforms": ["facebook"]}]})
    out = research.fetch_library(conn, "mango delivery", http=http)
    assert out == {"added": 1, "updated": 0, "seen": 1}
    url, params = http.calls[0]
    assert url.endswith("/ads_archive") and params["search_terms"] == "mango delivery" and "DE" in params["ad_reached_countries"]
    assert research.fetch_library(conn, "mango delivery", http=http) == {"added": 0, "updated": 1, "seen": 1}  # no duplicates
    assert list((tmp_path / "raw").glob("ad_library_*.json")) and "ads_archive" in (tmp_path / "outputs" / "api.log").read_text()
    assert "Ad Library API access" in research.fetch_library(conn, "x", http=_Http({"error": {"message": "no"}}))["error"]


def test_manual_add(conn):
    assert "page name" in research.add_manual(conn, {"page_name": "", "body": "x"})[1]
    rid, _ = research.add_manual(conn, {"page_name": "Fruit Co", "body": "Stop buying mangoes at the store.", "seen_on": "2026-10-01"})
    assert research.listing(conn)[0]["id"] == rid


def test_tagging_uses_our_taxonomy_and_the_research_cap(conn, monkeypatch):
    research.add_manual(conn, {"page_name": "Fruit Co", "body": "Stop buying mangoes at the store."})
    research.add_manual(conn, {"page_name": "Berry Inc", "body": "Watch our berries ripen."})
    more = {"format": "comparison", "offer_type": "no_offer", "product_type": "single_fruit",
            "visual_style": "macro_closeup", "why_it_works": "shows the difference"}
    fake = _Claude({"ads": [{"id": 1, "style": "us_vs_supermarket", "hook_type": "contrarian", "angle": "tree_ripened_quality", **more},
                            {"id": 2, "style": "ripening_timelapse", "hook_type": "curiosity", "angle": "flavor_experience", **more}]})
    assert research.tag(conn, client=fake) == {"tagged": 2}
    schema = fake.sent[0]["output_config"]["format"]["schema"]["properties"]["ads"]["items"]["properties"]
    assert "claymation" in schema["style"]["enum"] and "contrarian" in schema["hook_type"]["enum"]
    row = conn.execute("SELECT style, family, hook_type FROM competitor_ads WHERE id=1").fetchone()
    assert tuple(row) == ("us_vs_supermarket", "product_real", "contrarian")
    p = research.patterns(conn)
    assert p["style"]["theirs"] == {"us_vs_supermarket": 1, "ripening_timelapse": 1}
    assert "claymation" in p["unused_by_competitors"] and "us_vs_supermarket" in p["they_use_we_dont"]
    with conn:
        conn.execute("INSERT INTO model_calls (at, model, kind, units, cost_usd, source, source_id) VALUES (?, 'm', 'copy', 1, 10, 'market_research', 0)",
                     (datetime.utcnow().isoformat(),))
    research.add_manual(conn, {"page_name": "X", "body": "y"})
    with pytest.raises(budget.BudgetExceeded, match="Research cap"):
        research.tag(conn, client=fake)


def test_page_comments_import_into_the_real_library(conn, monkeypatch):
    assert "META_PAGE_ID" in research.import_page_comments(conn)["error"]
    monkeypatch.setenv("META_PAGE_ID", "123")
    http = _Http({"data": [{"comments": {"data": [
        {"message": "Is it worth $54?", "created_time": "2026-10-02T10:00:00+0000", "from": {"name": "Jo Smith"}},
        {"message": "ok", "created_time": "2026-10-02T11:00:00+0000"}]}}]})
    assert research.import_page_comments(conn, http=http) == {"added": 1}
    assert research.import_page_comments(conn, http=http) == {"added": 0}                 # no duplicates
    c = reviews.listing(conn, None, "comment")[0]
    assert c["text"] == "Is it worth $54?" and c["shown_as"] == "J." and c["source"] == "meta_comment"


def test_research_routes(conn, tmp_path, monkeypatch):
    import creative_intelligence.webapp.app as app_module
    monkeypatch.setattr("creative_intelligence.webapp.common.db", lambda: get_connection(tmp_path / "data" / "t.db"))
    queued = []
    monkeypatch.setattr("creative_intelligence.jobs.enqueue", lambda c, kind, p, **k: queued.append(kind) or 1)
    c = app_module.app.test_client()
    assert c.post("/api/adstudio/research/ads", json={"page_name": "Fruit Co", "body": "Ripe mangoes"}).status_code == 200
    d = c.get("/api/adstudio/research").get_json()
    assert d["ads"][0]["page_name"] == "Fruit Co" and d["cap_month"] == 10 and d["brief"] is None
    assert c.post("/api/adstudio/research/run").get_json()["job_id"] == 1 and queued == ["research_brief"]
    assert c.get("/gallery/research").headers["Location"].endswith("/ideas/market")      # moved to Ideas (2a)
    assert c.get("/ideas/market").status_code == 200
