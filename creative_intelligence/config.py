"""
Configuration for the creative intelligence subsystem.
Loads from the same .env as the main project, but reads only CI_ prefixed vars
plus the shared vars it needs (META_*, OPENAI_API_KEY).
Does not mutate or re-export anything from the main project's config.
"""
from __future__ import annotations

import os
from pathlib import Path

from creative_intelligence import brand as _brand   # the brand pack (brands/<BRAND>/brand.yaml)


def _load_env() -> None:
    """Load .env if python-dotenv is available.

    Searches upward from the repo root so this works both in the main
    checkout and in git worktrees (where .env lives in the parent repo).
    """
    try:
        from dotenv import load_dotenv, find_dotenv
        # find_dotenv() walks up from CWD until it finds a .env file.
        env_path = find_dotenv(usecwd=True)
        if env_path:
            load_dotenv(env_path, override=True)
        else:
            # Fallback: try repo root relative to this file
            fallback = Path(__file__).parent.parent / ".env"
            if fallback.exists():
                load_dotenv(fallback, override=True)
    except ImportError:
        pass  # dotenv optional; env vars may already be set


_load_env()


# ─────────────────────────────────────────────
# Meta API (read-only, shared with main project)
# ─────────────────────────────────────────────

META_ACCESS_TOKEN: str = os.getenv("META_ACCESS_TOKEN", "")
META_AD_ACCOUNT_ID: str = os.getenv("META_AD_ACCOUNT_ID", "")
META_API_VERSION: str = os.getenv("META_API_VERSION", "v19.0")

# ─────────────────────────────────────────────
# LLM
# ─────────────────────────────────────────────

OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
# Default model for creative generation. Can be overridden per call.
CI_LLM_MODEL: str = os.getenv("CI_LLM_MODEL", "gpt-4o-mini")
# Max tokens for generation responses.
CI_LLM_MAX_TOKENS: int = int(os.getenv("CI_LLM_MAX_TOKENS", "2000"))

# ─────────────────────────────────────────────
# Storage
# ─────────────────────────────────────────────

# Path to SQLite database.
CI_DB_PATH: str = os.getenv("CI_DB_PATH", "")

# Directory for generated operator reports.
CI_REPORTS_DIR: str = os.getenv(
    "CI_REPORTS_DIR",
    str(Path(__file__).parent.parent / "outputs" / "creative_reports"),
)

# ─────────────────────────────────────────────
# Data paths (read-only pointers to main project outputs)
# ─────────────────────────────────────────────

_raw_data_dir_env = os.getenv("CI_RAW_DATA_DIR", "").strip()
RAW_DATA_DIR: Path = (
    Path(_raw_data_dir_env)
    if _raw_data_dir_env
    else Path(__file__).parent.parent / "data" / "raw"
)

# ─────────────────────────────────────────────
# Product knowledge
# ─────────────────────────────────────────────

# Directory where product CSV/JSON/brief files can be dropped for ingestion.
CI_PRODUCT_KB_DIR: str = os.getenv(
    "CI_PRODUCT_KB_DIR",
    str(Path(__file__).parent.parent / "creative_intelligence_data" / "product_kb"),
)

# ─────────────────────────────────────────────
# Feature flags
# ─────────────────────────────────────────────

# AI tagging runs via `ci tag --ai` (Claude; see tagging/ai_tagger.py), which the
# Meta sync passes.

# Set CI_SHOPIFY_ENABLED=1 to enable Shopify product sync.
SHOPIFY_ENABLED: bool = os.getenv("CI_SHOPIFY_ENABLED", "0") == "1"

# ─────────────────────────────────────────────
# Shopify (read-only product sync)
# ─────────────────────────────────────────────

# e.g. "mystore.myshopify.com" or full URL "https://mystore.myshopify.com"
SHOPIFY_STORE_URL: str = os.getenv("SHOPIFY_STORE_URL", "")
# Admin API access token with read_products scope
SHOPIFY_ACCESS_TOKEN: str = os.getenv("SHOPIFY_ACCESS_TOKEN", "")
# Admin API version to target (default: 2024-01)
SHOPIFY_API_VERSION: str = os.getenv("SHOPIFY_API_VERSION", "2024-01")


# ─────────────────────────────────────────────
# Static rendering / image generation (v1.4)
# ─────────────────────────────────────────────

# Provider name: "mock" (always safe) | "nano_banana" (requires API key)
CI_IMAGE_PROVIDER: str = os.getenv("CI_IMAGE_PROVIDER", "mock")

# Set to "1" to call the image provider API; default off (spec-only mode)
CI_IMAGE_GENERATION_ENABLED: bool = os.getenv("CI_IMAGE_GENERATION_ENABLED", "0") == "1"

# Local directory for generated image files (relative to repo root by default)
CI_RENDER_OUTPUT_DIR: str = os.getenv(
    "CI_RENDER_OUTPUT_DIR",
    str(Path(__file__).parent.parent / "creative_intelligence_data" / "render_outputs"),
)

# Nano Banana credentials — only needed when CI_IMAGE_PROVIDER=nano_banana
CI_NANO_BANANA_API_KEY: str = os.getenv("CI_NANO_BANANA_API_KEY", "")
CI_NANO_BANANA_ENDPOINT: str = os.getenv(
    "CI_NANO_BANANA_ENDPOINT",
    "https://api.nanobanana.io/v1/generate",   # placeholder — update from API docs
)

# Local directory for assembled video files
CI_VIDEO_OUTPUT_DIR: str = os.getenv(
    "CI_VIDEO_OUTPUT_DIR",
    str(Path(__file__).parent.parent / "creative_intelligence_data" / "video_outputs"),
)

# Replicate credentials — only needed when CI_IMAGE_PROVIDER=replicate
CI_REPLICATE_API_KEY: str = os.getenv("CI_REPLICATE_API_KEY", "")
# Default model: Google Nano Banana Pro — fast, native aspect ratio support
# Override with any Replicate image model in owner/name or owner/name:version format
CI_REPLICATE_MODEL: str = os.getenv(
    "CI_REPLICATE_MODEL",
    "google/nano-banana-pro",
)
# Image-to-video model for AI video assembly (motion_mode="ai_video").
# minimax/video-01-live: high quality ~6s clips, takes first_frame_image + prompt.
# Override with any Replicate i2v model, e.g. stability-ai/stable-video-diffusion-img2vid-xt-1-1
CI_VIDEO_MOTION_MODEL: str = os.getenv(
    "CI_VIDEO_MOTION_MODEL",
    "minimax/video-01-live",
)

# ─────────────────────────────────────────────
# Scene-level clip generation (v1.7)
# ─────────────────────────────────────────────

# Provider for per-scene clip generation: "mock" | "replicate" | "runway"
CI_CLIP_PROVIDER: str = os.getenv("CI_CLIP_PROVIDER", "mock")

# Set CI_CLIP_GENERATION_ENABLED=1 to call the clip provider API
CI_CLIP_GENERATION_ENABLED: bool = os.getenv("CI_CLIP_GENERATION_ENABLED", "0") == "1"

# Local directory for generated scene clips
CI_CLIP_OUTPUT_DIR: str = os.getenv(
    "CI_CLIP_OUTPUT_DIR",
    str(Path(__file__).parent.parent / "creative_intelligence_data" / "clip_outputs"),
)

# Replicate model for text-to-video / image-to-video clip generation.
# bytedance/seedance-2.0: default — high-quality, native 9:16 support, image-to-video via 'image' field.
# minimax/video-01-live: alternative — uses first_frame_image for image conditioning.
# runwayml/gen-4.5: text-to-video only (no reliable image conditioning via Replicate).
CI_CLIP_MODEL: str = os.getenv("CI_CLIP_MODEL", "bytedance/seedance-2.0")

# Runway direct API key (scaffold — future use when not proxied through Replicate)
CI_RUNWAY_API_KEY: str = os.getenv("CI_RUNWAY_API_KEY", "")


def validate() -> list[str]:
    """Return a list of missing required config vars."""
    missing = []
    if not META_ACCESS_TOKEN:
        missing.append("META_ACCESS_TOKEN")
    if not META_AD_ACCOUNT_ID:
        missing.append("META_AD_ACCOUNT_ID")
    return missing


# ─────────────────────────────────────────────
# Google Drive — Ad Studio exports for outside agencies
# OAuth client from Google Cloud (type "Web application"); the app signs in
# as the Drive owner once via Ad Gallery → "Connect Google Drive".
# ─────────────────────────────────────────────
GOOGLE_OAUTH_CLIENT_ID: str = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "")
GOOGLE_OAUTH_CLIENT_SECRET: str = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "")
# Parent folder every export goes under ("Meta Ads").
# No default: a copy of the app must never export into the agency's folder by accident.
ADSTUDIO_DRIVE_FOLDER_ID: str = os.getenv("ADSTUDIO_DRIVE_FOLDER_ID", "")
# Only this Google account may be connected (the app itself has no login).
ADSTUDIO_DRIVE_ACCOUNT: str = os.getenv("ADSTUDIO_DRIVE_ACCOUNT", "")


# ─────────────────────────────────────────────
# Ad Studio — asset IDs and generation cost
# ─────────────────────────────────────────────
# Meta ad name for every finished ad. Fields: concept, angle, hook, format, product, version.
ASSET_ID_TEMPLATE: str = os.getenv(
    "ASSET_ID_TEMPLATE", _brand.get("asset_id_template").replace("{short}", _brand.short()))

# Price per unit for each model, seeded into the model_prices table on first run and
# editable there afterwards (each row keeps when it was last checked and where from).
MODEL_PRICES_SEED: list[dict] = [
    # Claude, per 1M tokens — from platform.claude.com/docs/en/about-claude/pricing (checked 2026-10-15)
    {"model": "claude-sonnet-4-5", "unit": "1M input tokens", "usd": 3.00,
     "source": "Anthropic pricing page", "checked_at": "2026-10-15"},
    {"model": "claude-sonnet-4-5:output", "unit": "1M output tokens", "usd": 15.00,
     "source": "Anthropic pricing page", "checked_at": "2026-10-15"},
    {"model": "claude-opus-5", "unit": "1M input tokens", "usd": 5.00,
     "source": "Anthropic pricing page", "checked_at": "2026-10-15"},
    {"model": "claude-opus-5:output", "unit": "1M output tokens", "usd": 25.00,
     "source": "Anthropic pricing page", "checked_at": "2026-10-15"},
    {"model": "google/nano-banana-pro", "unit": "image", "usd": 0.18,
     "source": "unverified: third-party table for Replicate; Google direct is $0.134 (1K/2K), $0.24 (4K)",
     "checked_at": None},
    # Claude Opus 5.5 — text check (T3). Per 1M tokens; from the Claude API reference pricing table (cached 2026-10-06).
    {"model": "claude-opus-5-5", "unit": "1M input tokens", "usd": 4.00,
     "source": "Anthropic pricing page", "checked_at": "2026-10-15"},
    {"model": "claude-opus-5-5:output", "unit": "1M output tokens", "usd": 20.00,
     "source": "Anthropic pricing page", "checked_at": "2026-10-15"},
    {"model": "claude-sonnet-5-5", "unit": "1M input tokens", "usd": 2.00,
     "source": "Anthropic pricing page", "checked_at": "2026-11-02"},
    {"model": "claude-sonnet-5-5:output", "unit": "1M output tokens", "usd": 10.00,
     "source": "Anthropic pricing page", "checked_at": "2026-11-02"},
    # Claude web search (Product Hub research): $10 per 1,000 searches, on top of tokens.
    {"model": "web_search", "unit": "1 search", "usd": 0.01,
     "source": "Anthropic pricing page", "checked_at": "2026-10-19"},
    # Talking heads (V5), image + audio -> video, per second. Third-party figures until the first bill.
    {"model": "bytedance/omni-human-1.5", "unit": "second of video", "usd": 0.14,
     "source": "unverified: fal.ai / Runware list ~$0.13-0.14/s", "checked_at": None},
    {"model": "kwaivgi/kling-avatar-v2", "unit": "second of video", "usd": 0.09,
     "source": "unverified: ~$0.45 per 5 s generation (MuAPI estimate)", "checked_at": None},
    {"model": "wan-video/wan-2.2-s2v", "unit": "second of video", "usd": 0.10,
     "source": "unverified estimate", "checked_at": None},
    # Voice-over (V3). Replicate shows no per-call price for these; estimates until the first bill.
    {"model": "jaaari/kokoro-82m", "unit": "voice-over", "usd": 0.002,
     "source": "unverified estimate (runs ~2 s on Replicate hardware)", "checked_at": None},
    # More natural voices to audition (2026-11-04: "the voices are still not there"); per 1K characters, unverified
    # until the first bill (Replicate shows none on the model pages)
    {"model": "inworld/realtime-tts-2", "unit": "1K characters", "usd": 0.05,
     "source": "estimate, 2026-11-04 (unverified)", "checked_at": None},
    {"model": "google/gemini-3.8-flash-tts", "unit": "1K characters", "usd": 0.05,
     "source": "estimate, 2026-11-04 (unverified)", "checked_at": None},
    {"model": "minimax/speech-2.8-hd", "unit": "1K characters", "usd": 0.10,
     "source": "estimate, 2026-11-04 (unverified)", "checked_at": None},
    {"model": "elevenlabs/v3", "unit": "1K characters", "usd": 0.20,
     "source": "unverified estimate: no Replicate price found", "checked_at": None},
    {"model": "victor-upmeet/whisperx", "unit": "transcription", "usd": 0.01,
     "source": "unverified estimate", "checked_at": None},
    # Kling 3.0 video on Replicate, per second of video (standard 720p, no audio). Replicate's page
    # shows no price; Kling's own API is $0.084/s (720p) and $0.112/s (1080p); a third-party post
    # says Replicate charges about twice that. Check it on the Admin page against the first bill.
    {"model": "kwaivgi/kling-v3-video", "unit": "second of video", "usd": 0.168,
     "source": "unverified estimate: 2x Kling direct ($0.084/s 720p)", "checked_at": None},
    # Read from each Replicate model page's own price data on 2026-10-19 (standard / 720p).
    {"model": "kwaivgi/kling-v3-video:pro", "unit": "second of video (pro, 1080p)", "usd": 0.224,
     "source": "unverified estimate: 2x Kling direct pro ($0.112/s 1080p)", "checked_at": None},
    {"model": "kwaivgi/kling-v3-video:audio", "unit": "second of video with sound", "usd": 0.252,
     "source": "Replicate model page, 2026-10-19", "checked_at": None},
    {"model": "google/veo-3.1-fast", "unit": "second of video", "usd": 0.10,
     "source": "Replicate model page, 2026-10-19", "checked_at": None},
    {"model": "google/veo-3.1-fast:audio", "unit": "second of video with sound", "usd": 0.15,
     "source": "Replicate model page, 2026-10-19", "checked_at": None},
    # Kling 3.0 on Kling's own API: 0.6 units a second at 720p, 0.8 at 1080p, at $0.14 a unit (resource pack list
    # price; third-party docs, check the first Kling bill). Sound priced like Replicate's ratio until checked.
    {"model": "851-labs/background-remover", "unit": "image", "usd": 0.002,
     "source": "Replicate CPU run, a second or two (estimate, 2026-11-03)", "checked_at": None},
    {"model": "kling-direct/avatar", "unit": "second of presenter video (std)", "usd": 0.059,
     "source": "assumed like a Kling 3.0 second until the first avatar bill (2026-11-02)", "checked_at": None},
    {"model": "kling-direct/kling-3.0", "unit": "second of video (720p)", "usd": 0.084,
     "source": "Kling API pricing via Atlas Cloud, 2026-11-02 (unverified)", "checked_at": None},
    {"model": "kling-direct/kling-3.0:pro", "unit": "second of video (1080p)", "usd": 0.112,
     "source": "Kling API pricing via Atlas Cloud, 2026-11-02 (unverified)", "checked_at": None},
    {"model": "kling-direct/kling-3.0:audio", "unit": "second of video with sound", "usd": 0.126,
     "source": "estimate (Replicate's sound ratio), 2026-11-02", "checked_at": None},
    {"model": "bytedance/seedance-2.0", "unit": "second of video (720p)", "usd": 0.18,
     "source": "Replicate model page, 2026-10-19", "checked_at": None},
    # The model test (feedback 2026-11-04: "seedance 2.5 is the best on the market … wan 3.0 decent for less").
    {"model": "bytedance/seedance-2.5", "unit": "second of video (720p)", "usd": 0.231,
     "source": "BytePlus list price via framesurfer.com, 2026-11-04 (unverified on Replicate)", "checked_at": None},
    {"model": "alibaba/wan-3", "unit": "second of video (720p)", "usd": 0.10,
     "source": "Alibaba list price (Replicate shows none), 2026-11-04 (unverified)", "checked_at": None},
]

# Spending caps (F5, creative_intelligence/budget.py). These are the defaults; Admin can change them.
# Generation = images and video: per run and per day. Claude text calls: per day. Meta sync tagging
# is never blocked; an Admin alert fires when a day's tagging passes BUDGET_TAGGING_ALERT_USD.
CI_VIDEO_MODEL: str = os.getenv("CI_VIDEO_MODEL", "kwaivgi/kling-v3-video")
# Kling's own API (providers/kling.py): when set, every Kling clip goes there first (about half Replicate's price),
# Replicate's Kling stays the fallback. The user sets it in Railway; never committed.
KLING_API_KEY: str = os.getenv("KLING_API_KEY", "")
# Shot-library routing (Video Studio V2): product, cut and macro shots -> CI_VIDEO_MODEL (Kling);
# orchard and lifestyle shots with ambient sound -> Veo; multi-reference shots -> Seedance.
CI_VIDEO_MODEL_AMBIENT: str = os.getenv("CI_VIDEO_MODEL_AMBIENT", "google/veo-3.1-fast")
CI_VIDEO_MODEL_MULTIREF: str = os.getenv("CI_VIDEO_MODEL_MULTIREF", "bytedance/seedance-2.0")
# Whole-video model test (2026-11-04): a video made on one of these uses it for every shot, with no fallback, so the
# quality is the same across the video. Both on Replicate.
CI_VIDEO_MODEL_SEEDANCE25: str = os.getenv("CI_VIDEO_MODEL_SEEDANCE25", "bytedance/seedance-2.5")
CI_VIDEO_MODEL_WAN3: str = os.getenv("CI_VIDEO_MODEL_WAN3", "alibaba/wan-3")
# If a route's model fails (not a budget stop), try this route once instead: "veo:kling,seedance:kling".
VIDEO_FALLBACKS: str = os.getenv("VIDEO_FALLBACKS", "veo:kling,seedance:kling")
# Voice-over (Video Studio V3): text-to-speech on Replicate. Community models need their version pinned.
KOKORO_MODEL: str = os.getenv("KOKORO_MODEL", "jaaari/kokoro-82m:f559560eb822dc509045f3921a1921234918b91739db4bf3daab2169b71c7a13")
ELEVENLABS_MODEL: str = os.getenv("ELEVENLABS_MODEL", "elevenlabs/v3")
# Wade, Jack and Ivy moved to ElevenLabs v3 (2026-11-02, "too AI"): the owner picked replacement voices from samples.
GEMINI_TTS_MODEL: str = os.getenv("GEMINI_TTS_MODEL", "google/gemini-3.8-flash-tts")
# Persona voices picked by ear (2026-11-04, "do Algenib, Laomedeia, Aoede, Mark, Sulafat"), each with the delivery
# direction it was auditioned with. Gemini TTS takes the direction as its style; ElevenLabs reads in context.
# House voices come from the brand pack (brands/<BRAND>/brand.yaml › voices); `model` there is an alias below.
_VOICE_MODELS = {"kokoro": KOKORO_MODEL, "elevenlabs": ELEVENLABS_MODEL, "gemini": GEMINI_TTS_MODEL}
HOUSE_VOICES: list[dict[str, str]] = [{**v, "model": _VOICE_MODELS.get(v.get("model", ""), v.get("model", ""))}
                                      for v in _brand.get("voices", [])]
DEFAULT_VOICE: str = os.getenv("DEFAULT_VOICE", _brand.get("default_voice") or (HOUSE_VOICES[0]["name"] if HOUSE_VOICES else ""))
# Voices that belong to one persona and never narrate anyone else's video: {voice: stand-in voice}.
PERSONA_ONLY_VOICES: dict[str, str] = dict(_brand.get("persona_only_voices") or {})
# Word timings for captions: WhisperX on Replicate (word-level alignment).
WORDS_MODEL: str = os.getenv("WORDS_MODEL", "victor-upmeet/whisperx:655845d6190ef70573c669245f245892cd039df4b880a1e3a65852c09252f5cc")
# Licensed music tracks you provide (music/): none until you add some; no AI music.
MUSIC_DIR: str = os.getenv("MUSIC_DIR", str(Path(__file__).resolve().parent.parent / "music"))
BUDGET_RUN_USD: float = float(os.getenv("BUDGET_RUN_USD", "10"))
BUDGET_DAY_USD: float = float(os.getenv("BUDGET_DAY_USD", "50"))
BUDGET_TEXT_DAY_USD: float = float(os.getenv("BUDGET_TEXT_DAY_USD", "15"))
BUDGET_RESEARCH_MONTH_USD: float = float(os.getenv("BUDGET_RESEARCH_MONTH_USD", "10"))   # Phase 2a research (brief R)
# Customer voice (Phase 2a R3). Fera is the store's reviews app (site reviews plus imported Google reviews); this is
# its public storefront widget key, already visible on every product page, read-only.
FERA_PUBLIC_KEY: str = os.getenv("FERA_PUBLIC_KEY", _brand.get("reviews.fera_public_key") or "")
REVIEW_SITES: dict = {k: _brand.get(f"reviews.{k}") for k in ("trustpilot", "testimonials") if _brand.get(f"reviews.{k}")}
BUDGET_VIDEO_JOB_USD: float = float(os.getenv("BUDGET_VIDEO_JOB_USD", "25"))
BUDGET_VIDEO_DAY_USD: float = float(os.getenv("BUDGET_VIDEO_DAY_USD", "60"))
BUDGET_TAGGING_ALERT_USD: float = float(os.getenv("BUDGET_TAGGING_ALERT_USD", "5"))

# Model names — change here (or by env var), never in feature code.
CI_CLAUDE_COPY_MODEL: str = os.getenv("CI_CLAUDE_COPY_MODEL", "claude-sonnet-4-5")    # hooks, briefs, chat, scenes
CI_CLAUDE_CHECK_MODEL: str = os.getenv("CI_CLAUDE_CHECK_MODEL", "claude-opus-5-5")    # scripts, research, run plans
# QA scores, text check, claims check: many small calls, half Opus's price (feedback 2026-11-02, "make these cheaper")
CI_CLAUDE_SCORE_MODEL: str = os.getenv("CI_CLAUDE_SCORE_MODEL", "claude-sonnet-5-5")
CI_CLAUDE_TAG_MODEL: str = os.getenv("CI_CLAUDE_TAG_MODEL", "claude-opus-5")          # tagging Meta ads in the sync
CI_IMAGE_MODEL: str = os.getenv("CI_IMAGE_MODEL", "google/nano-banana-pro")           # Replicate image model
