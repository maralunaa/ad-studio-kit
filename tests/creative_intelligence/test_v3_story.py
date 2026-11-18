"""Video Studio V3 — the timeline engine and storytelling videos: house voices, word-timed captions, crossfaded
shots from the library, ducked music, a code-drawn end card, 9:16 and 4:5; Claude's script uses approved facts
only. Replicate and Claude are faked; ffmpeg is real."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from creative_intelligence import config, jobs
from creative_intelligence.adstudio import qa, story, timeline as T, videotools as V, voices
from tests.creative_intelligence.test_v1_animate import FakeClaude, drain, env, png  # noqa: F401  (fixture)


@pytest.fixture(autouse=True)
def no_music(tmp_path, monkeypatch):
    """Tests don't see the real music/ folder unless they set MUSIC_DIR themselves."""
    monkeypatch.setattr(config, "MUSIC_DIR", str(tmp_path / "no-music"))


# ── voices ──────────────────────────────────────────────────────────────────
def test_house_voices_and_their_inputs():
    names = [v["name"] for v in voices.house()]
    assert names == ["Wade", "Jack", "Luis", "June", "Ivy", "Tess"]
    assert {v["gender"] for v in voices.house()} == {"man", "woman"}
    # persona voices picked by ear, 2026-11-04: Gemini TTS with a delivery direction, Jack stays on ElevenLabs
    assert voices.payload(voices.voice("tess"), "hi") == {"text": "hi", "voice": "Aoede", "output_format": "mp3",
                                                          "style": "relaxed, warm home cook chatting in her kitchen"}
    # June on ElevenLabs Sarah (2026-11-11: her Gemini voice didn't sound like her on screen)
    assert voices.payload(voices.voice("june"), "hi") == {"prompt": "hi", "voice": "Sarah", "stability": 0.4}
    assert voices.payload(voices.voice("Luis"), "hi") == {"prompt": "hi", "voice": "Mark", "stability": 0.4}
    assert voices.payload(voices.voice("Jack"), "hi") == {"text": "hi", "voice": "am_onyx"}   # his first voice back
    assert [voices.voice(n)["voice"] for n in ("Wade", "June", "Tess", "Jack", "Ivy")] == \
        ["Algenib", "Sarah", "Aoede", "am_onyx", "Sulafat"]
    with pytest.raises(ValueError):
        voices.voice("Nobody")


def test_rough_timings_cover_the_audio():
    w = voices.spread_words("This is the Rosy Sunshine mango.", 3.0)
    assert len(w) == 6 and w[0]["start"] > 0 and w[-1]["end"] <= 3.0
    assert all(a["end"] <= b["start"] for a, b in zip(w, w[1:]))


# ── timeline planning ───────────────────────────────────────────────────────
WORDS = [{"word": x, "start": i * 0.4, "end": i * 0.4 + 0.3} for i, x in enumerate(
    "This is the Rosy Sunshine mango. Cut it open, golden flesh. Shop now.".split())]


def test_lines_are_timed_from_the_words_and_each_gets_its_shot():
    lines = ["This is the Rosy Sunshine mango.", "Cut it open, golden flesh.", "Shop now."]
    spans = T.line_spans(lines, WORDS, 5.0)
    assert spans == [(0.0, 2.4), (2.4, 4.4), (4.4, 5.0)]
    pic = T.picture_plan(spans, [{"src": "a"}, {"src": "b"}, {"src": "c"}])
    assert [p["dur"] for p in pic] == [2.7, 2.3, 0.8]                    # + the crossfade, but never under 0.8 s
    tl = {"picture": pic, "transition": 0.3, "end_card": {"dur": 3}}
    assert T.duration(tl) == pytest.approx(2.7 + 2.3 + 0.8 - 0.6 + 3)


def test_captions_break_at_punctuation_and_a_few_words():
    chunks = T.caption_chunks(WORDS, max_words=3)
    assert [c["text"] for c in chunks] == ["This is the", "Rosy Sunshine mango.", "Cut it open,", "golden flesh.", "Shop now."]
    assert all(a["end"] <= b["start"] + 1e-9 or a["end"] - b["start"] < 0.51 for a, b in zip(chunks, chunks[1:]))


def test_validate_names_whats_missing(tmp_path):
    errs = T.validate({"aspect": "3:2", "picture": [{"src": str(tmp_path / "nope.mp4"), "dur": 0}],
                       "voice": {"src": "/nope.mp3"}})
    assert "unknown aspect" in errs and "picture 1: file missing" in errs and "voice file missing" in errs


def test_renders_every_track_in_both_sizes(tmp_path, monkeypatch):
    monkeypatch.setenv("VIDEO_X264_PRESET", "ultrafast")
    clips = [V.test_clip(tmp_path / f"c{i}.mp4", size="360x640", seconds=2, color=c) for i, c in enumerate(["0xd9708a", "0x70a0d9"])]
    V.run(["-f", "lavfi", "-i", "sine=frequency=220:duration=4", "-ac", "1", str(tmp_path / "vo.mp3")])
    V.run(["-f", "lavfi", "-i", "sine=frequency=660:duration=3", "-ac", "2", str(tmp_path / "m.mp3")])
    words = voices.spread_words("Pink outside. Sunset inside, every time.", 4.0)
    spans = T.line_spans(["Pink outside.", "Sunset inside, every time."], words, 4.0)
    pic = T.picture_plan(spans, [{"src": str(c)} for c in clips])
    for aspect, size in (("9:16", (1080, 1920)), ("4:5", (1080, 1350))):
        tl = {"aspect": aspect, "fps": 24, "transition": 0.3, "picture": pic, "voice": {"src": str(tmp_path / "vo.mp3"), "words": words},
              "text": [{"template": "bold_type", "headline": "Pink outside", "start": 0, "end": 1.5}],
              "music": {"src": str(tmp_path / "m.mp3"), "gain_db": -18},                       # looped under the voice
              "end_card": {"dur": 1.5, "image": png(tmp_path / "hero.png"), "headline": "Rosy Sunshine Mango", "cta": "Shop now"}}
        r = T.render(tl, tmp_path / f"out{aspect[0]}.mp4")
        p = V.probe(r["path"])
        assert (p["width"], p["height"]) == size and p["duration"] == pytest.approx(T.duration(tl), abs=0.15)
        assert r["captions"] >= 2
        import re, subprocess
        tail = subprocess.run([V.ffmpeg_exe(), "-hide_banner", "-sseof", "-1.2", "-i", r["path"], "-af", "volumedetect", "-f", "null", "-"],
                              capture_output=True, text=True).stderr
        assert float(re.search(r"mean_volume: ([-\d.]+)", tail).group(1)) > -60      # music still plays under the end card


# ── storytelling ────────────────────────────────────────────────────────────
SCRIPT = {"title": "PEM story", "hook_headline": "Pink outside, gold inside", "cta": "Shop now", "lines": [
    {"text": "Most mangoes never look like this.", "shot_type": "whole_fruit_hero"},
    {"text": "Cut it open and the flesh is golden and soft.", "shot_type": "cross_section_macro"},
    {"text": "Grown in the hills of Vietnam, it tastes like honey.", "shot_type": "bogus_type"},
    {"text": "Rosy Sunshine mango, from Test Fruit Co.", "shot_type": "knife_cut"}]}


class ScriptClaude:
    def __init__(self, body):
        self.body, self.sent = body, []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kw):
        self.sent.append(kw)
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(self.body))],
                               usage=SimpleNamespace(input_tokens=3000, output_tokens=400, cache_read_input_tokens=0))


def _shot(env, shot_type, passed=True):
    c = env.db()
    clip = V.test_clip(env.tmp / f"shot_{shot_type}.mp4", size="360x640", seconds=3)
    with c:
        vid = c.execute("INSERT INTO adstudio_videos (product_id, source_kind, source_id, style, shot_type, status, clip_path,"
                        " model, created_at) VALUES (1, 'card', 0, 'shot', ?, 'done', ?, 'kwaivgi/kling-v3-video', '2026-10-20')",
                        (shot_type, str(clip))).lastrowid
    rubric = qa.load_rubric()
    keys = [k["key"] for k in rubric["criteria"] if qa.applies(k, "video") and not k.get("measured")]
    qa.save(c, "video", vid, rubric, {k: 5 if passed else 1 for k in keys}, "", 0.0)
    c.close()
    return vid


def test_script_uses_library_shots_flags_origin_and_reads_approved_facts(env):
    _shot(env, "whole_fruit_hero")
    c = env.db()
    with c:
        c.execute("INSERT INTO facts (product_id, category, text, status) VALUES (1, 'characteristic', 'The flesh is fibreless.', 'approved')")
        c.execute("INSERT INTO facts (product_id, category, text, status) VALUES (1, 'characteristic', 'Pending thing.', 'pending')")
    p = dict(c.execute("SELECT * FROM adstudio_products WHERE id=1").fetchone())
    fake = ScriptClaude(SCRIPT)
    s = story.write_script(c, p, 30, hook="Custard, not fibre", client=fake, allow_new=True)
    ask = fake.sent[0]["messages"][0]["content"]
    assert "The flesh is fibreless." in ask and "Pending thing." not in ask and "Custard, not fibre" in ask
    assert "whole_fruit_hero: Whole-fruit hero" in ask and "(in the library)" in ask and "about 78 spoken words" in ask
    assert "Never say where the fruit comes from" in fake.sent[0]["system"]
    assert s["lines"][2]["shot_type"] == "whole_fruit_hero"                     # unknown type → a library shot
    assert s["flags"] == ["Line 3 says where it comes from"] and s["words"] == 33
    pl = story.plan(c, 1, s, "Tess")
    assert pl["missing"] == ["cross_section_macro", "knife_cut"] and pl["estimate_shots"] > 1
    assert 0.06 <= pl["estimate_voice"] <= 0.1 and pl["music"] == []        # Wade on Gemini TTS since 2026-11-04
    assert story.plan(c, 1, s, "Luis")["estimate_voice"] > pl["estimate_voice"]  # ElevenLabs is priced per character


def test_no_library_shots_and_no_generating_is_refused(env):
    c = env.db()
    vid, why = story.create(c, 1, SCRIPT, "June", generate_missing=False)
    assert vid is None and "None of these shots are in the library" in why
    vid, why = story.create(c, 1, SCRIPT, "Nobody")
    assert vid is None and "unknown voice" in why


class FakeReplicate:
    """Text-to-speech returns a URL; WhisperX returns words for the voice-over."""
    def __init__(self, words=True):
        self.calls, self.words = [], words

    def run(self, model, input):
        self.calls.append((model.split(":")[0], {k: v for k, v in input.items() if k != "audio_file"}))
        if "whisperx" in model:
            if not self.words:
                raise RuntimeError("model down")
            ws = voices.spread_words(" ".join(ln["text"] for ln in SCRIPT["lines"]), 7.5)
            return {"segments": [{"words": ws}], "detected_language": "en"}
        return "https://replicate.delivery/vo.mp3"


def test_story_end_to_end_with_library_shots(env, monkeypatch):
    for t in ("whole_fruit_hero", "cross_section_macro"):
        _shot(env, t)
    _shot(env, "knife_cut", passed=False)                                      # a failed shot is never used
    monkeypatch.setattr(voices, "_download", lambda url, out: V.run(["-f", "lavfi", "-i", "sine=frequency=200:duration=8",
                                                                      "-ac", "1", str(out)]) and out or out)
    monkeypatch.setattr("creative_intelligence.adstudio.chat._client", lambda **k: FakeClaude([({}, 1, 4)]))
    fake = FakeReplicate()
    monkeypatch.setattr(voices, "_client", lambda: fake)
    vid = env.client.post("/api/adstudio/story", json={"product_id": 1, "script": SCRIPT, "voice": "Tess"}).get_json()["video_id"]
    drain(env.db)
    c = env.db()
    v = dict(c.execute("SELECT * FROM adstudio_videos WHERE id=?", (vid,)).fetchone())
    assert v["status"] == "done" and v["style"] == "story" and v["voice"] == "Tess", v["message"]
    assert "line 4: cross_section_macro instead of knife_cut" in v["message"] and "no music" in v["message"]
    assert [m for m, _ in fake.calls] == ["google/gemini-3.8-flash-tts", "victor-upmeet/whisperx"]
    assert fake.calls[0][1]["voice"] == "Aoede"
    tl = json.loads(v["timeline_json"])
    assert len(tl["picture"]) == 4 and tl["text"] == []      # the captions say the hook (2026-11-09)
    assert tl["end_card"]["cta"] == "Shop now" and tl["end_card"]["headline"] == "Rosy Sunshine Mango"
    renders = [dict(r) for r in c.execute("SELECT * FROM adstudio_video_renders WHERE video_id=?", (vid,))]
    assert {r["aspect"] for r in renders} == {"9:16", "4:5"}
    p = V.probe(renders[0]["output_path"])
    assert p["duration"] == pytest.approx(T.duration(tl), abs=0.2)
    costs = {r[0]: r[1] for r in c.execute("SELECT model, cost_usd FROM model_calls WHERE source='video' AND source_id=?", (vid,))}
    assert costs["google/gemini-3.8-flash-tts"] > 0 and costs["victor-upmeet/whisperx"] == pytest.approx(0.01)
    assert c.execute("SELECT SUM(reuse_count) FROM adstudio_videos WHERE style='shot'").fetchone()[0] == 4   # one per line
    g = env.client.get("/api/adstudio/gallery?product_id=1&type=video").get_json()["items"]
    assert len(g) == 2 and all(i["style"] == "story" for i in g)
    qa_row = qa.scores_for(c, "video", [vid]).get(vid)
    assert qa_row and qa_row["passed"]                                         # no fruit-count check on a story


def test_captions_fall_back_when_word_timings_fail(env, monkeypatch):
    _shot(env, "whole_fruit_hero")
    monkeypatch.setattr(voices, "_download", lambda url, out: V.run(["-f", "lavfi", "-i", "sine=frequency=200:duration=6",
                                                                      "-ac", "1", str(out)]) and out or out)
    monkeypatch.setattr("creative_intelligence.adstudio.chat._client", lambda **k: FakeClaude([({}, 1, 1)]))
    monkeypatch.setattr(voices, "_client", lambda: FakeReplicate(words=False))
    c = env.db()
    vid, _ = story.create(c, 1, SCRIPT, "June")
    drain(env.db)
    v = dict(c.execute("SELECT status, message FROM adstudio_videos WHERE id=?", (vid,)).fetchone())
    assert v["status"] == "done" and "captions timed roughly" in v["message"]


def test_waits_for_shots_being_made(env, monkeypatch):
    _shot(env, "whole_fruit_hero")
    c = env.db()
    with c:
        c.execute("INSERT INTO adstudio_videos (product_id, source_kind, source_id, style, shot_type, status, created_at)"
                  " VALUES (1, 'card', 0, 'shot', 'knife_cut', 'running', '2026-10-20')")
    vid, _ = story.create(c, 1, SCRIPT, "June")
    job = jobs.claim(c, "t:1")
    out = story.process(vid, env.db)
    assert out == {"waiting": ["knife_cut"]}
    assert c.execute("SELECT COUNT(*) FROM jobs WHERE kind='story' AND status='queued'").fetchone()[0] == 1
    assert job and "Waiting for 1 new shot" in c.execute("SELECT step FROM adstudio_videos WHERE id=?", (vid,)).fetchone()[0]


def test_options_and_script_routes(env, monkeypatch):
    o = env.client.get("/api/adstudio/story/options?product_id=1").get_json()
    assert [v["name"] for v in o["voices"]] == ["Wade", "Jack", "Luis", "June", "Ivy", "Tess"] and o["lengths"] == [20, 30, 45]
    assert o["default_voice"] == config.DEFAULT_VOICE and len(o["shots"]) == 12 and not o["readiness"]["ready"]
    monkeypatch.setattr(story, "write_script", lambda conn, p, sec, hook, idea, client=None, allow_new=False:
                        {**SCRIPT, "words": 30, "est_seconds": 14})
    d = env.client.post("/api/adstudio/story/script", json={"product_id": 1, "seconds": 30, "hook_id": 1}).get_json()
    assert d["script"]["plan"]["missing"] and d["script"]["hook_headline"] == "Pink outside, gold inside"
    assert env.client.post("/api/adstudio/story/script", json={"product_id": 1, "seconds": 31}).status_code == 400


def test_all_text_layers_go_in_one_overlay_input(tmp_path):
    """A 30 s story has ~30 captions; each used to be its own ffmpeg input, which ran the server out of threads."""
    pngs = [tmp_path / f"{i}.png" for i in range(3)]
    lst = T.overlay_track([(pngs[0], 0.0, 3.0), (pngs[2], 4.0, 5.0), (pngs[1], 3.0, 3.5)], 108, 192, 8.0, tmp_path)
    body = lst.read_text().splitlines()
    files = [l.split("'")[1].split("/")[-1] for l in body if l.startswith("file")]
    durs = [float(l.split()[1]) for l in body if l.startswith("duration")]
    assert files == ["0.png", "1.png", "blank.png", "2.png", "blank.png", "blank.png"]   # last listed twice
    assert durs == pytest.approx([3.0, 0.5, 0.5, 1.0, 3.0]) and sum(durs) == pytest.approx(8.0)


def test_ffmpeg_threads_are_capped_and_errors_say_why(monkeypatch):
    monkeypatch.setenv("FFMPEG_THREADS", "3")
    a = V._limited(["-i", "a.mp4", "-f", "concat", "-i", "l.txt", "-c:v", "libx264", "out.mp4"])
    assert a[:4] == ["-filter_threads", "3", "-filter_complex_threads", "3"]
    assert a.count("-threads") == 3 and a[-3:] == ["-threads", "3", "out.mp4"]
    assert V._why("x\n[auto_scale_19] Failed to configure output pad\nError reinitializing filters!\nConversion failed!") \
        == "Error reinitializing filters!"


def test_by_default_the_script_only_sees_library_shots(env):
    _shot(env, "whole_fruit_hero")
    c = env.db()
    p = dict(c.execute("SELECT * FROM adstudio_products WHERE id=1").fetchone())
    fake = ScriptClaude(SCRIPT)
    s = story.write_script(c, p, 20, client=fake)
    ask = fake.sent[0]["messages"][0]["content"]
    assert "whole_fruit_hero" in ask and "knife_cut" not in ask and "use only these" in ask
    assert {ln["shot_type"] for ln in s["lines"]} == {"whole_fruit_hero"}       # nothing to make, nothing to swap


def test_a_story_is_scored_as_a_montage(env, monkeypatch):
    from creative_intelligence.adstudio import video_qa
    seen = {}

    def fake_ask(clip, refs, product, rubric, client=None, note=""):
        seen["keys"], seen["note"] = [k["key"] for k in rubric["criteria"]], note
        return ({"scores": {k["key"]: 5 for k in rubric["criteria"] if qa.applies(k, "video") and not k.get("measured")},
                 "fruit_count_first": 1, "fruit_count_last": 2, "subject_box": [0.2, 0.2, 0.8, 0.8], "reasoning": "ok"},
                SimpleNamespace(input_tokens=10, output_tokens=10, cache_read_input_tokens=0))
    monkeypatch.setattr(video_qa, "ask_claude", fake_ask)
    clip = V.test_clip(env.tmp / "m.mp4", size="360x640", seconds=1)
    row = video_qa.score(env.db(), 99, str(clip), [], {"name": "PEM", "product_type": "fruit"}, is_montage=True)
    assert "consistency" not in seen["keys"] and "MONTAGE" in seen["note"]
    assert row["passed"]                                                       # 1 then 2 fruit: no count fail


def test_music_catalog_puts_instrumentals_first_and_never_defaults_to_vocals(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MUSIC_DIR", str(tmp_path))
    for name in ("sunny.m4a", "sung.m4a", "unknown.m4a"):
        V.run(["-f", "lavfi", "-i", "sine=duration=1", "-c:a", "aac", str(tmp_path / name)])
    (tmp_path / "tracks.json").write_text(json.dumps({"tracks": [
        {"file": "sung.m4a", "title": "Sung (feat. X)", "vocals": True, "source": "Meta Sound Collection", "use": "Meta only"},
        {"file": "sunny.m4a", "title": "Sunny (Instrumental)", "vocals": False, "source": "Meta Sound Collection", "use": "Meta only"}]}))
    cat = story.music_catalog()
    assert [t["file"] for t in cat] == ["sunny.m4a", "sung.m4a", "unknown.m4a"] and cat[0]["use"] == "Meta only"
    assert story.pick_music("").name == "sunny.m4a" and story.pick_music("none") is None
    assert story.pick_music("sung.m4a").name == "sung.m4a" and story.pick_music("gone.m4a") is None


def test_add_music_marks_vocals_from_the_title():
    import importlib.util
    spec = importlib.util.spec_from_file_location("add_music", "scripts/add_music.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert m.vocals("Be Happy (Instrumental)") is False and m.vocals("Paint Me Happy (Loop)") is False
    assert m.vocals("I Feel The Sun (feat. Alex Blue)") is True and m.vocals("Heartline") is None
    assert m.slug("It's All Good (Instrumental)") == "it-s-all-good-instrumental"


def test_unapproved_claims_get_one_rewrite(env, monkeypatch):
    _shot(env, "whole_fruit_hero")
    from creative_intelligence.adstudio import facts
    checks = iter([{"status": "flagged", "unlisted": [{"claim": "most are stringy", "field": "script"}], "banned": []},
                   {"status": "ok", "unlisted": [], "banned": []}])
    monkeypatch.setattr(facts, "check_copy", lambda *a, **k: next(checks))
    fixed = {**SCRIPT, "lines": [{"text": "You've waited all year for this mango.", "shot_type": "nope"}]}

    class TwoTurns(ScriptClaude):
        def __init__(self):
            super().__init__(SCRIPT)
            self.bodies = [SCRIPT, fixed]

        def create(self, **kw):
            self.body = self.bodies[len(self.sent)]
            return super().create(**kw)
    fake = TwoTurns()
    c = env.db()
    s = story.write_script(c, dict(c.execute("SELECT * FROM adstudio_products WHERE id=1").fetchone()), 20, client=fake)
    assert len(fake.sent) == 2 and "most are stringy" in fake.sent[1]["messages"][0]["content"]
    assert s["claims"]["status"] == "ok" and s["repaired"] == ["most are stringy"]
    assert s["lines"][0]["shot_type"] == "whole_fruit_hero"                        # shot kept from the draft
    assert "never a claim about other fruit" in fake.sent[0]["system"]
