"""v57 (feedback 2026-11-04): "the quality needs to be the same on the same video; can we test a couple videos with
seedance 2.5 and wan 3 and compare with current". A video is remade word for word with every shot on one model, from
the same stills, never mixing models (both on Replicate); presenters can speak through Seedance 2.5 from a reference
picture and a black-screen video carrying the voice. Every external call is faked."""
from __future__ import annotations

import json

from creative_intelligence.adstudio import shots, talking, video_batch as VB
from creative_intelligence.providers import replicate
from tests.creative_intelligence.test_f6_video_batch import conn  # noqa: F401  (fixture)


def test_seedance_25_from_a_first_frame_lets_the_model_pick_the_ratio():
    p, _ = replicate._clip_payload("bytedance/seedance-2.5", "x", "data:img", 5, "9:16", False, None, "")
    assert p["image"] == "data:img" and p["aspect_ratio"] == "adaptive"
    p, _ = replicate._clip_payload("bytedance/seedance-2.0", "x", "data:img", 5, "9:16", False, None, "")
    assert p["aspect_ratio"] == "9:16"


def test_wan3_runs_on_replicate_and_never_falls_back(monkeypatch):
    monkeypatch.setattr("creative_intelligence.budget.check_now", lambda *a, **k: None)
    monkeypatch.setattr("creative_intelligence.config.CI_REPLICATE_API_KEY", "set")
    monkeypatch.setattr(replicate, "estimate", lambda *a, **k: 0.5)
    sent = []
    monkeypatch.setattr(replicate, "run", lambda model, payload, **k: sent.append((model, payload)) or "https://r/v.mp4")
    url, model, secs, sound = replicate.generate_clip("p", "data:img", 5, route="wan3", negative="melting")
    assert (url, model, secs, sound) == ("https://r/v.mp4", "alibaba/wan-3", 5, False)
    assert sent[0][1] == {"prompt": "p", "duration": 5, "resolution": "720p", "aspect_ratio": "9:16", "image": "data:img",
                          "negative_prompt": "melting"}

    def down(model, payload, **k):
        sent.append((model, payload))
        raise replicate.GenerationError("boom")
    monkeypatch.setattr(replicate, "run", down)
    sent.clear()
    try:
        replicate.generate_clip("p", "data:img", 5, route="wan3")
        raise AssertionError("should have failed")
    except replicate.GenerationError:
        assert [m for m, _ in sent] == ["alibaba/wan-3"]                 # no other model stands in


def test_an_engine_take_is_made_on_that_model_from_the_given_still(conn):
    vid, why, reused = shots.create(conn, 1, "cross_section_macro", look="b9.0", style="fruit_story",
                                    engine="seedance25", still="/vol/still.png")
    assert vid and not why and not reused
    r = conn.execute("SELECT route, model, prompt_version, still_path FROM adstudio_videos WHERE id=?", (vid,)).fetchone()
    assert r["route"] == "seedance25" and r["model"] == "bytedance/seedance-2.5" and r["still_path"] == "/vol/still.png"
    assert "+engine:seedance25+" in r["prompt_version"] and "+look:b9.0:fruit_story+" in r["prompt_version"]


def _batch(conn, items):
    with conn:
        return conn.execute("INSERT INTO video_batches (request_json, items_json, status, created_at) VALUES (?,?,?,?)",
                            (json.dumps({"product_ids": [1], "voice": "Wade"}), json.dumps(items), "drafts",
                             "2026-11-04T10:00:00")).lastrowid


def _item(style="fruit_story", kinds=("narrator", "narrator"), own=("whole_fruit_hero", "knife_cut")):
    lines = [{"text": f"Line {n}", "kind": k, "shot_type": t} for n, (k, t) in enumerate(zip(kinds, own))]
    return {"product_id": 1, "product": "Rosy Sunshine Mango", "style": style, "label": "Fruit Story", "keep": True,
            "script": {"style": style, "lines": lines, "own_shots": {t: "b1.0" for t in own}, "missing_shots": ["knife_cut"],
                       "hooks": ["Hook"], "hook_shots": [own[0]]}}


def test_a_remake_keeps_the_words_and_takes_every_shot_on_the_one_model(conn, monkeypatch):
    monkeypatch.setattr(shots, "passing", lambda c, pid, t, look="": {"still_path": f"/vol/{t}.png"} if look else None)
    bid = _batch(conn, [_item()])
    nb, why = VB.remake(conn, bid, "wan3", [0])
    assert nb and not why
    _, req, items = VB._load(conn, nb)
    s = items[0]["script"]
    assert req["engine"] == "wan3" and [ln["text"] for ln in s["lines"]] == ["Line 0", "Line 1"]
    assert s["engine"] == "wan3" and s["missing_shots"] == [] and set(s["own_shots"].values()) == {f"b{nb}.0"}
    assert s["engine_stills"] == {"whole_fruit_hero": "/vol/whole_fruit_hero.png", "knife_cut": "/vol/knife_cut.png"}
    made = []
    monkeypatch.setattr(shots, "create", lambda c, pid, t, **k: made.append((t, k["engine"], k["still"])) or (50, "", False))
    VB.approve(conn, nb)
    assert sorted(made) == [("knife_cut", "wan3", "/vol/knife_cut.png"),
                            ("whole_fruit_hero", "wan3", "/vol/whole_fruit_hero.png")]


def test_videos_that_would_mix_models_cant_be_remade(conn):
    bid = _batch(conn, [_item(kinds=("presenter", "narrator")), _item(own=("whole_fruit_hero", "orchard_wide"))])
    VB._save(conn, bid, [_item(kinds=("presenter", "narrator")),
                         {**_item(), "script": {**_item()["script"], "own_shots": {"whole_fruit_hero": "b1.0"}}}])
    assert "only videos made of shots" in VB.remake(conn, bid, "wan3", [0])[1]           # a presenter: Seedance only
    assert "knife_cut had no take of its own" in VB.remake(conn, bid, "seedance25", [1])[1]
    assert "Unknown model" in VB.remake(conn, bid, "veo", [1])[1]


def test_seedance_talks_from_a_reference_picture_and_a_black_video_with_the_voice():
    p = talking.seedance_talking_payload("data:face", "BLACK", 6.2, "holds up the fruit")
    assert p["reference_images"] == ["data:face"] and p["reference_videos"] == ["BLACK"] and "image" not in p
    assert p["duration"] == 7 and "[Image1]" in p["prompt"] and "[Video1]" in p["prompt"]
    assert "holds up the fruit" in p["prompt"] and talking.METHODS["seedance"]["model"] == "bytedance/seedance-2.5"


def test_behind_the_farm_is_a_batch_format_with_quiet_captions_and_its_own_shots():
    from creative_intelligence import prompts
    from creative_intelligence.adstudio import styles, timeline
    assert "behind_the_farm" in styles.catalog() and "behind_the_farm" in VB.batch_styles()
    look = styles.STYLE_LOOKS["behind_the_farm"]
    assert look["captions"] == "quiet" and look["transition"] == 0.0 and look["max_words"] == 6
    assert "quiet" in timeline.CAPTION_STYLES
    cards = prompts.shot_cards()
    assert all(k in cards and shots.own_ok(k) for k in ("hand_sorting", "paper_wrap", "doorstep_delivery"))
    text = prompts.text(styles.style_prompt("behind_the_farm"))
    assert "ice packs" in text and "arrives fresh" in text and "a small fruit farm" in text   # brand.yaml › about


def test_real_world_stills_ask_for_phone_footage_and_stylized_ones_dont():
    real = shots.still_prompt({"name": "Cherry Mango"}, shots.card("cross_section_macro"))
    clay = shots.still_prompt({"name": "Cherry Mango"}, shots.card("clay_hero"))
    assert "frame from a real phone video" in real and "no phone, screen or camera-app buttons" in real
    assert "one clear subject" in real and "premium" not in real
    assert "real phone video" not in clay


def test_a_presenter_video_is_remade_on_seedance_with_the_presenter_speaking_through_it(conn, monkeypatch):
    monkeypatch.setattr(shots, "passing", lambda c, pid, t, look="": {"still_path": f"/vol/{t}.png"} if look else None)
    bid = _batch(conn, [_item(kinds=("presenter", "narrator"))])
    nb, why = VB.remake(conn, bid, "seedance25", [0])
    assert nb and not why and VB._load(conn, nb)[1]["method"] == "seedance"


def test_a_stopped_model_test_plays_its_best_take_and_says_so(conn, monkeypatch):
    bid = _batch(conn, [_item()])
    monkeypatch.setattr(shots, "passing", lambda c, pid, t, look="": {"still_path": "/vol/x.png"} if look else None)
    nb, _ = VB.remake(conn, bid, "wan3", [0])
    _, _, items = VB._load(conn, nb)
    items[0]["error"] = "The knife cut take on wan3 didn't pass QA"
    VB._save(conn, nb, items, status="drafts")
    assert VB.retry(conn, nb) == {"retried": 1}
    assert VB._load(conn, nb)[0]["status"] == "shooting" and VB._load(conn, nb)[2][0]["error"] == ""
    assert "Nothing stopped" in VB.retry(conn, bid)["error"]


def test_best_take_prefers_a_pass_then_the_score(conn):
    with conn:
        for vid, ok, total in ((1, 0, 3.9), (2, 1, 3.6), (3, 0, 4.2)):
            conn.execute("INSERT INTO adstudio_videos (id, product_id, source_kind, source_id, style, shot_type, status,"
                         " clip_path, prompt_version, created_at) VALUES (?, 1, 'card', 0, 'shot', 'knife_cut', 'done',"
                         " '/c.mp4', 'shot:knife_cut@v8+look:b9.0:pov++engine:wan3+', '2026-11-04')", (vid,))
            conn.execute("INSERT INTO qa_scores (source, source_id, total, passed, scores_json, rubric_version)"
                         " VALUES ('video', ?, ?, ?, '{}', 5)", (vid, total, ok))
    assert shots.best_take(conn, 1, "knife_cut", "b9.0")["id"] == 2
    assert shots.best_take(conn, 1, "knife_cut", "b8.0") is None


def test_seedance_can_draw_the_presenter_from_the_description_with_no_face():
    """Seedance refuses realistic faces as references (2026-11-04): the presenter is described instead."""
    p = {"id": 5, "look": "a sun-weathered grower in his 50s, a worn cap || a green fruit orchard"}
    a = talking.seedance_text_payload(p, "BLACK", 6.2)
    assert "reference_images" not in a and "image" not in a and a["reference_videos"] == ["BLACK"]
    assert a["prompt"].startswith("A real handheld phone video. A sun-weathered grower") and "green fruit orchard" in a["prompt"]
    assert a["seed"] == talking.seedance_text_payload(p, "B", 3)["seed"] == 1005 and "[Video1]" in a["prompt"]
    assert talking.METHODS["seedance_text"]["model"] == "bytedance/seedance-2.5"


def test_each_line_is_read_knowing_the_lines_around_it():
    """2026-11-04: "sounds … still not 100%": every line was voiced cold; ElevenLabs gets its neighbours."""
    from creative_intelligence.adstudio import styles, voices
    lines = [{"text": "One.", "kind": "presenter"}, {"text": "On screen", "kind": "text"},
             {"text": "Two.", "kind": "narrator"}, {"text": "Three.", "kind": "narrator"}]
    st = {"lines": lines}
    assert styles.spoken_context(st, lines[0]) == ("", "Two.")
    assert styles.spoken_context(st, lines[2]) == ("One.", "Three.")
    assert styles.spoken_context(st, lines[3]) == ("Two.", "")
    v = {"name": "Wade", "model": "elevenlabs/v2-multilingual", "voice": "Roger"}
    assert voices.payload(v, "Two.", ("One.", "Three.")) == {"prompt": "Two.", "voice": "Roger", "stability": 0.4,
                                                            "previous_text": "One.", "next_text": "Three."}
    assert "previous_text" not in voices.payload(v, "Two.")
    v3 = {**v, "model": "elevenlabs/v3"}                 # v3 refuses the context (live, 2026-11-04)
    assert "previous_text" not in voices.payload(v3, "Two.", ("One.", "Three."))


def test_auditions_only_use_real_elevenlabs_voices(conn):
    from creative_intelligence.adstudio import voices
    for bad, why in (("Nobody", "unknown ElevenLabs voice"), ("Roger", "Write the line")):
        try:
            voices.audition(conn, bad, "" if bad == "Roger" else "Hi.")
            raise AssertionError("should have failed")
        except ValueError as exc:
            assert why in str(exc)


def test_new_voice_engines_take_their_own_fields():
    from creative_intelligence.adstudio import voices
    inw = {"name": "x", "model": "inworld/realtime-tts-2", "voice": "Dennis", "style": "say calmly and proudly"}
    assert voices.payload(inw, "Hi.") == {"text": "[say calmly and proudly] Hi.", "voice_id": "Dennis"}
    gem = {"name": "x", "model": "google/gemini-3.8-flash-tts", "voice": "Gacrux", "style": "warm, nostalgic"}
    assert voices.payload(gem, "Hi.") == {"text": "Hi.", "voice": "Gacrux", "output_format": "mp3", "style": "warm, nostalgic"}
    mm = {"name": "x", "model": "minimax/speech-2.8-hd", "voice": "English_Wiselady"}
    assert voices.payload(mm, "Hi.") == {"text": "Hi.", "voice_id": "English_Wiselady", "emotion": "auto"}


def test_a_persona_that_tells_memories_gets_them_past_the_claims_check(conn, monkeypatch):
    from creative_intelligence.adstudio import facts, presenters
    presenters.ensure_presets(conn)
    pid = conn.execute("SELECT id FROM presenters WHERE name='Ivy'").fetchone()[0]
    assert not presenters.tells_memories(conn, pid)
    assert presenters.update(conn, pid, {"memories": True, "brief": "A warm grandmother."}) == ""
    assert presenters.tells_memories(conn, pid) and "first-person memories" in presenters.brief_block(conn, pid)
    sent = []

    class Fake:
        class beta:
            class messages:
                @staticmethod
                def create(**k):
                    sent.append(k["messages"][0]["content"])
                    raise RuntimeError("stop")
    prod = {"id": 1, "name": "Rosy Sunshine Mango", "product_type": "fruit"}
    facts.check_copy(conn, prod, {"line": "When I was a little girl, mango was summer."}, client=Fake(), allow=facts.MEMORIES)
    facts.check_copy(conn, prod, {"line": "When I was a little girl, mango was summer."}, client=Fake())
    assert "ALLOWED BY THE OWNER" in sent[0] and "ALLOWED BY THE OWNER" not in sent[1]


def _line(text, shot):
    return {"text": text, "kind": "narrator", "shot_type": shot}


def test_a_batch_model_applies_to_videos_made_of_their_own_shots_only():
    it, s = {}, {"style": "fruit_story", "lines": [_line("A", "whole_fruit_hero"), _line("B", "knife_cut")],
                 "own_shots": {"whole_fruit_hero": "b1.0", "knife_cut": "b1.0"}, "missing_shots": ["knife_cut"]}
    VB.apply_engine(it, s, "seedance25")
    assert s["engine"] == "seedance25" and s["missing_shots"] == [] and not it.get("engine_note")
    it, s = {}, {"style": "fruit_story", "lines": [_line("A", "orchard_wide")], "own_shots": {"whole_fruit_hero": "x"}}
    VB.apply_engine(it, s, "wan3")
    assert "engine" not in s and "Made on Kling" in it["engine_note"]
    it, s = {}, {"style": "claymation", "lines": [_line("A", "clay_hero")], "own_shots": {}}
    VB.apply_engine(it, s, "seedance25")
    assert "engine" not in s and it["engine_note"]


def test_a_storyboard_prompt_edit_goes_into_the_take(conn, monkeypatch):
    with conn:
        conn.execute("INSERT INTO video_batches (request_json, items_json, status, created_at) VALUES (?,?,?,?)",
                     (json.dumps({"product_ids": [1]}), json.dumps([_item()]), "planned", "2026-11-04"))
    bid = conn.execute("SELECT max(id) FROM video_batches").fetchone()[0]
    st = VB.status(conn, bid)["items"][0]["prompts"]
    assert set(st) == {"whole_fruit_hero", "knife_cut"} and "vertical 9:16" in st["knife_cut"]["still"]
    assert VB.edit(conn, bid, 0, {"prompt_shot": "knife_cut", "still": "My own picture prompt."}) == ""
    assert VB.edit(conn, bid, 0, {"prompt_shot": "orchard_wide", "still": "x"}).startswith("That shot isn't")
    p = VB.status(conn, bid)["items"][0]["prompts"]["knife_cut"]
    assert p["still"] == "My own picture prompt." and p["edited"]
    vid, why, _ = shots.create(conn, 1, "knife_cut", look="b9.1", style="fruit_story",
                               prompt={"still": "My own picture prompt.", "motion": "Slow push in."})
    r = conn.execute("SELECT motion_prompt, copy_json, prompt_version FROM adstudio_videos WHERE id=?", (vid,)).fetchone()
    assert r["motion_prompt"] == "Slow push in." and json.loads(r["copy_json"])["still_prompt"] == "My own picture prompt."
    assert "+edited+" in r["prompt_version"]
    assert VB.edit(conn, bid, 0, {"prompt_shot": "knife_cut", "reset": True}) == ""
    assert not VB.status(conn, bid)["items"][0]["prompts"]["knife_cut"]["edited"]


def test_a_voice_model_that_answers_with_a_list_still_gives_one_url():
    from creative_intelligence.adstudio import voices

    class F:
        def url(self):
            return "https://r/a.mp3"
    assert voices._url([F()]) == "https://r/a.mp3" and voices._url("https://r/b.mp3") == "https://r/b.mp3"


def test_a_chosen_model_gets_every_shot_type_and_fixed_formats_get_no_angle():
    from creative_intelligence.adstudio import styles
    types = [k for k in __import__("creative_intelligence").prompts.shot_cards() if shots.own_ok(k)][:7]
    s = {"style": "fruit_story", "lines": [_line("x", t) for t in types]}
    assert len(VB.own_shots(s, "b1.0")) == VB.OWN_SHOTS and len(VB.own_shots(s, "b1.0", cap=99)) == 7
    assert styles.angle_for("behind_the_farm", 3) == "" and styles.angle_for("fruit_story", 3) in styles.ANGLES


def test_each_format_gets_its_own_narrator_delivery():
    from creative_intelligence.adstudio import styles, voices
    assert "storyteller" in styles.narrator_style({"style": "claymation"})
    ch = styles.narrator_style({"style": "fruit_characters", "character": {"personality": "a goofy mango who tells puns"}})
    assert "cartoon fruit character" in ch and "goofy mango" in ch
    assert styles.narrator_style({"style": "fruit_story"}) == ""
    v = {"name": "x", "model": "elevenlabs/v3", "voice": "Mark", "expressive": True}
    assert voices.payload(v, "Hi.")["stability"] == 0.3 and voices.payload(v, "Hi.")["style"] == 0.5


def test_retry_redoes_only_a_failed_draft_and_reuses_its_pieces(conn, monkeypatch):
    with conn:
        for vid, st, pieces in ((70, "failed", {"k1": {"audio": "/a.wav"}}), (71, "done", {})):
            conn.execute("INSERT INTO adstudio_videos (id, product_id, source_kind, source_id, style, status, copy_json,"
                         " created_at) VALUES (?, 1, 'styled', 0, 'talking_head', ?, ?, '2026-11-04')",
                         (vid, st, json.dumps({"pieces": pieces})))
    a, b = _item(), _item()
    a["draft_id"], b["draft_id"] = 70, 71
    bid = _batch(conn, [a, b])
    assert VB.retry(conn, bid) == {"retried": 1}
    _, _, items = VB._load(conn, bid)
    assert items[0].get("redo_of") == 70 and "draft_id" not in items[0] and items[1]["draft_id"] == 71


def test_retry_remakes_a_draft_judged_bad_from_scratch(conn):
    """2026-11-10: the pack's table presenter draft was a different face each clip: judged bad, a retry remakes it
    without reusing its presenter clips."""
    with conn:
        conn.execute("INSERT INTO adstudio_videos (id, product_id, source_kind, source_id, style, status, copy_json,"
                     " created_at) VALUES (72, 1, 'styled', 0, 'presenter_table', 'done', '{}', '2026-11-10')")
    a, b = _item(), _item()
    a["draft_id"], a["draft_ok"], b["draft_id"], b["draft_ok"] = 72, False, 72, True
    bid = _batch(conn, [a, b])
    assert VB.retry(conn, bid) == {"retried": 1}
    _, _, items = VB._load(conn, bid)
    assert items[0]["redo_of"] == 72 and items[0]["fresh"] and "draft_ok" not in items[0]
    assert items[1]["draft_id"] == 72


def test_grandmother_voice_only_narrates_her_own_videos():
    """2026-11-05: "don't use gradma voice as narrator when she is not presenter"."""
    from creative_intelligence.adstudio import characters, styles
    assert styles.narrator_voice("Ivy") == "Tess"
    assert styles.narrator_voice("Ivy", "Wade") == "Tess"
    assert styles.narrator_voice("Ivy", "Ivy") == "Ivy"
    assert styles.narrator_voice("Wade") == "Wade"
    assert all(c["voice"] != "Ivy" for c in characters.CAST)


def test_every_clip_is_told_no_on_screen_text():
    """2026-11-05: fruit-character clips came back with Chinese subtitles burned in."""
    from creative_intelligence import prompts
    from creative_intelligence.adstudio import shots
    assert "subtitles" in shots.no_text() and "subtitles" in shots.no_text("cuts")
    assert shots.no_text("cuts").startswith("cuts, ")
    assert "subtitles" in prompts.text("animate-motion-rules", motion="m", product="p")
