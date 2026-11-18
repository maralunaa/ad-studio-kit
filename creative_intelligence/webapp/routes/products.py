"""Products: the Product Hub (Shopify catalogue, profile, readiness, research, facts), reference
photos, hooks, chat, winning patterns, Shopify import."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from flask import Blueprint, jsonify, request, send_file

from creative_intelligence import knowledge
from creative_intelligence.webapp import common
from creative_intelligence.webapp.common import _claims, _primary_photo_uris, _product_dict, _uri

bp = Blueprint("products", __name__)
log = logging.getLogger("adstudio")
_HERE = Path(__file__).parent.parent


# ── Products ─────────────────────────────────────────────────────────────
@bp.post("/api/adstudio/products")
def adstudio_create_product() -> Any:
    from creative_intelligence.adstudio.profiles import normalize_type
    d = request.get_json(force=True, silent=True) or {}
    name = (d.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    conn = common.db()
    with conn:
        cur = conn.execute(
            "INSERT INTO adstudio_products (name, description, scent_notes, physical_desc, notes,"
            " varieties, product_type) VALUES (?,?,?,?,?,?,?)",
            (name, d.get("description", ""), d.get("scent_notes", ""),
             d.get("physical_desc", ""), d.get("notes", ""), d.get("varieties", ""),
             normalize_type(d.get("product_type"))),
        )
    pid = cur.lastrowid
    conn.close()
    return jsonify({"id": pid, "name": name})


@bp.post("/api/adstudio/product/<int:pid>/update")
def adstudio_update_product(pid: int) -> Any:
    """Update editable product fields (name, scent_notes, physical_desc, description, notes, …)."""
    from creative_intelligence.adstudio.profiles import normalize_type
    d = request.get_json(force=True, silent=True) or {}
    if "product_type" in d:
        d["product_type"] = normalize_type(d["product_type"])
    if d.get("taste_status") not in (None, "", "draft", "approved"):
        return jsonify({"error": "bad taste_status"}), 400
    sets, vals = [], []
    for k in ("name", "scent_notes", "physical_desc", "description", "notes",
              "varieties", "shopify_url", "info_json", "product_type", "taste", "taste_status",
              "customer_language"):
        if k in d:
            sets.append(f"{k}=?"); vals.append(d[k])
    if not sets:
        return jsonify({"error": "no fields"}), 400
    vals.append(pid)
    conn = common.db()
    with conn:
        conn.execute(f"UPDATE adstudio_products SET {', '.join(sets)} WHERE id=?", vals)
    conn.close()
    return jsonify({"ok": True})


@bp.get("/api/adstudio/products")
def adstudio_list_products() -> Any:
    conn = common.db()
    rows = [dict(r) for r in conn.execute(
        "SELECT p.*, (SELECT COUNT(*) FROM adstudio_photos WHERE product_id=p.id) AS photo_count,"
        " (SELECT COUNT(*) FROM adstudio_hooks WHERE product_id=p.id) AS hook_count,"
        " (SELECT id FROM adstudio_photos WHERE product_id=p.id ORDER BY is_hero DESC, is_primary DESC, id LIMIT 1)"
        " AS thumb_photo_id FROM adstudio_products p WHERE p.archived=0 ORDER BY p.id DESC").fetchall()]
    conn.close()
    return jsonify({"products": rows})


@bp.get("/api/adstudio/product/<int:pid>")
def adstudio_get_product(pid: int) -> Any:
    conn = common.db()
    prod = conn.execute("SELECT * FROM adstudio_products WHERE id=?", (pid,)).fetchone()
    if prod is None:
        conn.close()
        return jsonify({"error": "not found"}), 404
    photos = [dict(r) for r in conn.execute(
        "SELECT id, is_primary, is_hero, tag, role FROM adstudio_photos WHERE product_id=? ORDER BY is_hero DESC, id", (pid,)).fetchall()]
    hooks = [dict(r) for r in conn.execute(
        "SELECT * FROM adstudio_hooks WHERE product_id=? ORDER BY id DESC", (pid,)).fetchall()]
    from creative_intelligence.adstudio.facts import checks_for, with_season_check
    ck = checks_for(conn, "hook", [h["id"] for h in hooks])
    for h in hooks:
        h["claims_check"] = with_season_check(ck.get(h["id"]), [h.get(k) or "" for k in ("headline", "hook_text", "subhead", "body")])
    conn.close()
    return jsonify({"product": dict(prod), "photos": photos, "hooks": hooks})


# ── Photos ───────────────────────────────────────────────────────────────
@bp.post("/api/adstudio/product/<int:pid>/photo")
def adstudio_upload_photo(pid: int) -> Any:
    from creative_intelligence.adstudio.store import save_reference_photo
    f = request.files.get("file")
    if f is None:
        return jsonify({"error": "no file"}), 400
    raw = f.read()
    if not raw:
        return jsonify({"error": "empty file"}), 400
    path = save_reference_photo(raw, f.filename or "photo.png", pid)
    conn = common.db()
    is_primary = 0 if conn.execute(
        "SELECT 1 FROM adstudio_photos WHERE product_id=? LIMIT 1", (pid,)).fetchone() else 1
    with conn:
        cur = conn.execute(
            "INSERT INTO adstudio_photos (product_id, path, is_primary) VALUES (?,?,?)",
            (pid, path, is_primary))
    photo_id = cur.lastrowid
    conn.close()
    return jsonify({"id": photo_id, "is_primary": is_primary})


@bp.get("/api/adstudio/photo/<int:photo_id>")
def adstudio_serve_photo(photo_id: int) -> Any:
    conn = common.db()
    row = conn.execute("SELECT path FROM adstudio_photos WHERE id=?", (photo_id,)).fetchone()
    conn.close()
    if row is None or not Path(row["path"]).exists():
        return ("", 404)
    return send_file(row["path"], conditional=True)


@bp.post("/api/adstudio/photo/<int:photo_id>/delete")
def adstudio_delete_photo(photo_id: int) -> Any:
    conn = common.db()
    with conn:
        conn.execute("DELETE FROM adstudio_photos WHERE id=?", (photo_id,))
    conn.close()
    return jsonify({"ok": True})


# ── Hooks ────────────────────────────────────────────────────────────────
_HOOK_FIELDS = ("hook_text", "archetype", "d", "c", "ps",
                "headline", "subhead", "body", "cta", "hook_type", "prompt_version", "critic_note")


@bp.post("/api/adstudio/product/<int:pid>/hook")
def adstudio_add_hook(pid: int) -> Any:
    d = request.get_json(force=True, silent=True) or {}
    if d.get("hook_type") not in knowledge.hook_type_keys():
        d["hook_type"] = ""
    vals = [d.get(k) or "" for k in _HOOK_FIELDS]
    if not (vals[0] or vals[5]):  # need hook_text or headline
        return jsonify({"error": "hook_text or headline required"}), 400
    conn = common.db()
    with conn:
        cur = conn.execute(
            f"INSERT INTO adstudio_hooks (product_id, {', '.join(_HOOK_FIELDS)}, source, pattern_id, idea_id)"
            f" VALUES (?{',?'*len(_HOOK_FIELDS)},?,?,?)",
            (pid, *vals, d.get("source", "manual"), d.get("pattern_id") or None, d.get("idea_id") or None))
    hid = cur.lastrowid
    res = _claims(conn, "hook", hid, pid, {k: d.get(k, "") for k in ("hook_text", "headline", "subhead", "body", "cta")})
    conn.close()
    return jsonify({"id": hid, "claims_check": res})


@bp.post("/api/adstudio/product/<int:pid>/hooks/recheck")
def adstudio_recheck_hooks(pid: int) -> Any:
    """Claims-check every hook of a product again (after new facts or a new check prompt), in the worker: a product
    with many hooks took longer than a web request may (2026-10-26). Bulk reads the stored results."""
    from creative_intelligence import jobs
    conn = common.db()
    try:
        n = conn.execute("SELECT COUNT(*) FROM adstudio_hooks WHERE product_id=?", (pid,)).fetchone()[0]
        jobs.enqueue(conn, "hook_recheck", {"product_id": pid}, dedupe_key=f"hook_recheck:{pid}")
        return jsonify({"queued": n})
    finally:
        conn.close()


def recheck_hooks(conn, pid: int) -> dict[str, int]:
    """The hook_recheck job: every hook of the product claims-checked again and stored."""
    rows = [dict(r) for r in conn.execute("SELECT id, hook_text, headline, subhead, body, cta FROM adstudio_hooks"
                                          " WHERE product_id=?", (pid,))]
    ok = 0
    for r in rows:
        res = _claims(conn, "hook", r["id"], pid, {k: r[k] or "" for k in ("hook_text", "headline", "subhead", "body",
                                                                         "cta")})
        ok += (res or {}).get("status") == "ok"
    return {"hooks": len(rows), "ok": ok}


@bp.post("/api/adstudio/hook/<int:hid>/delete")
def adstudio_delete_hook(hid: int) -> Any:
    conn = common.db()
    with conn:
        conn.execute("DELETE FROM adstudio_hooks WHERE id=?", (hid,))
    conn.close()
    return jsonify({"ok": True})


@bp.post("/api/adstudio/product/<int:pid>/chat")
def adstudio_chat(pid: int) -> Any:
    from creative_intelligence.adstudio.chat import chat_reply
    d = request.get_json(force=True, silent=True) or {}
    messages = d.get("messages") or []
    if not messages:
        return jsonify({"error": "messages required"}), 400
    conn = common.db()
    prod = _product_dict(conn, pid)
    uris = _primary_photo_uris(conn, pid)
    conn.close()
    if prod is None:
        return jsonify({"error": "product not found"}), 404
    try:
        reply = chat_reply(messages, prod, uris)
        return jsonify({"reply": reply})
    except Exception as exc:
        log.exception("adstudio chat failed")
        return jsonify({"error": str(exc)[:300]}), 500


@bp.post("/api/adstudio/product/<int:pid>/extract-hooks")
def adstudio_extract_hooks(pid: int) -> Any:
    """Extract hooks from a chat conversation and (optionally) save them."""
    from creative_intelligence.adstudio.chat import extract_hooks
    d = request.get_json(force=True, silent=True) or {}
    messages = d.get("messages") or []
    if not messages:
        return jsonify({"error": "no conversation to pull from"}), 400
    conn = common.db()
    prod = _product_dict(conn, pid)
    uris = _primary_photo_uris(conn, pid)
    conn.close()
    if prod is None:
        return jsonify({"error": "product not found"}), 404
    try:
        hooks = extract_hooks(prod, messages, uris)
    except Exception as exc:
        log.exception("extract-hooks failed")
        return jsonify({"error": str(exc)[:300]}), 500

    saved = 0
    if d.get("save") and hooks:
        conn = common.db()
        with conn:
            for h in hooks:
                conn.execute(
                    "INSERT INTO adstudio_hooks (product_id, hook_text, archetype, d, c, ps,"
                    " headline, subhead, body, cta, source) VALUES (?,?,?,?,?,?,?,?,?,?, 'chat')",
                    (pid, h.get("hook_text", ""), h.get("archetype", ""), h.get("d", ""),
                     h.get("c", ""), h.get("ps", ""), h.get("headline", ""),
                     h.get("subhead", ""), h.get("body", ""), h.get("cta", "")))
                saved += 1
        conn.close()
    return jsonify({"hooks": hooks, "saved_count": saved})


@bp.post("/api/adstudio/product/<int:pid>/suggest-hooks")
def adstudio_suggest_hooks(pid: int) -> Any:
    """
    Body: {n, pattern: "auto" | "none" | <pattern id>, idea?, idea_id?, brief?}.
    "auto" builds on the top winning patterns from our Meta data. Without a typed or picked idea, the idea
    bank steers its share of hook sets (rules.ideas_mix).
    """
    from creative_intelligence.adstudio import ideas
    from creative_intelligence.adstudio.chat import suggest_hooks
    from creative_intelligence.adstudio.winning import current_patterns, get_pattern
    d = request.get_json(force=True, silent=True) or {}
    mode = str(d.get("pattern", "auto"))
    conn = common.db()
    idea, idea_id = ideas.pick_one(conn, pid, "hooks", (d.get("idea") or "").strip(), d.get("idea_id"),
                                   int(d.get("n", 5)))
    prod = _product_dict(conn, pid)
    uris = _primary_photo_uris(conn, pid)
    recent = [r[0] for r in conn.execute("SELECT COALESCE(NULLIF(hook_text,''), headline) FROM adstudio_hooks"
                                         " WHERE product_id=? ORDER BY id DESC LIMIT 30", (pid,))]
    if mode == "none":
        patterns = []
    elif mode.isdigit():
        one = get_pattern(conn, int(mode))
        patterns = [one] if one else []
    else:
        patterns = current_patterns(conn, limit=4)
    conn.close()
    if prod is None:
        return jsonify({"error": "product not found"}), 404
    if not patterns and not idea and mode != "none":
        return jsonify({"error": "No winning patterns yet — sync from Meta, or type an idea to test."}), 400
    try:
        hooks = suggest_hooks(prod, int(d.get("n", 5)), d.get("brief", ""), uris,
                              patterns=patterns, idea=idea, recent=recent)
        labels = {p["id"]: p["label"] for p in patterns}
        for h in hooks:
            h["pattern_label"] = labels.get(h.get("pattern_id"), "")
            if h["pattern_label"]:
                h["archetype"] = h["pattern_label"]
            h["source"] = ("pattern+idea" if h["pattern_label"] and idea else
                           "pattern" if h["pattern_label"] else "idea" if idea else "claude")
            h["idea_id"] = idea_id
        return jsonify({"hooks": hooks})
    except Exception as exc:
        log.exception("adstudio suggest-hooks failed")
        return jsonify({"error": str(exc)[:300]}), 500


# ── Winning patterns (real Meta results) + Meta sync ───────────────────────
@bp.get("/api/adstudio/patterns")
def adstudio_patterns() -> Any:
    from creative_intelligence.adstudio.winning import current_patterns, data_status
    conn = common.db()
    out = {"patterns": current_patterns(conn, limit=12), "status": data_status(conn)}
    conn.close()
    return jsonify(out)


@bp.post("/api/adstudio/claims/check")
def adstudio_claims_check() -> Any:
    """Body: {product_id, items: {field: text}} — check any copy on demand (no storage)."""
    from creative_intelligence.adstudio import facts
    d = request.get_json(force=True, silent=True) or {}
    conn = common.db()
    res = facts.check_copy(conn, _product_dict(conn, int(d.get("product_id") or 0)) or {}, d.get("items") or {})
    conn.close()
    return jsonify(res)


def _tag_photos(conn, pid: int, name: str) -> dict[int, str]:
    """Claude assigns a role to each of the product's photos; primary = core1."""
    from creative_intelligence.adstudio.core import classify_photos
    rows = [dict(r) for r in conn.execute(
        "SELECT id, path FROM adstudio_photos WHERE product_id=? ORDER BY id", (pid,)).fetchall()]
    rows = [r for r in rows if _uri(r["path"])]
    roles = classify_photos(name, [_uri(r["path"]) for r in rows])
    with conn:
        for r, role in zip(rows, roles):
            conn.execute("UPDATE adstudio_photos SET role=? WHERE id=?", (role, r["id"]))
        hero = next((r["id"] for r, role in zip(rows, roles) if role == "core1"), None)
        if hero:
            conn.execute("UPDATE adstudio_photos SET is_primary=(id=?) WHERE product_id=?", (hero, pid))
    return {r["id"]: role for r, role in zip(rows, roles)}


@bp.post("/api/adstudio/import-shopify")
def adstudio_import_shopify() -> Any:
    """Create/refresh a product from its Shopify URL: name, copy, variants, photos."""
    from urllib.request import Request, urlopen
    from creative_intelligence.adstudio import core as C
    from creative_intelligence.adstudio.store import save_reference_photo
    d = request.get_json(force=True, silent=True) or {}
    url = (d.get("url") or "").strip()
    n_photos = max(0, min(int(d.get("photos", 8)), 10))
    try:
        sp = C.fetch_shopify_product(url)
    except Exception as exc:
        return jsonify({"error": f"Shopify fetch failed: {str(exc)[:200]}"}), 400
    rows, estimate = C.rows_from_variants(sp["variants"])
    info = {"name": C.short_name(sp["title"]), "rows": rows, "estimate": estimate,
            "bab": C.bab_row([r for r in rows if r["on"]] or rows)}

    conn = common.db()
    pid = d.get("product_id")
    if not pid:
        hit = conn.execute("SELECT id FROM adstudio_products WHERE shopify_url LIKE ?",
                           (f"%/products/{sp['handle']}%",)).fetchone()
        pid = hit["id"] if hit else None
    with conn:
        if pid:
            conn.execute("UPDATE adstudio_products SET description=?, shopify_url=?, info_json=?"
                         " WHERE id=?", (sp["description"], url, json.dumps(info), int(pid)))
        else:
            cur = conn.execute(
                "INSERT INTO adstudio_products (name, description, shopify_url, info_json,"
                " product_type) VALUES (?,?,?,?, 'fruit')",
                (sp["title"], sp["description"], url, json.dumps(info)))
            pid = cur.lastrowid
    pid = int(pid)
    has_photos = conn.execute("SELECT 1 FROM adstudio_photos WHERE product_id=? LIMIT 1",
                              (pid,)).fetchone()
    added = 0
    if not has_photos:
        for i, src in enumerate(sp["images"][:n_photos]):
            try:
                sized = src + ("&" if "?" in src else "?") + "width=1200"
                raw = urlopen(Request(sized, headers={"User-Agent": _brand_ua()}),  # noqa: S310
                              timeout=30).read()
                path = save_reference_photo(raw, f"shopify{i}.jpg", pid)
                with conn:
                    conn.execute("INSERT INTO adstudio_photos (product_id, path, is_primary)"
                                 " VALUES (?,?,?)", (pid, path, 1 if added == 0 else 0))
                added += 1
            except Exception:
                log.exception("shopify image download failed: %s", src)
    roles: dict[int, str] = {}
    if added:
        try:
            roles = _tag_photos(conn, pid, sp["title"])
        except Exception:
            log.exception("photo tagging failed")
    conn.close()
    return jsonify({"id": pid, "photos_added": added, "roles": roles, "info": info})


@bp.post("/api/adstudio/product/<int:pid>/tag-photos")
def adstudio_tag_photos(pid: int) -> Any:
    conn = common.db()
    prod = _product_dict(conn, pid)
    if prod is None:
        conn.close(); return jsonify({"error": "product not found"}), 404
    try:
        roles = _tag_photos(conn, pid, prod["name"])
    except Exception as exc:
        conn.close()
        log.exception("tag-photos failed")
        return jsonify({"error": str(exc)[:300]}), 500
    conn.close()
    return jsonify({"roles": roles})


@bp.post("/api/adstudio/photo/<int:photo_id>/role")
def adstudio_photo_role(photo_id: int) -> Any:
    from creative_intelligence.adstudio.core import PHOTO_ROLES
    role = (request.get_json(force=True, silent=True) or {}).get("role", "")
    if role and role not in PHOTO_ROLES:
        return jsonify({"error": "bad role"}), 400
    conn = common.db()
    with conn:
        conn.execute("UPDATE adstudio_photos SET role=? WHERE id=?", (role, photo_id))
    conn.close()
    return jsonify({"ok": True})


@bp.post("/api/adstudio/product/<int:pid>/describe-fruit")
def adstudio_describe_fruit(pid: int) -> Any:
    """Claude drafts the fruit's whole/cut look (+ varieties) from its photos."""
    from creative_intelligence.adstudio.core import describe_fruit
    conn = common.db()
    prod = _product_dict(conn, pid)
    uris = _primary_photo_uris(conn, pid, limit=4)
    conn.close()
    if prod is None:
        return jsonify({"error": "product not found"}), 404
    try:
        return jsonify(describe_fruit(prod, uris))
    except Exception as exc:
        log.exception("describe-fruit failed")
        return jsonify({"error": str(exc)[:300]}), 500


# ── Product Hub ──────────────────────────────────────────────────────────
def _has_ads(conn) -> set[int]:
    """Products with ads made here or linked to Meta ads."""
    return {r[0] for r in conn.execute(
        "SELECT product_id FROM adstudio_assets UNION SELECT product_id FROM adstudio_composites"
        " UNION SELECT product_id FROM adstudio_textads")}


@bp.get("/api/adstudio/catalog")
def adstudio_catalog() -> Any:
    """Every product for the hub list: Shopify ones and ones added by hand (archived only on request)."""
    from creative_intelligence.adstudio import catalog, hub
    conn = common.db()
    archived = request.args.get("archived") == "1"
    st, ads = hub.stats(conn), _has_ads(conn)
    rows = []
    for r in conn.execute(
            "SELECT p.id, p.name, p.product_type, p.origin, p.shopify_url, p.shop_json, p.in_stock, p.season,"
            " p.archived, p.physical_desc, p.taste_status, p.fill_status,"
            " (SELECT id FROM adstudio_photos WHERE product_id=p.id ORDER BY is_hero DESC, is_primary DESC, id LIMIT 1)"
            " AS thumb_photo_id FROM adstudio_products p WHERE p.archived=? ORDER BY p.name COLLATE NOCASE",
            (int(archived),)):
        p = dict(r)
        shop = catalog.shop(p)
        ready = hub.readiness(p, st.get(p["id"], {}))
        rows.append({"id": p["id"], "name": p["name"], "product_type": p["product_type"] or "other",
                     "in_store": p["origin"] == "shopify", "in_stock": p["in_stock"], "season": p["season"],
                     "archived": p["archived"], "price_min": shop.get("price_min"),
                     "thumb_photo_id": p["thumb_photo_id"], "thumb_url": shop.get("image", ""),
                     "has_ads": p["id"] in ads, "ready_images": ready["images"]["ready"],
                     "ready_video": ready["video"]["ready"], "steps_done": ready["steps_done"],
                     "steps": ready["steps"], "pending": st.get(p["id"], {}).get("pending", 0),
                     "filling": p["fill_status"] in ("queued", "running"),
                     "researched": bool(st.get(p["id"], {}).get("researched"))})
    out = {"products": rows, "sync": catalog.last_sync(conn)}
    conn.close()
    return jsonify(out)


@bp.post("/api/adstudio/catalog/sync")
def adstudio_catalog_sync() -> Any:
    from creative_intelligence.adstudio import catalog
    conn = common.db()
    job = catalog.queue(conn, "manual")
    conn.close()
    return jsonify({"job_id": job})


@bp.get("/api/adstudio/catalog/sync")
def adstudio_catalog_sync_status() -> Any:
    from creative_intelligence.adstudio import catalog
    conn = common.db()
    busy = conn.execute("SELECT 1 FROM jobs WHERE kind='shopify_sync' AND status IN ('queued','running')").fetchone()
    out = {"sync": catalog.last_sync(conn), "running": bool(busy)}
    conn.close()
    return jsonify(out)


def _live_prices(p: dict[str, Any], shop: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    """Variant prices read live from the store page; the last sync's prices when the store can't be reached."""
    from creative_intelligence.adstudio import core as C
    if p.get("origin") == "shopify" and p.get("shopify_url"):
        try:
            return C.shopify_prices(p["shopify_url"]), True
        except Exception:
            log.warning("live price fetch failed for product %s", p["id"])
    return [{"title": v["title"], "price": f"${float(v['price']):,.2f}".replace(".00", ""), "available": v["available"]}
            for v in shop.get("variants", []) if str(v.get("price", "")).replace(".", "", 1).isdigit()], False


@bp.get("/api/adstudio/product/<int:pid>/profile")
def adstudio_profile(pid: int) -> Any:
    from creative_intelligence.adstudio import catalog, hub
    from creative_intelligence.adstudio.facts import CATEGORIES
    from creative_intelligence.adstudio.winning import current_patterns
    conn = common.db()
    p = _product_dict(conn, pid)
    if p is None:
        conn.close()
        return jsonify({"error": "not found"}), 404
    shop = catalog.shop(p)
    fam = catalog.family(p)
    facts = [dict(r) for r in conn.execute(
        "SELECT id, category, text, source, source_url, confidence, status, origin, note, family FROM facts"
        " WHERE (product_id=? OR (product_id IS NULL AND family<>'' AND family=?)) AND active=1 ORDER BY CASE status WHEN 'pending' THEN 0 WHEN 'approved' THEN 1"
        " WHEN 'do_not_use' THEN 2 ELSE 3 END, id", (pid, fam or None))]
    photos = [dict(r) for r in conn.execute("SELECT id, is_primary, is_hero, tag, role FROM adstudio_photos"
                                            " WHERE product_id=? ORDER BY is_hero DESC, id", (pid,))]
    patterns = current_patterns(conn, limit=4)
    out = {"readiness": hub.readiness(p, hub.stats(conn, pid).get(pid, {})), "facts": facts, "photos": photos,
           "research": hub.latest_research(conn, pid), "research_estimate": hub.estimate(conn),
           "fill": {"status": p["fill_status"], "step": p["fill_step"], "error": p["fill_error"],
                    "cost_usd": p["fill_cost_usd"]}, "fill_estimate": hub.fill_estimate(conn), "family": fam,
           "shop": {"type": shop.get("type", ""), "url": p.get("shopify_url") or "", "tags": shop.get("tags", []),
                    "images": len(shop.get("images", []))},
           "in_store": p["origin"] == "shopify" and not p["archived"], "categories": CATEGORIES, "tags": hub.TAGS,
           "patterns": [{"id": x["id"], "label": x["label"], "roas": x.get("roas_lift"), "spend": x.get("total_spend")}
                        for x in patterns]}
    conn.close()
    out["prices"], out["prices_live"] = _live_prices(p, shop) if out["in_store"] else ([], False)
    return jsonify(out)


@bp.post("/api/adstudio/product/<int:pid>/import-photos")
def adstudio_import_photos(pid: int) -> Any:
    """Copy the product's Shopify photos in as reference photos (up to 6; skips when it has photos)."""
    from creative_intelligence.adstudio import hub
    conn = common.db()
    p = _product_dict(conn, pid)
    if p is None:
        conn.close()
        return jsonify({"error": "not found"}), 404
    added = hub.import_shop_photos(conn, p)
    conn.close()
    return jsonify({"added": added})


@bp.post("/api/adstudio/product/<int:pid>/fill")
def adstudio_fill(pid: int) -> Any:
    """Fill whatever is empty in the profile again (photos, look, research, customer language, hooks)."""
    from creative_intelligence.adstudio import hub
    conn = common.db()
    if _product_dict(conn, pid) is None:
        conn.close()
        return jsonify({"error": "not found"}), 404
    job = hub.queue_fill(conn, pid)
    conn.close()
    return jsonify({"job_id": job})


@bp.post("/api/adstudio/photo/<int:photo_id>/tag")
def adstudio_photo_tag(photo_id: int) -> Any:
    from creative_intelligence.adstudio import hub
    tag = (request.get_json(force=True, silent=True) or {}).get("tag", "")
    if tag and tag not in hub.TAGS:
        return jsonify({"error": "bad tag"}), 400
    conn = common.db()
    hub.set_tag(conn, photo_id, tag)
    conn.close()
    return jsonify({"ok": True})


@bp.post("/api/adstudio/photo/<int:photo_id>/hero")
def adstudio_photo_hero(photo_id: int) -> Any:
    from creative_intelligence.adstudio import hub
    on = bool((request.get_json(force=True, silent=True) or {}).get("hero"))
    conn = common.db()
    why = hub.set_hero(conn, photo_id, on)
    conn.close()
    return (jsonify({"error": why}), 400) if why else jsonify({"ok": True})


@bp.post("/api/adstudio/product/<int:pid>/draft-look")
def adstudio_draft_look(pid: int) -> Any:
    """Claude writes the look from the photos and tags them (hand-set tags and heroes stay)."""
    from creative_intelligence.adstudio import hub
    conn = common.db()
    p = _product_dict(conn, pid)
    if p is None:
        conn.close()
        return jsonify({"error": "not found"}), 404
    try:
        return jsonify(hub.draft_look(conn, p))
    except Exception as exc:
        log.exception("draft-look failed")
        return jsonify({"error": str(exc)[:300]}), 400
    finally:
        conn.close()


@bp.post("/api/adstudio/product/<int:pid>/research")
def adstudio_research(pid: int) -> Any:
    """Body {confirm: true} starts research; without it, returns the estimate only."""
    from creative_intelligence.adstudio import hub
    d = request.get_json(force=True, silent=True) or {}
    conn = common.db()
    if _product_dict(conn, pid) is None:
        conn.close()
        return jsonify({"error": "not found"}), 404
    if not d.get("confirm"):
        est = hub.estimate(conn)
        conn.close()
        return jsonify({"estimate_usd": est})
    rid, why = hub.start_research(conn, pid)
    conn.close()
    return (jsonify({"error": why}), 400) if rid is None else jsonify({"research_id": rid})


@bp.get("/api/adstudio/product/<int:pid>/research")
def adstudio_research_status(pid: int) -> Any:
    from creative_intelligence.adstudio import hub
    conn = common.db()
    out = {"research": hub.latest_research(conn, pid)}
    conn.close()
    return jsonify(out)


@bp.post("/api/adstudio/product/<int:pid>/fact")
def adstudio_add_fact(pid: int) -> Any:
    """A fact the operator adds by hand is approved at once."""
    from creative_intelligence.adstudio.facts import CATEGORIES
    d = request.get_json(force=True, silent=True) or {}
    text, cat = (d.get("text") or "").strip(), d.get("category") or "other"
    if not text or cat not in CATEGORIES:
        return jsonify({"error": "text and a known category needed"}), 400
    if cat in ("nutrition", "health") and not (d.get("source_url") or d.get("source") or "").strip():
        return jsonify({"error": "Nutrition and health facts need a source."}), 400
    conn = common.db()
    with conn:
        fid = conn.execute("INSERT INTO facts (product_id, category, text, source, source_url, status) VALUES"
                           " (?,?,?,?,?, 'approved')", (pid, cat, text, (d.get("source") or "").strip(),
                                                        (d.get("source_url") or "").strip())).lastrowid
    conn.close()
    return jsonify({"id": fid})


@bp.post("/api/adstudio/fact/<int:fid>")
def adstudio_fact_action(fid: int) -> Any:
    """Body {status: pending|approved|rejected|do_not_use} and/or {text}."""
    from creative_intelligence.adstudio.facts import STATUSES
    d = request.get_json(force=True, silent=True) or {}
    sets, vals = [], []
    if "status" in d:
        if d["status"] not in STATUSES:
            return jsonify({"error": "bad status"}), 400
        sets.append("status=?"); vals.append(d["status"])
    if (d.get("text") or "").strip():
        sets.append("text=?"); vals.append(d["text"].strip())
    if not sets:
        return jsonify({"error": "nothing to change"}), 400
    conn = common.db()
    with conn:
        n = conn.execute(f"UPDATE facts SET {', '.join(sets)} WHERE id=? AND (product_id IS NOT NULL OR family<>'')",
                         (*vals, fid)).rowcount
    conn.close()
    return jsonify({"ok": bool(n)})


@bp.post("/api/adstudio/product/<int:pid>/facts/approve")
def adstudio_facts_approve(pid: int) -> Any:
    """Bulk approve: the given ids, or every pending fact shown on the profile (the product's and its
    fruit family's). Never touches 'do not use'."""
    from creative_intelligence.adstudio import catalog
    ids = [int(x) for x in (request.get_json(force=True, silent=True) or {}).get("ids") or [] if str(x).isdigit()]
    conn = common.db()
    fam = catalog.family(_product_dict(conn, pid) or {}) or None
    scope = "(product_id=? OR (product_id IS NULL AND family<>'' AND family=?)) AND status='pending'"
    with conn:
        if ids:
            n = conn.execute(f"UPDATE facts SET status='approved' WHERE {scope} AND id IN ({','.join('?' * len(ids))})",
                             (pid, fam, *ids)).rowcount
        else:
            n = conn.execute(f"UPDATE facts SET status='approved' WHERE {scope}", (pid, fam)).rowcount
    conn.close()
    return jsonify({"approved": n})


def _brand_ua() -> str:
    from creative_intelligence import brand
    return brand.user_agent()
