"""T6 — facts and claims library: prompts carry only approved facts; copy is checked for
banned words (code) and unlisted factual claims (Claude). Plus the Admin page.

No live API: Claude is faked.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from creative_intelligence.adstudio import facts
from creative_intelligence.adstudio import textads as T
from creative_intelligence.db import get_connection, init_db


class FakeClient:
    """Marks a claim supported when an approved fact mentions its key word."""
    def __init__(self, claims):
        self.claims, self.calls = claims, []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(stop_reason="end_turn",
                               content=[SimpleNamespace(type="text", text=json.dumps({"claims": self.claims}))],
                               usage=SimpleNamespace(input_tokens=900, output_tokens=120, cache_read_input_tokens=0))


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CI_DB_PATH", str(tmp_path / "t.db"))
    init_db(tmp_path / "t.db")
    c = get_connection(tmp_path / "t.db")
    with c:
        c.execute("INSERT INTO adstudio_products (id, name, product_type, scent_notes) VALUES (1, 'Honey Mango', 'fruit', 'custardy, zero fibre')")
        c.execute("INSERT INTO facts (product_id, category, text) VALUES (NULL, 'shipping', 'Ships within 2 days of picking')")
        c.execute("INSERT INTO facts (product_id, category, text, source) VALUES (1, 'growing_region', 'Grown in Homestead, Florida', 'farm records')")
        c.execute("INSERT INTO banned_terms (product_id, term) VALUES (NULL, 'organic')")
    yield c
    c.close()


PRODUCT = {"id": 1, "name": "Honey Mango", "product_type": "fruit", "scent_notes": "custardy, zero fibre"}


class TestPrompts:
    def test_prompt_block_lists_only_approved_facts_and_banned_terms(self, conn):
        b = facts.prompt_block(conn, PRODUCT)
        assert "Ships within 2 days of picking" in b and "Grown in Homestead, Florida (source: farm records)" in b
        assert "Flavour / key notes: custardy, zero fibre" in b and "NEVER use these words or claims: organic" in b

    def test_copy_prompts_include_the_library(self, conn):
        from creative_intelligence.adstudio.chat import _system
        assert "APPROVED FACTS" in _system(PRODUCT) and "Homestead" in _system(PRODUCT)

    def test_switch_off(self, conn, monkeypatch):
        from creative_intelligence.adstudio.chat import _system
        monkeypatch.setenv("FACTS_CHECK", "0")
        assert "APPROVED FACTS" not in _system(PRODUCT)


class TestCheck:
    def test_banned_words_match_whole_words_only(self):
        banned = [{"term": "organic"}, {"term": "#1"}]
        assert facts.banned_hits("Certified Organic mangoes", banned) == ["organic"]
        assert facts.banned_hits("inorganic chemistry", banned) == []
        assert facts.banned_hits("The #1 mango", banned) == ["#1"]

    def test_unapproved_claim_is_flagged(self, conn):
        client = FakeClient([{"field": "headline", "claim": "Picked this morning", "supported_by": "none"},
                             {"field": "subhead", "claim": "Grown in Homestead", "supported_by": "2"}])
        r = facts.check_copy(conn, PRODUCT, {"headline": "Picked this morning", "subhead": "Grown in Homestead"}, client)
        assert r["status"] == "flagged" and [c["claim"] for c in r["unlisted"]] == ["Picked this morning"]
        assert "Grown in Homestead, Florida" in client.calls[0]["messages"][0]["content"]
        assert client.calls[0]["model"] == "claude-sonnet-5-5"
        assert facts.summary_line(r) == "unapproved claim “Picked this morning” in headline"

    def test_approved_copy_passes_and_is_cached(self, conn):
        client = FakeClient([{"field": "headline", "claim": "Ships within 2 days", "supported_by": "1"}])
        items = {"headline": "Ships within 2 days of picking"}
        assert facts.check_copy(conn, PRODUCT, items, client)["status"] == "ok"
        assert facts.check_copy(conn, PRODUCT, items, client)["status"] == "ok"
        assert len(client.calls) == 1                                        # same text, same facts: cached

    def test_a_new_check_prompt_checks_again(self, conn, monkeypatch):
        from creative_intelligence import prompts
        client = FakeClient([])
        facts.check_copy(conn, PRODUCT, {"headline": "Fresh custard mango"}, client)
        monkeypatch.setattr(prompts, "version", lambda name: 99)
        monkeypatch.setattr(prompts, "text", lambda name, **k: "v99")
        facts.check_copy(conn, PRODUCT, {"headline": "Fresh custard mango"}, client)
        assert len(client.calls) == 2

    def test_puffery_and_scenes_are_not_claims_but_testimonials_are(self):
        from creative_intelligence import prompts
        p = prompts.text("claims-check-system")
        assert "puffery" in p and "illustrative scene" in p and "real customer said it" in p

    def test_banned_word_flags_without_claude(self, conn):
        r = facts.check_copy(conn, PRODUCT, {"headline": "Organic custard mango"}, FakeClient([]))
        assert r["status"] == "flagged" and r["banned"] == [{"field": "headline", "term": "organic"}]

    def test_claude_failure_is_reported_not_silent(self, conn):
        class Boom:
            beta = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: (_ for _ in ()).throw(OSError("down"))))
        r = facts.check_copy(conn, PRODUCT, {"headline": "Ripe now"}, Boom())
        assert r["status"] == "error" and r["error"] == "down"


@pytest.fixture
def client(conn, tmp_path, monkeypatch):
    import creative_intelligence.webapp.app as app_module
    from creative_intelligence.adstudio import chat
    monkeypatch.setattr("creative_intelligence.webapp.common.db", lambda: get_connection(tmp_path / "t.db"))
    fake = FakeClient([{"field": "primary text 1", "claim": "The #1 mango in America", "supported_by": "none"}])
    monkeypatch.setattr(chat, "_client", lambda **k: fake)
    return app_module.app.test_client()


class TestRoutes:
    def test_brief_with_unapproved_claim_is_flagged(self, client, monkeypatch):
        monkeypatch.setattr(T, "draft_brief", lambda *a, **k: T.normalize_brief(
            {"angle": "custard", "cta": "Get Yours", "primary_texts": ["The #1 mango in America"],
             "variants": {"bold_type": {"headline": "Mango Custard"}}}))
        b = client.post("/api/adstudio/product/1/brief/draft", json={"pattern": "none", "idea": "texture"}).get_json()
        assert b["claims_check"]["status"] == "flagged"
        assert b["claims_check"]["unlisted"][0]["claim"] == "The #1 mango in America"
        assert client.get(f"/api/adstudio/brief/{b['id']}").get_json()["claims_check"]["status"] == "flagged"

    def test_saved_hook_is_checked(self, client):
        r = client.post("/api/adstudio/product/1/hook", json={"headline": "Organic custard mango"}).get_json()
        assert r["claims_check"]["banned"][0]["term"] == "organic"
        hooks = client.get("/api/adstudio/product/1").get_json()["hooks"]
        assert hooks[0]["claims_check"]["status"] == "flagged"

    def test_recheck_hooks_stores_the_new_result(self, client, tmp_path):
        client.post("/api/adstudio/product/1/hook", json={"headline": "Organic custard mango"})
        conn = get_connection(tmp_path / "t.db")
        with conn:
            conn.execute("UPDATE adstudio_hooks SET headline='Custard mango'")
        conn.close()
        assert client.post("/api/adstudio/product/1/hooks/recheck").get_json() == {"queued": 1}   # in the worker
        from creative_intelligence import jobs, worker
        job = jobs.claim(get_connection(tmp_path / "t.db"), "test:1")
        assert job["kind"] == "hook_recheck"
        worker.run_one(job, lambda: get_connection(tmp_path / "t.db"))
        hooks = client.get("/api/adstudio/product/1").get_json()["hooks"]
        assert not hooks[0]["claims_check"]["banned"]                      # 'organic' is gone after the recheck

    def test_library_crud(self, client):
        assert client.post("/api/ops/facts", json={"kind": "fact", "category": "nutrition", "text": "High in vitamin C"}).status_code == 400
        d = client.post("/api/ops/facts", json={"kind": "fact", "category": "nutrition", "text": "High in vitamin C",
                                                "source": "USDA FoodData Central", "product_id": "1"}).get_json()
        new = next(f for f in d["facts"] if f["text"] == "High in vitamin C")
        assert new["product_id"] == 1 and new["source"] == "USDA FoodData Central"
        d = client.post("/api/ops/facts", json={"kind": "fact", "id": new["id"], "delete": True}).get_json()
        assert all(f["text"] != "High in vitamin C" for f in d["facts"])
        d = client.post("/api/ops/facts", json={"kind": "banned", "term": "cures"}).get_json()
        assert {b["term"] for b in d["banned"]} == {"organic", "cures"}


class TestAdmin:
    def test_admin_page_and_costs(self, client):
        assert client.get("/admin").status_code == 200
        d = client.get("/api/ops/costs").get_json()
        assert d["caps"] == {"run_usd": 10.0, "day_usd": 50.0}
        assert {p["model"] for p in d["prices"]} >= {"google/nano-banana-pro", "claude-opus-5-5"}

    def test_price_edit_is_marked_checked(self, client):
        assert client.post("/api/ops/prices", json={"model": "google/nano-banana-pro", "usd": "0.15"}).get_json()["ok"]
        p = next(x for x in client.get("/api/ops/costs").get_json()["prices"] if x["model"] == "google/nano-banana-pro")
        assert (p["usd"], p["source"]) == (0.15, "checked in Admin") and p["checked_at"]
        assert client.post("/api/ops/prices", json={"model": "x", "usd": "abc"}).status_code == 400


class TestCostLogging:
    def test_every_copy_call_is_logged_with_its_cost(self, conn):
        from creative_intelligence.adstudio.costs import LoggedClient
        inner = FakeClient([])
        inner.messages = inner.beta.messages
        LoggedClient(inner, "copy").messages.create(model="claude-sonnet-4-5", max_tokens=10, messages=[])
        row = conn.execute("SELECT model, kind, cost_usd FROM model_calls").fetchone()
        assert tuple(row) == ("claude-sonnet-4-5", "copy", 0.0045)            # 900 in x $3/M + 120 out x $15/M

    def test_model_names_come_from_config(self):
        from creative_intelligence import config
        from creative_intelligence.adstudio import chat, generate, qa
        assert (chat.MODEL, qa.MODEL, generate.MODEL) == (
            config.CI_CLAUDE_COPY_MODEL, config.CI_CLAUDE_SCORE_MODEL, config.CI_IMAGE_MODEL)


class TestOtherFruit:
    """Feedback 2026-10-23: "one of the ads says 'peach' … when there is a mango ad"."""

    def test_another_fruit_is_flagged_its_own_is_not(self, conn):
        r = facts.check_copy(conn, PRODUCT, {"headline": "Tastes Like Peach Custard"}, FakeClient([]))
        assert r["status"] == "flagged" and r["banned"] == [{"field": "headline", "term": "peach", "rule": "other_fruit"}]
        assert facts.check_copy(conn, PRODUCT, {"headline": "Not your grocery store mango"}, FakeClient([]))["status"] == "ok"
        assert facts.other_fruit_hits(conn, PRODUCT, "custard-peach flesh, pear-shaped") == ["peach"]

    def test_a_box_may_name_what_it_holds_but_not_a_comparison(self, conn):
        with conn:
            conn.execute("INSERT INTO adstudio_products (id, name, product_type) VALUES (2, 'Exotic Fruit Variety Box', 'box')")
            conn.execute("INSERT INTO facts (product_id, category, text) VALUES (2, 'contents', 'Fruits may include mango,"
                         " cherimoya and passion fruit.'), (2, 'taste', 'Cherimoya tastes like banana and pineapple.')")
        box = {"id": 2, "name": "Exotic Fruit Variety Box", "product_type": "box"}
        assert facts.other_fruit_hits(conn, box, "Cherimoya, mango and passion fruit in one box") == []
        assert facts.other_fruit_hits(conn, box, "Like banana and pineapple") == ["banana", "pineapple"]

    def test_the_rule_is_in_every_copy_prompt(self):
        from creative_intelligence import prompts
        for name in ("hook-university-system", "ai-ad-copy-rules", "claims-check-system", "story-script-system"):
            assert "Never compare it to or name another fruit" in prompts.text(name), name
