"""
Phase 2a R3: customer voice. Real reviews in, what customers want, worry about, say and use the fruit for out.

  Imports (re-runnable; an import never adds the same review twice)
    fera          the store's reviews app: site reviews and the Google reviews it imports, through its public
                  storefront API (read-only, slow, polite). New ones only after the first run.
    trustpilot    the public Trustpilot page, read with Claude's web fetch; words copied exactly, dated
    bbb           the BBB profile, found with web search on bbb.org
    testimonials  the site's testimonials page, marked curated (the store picked them)
    csv           anything else: support emails, survey answers, another export
  Extraction      Claude tags each review with desires, objections, use moments and exact phrases; the counts and the
                  links to the source reviews are computed here, and a phrase is kept only if it is really in the review.
  Objections      become angles only with an approved fact that answers them (fact_id); never otherwise.

Claude and web search calls are logged as source "market_research" under the research cap.
"""
from __future__ import annotations

from creative_intelligence import brand as _brand

import csv
import io
import json
import re
import time
from datetime import datetime, timedelta
from typing import Any

from creative_intelligence import config

MODEL = config.CI_CLAUDE_CHECK_MODEL
KINDS = {"desire": "Desires and reasons to buy", "objection": "Objections and problems", "moment": "Use moments",
         "phrase": "Phrases worth reusing"}
BATCH = 80
PER_GROUP = 400
MIN_TEXT = 15
TOP = 15
BATCH_ESTIMATE_USD = 0.09
WEB_ESTIMATE_USD = 0.25
FERA_URL = "https://api.fera.ai/v3/public/reviews"
WEB_EVERY = timedelta(days=28)
FERA_EVERY = timedelta(days=7)


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def _run(conn, kind: str, product_id: int | None = None) -> int:
    with conn:
        return conn.execute("INSERT INTO voice_runs (kind, product_id, started_at) VALUES (?,?,?)",
                            (kind, product_id, _now())).lastrowid


def _finish(conn, rid: int, result: dict[str, Any], cost: float = 0.0, error: str = "") -> None:
    with conn:
        conn.execute("UPDATE voice_runs SET finished_at=?, status=?, result_json=?, cost_usd=?, error=? WHERE id=?",
                     (_now(), "failed" if error else "done", json.dumps(result), round(cost, 4), error[:500], rid))


def _digest(text: str) -> str:
    import hashlib
    return hashlib.sha1(" ".join((text or "").split()).lower().encode()).hexdigest()[:10]


def _product_ids(conn) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in conn.execute("SELECT id, name FROM adstudio_products ORDER BY id"):
        out.setdefault(r["name"].strip().lower(), r["id"])
    return out


# ── imports ────────────────────────────────────────────────────────────────
def import_fera(conn, http: Any = None, max_pages: int = 60, pause: float = 1.0) -> dict[str, Any]:
    """Fera's public reviews feed, newest first; stops at the first page with nothing new (after the first run)."""
    import requests
    from creative_intelligence.adstudio import reviews
    from creative_intelligence.adstudio.winners import _log
    http = http or requests
    rid = _run(conn, "import_fera")
    names = _product_ids(conn)
    added = seen = 0
    try:
        for page in range(1, max_pages + 1):
            _log(f"{FERA_URL}?page={page}")
            j = http.get(FERA_URL, params={"public_key": config.FERA_PUBLIC_KEY, "limit": 100, "page": page,
                                           "sort_by": "created_at:desc"},
                         headers={"User-Agent": _brand.user_agent()}, timeout=60).json()
            new_here = 0
            for x in j.get("data") or []:
                seen += 1
                text = " ".join(p for p in ((x.get("heading") or "").strip(), (x.get("body") or "").strip()) if p)
                if len(text) < 3:
                    continue
                got, _ = reviews.add(conn, {
                    "kind": "review", "text": text, "author": (x.get("customer") or {}).get("display_name") or "A customer",
                    "rating": x.get("rating"), "source": "google" if x.get("channel_code") == "google" else "shopify",
                    "source_url": x.get("channel_url") or x.get("site_front_url") or "",
                    "posted_on": (x.get("created_at") or "")[:10], "external_id": f"fera:{x.get('id')}",
                    "product_id": names.get(((x.get("product") or {}).get("name") or "").strip().lower())})
                if got:
                    added += 1
                    new_here += 1
            pages = (j.get("meta") or {}).get("page_count") or page
            if page >= pages or (new_here == 0 and seen > 100):
                break
            time.sleep(pause)
    except Exception as exc:
        _finish(conn, rid, {"added": added, "seen": seen}, error=str(exc))
        raise
    out = {"added": added, "seen": seen}
    _finish(conn, rid, out)
    return out


WEB_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["reviews"], "properties": {"reviews": {
    "type": "array", "items": {"type": "object", "additionalProperties": False,
                               "required": ["author", "posted_on", "rating", "text", "url"],
                               "properties": {"author": {"type": "string"},
                                              "posted_on": {"type": "string", "description": "YYYY-MM-DD"},
                                              "rating": {"type": "integer"},
                                              "text": {"type": "string", "description": "the review's words, exactly"},
                                              "url": {"type": "string"}}}}}}


def import_web(conn, site: str, client: Any = None) -> dict[str, Any]:
    """Trustpilot, BBB or the testimonials page: Claude reads the public page(s) and copies each review exactly.
    Reviews without a date are left out (the library needs one); testimonials are marked curated."""
    from creative_intelligence import budget
    from creative_intelligence.adstudio import reviews
    from creative_intelligence.adstudio.chat import _client
    from creative_intelligence.adstudio.market import _format, _with_tools
    if site not in config.REVIEW_SITES:
        raise ValueError("unknown review site")
    budget.check_research(conn, WEB_ESTIMATE_USD)
    rid = _run(conn, f"import_{site}")
    client = client or _client(log=False)
    where = config.REVIEW_SITES[site]
    if site == "trustpilot":
        tools = [{"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 3, "allowed_domains": ["trustpilot.com"]}]
        ask = (f"Fetch {where} (and its next two pages, ?page=2 and ?page=3). Copy every review there exactly as written: "
               "the reviewer's name as shown, the date of experience or posting, the stars, the full words, the page URL.")
    else:
        domain = "bbb.org" if site == "bbb" else _brand.get("website")
        tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 3, "allowed_domains": [domain]},
                 {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 3, "allowed_domains": [domain]}]
        ask = (f"Find {where}, fetch the page, and copy every customer review or testimonial there exactly as written: "
               "the name as shown, the date when shown, the stars when shown, the full words, the page URL.")
    try:
        notes = _with_tools(conn, ask, "You copy real customer reviews from public pages for the brand's own "
                                       "research. Copy words exactly; never shorten, fix or invent a review.", tools, client)
        got = _format(conn, [{"type": "text", "text": f"NOTES:\n{notes}\n\nList every review. Dates as YYYY-MM-DD "
                                                      "('' when not shown); rating 0 when not shown."}],
                      WEB_SCHEMA, "You turn notes into a list. Keep each review's words exactly as in the notes.", client)
    except Exception as exc:
        _finish(conn, rid, {}, error=str(exc))
        raise
    added = skipped = 0
    for r in got["reviews"]:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", r["posted_on"] or "") or len((r["text"] or "").strip()) < 3:
            skipped += 1
            continue
        ext = f"{site}:{r['author'].strip().lower()}:{r['posted_on']}:{_digest(r['text'])}"
        new, _ = reviews.add(conn, {"kind": "review", "text": r["text"].strip(), "author": r["author"] or "A customer",
                                    "rating": r["rating"] or None, "source": site, "source_url": r["url"] or "",
                                    "posted_on": r["posted_on"], "external_id": ext, "curated": site == "testimonials"})
        added += 1 if new else 0
        skipped += 0 if new else 1
    out = {"added": added, "skipped": skipped, "read": len(got["reviews"])}
    _finish(conn, rid, out)
    return out


CSV_FIELDS = ("text", "author", "rating", "source", "posted_on", "product")


def import_csv(conn, raw: str) -> dict[str, Any]:
    """Columns: text, author, rating, source (google, shopify, email, …), posted_on (YYYY-MM-DD), product (name)."""
    from creative_intelligence.adstudio import reviews
    rid = _run(conn, "import_csv")
    rows = list(csv.DictReader(io.StringIO(raw)))
    if rows and not {"text", "posted_on"} <= {k.strip().lower() for k in rows[0]}:
        _finish(conn, rid, {}, error="needs text and posted_on columns")
        return {"error": "The CSV needs at least the columns text and posted_on (and ideally author, rating, source, product)."}
    names = _product_ids(conn)
    added, errors = 0, []
    for n, r in enumerate(rows, 2):
        r = {k.strip().lower(): (v or "").strip() for k, v in r.items() if k}
        src = r.get("source") or "other"
        new, why = reviews.add(conn, {"kind": "review", "text": r.get("text", ""), "author": r.get("author") or "A customer",
                                      "rating": r.get("rating"), "source": src if src in reviews.SOURCES else "other",
                                      "posted_on": r.get("posted_on", ""), "product_id": names.get(r.get("product", "").lower()),
                                      "external_id": f"csv:{r.get('author', '').lower()}:{r.get('posted_on')}:{_digest(r.get('text', ''))}"})
        if new:
            added += 1
        elif len(errors) < 5:
            errors.append(f"row {n}: {why}")
    out = {"added": added, "rows": len(rows), "errors": errors}
    _finish(conn, rid, out)
    return out


# ── extraction ─────────────────────────────────────────────────────────────
TAG_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["reviews"], "properties": {"reviews": {
    "type": "array", "items": {"type": "object", "additionalProperties": False,
                               "required": ["id", "desire", "objection", "moment", "phrase"],
                               "properties": {"id": {"type": "integer"},
                                              **{k: {"type": "array", "items": {"type": "string"}} for k in
                                                 ("desire", "objection", "moment", "phrase")}}}}}}
OBJ_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["objections"], "properties": {"objections": {
    "type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["theme", "fact_id", "angle"],
                               "properties": {"theme": {"type": "string"}, "fact_id": {"type": "integer"},
                                              "angle": {"type": "string"}}}}}}
TAG_SYSTEM = (
    f"You read real customer reviews of {_brand.name()} ({_brand.get('category')}). For each review list: "
    "desire = what they wanted or loved (a reason to buy), objection = a worry or problem (ripeness, damage, delays, "
    "refunds, price), moment = how they used it (gift, family treat, nostalgia, health, a celebration), phrase = up to "
    "two short phrases worth reusing in ads, copied EXACTLY from the review (3 to 12 words). Themes are 2 to 5 plain "
    "words; reuse a name from KNOWN THEMES whenever it fits, so the same idea is counted once. Empty lists are fine.")


def _group_reviews(conn, product_id: int | None) -> list[dict[str, Any]]:
    """The group's newest usable reviews (4–5 stars, never BBB): themes and phrases never come from complaints."""
    from creative_intelligence.adstudio import reviews
    q = (f"SELECT id, text, rating FROM reviews WHERE archived=0 AND kind='review' AND {reviews.USABLE} AND LENGTH(text)>=? AND "
         + ("product_id=?" if product_id else "product_id IS NULL") + " ORDER BY posted_on DESC LIMIT ?")
    return [dict(r) for r in conn.execute(q, (MIN_TEXT, product_id, PER_GROUP) if product_id else (MIN_TEXT, PER_GROUP))]


def estimate(conn, product_id: int | None) -> float:
    n = len(_group_reviews(conn, product_id))
    return round(-(-n // BATCH) * BATCH_ESTIMATE_USD + (0.03 if n else 0), 2)


def extract(conn, product_id: int | None, client: Any = None) -> dict[str, Any]:
    """Themes for one product (or shop-wide), counted from the reviews they were found in; objections mapped to
    approved facts. Replaces the group's earlier themes."""
    from creative_intelligence import budget
    from creative_intelligence.adstudio import costs, facts
    from creative_intelligence.adstudio.chat import _client
    rows = _group_reviews(conn, product_id)
    if not rows:
        return {"error": "No reviews with words for this product yet."}
    budget.check_research(conn, estimate(conn, product_id))
    rid = _run(conn, "extract", product_id)
    client = client or _client(log=False)
    text = {r["id"]: r["text"] for r in rows}
    found: dict[tuple[str, str], set[int]] = {}
    shown: dict[tuple[str, str], str] = {}
    cost = 0.0
    try:
        for i in range(0, len(rows), BATCH):
            chunk = rows[i:i + BATCH]
            known = [shown[kk] for kk in sorted((kk for kk in found if kk[0] != "phrase"),
                                                key=lambda kk: -len(found[kk]))][:120]
            ask = ("KNOWN THEMES: " + (", ".join(known) or "(none yet)") + "\n\nREVIEWS:\n"
                   + "\n".join(f"[{r['id']}] ({r['rating'] or '-'}★) {r['text'][:700]}" for r in chunk))
            resp = client.beta.messages.create(
                model=MODEL, max_tokens=12000, betas=["server-side-fallback-2026-07-01"], fallbacks="default",
                system=TAG_SYSTEM, output_config={"format": {"type": "json_schema", "schema": TAG_SCHEMA}},
                messages=[{"role": "user", "content": ask}])
            cost += costs.log_tokens(conn, MODEL, "copy", resp.usage, "market_research", rid)
            if resp.stop_reason == "refusal":
                raise RuntimeError("Claude declined to read these reviews.")
            for t in json.loads(next(b.text for b in resp.content if b.type == "text"))["reviews"]:
                if t["id"] not in text:
                    continue
                for kind in ("desire", "objection", "moment", "phrase"):
                    for v in t[kind][:4]:
                        v = " ".join(str(v).split()).strip(" .\"'“”")
                        if not v:
                            continue
                        if kind == "phrase" and v.lower() not in text[t["id"]].lower():
                            continue                      # a phrase must really be in the review
                        key = (kind, v.lower())
                        found.setdefault(key, set()).add(t["id"])
                        shown.setdefault(key, v if kind == "phrase" else v[0].upper() + v[1:])
        top = {k: sorted(((kk, ids) for kk, ids in found.items() if kk[0] == k), key=lambda x: -len(x[1]))[:TOP]
               for k in KINDS}
        objections = [shown[kk] for kk, _ in top["objection"]]
        mapped: dict[str, dict[str, Any]] = {}
        fs = facts.list_facts(conn, product_id, facts._family(dict(conn.execute(
            "SELECT * FROM adstudio_products WHERE id=?", (product_id,)).fetchone()))) if product_id else \
            [dict(r) for r in conn.execute("SELECT id, text FROM facts WHERE status='approved' AND product_id IS NULL")]
        fs = [f for f in fs if f.get("status", "approved") == "approved"]
        if objections and fs:
            resp = client.beta.messages.create(
                model=MODEL, max_tokens=3000, betas=["server-side-fallback-2026-07-01"], fallbacks="default",
                system=("Match each customer objection to the approved fact that answers it, if one does (fact_id 0 "
                        "when none does), and write the angle in one line using only that fact. Never answer an "
                        "objection with a claim that isn't in the facts."),
                output_config={"format": {"type": "json_schema", "schema": OBJ_SCHEMA}},
                messages=[{"role": "user", "content": "OBJECTIONS:\n" + "\n".join(f"- {o}" for o in objections)
                           + "\n\nAPPROVED FACTS:\n" + "\n".join(f"[{f['id']}] {f['text']}" for f in fs)}])
            cost += costs.log_tokens(conn, MODEL, "copy", resp.usage, "market_research", rid)
            ok = {f["id"] for f in fs}
            for m in json.loads(next(b.text for b in resp.content if b.type == "text"))["objections"]:
                if m["fact_id"] in ok:
                    mapped[m["theme"].lower()] = m
        with conn:
            conn.execute("DELETE FROM voice_themes WHERE " + ("product_id=?" if product_id else "product_id IS NULL"),
                         (product_id,) if product_id else ())
            for kind, items in top.items():
                for kk, ids in items:
                    m = mapped.get(shown[kk].lower()) if kind == "objection" else None
                    conn.execute("INSERT INTO voice_themes (run_id, product_id, kind, theme, mentions, review_ids, fact_id,"
                                 " angle) VALUES (?,?,?,?,?,?,?,?)",
                                 (rid, product_id, kind, shown[kk], len(ids), json.dumps(sorted(ids)[:200]),
                                  m["fact_id"] if m else None, m["angle"] if m else ""))
    except Exception as exc:
        _finish(conn, rid, {}, cost, str(exc))
        raise
    out = {"reviews": len(rows), "themes": sum(len(v) for v in top.values()), "cost_usd": round(cost, 4)}
    _finish(conn, rid, out, cost)
    return out


def themes(conn, product_id: int | None, shop_wide: bool = False) -> dict[str, list[dict[str, Any]]]:
    """A product's themes by kind, most mentioned first, each with up to 3 real quotes (exact, attributed, dated)."""
    from creative_intelligence.adstudio.reviews import SOURCES, display_author
    where = "product_id IS NULL" if shop_wide or not product_id else "product_id=?"
    rows = [dict(r) for r in conn.execute(f"SELECT * FROM voice_themes WHERE {where} ORDER BY mentions DESC",
                                          () if where.endswith("NULL") else (product_id,))]
    out: dict[str, list[dict[str, Any]]] = {k: [] for k in KINDS}
    for t in rows:
        ids = json.loads(t["review_ids"])[:3]
        t["quotes"] = [{"id": q["id"], "text": q["text"], "by": display_author(q["author"], q["kind"]),
                        "source": SOURCES.get(q["source"], q["source"]), "posted_on": q["posted_on"]}
                       for q in (conn.execute("SELECT * FROM reviews WHERE id=?", (i,)).fetchone() for i in ids) if q]
        if t["fact_id"]:
            f = conn.execute("SELECT text FROM facts WHERE id=?", (t["fact_id"],)).fetchone()
            t["fact"] = f[0] if f else ""
        out[t["kind"]].append(t)
    return out


def overview(conn) -> dict[str, Any]:
    """Which products have reviews and themes, the last runs, for the Customer voice tab."""
    groups = []
    for r in conn.execute("SELECT r.product_id, p.name, COUNT(*) AS n, MAX(r.posted_on) AS last FROM reviews r"
                          " LEFT JOIN adstudio_products p ON p.id=r.product_id WHERE r.archived=0 AND r.kind='review'"
                          " AND LENGTH(r.text)>=? GROUP BY r.product_id ORDER BY n DESC", (MIN_TEXT,)):
        t = conn.execute("SELECT COUNT(*), MAX(v.finished_at) FROM voice_themes t JOIN voice_runs v ON v.id=t.run_id WHERE "
                         + ("t.product_id=?" if r["product_id"] else "t.product_id IS NULL"),
                         (r["product_id"],) if r["product_id"] else ()).fetchone()
        groups.append({"product_id": r["product_id"], "product": r["name"] or "Shop-wide (no product)", "reviews": r["n"],
                       "last": r["last"], "themes": t[0], "extracted_at": t[1], "estimate": None})
    runs = [dict(r, result=json.loads(r["result_json"] or "{}")) for r in
            conn.execute("SELECT * FROM voice_runs ORDER BY id DESC LIMIT 12")]
    return {"groups": groups, "runs": runs}


def refresh(db: Any, http: Any = None, client: Any = None) -> dict[str, Any]:
    """The weekly customer-voice job: new Fera reviews, then Trustpilot and BBB, then themes again for the groups with
    10 or more new reviews since their last extraction. Stops cleanly at the research cap."""
    from creative_intelligence import budget
    conn = db()
    out: dict[str, Any] = {"fera": None, "web": {}, "extracted": [], "stopped": ""}
    try:
        out["fera"] = import_fera(conn, http=http)
        try:
            for site in ("trustpilot",):                        # monthly; BBB is complaints, never imported
                last = conn.execute("SELECT MAX(started_at) FROM voice_runs WHERE kind=? AND status='done'",
                                    (f"import_{site}",)).fetchone()[0]
                if not last or datetime.fromisoformat(last) <= datetime.utcnow() - WEB_EVERY:
                    out["web"][site] = import_web(conn, site, client=client)
            for g in overview(conn)["groups"]:
                since = g["extracted_at"] or ""
                fresh = conn.execute("SELECT COUNT(*) FROM reviews WHERE added_at>? AND "
                                     + ("product_id=?" if g["product_id"] else "product_id IS NULL"),
                                     (since, g["product_id"]) if g["product_id"] else (since,)).fetchone()[0]
                if g["themes"] and fresh >= 10:
                    extract(conn, g["product_id"], client=client)
                    out["extracted"].append(g["product"])
        except budget.BudgetExceeded as exc:
            out["stopped"] = str(exc)
        return out
    finally:
        conn.close()


def refresh_due(conn, now: datetime | None = None) -> bool:
    """Weekly (Fera is free; the paid parts inside have their own pace and the research cap)."""
    r = conn.execute("SELECT MAX(started_at) FROM voice_runs WHERE kind='import_fera'").fetchone()
    return not r[0] or datetime.fromisoformat(r[0]) <= (now or datetime.utcnow()) - FERA_EVERY
