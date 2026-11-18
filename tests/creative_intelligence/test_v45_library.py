"""V45 (2026-11-02): "make the shot library with 2-3 more shots of each type"; a shot type whose recipe keeps failing
QA isn't paid for again; videos rotate through the passing takes; scripts stay at 30 s and sound like a person."""
from __future__ import annotations

from creative_intelligence import prompts
from creative_intelligence.adstudio import shots, styles as S
from tests.creative_intelligence.test_s2a_video_styles import _shot, conn  # noqa: F401  (fixture)

T = "cross_section_macro"


def _failed(conn, shot_type=T):
    v = shots.card(shot_type)["version"]
    with conn:
        vid = conn.execute("INSERT INTO adstudio_videos (product_id, source_kind, source_id, style, shot_type, status,"
                           " card_version, created_at) VALUES (1, 'card', 0, 'shot', ?, 'done', ?, 'now')",
                           (shot_type, v)).lastrowid
        conn.execute("INSERT INTO qa_scores (source, source_id, rubric_version, scores_json, total, passed, failures_json,"
                     " reasoning, error, model, cost_usd, at) VALUES ('video', ?, 5, '{}', 2.5, 0, '[]', '', '', 'm', 0, 'now')",
                     (vid,))


def test_a_type_that_failed_twice_is_not_made_again(conn):
    _failed(conn)
    assert not shots.keeps_failing(conn, 1, T)
    _failed(conn)
    assert shots.keeps_failing(conn, 1, T)
    _shot(conn, 1, T, __import__("pathlib").Path("/tmp"))
    assert not shots.keeps_failing(conn, 1, T)                    # one passing take: it works for this product


def test_fill_plan_tops_every_type_up_and_skips_failing_ones(conn, tmp_path, monkeypatch):
    monkeypatch.setattr("creative_intelligence.webapp.common._product_dict",
                        lambda c, pid: {"id": 1, "name": "Honey Mango", "product_type": "fruit"})
    _shot(conn, 1, T, tmp_path)
    _failed(conn, "knife_cut")
    _failed(conn, "knife_cut")
    plan = shots.fill_plan(conn, 1, 3)
    made = {x["type"]: x for x in plan["make"]}
    assert made[T]["have"] == 1 and made[T]["make"] == 2
    assert "knife_cut" not in made and [x["type"] for x in plan["skipped"]] == ["knife_cut"]
    cards = prompts.shot_cards()
    assert all(not cards[t].get("character") for t in made)
    assert plan["usd"] > 0 and plan["takes"] == sum(x["make"] for x in plan["make"])


def test_fill_queues_extra_takes_from_new_stills(conn, tmp_path, monkeypatch):
    monkeypatch.setattr("creative_intelligence.webapp.common._product_dict",
                        lambda c, pid: {"id": 1, "name": "Honey Mango", "product_type": "fruit"})
    monkeypatch.setattr("creative_intelligence.budget.check", lambda *a, **k: None)
    monkeypatch.setattr("creative_intelligence.budget.caps", lambda c: {"video_job": 1000})
    monkeypatch.setattr("creative_intelligence.jobs.enqueue", lambda *a, **k: None)
    _shot(conn, 1, T, tmp_path)
    vid, why, reused = shots.create(conn, 1, T, extra=True)
    assert vid and not reused, why
    v = conn.execute("SELECT status, source_kind FROM adstudio_videos WHERE id=?", (vid,)).fetchone()
    assert v["status"] == "queued" and v["source_kind"] == "card"          # its own new still
    vid2, _, _ = shots.create(conn, 1, T, extra=True)
    assert vid2 != vid                                                     # more than one at once


def test_videos_rotate_through_passing_takes(conn, tmp_path):
    a = _shot(conn, 1, T, tmp_path)
    b = _shot(conn, 1, T, tmp_path)
    first = shots.passing(conn, 1, T)["id"]
    shots.use(conn, first)
    assert shots.passing(conn, 1, T)["id"] == ({a, b} - {first}).pop()


def test_fruit_character_scripts_fit_30_seconds():
    assert S.word_budget(30, "fruit_characters", None) == S.word_budget(30, "talking_head", None) < 70
    assert "Sound like a person" in prompts.text("story-script-system")


def test_claymation_gets_a_take_per_line_so_no_clip_repeats(conn, tmp_path, monkeypatch):
    """2026-11-02: 7 Claymation lines on 3 clay shots played the same clips again and again."""
    monkeypatch.setattr("creative_intelligence.webapp.common._product_dict",
                        lambda c, pid: {"id": 1, "name": "Honey Mango", "product_type": "fruit"})
    monkeypatch.setattr("creative_intelligence.budget.check", lambda *a, **k: None)
    monkeypatch.setattr("creative_intelligence.budget.caps", lambda c: {"video_job": 1000})
    monkeypatch.setattr("creative_intelligence.jobs.enqueue", lambda *a, **k: None)
    _shot(conn, 1, "clay_hero", tmp_path)
    _shot(conn, 1, "clay_reveal", tmp_path)
    s = {"style": "claymation", "lines": [{"shot_type": t} for t in
                                         ("clay_reveal", "clay_hero", "clay_hero", "clay_reveal", "clay_hero", "clay_cut")]}
    need = S.repeat_takes(conn, 1, s)
    assert need == {"clay_hero": 2, "clay_reveal": 1, "clay_cut": 1}
    ids = S.make_repeat_takes(conn, 1, s)
    assert len(ids) == 4 and S.repeat_takes(conn, 1, s) == {}            # what is being made counts
    assert S.repeat_takes(conn, 1, {**s, "style": "fruit_story"}) == {}    # only the formats made of their own shots


def test_a_repeated_type_plays_another_take(conn, tmp_path):
    a = _shot(conn, 1, "clay_hero", tmp_path)
    b = _shot(conn, 1, "clay_hero", tmp_path)
    takes = [t["id"] for t in shots.passing_takes(conn, 1, "clay_hero")]
    assert set(takes) == {a, b}


def test_fill_includes_clay(conn, monkeypatch):
    monkeypatch.setattr("creative_intelligence.webapp.common._product_dict",
                        lambda c, pid: {"id": 1, "name": "Honey Mango", "product_type": "fruit"})
    made = {x["type"] for x in shots.fill_plan(conn, 1, 3)["make"]}
    assert {"clay_hero", "clay_reveal", "clay_cut"} <= made and "character_scene" not in made
