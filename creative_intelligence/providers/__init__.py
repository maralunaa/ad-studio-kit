"""
Model providers behind small adapters, and the model registry.

  registry()        every model role the app uses: provider, model id (from config), budget bucket
                    and price, so Admin can show them and a model swap is one setting
  anthropic.client  Claude, with the budget check before each call and cost logging after
  replicate.run     Replicate (images, video), with the generation budget check before each call

Roles: copy (hooks, briefs, chat), check (QA, text and claims checks, run plans), tag (Meta sync
tagging), image, video.
"""
from __future__ import annotations

from typing import Any


def roles() -> dict[str, dict[str, str]]:
    from creative_intelligence import config
    return {
        "copy": {"provider": "anthropic", "model": config.CI_CLAUDE_COPY_MODEL, "bucket": "text",
                 "setting": "CI_CLAUDE_COPY_MODEL", "use": "Hooks, briefs, chat, scenes"},
        "check": {"provider": "anthropic", "model": config.CI_CLAUDE_CHECK_MODEL, "bucket": "text",
                  "setting": "CI_CLAUDE_CHECK_MODEL", "use": "Scripts, research, run plans"},
        "score": {"provider": "anthropic", "model": config.CI_CLAUDE_SCORE_MODEL, "bucket": "text",
                  "setting": "CI_CLAUDE_SCORE_MODEL", "use": "QA scores, text check, claims check"},
        "tag": {"provider": "anthropic", "model": config.CI_CLAUDE_TAG_MODEL, "bucket": "tagging",
                "setting": "CI_CLAUDE_TAG_MODEL", "use": "Tagging Meta ads in the sync"},
        "image": {"provider": "replicate", "model": config.CI_IMAGE_MODEL, "bucket": "generation",
                  "setting": "CI_IMAGE_MODEL", "use": "Images, Core shots, AI ad scenes"},
        "video": {"provider": "replicate", "model": config.CI_VIDEO_MODEL, "bucket": "generation",
                  "setting": "CI_VIDEO_MODEL", "use": "Video clips (Phase 2)"},
    }


def model(role: str) -> str:
    return roles()[role]["model"]


def registry(conn) -> list[dict[str, Any]]:
    from creative_intelligence.adstudio import costs
    costs.seed_prices(conn)
    prices = {r["model"]: dict(r) for r in conn.execute("SELECT * FROM model_prices")}
    out = []
    for role, r in roles().items():
        p = prices.get(r["model"]) or {}
        out.append({"role": role, **r, "price_usd": p.get("usd"), "unit": p.get("unit"),
                    "price_checked_at": p.get("checked_at"), "price_source": p.get("source")})
    return out
