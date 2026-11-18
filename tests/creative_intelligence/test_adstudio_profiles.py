"""Product type (fruit | candle | other) drives Ad Studio's hooks and image prompts."""
from __future__ import annotations

import pytest

from creative_intelligence.adstudio.chat import _product_line, _system
from creative_intelligence.adstudio.concepts import build_image_prompt
from creative_intelligence.adstudio.profiles import PROFILES, normalize_type, profile
from creative_intelligence.db import get_connection, init_db


def _p(t, **kw):
    return {"name": "X", "product_type": t, "scent_notes": "notes",
            "physical_desc": "green skin, white flesh", **kw}


def test_normalize_type():
    assert normalize_type("Fruit") == "fruit"
    assert normalize_type("nope") == normalize_type(None) == "other"


def test_candle_prompt_unchanged_rules():
    s = build_image_prompt(_p("candle"), "SCENE")
    assert "LABEL" in s and "Rosé" in s and "hands/fingers/people" in s
    assert "WHAT IT LOOKS LIKE" not in s


def test_fruit_prompt_is_fruit_specific():
    s = build_image_prompt(_p("fruit", varieties="A — red — pink"), "SCENE")
    assert "how THIS fruit looks" in s and "WHAT IT LOOKS LIKE\ngreen skin" in s
    assert "VARIETIES" in s and "PLU" in s
    assert "jar" not in s and "Rosé" not in s and "hands/fingers/people" not in s


def test_fruit_menu_has_no_candle_archetypes():
    assert "flame_macro" not in PROFILES["fruit"]["formats"]
    assert "flame_macro" not in PROFILES["other"]["formats"]
    assert "flame_macro" in PROFILES["candle"]["formats"]


def test_hook_system_follows_type():
    assert "TASTE" in _system(_p("fruit")) and "ATMOSPHERE" not in _system(_p("fruit"))
    assert "ATMOSPHERE" in _system(_p("candle"))
    assert "Flavor notes: notes" in _product_line(_p("fruit"))
    assert "Scent notes (the ONLY ingredients): notes" in _product_line(_p("candle"))
    assert profile({})["label"] == "Other"


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "t.db"
    init_db(db_path)
    import creative_intelligence.webapp.app as app_module
    monkeypatch.setattr("creative_intelligence.webapp.common.db", lambda: get_connection(db_path))
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


def test_create_and_change_type(client):
    pid = client.post("/api/adstudio/products",
                      json={"name": "Mango", "product_type": "fruit", "varieties": "v"}).get_json()["id"]
    p = client.get(f"/api/adstudio/product/{pid}").get_json()["product"]
    assert (p["product_type"], p["varieties"]) == ("fruit", "v")
    client.post(f"/api/adstudio/product/{pid}/update", json={"product_type": "bogus"})
    assert client.get(f"/api/adstudio/product/{pid}").get_json()["product"]["product_type"] == "other"
    cfg = client.get("/api/adstudio/config").get_json()["product_types"]
    assert set(cfg) == {"fruit", "candle", "other", "gift_box"} and "First Bite" in cfg["fruit"]["archetypes"]


def test_backfill_existing_products(tmp_path):
    db_path = tmp_path / "old.db"
    init_db(db_path)
    conn = get_connection(db_path)
    with conn:
        conn.execute("INSERT INTO adstudio_products (name, product_type) VALUES ('Guava Rosé Candle', '')")
        conn.execute("INSERT INTO adstudio_products (name, product_type) VALUES ('Kiwi berry', '')")
        conn.execute("DROP TABLE schema_version")          # a database from before numbered migrations
    conn.close()
    from creative_intelligence import db as db_module
    db_module._UP_TO_DATE.clear()
    init_db(db_path)
    conn = get_connection(db_path)
    got = dict(conn.execute("SELECT name, product_type FROM adstudio_products").fetchall())
    conn.close()
    assert got == {"Guava Rosé Candle": "candle", "Kiwi berry": "fruit"}


def test_pages_and_nav(client):
    """Four sections and their stages; old pages redirect to their new home (query kept)."""
    for path in ("/products", "/images/create", "/images/review", "/images/compose", "/images/shots", "/video/create",
                 "/video/shots", "/video/edit", "/video/review", "/gallery/assets", "/gallery/performance", "/admin"):
        r = client.get(path)
        assert r.status_code == 200, path
        html = r.get_data(as_text=True)
        assert all(f'href="/{s}" data-sec="{s}"' in html for s in ("products", "images", "video", "gallery")), path
        assert 'href="/admin"' in html and 'class="gnav"' not in html
        for gone in ("/test", "/copilot", "/render", "/collection", "/labels", "/core"):
            assert f'href="{gone}"' not in html
    review = client.get("/images/review").get_data(as_text=True)
    assert 'data-section="images" data-stage="review"' in review and '<em>2</em>Review' in review
    assert 'aria-current="step"' in review and 'class="stage on"' in review
    assert '<span class="soon">soon</span>' in client.get("/video/create").get_data(as_text=True)
    assert 'id="stagesBar" aria-label="Stages" hidden' in client.get("/products").get_data(as_text=True)
    assert client.get("/images/bogus").status_code == 404
    for old, new in (("/adstudio?product_id=3", "/products?product_id=3"), ("/core?product_id=3", "/images/shots?product_id=3"),
                     ("/adstudio?product_id=3&compose_core=9", "/images/compose?product_id=3&compose_core=9"),
                     ("/adstudio?drive=connected", "/gallery/assets?drive=connected"), ("/images", "/images/create"),
                     ("/video", "/video/create"), ("/gallery", "/gallery/assets")):
        r = client.get(old)
        assert r.status_code == 302 and r.location.endswith(new), old


def test_gallery_shows_every_product_with_one_status_path(client):
    from creative_intelligence.webapp.routes.gallery import stage
    assert stage({"kind": "image", "review_status": "", "qa": None}) == "draft"
    assert stage({"kind": "image", "review_status": "", "qa": {"total": 4}}) == "review"
    assert stage({"kind": "compose", "review_status": "approved", "qa": {}}) == "approved"
    assert stage({"kind": "compose", "is_ready": 1, "qa": {}}) == "approved"
    assert stage({"kind": "ai", "review_status": "approved", "exported": True, "qa": {}}) == "exported"
    assert stage({"kind": "ai", "review_status": "approved", "asset": {"meta_ad_id": "1"}}) == "live"
    assert stage({"kind": "image", "review_status": "rejected", "qa": {}}) == "rejected"
    d = client.get("/api/adstudio/gallery").get_json()                       # no product: every product
    assert d["items"] == [] and d["stages"]["review"] == "In review"
    assert client.get("/api/adstudio/stage-counts?product_id=1").get_json() == {"images.review": 0, "video.review": 0}


def test_retired_pages_say_so(client):
    r = client.get("/labels")
    assert r.status_code == 410 and "This page was retired" in r.get_data(as_text=True)
    r = client.post("/api/copilot/session", json={})
    assert r.status_code == 410 and r.get_json()["error"] == "This endpoint was retired."
    assert client.get("/api/adstudio/export-core?product_id=1").status_code != 410  # live lookalike
    assert client.get("/nope").status_code == 404
