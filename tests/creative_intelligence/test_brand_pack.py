"""The brand pack: brands/<BRAND>/ holds everything brand-specific; the engine reads it through brand.py."""
from __future__ import annotations

import pytest

from creative_intelligence import brand, prompts


@pytest.fixture
def pack(tmp_path, monkeypatch):
    """A small brand pack in a temporary folder, selected with BRAND_DIR."""
    (tmp_path / "prompts").mkdir()
    (tmp_path / "shots").mkdir()
    (tmp_path / "brand.yaml").write_text(
        "name: Acme Teas\nshort: ACM\nwebsite: acme.example\ndescription: a loose-leaf tea shop\n"
        "packaging: tin caddy\ncolors: {accent: '#123456', accent_ink: '#ffffff'}\n"
        "store: {type_map: {tea: other}, season_in_tags: [fresh-harvest]}\n"
        "market: {brands: [{name: Rival Tea, tier: A}], keywords: [loose leaf tea]}\n")
    (tmp_path / "prompts" / "copy-chat-system.v1.txt").write_text("You write for ${brand}, ${brand_description}.")
    monkeypatch.setenv("BRAND_DIR", str(tmp_path))
    brand.reload()
    prompts.versions.cache_clear()
    prompts._raw.cache_clear()
    prompts.shot_cards.cache_clear()
    yield tmp_path
    monkeypatch.undo()
    brand.reload()
    prompts.versions.cache_clear()
    prompts._raw.cache_clear()
    prompts.shot_cards.cache_clear()


def test_sample_pack_loads(monkeypatch):
    monkeypatch.delenv("BRAND_DIR")
    monkeypatch.setenv("BRAND", "sample")
    brand.reload()
    try:
        assert brand.name() == "Example Brand" and brand.short() == "EXB"
        assert brand.get("colors.accent").startswith("#") and brand.rgb("accent") == (47, 125, 91)
        assert brand.path("logo.png") is not None and brand.get("presenters")
    finally:
        monkeypatch.undo()
        brand.reload()


def test_pack_values_and_defaults(pack):
    assert (brand.name(), brand.short(), brand.get("packaging")) == ("Acme Teas", "ACM", "tin caddy")
    assert brand.get("timezone") == "UTC" and brand.get("rules.no_origin_claims") is False   # defaults fill gaps
    assert brand.user_agent() == "ACM-AdStudio/1.0"


def test_a_pack_prompt_replaces_the_core_one_and_placeholders_fill(pack):
    assert prompts.text("copy-chat-system") == "You write for Acme Teas, a loose-leaf tea shop."
    assert "Acme Teas" in prompts.text("hub-research-system")                 # core prompt, brand filled in
    assert "${brand" not in prompts.text("hub-research-system")


def test_shot_cards_read_the_brand_packaging(pack):
    cards = prompts.shot_cards()
    assert any("tin caddy" in str(c) for c in cards.values())


def test_no_core_file_names_a_brand():
    """The brand's name and site live only in the pack, never in core code, prompts or knowledge."""
    from pathlib import Path
    root = Path(brand.ROOT)
    words = (brand.name(), brand.get("website"))
    for folder in ("creative_intelligence", "knowledge", "config"):
        for f in (root / folder).rglob("*"):
            if f.suffix in (".py", ".txt", ".yaml", ".md", ".html", ".css", ".sql") and "__pycache__" not in f.parts:
                text = f.read_text(encoding="utf-8", errors="ignore")
                assert not any(w in text for w in words), f


def test_brand_css_and_logo_are_served(tmp_path, monkeypatch):
    from creative_intelligence.db import get_connection, init_db
    init_db(tmp_path / "t.db")
    import creative_intelligence.webapp.app as app_module
    monkeypatch.setattr("creative_intelligence.webapp.common.db", lambda: get_connection(tmp_path / "t.db"))
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        css = c.get("/brand.css")
        assert css.status_code == 200 and f"--accent:{brand.get('colors.accent')}" in css.get_data(as_text=True)
        assert c.get("/brand/logo.png").mimetype == "image/png"
        assert 'src="/brand/logo.png"' in c.get("/products").get_data(as_text=True)


def test_market_is_seeded_from_the_pack(pack, tmp_path):
    from creative_intelligence.adstudio import market
    from creative_intelligence.db import get_connection, init_db
    init_db(tmp_path / "m.db")
    conn = get_connection(tmp_path / "m.db")
    assert [b["name"] for b in market.brands(conn)] == ["Rival Tea"]
    assert [k["term"] for k in market.keywords(conn)] == ["loose leaf tea"]
    conn.close()


def test_catalog_uses_the_pack_store_rules(pack):
    from creative_intelligence.adstudio import catalog
    assert catalog.map_type("Tea: Sencha") == "other"
    assert catalog.season({"tags": ["Fresh-Harvest"]}) == "in"
