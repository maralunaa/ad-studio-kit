"""
What two video formats need, found by research when the product doesn't have it yet (feedback 2026-10-26: "use ai
research" for How to eat; Comment reply "can also be done with research and pull comments").

  How to eat     how to tell it's ripe, cut it and eat it: researched steps saved as how_to_eat facts (auto-approved
                 unless a step states a number, like any researched fact).
  Comment reply  a real comment from the Page first. Otherwise a real question people ask about this fruit: from the
                 product research's customer language when it has one, else researched now. Saved as reviews of kind
                 'question' with their URL and shown as "People often ask", never as a named customer's comment.
                 Comments are never invented (house rule: real reviews only).
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Any

from creative_intelligence import prompts

SEARCHES = 3
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["items"],
          "properties": {"items": {"type": "array", "items": {
              "type": "object", "additionalProperties": False, "required": ["text", "source_url"],
              "properties": {"text": {"type": "string"}, "source_url": {"type": "string"}}}}}}
TASKS = {
    "how_to_eat": "Find how to eat this fruit, as 3 to 6 short steps a person can follow: how to tell it is ripe, how "
                  "to cut or open it, what to eat and what to leave, and one or two simple ways to serve it.",
    "questions": "Find 3 to 6 real questions people ask about this fruit (taste, ripeness, how to eat it, what it is). "
                 "Quote each question as asked.",
}
QUOTE = re.compile(r'^"(.+)" \(question\) — (https?://\S+)$', re.M)


def estimate(conn) -> float:
    from creative_intelligence.adstudio import hub
    from creative_intelligence.adstudio.costs import price
    pin, pout = price(conn, hub.MODEL), price(conn, hub.MODEL + ":output")
    return round(SEARCHES * price(conn, "web_search") + (60_000 * pin + 4_000 * pout) / 1e6, 2)


def research(conn, product: dict[str, Any], what: str, client: Any = None) -> list[dict[str, str]]:
    """[{text, source_url}] for TASKS[what], each with a real URL. Logged as research costs; the text cap applies."""
    from creative_intelligence import budget
    from creative_intelligence.adstudio import costs, hub
    from creative_intelligence.adstudio.chat import _client
    budget.check(conn, "text", estimate(conn))
    client = client or _client(log=False)
    notes, usages = hub._ask_research(client, product, (product.get("shopify_url") or "").strip(),
                                      system="style-research-system", max_uses=SEARCHES, task=TASKS[what])
    searches = 0
    for u in usages:
        costs.log_tokens(conn, hub.MODEL, "copy", u, "research", product["id"])
        searches += getattr(getattr(u, "server_tool_use", None), "web_search_requests", 0) or 0
    if searches:
        hub._log_searches(conn, searches, product["id"])
    resp = client.beta.messages.create(
        model=hub.MODEL, max_tokens=2000, system=prompts.text("style-research-system"),
        output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": f"Turn these notes into the items, keeping each URL.\n\nNOTES:\n{notes}"}])
    costs.log_tokens(conn, hub.MODEL, "copy", resp.usage, "research", product["id"])
    items = json.loads(next(b.text for b in resp.content if b.type == "text"))["items"]
    return [i for i in items if re.match(r"https?://", i.get("source_url") or "") and (i.get("text") or "").strip()]


def ensure_how_to_eat(conn, product: dict[str, Any], client: Any = None) -> str:
    """'' when the product has (or now has) approved how-to-eat steps; else why not."""
    from creative_intelligence.adstudio import facts, hub
    if any(f["category"] == "how_to_eat" for f in facts.list_facts(conn, product["id"], facts._family(product))):
        return ""
    steps = research(conn, product, "how_to_eat", client)
    if not steps:
        return "Research found no how-to-eat steps for this fruit: add them in the Product Hub."
    from creative_intelligence.adstudio.facts import other_fruit_hits
    with conn:
        for s in steps:
            fact = {"text": s["text"].strip(), "category": "how_to_eat"}
            why = hub.needs_review(fact) or ("names another fruit" if other_fruit_hits(conn, product, fact["text"]) else "")
            conn.execute("INSERT INTO facts (product_id, category, text, source, source_url, confidence, status, note, origin)"
                         " VALUES (?, 'how_to_eat', ?, 'research', ?, 'medium', ?, ?, 'research')",
                         (product["id"], fact["text"], s["source_url"], "pending" if why else "approved", why))
    if any(f["category"] == "how_to_eat" for f in facts.list_facts(conn, product["id"], facts._family(product))):
        return ""
    return "The researched how-to-eat steps state numbers: approve them in the Product Hub first."


def ensure_questions(conn, product: dict[str, Any], client: Any = None) -> str:
    """'' when the product has a real comment or a real question to answer; else why not."""
    from creative_intelligence.adstudio import reviews
    if reviews.listing(conn, product["id"], "comment") or reviews.listing(conn, product["id"], "question"):
        return ""
    found = [{"text": m[1], "source_url": m[2]} for m in QUOTE.finditer(product.get("customer_language") or "")]
    found = found or research(conn, product, "questions", client)
    if not found:
        return "Research found no real questions about this fruit; import the Page comments instead."
    today, now = date.today().isoformat(), datetime.utcnow().isoformat(timespec="seconds")
    with conn:
        for q in found[:6]:
            conn.execute("INSERT INTO reviews (product_id, kind, text, author, source, source_url, posted_on, added_at,"
                         " curated) VALUES (?, 'question', ?, '', 'web', ?, ?, ?, 1)",
                         (product["id"], q["text"].strip().strip('"“”'), q["source_url"], today, now))
    return ""


PREPARE = {"how_to_eat": ensure_how_to_eat, "comment_reply": ensure_questions}


def prepare(conn, product: dict[str, Any], style: str, client: Any = None) -> str:
    """Run the research a format needs before its script; '' when it can be written."""
    fn = PREPARE.get(style)
    if not fn:
        return ""
    try:
        return fn(conn, product, client)
    except Exception as exc:                                    # noqa: BLE001 — the reason goes on the video
        return f"Research for this format failed: {str(exc)[:150]}"
