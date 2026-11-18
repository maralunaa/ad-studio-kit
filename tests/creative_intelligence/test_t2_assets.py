"""T2 — Asset IDs, tags, Meta linking, results on cards, grouped results, generation cost log.

No live API: Meta ads and their daily results are inserted as fixtures.
"""
from __future__ import annotations

import json

import pytest

from creative_intelligence.adstudio import assets, costs
from creative_intelligence.db import get_connection, init_db
from creative_intelligence.ingest import daily_insights as di


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)                 # cost log writes outputs/api.log here, not in the repo
    path = tmp_path / "t.db"
    init_db(path)
    c = get_connection(path)
    with c:
        c.execute("INSERT INTO adstudio_products (id, name, product_type) VALUES (1, 'Rosy Sunshine Mango', 'fruit')")
        c.execute("INSERT INTO creative_patterns (id, pattern_name, winner_count, creative_count, updated_at)"
                  " VALUES (7, 'hook_type × angle: hook_type:vs_supermarket + angle:flavor_experience', 3, 9, '2026-10-15')")
        c.execute("INSERT INTO adstudio_briefs (id, product_id, pattern_id, brief_json) VALUES (3, 1, 7, ?)",
                  (json.dumps({"angle": "custard texture"}),))
        for i, variant in enumerate(["bold_type", "bold_type", "minimal"], start=1):
            c.execute("INSERT INTO adstudio_textads (id, product_id, brief_id, kind, variant, title, status,"
                      " image_path, model, prompt) VALUES (?,1,3,'variant',?,?,'done','/x.png','google/nano-banana-pro','p')",
                      (i, variant, variant))
        c.execute("INSERT INTO adstudio_textads (id, product_id, brief_id, kind, variant, status)"
                  " VALUES (9, 1, 3, 'variant', 'premium', 'queued')")              # not finished: no asset
    yield c
    c.close()


def _meta_ad(c, ad_id, name, **day):
    di.upsert(c, [{"ad_id": ad_id, "ad_name": name, "date_start": day.get("date", "2026-10-14"),
                   "spend": day.get("spend", "100"), "impressions": "4000", "clicks": "80",
                   "actions": [{"action_type": "omni_purchase", "value": day.get("purchases", "4")},
                               {"action_type": "video_view", "value": "1000"}],
                   "action_values": [{"action_type": "omni_purchase", "value": day.get("revenue", "360")}],
                   "video_thruplay_watched_actions": [{"action_type": "video_view", "value": "250"}]}])


class TestIds:
    def test_one_id_per_finished_ad_with_versions(self, conn):
        assert assets.ensure_assets(conn) == 3
        rows = conn.execute("SELECT source_id, asset_id, concept_id FROM adstudio_assets ORDER BY source_id").fetchall()
        ids = [r["asset_id"] for r in rows]
        assert ids[0] == "EXB_b3_flavor-experience_vs-supermarket_bold-type_rosy-sunshine-mango_v01"
        assert ids[1].endswith("_bold-type_rosy-sunshine-mango_v02")       # same concept + format -> next version
        assert ids[2].endswith("_minimal_rosy-sunshine-mango_v01")
        assert {r["concept_id"] for r in rows} == {"B3"}                     # variants share the concept
        assert assets.ensure_assets(conn) == 0                               # idempotent; IDs never change

    def test_tags(self, conn):
        assets.ensure_assets(conn)
        a = dict(conn.execute("SELECT * FROM adstudio_assets WHERE source_id=1").fetchone())
        assert (a["angle"], a["hook_type"], a["format"], a["product_type"], a["source_tab"], a["image_model"]) == (
            "flavor_experience", "vs_supermarket", "bold_type", "fruit", "AI Ads", "google/nano-banana-pro")
        assert a["ai_generated"] == 1 and len(a["prompt_version"]) == 8

    def test_template_is_configurable(self, conn, monkeypatch):
        from creative_intelligence import config
        monkeypatch.setattr(config, "ASSET_ID_TEMPLATE", "EXB-{product}-{format}-{version:03d}")
        assets.ensure_assets(conn)
        first = conn.execute("SELECT asset_id FROM adstudio_assets ORDER BY id").fetchone()[0]
        assert first == "EXB-rosy-sunshine-mango-bold-type-001"

    def test_part_strips_separators(self):
        assert assets.part("Quality / authenticity_hook") == "quality-authentici"
        assert assets.part("") == "na"


class TestLinking:
    def test_auto_link_when_ad_name_contains_asset_id(self, conn):
        assets.ensure_assets(conn)
        aid = conn.execute("SELECT asset_id FROM adstudio_assets WHERE source_id=1").fetchone()[0]
        _meta_ad(conn, "120200000000000001", f"Oct test | {aid} | broad")
        _meta_ad(conn, "120200000000000002", "Some other ad")
        assert assets.link_from_meta(conn) == 1
        row = conn.execute("SELECT meta_ad_id, linked_by FROM adstudio_assets WHERE source_id=1").fetchone()
        assert tuple(row) == ("120200000000000001", "auto")
        assert assets.link_from_meta(conn) == 0                              # already linked: no change

    def test_exact_name_wins_over_contains(self, conn):
        assets.ensure_assets(conn)
        aid = conn.execute("SELECT asset_id FROM adstudio_assets WHERE source_id=1").fetchone()[0]
        _meta_ad(conn, "1201", f"{aid} copy")
        _meta_ad(conn, "1202", aid)
        assets.link_from_meta(conn)
        assert conn.execute("SELECT meta_ad_id FROM adstudio_assets WHERE source_id=1").fetchone()[0] == "1202"

    def test_manual_link_and_unlink(self, conn):
        assets.ensure_assets(conn)
        aid = conn.execute("SELECT asset_id FROM adstudio_assets WHERE source_id=2").fetchone()[0]
        assert assets.link_manual(conn, aid, "120299")
        assert conn.execute("SELECT linked_by FROM adstudio_assets WHERE asset_id=?", (aid,)).fetchone()[0] == "manual"
        assert assets.link_manual(conn, aid, None)
        assert conn.execute("SELECT meta_ad_id FROM adstudio_assets WHERE asset_id=?", (aid,)).fetchone()[0] is None
        assert not assets.link_manual(conn, "EXB_nope", "1")


class TestResults:
    def test_card_results(self, conn):
        assets.ensure_assets(conn)
        aid = conn.execute("SELECT asset_id FROM adstudio_assets WHERE source_id=1").fetchone()[0]
        _meta_ad(conn, "1301", aid, date="2026-10-13")
        _meta_ad(conn, "1301", aid, date="2026-10-14", spend="50", purchases="1", revenue="90")
        assets.link_from_meta(conn)
        r = assets.assets_for(conn, "textad", [1, 2])
        res = r[1]["results"]
        assert (res["days"], res["spend"], res["purchases"], res["cpa"], res["roas"], res["ctr"]) == (
            2, 150.0, 5, 30.0, 3.0, 2.0)
        assert (res["hook_rate"], res["hold_rate"]) == (25.0, 25.0)
        assert r[2]["results"] is None                                       # not linked yet

    def test_grouped_results(self, conn):
        assets.ensure_assets(conn)
        ids = [r[0] for r in conn.execute("SELECT asset_id FROM adstudio_assets ORDER BY source_id")]
        _meta_ad(conn, "1401", ids[0], spend="100", revenue="300")
        _meta_ad(conn, "1402", ids[2], spend="100", revenue="100")
        assets.link_from_meta(conn)
        by_format = {g["key"]: g for g in assets.aggregate(conn, "format")}
        assert by_format["bold_type"]["assets"] == 2 and by_format["bold_type"]["linked"] == 1
        assert by_format["bold_type"]["roas"] == 3.0 and by_format["minimal"]["roas"] == 1.0
        assert [g["key"] for g in assets.aggregate(conn, "format")][0] == "bold_type"   # best ROAS first
        with pytest.raises(ValueError):
            assets.aggregate(conn, "colour")


class TestCosts:
    def test_image_cost_logged_and_rolled_into_asset(self, conn, tmp_path):
        cost = costs.log_image(conn, "google/nano-banana-pro", "textad", 1)
        costs.log_image(conn, "google/nano-banana-pro", "textad", 1)        # a retry costs again
        assert cost == 0.18
        assets.ensure_assets(conn)
        assert conn.execute("SELECT cost_usd FROM adstudio_assets WHERE source_id=1").fetchone()[0] == 0.36
        log = (tmp_path / "outputs" / "api.log").read_text()
        assert "REPLICATE | google/nano-banana-pro | textad#1" in log

    def test_price_is_editable(self, conn):
        costs.seed_prices(conn)
        with conn:
            conn.execute("UPDATE model_prices SET usd=0.134, checked_at='2026-10-15' WHERE model='google/nano-banana-pro'")
        costs.seed_prices(conn)                                              # seeding never overwrites an edit
        assert costs.price(conn, "google/nano-banana-pro") == 0.134


class TestRoutes:
    @pytest.fixture
    def client(self, tmp_path, monkeypatch, conn):
        monkeypatch.setenv("CI_DB_PATH", str(tmp_path / "t.db"))
        from creative_intelligence.webapp import app as webapp
        return webapp.app.test_client()

    def test_gallery_cards_carry_asset_and_results(self, client, conn):
        ads = client.get("/api/adstudio/textads?done=1&product_id=1").get_json()["ads"]
        a = next(x for x in ads if x["id"] == 1)["asset"]
        assert a["asset_id"].startswith("EXB_b3_") and a["results"] is None
        _meta_ad(conn, "1501", a["asset_id"])
        assets.link_from_meta(conn)
        a = next(x for x in client.get("/api/adstudio/textads?done=1&product_id=1").get_json()["ads"]
                 if x["id"] == 1)["asset"]
        assert a["meta_ad_id"] == "1501" and a["results"]["roas"] == 3.6

    def test_link_route_validates(self, client):
        client.get("/api/adstudio/textads?done=1&product_id=1")             # registers assets
        aid = client.get("/api/adstudio/textads?done=1&product_id=1").get_json()["ads"][0]["asset"]["asset_id"]
        assert client.post(f"/api/adstudio/asset/{aid}/link", json={"meta_ad_id": "abc"}).status_code == 400
        assert client.post(f"/api/adstudio/asset/{aid}/link", json={"meta_ad_id": "120211"}).status_code == 200
        assert client.get(f"/api/adstudio/asset/{aid}").get_json()["meta_ad_id"] == "120211"

    def test_results_route(self, client):
        d = client.get("/api/adstudio/results?group=format").get_json()
        assert d["assets"] == 3 and d["linked"] == 0 and {r["key"] for r in d["rows"]} == {"bold_type", "minimal"}
        assert client.get("/api/adstudio/results?group=nope").status_code == 400
