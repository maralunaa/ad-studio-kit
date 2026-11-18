"""
The brand pack: everything that makes this app one brand's ad studio rather than another's.

    brands/<BRAND>/                     BRAND (env) picks the pack, default "sample"; BRAND_DIR points anywhere else
      brand.yaml                        name, store, naming, colours, fonts, logo, rules, voices, presenters,
                                        review sources, watched competitors (see brands/sample/brand.yaml)
      logo.png                          the mark in the top bar, login page and video end cards
      prompts/<name>.vN.txt             optional: replaces the core prompt of the same name (creative_intelligence/prompts)
      shots/<type>.vN.yaml              optional: adds or replaces shot recipe cards
      knowledge/                        optional: the brand's own playbook files (else knowledge/ at the repo root)
      qa_rubric.yaml                    optional: the brand's quality bar (else config/qa_rubric.yaml)

Code reads the pack through this module only: name(), short(), get("colors.accent"), path("logo.png"), and
prompt_values() for the ${brand}-style placeholders every prompt file may use.
"""
from __future__ import annotations

import copy
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS: dict[str, Any] = {
    "name": "Example Brand",
    "short": "EXB",
    "website": "example.com",
    "category": "specialty food",
    "description": "a direct-to-consumer brand",
    "about": "a small independent brand",
    "packaging": "plain kraft box",
    "packaging_detail": "OUR BOX: a plain kraft shipping box, open, lined with tissue paper; the products sit neatly "
                        "inside, filling it. No printing, logos or labels on the box.",
    "store": {"public_url": "", "season_in_tags": ["in-season"], "season_out_tags": ["preorder"],
              "hidden_tags": ["hidden"], "type_map": {"fruit": "fruit", "mix box": "gift_box", "candle": "candle"}},
    "timezone": "UTC",
    "asset_id_template": "{short}_{concept}_{angle}_{hook}_{format}_{product}_v{version:02d}",
    "colors": {"accent": "#ef8a2d", "accent_ink": "#1f1206", "ink": "#1d1915", "paper": "#fbf7f1",
               "muted": "#6c645b"},
    "fonts": {"headline": "Poppins-Bold.ttf", "body": "Poppins-Medium.ttf", "hand": "Caveat.ttf"},
    "rules": {"no_origin_claims": False},
    "end_card": {"cta": "Shop now"},
    "voices": [], "default_voice": "", "persona_only_voices": {},
    "presenters": [],
    "reviews": {"fera_public_key": "", "trustpilot": "", "testimonials": "", "bbb": False},
    "market": {"brands": [], "keywords": []},
}


def brand_dir() -> Path:
    if os.getenv("BRAND_DIR"):
        return Path(os.environ["BRAND_DIR"])
    return ROOT / "brands" / os.getenv("BRAND", "sample")


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


@lru_cache(maxsize=None)
def settings() -> dict[str, Any]:
    """brand.yaml over the defaults above (a missing pack runs on the defaults)."""
    import yaml
    f = brand_dir() / "brand.yaml"
    return _merge(DEFAULTS, yaml.safe_load(f.read_text(encoding="utf-8")) if f.exists() else {})


def get(key: str, default: Any = None) -> Any:
    """A dotted key, e.g. get("colors.accent")."""
    v: Any = settings()
    for part in key.split("."):
        if not isinstance(v, dict) or part not in v:
            return default
        v = v[part]
    return v


def name() -> str:
    return settings()["name"]


def short() -> str:
    return settings()["short"]


def path(rel: str) -> Path | None:
    """A file in the pack, or None when the pack doesn't have it."""
    p = brand_dir() / rel
    return p if p.exists() else None


def rgb(key: str) -> tuple[int, int, int]:
    """A brand colour (hex in brand.yaml) as an RGB tuple, for the PIL renderers."""
    h = str(get(f"colors.{key}", "#000000")).lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def prompt_values() -> dict[str, str]:
    """What the ${brand_*} placeholders become in prompt files and shot cards."""
    s = settings()
    return {"brand": s["name"], "brand_short": s["short"], "brand_site": s["website"], "brand_category": s["category"],
            "brand_description": s["description"], "brand_about": s["about"], "brand_packaging": s["packaging"]}


def user_agent() -> str:
    return f"{short()}-AdStudio/1.0"


def reload() -> None:
    """Tests and a changed pack: read brand.yaml again."""
    settings.cache_clear()
