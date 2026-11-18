"""
Hook University: the house hook method, reconnected from the original generator
(legacy/generation/hook_generator.py) for Ad Studio's Hooks & ideas and the auto-filled profile.

  - Every hook = Desire + Conflict + Partial Solution, one of the 12 archetypes (A1-A12),
    in prompts/hook-university-system (the original system prompt).
  - Every hook also gets a hook_type, its opening move, from the Meta Creative Playbook
    (knowledge/hooks.yaml): a separate tag from the archetype, which is its structure.
  - Each batch gets one random diversity angle and a required angle mix (sensory, ripeness window,
    discovery, this run's angle, then rarity / gift / health / value / novelty).
  - Hooks already saved for the product are "exhausted territory".
  - Banned templates are caught in code, then the original hook QA (placeholders, length,
    near-duplicates, repeated openings), and the playbook's checks: no personal-attribute copy,
    and emoji only in the body of about 15% of hooks (1-2 at most). Short of good hooks, it asks once
    more with everything seen so far forbidden.

Winning Meta patterns (adstudio.winning) and an operator idea still steer it, as before; approved
facts and banned words come in through chat._system (facts.prompt_block).
"""
from __future__ import annotations

import random
import re
from typing import Any

from creative_intelligence import knowledge, prompts

ANGLES = [
    "sensory immersion — taste, texture, aroma, mouthfeel, the moment of first bite",
    "discovery moment — the first time someone encounters this fruit and is astonished",
    "gift and occasion — sending to family or friends, surprising someone with something rare",
    "supermarket vs reality — what's missing from grocery store produce and why this is different",
    "the ripeness window — the narrow 24-48h when it's perfect and why that's hard to capture",
    "health, honestly — what the fruit contains (only nutrients the approved facts name, with their source); "
    "'can help', never 'will'; never cure or treat",
    "rarity and status — this is a rare find that most people will never taste",
    "social proof frame — other adventurous eaters who discovered it and couldn't go back",
    "price-to-experience — a premium fruit experience that's cheaper than a restaurant dessert",
    "curiosity and novelty — something most people have never heard of, let alone tasted",
    "contrast reversal — something that looks strange but tastes extraordinary",
    "season and scarcity — only available for a few weeks a year, harvested at peak",
    "the specific variety — not just the category but the specific cultivar and why it's superior",
    "kid reaction — children instinctively love it because it's naturally sweet and fun",
    "cook and creator angle — what chefs, bakers, and food lovers use it for",
]

BANNED = [
    "from the supermarket never quite delivers", "you've never tasted", "wishes it could be. hand-picked",
    "the way nature actually intended", "most intensely flavourful", "most intensely flavorful",
    "grocery store.*wishes it could be", "never quite delivers.*this is why",
    "been making people cancel their grocery orders", "grown at peak altitude and harvested the morning your order ships",
    "harvested the morning your order ships",
    "this is real.*avocado|this is real.*mango|this is real.*fruit|this is real.*papaya|this is real.*cherry",
    "grocery.store.*ruined for them forever", "ruined.*for them forever", "the way it should have always tasted",
    "sweet, complex, nothing artificial", "nothing artificial.*the way it should",
    "the way it should", "as it should be", "the way.*should taste", "taste.*as.*intended",
]

SCHEMA = (
    'Return ONLY JSON: {"hooks":[{'
    '"hook_text":"the full hook","archetype":"e.g. A3 — PS → Conflict → Desire",'
    '"hook_type":"the opening move, one hook type key",'
    '"d":"Desire: what the buyer wants","c":"Conflict: the specific obstacle",'
    '"ps":"Partial Solution: what is teased without completing the loop",'
    '"clarity":"why a 4-year-old would get it instantly",'
    '"headline":"3-7 word on-image headline","subhead":"<=12 word subhead",'
    '"body":"the primary text, 1-2 sentences","cta":"one allowed CTA"}]}'
)


def is_cliche(text: str) -> bool:
    low = (text or "").lower()
    return any(re.search(f, low) for f in BANNED)


def system() -> str:
    return prompts.text("hook-university-system")


def emoji_slots(n: int, rng: random.Random | None = None) -> list[int]:
    """Which hooks (1-based) may carry emoji in their body: about the playbook's share of the batch (decision e)."""
    rng = rng or random
    want = n * knowledge.load("rules")["emoji"]["share_of_ads"]
    k = int(want) + (rng.random() < want - int(want))       # on average exactly the share
    return sorted(rng.sample(range(1, n + 1), min(k, n)))


def request(product_name: str, n: int, angle: str, recent: list[str], patterns_block: str = "",
            idea: str = "", emoji: list[int] | None = None) -> str:
    """The batch brief: angle mix, this run's angle, exhausted hooks, then the JSON shape.
    emoji: the hook numbers whose body may carry 1-2 emoji (emoji_slots); none anywhere else."""
    short = angle.split(" — ")[0]
    mix = ["Hook 1: sensory — taste, texture, aroma, mouthfeel. Specific and evocative.",
           "Hook 2: ripeness — picked ripe, the short window when it's perfect (no places, countries or regions).",
           "Hook 3: discovery — someone tasting it for the very first time, astonished.",
           f"Hook 4: {short} — this run's unique angle (below).",
           "Hook 5+: one each from: rarity/scarcity · gift/occasion · health (approved nutrients only) · price-value · novelty"]
    parts = [f"Write {n} scroll-stopping Meta ad hooks for {product_name} using the D+C+PS framework."]
    if patterns_block:
        parts.append(patterns_block + "\nUse each pattern for FORMAT and voice only; the angle mix below decides "
                     "each hook's territory.")
    if idea.strip():
        parts.append(f"IDEA TO TEST (from the operator — every hook must execute this idea):\n{idea.strip()}")
    parts.append(
        "## HARD RULES\n"
        f"1. Every hook names the actual product: {product_name}. No [placeholders].\n"
        "2. One archetype per hook (A1–A10; label it). Spread them: no archetype twice.\n"
        "3. The loop is never completed. The PS teases a purchase or the eating experience, not content.\n"
        "4. Each hook has a completely different core angle. At most 1 grocery-store comparison; "
        "at most 1 hook opens with a question.\n"
        "5. Keep hook_text under 140 characters.\n"
        "6. Never say where it comes from or where it is grown: no country, region, farm location or "
        "'imported from' (house rule).\n"
        f"7. Give every hook a hook_type, one of: {', '.join(knowledge.hook_type_keys())}. It is separate from "
        "the archetype; no type more than twice.\n"
        "8. Never speak to a personal condition or identity: speak to what people want, not what they are.\n"
        + (f"9. Emoji allowed: only in the body of hook{'s' if len(emoji) > 1 else ''} "
           f"{', '.join(map(str, emoji))}, 1-2 at most. No emoji anywhere else.\n\n" if emoji
           else "9. No emoji anywhere.\n\n")
        + "## REQUIRED ANGLE MIX — each hook gets its own territory\n" + "\n".join(mix)
        + f"\n\n## THIS RUN'S UNIQUE ANGLE (Hook 4)\n→ {angle}")
    if recent:
        parts.append("## ALREADY WRITTEN FOR THIS PRODUCT — exhausted territory; do not reuse their opening, "
                     "metaphor or central idea\n" + "\n".join(f"- {h}" for h in recent[:30]))
    parts.append(SCHEMA)
    return "\n\n".join(parts)


COMPARISON = re.compile(r"grocery|supermarket|store-bought|in stores", re.I)
# House rule: no origin claims (country, region, where it is grown). The product's own name is allowed.
ORIGIN = re.compile(r"\b(grow(?:n|s) in (?!naturally|all by|by itself|on its own)|from the (?:farms|orchards|hills|valleys|groves) of|imported|native to|"
                    r"vietnam\w*|thai(?:land)?|mexic\w*|florid\w*|californ\w*|hawai\w*|philippin\w*|filipino|"
                    r"peru\w*|ecuador\w*|brazil\w*|colombi\w*|india|indian|pakistan\w*|taiwan\w*|japan\w*|"
                    r"chin(?:a|ese)|kore(?:a|an)|asia|asian|south america\w*|central america\w*|the tropics of)\b", re.I)


def origin_claim(text: str, product_name: str = "") -> bool:
    for w in re.findall(r"[A-Za-z]+", product_name or ""):
        text = re.sub(rf"\b{re.escape(w)}\b", " ", text, flags=re.I)
    return bool(ORIGIN.search(text or ""))


EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F]")
ON_IMAGE = ("hook_text", "headline", "subhead", "cta")


def emoji_count(text: str) -> int:
    return len([c for c in EMOJI.findall(text or "") if c != "\uFE0F"])


def screen(hooks: list[dict[str, Any]], seen: list[str], product_name: str = "") -> tuple[list[dict], list[dict]]:
    """(passed, flagged), in order, by the Hook University rules code can check: banned templates, the
    original hook QA (placeholders, length, near-duplicates, repeated openings, at a stricter duplicate
    threshold), the product named, no origin claim, one archetype once, at most one grocery-store comparison;
    and the playbook's: no personal-attribute copy, emoji only in the body (1-2) of about 15% of the batch.
    A hook_type that isn't a playbook type is cleared. Repeats in the batch are dropped."""
    from creative_intelligence.adstudio.hook_qa import QAConfig, filter_hooks
    uniq, texts = [], set()
    for h in hooks:
        t = (h.get("hook_text") or h.get("headline") or "").strip()
        if t and t not in texts:
            texts.add(t)
            uniq.append((h, t))
    results = filter_hooks([t for _, t in uniq], source_hooks=seen,
                           config=QAConfig(diversity="high", duplicate_threshold=0.5))
    words = re.findall(r"[a-z]+", product_name.lower())
    name_words = [w for w in (words[:-1] if len(words) > 1 else words) if len(w) > 2]   # "sunrise mango" -> "sunrise", not just "mango"
    passed, flagged = [], []
    archetypes: set[str] = set()
    comparisons = 0
    types = set(knowledge.hook_type_keys())
    em = knowledge.load("rules")["emoji"]
    emoji_left = max(1, int(len(uniq) * em["share_of_ads"] + 0.5))
    for (h, t), r in zip(uniq, results):
        flags = (["banned_template"] if is_cliche(t) else []) + [
            f for f in r.flags if not f.startswith(("generic:", "low_specificity:"))]
        low = t.lower()
        if name_words and not any(w in low for w in name_words):
            flags.append("product_not_named")
        if origin_claim(t, product_name):
            flags.append("origin_claim")
        if any(knowledge.personal_attribute(h.get(f) or "") for f in (*ON_IMAGE, "body")):
            flags.append("personal_attribute")
        if any(emoji_count(h.get(f) or "") for f in ON_IMAGE):
            flags.append("emoji_on_image")
        if (body_emoji := emoji_count(h.get("body") or "")) > em["max_per_ad"]:
            flags.append("too_many_emoji")
        if (h.get("hook_type") or "") not in types:
            h["hook_type"] = ""
        code = (re.match(r"\s*(A\d+)", h.get("archetype") or "") or [None, ""])[1]
        if not flags and code and code in archetypes:
            flags.append(f"archetype_repeated: {code}")
        if not flags and COMPARISON.search(t):
            if comparisons:
                flags.append("second_grocery_comparison")
        if not flags and body_emoji and not emoji_left:
            flags.append("emoji_over_share")
        h["qa_flags"] = flags
        if not flags:
            archetypes.add(code)
            comparisons += bool(COMPARISON.search(t))
            emoji_left -= bool(body_emoji)
        (flagged if flags else passed).append(h)
    return passed, flagged


def pick_angle(rng: random.Random | None = None) -> str:
    return (rng or random).choice(ANGLES)
