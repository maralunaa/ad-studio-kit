"""v51 (2026-11-03): captions follow the voice, steadier captions, the read-aloud pass, hard cuts, presenter QA note."""
import json
from types import SimpleNamespace

from creative_intelligence.adstudio import styles, timeline, video_qa


def _heard(text, step=0.4):
    return [{"word": w, "start": round(i * step, 3), "end": round(i * step + 0.3, 3)} for i, w in enumerate(text.split())]


def test_a_number_said_as_words_keeps_the_captions_on_the_voice():
    got = _heard("It is only thirty nine dollars a box okay")
    out = styles.align_words("It's only $39 a box, okay?".split(), got, 0, 5)
    by = {w["word"]: w for w in out}
    assert by["only"]["start"] == got[2]["start"]
    assert got[3]["start"] <= by["$39"]["start"] < by["$39"]["end"] <= got[5]["end"]
    assert by["okay?"]["start"] == got[-1]["start"]


def test_a_line_with_nothing_heard_is_spread_over_its_span():
    out = styles.script_words([{"kind": "narrator", "text": "one two three"}], [(2.0, 4.0)], [])
    assert [w["word"] for w in out] == ["one", "two", "three"] and 2.0 <= out[0]["start"] < out[-1]["end"] <= 4.0


def test_captions_dont_flash_or_blink_off_in_short_pauses():
    words = [{"word": w, "start": i * 0.2, "end": i * 0.2 + 0.15} for i, w in enumerate("so this is a cherimoya".split())]
    words[-1].update(start=1.6, end=2.1)                         # a short pause before the last word
    chunks = timeline.caption_chunks(words, 3)
    assert all(c["end"] - c["start"] >= 0.5 for c in chunks)
    assert chunks[0]["end"] == chunks[1]["start"]               # held through the pause, no blank frame


def _fake(reply):
    def create(**k):
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(reply))],
                               usage=SimpleNamespace(input_tokens=10, output_tokens=10))
    return SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create)))


def _script():
    return {"hooks": ["Introducing the cherimoya!", "Other hook", "Third hook"],
            "lines": [{"text": "Introducing the cherimoya!", "kind": "narrator", "shot_type": "a", "short": True},
                      {"text": "TEXT BEAT", "kind": "text", "shot_type": "b", "short": False},
                      {"text": "Experience its luscious sweetness today.", "kind": "narrator", "shot_type": "c", "short": True}]}


def test_the_read_aloud_pass_rewrites_spoken_lines_and_keeps_hook_one_as_line_one(monkeypatch):
    monkeypatch.setattr("creative_intelligence.adstudio.costs.log_tokens", lambda *a, **k: None)
    s = styles.speak(None, _fake({"hooks": ["x", "Other hook", "Third hook"],
                                  "lines": ["This is a cherimoya.", "It's soft. Like custard."]}), _script(), {"name": "C"})
    assert [ln["text"] for ln in s["lines"]] == ["This is a cherimoya.", "TEXT BEAT", "It's soft. Like custard."]
    assert s["hooks"][0] == "This is a cherimoya." and [ln["shot_type"] for ln in s["lines"]] == ["a", "b", "c"]


def test_the_read_aloud_pass_keeps_the_draft_when_the_answer_is_off(monkeypatch):
    monkeypatch.setattr("creative_intelligence.adstudio.costs.log_tokens", lambda *a, **k: None)
    draft = _script()
    assert styles.speak(None, _fake({"hooks": ["a"], "lines": ["only one"]}), draft, {"name": "C"}) == draft
    long = {"hooks": draft["hooks"], "lines": ["This is a cherimoya and it is a very long line now indeed", "Ok."]}
    assert styles.speak(None, _fake(long), draft, {"name": "C"})["lines"][0]["text"] == "Introducing the cherimoya!"


def test_hard_cuts_where_a_face_or_clay_is_on_screen_and_the_presenter_note():
    for k in ("talking_head", "presenter_table", "claymation", "fruit_characters"):
        assert styles.STYLE_LOOKS[k]["transition"] == 0.0
    assert video_qa.note_for("presenter_table") == video_qa.ON_CAMERA_NOTE
    assert video_qa.note_for("claymation") == video_qa.STYLIZED_NOTE and video_qa.note_for("pov") == ""


def test_captions_and_the_hook_stay_off_the_character_and_the_clay():
    assert {"fruit_characters", "claymation", "talking_head"} <= set(styles.CAPTIONS_TOP)
    assert timeline.hook_place({"label": "presenter", "character": True, "box": [0.1, 0.1, 0.9, 0.95]}) == "top"
    assert timeline.hook_place({"label": "presenter"}) == "low"            # under a presenter's face, as before


def test_a_throttled_voice_line_waits_and_tries_again(monkeypatch):
    from creative_intelligence.adstudio import voices
    from creative_intelligence.providers import replicate as rp
    monkeypatch.setattr(rp, "_cool_down", lambda exc, n: 0.0)
    calls = []

    class C:
        def run(self, model, input):
            calls.append(model)
            if len(calls) < 3:
                raise RuntimeError("status: 429 Request was throttled")
            return "https://x/voice.mp3"
    assert voices._run(C(), "m", {"text": "hi"}) == "https://x/voice.mp3" and len(calls) == 3


def test_the_ripening_lines_share_one_take_in_order():
    lines = [{"shot_type": "box_opening"}] + [{"shot_type": "ripening_transition"}] * 3 + [{"shot_type": "knife_cut"}]
    assert [styles._run_of(lines, i) for i in (1, 2, 3)] == [(0, 3), (1, 3), (2, 3)]
    assert styles._run_of(lines, 0) == (0, 1)


def test_a_swapped_shot_gets_words_that_fit_it(monkeypatch):
    monkeypatch.setattr("creative_intelligence.adstudio.costs.log_tokens", lambda *a, **k: None)
    s = {"hooks": ["Look at this.", "b", "c"],
         "lines": [{"text": "Look at this.", "kind": "presenter", "shot_type": "whole_fruit_hero", "short": True},
                   {"text": "Watch me cut it.", "kind": "presenter", "shot_type": "knife_cut", "short": False}]}
    out = styles.fit_shots(None, s, {"name": "C"}, {1: "cross_section_macro"},
                           client=_fake({"lines": ["See inside? White and soft."]}))
    assert out["lines"][1] == {**s["lines"][1], "text": "See inside? White and soft.", "shot_type": "cross_section_macro"}
    assert out["lines"][0] == s["lines"][0]


def test_the_talking_head_cutout_is_laid_on_pure_green(tmp_path):
    from PIL import Image
    from creative_intelligence.adstudio import talking
    im = Image.new("RGBA", (10, 10), (0, 0, 0, 0))
    im.putpixel((5, 5), (200, 100, 50, 255))
    im.save(tmp_path / "a.png")
    out = Image.open(talking.on_green(tmp_path / "a.png", tmp_path / "g.png")).convert("RGB")
    assert out.getpixel((0, 0)) == (0, 255, 0) and out.getpixel((5, 5)) == (200, 100, 50)


def test_scripts_are_told_what_other_videos_already_say_and_take_their_own_angle():
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE adstudio_videos (id INTEGER PRIMARY KEY, product_id INT, source_kind TEXT, copy_json TEXT)")
    conn.execute("CREATE TABLE video_batches (id INTEGER PRIMARY KEY, items_json TEXT)")
    quote = {"lines": [{"text": "Mark Twain called it the best.", "kind": "narrator"}, {"text": "Day 1", "kind": "text"}]}
    conn.execute("INSERT INTO adstudio_videos (product_id, source_kind, copy_json) VALUES (7, 'styled', ?)",
                 (json.dumps(quote),))
    conn.execute("INSERT INTO video_batches (items_json) VALUES (?)",
                 (json.dumps([{"product_id": 7, "script": {"lines": [{"text": "It melts.", "kind": "presenter"}]}},
                              {"product_id": 8, "script": {"lines": [{"text": "Other fruit.", "kind": "narrator"}]}}]),))
    assert styles.used_lines(conn, 7) == ["It melts.", "Mark Twain called it the best."]   # this batch first (v56)
    assert len(set(styles.ANGLES)) == len(styles.ANGLES) >= 6


def test_the_new_shot_recipes_have_no_hands_or_slicing_on_camera():
    from creative_intelligence import prompts
    cards = prompts.shot_cards()
    assert "no hands" in cards["knife_cut"]["motion"]["change"] and "rigid" in cards["knife_cut"]["motion"]["change"]
    assert "nothing in the box moves" in cards["packing"]["motion"]["change"]
    assert "rigid" in cards["clay_cut"]["motion"]["change"]
    assert "shrivelled" in cards["ripening_transition"]["timelapse_stages"][-1]
    assert styles.STYLE_LOOKS["ripening_timelapse"]["transition"] == 0.0
