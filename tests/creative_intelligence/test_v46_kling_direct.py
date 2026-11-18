"""V46 (2026-11-02): Kling clips go to Kling's own API first (about half the price), Replicate's Kling if it fails.
Kling's API is mocked: tests never call it."""
from __future__ import annotations

import pytest

from creative_intelligence import budget, config
from creative_intelligence.providers import kling, replicate as R


@pytest.fixture
def fake_kling(monkeypatch):
    sent, state = [], {"busy": 0, "status": ["processing", "succeeded"]}
    monkeypatch.setattr(config, "KLING_API_KEY", "test-key")
    monkeypatch.setattr(config, "CI_REPLICATE_API_KEY", "r8_test")
    monkeypatch.setattr(kling, "POLL_S", 0)
    monkeypatch.setattr(kling.time, "sleep", lambda s: None)
    monkeypatch.setattr(kling, "_log", lambda line: None)

    def call(method, path, body=None):
        sent.append((method, path, body))
        if method == "POST":
            if state["busy"]:
                state["busy"] -= 1
                raise kling.KlingError("1303: parallel task over resource pack limit")
            return {"code": 0, "data": {"id": "t1", "status": "submitted"}}
        st = state["status"].pop(0)
        return {"code": 0, "data": [{"id": "t1", "status": st, "message": "risk control" if st == "failed" else "",
                                     "outputs": [{"type": "video", "url": "https://kling/out.mp4", "duration": "5"}]}]}
    monkeypatch.setattr(kling, "_call", call)
    monkeypatch.setattr(budget, "check_now", lambda *a, **k: None)
    monkeypatch.setattr(R, "estimate", lambda *a, **k: 0.1)
    return sent, state


def test_a_kling_shot_goes_to_kling_direct(fake_kling):
    sent, _ = fake_kling
    url, used, secs, sound = R.generate_clip("The mango turns slowly", "data:image/png;base64,QUJD", 5, route="kling",
                                             negative="text, logos", end_image="data:image/png;base64,REVG")
    assert (url, used, secs, sound) == ("https://kling/out.mp4", "kling-direct/kling-3.0", 5, False)
    method, path, body = sent[0]
    assert path == "/image-to-video/kling-3.0" and body["settings"] == {"resolution": "720p", "duration": 5,
                                                                         "audio": "off", "multi_shot": False}
    assert body["contents"][0]["text"].endswith("Avoid: text, logos.")
    assert body["contents"][1] == {"type": "first_frame", "url": "QUJD"}            # plain Base64, no data: head
    assert body["contents"][2] == {"type": "last_frame", "url": "REVG"}


def test_its_parallel_limit_waits_and_a_failure_falls_back_to_replicate(fake_kling, monkeypatch):
    sent, state = fake_kling
    state["busy"] = 2
    assert R.generate_clip("x", "data:image/png;base64,QUJD", 5, route="kling")[1] == "kling-direct/kling-3.0"
    assert sum(1 for m, *_ in sent if m == "POST") == 3
    state["status"] = ["failed"]
    monkeypatch.setattr(R, "run", lambda model, payload, units=1, bucket="": "https://replicate/out.mp4")
    url, used, *_ = R.generate_clip("x", "data:image/png;base64,QUJD", 5, route="kling")
    assert url == "https://replicate/out.mp4" and used == "kwaivgi/kling-v3-video"


def test_without_a_key_nothing_changes(monkeypatch):
    monkeypatch.setattr(config, "KLING_API_KEY", "")
    monkeypatch.setattr(config, "CI_REPLICATE_API_KEY", "r8_test")
    monkeypatch.setattr(budget, "check_now", lambda *a, **k: None)
    monkeypatch.setattr(R, "estimate", lambda *a, **k: 0.1)
    monkeypatch.setattr(R, "run", lambda model, payload, units=1, bucket="": "https://replicate/out.mp4")
    assert R.generate_clip("x", "data:image/png;base64,QUJD", 5, route="kling")[1] == "kwaivgi/kling-v3-video"


def test_kling_direct_is_priced_at_about_half():
    from creative_intelligence.adstudio import costs
    from creative_intelligence.db import get_connection, init_db
    init_db()
    c = get_connection()
    assert costs.price(c, "kling-direct/kling-3.0") * 2 == pytest.approx(costs.price(c, "kwaivgi/kling-v3-video"))


def test_estimates_quote_kling_direct_when_its_key_is_set(monkeypatch):
    monkeypatch.setattr(config, "KLING_API_KEY", "k")
    assert R.price_key("kwaivgi/kling-v3-video", True, True) == "kling-direct/kling-3.0:pro:audio"
    monkeypatch.setattr(config, "KLING_API_KEY", "")
    assert R.price_key("kwaivgi/kling-v3-video", False) == "kwaivgi/kling-v3-video"
    assert R.price_key("google/veo-3.1-fast", True) == "google/veo-3.1-fast:audio"


def test_kling_avatar_sends_face_and_voice_and_reads_the_result(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "KLING_API_KEY", "k")
    monkeypatch.setattr(kling, "POLL_S", 0)
    monkeypatch.setattr(kling.time, "sleep", lambda s: None)
    monkeypatch.setattr(kling, "_log", lambda line: None)
    sent = []

    def call(method, path, body=None):
        sent.append((method, path, body))
        if method == "POST":
            return {"code": 0, "data": {"task_id": "a1", "task_status": "submitted"}}
        return {"code": 0, "data": {"task_status": "succeed", "final_unit_deduction": "3",
                                    "task_result": {"videos": [{"url": "https://kling/a.mp4", "duration": "4.2"}]}}}
    monkeypatch.setattr(kling, "_call", call)
    wav = tmp_path / "v.wav"
    wav.write_bytes(b"RIFF1234")
    url, secs = kling.avatar("data:image/png;base64,RkFDRQ==", str(wav), "talks warmly")
    assert (url, secs) == ("https://kling/a.mp4", 4.2)
    m, path, body = sent[0]
    assert path == "/v1/videos/avatar/image2video" and body["image"] == "RkFDRQ==" and body["mode"] == "std"
    assert body["sound_file"] == "UklGRjEyMzQ=" and sent[1][1] == "/v1/videos/avatar/image2video/a1"


def test_a_replicate_prediction_past_its_limit_is_cancelled(monkeypatch):
    cancelled = []

    class P:
        status, error, output = "processing", None, None

        def reload(self):
            pass

        def cancel(self):
            cancelled.append(1)
    from types import SimpleNamespace
    client = SimpleNamespace(predictions=SimpleNamespace(create=lambda **k: P()),
                             models=SimpleNamespace(predictions=SimpleNamespace(create=lambda **k: P())))
    monkeypatch.setattr(R.time, "sleep", lambda s: None)
    clock = iter(range(0, 10_000, 100))
    monkeypatch.setattr(R.time, "monotonic", lambda: next(clock))
    with pytest.raises(TimeoutError, match="cancelled"):
        R._run_limited(client, "kwaivgi/kling-avatar-v2", {}, 250)
    assert cancelled == [1]


def test_presenter_and_character_clips_go_to_kling_avatar(monkeypatch, tmp_path):
    from creative_intelligence.adstudio import talking
    from creative_intelligence.db import get_connection, init_db
    init_db()
    c = get_connection()
    monkeypatch.setattr(config, "KLING_API_KEY", "k")
    monkeypatch.setattr(budget, "check_now", lambda *a, **k: None)
    got = []
    monkeypatch.setattr(kling, "avatar", lambda img, audio, prompt="", mode="std": got.append(prompt) or ("https://k/a.mp4", 3.0))
    monkeypatch.setattr("creative_intelligence.storage.storage", lambda: type("S", (), {
        "put_url": staticmethod(lambda area, name, url: str(tmp_path / name)),
        "area_dir": staticmethod(lambda area: tmp_path)})())
    monkeypatch.setattr(talking.videotools, "probe_audio", lambda p: 3.0)
    monkeypatch.setattr("creative_intelligence.adstudio.store.to_data_uri", lambda p: "data:image/png;base64,QQ==")
    wav = tmp_path / "v.wav"
    wav.write_bytes(b"RIFF")
    out = talking.lipsync(c, str(tmp_path / "f.png"), wav, "lips", 1, "A cartoon mango talking")
    assert out.endswith("lips.mp4") and got == ["A cartoon mango talking"]
    assert talking.lipsync_estimate(c, "one two three") == round(
        __import__("creative_intelligence.adstudio.costs", fromlist=["price"]).price(c, kling.AVATAR)
        * talking.seconds_for("one two three"), 2)


def test_a_task_kling_never_starts_falls_back_in_minutes_and_pauses_kling(fake_kling, monkeypatch):
    """2026-11-02: avatar tasks sat in "submitted" for hours; videos waited 20 minutes a clip."""
    sent, state = fake_kling
    state["status"] = ["submitted"] * 100
    clock = iter(range(0, 100_000, 60))                     # each poll a minute later
    monkeypatch.setattr(kling.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(kling, "_paused", {})
    monkeypatch.setattr(R, "run", lambda model, payload, units=1, bucket="": "https://replicate/out.mp4")
    url, used, *_ = R.generate_clip("x", "data:image/png;base64,QUJD", 5, route="kling")
    assert used == "kwaivgi/kling-v3-video"                       # the fallback, after ~3 minutes not 20
    assert sum(1 for m, p, _ in sent if m == "GET") <= 5
    assert kling.paused("video") and not kling.enabled("video") and kling.enabled("avatar")


def test_a_stalled_kling_avatar_sends_presenter_clips_to_omnihuman(monkeypatch, tmp_path):
    from creative_intelligence.adstudio import talking
    from creative_intelligence.db import get_connection, init_db
    init_db()
    c = get_connection()
    monkeypatch.setattr(config, "KLING_API_KEY", "k")
    monkeypatch.setattr(kling, "_paused", {"avatar": kling.time.monotonic() + 600})
    monkeypatch.setattr(talking, "portrait", lambda conn, pid: ({"name": "Wade", "voice": "Wade"}, str(tmp_path / "f.png")))
    wav = tmp_path / "v.wav"
    wav.write_bytes(b"RIFF")
    monkeypatch.setattr(talking.voices, "speak", lambda *a, **k: (wav, 0.0))
    monkeypatch.setattr(talking.videotools, "probe_audio", lambda p: 3.0)
    monkeypatch.setattr("creative_intelligence.adstudio.store.to_data_uri", lambda p: "data:image/png;base64,QQ==")
    monkeypatch.setattr("creative_intelligence.storage.storage", lambda: type("S", (), {
        "put_url": staticmethod(lambda area, name, url: str(tmp_path / name)),
        "area_dir": staticmethod(lambda area: tmp_path)})())
    used = []
    monkeypatch.setattr(R, "run", lambda model, payload, units=1, bucket="": used.append(model) or "https://r/x.mp4")
    clip, secs, model = talking.make(c, 1, "Hi there, this is the mango.", "kling_avatar", "t1", 1)
    assert used == ["bytedance/omni-human-1.5"] and model == "bytedance/omni-human-1.5"
