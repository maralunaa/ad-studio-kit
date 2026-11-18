"""
Claude, inside Ad Studio.

Two entry points, both able to see the reference photo:
  chat_reply()    — freeform conversation to develop hooks/angles
  suggest_hooks() — returns N structured, saveable hooks for a product

Uses the Anthropic key already in .env. The system prompt is the hook engine
reverse-engineered from the app (Desire + Conflict + Partial Solution).
"""
from __future__ import annotations

import json
import re
from datetime import date
from typing import Any

from creative_intelligence.adstudio.profiles import profile

# Sonnet for copy quality; verified working on this account.
from creative_intelligence import config as _config
from creative_intelligence import knowledge, prompts

MODEL = _config.CI_CLAUDE_COPY_MODEL

SYSTEM = prompts.text("copy-chat-system")


def _system(product: dict[str, Any]) -> str:
    """SYSTEM plus the product type's take on what the buyer desires, and (T6) the approved
    facts and banned claims — the only facts hooks, headlines and briefs may state."""
    return SYSTEM + "\n" + profile(product)["hook_desire"] + "\n" + _facts(product) + "\n"


def _facts(product: dict[str, Any]) -> str:
    import os
    if os.getenv("FACTS_CHECK", "1") != "1":
        return ""
    try:
        from creative_intelligence.adstudio.facts import prompt_block
        from creative_intelligence.db import get_connection
        conn = get_connection()
        try:
            return prompt_block(conn, product)
        finally:
            conn.close()
    except Exception:
        return ""

_HOOK_SCHEMA = (
    'Return ONLY JSON: {"hooks":[{'
    '"hook_text":"the full hook line",'
    '"archetype":"e.g. A7 Most think X but actually Y",'
    '"d":"what the buyer wants","c":"the obstacle","ps":"the purchase/experience teased",'
    '"headline":"<=6 word on-image headline","subhead":"<=12 word subhead",'
    '"body":"1-2 sentence caption","cta":"2-4 word CTA",'
    '"hook_type":"its opening move, one of: $types (or empty if none fits)"'
    '}]}'
)


def _client(log: bool = True, kind: str = "copy"):
    """Claude client through the provider adapter: budget-checked before each call. With log=True
    every call is recorded with its cost; modules that log their own calls pass log=False. kind="qa" (quality
    scores) is never stopped by the daily text cap (costs.NEVER_BLOCKED)."""
    from creative_intelligence.providers import anthropic
    return anthropic.client(kind, log=log)


def _image_blocks(data_uris: list[str]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for uri in data_uris or []:
        m = re.match(r"data:(image/[a-zA-Z0-9.+-]+);base64,(.*)$", uri, re.DOTALL)
        if not m:
            continue
        blocks.append({
            "type": "image",
            "source": {"type": "base64", "media_type": m.group(1), "data": m.group(2)},
        })
    return blocks


SEASON_NOW = {"in": "in season and shipping now", "out": "out of season (pre-order or waiting list)"}


def when_line(product: dict[str, Any], today: date | None = None) -> str:
    """Today's date and the product's season (feedback 2026-10-29: "season ends in May" twice, written in October)."""
    today = today or date.today()
    now = SEASON_NOW.get(product.get("season") or "", "season status unknown")
    return (f"Today is {today:%B %d, %Y}. This product is {now}. Copy must be true today: no season end, start or "
            "countdown dates (\"season ends in May\", \"only until spring\"), even when a fact gives the months; "
            "say \"in season now\" or \"limited season\" only if that matches the status above.")


def _product_line(product: dict[str, Any]) -> str:
    prof = profile(product)
    bits = [f"Product: {product.get('name','(unnamed)')} (type: {prof['label']})", when_line(product)]
    if product.get("scent_notes"):
        bits.append(f"{prof['notes_label']}: {product['scent_notes']}")
    if product.get("physical_desc"):
        bits.append(f"Looks like: {product['physical_desc']}")
    if product.get("varieties"):
        bits.append(f"Varieties: {product['varieties']}")
    if product.get("description"):
        bits.append(f"Description: {product['description']}")
    if product.get("notes"):
        bits.append(
            "OPERATOR CONTEXT & NOTES (authoritative — honor these exactly, and "
            "elaborate them into concrete photographic detail in your scenes):\n"
            f"{product['notes']}")
    return "\n".join(bits)


def chat_reply(messages: list[dict[str, str]], product: dict[str, Any],
               image_data_uris: list[str] | None = None) -> str:
    """messages = [{role, text}]. The reference photo is attached to the FIRST turn."""
    c = _client()
    api_msgs: list[dict[str, Any]] = []
    imgs = _image_blocks(image_data_uris or [])
    for idx, m in enumerate(messages):
        content: list[dict[str, Any]] = []
        if idx == 0 and imgs:
            content.extend(imgs)
        content.append({"type": "text", "text": m["text"]})
        api_msgs.append({"role": m["role"], "content": content})

    r = c.messages.create(
        model=MODEL, max_tokens=1200,
        system=_system(product) + "\n" + _product_line(product),
        messages=api_msgs,
    )
    return "".join(b.text for b in r.content if getattr(b, "type", "") == "text").strip()


def suggest_hooks(product: dict[str, Any], n: int = 4,
                  brief: str = "", image_data_uris: list[str] | None = None,
                  patterns: list[dict[str, Any]] | None = None,
                  idea: str = "", recent: list[str] | None = None) -> list[dict]:
    """
    Return n structured, saveable hooks: Hook University (D+C+PS, archetypes, angle mix, banned
    templates and hook QA; adstudio/hook_university.py) merged with what Ad Studio adds — real
    winning Meta patterns, the operator's idea, approved facts, the reference photo, and on-image
    headline / subhead / body / CTA for every hook.

    patterns — winning patterns (adstudio.winning) to build on; each hook
               comes back with the pattern_id it follows.
    idea     — something the operator wants to test; every hook executes it.
    recent   — hooks already saved for this product (never repeated).
    brief    — legacy free-text steer.
    """
    from creative_intelligence.adstudio import hook_university as HU
    from creative_intelligence.adstudio.winning import prompt_block
    c = _client()
    imgs = _image_blocks(image_data_uris or [])
    recent = list(recent or [])
    angle = HU.pick_angle()
    emoji = HU.emoji_slots(n)
    block = prompt_block(patterns) if patterns else ""
    phrases = customer_phrases(product)
    system = HU.system() + "\n\n" + profile(product)["hook_desire"] + "\n" + _facts(product) + "\n" + (
        "\nThings real customers said in 4–5 star reviews (reuse their words where they fit):\n"
        + "\n".join(f"- {p}" for p in phrases) + "\n" if phrases else "")
    seen: list[str] = []
    passed: list[dict] = []
    flagged: list[dict] = []
    for attempt in range(2):                     # one more pass, everything seen forbidden, if short
        ask = HU.request(product.get("name") or "the product", n * 2 if attempt == 0 else n * 2, angle,
                         recent + seen, block, idea, emoji)
        if brief:
            ask += f"\n\nDirection from me: {brief}"
        if patterns:
            ask = ask.replace('"cta":"one allowed CTA"', '"cta":"one allowed CTA","pattern_id":<the pattern_id this hook follows>')
        content: list[dict[str, Any]] = list(imgs) + [{"type": "text", "text": _product_line(product) + "\n\n" + ask}]
        r = c.messages.create(model=MODEL, max_tokens=4000, system=system,
                              messages=[{"role": "user", "content": content}])
        text = "".join(b.text for b in r.content if getattr(b, "type", "") == "text")
        fresh = [h for h in _parse_hooks(text)
                 if (h.get("hook_text") or h.get("headline")) not in seen]
        seen += [h.get("hook_text") or h.get("headline") for h in fresh]
        passed, bad = HU.screen(passed + fresh, recent, product.get("name") or "")   # earlier passes re-pass first
        flagged += bad
        if len(passed) >= n:
            break
    kept, bad = HU.screen(critique_hooks(product, passed, n, phrases), recent, product.get("name") or "")
    hooks = (kept + flagged + bad)[:n]           # the critic keeps the best clean ones; edits are screened again
    valid = {p["id"] for p in patterns or []}
    for h in hooks:
        try:
            pid = int(h.get("pattern_id"))
        except (TypeError, ValueError):
            pid = None
        h["pattern_id"] = pid if pid in valid else None
        h.setdefault("hook_text", h.get("headline", ""))
        h["prompt_version"] = prompts.stamp("hook-university-system")
    return hooks


CRITIC_MODEL = _config.CI_CLAUDE_CHECK_MODEL


def customer_phrases(product: dict[str, Any], n: int = 8) -> list[str]:
    """Phrases customers repeat in 4–5 star reviews (customer voice), this product's first."""
    try:
        from creative_intelligence.db import get_connection
        conn = get_connection()
        try:
            return [r[0] for r in conn.execute(
                "SELECT theme FROM voice_themes WHERE kind='phrase' AND (product_id=? OR product_id IS NULL)"
                " ORDER BY product_id IS NULL, mentions DESC LIMIT ?", (product.get("id"), n))]
        finally:
            conn.close()
    except Exception:
        return []


def critique_hooks(product: dict[str, Any], hooks: list[dict], n: int, phrases: list[str] | None = None,
                   client: Any = None) -> list[dict]:
    """The hook critic (feedback 2026-10-22): one call scores every candidate against the headline rules, the
    playbook and the approved facts, edits what it can fix, and the best n are kept with the reason
    (critic_score, critic_note). If the critic can't run, the candidates are kept as they were."""
    if len(hooks) <= 1:
        return hooks
    listed = "\n".join(f"[{i}] headline: {h.get('headline', '')} | hook: {h.get('hook_text', '')} | subhead: "
                       f"{h.get('subhead', '')}" for i, h in enumerate(hooks))
    ask = (f"{_product_line(product)}\n\n{_facts(product)}\n"
           + ("Customer words (4–5 star reviews):\n" + "\n".join(f"- {p}" for p in phrases) + "\n\n" if phrases else "")
           + f"CANDIDATES:\n{listed}\n\nReturn ONLY JSON: {{\"reviews\":[{{\"i\":<number>,\"score\":<1-5>,"
             "\"headline\":\"kept or edited\",\"hook_text\":\"kept or edited\",\"reason\":\"one short line\"}}]}"
             " with one entry per candidate.")
    try:
        c = client or _client()
        r = c.messages.create(model=CRITIC_MODEL, max_tokens=3000, system=prompts.text("hook-critic-system"),
                              messages=[{"role": "user", "content": ask}])
        m = re.search(r"\{.*\}", "".join(b.text for b in r.content if getattr(b, "type", "") == "text"), re.DOTALL)
        reviews = json.loads(m.group(0))["reviews"] if m else []
    except Exception:
        return hooks[:n]
    by_i = {int(x["i"]): x for x in reviews if str(x.get("i", "")).isdigit() and int(x["i"]) < len(hooks)}
    ranked = sorted(by_i, key=lambda i: -float(by_i[i].get("score") or 0))
    out = []
    for i in ranked[:n]:
        h, rv = dict(hooks[i]), by_i[i]
        for k in ("headline", "hook_text"):
            if (rv.get(k) or "").strip():
                h[k] = rv[k].strip()
        h["critic_score"], h["critic_note"] = float(rv.get("score") or 0), (rv.get("reason") or "").strip()[:200]
        out.append(h)
    return out or hooks[:n]


HOOK_FIELDS = ("hook_text", "headline", "subhead", "body", "cta")


def repair_hook(product: dict[str, Any], hook: dict[str, Any], problems: list[str], client: Any = None) -> dict | None:
    """Rewrite only what the claims check flagged in one hook (feedback 2026-10-26: four of five new hooks came back
    flagged and unusable): keep its idea and shape, drop or replace each problem with an approved fact or plain
    description. None if Claude can't."""
    fields = {k: hook.get(k) or "" for k in HOOK_FIELDS}
    ask = (f"{_product_line(product)}\n\n{_facts(product)}\n\nTHIS HOOK:\n{json.dumps(fields, ensure_ascii=False)}\n\n"
           f"The claims check found: {'; '.join(problems)}.\nRewrite only what is needed so nothing goes beyond the approved "
           "facts: no other fruit named (not even as a comparison), no invented customers, quotes, numbers or guarantees, "
           "nothing about where it comes from. Keep the idea, the length of each field and the CTA. Return ONLY JSON with "
           f"the same keys: {', '.join(HOOK_FIELDS)}.")
    try:
        c = client or _client()
        r = c.messages.create(model=MODEL, max_tokens=1500, system=prompts.text("hook-university-system"),
                              messages=[{"role": "user", "content": ask}])
        m = re.search(r"\{.*\}", "".join(b.text for b in r.content if getattr(b, "type", "") == "text"), re.DOTALL)
        out = json.loads(m.group(0)) if m else {}
    except Exception:
        return None
    return {k: str(out.get(k) if out.get(k) is not None else fields[k]).strip() for k in HOOK_FIELDS} or None


def extract_hooks(product: dict[str, Any], messages: list[dict[str, str]],
                  image_data_uris: list[str] | None = None) -> list[dict]:
    """Pull every distinct hook already written in a chat into structured JSON.

    Uses the exact wording from the conversation — does not invent new hooks.
    """
    c = _client()
    convo = "\n\n".join(f"{m['role'].upper()}: {m['text']}" for m in messages)
    ask = (
        f"{_product_line(product)}\n\n"
        "Below is a brainstorming conversation. Extract EVERY distinct ad hook that "
        "was written in it into structured JSON. Preserve the EXACT wording of the "
        "hook line, body, and CTA in hook_text / body / cta — do not invent new hooks "
        "or reword them. The one exception: if a hook has no short on-image headline, "
        "distill a punchy ≤6-word `headline` from that hook (concise phrasing of your "
        "own is fine here) so it's ready to place on an image; still keep the full "
        "original line in `hook_text`. Leave body/cta empty only if truly absent.\n\n"
        f"CONVERSATION:\n{convo}\n\n{_HOOK_SCHEMA.replace('$types', ', '.join(knowledge.hook_type_keys()))}"
    )
    content = _image_blocks(image_data_uris or []) + [{"type": "text", "text": ask}]
    r = c.messages.create(
        model=MODEL, max_tokens=3000, system=_system(product),
        messages=[{"role": "user", "content": content}],
    )
    text = "".join(b.text for b in r.content if getattr(b, "type", "") == "text")
    return _parse_hooks(text)


def _parse_hooks(text: str) -> list[dict]:
    # tolerate ```json fences / stray prose around the JSON
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    hooks = data.get("hooks", data) if isinstance(data, dict) else data
    out = []
    for h in hooks or []:
        if isinstance(h, dict) and (h.get("hook_text") or h.get("headline")):
            if h.get("cta"):
                h["cta"] = knowledge.cta_label(h["cta"])
            out.append(h)
    return out
