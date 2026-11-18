"""Video Studio V5 — AI presenters: three editable presets, portrait looks made in the background, one approved
face per presenter, a character sheet that keeps that face, talking-head clips (native speech or house voice +
lip-sync), every clip ai_person and QA-scored against the face. Replicate and Claude are faked; ffmpeg is real."""
from __future__ import annotations

import json

import pytest

from creative_intelligence.adstudio import presenters as P, talking as T, videotools as V, voices
from tests.creative_intelligence.test_v1_animate import drain, env, png  # noqa: F401  (fixture)


@pytest.fixture
def made(env, monkeypatch):
    calls = []

    def fake_image(prompt, refs, aspect, bucket="generation"):
        calls.append((prompt, len(refs), aspect))
        return png(env.tmp / f"look{len(calls)}.png", size=(540, 960))
    monkeypatch.setattr("creative_intelligence.providers.replicate.generate_image", fake_image)
    return calls


def test_one_presenter_per_voice_brand_narrators_never_farmers(env):
    c = env.db()
    ps = P.listing(c)
    assert [p["name"] for p in ps] == ["Wade", "Jack", "Luis", "June", "Ivy", "Tess"] and all(p["status"] == "draft" for p in ps)
    assert sorted(p["voice"] for p in ps) == sorted(v["name"] for v in voices.house())
    assert {p["name"] for p in ps if p["framing"] == "selfie"} == {"Jack", "June"}
    from creative_intelligence import prompts
    for framing in P.FRAMING.values():
        rule = prompts.text("presenter-look", look="x", setting="y", framing=framing)
        assert "job title" in rule and "No text" in rule and "mouth closed" in rule and "camera app" in rule


def test_looks_approval_and_a_sheet_that_keeps_the_face(env, made):
    c = env.db()
    pid = P.listing(c)[0]["id"]
    with pytest.raises(ValueError):
        P.make_sheet(c, pid)                                                   # no face yet
    r = env.client.post(f"/api/adstudio/presenters/{pid}/looks", json={"kind": "portrait"}).get_json()
    assert len(r["queued"]) == 2
    drain(env.db)
    looks = P.listing(c)[0]["looks"]
    assert all(l["status"] == "done" and l["has_image"] for l in looks) and made[0][1:] == (0, "9:16")
    assert "stubble beard" in made[0][0] and "farmhouse porch" in made[0][0] and "phone selfie" not in made[0][0]
    assert c.execute("SELECT COUNT(*) FROM model_calls WHERE source='presenter'").fetchone()[0] == 2
    lid = looks[0]["id"]
    assert env.client.post(f"/api/adstudio/presenter-look/{lid}", json={"status": "approved"}).get_json() == {"ok": True}
    p = P.listing(c)[0]
    assert p["status"] == "approved" and p["approved_look_id"] == lid
    env.client.post(f"/api/adstudio/presenters/{pid}/looks", json={"kind": "sheet"})
    drain(env.db)
    assert len(made) == 7 and all(n == 1 for _, n, _ in made[2:])               # every angle uses the face
    assert env.client.get(f"/api/adstudio/presenter-look/{lid}/image").status_code == 200
    assert P.update(c, pid, {"person": "someone else entirely"}) == "" and P.listing(c)[0]["status"] == "draft"
    assert "unknown voice" in P.update(c, pid, {"voice": "Nobody"})


def _approved(env, made):
    c = env.db()
    pid = P.listing(c)[0]["id"]
    P.make_looks(c, pid, 1)
    drain(env.db)
    P.set_look_status(c, P.listing(c)[0]["looks"][0]["id"], "approved")
    return c, pid


LINE = "This is the Honey mango: up to two pounds of fruit."


def test_talking_clips_need_an_approved_face_and_fit_the_method(env, made):
    c = env.db()
    pid = P.listing(c)[0]["id"]
    assert "Approve a look" in T.create(c, pid, 1, LINE, "omnihuman")[1]
    c, pid = _approved(env, made)
    long = " ".join(["word"] * 40)
    vid, why = T.create(c, pid, 1, long, "veo")                                  # too long for Veo: house-voice avatar
    assert vid and json.loads(c.execute("SELECT copy_json FROM adstudio_videos WHERE id=?", (vid,)).fetchone()[0])["method"] == "kling_avatar"
    assert T.fit("veo", long) == "kling_avatar" and T.fit("veo", LINE) == "veo" and T.estimate(c, "veo", long) == T.estimate(c, "kling_avatar", long)
    assert T.veo_seconds(LINE) == 6 and T.veo_seconds("Hi there.") == 4
    from creative_intelligence.adstudio.costs import price
    assert T.estimate(c, "veo", LINE) == pytest.approx(round(price(c, "google/veo-3.1-fast:audio") * 6, 2))
    assert T.estimate(c, "omnihuman", LINE) == pytest.approx(round(0.14 * T.seconds_for(LINE), 2))


def test_comparison_makes_three_ai_person_clips(env, made, monkeypatch):
    c, pid = _approved(env, made)
    clip = lambda name: str(V.test_clip(env.tmp / name, size="360x640", seconds=2))
    monkeypatch.setattr("creative_intelligence.providers.replicate.generate_clip",
                        lambda prompt, start_image=None, duration=5, aspect_ratio="9:16", route="kling", audio=False, **k:
                        (clip("veo.mp4"), "google/veo-3.1-fast", 8, True))
    ran = []
    monkeypatch.setattr("creative_intelligence.providers.replicate.run",
                        lambda model, payload, units=1, bucket="generation": ran.append((model, sorted(payload), bucket)) or clip(f"{len(ran)}.mp4"))
    monkeypatch.setattr(voices, "speak", lambda conn, text, name, out, source="", source_id=0, client=None, context=("", ""), style="":
                        (V.run(["-f", "lavfi", "-i", "sine=duration=3", str(out.with_suffix(".mp3"))]) and None or out.with_suffix(".mp3"), 0.002))
    seen = []
    monkeypatch.setattr("creative_intelligence.adstudio.video_qa.score", lambda *a, **k: seen.append(k) or {})
    r = env.client.post("/api/adstudio/talking", json={"presenter_id": pid, "product_id": 1, "line": LINE, "compare": True}).get_json()
    assert len(r["queued"]) == 3
    drain(env.db)
    rows = [dict(x) for x in c.execute("SELECT * FROM adstudio_videos WHERE style='talking' ORDER BY id")]
    assert [x["status"] for x in rows] == ["done"] * 3 and all(x["ai_person"] == 1 for x in rows)
    assert [m for m, _, _ in ran] == ["bytedance/omni-human-1.5", "kwaivgi/kling-avatar-v2"] and all(b == "video" for *_, b in ran)
    assert "mode" in ran[1][1] and "prompt" in ran[0][1]
    assert all(k.get("presenter") for k in seen) and len(seen) == 3
    clips = env.client.get("/api/adstudio/talking").get_json()["clips"]
    assert {x["method_label"] for x in clips} == {"Veo 3.1 Fast, its own voice", "OmniHuman 1.5 + house voice", "Kling Avatar + house voice"}
    assert env.client.get(f"/api/adstudio/talking/{rows[0]['id']}/file").status_code == 200
    veo_cost = c.execute("SELECT cost_usd FROM model_calls WHERE model='google/veo-3.1-fast'").fetchone()[0]
    from creative_intelligence.adstudio.costs import price
    assert veo_cost == pytest.approx(8 * price(c, "google/veo-3.1-fast:audio"))


def test_lines_with_unapproved_claims_are_refused(env, made, monkeypatch):
    c, pid = _approved(env, made)
    monkeypatch.setattr("creative_intelligence.adstudio.facts.check_copy",
                        lambda *a, **k: {"status": "flagged", "banned": [], "unlisted": [{"field": "line", "claim": "best mango"}]})
    r = env.client.post("/api/adstudio/talking", json={"presenter_id": pid, "product_id": 1, "line": "The best mango ever", "method": "veo"})
    assert r.status_code == 400 and "best mango" in r.get_json()["error"]


def test_old_wade_row_becomes_luis_and_the_new_presets_join(env):
    c = env.db()
    with c:
        c.execute("DELETE FROM presenters")
        c.execute("INSERT INTO presenters (id, name, role, voice, look, created_at) VALUES"
                  " (1, 'Wade', 'Fruit guide', 'Wade', 'a man, Latino || a kitchen', 'now')")
        c.execute("INSERT INTO presenter_looks (presenter_id, kind, label, prompt, status, path, created_at) VALUES"
                  " (1, 'portrait', 'Option 1', 'p', 'done', 'x.png', 'now')")
    from pathlib import Path
    sql = (Path(P.__file__).parents[1] / "migrations" / "0019_presenter_framing.sql").read_text().split("\n", 3)[-1]
    c.executescript(sql)
    ps = {p["name"]: p for p in P.listing(c)}
    assert ps["Luis"]["id"] == 1 and len(ps["Luis"]["looks"]) == 1 and ps["Luis"]["voice"] == "Luis"
    assert ps["Jack"]["framing"] == "selfie" and ps["Wade"]["framing"] == "portrait" and len(ps) == 6


def test_each_voice_can_be_heard_made_once(env, monkeypatch):
    made = []

    def speak(conn, text, name, out, source="", source_id=0, client=None):
        made.append((name, source))
        f = out.with_suffix(".mp3")
        f.write_bytes(b"ID3fake")
        return f, 0.002
    monkeypatch.setattr(voices, "speak", speak)
    for _ in range(2):
        r = env.client.get("/api/adstudio/voices/Jack/sample")
        assert r.status_code == 200 and r.data == b"ID3fake"
    assert made == [("Jack", "voice_sample")]                                    # kept after the first play
    assert env.client.get("/api/adstudio/voices/Nobody/sample").status_code == 400


def test_a_persona_brief_steers_the_script_and_keeps_the_face(env, monkeypatch):
    """Feedback 2026-11-04: the Farmer, the Fruit Nerd, the Chef, the Wellness Coach, the Grandparent."""
    c = env.db()
    pid = P.listing(c)[0]["id"]
    with c:
        c.execute("UPDATE presenters SET status='approved', approved_look_id=99 WHERE id=?", (pid,))
    assert P.update(c, pid, {"role": "The Farmer", "brief": "A sun-weathered grower; covers how the fruit is picked."}) == ""
    row = c.execute("SELECT status, approved_look_id, role FROM presenters WHERE id=?", (pid,)).fetchone()
    assert (row["status"], row["approved_look_id"], row["role"]) == ("approved", 99, "The Farmer")   # look unchanged
    block = P.brief_block(c, pid)
    assert "The Farmer" in block and "sun-weathered grower" in block and "never a medical claim" in block
    assert P.brief_block(c, None) == "" and P.brief_block(c, P.listing(c)[1]["id"]) == ""
    from creative_intelligence.adstudio import styles as S
    seen = {}
    monkeypatch.setattr(S, "_ask", lambda client, ask, *a, **k: seen.setdefault("ask", ask) and (_ for _ in ()).throw(RuntimeError("stop")))
    monkeypatch.setattr("creative_intelligence.adstudio.chat._client", lambda **k: object())
    try:
        S.write(c, {"id": 1, "name": "Honey Mango", "product_type": "fruit"}, "talking_head", presenter_id=pid, plan=True)
    except Exception as exc:
        seen["why"] = str(exc)
    assert "THE PRESENTER ON CAMERA" in seen.get("ask", ""), seen.get("why")
