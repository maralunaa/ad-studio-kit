"""R1: the market. The brand registry (seeded with the brief's 17 brands in tiers A/B/C) and US keywords, the monthly
refresh through the official Ad Library API (no scraper), longevity stages, concepts with variant counts, adding an
ad by screenshot or landing page (Claude reads it; the operator saves), and monthly brand context for Tier A and B.
Meta and Claude are faked."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from creative_intelligence import budget
from creative_intelligence.adstudio import market, research
from creative_intelligence.db import get_connection, init_db


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("CI_DB_PATH", str(tmp_path / "data" / "t.db"))
    monkeypatch.setenv("CI_RAW_DATA_DIR", str(tmp_path / "raw"))
    monkeypatch.setattr("creative_intelligence.config.CI_RENDER_OUTPUT_DIR", str(tmp_path / "vol" / "renders"))
    monkeypatch.chdir(tmp_path)
    init_db(tmp_path / "data" / "t.db")
    c = get_connection(tmp_path / "data" / "t.db")
    yield c
    c.close()


class _Http:
    def __init__(self, body):
        self.body, self.calls = body, []

    def get(self, url, params=None, timeout=None):
        self.calls.append(params)
        return SimpleNamespace(json=lambda: self.body)


class _Claude:
    def __init__(self, *bodies):
        self.bodies, self.sent = list(bodies), []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.sent.append(kw)
        body = self.bodies.pop(0)
        text = body if isinstance(body, str) else json.dumps(body)
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=text)],
                               usage=SimpleNamespace(input_tokens=1000, output_tokens=200, cache_read_input_tokens=0,
                                                     server_tool_use=SimpleNamespace(web_search_requests=2)))


def _ago(days: int) -> str:
    return (date.today() - timedelta(days=days)).isoformat()


def test_the_brief_brands_and_keywords_are_seeded(conn):
    bs = market.brands(conn)
    assert len(bs) == 17 and [b["tier"] for b in bs].count("A") == 5 and [b["tier"] for b in bs].count("B") == 6
    assert "Island Fruit Box" in {b["name"] for b in bs}
    assert {k["term"] for k in market.keywords(conn)} >= {"exotic fruit", "cherimoya", "mango delivery"}
    mf = next(b for b in bs if b["name"] == "Coastal Fruit Co")
    assert "country=US" in mf["us_library_url"] and "q=Miami+Fruit" in mf["us_library_url"]
    market.save_brand(conn, {"id": mf["id"], "name": "Coastal Fruit Co", "tier": "A", "page_id": "123-45", "website": "coastalfruitco.example"})
    mf = next(b for b in market.brands(conn) if b["name"] == "Coastal Fruit Co")
    assert mf["us_library_url"].endswith("view_all_page_id=12345")
    assert market.save_brand(conn, {"name": "Coastal Fruit Co"})[1] == "That brand is already in the list."
    assert market.save_brand(conn, {"name": "X", "tier": "D"})[1] == "Tier is A, B or C."


def test_stages_and_days():
    assert [market.stage(d)[0] for d in (0, 13, 14, 29, 30, 89, 90)] == \
        ["new", "new", "traction", "traction", "proven", "proven", "evergreen"]
    assert market.days_running({"started_on": "2026-01-01", "ended_on": "2026-01-31"}) == 30
    assert market.days_running({"started_on": "2026-01-01", "active": 0, "last_seen_on": "2026-01-11"}) == 10
    assert market.days_running({"started_on": ""}) is None


def test_refresh_reads_brands_by_page_and_keywords_and_keeps_longevity(conn):
    b = next(x for x in market.brands(conn) if x["name"] == "Coastal Fruit Co")
    market.save_brand(conn, {**b, "page_id": "777"})
    ad = {"id": "1", "page_id": "777", "page_name": "Coastal Fruit Co", "ad_creative_bodies": ["Rare fruit, picked ripe."],
          "ad_creative_link_titles": ["Shop"], "ad_creative_link_captions": ["coastalfruitco.example"],
          "ad_delivery_start_time": _ago(100), "ad_snapshot_url": "https://x.invalid/1"}
    twin = dict(ad, id="2", ad_creative_bodies=["Rare fruit — picked RIPE!"])          # a variant of the same concept
    http = _Http({"data": [ad, twin]})
    out = market.scan(conn, "manual", http=http)
    assert out["status"] == "done" and out["added"] == 2 and out["searches"] == 17 + 6
    by_page = [p for p in http.calls if "search_page_ids" in p]
    assert len(by_page) == 1 and json.loads(by_page[0]["search_page_ids"]) == ["777"] and "search_terms" not in by_page[0]
    rows = market.listing(conn)
    assert rows[0]["brand"] == "Coastal Fruit Co" and rows[0]["tier"] == "A" and rows[0]["stage"] == "evergreen"
    assert rows[0]["variants"] == 2 and rows[0]["landing_url"] == "coastalfruitco.example"
    assert len(market.concepts(conn)) == 1 and market.listing(conn, stage_key="proven")
    ended = _Http({"data": [dict(ad, ad_delivery_stop_time=_ago(5))]})
    research.fetch_library(conn, "x", http=ended)
    r = conn.execute("SELECT active, ended_on FROM competitor_ads WHERE ad_archive_id='1'").fetchone()
    assert tuple(r) == (0, _ago(5))


def test_no_ad_library_access_stops_once_and_retries_daily(conn):
    out = market.scan(conn, http=_Http({"error": {"message": "no access"}}))
    assert out["status"] == "failed" and out["searches"] == 1 and "Ad Library API access" in out["errors"][0]
    assert not market.scan_due(conn)
    assert market.scan_due(conn, now=datetime.utcnow() + timedelta(days=2))


def test_add_by_screenshot_keeps_the_shot_and_never_saves_by_itself(conn, tmp_path):
    fake = _Claude({"page_name": "Meadow Produce", "body": "Try the Exotic Fruit Club.", "headline": "Join",
                    "cta": "Shop now", "started_on": "2026-07-01", "active": True, "variants": 4, "media_kind": "image"})
    got = market.read_screenshot(conn, b"\x89PNG fake", "image/png", client=fake)
    assert got["variants"] == 4 and got["media_path"].endswith(".png") and (tmp_path / "vol").exists()
    assert conn.execute("SELECT COUNT(*) FROM competitor_ads").fetchone()[0] == 0
    assert fake.sent[0]["messages"][0]["content"][0]["type"] == "image"
    rid, _ = research.add_manual(conn, {**got, "url": "https://www.facebook.com/ads/library/?id=998877"})
    row = dict(conn.execute("SELECT * FROM competitor_ads WHERE id=?", (rid,)).fetchone())
    assert row["ad_archive_id"] == "998877" and row["brand_id"] and row["media_path"] == got["media_path"]
    assert research.add_manual(conn, {**got, "url": "https://www.facebook.com/ads/library/?id=998877"})[1] == \
        "That Ad Library ad is already here."
    with pytest.raises(ValueError):
        market.read_screenshot(conn, b"x", "application/pdf", client=fake)


def test_media_paths_from_outside_the_research_area_are_dropped(conn, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("x")
    rid, _ = research.add_manual(conn, {"page_name": "Gift Plate", "body": "Gift food", "media_path": str(secret)})
    assert conn.execute("SELECT media_path FROM competitor_ads WHERE id=?", (rid,)).fetchone()[0] == ""


def test_landing_page_and_brand_context_use_web_tools_then_format(conn):
    fake = _Claude("Brand: Kumo Berry. Headline: The sweetest strawberry. Button: Shop.",
                   {"page_name": "Kumo Berry", "body": "The sweetest strawberry", "headline": "Omakase Berry", "cta": "Shop",
                    "started_on": "", "active": True, "variants": 1, "media_kind": "unknown"})
    got = market.read_landing(conn, "https://kumoberry.example/", client=fake)
    assert got["landing_url"] == "https://kumoberry.example/" and fake.sent[0]["tools"][0]["type"] == "web_fetch_20260209"
    assert "tools" not in fake.sent[1] and "output_config" in fake.sent[1]
    b = next(x for x in market.brands(conn) if x["name"] == "Kumo Berry")
    fake = _Claude("Offer: berries. Prices: $50. Guarantee: not stated.",
                   {"offer": "Omakase berries", "prices": "$50 a box", "guarantee": "not stated", "shipping": "2-day",
                    "positioning": "luxury", "sources": ["https://kumoberry.example/"]})
    ctx = market.brand_context(conn, b, client=fake)
    assert ctx["prices"] == "$50 a box" and fake.sent[0]["tools"][0]["allowed_domains"] == ["kumoberry.example"]
    search, fetch = fake.sent[0]["tools"]
    assert search["max_uses"] == 2 and fetch["max_uses"] == 2 and fetch["max_content_tokens"] == 6000   # cheap reads
    assert next(x for x in market.brands(conn) if x["name"] == "Kumo Berry")["context"]["offer"] == "Omakase berries"
    assert b["id"] not in {x["id"] for x in market.context_due(conn)}
    from datetime import datetime, timedelta
    assert b["id"] not in {x["id"] for x in market.context_due(conn, datetime.utcnow() + timedelta(days=60))}
    assert b["id"] in {x["id"] for x in market.context_due(conn, datetime.utcnow() + timedelta(days=91))}
    assert conn.execute("SELECT COUNT(*) FROM model_calls WHERE model='web_search'").fetchone()[0] == 2   # landing read + context


def test_the_monthly_job_stops_at_the_research_cap(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(budget, "check_research", lambda *a, **k: (_ for _ in ()).throw(budget.BudgetExceeded("cap")))
    out = market.refresh(lambda: get_connection(tmp_path / "data" / "t.db"), http=_Http({"data": []}))
    assert out["scan"]["status"] == "done" and out["stopped"] == "cap" and out["context"] == []


def test_scheduler_queues_the_monthly_refresh(conn, tmp_path, monkeypatch):
    from creative_intelligence.ingest import scheduler
    queued = []
    monkeypatch.setattr("creative_intelligence.jobs.enqueue", lambda c, kind, p, **k: queued.append(kind) or 1)
    monkeypatch.setattr(scheduler, "storage_check", lambda f: False)
    scheduler.backup_tick(lambda: get_connection(tmp_path / "data" / "t.db"))
    assert "research_scan" in queued


def test_routes(conn, tmp_path, monkeypatch):
    import creative_intelligence.webapp.app as app_module
    monkeypatch.setattr("creative_intelligence.webapp.common.db", lambda: get_connection(tmp_path / "data" / "t.db"))
    queued = []
    monkeypatch.setattr("creative_intelligence.jobs.enqueue", lambda c, kind, p, **k: queued.append(kind) or 7)
    c = app_module.app.test_client()
    d = c.get("/api/adstudio/market").get_json()
    assert len(d["brands"]) == 17 and d["scan"] is None and d["stages"][0]["key"] == "evergreen"
    assert c.post("/api/adstudio/market/refresh").get_json() == {"job_id": 7} and queued == ["research_scan"]
    assert c.post("/api/adstudio/market/brands", json={"name": "New Co", "tier": "B"}).status_code == 200
    assert c.post("/api/adstudio/market/keywords", json={"term": "rambutan"}).get_json() == {"ok": True}
    assert c.post("/api/adstudio/market/keywords", json={"term": "x"}).status_code == 400
    assert c.post("/api/adstudio/market/read", json={"landing_url": "nope"}).status_code == 400
    assert c.post("/api/adstudio/market/brand/1/context", json={}).get_json() == {"estimate": market.CONTEXT_ESTIMATE_USD}
    assert c.get("/api/adstudio/market/ad/1/media").status_code == 404
    assert b"loadResearchAdmin" in c.get("/admin").data and b"mkStage" in c.get("/ideas/market").data
