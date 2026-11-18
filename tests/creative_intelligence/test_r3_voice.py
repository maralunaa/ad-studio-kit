"""R3: customer voice. Real reviews imported from Fera (site and Google), Trustpilot/BBB/testimonials (Claude reads the
public pages, words exact, curated marked) and CSV, never twice; themes counted in code from the reviews they were
found in, phrases kept only when really in the review; objections become angles only with an approved fact.
Fera and Claude are faked."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from creative_intelligence import budget
from creative_intelligence.adstudio import ideas, reviews, voice
from creative_intelligence.db import get_connection, init_db


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("CI_DB_PATH", str(tmp_path / "data" / "t.db"))
    monkeypatch.chdir(tmp_path)
    init_db(tmp_path / "data" / "t.db")
    c = get_connection(tmp_path / "data" / "t.db")
    with c:
        c.execute("INSERT INTO adstudio_products (id, name, product_type) VALUES (1, 'Honey Mango', 'fruit')")
    yield c
    c.close()


class _Http:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(params)
        body = self.pages[params["page"] - 1]
        return SimpleNamespace(json=lambda: body)


class _Claude:
    def __init__(self, *bodies):
        self.bodies, self.sent = list(bodies), []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.sent.append(kw)
        body = self.bodies.pop(0)
        return SimpleNamespace(stop_reason="end_turn",
                               content=[SimpleNamespace(type="text", text=body if isinstance(body, str) else json.dumps(body))],
                               usage=SimpleNamespace(input_tokens=1000, output_tokens=200, cache_read_input_tokens=0,
                                                     server_tool_use=SimpleNamespace(web_search_requests=1)))


def _fera(n, start=0):
    return [{"id": f"r{i}", "created_at": "2026-09-0%dT10:00:00Z" % (1 + i % 9), "rating": 5.0, "heading": None,
             "body": f"Review number {i}: the mango arrived perfectly ripe.", "customer": {"display_name": "Sam K."},
             "channel_code": "google" if i % 2 else "web", "product": {"name": "Honey Mango"}}
            for i in range(start, start + n)]


def test_fera_import_is_incremental_and_never_twice(conn):
    http = _Http([{"data": _fera(3), "meta": {"page_count": 1}}])
    assert voice.import_fera(conn, http=http, pause=0) == {"added": 3, "seen": 3}
    assert voice.import_fera(conn, http=http, pause=0) == {"added": 0, "seen": 3}
    r = reviews.listing(conn, 1)
    assert len(r) == 3 and {x["source"] for x in r} == {"google", "shopify"} and r[0]["rating"] == 5
    assert http.calls[0]["public_key"].startswith("pk_")


def test_web_imports_keep_exact_dated_words_and_mark_testimonials_curated(conn):
    fake = _Claude("notes", {"reviews": [
        {"author": "Linda P", "posted_on": "2026-08-02", "rating": 5, "text": "Best fruit box ever.", "url": "https://t/1"},
        {"author": "No Date", "posted_on": "", "rating": 4, "text": "Lovely.", "url": ""}]})
    out = voice.import_web(conn, "testimonials", client=fake)
    assert out == {"added": 1, "skipped": 1, "read": 2}
    r = reviews.listing(conn)[0]
    assert r["curated"] == 1 and r["source"] == "testimonials" and r["text"] == "Best fruit box ever."
    assert fake.sent[0]["tools"][0]["allowed_domains"] == ["testfruit.example"]
    again = _Claude("notes", {"reviews": [{"author": "Linda P", "posted_on": "2026-08-02", "rating": 5,
                                           "text": "Best fruit box ever.", "url": ""}]})
    assert voice.import_web(conn, "testimonials", client=again)["added"] == 0
    with pytest.raises(ValueError):
        voice.import_web(conn, "yelp", client=fake)


def test_csv_import(conn):
    out = voice.import_csv(conn, "text,author,rating,source,posted_on,product\n"
                                 "Gift for my mom she loved it,Ana R,5,email,2026-07-01,Honey Mango\n"
                                 "no date here,Bo,4,email,,\n")
    assert out["added"] == 1 and out["rows"] == 2 and "row 3" in out["errors"][0]
    assert voice.import_csv(conn, "words\nhi\n")["error"].startswith("The CSV needs")


def test_extraction_counts_in_code_checks_phrases_and_maps_objections(conn):
    voice.import_fera(conn, http=_Http([{"data": _fera(4), "meta": {"page_count": 1}}]), pause=0)
    with conn:
        fid = conn.execute("INSERT INTO facts (product_id, category, text, status) VALUES (1, 'ripeness',"
                           " 'Arrives firm and ripens in 2 to 3 days', 'approved')").lastrowid
    ids = [r["id"] for r in reviews.listing(conn, 1)]
    tags = {"reviews": [{"id": i, "desire": ["Custard texture"], "objection": ["will it be ripe"] if n < 3 else [],
                         "moment": ["gift"] if n == 0 else [],
                         "phrase": ["arrived perfectly ripe", "a phrase that is not there"]} for n, i in enumerate(ids)]}
    fake = _Claude(tags, {"objections": [{"theme": "Will it be ripe", "fact_id": fid, "angle": "Arrives firm, ripe in 2-3 days"}]})
    out = voice.extract(conn, 1, client=fake)
    assert out["reviews"] == 4
    t = voice.themes(conn, 1)
    assert t["desire"][0]["theme"] == "Custard texture" and t["desire"][0]["mentions"] == 4
    assert [x["theme"] for x in t["phrase"]] == ["arrived perfectly ripe"]            # the invented one is dropped
    obj = t["objection"][0]
    assert obj["mentions"] == 3 and obj["fact_id"] == fid and obj["fact"].startswith("Arrives firm")
    assert obj["quotes"][0]["by"] == "Sam K." and "KNOWN THEMES" in fake.sent[0]["messages"][0]["content"]


def test_only_answered_objections_become_ideas(conn, tmp_path, monkeypatch):
    import creative_intelligence.webapp.app as app_module
    monkeypatch.setattr("creative_intelligence.webapp.common.db", lambda: get_connection(tmp_path / "data" / "t.db"))
    voice.import_fera(conn, http=_Http([{"data": _fera(2), "meta": {"page_count": 1}}]), pause=0)
    with conn:
        conn.execute("INSERT INTO voice_runs (kind, started_at) VALUES ('extract', 'now')")
        bare = conn.execute("INSERT INTO voice_themes (run_id, product_id, kind, theme, mentions, review_ids) VALUES"
                            " (1, 1, 'objection', 'Arrived damaged', 7, '[1]')").lastrowid
        fid = conn.execute("INSERT INTO facts (product_id, category, text, status) VALUES (1, 'ripeness', 'Ripens in 2-3 days',"
                           " 'approved')").lastrowid
        ok = conn.execute("INSERT INTO voice_themes (run_id, product_id, kind, theme, mentions, review_ids, fact_id, angle)"
                          " VALUES (1, 1, 'objection', 'Will it be ripe', 40, '[1,2]', ?, 'Arrives firm, ripe in 2-3 days')",
                          (fid,)).lastrowid
    c = app_module.app.test_client()
    assert "No approved fact" in c.post(f"/api/adstudio/voice/theme/{bare}/idea").get_json()["error"]
    iid = c.post(f"/api/adstudio/voice/theme/{ok}/idea").get_json()["id"]
    i = ideas.get(conn, iid)
    assert i["title"] == "Arrives firm, ripe in 2-3 days" and i["product_ids"] == [1] and "40 customer reviews" in i["why"]
    assert ideas.evidence(conn, i)["customers"]["mentions"] == 40


def test_weekly_refresh_respects_cadence_and_cap(conn, tmp_path, monkeypatch):
    assert voice.refresh_due(conn)
    monkeypatch.setattr(budget, "check_research", lambda *a, **k: (_ for _ in ()).throw(budget.BudgetExceeded("cap")))
    out = voice.refresh(lambda: get_connection(tmp_path / "data" / "t.db"),
                        http=_Http([{"data": _fera(1), "meta": {"page_count": 1}}]))
    assert out["fera"]["added"] == 1 and out["stopped"] == "cap" and not voice.refresh_due(conn)


def test_routes(conn, tmp_path, monkeypatch):
    import creative_intelligence.webapp.app as app_module
    monkeypatch.setattr("creative_intelligence.webapp.common.db", lambda: get_connection(tmp_path / "data" / "t.db"))
    queued = []
    monkeypatch.setattr("creative_intelligence.jobs.enqueue", lambda c, kind, p, **k: queued.append((kind, p)) or 3)
    c = app_module.app.test_client()
    assert c.post("/api/adstudio/voice/import", json={"source": "fera"}).get_json() == {"job_id": 3}
    assert c.post("/api/adstudio/voice/import", json={"source": "yelp"}).status_code == 400
    voice.import_fera(conn, http=_Http([{"data": _fera(2), "meta": {"page_count": 1}}]), pause=0)
    d = c.get("/api/adstudio/voice?product_id=1").get_json()
    assert d["groups"][0]["product"] == "Honey Mango" and d["groups"][0]["estimate"] > 0
    assert c.post("/api/adstudio/voice/extract", json={"product_id": 1}).get_json()["estimate"] > 0
    assert c.post("/api/adstudio/voice/extract", json={"product_id": 1, "confirm": True}).get_json() == {"job_id": 3}
    assert queued[-1] == ("voice", {"step": "extract", "product_id": 1})
    assert b"vKind" in c.get("/ideas/voice").data and b"voiceSlot" in c.get("/products").data
