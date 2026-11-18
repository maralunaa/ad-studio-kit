"""K2a: prompts read the knowledge base by key and record its version; Hook University keeps its angles and
archetypes and adds the playbook's hook type, personal-attribute check and emoji share. No live API."""
from __future__ import annotations

import json
import random
from types import SimpleNamespace

import pytest

from creative_intelligence import knowledge, prompts
from creative_intelligence.adstudio import facts
from creative_intelligence.adstudio import hook_university as HU
from creative_intelligence.db import get_connection, init_db

GOOD = ["Rosy Sunshine Mango weighs two pounds, yet most people walk past it at the market every week",
        "The Rosy Sunshine Mango ripens in a window so short most growers never ship it at all",
        "My kids asked for Rosy Sunshine Mango before dessert, and I still haven't told them why",
        "Rosy Sunshine Mango cuts like butter, but the first slice is not the part people remember",
        "One Rosy Sunshine Mango feeds four, though nobody at the table will want to share it",
        "Rosy Sunshine Mango tastes like honey and sunset, and the seed is thinner than you'd expect",
        "Rosy Sunshine Mango smells like vanilla before you even pick up a knife to open it up"]
KNOWLEDGE_PROMPTS = ("hook-university-system", "ai-ad-copy-rules", "claims-check-system", "story-script-system",
                     "image-concepts-system", "run-plan-system")


def _hk(i, **kw):
    return {"hook_text": GOOD[i], "headline": "Mango custard", "archetype": f"A{i + 1} — x", "body": "", **kw}


# ── prompts ────────────────────────────────────────────────────────────────
def test_prompts_read_knowledge_by_key_and_record_its_version():
    for name in KNOWLEDGE_PROMPTS:
        raw = prompts._raw(name, prompts.version(name))
        assert knowledge.keys_in(raw), name                      # the file references keys…
        text = prompts.text(name)
        assert "{{knowledge:" not in text and "[Meta Creative Playbook:" in text   # …filled in on read
        assert prompts.stamp(name).endswith(f"+knowledge@{knowledge.version()}")
    assert "knowledge@" not in prompts.stamp("core-brief-system")            # untouched prompts stamp as before
    assert "knowledge@" not in prompts.stamp("hook-university-system").split("+")[0]
    hu = prompts.text("hook-university-system")
    assert "visual_sensory (Visual / sensory)" in hu and "Example pattern (not copy)" in hu
    assert "A12" in hu and "D+C+PS" in hu                                    # Hook University itself stays


def test_used_by_lists_the_prompts_that_read_each_key():
    used = knowledge.used_by()
    assert used["hooks.types"] == ["hook-university-system"]
    assert set(used["compliance.rules"]) >= {"hook-university-system", "claims-check-system", "story-script-system"}
    assert knowledge.errors() == []


def test_an_unknown_key_in_a_prompt_is_an_error(monkeypatch):
    with pytest.raises(KeyError):
        knowledge.render("rules.no_such_rule")
    monkeypatch.setattr(knowledge, "used_by", lambda: {"rules.no_such_rule": ["x"]})
    assert any("rules.no_such_rule" in e for e in knowledge.errors())


def test_old_prompt_versions_stay_for_the_record():
    assert "{{knowledge:" not in prompts.text("hook-university-system", 2)
    assert "No emoji" in prompts.text("ai-ad-copy-rules", 1)


# ── Hook University + playbook ─────────────────────────────────────────────
def test_angles_stay_with_the_health_angle_rewritten():
    assert len(HU.ANGLES) == 15
    health = next(a for a in HU.ANGLES if a.startswith("health"))
    assert "approved facts" in health and "can help" in health and "vitality" not in health
    assert HU.is_cliche("You've never tasted mango like this")              # decision a: still banned
    assert any(a.startswith("discovery moment") for a in HU.ANGLES)


def test_request_asks_for_hook_types_and_places_emoji():
    ask = HU.request("Rosy Sunshine Mango", 7, HU.ANGLES[0], [], emoji=[3])
    assert "hook_type" in ask and "visual_sensory" in ask and "offer" in ask
    assert "only in the body of hook 3" in ask and "personal condition" in ask
    assert "No emoji anywhere." in HU.request("Rosy Sunshine Mango", 7, HU.ANGLES[0], [], emoji=[])


def test_emoji_slots_are_about_fifteen_percent():
    rng = random.Random(4)
    assert len(HU.emoji_slots(20, rng)) == 3 and len(HU.emoji_slots(7, rng)) in (1, 2)
    picks = [len(HU.emoji_slots(5, random.Random(i))) for i in range(400)]
    assert 0.6 < sum(picks) / 400 < 0.9                                    # 15% of 5 = 0.75 a batch on average


def test_screen_checks_type_personal_attributes_and_emoji():
    hooks = [_hk(0, hook_type="visual_sensory"),
             _hk(1, hook_type="made_up_type"),
             _hk(2, body="Are you diabetic? This one is different."),
             _hk(3, headline="Mango custard 🥭"),
             _hk(4, body="Juice everywhere 🥭🥭🥭"),
             _hk(5, body="Custard in a skin 🥭"),
             _hk(6, body="Honey and vanilla 🍯")]
    ok, bad = HU.screen(hooks, [], "Rosy Sunshine Mango")
    flags = {h["hook_text"]: h["qa_flags"] for h in ok + bad}
    assert hooks[0]["hook_type"] == "visual_sensory" and hooks[1]["hook_type"] == ""   # unknown type cleared
    assert flags[GOOD[2]] == ["personal_attribute"]
    assert flags[GOOD[3]] == ["emoji_on_image"]
    assert flags[GOOD[4]] == ["too_many_emoji"]
    assert flags[GOOD[5]] == [] and flags[GOOD[6]] == ["emoji_over_share"]     # 15% of 7 = one hook with emoji


class _HookClient:
    def __init__(self, replies):
        self.replies, self.sent = list(replies), []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.sent.append(kw)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps({"hooks": self.replies.pop(0)}))])


def test_suggested_hooks_carry_type_and_versions(monkeypatch):
    from creative_intelligence.adstudio import chat
    fake = _HookClient([[_hk(i, hook_type=t) for i, t in enumerate(("curiosity", "gift", "offer", "problem", "gift"))]])
    monkeypatch.setattr(chat, "_client", lambda: fake)
    hooks = chat.suggest_hooks({"name": "Rosy Sunshine Mango", "product_type": "fruit"}, 3)
    assert [h["hook_type"] for h in hooks] == ["curiosity", "gift", "offer"]
    assert all(h["prompt_version"].startswith("hook-university-system@v4+knowledge@pb16-") for h in hooks)
    assert "Hook types: the opening move" in fake.sent[0]["system"]


# ── copy check and saving ─────────────────────────────────────────────────
@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CI_DB_PATH", str(tmp_path / "t.db"))
    init_db(tmp_path / "t.db")
    c = get_connection(tmp_path / "t.db")
    with c:
        c.execute("INSERT INTO adstudio_products (id, name, product_type) VALUES (1, 'Rosy Sunshine Mango', 'fruit')")
    yield c
    c.close()


class _ClaimsClient:
    def __init__(self):
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.system = kw["system"]
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text='{"claims": []}')],
                               usage=SimpleNamespace(input_tokens=1, output_tokens=1, cache_read_input_tokens=0))


def test_copy_check_flags_personal_attributes_everywhere(conn):
    fake = _ClaimsClient()
    r = facts.check_copy(conn, {"id": 1, "name": "Rosy Sunshine Mango", "product_type": "fruit"},
                         {"script": "Struggling with your weight? Rosy Sunshine Mango is sweet."}, client=fake)
    assert r["status"] == "flagged"
    assert r["banned"] == [{"field": "script", "term": "Struggling with your weight", "rule": "personal_attribute"}]
    assert "cures, treats" in fake.system and "Reviews and testimonials must be real" in fake.system


@pytest.fixture
def client(conn, tmp_path, monkeypatch):
    import creative_intelligence.webapp.app as app_module
    from creative_intelligence.adstudio import chat
    monkeypatch.setattr("creative_intelligence.webapp.common.db", lambda: get_connection(tmp_path / "t.db"))
    monkeypatch.setattr(chat, "_client", lambda **k: _ClaimsClient())
    return app_module.app.test_client()


def test_hook_type_and_versions_are_saved_and_shown(client):
    client.post("/api/adstudio/product/1/hook", json={"hook_text": GOOD[0], "hook_type": "curiosity",
                                                      "prompt_version": "hook-university-system@v3+knowledge@pb16-x"})
    client.post("/api/adstudio/product/1/hook", json={"hook_text": GOOD[1], "hook_type": "nonsense"})
    hooks = {h["hook_text"]: h for h in client.get("/api/adstudio/product/1").get_json()["hooks"]}
    assert hooks[GOOD[0]]["hook_type"] == "curiosity" and hooks[GOOD[0]]["prompt_version"].endswith("pb16-x")
    assert hooks[GOOD[1]]["hook_type"] == ""
    types = client.get("/api/adstudio/config").get_json()["hook_types"]
    assert [t["key"] for t in types] == knowledge.hook_type_keys() and types[0]["name"] == "Visual / sensory"


def test_season_dates_are_flagged_and_writers_know_today():
    """Feedback 2026-10-30: "Season ends in May" twice, written in October."""
    from datetime import date
    from creative_intelligence.adstudio import facts
    from creative_intelligence.adstudio.chat import when_line
    assert facts.season_date_hits("Cherimoya season ends in May") and facts.season_date_hits("Only until spring")
    assert not facts.season_date_hits("In season now") and not facts.season_date_hits("Limited season, hand-sorted")
    res = facts.with_season_check({"status": "ok", "banned": [], "unlisted": [], "claims": []}, ["Season Ends in May"])
    assert res["status"] == "flagged" and res["banned"][0]["rule"] == "season_date"
    line = when_line({"season": "in"}, date(2026, 10, 30))
    assert "October 30, 2026" in line and "in season" in line
