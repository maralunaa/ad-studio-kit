"""Ideas (Phase 2a): the idea bank, the generation mix, and the customer voice summary. The Market tab reads the
research endpoints in routes/results.py."""
from __future__ import annotations

import json
import logging
from typing import Any

from flask import Blueprint, jsonify, request, send_file

from creative_intelligence import knowledge
from creative_intelligence.webapp import common

bp = Blueprint("ideas", __name__)
log = logging.getLogger("adstudio")


@bp.get("/api/adstudio/ideas")
def ideas_list() -> Any:
    """?status=&product_id= -> the bank with evidence in parts, counts, the mix, the brief's open suggestions,
    and the choices the idea form offers."""
    from creative_intelligence import budget
    from creative_intelligence.adstudio import ideas, research
    conn = common.db()
    pid = request.args.get("product_id", type=int)
    rows = ideas.listing(conn, request.args.get("status") or None, pid)
    brief = research.latest_brief(conn)
    taken = {r[0] for r in conn.execute("SELECT source_ref FROM ideas WHERE source='brief'")}
    suggestions = [dict(s, index=n) for n, s in enumerate((brief or {}).get("suggestions") or [])
                   if ideas.brief_ref(brief["id"], s) not in taken] if brief else []
    out = {"ideas": rows, "counts": ideas.counts(conn), "mix": ideas.mix(),
           "statuses": [{"key": k, "name": ideas.STATUS_NAMES[k]} for k in ideas.STATUSES],
           "brief": {"id": brief["id"], "created_at": brief["created_at"], "suggestions": suggestions} if brief else None,
           "angles": ideas._angles(), "hook_types": [{"key": t["key"], "name": t["name"]} for t in knowledge.hook_types()],
           "styles": [{"key": s["key"], "name": s["name"], "medium": s["medium"]}
                      for s in knowledge.load("styles")["styles"] if s["status"] == "live"],
           "products": [dict(r) for r in conn.execute("SELECT id, name FROM adstudio_products ORDER BY name")],
           "draft_estimate": ideas.DRAFT_ESTIMATE_USD,
           "research_spent": budget.research_spent_this_month(conn), "research_cap": budget.caps(conn)["research_month"]}
    conn.close()
    return jsonify(out)


@bp.get("/api/adstudio/ideas/<int:iid>")
def idea_get(iid: int) -> Any:
    from creative_intelligence.adstudio import ideas
    conn = common.db()
    i = ideas.get(conn, iid)
    if i:
        i["ev"] = ideas.evidence(conn, i)
        i["used"] = ideas.usage(conn, iid)
    conn.close()
    return (jsonify(i), 200) if i else (jsonify({"error": "idea not found"}), 404)


@bp.post("/api/adstudio/ideas")
def idea_create() -> Any:
    from creative_intelligence.adstudio import ideas
    conn = common.db()
    iid, why = ideas.create(conn, request.get_json(force=True, silent=True) or {})
    conn.close()
    return (jsonify({"error": why}), 400) if iid is None else jsonify({"id": iid})


@bp.post("/api/adstudio/ideas/<int:iid>")
def idea_update(iid: int) -> Any:
    """Body: any idea fields, and/or {status}."""
    from creative_intelligence.adstudio import ideas
    conn = common.db()
    why = ideas.update(conn, iid, request.get_json(force=True, silent=True) or {})
    conn.close()
    return (jsonify({"error": why}), 400) if why else jsonify({"ok": True})


@bp.post("/api/adstudio/ideas/mix")
def ideas_mix() -> Any:
    """Body {share: 0..1}: how much of what Ad Studio makes follows an idea."""
    from creative_intelligence.adstudio import ideas
    try:
        return jsonify({"mix": ideas.set_share((request.get_json(force=True, silent=True) or {}).get("share"))})
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc) or "Give a share between 0 and 100%."}), 400


@bp.post("/api/adstudio/ideas/suggestion")
def ideas_from_suggestion() -> Any:
    """Body {brief_id, index}: approve one of the research brief's suggestions into the bank."""
    from creative_intelligence.adstudio import ideas, research
    d = request.get_json(force=True, silent=True) or {}
    conn = common.db()
    brief = research.latest_brief(conn)
    if not brief or brief["id"] != int(d.get("brief_id") or 0):
        conn.close()
        return jsonify({"error": "That brief is no longer the latest."}), 400
    try:
        s = brief["suggestions"][int(d.get("index"))]
    except (IndexError, TypeError, ValueError):
        conn.close()
        return jsonify({"error": "No such suggestion."}), 400
    iid, why = ideas.from_suggestion(conn, s, brief["id"])
    conn.close()
    return (jsonify({"error": why}), 400) if iid is None else jsonify({"id": iid})


@bp.post("/api/adstudio/ideas/seed")
def ideas_seed() -> Any:
    """Free: new ideas from our winners, winning patterns, Hook University angles and customer themes."""
    from creative_intelligence.adstudio import ideas
    conn = common.db()
    out = ideas.seed(conn)
    conn.close()
    return jsonify(out)


@bp.post("/api/adstudio/ideas/draft")
def ideas_draft() -> Any:
    """Body {confirm: true}: Claude drafts ideas from the evidence (research cap). Without confirm: the estimate."""
    from creative_intelligence import budget
    from creative_intelligence.adstudio import ideas
    d = request.get_json(force=True, silent=True) or {}
    if not d.get("confirm"):
        return jsonify({"estimate": ideas.DRAFT_ESTIMATE_USD})
    conn = common.db()
    try:
        out = ideas.draft(conn)
    except budget.BudgetExceeded as exc:
        out = {"error": str(exc)}
    except Exception as exc:
        log.exception("idea draft failed")
        out = {"error": str(exc)[:300]}
    conn.close()
    return (jsonify(out), 400) if out.get("error") else jsonify(out)


@bp.get("/api/adstudio/voice")
def customer_voice() -> Any:
    """?product_id=&shop_wide=1&q= -> the groups with reviews and themes, the last runs, one group's themes (desires,
    objections, moments, phrases with counts and real quotes) and matching reviews."""
    from creative_intelligence import config
    from creative_intelligence.adstudio import reviews, voice
    conn = common.db()
    pid = request.args.get("product_id", type=int)
    shop = request.args.get("shop_wide") == "1"
    q = (request.args.get("q") or "").strip().lower()
    out = voice.overview(conn)
    for g in out["groups"]:
        g["estimate"] = voice.estimate(conn, g["product_id"])
    out["kinds"] = voice.KINDS
    out["themes"] = voice.themes(conn, None if shop else pid, shop_wide=shop or not pid)
    out["quotes"] = [{k: r[k] for k in ("id", "text", "shown_as", "source_name", "posted_on", "rating", "kind", "curated")}
                     for r in reviews.listing(conn, None if shop else pid) if not q or q in r["text"].lower()][:50]
    out["sites"] = list(config.REVIEW_SITES)
    conn.close()
    return jsonify(out)


@bp.post("/api/adstudio/voice/import")
def voice_import() -> Any:
    """Body {source: fera | trustpilot | bbb | testimonials}: queue the import (web ones on the research cap)."""
    from creative_intelligence import config, jobs
    src = (request.get_json(force=True, silent=True) or {}).get("source")
    if src != "fera" and src not in config.REVIEW_SITES:
        return jsonify({"error": "unknown source"}), 400
    conn = common.db()
    jid = jobs.enqueue(conn, "voice", {"source": src}, dedupe_key=f"voice:{src}")
    conn.close()
    return jsonify({"job_id": jid})


@bp.post("/api/adstudio/voice/csv")
def voice_csv() -> Any:
    """Multipart 'file': a CSV of real reviews (text, author, rating, source, posted_on, product)."""
    from creative_intelligence.adstudio import voice
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "Pick a CSV file."}), 400
    conn = common.db()
    out = voice.import_csv(conn, f.read().decode("utf-8-sig", errors="replace"))
    conn.close()
    return (jsonify(out), 400) if out.get("error") else jsonify(out)


@bp.post("/api/adstudio/voice/extract")
def voice_extract() -> Any:
    """Body {product_id (null = shop-wide), confirm}: without confirm the estimate; with it, queue the extraction."""
    from creative_intelligence import budget, jobs
    from creative_intelligence.adstudio import voice
    d = request.get_json(force=True, silent=True) or {}
    pid = int(d["product_id"]) if d.get("product_id") else None
    conn = common.db()
    try:
        est = voice.estimate(conn, pid)
        if not d.get("confirm"):
            return jsonify({"estimate": est})
        budget.check_research(conn, est)
        return jsonify({"job_id": jobs.enqueue(conn, "voice", {"step": "extract", "product_id": pid},
                                               dedupe_key=f"voice:extract:{pid}")})
    except budget.BudgetExceeded as exc:
        return jsonify({"error": str(exc)}), 400
    finally:
        conn.close()


@bp.post("/api/adstudio/voice/theme/<int:tid>/idea")
def voice_theme_idea(tid: int) -> Any:
    """An objection answered by an approved fact (or any desire or moment) becomes a New idea in the bank."""
    from creative_intelligence.adstudio import ideas
    conn = common.db()
    t = conn.execute("SELECT * FROM voice_themes WHERE id=?", (tid,)).fetchone()
    if t is None:
        conn.close()
        return jsonify({"error": "theme not found"}), 404
    if t["kind"] == "objection" and not t["fact_id"]:
        conn.close()
        return jsonify({"error": "No approved fact answers this objection yet, so it can't be an angle."}), 400
    f = conn.execute("SELECT text FROM facts WHERE id=?", (t["fact_id"],)).fetchone() if t["fact_id"] else None
    iid, why = ideas.create(conn, {
        "title": t["angle"] or t["theme"], "product_ids": [t["product_id"]] if t["product_id"] else [],
        "why": f"{t['mentions']} customer reviews mention “{t['theme']}”" + (f"; answered by the approved fact: {f[0]}" if f else "."),
        "evidence": {"customer_terms": [w for w in t["theme"].lower().split() if len(w) > 3][:4],
                     "review_ids": json.loads(t["review_ids"])[:20], "voice_theme_id": tid}},
        source="voice", source_ref=f"theme:{tid}")
    conn.close()
    return (jsonify({"error": why}), 400) if iid is None else jsonify({"id": iid})


# ── Market (R1): brands, keywords, the monthly refresh, adding an ad ───────────
@bp.get("/api/adstudio/market")
def market_overview() -> Any:
    """?brand_id=&stage= -> competitor ads longest-running first, the brands (with US Ad Library links and context),
    keywords, the last refresh and the research spend."""
    from creative_intelligence import budget
    from creative_intelligence.adstudio import market
    conn = common.db()
    out = {"ads": market.listing(conn, request.args.get("brand_id", type=int), request.args.get("stage") or ""),
           "brands": market.brands(conn), "keywords": market.keywords(conn), "tiers": market.TIERS,
           "stages": [{"key": k, "name": n, "from_days": d} for k, n, d in market.STAGES],
           "scan": market.last_scan(conn), "concepts": len(market.concepts(conn)),
           "spent_month": budget.research_spent_this_month(conn), "cap_month": budget.caps(conn)["research_month"],
           "estimates": {"read": market.READ_ESTIMATE_USD, "context": market.CONTEXT_ESTIMATE_USD}}
    conn.close()
    return jsonify(out)


@bp.post("/api/adstudio/market/refresh")
def market_refresh() -> Any:
    """Queue the market refresh now (Ad Library API, tagging, brand context that is due; research cap applies)."""
    from creative_intelligence import jobs
    conn = common.db()
    jid = jobs.enqueue(conn, "research_scan", {"trigger": "manual"}, dedupe_key="research_scan")
    conn.close()
    return jsonify({"job_id": jid})


@bp.post("/api/adstudio/market/read")
def market_read() -> Any:
    """A screenshot (multipart 'file') or {landing_url}: Claude fills the Add ad form. Nothing is saved but the
    screenshot; the operator checks the fields and saves with /api/adstudio/research/ads."""
    from creative_intelligence import budget
    from creative_intelligence.adstudio import market
    conn = common.db()
    try:
        f = request.files.get("file")
        if f:
            got = market.read_screenshot(conn, f.read(), f.mimetype or "")
        else:
            got = market.read_landing(conn, ((request.get_json(force=True, silent=True) or {}).get("landing_url") or "").strip())
    except (ValueError, budget.BudgetExceeded) as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        log.exception("market read failed")
        return jsonify({"error": str(exc)[:300]}), 500
    finally:
        conn.close()
    return jsonify(got)


@bp.get("/api/adstudio/visuals")
def visuals_list() -> Any:
    """?kind=ours|winners|competitor|swipe: the visual board (feedback 2026-10-22)."""
    from creative_intelligence.adstudio import visuals
    conn = common.db()
    out = {"items": visuals.listing(conn, request.args.get("kind", "")), "kinds": visuals.KINDS}
    conn.close()
    return jsonify(out)


@bp.get("/api/adstudio/visuals/<kind>/<ref>/image")
def visuals_image(kind: str, ref: str) -> Any:
    from creative_intelligence.adstudio import visuals
    conn = common.db()
    try:
        p = visuals.image_path(conn, kind, ref)
    except Exception:
        p = None
    conn.close()
    return send_file(str(p)) if p else ("", 404)


@bp.post("/api/adstudio/visuals/like")
def visuals_like() -> Any:
    """Body {kind, ref, product_id, n (1–4), hook_ids?, size?}: AI-designed ads for the product, like this picture."""
    from creative_intelligence import jobs
    from creative_intelligence.adstudio import visuals
    d = request.get_json(force=True, silent=True) or {}
    conn = common.db()
    ids, why = visuals.make_like(conn, d.get("kind", ""), str(d.get("ref", "")), int(d.get("product_id") or 0),
                                 int(d.get("n") or 2), [int(h) for h in d.get("hook_ids") or []], d.get("size") or "4:5")
    if ids:
        jobs.enqueue(conn, "like_this", {"ids": ids})
    conn.close()
    return (jsonify({"error": why}), 400) if why else jsonify({"queued": len(ids)})


@bp.post("/api/adstudio/visuals/repurpose")
def visuals_repurpose() -> Any:
    """Body {items: [{kind, id}], product_id, action: statics|hooks|idea|video, n?, size?}."""
    from creative_intelligence import jobs
    from creative_intelligence.adstudio import visuals
    d = request.get_json(force=True, silent=True) or {}
    conn = common.db()
    try:
        out = visuals.repurpose(conn, d.get("items") or [], int(d.get("product_id") or 0), d.get("action", ""),
                                int(d.get("n") or 2), d.get("size") or "4:5")
        if out.get("ids"):
            jobs.enqueue(conn, "like_this", {"ids": out["ids"]})
        return (jsonify(out), 400) if out.get("error") and not out.get("queued") else jsonify(out)
    finally:
        conn.close()


@bp.get("/api/adstudio/instagram")
def instagram_status() -> Any:
    from creative_intelligence.adstudio import instagram
    conn = common.db()
    try:
        return jsonify(instagram.status(conn))
    finally:
        conn.close()


@bp.post("/api/adstudio/instagram/refresh")
def instagram_refresh() -> Any:
    from creative_intelligence import jobs
    conn = common.db()
    try:
        jobs.enqueue(conn, "ig_sync", {"trigger": "manual"}, dedupe_key="ig_sync")
        return jsonify({"queued": True})
    finally:
        conn.close()


@bp.post("/api/adstudio/market/screens")
def market_screens() -> Any:
    """Several screenshots at once (multipart 'files'): each is saved as a competitor ad and read by Claude in the
    worker (brand, words, format, angle, why it works), so the board fills without filling a form per ad."""
    from creative_intelligence import jobs
    from creative_intelligence.adstudio import market
    conn = common.db()
    added = []
    problems = []
    for f in request.files.getlist("files")[:30]:
        try:
            added.append(market.store_screenshot(f.read(), f.mimetype or ""))
        except ValueError as exc:
            problems.append(f"{f.filename}: {exc}")
    if added:
        jobs.enqueue(conn, "read_screens", {"paths": added})
    conn.close()
    return jsonify({"queued": len(added), "problems": problems})


@bp.get("/api/adstudio/market/ad/<int:aid>/media")
def market_media(aid: int) -> Any:
    from pathlib import Path
    from flask import send_file
    conn = common.db()
    r = conn.execute("SELECT media_path FROM competitor_ads WHERE id=?", (aid,)).fetchone()
    conn.close()
    if not r or not r[0] or not Path(r[0]).exists():
        return ("", 404)
    return send_file(r[0], conditional=True)


@bp.post("/api/adstudio/market/brands")
def market_save_brand() -> Any:
    """Body: brand fields (with id to edit). Admin › Research."""
    from creative_intelligence.adstudio import market
    conn = common.db()
    bid, why = market.save_brand(conn, request.get_json(force=True, silent=True) or {})
    conn.close()
    return (jsonify({"error": why}), 400) if bid is None else jsonify({"id": bid})


@bp.post("/api/adstudio/market/brand/<int:bid>/context")
def market_brand_context(bid: int) -> Any:
    """Body {confirm: true}: refresh this brand's offer, prices, guarantee and positioning now (research cap)."""
    from creative_intelligence import budget
    from creative_intelligence.adstudio import market
    if not (request.get_json(force=True, silent=True) or {}).get("confirm"):
        return jsonify({"estimate": market.CONTEXT_ESTIMATE_USD})
    conn = common.db()
    try:
        b = next((x for x in market.brands(conn) if x["id"] == bid), None)
        if b is None or not b["website"]:
            return jsonify({"error": "Add the brand's website first."}), 400
        return jsonify({"context": market.brand_context(conn, b)})
    except budget.BudgetExceeded as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        log.exception("brand context failed")
        return jsonify({"error": str(exc)[:300]}), 500
    finally:
        conn.close()


@bp.post("/api/adstudio/market/keywords")
def market_keywords() -> Any:
    """Body {term} to add, or {remove: id}."""
    from creative_intelligence.adstudio import market
    d = request.get_json(force=True, silent=True) or {}
    conn = common.db()
    if d.get("remove"):
        market.remove_keyword(conn, int(d["remove"]))
        why = ""
    else:
        why = market.add_keyword(conn, d.get("term") or "")
    conn.close()
    return (jsonify({"error": why}), 400) if why else jsonify({"ok": True})


@bp.post("/api/adstudio/market/ad/<int:aid>/video")
def market_video(aid: int) -> Any:
    """Multipart 'file': a competitor ad's video the operator saved. Proven or Evergreen ads are decoded into a
    competitor-derived structure template (research cap)."""
    from creative_intelligence import budget
    from creative_intelligence.adstudio import market
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "Pick the video file."}), 400
    conn = common.db()
    try:
        out = market.attach_video(conn, aid, f.read(), f.filename or "")
    except budget.BudgetExceeded as exc:
        out = {"error": str(exc)}
    finally:
        conn.close()
    return (jsonify(out), 400) if out.get("error") else jsonify(out)


@bp.post("/api/adstudio/market/ad/<int:aid>/decode")
def market_decode(aid: int) -> Any:
    from creative_intelligence import budget
    from creative_intelligence.adstudio import market
    conn = common.db()
    try:
        out = market.queue_decode(conn, aid)
    except budget.BudgetExceeded as exc:
        out = {"error": str(exc)}
    finally:
        conn.close()
    return (jsonify(out), 400) if out.get("error") else jsonify(out)


# ── Swipe file and taste rubric (R6) ──────────────────────────────────────────
@bp.get("/api/adstudio/swipe")
def swipe_overview() -> Any:
    from creative_intelligence.adstudio import swipe
    conn = common.db()
    out = {"items": swipe.listing(conn), "proposals": swipe.proposals(conn), "approved": swipe.approved(),
           "estimate": swipe.PROPOSE_ESTIMATE_USD}
    conn.close()
    return jsonify(out)


@bp.post("/api/adstudio/swipe")
def swipe_save() -> Any:
    """JSON {kind: competitor_ad | own_asset | link, ref_id?, url?, notes?} or multipart 'file' (+ notes) for a screenshot."""
    from creative_intelligence.adstudio import swipe
    conn = common.db()
    f = request.files.get("file")
    if f:
        sid, why = swipe.save_upload(conn, f.read(), f.mimetype or "", request.form.get("notes") or "")
    else:
        sid, why = swipe.save(conn, request.get_json(force=True, silent=True) or {})
    conn.close()
    return (jsonify({"error": why}), 400) if sid is None else jsonify({"id": sid})


@bp.post("/api/adstudio/swipe/<int:sid>")
def swipe_edit(sid: int) -> Any:
    """Body {notes} to edit, or {remove: true}."""
    from creative_intelligence.adstudio import swipe
    d = request.get_json(force=True, silent=True) or {}
    conn = common.db()
    if d.get("remove"):
        swipe.remove(conn, sid)
    else:
        swipe.update_notes(conn, sid, d.get("notes") or "")
    conn.close()
    return jsonify({"ok": True})


@bp.get("/api/adstudio/swipe/<int:sid>/image")
def swipe_image(sid: int) -> Any:
    from pathlib import Path
    from flask import send_file
    conn = common.db()
    r = conn.execute("SELECT media_path FROM swipe_items WHERE id=?", (sid,)).fetchone()
    conn.close()
    if not r or not r[0] or not Path(r[0]).is_file():
        return ("", 404)
    return send_file(r[0], conditional=True)


@bp.post("/api/adstudio/taste/propose")
def taste_propose() -> Any:
    """Body {confirm: true}: queue Claude's taste rubric proposals (research cap). Without confirm: the estimate."""
    from creative_intelligence import jobs
    from creative_intelligence.adstudio import swipe
    if not (request.get_json(force=True, silent=True) or {}).get("confirm"):
        return jsonify({"estimate": swipe.PROPOSE_ESTIMATE_USD})
    conn = common.db()
    jid = jobs.enqueue(conn, "taste", {"trigger": "manual"}, dedupe_key="taste")
    conn.close()
    return jsonify({"job_id": jid})


@bp.post("/api/adstudio/taste/<int:pid>/decide")
def taste_decide(pid: int) -> Any:
    """Body {index, approve}: one proposed criterion into the rubric (approve) or not."""
    from creative_intelligence.adstudio import swipe
    d = request.get_json(force=True, silent=True) or {}
    conn = common.db()
    why = swipe.decide(conn, pid, int(d.get("index", -1)), bool(d.get("approve")))
    conn.close()
    return (jsonify({"error": why}), 400) if why else jsonify({"ok": True})


@bp.post("/api/adstudio/taste/retire")
def taste_retire() -> Any:
    from creative_intelligence.adstudio import swipe
    ok = swipe.retire((request.get_json(force=True, silent=True) or {}).get("key") or "")
    return (jsonify({"ok": True}), 200) if ok else (jsonify({"error": "not found"}), 404)


@bp.get("/api/adstudio/taste/export")
def taste_export() -> Any:
    from creative_intelligence.adstudio import swipe
    return jsonify({"yaml": swipe.export(), "count": len(swipe.approved())})
