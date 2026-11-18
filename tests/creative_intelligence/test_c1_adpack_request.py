"""Ad Pack request (Part C, C1/C2): the form's limits, templates, the sentence reader (Claude faked) and the pages."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from creative_intelligence.adstudio import adpack
from creative_intelligence.db import get_connection, init_db


@pytest.fixture
def path(tmp_path, monkeypatch):
    p = tmp_path / "t.db"
    monkeypatch.setenv("CI_DB_PATH", str(p))
    init_db(p)
    c = get_connection(p)
    with c:
        c.execute("INSERT INTO adstudio_products (id, name, product_type) VALUES (1, 'Rosy Sunshine Mango', 'fruit')")
        c.execute("INSERT INTO adstudio_products (id, name, product_type) VALUES (92, 'Exotic Fruit Variety Box', 'box')")
    c.close()
    return p


@pytest.fixture
def conn(path):
    c = get_connection(path)
    yield c
    c.close()


def test_normalize_keeps_limits_and_known_styles():
    r = adpack.normalize({"product_ids": [1, "1", "x", 92], "statics": 500, "videos": {"proven": 3, "nope": 2,
                          "talking_head": 99, "pov": 0}, "sizes": ["1:1", "16:9"], "angles": "ideas", "mix_new": 140})
    assert r["product_ids"] == [1, 92] and r["statics"] == adpack.MAX_STATICS
    assert r["videos"] == {"proven": 3, "talking_head": adpack.MAX_VIDEOS}
    assert r["sizes"] == ["1:1"] and r["angles"] == "auto" and r["mix_new"] == 100      # no ideas picked → auto
    assert adpack.normalize({})["sizes"] == ["9:16", "4:5"]


def test_built_in_templates_only_use_real_styles(conn):
    for t in adpack.templates(conn):
        assert sum(t["request"]["videos"].values()) == sum(adpack.BUILT_IN[t["name"]]["videos"].values())


def test_saved_template_has_no_products_and_replaces(conn):
    assert adpack.save_template(conn, "Weekly pack", {}) == "That name is a built-in template."
    assert adpack.save_template(conn, "", {}) == "Name the template."
    assert adpack.save_template(conn, "Mango week", {"product_ids": [1], "statics": 6}) == ""
    assert adpack.save_template(conn, "Mango week", {"statics": 8}) == ""
    saved = [t for t in adpack.templates(conn) if t["saved"]]
    assert len(saved) == 1 and saved[0]["request"]["statics"] == 8 and saved[0]["request"]["product_ids"] == []


def test_create_needs_products_and_something_to_make(conn):
    assert adpack.create(conn, {"statics": 5}) == (None, "Pick at least one product.")
    assert adpack.create(conn, {"product_ids": [1], "statics": 0, "videos": {}}) == (None, "Ask for some statics or videos.")
    pid, err = adpack.create(conn, {"product_ids": [1], "statics": 5, "name": "Mother's Day"})
    assert not err and adpack.listing(conn)[0]["name"] == "Mother's Day" and adpack.listing(conn)[0]["status"] == "request"


class FakeClient:
    def __init__(self, out):
        self.out, self.asked = out, None
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.asked = kw
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(self.out))], stop_reason="end_turn",
                               usage=SimpleNamespace(input_tokens=100, output_tokens=50))


def test_parse_matches_products_and_lists_the_rest(conn):
    fake = FakeClient({"product_names": ["rosy sunshine mango", "Variety Box", "Durian"], "statics": 15,
                       "videos": [{"style": "talking_head", "count": 2}, {"style": "proven", "count": 3}],
                       "sizes": [], "notes": "by Friday", "name": ""})
    out = adpack.parse(conn, "15 statics and 5 videos for the mango, the variety box and durian", client=fake)
    assert out["request"]["product_ids"] == [1, 92] and out["unknown_products"] == ["Durian"]
    assert out["request"]["videos"] == {"talking_head": 2, "proven": 3} and out["request"]["sizes"] == ["9:16", "4:5"]
    assert out["request"]["notes"] == "by Friday"
    assert "Rosy Sunshine Mango" in fake.asked["messages"][0]["content"]
    assert adpack.parse(conn, "  ")["error"]


def test_pages_and_api(path, monkeypatch):
    import creative_intelligence.webapp.app as app_module
    monkeypatch.setattr("creative_intelligence.webapp.common.db", lambda: get_connection(path))
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        assert c.get("/adpack").headers["Location"].endswith("/adpack/request")
        page = c.get("/adpack/request")
        assert page.status_code == 200 and b"Ad Pack" in page.data
        assert c.get("/adpack/nope").status_code == 404
        o = c.get("/api/adstudio/adpack/options").get_json()
        assert {p["name"] for p in o["products"]} == {"Rosy Sunshine Mango", "Exotic Fruit Variety Box"}
        assert o["video_styles"][0][0] == "proven" and o["default"]["statics"] == 10
        assert c.post("/api/adstudio/adpack", json={"request": {"statics": 3}}).get_json()["error"]
        assert c.post("/api/adstudio/adpack", json={"request": {"product_ids": [1], "statics": 3}}).get_json()["id"]
        assert c.post("/api/adstudio/adpack/templates", json={"name": "T", "request": {"statics": 4}}).get_json()["ok"]
        assert len(c.get("/api/adstudio/adpack/options").get_json()["packs"]) == 1


def _filled(c, pid):
    """A product whose profile is already filled (look, research, three hooks)."""
    c.execute("UPDATE adstudio_products SET physical_desc='round, green' WHERE id=?", (pid,))
    c.execute("INSERT INTO product_research (product_id, status, created_at) VALUES (?, 'done', 'x')", (pid,))


def test_plan_prices_each_product_and_says_what_is_missing(conn, monkeypatch):
    """C3: per product the statics (all AI-designed, every size), the videos to their drafts, what isn't ready, and
    the totals against today's caps. Nothing is spent."""
    from creative_intelligence.adstudio import adpack as A, batch
    monkeypatch.setattr(batch, "ai_price", lambda c: 0.18)
    monkeypatch.setattr(A, "_video_estimate", lambda c, pid, style: 4.0)
    monkeypatch.setattr(A, "fill_needed", lambda c, prod: [])          # the fill has its own test
    with conn:
        conn.execute("INSERT INTO adstudio_photos (product_id, path) VALUES (1, '/tmp/x.png')")
        conn.execute("INSERT INTO adstudio_hooks (product_id, headline) VALUES (1, 'Pink outside'), (1, 'Custard')")
    pid, _ = A.create(conn, {"product_ids": [1, 92], "statics": 5, "sizes": ["9:16", "4:5"],
                             "videos": {"proven": 2, "ripening_timelapse": 1, "pov": 1}})
    p = A.plan(conn, pid)
    mango, box = p["products"]
    assert mango["static_files"] == 10 and mango["static_usd"] == 1.8
    assert "2 usable hooks: Make writes 5 more first" in mango["notes"][0] and not mango["warnings"]
    rows = {v["style"]: v for v in mango["videos"]}
    assert "No decoded winners yet" in rows["proven"]["note"] and rows["proven"]["ready"]
    assert not rows["ripening_timelapse"]["ready"] and "Ripeness" in rows["ripening_timelapse"]["why"]
    assert mango["video_usd"] == 12.0                                  # 2 proven + 1 POV; the not-ready one isn't priced
    assert "No usable hooks: Make writes" in box["notes"][0]          # feedback 2026-10-26: no longer blocks the pack
    assert not any("hook" in w for w in box["warnings"]) and box["ready"] == bool(box["photos"])
    assert p["total_usd"] == round(1.8 * 2 + 12.0 * 2, 2) and set(p["left_today"]) == {"images", "video", "text"} and p["text_usd"] > 0
    assert A.get(conn, pid)["status"] == "planned" and A.get(conn, pid)["plan"]["total_usd"] == p["total_usd"]


def test_produce_starts_the_batches_and_the_pack_job_approves_storyboards(conn, path, monkeypatch):
    """C4: one approval starts an all-AI Bulk batch and a video batch per product; the pack job approves the
    storyboards once written and marks the pack for review when everything is made."""
    from creative_intelligence.adstudio import adpack as A, batch, video_batch
    monkeypatch.setattr(batch, "ai_price", lambda c: 0.18)
    monkeypatch.setattr(A, "_video_estimate", lambda c, pid, style: 4.0)
    queued = []
    monkeypatch.setattr("creative_intelligence.jobs.enqueue", lambda c, kind, p=None, **k: queued.append(kind) or 1)
    with conn:
        conn.execute("INSERT INTO adstudio_photos (product_id, path) VALUES (1, '/tmp/x.png')")
        conn.execute("INSERT INTO adstudio_hooks (product_id, headline) VALUES (1, 'Pink outside'), (1, 'Custard'),"
                     " (1, 'Gold inside')")
        _filled(conn, 1)
    pid, _ = A.create(conn, {"product_ids": [1], "statics": 4, "sizes": ["4:5"], "videos": {"proven": 5, "pov": 1}})
    assert A.produce(conn, pid)["error"] == "Make the plan first."
    p = A.plan(conn, pid)
    assert {v["style"]: v["count"] for v in p["products"][0]["videos"]} == {"proven": 3, "pov": 1}   # 3 per format
    r = A.produce(conn, pid)                         # the click only queues the pack job (feedback 2026-11-09)
    assert r == {"started": 1, "errors": [], "filling": []} and queued == ["adpack"]
    db = lambda: get_connection(path)
    assert A.advance(db, pid) == {"status": "producing"}
    assert "compose_batch" in queued and "video_batch" in queued
    made = A.get(conn, pid)["plan"]["made"]
    assert made["pending"] == []
    b = batch.status(conn, made["bulk"]["1"])
    assert b["total"] == 4                                             # 4 statics × 1 size, all AI-designed
    vb = json.loads(conn.execute("SELECT request_json FROM video_batches WHERE id=?", (made["video"]["1"],)).fetchone()[0])
    assert vb["styles"] == {"fruit_story": 3, "pov": 1}                # no winners yet: proven filled from ideas
    assert A.produce(conn, pid)["error"] == "This pack is already being made."
    approved = []
    monkeypatch.setattr(video_batch, "approve", lambda c, bid: approved.append(bid) or {})
    with conn:
        conn.execute("UPDATE video_batches SET status='planned' WHERE id=?", (made["video"]["1"],))
    assert A.advance(db, pid) == {"status": "producing"} and approved == [made["video"]["1"]]
    with conn:
        conn.execute("UPDATE video_batches SET status='drafts' WHERE id=?", (made["video"]["1"],))
        conn.execute("UPDATE compose_batches SET status='done' WHERE id=?", (made["bulk"]["1"],))
    assert A.advance(db, pid) == {"status": "review"} and A.get(conn, pid)["status"] == "review"


def test_creative_direction_reaches_every_brief_but_never_the_ad_words(conn, monkeypatch):
    """Feedback 2026-10-26: the templates changed only the counts; "make this pack Christmas ads" must change the ads."""
    from creative_intelligence.adstudio import adpack as A, batch, textads
    assert A.templates(conn)[1]["request"]["direction"].startswith("A gift")             # Gifting push sets one
    seen = []
    monkeypatch.setattr(textads, "draft_brief", lambda prod, idea="", **k: seen.append(idea) or
                        textads.normalize_brief({"hook": "Pink outside", "cta": "Shop now"}))
    monkeypatch.setattr("creative_intelligence.webapp.common._primary_photo_uris", lambda *a, **k: [])
    batch._brief_for(conn, 1, {"headline": "Pink outside"}, 9, "Christmas: warm, festive table")
    assert "Pink outside" in seen[0] and "CREATIVE DIRECTION FOR THIS PACK" in seen[0] and "Christmas" in seen[0]

    def down(*a, **k):
        raise OSError("down")
    monkeypatch.setattr(textads, "draft_brief", down)
    _, b = batch._brief_for(conn, 1, {"headline": "Pink outside"}, 9, "Christmas: warm, festive table")
    assert "Christmas" not in json.dumps(b)                            # the fallback keeps the hook's own words


def test_a_product_without_hooks_gets_some_written_before_its_statics(conn, monkeypatch):
    """Feedback 2026-10-26 (Large Cherimoya): "No usable hooks" stopped the pack. Make now writes Hook University hooks
    first (claims-checked, mocked here), then the statics use them."""
    from creative_intelligence.adstudio import adpack as A, batch, hub
    monkeypatch.setattr(batch, "ai_price", lambda c: 0.18)
    monkeypatch.setattr("creative_intelligence.jobs.enqueue", lambda *a, **k: 1)
    written = []

    def auto_hooks(c, product):
        written.append(product["id"])
        with c:
            c.execute("INSERT INTO adstudio_hooks (product_id, headline) VALUES (?, 'Custard inside'), (?, 'Ripe today'),"
                      " (?, 'Scoop it')", (product["id"],) * 3)
        return 3
    monkeypatch.setattr(hub, "auto_hooks", auto_hooks)
    with conn:
        conn.execute("INSERT INTO adstudio_photos (product_id, path) VALUES (1, '/tmp/x.png')")
        conn.execute("UPDATE adstudio_products SET physical_desc='round, green' WHERE id=1")
        conn.execute("INSERT INTO product_research (product_id, status, created_at) VALUES (1, 'done', 'x')")
    pid, _ = A.create(conn, {"product_ids": [1], "statics": 2, "sizes": ["4:5"], "videos": {}})
    p = A.plan(conn, pid)
    assert p["ready"] and "No usable hooks: Make writes" in p["products"][0]["notes"][0]
    monkeypatch.setattr(A, "fill_needed", lambda c, prod: [])          # hooks only: written at the start, no fill
    r = A.produce(conn, pid)
    assert r["started"] == 1 and not r["filling"]
    A._next_steps(conn, pid, A.get(conn, pid))
    assert written == [1]
    assert len(batch._hooks(conn, 1)) == 3


def test_new_hooks_that_the_check_flags_are_repaired_and_checked_again(conn, monkeypatch):
    """Feedback 2026-10-26: four of five new hooks came back flagged ("vanilla-pineapple", an invented brother).
    Each flagged hook gets its problems rewritten and is checked again; only one that still fails stays flagged."""
    from creative_intelligence.adstudio import chat, hub, ideas
    monkeypatch.setattr("creative_intelligence.adstudio.winning.current_patterns", lambda c, limit=4: [])
    monkeypatch.setattr("creative_intelligence.webapp.common._primary_photo_uris", lambda *a, **k: [])
    monkeypatch.setattr(ideas, "pick_one", lambda *a, **k: ("", None))
    monkeypatch.setattr(chat, "suggest_hooks", lambda *a, **k: [
        {"headline": "Vanilla-pineapple pudding", "hook_text": "Like pineapple custard", "cta": "Shop now"},
        {"headline": "Eat it cold, with a spoon", "hook_text": "Cold, soft, sweet", "cta": "Shop now"}])
    asked = []
    monkeypatch.setattr(chat, "repair_hook", lambda prod, h, problems, client=None: asked.append(problems) or
                        {**{k: h.get(k, "") for k in chat.HOOK_FIELDS}, "headline": "Vanilla-sweet and spoon-soft",
                         "hook_text": "Custard-soft, eaten cold"})

    def check(c, source, sid, pid, items):
        bad = any("pineapple" in (v or "").lower() for v in items.values())
        res = {"status": "flagged" if bad else "ok", "unlisted": [],
               "banned": [{"field": "headline", "rule": "other_fruit", "term": "pineapple"}] if bad else []}
        from creative_intelligence.adstudio import facts
        facts.store(c, source, sid, res)
        return res
    monkeypatch.setattr("creative_intelligence.webapp.common._claims", check)
    from creative_intelligence.webapp.common import _product_dict
    assert hub.auto_hooks(conn, _product_dict(conn, 1)) == 2
    assert asked == [["“pineapple” (another fruit)"]]
    from creative_intelligence.adstudio import batch
    assert sorted(h["headline"] for h in batch._hooks(conn, 1)) == ["Eat it cold, with a spoon", "Vanilla-sweet and spoon-soft"]


def test_the_pack_fills_an_empty_product_first_then_starts_it(conn, path, monkeypatch):
    """Feedback 2026-11-09: "if we don't have the product filled it should fill the product in the same run"."""
    from creative_intelligence.adstudio import adpack as A, batch
    monkeypatch.setattr(batch, "ai_price", lambda c: 0.18)
    monkeypatch.setattr(A, "_video_estimate", lambda c, pid, style: 4.0)
    queued = []
    monkeypatch.setattr("creative_intelligence.jobs.enqueue", lambda c, kind, p=None, **k: queued.append(kind) or 1)
    with conn:
        conn.execute("UPDATE adstudio_products SET origin='shopify' WHERE id=1")       # no photos yet: from the shop
    pid, _ = A.create(conn, {"product_ids": [1], "statics": 2, "sizes": ["4:5"], "videos": {"pov": 1}})
    p = A.plan(conn, pid)
    prod = p["products"][0]
    assert p["ready"] and not prod["warnings"]
    assert any(n.startswith("Fills the product first") and "photos from the shop" in n for n in prod["notes"])
    r = A.produce(conn, pid)
    assert r["filling"] == [1] and queued == ["fill", "adpack"]
    db = lambda: get_connection(path)
    assert A.advance(db, pid) == {"status": "producing"}
    assert "compose_batch" not in queued                               # waits for the fill
    assert A.progress(conn, A.get(conn, pid))[0]["filling"]
    with conn:
        conn.execute("UPDATE adstudio_products SET fill_status='done' WHERE id=1")
        conn.execute("INSERT INTO adstudio_photos (product_id, path) VALUES (1, '/tmp/x.png')")
        conn.execute("INSERT INTO adstudio_hooks (product_id, headline) VALUES (1, 'A'), (1, 'B'), (1, 'C')")
    A.advance(db, pid)
    made = A.get(conn, pid)["plan"]["made"]
    assert made["filling"] == [] and made["bulk"]["1"] and made["video"]["1"]


def test_an_animated_still_approves_the_best_images_or_makes_some_first(conn, path, monkeypatch):
    """Feedback 2026-11-09: "now it's saying to approve images, should auto do it"."""
    from creative_intelligence.adstudio import adpack as A, batch, runs, video_batch
    monkeypatch.setattr(batch, "ai_price", lambda c: 0.18)
    monkeypatch.setattr(A, "_video_estimate", lambda c, pid, style: 4.0)
    monkeypatch.setattr(A, "_images_usd", lambda c: 1.5)
    monkeypatch.setattr(A, "fill_needed", lambda c, prod: [])
    monkeypatch.setattr("creative_intelligence.jobs.enqueue", lambda *a, **k: 1)
    monkeypatch.setattr(runs, "create_run", lambda c, pid, n, aspect: (77, ""))
    with conn:
        conn.execute("INSERT INTO adstudio_photos (product_id, path) VALUES (1, '/tmp/x.png')")
    pid, _ = A.create(conn, {"product_ids": [1], "statics": 0, "videos": {"animate": 1}})
    row = A.plan(conn, pid)["products"][0]["videos"][0]
    assert row["ready"] and "makes 8 first" in row["note"] and row["usd"] == 5.5
    monkeypatch.setattr(runs, "status", lambda c, rid: {"status": "running"})
    A.produce(conn, pid)
    A._next_steps(conn, pid, A.get(conn, pid))
    made = A.get(conn, pid)["plan"]["made"]
    assert made["animate"]["1"] == {"count": 1, "run_id": 77, "batch": None} and "1" not in made["video"]
    A._next_steps(conn, pid, A.get(conn, pid))
    assert A.get(conn, pid)["plan"]["made"]["animate"]["1"]["batch"] is None          # still making the images
    with conn:                                          # the run is done: two images, one passed QA
        for iid, ok in ((5, 1), (6, 0)):
            conn.execute("INSERT INTO adstudio_images (id, product_id, status, image_path) VALUES (?, 1, 'done', '/tmp/i.png')",
                         (iid,))
            conn.execute("INSERT INTO qa_scores (source, source_id, total, passed) VALUES ('image', ?, 4.0, ?)", (iid, ok))
    monkeypatch.setattr(runs, "status", lambda c, rid: {"status": "done"})
    A._next_steps(conn, pid, A.get(conn, pid))
    a = A.get(conn, pid)["plan"]["made"]["animate"]["1"]
    assert a["batch"] and video_batch._picks(conn, 1, "animate") == [5]
    assert json.loads(conn.execute("SELECT request_json FROM video_batches WHERE id=?", (a["batch"],)).fetchone()[0]
                      )["styles"] == {"animate": 1}
    # with QA-passed images already there, the plan approves them instead of making more
    with conn:
        conn.execute("UPDATE adstudio_images SET review_status=NULL WHERE id=5")
    pid2, _ = A.create(conn, {"product_ids": [1], "statics": 0, "videos": {"animate": 1}})
    assert "Uses the 1 best images we have" in A.plan(conn, pid2)["products"][0]["videos"][0]["note"]
