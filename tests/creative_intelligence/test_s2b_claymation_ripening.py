"""S2b: Claymation (clay shot cards with a stop-motion frame rate, the clay fruit sliced open to the real one) and the
Ripening time-lapse (a locked-off ripening shot whose still and words follow the approved ripeness facts)."""
from __future__ import annotations

from pathlib import Path

import pytest

from creative_intelligence import prompts
from creative_intelligence.adstudio import shots, styles as S, timeline, videotools
from tests.creative_intelligence.test_s2a_video_styles import _Claude, _shot, conn  # noqa: F401  (fixture)


def test_cards():
    cards = prompts.shot_cards()
    assert "clay_slice_reveal" not in cards                                          # one clay shot (2026-10-22)
    assert cards["clay_hero"]["stop_motion_fps"] == 12 and cards["clay_hero"]["cast"] is True
    assert cards["ripening_transition"]["facts_category"] == "ripeness" and not cards["ripening_transition"]["strict_qa"]   # changes colour on purpose (v3)


def test_style_only_cards_are_cast_only_by_their_style(conn, tmp_path):
    for t in ("cross_section_macro", "clay_hero"):
        _shot(conn, 1, t, tmp_path)
    clay = [m["type"] for m in S.menu_for(conn, 1, "claymation")]
    assert {"clay_hero", "cross_section_macro"} <= set(clay)
    assert "clay_hero" not in {m["type"] for m in S.menu_for(conn, 1, "fruit_story")}


def test_claymation_needs_its_clay_shot(conn, tmp_path):
    p = {"id": 1, "name": "Honey Mango", "product_type": "fruit"}
    _shot(conn, 1, "cross_section_macro", tmp_path)
    with pytest.raises(ValueError, match="shot in Video › Shots first"):
        S.write(conn, p, "claymation", client=_Claude())
    for t in ("clay_hero", "clay_cut", "clay_reveal"):
        _shot(conn, 1, t, tmp_path)
    fake = _Claude()
    s = S.write(conn, p, "claymation", client=fake)
    assert "CLAYMATION" in fake.sent[0]["messages"][0]["content"] and "clay_hero" in fake.sent[0]["messages"][0]["content"]
    # feedback 2026-10-26: "full clay, not turn into real fruit": clay on every line, the last too
    assert all(ln["shot_type"] in ("clay_hero", "clay_cut", "clay_reveal") for ln in s["lines"])


def test_a_swap_never_brings_in_a_clay_shot(conn, tmp_path):
    """A Fruit story showed the clay fruit (2026-10-26): a missing shot is swapped only for a shot any style may use."""
    from creative_intelligence.adstudio import story
    _shot(conn, 1, "clay_hero", tmp_path)
    assert story._shot_for(conn, 1, "knife_cut", set()) is None
    _shot(conn, 1, "cross_section_macro", tmp_path)
    assert story._shot_for(conn, 1, "knife_cut", set())["shot_type"] == "cross_section_macro"


def test_one_voice_per_video():
    """Veo invents a new voice per clip (the presenter sang, then sounded Australian): styled videos use the house voice."""
    assert S.one_voice("veo") == "kling_avatar" and S.one_voice("omnihuman") == "omnihuman"
    assert S.DEFAULT_TALKING == "kling_avatar"


def test_ripening_follows_the_approved_guide(conn, tmp_path):
    p = {"id": 1, "name": "Honey Mango", "product_type": "fruit"}
    _shot(conn, 1, "ripening_transition", tmp_path)
    with pytest.raises(ValueError, match="Ripeness"):
        S.write(conn, p, "ripening_timelapse", client=_Claude())
    with conn:
        conn.execute("INSERT INTO facts (product_id, category, text, status) VALUES (1, 'ripeness',"
                     " 'Arrives firm; ready to eat in 2 to 3 days at room temperature', 'approved')")
    s = S.write(conn, p, "ripening_timelapse", client=_Claude())
    assert all(ln["kind"] == "text" for ln in s["lines"])                              # no voice
    prompt = shots.still_prompt(p, prompts.shot_cards()["ripening_transition"], "Arrives firm; ready in 2 to 3 days")
    assert "APPROVED FACTS TO MATCH EXACTLY: Arrives firm" in prompt


def test_stop_motion_segments_hold_frames(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(videotools, "frame", lambda src, out, aspect, box: (Path(out), {}))
    monkeypatch.setattr(videotools, "probe", lambda p: {"duration": 5.0, "width": 1080, "height": 1920, "fps": 24})
    monkeypatch.setattr(videotools, "run", lambda args, timeout=600: calls.append(args) or "")
    timeline._segment({"src": "clip.mp4", "dur": 3.0, "stop_motion_fps": 12}, "9:16", 30, tmp_path / "s.mp4", tmp_path, 0)
    vf = calls[-1][calls[-1].index("-vf") + 1]
    assert "fps=12,fps=30" in vf
    timeline._segment({"src": "clip.mp4", "dur": 3.0}, "9:16", 30, tmp_path / "s.mp4", tmp_path, 1)
    assert "fps=12" not in calls[-1][calls[-1].index("-vf") + 1]


def test_a_price_variant_with_no_row_costs_what_the_plain_model_costs(conn):
    """Seedance with sound ('bytedance/seedance-2.0:audio') had no price and was logged as $0 (2026-10-26)."""
    from creative_intelligence.adstudio.costs import price
    base = price(conn, "bytedance/seedance-2.0")
    assert base > 0 and price(conn, "bytedance/seedance-2.0:audio") == base
    assert price(conn, "google/veo-3.1-fast:audio") > price(conn, "google/veo-3.1-fast")   # its own row still wins
    assert price(conn, "kwaivgi/kling-v3-video:pro:audio") == price(conn, "kwaivgi/kling-v3-video:pro")
    assert price(conn, "no/such-model") == 0.0


def test_ripening_is_a_real_time_lapse_from_stills(conn, tmp_path):
    """Feedback 2026-10-26: "ripening time-lapse should be an actual ripening time-lapse". The card makes the first
    still plus one edited still per stage and blends them (no video model), and prices it that way."""
    from creative_intelligence.adstudio import costs
    from creative_intelligence.providers import model
    c = prompts.shot_cards()["ripening_transition"]
    assert len(c["timelapse_stages"]) == 3
    assert shots.estimate(conn, c, True) == round(costs.price(conn, model("image")) * 4 + shots.animate.QA_ESTIMATE_USD, 2)
    from PIL import Image
    stills = []
    for i, col in enumerate([(60, 140, 60), (140, 160, 60), (230, 120, 40)]):
        p = tmp_path / f"s{i}.png"
        Image.new("RGB", (720, 1280), col).save(p)
        stills.append(str(p))
    out = shots.timelapse_clip(stills, tmp_path / "t.mp4")
    assert videotools.probe(out)["duration"] > 3


def test_fast_cut_joins_with_hard_cuts(tmp_path):
    clips = [videotools.test_clip(tmp_path / f"c{i}.mp4", size="360x640", seconds=1) for i in range(3)]
    tl = {"aspect": "9:16", "fps": 30, "transition": 0.0, "picture": [{"src": str(c), "dur": 1.0} for c in clips],
          "text": [{"template": "label", "headline": "Gold inside", "start": 0, "end": 1}]}
    res = timeline.render(tl, tmp_path / "fast.mp4")
    assert abs(res["duration"] - 3.0) < 0.2


def test_a_stand_in_shows_what_the_line_says(conn, tmp_path):
    """Live test 2026-10-27: "Slice it open" played over a tree because the knife cut hadn't passed QA."""
    from creative_intelligence.adstudio import story
    for t in ("fruit_on_tree", "cross_section_macro", "box_opening"):
        _shot(conn, 1, t, tmp_path)
    assert story._shot_for(conn, 1, "knife_cut", set())["shot_type"] == "cross_section_macro"
    assert story._shot_for(conn, 1, "hand_picking", set())["shot_type"] == "fruit_on_tree"
    assert story._shot_for(conn, 1, "packing", set())["shot_type"] == "box_opening"


def test_a_long_stage_wraps_inside_the_frame(tmp_path):
    """Live test 2026-10-27: "It gives slightly to a gentle squeeze." ran off the edge of the chip."""
    m: dict = {}
    timeline.day_png("It gives slightly to a gentle squeeze, then it is ready to eat.", 1080, 1920, "9:16",
                     tmp_path / "d.png", m)
    from PIL import Image
    alpha = Image.open(tmp_path / "d.png").getchannel("A")
    assert alpha.getbbox()[2] <= 1080 - 0.06 * 1080 + 1      # nothing drawn past the right edge of the safe zone
