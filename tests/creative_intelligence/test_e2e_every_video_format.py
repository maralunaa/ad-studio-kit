"""Every video format, end to end, both ways (feedback 2026-10-23: "have you tested all video variants and functions?").

One video: Write the script → Make (1 hook, 30 s, 9:16) → the worker makes any missing shots and the video.
Several at once: storyboards → approve → shots → one draft each → finals for one.
Real ffmpeg renders; Claude, Replicate, voices and the presenter are faked. A voice comes back as .wav (Kokoro), the
case that broke job 200. Nothing is set up by hand in Shots or Characters first: each format has to get there alone.
"""
from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest

from creative_intelligence import jobs
from creative_intelligence.adstudio import story, styles, talking, video_batch as VB, videotools as V, voices
from tests.creative_intelligence.test_v1_animate import env, png  # noqa: F401  (fixture)

ONE_VIDEO = [k for k in styles.catalog()]


class FakeClaude:
    """Answers by what is asked: video QA (always passes), claims (none), character bibles, anything else empty."""

    def __init__(self):
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))
        self.messages = self.beta.messages

    def create(self, **kw):
        props = (((kw.get("output_config") or {}).get("format") or {}).get("schema") or {}).get("properties") or {}
        if "scores" in props:
            keys = props["scores"]["required"]
            body = {"scores": {k: 5 for k in keys}, "fruit_count_first": 1, "fruit_count_last": 1,
                    "subject_box": [0.3, 0.12, 0.7, 0.55], "reasoning": "fine"}
            body = {k: v for k, v in body.items() if k in props or k == "scores"}
        elif "claims" in props:
            body = {"claims": []}
        else:
            body = {k: ([] if v.get("type") == "array" else 0 if v.get("type") in ("integer", "number")
                        else "" if v.get("type") == "string" else {}) for k, v in props.items()}
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(body))],
                               usage=SimpleNamespace(input_tokens=100, output_tokens=50, cache_read_input_tokens=0))


def fake_script(client, ask):
    """A script from the shots the prompt offers: narrator, presenter (when allowed), text, narrator."""
    menu = re.findall(r"^- ([a-z_]+): ", ask.split("SHOTS IN THE LIBRARY")[-1], re.M)
    n = int(re.search(r"plus (\d+) hooks", ask)[1])
    pick = (menu * 4)[:4]
    lines = [{"text": "Pink outside, gold inside", "kind": "narrator", "shot_type": pick[0], "short": True},
             {"text": "Here is why it tastes like custard", "kind": "presenter", "shot_type": pick[1], "short": True},
             {"text": "Almost no acidity", "kind": "text", "shot_type": pick[2], "short": False},
             {"text": "Order yours today", "kind": "narrator", "shot_type": pick[3], "short": True}]
    if "STYLE:" in ask and "supermarket" in ask.split("STYLE:")[1][:400].lower():
        lines[2]["compare"] = True                                     # Us vs supermarket: the split screen
    return {"title": "t", "hook_headline": "Pink outside", "cta": "Shop now", "_usage": SimpleNamespace(),
            "hooks": ["Pink outside, gold inside"] + [f"Hook {i}" for i in range(1, n)], "lines": lines}


@pytest.fixture
def world(env, monkeypatch):
    c = env.db()
    with c:
        c.execute("UPDATE adstudio_products SET shopify_url='https://testfruit.example/products/pem' WHERE id=1")
        for cat, text in (("ripeness", "Ready when it gives slightly to a gentle press."),
                          ("how_to_eat", "Cut the cheeks off either side of the stone, then score the flesh.")):
            c.execute("INSERT INTO facts (product_id, category, text) VALUES (1, ?, ?)", (cat, text))
        c.execute("INSERT INTO reviews (product_id, kind, text, author, source, posted_on, added_at)"
                  " VALUES (1, 'comment', 'Is it really pink inside?', 'Ana', 'meta_comment', '2026-10-01', 'now')")
        c.execute("INSERT INTO video_templates (ad_id, ad_name, status, template_json, created_at) VALUES ('1', 'Winner', 'done', ?, 'now')",
                  (json.dumps({"beats": ["hook", "proof", "cta"]}),))
        c.execute("INSERT INTO presenters (id, name, voice, status, created_at) VALUES (1, 'Wade', 'Wade', 'approved', '')")
    c.close()

    def speak(conn, text, name, out, source="", source_id=0, client=None, context=("", ""), style=""):
        f = out.with_suffix(".wav")                                       # Kokoro returns .wav
        V.run(["-f", "lavfi", "-i", "sine=frequency=300:duration=1.5", str(f)])
        return f, 0.002

    def make(conn, pid, line, method, out_name, source_id, client=None, face=None, action="", context=("", "")):
        made.append((face, action))
        from creative_intelligence.storage import storage
        clip = storage().area_dir("videos") / f"{out_name}.mp4"
        silent = V.test_clip(env.tmp / f"{out_name}_s.mp4", size="360x640", seconds=2)
        V.run(["-i", str(silent), "-f", "lavfi", "-i", "sine=duration=2", "-shortest", "-c:v", "copy", "-c:a", "aac",
               str(clip)])
        return str(clip), 2.0, "bytedance/omni-human-1.5"
    monkeypatch.setattr(voices, "speak", speak)
    made = world_made = []
    monkeypatch.setattr(talking, "make", make)

    def lipsync(conn, face, audio, out_name, source_id, prompt=""):      # the fruit character talking
        made.append((face, "lipsync"))
        return make(conn, 0, "", "omnihuman", out_name, source_id)[0]
    monkeypatch.setattr(talking, "lipsync", lipsync)
    monkeypatch.setattr(talking, "portrait", lambda conn, pid: ({"name": "Wade", "voice": "Wade"}, png(env.tmp / "face.png")))
    monkeypatch.setattr(talking, "table_scene", lambda conn, pr, pid: png(env.tmp / "table.png"))
    monkeypatch.setattr(talking, "cutout_face", lambda conn, pr: png(env.tmp / "cutout.png"))
    monkeypatch.setattr(voices, "word_timings", lambda conn, audio, source="", source_id=0, client=None:
                        voices.spread_words("Pink outside gold inside here is why", 4.0))
    monkeypatch.setattr("creative_intelligence.adstudio.chat._client", lambda **k: FakeClaude())
    monkeypatch.setattr(styles, "_ask", fake_script)
    monkeypatch.setattr(story, "WAIT_S", 0)
    monkeypatch.setattr(VB, "CHECK_EVERY_S", 0)
    monkeypatch.setattr("creative_intelligence.budget.check", lambda *a, **k: None)
    env.made = world_made
    return env


def drain(db, limit=200):
    """Work the queue until it is empty (jobs that re-queue themselves to wait, or are queued for later, run at once:
    a batch whose shots are all in the library has only its delayed 'advance' step left)."""
    from creative_intelligence import worker
    for _ in range(limit):
        c = db()
        with c:
            c.execute("UPDATE jobs SET run_after='' WHERE status='queued'")
        job = jobs.claim(c, "test:1")
        c.close()
        if job is None:
            return
        worker.run_one(job, db)
    raise AssertionError("the queue never emptied")


def _write(w, style):
    body = {"product_id": 1, "style": style, "seconds": 30, "template_id": 1, "comment_id": 1}
    for _ in range(5):                                       # Fruit characters: the character is made first
        d = w.client.post("/api/adstudio/styles/script", json=body).get_json()
        if not d.get("preparing"):
            return d
        drain(w.db)
    return d


@pytest.mark.parametrize("style", ONE_VIDEO)
def test_one_video(world, style):
    d = _write(world, style)
    assert "script" in d, d
    s = d["script"]
    opts = {"hooks": 1, "lengths": ["full"], "aspects": ["9:16"], "captions": ["bold"]}
    e = world.client.post("/api/adstudio/styles/estimate", json={"product_id": 1, "script": s, "voice": "Wade",
                                                                  "presenter_id": 1, **opts}).get_json()
    assert e["estimate"]["variants"] == 1, e
    r = world.client.post("/api/adstudio/styles", json={"product_id": 1, "script": s, "voice": "Wade",
                                                         "presenter_id": 1, **opts}).get_json()
    assert r.get("video_id"), (r, s.get("claims"))
    drain(world.db)
    c = world.db()
    v = dict(c.execute("SELECT status, message FROM adstudio_videos WHERE id=?", (r["video_id"],)).fetchone())
    renders = c.execute("SELECT output_path FROM adstudio_video_renders WHERE video_id=? AND status='done'",
                        (r["video_id"],)).fetchall()
    assert v["status"] == "done" and len(renders) == 1, v
    assert V.probe(renders[0][0])["duration"] > 2
    if style not in styles.NO_OWN_SHOTS + tuple(styles.OWN_SHOTS_ONLY):   # its own shots, in its format's look
        own = c.execute("SELECT COUNT(*) FROM adstudio_videos WHERE style='shot' AND instr(prompt_version, ?) > 0",
                        (f":{style}+",)).fetchone()[0]
        assert own >= 1, style
    if style == "presenter_table":                                    # on camera the whole video, at the table
        assert all(face and face.endswith(".png") and "table_" in face for face, _ in world.made) and world.made
        assert {ln["kind"] for ln in s["lines"]} == {"presenter"}
    if style == "us_vs_supermarket":
        tl = json.loads(c.execute("SELECT timeline_json FROM adstudio_videos WHERE id=?", (r["video_id"],)).fetchone()[0])
        assert any(p.get("split") for p in tl["picture"])


BATCH = [k for k in __import__("creative_intelligence.adstudio.video_batch", fromlist=["x"]).batch_styles()]


@pytest.mark.parametrize("style", BATCH)
def test_several_at_once(world, style):
    """One format per test, so CI runs them side by side (one test of all formats took 24 minutes alone)."""
    r = world.client.post("/api/adstudio/video-batch", json={"product_ids": [1], "styles": {style: 1}, "voice": "Wade",
                                                              "presenter_id": 1}).get_json()
    bid = r["batch_id"]
    drain(world.db)
    st = world.client.get(f"/api/adstudio/video-batch/{bid}").get_json()
    errors = {i["style"]: i["error"] for i in st["items"] if i["error"]}
    assert st["status"] == "planned" and not errors, errors
    a = world.client.post(f"/api/adstudio/video-batch/{bid}/approve", json={}).get_json()
    assert "error" not in a, a
    drain(world.db)
    st = world.client.get(f"/api/adstudio/video-batch/{bid}").get_json()
    errors = {i["style"]: i["error"] for i in st["items"] if i["error"]}
    if style == "animate":                                             # an animated still is made at approve, no draft
        assert not errors and st["items"][0]["final_id"]
        return
    assert st["status"] == "drafts" and not errors, (st["status"], errors)
    assert st["items"][0]["draft"]["status"] == "done", st["items"][0]["draft"]
    assert world.client.post(f"/api/adstudio/video-batch/{bid}/judge", json={"index": 0, "ok": True}).get_json()
    f = world.client.post(f"/api/adstudio/video-batch/{bid}/finals", json={}).get_json()
    assert f.get("started") == 1, f
    drain(world.db)
    c = world.db()
    left = c.execute("SELECT id, style, status, message FROM adstudio_videos WHERE status NOT IN ('done')").fetchall()
    assert not [dict(x) for x in left]
