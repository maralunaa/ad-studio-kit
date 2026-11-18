"""T3 — AI Ads text moves into Compose: text-free images, the 12 layouts as templates,
copy drawn by code, prices from Shopify.

No live API: image generation, Shopify and Claude are faked; images are real PNGs.
"""
from __future__ import annotations

import json

import pytest
from PIL import Image

from creative_intelligence.adstudio import core as C
from creative_intelligence.adstudio import textads as T
from creative_intelligence.adstudio.render_template import aspect_key, render
from creative_intelligence.adstudio.templates import TEMPLATES, copy_fields
from creative_intelligence.db import get_connection, init_db

COPY = {"headline": "Silky custard texture, zero strings", "sub": "Farm-ripened when the pink skin appears",
        "cta": "Get Yours", "price": "$35", "badge": "In season now",
        "review": "Best mango I've ever had. It really tastes like custard.", "author": "Dana R.",
        "items": ["Ripe in 2 days on your counter", "Custard texture, zero fibre"],                  # S3 lo-fi lines
        "reviews": [{"id": 1, "text": "Best mango I've ever had.", "author": "Dana R.", "rating": 5,  # library quotes
                     "source": "Google review", "posted_on": "2026-09-12", "kind": "review"}]}
SIZES = {"9:16": (1080, 1920), "4:5": (1080, 1350), "1:1": (1080, 1080)}


@pytest.fixture
def photo(tmp_path):
    def make(size):
        p = tmp_path / f"base_{size[0]}x{size[1]}.png"
        Image.new("RGB", size, (90, 120, 70)).save(p)
        return str(p)
    return make


class TestTemplates:
    def test_twelve_formats_plus_scene(self):
        assert set(T.VARIANTS) <= set(TEMPLATES) and "scene" in TEMPLATES and len(T.VARIANTS) == 12

    @pytest.mark.parametrize("size", list(SIZES))
    @pytest.mark.parametrize("key", list(TEMPLATES))
    def test_every_template_draws_exactly_the_copy(self, photo, key, size):
        img, drawn = render(photo(SIZES[size]), key, COPY)
        assert img.size == SIZES[size]
        assert drawn == {f: COPY[f] for f in copy_fields(key)}            # every field, exact, never cut

    def test_optional_fields_left_empty_are_not_drawn(self, photo):
        _, drawn = render(photo(SIZES["4:5"]), "benefit_stack", {**COPY, "price": ""})
        assert "price" not in drawn and drawn["headline"] == COPY["headline"]

    def test_aspect_keys(self):
        assert [aspect_key(*SIZES[k]) for k in SIZES] == list(SIZES)


class TestTextFreePrompt:
    def test_no_copy_in_the_image_prompt(self):
        product = {"name": "Rosy Sunshine Mango", "product_type": "fruit", "physical_desc": "blush-pink skin"}
        brief = {"visual_direction": "warm orchard light"}
        p = T.build_textfree_prompt(product, brief, "bold_type", "9:16")
        assert "NO TEXT of any kind" in p and "COMPOSITION — BOLD TYPE" in p and "warm orchard light" in p
        assert "ON-IMAGE TEXT" not in p
        scene = T.build_textfree_prompt(product, brief, "x", "4:5", scene="Halves on an oak board")
        assert "Halves on an oak board" in scene and "NO TEXT of any kind" in scene


@pytest.fixture
def client(tmp_path, monkeypatch):
    import creative_intelligence.webapp.app as app_module
    from creative_intelligence.adstudio import generate, store
    path = tmp_path / "t.db"
    init_db(path)
    monkeypatch.setattr("creative_intelligence.webapp.common.db", lambda: get_connection(path))
    monkeypatch.setenv("TEXTFREE_AI_ADS", "1")
    monkeypatch.chdir(tmp_path)
    calls = {"generate": 0, "textcheck": 0}

    def fake_generate(prompt, uris, aspect):
        calls["generate"] += 1
        calls["last_prompt"] = prompt
        return "fake://img"

    def fake_save(src, pid, aid, folder=None):
        f = tmp_path / f"ad{aid}_p{pid}.png"
        Image.new("RGB", (1080, 1920), (60, 80, 50)).save(f)
        return str(f)
    monkeypatch.setattr(generate, "generate_ad", fake_generate)
    monkeypatch.setattr(store, "save_generated_image", fake_save)
    monkeypatch.setattr(store, "textads_dir", lambda: tmp_path)
    monkeypatch.setattr("creative_intelligence.webapp.common.textcheck_later", lambda ids: calls.__setitem__("textcheck", calls["textcheck"] + 1))
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        c.calls = calls
        c.conn = lambda: get_connection(path)
        yield c


def _ad(client, monkeypatch, variant="direct_response"):
    pid = client.post("/api/adstudio/products", json={"name": "Rosy Sunshine Mango", "product_type": "fruit"}).get_json()["id"]
    variants = {k: {"headline": f"{k} line", "sub": f"{k} sub"} for k in T.VARIANTS}
    monkeypatch.setattr(T, "draft_brief", lambda *a, **k: T.normalize_brief(
        {"angle": "custard", "cta": "Get Yours", "primary_texts": ["p1"], "variants": variants}))
    b = client.post(f"/api/adstudio/product/{pid}/brief/draft", json={"pattern": "none", "idea": "texture"}).get_json()
    client.post(f"/api/adstudio/brief/{b['id']}/variants", json={})
    ads = client.get(f"/api/adstudio/textads?brief_id={b['id']}").get_json()["ads"]
    return pid, next(a for a in ads if a["variant"] == variant)


class TestAiAdsTextFree:
    def test_run_makes_clean_image_then_draws_copy(self, client, monkeypatch):
        pid, ad = _ad(client, monkeypatch)
        assert client.post(f"/api/adstudio/textad/{ad['id']}/run").get_json()["status"] == "done"
        assert "NO TEXT of any kind" in client.calls["last_prompt"] and "direct_response line" not in client.calls["last_prompt"]
        row = dict(client.conn().execute("SELECT * FROM adstudio_textads WHERE id=?", (ad["id"],)).fetchone())
        assert row["base_path"].endswith("_base.png") and row["image_path"] != row["base_path"]
        assert Image.open(row["image_path"]).size == (1080, 1920)
        check = json.loads(row["textcheck_json"])
        assert row["textcheck_status"] == "code"
        assert check["drawn"] == {"headline": "direct_response line", "sub": "direct_response sub", "cta": "Get Yours"}
        assert client.calls["textcheck"] == 0                               # code-drawn text needs no OCR

    def test_edit_text_redraws_without_a_new_image(self, client, monkeypatch):
        pid, ad = _ad(client, monkeypatch)
        client.post(f"/api/adstudio/textad/{ad['id']}/run")
        n = client.calls["generate"]
        r = client.post(f"/api/adstudio/textad/{ad['id']}/regenerate", json={"headline": "Custard, not fruit-water"}).get_json()
        assert r["redrawn"] and r["drawn"]["headline"] == "Custard, not fruit-water"
        assert client.calls["generate"] == n                                # free and instant
        # a fix note still asks the image model for a new picture
        client.post(f"/api/adstudio/textad/{ad['id']}/regenerate", json={"fix": "show the seed"})
        assert client.calls["generate"] == n + 1


class TestComposePrice:
    def _image(self, client, pid, tmp_path, shopify_url="https://shop.testfruit.example/products/rosy-sunshine-mango"):
        img = tmp_path / "base.png"
        Image.new("RGB", (1080, 1350), (90, 60, 40)).save(img)
        c = client.conn()
        with c:
            c.execute("UPDATE adstudio_products SET shopify_url=?, info_json=? WHERE id=?",
                      (shopify_url, json.dumps({"rows": [{"variant_title": "5lb | 6-8 pcs", "price": "$45"}]}), pid))
            return c.execute("INSERT INTO adstudio_images (product_id, concept, prompt, status, image_path)"
                             " VALUES (?, 'x', 'x', 'done', ?)", (pid, str(img))).lastrowid

    def _compose(self, client, image_id, **layout):
        return client.post("/api/adstudio/compose", json={
            "image_id": image_id, "headline": "Ripe on arrival", "subhead": "Farm to door", "cta": "Get Yours",
            "layout": {"template": "benefit_stack", **layout}})

    def test_price_read_live_from_shopify(self, client, monkeypatch, tmp_path):
        pid, _ = _ad(client, monkeypatch)
        iid = self._image(client, pid, tmp_path)
        monkeypatch.setattr(C, "shopify_prices", lambda url: [{"title": "2lb | 2-3 pcs", "price": "$35", "available": True},
                                                            {"title": "5lb | 6-8 pcs", "price": "$49", "available": True}])
        r = self._compose(client, iid, show_price=True, price_variant="5lb | 6-8 pcs")
        layout = json.loads(client.conn().execute("SELECT layout_json FROM adstudio_composites WHERE id=?",
                                                  (r.get_json()["id"],)).fetchone()[0])
        assert (layout["price"], layout["price_source"]) == ("$49", "shopify live")

    def test_price_falls_back_to_import_when_shopify_is_down(self, client, monkeypatch, tmp_path):
        pid, _ = _ad(client, monkeypatch)
        iid = self._image(client, pid, tmp_path)
        monkeypatch.setattr(C, "shopify_prices", lambda url: (_ for _ in ()).throw(OSError("down")))
        r = self._compose(client, iid, show_price=True, price_variant="5lb | 6-8 pcs")
        layout = json.loads(client.conn().execute("SELECT layout_json FROM adstudio_composites WHERE id=?",
                                                  (r.get_json()["id"],)).fetchone()[0])
        assert (layout["price"], layout["price_source"]) == ("$45", "shopify at import")

    def test_price_off_by_default_and_needs_shopify(self, client, monkeypatch, tmp_path):
        pid, _ = _ad(client, monkeypatch)
        iid = self._image(client, pid, tmp_path, shopify_url="")
        assert self._compose(client, iid).status_code == 200                 # no price asked: fine
        r = self._compose(client, iid, show_price=True)
        assert r.status_code == 400 and "no Shopify link" in r.get_json()["error"]

    def test_format_price(self):
        assert (C.format_price(3500), C.format_price(3550), C.format_price(129900)) == ("$35", "$35.50", "$1,299")
