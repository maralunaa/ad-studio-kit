"""Compose → Bulk: approved images × ad types × hooks × sizes in one click, made in the background. Code-drawn
ads are free and rotate so neighbours differ; AI scenes are optional and paid. No live API."""
from __future__ import annotations

import json

import pytest
from PIL import Image

from creative_intelligence.adstudio import batch as B
from tests.creative_intelligence.test_v1_animate import drain, env, png  # noqa: F401  (fixture)


@pytest.fixture
def bulk(env):
    c = env.db()
    with c:
        for i in (3, 4, 5):
            c.execute("INSERT INTO adstudio_images (id, product_id, title, status, image_path, aspect_ratio, review_status)"
                      " VALUES (?, 1, 'Approved', 'done', ?, '4:5', 'approved')", (i, png(env.tmp / f"a{i}.png")))
        c.execute("INSERT INTO adstudio_hooks (id, product_id, headline, source) VALUES (3, 1, 'Two pounds of mango',"
                  " 'hook_university'), (4, 1, 'Worth the wait', 'manual')")
    c.close()
    return env


def body(**over):
    return {"images": ["img:1", "img:3", "img:4", "img:5"], "types": ["bold_type", "direct_response", "minimal", "premium"],
            "hook_ids": [1, 2, 3], "per_image": 3, "sizes": ["4:5", "9:16"], **over}


def test_options_only_approved_images_and_explain_what_cannot_apply(bulk):
    o = bulk.client.get("/api/adstudio/product/1/bulk").get_json()
    assert {i["id"] for i in o["images"]} == {1, 3, 4, 5}                      # image 2 isn't approved
    t = {x["key"]: x for x in o["types"]}
    assert "4–5 star reviews" in t["social_proof"]["why"] and not t["social_proof"]["on"]
    assert "One ad" in t["notes_list"]["why"] and "One ad" in t["comments_screenshot"]["why"]   # text layouts: One ad only
    assert t["bold_type"]["made"] == "AI-designed" and t["premium"]["made"] == "AI-designed and code"
    assert not t["origin_story"]["on"] and t["bold_type"]["on"] and t["overlay"]["on"] and "scene" not in t
    assert o["hooks"][0]["source"] == "hook_university" and sum(h["best"] for h in o["hooks"]) == 4


def test_rotation_spreads_types_and_hooks_and_copyless_types_need_no_hook():
    types = [{"key": k, "fields": f} for k, f in (("bold_type", ["headline"]), ("premium", ["headline"]), ("minimal", []))]
    ps = B.pairs(types, [1, 2, 3])
    assert len(ps) == 7 and ("minimal", None) in ps and ps.count(("minimal", None)) == 1
    assert all(a != b for a, b in zip(ps, ps[1:]))
    items = B.plan(["img:1", "img:2"], types, [1, 2, 3], 3, ["4:5"])
    first, second = [(i["type"], i["hook_id"]) for i in items[:3]], [(i["type"], i["hook_id"]) for i in items[3:]]
    assert len({t for t, _ in first}) == 3 and not set(first) & set(second)       # different mix per image
    assert len(B.plan(["img:1"], types, [1, 2, 3], 99, ["4:5", "9:16", "1:1"])) == 21    # one image, everything


def test_estimate_and_limits(bulk):
    e = bulk.client.post("/api/adstudio/product/1/bulk/estimate", json=body(ai=0)).get_json()["estimate"]
    assert e["ads"] == 24 and e["ai"] == 0 and e["total_usd"] == 0.0       # code draws only the light layouts
    e = bulk.client.post("/api/adstudio/product/1/bulk/estimate", json=body(ai="mostly")).get_json()["estimate"]
    assert (e["ai"], e["ads"]) == (min(B.MAX_AI, 19), 24 - min(B.MAX_AI, 19))   # "mostly": 80% of the 24, code the rest
    e = bulk.client.post("/api/adstudio/product/1/bulk/estimate", json=body(types=["premium"], ai=0)).get_json()["estimate"]
    assert "look alike" in e["variety_warning"]
    e = bulk.client.post("/api/adstudio/product/1/bulk/estimate", json=body(ai=4)).get_json()["estimate"]
    assert e["ai"] == 4 and e["ads"] == 20 and e["ai_usd"] == pytest.approx(4 * B.ai_price(bulk.db()))
    bad = bulk.client.post("/api/adstudio/product/1/bulk/estimate", json=body(images=["img:2"], ai=0))
    assert bad.status_code == 400 and "image" in bad.get_json()["error"]
    assert "hook" in bulk.client.post("/api/adstudio/product/1/bulk/estimate", json=body(hook_ids=[])).get_json()["error"]
    assert "too many" in bulk.client.post("/api/adstudio/product/1/bulk/estimate",
                                          json=body(per_image=99, sizes=["4:5", "9:16", "1:1"], images=["img:1"] * 60,
                                                    ai=0)).get_json()["error"]


def test_batch_makes_every_ad_in_every_size_in_the_background(bulk, monkeypatch):
    monkeypatch.setattr(B, "MAX_OVER_SUBJECT", 1.01)          # the test photos are texture edge to edge
    checks = []
    monkeypatch.setenv("FACTS_CHECK", "1")
    monkeypatch.setattr("creative_intelligence.adstudio.facts.check_copy",
                        lambda conn, prod, items, **k: checks.append(items["headline"]) or {"status": "ok", "banned": [], "unlisted": []})
    r = bulk.client.post("/api/adstudio/product/1/bulk", json=body(ai=0)).get_json()
    bid = r["batch"]["id"]
    assert r["batch"]["status"] == "queued" and r["batch"]["total"] == 24
    drain(bulk.db)
    s = bulk.client.get(f"/api/adstudio/bulk/{bid}").get_json()["batch"]
    assert (s["status"], s["done"], s["failed"]) == ("done", 24, 0), s["message"]
    c = bulk.db()
    rows = [dict(x) for x in c.execute("SELECT c.*, i.aspect_ratio FROM adstudio_composites c JOIN adstudio_images i"
                                       " ON i.id=c.image_id WHERE batch_id=?", (bid,))]
    assert len(rows) == 24 and {r["aspect_ratio"] for r in rows} == {"4:5", "9:16"}
    for r in rows:
        w, h = Image.open(r["output_path"]).size
        assert abs(w / h - (0.8 if r["aspect_ratio"] == "4:5" else 9 / 16)) < 0.01
    assert len(checks) == len({r["hook_id"] for r in rows if r["hook_id"]}) <= 3         # one claims check per hook
    assert c.execute("SELECT COUNT(*) FROM copy_checks WHERE source='composite'").fetchone()[0] == sum(1 for r in rows if r["hook_id"])
    sized = c.execute("SELECT COUNT(*) FROM adstudio_images WHERE parent_image_id IS NOT NULL").fetchone()[0]
    assert sized == 4                                                                   # one 9:16 canvas per image
    assert bulk.client.get("/api/adstudio/product/1/bulk").get_json()["batches"][0]["id"] == bid


def test_ai_scenes_are_optional_and_go_through_the_ai_ads_path(bulk, monkeypatch):
    ran = []
    monkeypatch.setattr("creative_intelligence.webapp.routes.aiads.run_textad", lambda ad_id: ran.append(ad_id) or {"status": "done"})
    r = bulk.client.post("/api/adstudio/product/1/bulk", json=body(images=["img:1"], per_image=1, sizes=["9:16"], ai=3)).get_json()
    drain(bulk.db)
    c = bulk.db()
    ads = [dict(x) for x in c.execute("SELECT * FROM adstudio_textads WHERE batch_id=?", (r["batch"]["id"],))]
    assert len(ads) == 3 == len(ran) and {a["aspect_ratio"] for a in ads} == {"9:16"}
    assert all(a["variant"] in {"bold_type", "direct_response", "minimal", "premium"} for a in ads)
    assert {a["text_mode"] for a in ads} == {"painted"}                    # AI-designed: the model paints the words
    s = B.status(c, r["batch"]["id"])
    assert (s["done"], s["total"]) == (3, 3)                               # more AI ads than the total: all AI


def test_half_is_the_default_mix(bulk):
    """Feedback 2026-10-22: half the batch AI-designed, half drawn by code on the approved photos."""
    e = bulk.client.post("/api/adstudio/product/1/bulk/estimate", json=body(ai="half")).get_json()["estimate"]
    assert e["ai"] == e["ads"] == 12 and e["ai_usd"] > 0                  # half of the 24 each
    e = bulk.client.post("/api/adstudio/product/1/bulk/estimate", json=body(ai="half", per_image=6,
                                                                           sizes=["4:5", "9:16"])).get_json()["estimate"]
    assert e["ai"] == e["ads"]


def test_one_batch_at_a_time_and_cancel_keeps_what_is_made(bulk, monkeypatch):
    monkeypatch.setenv("FACTS_CHECK", "0")
    bid = bulk.client.post("/api/adstudio/product/1/bulk", json=body()).get_json()["batch"]["id"]
    again = bulk.client.post("/api/adstudio/product/1/bulk", json=body())                    # a second click
    assert again.status_code == 400 and "already being made" in again.get_json()["error"]
    assert bulk.client.post(f"/api/adstudio/bulk/{bid}/cancel").get_json()["batch"]["status"] == "cancelling"
    drain(bulk.db)
    s = bulk.client.get(f"/api/adstudio/bulk/{bid}").get_json()["batch"]
    assert s["status"] == "cancelled" and s["done"] == 0
    assert bulk.client.post(f"/api/adstudio/bulk/{bid}/cancel").status_code == 400
    assert bulk.client.post("/api/adstudio/product/1/bulk", json=body()).status_code == 200      # free again
def test_ai_designed_get_a_real_brief_and_a_real_review(bulk, monkeypatch):
    """2026-10-23: the old app's ads were better because Claude wrote copy for each format and a visual direction.
    Bulk's AI half now drafts that brief per hook; Social Proof carries a real 4–5 star review about the fruit."""
    monkeypatch.setenv("FACTS_CHECK", "0")
    c = bulk.db()
    with c:
        for t, stars in (("The communication was amazing", 5), ("Best mango ever, so sweet", 5), ("Bruised", 1)):
            c.execute("INSERT INTO reviews (product_id, kind, text, author, rating, source, posted_on, added_at)"
                      " VALUES (1, 'review', ?, 'Sam Kay', ?, 'google', '2026-10-01', 'now')", (t, stars))
    c.close()
    drafted, made = [], []
    monkeypatch.setattr("creative_intelligence.adstudio.textads.draft_brief", lambda prod, idea="", has_review=False, **k: (
        drafted.append((idea, has_review)) or {"angle": "a", "hook": idea, "cta": "Shop now", "visual_direction": "warm dusk",
                                               "primary_texts": [], "variants": {"bold_type": {"headline": "Zero strings", "sub": ""},
                                                                                 "social_proof": {"headline": "Mango", "sub": ""}}}))
    monkeypatch.setattr("creative_intelligence.webapp.routes.aiads.run_textad", lambda ad_id: made.append(ad_id) or {"status": "done"})
    r = bulk.client.post("/api/adstudio/product/1/bulk", json=body(images=["img:3"], per_image=1, sizes=["4:5"], ai=2,
                                                                   types=["bold_type", "social_proof"], hook_ids=[3])).get_json()
    drain(bulk.db)
    c = bulk.db()
    ads = [dict(x) for x in c.execute("SELECT t.*, b.brief_json, b.review_text FROM adstudio_textads t JOIN adstudio_briefs b"
                                      " ON b.id=t.brief_id WHERE t.batch_id=?", (r["batch"]["id"],))]
    c.close()
    assert len(drafted) == 1 and drafted[0][1] is True and len(made) == 2          # one brief per hook
    bold = next(a for a in ads if a["variant"] == "bold_type")
    assert bold["headline"] == "Zero strings" and json.loads(bold["brief_json"])["visual_direction"] == "warm dusk"
    assert ads[0]["review_text"] == "Best mango ever, so sweet"                      # about the fruit, never the 1-star


def test_facts_and_reviews_shown_plainly():
    assert B.shown_fact("4,199 reviews, average 4.37 (Fera, incl. Google, as of 2026-10-21)") == "4,199 reviews, average 4.37"
    w = {"mango", "pink", "elephant"}
    assert B.relevance("Best mango ever, so sweet", w) > B.relevance("The communication was amazing", w)


def test_words_avoid_the_fruit(tmp_path):
    """Feedback 2026-10-22: text sat on pale fruit. Copy moves to the clear part; with no clear part it isn't made."""
    from PIL import Image, ImageDraw
    from creative_intelligence.adstudio import render_template as RT
    photo = Image.new("RGB", (1080, 1350), (120, 120, 118))
    ImageDraw.Draw(photo).ellipse((240, 160, 840, 760), fill=(238, 232, 214))        # pale cherimoya flesh, top half
    for x in range(300, 800, 70):
        ImageDraw.Draw(photo).ellipse((x, 400, x + 22, 440), fill=(20, 20, 20))         # seeds
    path = str(tmp_path / "pale.png")
    photo.save(path)
    assert RT.subject_share(photo, (240, 160, 840, 760)) > 0.8                        # pale fruit is seen now
    assert RT.subject_share(photo, (0, 1000, 1080, 1300)) == 0
    m: dict = {}
    RT.render(path, "bold_type", {"headline": "Custard you can hold", "cta": "Shop now"}, m)
    assert m["over_subject"] <= 0.25
    full = Image.new("RGB", (1080, 1350), (90, 90, 88))
    d = ImageDraw.Draw(full)
    d.rectangle((0, 0, 1080, 1350), fill=(238, 232, 214))                             # fruit fills the frame
    for x in range(40, 1040, 90):
        for y in range(40, 1300, 120):
            d.ellipse((x, y, x + 24, y + 40), fill=(20, 20, 20))
    d.rectangle((0, 0, 1080, 6), fill=(90, 90, 88))                                   # a thin backdrop edge
    full.save(tmp_path / "full.png")
    m2: dict = {}
    RT.render(str(tmp_path / "full.png"), "bold_type", {"headline": "Custard you can hold"}, m2)
    assert m2["over_subject"] > B.MAX_OVER_SUBJECT                                    # bulk skips this one


def test_families_alternate():
    types = [{"key": k, "fields": ["headline"]} for k in ("bold_type", "premium", "notes_list", "post_it", "offer_banner")]
    fam = [B._family(t["key"]) for t in B.interleave(types)]
    assert all(a != b for a, b in zip(fam, fam[1:3]))                     # neighbours differ while each family lasts


def test_every_ad_is_ai_designed_by_default(bulk):
    """Feedback 2026-10-23: "too many are coded … more ai generated"."""
    e = bulk.client.post("/api/adstudio/product/1/bulk/estimate", json=body()).get_json()["estimate"]
    assert e["ads"] == 0 and e["ai"] == 24                             # as many as the code layouts would have made
    e = bulk.client.post("/api/adstudio/product/1/bulk/estimate", json=body(ai="mostly")).get_json()["estimate"]
    assert e["ads"] > 0 and e["ai"] > e["ads"]                         # code layouts only when asked for

def test_ai_ads_are_made_several_at_a_time(bulk, monkeypatch):
    """Feedback 2026-10-26 ("how can we increase generation speed"): 16 ads one by one took 7 minutes."""
    import threading
    import time
    now, peak, lock = [0], [0], threading.Lock()

    def slow(ad_id):
        with lock:
            now[0] += 1
            peak[0] = max(peak[0], now[0])
        time.sleep(0.2)
        with lock:
            now[0] -= 1
        return {"status": "done"}
    monkeypatch.setattr("creative_intelligence.webapp.routes.aiads.run_textad", slow)
    r = bulk.client.post("/api/adstudio/product/1/bulk", json=body(images=[], sizes=["4:5"], ai=8)).get_json()
    drain(bulk.db)
    s = B.status(bulk.db(), r["batch"]["id"])
    assert (s["done"], s["total"]) == (8, 8) and peak[0] == B.AI_PARALLEL


def test_download_only_the_selected(bulk):
    """Feedback 2026-10-30: "i want to be able to download the selected ones"."""
    import io
    import zipfile
    r = bulk.client.get("/api/adstudio/export-gallery?keys=image:999999,nonsense")
    assert r.status_code == 200 and zipfile.ZipFile(io.BytesIO(r.data)).namelist() == []


def test_an_ai_ad_whose_words_fail_the_claims_check_uses_the_hook_or_is_skipped(bulk, monkeypatch):
    """2026-11-09 ("a lot of 'tastes like raspberry'"): a flagged brief line falls back to the hook's checked words; if
    those fail too the picture is never paid for."""
    made = []
    monkeypatch.setattr("creative_intelligence.adstudio.textads.draft_brief", lambda prod, idea="", has_review=False, **k: (
        {"angle": "a", "hook": idea, "cta": "Shop now", "visual_direction": "d", "primary_texts": [],
         "variants": {"bold_type": {"headline": "Tastes like raspberry", "sub": ""},
                      "premium": {"headline": "Tastes like raspberry", "sub": ""}}}))
    bad = {"Tastes like raspberry"}
    monkeypatch.setattr("creative_intelligence.webapp.common._claims", lambda conn, s, i, p, items: (
        {"status": "flagged" if items["headline"] in bad else "ok"}))
    monkeypatch.setattr("creative_intelligence.webapp.routes.aiads.run_textad", lambda ad_id: made.append(ad_id) or {"status": "done"})
    r = bulk.client.post("/api/adstudio/product/1/bulk", json=body(images=["img:3"], per_image=1, sizes=["4:5"], ai=1,
                                                                   types=["bold_type"], hook_ids=[3])).get_json()
    drain(bulk.db)
    c = bulk.db()
    ad = dict(c.execute("SELECT * FROM adstudio_textads WHERE batch_id=?", (r["batch"]["id"],)).fetchone())
    c.close()
    assert ad["headline"] == "Two pounds of mango" and made == [ad["id"]]
    bad.add("Two pounds of mango")
    made.clear()
    r = bulk.client.post("/api/adstudio/product/1/bulk", json=body(images=["img:3"], per_image=1, sizes=["4:5"], ai=1,
                                                                   types=["bold_type"], hook_ids=[3])).get_json()
    drain(bulk.db)
    c = bulk.db()
    ad = dict(c.execute("SELECT * FROM adstudio_textads WHERE batch_id=?", (r["batch"]["id"],)).fetchone())
    c.close()
    assert made == [] and ad["status"] == "failed" and "claims check" in ad["error_message"]
