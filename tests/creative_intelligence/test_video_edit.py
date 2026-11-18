"""Video › Edit (feedback 2026-10-26): change a finished video's words, shots, voice or music, or ask for a change,
and get a new version that reuses what didn't change. Real ffmpeg; Claude, voices and the presenter are faked."""
from __future__ import annotations

import json

from creative_intelligence.adstudio import styles, video_edit
from tests.creative_intelligence.test_e2e_every_video_format import drain, fake_script, world, _write  # noqa: F401
from tests.creative_intelligence.test_v1_animate import env, png  # noqa: F401


def _made(w, style="fruit_story"):
    s = _write(w, style)["script"]
    r = w.client.post("/api/adstudio/styles", json={"product_id": 1, "script": s, "voice": "Wade", "presenter_id": 1,
                                                     "hooks": 1, "lengths": ["full"], "aspects": ["9:16"],
                                                     "captions": ["bold"]}).get_json()
    drain(w.db)
    return r["video_id"]


def test_edit_lists_loads_rewrites_and_makes_a_new_version(world, monkeypatch):
    vid = _made(world)
    lst = world.client.get("/api/adstudio/video-edit?product_id=1").get_json()["videos"]
    assert [v["id"] for v in lst] == [vid] and lst[0]["render_id"]
    v = world.client.get(f"/api/adstudio/video-edit/{vid}").get_json()
    assert v["script"]["lines"] and v["shots"] and "Wade" in v["voices"]
    asked = []
    monkeypatch.setattr(styles, "_ask", lambda client, ask: asked.append(ask) or fake_script(client, ask.split(
        "THE CURRENT SCRIPT")[0] + "SHOTS IN THE LIBRARY" + ask.split("SHOTS (use only these):")[1] + "\nplus 1 hooks"))
    r = world.client.post(f"/api/adstudio/video-edit/{vid}/revise", json={"change": "more playful"}).get_json()
    assert "CHANGE IT LIKE THIS: more playful" in asked[0] and r["script"]["change"] == "more playful"
    s = r["script"]
    s["lines"][2]["text"] = "So little acid"
    c = world.client.post(f"/api/adstudio/video-edit/{vid}/check", json={"script": s}).get_json()
    assert c["estimate"]["variants"] == 1 and c["problems"] == []
    n = world.client.post(f"/api/adstudio/video-edit/{vid}/version", json={"script": s, "settings": {"voice": "Wade"},
                                                                           "new_takes": [s["lines"][0]["shot_type"]]}).get_json()
    assert n.get("video_id"), n
    drain(world.db)
    conn = world.db()
    row = dict(conn.execute("SELECT status, copy_json FROM adstudio_videos WHERE id=?", (n["video_id"],)).fetchone())
    st = json.loads(row["copy_json"])
    assert row["status"] == "done" and st["parent_id"] == vid and st["own_shots"]          # the fresh take was used
    assert conn.execute("SELECT status FROM adstudio_videos WHERE id=?", (vid,)).fetchone()[0] == "done"   # original stays
    assert {v["id"] for v in video_edit.editable(conn, 1)} == {vid, n["video_id"]}
