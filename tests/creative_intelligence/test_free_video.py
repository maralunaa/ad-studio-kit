"""Free (feedback 2026-10-26): the prompt goes to the model as written, Seedance by default; one take in Review."""
from __future__ import annotations

from tests.creative_intelligence.test_v1_animate import FakeClaude, drain, env, png  # noqa: F401  (fixture)


def test_free_prompt_goes_to_seedance_as_written_and_lands_in_review(env, monkeypatch):
    monkeypatch.setattr("creative_intelligence.adstudio.chat._client", lambda **k: FakeClaude([({}, 1, 1)] * 2))
    e = env.client.post("/api/adstudio/free-video", json={"estimate": True}).get_json()
    assert e["estimate"] > 0 and "seedance" in e["models"]
    bad = env.client.post("/api/adstudio/free-video", json={"product_id": 1, "prompt": "x", "model": "kling"}).get_json()
    assert "start image" in bad["error"]
    r = env.client.post("/api/adstudio/free-video", json={"product_id": 1, "prompt": "A hand turns the mango slowly",
                                                          "seconds": 5}).get_json()
    drain(env.db)
    c = env.db()
    v = dict(c.execute("SELECT * FROM adstudio_videos WHERE id=?", (r["video_id"],)).fetchone())
    assert v["status"] == "done" and v["style"] == "free", v["message"]
    dur, aspect, prompt, route, audio = env.calls["video"][-1]
    assert route == "seedance" and prompt.startswith("A hand turns the mango slowly")       # as written
    rd = c.execute("SELECT template, note FROM adstudio_video_renders WHERE video_id=?", (r["video_id"],)).fetchone()
    assert rd["template"] == "free" and rd["note"].startswith("Free · ")
    r2 = env.client.post("/api/adstudio/free-video", json={"product_id": 1, "prompt": "Slow push in", "image_id": 1,
                                                           "model": "kling"}).get_json()
    drain(env.db)
    assert c.execute("SELECT status FROM adstudio_videos WHERE id=?", (r2["video_id"],)).fetchone()[0] == "done"
    assert env.calls["video"][-1][3] == "kling" and env.calls["video"][-1][2].startswith("Slow push in")


def test_free_clips_have_sound_and_go_to_15_seconds(env, monkeypatch):
    """2026-11-02: "we are missing the sound, also would like the option to make the video longer"."""
    monkeypatch.setattr("creative_intelligence.adstudio.chat._client", lambda **k: FakeClaude([({}, 1, 1)] * 2))
    e = env.client.post("/api/adstudio/free-video", json={"estimate": True, "product_id": 1}).get_json()
    assert 15 in e["seconds"]
    veo = env.client.post("/api/adstudio/free-video", json={"estimate": True, "model": "veo"}).get_json()
    assert max(veo["seconds"]) == 8
    r = env.client.post("/api/adstudio/free-video", json={"product_id": 1, "prompt": "Waves of juice", "seconds": 15}).get_json()
    drain(env.db)
    c = env.db()
    v = c.execute("SELECT duration_s, has_audio FROM adstudio_videos WHERE id=?", (r["video_id"],)).fetchone()
    assert v["duration_s"] == 15 and v["has_audio"] == 1 and env.calls["video"][-1][4] is True
    r = env.client.post("/api/adstudio/free-video", json={"product_id": 1, "prompt": "Quiet", "sound": False}).get_json()
    drain(env.db)
    assert env.calls["video"][-1][4] is False


def test_a_presenter_can_be_in_a_free_clip(env, monkeypatch):
    """2026-11-02: "on free prompt we should be able to use our cartoon characters, clay and presenters as well"."""
    from creative_intelligence.adstudio import free_video, talking
    c = env.db()
    with c:
        c.execute("INSERT INTO presenters (id, name, status, approved_look_id, voice, created_at) VALUES (9, 'Wade', 'approved', 1, 'Wade', 'now')")
    face = png(env.tmp / "face.png")
    monkeypatch.setattr(talking, "portrait", lambda conn, pid: ({"name": "Wade"}, face))
    assert {"key": "presenter:9", "label": "Wade (presenter)"} in free_video.cast_options(c, 1)
    bad = env.client.post("/api/adstudio/free-video", json={"product_id": 1, "prompt": "x", "cast": "presenter:9",
                                                           "model": "veo"}).get_json()
    assert "Seedance" in bad["error"]
    seen = []
    real = free_video.cast_picture
    monkeypatch.setattr(free_video, "cast_picture", lambda *a: seen.append(a[2]) or real(*a))
    r = env.client.post("/api/adstudio/free-video", json={"product_id": 1, "prompt": "Wade holds up the mango",
                                                         "cast": "presenter:9"}).get_json()
    drain(env.db)
    assert seen == ["presenter:9"]
    assert c.execute("SELECT status FROM adstudio_videos WHERE id=?", (r["video_id"],)).fetchone()[0] == "done"


def test_a_start_image_and_a_cast_together(env, monkeypatch):
    """2026-11-02: the cartoon mango waking up in the box photo: the character is put into the image, then animated."""
    from creative_intelligence.adstudio import free_video, talking
    c = env.db()
    with c:
        c.execute("INSERT INTO presenters (id, name, status, approved_look_id, voice, created_at) VALUES"
                  " (9, 'Wade', 'approved', 1, 'Wade', 'now')")
    face = png(env.tmp / "face.png")
    monkeypatch.setattr(talking, "portrait", lambda conn, pid: ({"name": "Wade"}, face))
    put = []
    monkeypatch.setattr(free_video, "put_in", lambda conn, vid, still, pic, key: put.append((pic, key)) or still)
    with_cast = env.client.post("/api/adstudio/free-video", json={"estimate": True, "product_id": 1, "image_id": 1,
                                                                 "cast": "presenter:9", "model": "kling"}).get_json()
    plain = env.client.post("/api/adstudio/free-video", json={"estimate": True, "product_id": 1, "image_id": 1,
                                                             "model": "kling"}).get_json()
    assert with_cast["estimate"] > plain["estimate"]
    r = env.client.post("/api/adstudio/free-video", json={"product_id": 1, "prompt": "He waves", "image_id": 1,
                                                         "cast": "presenter:9", "model": "kling"}).get_json()
    assert r.get("video_id"), r
    drain(env.db)
    assert put == [(face, "presenter:9")]
    assert c.execute("SELECT status FROM adstudio_videos WHERE id=?", (r["video_id"],)).fetchone()[0] == "done"


def test_a_cartoon_on_purpose_is_judged_as_one():
    from creative_intelligence.adstudio import free_video
    assert free_video.stylized('A cartoon mango wakes up inside the box', None)
    assert free_video.stylized('Slow push in', "character:3") and free_video.stylized('x', "clay:0")
    assert not free_video.stylized('A hand turns the mango slowly', "presenter:9")
