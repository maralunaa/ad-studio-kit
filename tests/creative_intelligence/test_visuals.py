"""The visual board (feedback 2026-10-22): pictures to reuse (our best ads, decoded winners, competitor screenshots,
saved ads), "Make ours like this" (AI-designed, the picture guides composition only), several screenshots at once.
Meta, Claude and the image model are faked."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from PIL import Image

from creative_intelligence.adstudio import market, visuals
from creative_intelligence.db import get_connection, init_db


@pytest.fixture
def conn(tmp_path, monkeypatch):
    db = tmp_path / "data" / "t.db"
    monkeypatch.setenv("CI_DB_PATH", str(db))
    monkeypatch.setattr("creative_intelligence.config.CI_RENDER_OUTPUT_DIR", str(tmp_path / "vol" / "renders"))
    monkeypatch.chdir(tmp_path)
    init_db(db)
    c = get_connection(db)
    with c:
        c.execute("INSERT INTO adstudio_products (id, name, product_type) VALUES (1, 'Honey Mango', 'fruit')")
        c.execute("INSERT INTO adstudio_hooks (id, product_id, headline, source) VALUES (1, 1, 'Custard you can hold',"
                  " 'hook_university')")
    yield c
    c.close()


def _shot(tmp_path, name="s.png"):
    p = tmp_path / name
    Image.new("RGB", (400, 500), (200, 120, 60)).save(p)
    return p.read_bytes()


def test_board_lists_each_kind_and_makes_ours_like_this(conn, tmp_path, monkeypatch):
    path = market.store_screenshot(_shot(tmp_path), "image/png")
    with conn:
        conn.execute("INSERT INTO competitor_ads (source, page_name, body, media_path, media_kind, format, angle, added_at)"
                     " VALUES ('manual', 'Fruit Co', 'x', ?, 'image', 'static', 'gifting', 'now')", (path,))
    monkeypatch.setattr(visuals, "_ours", lambda conn: [{"kind": "ours", "id": "c1", "title": "Our ad", "note": "ROAS 4.0",
                                                         "image": "/x"}])
    items = visuals.listing(conn)
    assert {i["kind"] for i in items} == {"ours", "competitor"}
    comp = next(i for i in items if i["kind"] == "competitor")
    assert "gifting" in comp["note"] and visuals.image_path(conn, "competitor", str(comp["id"])).exists()
    monkeypatch.setattr("creative_intelligence.budget.check", lambda *a, **k: None)
    ids, why = visuals.make_like(conn, "competitor", str(comp["id"]), 1, n=9)
    assert not why and len(ids) == visuals.LIKE_MAX
    row = dict(conn.execute("SELECT * FROM adstudio_textads WHERE id=?", (ids[0],)).fetchone())
    assert row["text_mode"] == "painted" and row["ref_path"] == path and row["headline"] == "Custard you can hold"
    assert visuals.make_like(conn, "competitor", "999", 1)[1] == "That picture isn't available."
    with conn:
        conn.execute("DELETE FROM adstudio_hooks")
    assert "hooks" in visuals.make_like(conn, "competitor", str(comp["id"]), 1)[1]


def test_reference_goes_last_with_the_rule(conn, tmp_path, monkeypatch):
    from creative_intelligence.webapp.routes import aiads
    path = market.store_screenshot(_shot(tmp_path), "image/png")
    with conn:
        conn.execute("INSERT INTO adstudio_briefs (id, product_id, idea, brief_json) VALUES (1, 1, 'x', '{}')")
        conn.execute("INSERT INTO adstudio_textads (id, product_id, brief_id, kind, variant, title, headline, status,"
                     " aspect_ratio, text_mode, ref_path) VALUES (5, 1, 1, 'variant', 'bold_type', 'Like', 'Hi', 'queued',"
                     " '4:5', 'painted', ?)", (path,))
    sent = {}
    monkeypatch.setattr("creative_intelligence.webapp.common.db", lambda: get_connection(tmp_path / "data" / "t.db"))
    monkeypatch.setattr(aiads, "_primary_photo_uris", lambda conn, pid, limit=3: ["data:product"])
    monkeypatch.setattr("creative_intelligence.adstudio.generate.generate_ad",
                        lambda prompt, uris, ar: sent.update(prompt=prompt, uris=uris) or (_ for _ in ()).throw(RuntimeError("stop")))
    aiads.run_textad(5)
    assert sent["uris"][0] == "data:product" and sent["uris"][-1].startswith("data:image")
    assert visuals.LIKE_RULE in sent["prompt"]


def test_several_screenshots_are_read_and_saved(conn, tmp_path, monkeypatch):
    paths = [market.store_screenshot(_shot(tmp_path, f"{n}.png") + bytes([n]), "image/png") for n in range(2)]
    reads = iter([{"page_name": "Fruit Co", "body": "Cut open", "headline": "", "cta": "Shop", "started_on": "",
                   "active": True, "variants": 1, "media_kind": "unknown"},
                  {"page_name": "", "body": "", "headline": "", "cta": "", "started_on": "", "active": True,
                   "variants": 1, "media_kind": "image"}])
    monkeypatch.setattr(market, "_read", lambda conn, content, client: next(reads))
    monkeypatch.setattr("creative_intelligence.adstudio.research.tag", lambda conn, client=None: {"tagged": 0})
    out = market.read_screens(lambda: get_connection(tmp_path / "data" / "t.db"), paths + ["/etc/passwd"])
    assert out["added"] == 2
    rows = conn.execute("SELECT page_name, headline, media_path FROM competitor_ads ORDER BY id").fetchall()
    assert [r["page_name"] for r in rows] == ["Fruit Co", "Unknown brand"] and rows[1]["headline"] == "(no words on the ad)"
    assert all(r["media_path"] in paths for r in rows)                      # never a path from elsewhere
    with pytest.raises(ValueError):
        market.store_screenshot(b"%PDF", "application/pdf")


def test_routes_and_idea_pictures(conn, tmp_path, monkeypatch):
    import creative_intelligence.webapp.app as app_module
    from creative_intelligence.adstudio import ideas
    monkeypatch.setattr("creative_intelligence.webapp.common.db", lambda: get_connection(tmp_path / "data" / "t.db"))
    queued = []
    monkeypatch.setattr("creative_intelligence.jobs.enqueue", lambda c, kind, p, **k: queued.append((kind, p)) or 1)
    c = app_module.app.test_client()
    assert "competitor" in c.get("/api/adstudio/visuals").get_json()["kinds"]
    from io import BytesIO
    r = c.post("/api/adstudio/market/screens", data={"files": [(BytesIO(_shot(tmp_path)), "a.png", "image/png")]},
               content_type="multipart/form-data").get_json()
    assert r["queued"] == 1 and queued[0][0] == "read_screens"
    assert c.get("/api/adstudio/visuals/swipe/99/image").status_code == 404
    assert b"loadVisuals" in c.get("/ideas/visuals").data
    assert ideas.picture({"source_ref": "winner:3", "evidence": {}}) == "/api/adstudio/visuals/winners/3/image"
    assert ideas.picture({"source_ref": "", "evidence": {"competitor_ad_ids": [7]}}) == "/api/adstudio/market/ad/7/media"


class _IG:
    """Meta's Graph API for one Page with an Instagram account and two posts (read only, faked)."""

    def __init__(self, tmp_path, connected=True):
        self.tmp, self.connected, self.calls = tmp_path, connected, []

    def get(self, url, params=None, timeout=0):
        self.calls.append(url)
        if url.endswith("me/accounts"):
            data = {"data": [{"id": "p1", "instagram_business_account": {"id": "ig1"}}]} if self.connected else \
                {"error": {"message": "(#10) missing instagram_basic"}}
            return SimpleNamespace(json=lambda: data)
        if url.endswith("ig1/media"):
            return SimpleNamespace(json=lambda: {"data": [
                {"id": "m1", "caption": "Pink outside, gold inside\nmore", "media_type": "IMAGE", "media_url": "https://x/a.jpg",
                 "permalink": "https://instagram.com/p/a", "timestamp": "2026-10-20T10:00:00+0000", "like_count": 120, "comments_count": 9},
                {"id": "m2", "caption": "Unboxing", "media_type": "VIDEO", "thumbnail_url": "https://x/b.jpg",
                 "permalink": "https://instagram.com/p/b", "timestamp": "2026-10-22T10:00:00+0000", "like_count": 40, "comments_count": 2}]})
        if url.endswith("/insights"):
            return SimpleNamespace(json=lambda: {"data": [{"name": "reach", "values": [{"value": 2000}]}]})
        return SimpleNamespace(status_code=200, content=_shot(self.tmp, "ig.png"))


def test_instagram_posts_sync_read_only_and_show_in_the_library(conn, tmp_path):
    """Feedback 2026-10-27: "add a section for our own organic content (pulled from IG)"."""
    from creative_intelligence.adstudio import instagram
    http = _IG(tmp_path)
    assert instagram.sync(conn, http) == {"posts": 2, "added": 2}
    assert instagram.sync(conn, http)["added"] == 0                       # the next sync updates, never duplicates
    items = visuals.listing(conn, "organic")
    assert [i["id"] for i in items] == ["m2", "m1"] and items[1]["title"] == "Pink outside, gold inside"
    assert "120 likes" in items[1]["note"] and "reach 2000" in items[1]["note"] and items[1]["link"]
    assert visuals.image_path(conn, "organic", "m1").exists()
    assert instagram.status(conn)["posts"] == 2 and instagram.status(conn)["last"]["status"] == "done"


def test_instagram_says_what_to_do_when_the_token_cant_read_it(conn, tmp_path):
    from creative_intelligence.adstudio import instagram
    with pytest.raises(RuntimeError, match="instagram_basic"):
        instagram.sync(conn, _IG(tmp_path, connected=False))
    assert "Instagram isn't connected yet" in instagram.status(conn)["last"]["message"]


def test_selected_pictures_are_repurposed_for_a_product(conn, tmp_path, monkeypatch):
    """Feedback 2026-10-27: "there needs to be a clear way to select things to repurpose and use on ads"."""
    from creative_intelligence.adstudio import hub, instagram
    instagram.sync(conn, _IG(tmp_path))
    items = [{"kind": "organic", "id": "m1"}, {"kind": "organic", "id": "m2"}]
    monkeypatch.setattr("creative_intelligence.budget.check", lambda *a, **k: None)
    r = visuals.repurpose(conn, items, 1, "statics", n=2)
    assert r["queued"] == 4 and len(r["ids"]) == 4                           # two pictures × two ads each
    seen = {}
    monkeypatch.setattr(hub, "auto_hooks", lambda c, product, idea="": seen.update(idea=idea) or 5)
    assert visuals.repurpose(conn, items, 1, "hooks")["written"] == 5
    assert "Pink outside, gold inside" in seen["idea"] and "Never copy" in seen["idea"]
    r = visuals.repurpose(conn, items, 1, "idea")
    idea = dict(conn.execute("SELECT * FROM ideas WHERE id=?", (r["idea_id"],)).fetchone())
    assert idea["source"] == "swipe" and "organic:m1" in idea["source_ref"]
    assert "Pick one of our winning videos" in visuals.repurpose(conn, items, 1, "video")["error"]
    assert visuals.repurpose(conn, [], 1, "statics")["error"] == "Select at least one picture."
