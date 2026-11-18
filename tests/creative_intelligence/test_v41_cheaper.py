"""V41 (feedback 2026-11-02, "how can we make these cheaper?"): a shot's still is checked before a clip is paid for,
presenter clips use Kling Avatar, presenter scripts are written to fit 30 s, and QA scores and checks run on Sonnet."""
from __future__ import annotations

import json

from creative_intelligence import config
from creative_intelligence.adstudio import animate, qa, shots, styles as S, talking, textcheck, timeline, video_qa
from creative_intelligence.db import get_connection
from tests.creative_intelligence.test_s2a_video_styles import SCRIPT, _Claude, _shot, conn  # noqa: F401  (fixture)


def _queued_shot(conn, shot_type="cross_section_macro"):
    with conn:
        return conn.execute("INSERT INTO adstudio_videos (product_id, source_kind, source_id, style, shot_type, status,"
                            " created_at) VALUES (1, 'card', 0, 'shot', ?, 'queued', 'now')", (shot_type,)).lastrowid


def _fakes(monkeypatch, tmp_path, verdicts):
    made, clips = [], []

    def still(conn, video, product, c, refs, tag=""):
        p = tmp_path / f"still{tag or '_1'}.png"
        p.write_bytes(b"x")
        made.append(str(p))
        return str(p)

    checks = iter(verdicts)
    monkeypatch.setattr(shots, "_generated_still", still)
    monkeypatch.setattr(shots, "check_still", lambda conn, vid, s, refs, product: next(checks))
    monkeypatch.setattr(shots, "approved_still", lambda *a, **k: None)
    monkeypatch.setattr("creative_intelligence.webapp.common._primary_photo_uris", lambda *a, **k: [])
    monkeypatch.setattr("creative_intelligence.webapp.common._product_dict", lambda *a, **k: {"id": 1})
    monkeypatch.setattr(animate, "make_clip", lambda conn, video, s, *a, **k: clips.append(s) or str(tmp_path / "c.mp4"))
    monkeypatch.setattr(video_qa, "score", lambda *a, **k: {"total": 4.5, "passed": True})
    monkeypatch.setattr("creative_intelligence.budget.spent_on_video", lambda *a, **k: 0.0)
    return made, clips


def test_a_bad_still_is_made_again_before_any_clip(conn, tmp_path, monkeypatch):
    made, clips = _fakes(monkeypatch, tmp_path, [(False, 8), (True, 13)])
    vid = _queued_shot(conn)
    db = lambda: get_connection(tmp_path / "data" / "t.db")
    assert shots.process(vid, db)["passed"] is True
    assert len(made) == 2 and clips == [made[1]]            # one clip, from the still that passed
    assert made[0] != made[1]                               # each try is its own file


def test_after_three_bad_stills_the_best_one_is_animated(conn, tmp_path, monkeypatch):
    made, clips = _fakes(monkeypatch, tmp_path, [(False, 9), (False, 11), (False, 7)])
    vid = _queued_shot(conn)
    shots.process(vid, lambda: get_connection(tmp_path / "data" / "t.db"))
    assert len(made) == shots.STILL_TRIES and clips == [made[1]]


def test_the_still_check_scores_only_what_a_still_shows(conn, monkeypatch):
    seen = {}

    def ask(image, refs, product, rubric, source, client=None):
        seen["keys"] = [c["key"] for c in rubric["criteria"]]
        return {"scores": {"fruit_accuracy": 4, "artefacts": 3, "never_include": 5}}, None

    monkeypatch.setattr(qa, "ask_claude", ask)
    monkeypatch.setattr(qa, "_log_cost", lambda *a, **k: 0.0)
    ok, total = shots.check_still(conn, 1, "s.png", [], {})
    assert set(seen["keys"]) == set(shots.STILL_CHECKS) and not ok and total == 12    # artefacts 3 is below 4

    def broken(*a, **k):
        raise RuntimeError("down")
    monkeypatch.setattr(qa, "ask_claude", broken)
    assert shots.check_still(conn, 1, "s.png", [], {})[0] is True                      # never blocks the shot


def test_presenter_clips_use_kling_avatar_and_characters_keep_omnihuman():
    assert S.DEFAULT_TALKING == "kling_avatar" and talking.FALLBACK == "kling_avatar"
    assert S.one_voice("veo") == "kling_avatar"
    assert talking.METHODS[talking.LIPSYNC]["model"] == "bytedance/omni-human-1.5"


def test_presenter_scripts_get_fewer_words():
    assert S.word_budget(30, "talking_head", None) == round((30 - timeline.END_CARD_S) * S.PRESENTER_WORDS_PER_S)
    assert S.word_budget(30, S.TABLE, None) < S.word_budget(30, "sensory", None)
    assert S.word_budget(30, "fruit_story", "presenter") == S.word_budget(30, "talking_head", None)


def test_a_long_script_is_trimmed_once(conn, tmp_path):
    _shot(conn, 1, "cross_section_macro", tmp_path)
    long = json.loads(json.dumps(SCRIPT))
    long["lines"][1]["text"] = " ".join(["juicy"] * 120)
    fake = _Claude(long)
    S.write(conn, {"id": 1, "name": "Rosy Sunshine Mango", "product_type": "fruit"}, "kinetic", client=fake)
    asks = [m["messages"][0]["content"] for m in fake.sent if isinstance(m["messages"][0]["content"], str)]
    assert sum("so it runs long" in a for a in asks) == 1


def test_scores_and_checks_run_on_the_cheaper_model():
    assert config.CI_CLAUDE_SCORE_MODEL == "claude-sonnet-5-5"
    assert qa.MODEL == textcheck.MODEL == config.CI_CLAUDE_SCORE_MODEL
    assert S.MODEL == config.CI_CLAUDE_CHECK_MODEL                 # scripts stay on the stronger model


def test_talking_head_presenter_is_cut_out_in_the_bottom_left_corner(tmp_path):
    """Feedback 2026-11-02 (with an example): the presenter cut out from the green, head and shoulders flush with
    the bottom-left corner, no circle."""
    import subprocess

    from PIL import Image, ImageDraw
    from creative_intelligence.adstudio import videotools
    face = Image.new("RGB", (720, 1280), (0, 255, 0))
    ImageDraw.Draw(face).rectangle([140, 300, 580, 1280], fill=(60, 90, 140))
    face.save(tmp_path / "face.png")
    Image.new("RGB", (1080, 1920), (240, 130, 110)).save(tmp_path / "shot.png")
    for src, out in (("face.png", "pres.mp4"), ("shot.png", "shot.mp4")):
        videotools.run(["-loop", "1", "-i", str(tmp_path / src), "-t", "2", "-pix_fmt", "yuv420p", "-r", "25",
                        str(tmp_path / out)])
    measured: dict = {}
    seg = {"src": str(tmp_path / "shot.mp4"), "dur": 2.0, "pip": str(tmp_path / "pres.mp4"), "pip_cut": True}
    out = timeline._segment(seg, "9:16", 25, tmp_path / "seg.mp4", tmp_path, 0, measured)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", "1", "-i", str(out), "-frames:v", "1",
                    str(tmp_path / "f.png")], check=True)
    f = Image.open(tmp_path / "f.png").convert("RGB")
    x, y, w, h = timeline.cut_box(1080, 1920)
    assert x == 0 and y + h == 1920 and measured["boxes"][-1][-1] == "presenter"
    r, g, b = f.getpixel((5, y + 5))                        # the green beside the shirt is gone: the shot shows
    assert r > 200 and g < 170
    r, g, b = f.getpixel((w // 2, 1900))                    # the shirt sits on the bottom edge
    assert b > r and b > 110


def test_every_video_shows_in_the_gallery_qa_rejected_too():
    import inspect

    from creative_intelligence.webapp.routes import gallery
    assert 'or i["kind"] == "video"' in inspect.getsource(gallery.adstudio_gallery)


def test_presenter_at_the_table_makes_no_shots():
    """Feedback 2026-11-02: "the presenter should be only the presenter, not the other scenes"."""
    from creative_intelligence.adstudio import video_batch
    s = {"style": S.TABLE, "lines": [{"text": "Hi.", "kind": "presenter", "shot_type": "cross_section_macro"}],
         "hook_shots": ["whole_fruit_hero"]}
    assert S.new_shots(None, 1, s) == set() and video_batch.own_shots(s, "b1.0") == {}


def test_each_character_line_moves_its_own_way(conn, tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(S, "character_face", lambda conn, vid, vdir, n=1: f"face{n % len(S.FACE_AT)}.png")
    monkeypatch.setattr(talking, "lipsync", lambda conn, face, audio, name, vid, prompt: sent.append((face, prompt)) or "c.mp4")
    monkeypatch.setattr(S.voices, "speak", lambda *a, **k: (str(tmp_path / "a.wav"), 1.0))
    monkeypatch.setattr(S, "_wav", lambda src, out: out)
    monkeypatch.setattr(S.videotools, "probe_audio", lambda p: 1.0)
    for text in ("Hello there.", "I am so ripe.", "Cut me open!", "Order now, friend."):
        S.make_piece(conn, 1, {"kind": "narrator", "text": text}, {"voice": "June", "character": {"id": 1}}, tmp_path)
    assert len({p for _, p in sent}) > 1 and len({f for f, _ in sent}) > 1
    assert all("mouth clearly forming every word" in p for _, p in sent)


def test_a_hung_replicate_call_times_out(monkeypatch):
    import time

    import pytest
    from creative_intelligence.providers import replicate as R

    class Slow:
        def run(self, model, input):
            time.sleep(2)
    with pytest.raises(TimeoutError, match="timed out"):
        R._run_limited(Slow(), "m/x", {}, 0.2)


def test_a_waiting_video_shows_what_its_format_really_takes(conn):
    from creative_intelligence.webapp.routes import gallery
    with conn:
        for mins in (20, 24, 30):
            conn.execute("INSERT INTO adstudio_videos (product_id, source_kind, source_id, style, status, created_at,"
                         " finished_at) VALUES (1, 'styled', 0, 'talking_head', 'done', datetime('now', ?),"
                         " datetime('now'))", (f"-{mins} minutes",))
    assert gallery.typical_minutes(conn)["talking_head"] == 24
