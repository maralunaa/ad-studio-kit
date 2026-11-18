"""S2a: four new video styles on the styled-video pipeline. Us vs supermarket (a real split screen with its own
supermarket-fruit shot card), How to eat it (steps only from approved facts; presenter or hands only), Fast-cut
montage (text over library shots, music), and the Listicle video (one fruit per line from Product Hub data, no
Claude call). Claude is faked; ffmpeg tests run where ffmpeg exists (CI)."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from creative_intelligence import prompts
from creative_intelligence.adstudio import qa, shots, styles as S, timeline, videotools
from creative_intelligence.db import get_connection, init_db

SCRIPT = {"title": "t", "hook_headline": "Pale vs gold", "cta": "Shop now", "hooks": ["Pale vs gold."],
          "lines": [{"text": "Pale vs gold.", "kind": "presenter", "shot_type": "cross_section_macro", "short": True,
                     "compare": True},
                    {"text": "Cut it open.", "kind": "narrator", "shot_type": "cross_section_macro", "short": True}]}


class _Claude:
    def __init__(self, body=SCRIPT):
        self.body, self.sent = body, []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.sent.append(kw)
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(self.body))],
                               usage=SimpleNamespace(input_tokens=1, output_tokens=1, cache_read_input_tokens=0))


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("CI_DB_PATH", str(tmp_path / "data" / "t.db"))
    init_db(tmp_path / "data" / "t.db")
    c = get_connection(tmp_path / "data" / "t.db")
    with c:
        for i, (n, notes) in enumerate([("Rosy Sunshine Mango", "custardy, zero fibre"), ("Lychee", "floral, juicy"),
                                        ("Mangosteen", "sweet-tart"), ("Rambutan", "")], 1):
            c.execute("INSERT INTO adstudio_products (id, name, product_type, scent_notes) VALUES (?,?, 'fruit', ?)",
                      (i, n, notes))
    monkeypatch.setattr("creative_intelligence.adstudio.costs.log_tokens", lambda *a, **k: 0)
    monkeypatch.setattr("creative_intelligence.adstudio.facts.check_copy",
                        lambda *a, **k: {"status": "ok", "unlisted": [], "banned": []})
    yield c
    c.close()


def _shot(conn, pid, shot_type, tmp_path):
    clip = tmp_path / f"{pid}_{shot_type}.mp4"
    clip.write_bytes(b"x")
    with conn:
        vid = conn.execute("INSERT INTO adstudio_videos (product_id, source_kind, source_id, style, shot_type, status,"
                           " clip_path, created_at) VALUES (?, 'card', 0, 'shot', ?, 'done', ?, 'now')",
                           (pid, shot_type, str(clip))).lastrowid
        conn.execute("INSERT INTO qa_scores (source, source_id, rubric_version, scores_json, total, passed, failures_json,"
                     " reasoning, error, model, cost_usd, at) VALUES ('video', ?, 4, '{}', 4.5, 1, '[]', '', '', 'm', 0, 'now')",
                     (vid,))
    return vid


def test_new_styles_are_presets():
    cat = S.catalog()
    assert cat["how_to_eat"]["modes"] == ["presenter", "hands"] and cat["how_to_eat"]["requires_facts"] == ["how_to_eat"]
    assert cat["fast_cut_montage"]["voice"] is False and "listicle_video" not in cat              # removed 2026-10-22
    assert S.reserved_shots()["supermarket_fruit"] == ["us_vs_supermarket"]
    assert S.label("how_to_eat", "hands") == "How to eat it / FAQ · hands only"


def test_supermarket_card_is_scored_without_fruit_accuracy():
    c = prompts.shot_cards()["supermarket_fruit"]
    assert c["qa_skip"] == ["fruit_accuracy"] and "deliberately" in c["qa_note"] and c["styles"] == ["us_vs_supermarket"]


@pytest.mark.skip(reason="Us vs supermarket retired 2026-10-30")
def test_us_vs_supermarket_needs_its_comparison_shot_and_hides_it_from_other_styles(conn, tmp_path):
    p = {"id": 1, "name": "Rosy Sunshine Mango", "product_type": "fruit"}
    _shot(conn, 1, "cross_section_macro", tmp_path)
    with pytest.raises(ValueError, match="Supermarket fruit"):
        S.write(conn, p, "us_vs_supermarket", client=_Claude())
    _shot(conn, 1, "supermarket_fruit", tmp_path)
    assert "supermarket_fruit" not in [m["type"] for m in S.menu_for(conn, 1, "fruit_story")]
    s = S.write(conn, p, "us_vs_supermarket", client=_Claude())
    assert s["lines"][0]["compare"] is True and s["style"] == "us_vs_supermarket"


def test_how_to_eat_only_with_approved_steps_and_hands_mode_has_no_presenter(conn, tmp_path):
    p = {"id": 1, "name": "Rosy Sunshine Mango", "product_type": "fruit"}
    _shot(conn, 1, "cross_section_macro", tmp_path)
    with pytest.raises(ValueError, match="How to eat it"):
        S.write(conn, p, "how_to_eat", client=_Claude())
    with conn:
        conn.execute("INSERT INTO facts (product_id, category, text, status) VALUES (1, 'how_to_eat',"
                     " 'Cut around the stone, then score the cheeks', 'approved')")
    fake = _Claude()
    s = S.write(conn, p, "how_to_eat", client=fake)
    assert s["mode"] == "hands" and {ln["kind"] for ln in s["lines"]} == {"narrator"}
    assert "MODE: hands" in fake.sent[0]["messages"][0]["content"] and "HOW TO EAT IT" in fake.sent[0]["messages"][0]["content"]
    assert S.write(conn, p, "how_to_eat", client=_Claude(), mode="presenter")["lines"][0]["kind"] == "presenter"


def test_split_labels_sit_inside_the_safe_zone(tmp_path):
    for aspect, (W, H) in (("9:16", (1080, 1920)), ("4:5", (1080, 1350))):
        m: dict = {"frame": [W, H]}
        timeline.split_labels_png(["Supermarket", "Test Fruit Co"], W, H, aspect, tmp_path / "l.png", m)
        assert qa.outside_safe_zone(m) == [] and len(m["boxes"]) == 2 and m["min_contrast"] >= 4.5


def test_timeline_checks_the_split_clip(tmp_path):
    a = tmp_path / "a.mp4"
    a.write_bytes(b"x")
    errs = timeline.validate({"aspect": "9:16", "picture": [{"src": str(a), "dur": 2, "split": str(tmp_path / "nope.mp4")}]})
    assert "picture 1: split clip missing" in errs


def _ffmpeg() -> bool:
    try:
        videotools.ffmpeg_exe()
        return True
    except videotools.VideoToolError:
        return False


@pytest.mark.skipif(not _ffmpeg(), reason="needs ffmpeg")
def test_split_segment_stacks_both_clips(tmp_path, monkeypatch):
    monkeypatch.setenv("VIDEO_X264_PRESET", "ultrafast")
    top = videotools.test_clip(tmp_path / "top.mp4", size="720x1280", seconds=2, color="0xb0b0a0")
    bot = videotools.test_clip(tmp_path / "bot.mp4", size="720x1280", seconds=2, color="0xf4a340")
    m: dict = {"frame": [1080, 1920]}
    out = timeline._segment({"src": str(bot), "split": str(top), "split_labels": ["Supermarket", "Test Fruit Co"],
                             "dur": 2.0}, "9:16", 30, tmp_path / "seg.mp4", tmp_path, 0, m)
    p = videotools.probe(out)
    assert (p["width"], p["height"]) == (1080, 1920) and p["duration"] == pytest.approx(2.0, abs=0.15)
    assert len([b for b in m["boxes"] if b[4] == "split_label"]) == 2


def test_captions_use_the_script_words_with_the_heard_timings():
    """Kinetic test 2026-10-26: the transcript heard "Chill have Skip" for "Chill, halve, skip": captions show what the
    script says; the transcript only times it. A line that doesn't line up is spread over its own span."""
    from creative_intelligence.adstudio import styles
    lines = [{"kind": "text", "text": "Never tasted it?"}, {"kind": "narrator", "text": "Chill, halve, skip."},
             {"kind": "narrator", "text": "Spoon it out today"}]
    heard = [{"word": "Chill,", "start": 2.1, "end": 2.4}, {"word": "have,", "start": 2.5, "end": 2.8},
             {"word": "Skip.", "start": 2.9, "end": 3.2}, {"word": "Spoonitout", "start": 4.1, "end": 4.9}]
    w = styles.script_words(lines, [(0, 2), (2, 4), (4, 6)], heard)
    assert [x["word"] for x in w] == ["Chill,", "halve,", "skip.", "Spoon", "it", "out", "today"]
    assert w[1]["start"] == 2.5 and all(4 <= x["start"] < 6 for x in w[3:])


def test_kinetic_type_has_no_voice():
    from creative_intelligence.adstudio import styles
    assert styles.catalog()["kinetic"]["voice"] is False


def test_sensory_plays_only_on_its_tactile_shots(conn, tmp_path):
    """Feedback 2026-10-26: a sensory video looked like kinetic type; it should be fruit dripping, hands pulling the
    fruit apart. Every line lands on its own cards (or the juice macro), whatever the script asked for."""
    from creative_intelligence.adstudio import styles as S2
    p = {"id": 1, "name": "Rosy Sunshine Mango", "product_type": "fruit"}
    _shot(conn, 1, "cross_section_macro", tmp_path)
    s = S2.write(conn, p, "sensory", client=_Claude(), plan=True)
    # 2026-11-11: no juice shots ("syrup"), and hands can't pull a mango apart: the spoon and the knife instead
    tactile = {"spoon_scoop", "knife_cut", "cross_section_macro"}
    assert {ln["shot_type"] for ln in s["lines"]} <= tactile and all(ln["kind"] == "text" for ln in s["lines"])
    assert set(s["hook_shots"]) <= tactile and {"spoon_scoop", "knife_cut"} <= set(s["missing_shots"])
    assert not {"hand_tear", "juice_drip", "juice_texture_macro"} & set(s["missing_shots"])


def test_text_formats_each_have_their_own_look():
    """Feedback 2026-10-26: kinetic, sensory, fast-cut and ripening looked the same."""
    from creative_intelligence.adstudio import styles as S2, timeline
    looks = {k: v["text"] for k, v in S2.STYLE_LOOKS.items() if "text" in v}
    assert looks == {"fast_cut_montage": "label", "sensory": "whisper", "ripening_timelapse": "day"}
    assert set(looks.values()) <= set(timeline.TEXT_LOOKS) and S2.STYLE_LOOKS["fast_cut_montage"]["transition"] == 0
    # every voiced format has its own caption look, and every look exists
    caps = {k: v["captions"] for k, v in S2.STYLE_LOOKS.items() if "captions" in v}
    assert set(caps.values()) <= set(timeline.CAPTION_STYLES) and len(set(caps.values())) >= 6
    assert set(S2.catalog()) - {"kinetic"} <= set(S2.STYLE_LOOKS)


def test_scripts_are_told_the_openings_other_videos_already_use(conn, tmp_path):
    """Live test 2026-10-26: five formats opened with "You've never seen a mango this pink"."""
    import json as _json
    from creative_intelligence.adstudio import styles as S2
    with conn:
        conn.execute("INSERT INTO adstudio_videos (product_id, source_kind, source_id, style, copy_json, created_at)"
                     " VALUES (1, 'styled', 0, 'pov', ?, '')", (_json.dumps({"lines": [{"text": "A mango this pink?"}],
                                                                              "hooks": ["Pink outside, gold inside"]}),))
    assert S2.used_openings(conn, 1) == ["A mango this pink?", "Pink outside, gold inside"]
    _shot(conn, 1, "cross_section_macro", tmp_path)
    fake = _Claude()
    S2.write(conn, {"id": 1, "name": "Rosy Sunshine Mango", "product_type": "fruit"}, "fruit_story", client=fake)
    assert "A mango this pink?" in fake.sent[0]["messages"][0]["content"]


def test_music_changes_between_formats_and_generations(conn, monkeypatch):
    """Feedback 2026-10-26: "the music was the same on all of them"."""
    import json as _json
    from creative_intelligence.adstudio import story, styles as S2
    files = ["a.m4a", "b.m4a", "c.m4a", "power-on-instrumental.m4a", "slow-it-down-instrumental.m4a", "v.m4a"]
    monkeypatch.setattr(story, "music_catalog", lambda: [{"file": f, "vocals": f == "v.m4a"} for f in files])
    fast = S2.pick_track(conn, 1, "fast_cut_montage", False)
    sens = S2.pick_track(conn, 1, "sensory", False)
    assert fast == "power-on-instrumental.m4a" and sens == "slow-it-down-instrumental.m4a"
    with conn:                                       # the product's last video used it: the next one moves on
        conn.execute("INSERT INTO adstudio_videos (product_id, source_kind, source_id, style, copy_json, created_at)"
                     " VALUES (1, 'styled', 0, 'fast_cut_montage', ?, '')", (_json.dumps({"music_used": fast}),))
    assert S2.pick_track(conn, 1, "fast_cut_montage", False) != fast
    assert S2.pick_track(conn, 1, "fruit_story", True) != "v.m4a"           # a voice never gets a vocal track
