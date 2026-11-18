"""
Facts and claims library (T6) — copy may only state approved facts.

  facts         approved factual statements, per product or for every product, by category
                (growing region, ripeness, shipping, guarantee, nutrition with source, other)
  banned_terms  words or claims that must never appear (per product or everywhere)

Used two ways:
  1. prompt_block()  every hook / headline / brief prompt gets the approved facts and the
                     banned list, and is told to make no other factual claims.
  2. check_copy()    every piece of copy (Compose text, hooks, briefs, Meta primary text) is
                     checked: banned terms by code; factual claims by Claude, each matched to
                     an approved fact or flagged as unlisted. Results are cached per text.

The operator's own product setup (flavour notes, look, notes for Claude) counts as an approved
source, since the operator wrote it; the researched taste counts once it is approved.

Facts have a status (Product Hub): research adds them as pending, with a source URL and a
confidence; only approved ones reach prompts. A product that is not in the store may never state
a price, stock or "order now".
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any

CATEGORIES = {"growing_region": "Growing region", "ripeness": "Ripeness", "shipping": "Shipping & delivery",
              "guarantee": "Guarantee", "nutrition": "Nutrition (with source)", "benefit": "Benefit",
              "characteristic": "Characteristic", "health": "Health claim (reliable source)",
              "how_to_eat": "How to eat it (steps)", "other": "Other"}
STATUSES = ("pending", "approved", "rejected", "do_not_use")
NOT_IN_STORE_TERMS = ("order now", "shop now", "buy now", "add to cart", "in stock", "available now", "ships today")
from creative_intelligence import config as _config
from creative_intelligence import knowledge, prompts

MODEL = _config.CI_CLAUDE_SCORE_MODEL


# ── library ───────────────────────────────────────────────────────────────
def list_facts(conn, product_id: int | None, family: str = "") -> list[dict[str, Any]]:
    """Approved facts for a product: its own, its fruit family's (catalog.family) and the global ones."""
    return [dict(r) for r in conn.execute(
        "SELECT * FROM facts WHERE active=1 AND status='approved' AND ((product_id IS NULL AND family='')"
        " OR product_id=? OR (product_id IS NULL AND family<>'' AND family=?)) ORDER BY category, id",
        (product_id, family or None))]


def _family(product: dict[str, Any]) -> str:
    from creative_intelligence.adstudio.catalog import family
    return family(product)


def list_banned(conn, product_id: int | None) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM banned_terms WHERE product_id IS NULL OR product_id=? ORDER BY term", (product_id,))]


def _setup_lines(product: dict[str, Any]) -> list[str]:
    out = []
    for k, label in (("scent_notes", "Flavour / key notes"), ("physical_desc", "Looks like"),
                     ("varieties", "Varieties"), ("notes", "Operator notes")):
        if (product.get(k) or "").strip():
            out.append(f"{label}: {product[k].strip()}")
    if product.get("taste_status") == "approved" and (product.get("taste") or "").strip():
        out.append(f"Taste & characteristics: {product['taste'].strip()}")
    return out


def not_in_store(product: dict[str, Any]) -> bool:
    """Added by hand and not sold in the Shopify store (or no longer sold there)."""
    return bool(product.get("archived")) or (product.get("origin") == "local"
                                             and not (product.get("shopify_url") or "").strip())


# Fruits a product's copy may only name when they are its own (feedback 2026-10-23: "Tastes like peach custard" on a
# mango ad). Orange, lime, lemon and date are left out: they are colours or everyday words far more often than fruit.
OTHER_FRUITS = ("apple", "apricot", "avocado", "banana", "blackberry", "blueberry", "cantaloupe", "cherimoya", "cherry",
                "coconut", "cranberry", "dragon fruit", "durian", "feijoa", "fig", "grape", "grapefruit", "guava",
                "honeydew", "jackfruit", "kiwi", "kumquat", "longan", "loquat", "lychee", "mamey", "mango", "mangosteen",
                "melon", "nectarine", "papaya", "passion fruit", "peach", "pear", "persimmon", "pineapple", "pitaya",
                "plum", "pomegranate", "pomelo", "rambutan", "raspberry", "sapote", "soursop", "starfruit", "strawberry",
                "tamarind", "watermelon", "yuzu")


def _fruit_re(f: str) -> str:
    stem = f[:-1] + "(?:y|ies)" if f.endswith("y") else f + "(?:e?s)?"
    return r"(?<!\w)" + stem.replace(" ", r"[\s-]?") + r"(?![\w])"


def own_fruits(conn, product: dict[str, Any]) -> set[str]:
    """The fruits a product may name: in its name, family or varieties; a box also the fruits its approved facts
    list as contents (not a taste comparison: "tastes like banana")."""
    # a nickname in brackets is not the fruit (2026-11-09: "Blood Orange (Raspberry)" let "Tastes like raspberry" through)
    name = re.sub(r"\([^)]*\)", " ", str(product.get("name") or ""))
    words = name + " " + str(product.get("varieties") or "") + " " + _family(product)
    if str(product.get("product_type") or "").lower() != "fruit":    # a candle's scent notes are its own words
        words += " " + str(product.get("scent_notes") or "")          # (2026-11-13: "Pink Pineapple and Passion fruit")
    if re.search(r"\b(?:box|variety|sampler|assortment|mix)\b", product.get("name") or "", re.I) and product.get("id"):
        words += " " + " ".join(f["text"] for f in list_facts(conn, product["id"], _family(product))
                                if not re.search(r"\blike\b", f["text"], re.I))
    return {f for f in OTHER_FRUITS if re.search(_fruit_re(f), words, re.I)}


# A fruit named for its colour is fine (2026-11-09: a blood orange is raspberry-red, it doesn't taste of raspberry).
COLOUR_AFTER = r"[\s-]?(?:red|pink|purple|colou?r(?:ed)?|hued?|tint(?:ed)?|blush)\b"


def other_fruit_hits(conn, product: dict[str, Any], text: str) -> list[str]:
    own = own_fruits(conn, product)
    text = text or ""
    return [f for f in OTHER_FRUITS if f not in own
            and any(not re.match(COLOUR_AFTER, text[m.end():], re.I) for m in re.finditer(_fruit_re(f), text, re.I))
            and not (f == "pear" and re.search(r"\bpear[\s-]?shaped\b", text, re.I))
            and not (f in ("apple", "melon") and any(o.endswith(f) or f in o for o in own))]


# Season dates go stale (feedback 2026-10-30: "Season ends in May" twice, in October): copy never says when a season
# ends or starts, or counts down to it. "In season now" and "limited season" stay allowed.
MONTHS = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*"
SEASON_DATE = re.compile(rf"\b(?:season\s+(?:ends?|ending|starts?|starting|opens?|closes?|runs?|is over)\b[^.!?]{{0,25}}?"
                         rf"\b(?:{MONTHS}|spring|summer|fall|autumn|winter)\b|(?:until|through|till|by|ends? in|only in)\s+"
                         rf"(?:{MONTHS}|spring|summer|fall|autumn|winter)\b(?!\s*\d)|\d+\s+(?:days?|weeks?)\s+(?:left|to go)|last (?:chance|call) (?:this|for the) season)",
                         re.I)


def season_date_hits(text: str) -> list[str]:
    return [m.group(0) for m in SEASON_DATE.finditer(text or "")]


def with_season_check(result: dict[str, Any] | None, texts: list[str]) -> dict[str, Any] | None:
    """A stored check, flagged too when the copy dates the season (hooks checked before the rule existed)."""
    hits = [h for t in texts for h in season_date_hits(t)]
    if not hits or (result or {}).get("status") == "flagged":
        return result
    out = dict(result or {"banned": [], "unlisted": [], "claims": []})
    out["banned"] = list(out.get("banned") or []) + [{"field": "copy", "term": h, "rule": "season_date"} for h in hits]
    out["status"] = "flagged"
    return out


STORE_FREE_CTA = "Learn more"


def store_safe_cta(product: dict[str, Any], cta: str) -> str:
    """A product not sold in the store can't say "Shop now" (not_in_store_hits): its button says Learn more instead of
    the whole ad being skipped (2026-11-13: every ad for a hand-added candle failed on its CTA)."""
    return STORE_FREE_CTA if not_in_store(product) and not_in_store_hits(cta) else cta


def not_in_store_hits(text: str) -> list[str]:
    hits = [t for t in NOT_IN_STORE_TERMS if re.search(r"(?<!\w)" + re.escape(t) + r"(?!\w)", text or "", re.I)]
    if re.search(r"[$€£]\s?\d", text or ""):
        hits.append("a price")
    return hits


def prompt_block(conn, product: dict[str, Any]) -> str:
    pid = product.get("id")
    facts = list_facts(conn, pid, _family(product)) if pid else []
    banned = list_banned(conn, pid) if pid else []
    lines = [f"- [{CATEGORIES.get(f['category'], f['category'])}] {f['text']}"
             + (f" (source: {f['source']})" if f.get("source") else "") for f in facts]
    block = ("APPROVED FACTS — the ONLY factual claims you may make (regions, ripeness, shipping times, "
             "guarantees, nutrition, numbers, awards, comparisons). Feeling and desire are free, and so is taste "
             "in plain words (sweet, tart, juicy); saying it tastes like another food is a claim. Facts are not free.\n" + ("\n".join(lines) if lines else "- (none in the library yet)"))
    setup = _setup_lines(product)
    if setup:
        block += "\nAlso approved (the operator's product setup):\n" + "\n".join(f"- {x}" for x in setup)
    if banned:
        block += "\nNEVER use these words or claims: " + "; ".join(b["term"] for b in banned)
    if not_in_store(product):
        block += ("\nThis product is NOT sold in the store yet: never state a price, stock or availability, "
                  "and never say 'order now', 'shop now' or similar.")
    return block + "\nIf a line needs a fact that is not listed, rewrite it without the fact."


# ── checking ──────────────────────────────────────────────────────────────
def banned_hits(text: str, banned: list[dict[str, Any]]) -> list[str]:
    hits = []
    for b in banned:
        term = b["term"].strip()
        if term and re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", text or "", re.I):
            hits.append(term)
    return hits


CLAIM_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["claims"],
    "properties": {"claims": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["field", "claim", "supported_by"],
        "properties": {
            "field": {"type": "string"},
            "claim": {"type": "string", "description": "the factual claim, quoted from the copy"},
            "supported_by": {"type": "string", "description": "the approved fact's number, 'setup', or 'none'"},
        }}}},
}


def _key(product_id: Any, facts: list, setup: list, items: dict[str, str]) -> str:
    raw = json.dumps([product_id, [(f["id"], f["text"]) for f in facts], setup, sorted(items.items()),
                      prompts.version("claims-check-system")])           # a new prompt checks again
    return hashlib.sha1(raw.encode()).hexdigest()


MEMORIES = ("First-person memories and feelings told by the presenter (\"when I was a little girl…\", \"it reminds me "
            "of…\") are allowed by the owner for this persona: never flag the memory itself. A fact about the fruit "
            "inside a memory (where it grows, a health benefit, a taste fact) still needs an approved fact.")


def check_copy(conn, product: dict[str, Any], items: dict[str, str], client: Any = None,
               source: str = "", source_id: int = 0, allow: str = "") -> dict[str, Any]:
    """Check copy fields ({field: text}) against the library. Never raises. allow: what the owner lets through for
    this copy (MEMORIES for a persona that tells memories)."""
    items = {k: (v or "").strip() for k, v in items.items() if (v or "").strip()}
    pid = product.get("id")
    banned = list_banned(conn, pid) if pid else []
    result: dict[str, Any] = {"banned": [], "unlisted": [], "claims": [], "status": "ok"}
    for field, text in items.items():
        for term in banned_hits(text, banned) + (not_in_store_hits(text) if not_in_store(product) else []):
            result["banned"].append({"field": field, "term": term})
        for term in season_date_hits(text):                       # dated seasons go stale
            result["banned"].append({"field": field, "term": term, "rule": "season_date"})
        for term in knowledge.personal_attribute(text):          # playbook: no personal-attribute copy
            result["banned"].append({"field": field, "term": term, "rule": "personal_attribute"})
        if pid:
            for term in other_fruit_hits(conn, product, text):    # house: only the product's own fruit
                result["banned"].append({"field": field, "term": term, "rule": "other_fruit"})
    if not items:
        return result
    facts = list_facts(conn, pid, _family(product)) if pid else []
    setup = _setup_lines(product)
    key = _key(pid, facts, setup, {**items, **({"_allow": allow} if allow else {})})
    cached = conn.execute("SELECT result_json FROM copy_check_cache WHERE key=?", (key,)).fetchone()
    if cached:
        result["claims"] = json.loads(cached[0])
    else:
        try:
            from creative_intelligence.adstudio.chat import _client
            from creative_intelligence.adstudio.runs import log_claude
            client = client or _client(log=False)
            listing = "\n".join(f"{i}. {f['text']}" for i, f in enumerate(facts, 1)) or "(none)"
            copy = "\n".join(f"[{k}] {v}" for k, v in items.items())
            resp = client.beta.messages.create(
                model=MODEL, max_tokens=4000, betas=["server-side-fallback-2026-07-01"], fallbacks="default",
                system=(prompts.text("claims-check-system")),
                output_config={"format": {"type": "json_schema", "schema": CLAIM_SCHEMA}},
                messages=[{"role": "user", "content":
                           f"APPROVED FACTS:\n{listing}\n\nPRODUCT SETUP:\n" + ("\n".join(setup) or "(none)")
                           + (f"\n\nALLOWED BY THE OWNER: {allow}" if allow else "")
                           + f"\n\nCOPY TO CHECK:\n{copy}"}])
            if resp.stop_reason == "refusal":
                raise RuntimeError("Claude declined to check this copy")
            log_claude(conn, resp.usage, "claims-check", source or "copy", source_id, None)
            result["claims"] = json.loads(next(b.text for b in resp.content if b.type == "text"))["claims"]
            with conn:
                conn.execute("INSERT OR REPLACE INTO copy_check_cache (key, result_json, at) VALUES (?,?,?)",
                             (key, json.dumps(result["claims"]), datetime.utcnow().isoformat()))
        except Exception as exc:
            result["status"], result["error"] = "error", str(exc)[:200]
    result["unlisted"] = [c for c in result["claims"] if str(c.get("supported_by", "none")).lower() == "none"]
    if result["banned"] or result["unlisted"]:
        result["status"] = "flagged"
    return result


def store(conn, source: str, source_id: int, result: dict[str, Any]) -> None:
    with conn:
        conn.execute("INSERT INTO copy_checks (source, source_id, status, result_json, at) VALUES (?,?,?,?,?)"
                     " ON CONFLICT(source, source_id) DO UPDATE SET status=excluded.status,"
                     " result_json=excluded.result_json, at=excluded.at",
                     (source, source_id, result["status"], json.dumps(result), datetime.utcnow().isoformat()))


def checks_for(conn, source: str, ids: list[int]) -> dict[int, dict[str, Any]]:
    out = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for r in conn.execute(f"SELECT source_id, result_json FROM copy_checks WHERE source=? AND source_id IN"
                              f" ({','.join('?' * len(chunk))})", [source, *chunk]):
            out[r["source_id"]] = json.loads(r["result_json"])
    return out


def summary_line(result: dict[str, Any] | None) -> str:
    if not result:
        return ""
    bits = [f"banned “{b['term']}” in {b['field']}" for b in result.get("banned", [])]
    bits += [f"unapproved claim “{c['claim']}” in {c['field']}" for c in result.get("unlisted", [])]
    return "; ".join(bits)
