"""R6: the swipe file (competitor ads, our own ads, screenshots, links, with notes) and the monthly taste rubric
proposals: Claude proposes criteria from the swipe file and our winners; each is approved or rejected one at a time,
and approved ones join the QA rubric through an overlay on the volume. Claude is faked."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from creative_intelligence.adstudio import qa, research, swipe
from creative_intelligence.db import get_connection, init_db


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("CI_DB_PATH", str(tmp_path / "data" / "t.db"))
    monkeypatch.setattr("creative_intelligence.config.CI_RENDER_OUTPUT_DIR", str(tmp_path / "vol" / "renders"))
    monkeypatch.chdir(tmp_path)
    init_db(tmp_path / "data" / "t.db")
    c = get_connection(tmp_path / "data" / "t.db")
    monkeypatch.setattr("creative_intelligence.adstudio.costs.log_tokens", lambda *a, **k: 0.1)
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


def _fill(conn):
    rid, _ = research.add_manual(conn, {"page_name": "Coastal Fruit Co", "body": "Cut open, one line."})
    assert swipe.save(conn, {"kind": "competitor_ad", "ref_id": rid, "notes": "macro cross-section"})[0]
    assert swipe.save(conn, {"kind": "link", "url": "https://example.com/ad", "notes": "bold type"})[0]
    sid, _ = swipe.save_upload(conn, b"\x89PNG fake", "image/png", "great light")
    assert sid
    return rid


def test_swipe_file_saves_each_kind_once(conn, tmp_path):
    rid = _fill(conn)
    items = swipe.listing(conn)
    assert {i["kind"] for i in items} == {"competitor_ad", "link", "screenshot"}
    assert next(i for i in items if i["kind"] == "competitor_ad")["title"].startswith("Coastal Fruit Co")
    assert swipe.save(conn, {"kind": "competitor_ad", "ref_id": rid})[1] == "Already in the swipe file."
    assert swipe.save(conn, {"kind": "link", "url": "nope"})[1].startswith("Paste the full link")
    assert swipe.save(conn, {"kind": "screenshot", "media_path": str(tmp_path / "x.png")})[1] == "Upload the screenshot first."
    assert swipe.save_upload(conn, b"x", "application/pdf", "")[1].startswith("Upload a PNG")
    sid = items[0]["id"]
    assert swipe.update_notes(conn, sid, "changed") and swipe.listing(conn)[0]["notes"] == "changed"


def test_proposals_need_approval_one_by_one_then_join_the_rubric(conn):
    assert swipe.propose(conn, client=_Claude({}))["error"].startswith("Save at least 3")
    _fill(conn)
    assert swipe.propose_due(conn)
    fake = _Claude({"summary": "Close crops win.", "criteria": [
        {"name": "Macro cross-section", "question": "Is the cut fruit shown in one macro close-up?", "weight": 3,
         "applies_to": ["images", "textads"], "evidence": "4 of 6 saved ads"},
        {"name": "Fruit accuracy", "question": "dupe of an existing key?", "weight": 1, "applies_to": ["images"],
         "evidence": "x"},
        {"name": "Bold single line", "question": "Is there one bold line of text at most?", "weight": 0.2,
         "applies_to": ["composites"], "evidence": "winners"}]})
    out = swipe.propose(conn, client=fake)
    keys = [i["key"] for i in out["items"]]
    assert keys == ["taste_macro_cross_section", "taste_fruit_accuracy", "taste_bold_single_line"]
    assert [i["weight"] for i in out["items"]] == [1.0, 1.0, 0.5]                     # taste never outweighs accuracy
    assert any(b["type"] == "image" for b in fake.sent[0]["messages"][0]["content"])
    before = {c["key"] for c in qa.load_rubric()["criteria"]}
    assert not any(k.startswith("taste_") for k in before)
    assert swipe.decide(conn, out["id"], 0, True) == "" and swipe.decide(conn, out["id"], 2, False) == ""
    assert swipe.decide(conn, out["id"], 0, True) == "Already decided."
    r = qa.load_rubric()
    assert r["taste"] == ["taste_macro_cross_section"] and "taste_bold_single_line" not in {c["key"] for c in r["criteria"]}
    c = next(c for c in r["criteria"] if c["key"] == "taste_macro_cross_section")
    assert c["applies_to"] == ["images", "textads"] and "macro" in c["question"]
    assert "taste_macro_cross_section" not in {c["key"] for c in qa.load_rubric(qa.RUBRIC_PATH)["criteria"]}
    assert "Macro cross-section" in swipe.export()
    assert swipe.retire("taste_macro_cross_section") and qa.load_rubric()["taste"] == []
    assert not swipe.propose_due(conn)


def test_routes(conn, tmp_path, monkeypatch):
    import creative_intelligence.webapp.app as app_module
    monkeypatch.setattr("creative_intelligence.webapp.common.db", lambda: get_connection(tmp_path / "data" / "t.db"))
    queued = []
    monkeypatch.setattr("creative_intelligence.jobs.enqueue", lambda c, kind, p, **k: queued.append(kind) or 4)
    c = app_module.app.test_client()
    assert c.post("/api/adstudio/swipe", json={"kind": "link", "url": "https://x.example/ad"}).status_code == 200
    d = c.get("/api/adstudio/swipe").get_json()
    assert len(d["items"]) == 1 and d["proposals"] == [] and d["approved"] == []
    assert c.post("/api/adstudio/taste/propose", json={}).get_json() == {"estimate": swipe.PROPOSE_ESTIMATE_USD}
    assert c.post("/api/adstudio/taste/propose", json={"confirm": True}).get_json() == {"job_id": 4} and queued == ["taste"]
    assert c.post("/api/adstudio/taste/9/decide", json={"index": 0, "approve": True}).status_code == 400
    assert c.get(f"/api/adstudio/swipe/{d['items'][0]['id']}/image").status_code == 404
    assert b"tsPropose" in c.get("/ideas/market").data and b"assetSwipe" in c.get("/gallery/assets").data
    assert b"loadProductIdeas" in c.get("/products").data
