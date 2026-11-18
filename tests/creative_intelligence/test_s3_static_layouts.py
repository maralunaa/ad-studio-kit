"""S3: new static layouts. Native lo-fi (notes list, text thread, post-it, real comments, review mashup), the
offer-first banner and the listicle card are Compose layouts and presets from styles.yaml. Quotes only ever come
from the real reviews library, exact, with author, source and date; the brand's own lines are claims-checked."""
from __future__ import annotations

import json

import pytest
from PIL import Image

from creative_intelligence import knowledge
from creative_intelligence.adstudio import qa, reviews
from creative_intelligence.adstudio.render_template import render
from creative_intelligence.adstudio.templates import TEMPLATES, review_kind
from creative_intelligence.db import get_connection, init_db

NEW = ("notes_list", "text_thread", "post_it", "comments_screenshot", "review_mashup", "offer_banner", "listicle_card")


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("CI_DB_PATH", str(tmp_path / "data" / "t.db"))
    init_db(tmp_path / "data" / "t.db")
    c = get_connection(tmp_path / "data" / "t.db")
    with c:
        c.execute("INSERT INTO adstudio_products (id, name, product_type) VALUES (1, 'Honey Mango', 'fruit')")
        c.execute("INSERT INTO adstudio_products (id, name, product_type) VALUES (2, 'Lychee', 'fruit')")
    yield c
    c.close()


def _add(conn, **kw):
    d = {"kind": "review", "text": "Best mango I've had in years.", "author": "Maria Lopez", "rating": 5,
         "source": "google", "posted_on": "2026-09-12", "product_id": 1, **kw}
    return reviews.add(conn, d)


def test_library_keeps_only_real_attributed_dated_quotes(conn):
    assert "exact words" in _add(conn, text="")[1]
    assert "posted" in _add(conn, source="made_up")[1]
    assert "date" in _add(conn, posted_on="last week")[1]
    assert "author" in _add(conn, author=" ")[1]
    rid, _ = _add(conn)
    cid, _ = _add(conn, kind="comment", text="Arrived perfect, kids loved it.", author="Jo Smith", rating=None,
                  source="meta_comment", product_id=None)
    _add(conn, product_id=2, text="Lychees were unreal.")
    rows = reviews.listing(conn, 1)
    assert [r["id"] for r in rows] == [cid, rid] or {r["id"] for r in rows} == {rid, cid}      # shop-wide included
    shown = {r["id"]: r["shown_as"] for r in rows}
    assert shown[rid] == "Maria L." and shown[cid] == "J."                   # comments: name hidden
    assert reviews.resolve(conn, [cid, 999, rid], 1, "review") == [
        {"id": rid, "text": "Best mango I've had in years.", "author": "Maria L.", "rating": 5,
         "source": "Google review", "posted_on": "2026-09-12", "kind": "review"}]   # unknown and wrong-kind ids dropped


@pytest.mark.parametrize("size", [(1080, 1920), (1080, 1350), (1080, 1080)])
def test_new_layouts_draw_inside_the_safe_zone_with_contrast(tmp_path, size):
    p = tmp_path / "base.png"
    Image.new("RGB", size, (200, 170, 140)).save(p)
    revs = [{"id": i, "text": t, "author": "Maria L.", "rating": r, "source": "Google review", "posted_on": "2026-09-12"}
            for i, (t, r) in enumerate([("Best mango I've had in years.", 5), ("Arrived perfect.", None)], 1)]
    copy = {"headline": "Why people reorder", "sub": "Free shipping over $60", "cta": "Shop now", "badge": "No. 3",
            "items": ["Ripe in 2 days on your counter", "Custard texture, zero fibre"], "reviews": revs}
    for t in NEW:
        m: dict = {}
        render(str(p), t, copy, m)
        assert qa.outside_safe_zone(m) == [], t
        assert m["min_contrast"] >= 4.5, t
    m: dict = {}
    render(str(p), "review_mashup", copy, m)
    assert m["reviews_shown"] == [1, 2] and m["words"]["review"] == 8         # real quotes: counted apart


def test_a_quote_that_does_not_fit_is_left_out_not_cut(tmp_path):
    p = tmp_path / "base.png"
    Image.new("RGB", (1080, 1080), (200, 170, 140)).save(p)
    long = " ".join(["The lychees were unreal and the mangoes even better."] * 12)
    revs = [{"id": 1, "text": long, "author": "Sam K.", "rating": 4, "source": "Shopify review", "posted_on": "2026-08-30"},
            {"id": 2, "text": "Arrived perfect.", "author": "J.", "rating": None, "source": "Comment", "posted_on": "2026-10-01"}]
    m: dict = {}
    render(str(p), "review_mashup", {"headline": "x", "reviews": revs}, m)
    assert m["reviews_shown"] == [2]


def test_stars_only_show_a_real_rating(tmp_path):
    p = tmp_path / "base.png"
    Image.new("RGB", (1080, 1350), (90, 120, 70)).save(p)
    no, _ = render(str(p), "social_proof", {"headline": "x", "review": "Lovely.", "author": "A."})
    yes, _ = render(str(p), "social_proof", {"headline": "x", "review": "Lovely.", "author": "A.", "rating": 4})
    assert list(no.getdata()) != list(yes.getdata())


def test_styles_yaml_presets(conn):
    lofi = knowledge.style("native_lofi_static")
    assert lofi["status"] == "live" and set(lofi["route"]["templates"]) == {
        "notes_list", "text_thread", "post_it", "handwritten_letter", "comments_screenshot", "review_mashup"}
    assert "offer_banner" in knowledge.style("harvest_drop")["route"]["templates"]
    assert knowledge.style("listicle_quiz")["route"]["templates"] == ["listicle_card"]
    assert review_kind("comments_screenshot") == "comment" and review_kind("review_mashup") == "review"
    assert review_kind("social_proof") == "review" and review_kind("notes_list") is None
    assert set(NEW) <= set(TEMPLATES)


def test_bulk_and_fan_out_leave_out_layouts_that_need_lines_or_reviews(conn):
    from creative_intelligence.adstudio import batch
    types = {t["key"]: t for t in batch.types_for({"product_type": "fruit"})}
    for k in ("notes_list", "review_mashup", "comments_screenshot", "social_proof"):
        assert types[k]["why"] and not types[k]["on"], k
    assert "One ad" in types["offer_banner"]["why"]                       # text layouts are One ad only (2026-10-23)


def test_compose_resolves_lines_and_real_reviews(conn, tmp_path, monkeypatch):
    import creative_intelligence.webapp.app as app_module
    monkeypatch.setattr("creative_intelligence.webapp.common.db", lambda: get_connection(tmp_path / "data" / "t.db"))
    monkeypatch.setattr("creative_intelligence.webapp.common._claims", lambda *a, **k: None)
    monkeypatch.setattr("creative_intelligence.adstudio.store.composites_dir", lambda: tmp_path)
    img = tmp_path / "img.png"
    Image.new("RGB", (1080, 1350), (200, 170, 140)).save(img)
    with conn:
        conn.execute("INSERT INTO adstudio_images (id, product_id, image_path) VALUES (5, 1, ?)", (str(img),))
    rid, _ = _add(conn)
    c = app_module.app.test_client()
    body = {"image_id": 5, "headline": "Why people reorder", "cta": "Shop now"}
    r = c.post("/api/adstudio/compose", json={**body, "layout": {"template": "review_mashup", "review": "I made this up"}})
    assert r.status_code == 400 and "real review" in r.get_json()["error"]          # typed quotes never drawn
    r = c.post("/api/adstudio/compose", json={**body, "layout": {"template": "review_mashup", "review_ids": [rid]}})
    assert r.status_code == 200
    lay = json.loads(conn.execute("SELECT layout_json FROM adstudio_composites WHERE id=?", (r.get_json()["id"],)).fetchone()[0])
    assert lay["reviews"][0]["text"] == "Best mango I've had in years." and lay["reviews"][0]["posted_on"] == "2026-09-12"
    assert "review" not in lay
    r = c.post("/api/adstudio/compose", json={**body, "layout": {"template": "notes_list"}})
    assert r.status_code == 400 and "short lines" in r.get_json()["error"]
    r = c.post("/api/adstudio/compose", json={**body, "layout": {"template": "notes_list", "items": ["Ripe in 2 days", " "]}})
    assert r.status_code == 200


def test_forged_quotes_in_a_layout_are_never_drawn(conn, tmp_path, monkeypatch):
    """A layout that arrives with its own `reviews` list is replaced by the library's, on every compose path."""
    from creative_intelligence.webapp.routes.compose import _resolve_copy_lists
    rid, _ = _add(conn)
    lay = {"template": "review_mashup", "reviews": [{"id": 7, "text": "Invented praise", "author": "Fake F."}]}
    with pytest.raises(ValueError):
        _resolve_copy_lists(conn, 1, lay)
    lay = {"template": "review_mashup", "review_ids": [rid], "reviews": [{"id": 7, "text": "Invented praise"}]}
    _resolve_copy_lists(conn, 1, lay)
    assert [r["text"] for r in lay["reviews"]] == ["Best mango I've had in years."]
    lay = {"template": "social_proof", "review": "Typed in", "author": "Me", "rating": 5, "review_ids": [rid]}
    _resolve_copy_lists(conn, 1, lay)
    assert lay["review"] == "Best mango I've had in years." and lay["author"] == "Maria L. · Google review"


def test_handwritten_letter(tmp_path):
    from pathlib import Path
    from creative_intelligence.adstudio.render_template import FONT_DIR, WEIGHTS
    assert (FONT_DIR / WEIGHTS["hand"]).exists() and "SIL Open Font License" in (FONT_DIR / "Caveat-OFL.txt").read_text()
    p = tmp_path / "base.png"
    Image.new("RGB", (1080, 1920), (160, 120, 80)).save(p)
    m: dict = {}
    copy = {"headline": "Dear fruit lover,", "sub": "the Test Fruit Co team",
            "items": ["We pick every mango when it's ripe on the tree.", "Leave it on the counter for two days.",
                      "Enjoy it cold, with a spoon."]}
    _, drawn = render(str(p), "handwritten_letter", copy, m)
    assert drawn == copy and qa.outside_safe_zone(m) == [] and m["min_contrast"] >= 4.5
    assert m["lofi"] and qa.word_count_score(m) == 5.0                    # lo-fi limit (45), not 20
    assert qa.word_count_score({**m, "lofi": False}) == 2.0
    assert "handwritten_letter" in knowledge.style("native_lofi_static")["route"]["templates"]
