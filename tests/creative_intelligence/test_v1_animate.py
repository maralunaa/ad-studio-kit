"""Video Studio V1 — animate an approved still: 9:16 start still (never cropped), clip, video QA with a
first-to-last fruit-count check and one quiet retake, code-drawn text over the clip in 9:16 and 4:5,
gallery items with Asset IDs, export, caps. Replicate and Claude are faked; ffmpeg is real."""
from __future__ import annotations

import io
import json
import zipfile
from types import SimpleNamespace

import pytest
from PIL import Image

from creative_intelligence import db as D
from creative_intelligence import jobs, worker
from creative_intelligence.adstudio import videotools


def png(path, size=(800, 1000), color=(220, 120, 140)):
    """A picture with some texture, so it is the size of a real one (a flat colour compresses to nothing)."""
    noise = Image.effect_noise(size, 40).convert("RGB")
    Image.blend(Image.new("RGB", size, color), noise, 0.3).save(path, "PNG")
    return str(path)


class FakeClaude:
    """Video QA answers, one per call: (scores, fruit_count_first, fruit_count_last)."""

    def __init__(self, answers):
        self.answers, self.calls = list(answers), 0
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kw):
        self.calls += 1
        scores, first, last = self.answers.pop(0)
        keys = kw["output_config"]["format"]["schema"]["properties"]["scores"]["required"]
        body = {"scores": {k: scores.get(k, 5) for k in keys}, "fruit_count_first": first,
                "fruit_count_last": last, "subject_box": [0.3, 0.12, 0.7, 0.55], "reasoning": "checked"}
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(body))],
                               usage=SimpleNamespace(input_tokens=6000, output_tokens=200, cache_read_input_tokens=0))


@pytest.fixture
def env(tmp_path, monkeypatch):
    path = tmp_path / "data" / "creative_intel.db"
    monkeypatch.setenv("CI_DB_PATH", str(path))
    monkeypatch.setattr("creative_intelligence.config.CI_RENDER_OUTPUT_DIR", "")
    monkeypatch.setenv("FACTS_CHECK", "0")
    monkeypatch.setenv("VIDEO_X264_PRESET", "ultrafast")
    D._UP_TO_DATE.clear()
    D.init_db(path)
    factory = lambda: D.get_connection(path)
    monkeypatch.setattr("creative_intelligence.webapp.common.db", factory)
    src = png(tmp_path / "approved.png")                                   # a 4:5 approved image
    c = factory()
    with c:
        c.execute("INSERT INTO adstudio_products (id, name, product_type) VALUES (1, 'Honey Mango', 'fruit')")
        c.execute("INSERT INTO adstudio_images (id, product_id, title, status, image_path, aspect_ratio, review_status,"
                  " prompt) VALUES (1, 1, 'Hero', 'done', ?, '4:5', 'approved', 'Create a premium 4:5 product photograph.')", (src,))
        c.execute("INSERT INTO adstudio_images (id, product_id, title, status, image_path, aspect_ratio)"
                  " VALUES (2, 1, 'Unreviewed', 'done', ?, '4:5')", (src,))
        c.execute("INSERT INTO adstudio_hooks (id, product_id, headline, subhead, cta) VALUES"
                  " (1, 1, 'Custard, not fibre', 'Tree-ripened', 'Shop now'), (2, 1, 'Pink outside, gold inside', '', '')")
    calls = {"still": [], "video": []}

    def fake_image(prompt, refs, aspect, bucket="generation"):
        calls["still"].append((aspect, bucket, prompt[:40]))
        return png(tmp_path / f"still{len(calls['still'])}.png", size=(900, 900))   # wrong shape on purpose

    def fake_clip(prompt, start_image=None, duration=5, aspect_ratio="9:16", route="kling", audio=False, refs=None,
                  negative="", end_image=None):
        calls["video"].append((duration, aspect_ratio, prompt, route, audio))
        calls.setdefault("end", []).append(end_image)
        clip = videotools.test_clip(tmp_path / f"clip{len(calls['video'])}.mp4", size="480x480", seconds=2)
        model = {"veo": "google/veo-3.1-fast", "seedance": "bytedance/seedance-2.0"}.get(route, "kwaivgi/kling-v3-video")
        return str(clip), model, duration, audio
    monkeypatch.setattr("creative_intelligence.providers.replicate.generate_image", fake_image)
    monkeypatch.setattr("creative_intelligence.providers.replicate.generate_clip", fake_clip)
    monkeypatch.setattr("creative_intelligence.webapp.common._primary_photo_uris", lambda c, pid, limit=3: [])
    from creative_intelligence.webapp.app import app
    return SimpleNamespace(client=app.test_client(), db=factory, calls=calls, tmp=tmp_path)


def drain(db):
    for _ in range(10):
        c = db()
        job = jobs.claim(c, "test:1")
        c.close()
        if job is None:
            return
        worker.run_one(job, db)


def start(env, **over):
    body = {"product_id": 1, "source_kind": "image", "source_id": 1, "preset": "push_in", "duration": 5,
            "template": "bold_type", "hook_id": 1, **over}
    return env.client.post("/api/adstudio/animate", json=body)


def test_only_approved_images_and_the_dialog_offers_safe_moves_first(env):
    o = env.client.get("/api/adstudio/animate/options?product_id=1&source_kind=image&source_id=2").get_json()
    assert o["can_animate"] is False and "Approve" in o["reason"]
    assert start(env, source_id=2).status_code == 400
    o = env.client.get("/api/adstudio/animate/options?product_id=1&source_kind=image&source_id=1").get_json()
    assert o["can_animate"] and o["presets"][0]["safe"] and o["needs_still"]
    assert o["estimate"]["5"] == pytest.approx(0.18 + 0.84 + 0.05)


def test_animate_end_to_end(env, monkeypatch):
    monkeypatch.setattr("creative_intelligence.adstudio.chat._client", lambda **k: FakeClaude([({}, 1, 1)]))
    vid = start(env).get_json()["video_id"]
    assert [j["kind"] for j in env.client.get("/api/ops/jobs").get_json()["jobs"]] == ["animate"]
    drain(env.db)
    v = env.client.get(f"/api/adstudio/video/{vid}").get_json()
    assert v["status"] == "done" and v["qa"]["passed"] and v["attempts"] == 1
    # the start still: extended (outpaint, the source photo first), then padded to 9:16, never cropped
    aspect, bucket, prompt = env.calls["still"][0]
    assert (aspect, bucket) == ("9:16", "video") and prompt.startswith("The first image is a 9:16")
    c = env.db()
    still = c.execute("SELECT still_path FROM adstudio_videos WHERE id=?", (vid,)).fetchone()[0]
    assert Image.open(still).size == (1080, 1920)
    # the clip: from the motion rules, then both sizes with the hook drawn by code
    assert "one continuous shot" in env.calls["video"][0][2] and "Do not" not in env.calls["video"][0][2]  # motion rules v3
    sizes = {(r["aspect"]): videotools.probe(r["output_path"]) for r in
             c.execute("SELECT * FROM adstudio_video_renders WHERE video_id=?", (vid,))}
    assert (sizes["9:16"]["width"], sizes["9:16"]["height"]) == (1080, 1920)
    assert (sizes["4:5"]["width"], sizes["4:5"]["height"]) == (1080, 1350)
    kinds = dict(c.execute("SELECT kind, SUM(cost_usd) FROM model_calls WHERE source='video' GROUP BY kind").fetchall())
    assert kinds["video-still"] == 0.18 and kinds["video"] == 0.84 and v["spent_usd"] < 5
    # in the gallery with Asset IDs, then in the launch kit as .mp4 with the video columns
    g = env.client.get("/api/adstudio/gallery?product_id=1&type=video").get_json()
    assert g["counts"]["type"]["video"] == 2 and all(i["asset"]["asset_id"].startswith(f"EXB_v{vid}_") for i in g["items"])
    assert {i["asset"]["concept_id"] for i in g["items"]} == {f"V{vid}"}
    rid = g["items"][0]["id"]
    assert env.client.post(f"/api/adstudio/video-render/{rid}/review", json={"action": "ready"}).get_json()["ok"]
    z = zipfile.ZipFile(io.BytesIO(env.client.get("/api/adstudio/export-gallery?product_id=1&ready=1").data))
    files = z.namelist()
    assert len(files) == 2 and files[0].endswith(".mp4")
    row = z.read("manifest.csv").decode().splitlines()
    assert row[0].startswith("file,meta_ad_name,type,style,length_s,size") and ",Videos,animate,5," in row[1]
    assert env.client.get(f"/api/adstudio/video-render/{rid}/file").status_code == 200


def test_a_clip_whose_fruit_count_changes_is_retaken_once(env, monkeypatch):
    fake = FakeClaude([({}, 2, 3), ({}, 2, 2)])                    # first clip: a third mango appears
    monkeypatch.setattr("creative_intelligence.adstudio.chat._client", lambda **k: fake)
    vid = start(env).get_json()["video_id"]
    drain(env.db)
    v = env.client.get(f"/api/adstudio/video/{vid}").get_json()
    assert v["attempts"] == 2 and v["qa"]["passed"] and fake.calls == 2
    assert "Fruit count changed from 2 to 3" in env.calls["video"][1][2]   # the retake is told why
    assert len(env.calls["still"]) == 1                                     # the still is made once


def test_never_more_than_one_retake(env, monkeypatch):
    bad = ({"motion_artefacts": 1}, 1, 1)
    monkeypatch.setattr("creative_intelligence.adstudio.chat._client", lambda **k: FakeClaude([bad, bad, bad]))
    vid = start(env).get_json()["video_id"]
    drain(env.db)
    v = env.client.get(f"/api/adstudio/video/{vid}").get_json()
    assert v["status"] == "done" and v["attempts"] == 2 and not v["qa"]["passed"] and len(env.calls["video"]) == 2
    assert "did not pass QA" in v["message"]
    g = env.client.get("/api/adstudio/gallery?product_id=1&type=video&score=qa_rejected").get_json()
    assert len(g["items"]) == 2                                             # kept, under Rejected by QA


def test_more_headlines_cost_nothing(env, monkeypatch):
    monkeypatch.setattr("creative_intelligence.adstudio.chat._client", lambda **k: FakeClaude([({}, 1, 1)]))
    vid = start(env).get_json()["video_id"]
    drain(env.db)
    c = env.db()
    before = c.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0]
    r = env.client.post(f"/api/adstudio/video/{vid}/versions", json={"template": "minimal", "hook_ids": [1, 2]}).get_json()
    assert r["versions"] == 4
    drain(env.db)
    assert c.execute("SELECT COUNT(*) FROM adstudio_video_renders WHERE video_id=?", (vid,)).fetchone()[0] == 6
    assert c.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == before


def test_video_caps(env):
    assert env.client.post("/api/ops/caps", json={"key": "video_job", "usd": 0.5}).status_code == 200
    r = start(env)
    assert r.status_code == 400 and "video job cap" in r.get_json()["error"]
    env.client.post("/api/ops/caps", json={"key": "video_job", "usd": 25})
    env.client.post("/api/ops/caps", json={"key": "video_day", "usd": 0})
    assert "Daily cap reached" in start(env).get_json()["error"]


def test_text_layer_is_transparent_except_the_copy(tmp_path):
    from creative_intelligence.adstudio.render_template import render_layer_to
    frame = png(tmp_path / "frame.png", size=(540, 960), color=(30, 60, 30))
    out = tmp_path / "layer.png"
    drawn = render_layer_to(frame, str(out), "bold_type", {"headline": "Custard, not fibre", "cta": "Shop now"})
    layer = Image.open(out)
    assert layer.mode == "RGBA" and layer.size == (540, 960) and drawn["headline"] == "Custard, not fibre"
    alpha = layer.getchannel("A")
    assert alpha.getextrema() == (0, 255) and alpha.getpixel((5, 5)) == 0     # see-through where there is no text


def test_a_restart_closes_a_half_made_clip(env, monkeypatch):
    vid = start(env).get_json()["video_id"]
    c = env.db()
    jobs.claim(c, "dead:1")
    jobs.recover(c)                                                # the first time: made again
    assert c.execute("SELECT status FROM adstudio_videos WHERE id=?", (vid,)).fetchone()[0] == "queued"
    jobs.claim(c, "dead:2")
    out = jobs.recover(c)                                          # cut off again: closed
    assert out["failed"] == 1
    assert c.execute("SELECT status FROM adstudio_videos WHERE id=?", (vid,)).fetchone()[0] == "failed"


# ── product-aware framing and text (after the first real clips put the headline on the mango) ──
def test_4x5_window_keeps_the_product_and_crops_only_background():
    win = videotools.window_for([0.3, 0.12, 0.7, 0.55], 1080, 1920, "4:5")
    assert win == (0, 1350)                                        # product near the top: window from the top
    top, h = videotools.window_for([0.3, 0.5, 0.7, 0.9], 1080, 1920, "4:5")
    assert top <= 0.47 * 1920 and top + h >= 0.93 * 1920           # product low: window moves down, all of it in view
    assert videotools.window_for([0.1, 0.05, 0.9, 0.95], 1080, 1920, "4:5") is None   # too tall: pad instead
    assert videotools.window_for(None, 1080, 1920, "4:5") is None


def test_text_swaps_sides_when_it_would_cover_the_product():
    from creative_intelligence.adstudio.animate import choose_layout, overlap
    top_fruit = [0.25, 0.10, 0.75, 0.50]
    assert overlap("bold_type", top_fruit, 1080, 1920) > 0.12
    chosen, note = choose_layout("bold_type", top_fruit, 1080, 1920)        # whichever covers the fruit least
    assert overlap(chosen, top_fruit, 1080, 1920) < overlap("bold_type", top_fruit, 1080, 1920)
    assert note == "Text stacked below the product"                         # headline, line and button together
    mango = [0.05, 0.30, 0.95, 0.80]                                       # feedback 2026-10-30: CTA on the fruit
    assert choose_layout("bold_type", mango, 1080, 1920)[1] == "Text stacked above the product"
    low_fruit = [0.25, 0.66, 0.75, 0.95]       # in the bottom 35% of 9:16, where no text goes (playbook safe zone)
    assert choose_layout("bold_type", low_fruit, 1080, 1920) == ("bold_type", "")
    assert choose_layout("bold_type", None, 1080, 1920) == ("bold_type", "")


def test_renders_use_the_product_box(env, monkeypatch):
    monkeypatch.setattr("creative_intelligence.adstudio.chat._client", lambda **k: FakeClaude([({}, 1, 1)]))
    vid = start(env).get_json()["video_id"]
    drain(env.db)
    c = env.db()
    assert json.loads(c.execute("SELECT subject_box FROM adstudio_videos WHERE id=?", (vid,)).fetchone()[0]) == [0.3, 0.12, 0.7, 0.55]
    rows = {r["aspect"]: dict(r) for r in c.execute("SELECT * FROM adstudio_video_renders WHERE video_id=?", (vid,))}
    assert rows["9:16"]["template"] == "bold_type" and rows["9:16"]["note"] == "Text stacked below the product"
    assert videotools.probe(rows["4:5"]["output_path"])["height"] == 1350
    # in 4:5 this fruit fills most of the frame: the stack covers it least
    assert rows["4:5"]["note"].startswith("Text stacked")
    g = env.client.get("/api/adstudio/gallery?product_id=1&type=video").get_json()
    assert sorted(i["render_note"][:12] for i in g["items"]) == ["Text stacked", "Text stacked"]



def test_outpaint_keeps_the_approved_photo_pixel_for_pixel_with_room_on_top(tmp_path):
    from creative_intelligence.adstudio.animate import _headroom_canvas, _restore_photo
    photo = png(tmp_path / "photo.png", size=(1080, 1350), color=(200, 60, 90))
    canvas = Image.open(_headroom_canvas(photo, tmp_path / "canvas.png"))
    assert canvas.size == (1080, 1920) and canvas.getpixel((540, 100)) == (128, 128, 128)     # empty band on top
    filled = tmp_path / "filled.png"
    Image.new("RGB", (1080, 1920), (10, 200, 10)).save(filled)          # the model "changed everything"
    kept = Image.open(_restore_photo(str(filled), photo))
    src = Image.open(photo)
    assert kept.getpixel((540, 1900)) == src.getpixel((540, 1330))      # the photo is back, exactly
    assert kept.getpixel((540, 100)) == (10, 200, 10)                   # the new band is the model's


def test_retry_every_failed_job(env):
    """Feedback 2026-10-30: "rerun the failed jobs … there should also be a button for that"."""
    from datetime import datetime
    c = env.db()
    now = datetime.utcnow().isoformat(timespec="seconds")
    with c:
        for kind in ("shot", "styled", "image_run", "ig_sync"):
            c.execute("INSERT INTO jobs (kind, payload_json, status, attempts, max_attempts, run_after, created_at,"
                      " finished_at, error) VALUES (?, '{}', 'failed', 1, 1, ?, ?, ?, 'boom')", (kind, now, now, now))
    r = env.client.post("/api/ops/jobs/retry-failed").get_json()
    assert r["queued"] == 2
    assert {x[0] for x in c.execute("SELECT kind FROM jobs WHERE status='queued' AND error IS NULL")} >= {"shot", "styled"}
