"""
Image generation for Ad Studio: Replicate (CI_IMAGE_MODEL, google/nano-banana-pro) with reference
photos as image_input, through the Replicate adapter (creative_intelligence/providers/replicate.py),
which checks the generation budget before every call.
"""
from __future__ import annotations

from creative_intelligence import config as _config
from creative_intelligence.providers.replicate import GenerationError as AdGenError  # noqa: F401  (routes catch it)

MODEL = _config.CI_IMAGE_MODEL


def generate_ad(prompt: str, reference_data_uris: list[str], aspect_ratio: str = "4:5") -> str:
    """Generate one ad image. Returns a URL or file:// URI. Raises BudgetExceeded over the cap."""
    from creative_intelligence.providers.replicate import generate_image
    return generate_image(prompt, reference_data_uris, aspect_ratio)
