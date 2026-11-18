"""S2d: the fruit character system. A bible (name, personality, house voice, do's and don'ts), AI look sheets that Mara
approves one of, character shots that reference the approved sheet (and are QA'd against it), and episodes that
stay animated to the end (no real fruit). Replicate and Claude are faked."""
from __future__ import annotations

import pytest

from creative_intelligence import prompts
from creative_intelligence.adstudio import characters, shots, styles as S
from tests.creative_intelligence.test_s2a_video_styles import _Claude, _shot, conn  # noqa: F401  (fixture)

P = {"id": 1, "name": "Rosy Sunshine Mango", "product_type": "fruit"}
SCRIPT = {"title": "t", "hook_headline": "Meet Pinky", "cta": "Shop now", "hooks": ["Hi, I'm Pinky."],
          "lines": [{"text": "Hi, I'm Pinky.", "kind": "narrator", "shot_type": "character_scene", "short": True},
                    {"text": "Want to see inside?", "kind": "presenter", "shot_type": "character_scene", "short": True}]}


def _approved(conn, tmp_path, monkeypatch):
    monkeypatch.setattr("creative_intelligence.budget.check", lambda *a, **k: None)
    monkeypatch.setattr("creative_intelligence.jobs.enqueue", lambda *a, **k: None)
    cid, why = characters.create(conn, 1, {"name": "Pinky", "personality": "a sunny, dramatic mango", "voice": "June",
                                           "dos": "be witty", "donts": "never mock customers"})
    assert why == ""
    ids = characters.make_sheets(conn, cid)
    sheet = tmp_path / "sheet.png"
    sheet.write_bytes(b"\x89PNG" + b"0" * 100)
    monkeypatch.setattr("creative_intelligence.webapp.common._primary_photo_uris", lambda *a, **k: [])
    monkeypatch.setattr("creative_intelligence.storage.storage",
                        lambda: type("S", (), {"put_url": lambda self, area, name, url: str(sheet)})())
    monkeypatch.setattr("creative_intelligence.adstudio.costs.log_image", lambda *a, **k: 0)
    from creative_intelligence.db import get_connection
    res = characters.process(ids, lambda: get_connection(tmp_path / "data" / "t.db"), generate=lambda *a, **k: "file://x")
    assert res["made"] == 2
    return cid, ids


def test_bible_is_validated(conn):
    assert "name and a personality" in characters.create(conn, 1, {"name": "", "voice": "June"})[1]
    assert "house voices" in characters.create(conn, 1, {"name": "P", "personality": "x", "voice": "Robot"})[1]


def test_nothing_is_usable_until_a_sheet_is_approved(conn, tmp_path, monkeypatch):
    cid, ids = _approved(conn, tmp_path, monkeypatch)
    assert characters.for_product(conn, 1) is None
    vid, why, _ = shots.create(conn, 1, "character_scene")
    assert vid is None and "Use this one" in why
    assert characters.set_sheet_status(conn, ids[0], "approved") == ""
    ch = characters.for_product(conn, 1)
    assert ch["name"] == "Pinky" and ch["sheet_path"].endswith("sheet.png")
    assert characters.set_sheet_status(conn, ids[0], "rejected") == "" and characters.for_product(conn, 1) is None


def test_sheet_prompt_and_card():
    t = prompts.text("character-sheet", character="Pinky", personality="sunny", product="Rosy Sunshine Mango", look="a grin")
    assert "Pinky" in t and "No text" in t and "${" not in t and "caricature" in t
    c = prompts.shot_cards()["character_scene"]
    assert c["character"] and "FIRST reference image is the approved" in c["qa_note"] and c["styles"] == ["fruit_characters"]


def test_episodes_need_the_character_and_stay_animated_to_the_end(conn, tmp_path, monkeypatch):
    _shot(conn, 1, "cross_section_macro", tmp_path)
    with pytest.raises(ValueError, match="no fruit character yet"):
        S.write(conn, P, "fruit_characters", client=_Claude(SCRIPT))
    cid, ids = _approved(conn, tmp_path, monkeypatch)
    characters.set_sheet_status(conn, ids[1], "approved")
    scene = _shot(conn, 1, "character_scene", tmp_path)
    with conn:                                   # a character shot belongs to the character it was made for
        conn.execute("UPDATE adstudio_videos SET prompt_version=? WHERE id=?", (f"shot:character_scene@v1+character:{cid}+x", scene))
    fake = _Claude(SCRIPT)
    s = S.write(conn, P, "fruit_characters", client=fake)
    assert "Pinky" in fake.sent[0]["messages"][0]["content"] and "never mock customers" in fake.sent[0]["messages"][0]["content"]
    # feedback 2026-10-26: "full animation, not turn into real fruit": every line on the character, the last too
    assert {ln["shot_type"] for ln in s["lines"]} == {"character_scene"} and s["lines"][-1]["kind"] == "narrator"
    assert s["character"] == {"id": cid, "name": "Pinky", "voice": "June"}
