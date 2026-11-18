"""Video Studio V6 — styles and variants: lines of three kinds (narrator, presenter, text), 3 hooks, 15 s cuts,
two sizes, two caption styles; every piece made once and reused across variants. Replicate and Claude are faked;
ffmpeg is real."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from creative_intelligence import config
from creative_intelligence.adstudio import styles as S, talking, videotools as V, voices
from tests.creative_intelligence.test_v1_animate import drain, env, png  # noqa: F401  (fixture)
from tests.creative_intelligence.test_v3_story import ScriptClaude, _shot


@pytest.fixture(autouse=True)
def no_music(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MUSIC_DIR", str(tmp_path / "no-music"))


SCRIPT = {"title": "PEM fruit story", "hook_headline": "Meet the pink giant", "cta": "Order yours", "hooks": [
    "This mango blushes pink as it grows.", "Two pounds of mango, one thin seed.", "You've waited all season for this one."],
    "lines": [{"text": "This mango blushes pink as it grows.", "kind": "presenter", "shot_type": "x", "short": False},
              {"text": "Cut it open: golden, soft flesh.", "kind": "narrator", "shot_type": "cross_section_macro", "short": True},
              {"text": "Almost no acidity", "kind": "text", "shot_type": "juice_texture_macro", "short": False},
              {"text": "Harvested ripe, shipped ready to enjoy.", "kind": "presenter", "shot_type": "whole_fruit_hero", "short": False}]}


def test_clean_fits_the_style():
    menu = [{"type": "whole_fruit_hero"}, {"type": "cross_section_macro"}]
    s = S._clean(json.loads(json.dumps(SCRIPT)), menu, "sensory")
    assert {ln["kind"] for ln in s["lines"]} == {"text"} and s["lines"][0]["shot_type"] == "whole_fruit_hero"
    s = S._clean(json.loads(json.dumps(SCRIPT)), menu, "kinetic")
    assert "presenter" not in {ln["kind"] for ln in s["lines"]}
    s = S._clean(json.loads(json.dumps(SCRIPT)), menu, "fruit_story")
    assert s["lines"][0]["short"] and s["lines"][-1]["short"] and s["hooks"][0] == s["lines"][0]["text"]


def test_talking_head_is_the_presenter_in_the_corner_over_shots(tmp_path):
    """Feedback 2026-10-29: a montage with the presenter's head talking on top, not the presenter full screen."""
    from creative_intelligence.adstudio import timeline as T
    menu = [{"type": "whole_fruit_hero"}, {"type": "cross_section_macro"}]
    s = S._clean(json.loads(json.dumps(SCRIPT)), menu, "talking_head")
    assert {ln["kind"] for ln in s["lines"]} == {"presenter"}      # one voice, the head on screen throughout
    x, y, size = T.pip_box(1080, 1920, "9:16")
    assert x > 0 and y + size == round(0.65 * 1920) and size < 1080 / 2   # bottom left, inside the safe zone
    assert T.hook_place({"pip": "head.mp4", "label": "presenter"}) == "top"   # the hook stays off the head
    mask, ring = T.pip_masks(size, tmp_path, 0)
    assert mask.exists() and ring.exists()


def test_twelve_variants_and_the_15_second_cut():
    vs = S.variants(SCRIPT)
    assert len(vs) == 12 and {v["captions"] for v in vs} == {"bold"}
    assert len(S.variants(SCRIPT, captions=("bold", "boxed"))) == 24
    short = S.lines_for(SCRIPT, 1, "short")
    assert [ln["text"] for ln in short] == ["Two pounds of mango, one thin seed.", "Cut it open: golden, soft flesh.",
                                            "Harvested ripe, shipped ready to enjoy."]


def test_pieces_are_paid_once_extra_variants_cost_only_new_hooks(env):
    c = env.db()
    vs = S.variants(SCRIPT)
    e = S.estimate(c, SCRIPT, vs, "Wade", 1, "omnihuman")
    hook_cost = talking.estimate(c, "omnihuman", SCRIPT["hooks"][1]) + talking.estimate(c, "omnihuman", SCRIPT["hooks"][2])
    assert e["pieces"] == 6 and e["presenter_lines"] == 4 and e["variants"] == 12          # 2 body presenter lines + 3 hooks… minus reuse
    assert e["total"] - e["first"] == pytest.approx(hook_cost + 0.01 * 5, abs=0.03)
    assert e["per_extra_variant"] < e["first"] / 3


def _env_pieces(env, monkeypatch):
    for t in ("cross_section_macro", "juice_texture_macro", "whole_fruit_hero"):
        _shot(env, t)
    made = {"narrator": 0, "presenter": 0}

    def speak(conn, text, name, out, source="", source_id=0, client=None, context=("", ""), style=""):
        made["narrator"] += 1
        f = out.with_suffix(".mp3")
        V.run(["-f", "lavfi", "-i", "sine=frequency=300:duration=1.5", str(f)])
        return f, 0.002

    def make(conn, pid, line, method, out_name, source_id, client=None, face=None, action="", context=("", "")):
        made["presenter"] += 1
        from creative_intelligence.storage import storage
        clip = storage().area_dir("videos") / f"{out_name}.mp4"
        silent = V.test_clip(env.tmp / f"{out_name}_s.mp4", size="360x640", seconds=2)
        V.run(["-i", str(silent), "-f", "lavfi", "-i", "sine=duration=2", "-shortest", "-c:v", "copy", "-c:a", "aac", str(clip)])
        return str(clip), 2.0, "bytedance/omni-human-1.5"
    monkeypatch.setattr(voices, "speak", speak)
    monkeypatch.setattr(talking, "make", make)
    monkeypatch.setattr(talking, "portrait", lambda conn, pid: ({"name": "Wade", "voice": "Wade"}, png(env.tmp / "face.png")))
    monkeypatch.setattr(talking, "table_scene", lambda conn, pr, pid: png(env.tmp / "table.png"))
    monkeypatch.setattr(voices, "word_timings", lambda conn, audio, source="", source_id=0, client=None:
                        voices.spread_words("This mango blushes pink as it grows Cut it open golden soft flesh", 6.0))
    monkeypatch.setattr("creative_intelligence.adstudio.chat._client", lambda **k: __import__(
        "tests.creative_intelligence.test_v1_animate", fromlist=["FakeClaude"]).FakeClaude([({}, 1, 1)]))
    return made


def test_variants_render_from_shared_pieces(env, monkeypatch):
    made = _env_pieces(env, monkeypatch)
    c = env.db()
    s = {**json.loads(json.dumps(SCRIPT)), "style": "fruit_story", "claims": {"status": "ok"}}
    vs = S.variants(s, hooks=2, aspects=("9:16",), captions=("bold", "boxed"))
    vid, why = S.create(c, 1, s, "Wade", 1, "omnihuman", vs)
    assert vid, why
    drain(env.db)
    v = dict(c.execute("SELECT * FROM adstudio_videos WHERE id=?", (vid,)).fetchone())
    assert v["status"] == "done" and v["ai_person"] == 1 and v["style"] == "fruit_story", v["message"]
    assert made == {"narrator": 1, "presenter": 3}                     # 2 hooks + the closing presenter line, each once
    rs = [dict(r) for r in c.execute("SELECT * FROM adstudio_video_renders WHERE video_id=?", (vid,))]
    assert len(rs) == 8 and {r["template"] for r in rs} == {"bold", "boxed"}
    assert any(r["note"].startswith("Hook 2 · short · 9:16 · boxed captions") for r in rs)
    tl = json.loads(v["timeline_json"])
    assert any(t["template"] == "kinetic" and t["headline"] == "Almost no acidity" for t in tl["text"])
    assert tl["picture"][0]["label"] == "presenter"
    full = [r for r in rs if "full" in r["note"]][0]
    short = [r for r in rs if "short" in r["note"]][0]
    assert V.probe(short["output_path"])["duration"] < V.probe(full["output_path"])["duration"]


def test_presenter_styles_need_an_approved_presenter_and_clean_claims(env):
    c = env.db()
    s = {**json.loads(json.dumps(SCRIPT)), "style": "fruit_story", "claims": {"status": "ok"}}
    vid, why = S.create(c, 1, s, "Wade", 1, "omnihuman")
    assert vid is None and ("Approve a look" in why or "not found" in why)
    s2 = {**s, "lines": [{**ln, "kind": "narrator"} for ln in s["lines"]], "claims": {"status": "flagged"}}
    assert "isn't an approved fact" in S.create(c, 1, s2, "Wade")[1]


def test_script_route_with_a_template(env, monkeypatch):
    _shot(env, "whole_fruit_hero")
    fake = ScriptClaude(SCRIPT)
    monkeypatch.setattr("creative_intelligence.adstudio.chat._client", lambda **k: fake)
    monkeypatch.setattr("creative_intelligence.adstudio.facts.check_copy", lambda *a, **k: {"status": "ok", "unlisted": [], "banned": []})
    c = env.db()
    with c:
        c.execute("INSERT INTO video_templates (ad_id, ad_name, source, status, template_json, created_at) VALUES"
                  " ('a1', 'Concept 174', 'thumbnail', 'done', ?, 'now')", (json.dumps({"hook": {"type": "unboxing"}}),))
    o = env.client.get("/api/adstudio/styles").get_json()
    assert o["templates"][0]["name"] == "Concept 174" and o["default_method"] == "seedance_text"
    d = env.client.post("/api/adstudio/styles/script", json={"product_id": 1, "style": "template", "template_id": o["templates"][0]["id"]}).get_json()
    assert d["script"]["style"] == "template" and "unboxing" in fake.sent[0]["messages"][0]["content"]
    assert "FROM A WINNING AD" in fake.sent[0]["messages"][0]["content"]
    assert {ln["kind"] for ln in d["script"]["lines"]} <= {"narrator", "text"}               # no presenter in this style
    e = env.client.post("/api/adstudio/styles/estimate", json={"script": d["script"], "voice": "Wade", "aspects": ["9:16"]}).get_json()
    assert e["estimate"]["variants"] == 6
