"""Product Hub: the Shopify catalogue sync, readiness, photo tags and heroes, research (Claude with
web search, faked), fact approval, and the not-in-store rules. No live API: Shopify and Claude are faked."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from PIL import Image

from creative_intelligence.adstudio import catalog, facts, hub
from creative_intelligence.db import get_connection, init_db


def _p(handle, title, ptype="Fruit: Mango", tags=(), available=True, price="89.00"):
    return {"handle": handle, "title": title, "product_type": ptype, "tags": list(tags), "body_html": f"<p>{title} copy</p>",
            "variants": [{"title": "2 lbs", "price": price, "available": available}],
            "images": [{"src": f"https://cdn.example/{handle}.jpg"}]}


FEED = [_p("honey-mango", "Honey Mango", tags=["c.abundant", "core active"]),
        _p("asian-pear", "Asian Pear", "Fruit: Pear", tags=["c.seasonal"], available=False),
        _p("mango-variety-box", "Mango Variety Box", "Mix Box: Mango, Variety", tags=["preorder"]),
        _p("guava-rose-candle", "Guava Rosé Candle", "Candle: Guava Rose"),
        _p("pem-bab", "Honey Mango", tags=["buildabox", "hidden"]),
        _p("gift-card", "Gift Card", "Gift Card"),
        _p("snack", "Tropical Bites", "Other: Tropical Bites, Mango")]


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CI_DB_PATH", str(tmp_path / "t.db"))
    init_db(tmp_path / "t.db")
    c = get_connection(tmp_path / "t.db")
    yield c
    c.close()


def _row(conn, **where):
    k, v = next(iter(where.items()))
    return dict(conn.execute(f"SELECT * FROM adstudio_products WHERE {k}=?", (v,)).fetchone())


# ── catalogue sync ──────────────────────────────────────────────────────────
class TestSync:
    def test_lists_maps_and_badges(self, conn):
        counts = catalog.sync(conn, "manual", fetch=lambda: FEED)
        assert counts["listed"] == 5 and counts["added"] == 5            # hidden twin and gift card left out
        pem = _row(conn, shopify_handle="honey-mango")
        assert (pem["product_type"], pem["origin"], pem["season"], pem["in_stock"]) == ("fruit", "shopify", "in", 1)
        assert json.loads(pem["shop_json"])["price_min"] == 89.0 and pem["description"] == "Honey Mango copy"
        pear = _row(conn, shopify_handle="asian-pear")
        assert (pear["season"], pear["in_stock"]) == ("out", 0)
        assert _row(conn, shopify_handle="mango-variety-box")["product_type"] == "gift_box"
        assert _row(conn, shopify_handle="mango-variety-box")["season"] == "out"     # preorder = out of season
        assert _row(conn, shopify_handle="guava-rose-candle")["product_type"] == "candle"
        assert _row(conn, shopify_handle="snack")["product_type"] == "other"
        assert catalog.last_sync(conn)["counts"]["listed"] == 5

    def test_links_existing_products_and_keeps_their_type_and_work(self, conn):
        with conn:
            conn.execute("INSERT INTO adstudio_products (id, name, product_type, shopify_url, physical_desc) VALUES"
                         " (7, 'PEM', 'candle', 'https://shop.testfruit.example/products/honey-mango', 'pink')")
            conn.execute("INSERT INTO adstudio_products (id, name, product_type) VALUES (8, 'asian pear', 'fruit')")
            conn.execute("INSERT INTO adstudio_products (id, name, product_type) VALUES (9, 'Kiwi Test', 'fruit')")
        counts = catalog.sync(conn, fetch=lambda: FEED)
        assert counts["linked"] == 2 and counts["added"] == 3
        pem = _row(conn, id=7)
        assert (pem["shopify_handle"], pem["origin"], pem["product_type"], pem["physical_desc"]) == \
            ("honey-mango", "shopify", "candle", "pink")
        assert _row(conn, id=8)["shopify_handle"] == "asian-pear"         # linked by name
        assert _row(conn, id=9)["origin"] == "local"                      # not in store
        assert facts.not_in_store(_row(conn, id=9)) and not facts.not_in_store(pem)

    def test_archives_never_deletes_and_restores(self, conn):
        catalog.sync(conn, fetch=lambda: FEED)
        n = conn.execute("SELECT COUNT(*) FROM adstudio_products").fetchone()[0]
        c = catalog.sync(conn, fetch=lambda: [p for p in FEED if p["handle"] != "asian-pear"])
        assert c["archived"] == 1 and conn.execute("SELECT COUNT(*) FROM adstudio_products").fetchone()[0] == n
        assert _row(conn, shopify_handle="asian-pear")["archived"] == 1
        assert catalog.sync(conn, fetch=lambda: FEED)["restored"] == 1
        assert _row(conn, shopify_handle="asian-pear")["archived"] == 0

    def test_a_cut_short_feed_archives_nothing(self, conn):
        catalog.sync(conn, fetch=lambda: FEED)
        c = catalog.sync(conn, fetch=lambda: FEED[:1])
        assert c["archived"] == 0 and c["archive_skipped"] == 4

    def test_failed_fetch_is_recorded(self, conn):
        def boom():
            raise RuntimeError("store down")
        with pytest.raises(RuntimeError):
            catalog.sync(conn, fetch=boom)
        assert catalog.last_sync(conn)["status"] == "failed"

    def test_daily_schedule(self, conn):
        from datetime import datetime
        assert not catalog.is_due(conn, datetime(2026, 10, 19, 5))
        assert catalog.is_due(conn, datetime(2026, 10, 19, 7))
        catalog.sync(conn, "scheduled", fetch=lambda: FEED)
        assert not catalog.is_due(conn, datetime.utcnow().replace(hour=23))

    def test_feed_paging(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        pages = {1: [{"handle": f"p{i}"} for i in range(250)], 2: [{"handle": "last"}]}

        def get(url, **kw):
            n = int(url.rsplit("page=", 1)[1])
            return SimpleNamespace(raise_for_status=lambda: None, status_code=200, json=lambda: {"products": pages.get(n, [])})
        assert len(catalog.fetch_feed(get)) == 251


# ── readiness ───────────────────────────────────────────────────────────────
def _photo(conn, pid, tag="", hero=0, tmp=None):
    path = str(tmp / f"ph{conn.execute('SELECT COUNT(*) FROM adstudio_photos').fetchone()[0]}.jpg")
    Image.new("RGB", (64, 80), (200, 120, 150)).save(path)
    with conn:
        return conn.execute("INSERT INTO adstudio_photos (product_id, path, tag, is_hero) VALUES (?,?,?,?)",
                            (pid, path, tag, hero)).lastrowid


class TestReadiness:
    def test_images_then_video(self, conn, tmp_path):
        with conn:
            conn.execute("INSERT INTO adstudio_products (id, name, product_type) VALUES (1, 'PEM', 'fruit')")
        p = lambda: _row(conn, id=1)
        r = hub.readiness(p(), hub.stats(conn, 1)[1])
        assert not r["images"]["ready"] and "tag 2+ reference photos" in r["images"]["missing"]
        _photo(conn, 1, "whole", tmp=tmp_path); _photo(conn, 1, "in_hand", tmp=tmp_path)
        assert "a cut reference photo" in hub.readiness(p(), hub.stats(conn, 1)[1])["images"]["missing"]
        _photo(conn, 1, "cut", tmp=tmp_path)
        with conn:
            conn.execute("UPDATE adstudio_products SET physical_desc='pink oval' WHERE id=1")
            conn.execute("INSERT INTO adstudio_hooks (product_id, headline) VALUES (1, 'Pink outside')")
        r = hub.readiness(p(), hub.stats(conn, 1)[1])
        assert r["images"]["ready"] and not r["video"]["ready"]
        with conn:
            for i in range(5):
                conn.execute("INSERT INTO facts (product_id, text, status) VALUES (1, ?, 'approved')", (f"fact {i}",))
            conn.execute("INSERT INTO facts (product_id, text, status) VALUES (1, 'pending one', 'pending')")
        assert hub.readiness(p(), hub.stats(conn, 1)[1])["video"]["missing"] == ["the taste approved"]
        with conn:
            conn.execute("UPDATE adstudio_products SET taste='sweet', taste_status='approved' WHERE id=1")
        r = hub.readiness(p(), hub.stats(conn, 1)[1])
        assert r["video"]["ready"] and r["steps_done"] == r["steps"] == 5

    def test_a_cut_photo_is_only_needed_for_fruit(self, conn, tmp_path):
        with conn:
            conn.execute("INSERT INTO adstudio_products (id, name, product_type, physical_desc) VALUES (2, 'Box', 'gift_box', 'white box')")
            conn.execute("INSERT INTO adstudio_hooks (product_id, headline) VALUES (2, 'A gift')")
        _photo(conn, 2, "in_box", tmp=tmp_path); _photo(conn, 2, "lifestyle", tmp=tmp_path)
        assert hub.readiness(_row(conn, id=2), hub.stats(conn, 2)[2])["images"]["ready"]

    def test_heroes_at_most_three_first_is_primary(self, conn, tmp_path):
        with conn:
            conn.execute("INSERT INTO adstudio_products (id, name) VALUES (1, 'PEM')")
        ids = [_photo(conn, 1, tmp=tmp_path) for _ in range(4)]
        for i in ids[:3]:
            assert hub.set_hero(conn, i, True) == ""
        assert "Up to 3" in hub.set_hero(conn, ids[3], True)
        hub.set_hero(conn, ids[0], False)
        assert conn.execute("SELECT id FROM adstudio_photos WHERE is_primary=1").fetchone()[0] == ids[1]
        from creative_intelligence.webapp.common import _primary_photo_uris
        assert len(_primary_photo_uris(conn, 1)) == 3


# ── Claude helpers (faked) ──────────────────────────────────────────────────
def _resp(text, stop="end_turn", searches=0):
    return SimpleNamespace(stop_reason=stop, content=[SimpleNamespace(type="text", text=text)],
                           usage=SimpleNamespace(input_tokens=1000, output_tokens=200, cache_read_input_tokens=0,
                                                 cache_creation_input_tokens=0,
                                                 server_tool_use=SimpleNamespace(web_search_requests=searches)))


class FakeClient:
    def __init__(self, plain, beta):
        self.plain, self.beta_out, self.calls = list(plain), list(beta), []
        self.messages = SimpleNamespace(create=self._plain)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._beta))

    def _plain(self, **kw):
        self.calls.append(("plain", kw))
        return self.plain.pop(0)

    def _beta(self, **kw):
        self.calls.append(("beta", kw))
        return self.beta_out.pop(0)


RESEARCH = {"taste": "Honeyed, very low acid, buttery and fibreless.", "facts": [
    {"text": "Honey mangoes can weigh up to 2 lb.", "category": "characteristic",
     "source_url": "https://shop.testfruit.example/products/honey-mango", "confidence": "high", "use": True, "reason": ""},
    {"text": "A cup of mango has about 60 mg of vitamin C.", "category": "nutrition",
     "source_url": "https://fdc.nal.usda.gov/food/123", "confidence": "high", "use": True, "reason": ""},
    {"text": "Mango lowers blood sugar.", "category": "health", "source_url": "https://blog.example/mango",
     "confidence": "low", "use": True, "reason": ""},
    {"text": "Test Fruit Co ships within a day of picking.", "category": "shipping",
     "source_url": "https://othershop.example/mango", "confidence": "medium", "use": True, "reason": ""},
    {"text": "Cures colds.", "category": "health", "source_url": "https://x.example", "confidence": "low",
     "use": False, "reason": "no reliable source"},
    {"text": "No source here.", "category": "other", "source_url": "", "confidence": "low", "use": True, "reason": ""},
]}


class TestResearch:
    @pytest.fixture
    def pem(self, conn):
        with conn:
            conn.execute("INSERT INTO adstudio_products (id, name, product_type, origin, shopify_url, description) VALUES"
                         " (1, 'Honey Mango', 'fruit', 'shopify',"
                         " 'https://shop.testfruit.example/products/honey-mango', 'Up to 2 lb each')")
        return 1

    def test_estimate_is_shown_and_capped(self, conn, pem, monkeypatch):
        est = hub.estimate(conn)
        assert 0.6 < est < 1.5
        from creative_intelligence import budget
        budget.set_cap(conn, "text_day", 0.01)
        rid, why = hub.start_research(conn, pem)
        assert rid is None and why

    def test_run_saves_sourced_facts_as_pending(self, conn, pem, tmp_path):
        rid, _ = hub.start_research(conn, pem)
        client = FakeClient([_resp("partial", stop="pause_turn", searches=3), _resp("TASTE: honeyed. FACTS: …", searches=2)],
                            [_resp(json.dumps(RESEARCH))])
        out = hub.run_research(lambda: get_connection(tmp_path / "t.db"), rid, client=client)
        assert out["searches"] == 5 and out["counts"] == {"approved": 0, "pending": 2, "do_not_use": 3, "dropped": 1, "repeats": 0, "taste": 1,
                                                       "family": 0, "quotes": 0}
        assert client.calls[1][1]["messages"][1]["role"] == "assistant"            # pause_turn resumed
        assert client.calls[0][1]["tools"][0]["type"] == "web_search_20260209"
        rows = {r["text"]: dict(r) for r in conn.execute("SELECT * FROM facts WHERE product_id=1")}
        assert rows["Honey mangoes can weigh up to 2 lb."]["status"] == "pending"
        assert rows["A cup of mango has about 60 mg of vitamin C."]["source"] == "fdc.nal.usda.gov"
        assert rows["Mango lowers blood sugar."]["status"] == "do_not_use"
        assert "own product page" in rows["Test Fruit Co ships within a day of picking."]["note"]
        assert rows["Cures colds."]["status"] == "do_not_use"
        p = _row(conn, id=1)
        assert (p["taste_status"], p["taste"]) == ("approved", RESEARCH["taste"])
        r = hub.latest_research(conn, 1)
        assert r["status"] == "done" and r["cost_usd"] > 0.05 and r["searches"] == 5
        assert conn.execute("SELECT units FROM model_calls WHERE model='web_search'").fetchone()[0] == 5
        # pending facts never reach prompts; approved ones do
        assert "60 mg" not in facts.prompt_block(conn, p)
        with conn:
            conn.execute("UPDATE facts SET status='approved' WHERE product_id=1 AND status='pending'")
        assert "60 mg" in facts.prompt_block(conn, p) and "Taste & characteristics" in facts.prompt_block(conn, p)   # researched taste is approved

    def test_rerun_keeps_an_approved_taste_and_skips_repeats(self, conn, pem, tmp_path):
        with conn:
            conn.execute("UPDATE adstudio_products SET taste='mine', taste_status='approved' WHERE id=1")
        counts = hub.save_research(conn, _row(conn, id=1), RESEARCH, "shop.testfruit.example")
        again = hub.save_research(conn, _row(conn, id=1), RESEARCH, "shop.testfruit.example")
        assert counts["pending"] == 2 and again["repeats"] == 5
        assert _row(conn, id=1)["taste"] == "mine"
        assert "Taste & characteristics: mine" in facts.prompt_block(conn, _row(conn, id=1))

    def test_failed_research_is_recorded(self, conn, pem, tmp_path):
        rid, _ = hub.start_research(conn, pem)
        client = FakeClient([_resp("")], [])
        with pytest.raises(RuntimeError):
            hub.run_research(lambda: get_connection(tmp_path / "t.db"), rid, client=client)
        assert hub.latest_research(conn, 1)["status"] == "failed"

    def test_draft_look_tags_untagged_photos_and_picks_heroes(self, conn, pem, tmp_path):
        a = _photo(conn, 1, tmp=tmp_path); b = _photo(conn, 1, "lifestyle", tmp=tmp_path); c = _photo(conn, 1, tmp=tmp_path)
        client = FakeClient([], [_resp(json.dumps({"look": "Large pink oval mango.", "tags": ["whole", "cut", "none"],
                                                   "heroes": [1]}))])
        out = hub.draft_look(conn, _row(conn, id=1), client=client)
        tags = dict(conn.execute("SELECT id, tag FROM adstudio_photos").fetchall())
        assert out["tagged"] == 1 and tags == {a: "whole", b: "lifestyle", c: ""}   # hand-set tag kept
        assert _row(conn, id=1)["physical_desc"] == "Large pink oval mango."
        assert conn.execute("SELECT id FROM adstudio_photos WHERE is_hero=1").fetchall()[0][0] == a


# ── not in store ────────────────────────────────────────────────────────────
def test_not_in_store_products_never_state_price_stock_or_order_now(conn):
    p = {"id": 5, "name": "Kiwi Berry", "origin": "local", "shopify_url": ""}
    assert "NOT sold in the store" in facts.prompt_block(conn, p)
    r = facts.check_copy(conn, p, {"cta": "Order now", "headline": "Only $19"}, client=FakeClient([], [_resp('{"claims": []}')] * 2))
    assert {b["term"] for b in r["banned"]} == {"order now", "a price"} and r["status"] == "flagged"
    shop = {"id": 6, "name": "PEM", "origin": "shopify", "shopify_url": "https://x/products/pem"}
    assert "NOT sold" not in facts.prompt_block(conn, shop)


# ── routes ──────────────────────────────────────────────────────────────────
@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "t.db"
    monkeypatch.chdir(tmp_path)
    init_db(db_path)
    import creative_intelligence.webapp.app as app_module
    monkeypatch.setattr("creative_intelligence.webapp.common.db", lambda: get_connection(db_path))
    conn = get_connection(db_path)
    catalog.sync(conn, fetch=lambda: FEED)
    with conn:
        conn.execute("INSERT INTO adstudio_products (id, name, product_type) VALUES (50, 'Kiwi Berry', 'fruit')")
    conn.close()
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        c.db_path = db_path
        yield c


class TestRoutes:
    def test_catalog_lists_store_and_not_in_store(self, client):
        d = client.get("/api/adstudio/catalog").get_json()
        by = {p["name"]: p for p in d["products"]}
        assert len(d["products"]) == 6 and d["sync"]["status"] == "done"
        assert by["Honey Mango"]["in_store"] and by["Honey Mango"]["season"] == "in"
        assert not by["Kiwi Berry"]["in_store"] and by["Mango Variety Box"]["product_type"] == "gift_box"
        assert by["Honey Mango"]["thumb_url"].endswith("honey-mango.jpg")

    def test_sync_now_queues_one_job(self, client):
        a = client.post("/api/adstudio/catalog/sync").get_json()["job_id"]
        assert client.post("/api/adstudio/catalog/sync").get_json()["job_id"] == a
        assert client.get("/api/adstudio/catalog/sync").get_json()["running"]

    def test_profile_and_fact_flow(self, client, monkeypatch):
        monkeypatch.setattr("creative_intelligence.adstudio.core.shopify_prices",
                            lambda url: [{"title": "2 lbs", "price": "$89", "available": True}])
        pid = next(p["id"] for p in client.get("/api/adstudio/catalog").get_json()["products"] if p["name"] == "Honey Mango")
        prof = client.get(f"/api/adstudio/product/{pid}/profile").get_json()
        assert prof["in_store"] and prof["prices_live"] and prof["prices"][0]["price"] == "$89"
        assert prof["research_estimate"] > 0 and set(prof["tags"]) == set(hub.TAGS)
        assert client.post(f"/api/adstudio/product/{pid}/research", json={}).get_json()["estimate_usd"] > 0
        conn = get_connection(client.db_path)
        hub.save_research(conn, {"id": pid}, RESEARCH, "shop.testfruit.example")
        conn.close()
        assert client.post(f"/api/adstudio/product/{pid}/facts/approve").get_json()["approved"] == 2
        assert client.post(f"/api/adstudio/product/{pid}/fact", json={"text": "Grown in Florida", "category": "growing_region"}).get_json()["id"]
        assert "error" in client.post(f"/api/adstudio/product/{pid}/fact", json={"text": "x", "category": "health"}).get_json()
        prof = client.get(f"/api/adstudio/product/{pid}/profile").get_json()
        assert [f["status"] for f in prof["facts"]].count("approved") == 3
        fid = prof["facts"][0]["id"]
        assert client.post(f"/api/adstudio/fact/{fid}", json={"status": "do_not_use"}).get_json()["ok"]
        assert client.post(f"/api/adstudio/fact/{fid}", json={"status": "bogus"}).status_code == 400
        client.post(f"/api/adstudio/product/{pid}/update", json={"taste": "sweet", "taste_status": "approved",
                                                                 "customer_language": "best mango ever"})
        p = client.get(f"/api/adstudio/product/{pid}").get_json()["product"]
        assert (p["taste_status"], p["customer_language"]) == ("approved", "best mango ever")

    def test_not_in_store_profile_has_no_prices(self, client):
        prof = client.get("/api/adstudio/product/50/profile").get_json()
        assert not prof["in_store"] and prof["prices"] == []

    def test_admin_lists_only_approved_facts(self, client):
        conn = get_connection(client.db_path)
        hub.save_research(conn, {"id": 50}, RESEARCH, "shop.testfruit.example")
        conn.close()
        assert client.get("/api/ops/facts").get_json()["facts"] == []


# ── Hook University merged into hook suggestions ────────────────────────────
class _HookClient:
    def __init__(self, replies):
        self.replies, self.sent = list(replies), []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.sent.append(kw)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps({"hooks": self.replies.pop(0)}))])


_ARCH = iter(range(1, 10**6))


def _hk(text, **kw):
    return {"hook_text": text, "headline": text[:20], "archetype": f"A{next(_ARCH) % 10 + 1}", "d": "d", "c": "c",
            "ps": "ps", **kw}


GOOD = ["Honey Mango weighs two pounds, yet most people walk past it at the market every week",
        "The Honey Mango ripens in a window so short most growers never ship it at all",
        "My kids asked for Honey Mango before dessert, and I still haven't told them why",
        "Honey Mango cuts like butter, but the first slice is not the part people remember",
        "One Honey Mango feeds four, though nobody at the table will want to share it",
        "Honey Mango tastes like honey and sunset, and the seed is thinner than you'd expect"]


class TestHookUniversity:
    def test_cliches_are_caught(self):
        from creative_intelligence.adstudio import hook_university as HU
        assert HU.is_cliche("Honey Mango the way it should have always tasted")
        assert HU.is_cliche("You've never tasted mango like this")
        assert not HU.is_cliche(GOOD[0])

    def test_suggest_filters_and_retries_with_everything_seen_forbidden(self, monkeypatch):
        from creative_intelligence.adstudio import chat
        first = [_hk(GOOD[0]), _hk("You've never tasted Honey Mango like this. We guarantee it, honestly."),
                 _hk(GOOD[0]), _hk("[fruit] is great"), _hk(GOOD[1]), _hk(GOOD[2])]
        fake = _HookClient([first, [_hk(GOOD[3]), _hk(GOOD[4]), _hk(GOOD[5])]])
        monkeypatch.setattr(chat, "_client", lambda: fake)
        hooks = chat.suggest_hooks({"name": "Honey Mango", "product_type": "fruit"}, 5,
                                   recent=["An old hook about Honey Mango"])
        assert [h["hook_text"] for h in hooks] == GOOD[:5] and all(not h["qa_flags"] for h in hooks)
        assert len(fake.sent) == 3 and fake.sent[2]["model"] == chat.CRITIC_MODEL       # 2 drafts, then the critic
        assert "Hook University" in fake.sent[0]["system"] and "A12" in fake.sent[0]["system"]
        ask2 = fake.sent[1]["messages"][0]["content"][-1]["text"]
        assert "An old hook about" in ask2 and "never tasted Honey" in ask2     # seen = exhausted
        assert "REQUIRED ANGLE MIX" in ask2 and "D+C+PS" in ask2

    def test_house_rules_code_can_check(self):
        from creative_intelligence.adstudio import hook_university as HU
        hooks = [{"hook_text": GOOD[0], "archetype": "A1 — x"},
                 {"hook_text": "Dense honeyed flesh, the mango texture grocery stores can't deliver to anyone", "archetype": "A2"},
                 {"hook_text": "Honey Mango tastes nothing like the grocery store kind, and here is why", "archetype": "A3"},
                 {"hook_text": "Honey Mango tastes nothing like the supermarket kind you buy each week", "archetype": "A4"},
                 {"hook_text": "Honey Mango is the one fruit my kids fight over, every single summer", "archetype": "A1"}]
        ok, bad = HU.screen(hooks, [], "Honey Mango")
        assert [h["hook_text"] for h in ok] == [GOOD[0], hooks[2]["hook_text"]]
        flags = {h["archetype"]: h["qa_flags"] for h in bad}
        assert flags["A2"] == ["product_not_named"] and flags["A1"] == ["archetype_repeated: A1"]
        assert any(f.startswith(("near_duplicate", "second_grocery")) for f in flags["A4"])

    def test_flagged_hooks_only_fill_a_gap(self, monkeypatch):
        from creative_intelligence.adstudio import chat
        fake = _HookClient([[_hk(GOOD[0]), _hk("short one")], [_hk("Ever wondered why mango from the supermarket never quite delivers?")]])
        monkeypatch.setattr(chat, "_client", lambda: fake)
        hooks = chat.suggest_hooks({"name": "Honey Mango", "product_type": "fruit"}, 3)
        assert hooks[0]["hook_text"] == GOOD[0] and hooks[1]["qa_flags"] and "banned_template" in hooks[2]["qa_flags"]


# ── auto-fill ───────────────────────────────────────────────────────────────
LANG = {"quotes": [{"quote": "tastes like a mango dipped in honey", "kind": "praise", "source_url": "https://reddit.com/r/x"},
                   {"quote": "made up", "kind": "praise", "source_url": ""}]}
LOOK = {"look": "Large pink oval mango.", "tags": ["whole", "cut"], "heroes": [1]}


RESEARCH_Q = {**RESEARCH, "quotes": LANG["quotes"]}


class TestFill:
    @pytest.fixture
    def setup(self, conn, tmp_path, monkeypatch):
        monkeypatch.setenv("FACTS_CHECK", "0")
        with conn:
            conn.execute("INSERT INTO adstudio_products (id, name, product_type, origin, shopify_url, shop_json) VALUES"
                         " (1, 'Honey Mango', 'fruit', 'shopify', 'https://shop.testfruit.example/products/pem',"
                         " '{\"type\": \"Fruit: Mango, Honey\", \"images\": [\"https://cdn.example/a.jpg\"]}')")
        from creative_intelligence.adstudio import chat
        hooks = _HookClient([[_hk(g) for g in GOOD]])
        monkeypatch.setattr(chat, "_client", lambda log=True: hooks)

        def fake_import(c, p, limit=6):
            _photo(c, p["id"], tmp=tmp_path); _photo(c, p["id"], tmp=tmp_path)
            return 2
        monkeypatch.setattr(hub, "import_shop_photos", fake_import)
        client = FakeClient([_resp("notes", searches=4), _resp("quotes", searches=3)],
                            [_resp(json.dumps(LOOK)), _resp(json.dumps(RESEARCH_Q)), _resp(json.dumps(LANG))])
        return client, (lambda: get_connection(tmp_path / "t.db"))

    def test_fills_every_empty_part_in_one_research_pass(self, conn, setup):
        client, db = setup
        assert hub.queue_fill(conn, 1) and hub.queue_fill(conn, 1) is None          # one at a time
        with conn:
            conn.execute("UPDATE jobs SET status='failed' WHERE kind='fill'")
        assert hub.queue_fill(conn, 1)                                             # a lost job doesn't block it
        out = hub.fill(db, 1, client=client)
        assert out["done"] == ["photos", "look", "research", "hooks"] and not out["errors"]
        assert len([c for c in client.calls if c[0] == "plain"]) == 1             # one web-search pass
        search = next(c for c in client.calls if c[0] == "plain")          # the look runs alongside, so order varies
        assert search[1]["tools"][0]["max_uses"] == 5
        p = _row(conn, id=1)
        assert p["fill_status"] == "done" and p["physical_desc"] == "Large pink oval mango." and p["taste_status"] == "approved"
        assert p["customer_language"] == '"tastes like a mango dipped in honey" (praise) — https://reddit.com/r/x'
        hooks = conn.execute("SELECT source FROM adstudio_hooks WHERE product_id=1").fetchall()
        assert len(hooks) == 5 and {h[0] for h in hooks} == {"hook_university"}

    def test_products_researched_before_quotes_get_the_language_pass(self, conn, setup):
        client, db = setup
        with conn:
            conn.execute("UPDATE adstudio_products SET physical_desc='mine' WHERE id=1")
            conn.execute("INSERT INTO adstudio_hooks (product_id, headline) VALUES (1, 'mine')")
            conn.execute("INSERT INTO product_research (product_id, status, result_json, created_at) VALUES"
                         " (1, 'done', '{\"counts\": {\"pending\": 3}}', '2026-10-19')")
        client.plain.pop(0); client.beta_out.pop(0); client.beta_out.pop(0)
        out = hub.fill(db, 1, client=client)
        assert out["done"] == ["photos", "customer language"]
        assert "honey" in _row(conn, id=1)["customer_language"]

    def test_filled_parts_are_left_alone_and_a_failed_step_does_not_stop_the_rest(self, conn, setup, monkeypatch):
        client, db = setup
        with conn:
            conn.execute("UPDATE adstudio_products SET physical_desc='mine', customer_language='mine' WHERE id=1")
            conn.execute("INSERT INTO adstudio_hooks (product_id, headline) VALUES (1, 'mine')")
        monkeypatch.setattr(hub, "run_research", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("search down")))
        out = hub.fill(db, 1, client=client)
        assert out["done"] == ["photos"] and out["errors"] == ["research: search down"]
        p = _row(conn, id=1)
        assert (p["fill_status"], p["physical_desc"], p["customer_language"]) == ("failed", "mine", "mine")

    def test_the_text_cap_stops_it(self, conn, setup):
        client, db = setup
        from creative_intelligence import budget
        budget.set_cap(conn, "text_day", 0.01)
        with pytest.raises(RuntimeError):
            hub.fill(db, 1, client=client)
        assert _row(conn, id=1)["fill_status"] == "failed"


class TestFamily:
    def test_family_from_the_shopify_type(self):
        f = lambda t: catalog.family({"shop_json": json.dumps({"type": t})})
        assert f("Fruit: Mango, Honey") == "mango" and f("Mix Box: Mango, Variety") == "mango"
        assert f("Mix Box: Peaches") == "peach" and f("Fruit: Avocados, Hass") == "avocado"
        assert f("Mix Box: Variety Box, Citrus") == "" and f("Candle: Guava Rose") == "" and f("") == ""

    def test_family_facts_are_researched_once_and_shared(self, conn):
        catalog.sync(conn, fetch=lambda: FEED)
        pem = _row(conn, shopify_handle="honey-mango")
        box = _row(conn, shopify_handle="mango-variety-box")
        data = {"taste": "", "quotes": [], "facts": [
            {**RESEARCH["facts"][1], "scope": "family"}, {**RESEARCH["facts"][0], "scope": "variety"}]}
        c = hub.save_research(conn, pem, data, "shop.testfruit.example")
        assert c["family"] == 1 and c["pending"] == 2
        assert hub.family_known(conn, "mango") == ["A cup of mango has about 60 mg of vitamin C."]
        assert hub.save_research(conn, box, data, "shop.testfruit.example")["repeats"] == 1   # shared one not repeated
        with conn:
            conn.execute("UPDATE facts SET status='approved'")
        assert "60 mg" in facts.prompt_block(conn, box)
        assert hub.stats(conn, box["id"])[box["id"]]["facts"] == 2                 # its own + the family's
        assert conn.execute("SELECT COUNT(*) FROM facts WHERE family='mango'").fetchone()[0] == 1
        pear = _row(conn, shopify_handle="asian-pear")
        assert "60 mg" not in facts.prompt_block(conn, pear)

    def test_research_is_told_what_the_family_already_knows(self, conn, tmp_path):
        catalog.sync(conn, fetch=lambda: FEED)
        pem = _row(conn, shopify_handle="honey-mango")
        hub.save_research(conn, pem, {"facts": [{**RESEARCH["facts"][1], "scope": "family"}]}, "x")
        box = _row(conn, shopify_handle="mango-variety-box")
        with conn:
            rid = conn.execute("INSERT INTO product_research (product_id, created_at) VALUES (?, 'now')", (box["id"],)).lastrowid
        client = FakeClient([_resp("notes", searches=2)], [_resp(json.dumps({"taste": "", "facts": [], "quotes": []}))])
        hub.run_research(lambda: get_connection(tmp_path / "t.db"), rid, client=client)
        ask = client.calls[0][1]["messages"][0]["content"]
        assert "ALREADY KNOWN FOR THE MANGO FAMILY" in ask and "60 mg" in ask


def test_nothing_fills_by_itself_the_button_does(client):
    pid = client.post("/api/adstudio/products", json={"name": "Kiwi Berry", "product_type": "fruit"}).get_json()["id"]
    conn = get_connection(client.db_path)
    pem = conn.execute("SELECT id FROM adstudio_products WHERE shopify_handle='honey-mango'").fetchone()[0]
    assert _row(conn, id=pid)["fill_status"] == "" and not conn.execute("SELECT 1 FROM jobs WHERE kind='fill'").fetchone()
    conn.close()
    prof = client.get(f"/api/adstudio/product/{pem}/profile").get_json()
    assert prof["fill"]["status"] == "" and 0.5 < prof["fill_estimate"] < 1.5
    assert client.post(f"/api/adstudio/product/{pem}/fill").get_json()["job_id"]
    item = next(p for p in client.get("/api/adstudio/catalog").get_json()["products"] if p["id"] == pem)
    assert item["filling"] and not item["researched"]


# ── house rule: no origin claims ────────────────────────────────────────────
def test_no_origin_claims_in_facts_or_hooks():
    from creative_intelligence.adstudio import hook_university as HU
    assert HU.origin_claim("Grown in Binh Dinh province, Vietnam")
    assert HU.origin_claim("Imported from Mexico every spring")
    assert not HU.origin_claim("Asian Pear is crisp like an apple, juicy like a pear", "Asian Pear")
    assert not HU.origin_claim("Keitt Mango (California Grown) stays green when ripe", "Keitt Mango (California Grown)")
    f = {"text": "It is a Vietnamese variety.", "category": "characteristic", "source_url": "https://x.example",
         "confidence": "high", "use": True}
    assert hub.judge(f, "shop.testfruit.example")[0] == "do_not_use"
    assert hub.judge({**f, "text": "Elephant mangoes are named for their size.", "category": "growing_region"},
                     "shop.testfruit.example")[0] == "do_not_use"
    assert hub.judge({**f, "text": "The flesh is fibreless."}, "x")[0] == "approved"             # auto-approved
    assert hub.judge({**f, "text": "Each one weighs about 2 lb."}, "x")[0] == "pending"          # numbers wait
    assert hub.judge({**f, "text": "Arrives in 2 days.", "category": "shipping"}, "x")[0] == "pending"
    ok, bad = HU.screen([{"hook_text": "Honey Mango came all the way from Vietnam to your kitchen table", "archetype": "A1"}],
                        [], "Honey Mango")
    assert not ok and "origin_claim" in bad[0]["qa_flags"]
    assert "Never claim where the fruit comes from" in HU.system()
    assert "origin" not in " ".join(HU.ANGLES).lower() and "ripeness" in HU.request("PEM", 5, HU.ANGLES[0], [])
    from creative_intelligence import prompts
    assert "Do not research or report where the product comes from" in prompts.text("hub-research-system")


def test_hook_critic_keeps_the_best_and_says_why():
    """F4: the critic scores candidates, keeps the best n, applies its edits and says why. No live API."""
    import json as _json
    from types import SimpleNamespace
    from creative_intelligence.adstudio import chat
    hooks = [{"headline": "Amazing mango!", "hook_text": "Amazing mango!"},
             {"headline": "No fibre to floss out", "hook_text": "Love mango but hate the strings?"},
             {"headline": "Ripe on your counter in 2 days", "hook_text": "Tired of mangoes that never ripen?"}]
    body = {"reviews": [{"i": 0, "score": 1, "headline": "", "hook_text": "", "reason": "vague praise"},
                        {"i": 1, "score": 4.5, "headline": "", "hook_text": "", "reason": "named frustration"},
                        {"i": 2, "score": 4, "headline": "Ripe on your counter", "hook_text": "", "reason": "shortened"}]}
    sent = []
    fake = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: sent.append(kw) or SimpleNamespace(
        content=[SimpleNamespace(type="text", text=_json.dumps(body))])))
    out = chat.critique_hooks({"name": "Honey Mango"}, hooks, 2, ["sweet like candy"], client=fake)
    assert [h["headline"] for h in out] == ["No fibre to floss out", "Ripe on your counter"]
    assert out[0]["critic_note"] == "named frustration" and out[1]["critic_score"] == 4.0
    assert "sweet like candy" in sent[0]["messages"][0]["content"] and sent[0]["model"] == chat.CRITIC_MODEL
    broken = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: (_ for _ in ()).throw(RuntimeError("down"))))
    assert chat.critique_hooks({"name": "x"}, hooks, 2, client=broken) == hooks[:2]       # never blocks hooks
