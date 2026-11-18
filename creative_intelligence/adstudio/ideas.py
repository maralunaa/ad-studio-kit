"""
The idea bank (Phase 2a R4): ideas worth testing, each with the evidence behind it kept in parts.

  An idea   an angle (why someone buys) + a hook pattern (a pattern, never copy: generation writes its own words,
            decision f) + a style from styles.yaml, for some products or all of them, and why it might work.
  Evidence  three parts, never one number:
              ours       our own ads made from the idea (or, until there are some, our ads with the same angle),
                         with their Meta results
              market     competitor ads with the idea's style, angle or hook type: how many brands, the longest run
              customers  how many real reviews and comments use the idea's customer words
  Status    new → approved → in_production → testing → winner / fatigued / retired. Generation uses an idea once it
            is approved; using it moves it to in production, a linked Meta ad moves it to testing. Winner, fatigued
            and retired are set by hand.

The mix (knowledge rules.ideas_mix): ideas steer a set share of what Ad Studio makes, per product and per surface
(image runs, Hook University hooks, AI ad briefs, video scripts), counted over the recent `window` of each, so the
rest still follows the playbook, Hook University and the winning patterns. assign() hands out ideas to the share
that is due, least-used first. An idea picked by hand ("Use this idea", idea_id) or typed in always applies.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

from creative_intelligence import knowledge

STATUSES = ("new", "approved", "in_production", "testing", "winner", "fatigued", "retired")
STATUS_NAMES = {"new": "New", "approved": "Approved", "in_production": "In production", "testing": "Testing",
                "winner": "Winner", "fatigued": "Fatigued", "retired": "Retired"}
from creative_intelligence import brand as _brand
NO_ANGLES = {"farm_origin"} if _brand.get("rules.no_origin_claims") else set()   # brand rule: no origin claims
# surface → (table, rows that count as one made item); idea_id on that table marks the idea-led ones
SURFACES = {
    "image": ("adstudio_images", "parent_image_id IS NULL AND core_id IS NULL"),
    "hooks": ("adstudio_hooks", "1=1"),
    "aiads": ("adstudio_briefs", "1=1"),
    "video": ("adstudio_videos", "source_kind IN ('story','styled')"),
}
MEDIUM = {"image": "static", "aiads": "static", "hooks": "any", "video": "video"}
BAR_STEPS = (10, 25, 40, 55, 70, 85, 100)   # width classes in studio.css (.w10 … .w100): no inline styles


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def mix() -> dict[str, Any]:
    return dict(knowledge.get("rules.ideas_mix"))


def set_share(share: float) -> dict[str, Any]:
    """Change the share from the Ideas page: an approved knowledge override (undo it in Admin › Knowledge)."""
    share = round(float(share), 2)
    if not 0 <= share <= 1:
        raise ValueError("The share is between 0 and 100%.")
    rest = [o for o in knowledge.overrides() if o["key"] != "rules.ideas_mix.share"]
    knowledge.save_overrides(rest + [{"file": "rules", "key": "rules.ideas_mix.share", "value": share,
                                      "proposal": "Ideas page", "approved_at": _now()}])
    return mix()


# ── the bank ──────────────────────────────────────────────────────────────
def _medium(style: str) -> str:
    try:
        return knowledge.style(style)["medium"] if style else "any"
    except KeyError:
        return "any"


def _clean(d: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    title = (d.get("title") or "").strip()
    angle = (d.get("angle") or "").strip()
    if not title:
        return None, "Give the idea a title."
    if angle in NO_ANGLES:
        return None, "Ads never claim where the fruit comes from, so an origin angle can't be an idea."
    style = (d.get("style") or "").strip()
    if style:
        try:
            knowledge.style(style)
        except KeyError:
            return None, f"Unknown style {style!r}."
    hook_type = (d.get("hook_type") or "").strip()
    if hook_type and hook_type not in knowledge.hook_type_keys():
        return None, f"Unknown hook type {hook_type!r}."
    pids = sorted({int(p) for p in d.get("product_ids") or [] if str(p).isdigit()})
    ev = d.get("evidence") or {}
    terms = [t.strip() for t in ev.get("customer_terms") or [] if t and t.strip()]
    evidence = {"customer_terms": terms[:8], "review_ids": [int(x) for x in ev.get("review_ids") or []][:20],
                "competitor_ad_ids": [int(x) for x in ev.get("competitor_ad_ids") or []][:20],
                "voice_theme_id": int(ev["voice_theme_id"]) if str(ev.get("voice_theme_id") or "").isdigit() else None,
                "notes": (ev.get("notes") or "").strip()}
    return {"title": title, "angle": angle, "hook": (d.get("hook") or "").strip(), "hook_type": hook_type,
            "style": style, "medium": _medium(style), "product_ids": json.dumps(pids),
            "why": (d.get("why") or "").strip(), "evidence_json": json.dumps(evidence)}, ""


def create(conn, d: dict[str, Any], source: str = "manual", source_ref: str = "", status: str = "new"
           ) -> tuple[int | None, str]:
    row, why = _clean(d)
    if row is None:
        return None, why
    if status not in STATUSES:
        return None, "unknown status"
    with conn:
        iid = conn.execute(
            f"INSERT INTO ideas ({', '.join(row)}, status, source, source_ref, created_at, updated_at)"
            f" VALUES ({','.join('?' * len(row))},?,?,?,?,?)",
            (*row.values(), status, source, source_ref, _now(), _now())).lastrowid
    return iid, ""


def update(conn, iid: int, d: dict[str, Any]) -> str:
    cur = get(conn, iid)
    if cur is None:
        return "idea not found"
    merged = {**cur, **{k: v for k, v in d.items() if k != "status"}}
    row, why = _clean(merged)
    if row is None:
        return why
    with conn:
        conn.execute(f"UPDATE ideas SET {', '.join(k + '=?' for k in row)}, updated_at=? WHERE id=?",
                     (*row.values(), _now(), iid))
    return set_status(conn, iid, d["status"]) if d.get("status") else ""


def set_status(conn, iid: int, status: str) -> str:
    if status not in STATUSES:
        return "unknown status"
    with conn:
        n = conn.execute("UPDATE ideas SET status=?, updated_at=? WHERE id=?", (status, _now(), iid)).rowcount
    return "" if n else "idea not found"


def _row(r: Any) -> dict[str, Any]:
    d = dict(r)
    d["product_ids"] = json.loads(d.get("product_ids") or "[]")
    d["evidence"] = json.loads(d.pop("evidence_json", None) or "{}")
    d["status_name"] = STATUS_NAMES.get(d["status"], d["status"])
    return d


def get(conn, iid: int) -> dict[str, Any] | None:
    r = conn.execute("SELECT * FROM ideas WHERE id=?", (iid,)).fetchone()
    return _row(r) if r else None


def listing(conn, status: str | None = None, product_id: int | None = None, with_evidence: bool = True
            ) -> list[dict[str, Any]]:
    q, args = "SELECT * FROM ideas", []
    if status:
        q, args = q + " WHERE status=?", [status]
    rows = [_row(r) for r in conn.execute(q + " ORDER BY id DESC", args)]
    if product_id is not None:
        rows = [i for i in rows if not i["product_ids"] or product_id in i["product_ids"]]
    if with_evidence:
        for i in rows:
            i["ev"] = evidence(conn, i)
        _bars(rows)
    names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM adstudio_products")}
    styles = {s["key"]: s["name"] for s in knowledge.load("styles")["styles"]}
    for i in rows:
        i["products"] = [names[p] for p in i["product_ids"] if p in names] or ["Every product"]
        i["style_name"] = styles.get(i["style"], "Any style")
        i["used"] = usage(conn, i["id"])
        i["picture"] = picture(i)
    return rows


def picture(i: dict[str, Any]) -> str:
    """The picture an idea came from (feedback 2026-10-22: ideas as visuals): a decoded winner or a competitor ad."""
    ref = i.get("source_ref") or ""
    if ref.startswith("winner:"):
        return f"/api/adstudio/visuals/winners/{ref.split(':', 1)[1]}/image"
    ads = (i.get("evidence") or {}).get("competitor_ad_ids") or []
    return f"/api/adstudio/market/ad/{ads[0]}/media" if ads else ""


def counts(conn) -> dict[str, int]:
    out = {s: 0 for s in STATUSES}
    for r in conn.execute("SELECT status, COUNT(*) FROM ideas GROUP BY status"):
        out[r[0]] = r[1]
    return out


# ── evidence, in parts ──────────────────────────────────────────────────────
def _results(conn, where: str, args: list[Any]) -> dict[str, Any]:
    from creative_intelligence.adstudio.assets import metrics
    n = conn.execute(f"SELECT COUNT(*), COUNT(meta_ad_id) FROM adstudio_assets WHERE {where}", args).fetchone()
    m = conn.execute(
        "SELECT COUNT(DISTINCT i.date) AS days, SUM(i.spend) AS spend, SUM(i.impressions) AS impressions,"
        " SUM(i.clicks) AS clicks, SUM(i.purchases) AS purchases, SUM(i.revenue) AS revenue,"
        " SUM(i.video_3s_views) AS v3, SUM(i.thruplays) AS thru, SUM(i.video_15s_views) AS v15"
        f" FROM ad_daily_insights i WHERE i.ad_id IN (SELECT meta_ad_id FROM adstudio_assets WHERE {where}"
        " AND meta_ad_id IS NOT NULL)", args).fetchone()
    return {"assets": n[0], "linked": n[1], **(metrics(dict(m)) if m and m["days"] else {})}


def evidence(conn, idea: dict[str, Any]) -> dict[str, Any]:
    """{ours, market, customers}: each with a label and a raw score the listing turns into a bar."""
    own = _results(conn, "idea_id=?", [idea["id"]])
    basis = "idea"
    if not own.get("spend") and idea["angle"]:
        own, basis = _results(conn, "angle=?", [idea["angle"]]), "angle"
    if own.get("roas") is not None:
        ours = {"label": f"Ours ROAS {own['roas']:.1f}" + (" (same angle)" if basis == "angle" else ""),
                "score": own["roas"], "basis": basis, **own}
    elif own.get("ctr") is not None:
        ours = {"label": f"Ours CTR {own['ctr']:.1f}%", "score": own["ctr"] / 2, "basis": basis, **own}
    else:
        made = own.get("assets", 0) if basis == "idea" else 0
        ours = {"label": f"Ours {made} made, no results yet" if made else "Ours not tested", "score": 0, "basis": basis,
                **own}

    conds, args = [], []
    for col in ("style", "angle", "hook_type"):
        if idea.get(col):
            conds.append(f"{col}=?")
            args.append(idea[col])
    ids = idea["evidence"].get("competitor_ad_ids") or []
    if ids:
        conds.append(f"id IN ({','.join('?' * len(ids))})")
        args += ids
    rows = [dict(r) for r in conn.execute(
        f"SELECT page_name, started_on FROM competitor_ads WHERE {' OR '.join(conds)}", args)] if conds else []
    days = [(date.today() - date.fromisoformat(r["started_on"][:10])).days for r in rows
            if (r["started_on"] or "")[:10].count("-") == 2]
    brands = len({r["page_name"] for r in rows})
    longest = max(days) if days else 0
    market = {"label": (f"Market {brands} brand{'s' if brands != 1 else ''}" + (f", {longest} d" if longest else ""))
              if brands else "Market none seen", "brands": brands, "ads": len(rows), "longest_days": longest,
              "score": brands + longest / 30}

    terms = idea["evidence"].get("customer_terms") or []
    pids = idea["product_ids"]
    mentions, quote = 0, None
    if terms:
        like = " OR ".join("LOWER(text) LIKE ?" for _ in terms)
        from creative_intelligence.adstudio.reviews import USABLE
        where = f"archived=0 AND {USABLE} AND ({like})"
        targs: list[Any] = [f"%{t.lower()}%" for t in terms]
        if pids:
            where += f" AND (product_id IN ({','.join('?' * len(pids))}) OR product_id IS NULL)"
            targs += pids
        mentions = conn.execute(f"SELECT COUNT(*) FROM reviews WHERE {where}", targs).fetchone()[0]
        q = conn.execute(f"SELECT id, text, author, kind, source, posted_on FROM reviews WHERE {where}"
                         " ORDER BY COALESCE(rating,0) DESC, LENGTH(text) LIMIT 1", targs).fetchone()
        if q:
            from creative_intelligence.adstudio.reviews import SOURCES, display_author
            quote = {"id": q["id"], "text": q["text"], "by": display_author(q["author"], q["kind"]),
                     "source": SOURCES.get(q["source"], q["source"]), "posted_on": q["posted_on"]}
    theme = conn.execute("SELECT mentions, review_ids FROM voice_themes WHERE id=?",
                         (idea["evidence"]["voice_theme_id"],)).fetchone() if idea["evidence"].get("voice_theme_id") else None
    if theme and theme["mentions"] > mentions:                   # counted by the customer-voice extraction
        mentions = theme["mentions"]
        first = json.loads(theme["review_ids"])[:1]
        q = conn.execute("SELECT id, text, author, kind, source, posted_on FROM reviews WHERE id=?", (first[0],)).fetchone() \
            if first else None
        if q:
            from creative_intelligence.adstudio.reviews import SOURCES, display_author
            quote = {"id": q["id"], "text": q["text"], "by": display_author(q["author"], q["kind"]),
                     "source": SOURCES.get(q["source"], q["source"]), "posted_on": q["posted_on"]}
    linked = bool(terms or theme)
    customers = {"label": f"Customers {mentions} mention{'s' if mentions != 1 else ''}" if linked else "Customers not linked",
                 "mentions": mentions, "terms": terms, "quote": quote, "score": mentions}
    return {"ours": ours, "market": market, "customers": customers}


def _bars(rows: list[dict[str, Any]]) -> None:
    """Each part's bar is relative to the strongest idea in the bank on that part (a width class, not a style)."""
    for part in ("ours", "market", "customers"):
        top = max((i["ev"][part]["score"] or 0 for i in rows), default=0)
        for i in rows:
            s = i["ev"][part]["score"] or 0
            pct = 100 * s / top if top else 0
            i["ev"][part]["bar"] = next((b for b in BAR_STEPS if pct <= b), 100) if s else 0


def usage(conn, iid: int) -> dict[str, int]:
    out = {k: conn.execute(f"SELECT COUNT(*) FROM {t} WHERE idea_id=? AND {w}", (iid,)).fetchone()[0]
           for k, (t, w) in SURFACES.items()}
    out["assets"] = conn.execute("SELECT COUNT(*) FROM adstudio_assets WHERE idea_id=?", (iid,)).fetchone()[0]
    return out


# ── the mix ─────────────────────────────────────────────────────────────────
def eligible(conn, product_id: int, surface: str, style: str = "") -> list[dict[str, Any]]:
    """Ideas generation may use for this product and surface (and the chosen style, when there is one: ideas
    with no style fit any), least used first, then the newest."""
    live = mix()["statuses"]
    want = MEDIUM[surface]
    rows = [_row(r) for r in conn.execute(
        f"SELECT * FROM ideas WHERE status IN ({','.join('?' * len(live))}) ORDER BY id DESC", live)]
    rows = [i for i in rows if (not i["product_ids"] or product_id in i["product_ids"])
            and (want == "any" or i["medium"] in ("any", want)) and (not style or i["style"] in ("", style))]
    used = {i["id"]: sum(usage(conn, i["id"]).values()) for i in rows}
    return sorted(rows, key=lambda i: used[i["id"]])


def due(conn, product_id: int, surface: str, n: int) -> int:
    """How many of the next n items should follow an idea so the share holds over the recent window."""
    m = mix()
    table, where = SURFACES[surface]
    recent = conn.execute(f"SELECT idea_id FROM {table} WHERE product_id=? AND {where} ORDER BY id DESC LIMIT ?",
                          (product_id, int(m["window"]))).fetchall()
    led = sum(1 for r in recent if r[0])
    return max(0, min(n, round((len(recent) + n) * float(m["share"])) - led))


def assign(conn, product_id: int, surface: str, n: int) -> list[dict[str, Any] | None]:
    """n slots: the due share get an idea each (least used first, spread out), the rest None."""
    slots: list[dict[str, Any] | None] = [None] * n
    pool = eligible(conn, product_id, surface) if n else []
    k = due(conn, product_id, surface, n) if pool else 0
    for j in range(k):
        slots[(j * n) // k] = pool[j % len(pool)]
    return slots


def pick_one(conn, product_id: int, surface: str, typed: str = "", idea_id: Any = None, n: int = 1,
             style: str = "") -> tuple[str, int | None]:
    """For one generation call that makes n items under one idea (a brief, a set of hooks, a script): (the idea
    text for the prompt, idea id or None). A picked idea always applies; a typed idea is the operator's own;
    otherwise the mix decides (an idea when at least half of the n items are due one)."""
    if idea_id:
        i = get(conn, int(idea_id))
        if i is None:
            return typed, None
        touch(conn, i["id"])
        return "\n".join(x for x in (prompt_block(i), typed) if x), i["id"]
    if typed.strip():
        return typed, None
    pool = eligible(conn, product_id, surface, style)
    if not pool or due(conn, product_id, surface, n) * 2 < n:
        return "", None
    got = pool[0]
    touch(conn, got["id"])
    return prompt_block(got), got["id"]


def touch(conn, iid: int) -> None:
    """Used for generation: an approved idea is now in production."""
    with conn:
        conn.execute("UPDATE ideas SET status='in_production', updated_at=? WHERE id=? AND status='approved'",
                     (_now(), iid))


def sync_status(conn) -> int:
    """After the Meta sync links assets: ideas in production with a linked ad are now testing."""
    with conn:
        return conn.execute(
            "UPDATE ideas SET status='testing', updated_at=? WHERE status='in_production' AND id IN"
            " (SELECT idea_id FROM adstudio_assets WHERE idea_id IS NOT NULL AND meta_ad_id IS NOT NULL)",
            (_now(),)).rowcount


def prompt_block(i: dict[str, Any]) -> str:
    """What a generation prompt is told about the idea. The hook is a pattern: never copied word for word."""
    style = ""
    if i.get("style"):
        try:
            s = knowledge.style(i["style"])
            style = f"{s['name']} ({s.get('when', '')})".strip()
        except KeyError:
            pass
    lines = [f"IDEA FROM THE IDEA BANK (follow it for this one): {i['title']}"]
    if i.get("angle"):
        lines.append(f"Angle: {i['angle'].replace('_', ' ')}")
    if i.get("hook"):
        lines.append(f"Hook pattern (a pattern to follow in your own words, never copied): {i['hook']}")
    if style:
        lines.append(f"Style: {style}")
    if i.get("why"):
        lines.append(f"Why it might work: {i['why']}")
    terms = (i.get("evidence") or {}).get("customer_terms") or []
    if terms:
        lines.append("Customers' own words for it: " + ", ".join(terms))
    lines.append("Every claim must still be an approved fact; the playbook rules still apply.")
    return "\n".join(lines)


# ── seeding the bank from what Ad Studio already has (free, no Claude call) ────
SEED_PRODUCTS = 3


def _top_products(conn) -> list[int]:
    """The products with the most approved work, for ideas that aren't tied to one product."""
    return [r[0] for r in conn.execute(
        "SELECT p.id FROM adstudio_products p LEFT JOIN adstudio_images i ON i.product_id=p.id AND i.review_status='approved'"
        " WHERE COALESCE(p.archived,0)=0 GROUP BY p.id ORDER BY COUNT(i.id) DESC, p.id LIMIT ?", (SEED_PRODUCTS,))]


def seed(conn) -> dict[str, Any]:
    """Feedback 2026-10-22: the bank was empty. New ideas (to approve) from our decoded winning videos, the winning
    patterns from our results, Hook University's angles and what 4–5 star reviews keep saying. Each is added once."""
    from creative_intelligence.adstudio import winning
    have = {r[0] for r in conn.execute("SELECT source_ref FROM ideas WHERE source='seeded'")}
    angles, hook_types = set(_angles()), set(knowledge.hook_type_keys())
    top = _top_products(conn)
    added = 0

    def add(ref: str, d: dict[str, Any]) -> None:
        nonlocal added
        if ref in have:
            return
        d["angle"] = d.get("angle") if d.get("angle") in angles else ""
        d["hook_type"] = d.get("hook_type") if d.get("hook_type") in hook_types else ""
        if create(conn, d, source="seeded", source_ref=ref)[0]:
            added += 1

    for t in conn.execute("SELECT id, ad_name FROM video_templates WHERE origin='own' ORDER BY id"):
        add(f"winner:{t['id']}", {"title": f"More like our winner: {t['ad_name'] or 'video ' + str(t['id'])}",
                                  "style": "template", "product_ids": top,
                                  "why": "One of our own decoded winning videos; same structure, new footage and hooks.",
                                  "evidence": {"notes": f"Decoded winner #{t['id']}"}})
    for p in winning.current_patterns(conn, limit=6, with_examples=False):
        add(f"pattern:{p['id']}", {"title": p.get("label") or p.get("pattern_name") or f"Pattern {p['id']}",
                                   "angle": p.get("angle"), "hook_type": p.get("hook_type"), "product_ids": top,
                                   "why": p.get("description") or "A pattern our winning ads share.",
                                   "evidence": {"notes": "Winning pattern from our Meta results"}})
    for a in sorted(angles):
        add(f"angle:{a}", {"title": a.replace("_", " ").capitalize(), "angle": a, "product_ids": top,
                           "why": "A Hook University angle to test with this product.",
                           "evidence": {"notes": "Hook University angle"}})
    for th in conn.execute("SELECT id, product_id, theme, mentions, angle FROM voice_themes WHERE kind IN"
                           " ('desire','moment','phrase') AND mentions>=5 ORDER BY mentions DESC LIMIT 12"):
        add(f"theme:{th['id']}", {"title": f"Customers say: {th['theme']}", "angle": th["angle"],
                                  "product_ids": [th["product_id"]] if th["product_id"] else top,
                                  "why": f"{th['mentions']} reviews (4–5 stars) say this.",
                                  "evidence": {"voice_theme_id": th["id"]}})
    return {"added": added}


# ── drafting ideas from the evidence (one Claude call, on the research cap) ────
DRAFT_ESTIMATE_USD = 0.25
DRAFT_N = 12


def _angles() -> list[str]:
    from creative_intelligence.tagging.ai_tagger import TAXONOMY
    return [a for a in TAXONOMY["angle"] if a not in NO_ANGLES and a != "other"]


def draft(conn, n: int = DRAFT_N, client: Any = None) -> dict[str, Any]:
    """Claude drafts n ideas (status new) from our results, the market and real customer words."""
    from creative_intelligence import budget, prompts
    from creative_intelligence.adstudio import assets, costs, facts, research, reviews
    from creative_intelligence.adstudio.chat import _client
    budget.check_research(conn, DRAFT_ESTIMATE_USD)
    products = [dict(r) for r in conn.execute("SELECT id, name, product_type FROM adstudio_products ORDER BY name")]
    if not products:
        return {"error": "Add products first."}
    styles = {s["key"]: s for s in knowledge.load("styles")["styles"] if s["status"] == "live"}
    hook_types = knowledge.hook_type_keys()
    angles = _angles()
    ours = {g: [{k: r[k] for k in ("key", "assets", "linked", "roas", "ctr", "hook_rate")} for r in assets.aggregate(conn, g)
                if r["linked"]][:8] for g in ("angle", "hook_type", "style")}
    market = [dict(r) for r in conn.execute(
        "SELECT id, page_name, headline, SUBSTR(body,1,240) AS body, style, hook_type, angle, started_on FROM competitor_ads"
        " WHERE tagged_at IS NOT NULL ORDER BY started_on LIMIT 60")]
    voice = [{"text": r["text"][:300], "product_id": r["product_id"], "rating": r["rating"]}
             for r in reviews.listing(conn)[:120]]
    fact_lines = []
    for p in products[:40]:
        fs = facts.list_facts(conn, p["id"], facts._family(p))
        if fs:
            fact_lines.append(f"- {p['name']}: " + "; ".join(f["text"] for f in fs[:8]))
    have = [f"- {i['title']}" for i in listing(conn, with_evidence=False)][:60]
    names = [p["name"] for p in products]
    schema = {"type": "object", "additionalProperties": False, "required": ["ideas"], "properties": {"ideas": {
        "type": "array", "items": {"type": "object", "additionalProperties": False,
                                   "required": ["title", "angle", "hook", "hook_type", "style", "products", "why",
                                                "customer_terms", "competitor_ad_ids"],
                                   "properties": {
                                       "title": {"type": "string", "description": "the idea in a few words"},
                                       "angle": {"type": "string", "enum": angles},
                                       "hook": {"type": "string", "description": "the hook PATTERN, not copy"},
                                       "hook_type": {"type": "string", "enum": hook_types},
                                       "style": {"type": "string", "enum": list(styles)},
                                       "products": {"type": "array", "items": {"type": "string", "enum": names}},
                                       "why": {"type": "string", "description": "the evidence it rests on, one or two sentences"},
                                       "customer_terms": {"type": "array", "items": {"type": "string"},
                                                          "description": "2-5 short words or phrases customers use for it"},
                                       "competitor_ad_ids": {"type": "array", "items": {"type": "integer"}}}}}}}
    ask = (f"OUR RESULTS (grouped, linked Meta ads only):\n{json.dumps(ours)}\n\n"
           f"WHAT OTHER BRANDS RUN (tagged; started_on = when it began running):\n{json.dumps(market)}\n\n"
           f"MARKET PATTERNS:\n{json.dumps({k: research.patterns(conn)[k] for k in ('style', 'hook_type')})}\n\n"
           f"REAL CUSTOMER WORDS (exact):\n" + "\n".join(f"- {v['text']}" for v in voice) + "\n\n"
           f"APPROVED FACTS BY PRODUCT:\n" + "\n".join(fact_lines) + "\n\n"
           f"STYLES WE CAN MAKE: {json.dumps({k: s['name'] + ' (' + s['medium'] + ')' for k, s in styles.items()})}\n\n"
           f"IDEAS ALREADY IN THE BANK (don't repeat):\n" + ("\n".join(have) or "(none)") + "\n\n"
           f"Draft {n} ideas.")
    client = client or _client(log=False)
    resp = client.beta.messages.create(
        model=config_model(), max_tokens=8000, betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        system=prompts.text("ideas-draft-system"), output_config={"format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": ask}])
    cost = costs.log_tokens(conn, config_model(), "copy", resp.usage, "market_research", 0)
    if resp.stop_reason == "refusal":
        return {"error": "Claude declined to draft ideas."}
    got = json.loads(next(b.text for b in resp.content if b.type == "text"))["ideas"]
    by_name = {p["name"]: p["id"] for p in products}
    known_ads = {a["id"] for a in market}
    made = []
    for g in got:
        iid, _ = create(conn, {**g, "product_ids": [by_name[x] for x in g["products"] if x in by_name],
                               "evidence": {"customer_terms": g["customer_terms"],
                                            "competitor_ad_ids": [a for a in g["competitor_ad_ids"] if a in known_ads]}},
                        source="drafted", source_ref=prompts.stamp("ideas-draft-system"))
        if iid:
            made.append(iid)
    return {"created": len(made), "ids": made, "cost_usd": cost}


def config_model() -> str:
    from creative_intelligence import config
    return config.CI_CLAUDE_CHECK_MODEL


def from_suggestion(conn, s: dict[str, Any], brief_id: int) -> tuple[int | None, str]:
    """A suggestion from the Monday brief, approved into the bank in one click. Older briefs suggested only a style,
    a product and an angle line; newer ones are whole ideas."""
    names = _product_ids_by_name(conn)
    prods = s.get("products") or ([s["product"]] if s.get("product") else [])
    title = s.get("title") or f"{s.get('style_name') or s.get('style')}: {s.get('angle', '')}".strip(": ")
    ref = f"brief:{brief_id}:{title.lower()}"
    if conn.execute("SELECT 1 FROM ideas WHERE source='brief' AND source_ref=?", (ref,)).fetchone():
        return None, "Already in the bank."
    angle = s.get("angle") if s.get("angle") in _angles() else ""
    return create(conn, {"title": title, "angle": angle, "hook": s.get("hook") or "", "hook_type": s.get("hook_type") or "",
                         "style": s.get("style") or "", "product_ids": [names[p] for p in prods if p in names],
                         "why": s.get("why") or "", "evidence": {"customer_terms": s.get("customer_terms") or []}},
                  source="brief", source_ref=ref, status="approved")


def brief_ref(brief_id: int, s: dict[str, Any]) -> str:
    title = s.get("title") or f"{s.get('style_name') or s.get('style')}: {s.get('angle', '')}".strip(": ")
    return f"brief:{brief_id}:{title.lower()}"


def _product_ids_by_name(conn) -> dict[str, int]:
    return {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM adstudio_products")}
