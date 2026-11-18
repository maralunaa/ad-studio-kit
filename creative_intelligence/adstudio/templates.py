"""
The 12 AI Ads formats as Compose templates — layout data only, no drawing code.

Each template has:
  image_brief  what the TEXT-FREE image should look like and where it leaves calm space
               for copy (sent to the image model; it never draws words)
  elements     what code draws on top, in reading order. Boxes are fractions of the
               *safe area* [x, y, w, h], so one layout adapts to every size: the safe area
               is the Stories/Reels-safe middle of a 9:16 frame and almost the whole frame
               of a 4:5 or 1:1 one (see safe_area(), from knowledge/rules.yaml). Nothing is cropped.
  price_slot   whether the template has room for an optional price (off by default)

Element types (all text comes from the brief, the operator or Shopify — never invented):
  scrim   soft dark/light gradient for legibility       {box, from: top|bottom, strength}
  block   flat colour panel                              {box, fill}
  text    one copy field, shrunk to fit                  {field, box, size, weight, align, color, max_lines, upper, tracking}
  button  CTA pill                                       {field, box, size, fill}
  stars   five-star row (social proof only)              {box}
  badge   small pill (optional text)                     {field, box, size}
  list    lines of the brand's own copy (notes, post-it)       {field: items, box, size, color, bullet, numbered, max_items}
  bubbles text messages from the brand (its own voice; never  {field: items, box, size, fill, max_items}
          presented as a customer conversation)
  cards   real reviews or comments from the library      {field: reviews, box, size, max_items}
          (exact words, author, source, date; a quote
          that can't fit is left out, never cut)

Text colours are preferences: when one would read below 4.5:1 on the photo behind it (as
QA measures legibility), the renderer switches to white or ink, or shades the patch behind
light text on a mid-tone photo — so every layout stays legible on any approved image.

Kept independent of the renderer (render_template.py) so the same layouts could later be
drawn in HTML instead.
"""
from __future__ import annotations

from typing import Any

from creative_intelligence import knowledge


def safe_area() -> dict[str, tuple[float, float, float]]:
    """Per aspect ratio: (top, bottom) of the band text and logo stay inside, as fractions of the frame height,
    plus the side margin. From the playbook's safe zones (knowledge/rules.yaml); 9:16 keeps clear of the top
    14% and bottom 35% (Stories and Reels share one safe zone since March 2026)."""
    zones = knowledge.load("rules")["safe_zones"]
    return {k: (z["top"], z["bottom"], z["side"]) for k, z in zones.items() if k != "source"}

from creative_intelligence import brand as _brand

# Colours for code-drawn ads. "orange" is the brand accent (brand.yaml › colors.accent; the key name is kept because
# layouts refer to it); the rest is a neutral palette.
COLORS = {
    "orange": _brand.rgb("accent"), "accent": _brand.rgb("accent"), "navy": (46, 77, 107), "espresso": (40, 30, 24),
    "cream": (255, 249, 236), "white": (255, 255, 255), "ink": (24, 20, 17),
    "paper": (250, 247, 240), "postit": (255, 228, 120), "grey": (233, 229, 222),
}

FIELDS = ("headline", "sub", "cta", "price", "badge", "review", "author", "items", "reviews")
LIST_FIELDS = ("items", "reviews")      # filled with lists: the brand's own lines, or real reviews from the library

TEMPLATES: dict[str, dict[str, Any]] = {
    "minimal": {
        "name": "Minimal",
        "image_brief": "Pure product hero, generous clean background, no props competing with the fruit.",
        "elements": [],
    },
    "bold_type": {
        "name": "Bold Type",
        "image_brief": "Product sits in the lower half of the frame; the top 45% of the frame is a calm, "
                       "plain, softly lit background with nothing in it, ready for a large headline.",
        "elements": [
            {"type": "text", "field": "headline", "box": [0, 0.0, 1, 0.42], "size": 0.13, "weight": "bold",
             "align": "left", "color": "auto", "max_lines": 3, "upper": True},
            {"type": "button", "field": "cta", "box": [0, 0.90, 1, 0.10], "size": 0.042, "fill": "orange"},
        ],
    },
    "benefit_stack": {
        "name": "Benefit Stack",
        "image_brief": "Product photographed in the top 60% of the frame; the bottom 40% is plain and will be "
                       "covered by a colour panel, so keep everything important in the top 60%.",
        "price_slot": True,
        "elements": [
            {"type": "block", "box": [-0.2, 0.60, 1.4, 0.40], "fill": "navy", "bleed": True},
            {"type": "text", "field": "headline", "box": [0, 0.64, 1, 0.12], "size": 0.058, "weight": "bold",
             "align": "left", "color": "white", "max_lines": 2},
            {"type": "text", "field": "sub", "box": [0, 0.77, 1, 0.10], "size": 0.048, "weight": "medium",
             "align": "left", "color": "cream", "max_lines": 2},
            {"type": "text", "field": "price", "box": [0.55, 0.89, 0.45, 0.08], "size": 0.05, "weight": "bold",
             "align": "right", "color": "white", "max_lines": 1},
            {"type": "button", "field": "cta", "box": [0, 0.89, 0.55, 0.09], "size": 0.04, "fill": "orange", "align": "left"},
        ],
    },
    "social_proof": {
        "name": "Social Proof",
        "image_brief": "Product on a warm, real surface in the lower half; the upper half is calm and slightly "
                       "out of focus so a review card can sit on it.",
        "elements": [
            {"type": "block", "box": [0, 0.02, 1, 0.50], "fill": "cream", "radius": 0.04},
            {"type": "stars", "box": [0.06, 0.06, 0.5, 0.05]},
            {"type": "text", "field": "review", "box": [0.06, 0.13, 0.88, 0.27], "size": 0.052, "weight": "medium",
             "align": "left", "color": "ink", "max_lines": 6, "quote": True},
            {"type": "text", "field": "author", "box": [0.06, 0.42, 0.88, 0.06], "size": 0.036, "weight": "regular",
             "align": "left", "color": "ink", "max_lines": 1, "prefix": "— "},
            {"type": "scrim", "from": "bottom", "box": [0, 0.7, 1, 0.3], "strength": 0.6},
            {"type": "text", "field": "headline", "box": [0, 0.86, 1, 0.12], "size": 0.06, "weight": "bold",
             "align": "center", "color": "white", "max_lines": 2},
        ],
    },
    "origin_story": {
        "name": "Origin Story",
        "image_brief": "The fruit in its orchard or on the farm, natural light, a sense of place; the bottom "
                       "third is darker or calmer for a line of text.",
        "elements": [
            {"type": "scrim", "from": "bottom", "box": [0, 0.55, 1, 0.45], "strength": 0.65},
            {"type": "text", "field": "headline", "box": [0, 0.74, 1, 0.24], "size": 0.07, "weight": "semibold",
             "align": "left", "color": "white", "max_lines": 3},
        ],
    },
    "lifestyle_tagline": {
        "name": "Lifestyle Tagline",
        "image_brief": "A real, warm moment with the fruit (hands, a table, a kitchen); leave the lower third calm.",
        "elements": [
            {"type": "scrim", "from": "bottom", "box": [0, 0.6, 1, 0.4], "strength": 0.55},
            {"type": "text", "field": "headline", "box": [0, 0.78, 1, 0.2], "size": 0.068, "weight": "semibold",
             "align": "center", "color": "white", "max_lines": 3},
        ],
    },
    "direct_response": {
        "name": "Direct Response",
        "image_brief": "Clear product shot in the middle of the frame; calm background above and below it.",
        "price_slot": True,
        "elements": [
            {"type": "scrim", "from": "top", "box": [0, 0.0, 1, 0.32], "strength": 0.45},
            {"type": "text", "field": "headline", "box": [0, 0.0, 1, 0.20], "size": 0.085, "weight": "bold",
             "align": "center", "color": "auto", "max_lines": 2},
            {"type": "text", "field": "sub", "box": [0, 0.20, 1, 0.09], "size": 0.045, "weight": "medium",
             "align": "center", "color": "auto", "max_lines": 2},
            {"type": "text", "field": "price", "box": [0, 0.80, 1, 0.08], "size": 0.06, "weight": "bold",
             "align": "center", "color": "auto", "max_lines": 1},
            {"type": "button", "field": "cta", "box": [0, 0.89, 1, 0.1], "size": 0.048, "fill": "orange"},
        ],
    },
    "premium": {
        "name": "Premium",
        "image_brief": "Dark, luxurious surface (slate, dark wood or linen), low-key lighting, the fruit as a "
                       "precious object; a calm dark band at the top.",
        "elements": [
            # was 4% type, letter-spaced, one line: unreadable on a phone (feedback 2026-10-30)
            {"type": "text", "field": "headline", "box": [0, 0.02, 1, 0.17], "size": 0.075, "weight": "semibold",
             "align": "center", "color": "cream", "max_lines": 2, "upper": True, "tracking": 0.06},
        ],
    },
    "scroll_stopping": {
        "name": "Scroll Stopping",
        "image_brief": "Bold, high-contrast, saturated shot of the fruit (dramatic cut, splash or close-up) with "
                       "a calm area in the top half.",
        "elements": [
            {"type": "scrim", "from": "top", "box": [0, 0.0, 1, 0.6], "strength": 0.55},
            {"type": "text", "field": "headline", "box": [0, 0.02, 1, 0.36], "size": 0.11, "weight": "bold",
             "align": "left", "color": "ink", "max_lines": 4, "upper": True, "highlight": "orange"},
            {"type": "text", "field": "sub", "box": [0, 0.41, 0.85, 0.1], "size": 0.045, "weight": "semibold",
             "align": "left", "color": "white", "max_lines": 2},
        ],
    },
    "craving_macro": {
        "name": "Craving Macro",
        "image_brief": "Extreme close-up of the fruit's flesh and texture, juicy and appetising, filling the frame.",
        "elements": [],
    },
    "supermarket_contrast": {
        "name": "Supermarket Contrast",
        "image_brief": "Split frame below the top 25%: on the left a dull, pale, fibrous supermarket fruit under "
                       "flat light; on the right our fruit, ripe and vivid. The top 25% is plain for a headline.",
        "elements": [
            {"type": "block", "box": [-0.2, 0.0, 1.4, 0.22], "fill": "cream", "bleed": True},
            {"type": "text", "field": "headline", "box": [0, 0.03, 1, 0.17], "size": 0.07, "weight": "bold",
             "align": "center", "color": "ink", "max_lines": 2},
        ],
    },
    "seasonal_drop": {
        "name": "Seasonal Drop",
        "image_brief": "Dark, dramatic scene with the fruit lit like a hero; the top 40% is dark and calm.",
        "price_slot": True,
        "elements": [
            {"type": "scrim", "from": "top", "box": [0, 0.0, 1, 0.5], "strength": 0.6},
            {"type": "badge", "field": "badge", "box": [0, 0.0, 1, 0.05], "size": 0.032},
            {"type": "text", "field": "headline", "box": [0, 0.07, 1, 0.24], "size": 0.1, "weight": "bold",
             "align": "left", "color": "white", "max_lines": 3, "upper": True},
            {"type": "text", "field": "sub", "box": [0, 0.31, 1, 0.08], "size": 0.045, "weight": "medium",
             "align": "left", "color": "cream", "max_lines": 2},
            {"type": "text", "field": "price", "box": [0, 0.90, 1, 0.08], "size": 0.055, "weight": "bold",
             "align": "left", "color": "white", "max_lines": 1},
        ],
    },
    # Claude-invented scenes: headline + sub over the top, CTA at the bottom
    # ── native lo-fi and offer-first (brief S3; playbook › Styles to test next) ──
    "notes_list": {
        "lofi": True,
        "name": "Notes-app list",
        "image_brief": "The fruit on a real kitchen surface in the lower 40% of the frame; the top 60% plain and "
                       "calm (it will be covered by a notes page).",
        "elements": [
            {"type": "block", "box": [0, 0.0, 1, 0.66], "fill": "paper", "radius": 0.03},
            {"type": "text", "field": "headline", "box": [0.06, 0.03, 0.88, 0.13], "size": 0.07, "weight": "bold",
             "align": "left", "color": "ink", "max_lines": 2},
            {"type": "list", "field": "items", "box": [0.06, 0.17, 0.88, 0.46], "size": 0.05, "color": "ink",
             "bullet": "–", "max_items": 6},
        ],
    },
    "text_thread": {
        "lofi": True,
        "name": "Text-message thread",
        "image_brief": "The fruit cut open in the lower 35% of the frame; the rest a soft, plain, out-of-focus "
                       "background for message bubbles.",
        "elements": [
            {"type": "text", "field": "sender", "static": _brand.name(), "box": [0.0, 0.0, 1, 0.07], "size": 0.04,
             "weight": "semibold", "align": "center", "color": "auto", "max_lines": 1},
            {"type": "bubbles", "field": "items", "box": [0.0, 0.09, 1, 0.59], "size": 0.045, "fill": "orange",
             "max_items": 4},
            {"type": "text", "field": "headline", "box": [0, 0.72, 1, 0.12], "size": 0.06, "weight": "bold",
             "align": "center", "color": "auto", "max_lines": 2},
        ],
    },
    "post_it": {
        "lofi": True,
        "name": "Post-it note",
        "image_brief": "The fruit whole and cut on a real surface, filling the lower half; the upper half calm "
                       "for a square sticky note.",
        "elements": [
            {"type": "block", "box": [0.12, 0.02, 0.76, 0.56], "fill": "postit", "radius": 0.01},
            {"type": "text", "field": "headline", "box": [0.18, 0.06, 0.64, 0.16], "size": 0.075, "weight": "bold",
             "align": "left", "color": "ink", "max_lines": 2},
            {"type": "list", "field": "items", "box": [0.18, 0.23, 0.64, 0.32], "size": 0.05, "color": "ink",
             "bullet": "·", "max_items": 3},
        ],
    },
    "comments_screenshot": {
        "name": "Real comments",
        "image_brief": "The fruit on a real surface in the lower 35% of the frame; the rest calm and plain.",
        "elements": [
            {"type": "cards", "field": "reviews", "kind": "comment", "box": [0, 0.0, 1, 0.66], "size": 0.042,
             "max_items": 3},
            {"type": "text", "field": "headline", "box": [0, 0.70, 1, 0.12], "size": 0.06, "weight": "bold",
             "align": "center", "color": "auto", "max_lines": 2},
        ],
    },
    "review_mashup": {
        "name": "Review mashup",
        "image_brief": "The fruit on a real surface in the lower 35% of the frame; the rest calm and plain.",
        "elements": [
            {"type": "cards", "field": "reviews", "kind": "review", "box": [0, 0.0, 1, 0.70], "size": 0.042,
             "max_items": 3},
            {"type": "text", "field": "headline", "box": [0, 0.74, 1, 0.12], "size": 0.06, "weight": "bold",
             "align": "center", "color": "auto", "max_lines": 2},
            {"type": "button", "field": "cta", "box": [0, 0.88, 1, 0.12], "size": 0.042, "fill": "orange"},
        ],
    },
    "handwritten_letter": {
        "lofi": True,
        "name": "Handwritten letter",
        "image_brief": "The fruit and the open box on a real wooden table in the lower 35% of the frame; the rest "
                       "calm and plain (it will be covered by a sheet of letter paper).",
        "elements": [
            {"type": "block", "box": [0.02, 0.0, 0.96, 0.68], "fill": "paper", "radius": 0.01},
            {"type": "text", "field": "headline", "box": [0.08, 0.03, 0.84, 0.10], "size": 0.075, "weight": "hand",
             "align": "left", "color": "ink", "max_lines": 1},
            {"type": "list", "field": "items", "box": [0.08, 0.14, 0.84, 0.42], "size": 0.06, "weight": "hand",
             "color": "ink", "bullet": "", "max_items": 5},
            {"type": "text", "field": "sub", "box": [0.40, 0.57, 0.52, 0.09], "size": 0.06, "weight": "hand",
             "align": "right", "color": "ink", "max_lines": 1, "prefix": "— "},
        ],
    },
    "offer_banner": {
        "name": "Offer-first banner",
        "image_brief": "The fruit or the open box as the hero in the lower 60% of the frame; the top 35% plain.",
        "elements": [
            {"type": "block", "box": [0, 0.0, 1, 0.30], "fill": "orange", "bleed": True},
            {"type": "text", "field": "headline", "box": [0.04, 0.03, 0.92, 0.17], "size": 0.09, "weight": "bold",
             "align": "center", "color": "ink", "max_lines": 2},
            {"type": "text", "field": "sub", "box": [0.04, 0.20, 0.92, 0.08], "size": 0.045, "weight": "medium",
             "align": "center", "color": "ink", "max_lines": 1},
            {"type": "button", "field": "cta", "box": [0, 0.88, 1, 0.12], "size": 0.045, "fill": "espresso"},
        ],
        "price_slot": True,
    },
    "listicle_card": {
        "name": "Listicle card",
        "image_brief": "One fruit, cut open, as the hero in the middle of the frame; calm space above and below.",
        "elements": [
            {"type": "badge", "field": "badge", "box": [0, 0.0, 0.3, 0.07], "size": 0.045},
            {"type": "text", "field": "headline", "box": [0, 0.09, 1, 0.14], "size": 0.08, "weight": "bold",
             "align": "left", "color": "auto", "max_lines": 2},
            {"type": "text", "field": "sub", "box": [0, 0.84, 1, 0.14], "size": 0.05, "weight": "medium",
             "align": "left", "color": "auto", "max_lines": 2},
        ],
    },
    "scene": {
        "name": "Scene",
        "image_brief": "Keep the top 30% of the frame calm and uncluttered for a headline, and the bottom 12% "
                       "calm for a button.",
        "elements": [
            {"type": "scrim", "from": "top", "box": [0, 0.0, 1, 0.36], "strength": 0.5},
            {"type": "text", "field": "headline", "box": [0, 0.0, 1, 0.2], "size": 0.08, "weight": "bold",
             "align": "center", "color": "auto", "max_lines": 3},
            {"type": "text", "field": "sub", "box": [0, 0.2, 1, 0.1], "size": 0.042, "weight": "medium",
             "align": "center", "color": "auto", "max_lines": 2},
            {"type": "button", "field": "cta", "box": [0, 0.9, 1, 0.1], "size": 0.042, "fill": "white"},
        ],
    },
}


def template_for(variant: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(variant, dict):                 # a layout built for one render (animations' video stacks)
        return variant
    return TEMPLATES.get(variant) or TEMPLATES["scene"]


def copy_fields(variant: str) -> list[str]:
    """The copy fields a template can draw (fixed labels, like the text thread's sender, aren't copy)."""
    return [e["field"] for e in template_for(variant)["elements"] if e.get("field") and not e.get("static")]


def review_kind(variant: str) -> str | None:
    """'review' or 'comment' when the layout shows real quotes from the library, else None."""
    for e in template_for(variant)["elements"]:
        if e.get("type") == "cards":
            return e.get("kind", "review")
        if e.get("field") == "review":
            return "review"
    return None


def template_list() -> list[dict[str, Any]]:
    return [{"key": k, "name": t["name"], "fields": copy_fields(k), "price_slot": bool(t.get("price_slot")),
             "review_kind": review_kind(k)} for k, t in TEMPLATES.items()]
