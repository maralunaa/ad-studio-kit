"""
Phase 2a research (brief R; decision h): what the category runs, tagged with our own taxonomy, feeding styles and angles.

  fetch_library()   competitor ads from the Meta Ad Library API, read-only (GET ads_archive). Raw responses saved
                    timestamped, every call logged to outputs/api.log. Meta's API returns ordinary commercial ads only
                    for ads delivered in the EU/UK, so it searches those countries; US-only ads are added by hand.
  add_manual()      the "Add ad" fallback: an ad the operator saw (page, words, link, date). No scraper.
  tag()             Claude tags each ad with styles.yaml styles and families, hooks.yaml hook types and the Meta
                    tagger's angles, so competitors and our own ads compare on one taxonomy.
  patterns()        which styles and hook types the category uses, which we use, and the gaps both ways
                    ("no exotic fruit brand uses claymation").
  weekly_brief()    Claude suggests styles (not only angles) per product from the patterns, the real reviews and
                    comments, and the approved facts, e.g. "Ripening time-lapse for Lychee: answers the top
                    objection, unused by competitors". Scheduled weekly; also on demand.
  import_page_comments()  the Meta Page comments import (needs the read-only Page permission, decision j): real
                    comments into the reviews library, for customer language and the Comment-reply style.

Claude calls are logged as source "market_research" and checked against the research cap ($10 a month by default).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from typing import Any

from creative_intelligence import config, knowledge

MODEL = config.CI_CLAUDE_CHECK_MODEL
EU_COUNTRIES = ["DE", "FR", "NL", "ES", "IT", "IE", "BE", "AT", "PT", "SE"]
TAG_BATCH = 20
TAG_ESTIMATE_USD = 0.15         # a batch of 20, screenshots included
WEEK = timedelta(days=7)
FIELDS = ("id,page_id,page_name,ad_creative_bodies,ad_creative_link_titles,ad_creative_link_captions,"
          "ad_delivery_start_time,ad_delivery_stop_time,ad_snapshot_url,publisher_platforms")


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


# ── collecting ads ────────────────────────────────────────────────────────
def add_manual(conn, d: dict[str, Any]) -> tuple[int | None, str]:
    """An ad the operator saw: typed in, or read from a screenshot or landing page first (market.read_*)."""
    from creative_intelligence.adstudio import market
    page, body = (d.get("page_name") or "").strip(), (d.get("body") or "").strip()
    headline = (d.get("headline") or "").strip()
    if not page or not (body or headline):
        return None, "Add the brand's page name and the ad's words (primary text or headline)."
    url = (d.get("url") or "").strip()
    archive = market.archive_id(url)
    if archive and conn.execute("SELECT 1 FROM competitor_ads WHERE ad_archive_id=?", (archive,)).fetchone():
        return None, "That Ad Library ad is already here."
    with conn:
        rid = conn.execute(
            "INSERT INTO competitor_ads (source, ad_archive_id, page_name, body, headline, url, started_on, ended_on,"
            " active, notes, cta, landing_url, media_path, media_kind, brand_id, concept_key, last_seen_on, added_at)"
            " VALUES ('manual',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (archive, page, body, headline, url, (d.get("seen_on") or d.get("started_on") or "").strip(),
             (d.get("ended_on") or "").strip(), 0 if d.get("ended_on") or d.get("active") is False else 1, (d.get("notes") or "").strip(),
             (d.get("cta") or "").strip(), (d.get("landing_url") or "").strip(), market.safe_media(d.get("media_path") or ""),
             d.get("media_kind") or "", market.brand_for(conn, page), market.concept_key(page, body, headline),
             _now()[:10], _now())).lastrowid
    return rid, ""


def fetch_library(conn, search_terms: str, countries: list[str] | None = None, http: Any = None,
                  page_ids: list[str] | None = None) -> dict[str, Any]:
    """Read-only Ad Library search, by words or by the brands' Page ids. New ads are stored (dedupe by archive id);
    ads seen again get their stop date and active flag refreshed. Returns {added, updated, seen} or {error}."""
    import requests
    from creative_intelligence.adstudio import market
    from creative_intelligence.adstudio.winners import _log, save_raw
    terms = (search_terms or "").strip()
    if not terms and not page_ids:
        return {"error": "Type what to search for (e.g. exotic fruit, mango delivery)."}
    http = http or requests
    url = f"https://graph.facebook.com/{os.getenv('META_API_VERSION', 'v26.0')}/ads_archive"
    _log(url)
    params = {"ad_type": "ALL", "ad_active_status": "ALL", "limit": 50, "fields": FIELDS,
              "ad_reached_countries": json.dumps(countries or EU_COUNTRIES),
              "access_token": os.getenv("META_ACCESS_TOKEN", "")}
    if page_ids:
        params["search_page_ids"] = json.dumps(page_ids[:10])
    else:
        params["search_terms"] = terms
    data = http.get(url, params=params, timeout=60).json()
    save_raw("ad_library", data)
    if "error" in data:
        msg = (data["error"] or {}).get("message", "Ad Library error")
        return {"error": f"Meta Ad Library: {msg} (the token's account needs Ad Library API access)."}
    added = updated = 0
    today = _now()[:10]
    for ad in data.get("data") or []:
        bodies, titles = ad.get("ad_creative_bodies") or [], ad.get("ad_creative_link_titles") or []
        page, body, head = ad.get("page_name") or "", bodies[0] if bodies else "", titles[0] if titles else ""
        stop = (ad.get("ad_delivery_stop_time") or "")[:10]
        active = 0 if stop and stop < today else 1
        captions = ad.get("ad_creative_link_captions") or []
        with conn:
            n = conn.execute(
                "INSERT OR IGNORE INTO competitor_ads (source, ad_archive_id, page_name, body, headline, url, started_on,"
                " ended_on, active, platforms, landing_url, brand_id, concept_key, last_seen_on, added_at)"
                " VALUES ('ad_library',?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (str(ad.get("id")), page, body, head, ad.get("ad_snapshot_url") or "",
                 (ad.get("ad_delivery_start_time") or "")[:10], stop, active,
                 ",".join(ad.get("publisher_platforms") or []), captions[0] if captions else "",
                 market.brand_for(conn, page, ad.get("page_id")), market.concept_key(page, body, head), today,
                 _now())).rowcount
            if n:
                added += 1
            else:
                updated += conn.execute("UPDATE competitor_ads SET ended_on=?, active=?, last_seen_on=? WHERE ad_archive_id=?",
                                        (stop, active, today, str(ad.get("id")))).rowcount
    return {"added": added, "updated": updated, "seen": len(data.get("data") or [])}


def listing(conn, limit: int = 200) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT * FROM competitor_ads ORDER BY id DESC LIMIT ?", (limit,))]


# ── tagging ────────────────────────────────────────────────────────────────
PRODUCT_TYPES = {"single_fruit": "one fruit or one kind", "variety_box": "a mixed or build-your-own box",
                 "gift_box": "a gift box, basket or gift set", "subscription": "a club, membership or subscription",
                 "other_food": "another food or drink", "other": "none of these"}
VISUAL_STYLES = {"studio_clean": "polished studio product shot", "natural_lifestyle": "natural-light lifestyle scene",
                 "ugc_phone": "phone-shot, creator or customer look", "macro_closeup": "macro close-up of the food",
                 "graphic_text": "graphic, text-led design", "animation": "illustration, animation or claymation",
                 "other": "none of these"}


def _taxonomy() -> dict[str, list[str]]:
    """The same taxonomy our own ads use: styles.yaml, hooks.yaml, the Meta tagger's angles, formats and offers."""
    from creative_intelligence.tagging.ai_tagger import TAXONOMY
    return {"style": [s["key"] for s in knowledge.load("styles")["styles"]] + ["other"],
            "hook_type": knowledge.hook_type_keys() + ["other"],
            "angle": list(TAXONOMY["angle"]), "format": list(TAXONOMY["archetype"]),
            "offer_type": list(TAXONOMY["offer_style"]), "product_type": list(PRODUCT_TYPES),
            "visual_style": list(VISUAL_STYLES)}


TAG_FIELDS = ("style", "hook_type", "angle", "format", "offer_type", "product_type", "visual_style")


def tag(conn, client: Any = None, limit: int = TAG_BATCH) -> dict[str, Any]:
    """Tag untagged competitor ads (one Claude call per batch), screenshots included. Research cap checked first."""
    import base64
    from pathlib import Path
    from creative_intelligence import budget
    from creative_intelligence.adstudio import costs
    from creative_intelligence.adstudio.chat import _client
    from creative_intelligence.tagging.ai_tagger import TAXONOMY
    rows = [dict(r) for r in conn.execute("SELECT id, page_name, body, headline, cta, media_path FROM competitor_ads"
                                          " WHERE tagged_at IS NULL ORDER BY id LIMIT ?", (limit,))]
    if not rows:
        return {"tagged": 0}
    budget.check_research(conn, TAG_ESTIMATE_USD)
    tax = _taxonomy()
    styles = {s["key"]: s for s in knowledge.load("styles")["styles"]}
    item = {f: {"type": "string", "enum": tax[f]} for f in TAG_FIELDS}
    item["id"] = {"type": "integer"}
    item["why_it_works"] = {"type": "string", "description": "one sentence: why this ad might work"}
    schema = {"type": "object", "additionalProperties": False, "required": ["ads"], "properties": {"ads": {
        "type": "array", "items": {"type": "object", "additionalProperties": False, "required": list(item),
                                   "properties": item}}}}
    lists = {"STYLES": "\n".join(f"- {k}: {s['name']} ({s['medium']}; {s.get('when', '')})" for k, s in styles.items()),
             "HOOK TYPES": "\n".join(f"- {t['key']}: {t['definition']}" for t in knowledge.hook_types()),
             "ANGLES": "\n".join(f"- {k}: {v}" for k, v in TAXONOMY["angle"].items()),
             "FORMATS": "\n".join(f"- {k}: {v}" for k, v in TAXONOMY["archetype"].items()),
             "OFFER TYPES": "\n".join(f"- {k}: {v}" for k, v in TAXONOMY["offer_style"].items()),
             "PRODUCT TYPES": "\n".join(f"- {k}: {v}" for k, v in PRODUCT_TYPES.items()),
             "VISUAL STYLES": "\n".join(f"- {k}: {v}" for k, v in VISUAL_STYLES.items())}
    content: list[dict[str, Any]] = [{"type": "text", "text": "\n\n".join(f"{k}:\n{v}" for k, v in lists.items())}]
    for r in rows:
        content.append({"type": "text", "text": f"[{r['id']}] {r['page_name']}\nHeadline: {r['headline']}\n"
                                                f"Button: {r['cta']}\nText: {r['body'][:600]}"})
        mp = Path(r["media_path"]) if r["media_path"] else None
        if mp and mp.is_file() and mp.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
            mime = {"jpg": "jpeg"}.get(mp.suffix[1:].lower(), mp.suffix[1:].lower())
            content.append({"type": "image", "source": {"type": "base64", "media_type": f"image/{mime}",
                                                        "data": base64.standard_b64encode(mp.read_bytes()).decode()}})
    client = client or _client(log=False)
    resp = client.beta.messages.create(
        model=MODEL, max_tokens=6000, betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        system=("You tag other brands' Meta ads with one fixed taxonomy, the same one the brand uses for its own "
                "ads, so they can be compared. For each ad pick the single best value in every list ('other' when "
                "none fits) and say in one sentence why it might work. Judge only what the words and image show."),
        output_config={"format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": content}])
    costs.log_tokens(conn, MODEL, "copy", resp.usage, "market_research", 0)
    tags = json.loads(next(b.text for b in resp.content if b.type == "text"))["ads"]
    ids = {r["id"] for r in rows}
    done = 0
    with conn:
        for t in tags:
            if t["id"] not in ids:
                continue
            fam = (styles.get(t["style"]) or {}).get("family", "")
            done += conn.execute(
                f"UPDATE competitor_ads SET {', '.join(f + '=?' for f in TAG_FIELDS)}, family=?, why_it_works=?,"
                " tagged_at=? WHERE id=?",
                (*[t[f] for f in TAG_FIELDS], fam, t["why_it_works"], _now(), t["id"])).rowcount
    return {"tagged": done}


# ── patterns and gaps ───────────────────────────────────────────────────────
def _our_style(asset: dict[str, Any], template_style: dict[str, str]) -> str:
    fmt, style = asset.get("format") or "", (asset.get("style") or "").split("/")[0]
    if fmt.startswith("compose-"):
        return template_style.get(fmt.removeprefix("compose-"), "")
    if style in {s["key"] for s in knowledge.load("styles")["styles"]}:
        return style
    return "animated_still" if style == "animate" else ""


def patterns(conn) -> dict[str, Any]:
    """Counts by family, style and hook type for competitors and for our own ads, and the gaps."""
    styles = {s["key"]: s for s in knowledge.load("styles")["styles"]}
    template_style = {t: s["key"] for s in styles.values() if s["route"]["kind"] == "compose"
                      for t in s["route"].get("templates", [])}
    theirs = [dict(r) for r in conn.execute("SELECT style, family, hook_type FROM competitor_ads WHERE tagged_at IS NOT NULL")]
    ours_rows = [dict(r) for r in conn.execute("SELECT format, style, hook_type FROM adstudio_assets")]
    ours = [{"style": _our_style(a, template_style), "hook_type": a.get("hook_type") or ""} for a in ours_rows]
    for o in ours:
        o["family"] = (styles.get(o["style"]) or {}).get("family", "")

    def count(rows, key):
        out: dict[str, int] = {}
        for r in rows:
            if r.get(key) and r[key] != "other":
                out[r[key]] = out.get(r[key], 0) + 1
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    t_style, o_style = count(theirs, "style"), count(ours, "style")
    return {"crowding": crowding(conn),"competitor_ads": len(theirs), "our_ads": len(ours),
            "family": {"theirs": count(theirs, "family"), "ours": count(ours, "family")},
            "style": {"theirs": t_style, "ours": o_style},
            "hook_type": {"theirs": count(theirs, "hook_type"), "ours": count(ours, "hook_type")},
            "unused_by_competitors": [k for k in styles if k not in t_style],
            "they_use_we_dont": [k for k in t_style if k not in o_style],
            "names": {k: s["name"] for k, s in styles.items()}}


CROWDED_BRANDS = 3


def crowding(conn) -> dict[str, list[dict[str, Any]]]:
    """Proven and Evergreen ads (30+ days) by angle, hook type and format: how many brands run each, the longest
    run, and whether it is crowded (3+ brands) or a gap (one brand, still running long)."""
    from creative_intelligence.adstudio import market
    long = [r for r in market.listing(conn, limit=5000) if r["stage"] in ("proven", "evergreen") and r["tagged_at"]]
    out: dict[str, list[dict[str, Any]]] = {}
    for f in ("angle", "hook_type", "format", "style"):
        groups: dict[str, dict[str, Any]] = {}
        for r in long:
            k = r.get(f) or ""
            if not k or k == "other":
                continue
            g = groups.setdefault(k, {"key": k, "brands": set(), "ads": 0, "longest": 0})
            g["brands"].add(r["brand"])
            g["ads"] += 1
            g["longest"] = max(g["longest"], r["days"] or 0)
        rows = []
        for g in groups.values():
            n = len(g["brands"])
            rows.append({"key": g["key"], "brands": n, "ads": g["ads"], "longest": g["longest"],
                         "brand_names": sorted(g["brands"]),
                         "label": "crowded" if n >= CROWDED_BRANDS else "gap" if n == 1 else ""})
        out[f] = sorted(rows, key=lambda x: (-x["brands"], -x["longest"]))
    return out


# ── the Monday research brief (R5) ───────────────────────────────────────────
BRIEF_ESTIMATE_USD = 0.25
FATIGUE_MIN_DAYS = 21
WINNER_MIN_SPEND = 50.0


def _since_last_brief(conn) -> str:
    r = conn.execute("SELECT MAX(created_at) FROM research_briefs").fetchone()
    return r[0] or (datetime.utcnow() - WEEK).isoformat(timespec="seconds")


def _angle_counts(conn) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in conn.execute("SELECT angle, COUNT(*) FROM competitor_ads WHERE active=1 AND tagged_at IS NOT NULL"
                          " AND angle NOT IN ('', 'other') GROUP BY angle"):
        out[r[0]] = r[1]
    return out


def _account_ads(conn) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Our winners (best ROAS over the last 14 days, real spend) and fatigued ads by the playbook's rule (two trips:
    CTR down 25% from week one, cost per result up 30% from the first two weeks; daily frequency isn't synced)."""
    from creative_intelligence.adstudio.assets import metrics
    rules = knowledge.load("metrics")["fatigue"]
    today = conn.execute("SELECT MAX(date) FROM ad_daily_insights").fetchone()[0]
    if not today:
        return [], []
    end = datetime.fromisoformat(today)
    d14, d7 = (end - timedelta(days=13)).date().isoformat(), (end - timedelta(days=6)).date().isoformat()

    def agg(ad_id: str, lo: str, hi: str) -> dict[str, Any]:
        r = conn.execute("SELECT COUNT(*) AS days, SUM(spend) AS spend, SUM(impressions) AS impressions, SUM(clicks) AS clicks,"
                         " SUM(purchases) AS purchases, SUM(revenue) AS revenue FROM ad_daily_insights"
                         " WHERE ad_id=? AND date BETWEEN ? AND ?", (ad_id, lo, hi)).fetchone()
        return metrics(dict(r))

    winners, fatigued = [], []
    for a in conn.execute("SELECT ad_id, MAX(ad_name) AS name, MIN(date) AS first FROM ad_daily_insights"
                          " GROUP BY ad_id HAVING MAX(date) >= ?", (d7,)).fetchall():
        recent = agg(a["ad_id"], d14, today)
        if recent["spend"] >= WINNER_MIN_SPEND and (recent["purchases"] or 0) >= 3 and recent["roas"]:
            winners.append({"ad_id": a["ad_id"], "name": a["name"], "roas": recent["roas"], "spend": recent["spend"],
                            "purchases": recent["purchases"]})
        first = datetime.fromisoformat(a["first"])
        if (end - first).days < FATIGUE_MIN_DAYS:
            continue
        wk1 = agg(a["ad_id"], a["first"], (first + timedelta(days=6)).date().isoformat())
        two = agg(a["ad_id"], a["first"], (first + timedelta(days=13)).date().isoformat())
        last = agg(a["ad_id"], d7, today)
        trips = []
        if wk1["ctr"] and last["ctr"] is not None and last["ctr"] <= wk1["ctr"] * (1 - rules["engagement_drop_from_week_one"]):
            trips.append(f"CTR {wk1['ctr']}% → {last['ctr']}%")
        if two["cpa"] and last["cpa"] and last["cpa"] >= two["cpa"] * (1 + rules["cost_per_result_rise_from_first_two_weeks"]):
            trips.append(f"cost per purchase ${two['cpa']} → ${last['cpa']}")
        if len(trips) >= rules["trips_needed"]:
            fatigued.append({"ad_id": a["ad_id"], "name": a["name"], "trips": trips, "spend_7d": last["spend"]})
    winners.sort(key=lambda w: -w["roas"])
    return winners[:5], sorted(fatigued, key=lambda f: -f["spend_7d"])[:8]


def brief_sections(conn) -> dict[str, Any]:
    """What changed since the last brief, computed (no Claude): new long-runners, angles gaining or fading, new
    customer themes, our winners and fatigued ads."""
    from creative_intelligence.adstudio import market
    since = _since_last_brief(conn)
    last = latest_brief(conn) or {}
    long_new = []
    for r in market.listing(conn, limit=5000):
        if r["stage"] not in ("proven", "evergreen") or not r["days"]:
            continue
        crossed = any(0 <= r["days"] - lim < 7 for lim in (30, 90))
        if crossed or (r["added_at"] or "") >= since:
            long_new.append({k: r[k] for k in ("id", "brand", "headline", "body", "days", "stage_name", "style", "angle")})
    now = _angle_counts(conn)
    before = (last.get("snapshot") or {}).get("angles") or {}
    trend = sorted(({"angle": k, "now": now.get(k, 0), "before": before.get(k, 0), "change": now.get(k, 0) - before.get(k, 0)}
                    for k in set(now) | set(before)), key=lambda x: -abs(x["change"]))
    themes = [dict(r) for r in conn.execute(
        "SELECT t.kind, t.theme, t.mentions, p.name AS product FROM voice_themes t JOIN voice_runs v ON v.id=t.run_id"
        " LEFT JOIN adstudio_products p ON p.id=t.product_id WHERE v.finished_at>=? AND t.kind IN ('desire','objection','moment')"
        " ORDER BY t.mentions DESC LIMIT 10", (since,))]
    winners, fatigued = _account_ads(conn)
    return {"since": since, "long_runners": long_new[:12], "angles": {"gaining": [t for t in trend if t["change"] > 0][:5],
            "fading": [t for t in trend if t["change"] < 0][:5]} if before else {"gaining": [], "fading": []},
            "themes": themes, "winners": winners, "fatigued": fatigued, "snapshot": {"angles": now}}


def weekly_brief(conn, client: Any = None, trigger: str = "manual") -> dict[str, Any]:
    """The Monday brief: the computed sections, a short read, and 6 to 10 diverse ideas to approve into the bank.
    Research cap checked first. Stored; posted to Slack when SLACK_WEBHOOK_URL is set (BRIEF_SLACK=0 turns it off)."""
    from creative_intelligence import budget
    from creative_intelligence.adstudio import costs, facts, ideas, reviews
    from creative_intelligence.adstudio.chat import _client
    budget.check_research(conn, BRIEF_ESTIMATE_USD)
    sec = brief_sections(conn)
    pat = patterns(conn)
    products = [dict(r) for r in conn.execute("SELECT id, name, product_type FROM adstudio_products ORDER BY name")]
    styles = {s["key"]: s for s in knowledge.load("styles")["styles"] if s["status"] == "live"}
    names = [p["name"] for p in products]
    fact_lines = []
    for p in products[:25]:
        fs = facts.list_facts(conn, p["id"], facts._family(p))
        if fs:
            fact_lines.append(f"- {p['name']}: " + "; ".join(f["text"] for f in fs[:6]))
    voice = [r["text"][:240] for r in reviews.listing(conn)[:40]]
    have = [i["title"] for i in ideas.listing(conn, with_evidence=False)][:80]
    item = {"title": {"type": "string"}, "angle": {"type": "string", "enum": ideas._angles()},
            "hook": {"type": "string", "description": "the hook PATTERN, not copy"},
            "hook_type": {"type": "string", "enum": knowledge.hook_type_keys()},
            "style": {"type": "string", "enum": list(styles)},
            "products": {"type": "array", "items": {"type": "string", "enum": names}} if names else
            {"type": "array", "items": {"type": "string"}},
            "why": {"type": "string"}, "customer_terms": {"type": "array", "items": {"type": "string"}}}
    schema = {"type": "object", "additionalProperties": False, "required": ["summary", "suggestions"], "properties": {
        "summary": {"type": "string", "description": "three to five sentences: what changed and what to test"},
        "suggestions": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                                   "required": list(item), "properties": item}}}}
    ask = (f"WHAT CHANGED SINCE THE LAST BRIEF:\n{json.dumps({k: v for k, v in sec.items() if k != 'snapshot'}, default=str)[:12000]}\n\n"
           f"CROWDED VS GAP (Proven/Evergreen):\n{json.dumps({k: pat['crowding'][k][:8] for k in ('angle', 'hook_type', 'format')}, default=str)[:4000]}\n"
           f"STYLES NO COMPETITOR USES: {', '.join(pat['unused_by_competitors'])}\n"
           f"STYLES WE CAN MAKE: {json.dumps({k: s['name'] + ' (' + s['medium'] + ')' for k, s in styles.items()})}\n\n"
           "APPROVED FACTS BY PRODUCT:\n" + "\n".join(fact_lines) + "\n\nREAL CUSTOMER WORDS (exact):\n"
           + "\n".join(f"- {v}" for v in voice) + "\n\nALREADY IN THE IDEA BANK (don't repeat):\n"
           + ("\n".join(f"- {t}" for t in have) or "(none)")
           + "\n\nWrite the brief: a short read of what changed, then 6 to 10 ideas to test this week, diverse in angle, "
             "hook type, style (statics and videos) and product. Build on the long-runners and gaps, answer the "
             "objections customers raise with approved facts, refresh what is fatigued, and extend our winners.")
    client = client or _client(log=False)
    resp = client.beta.messages.create(
        model=MODEL, max_tokens=8000, betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        system=prompts_text(), output_config={"format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": ask}])
    cost = costs.log_tokens(conn, MODEL, "copy", resp.usage, "market_research", 0)
    if resp.stop_reason == "refusal":
        raise RuntimeError("Claude declined to write the brief.")
    brief = json.loads(next(b.text for b in resp.content if b.type == "text"))
    brief["suggestions"] = [s for s in brief["suggestions"] if s["style"] in styles][:10]
    for s in brief["suggestions"]:
        s["style_name"] = styles[s["style"]]["name"]
    brief.update(sections={k: v for k, v in sec.items() if k != "snapshot"}, snapshot=sec["snapshot"])
    with conn:
        bid = conn.execute("INSERT INTO research_briefs (created_at, trigger, brief_json, cost_usd) VALUES (?,?,?,?)",
                           (_now(), trigger, json.dumps(brief, default=str), cost)).lastrowid
    _slack(brief)
    return {"id": bid, **brief}


def _slack(brief: dict[str, Any]) -> bool:
    if os.getenv("BRIEF_SLACK", "1") == "0":
        return False
    from creative_intelligence.alerts import notify_slack
    s = brief["sections"]
    lines = [brief["summary"], "",
             f"{len(s['long_runners'])} new long-running competitor ads · {len(s['themes'])} new customer themes · "
             f"{len(s['winners'])} winners · {len(s['fatigued'])} fatigued ads",
             "Ideas to approve: " + "; ".join(x["title"] for x in brief["suggestions"][:10]),
             "Approve them in Ad Studio › Ideas › Idea bank."]
    return notify_slack("Monday research brief", "\n".join(lines), level="info")


def prompts_text() -> str:
    from creative_intelligence import prompts
    return prompts.text("research-brief-system")


def latest_brief(conn) -> dict[str, Any] | None:
    r = conn.execute("SELECT * FROM research_briefs ORDER BY id DESC LIMIT 1").fetchone()
    return {"id": r["id"], "created_at": r["created_at"], **json.loads(r["brief_json"])} if r else None


def brief_due(conn, now: datetime | None = None) -> bool:
    """Mondays (UTC): due when there is no brief since this Monday began and there is something to read
    (tagged competitor ads, customer themes or synced Meta results)."""
    now = now or datetime.utcnow()
    if now.weekday() != 0:
        return False
    if not (conn.execute("SELECT 1 FROM competitor_ads WHERE tagged_at IS NOT NULL LIMIT 1").fetchone()
            or conn.execute("SELECT 1 FROM voice_themes LIMIT 1").fetchone()
            or conn.execute("SELECT 1 FROM ad_daily_insights LIMIT 1").fetchone()):
        return False
    monday = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    r = conn.execute("SELECT MAX(created_at) FROM research_briefs").fetchone()
    return not r[0] or r[0] < monday


# ── Meta Page comments (decision j) ───────────────────────────────────────
def import_page_comments(conn, http: Any = None) -> dict[str, Any]:
    """Real comments from the brand's Page posts into the reviews library (kind comment). Needs META_PAGE_ID and a token
    with the read-only Page permissions (pages_show_list, pages_read_engagement)."""
    from creative_intelligence.adstudio import reviews
    from creative_intelligence.adstudio.winners import meta_get, save_raw
    page = os.getenv("META_PAGE_ID", "").strip()
    if not page:
        return {"error": "Set META_PAGE_ID and a Page token with pages_read_engagement first (decision j)."}
    data = meta_get(f"{page}/posts", "comments.limit(50){message,created_time,from}", http)
    save_raw("page_comments", data)
    if "error" in data:
        return {"error": "Meta: " + (data["error"] or {}).get("message", "Page comments not readable with this token.")}
    have = {(r["text"], r["posted_on"]) for r in reviews.listing(conn, None, "comment")}
    added = 0
    for post in data.get("data") or []:
        for c in ((post.get("comments") or {}).get("data") or []):
            text, day = (c.get("message") or "").strip(), (c.get("created_time") or "")[:10]
            if len(text) < 3 or (text, day) in have:
                continue
            rid, _ = reviews.add(conn, {"kind": "comment", "text": text, "author": (c.get("from") or {}).get("name") or "A customer",
                                        "source": "meta_comment", "posted_on": day})
            if rid:
                added += 1
                have.add((text, day))
    return {"added": added}
