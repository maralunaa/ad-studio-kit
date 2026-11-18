"""Video Studio V2 — shot library: versioned recipe cards, model routing with fallbacks and sound,
library first (a passing shot is never made twice), stricter QA for busy shots, retire, reuse.
Replicate and Claude are faked; ffmpeg is real."""
from __future__ import annotations

import pytest

from creative_intelligence import jobs, prompts
from creative_intelligence.providers import replicate as R
from tests.creative_intelligence.test_v1_animate import FakeClaude, drain, env, png  # noqa: F401  (fixture)


def test_ten_cards_with_the_shotcraft_fields():
    cards = prompts.shot_cards()
    assert set(cards) - {"supermarket_fruit", "clay_hero", "clay_cut", "clay_reveal", "hand_tear", "juice_drip", "spoon_scoop", "ripening_transition", "character_scene", "hand_sorting", "paper_wrap", "doorstep_delivery"} == {"orchard_wide", "fruit_on_tree", "hand_picking", "packing", "whole_fruit_hero", "knife_cut",
                          "cross_section_macro", "juice_texture_macro", "box_opening", "plated_sharing"}
    for c in cards.values():
        assert c["route"] in ("kling", "veo", "seedance") and c["audio"] in ("none", "ambient")
        assert {"locks", "freedom", "composition", "lighting"} <= set(c["still"]) and c["pitfalls"] and c["params"]
        assert c["version"] >= 1
    assert cards["orchard_wide"]["route"] == "veo" and cards["knife_cut"]["route"] == "kling"
    assert cards["orchard_wide"]["version"] == 3 and cards["orchard_wide"]["count_check"] is False   # newest wins


def test_each_model_gets_its_own_fields():
    p, secs = R._clip_payload("google/veo-3.1-fast", "x", "data:img", 6, "9:16", True, None, "no text")
    assert p["image"] == "data:img" and p["resolution"] == "720p" and p["generate_audio"] and secs == 6
    p, secs = R._clip_payload("google/veo-3.1-fast", "x", None, 5, "9:16", False, None, "")
    assert secs in (4, 6) and "image" not in p                       # Veo only does 4, 6 or 8 s
    p, _ = R._clip_payload("bytedance/seedance-2.0", "x", None, 5, "9:16", False, ["r1", "r2"], "")
    assert p["reference_images"] == ["r1", "r2"] and "image" not in p
    p, _ = R._clip_payload("kwaivgi/kling-v3-video", "x", "data:img", 5, "9:16", False, None, "no text")
    assert p["start_image"] == "data:img" and p["mode"] == "standard" and p["negative_prompt"] == "no text"


def test_a_failing_route_falls_back_once(monkeypatch):
    from creative_intelligence import config
    monkeypatch.setattr(config, "CI_REPLICATE_API_KEY", "r8_test")
    monkeypatch.setattr("creative_intelligence.budget.check_now", lambda *a, **k: None)
    monkeypatch.setattr(R, "estimate", lambda m, u=1: 0.1)
    tried = []

    def fake_run(model, payload, units=1, bucket="generation"):
        tried.append(model)
        if "veo" in model:
            raise R.GenerationError("model unavailable")
        return "https://out/clip.mp4"
    monkeypatch.setattr(R, "run", fake_run)
    url, used, _, sound = R.generate_clip("orchard", "data:img", 6, route="veo", audio=True)
    assert tried == ["google/veo-3.1-fast", "kwaivgi/kling-v3-video"] and used == "kwaivgi/kling-v3-video"
    assert sound is False                                    # Kling's sound rate is far higher: fall back silent


def test_library_lists_every_card_with_where_its_still_comes_from(env):
    c = env.db()
    with c:
        c.execute("UPDATE adstudio_images SET archetype='cut_reveal' WHERE id=1")
    lib = {x["type"]: x for x in env.client.get("/api/adstudio/shots?product_id=1").get_json()["cards"]}
    assert len(lib) == 19 and all(x["status"] == "missing" for x in lib.values())   # 2026-11-11: two juice cards retired, a mango is not pulled apart by hand; + 9 style-only cards (S2; clay cut and reveal, sensory 2026-10-26) + 3 Behind the farm (v57)
    assert lib["cross_section_macro"]["from_image"] == "Hero" and lib["orchard_wide"]["from_image"] is None
    assert lib["orchard_wide"]["estimate"] == pytest.approx(0.18 + 6 * 0.15 + 0.05)   # card still + Veo with sound


def test_a_shot_is_made_once_and_then_reused(env, monkeypatch):
    monkeypatch.setattr("creative_intelligence.adstudio.chat._client", lambda **k: FakeClaude([({}, 1, 1)]))
    r = env.client.post("/api/adstudio/shots", json={"product_id": 1, "shot_type": "orchard_wide"}).get_json()
    assert r["reused"] is False
    drain(env.db)
    c = env.db()
    v = dict(c.execute("SELECT * FROM adstudio_videos WHERE id=?", (r["video_id"],)).fetchone())
    assert v["style"] == "shot" and v["status"] == "done" and v["has_audio"] == 1 and v["model"] == "google/veo-3.1-fast"
    assert v["prompt_version"].startswith("shot:orchard_wide@v3+")
    assert env.calls["video"][0][3:] == ("veo", True)                     # routed to Veo, with sound
    still = env.calls["still"][0][2]                                     # still made from the card, phone-real (v57)
    assert still.startswith("A vertical 9:16 still for a video shot")
    cost = c.execute("SELECT cost_usd FROM model_calls WHERE source='video' AND kind='video'").fetchone()[0]
    assert cost == pytest.approx(6 * 0.15)                                # the audio rate
    before = c.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0]
    again = env.client.post("/api/adstudio/shots", json={"product_id": 1, "shot_type": "orchard_wide"}).get_json()
    assert again == {"video_id": r["video_id"], "reused": True}
    assert c.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == before   # nothing generated
    lib = {x["type"]: x for x in env.client.get("/api/adstudio/shots?product_id=1").get_json()["cards"]}
    assert lib["orchard_wide"]["status"] == "passing"
    assert env.client.get(f"/api/adstudio/video/{r['video_id']}/clip").status_code == 200
    assert env.client.get(f"/api/adstudio/video/{r['video_id']}/poster").status_code == 200


def test_busy_shots_get_strict_qa_and_a_targeted_retake(env, monkeypatch):
    fake = FakeClaude([({"consistency": 4}, 2, 2), ({}, 2, 2)])          # 4 passes normally, not for strict cards
    monkeypatch.setattr("creative_intelligence.adstudio.chat._client", lambda **k: fake)
    r = env.client.post("/api/adstudio/shots", json={"product_id": 1, "shot_type": "hand_picking"}).get_json()
    drain(env.db)
    c = env.db()
    v = dict(c.execute("SELECT * FROM adstudio_videos WHERE id=?", (r["video_id"],)).fetchone())
    assert v["attempts"] == 2 and fake.calls == 2
    assert "Keep exactly the same fruits" in env.calls["video"][1][2]      # banana-claude style recovery note


def test_retire_frees_the_slot(env, monkeypatch):
    monkeypatch.setattr("creative_intelligence.adstudio.chat._client", lambda **k: FakeClaude([({}, 1, 1), ({}, 1, 1)]))
    first = env.client.post("/api/adstudio/shots", json={"product_id": 1, "shot_type": "whole_fruit_hero"}).get_json()
    drain(env.db)
    assert env.client.post(f"/api/adstudio/shot/{first['video_id']}/retire").get_json()["ok"]
    second = env.client.post("/api/adstudio/shots", json={"product_id": 1, "shot_type": "whole_fruit_hero"}).get_json()
    assert second["reused"] is False and second["video_id"] != first["video_id"]


def test_reuse_is_counted(env, monkeypatch):
    from creative_intelligence.adstudio import shots
    monkeypatch.setattr("creative_intelligence.adstudio.chat._client", lambda **k: FakeClaude([({}, 1, 1)]))
    r = env.client.post("/api/adstudio/shots", json={"product_id": 1, "shot_type": "cross_section_macro"}).get_json()
    drain(env.db)
    shots.use(env.db(), r["video_id"])
    assert env.client.get("/api/adstudio/shots?product_id=1").get_json()["reuses"] == 1


def test_a_restart_closes_a_half_made_shot(env):
    r = env.client.post("/api/adstudio/shots", json={"product_id": 1, "shot_type": "packing"}).get_json()
    c = env.db()
    jobs.claim(c, "dead:1")
    jobs.recover(c)                                                # the first time: made again
    assert c.execute("SELECT status FROM adstudio_videos WHERE id=?", (r["video_id"],)).fetchone()[0] == "queued"
    jobs.claim(c, "dead:2")
    jobs.recover(c)
    assert c.execute("SELECT status FROM adstudio_videos WHERE id=?", (r["video_id"],)).fetchone()[0] == "failed"



def test_an_empty_model_result_is_a_clear_failure():
    with pytest.raises(R.GenerationError, match="safety filter"):
        R.output_url(None)
    with pytest.raises(R.GenerationError):
        R.output_url([])


def test_wide_shots_skip_the_count_rule(env, monkeypatch):
    monkeypatch.setattr("creative_intelligence.adstudio.chat._client", lambda **k: FakeClaude([({}, 40, 45)]))
    r = env.client.post("/api/adstudio/shots", json={"product_id": 1, "shot_type": "orchard_wide"}).get_json()
    drain(env.db)
    lib = {x["type"]: x for x in env.client.get("/api/adstudio/shots?product_id=1").get_json()["cards"]}
    assert lib["orchard_wide"]["status"] == "passing" and len(env.calls["video"]) == 1



def test_end_frame_fields_per_model():
    p, _ = R._clip_payload("kwaivgi/kling-v3-video", "x", "start", 5, "9:16", False, None, "", end_image="end")
    assert p["end_image"] == "end"
    p, _ = R._clip_payload("google/veo-3.1-fast", "x", "start", 6, "9:16", False, None, "", end_image="end")
    assert p["last_frame"] == "end"
    p, _ = R._clip_payload("bytedance/seedance-2.0", "x", "start", 5, "9:16", False, None, "", end_image="end")
    assert p["last_frame_image"] == "end"


def test_knife_cut_starts_from_the_real_cut_photo_with_no_end_frame(env, monkeypatch):
    """v8 (2026-11-04 model test): ending on the cut photo made every model morph a whole fruit into cubes; the shot
    now starts already open, from the real cut photo, and only the camera moves."""
    monkeypatch.setattr("creative_intelligence.adstudio.chat._client", lambda **k: FakeClaude([({}, 1, 1)]))
    c = env.db()
    with c:
        c.execute("UPDATE adstudio_images SET archetype='cut_reveal' WHERE id=1")
    lib = {x["type"]: x for x in env.client.get("/api/adstudio/shots?product_id=1").get_json()["cards"]}
    assert lib["knife_cut"]["card_version"] == 8 and lib["knife_cut"]["from_image"] == "Hero"
    assert lib["knife_cut"]["end_image"] is None
    env.client.post("/api/adstudio/shots", json={"product_id": 1, "shot_type": "knife_cut"})
    drain(env.db)
    assert not env.calls.get("end") or not env.calls["end"][0]


# ── any product we add later ─────────────────────────────────────────────
def test_cards_follow_the_product_type_and_its_own_setup(env, monkeypatch):
    c = env.db()
    with c:
        c.execute("INSERT INTO adstudio_products (id, name, product_type, physical_desc) VALUES"
                  " (2, 'Guava Rose Candle', 'candle', 'amber jar, wood lid')")
        c.execute("UPDATE adstudio_products SET physical_desc='kiwi-sized, fuzzy brown skin, bright green flesh' WHERE id=1")
    assert env.client.get("/api/adstudio/shots?product_id=2").get_json()["cards"] == []        # no candle cards yet
    r = env.client.post("/api/adstudio/shots", json={"product_id": 2, "shot_type": "orchard_wide"})
    assert r.status_code == 400 and "isn't made for this product type" in r.get_json()["error"]
    monkeypatch.setattr("creative_intelligence.adstudio.chat._client", lambda **k: FakeClaude([({}, 1, 1)]))
    env.client.post("/api/adstudio/shots", json={"product_id": 1, "shot_type": "fruit_on_tree"})
    drain(env.db)
    assert "fuzzy brown skin" in env.calls["video"][0][2]                    # the clip knows this product
    still_prompt = __import__("creative_intelligence.adstudio.shots", fromlist=["x"]).still_prompt(
        {"name": "Golden Kiwi", "product_type": "fruit", "physical_desc": "fuzzy brown skin"},
        prompts.shot_cards()["fruit_on_tree"])
    assert "Golden Kiwi" in still_prompt and "fuzzy brown skin" in still_prompt


def test_never_starts_and_ends_on_the_same_photo(env, monkeypatch):
    import json as _json
    c = env.db()
    with c:
        c.execute("UPDATE adstudio_images SET archetype='kitchen_prep' WHERE id=1")
    cards = dict(prompts.shot_cards())
    knife = dict(cards["knife_cut"], from_images=["kitchen_prep"], end_from_images=["kitchen_prep"])
    monkeypatch.setattr(prompts, "shot_cards", lambda: {**cards, "knife_cut": knife})
    r = env.client.post("/api/adstudio/shots", json={"product_id": 1, "shot_type": "knife_cut"}).get_json()
    assert c.execute("SELECT source_kind FROM adstudio_videos WHERE id=?", (r["video_id"],)).fetchone()[0] == "card"


def test_stop_ends_before_the_next_paid_step(env):
    r = env.client.post("/api/adstudio/shots", json={"product_id": 1, "shot_type": "packing"}).get_json()
    assert env.client.post(f"/api/adstudio/video/{r['video_id']}/stop").get_json()["ok"]
    drain(env.db)
    c = env.db()
    v = c.execute("SELECT status, message FROM adstudio_videos WHERE id=?", (r["video_id"],)).fetchone()
    assert v[0] == "failed" and "nothing was spent" in v[1] and env.calls["video"] == [] and env.calls["still"] == []
    assert c.execute("SELECT COUNT(*) FROM ops_alerts").fetchone()[0] == 0               # a stop is not an alert
    assert env.client.post(f"/api/adstudio/video/{r['video_id']}/stop").status_code == 400


def test_compare_models_on_one_shot_from_the_same_still(env, monkeypatch):
    """Feedback 2026-10-26: "confirm what model is better for each variant". One still, one take per model, one QA."""
    from creative_intelligence.adstudio import shots
    monkeypatch.setattr("creative_intelligence.adstudio.chat._client", lambda **k: FakeClaude([({}, 1, 1)] * 3))
    e = env.client.post("/api/adstudio/shots/compare", json={"product_id": 1, "shot_types": ["knife_cut"]}).get_json()
    assert e["takes"] == 3 and e["estimate"] > 0
    q = env.client.post("/api/adstudio/shots/compare", json={"product_id": 1, "shot_types": ["knife_cut"],
                                                             "confirm": True}).get_json()
    assert q["queued"] == 1
    drain(env.db)
    takes = env.client.get("/api/adstudio/shots/compare?product_id=1").get_json()["takes"]
    assert sorted(t["route"] for t in takes) == ["kling", "seedance", "veo"] and all(t["status"] == "done" for t in takes)
    assert [c[3] for c in env.calls["video"]] == ["kling", "veo", "seedance"] or len(env.calls["video"]) == 3
    assert len(env.calls["still"]) == 1                                    # one still, shared
    c = env.db()
    stills = {r[0] for r in c.execute("SELECT still_path FROM adstudio_videos WHERE id IN (%s)" % ",".join(
        str(t["id"]) for t in takes))}
    assert len(stills) == 1
    assert shots.passing(c, 1, "knife_cut") is None                        # out of the library until promoted
    win = next(t for t in takes if t["route"] == "veo")
    assert env.client.post(f"/api/adstudio/shot/{win['id']}/promote").get_json() == {"ok": True}
    assert shots.passing(c, 1, "knife_cut")["id"] == win["id"]


def test_seedance_gets_the_negatives_as_constraints_and_veo_a_720_still(tmp_path):
    """Research 2026-10-26: Seedance has no negative field (BytePlus asks for constraint sentences); Veo wants 720x1280."""
    from PIL import Image
    from creative_intelligence.adstudio import animate
    p, _ = R._clip_payload("bytedance/seedance-2.0", "Slice it.", "data:image/png;base64,x", 5, "9:16", False, None,
                           "extra fruit, warped hands")
    assert "negative_prompt" not in p and "Avoid: extra fruit, warped hands." in p["prompt"] and "one continuous shot" in p["prompt"]
    Image.new("RGB", (900, 1600)).save(tmp_path / "s.png")
    assert Image.open(animate.veo_still(str(tmp_path / "s.png"))).size == (720, 1280)


def test_shots_that_change_the_fruit_on_purpose_skip_the_count_check():
    """A knife cut makes two halves and a plated shot loses a slice; QA forced 'Same fruit first to last' to 1 on every
    take (comparison, 2026-10-26). These cards judge the fruit's look instead, with a note saying why."""
    from creative_intelligence import prompts
    cards = prompts.shot_cards()
    for t in ("knife_cut", "plated_sharing", "clay_cut"):
        assert cards[t]["count_check"] is False and "same" in cards[t]["qa_note"].lower(), t
    assert cards["whole_fruit_hero"].get("count_check", True)


def test_a_shot_that_changes_on_purpose_needs_same_fruit_4_not_5(env, monkeypatch):
    """Live test 2026-10-26: plated scored 4.29 but failed on 'Same fruit first to last' 4 (strict wants 5)."""
    from creative_intelligence.adstudio import qa, video_qa
    c = env.db()
    keys = [k["key"] for k in qa.load_rubric()["criteria"]]
    reading = {"scores": {**{k: 5 for k in keys}, "consistency": 4}, "reasoning": "ok"}
    monkeypatch.setattr(video_qa, "ask_claude", lambda *a, **k: (reading, None))
    monkeypatch.setattr(qa, "_log_cost", lambda *a, **k: 0.0)
    assert video_qa.score(c, 1, "x.mp4", [], {"name": "M"}, strict_qa=True, count_check=False)["passed"]
    assert not video_qa.score(c, 1, "x.mp4", [], {"name": "M"}, strict_qa=True, count_check=True)["passed"]
