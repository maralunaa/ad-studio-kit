"""Provider adapters. No live API."""
from __future__ import annotations


def test_org_key_names_its_workspace(monkeypatch):
    from creative_intelligence.providers import anthropic as A
    monkeypatch.delenv("ANTHROPIC_WORKSPACE_ID", raising=False)
    assert A.workspace_headers() == {}
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", " wrkspc_abc ")
    assert A.workspace_headers() == {"anthropic-workspace-id": "wrkspc_abc"}
    import anthropic
    from types import SimpleNamespace
    made = {}
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: made.update(kw) or SimpleNamespace(
        messages=None, beta=SimpleNamespace(messages=None)))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    import importlib
    importlib.reload(A).client("copy", log=False)          # the real factory (conftest blocks it), Anthropic faked
    assert made["default_headers"] == {"anthropic-workspace-id": "wrkspc_abc"}


def test_replicate_throttle_waits_and_retries(monkeypatch):
    """2026-10-23: under $5 of Replicate credit, 7 of 8 images failed at once with 429. A throttle now waits."""
    import sys
    from types import SimpleNamespace
    from creative_intelligence import budget, config
    from creative_intelligence.providers import replicate as R
    calls, waits = [], []

    class Client:
        def __init__(self, **kw):
            pass

        def run(self, model, input):
            calls.append(model)
            if len(calls) < 3:
                raise RuntimeError("ReplicateError Details: status: 429 detail: Request was throttled.")
            return "https://out/x.png"
    monkeypatch.setitem(sys.modules, "replicate", SimpleNamespace(Client=Client))
    monkeypatch.setattr(config, "CI_REPLICATE_API_KEY", "r8_test")
    monkeypatch.setattr(budget, "check_now", lambda *a, **k: None)
    monkeypatch.setattr(R, "estimate", lambda *a, **k: 0.1)
    monkeypatch.setattr(R.time, "sleep", lambda s: waits.append(s))
    monkeypatch.setattr(R, "_cool_until", [0.0])
    clock = [1000.0]
    monkeypatch.setattr(R.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(R, "_wait_turn", lambda: None)
    assert R.run("google/nano-banana-pro", {}) == "https://out/x.png" and len(calls) == 3
    assert R._cool_until[0] > 1000                                    # every thread waits out the cool-down
    monkeypatch.setattr(R, "THROTTLE_RETRIES", 0)
    calls.clear()
    import pytest
    with pytest.raises(R.GenerationError, match="add credit"):
        R.run("google/nano-banana-pro", {})
