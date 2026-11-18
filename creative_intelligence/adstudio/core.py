"""
Core photos — the fixed, repeatable PDP image set for every product (and, since 2026-11-13, for candles
and other products, each type with its own house direction: TYPE_KINDS, PRODUCT_SHOTS).

  core1  Fruit in our white box + cut & uncut fruit on a wooden cutting board.
  core2  Close, focused cut & uncut fruit on the board; box + foliage blurred behind.
  core3  Lifestyle — cut / styled fruit, usually in a pretty feminine hand.

Unlike the Images tab (Claude invents diverse scenes), core shots are a locked
house style: the art direction lives here, and only the fruit changes. The
fruit's reference photos (usually imported from its Shopify listing) go to
Nano Banana as image_input so skin, flesh and the box look like ours.

Also: Shopify import (title, description, variants → infographic rows,
product images → reference photos) and a Claude helper that drafts the
fruit's whole/cut appearance from its reference photos.
"""
from __future__ import annotations

import json
import re
from typing import Any
from creative_intelligence import prompts

CORE_KINDS = {
    "core1": "Core 1 · Box + board",
    "core2": "Core 2 · Close-up focus",
    "core3": "Core 3 · Lifestyle",
}
DEFAULT_ASPECT = "1:1"

PHOTO_ROLES = {
    "core1": "Core 1 layout",
    "core2": "Core 2 layout",
    "core3": "Lifestyle",
    "infographic": "Infographic (never sent)",
    "other": "Other fruit (never sent)",
}

from creative_intelligence import brand as _brand
BOX = _brand.get("packaging_detail")          # the brand's packaging, as the image model should draw it

BOARD = (
    "A warm-toned acacia/teak wooden serving board with visible grain (a paddle "
    "board with a handle is typical)."
)

SETTING = (
    "Outdoors in a lush garden: glossy tropical green foliage (monstera, banana "
    "and palm leaves) behind and around the box. Bright, even natural daylight; "
    "fresh, vibrant, true-to-life colour; the fruit looks perfectly ripe."
)

# The house layouts, written from the owner's real photos. Geometry is explicit
# because the image model defaults to a top-down "box on a table" view.
SHOTS = {
    "core1": (
        "COMPOSITION — straight-on hero shot, square.\n"
        "- CAMERA: at EYE LEVEL with the scene, lens roughly level with the "
        "middle of the box, shooting HORIZONTALLY straight through the scene. "
        "NOT from above, NOT a top-down or 45° downward view.\n"
        "- THE BOX is PROPPED UP so it stands nearly VERTICAL, its open face "
        "turned toward the camera — we look straight INTO the box and see every "
        "fruit face-on, like a display case. It is NOT lying flat facing the "
        "sky. The box fills roughly the TOP THREE-QUARTERS of the frame and "
        "nearly touches the top and both side edges of the photo. Slivers of "
        "foliage (and maybe a whole fruit or two) peek out around its edges.\n"
        "- THE BOARD runs across the BOTTOM QUARTER of the frame, in front of "
        "and slightly overlapping the bottom of the box, almost reaching both "
        "side edges, set slightly askew (rotated a few degrees) for a natural, "
        "styled look.\n"
        "- ON THE BOARD: a SMALL number of freshly cut pieces, placed "
        "deliberately and SYMMETRICALLY, standing upright with their cut faces "
        "pointing DIRECTLY at the camera, filling the board. Neat and "
        "intentional — never scattered, piled or thrown on. For a large fruit "
        "this is typically two halves side by side; for small fruit, a tidy "
        "symmetric row.\n"
        "- Everything sharp, balanced, abundant and clean."
    ),
    "core2": (
        "COMPOSITION — tight, low, angled close-up with very shallow focus, "
        "square. Built on DEPTH and a DIAGONAL, not symmetry.\n"
        "- CAMERA: low, just above board height, looking ALONG the board at an "
        "angle (a three-quarter view) — not square-on to the scene, not from "
        "above. Shot wide open (~f/1.8): only one plane is sharp.\n"
        "- THE BOARD runs DIAGONALLY from the lower-left out of frame, receding "
        "toward the right into the distance, on a pale surface.\n"
        "- HERO PIECE: one large cut piece, lower-left of centre, cut face "
        "turned toward the camera, filling roughly half the frame. It is the "
        "ONLY tack-sharp thing in the image — flesh texture and seeds in fine "
        "detail. A little whole-fruit skin may show just behind it.\n"
        "- SECOND PIECE: a second cut piece further back along the diagonal, "
        "upper-right, smaller with distance and already softly OUT OF FOCUS. "
        "It adds depth, not balance — the frame is deliberately ASYMMETRIC.\n"
        "- BACKGROUND (top third): whole fruit large and close behind, HEAVILY "
        "blurred into soft shapes with glimpses of kraft packing paper between "
        "them. The box itself is NOT clearly visible — no box edges, no crisp "
        "detail.\n"
        "- FOREGROUND FRAMING: an out-of-focus monstera leaf in the lower-right "
        "corner (a sliver of leaf in the lower-left corner is fine).\n"
        "- Tight crop; bright, fresh, slightly punchy colour."
    ),
}

# Core 3 rotates through lifestyle setups so repeat runs stay varied.
LIFESTYLE = [
    "A pretty, slender, youthful feminine hand with a clean, natural manicure "
    "(soft nude or blush polish) holds freshly cut pieces up toward the camera, "
    "flesh facing the lens, against softly blurred garden greenery and "
    "flowers. Close crop on hand and fruit.",
    "A slender feminine hand with a neat manicure enjoys the fruit the way it is "
    "naturally eaten — e.g. scooping the flesh with a small gold spoon, peeling "
    "back the skin, or lifting a bite-sized piece — over a small ceramic plate "
    "of cut fruit on a sunlit linen-covered table.",
    "Styled overhead flat-lay: the fruit cut into halves, wedges or slices, "
    "arranged beautifully on a pale ceramic plate on warm travertine or marble, "
    "with a few whole fruit and a linen napkin; a slender manicured hand "
    "reaches in to pick up a piece.",
    "Bright, airy kitchen counter by a window in soft morning light: cut fruit "
    "on a wooden board beside a bowl of yogurt or a glass of sparkling water; "
    "a slender manicured feminine hand holds a cut piece mid-frame.",
    "Aesthetic brunch table outdoors in dappled sun: an elegant plate of the "
    "cut fruit, glassware and fresh flowers softly blurred; a slender feminine "
    "hand with delicate gold rings holds a cut half close to the camera, "
    "looking irresistibly juicy.",
]

TECHNICAL = (
    "Photorealistic, high-end food and product photography shot on a full-frame "
    "camera. True micro-texture on skin and flesh (pores, fibres, seeds, juice "
    "glisten). Natural colour, no oversaturation, no HDR, no plastic CGI sheen."
)

NEGATIVE = (
    "NO TEXT anywhere — no words, letters, numbers, logos, stickers, price tags, "
    "watermarks, or printing on the box. Do not include: fruit that is not this "
    "product, look-alike or generic stand-in varieties, bruised or rotten fruit, "
    "unripe-looking flesh, cartoon/illustration/3D look, extra or malformed "
    "fingers, distorted hands, messy or scattered arrangements, cluttered props."
)


# ─────────────────────────────────────────────────────────────────────────────
# Other product types (2026-11-13: "we should be able to do product shots for other product types as well"): the same
# three slots, each type with its own house direction. The product's reference photos stay the source of truth.
# ─────────────────────────────────────────────────────────────────────────────
TYPE_KINDS = {
    "fruit": CORE_KINDS,
    "candle": {"core1": "Core 1 · Hero", "core2": "Core 2 · Flame close-up", "core3": "Core 3 · Lifestyle"},
    "other": {"core1": "Core 1 · Hero", "core2": "Core 2 · Detail close-up", "core3": "Core 3 · In use"},
}

PRODUCT_SHOTS = {
    "candle": {
        "core1": (
            "COMPOSITION — straight-on hero shot, square. The candle, lit, stands centred on a warm natural surface "
            "(pale travertine, oak or linen), camera at the height of the label, shooting level so the vessel reads "
            "true with no distortion. The candle fills about the middle half of the frame; its label is sharp, "
            "straight and fully legible exactly as in the reference photos. One or two quiet props that echo its "
            "scent notes sit softly behind it. A soft, warm, uncluttered background."),
        "core2": (
            "COMPOSITION — close, low, angled detail shot, square, very shallow focus. The flame and the glossy melt "
            "pool fill the upper part of the frame and are the only tack-sharp things; the rim and the top of the "
            "label fall softly out of focus. Warm glow on the wax, a dim cosy background with soft bokeh."),
    },
    "other": {
        "core1": (
            "COMPOSITION — clean hero shot, square. The product alone, centred on a seamless soft-toned studio sweep, "
            "camera level with its front so it reads true with no distortion. It fills about the middle half of the "
            "frame; every printed word, label and logo on it is sharp and exactly as in the reference photos. Soft, "
            "even light with a gentle shadow beneath it. Nothing else in the frame."),
        "core2": (
            "COMPOSITION — tight detail close-up, square, shallow focus. The product's most distinctive feature (its "
            "texture, material, finish or contents) fills most of the frame and is tack-sharp; the rest of the "
            "product falls softly out of focus. Bright, clean light."),
    },
}

PRODUCT_LIFESTYLE = {
    "candle": [
        "Evening living room: the lit candle on a side table beside a stack of books and a linen throw, soft lamp "
        "light, the room softly blurred behind.",
        "A slender hand with a natural manicure lights the candle with a long match, close crop on the hand, the "
        "flame and the label.",
        "A calm bathroom shelf: the lit candle beside folded towels and a small vase of greenery, soft daylight.",
        "A dinner table at dusk: the lit candle among glassware and a few flowers, warm and softly blurred around it.",
    ],
    "other": [
        "The product in use in a bright, real home setting that suits it, held or used by a slender hand with a "
        "natural manicure, the product sharp and its label legible.",
        "The product on a sunlit table among two or three everyday things it belongs with, softly blurred behind.",
        "A close, natural moment of someone using the product, the product the clear hero of the frame.",
    ],
}

def shot_spec(kind: str, variant: int = 0, ptype: str = "fruit") -> str:
    """The house direction for a shot kind (core3 picks a lifestyle setup), for this product type."""
    if ptype in PRODUCT_SHOTS:
        if kind == "core3":
            setups = PRODUCT_LIFESTYLE[ptype]
            return ("COMPOSITION — lifestyle shot, square. " + setups[variant % len(setups)]
                    + " The product looks exactly as in the reference photos, its label sharp and legible. Any hands "
                    "are anatomically perfect, at most two.\nLIGHT & MOOD: soft, warm natural light; calm and "
                    "aspirational.")
        return PRODUCT_SHOTS[ptype][kind]
    if kind == "core3":
        return ("COMPOSITION — lifestyle shot, square. The fruit is shown cut and "
                "served in an aesthetic, mouth-watering way. "
                + LIFESTYLE[variant % len(LIFESTYLE)]
                + " Hands must be anatomically perfect and elegant.\n"
                "LIGHT & MOOD: soft, warm natural daylight; airy, feminine, "
                "aspirational and appetising.")
    if kind == "core2":
        return (f"{SHOTS[kind]}\n\n{BOARD}\nLight: bright natural daylight; the "
                "fruit looks perfectly ripe.")
    return f"{SHOTS[kind]}\n\n{BOX}\n{BOARD}\n\nSETTING & LIGHT\n{SETTING}"


def build_core_prompt(product: dict[str, Any], kind: str, variant: int = 0,
                      corrections: str = "", brief: str = "",
                      n_layout: int = 0, n_fruit: int = 0) -> str:
    """
    Assemble the Nano Banana prompt for one core shot of one fruit.

    image_input order is [n_layout layout refs][n_fruit fruit refs]; the prompt
    names each group so the model knows which image to copy the framing from
    and which to copy the fruit from.
    """
    if kind not in CORE_KINDS:
        raise ValueError(f"unknown core kind {kind!r}")
    ptype = product_type(product)
    if ptype != "fruit":
        return _product_prompt(product, ptype, kind, variant, corrections, brief, n_layout, n_fruit)
    name = product.get("name", "the fruit")
    look = (product.get("physical_desc") or "").strip()
    varieties = (product.get("varieties") or "").strip()
    notes = (product.get("notes") or "").strip()

    parts = [f"Create a premium square product photograph of {name}."]
    refs = []
    if n_layout:
        idx = ", ".join(str(i + 1) for i in range(n_layout))
        refs.append(
            f"Reference image{'s' if n_layout > 1 else ''} {idx} = the LAYOUT "
            f"REFERENCE: a real photo from our shoot. Reproduce its composition "
            f"exactly — camera height and angle, how the box is propped and "
            f"oriented, how much of the frame the box and board fill, where the "
            f"board sits and its tilt, and the count and symmetric arrangement of "
            f"the cut pieces. (If it shows a different fruit, copy ONLY the "
            f"layout, never that fruit.)")
    if n_fruit:
        a = n_layout + 1
        idx = ", ".join(str(a + i) for i in range(n_fruit))
        refs.append(
            f"Reference image{'s' if n_fruit > 1 else ''} {idx} = the FRUIT: the "
            f"single source of truth for how {name} looks whole AND cut — skin "
            f"colour and texture, shape, size, flesh colour, seed pattern.")
    if refs:
        parts.append("REFERENCE IMAGES\n" + "\n".join(refs))
    if corrections.strip():
        parts.append(
            "CORRECTIONS — HIGHEST PRIORITY\nA previous attempt got these wrong; fix "
            "each exactly, above every other instruction:\n" + corrections.strip())
    if brief.strip():
        parts.append(f"SHOT BRIEF (from the art director, for this exact fruit)\n{brief.strip()}")
    if look:
        parts.append(f"THE FRUIT\n{look}")
    if varieties:
        parts.append(
            "VARIETY BOX — show a MIX of all of these varieties together, clearly "
            "distinguishable, and cut at least one of each so every flesh colour "
            f"is visible:\n{varieties}")
    if notes:
        parts.append(f"OPERATOR NOTES (authoritative)\n{notes}")
    parts.append(shot_spec(kind, variant))
    parts.append(f"TECHNICAL\n{TECHNICAL}")
    parts.append(NEGATIVE)
    return "\n\n".join(parts)



def product_type(product: dict[str, Any]) -> str:
    """fruit | candle | other. No type set keeps the fruit set (product shots were fruit-only until 2026-11-13)."""
    from creative_intelligence.adstudio.profiles import normalize_type
    if not (product.get("product_type") or "").strip():
        return "fruit"
    t = normalize_type(product.get("product_type"))
    return t if t in TYPE_KINDS else "other"


def _product_prompt(product: dict[str, Any], ptype: str, kind: str, variant: int, corrections: str, brief: str,
                    n_layout: int, n_refs: int) -> str:
    """A candle or other product's shot: the product from its own photos, the type's house direction."""
    from creative_intelligence.adstudio.profiles import profile
    prof = profile(product)
    name = product.get("name", "the product")
    parts = [f"Create a premium square product photograph of {name}.", prof["reference_lock"]]
    refs = []
    if n_layout:
        idx = ", ".join(str(i + 1) for i in range(n_layout))
        refs.append(f"Reference image{'s' if n_layout > 1 else ''} {idx} = the LAYOUT REFERENCE: copy its framing, "
                    "camera angle and light, never the product in it if it is a different one.")
    if n_refs:
        a = n_layout + 1
        idx = ", ".join(str(a + i) for i in range(n_refs))
        refs.append(f"Reference image{'s' if n_refs > 1 else ''} {idx} = THE PRODUCT: the single source of truth for "
                    f"how {name} looks — shape, colours, material, label and every printed word.")
    if refs:
        parts.append("REFERENCE IMAGES\n" + "\n".join(refs))
    if corrections.strip():
        parts.append("CORRECTIONS — HIGHEST PRIORITY\nA previous attempt got these wrong; fix each exactly, above "
                     "every other instruction:\n" + corrections.strip())
    if brief.strip():
        parts.append(f"SHOT BRIEF (from the art director, for this exact product)\n{brief.strip()}")
    if (look := (product.get("physical_desc") or "").strip()):
        parts.append(f"THE PRODUCT\n{look}")
    if (notes := (product.get("notes") or "").strip()):
        parts.append(f"{prof['notes_label'].upper()} (authoritative)\n{notes}")
    parts.append(shot_spec(kind, variant, ptype))
    parts.append(f"TECHNICAL\n{prof['technical']}")
    parts.append("NO TEXT anywhere except what is printed on the product itself. " + prof["negative"])
    return "\n\n".join(parts)

# ─────────────────────────────────────────────────────────────────────────────
# Variants → infographic rows
# ─────────────────────────────────────────────────────────────────────────────
SIZE_NAMES = {2: "SAMPLE", 5: "MEDIUM", 8: "LARGE", 16: "CASE"}
DEFAULT_WEIGHTS = (2, 5, 8)


def parse_variant(title: str) -> dict[str, Any] | None:
    """
    '5lb | 6-8 pcs' / '~2lb | 1 pcs' / '2lb | ~50 pcs' → row dict, or None for
    legacy titles without a piece count ('4lb box').
    """
    m = re.match(r"\s*(~?)\s*(\d+(?:\.\d+)?)\s*lbs?\s*\|\s*(.+?)\s*pcs?\s*$", title, re.I)
    if not m:
        return None
    approx, lb, pcs = m.group(1), float(m.group(2)), m.group(3).strip()
    lb_n = int(lb) if lb.is_integer() else lb
    pcs = re.sub(r"\s*-\s*", "–", pcs)
    fixed_count = bool(re.fullmatch(r"\d+", pcs))
    if approx and fixed_count:          # sold by piece, weight varies (cacao)
        size = f"{pcs} PC"
    else:
        size = SIZE_NAMES.get(lb_n, f"{lb_n} LB")
    return {
        "size": size,
        "weight": f"{approx}{lb_n} {'LB' if lb_n == 1 else 'LBS'}",
        "pieces": pcs,
        "lb": lb_n,
        "weight_varies": bool(approx),
    }


def rows_from_variants(variants: list[dict[str, Any]]) -> tuple[list[dict], str]:
    """
    Shopify variants → (rows, estimate). Rows keep an `on` flag: by default the
    available 2/5/8 lb boxes are on (falling back to every available box).
    """
    rows = []
    for v in variants:
        r = parse_variant(v.get("title", ""))
        if r:
            r["available"] = bool(v.get("available", True))
            r["variant_title"] = v.get("title", "")
            if v.get("price") is not None:
                r["price"] = format_price(v["price"])     # stored at import; Compose re-reads it live
            rows.append(r)
    avail = [r for r in rows if r["available"]] or rows
    defaults = [r for r in avail if r["lb"] in DEFAULT_WEIGHTS]
    chosen = {id(r) for r in (defaults or avail)}
    for r in rows:
        r["on"] = id(r) in chosen
    estimate = "weight" if any(r["weight_varies"] for r in rows) else "pieces"
    return rows, estimate


def bab_row(rows: list[dict[str, Any]]) -> dict[str, str]:
    """The Build-a-Box sample row: the smallest box, labelled SMALL."""
    if not rows:
        return {"size": "SMALL", "weight": "2 LBS", "pieces": ""}
    r = min(rows, key=lambda x: x.get("lb", 99))
    size = r["size"] if r["size"].endswith(" PC") else "SMALL"
    return {"size": size, "weight": r["weight"], "pieces": r["pieces"]}


# ─────────────────────────────────────────────────────────────────────────────
# Shopify import
# ─────────────────────────────────────────────────────────────────────────────
def shopify_js_url(url: str) -> str:
    m = re.match(r"(https?://[^/]+)/(?:collections/[^/]+/)?products/([^/?#.]+)", url.strip())
    if not m:
        raise ValueError("Paste a Shopify product URL (…/products/<handle>)")
    return f"{m.group(1)}/products/{m.group(2)}.js"


def fetch_shopify_product(url: str) -> dict[str, Any]:
    """Public storefront JSON for a product URL (no auth; read-only)."""
    import requests
    r = requests.get(shopify_js_url(url), timeout=20,
                     headers={"User-Agent": _brand.user_agent()})
    r.raise_for_status()
    d = r.json()
    desc = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", d.get("description") or "")).strip()
    images = [("https:" + i) if i.startswith("//") else i for i in d.get("images", [])]
    return {"handle": d.get("handle", ""), "title": d.get("title", ""),
            "description": desc, "variants": d.get("variants", []), "images": images}


def format_price(cents: Any) -> str:
    """Shopify storefront .js prices are integer cents: 3500 -> '$35', 3550 -> '$35.50'."""
    v = int(cents) / 100
    return f"${v:,.0f}" if v == int(v) else f"${v:,.2f}"


def shopify_prices(url: str) -> list[dict[str, Any]]:
    """Live variant prices for a product URL: [{title, price, available}] (read-only)."""
    return [{"title": v.get("title", ""), "price": format_price(v.get("price", 0)),
             "available": bool(v.get("available", True))} for v in fetch_shopify_product(url)["variants"]]


def short_name(title: str) -> str:
    """'Large Cherimoya (Custard Apple)' → 'Large Cherimoya' — infographic label."""
    return re.sub(r"\s*\(.*?\)\s*", " ", title).strip()


# ─────────────────────────────────────────────────────────────────────────────
# Claude: tag reference photos, write per-shot briefs, draft the fruit's look
# ─────────────────────────────────────────────────────────────────────────────
DESCRIBE_SYSTEM = prompts.text("core-describe-system")


def describe_fruit(product: dict[str, Any], image_data_uris: list[str]) -> dict[str, str]:
    """Return {"physical_desc", "varieties"} drafted by Claude."""
    from creative_intelligence.adstudio.chat import _image_blocks
    ask = (
        f"Fruit: {product.get('name')}\nProduct copy: {product.get('description','')[:1200]}\n\n"
        "Return ONLY JSON: {\"physical_desc\": \"3-5 sentences: whole fruit (size, "
        "shape, skin colour & texture), how it is cut for display (halves, "
        "lengthwise, cross-section, peeled), the cut flesh (colour, texture, "
        "seeds/pit), and how it is typically eaten\", \"varieties\": \"if this is a "
        "mixed/variety box, one line per variety: name — skin — flesh; else empty "
        "string\"}"
    )
    d = _json(_ask(DESCRIBE_SYSTEM, _image_blocks(image_data_uris) + [{"type": "text", "text": ask}], 900)) or {}
    return {"physical_desc": (d.get("physical_desc") or "").strip(),
            "varieties": (d.get("varieties") or "").strip()}

def _json(text: str) -> Any:
    m = re.search(r"[\[{].*[\]}]", text, re.DOTALL)
    try:
        return json.loads(m.group(0)) if m else None
    except json.JSONDecodeError:
        return None


def _ask(system: str, content: list[dict], max_tokens: int = 1200) -> str:
    from creative_intelligence.adstudio.chat import _client, MODEL
    r = _client().messages.create(model=MODEL, max_tokens=max_tokens, system=system,
                                  messages=[{"role": "user", "content": content}])
    return "".join(b.text for b in r.content if getattr(b, "type", "") == "text")


def _numbered(uris: list[str], label: str) -> list[dict]:
    from creative_intelligence.adstudio.chat import _image_blocks
    out: list[dict] = []
    for i, u in enumerate(uris):
        out.append({"type": "text", "text": f"{label} {i + 1}:"})
        out += _image_blocks([u])
    return out


CLASSIFY_SYSTEM = prompts.text("core-classify-system")


def classify_photos(product_name: str, image_data_uris: list[str]) -> list[str]:
    """Return one role (see PHOTO_ROLES) per image, in order."""
    if not image_data_uris:
        return []
    content = _numbered(image_data_uris, "Photo") + [{"type": "text", "text": (
        f"The product is: {product_name}. Return ONLY JSON: "
        '{"roles": ["core1"|"core2"|"core3"|"infographic"|"other", ...]} — '
        f"exactly {len(image_data_uris)} entries, in photo order.")}]
    d = _json(_ask(CLASSIFY_SYSTEM, content, 400)) or {}
    roles = [r if r in PHOTO_ROLES else "core3" for r in (d.get("roles") or [])]
    return (roles + [""] * len(image_data_uris))[:len(image_data_uris)]


BRIEF_SYSTEM = prompts.text("core-brief-system")


def write_shot_brief(product: dict[str, Any], kind: str, variant: int,
                     layout_uris: list[str], fruit_uris: list[str]) -> str:
    """Claude studies the layout + fruit refs and writes a precise brief for this shot."""
    ptype = product_type(product)
    if ptype != "fruit":
        content = (_numbered(layout_uris, "LAYOUT reference") + _numbered(fruit_uris, "PRODUCT reference")
                   + [{"type": "text", "text": (
                       f"Product: {product.get('name')} ({ptype})\n"
                       f"Appearance notes: {product.get('physical_desc') or '(none)'}\n"
                       f"\nHouse direction for this shot ({TYPE_KINDS[ptype][kind]}):\n{shot_spec(kind, variant, ptype)}\n\n"
                       "Write the shot brief.")}])
        return _ask(prompts.text("core-brief-product"), content, 700).strip()
    content = (_numbered(layout_uris, "LAYOUT reference")
               + _numbered(fruit_uris, "FRUIT reference")
               + [{"type": "text", "text": (
                   f"Fruit: {product.get('name')}\n"
                   f"Appearance notes: {product.get('physical_desc') or '(none)'}\n"
                   + (f"Variety box contents: {product['varieties']}\n" if product.get("varieties") else "")
                   + f"\nHouse direction for this shot ({CORE_KINDS[kind]}):\n{shot_spec(kind, variant)}\n\n"
                   "Write the shot brief.")}])
    return _ask(BRIEF_SYSTEM, content, 700).strip()
