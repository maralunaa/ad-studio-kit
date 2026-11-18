"""Root conftest — adds project root to sys.path for tests, and runs the suite under its own brand pack
(tests/brand/brand.yaml), so tests don't change when the shipped sample brand does."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ["BRAND_DIR"] = str(Path(__file__).parent / "tests" / "brand")

import pytest


@pytest.fixture(autouse=True)
def _no_live_apis(monkeypatch):
    """CLAUDE.md: never hit a live API in tests. Any Claude or image-model call that a test
    hasn't faked fails loudly here instead of going out (tests that need one pass a fake
    client or monkeypatch these themselves, which overrides this)."""
    def blocked(*a, **k):
        raise RuntimeError("live API call blocked in tests — pass a fake")
    try:
        from creative_intelligence.adstudio import chat, generate
        from creative_intelligence.providers import anthropic as claude_provider
        monkeypatch.setattr(chat, "_client", blocked)
        monkeypatch.setattr(generate, "generate_ad", blocked)
        monkeypatch.setattr(claude_provider, "client", blocked)
    except ImportError:
        pass
    try:
        import replicate
        monkeypatch.setattr(replicate, "Client", blocked)
    except ImportError:
        pass
