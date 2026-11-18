"""Video Studio V4 — winner decoder: top video ads by 90-day spend ranked by CPA, read-only Meta fetch
(video, or thumbnail + copy when the token can't read it), scene detection, transcript, Claude's structure
template. Meta, Replicate and Claude are faked; ffmpeg is real."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from creative_intelligence.adstudio import videotools as V, voices, winners as W
from tests.creative_intelligence.test_v1_animate import drain, env, png  # noqa: F401  (fixture)


def _ads(env):
    c = env.db()
    rows = [("a1", "Concept 174: Packaging - cherimoya", 12229, 153), ("a2", "Concept 86: Avatar Lychee Luke", 2532, 30),
            ("a3", "Concept 86: Avatar Lychee Luke", 2400, 10),                 # same concept: kept once
            ("a4", "Concept 267: Rainier Avatar", 2131, 26), ("a5", "Tiny test", 120, 2), ("a6", "Image ad", 9000, 90)]
    with c:
        for ad, name, spend, pu in rows:
            c.execute("INSERT INTO creatives (id, ad_id, ad_name, format) VALUES (?,?,?,?)",
                      (f"c{ad}", ad, name, "image" if ad == "a6" else "video"))
            for snap, factor in (("2026-10-01", 0.5), ("2026-10-19", 1.0)):        # the latest snapshot counts
                c.execute("INSERT INTO creative_performance (creative_id, ad_id, date_range, snapshot_date, spend, purchases,"
                          " revenue, roas) VALUES (?,?, '90d', ?,?,?,?,?)", (f"c{ad}", ad, snap, spend * factor, pu, spend * 2, 2.0))
        c.execute("INSERT INTO ad_daily_insights (ad_id, date, impressions, video_3s_views, thruplays) VALUES"
                  " ('a1', '2026-10-18', 1000, 400, 100)")
    return c


def test_candidates_are_top_spend_videos_ranked_by_cpa(env):
    c = _ads(env)
    cands = W.candidates(c)
    assert [x["ad_id"] for x in cands] == ["a1", "a4", "a2"]          # CPA 79.9, 82.0, 84.4; a3 same concept, a5 under $300
    assert cands[0]["cpa"] == pytest.approx(12229 / 153, abs=0.01) and cands[0]["hook_rate"] == 0.4 and cands[0]["hold_rate"] == 0.25


def test_video_id_from_any_creative_shape():
    assert W.video_id_of({"video_id": 1}) == "1"
    assert W.video_id_of({"object_story_spec": {"video_data": {"video_id": "2"}}}) == "2"
    assert W.video_id_of({"asset_feed_spec": {"videos": [{"video_id": "3"}]}}) == "3"
    assert W.video_id_of({}) is None


class FakeMeta:
    def __init__(self, video_ok=True):
        self.video_ok, self.calls = video_ok, []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url.rsplit("/", 1)[1], params["fields"]))
        if url.endswith("/a1"):
            body = {"name": "Concept 174", "creative": {"id": "cr1", "video_id": "v1", "thumbnail_url": "https://x/t.jpg",
                                                       "body": "Fruit you've never tasted", "title": "Shop now"}}
        elif self.video_ok:
            body = {"source": "https://video/v1.mp4", "length": 6.0}
        else:
            body = {"error": {"code": 10, "message": "Application does not have permission for this action"}}
        return SimpleNamespace(json=lambda: body)


def test_fetch_is_read_only_logged_and_falls_back_to_the_thumbnail(env, tmp_path, monkeypatch):
    monkeypatch.setenv("CI_RAW_DATA_DIR", str(tmp_path / "raw"))
    monkeypatch.chdir(tmp_path)
    got_files = []

    def dl(url, out):
        got_files.append(url)
        return png(out) if out.suffix == ".jpg" else V.test_clip(out, seconds=1)
    meta = FakeMeta(video_ok=False)
    got = W.fetch("a1", tmp_path, http=meta, download=dl)
    assert got["source"] == "thumbnail" and "permission" in got["error"] and got_files == ["https://x/t.jpg"]
    assert got["copy"]["body"] == "Fruit you've never tasted"
    assert [u for u, _ in meta.calls] == ["a1", "v1"]                              # GETs only
    raws = sorted(p.name for p in (tmp_path / "raw").iterdir())
    assert len(raws) == 2 and raws[0].startswith("winner_ad_a1_")                 # timestamped, never overwritten
    log = (tmp_path / "outputs" / "api.log").read_text()
    assert log.count(" GET https://graph.facebook.com/") == 2 and "access_token" not in log
    got = W.fetch("a1", tmp_path, http=FakeMeta(), download=dl)
    assert got["source"] == "video" and got["video_path"].endswith("winner_a1.mp4")


def test_scene_detection_finds_the_cuts(tmp_path, monkeypatch):
    monkeypatch.setenv("VIDEO_X264_PRESET", "ultrafast")
    a = V.test_clip(tmp_path / "a.mp4", seconds=2, color="0xd9708a")
    b = V.test_clip(tmp_path / "b.mp4", seconds=2, color="0x2050c0")
    (tmp_path / "l.txt").write_text(f"file '{a}'\nfile '{b}'\n")
    V.run(["-f", "concat", "-safe", "0", "-i", str(tmp_path / "l.txt"), "-c", "copy", str(tmp_path / "ab.mp4")])
    cuts = W.scenes(tmp_path / "ab.mp4")
    assert cuts[0] == 0.0 and any(1.8 < t < 2.2 for t in cuts)
    frames = W.frames_for(tmp_path / "ab.mp4", cuts, 4.0, tmp_path)
    assert len(frames) >= 3 and frames[-1][0] == pytest.approx(3.0)


TEMPLATE = {"summary": "A creator opens a box of rare fruit and cuts each one.", "hook": {"type": "unboxing", "what_happens": "Box opens",
            "spoken": "You've never seen fruit like this", "text": "Rare fruit haul"},
            "shots": [{"start": 0, "end": 2, "type": "unboxing", "what": "box opens", "presenter": True, "text": "Rare fruit haul"},
                      {"start": 2, "end": 4, "type": "cut_reveal", "what": "cherimoya halved", "presenter": False, "text": ""}],
            "presenter_share": 0.5, "product_first_s": 0.4, "price_first_s": None, "cta": {"text": "Shop now", "at_s": 3.5},
            "on_screen_text": ["Rare fruit haul"], "pacing": "a cut every 2 s", "why_it_works": ["box reveal in 1 s"],
            "how_to_rebuild": ["box_opening shot", "cross_section_macro", "narrator hook"]}


class FakeClaude:
    def __init__(self):
        self.sent = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kw):
        self.sent.append(kw)
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(TEMPLATE))],
                               usage=SimpleNamespace(input_tokens=20000, output_tokens=1500, cache_read_input_tokens=0))


def test_decode_end_to_end(env, tmp_path, monkeypatch):
    monkeypatch.setenv("CI_RAW_DATA_DIR", str(tmp_path / "raw"))
    c = _ads(env)
    fake = FakeClaude()
    monkeypatch.setattr("creative_intelligence.adstudio.chat._client", lambda **k: fake)
    silent = V.test_clip(tmp_path / "w0.mp4", seconds=3)
    V.run(["-i", str(silent), "-f", "lavfi", "-i", "sine=duration=3", "-shortest", "-c:v", "copy", "-c:a", "aac", str(tmp_path / "w.mp4")])
    monkeypatch.setattr(W, "fetch", lambda ad_id, out_dir, http=None, download=None: {
        "source": "video", "video_id": "v1", "copy": {"body": "Rare fruit", "title": "", "cta": "SHOP_NOW"},
        "video_path": str(tmp_path / "w.mp4")})
    monkeypatch.setattr(voices, "word_timings", lambda conn, audio, source="", source_id=0, client=None:
                        [{"word": "Rare", "start": 0.2, "end": 0.5}, {"word": "fruit", "start": 0.5, "end": 0.9}])
    r = env.client.post("/api/adstudio/winners/decode", json={}).get_json()
    assert len(r["queued"]) == 3
    assert env.client.post("/api/adstudio/winners/decode", json={}).get_json()["queued"] == []      # never twice
    drain(env.db)
    d = env.client.get("/api/adstudio/winners").get_json()
    t = next(x for x in d["templates"] if x["ad_id"] == "a1")
    assert t["status"] == "done" and t["source"] == "video" and t["template"]["hook"]["type"] == "unboxing"
    assert t["metrics"]["cpa"] == pytest.approx(79.93, abs=0.01) and t["duration_s"] == pytest.approx(3.0, abs=0.1)
    ask = fake.sent[0]["messages"][0]["content"][-1]["text"]
    assert "[0.2] Rare" in ask and "Rare fruit" in ask and "\"cpa\"" in ask
    assert sum(1 for b in fake.sent[0]["messages"][0]["content"] if b["type"] == "image") >= 2
    cost = c.execute("SELECT SUM(cost_usd) FROM model_calls WHERE source='winner'").fetchone()[0]
    assert cost > 0 and d["estimate"] > 0


def test_a_failed_fetch_is_recorded(env, monkeypatch):
    c = _ads(env)
    monkeypatch.setattr(W, "fetch", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("Meta: token expired")))
    W.queue(c, W.candidates(c)[:1])
    drain(env.db)
    t = W.listing(c)[0]
    assert t["status"] == "failed" and "token expired" in t["error"]


def test_upload_the_video_when_meta_wont_and_redecode(env, tmp_path, monkeypatch):
    import io
    monkeypatch.setenv("CI_RAW_DATA_DIR", str(tmp_path / "raw"))
    c = _ads(env)
    fake = FakeClaude()
    monkeypatch.setattr("creative_intelligence.adstudio.chat._client", lambda **k: fake)
    monkeypatch.setattr(W, "fetch", lambda *a, **k: {"source": "thumbnail", "video_id": "v1", "copy": {"body": "x"},
                                                     "thumb_path": png(tmp_path / "t.jpg"), "error": "(#10) no permission"})
    monkeypatch.setattr(voices, "word_timings", lambda *a, **k: [])
    W.queue(c, W.candidates(c)[:1])
    drain(env.db)
    t = W.listing(c)[0]
    assert t["source"] == "thumbnail" and "#10" in t["error"]
    assert "ONLY THE THUMBNAIL" in fake.sent[0]["messages"][0]["content"][-1]["text"]
    bad = env.client.post(f"/api/adstudio/winners/{t['id']}/video", data={"file": (io.BytesIO(b"nope"), "x.txt")})
    assert bad.status_code == 400
    clip = V.test_clip(tmp_path / "up.mp4", seconds=2).read_bytes()
    ok = env.client.post(f"/api/adstudio/winners/{t['id']}/video", data={"file": (io.BytesIO(clip), "ad.mp4")})
    assert ok.get_json() == {"ok": True}
    drain(env.db)
    t = W.listing(c)[0]
    assert t["status"] == "done" and t["source"] == "upload" and t["error"] == "" and t["duration_s"] == pytest.approx(2, abs=0.1)
    assert "ONLY THE THUMBNAIL" not in fake.sent[-1]["messages"][0]["content"][-1]["text"]
    assert env.client.post("/api/adstudio/winners/redecode").get_json() == {"queued": []}     # nothing partial left
