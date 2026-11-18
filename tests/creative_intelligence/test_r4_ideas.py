"""R4: the idea bank. Ideas carry their evidence in parts (ours, market, customers); generation uses them only for the
share set in knowledge rules.ideas_mix (30% by default) so the playbook, Hook University and winning patterns still
drive the rest; a picked idea always applies; idea_id follows what an idea made onto the finished asset. Claude is faked."""
from __future__ import annotations

import json
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from creative_intelligence import knowledge
from creative_intelligence.adstudio import assets, ideas, runs
from creative_intelligence.db import get_connection, init_db


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("CI_DB_PATH", str(tmp_path / "data" / "t.db"))
    monkeypatch.chdir(tmp_path)
    init_db(tmp_path / "data" / "t.db")
    c = get_connection(tmp_path / "data" / "t.db")
    with c:
        c.execute("INSERT INTO adstudio_products (id, name, product_type) VALUES (1, 'Lychee', 'fruit')")
        c.execute("INSERT INTO adstudio_products (id, name, product_type) VALUES (2, 'Mangosteen', 'fruit')")
    monkeypatch.setattr("creative_intelligence.adstudio.costs.log_tokens", lambda *a, **k: 0.01)
    yield c
    c.close()


class _Claude:
    def __init__(self, body):
        self.body, self.sent = body, []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.sent.append(kw)
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(self.body))],
                               usage=SimpleNamespace(input_tokens=1000, output_tokens=200, cache_read_input_tokens=0))


def _idea(conn, status="approved", **kw):
    iid, why = ideas.create(conn, {"title": kw.pop("title", "Arrives firm, ready in 2 to 3 days"), "angle": "flavor_experience",
                                   "hook": "Name the worry, then the fact", **kw}, status=status)
    assert iid, why
    return iid


def _images(conn, n, pid=1, idea_id=None):
    with conn:
        for _ in range(n):
            conn.execute("INSERT INTO adstudio_images (product_id, archetype, title, concept, prompt, aspect_ratio, model,"
                         " status, idea_id) VALUES (?, 'a', 't', 'c', 'p', '4:5', 'm', 'done', ?)", (pid, idea_id))


def test_the_mix_is_in_the_knowledge_rules():
    assert knowledge.get("rules.ideas_mix")["share"] == 0.30 and knowledge.errors() == []
    assert "rules.ideas_mix" in knowledge.CODE_USES


def test_ideas_are_checked(conn):
    assert "where the fruit comes from" in ideas.create(conn, {"title": "Grown on the farm", "angle": "farm_origin"})[1]
    assert "Unknown style" in ideas.create(conn, {"title": "x", "style": "nope"})[1]
    iid = _idea(conn, style="ripening_timelapse", product_ids=[1])
    i = ideas.get(conn, iid)
    assert i["medium"] == "video" and i["product_ids"] == [1] and i["status"] == "approved"
    assert ideas.update(conn, iid, {"status": "winner", "why": "412 mentions"}) == ""
    assert ideas.get(conn, iid)["status"] == "winner" and ideas.get(conn, iid)["why"] == "412 mentions"


def test_only_the_share_follows_ideas(conn):
    _idea(conn)
    assert sum(1 for s in ideas.assign(conn, 1, "image", 10) if s) == 3            # 30% of a fresh 10
    _images(conn, 10)                                                              # 10 made, none from an idea
    assert ideas.due(conn, 1, "image", 10) == 6                                    # catches up to 30% of the 20
    _images(conn, 6, idea_id=1)
    assert ideas.due(conn, 1, "image", 4) == 0                                     # 6 of 20 already: on target


def test_not_approved_wrong_product_or_wrong_medium_are_never_used(conn):
    _idea(conn, status="new")
    _idea(conn, title="Mangosteen only", product_ids=[2])
    _idea(conn, title="A video idea", style="ripening_timelapse")
    assert ideas.assign(conn, 1, "image", 10) == [None] * 10
    assert [i["title"] for i in ideas.eligible(conn, 1, "video")] == ["A video idea"]
    assert [i["title"] for i in ideas.eligible(conn, 1, "video", style="claymation")] == []


def test_pick_one_picked_typed_and_mixed(conn):
    iid = _idea(conn)
    text, got = ideas.pick_one(conn, 1, "aiads", idea_id=iid)                       # picked: always applies
    assert got == iid and "IDEA FROM THE IDEA BANK" in text and "never copied" in text
    assert ideas.get(conn, iid)["status"] == "in_production"
    assert ideas.pick_one(conn, 1, "aiads", typed="Mother's Day gift") == ("Mother's Day gift", None)
    assert ideas.pick_one(conn, 1, "aiads") == ("", None)                           # first brief: not due yet
    with conn:
        for _ in range(3):
            conn.execute("INSERT INTO adstudio_briefs (product_id, idea, brief_json) VALUES (1, '', '{}')")
    assert ideas.pick_one(conn, 1, "aiads")[1] == iid                               # 1 of 4 is due now


def test_image_run_plans_its_share_from_ideas(conn):
    iid = _idea(conn, product_ids=[1])
    slot = {"title": "t", "archetype": "a", "scene": "s", "camera": "c", "lighting": "l", "surface": "x",
            "fruit_state": "whole", "label": "new_concept"}
    fake = _Claude({"slots": [dict(slot, camera=f"c{n}") for n in range(8)]})
    monkeypatch_profile = {"formats": {"a": {"name": "A", "blurb": "b"}}}
    import creative_intelligence.adstudio.profiles as P
    orig = P.profile
    P.profile = lambda product: monkeypatch_profile
    try:
        slots = runs.plan_run({"id": 1, "name": "Lychee", "product_type": "fruit"}, 8, [], [], conn, 5, client=fake)
    finally:
        P.profile = orig
    led = [s for s in slots if s.get("idea_id")]
    assert len(led) == 2 and all(s["idea_id"] == iid for s in led)                  # round(8 × 0.3)
    assert "IDEA FROM THE IDEA BANK" in fake.sent[0]["messages"][0]["content"][-1]["text"]


def test_evidence_in_parts(conn):
    old = (date.today() - timedelta(days=95)).isoformat()
    with conn:
        conn.execute("INSERT INTO competitor_ads (source, page_name, body, style, angle, tagged_at, started_on, added_at)"
                     " VALUES ('manual', 'Fruit Co', 'x', 'ripening_timelapse', '', 'now', ?, 'now')", (old,))
        for t, stars in (("Ripe on day two, as promised", 5), ("Arrived perfectly ripe", 4), ("Too sweet", 5),
                         ("Never ripened, so disappointed", 1)):                 # 1 star: never counted or quoted
            conn.execute("INSERT INTO reviews (product_id, kind, text, author, source, posted_on, added_at, rating)"
                         " VALUES (1, 'review', ?, 'Sam Kay', 'google', '2026-08-30', 'now', ?)", (t, stars))
    _idea(conn, style="ripening_timelapse", product_ids=[1], evidence={"customer_terms": ["ripe"]})
    _idea(conn, title="Nothing behind it")
    rows = {i["title"]: i for i in ideas.listing(conn)}
    ev = rows["Arrives firm, ready in 2 to 3 days"]["ev"]
    assert ev["customers"]["mentions"] == 2 and ev["customers"]["quote"]["by"] == "Sam K."
    assert ev["market"]["brands"] == 1 and ev["market"]["longest_days"] == 95 and ev["market"]["bar"] == 100
    assert ev["ours"]["label"] == "Ours not tested" and ev["ours"]["bar"] == 0
    assert rows["Nothing behind it"]["ev"]["customers"]["label"] == "Customers not linked"


def test_idea_id_reaches_the_asset_and_testing_follows_the_meta_link(conn):
    iid = _idea(conn, status="in_production")
    with conn:
        bid = conn.execute("INSERT INTO adstudio_briefs (product_id, idea, brief_json, idea_id) VALUES (1, '', '{}', ?)",
                           (iid,)).lastrowid
        conn.execute("INSERT INTO adstudio_textads (product_id, brief_id, kind, variant, title, status, image_path)"
                     " VALUES (1, ?, 'variant', 'bold_type', 't', 'done', '/x.png')", (bid,))
    assets.ensure_assets(conn)
    a = conn.execute("SELECT idea_id, asset_id FROM adstudio_assets").fetchone()
    assert a["idea_id"] == iid
    assert ideas.sync_status(conn) == 0
    with conn:
        conn.execute("UPDATE adstudio_assets SET meta_ad_id='99'")
    assert ideas.sync_status(conn) == 1 and ideas.get(conn, iid)["status"] == "testing"
    assert ideas.usage(conn, iid)["assets"] == 1 and ideas.usage(conn, iid)["aiads"] == 1


def test_draft_uses_the_evidence_and_the_research_cap(conn):
    with conn:
        conn.execute("INSERT INTO reviews (product_id, kind, text, author, source, posted_on, added_at, rating)"
                     " VALUES (1, 'review', 'Ripe on day two', 'Sam Kay', 'google', '2026-08-30', 'now', 5)")
    fake = _Claude({"ideas": [{"title": "Ready in 2 to 3 days", "angle": "flavor_experience", "hook": "worry, then fact",
                               "hook_type": knowledge.hook_type_keys()[0], "style": "ripening_timelapse",
                               "products": ["Lychee"], "why": "customers ask", "customer_terms": ["ripe"],
                               "competitor_ad_ids": [999]}]})
    out = ideas.draft(conn, client=fake)
    assert out["created"] == 1
    i = ideas.get(conn, out["ids"][0])
    assert i["status"] == "new" and i["source"] == "drafted" and i["product_ids"] == [1]
    assert i["evidence"]["competitor_ad_ids"] == []                                 # unknown ads are dropped
    schema = fake.sent[0]["output_config"]["format"]["schema"]["properties"]["ideas"]["items"]["properties"]
    assert "farm_origin" not in schema["angle"]["enum"] and "Ripe on day two" in fake.sent[0]["messages"][0]["content"]


def test_routes(conn, tmp_path, monkeypatch):
    import creative_intelligence.webapp.app as app_module
    monkeypatch.setattr("creative_intelligence.webapp.common.db", lambda: get_connection(tmp_path / "data" / "t.db"))
    c = app_module.app.test_client()
    assert c.post("/api/adstudio/ideas", json={"title": ""}).status_code == 400
    iid = c.post("/api/adstudio/ideas", json={"title": "Gift they talk about", "angle": "gifting"}).get_json()["id"]
    assert c.post(f"/api/adstudio/ideas/{iid}", json={"status": "approved"}).get_json() == {"ok": True}
    d = c.get("/api/adstudio/ideas?product_id=1").get_json()
    assert d["ideas"][0]["products"] == ["Every product"] and d["counts"]["approved"] == 1 and d["mix"]["share"] == 0.3
    assert c.post("/api/adstudio/ideas/mix", json={"share": 0.2}).get_json()["mix"]["share"] == 0.2
    assert knowledge.get("rules.ideas_mix.share") == 0.2 and knowledge.errors() == []
    assert c.post("/api/adstudio/ideas/mix", json={"share": 2}).status_code == 400
    with conn:
        conn.execute("INSERT INTO research_briefs (created_at, brief_json) VALUES ('2026-10-19T08:00:00', ?)",
                     (json.dumps({"summary": "s", "suggestions": [{"style": "claymation", "style_name": "Claymation",
                                                                    "product": "Lychee", "angle": "a fun reveal",
                                                                    "why": "no brand runs it"}]}),))
    assert len(c.get("/api/adstudio/ideas").get_json()["brief"]["suggestions"]) == 1
    sid = c.post("/api/adstudio/ideas/suggestion", json={"brief_id": 1, "index": 0}).get_json()["id"]
    assert ideas.get(conn, sid)["status"] == "approved" and ideas.get(conn, sid)["product_ids"] == [1]
    assert c.post("/api/adstudio/ideas/suggestion", json={"brief_id": 1, "index": 0}).status_code == 400
    assert c.get("/api/adstudio/ideas").get_json()["brief"]["suggestions"] == []
    assert c.post("/api/adstudio/ideas/draft", json={}).get_json() == {"estimate": ideas.DRAFT_ESTIMATE_USD}
    v = c.get("/api/adstudio/voice").get_json()
    assert v["groups"] == [] and v["quotes"] == [] and set(v["themes"]) == {"desire", "objection", "moment", "phrase"}
    for stage in ("bank", "market", "voice"):
        assert c.get(f"/ideas/{stage}").status_code == 200
    assert c.get("/ideas").headers["Location"].endswith("/ideas/visuals")
    assert b'href="/ideas"' in c.get("/products").data


def test_seed_fills_an_empty_bank_once_for_free(conn):
    """Feedback 2026-10-22: the bank was empty. Seeding uses what Ad Studio has; no Claude call."""
    with conn:
        conn.execute("INSERT INTO video_templates (ad_id, ad_name, origin, created_at) VALUES ('a1', 'Mango cut ASMR', 'own', 'now')")
        conn.execute("INSERT INTO video_templates (ad_id, ad_name, origin, created_at) VALUES ('c1', 'Their ad', 'competitor', 'now')")
        conn.execute("INSERT INTO voice_themes (run_id, product_id, kind, theme, mentions, angle) VALUES"
                     " (1, 1, 'desire', 'sweet like candy', 9, 'flavor_experience'), (1, 1, 'objection', 'pricey', 30, '')")
    out = ideas.seed(conn)
    titles = {i["title"] for i in ideas.listing(conn)}
    assert out["added"] >= 3 and "More like our winner: Mango cut ASMR" in titles and "Customers say: sweet like candy" in titles
    assert not any("Their ad" in t or "pricey" in t for t in titles)              # competitors and objections stay out
    assert not any(i["angle"] == "farm_origin" for i in ideas.listing(conn))
    assert all(i["status"] == "new" for i in ideas.listing(conn))
    assert ideas.seed(conn)["added"] == 0                                           # once each
