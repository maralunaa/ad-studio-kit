"""G4 (feedback 2026-10-22): easy fruit characters. "Make characters" writes four caricatures (cheerful, goofy, grumpy,
sleepy) from a code template and queues one look sheet each; "Use this one" approves a sheet and makes that character
the one episodes use; the Fruit characters style picks it, and says exactly what to do when no character is ready.
Replicate and Claude are faked; jobs are captured, never run against a real API."""
from __future__ import annotations

import pytest

from creative_intelligence import budget, config
from creative_intelligence.adstudio import characters, shots, styles as S
from creative_intelligence.adstudio.hook_university import origin_claim
from creative_intelligence.db import get_connection
from tests.creative_intelligence.test_s2a_video_styles import _Claude, _shot, conn  # noqa: F401  (fixture)

P = {"id": 1, "name": "Honey Mango", "product_type": "fruit"}
SCRIPT = {"title": "t", "hook_headline": "Meet me", "cta": "Shop now", "hooks": ["Hi."],
          "lines": [{"text": "Hi.", "kind": "narrator", "shot_type": "character_scene", "short": True},
                    {"text": "Want to see inside?", "kind": "narrator", "shot_type": "character_scene", "short": True}]}


@pytest.fixture
def queued(monkeypatch):
    """Capture the jobs instead of running them; the generation cap always allows."""
    jobs = []
    monkeypatch.setattr("creative_intelligence.budget.check", lambda *a, **k: None)
    monkeypatch.setattr("creative_intelligence.jobs.enqueue", lambda conn, kind, payload, **k: jobs.append((kind, payload)))
    return jobs


def _draw(conn, tmp_path, monkeypatch, sheet_ids, fail=False):
    """Run the character_sheet job with a fake image model."""
    sheet = tmp_path / "sheet.png"
    sheet.write_bytes(b"\x89PNG" + b"0" * 100)
    monkeypatch.setattr("creative_intelligence.webapp.common._primary_photo_uris", lambda *a, **k: [])
    monkeypatch.setattr("creative_intelligence.storage.storage",
                        lambda: type("S", (), {"put_url": lambda self, area, name, url: str(sheet)})())
    monkeypatch.setattr("creative_intelligence.adstudio.costs.log_image", lambda *a, **k: 0)

    def gen(*a, **k):
        if fail:
            raise RuntimeError("model busy")
        return "file://x"
    return characters.process(sheet_ids, lambda: get_connection(tmp_path / "data" / "t.db"), generate=gen)


def _sheet_of(conn, name):
    return conn.execute("SELECT s.id FROM character_sheets s JOIN characters c ON c.id=s.character_id WHERE c.name=?"
                        " ORDER BY s.id DESC", (name,)).fetchone()[0]


def _scene_for(conn, tmp_path, name):
    cid = conn.execute("SELECT id FROM characters WHERE name=?", (name,)).fetchone()[0]
    vid = _shot(conn, 1, "character_scene", tmp_path)
    with conn:
        conn.execute("UPDATE adstudio_videos SET prompt_version=? WHERE id=?", (f"shot:character_scene@v1+character:{cid}+x", vid))
    return vid


def test_make_characters_writes_five_distinct_caricatures_without_origin_claims(conn, queued):
    cids, sids, why = characters.make_cast(conn, 1)
    assert why == "" and len(cids) == 5 and len(sids) == 5
    cs = characters.listing(conn, 1)
    assert [c["name"] for c in cs] == ["Sunny Mango", "Goofy Mango", "Grumpy Mango", "Sleepy Mango", "Teary Mango"]
    pers = " | ".join(c["personality"] for c in cs)
    for mood in ("sunny", "goofy", "grumpy", "sleepy", "sad"):
        assert mood in pers
    assert len({c["personality"] for c in cs}) == 5 and len({c["look"] for c in cs}) == 5
    voices = [c["voice"] for c in cs]
    assert len(set(voices)) == 5 and set(voices) <= {v["name"] for v in config.HOUSE_VOICES}
    for c in cs:
        for field in ("name", "personality", "dos", "donts", "look"):
            assert not origin_claim(c[field], P["name"]), (c["name"], field)
        assert "where it is grown" in c["donts"] and "health" in c["donts"]
        assert "end by naming Honey Mango and the offer" in c["dos"]
        assert c["status"] == "draft" and [s["status"] for s in c["sheets"]] == ["queued"]
    # one paid character_sheet job per character; each sheet asks for a caricature in its own pose
    assert [k for k, _ in queued] == ["character_sheet"] * 5
    prompts = [r[0] for r in conn.execute("SELECT prompt FROM character_sheets ORDER BY id")]
    assert all("caricature" in p and "Honey Mango" in p and "plain off-white background" in p
               and "true shape, skin colour, texture" in p and "${" not in p for p in prompts)
    assert "giant yawn" in prompts[3] and "scowl" in prompts[2]
    # a second click doesn't duplicate them
    assert characters.make_cast(conn, 1)[2].startswith("This product already has every character")


def test_make_characters_checks_the_cap_for_the_whole_batch_first(conn, monkeypatch):
    seen = []

    def cap(conn, bucket, usd):
        seen.append((bucket, usd))
        raise budget.BudgetExceeded("Over today's generation cap.")
    monkeypatch.setattr("creative_intelligence.budget.check", cap)
    cids, sids, why = characters.make_cast(conn, 1)
    assert cids == [] and "cap" in why and seen == [("generation", characters.estimate(conn, 5))]
    assert conn.execute("SELECT COUNT(*) FROM characters").fetchone()[0] == 0


def test_use_this_one_approves_and_the_style_picks_that_character(conn, tmp_path, monkeypatch, queued):
    _, sids, _ = characters.make_cast(conn, 1)
    assert _draw(conn, tmp_path, monkeypatch, sids)["made"] == 5
    assert characters.next_step(conn, 1)["step"] == "pick"
    assert characters.set_sheet_status(conn, _sheet_of(conn, "Goofy Mango"), "approved") == ""
    assert characters.for_product(conn, 1)["name"] == "Goofy Mango" and characters.next_step(conn, 1)["step"] == "ready"
    assert [c["name"] for c in characters.listing(conn, 1) if c["in_use"]] == ["Goofy Mango"]

    _shot(conn, 1, "cross_section_macro", tmp_path)
    _scene_for(conn, tmp_path, "Goofy Mango")
    fake = _Claude(SCRIPT)
    s = S.write(conn, P, "fruit_characters", client=fake)
    assert s["character"]["name"] == "Goofy Mango" and s["character"]["voice"] == "Luis"
    assert "Goofy Mango" in fake.sent[0]["messages"][0]["content"]
    assert {ln["shot_type"] for ln in s["lines"]} == {"character_scene"}            # animated to the end

    # picking another approved look switches the character; its own scene shot is needed (Goofy's isn't reused)
    characters.set_sheet_status(conn, _sheet_of(conn, "Grumpy Mango"), "approved")
    assert characters.for_product(conn, 1)["name"] == "Grumpy Mango"
    fake = _Claude(SCRIPT)
    with pytest.raises(ValueError, match="Make the Fruit character scene shot for Grumpy Mango first"):
        S.write(conn, P, "fruit_characters", client=fake)
    assert fake.sent == []
    # and back again: Goofy is still approved, one click makes it the one in use
    characters.set_sheet_status(conn, _sheet_of(conn, "Goofy Mango"), "approved")
    assert S.write(conn, P, "fruit_characters", client=_Claude(SCRIPT))["character"]["name"] == "Goofy Mango"


def test_scene_shot_is_made_for_the_character_in_use(conn, tmp_path, monkeypatch, queued):
    _, sids, _ = characters.make_cast(conn, 1)
    _draw(conn, tmp_path, monkeypatch, sids)
    characters.set_sheet_status(conn, _sheet_of(conn, "Sleepy Mango"), "approved")
    cid = characters.for_product(conn, 1)["id"]
    monkeypatch.setattr("creative_intelligence.budget.caps", lambda conn: {"video_job": 1000})
    vid, why, reused = shots.create(conn, 1, "character_scene")
    assert why == "" and not reused
    stamp = conn.execute("SELECT prompt_version FROM adstudio_videos WHERE id=?", (vid,)).fetchone()[0]
    assert f"+character:{cid}+" in stamp and queued[-1][0] == "shot"
    assert shots.create(conn, 1, "character_scene")[0] == vid       # already being made: not queued twice


@pytest.mark.parametrize("state, says", [
    ("none", "no fruit character yet. Open Video › Create › Characters and click Make characters"),
    ("draft", "no look sheet yet"),
    ("queued", "still being drawn"),
    ("failed", "failed (model busy)"),
    ("done", "click Use this one"),
])
def test_the_style_says_exactly_what_to_do_before_any_claude_call(conn, tmp_path, monkeypatch, queued, state, says):
    if state == "draft":                          # the prod case: a hand-made character, never given a look sheet
        characters.create(conn, 1, {"name": "Cherimoya", "personality": "a sweet one", "voice": "June"})
    elif state != "none":
        _, sids, _ = characters.make_cast(conn, 1)
        if state != "queued":
            _draw(conn, tmp_path, monkeypatch, sids, fail=state == "failed")
    fake = _Claude(SCRIPT)
    with pytest.raises(ValueError) as e:
        S.write(conn, P, "fruit_characters", client=fake)
    assert says in str(e.value) and fake.sent == []
    if state == "draft":
        assert "Cherimoya has no look sheet yet" in str(e.value) and "Make look sheet, then Use this one" in str(e.value)
    vid, why, _ = shots.create(conn, 1, "character_scene")
    assert vid is None and says in why


def test_an_episode_needs_no_real_cut_shot(conn, tmp_path, monkeypatch, queued):
    """Feedback 2026-10-26: "full animation, not turn into real fruit": no cross-section or knife cut is needed."""
    _, sids, _ = characters.make_cast(conn, 1)
    _draw(conn, tmp_path, monkeypatch, sids)
    characters.set_sheet_status(conn, _sheet_of(conn, "Sunny Mango"), "approved")
    _scene_for(conn, tmp_path, "Sunny Mango")
    s = S.write(conn, P, "fruit_characters", client=_Claude(SCRIPT))
    assert {ln["shot_type"] for ln in s["lines"]} == {"character_scene"}


def test_routes_make_list_and_approve(conn, tmp_path, monkeypatch, queued):
    import creative_intelligence.webapp.app as app_module
    monkeypatch.setattr("creative_intelligence.webapp.common.db", lambda: get_connection(tmp_path / "data" / "t.db"))
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        d = c.get("/api/adstudio/characters?product_id=1").get_json()
        assert d["next"]["step"] == "make" and d["cast_size"] == 5 and d["scene"]["status"] == "none"
        assert d["estimate_cast"] == characters.estimate(conn, 5)
        r = c.post("/api/adstudio/characters/make", json={"product_id": 1}).get_json()
        assert len(r["characters"]) == 5 and len(r["queued"]) == 5
        assert c.post("/api/adstudio/characters/make", json={"product_id": 1}).status_code == 400
        _draw(conn, tmp_path, monkeypatch, r["queued"])
        d = c.get("/api/adstudio/characters?product_id=1").get_json()
        assert d["next"]["step"] == "pick" and all(ch["sheets"][0]["status"] == "done" for ch in d["characters"])
        assert c.post(f"/api/adstudio/character-sheet/{r['queued'][0]}", json={"status": "approved"}).get_json() == {"ok": True}
        d = c.get("/api/adstudio/characters?product_id=1").get_json()
        assert d["next"]["step"] == "ready" and [ch["name"] for ch in d["characters"] if ch["in_use"]] == ["Sunny Mango"]
        assert c.post(f"/api/adstudio/characters/{r['characters'][1]}/sheets", json={"n": 1}).get_json()["queued"]
