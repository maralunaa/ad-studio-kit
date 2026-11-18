"""
Bulk static ads (Compose → Bulk): many approved images × ad types × hooks × sizes in one click.

  options()   what the Bulk form offers for a product: approved images (and Core product shots),
              every code-drawn ad type (with why one can't apply), hooks (best first), sizes
  plan()      who gets what: each image gets `per_image` (type, hook) pairs, walked so neighbouring
              ads differ in both type and hook and the batch spreads over all of them; every pair
              is made in every chosen size
  estimate()  how many ads, what they cost: code-drawn ads are free (one claims check per hook);
              optional AI scenes are paid per image
  create()    stores the batch and queues the "compose_batch" job; process() makes the ads in the
              worker and keeps the batch's progress

Code-drawn ads only use the approved photo, the hook's copy and the Shopify price; nothing is
invented. AI scenes go through the AI Ads path (text-free image, copy drawn by code).
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from creative_intelligence import knowledge

SIZES = ("4:5", "9:16", "1:1")
MAX_ADS = 600
MAX_AI = 48
BEST_HOOKS = 5
OVERLAY = "overlay"
OVERLAY_LAYOUT = {"position": "auto", "color": "auto", "scrim": True}
# Feedback 2026-10-23 ("much better before: not so much text on the image, visually appealing, varied"): most of a
# batch is AI-designed with the full rotating prompts; code draws only light, photo-first layouts (no text, a label,
# one line, or one headline and a button). Text-box layouts stay in One ad.
LIGHT_CODE = {"minimal", "premium", "lifestyle_tagline", "craving_macro", OVERLAY}
AI_SHARE = 0.8
AI_PARALLEL = int(os.getenv("BULK_AI_PARALLEL", "4"))    # AI ads made at once (Replicate's throttle retry covers bursts)
PANEL_FALLBACK = "premium"
MAX_OVER_SUBJECT = 0.25        # an ad whose words still cover more of the fruit than this isn't delivered
from creative_intelligence import brand as _brand
OFF_BY_DEFAULT = {"origin_story"} if _brand.get("rules.no_origin_claims") else set()   # brand rule: no origin claims


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


MIN_LINES = 3


def material(conn, product: dict[str, Any]) -> dict[str, list]:
    """What the list and review layouts draw in bulk (feedback 2026-10-22: they were greyed out): short approved
    facts as the lines (exact words, already approved), and real 4–5 star reviews and comments from the library."""
    from creative_intelligence.adstudio import facts, reviews
    pid = product.get("id")
    lines = [t for t in (shown_fact(f["text"]) for f in facts.list_facts(conn, pid, facts._family(product))
                         if f.get("category") != "how_to_eat") if t and len(t) <= 90]
    words = topic_words(conn, product)
    short = lambda kind: [r["id"] for r in sorted((r for r in reviews.listing(conn, pid, kind) if len(r["text"]) <= 220),
                                                  key=lambda r: (-relevance(r["text"], words), -(r.get("rating") or 0),
                                                                 len(r["text"])))]
    return {"lines": lines, "review": short("review"), "comment": short("comment")}


def shown_fact(text: str) -> str:
    """A fact as it may appear on an ad: its source note in brackets ("(Fera, incl. Google, as of …)") is for us."""
    import re
    return re.sub(r"\s*\([^)]*\)", "", text or "").strip()


GENERIC = {"fruit", "fruits", "fresh", "box", "boxes", "variety", "gift", "basket", "exotic", "the", "and", "with"}
TASTE_WORDS = ("fruit", "sweet", "ripe", "flavor", "flavour", "taste", "juicy", "delicious", "fresh", "mango", "box")


def topic_words(conn, product: dict[str, Any]) -> set[str]:
    """Words a review about this product would use: its name, and for a box the fruits its facts name."""
    import re
    from creative_intelligence.adstudio import facts
    text = (product.get("name") or "") + " " + " ".join(f["text"] for f in facts.list_facts(
        conn, product.get("id"), facts._family(product)) if product.get("product_type") == "gift_box")
    return {w for w in re.findall(r"[a-z]{4,}", text.lower()) if w not in GENERIC}


def relevance(text: str, words: set[str]) -> int:
    """Reviews about the fruit beat praise for the service (A2: 'The communication was amazing' on a mango ad)."""
    t = text.lower()
    return 3 * sum(1 for w in words if w in t) + sum(1 for w in TASTE_WORDS if w in t)


def _needs(k: str, fields: list[str], mat: dict[str, list] | None) -> str:
    """Why a list or review layout can't be drawn in bulk for this product, or ''."""
    from creative_intelligence.adstudio.templates import template_for
    if mat is None:
        return "Needs its own lines or real reviews: use One ad" if {"items", "reviews", "review"} & set(fields) else ""
    if "items" in fields and len(mat["lines"]) < MIN_LINES:
        return f"Needs {MIN_LINES} short approved facts (Product Hub › Facts)"
    if {"reviews", "review"} & set(fields):
        kind = next((e.get("kind") for e in template_for(k)["elements"] if e.get("type") == "cards"), None) or "review"
        if not mat[kind]:
            return ("Needs real comments: import Page comments" if kind == "comment"
                    else "Needs 4–5 star reviews in the library (Ideas › Customer voice)")
    return ""


def fill_lists(layout: dict[str, Any], mat: dict[str, list], i: int) -> None:
    """Rotate the facts and reviews so neighbouring ads show different ones (ad number i)."""
    from creative_intelligence.adstudio.templates import template_for
    els = template_for(layout["template"])["elements"]
    for e in els:
        if e.get("field") == "items":
            n = min(e.get("max_items") or 4, 4, len(mat["lines"]))
            layout["items"] = [mat["lines"][(i * n + j) % len(mat["lines"])] for j in range(n)]
        elif e.get("field") in ("reviews", "review"):
            ids = mat[e.get("kind") or "review"]
            n = min(e.get("max_items") or 1, len(ids)) if e.get("field") == "reviews" else 1
            layout["review_ids"] = [ids[(i * n + j) % len(ids)] for j in range(n)]


def types_for(product: dict[str, Any], mat: dict[str, list] | None = None) -> list[dict[str, Any]]:
    """Every code-drawn type: key, name, copy fields, on by default, and why it can't apply (or '')."""
    from creative_intelligence.adstudio.templates import TEMPLATES, copy_fields
    from creative_intelligence.adstudio.textads import VARIANTS
    from creative_intelligence.adstudio.profiles import normalize_type
    fruit = normalize_type(product.get("product_type")) == "fruit"
    out = []
    for k, t in TEMPLATES.items():
        if k == "scene":
            continue
        v = VARIANTS.get(k, {})
        fields = copy_fields(k)
        heavy = k not in VARIANTS and k not in LIGHT_CODE
        why = ("Text layout: make it in One ad" if heavy else
               "Fruit only" if v.get("fruit_only") and not fruit else
               "Needs 4–5 star reviews in the library" if k == "social_proof" and (mat is None or not mat["review"]) else "")
        out.append({"key": k, "name": t["name"], "fields": fields, "price_slot": bool(t.get("price_slot")),
                    "why": why, "on": not why and k not in OFF_BY_DEFAULT,
                    "made": "AI-designed and code" if k in LIGHT_CODE and k in VARIANTS else
                            "AI-designed" if k in VARIANTS else "code"})
    out.append({"key": OVERLAY, "name": "Simple overlay", "fields": ["headline", "sub", "cta"], "price_slot": False,
                "why": "", "on": True})
    return out


def _hooks(conn, pid: int, usable: bool = True) -> list[dict[str, Any]]:
    """A product's hooks, Hook University first. usable=True leaves out hooks whose claims check failed (A2,
    2026-10-23: "3 Weeks Left" and "6–10 fruits" were flagged but still made into ads); each hook carries its check."""
    from creative_intelligence.adstudio import facts
    rows = [dict(r) for r in conn.execute("SELECT * FROM adstudio_hooks WHERE product_id=? ORDER BY id DESC", (pid,))]
    rows.sort(key=lambda h: h.get("source") != "hook_university")          # Hook University first, newest first
    checks = facts.checks_for(conn, "hook", [h["id"] for h in rows])
    for h in rows:
        h["headline"] = h.get("headline") or h.get("hook_text") or ""
        h["cta"] = knowledge.cta_label(h.get("cta"))
        h["claims"] = facts.with_season_check(checks.get(h["id"]), [h["headline"], h.get("subhead") or "", h.get("body") or ""])
        h["flagged"] = (h["claims"] or {}).get("status") == "flagged"
    return [h for h in rows if h["headline"] and not (usable and h["flagged"])]


def options(conn, pid: int) -> dict[str, Any]:
    from creative_intelligence.webapp.common import _product_dict
    prod = _product_dict(conn, pid) or {}
    imgs = [dict(r) for r in conn.execute(
        "SELECT id, title, aspect_ratio FROM adstudio_images WHERE product_id=? AND review_status='approved'"
        " AND image_path IS NOT NULL AND core_id IS NULL AND parent_image_id IS NULL AND COALESCE(archived,0)=0"
        " ORDER BY id DESC", (pid,))]
    cores = [dict(r) for r in conn.execute(
        "SELECT id, kind, aspect_ratio, is_favorite FROM adstudio_core WHERE product_id=? AND image_path IS NOT NULL"
        " AND kind NOT LIKE 'info%' ORDER BY is_favorite DESC, id DESC", (pid,))]
    from creative_intelligence.adstudio import facts
    hooks = _hooks(conn, pid, usable=False)
    good = [h["id"] for h in hooks if not h["flagged"]]
    return {"images": imgs, "cores": cores, "types": types_for(prod, material(conn, prod)),
            "hooks": [{"id": h["id"], "headline": h["headline"], "subhead": h.get("subhead") or "",
                       "cta": h.get("cta") or "", "source": h.get("source") or "",
                       "best": h["id"] in good[:BEST_HOOKS], "flagged": h["flagged"],
                       "why": facts.summary_line(h["claims"]) if h["flagged"] else ""}
                      for h in hooks],
            "sizes": list(SIZES), "ai_usd": ai_price(conn), "has_price": bool(prod.get("shopify_url"))}


def ai_price(conn) -> float:
    from creative_intelligence.adstudio.costs import price
    from creative_intelligence.providers import model
    return round(price(conn, model("image")), 2)


def pairs(types: list[dict[str, Any]], hooks: list[int]) -> list[tuple[str, int | None]]:
    """Every (type, hook) pair, ordered by diagonals so neighbours differ in type and hook. A type with no
    copy (Minimal, Craving Macro) appears once, with no hook."""
    hs = hooks or [None]
    out, seen = [], set()
    for d in range(len(hs)):
        for i, t in enumerate(types):
            h = None if "headline" not in t["fields"] else hs[(i + d) % len(hs)]
            if (t["key"], h) not in seen:
                seen.add((t["key"], h))
                out.append((t["key"], h))
    return out


PALETTE_CYCLE = ("", "dark", "navy", "light")


def _family(key: str) -> str:
    """photo-led copy, a panel or cards of its own, or lo-fi paper: neighbours should come from different ones."""
    from creative_intelligence.adstudio.render_template import photo_led
    from creative_intelligence.adstudio.templates import TEMPLATES
    if key == OVERLAY:
        return "photo"
    return "lofi" if TEMPLATES.get(key, {}).get("lofi") else "photo" if photo_led(key) else "panel"


def interleave(types: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Types in turn from each family (photo-led, panel, lo-fi), so a batch never runs through one look first."""
    groups: dict[str, list] = {}
    for t in types:
        groups.setdefault(_family(t["key"]), []).append(t)
    out, lists = [], list(groups.values())
    while any(lists):
        for g in lists:
            if g:
                out.append(g.pop(0))
    return out


def plan(images: list[str], types: list[dict[str, Any]], hooks: list[int], per_image: int,
         sizes: list[str]) -> list[dict[str, Any]]:
    """images are keys 'img:<id>' or 'core:<id>'. Each image takes the next `per_image` pairs (wrapping), so
    a 5-image batch walks through every type and hook before repeating; a single image can ask for all.
    Types alternate by family and every ad gets the next colour theme (feedback 2026-10-22: too samey)."""
    ps = pairs(interleave(types), hooks)
    if not ps:
        return []
    per_image = max(1, min(per_image, len(ps)))
    out = []
    for n, key in enumerate(images):
        for j in range(per_image):
            t, h = ps[(n * per_image + j) % len(ps)]
            palette = PALETTE_CYCLE[(n * per_image + j) % len(PALETTE_CYCLE)]
            for s in sizes:
                out.append({"image": key, "type": t, "hook_id": h, "size": s, "palette": palette})
    return out


def spread(items: list[dict[str, Any]], k: int) -> list[dict[str, Any]]:
    """k of the items, spread evenly over the list (so over the images), in order."""
    n = len(items)
    return [items[i * n // k] for i in range(k)] if 0 < k < n else items if k >= n else []


def ai_plan(types: list[dict[str, Any]], hooks: list[int], n: int, sizes: list[str]) -> list[dict[str, Any]]:
    """n AI-designed ads, rotating through the chosen types and hooks, sizes in turn. Types that show a review are
    left out: the model would have to paint a quote, and quotes only ever come from the reviews library."""
    from creative_intelligence.adstudio.textads import VARIANTS
    ps = [p for p in pairs([t for t in types if t["key"] in VARIANTS and t["key"] != "origin_story"
                            and not {"items", "reviews"} & set(t["fields"])], hooks)]
    return [{"type": ps[i % len(ps)][0], "hook_id": ps[i % len(ps)][1], "size": sizes[i % len(sizes)]}
            for i in range(min(n, MAX_AI))] if ps and sizes else []


def estimate(conn, items: list[dict[str, Any]], ai: list[dict[str, Any]]) -> dict[str, Any]:
    ai_usd = round(len(ai) * ai_price(conn), 2)
    looks = len({(i["type"], i.get("palette", "")) for i in items})
    return {"ads": len(items), "images": len({i["image"] for i in items}), "ai": len(ai), "ai_usd": ai_usd,
            "looks": looks, "variety_warning": "These ads will look alike: pick more ad types or more images."
            if items and (len({i["type"] for i in items}) < 4 or len({i["image"] for i in items}) < 3) else "",
            "claims_checks": len({i["hook_id"] for i in items + ai if i["hook_id"]}), "total_usd": ai_usd}


def _parse(conn, pid: int, body: dict[str, Any]) -> tuple[list, list, str]:
    """(items, ai_items, error) from the form."""
    from creative_intelligence.webapp.common import _product_dict
    prod = _product_dict(conn, pid)
    if not prod:
        return [], [], "product not found"
    opts = options(conn, pid)
    ok_imgs = {f"img:{i['id']}" for i in opts["images"]} | {f"core:{c['id']}" for c in opts["cores"]}
    images = [k for k in body.get("images") or [] if k in ok_imgs]
    by_key = {t["key"]: t for t in opts["types"] if not t["why"]}
    types = [by_key[k] for k in body.get("types") or [] if k in by_key]
    ok_hooks = {h["id"] for h in opts["hooks"] if not h["flagged"]}
    hooks = [int(h) for h in body.get("hook_ids") or [] if int(h) in ok_hooks]
    sizes = [s for s in SIZES if s in (body.get("sizes") or [])]
    if not types:
        return [], [], "Pick at least one ad type."
    if not sizes:
        return [], [], "Pick at least one size."
    if any("headline" in t["fields"] for t in types) and not hooks:
        if not ok_hooks:
            return [], [], ("Every hook for this product was flagged by the claims check, so none can be used: "
                            "click Write new hooks.")
        return [], [], "Pick at least one hook that isn't flagged (or only types with no copy)."
    items = plan(images, [t for t in types if t["key"] in LIGHT_CODE], hooks, int(body.get("per_image") or 1), sizes)
    # The share is of the total (feedback 2026-10-30: "80% would be 80% AI and the rest of the total use images"):
    # the total is images × ads per image × sizes; AI-designed ads take their share and code draws the rest.
    mode, total = body.get("ai"), len(items)
    n_ai = (total if mode in ("all", None) else round(total * AI_SHARE) if mode == "mostly"
            else round(total / 2) if mode == "half" else int(mode or 0))
    n_ai = min(MAX_AI, n_ai)
    items = spread(items, max(0, total - n_ai))
    ai = ai_plan(types, hooks, n_ai, sizes)
    if not items and not ai:
        return [], [], "Pick at least one image."
    if len(items) > MAX_ADS:
        return [], [], f"{len(items)} ads is too many at once (max {MAX_ADS}). Fewer images, ads per image or sizes."
    return items, ai, ""


def preview(conn, pid: int, body: dict[str, Any]) -> dict[str, Any]:
    items, ai, err = _parse(conn, pid, body)
    if err:
        return {"error": err}
    return {"estimate": estimate(conn, items, ai)}


def create(conn, pid: int, body: dict[str, Any]) -> tuple[int | None, str]:
    from creative_intelligence import budget, jobs
    if conn.execute("SELECT 1 FROM compose_batches WHERE product_id=? AND status IN ('queued','running','cancelling')",
                    (pid,)).fetchone():
        return None, "A batch is already being made for this product. Wait for it, or cancel it first."
    items, ai, err = _parse(conn, pid, body)
    if err:
        return None, err
    est = estimate(conn, items, ai)
    if ai:
        try:
            budget.check(conn, "generation", est["ai_usd"])
        except budget.BudgetExceeded as exc:
            return None, str(exc)
    layout = {"show_price": bool(body.get("show_price")), "price_variant": body.get("price_variant") or "",
              "direction": str(body.get("direction") or "").strip()[:600]}       # Ad Pack: into every brief
    with conn:
        bid = conn.execute("INSERT INTO compose_batches (product_id, plan_json, total, status, created_at) VALUES"
                           " (?,?,?, 'queued', ?)", (pid, json.dumps({"items": items, "ai": ai, "layout": layout}),
                                                     len(items) + len(ai), _now())).lastrowid
    jobs.enqueue(conn, "compose_batch", {"batch_id": bid}, dedupe_key=f"batch:{bid}")
    return bid, ""


def _base(conn, key: str, size: str, cache: dict) -> tuple[int, str]:
    """(image id, path) of the base at this size: the photo itself when it already has the size, else a
    child image on a canvas of that size (the photo is never cropped), made once per batch."""
    from creative_intelligence.adstudio.sizes import fit_canvas
    from creative_intelligence.adstudio.store import composites_dir
    from creative_intelligence.webapp.routes.compose import _core_as_image
    if (key, size) in cache:
        return cache[(key, size)]
    kind, _, n = key.partition(":")
    iid = _core_as_image(conn, int(n)) if kind == "core" else int(n)
    img = dict(conn.execute("SELECT * FROM adstudio_images WHERE id=?", (iid,)).fetchone())
    if img["aspect_ratio"] == size:
        cache[(key, size)] = (iid, img["image_path"])
        return cache[(key, size)]
    sized = fit_canvas(img["image_path"], size, str(composites_dir() / f"img{iid}_{size.replace(':', 'x')}.png"))
    hit = conn.execute("SELECT id FROM adstudio_images WHERE parent_image_id=? AND aspect_ratio=? AND image_path=?",
                       (iid, size, sized)).fetchone()
    if hit:
        sid = hit[0]
    else:
        with conn:
            sid = conn.execute(
                "INSERT INTO adstudio_images (product_id, archetype, title, concept, prompt, aspect_ratio, model, status,"
                " image_path, parent_image_id, prompt_version) VALUES (?,?,?,?,'', ?, ?, 'done', ?, ?, ?)",
                (img["product_id"], img["archetype"], f"{img['title']} · {size}", img["concept"], size, img["model"],
                 sized, iid, img["prompt_version"])).lastrowid
    cache[(key, size)] = (sid, sized)
    return cache[(key, size)]


def _bump(conn, bid: int, ok: bool) -> None:
    with conn:
        conn.execute(f"UPDATE compose_batches SET {'done' if ok else 'failed'}={'done' if ok else 'failed'}+1 WHERE id=?",
                     (bid,))


def process(batch_id: int, db: Callable[[], Any]) -> dict[str, Any]:
    """The compose_batch job: code-drawn ads first (free, fast), then any AI scenes one at a time."""
    from creative_intelligence.adstudio import facts, qa
    from creative_intelligence.adstudio.overlay import compose
    from creative_intelligence.adstudio.store import composites_dir
    from creative_intelligence.webapp.common import _claims, _product_dict
    from creative_intelligence.adstudio.render_template import photo_led
    from creative_intelligence.adstudio.templates import copy_fields
    from creative_intelligence.webapp.common import _product_dict
    from creative_intelligence.webapp.routes.compose import _resolve_copy_lists, _resolve_price
    conn = db()
    try:
        b = dict(conn.execute("SELECT * FROM compose_batches WHERE id=?", (batch_id,)).fetchone())
        p = json.loads(b["plan_json"])
        pid = b["product_id"]
        with conn:
            conn.execute("UPDATE compose_batches SET status='running', started_at=? WHERE id=? AND status='queued'",
                         (_now(), batch_id))
        hooks = {h["id"]: h for h in _hooks(conn, pid)}
        price = dict(p.get("layout") or {})
        try:
            _resolve_price(conn, pid, price)
        except ValueError as exc:
            price = {}
            with conn:
                conn.execute("UPDATE compose_batches SET message=? WHERE id=?", (f"No price: {exc}", batch_id))
        checked: dict[int, dict[str, Any]] = {}
        cache: dict = {}
        prod = _product_dict(conn, pid) or {}
        mat = material(conn, prod)
        stopped = False
        for i, it in enumerate(p["items"]):
            if i % 4 == 0 and cancelled(conn, batch_id):
                stopped = True
                break
            try:
                h = hooks.get(it["hook_id"]) or {}
                copy = {"headline": h.get("headline", ""), "subhead": h.get("subhead") or "",
                        "cta": facts.store_safe_cta(prod, h.get("cta") or "")}
                layout = dict(OVERLAY_LAYOUT) if it["type"] == OVERLAY else {"template": it["type"],
                                                                             "palette": it.get("palette", "")}
                if price.get("price") and it["type"] != OVERLAY:
                    layout.update({k: price[k] for k in ("show_price", "price", "price_variant", "price_source") if k in price})
                if it["type"] != OVERLAY and _needs(it["type"], copy_fields(it["type"]), None):
                    fill_lists(layout, mat, i)
                    _resolve_copy_lists(conn, pid, layout)
                iid, path = _base(conn, it["image"], it["size"], cache)
                with conn:
                    cid = conn.execute(
                        "INSERT INTO adstudio_composites (product_id, image_id, hook_id, headline, subhead, cta, layout_json,"
                        " batch_id) VALUES (?,?,?,?,?,?,?,?)",
                        (pid, iid, it["hook_id"], copy["headline"], copy["subhead"], copy["cta"], json.dumps(layout),
                         batch_id)).lastrowid
                out = str(composites_dir() / f"comp{cid}.png")
                metrics: dict[str, Any] = {}
                compose(path, out, headline=copy["headline"], subhead=copy["subhead"], cta=copy["cta"], layout=layout,
                        metrics=metrics)
                if (it["type"] == OVERLAY or photo_led(it["type"])) and metrics.get("over_subject", 0) > MAX_OVER_SUBJECT:
                    # no clear space on this photo: the same copy on a layout with its own panel, not a skipped ad
                    layout = {"template": PANEL_FALLBACK, "palette": it.get("palette", "")}
                    metrics = {}
                    compose(path, out, headline=copy["headline"], subhead=copy["subhead"], cta=copy["cta"],
                            layout=layout, metrics=metrics)
                    with conn:
                        conn.execute("UPDATE adstudio_composites SET layout_json=? WHERE id=?", (json.dumps(layout), cid))
                with conn:
                    conn.execute("UPDATE adstudio_composites SET output_path=? WHERE id=?", (out, cid))
                src = conn.execute("SELECT COALESCE(parent_image_id, id) FROM adstudio_images WHERE id=?", (iid,)).fetchone()[0]
                qa.inherit(conn, "composite", cid, "image", src, metrics)
                if it["hook_id"] and it["hook_id"] in checked:
                    facts.store(conn, "composite", cid, checked[it["hook_id"]])
                elif it["hook_id"]:
                    checked[it["hook_id"]] = _claims(conn, "composite", cid, pid, copy) or {"status": "ok"}
                _bump(conn, batch_id, True)
            except Exception as exc:
                _bump(conn, batch_id, False)
                with conn:
                    conn.execute("UPDATE compose_batches SET message=? WHERE id=?", (str(exc)[:300], batch_id))
        if p.get("ai") and not stopped and not cancelled(conn, batch_id):
            _ai(conn, batch_id, pid, p["ai"], hooks, (p.get("layout") or {}).get("direction", ""))
        stopped = stopped or cancelled(conn, batch_id)
        with conn:
            conn.execute("UPDATE compose_batches SET status=?, finished_at=? WHERE id=?",
                         ("cancelled" if stopped else "done", _now(), batch_id))
        r = conn.execute("SELECT done, failed FROM compose_batches WHERE id=?", (batch_id,)).fetchone()
        return {"batch_id": batch_id, "done": r[0], "failed": r[1]}
    except Exception as exc:
        with conn:
            conn.execute("UPDATE compose_batches SET status='failed', message=? WHERE id=?", (str(exc)[:300], batch_id))
        raise
    finally:
        conn.close()


def _brief_for(conn, pid: int, hook: dict[str, Any], batch_id: int, direction: str = "") -> tuple[int, dict[str, Any]]:
    """The old app's brief, which made its ads good: Claude writes the on-image copy for each format (headline <=38,
    sub <=45, a different headline per format) and one visual direction, carrying this hook's angle. A real 4–5 star
    review rides along for Social Proof. Falls back to the hook's own words if Claude can't be reached."""
    from creative_intelligence.adstudio import reviews, textads
    from creative_intelligence.webapp.common import _primary_photo_uris, _product_dict
    prod = _product_dict(conn, pid) or {}
    rid = (material(conn, prod)["review"] or [None])[0]
    review = reviews.resolve(conn, [rid], pid, "review")[0] if rid else None
    words = idea = " ".join(x for x in (hook.get("headline"), hook.get("hook_text")) if x)
    if direction:                      # the pack's creative direction: words and picture follow it, the facts still rule
        idea += (f"\n\nCREATIVE DIRECTION FOR THIS PACK (shape the copy and the visual direction around it; it never "
                 f"adds a claim, a date or urgency the approved facts don't give): {direction}")
    try:
        b = textads.draft_brief(prod, idea=idea, has_review=bool(review), image_data_uris=_primary_photo_uris(conn, pid, 1))
    except Exception:
        b = textads.normalize_brief({"hook": words, "cta": hook.get("cta") or ""})
    with conn:
        bid = conn.execute("INSERT INTO adstudio_briefs (product_id, idea, brief_json, review_text, review_author)"
                           " VALUES (?,?,?,?,?)", (pid, f"Bulk batch {batch_id}: {hook.get('headline', '')}"[:200],
                                                    json.dumps(b), (review or {}).get("text", ""),
                                                    (review or {}).get("author", ""))).lastrowid
    return bid, b


def _ai(conn, batch_id: int, pid: int, ai: list[dict[str, Any]], hooks: dict[int, dict[str, Any]],
        direction: str = "") -> None:
    """AI-designed ads: one real brief per hook (per-format copy and a visual direction, as the old app did), each ad
    claims-checked on its own words, then the pictures made AI_PARALLEL at a time (feedback 2026-10-26: 16 ads one by
    one took 7 minutes)."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from creative_intelligence.adstudio import facts
    from creative_intelligence.webapp.common import _claims, _product_dict
    prod = _product_dict(conn, pid) or {}
    from creative_intelligence.webapp.routes.aiads import run_textad
    from creative_intelligence.adstudio.textads import VARIANTS
    from creative_intelligence.webapp.common import db as conn_for_check
    briefs: dict[Any, tuple[int, dict[str, Any]]] = {}
    made: list[int] = []
    for n, it in enumerate(ai):
        if cancelled(conn, batch_id):
            break
        h = hooks.get(it["hook_id"]) or {}
        if it["hook_id"] not in briefs:
            briefs[it["hook_id"]] = _brief_for(conn, pid, h, batch_id, direction)
        bid, b = briefs[it["hook_id"]]
        copy = b.get("variants", {}).get(it["type"]) or {}
        head = copy.get("headline") or h.get("headline", "")
        sub = copy.get("sub", "") if "sub" in VARIANTS[it["type"]]["fields"] else ""
        cta = facts.store_safe_cta(prod, b.get("cta") or h.get("cta") or "")
        with conn:
            ad = conn.execute(
                "INSERT INTO adstudio_textads (product_id, brief_id, kind, variant, title, headline, subhead, cta,"
                " style_seed, aspect_ratio, batch_id, text_mode) VALUES (?,?, 'variant', ?,?,?,?,?,?,?,?, 'painted')",
                (pid, bid, it["type"], VARIANTS[it["type"]]["name"], head, sub, cta, n, it["size"], batch_id)).lastrowid
        res = _claims(conn, "textad", ad, pid, {"headline": head, "subhead": sub, "cta": cta})
        if res and res.get("status") == "flagged" and h.get("headline") and head != h["headline"]:
            # the brief's words claimed what the facts don't (feedback 2026-11-09: "Tastes like raspberry"): the
            # hook's own checked words instead
            head, sub = h["headline"], (h.get("subhead") or "") if sub else ""
            with conn:
                conn.execute("UPDATE adstudio_textads SET headline=?, subhead=? WHERE id=?", (head, sub, ad))
            res = _claims(conn, "textad", ad, pid, {"headline": head, "subhead": sub, "cta": cta})
        if res and res.get("status") == "flagged":           # never pay for a picture of a claim we can't make
            with conn:
                conn.execute("UPDATE adstudio_textads SET status='failed', error_message=? WHERE id=?",
                             ("Skipped: the claims check flagged its words", ad))
            _bump(conn, batch_id, False)
            continue
        made.append(ad)

    def one(ad: int) -> bool | None:
        c = conn_for_check()
        try:
            if cancelled(c, batch_id):
                with c:
                    c.execute("UPDATE adstudio_textads SET status='failed', error_message='Cancelled' WHERE id=?", (ad,))
                return None
        finally:
            c.close()
        return run_textad(ad).get("status") == "done"
    with ThreadPoolExecutor(max_workers=AI_PARALLEL) as pool:      # each ad opens its own connection
        for f in as_completed([pool.submit(one, ad) for ad in made]):
            if f.result() is not None:
                _bump(conn, batch_id, f.result())


def cancelled(conn, bid: int) -> bool:
    r = conn.execute("SELECT status FROM compose_batches WHERE id=?", (bid,)).fetchone()
    return bool(r) and r[0] == "cancelling"


def cancel(conn, bid: int) -> bool:
    """Stop a batch: the ads already made stay in the Gallery; no new ones start."""
    with conn:
        return conn.execute("UPDATE compose_batches SET status='cancelling' WHERE id=? AND status IN ('queued','running')",
                            (bid,)).rowcount == 1


def status(conn, bid: int) -> dict[str, Any] | None:
    r = conn.execute("SELECT * FROM compose_batches WHERE id=?", (bid,)).fetchone()
    if r is None:
        return None
    d = dict(r)
    d.pop("plan_json", None)
    return d


def recent(conn, pid: int, n: int = 5) -> list[dict[str, Any]]:
    return [status(conn, r[0]) for r in conn.execute(
        "SELECT id FROM compose_batches WHERE product_id=? ORDER BY id DESC LIMIT ?", (pid, n))]
