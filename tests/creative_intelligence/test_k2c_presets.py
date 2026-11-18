"""K2c: the Images and Video style pickers come from knowledge/styles.yaml, so adding a style is a knowledge entry
plus its template or prompt; Fruit Story has a presenter-led / narrated switch; decoded winners keep working.
Claude is faked."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from creative_intelligence import knowledge, prompts
from creative_intelligence.adstudio import styles as S

SCRIPT = {"title": "PEM fruit story", "hook_headline": "Meet the pink giant", "cta": "Shop now",
          "hooks": ["This mango blushes pink as it grows."],
          "lines": [{"text": "This mango blushes pink as it grows.", "kind": "presenter", "shot_type": "whole_fruit_hero", "short": False},
                    {"text": "Cut it open: golden, soft flesh.", "kind": "narrator", "shot_type": "whole_fruit_hero", "short": True},
                    {"text": "Why it's different.", "kind": "presenter", "shot_type": "whole_fruit_hero", "short": False}]}
MENU = [{"type": "whole_fruit_hero", "label": "Hero", "summary": "", "in_library": True}]


def test_video_styles_are_the_live_knowledge_styles():
    cat = S.catalog()
    assert list(cat)[0] == "fruit_story"                                                        # playbook priority
    assert [cat[k]["label"] for k in ("talking_head", "sensory", "pov")] == ["Talking head / explainer", "Sensory / ASMR",
                                                                              "POV unboxing / gift reveal"]
    assert {"kinetic", "template", "how_to_eat", "fast_cut_montage"} <= set(cat)
    assert not {"us_vs_supermarket", "comment_reply"} & set(cat)                            # retired 2026-10-30
    assert cat["fruit_story"]["modes"] == ["presenter", "narrated"] and cat["fruit_story"]["presenter"]
    assert cat["sensory"]["voice"] is False and not cat["sensory"]["presenter"]
    assert cat["template"]["prompt"] == "style-template"                       # decoded winners keep working
    for st in (x for x in cat.values() if x["route"] != "listicle"):          # the listicle needs no prompt
        assert "{{knowledge:" not in prompts.text(st["prompt"])                   # each prompt reads its playbook entry
        assert prompts.stamp(st["prompt"]).split("+")[-1].startswith("knowledge@")


def test_adding_a_style_is_a_knowledge_change(monkeypatch):
    real = knowledge.load

    def load(name):
        d = real(name)
        if name != "styles":
            return d
        sensory = next(x for x in d["styles"] if x["key"] == "sensory")
        extra = {**sensory, "key": "slow_reveal", "name": "Slow reveal", "priority": 2}
        return {**d, "styles": d["styles"] + [extra]}
    monkeypatch.setattr(knowledge, "load", load)
    assert "slow_reveal" in S.catalog() and S.catalog()["slow_reveal"]["prompt"] == "style-sensory"


def test_modes_and_labels():
    assert S.mode_for("fruit_story", None) == "presenter" and S.mode_for("fruit_story", "narrated") == "narrated"
    assert S.mode_for("fruit_story", "hologram") == "presenter" and S.mode_for("sensory", "narrated") is None
    assert S.label("fruit_story", "narrated") == "Fruit Story · narrated"
    assert S.label("fruit_story", "presenter") == "Fruit Story · presenter-led" and S.label("kinetic") == "Kinetic type"


def test_narrated_fruit_story_has_no_presenter_lines():
    s = S._clean(json.loads(json.dumps(SCRIPT)), MENU, "fruit_story", "narrated")
    assert {ln["kind"] for ln in s["lines"]} == {"narrator"}
    s = S._clean(json.loads(json.dumps(SCRIPT)), MENU, "fruit_story", "presenter")
    assert [ln["kind"] for ln in s["lines"]] == ["presenter", "narrator", "presenter"]


class _Claude:
    def __init__(self):
        self.sent = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.sent.append(kw)
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(SCRIPT))],
                               usage=SimpleNamespace(input_tokens=1, output_tokens=1, cache_read_input_tokens=0))


def test_write_tells_claude_the_mode(monkeypatch):
    from creative_intelligence.adstudio import costs, facts, story
    monkeypatch.setattr(story, "shot_menu", lambda conn, pid: MENU)
    monkeypatch.setattr(facts, "prompt_block", lambda conn, p: "APPROVED FACTS: (none)")
    monkeypatch.setattr(facts, "check_copy", lambda *a, **k: {"status": "ok", "unlisted": [], "banned": []})
    monkeypatch.setattr(costs, "log_tokens", lambda *a, **k: None)
    fake = _Claude()
    s = S.write(None, {"id": 1, "name": "Rosy Sunshine Mango", "product_type": "fruit"}, "fruit_story", client=fake,
                mode="narrated")
    ask = fake.sent[0]["messages"][0]["content"]
    assert "MODE: narrated" in ask and "Narrated: voice-over only" in ask and "Brand video structures" in ask
    assert s["mode"] == "narrated" and all(ln["kind"] == "narrator" for ln in s["lines"])
    n = len(fake.sent)
    s = S.write(None, {"id": 1, "name": "Rosy Sunshine Mango", "product_type": "fruit"}, "fruit_story", client=fake)
    # the write call, not the read-aloud pass after it (v51)
    assert s["mode"] == "presenter" and "MODE: presenter" in fake.sent[n]["messages"][0]["content"]


def test_create_keeps_narrated_scripts_free_of_presenters(monkeypatch):
    from creative_intelligence import budget
    monkeypatch.setattr(budget, "caps", lambda conn: {"video_job": 1000})
    monkeypatch.setattr(budget, "check", lambda *a, **k: None)
    monkeypatch.setattr(S, "estimate", lambda *a, **k: {"total": 0.1})
    seen = {}

    class Conn:
        def execute(self, sql, args=()):
            seen["settings"] = json.loads(args[3]) if "INSERT INTO adstudio_videos" in sql else seen.get("settings")
            return SimpleNamespace(lastrowid=9)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    monkeypatch.setattr("creative_intelligence.jobs.enqueue", lambda *a, **k: None)
    s = {**json.loads(json.dumps(SCRIPT)), "style": "fruit_story", "mode": "narrated", "claims": {"status": "ok"}}
    vid, why = S.create(Conn(), 1, s, "Wade")                # no presenter chosen: fine when narrated
    assert vid == 9 and why == ""
    assert seen["settings"]["mode"] == "narrated" and {ln["kind"] for ln in seen["settings"]["lines"]} == {"narrator"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CI_DB_PATH", str(tmp_path / "data" / "t.db"))
    from creative_intelligence.webapp import app as webapp
    webapp.app.config["TESTING"] = True
    return webapp.app.test_client()


def test_pickers_come_from_styles_yaml(client):
    from creative_intelligence.adstudio.templates import TEMPLATES
    cfg = client.get("/api/adstudio/config").get_json()
    static = cfg["static_styles"]
    assert static[0]["name"] == "Appetite close-up" and "craving_macro" in static[0]["templates"]
    assert {t for s in static for t in s["templates"]} == set(TEMPLATES)        # every Compose layout has its style
    video = {s["key"]: s for s in cfg["video_styles"]}
    assert video["fruit_story"]["live"] and video["fruit_story"]["modes"] == ["presenter", "narrated"]
    assert video["talking_head"]["needs_presenter"] and not video["fruit_story"]["needs_presenter"]
    assert video["fruit_characters"]["live"] and not any((video.get(k) or {}).get("live") for k in ("us_vs_supermarket", "comment_reply"))
    assert not any(v["live"] for k, v in video.items() if k in ("carousel",))                    # planned: coming soon
    assert video["animated_still"]["live"]                                    # made by Video › Animate (V1)
    page = client.get("/video/create").data.decode()
    assert "'Storytelling'" not in page and "renderStory" not in page      # S1: Fruit Story narrated replaces it
    o = client.get("/api/adstudio/styles").get_json()
    assert set(o["styles"]) == set(S.catalog()) and o["mode_labels"]["narrated"] == "narrated"
