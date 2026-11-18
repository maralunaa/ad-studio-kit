"""v72 (feedback 2026-11-09, the Blood Orange ad pack): "a lot of 'tastes like raspberry' … it has raspberry colour not
taste"; "the clay one says 'this clay orange'"; "text over fruit is not matching voice over". No external calls."""
from __future__ import annotations

from creative_intelligence.adstudio import facts, styles
from tests.creative_intelligence.test_f6_video_batch import conn  # noqa: F401  (fixture)

BLOOD = {"id": 0, "name": "Blood Orange (Raspberry)", "varieties": "", "product_type": "fruit"}


def test_a_nickname_in_brackets_is_not_the_fruit_but_its_colour_is_fine(conn):
    assert facts.other_fruit_hits(conn, BLOOD, "Tastes like raspberry") == ["raspberry"]
    assert facts.other_fruit_hits(conn, BLOOD, "It tastes like orange, then raspberry.") == ["raspberry"]
    assert facts.other_fruit_hits(conn, BLOOD, "Raspberry-red flesh inside") == []
    assert facts.other_fruit_hits(conn, BLOOD, "flesh the colour of a raspberry red wine") == []
    assert "raspberry" in facts.own_fruits(conn, {"name": "Raspberry box", "varieties": ""})


def test_the_copy_rules_say_a_taste_comparison_is_a_claim(conn):
    from creative_intelligence import prompts
    assert "tastes like another food is a claim" in facts.prompt_block(conn, {"name": "Mango"})
    assert "tastes like raspberry" in prompts.text("claims-check-system")


def test_clay_and_cartoon_scripts_never_name_what_they_are_made_of():
    assert styles.medium_hits("claymation", ["this clay fellow thinks he's it"]) == ["clay"]
    assert styles.medium_hits("fruit_characters", ["I'm a cartoon orange"]) == ["cartoon"]
    assert styles.medium_hits("fruit_story", ["clay soil"]) == []
    msg = styles.claim_problems({"banned": [{"term": "clay", "rule": "medium"}]})[0]
    assert "made of" in msg


def test_the_pack_makes_its_presenters_the_way_the_owner_picked(conn):
    from creative_intelligence.adstudio import adpack
    req = adpack._video_request(conn, 1, 5, {"sizes": ["9:16"], "direction": ""}, {"talking_head": 1})
    assert req["method"] == styles.PICKED_TALKING and req["voice"] == req["presenter_id"] == "rotate"
    r = adpack.normalize({"voice": "June", "presenter_id": "4"})
    assert (r["voice"], r["presenter_id"]) == ("June", 4)
    assert adpack.normalize({"voice": "Nobody"})["voice"] == "rotate" and adpack.normalize({})["presenter_id"] == "rotate"


def test_rotate_gives_every_voice_and_presenter_equal_use_least_used_first(conn):
    """2026-11-09: "how is voice and presenter chosen? there should be equal usage of all … or an option to choose"."""
    from creative_intelligence.adstudio import video_batch as VB
    with conn:
        for i in (1, 2, 3):
            conn.execute("INSERT INTO presenters (id, name, status, created_at) VALUES (?, ?, 'approved', 'now')", (i, f"P{i}"))
        conn.execute("INSERT INTO presenters (id, name, status, created_at) VALUES (9, 'Draft', 'draft', 'now')")
        conn.execute("INSERT INTO adstudio_videos (product_id, source_kind, source_id, style, duration_s, model, voice, ai_person, created_at, copy_json)"
                     " VALUES (1, 'styled', 0, 'talking_head', 30, 'styled', 'Wade', 1, 'now', '{\"presenter_id\": 1}')")
    names = VB.narrator_voices()
    assert "Ivy" not in names and "Wade" in names                 # the grandmother's voice is hers alone
    narrated = [{"style": "claymation"} for _ in range(len(names))]
    hosted = [{"style": "talking_head"} for _ in range(3)]
    VB.cast(conn, {"voice": "rotate", "presenter_id": "rotate"}, narrated + hosted)
    got = [i["voice"] for i in narrated]
    assert sorted(got) == sorted(names) and got[-1] == "Wade"     # each once, the one already used last
    assert [i["presenter_id"] for i in hosted] == [2, 3, 1] and all(i["voice"] for i in hosted)
    chosen = [{"style": "fruit_story"}, {"style": "talking_head"}]
    VB.cast(conn, {"voice": "June", "presenter_id": 3}, chosen)   # a choice: the request's own, nothing per video
    assert chosen == [{"style": "fruit_story"}, {"style": "talking_head"}]
    assert VB.who({}, {"voice": "June"}, "voice") == "June" and VB.who({"voice": "Tess"}, {"voice": "rotate"}, "voice") == "Tess"
    assert VB.who({}, {"presenter_id": "rotate"}, "presenter_id") is None


def test_scores_are_never_stopped_by_the_text_cap_and_the_table_keeps_one_face(monkeypatch):
    """2026-11-10: three packs used the day's Claude cap on scripts, so the paid clips and AI ads were never scored and
    every video failed; and Presenter at the table on Seedance-from-text showed a different man in every clip."""
    from creative_intelligence import budget
    from creative_intelligence.adstudio import costs, talking
    calls = []
    monkeypatch.setattr(budget, "check_now", lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(budget.BudgetExceeded("cap")))

    class Msgs:
        def create(self, **kw):
            return "ok"
    assert costs._LoggedMessages(Msgs(), "qa", log=False).create(model="m") == "ok" and not calls
    try:
        costs._LoggedMessages(Msgs(), "copy", log=False).create(model="m")
        raise AssertionError("copy should be capped")
    except budget.BudgetExceeded:
        pass
    assert styles.scene_method("seedance_text", "presenter_table") == talking.FALLBACK
    assert styles.scene_method("seedance_text", "fruit_story") == talking.FALLBACK
    assert styles.scene_method("seedance_text", "talking_head") == talking.FALLBACK   # from the green cut-out
    assert styles.scene_method("kling_avatar", "presenter_table") == "kling_avatar"


def test_a_shot_that_was_never_scored_is_scored_again_without_making_it_again(conn, monkeypatch):
    from creative_intelligence.adstudio import animate, shots, video_qa
    with conn:
        vid = conn.execute(
            "INSERT INTO adstudio_videos (product_id, source_kind, source_id, style, duration_s, model, status, shot_type,"
            " clip_path, message, created_at) VALUES (1, 'card', 0, 'shot', 5, 'm', 'done', 'box_opening', '/x.mp4',"
            " 'not scored after 1 clip(s); $0.58', 'now')").lastrowid
        conn.execute("INSERT INTO adstudio_videos (product_id, source_kind, source_id, style, duration_s, model, status,"
                     " shot_type, clip_path, message, created_at) VALUES (1, 'card', 0, 'shot', 5, 'm', 'done',"
                     " 'box_opening', '/y.mp4', 'passed QA after 1 clip(s); $0.58', 'now')")
    assert shots.unscored(conn, 1) == [vid]
    monkeypatch.setattr(shots, "_inputs", lambda c, v: ({}, {"name": "x"}, []))
    monkeypatch.setattr(video_qa, "score", lambda *a, **k: {"passed": True, "total": 4.5})
    monkeypatch.setattr(animate, "_set", lambda c, v, **k: c.execute("UPDATE adstudio_videos SET message=? WHERE id=?",
                                                                     (k["message"], v)))
    assert shots.rescore(vid, lambda: _Keep(conn))["passed"] and shots.unscored(conn, 1) == []


class _Keep:
    """The test's connection, not closed by the code under test."""
    def __init__(self, c):
        self._c = c

    def __getattr__(self, n):
        return (lambda: None) if n == "close" else getattr(self._c, n)


def test_mango_is_not_pulled_apart_by_hand_and_robotic_voices_dont_narrate():
    """Feedback 2026-11-11: a mango whole and cut at once (Hands pull it apart), and a "very robotic" narrator."""
    from creative_intelligence import config, prompts
    from creative_intelligence.adstudio import shots
    from creative_intelligence.adstudio import video_batch as VB
    c = shots.card("hand_tear")
    assert not shots.applies(c, {"name": "Honey Mango", "product_type": "fruit"})
    assert shots.applies(c, {"name": "Cherimoya", "product_type": "fruit"})
    assert "never both" in prompts.text("fruit-reference-lock") and "gel" in prompts.text("video-qa-system")
    kokoro = [v["name"] for v in config.HOUSE_VOICES if v["model"] == config.KOKORO_MODEL]
    assert kokoro and not set(kokoro) & set(VB.narrator_voices())


def test_citrus_is_not_torn_or_spooned_juice_shots_are_retired_and_boxes_stay_whole():
    """Feedback 2026-11-11: "not really how you open an orange", juice "always some syrup", "the box is being built"."""
    from creative_intelligence import knowledge, prompts
    from creative_intelligence.adstudio import shots
    orange = {"name": "Blood Orange", "product_type": "fruit"}
    assert not shots.applies(shots.card("hand_tear"), orange) and not shots.applies(shots.card("spoon_scoop"), orange)
    assert shots.applies(shots.card("knife_cut"), orange)
    assert not shots.applies(shots.card("juice_drip"), orange) and not shots.applies(shots.card("juice_texture_macro"), orange)
    assert "juice_drip" not in knowledge.style("sensory")["shot_types"]
    assert "rigid from the first frame" in shots.card("packing")["motion"]["change"]
    assert "builds itself up" in prompts.text("video-qa-system")


def test_a_batch_keeps_each_format_inside_its_own_length():
    """Feedback 2026-11-11: the clay papaya ran 28 s on three clay clips, one looping ("we can make some shorter")."""
    from creative_intelligence.adstudio import video_batch as VB
    assert VB.fit_length("claymation", 30) == 20 and VB.fit_length("sensory", 30) == 15
    assert VB.fit_length("claymation", 5) == 10 and VB.fit_length("no_such_style", 30) == 30


def test_a_product_not_in_the_store_gets_a_learn_more_button_and_a_candle_may_name_its_scent(conn):
    """2026-11-13: every ad for a hand-added candle was skipped: "Shop now" is banned for a product not sold in the
    store, and its scent notes (pineapple, passion fruit) read as other fruits."""
    from creative_intelligence.adstudio import facts
    candle = {"id": 0, "name": "Candle - Wild Passion", "product_type": "candle", "origin": "local", "shopify_url": "",
              "scent_notes": "Pink Pineapple and Passion fruit scented"}
    assert facts.store_safe_cta(candle, "Shop now") == "Learn more" and facts.store_safe_cta(candle, "Discover") == "Discover"
    sold = {**candle, "shopify_url": "https://shop.example/products/wild-passion"}
    assert facts.store_safe_cta(sold, "Shop now") == "Shop now"
    assert facts.other_fruit_hits(conn, candle, "Pink pineapple and passion fruit, poured by hand") == []
    assert facts.other_fruit_hits(conn, candle, "Smells like mango") == ["mango"]
