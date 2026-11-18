"""
The visual board (feedback 2026-10-22: "we actually want to see visuals that we can reuse on our products").

One grid of pictures to work from:
  ours        our best Meta ads by return on spend, from the read-only sync's creative thumbnails (cached on the volume)
  winners     our decoded winning videos (a frame of each)
  competitor  competitor ads added by screenshot (or, once Meta allows it, the Ad Library API)
  swipe       anything saved to the swipe file with a picture
"Make ours like this" turns any of them into AI-designed ads for a product: the picture guides composition, light,
layout and mood; the product comes from our photos and the words from an approved hook (spell-checked as usual).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import requests

KINDS = {"ours": "Our best ads", "winners": "Our winning videos", "organic": "Our Instagram posts",
         "competitor": "Competitor ads", "swipe": "Saved ads"}
OURS_LIMIT = 120                 # feedback 2026-10-27: "we should be able to see more of our winners"
LIKE_VARIANTS = ("bold_type", "lifestyle_tagline", "scroll_stopping", "premium")
LIKE_MAX = 4
LIKE_RULE = ("Use the LAST image only as a reference for composition, camera angle, lighting, layout and mood. "
             "Do not copy anything else from it: no brand, logo, fonts, colours, words, people or product. The product "
             "is ours, exactly as in the first image(s).")


def _cache_dir() -> Path:
    from creative_intelligence.storage import storage
    d = Path(storage().area_dir("research")) / "visuals"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ours(conn) -> list[dict[str, Any]]:
    """Our ads with the best return on spend (at least $50 spent) that the latest sync has a thumbnail for."""
    from creative_intelligence.ingest import ads_reader
    raw = {c["id"]: c for c in ads_reader._load("creatives") if c.get("id") and c.get("thumbnail_url")}
    rows = conn.execute(
        "SELECT p.creative_id, c.ad_name, c.format, c.headline, c.primary_text, c.hook_text,"
        " SUM(p.spend) AS spend, SUM(p.revenue) AS revenue, SUM(p.purchases) AS buys"
        " FROM creative_performance p LEFT JOIN creatives c ON c.id=p.creative_id GROUP BY p.creative_id"
        " HAVING SUM(p.spend) >= 50 ORDER BY SUM(p.revenue) / SUM(p.spend) DESC").fetchall()
    out = []
    for r in rows:
        if r["creative_id"] in raw:
            roas = (r["revenue"] or 0) / (r["spend"] or 1)
            out.append({"kind": "ours", "id": r["creative_id"], "title": r["ad_name"] or r["creative_id"],
                        "note": f"ROAS {roas:.1f} · ${r['spend']:.0f} spent · {int(r['buys'] or 0)} purchases",
                        "image": f"/api/adstudio/visuals/ours/{r['creative_id']}/image", "format": r["format"] or "",
                        "text": " ".join(x for x in (r["headline"], r["hook_text"] or r["primary_text"]) if x)[:600],
                        "roas": round(roas, 2), "spend": round(r["spend"] or 0), "buys": int(r["buys"] or 0)})
        if len(out) >= OURS_LIMIT:
            break
    return out


def listing(conn, kind: str = "") -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if kind in ("", "ours"):
        try:
            items += _ours(conn)
        except Exception:
            pass                                              # no sync yet: the rest still shows
    if kind in ("", "winners"):
        items += [{"kind": "winners", "id": r["id"], "title": r["ad_name"] or f"Winner {r['id']}", "note": "decoded winning video",
                   "image": f"/api/adstudio/visuals/winners/{r['id']}/image"}
                  for r in conn.execute("SELECT id, ad_name FROM video_templates WHERE origin='own' AND video_path<>''"
                                        " ORDER BY id DESC")]
    if kind in ("", "organic"):
        items += [{"kind": "organic", "id": r["id"], "title": (r["caption"] or "Instagram post").split("\n")[0][:80],
                   "note": " · ".join(x for x in (r["media_type"].replace("_", " ").lower(), f"{r['likes']} likes",
                                                  f"{r['comments']} comments", f"reach {r['reach']}" if r["reach"] else "",
                                                  (r["posted_at"] or "")[:10]) if x),
                   "image": f"/api/adstudio/visuals/organic/{r['id']}/image", "text": (r["caption"] or "")[:600],
                   "link": r["permalink"], "likes": r["likes"], "reach": r["reach"] or 0}
                  for r in conn.execute("SELECT * FROM ig_posts WHERE media_path<>'' ORDER BY posted_at DESC LIMIT 120")]
    if kind in ("", "competitor"):
        items += [{"kind": "competitor", "id": r["id"], "title": r["page_name"] or "Competitor ad",
                   "note": " · ".join(x for x in (r["format"], r["angle"], (r["why_it_works"] or "")[:90]) if x),
                   "image": f"/api/adstudio/market/ad/{r['id']}/media",
                   "text": " ".join(x for x in (r["headline"], r["body"], r["angle"]) if x)[:600]}
                  for r in conn.execute("SELECT id, page_name, format, angle, why_it_works, headline, body FROM competitor_ads"
                                        " WHERE media_path<>'' AND media_kind<>'video' ORDER BY id DESC LIMIT 120")]
    if kind in ("", "swipe"):
        items += [{"kind": "swipe", "id": r["id"], "title": r["notes"] or "Saved ad", "note": r["kind"].replace("_", " "),
                   "image": f"/api/adstudio/swipe/{r['id']}/image", "text": r["notes"] or ""}
                  for r in conn.execute("SELECT id, kind, notes FROM swipe_items WHERE media_path<>'' ORDER BY id DESC LIMIT 120")]
    return items


def image_path(conn, kind: str, ref: str) -> Path | None:
    """The picture on disk (fetched and cached the first time for our ads and winners)."""
    if kind == "ours":
        p = _cache_dir() / f"ours_{hashlib.sha1(ref.encode()).hexdigest()[:16]}.jpg"
        if not p.exists():
            from creative_intelligence.ingest import ads_reader
            url = next((c.get("thumbnail_url") for c in ads_reader._load("creatives") if c.get("id") == ref), None)
            if not url:
                return None
            r = requests.get(url, timeout=20)                 # our own ad's thumbnail on Meta's CDN (read only)
            if r.status_code != 200 or not r.content:
                return None
            p.write_bytes(r.content)
        return p
    if kind == "winners":
        row = conn.execute("SELECT video_path FROM video_templates WHERE id=?", (int(ref),)).fetchone()
        if not row or not Path(row[0] or "").exists():
            return None
        p = _cache_dir() / f"winner_{int(ref)}.jpg"
        if not p.exists():
            from creative_intelligence.adstudio import videotools
            videotools.run(["-ss", "1", "-i", row[0], "-frames:v", "1", "-q:v", "3", str(p)])
        return p
    if kind == "organic":
        row = conn.execute("SELECT media_path FROM ig_posts WHERE id=?", (str(ref),)).fetchone()
    elif kind == "competitor":
        row = conn.execute("SELECT media_path FROM competitor_ads WHERE id=?", (int(ref),)).fetchone()
    elif kind == "swipe":
        row = conn.execute("SELECT media_path FROM swipe_items WHERE id=?", (int(ref),)).fetchone()
    else:
        return None
    return Path(row[0]) if row and row[0] and Path(row[0]).exists() else None


def make_like(conn, kind: str, ref: str, product_id: int, n: int = 2, hook_ids: list[int] | None = None,
              size: str = "4:5") -> tuple[list[int], str]:
    """Queue n AI-designed ads for the product, guided by the picture. Hooks: the ones given, else the best saved."""
    from creative_intelligence import budget
    from creative_intelligence.adstudio import batch
    from creative_intelligence.adstudio.textads import VARIANTS
    path = image_path(conn, kind, ref)
    if path is None:
        return [], "That picture isn't available."
    hooks = batch._hooks(conn, product_id)
    if hook_ids:
        hooks = [h for h in hooks if h["id"] in set(hook_ids)] or hooks
    if not hooks:
        return [], "Write some hooks for this product first (Products › Hooks)."
    n = max(1, min(LIKE_MAX, int(n)))
    try:
        budget.check(conn, "generation", n * batch.ai_price(conn))
    except budget.BudgetExceeded as exc:
        return [], str(exc)
    rotation = [k for k in LIKE_VARIANTS if k in VARIANTS]          # text formats, not the no-text Minimal
    label = next((i["title"] for i in listing(conn, kind) if str(i["id"]) == str(ref)), kind)
    with conn:
        brief = conn.execute("INSERT INTO adstudio_briefs (product_id, idea, brief_json) VALUES (?,?,?)",
                             (product_id, f"Like this: {label}"[:200], json.dumps({"angle": "", "hook": "", "cta": "",
                                                                                   "primary_texts": [], "variants": {}}))).lastrowid
    ids = []
    for i in range(n):
        h = hooks[i % len(hooks)]
        with conn:
            ids.append(conn.execute(
                "INSERT INTO adstudio_textads (product_id, brief_id, kind, variant, title, headline, subhead, cta,"
                " style_seed, aspect_ratio, text_mode, ref_path, ref_label) VALUES (?,?, 'variant', ?,?,?,?,?,?,?,"
                " 'painted', ?, ?)",
                (product_id, brief, rotation[i % len(rotation)], f"Like: {label}"[:80], h["headline"], h.get("subhead") or "",
                 h.get("cta") or "", i, size, str(path), label[:120])).lastrowid)
    return ids, ""


# ── Repurpose (feedback 2026-10-27: "a clear way to select things to repurpose and use on ads") ──
ACTIONS = {"statics": "Static ads like these", "hooks": "Hooks from their angle", "idea": "Save as an idea",
           "video": "A video from this winner's structure"}


def repurpose(conn, items: list[dict[str, Any]], product_id: int, action: str, n: int = 2,
              size: str = "4:5") -> dict[str, Any]:
    """Selected pictures (ours, winners, Instagram, competitors, saved) turned into something for a product:
    statics guided by each picture, hooks written from their angle (never their words or claims), an idea in the
    bank pointing back at them, or the "From a winning ad" video for a decoded winner."""
    from creative_intelligence.webapp.common import _product_dict
    product = _product_dict(conn, product_id)
    if product is None:
        return {"error": "Pick a product."}
    picked = [i for i in listing(conn) if any(str(i["id"]) == str(s.get("id")) and i["kind"] == s.get("kind")
                                                 for s in items)]
    if not picked:
        return {"error": "Select at least one picture."}
    if action == "statics":
        ids: list[int] = []
        for it in picked[:8]:
            got, why = make_like(conn, it["kind"], str(it["id"]), product_id, n, None, size)
            if why:
                return {"error": why, "queued": len(ids)}
            ids += got
        return {"queued": len(ids), "ids": ids, "where": "Gallery"}
    if action == "hooks":
        from creative_intelligence.adstudio import hub
        refs = "\n".join(f"- {it['title']}: {it.get('text') or it.get('note') or ''}"[:300] for it in picked[:8])
        idea = ("Take the angle and structure of these ads and posts and make them ours for this product. Never copy "
                "their words, claims, numbers, brand or people; only what the approved facts say:\n" + refs)
        return {"written": hub.auto_hooks(conn, product, idea=idea), "where": "the product's hooks"}
    if action == "idea":
        from creative_intelligence.adstudio import ideas
        comp = [int(it["id"]) for it in picked if it["kind"] == "competitor"]
        notes = "; ".join(f"{KINDS.get(it['kind'], it['kind'])}: {it['title']}" for it in picked)[:900]
        iid, why = ideas.create(conn, {"title": f"Like: {picked[0]['title']}"[:80], "product_ids": [product_id],
                                       "why": "Repurposed from the visual library.",
                                       "evidence": {"competitor_ad_ids": comp, "notes": notes}},
                                source="swipe", source_ref=",".join(f"{it['kind']}:{it['id']}" for it in picked)[:200])
        return {"error": why} if why else {"idea_id": iid, "where": "Idea bank"}
    if action == "video":
        win = [it for it in picked if it["kind"] == "winners"]
        if len(win) != 1:
            return {"error": "Pick one of our winning videos: its structure is rebuilt for the product."}
        return {"goto": f"/video?product_id={product_id}&style=template&template_id={win[0]['id']}"}
    return {"error": "Unknown action."}
