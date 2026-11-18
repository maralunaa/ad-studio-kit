"""
Product Hub: the profile behind each product, how ready it is, and the two Claude helpers that fill it.

  readiness()     ready for images = 2+ tagged reference photos (one of them cut, for fruit), the
                  look written, 1+ saved hook. Ready for video = that, plus 5+ approved facts and the
                  taste approved. Each missing item comes back as a line the page shows.
  draft_look()    Claude looks at the reference photos: writes the look and tags each photo
                  (whole / cut / in hand / in box / lifestyle) and suggests 1-3 heroes.
  research        Claude with web search: a taste draft and facts, each with a source URL and a
                  confidence, saved as pending (or "do not use"). Only approved facts reach prompts
                  (facts.py). Costed on the Claude text cap; the estimate is shown before it starts.
  customer language  Claude with web search: real customers' words, quoted exactly, with their URLs.
  fill()          "Fill profile" (on demand, from the product list or the profile): fills whatever is
                  empty, in order: Shopify photos, look and photo tags, research (taste, facts and
                  customer language in one web-search pass), 5 Hook University hooks. Facts true of a
                  whole fruit family are researched once and shared (catalog.family).
"""
from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from creative_intelligence import brand as _brand, config, prompts

log = logging.getLogger("adstudio.hub")

TAGS = {"whole": "Whole", "cut": "Cut", "in_hand": "In hand", "in_box": "In box", "lifestyle": "Lifestyle"}
MAX_HEROES = 3
FACTS_FOR_VIDEO = 5
MODEL = config.CI_CLAUDE_CHECK_MODEL           # vision + web search (dynamic filtering)
MAX_SEARCHES = 5
LOOK_PHOTOS = 8


# ── readiness ───────────────────────────────────────────────────────────────
def stats(conn, pid: int | None = None) -> dict[int, dict[str, int]]:
    """Counts behind readiness, per product (all products when pid is None). Facts count the product's
    own and its fruit family's."""
    from creative_intelligence.adstudio.catalog import family
    where, args = ("WHERE p.id=?", (pid,)) if pid else ("", ())
    fam = {(r[0], r[1]): r[2] for r in conn.execute(
        "SELECT family, status, COUNT(*) FROM facts WHERE product_id IS NULL AND family<>'' AND active=1"
        " GROUP BY family, status")}
    fams = {r[0]: family(dict(r)) for r in conn.execute(f"SELECT p.id, p.shop_json FROM adstudio_products p {where}", args)}
    out = {r["id"]: dict(r) for r in conn.execute(
        "SELECT p.id,"
        " (SELECT COUNT(*) FROM adstudio_photos x WHERE x.product_id=p.id AND x.tag<>'') AS tagged,"
        " (SELECT COUNT(*) FROM adstudio_photos x WHERE x.product_id=p.id AND x.tag='cut') AS cut,"
        " (SELECT COUNT(*) FROM adstudio_photos x WHERE x.product_id=p.id) AS photos,"
        " (SELECT COUNT(*) FROM adstudio_hooks h WHERE h.product_id=p.id) AS hooks,"
        " (SELECT COUNT(*) FROM facts f WHERE f.product_id=p.id AND f.active=1 AND f.status='approved') AS facts,"
        " (SELECT COUNT(*) FROM facts f WHERE f.product_id=p.id AND f.active=1 AND f.status='pending') AS pending,"
        " (SELECT COUNT(*) FROM product_research r WHERE r.product_id=p.id AND r.status='done') AS researched"
        f" FROM adstudio_products p {where}", args)}
    for i, d in out.items():
        f = fams.get(i, "")
        d["facts"] += fam.get((f, "approved"), 0) if f else 0
        d["pending"] += fam.get((f, "pending"), 0) if f else 0
    return out


def readiness(product: dict[str, Any], st: dict[str, int]) -> dict[str, Any]:
    fruit = product.get("product_type") == "fruit"
    refs_ok = st.get("tagged", 0) >= 2 and (st.get("cut", 0) >= 1 or not fruit)
    img = [
        {"key": "refs", "ok": refs_ok, "fix": "references",
         "label": "2+ tagged reference photos" + (", 1 of them cut" if fruit else ""),
         "need": ("tag 2+ reference photos" if st.get("tagged", 0) < 2 else "a cut reference photo")},
        {"key": "look", "ok": bool((product.get("physical_desc") or "").strip()), "fix": "look",
         "label": "The look written", "need": "the look"},
        {"key": "hook", "ok": st.get("hooks", 0) >= 1, "fix": "hooks", "label": "1+ saved hook", "need": "a saved hook"},
    ]
    vid = [
        {"key": "facts", "ok": st.get("facts", 0) >= FACTS_FOR_VIDEO, "fix": "facts",
         "label": f"{FACTS_FOR_VIDEO}+ approved facts ({st.get('facts', 0)} of {FACTS_FOR_VIDEO})",
         "need": f"{max(0, FACTS_FOR_VIDEO - st.get('facts', 0))} more approved fact(s)"},
        {"key": "taste", "ok": product.get("taste_status") == "approved", "fix": "look",
         "label": "Taste & characteristics approved", "need": "the taste approved"},
    ]
    images_ready = all(c["ok"] for c in img)
    return {"images": {"ready": images_ready, "checks": img, "missing": [c["need"] for c in img if not c["ok"]]},
            "video": {"ready": images_ready and all(c["ok"] for c in vid), "checks": vid,
                      "missing": [c["need"] for c in img + vid if not c["ok"]]},
            "steps_done": sum(c["ok"] for c in img + vid), "steps": len(img) + len(vid)}


# ── photos: tags and heroes ─────────────────────────────────────────────────
def set_tag(conn, photo_id: int, tag: str) -> None:
    if tag and tag not in TAGS:
        raise ValueError("bad tag")
    with conn:
        conn.execute("UPDATE adstudio_photos SET tag=? WHERE id=?", (tag, photo_id))


def set_hero(conn, photo_id: int, on: bool) -> str:
    """Mark or unmark a hero (at most 3). The first hero is also the primary photo. Returns '' or why not."""
    row = conn.execute("SELECT product_id FROM adstudio_photos WHERE id=?", (photo_id,)).fetchone()
    if row is None:
        return "photo not found"
    pid = row["product_id"]
    heroes = [r[0] for r in conn.execute("SELECT id FROM adstudio_photos WHERE product_id=? AND is_hero=1"
                                         " ORDER BY id", (pid,))]
    if on and photo_id not in heroes and len(heroes) >= MAX_HEROES:
        return f"Up to {MAX_HEROES} heroes; unmark one first."
    with conn:
        conn.execute("UPDATE adstudio_photos SET is_hero=? WHERE id=?", (int(on), photo_id))
        _sync_primary(conn, pid)
    return ""


def _sync_primary(conn, pid: int) -> None:
    first = conn.execute("SELECT id FROM adstudio_photos WHERE product_id=? AND is_hero=1 ORDER BY id LIMIT 1",
                         (pid,)).fetchone()
    if first:
        conn.execute("UPDATE adstudio_photos SET is_primary=(id=?) WHERE product_id=?", (first[0], pid))


# ── draft the look and tag the photos (Claude vision) ───────────────────────
LOOK_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["look", "tags", "heroes"],
    "properties": {
        "look": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string", "enum": [*TAGS, "none"]},
                 "description": "one tag per photo, in photo order"},
        "heroes": {"type": "array", "items": {"type": "integer"}, "description": "1-based photo numbers, 1 to 3"},
    }}


def draft_look(conn, product: dict[str, Any], client: Any = None) -> dict[str, Any]:
    """Write the look (as a draft in the Look field) and tag untagged photos. Hand-set tags and heroes stay."""
    from creative_intelligence.adstudio import costs
    from creative_intelligence.adstudio.chat import _client, _image_blocks
    from creative_intelligence.adstudio.store import to_data_uri
    photos = [dict(r) for r in conn.execute("SELECT id, path, tag, is_hero FROM adstudio_photos WHERE product_id=?"
                                            " ORDER BY is_hero DESC, is_primary DESC, id LIMIT ?",
                                            (product["id"], LOOK_PHOTOS))]
    uris, used = [], []
    for p in photos:
        try:
            uris.append(to_data_uri(p["path"]))
            used.append(p)
        except Exception:
            log.warning("photo %s unreadable", p["id"])
    if not used:
        raise ValueError("Add a reference photo first.")
    content: list[dict[str, Any]] = []
    for i, u in enumerate(uris, 1):
        content += [{"type": "text", "text": f"Photo {i}:"}, *_image_blocks([u])]
    content.append({"type": "text", "text": f"Product: {product.get('name')}\nType: {product.get('product_type')}\n"
                    f"Product copy: {(product.get('description') or '')[:1500]}\n\nWrite the look and tag the "
                    f"{len(used)} photo(s)."})
    client = client or _client(log=False)
    resp = client.beta.messages.create(
        model=MODEL, max_tokens=2000, betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        system=prompts.text("hub-look-system"),
        output_config={"format": {"type": "json_schema", "schema": LOOK_SCHEMA}},
        messages=[{"role": "user", "content": content}])
    if resp.stop_reason == "refusal":
        raise RuntimeError("Claude declined to describe these photos")
    costs.log_tokens(conn, MODEL, "copy", resp.usage, "product-look", product["id"])
    d = json.loads(next(b.text for b in resp.content if b.type == "text"))
    tags = (d.get("tags") or []) + [""] * len(used)
    tagged = 0
    with conn:
        for p, t in zip(used, tags):
            if not p["tag"] and t in TAGS:
                conn.execute("UPDATE adstudio_photos SET tag=? WHERE id=?", (t, p["id"]))
                tagged += 1
        if not any(p["is_hero"] for p in photos):
            for n in [n for n in d.get("heroes") or [] if 1 <= n <= len(used)][:MAX_HEROES]:
                conn.execute("UPDATE adstudio_photos SET is_hero=1 WHERE id=?", (used[n - 1]["id"],))
            _sync_primary(conn, product["id"])
        conn.execute("UPDATE adstudio_products SET physical_desc=? WHERE id=?", (d["look"].strip(), product["id"]))
    return {"look": d["look"].strip(), "tagged": tagged}


# ── research (Claude + web search): taste, facts and customer language in one pass ─
LANG_ITEM = {"type": "object", "additionalProperties": False, "required": ["quote", "kind", "source_url"],
             "properties": {"quote": {"type": "string"},
                            "kind": {"type": "string", "enum": ["praise", "question", "objection", "description"]},
                            "source_url": {"type": "string"}}}
FACT_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["taste", "facts", "quotes"],
    "properties": {
        "taste": {"type": "string", "description": "3-5 sentences: flavour, aroma, texture, sweetness/acidity, "
                                                   "size, how it is eaten, season. Empty if nothing was found."},
        "facts": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["text", "category", "scope", "source_url", "confidence", "use", "reason"],
            "properties": {
                "text": {"type": "string"},
                "scope": {"type": "string", "enum": ["family", "variety"],
                          "description": "family = true of the whole fruit family; variety = this product only"},
                "category": {"type": "string", "enum": ["characteristic", "benefit", "nutrition", "health",
                                                        "growing_region", "ripeness", "shipping", "guarantee", "other"]},
                "source_url": {"type": "string"},
                "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                "use": {"type": "boolean", "description": "false = DO NOT USE"},
                "reason": {"type": "string", "description": "why not to use it, or a caution about using it; "
                                                            "never part of the fact's text"},
            }}},
        "quotes": {"type": "array", "items": LANG_ITEM},
    }}


def estimate(conn) -> float:
    """What one research run costs, roughly: the searches, the research call (search results make it long:
    the first real run read about 160k tokens with 8 searches; 5 searches, about 100k) and the formatting call."""
    from creative_intelligence.adstudio.costs import price
    pin, pout = price(conn, MODEL), price(conn, MODEL + ":output")
    return round(MAX_SEARCHES * price(conn, "web_search") + (100_000 * pin + 10_000 * pout) / 1e6
                 + (14_000 * pin + 6_000 * pout) / 1e6, 2)


def start_research(conn, pid: int) -> tuple[int | None, str]:
    """Queue research if the text cap leaves room. Returns (research id, '') or (None, why not)."""
    from creative_intelligence import budget, jobs
    est = estimate(conn)
    try:
        budget.check(conn, "text", est)
    except budget.BudgetExceeded as exc:
        return None, str(exc)
    busy = conn.execute("SELECT id FROM product_research WHERE product_id=? AND status IN ('queued','running')",
                        (pid,)).fetchone()
    if busy:
        return busy[0], ""
    with conn:
        rid = conn.execute("INSERT INTO product_research (product_id, estimate_usd, created_at) VALUES (?,?,?)",
                           (pid, est, datetime.utcnow().isoformat(timespec="seconds"))).lastrowid
    jobs.enqueue(conn, "research", {"research_id": rid}, dedupe_key=f"research:{rid}")
    return rid, ""


def _research_row(conn, pid: int) -> int:
    with conn:
        return conn.execute("INSERT INTO product_research (product_id, estimate_usd, created_at) VALUES (?,?,?)",
                            (pid, estimate(conn), datetime.utcnow().isoformat(timespec="seconds"))).lastrowid


def latest_research(conn, pid: int) -> dict[str, Any] | None:
    r = conn.execute("SELECT * FROM product_research WHERE product_id=? ORDER BY id DESC LIMIT 1", (pid,)).fetchone()
    if r is None:
        return None
    d = dict(r)
    d["result"] = json.loads(d.pop("result_json") or "{}")
    return d


def _ask_research(client: Any, product: dict[str, Any], page_url: str, system: str = "hub-research-system",
                  max_uses: int = MAX_SEARCHES, task: str = "Research this product and write the notes.",
                  known: list[str] | None = None, family: str = "") -> tuple[str, list[Any]]:
    """The web-search call; follows pause_turn. Returns (notes text, usages)."""
    user = (f"Product: {product.get('name')}\nType: {product.get('product_type')}\n"
            + (f"Fruit family: {family}\n" if family else "")
            + f"{_brand.name()} product page: {page_url or '(none: this product is not in the store yet)'}\n"
            f"What that page says: {(product.get('description') or '(nothing)')[:3000]}\n\n"
            + (f"FACTS ALREADY KNOWN FOR THE {family.upper()} FAMILY (do not research these again):\n"
               + "\n".join(f"- {k}" for k in known[:60]) + "\n\n" if known else "")
            + task)
    messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
    usages, resp = [], None
    for _ in range(4):
        resp = client.messages.create(
            model=MODEL, max_tokens=8000, system=prompts.text(system), messages=messages,
            tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": max_uses}])
        usages.append(resp.usage)
        if resp.stop_reason != "pause_turn":
            break
        messages = [messages[0], {"role": "assistant", "content": resp.content}]
    notes = "\n".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    if not notes:
        raise RuntimeError("Research came back empty")
    return notes, usages


def _format(client: Any, notes: str) -> tuple[dict[str, Any], Any]:
    resp = client.beta.messages.create(
        model=MODEL, max_tokens=6000, betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        system=prompts.text("hub-research-format"),
        output_config={"format": {"type": "json_schema", "schema": FACT_SCHEMA}},
        messages=[{"role": "user", "content": f"RESEARCH NOTES:\n{notes}"}])
    if resp.stop_reason == "refusal":
        raise RuntimeError("Claude declined to format the research")
    return json.loads(next(b.text for b in resp.content if b.type == "text")), resp.usage


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""


def _brand_claim_pattern() -> str:
    """Words that make a researched fact a claim about the brand itself: its name, its short name, "our", "we"."""
    words = [re.escape(w) for w in (_brand.name(), _brand.short()) if w]
    return r"|".join([w.replace(r"\ ", r"\s*") for w in words] + [r"\bour\b", r"\bwe\b"])


def judge(fact: dict[str, Any], store_host: str, product_name: str = "") -> tuple[str, str]:
    """Code's last word on a researched fact: (status, note)."""
    url, text = (fact.get("source_url") or "").strip(), fact.get("text") or ""
    if not re.match(r"https?://", url):
        return "", "no source URL"                                  # dropped
    if not fact.get("use", True):
        return "do_not_use", fact.get("reason") or "marked do not use by research"
    if re.search(_brand_claim_pattern(), text, re.I) and _host(url) != store_host:
        return "do_not_use", f"a {_brand.name()} claim not taken from our own product page"
    from creative_intelligence.adstudio.hook_university import origin_claim
    if _brand.get("rules.no_origin_claims") and (fact.get("category") == "growing_region"
                                                 or origin_claim(text, product_name)):
        return "do_not_use", "origin claim: where it comes from or is grown is not used in our ads"
    if fact.get("category") == "health" and fact.get("confidence") != "high":
        return "do_not_use", "health claim without a strong, reliable source"
    why = needs_review(fact)
    if why:
        return "pending", why
    return "approved", "auto-approved; switch to Do not use if it shouldn't be used"


# Feedback 2026-10-22: facts approve themselves so production doesn't wait. These still wait for a person:
# the claims that can get an ad rejected or promise something (health, guarantees, shipping, numbers).
REVIEW_CATEGORIES = {"health": "health claim", "guarantee": "a guarantee", "shipping": "a shipping promise"}


def needs_review(fact: dict[str, Any]) -> str:
    """Why a researched fact waits for a person, or '' when it can be approved on its own."""
    if fact.get("category") in REVIEW_CATEGORIES:
        return f"needs your OK: {REVIEW_CATEGORIES[fact['category']]}"
    if re.search(r"\d", fact.get("text") or ""):
        return "needs your OK: it states a number"
    return ""


def save_research(conn, product: dict[str, Any], data: dict[str, Any], store_host: str) -> dict[str, int]:
    """Taste approved as it lands (an approved taste is never overwritten); facts as pending / do not use (no repeats),
    family-wide ones shared with the whole fruit family; customer quotes into Customer language when empty."""
    from creative_intelligence.adstudio.catalog import family
    fam = family(product)
    out = {"approved": 0, "pending": 0, "do_not_use": 0, "dropped": 0, "repeats": 0, "family": 0}
    have = {_key(r[0]) for r in conn.execute("SELECT text FROM facts WHERE product_id=? OR (product_id IS NULL"
                                             " AND family<>'' AND family=?)", (product["id"], fam or None))}
    with conn:
        for f in data.get("facts") or []:
            status, note = judge(f, store_host, product.get("name") or "")
            if not status:
                out["dropped"] += 1
                continue
            if _key(f["text"]) in have:
                out["repeats"] += 1
                continue
            have.add(_key(f["text"]))
            shared = bool(fam) and f.get("scope") == "family"
            conn.execute("INSERT INTO facts (product_id, family, category, text, source, source_url, confidence, status,"
                         " origin, note) VALUES (?,?,?,?,?,?,?,?, 'research', ?)",
                         (None if shared else product["id"], fam if shared else "", f.get("category") or "other",
                          f["text"].strip(), _host(f["source_url"]), f["source_url"].strip(), f.get("confidence", ""),
                          status, note))
            out[status] += 1
            out["family"] += shared
        quotes = [q for q in data.get("quotes") or []
                  if re.match(r"https?://", q.get("source_url") or "") and (q.get("quote") or "").strip()]
        out["quotes"] = len(quotes)
        cur = conn.execute("SELECT customer_language FROM adstudio_products WHERE id=?", (product["id"],)).fetchone()
        if quotes and not ((cur[0] if cur else "") or "").strip():
            conn.execute("UPDATE adstudio_products SET customer_language=? WHERE id=?", (_quote_lines(quotes), product["id"]))
        taste = (data.get("taste") or "").strip()
        if taste and product.get("taste_status") != "approved":
            # approved as it lands (feedback 2026-10-30: "auto approve this when it runs"); edit it any time
            conn.execute("UPDATE adstudio_products SET taste=?, taste_status='approved' WHERE id=?", (taste, product["id"]))
            out["taste"] = 1
    return out


def _quote_lines(quotes: list[dict[str, Any]]) -> str:
    return "\n".join(f'"{q["quote"].strip()}" ({q["kind"]}) — {q["source_url"].strip()}' for q in quotes)


def family_known(conn, fam: str) -> list[str]:
    """Facts already researched for a fruit family (not rejected), so research skips them."""
    if not fam:
        return []
    return [r[0] for r in conn.execute("SELECT text FROM facts WHERE product_id IS NULL AND family=? AND active=1"
                                       " AND status IN ('pending','approved') ORDER BY id", (fam,))]


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def run_research(db: Callable[[], Any], research_id: int, client: Any = None) -> dict[str, Any]:
    """The research job (in the worker)."""
    from creative_intelligence.adstudio import catalog, costs
    from creative_intelligence.adstudio.chat import _client
    conn = db()
    try:
        r = conn.execute("SELECT * FROM product_research WHERE id=?", (research_id,)).fetchone()
        product = dict(conn.execute("SELECT * FROM adstudio_products WHERE id=?", (r["product_id"],)).fetchone())
        with conn:
            conn.execute("UPDATE product_research SET status='running' WHERE id=?", (research_id,))
        page = (product.get("shopify_url") or "").strip()
        client = client or _client(log=False)
        cost, searches = 0.0, 0
        try:
            fam = catalog.family(product)
            notes, usages = _ask_research(client, product, page, known=family_known(conn, fam), family=fam)
            for u in usages:
                cost += costs.log_tokens(conn, MODEL, "copy", u, "research", research_id)
                n = getattr(getattr(u, "server_tool_use", None), "web_search_requests", 0) or 0
                searches += n
            if searches:
                cost += _log_searches(conn, searches, research_id)
            data, usage = _format(client, notes)
            cost += costs.log_tokens(conn, MODEL, "copy", usage, "research", research_id)
            counts = save_research(conn, product, data, _host(page) or _host(catalog.store_url()))
        except Exception as exc:
            with conn:
                conn.execute("UPDATE product_research SET status='failed', error=?, cost_usd=?, searches=?,"
                             " finished_at=? WHERE id=?", (str(exc)[:500], round(cost, 4), searches,
                                                          datetime.utcnow().isoformat(timespec="seconds"), research_id))
            raise
        with conn:
            conn.execute("UPDATE product_research SET status='done', cost_usd=?, searches=?, result_json=?,"
                         " finished_at=? WHERE id=?", (round(cost, 4), searches, json.dumps({"counts": counts,
                                                       "notes": notes[:20000]}),
                                                       datetime.utcnow().isoformat(timespec="seconds"), research_id))
        return {"counts": counts, "cost_usd": round(cost, 4), "searches": searches}
    finally:
        conn.close()


def _log_searches(conn, n: int, source_id: int, source: str = "research") -> float:
    from creative_intelligence.adstudio.costs import _api_log, price
    cost = round(n * price(conn, "web_search"), 4)
    now = datetime.utcnow().isoformat()
    with conn:
        conn.execute("INSERT INTO model_calls (at, model, kind, units, cost_usd, source, source_id, run_id)"
                     " VALUES (?,?,?,?,?,?,?,NULL)", (now, "web_search", "copy", n, cost, source, source_id))
    _api_log(f"{now} | ANTHROPIC | web_search | {source}#{source_id} | {n} searches | ${cost:.4f}")
    return cost


# ── customer language (Claude + web search) ─────────────────────────────────
LANG_SEARCHES = 6
LANG_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["quotes"],
               "properties": {"quotes": {"type": "array", "items": LANG_ITEM}}}


def language_estimate(conn) -> float:
    from creative_intelligence.adstudio.costs import price
    pin, pout = price(conn, MODEL), price(conn, MODEL + ":output")
    return round(LANG_SEARCHES * price(conn, "web_search") + (110_000 * pin + 6_000 * pout) / 1e6
                 + (8_000 * pin + 3_000 * pout) / 1e6, 2)


def research_language(conn, product: dict[str, Any], client: Any = None) -> dict[str, Any]:
    """Real customer words with their URLs into the Customer language field (only when it is empty)."""
    from creative_intelligence.adstudio import costs
    from creative_intelligence.adstudio.chat import _client
    client = client or _client(log=False)
    notes, usages = _ask_research(client, product, (product.get("shopify_url") or "").strip(), "hub-language-system",
                                  LANG_SEARCHES, "Find what real customers say about this product and write the notes.")
    cost, searches = 0.0, 0
    for u in usages:
        cost += costs.log_tokens(conn, MODEL, "copy", u, "language", product["id"])
        searches += getattr(getattr(u, "server_tool_use", None), "web_search_requests", 0) or 0
    if searches:
        cost += _log_searches(conn, searches, product["id"], "language")
    resp = client.beta.messages.create(
        model=MODEL, max_tokens=4000, betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        system=prompts.text("hub-language-format"),
        output_config={"format": {"type": "json_schema", "schema": LANG_SCHEMA}},
        messages=[{"role": "user", "content": f"NOTES:\n{notes}"}])
    cost += costs.log_tokens(conn, MODEL, "copy", resp.usage, "language", product["id"])
    quotes = [q for q in json.loads(next(b.text for b in resp.content if b.type == "text"))["quotes"]
              if re.match(r"https?://", q.get("source_url") or "") and (q.get("quote") or "").strip()]
    text = _quote_lines(quotes)
    cur = conn.execute("SELECT customer_language FROM adstudio_products WHERE id=?", (product["id"],)).fetchone()[0]
    if text and not (cur or "").strip():
        with conn:
            conn.execute("UPDATE adstudio_products SET customer_language=? WHERE id=?", (text, product["id"]))
    return {"quotes": len(quotes), "cost_usd": round(cost, 4), "searches": searches}


# ── auto-fill ───────────────────────────────────────────────────────────────
AUTO_HOOKS = 5
LOOK_ESTIMATE = 0.04
HOOKS_ESTIMATE = 0.10


def fill_estimate(conn) -> float:
    """Research now brings the customer language too; the separate language pass only runs for
    products researched before that."""
    return round(LOOK_ESTIMATE + estimate(conn) + HOOKS_ESTIMATE, 2)


def _needs_language(conn, pid: int, product: dict[str, Any]) -> bool:
    if (product.get("customer_language") or "").strip():
        return False
    r = conn.execute("SELECT result_json FROM product_research WHERE product_id=? AND status='done'"
                                   " ORDER BY id DESC LIMIT 1", (pid,)).fetchone()
    return bool(r) and "quotes" not in json.loads(r[0] or "{}").get("counts", {})


def queue_fill(conn, pid: int) -> int | None:
    """Queue the auto-fill for a product unless one is already queued or running. Returns the job id."""
    from creative_intelligence import jobs
    row = conn.execute("SELECT fill_status, archived FROM adstudio_products WHERE id=?", (pid,)).fetchone()
    if row is None or row["archived"]:
        return None
    if row["fill_status"] in ("queued", "running") and conn.execute(
            "SELECT 1 FROM jobs WHERE kind='fill' AND status IN ('queued','running')"
            " AND json_extract(payload_json, '$.product_id')=?", (pid,)).fetchone():
        return None                              # one at a time; a status left by a lost job is retried
    with conn:
        conn.execute("UPDATE adstudio_products SET fill_status='queued', fill_step='', fill_error='' WHERE id=?", (pid,))
    return jobs.enqueue(conn, "fill", {"product_id": pid}, dedupe_key=f"fill:{pid}")


def import_shop_photos(conn, product: dict[str, Any], limit: int = 6) -> int:
    """Copy the product's Shopify photos in as reference photos (only when it has none)."""
    from urllib.request import Request, urlopen
    from creative_intelligence.adstudio import catalog
    from creative_intelligence.adstudio.costs import _api_log
    from creative_intelligence.adstudio.store import save_reference_photo
    pid = product["id"]
    if conn.execute("SELECT 1 FROM adstudio_photos WHERE product_id=? LIMIT 1", (pid,)).fetchone():
        return 0
    added = 0
    for i, src in enumerate(catalog.shop(product).get("images", [])[:limit]):
        try:
            sized = src + ("&" if "?" in src else "?") + "width=1200"
            raw = urlopen(Request(sized, headers={"User-Agent": _brand.user_agent()}), timeout=30).read()  # noqa: S310
            _api_log(f"{datetime.utcnow().isoformat()} | SHOPIFY | GET {sized} | image")
            path = save_reference_photo(raw, f"shopify{i}.jpg", pid)
            with conn:
                conn.execute("INSERT INTO adstudio_photos (product_id, path, is_primary) VALUES (?,?,?)",
                             (pid, path, int(added == 0)))
            added += 1
        except Exception:
            log.exception("shopify image download failed: %s", src)
    return added


def _usable_hooks(conn, pid: int) -> list[dict[str, Any]]:
    from creative_intelligence.adstudio import batch
    return batch._hooks(conn, pid)


def auto_hooks(conn, product: dict[str, Any], idea: str = "") -> int:
    """Hook University hooks, saved and claims-checked like hooks saved by hand. idea: a direction to write to (the
    visual library's "Hooks from their angle"); else the idea bank picks one."""
    from creative_intelligence.adstudio.chat import suggest_hooks
    from creative_intelligence.adstudio.winning import current_patterns
    from creative_intelligence.webapp.common import _claims, _primary_photo_uris
    from creative_intelligence.adstudio import ideas
    patterns = current_patterns(conn, limit=4)
    labels = {p["id"]: p["label"] for p in patterns}
    picked, idea_id = ideas.pick_one(conn, product["id"], "hooks", n=AUTO_HOOKS)
    idea = idea or picked
    hooks = suggest_hooks(product, AUTO_HOOKS, "", _primary_photo_uris(conn, product["id"]), patterns=patterns,
                          idea=idea)
    fields = ("hook_text", "archetype", "d", "c", "ps", "headline", "subhead", "body", "cta", "hook_type",
              "prompt_version", "critic_note")
    todo = []
    for h in hooks:
        with conn:
            hid = conn.execute(
                f"INSERT INTO adstudio_hooks (product_id, {', '.join(fields)}, source, pattern_id, idea_id) VALUES"
                f" (?{',?' * len(fields)}, 'hook_university', ?, ?)",
                (product["id"], *[str(h.get(k) or "") for k in fields], h.get("pattern_id"), idea_id)).lastrowid
        if h.get("pattern_id") in labels and not h.get("archetype"):
            with conn:
                conn.execute("UPDATE adstudio_hooks SET archetype=? WHERE id=?", (labels[h["pattern_id"]], hid))
        todo.append((hid, {k: h.get(k, "") for k in ("hook_text", "headline", "subhead", "body", "cta")}))
    # The claims checks run side by side (one Claude call each), each on its own connection to the same database.
    from creative_intelligence.db import get_connection
    path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(lambda t: _own_conn(lambda: get_connection(path),
                                                    lambda c: _claims(c, "hook", t[0], product["id"], t[1])), todo))
    def repair(item):                                      # a flagged hook gets its problems fixed, then re-checked
        (hid, fields), res = item
        for _ in range(HOOK_REPAIRS):
            if (res or {}).get("status") != "flagged":
                break
            res = _own_conn(lambda: get_connection(path), lambda c: _repair_hook(c, product, hid, fields, res)) or res
    flagged = [x for x in zip(todo, results) if (x[1] or {}).get("status") == "flagged"]
    with ThreadPoolExecutor(max_workers=5) as pool:          # side by side: one by one took minutes (2026-10-30)
        list(pool.map(repair, flagged))
    return len(hooks)


HOOK_REPAIRS = 2


def _repair_hook(conn, product: dict[str, Any], hid: int, fields: dict[str, str], res: dict[str, Any]) -> dict | None:
    from creative_intelligence.adstudio.chat import HOOK_FIELDS, repair_hook
    from creative_intelligence.adstudio.styles import claim_problems
    from creative_intelligence.webapp.common import _claims
    fixed = repair_hook(product, fields, claim_problems(res))
    if not fixed:
        return None
    fields.update(fixed)
    with conn:
        conn.execute(f"UPDATE adstudio_hooks SET {', '.join(k + '=?' for k in HOOK_FIELDS)} WHERE id=?",
                     (*[fixed[k] for k in HOOK_FIELDS], hid))
    return _claims(conn, "hook", hid, product["id"], {k: fixed[k] for k in HOOK_FIELDS})


def _own_conn(db: Callable[[], Any], fn: Callable[[Any], Any]) -> Any:
    """Run fn on a connection of its own (SQLite connections stay on the thread that opened them)."""
    c = db()
    try:
        return fn(c)
    finally:
        c.close()


def fill(db: Callable[[], Any], pid: int, client: Any = None) -> dict[str, Any]:
    """The auto-fill job: each step runs only when its part of the profile is empty. A failed step is
    noted and the next one still runs; the daily text cap stops the job."""
    from creative_intelligence import budget
    from creative_intelligence.adstudio.costs import spend_since
    conn = db()
    started = datetime.utcnow().isoformat()
    done: list[str] = []
    errors: list[str] = []

    def step(name: str) -> None:
        with conn:
            conn.execute("UPDATE adstudio_products SET fill_status='running', fill_step=? WHERE id=?", (name, pid))

    def product() -> dict[str, Any]:
        return dict(conn.execute("SELECT * FROM adstudio_products WHERE id=?", (pid,)).fetchone())

    try:
        try:
            budget.check(conn, "text", fill_estimate(conn))
        except budget.BudgetExceeded as exc:
            raise RuntimeError(str(exc)) from exc
        p = product()
        plan = [
            ("photos", lambda: p["origin"] == "shopify" and not conn.execute(
                "SELECT 1 FROM adstudio_photos WHERE product_id=?", (pid,)).fetchone(),
             lambda: import_shop_photos(conn, product())),
            ("look", lambda: not (product().get("physical_desc") or "").strip() and conn.execute(
                "SELECT 1 FROM adstudio_photos WHERE product_id=?", (pid,)).fetchone(),
             lambda: (lambda p: side.append(pool.submit(_own_conn, db, lambda c: draft_look(c, p, client=client))))(product())),
            ("research", lambda: not conn.execute("SELECT 1 FROM product_research WHERE product_id=? AND status='done'",
                                                  (pid,)).fetchone(),
             lambda: run_research(db, _research_row(conn, pid), client=client)),
            ("customer language", lambda: _needs_language(conn, pid, product()),
             lambda: research_language(conn, product(), client=client)),
            ("hooks", lambda: not _usable_hooks(conn, pid),         # all flagged counts as none (2026-10-26)
             lambda: auto_hooks(conn, product())),
        ]
        # Speed (feedback 2026-10-22): the photo look runs beside the web research; hooks wait for both.
        side: list[Any] = []
        pool = ThreadPoolExecutor(max_workers=1)

        def join_side() -> None:
            while side:
                f = side.pop()
                try:
                    f.result()
                except Exception as exc:
                    log.exception("auto-fill look failed for product %s", pid)
                    errors.append(f"look: {str(exc)[:160]}")

        for name, needed, run in plan:
            if name == "hooks":
                join_side()
            if not needed():
                continue
            step(name)
            try:
                run()
                done.append(name)
            except budget.BudgetExceeded as exc:
                errors.append(f"{name}: {exc}")
                break
            except Exception as exc:
                log.exception("auto-fill %s failed for product %s", name, pid)
                errors.append(f"{name}: {str(exc)[:160]}")
        join_side()
        pool.shutdown()
        cost = spend_since(conn, started)
        with conn:
            conn.execute("UPDATE adstudio_products SET fill_status=?, fill_step='', fill_error=?, fill_cost_usd=? WHERE id=?",
                         ("failed" if errors else "done", "; ".join(errors)[:500], cost, pid))
        return {"done": done, "errors": errors, "cost_usd": cost}
    except Exception as exc:
        with conn:
            conn.execute("UPDATE adstudio_products SET fill_status='failed', fill_step='', fill_error=? WHERE id=?",
                         (str(exc)[:500], pid))
        raise
    finally:
        conn.close()
