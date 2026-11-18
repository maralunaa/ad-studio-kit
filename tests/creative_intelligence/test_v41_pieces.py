"""V41 (2026-11-02, "it takes ages and then it fails"): one failed clip no longer throws away the paid ones, a failed
clip gets one more round, and Replicate's own failures ("Prediction interrupted; please retry", ModelError) retry."""
from __future__ import annotations

import json

from creative_intelligence.adstudio import styles as S, talking
from tests.creative_intelligence.test_v1_animate import drain, env  # noqa: F401  (fixture)
from tests.creative_intelligence.test_v6_styles import SCRIPT, _env_pieces


def _start(env, monkeypatch, fail_times):
    made = _env_pieces(env, monkeypatch)
    real, calls = talking.make, {}

    def flaky(conn, pid, line, *a, **k):
        calls[line] = calls.get(line, 0) + 1
        if calls[line] <= fail_times.get(line, 0):
            raise RuntimeError("Prediction interrupted; please retry (code: PW)")
        return real(conn, pid, line, *a, **k)
    monkeypatch.setattr(talking, "make", flaky)
    c = env.db()
    s = {**json.loads(json.dumps(SCRIPT)), "style": "fruit_story", "claims": {"status": "ok"}}
    vid, why = S.create(c, 1, s, "Wade", 1, "omnihuman", S.variants(s, hooks=1, lengths=("full",), aspects=("9:16",)))
    assert vid, why
    drain(env.db)
    return c, vid, made, calls


def test_a_clip_that_fails_once_is_made_again_in_the_same_job(env, monkeypatch):
    last = SCRIPT["lines"][-1]["text"]
    c, vid, made, calls = _start(env, monkeypatch, {last: 1})
    v = c.execute("SELECT status, message FROM adstudio_videos WHERE id=?", (vid,)).fetchone()
    assert v["status"] == "done", v["message"]
    assert calls[last] == 2


def test_a_clip_that_keeps_failing_keeps_the_others(env, monkeypatch):
    last = SCRIPT["lines"][-1]["text"]
    c, vid, made, calls = _start(env, monkeypatch, {last: 9})
    v = c.execute("SELECT status, message, copy_json FROM adstudio_videos WHERE id=?", (vid,)).fetchone()
    assert v["status"] == "failed" and "failed twice" in v["message"]
    kept = json.loads(v["copy_json"])["pieces"]
    assert len(kept) >= 2 and all(p.get("audio") for p in kept.values())       # the made ones are saved for a retry


def test_replicate_retries_its_own_failures(monkeypatch):
    from creative_intelligence import budget, config
    from creative_intelligence.providers import replicate as R

    class ModelError(Exception):
        pass
    calls = []

    class Client:
        def __init__(self, **k):
            pass

        def run(self, model, input):
            calls.append(1)
            if len(calls) == 1:
                raise ModelError("")
            if len(calls) == 2:
                raise RuntimeError("Prediction interrupted; please retry (code: PW)")
            return "https://out/x.mp4"
    import sys
    from types import SimpleNamespace
    monkeypatch.setitem(sys.modules, "replicate", SimpleNamespace(Client=Client))
    monkeypatch.setattr(config, "CI_REPLICATE_API_KEY", "r8_test")
    monkeypatch.setattr(budget, "check_now", lambda *a, **k: None)
    monkeypatch.setattr(R, "estimate", lambda *a, **k: 0.1)
    monkeypatch.setattr(R.time, "sleep", lambda s: None)
    assert R.run("bytedance/omni-human-1.5", {}, bucket="video") == "https://out/x.mp4" and len(calls) == 3


def test_the_daily_cap_pauses_the_video_instead_of_failing_it(env, monkeypatch):
    from creative_intelligence import budget, jobs
    queued = []
    real_enqueue = jobs.enqueue
    monkeypatch.setattr(jobs, "enqueue", lambda conn, kind, payload=None, dedupe_key=None, max_attempts=1, delay_s=0:
                        queued.append((kind, delay_s)) if delay_s > 3600 else real_enqueue(conn, kind, payload, dedupe_key,
                                                                                            max_attempts, delay_s))

    def capped(*a, **k):
        raise budget.BudgetExceeded(budget.CAP_MESSAGE)
    c, vid, made, calls = _start(env, monkeypatch, {})
    # a fresh video whose clips hit the cap
    monkeypatch.setattr(talking, "make", capped)
    s = {**json.loads(json.dumps(SCRIPT)), "style": "fruit_story", "claims": {"status": "ok"},
         "hooks": ["A brand new hook nobody made yet."]}
    vid2, why = S.create(c, 1, s, "Wade", 1, "omnihuman", S.variants(s, hooks=1, lengths=("full",), aspects=("9:16",)))
    drain(env.db)
    v = c.execute("SELECT status, step FROM adstudio_videos WHERE id=?", (vid2,)).fetchone()
    assert v["status"] == "queued" and "Paused" in v["step"], dict(v)
    assert queued and queued[-1][0] == "styled"


def test_kling_waits_its_turn_and_its_pack_limit_retries(monkeypatch):
    import sys
    import threading
    import time as _t
    from types import SimpleNamespace
    from creative_intelligence import budget, config
    from creative_intelligence.providers import replicate as R
    now, peak, lock, calls = [0], [0], threading.Lock(), []

    class Client:
        def __init__(self, **k):
            pass

        def run(self, model, input):
            with lock:
                calls.append(1)
                if len(calls) == 1:
                    raise RuntimeError("Error generating video: parallel task over resource pack limit")
                now[0] += 1
                peak[0] = max(peak[0], now[0])
            _t.sleep(0.05)
            with lock:
                now[0] -= 1
            return "https://out/x.mp4"
    monkeypatch.setitem(sys.modules, "replicate", SimpleNamespace(Client=Client))
    monkeypatch.setattr(config, "CI_REPLICATE_API_KEY", "r8_test")
    monkeypatch.setattr(budget, "check_now", lambda *a, **k: None)
    monkeypatch.setattr(R, "estimate", lambda *a, **k: 0.1)
    monkeypatch.setattr(R, "_cool_down", lambda exc, n: 0)
    monkeypatch.setattr(R, "_wait_turn", lambda: None)
    monkeypatch.setattr(R, "_model_slots", {})
    ts = [threading.Thread(target=R.run, args=("kwaivgi/kling-v3-video", {}), kwargs={"bucket": "video"}) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert peak[0] <= R.MODEL_LIMITS["kwaivgi/kling-v3-video"] and len(calls) == 9     # the pack-limit one retried


def test_a_stopped_video_stays_stopped_and_pays_for_nothing(env, monkeypatch):
    """2026-11-02: a restarted job set a stopped video back to running and kept making clips."""
    from creative_intelligence.adstudio import animate
    made = _env_pieces(env, monkeypatch)
    c = env.db()
    s = {**json.loads(json.dumps(SCRIPT)), "style": "fruit_story", "claims": {"status": "ok"}}
    vid, why = S.create(c, 1, s, "Wade", 1, "omnihuman", S.variants(s, hooks=1, lengths=("full",), aspects=("9:16",)))
    assert vid, why
    animate.request_stop(c, vid)
    drain(env.db)
    v = c.execute("SELECT status, message FROM adstudio_videos WHERE id=?", (vid,)).fetchone()
    assert v["status"] == "failed" and v["message"] == "Stopped." and made == {"narrator": 0, "presenter": 0}
