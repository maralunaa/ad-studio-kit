"""
Product-type profiles — what makes a fruit ad different from a candle ad.

Every product in Ad Studio has a `product_type` (fruit | candle | gift_box | other); a gift box
(Shopify "Mix Box") uses the general profile under its own name. The
type picks the hook brief Claude writes to, the archetype menu Claude invents
image concepts from, and the reference-lock / do-not-include blocks pasted into
every Nano Banana prompt. The Setup → Images → Compose → Gallery flow is the
same for every type; what flows through it is not.

Candle keeps the original (approved) blocks from formats.py verbatim.
"""
from __future__ import annotations

from typing import Any

from creative_intelligence.adstudio import formats as F
from creative_intelligence.adstudio.core import BOX, TECHNICAL as FOOD_TECHNICAL
from creative_intelligence import prompts

DEFAULT_TYPE = "other"

# ─────────────────────────────────────────────────────────────────────────────
# Fruit
# ─────────────────────────────────────────────────────────────────────────────
FRUIT_FORMATS: dict[str, dict[str, str]] = {
    "cut_reveal": {"name": "Cross-Section Reveal",
                   "blurb": "The fruit halved, flesh face-on and filling the frame — the 'what's inside' moment."},
    "first_bite": {"name": "First Bite",
                   "blurb": "A bite just taken or a spoon mid-scoop; juice and texture, human and craveable."},
    "in_hand": {"name": "In-Hand Scale",
                "blurb": "A hand holding whole and cut fruit — shows real size and feels personal."},
    "unboxing": {"name": "Unboxing",
                 "blurb": "Our white box just opened on a doorstep or counter, hands lifting the first fruit."},
    "orchard": {"name": "Farm / Orchard Origin",
                "blurb": "On the tree, in a harvest crate, or in a grower's hands — provenance and freshness."},
    "juice_macro": {"name": "Juice / Texture Macro",
                    "blurb": "Extreme close-up of flesh fibres, seeds, a drip of juice. Pure appetite appeal."},
    "flat_lay": {"name": "Overhead Flat-Lay",
                 "blurb": "Top-down arrangement of whole, halved and sliced fruit on a styled surface."},
    "kitchen_prep": {"name": "Kitchen Prep",
                     "blurb": "Mid-cut on a wooden board, knife in frame, morning window light."},
    "plated": {"name": "Plated / Served",
               "blurb": "Served the way it's best eaten — a bowl, breakfast plate, dessert or smoothie."},
    "variety_lineup": {"name": "Variety Lineup",
                       "blurb": "Several varieties or ripeness stages side by side, each cut to show the flesh."},
    "color_pop": {"name": "Color Pop",
                  "blurb": "The fruit on a bold saturated backdrop that complements its flesh colour."},
    "outdoor_life": {"name": "Outdoor Lifestyle",
                     "blurb": "Picnic, pool, beach or patio — the fruit as part of a sunny moment."},
    "sharing": {"name": "Sharing / Family",
                "blurb": "Several hands reaching for cut pieces on a shared plate — social and warm."},
    "pov": {"name": "POV Moment",
            "blurb": "First-person view: holding a cut half over a table, about to eat. Native, scroll-stopping."},
    "vs_supermarket": {"name": "Ours vs Supermarket",
                       "blurb": "Split comparison: our peak-ripe fruit beside a pale, generic store version."},
    "gift": {"name": "Gift Moment",
             "blurb": "The box being handed over or opened as a gift — ribbon, card, a delighted reaction."},
    "seasonal_drop": {"name": "Seasonal Drop",
                      "blurb": "Dark, dramatic still life with 'in season now, not for long' energy."},
    "editorial": {"name": "Editorial Still Life",
                  "blurb": "Painterly, magazine-grade still life — dramatic light, generous negative space."},
    "minimal_studio": {"name": "Minimal Studio",
                       "blurb": "Whole + halved fruit on a seamless tonal sweep. Clean and premium."},
    "ingredient_use": {"name": "Recipe / Pairing",
                       "blurb": "The fruit paired with what it's great with — yogurt, cheese, cocktails, salads."},
}

FRUIT_REFERENCE_LOCK = prompts.text("fruit-reference-lock")

FRUIT_NEGATIVE = (
    "Do NOT include: any fruit that is not this product (unless the scene "
    "explicitly pairs it), generic or look-alike varieties, bruised, rotten, "
    "shrivelled or unripe-looking fruit, supermarket PLU stickers, plastic "
    "clamshells or foam netting, extra or malformed fingers, distorted hands, "
    "cartoon/illustration/3D-render/AI-glossy look, oversaturation, harsh "
    "shadows, cluttered or messy props, watermarks, brand or platform logos, "
    "website URLs."
)

FRUIT_CONCEPT_RULES = prompts.text("fruit-concept-rules", box=BOX)

FRUIT_HOOK_DESIRE = (
    "For fresh fruit the Desire is usually TASTE and DISCOVERY: a flavour they've "
    "never had, peak-ripe quality they can't get at the supermarket, farm-direct "
    "freshness, rarity and short seasons, gifting. Be sensory and specific about "
    "flavour and texture (e.g. 'tastes like pineapple-banana custard'), and "
    "honest about what the fruit actually is."
)

# ─────────────────────────────────────────────────────────────────────────────
# Candle (original Ad Studio behaviour)
# ─────────────────────────────────────────────────────────────────────────────
CANDLE_CONCEPT_RULES = prompts.text("candle-concept-rules")

CANDLE_HOOK_DESIRE = (
    "For a candle/home product the Desire is usually ATMOSPHERE, MEMORY, or "
    "FEELING — not taste. Keep a quiet-luxury, warm, real tone. Be concrete and "
    "physical."
)

# ─────────────────────────────────────────────────────────────────────────────
# Other — generic product
# ─────────────────────────────────────────────────────────────────────────────
_CANDLE_ONLY = {"flame_macro", "ingredient_story"}

OTHER_REFERENCE_LOCK = prompts.text("other-reference-lock")

OTHER_NEGATIVE = (
    "Do NOT include: any redesign of the product or its packaging, altered or "
    "misspelled printed text, extra units of the product, extra or malformed "
    "fingers, clutter or props beyond what is specified, price tags, barcodes, "
    "QR codes, watermarks, brand or platform logos, website URLs, "
    "cartoon/illustration/3D-render/AI-glossy look, harsh shadows, "
    "oversaturation, or a busy background."
)

OTHER_HOOK_DESIRE = (
    "Find the product's single strongest, most specific benefit and make the "
    "Desire concrete and physical — what life looks or feels like with it."
)


def _menu(formats: dict[str, dict[str, Any]], drop: set[str] = frozenset()) -> dict[str, dict]:
    return {k: {"name": v["name"], "blurb": v["blurb"]} for k, v in formats.items() if k not in drop}


PROFILES: dict[str, dict[str, Any]] = {
    "fruit": {
        "label": "Fruit",
        "notes_label": "Flavor notes",
        "hook_desire": FRUIT_HOOK_DESIRE,
        "concept_rules": FRUIT_CONCEPT_RULES,
        "reference_lock": FRUIT_REFERENCE_LOCK,
        "negative": FRUIT_NEGATIVE,
        "technical": FOOD_TECHNICAL,
        "formats": FRUIT_FORMATS,
    },
    "candle": {
        "label": "Candle",
        "notes_label": "Scent notes (the ONLY ingredients)",
        "hook_desire": CANDLE_HOOK_DESIRE,
        "concept_rules": CANDLE_CONCEPT_RULES,
        "reference_lock": F.REFERENCE_LOCK,
        "negative": F.NEGATIVE,
        "technical": F.TECHNICAL,
        "formats": _menu(F.FORMATS),
    },
    "other": {
        "label": "Other",
        "notes_label": "Key features",
        "hook_desire": OTHER_HOOK_DESIRE,
        "concept_rules": "",
        "reference_lock": OTHER_REFERENCE_LOCK,
        "negative": OTHER_NEGATIVE,
        "technical": F.TECHNICAL,
        "formats": _menu(F.FORMATS, _CANDLE_ONLY),
    },
}


PROFILES["gift_box"] = {**PROFILES["other"], "label": "Gift box", "notes_label": "What's inside"}


def normalize_type(value: Any) -> str:
    v = (str(value or "")).strip().lower()
    return v if v in PROFILES else DEFAULT_TYPE


# Which prompt files (creative_intelligence/prompts/) build each kind of picture, by product type.
PROMPT_FILES = {"fruit": ("fruit-reference-lock", "fruit-concept-rules"),
                "candle": ("label-reference-lock", "candle-concept-rules"),
                "other": ("other-reference-lock", None), "gift_box": ("other-reference-lock", None)}
PURPOSE_PROMPTS = {"image": ("image-concepts-system",), "run": ("run-plan-system",),
                   "aiad": ("ai-ad-scene-system", "ai-ad-copy-rules"), "core": ("core-brief-system",)}


def prompt_stamp(product: dict[str, Any] | None, purpose: str) -> str:
    """'name@vN+…' for the prompt files used to make a picture of this product (prompts.stamp)."""
    lock, rules = PROMPT_FILES[normalize_type((product or {}).get("product_type"))]
    names = [*PURPOSE_PROMPTS[purpose]]
    if rules and purpose in ("image", "run"):
        names.append(rules)
    if purpose != "core":
        names.append(lock)
    return prompts.stamp(*names)


def profile(product: dict[str, Any] | None) -> dict[str, Any]:
    """The profile for a product row (unknown/missing type → 'other')."""
    return PROFILES[normalize_type((product or {}).get("product_type"))]
