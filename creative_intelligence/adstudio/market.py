"""
Phase 2a R1: the market. Which brands we watch, what they run, and for how long.

  Brands      the registry (tiers A direct, B premium fruit and gifting, C same audience, other food), seeded from the
              brief; keywords are the US searches. Each brand and keyword has a one-click link to the US Ad Library.
  Refresh     monthly (and on demand): the official Ad Library API for every active brand and keyword. Meta's API
              only returns ordinary ads delivered in the EU/UK, so US-only ads come in by hand. No scraper: Meta's
              terms forbid automated collection, and the brief says to respect them.
  Add an ad   by Ad Library link, landing page or screenshot: Claude reads the screenshot (or the page) and fills the
              form; the operator checks it and saves.
  Longevity   days running = start to stop (or today). New test < 14 d, Traction 14-30, Proven 30+, Evergreen 90+.
  Concepts    near-identical variants (same brand, same words once case, numbers and punctuation are ignored) share a
              concept; the variant count shows how much a brand invests in it.
  Context     monthly, Tier A and B: Claude web search and fetch summarise each brand's offer, prices, guarantee and
              positioning from its public site.

Claude calls are logged as source "market_research" and checked against the research cap.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import quote_plus

from creative_intelligence import config

from creative_intelligence import brand as _brand

MODEL = config.CI_CLAUDE_CHECK_MODEL
TIERS = dict(_brand.get("market.tiers") or {"A": "Direct competitors", "B": "Premium and gifting",
                                                "C": "Same audience (borrow winning formats)"})
STAGES = [("evergreen", "Evergreen", 90), ("proven", "Proven", 30), ("traction", "Traction", 14), ("new", "New test", 0)]
REFRESH_EVERY = timedelta(days=28)
CONTEXT_EVERY = timedelta(days=90)     # brands change their offer slowly; the first run cost $9 for 11 brands
CONTEXT_SEARCHES = 2
CONTEXT_FETCHES = 2
CONTEXT_PAGE_TOKENS = 6000             # read the start of each page, not all of it
CONTEXT_ESTIMATE_USD = 0.20
READ_ESTIMATE_USD = 0.03
SHOT_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
VIDEO_TYPES = (".mp4", ".mov", ".m4v", ".webm")
DECODE_STAGES = ("proven", "evergreen")


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


# ── brands and keywords ─────────────────────────────────────────────────────
def _seed(conn) -> None:
    """First use: the brand pack's watched brands and search terms (brand.yaml › market). Once there is any brand or
    term, the app's own list is the one in force."""
    if conn.execute("SELECT 1 FROM research_brands LIMIT 1").fetchone() or \
            conn.execute("SELECT 1 FROM research_keywords LIMIT 1").fetchone():
        return
    with conn:
        for b in _brand.get("market.brands") or []:
            conn.execute("INSERT OR IGNORE INTO research_brands (name, tier, website, page_name, why, added_at)"
                         " VALUES (?,?,?,?,?,?)", (b["name"], b.get("tier", "A"), b.get("website", ""),
                                                   b.get("page_name") or b["name"], b.get("why", ""), _now()))
        for term in _brand.get("market.keywords") or []:
            conn.execute("INSERT OR IGNORE INTO research_keywords (term, added_at) VALUES (?,?)", (term, _now()))


def brands(conn, active_only: bool = False) -> list[dict[str, Any]]:
    _seed(conn)
    q = "SELECT * FROM research_brands" + (" WHERE active=1" if active_only else "") + " ORDER BY tier, name"
    out = []
    for r in conn.execute(q):
        b = dict(r)
        b["context"] = json.loads(b.pop("context_json") or "{}")
        b["us_library_url"] = library_url(b["page_name"] or b["name"], b["page_id"])
        n = conn.execute("SELECT COUNT(*), MAX(julianday(COALESCE(NULLIF(ended_on,''), date('now'))) - julianday(started_on))"
                         " FROM competitor_ads WHERE brand_id=?", (b["id"],)).fetchone()
        b["ads"], b["longest_days"] = n[0], int(n[1] or 0)
        out.append(b)
    return out


def save_brand(conn, d: dict[str, Any]) -> tuple[int | None, str]:
    name = (d.get("name") or "").strip()
    tier = (d.get("tier") or "C").strip().upper()
    if not name:
        return None, "Give the brand's name."
    if tier not in TIERS:
        return None, "Tier is A, B or C."
    vals = {"name": name, "tier": tier, "website": (d.get("website") or "").strip(),
            "page_name": (d.get("page_name") or name).strip(), "page_id": re.sub(r"\D", "", str(d.get("page_id") or "")),
            "why": (d.get("why") or "").strip(), "active": 1 if d.get("active", True) else 0}
    with conn:
        if d.get("id"):
            n = conn.execute(f"UPDATE research_brands SET {', '.join(k + '=?' for k in vals)} WHERE id=?",
                             (*vals.values(), int(d["id"]))).rowcount
            return (int(d["id"]), "") if n else (None, "brand not found")
        if conn.execute("SELECT 1 FROM research_brands WHERE name=?", (name,)).fetchone():
            return None, "That brand is already in the list."
        bid = conn.execute(f"INSERT INTO research_brands ({', '.join(vals)}, added_at) VALUES ({','.join('?' * len(vals))},?)",
                           (*vals.values(), _now())).lastrowid
    return bid, ""


def keywords(conn) -> list[dict[str, Any]]:
    _seed(conn)
    return [dict(r, us_library_url=library_url(r["term"])) for r in
            conn.execute("SELECT * FROM research_keywords ORDER BY term")]


def add_keyword(conn, term: str) -> str:
    term = (term or "").strip().lower()
    if len(term) < 3:
        return "Type a search of at least 3 letters."
    with conn:
        conn.execute("INSERT OR IGNORE INTO research_keywords (term, added_at) VALUES (?,?)", (term, _now()))
    return ""


def remove_keyword(conn, kid: int) -> None:
    with conn:
        conn.execute("DELETE FROM research_keywords WHERE id=?", (kid,))


def library_url(term: str, page_id: str = "") -> str:
    """The public US Ad Library, opened in the operator's own browser (we never fetch it)."""
    base = "https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country=US&media_type=all"
    if page_id:
        return f"{base}&view_all_page_id={page_id}"
    return f"{base}&q={quote_plus(term)}&search_type=keyword_unordered"


def brand_for(conn, page_name: str, page_id: Any = None) -> int | None:
    """The registry brand an ad belongs to, by Page id or by name."""
    if page_id:
        r = conn.execute("SELECT id FROM research_brands WHERE page_id=?", (str(page_id),)).fetchone()
        if r:
            return r[0]
    key = (page_name or "").strip().lower()
    if not key:
        return None
    r = conn.execute("SELECT id FROM research_brands WHERE LOWER(page_name)=? OR LOWER(name)=?", (key, key)).fetchone()
    return r[0] if r else None


# ── longevity and concepts ───────────────────────────────────────────────────
def days_running(ad: dict[str, Any], today: date | None = None) -> int | None:
    try:
        start = date.fromisoformat((ad.get("started_on") or "")[:10])
    except ValueError:
        return None
    stop = ad.get("ended_on") or ("" if ad.get("active", 1) else ad.get("last_seen_on") or "")
    try:
        end = date.fromisoformat(stop[:10])                 # ended: its stop date (or when we last saw it)
    except ValueError:
        end = today or date.today()
    return max(0, (end - start).days)


def stage(days: int | None) -> tuple[str, str]:
    if days is None:
        return "", "Start date unknown"
    return next((k, label) for k, label, low in STAGES if days >= low)


def concept_key(page: str, body: str, headline: str) -> str:
    norm = re.sub(r"[^a-z ]+", " ", f"{body} {headline}".lower())
    norm = " ".join(norm.split())
    return hashlib.sha1(f"{(page or '').strip().lower()}|{norm}".encode()).hexdigest()[:12] if norm else ""


def safe_media(path: str) -> str:
    """A screenshot path only if it is one we stored in the research area (never a path from elsewhere)."""
    from pathlib import Path
    from creative_intelligence.storage import storage
    if not path:
        return ""
    try:
        p = Path(path).resolve()
        return str(p) if p.is_file() and p.is_relative_to(storage().area_dir("research").resolve()) else ""
    except (OSError, ValueError):
        return ""


def archive_id(url: str) -> str | None:
    m = re.search(r"facebook\.com/ads/library/?\?(?:.*&)?id=(\d+)", url or "")
    return m.group(1) if m else None


def listing(conn, brand_id: int | None = None, stage_key: str = "", limit: int = 400) -> list[dict[str, Any]]:
    """Competitor ads newest-first in storage, returned longest-running first, with stage, tier and variant count."""
    q, args = ("SELECT a.*, b.name AS brand, b.tier FROM competitor_ads a LEFT JOIN research_brands b ON b.id=a.brand_id",
               [])
    if brand_id:
        q, args = q + " WHERE a.brand_id=?", [brand_id]
    rows = [dict(r) for r in conn.execute(q + " ORDER BY a.id DESC LIMIT ?", (*args, limit))]
    variants: dict[str, int] = {}
    for r in conn.execute("SELECT concept_key, COUNT(*) FROM competitor_ads WHERE concept_key!='' GROUP BY concept_key"):
        variants[r[0]] = r[1]
    for r in rows:
        r["days"] = days_running(r)
        r["stage"], r["stage_name"] = stage(r["days"])
        r["variants"] = variants.get(r["concept_key"], 1) if r["concept_key"] else 1
        r["brand"] = r["brand"] or r["page_name"]
        r["has_media"] = bool(r["media_path"])
        r["has_video"] = bool(r.get("video_path"))
        t = conn.execute("SELECT status FROM video_templates WHERE id=?", (r["template_id"],)).fetchone() \
            if r.get("template_id") else None
        r["template_status"] = t[0] if t else ""
    if stage_key:
        keep = {"proven": {"proven", "evergreen"}}.get(stage_key, {stage_key})
        rows = [r for r in rows if r["stage"] in keep]
    rows.sort(key=lambda r: -(r["days"] or -1))
    return rows


def concepts(conn) -> list[dict[str, Any]]:
    """One row per concept: its brand, variants, longest run and whether any variant is still active."""
    out: dict[str, dict[str, Any]] = {}
    for r in listing(conn, limit=5000):
        k = r["concept_key"] or f"ad{r['id']}"
        c = out.setdefault(k, {"key": k, "brand": r["brand"], "tier": r["tier"], "sample": r, "variants": 0,
                               "days": r["days"], "active": False})
        c["variants"] += 1
        c["days"] = max(c["days"] or 0, r["days"] or 0)
        c["active"] = c["active"] or bool(r["active"])
    for c in out.values():
        c["stage"], c["stage_name"] = stage(c["days"])
    return sorted(out.values(), key=lambda c: -(c["days"] or 0))


# ── the monthly refresh ─────────────────────────────────────────────────────
def last_scan(conn) -> dict[str, Any] | None:
    r = conn.execute("SELECT * FROM research_scans ORDER BY id DESC LIMIT 1").fetchone()
    return dict(r, result=json.loads(r["result_json"] or "{}")) if r else None


def scan_due(conn, now: datetime | None = None) -> bool:
    """Monthly; after a failed try (no Ad Library access yet), at most one more try a day."""
    now = now or datetime.utcnow()
    done = conn.execute("SELECT MAX(started_at) FROM research_scans WHERE status='done'").fetchone()[0]
    tried = conn.execute("SELECT MAX(started_at) FROM research_scans").fetchone()[0]
    return (not done or datetime.fromisoformat(done) <= now - REFRESH_EVERY) and \
        (not tried or datetime.fromisoformat(tried) <= now - timedelta(days=1))


def scan(conn, trigger: str = "manual", http: Any = None) -> dict[str, Any]:
    """Every active brand (by Page id when known, else by name) and keyword through the Ad Library API."""
    from creative_intelligence.adstudio import research
    with conn:
        sid = conn.execute("INSERT INTO research_scans (started_at, trigger) VALUES (?,?)", (_now(), trigger)).lastrowid
    out: dict[str, Any] = {"added": 0, "updated": 0, "searches": 0, "errors": []}
    targets = [(b["name"], [b["page_id"]] if b["page_id"] else None, b["page_name"] or b["name"])
               for b in brands(conn, active_only=True)]
    targets += [(k["term"], None, k["term"]) for k in keywords(conn) if k["active"]]
    for label, pages, terms in targets:
        got = research.fetch_library(conn, terms, http=http, page_ids=pages)
        out["searches"] += 1
        if got.get("error"):
            out["errors"].append(f"{label}: {got['error']}")
            if "Ad Library API access" in got["error"]:      # the token can't search at all: stop, say so once
                break
            continue
        out["added"] += got["added"]
        out["updated"] += got.get("updated", 0)
    status = "failed" if out["errors"] and not (out["added"] or out["updated"]) and out["searches"] <= 1 else "done"
    with conn:
        conn.execute("UPDATE research_scans SET finished_at=?, status=?, result_json=?, error=? WHERE id=?",
                     (_now(), status, json.dumps(out), "; ".join(out["errors"])[:500], sid))
    return {"id": sid, "status": status, **out}


# ── reading an ad the operator saw ──────────────────────────────────────────
READ_SCHEMA = {"type": "object", "additionalProperties": False,
               "required": ["page_name", "body", "headline", "cta", "started_on", "active", "variants", "media_kind"],
               "properties": {
                   "page_name": {"type": "string", "description": "the advertiser's Page name as shown"},
                   "body": {"type": "string", "description": "the primary text, exactly as shown"},
                   "headline": {"type": "string"},
                   "cta": {"type": "string", "description": "the button, e.g. Shop now; '' if none"},
                   "started_on": {"type": "string", "description": "YYYY-MM-DD from 'Started running on …', or ''"},
                   "active": {"type": "boolean"},
                   "variants": {"type": "integer", "description": "from 'N ads use this creative and text', else 1"},
                   "media_kind": {"type": "string", "enum": ["image", "video", "carousel", "unknown"]}}}


def _format(conn, content: list[dict[str, Any]], schema: dict[str, Any], system: str, client: Any,
            source_id: int = 0) -> dict[str, Any]:
    """One structured-output call (no tools) that fills a schema from what it is shown."""
    from creative_intelligence.adstudio import costs
    resp = client.beta.messages.create(
        model=MODEL, max_tokens=3000, betas=["server-side-fallback-2026-07-01"], fallbacks="default", system=system,
        output_config={"format": {"type": "json_schema", "schema": schema}}, messages=[{"role": "user", "content": content}])
    costs.log_tokens(conn, MODEL, "copy", resp.usage, "market_research", source_id)
    if resp.stop_reason == "refusal":
        raise RuntimeError("Claude declined this one.")
    return json.loads(next(b.text for b in resp.content if b.type == "text"))


def _with_tools(conn, user: str, system: str, tools: list[dict[str, Any]], client: Any, source_id: int = 0) -> str:
    """A web search / fetch call, following pause_turn; returns Claude's notes. Searches are logged at their price."""
    from creative_intelligence.adstudio import costs
    from creative_intelligence.adstudio.hub import _log_searches
    messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
    resp, searches = None, 0
    for _ in range(4):
        resp = client.beta.messages.create(model=MODEL, max_tokens=4000, betas=["server-side-fallback-2026-07-01"],
                                           fallbacks="default", system=system, messages=messages, tools=tools)
        costs.log_tokens(conn, MODEL, "copy", resp.usage, "market_research", source_id)
        searches += getattr(getattr(resp.usage, "server_tool_use", None), "web_search_requests", 0) or 0
        if resp.stop_reason != "pause_turn":
            break
        messages = [messages[0], {"role": "assistant", "content": resp.content}]
    if searches:
        _log_searches(conn, searches, source_id, source="market_research")
    if resp.stop_reason == "refusal":
        raise RuntimeError("Claude declined this one.")
    notes = "\n".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    if not notes:
        raise RuntimeError("Nothing came back from the web.")
    return notes


READ_SYSTEM = ("You read another brand's Meta ad so it can be filed for research. Copy the words exactly as shown; never "
               "invent or improve them. Leave a field empty when it isn't visible.")


def _read(conn, content: list[dict[str, Any]], client: Any) -> dict[str, Any]:
    from creative_intelligence import budget
    from creative_intelligence.adstudio.chat import _client
    budget.check_research(conn, READ_ESTIMATE_USD)
    got = _format(conn, content, READ_SCHEMA, READ_SYSTEM, client or _client(log=False))
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", got.get("started_on") or ""):
        got["started_on"] = ""
    return got


def read_screenshot(conn, data: bytes, mime: str, client: Any = None) -> dict[str, Any]:
    """Claude reads an Ad Library (or feed) screenshot into the Add ad form; the screenshot is kept on the volume."""
    from creative_intelligence.storage import storage
    if mime not in SHOT_TYPES:
        raise ValueError("Upload a PNG, JPEG or WebP screenshot.")
    if len(data) > 8_000_000:
        raise ValueError("That screenshot is over 8 MB.")
    got = _read(conn, [{"type": "image", "source": {"type": "base64", "media_type": mime,
                                                    "data": base64.standard_b64encode(data).decode()}},
                       {"type": "text", "text": "Read this ad."}], client)
    name = f"ad_{datetime.utcnow():%Y%m%d_%H%M%S}_{hashlib.sha1(data).hexdigest()[:8]}.{SHOT_TYPES[mime]}"
    got["media_path"] = storage().put_bytes("research", name, data)
    got["media_kind"] = got.get("media_kind") if got.get("media_kind") != "unknown" else "image"
    return got


def store_screenshot(data: bytes, mime: str) -> str:
    """Keep a screenshot in the research area (for reading later in the worker). Raises ValueError if unusable."""
    from creative_intelligence.storage import storage
    if mime not in SHOT_TYPES:
        raise ValueError("Upload a PNG, JPEG or WebP screenshot.")
    if len(data) > 8_000_000:
        raise ValueError("That screenshot is over 8 MB.")
    name = f"ad_{datetime.utcnow():%Y%m%d_%H%M%S}_{hashlib.sha1(data).hexdigest()[:8]}.{SHOT_TYPES[mime]}"
    return storage().put_bytes("research", name, data)


def read_screens(db: Any, paths: list[str], client: Any = None) -> dict[str, Any]:
    """The worker side of 'drop several screenshots': each is read by Claude and saved as a competitor ad with its
    picture, then the new ads are tagged (format, angle, why it works). Stops cleanly at the research cap."""
    from pathlib import Path
    from creative_intelligence import budget
    from creative_intelligence.adstudio import research
    conn = db()
    out: dict[str, Any] = {"added": 0, "skipped": [], "stopped": ""}
    try:
        for p in paths:
            path = safe_media(p)
            if not path:
                continue
            mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}.get(
                Path(path).suffix.lstrip(".").lower(), "image/png")
            try:
                got = _read(conn, [{"type": "image", "source": {"type": "base64", "media_type": mime,
                                                                "data": base64.standard_b64encode(Path(path).read_bytes()).decode()}},
                                   {"type": "text", "text": "Read this ad."}], client)
            except budget.BudgetExceeded as exc:
                out["stopped"] = str(exc)
                break
            got.update(media_path=path, media_kind="image" if got.get("media_kind") in (None, "", "unknown") else got["media_kind"])
            got["page_name"] = got.get("page_name") or "Unknown brand"
            if not (got.get("body") or got.get("headline")):
                got["headline"] = "(no words on the ad)"
            rid, why = research.add_manual(conn, got)
            out["added" if rid else "skipped"] = out["added"] + 1 if rid else out["skipped"] + [why]
        try:
            research.tag(conn, client=client)
        except budget.BudgetExceeded as exc:
            out["stopped"] = out["stopped"] or str(exc)
        return out
    finally:
        conn.close()


def read_landing(conn, url: str, client: Any = None) -> dict[str, Any]:
    """Claude fetches a brand's landing page (web fetch, public page) and fills what an ad would say from it."""
    from creative_intelligence import budget
    from creative_intelligence.adstudio.chat import _client
    if not re.match(r"https?://[^\s/]+\.[^\s]+", url or ""):
        raise ValueError("Paste the full landing page link (https://…).")
    budget.check_research(conn, READ_ESTIMATE_USD * 2)
    client = client or _client(log=False)
    notes = _with_tools(conn, f"Fetch {url} and write down, exactly as the page shows them: the brand, the main headline, "
                              "the main offer line, the button text and any price.",
                        "You read a public landing page for ad research. Quote, don't rewrite.",
                        [{"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 1}], client)
    got = _read(conn, [{"type": "text", "text": f"NOTES FROM THE LANDING PAGE {url}:\n{notes}\n\nFill the form: the brand "
                                                "as page_name, the headline, the offer line as body, the button as cta; "
                                                "started_on is ''."}], client)
    got["landing_url"] = url
    return got


# ── competitor videos → competitor-derived structure templates (R2) ────────────
def attach_video(conn, ad_id: int, raw: bytes, filename: str) -> dict[str, Any]:
    """Keep a competitor ad's video (saved by the operator) and, when the ad is Proven or Evergreen, decode it into a
    structure template labelled competitor-derived. Returns {template_id?} or {error}."""
    from pathlib import Path
    from creative_intelligence.adstudio import videotools
    from creative_intelligence.storage import storage
    ad = next((a for a in listing(conn, limit=100000) if a["id"] == ad_id), None)
    if ad is None:
        return {"error": "ad not found"}
    ext = (Path(filename).suffix or ".mp4").lower()
    if ext not in VIDEO_TYPES:
        return {"error": "Upload an mp4, mov or webm video."}
    path = storage().put_bytes("research", f"video_{ad_id}_{int(datetime.utcnow().timestamp())}{ext}", raw)
    try:
        videotools.probe(path)
    except videotools.VideoToolError:
        return {"error": "That file isn't a video ffmpeg can read."}
    with conn:
        conn.execute("UPDATE competitor_ads SET video_path=?, media_kind='video' WHERE id=?", (path, ad_id))
    if ad["stage"] not in DECODE_STAGES:
        return {"kept": True, "decoded": False,
                "why": f"Kept. Only Proven or Evergreen ads (30+ days) are decoded; this one is {ad['stage_name']}."}
    return queue_decode(conn, ad_id)


def queue_decode(conn, ad_id: int) -> dict[str, Any]:
    """One competitor-derived template and decode job (V4 decoder: cuts, transcript, shot structure). Research cap."""
    from creative_intelligence import budget, jobs
    from creative_intelligence.adstudio import winners
    a = conn.execute("SELECT a.*, b.name AS brand FROM competitor_ads a LEFT JOIN research_brands b ON b.id=a.brand_id"
                     " WHERE a.id=?", (ad_id,)).fetchone()
    if a is None or not a["video_path"]:
        return {"error": "Upload the ad's video first."}
    if a["template_id"] and conn.execute("SELECT 1 FROM video_templates WHERE id=? AND status IN ('queued','running','done')",
                                         (a["template_id"],)).fetchone():
        return {"template_id": a["template_id"]}
    budget.check_research(conn, winners.estimate(conn))
    brand = a["brand"] or a["page_name"]
    meta = {"competitor": True, "brand": brand, "copy": {"body": a["body"], "title": a["headline"]}}
    with conn:
        tid = conn.execute("INSERT INTO video_templates (ad_id, ad_name, metrics_json, upload_path, origin, competitor_ad_id,"
                           " created_at) VALUES (?,?,?,?, 'competitor', ?, ?)",
                           (f"competitor:{ad_id}", f"{brand} (competitor-derived)", json.dumps(meta), a["video_path"],
                            ad_id, _now())).lastrowid
        conn.execute("UPDATE competitor_ads SET template_id=? WHERE id=?", (tid, ad_id))
    jobs.enqueue(conn, "decode", {"template_id": tid}, dedupe_key=f"decode:{tid}")
    return {"template_id": tid}


# ── brand context (monthly, Tier A and B) ────────────────────────────────────
CONTEXT_SCHEMA = {"type": "object", "additionalProperties": False,
                  "required": ["offer", "prices", "guarantee", "shipping", "positioning", "sources"],
                  "properties": {k: {"type": "string"} for k in ("offer", "prices", "guarantee", "shipping", "positioning")}
                  | {"sources": {"type": "array", "items": {"type": "string"}}}}


def context_due(conn, now: datetime | None = None) -> list[dict[str, Any]]:
    cut = ((now or datetime.utcnow()) - CONTEXT_EVERY).isoformat()
    return [b for b in brands(conn, active_only=True) if b["tier"] in ("A", "B") and b["website"]
            and (not b["context_at"] or b["context_at"] <= cut)]


def brand_context(conn, brand: dict[str, Any], client: Any = None) -> dict[str, Any]:
    """Offer, prices, guarantee, shipping and positioning from the brand's own public site (web search and fetch)."""
    from creative_intelligence import budget
    from creative_intelligence.adstudio.chat import _client
    budget.check_research(conn, CONTEXT_ESTIMATE_USD)
    client = client or _client(log=False)
    site = brand["website"]
    notes = _with_tools(
        conn, f"Brand: {brand['name']} ({site}). From its own public website only, find its current offer (what it "
              "sells, boxes, clubs), prices, guarantee, shipping promise and how it positions itself. Quote prices as "
              "shown, list the page URLs you used, and say 'not stated' for anything you can't find.",
        "You summarise a competitor's public positioning for the brand's ad research. Facts only, from the site.",
        [{"type": "web_search_20260209", "name": "web_search", "max_uses": CONTEXT_SEARCHES, "allowed_domains": [site]},
         {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": CONTEXT_FETCHES, "allowed_domains": [site],
          "max_content_tokens": CONTEXT_PAGE_TOKENS}],
        client, brand["id"])
    ctx = _format(conn, [{"type": "text", "text": f"NOTES ON {brand['name']}:\n{notes}"}], CONTEXT_SCHEMA,
                  "Put the notes into the fields, short and factual; 'not stated' where the notes say so.", client, brand["id"])
    with conn:
        conn.execute("UPDATE research_brands SET context_json=?, context_at=? WHERE id=?",
                     (json.dumps(ctx), _now(), brand["id"]))
    return ctx


def refresh(db: Any, trigger: str = "scheduled", http: Any = None, client: Any = None) -> dict[str, Any]:
    """The monthly research job: the Ad Library refresh, tagging what is new, and the brand context that is due.
    Stops cleanly at the research cap."""
    from creative_intelligence import budget
    from creative_intelligence.adstudio import research
    conn = db()
    try:
        out: dict[str, Any] = {"scan": scan(conn, trigger, http=http), "context": [], "stopped": ""}
        try:
            while research.tag(conn, client=client).get("tagged"):
                pass
            for b in context_due(conn):
                brand_context(conn, b, client=client)
                out["context"].append(b["name"])
        except budget.BudgetExceeded as exc:
            out["stopped"] = str(exc)
        return out
    finally:
        conn.close()
