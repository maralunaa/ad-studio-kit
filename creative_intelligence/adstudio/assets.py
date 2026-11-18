"""
Asset registry — one row per finished ad, so every ad can be matched to its own Meta results.

  Asset ID    one per final ad, built from config.ASSET_ID_TEMPLATE, e.g.
              EXB_B3_authenticity_quality-authent_bold-type_pink-elephant-mango_v01
              The agency uses it as the Meta ad name; the sync links them up.
  Concept ID  shared by the variants of one idea: an AI Ads brief (B<id>), a hook used in
              Compose (H<id>), or a Core shot kind (K-<kind>).

Sources: AI Ads (adstudio_textads), Compose (adstudio_composites) and Core
(adstudio_core). Rows are created lazily by ensure_assets() for every finished ad,
so existing work gets IDs too. Asset IDs never change once issued.

Results come from ad_daily_insights (T1), summed over every day stored for the
linked Meta ad. Hook rate = 3-second views / impressions; hold rate = thruplays /
3-second views (video only).

  python -m creative_intelligence.adstudio.assets link   # run by the Meta sync
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import datetime
from typing import Any

from creative_intelligence import config
from creative_intelligence.adstudio import diagnosis

SOURCES = {
    # source key: (table, image column, tab label)
    "textad": ("adstudio_textads", "image_path", "AI Ads"),
    "composite": ("adstudio_composites", "output_path", "Compose"),
    "core": ("adstudio_core", "image_path", "Core"),
    "video_render": ("adstudio_video_renders", "output_path", "Video"),
}
GROUPS = ("angle", "hook_type", "format", "style", "product")


def part(text: Any, n: int = 18) -> str:
    """One Asset ID segment: lowercase letters, digits and hyphens only (underscores separate segments)."""
    s = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    return (s[:n].rstrip("-")) or "na"


def pattern_tags(pattern_name: str | None) -> dict[str, str]:
    """'hook_type × angle: hook_type:social_proof + angle:fear' -> {'hook_type': 'social_proof', 'angle': 'fear'}."""
    body = (pattern_name or "").split(": ", 1)[-1]
    return dict(re.findall(r"([a-z_]+):([a-z_]+)", body))


def _pattern_name(conn, pattern_id: Any) -> str | None:
    if not pattern_id:
        return None
    r = conn.execute("SELECT pattern_name FROM creative_patterns WHERE id=?", (pattern_id,)).fetchone()
    return r["pattern_name"] if r else None


def _prompt_version(prompt: str | None, recorded: str | None = None) -> str:
    """The prompt files and versions that built it (prompts.stamp); older rows: a hash of the prompt."""
    if recorded:
        return recorded
    return hashlib.sha1((prompt or "").encode()).hexdigest()[:8] if prompt else ""


def _cost(conn, sources: list[tuple[str, int]]) -> float:
    total = 0.0
    for src, sid in sources:
        r = conn.execute("SELECT COALESCE(SUM(cost_usd),0) FROM model_calls WHERE source=? AND source_id=?",
                         (src, sid)).fetchone()
        total += r[0] or 0.0
    return round(total, 4)


def describe(conn, source: str, row: dict[str, Any], product: dict[str, Any]) -> dict[str, Any]:
    """Concept, ID parts and tags for one finished source row."""
    ptype = product.get("product_type") or "other"
    tags: dict[str, Any] = {"product": product.get("name", ""), "product_type": ptype,
                            "source_tab": SOURCES[source][2], "ai_generated": 1}
    if source == "textad":
        brief = conn.execute("SELECT pattern_id, brief_json, idea_id FROM adstudio_briefs WHERE id=?",
                             (row["brief_id"],)).fetchone()
        tags["idea_id"] = brief["idea_id"] if brief else None
        pt = pattern_tags(_pattern_name(conn, brief["pattern_id"] if brief else None))
        concept = f"B{row['brief_id']}"
        fmt = row["variant"] if row.get("kind") == "variant" else "scene"
        tags.update(angle=pt.get("angle", ""), hook_type=pt.get("hook_type", ""), format=fmt,
                    style=row.get("text_layout") or "", image_model=row.get("model") or "",
                    prompt_version=_prompt_version(row.get("prompt"), row.get("prompt_version")))
        cost_src = [("textad", row["id"])]
    elif source == "composite":
        hook = conn.execute("SELECT archetype, hook_type, source, pattern_id, idea_id FROM adstudio_hooks WHERE id=?",
                            (row.get("hook_id"),)).fetchone() if row.get("hook_id") else None
        img_idea = conn.execute("SELECT p.idea_id FROM adstudio_images i JOIN adstudio_images p"
                                " ON p.id=COALESCE(i.parent_image_id, i.id) WHERE i.id=?", (row["image_id"],)).fetchone()
        tags["idea_id"] = (hook["idea_id"] if hook else None) or (img_idea[0] if img_idea else None)
        pt = pattern_tags(_pattern_name(conn, hook["pattern_id"] if hook else None))
        img = conn.execute("SELECT model, prompt, core_id, prompt_version FROM adstudio_images WHERE id=?",
                           (row["image_id"],)).fetchone()
        layout = json.loads(row.get("layout_json") or "{}")
        concept = f"H{row['hook_id']}" if row.get("hook_id") else f"I{row['image_id']}"
        tags.update(angle=pt.get("angle", ""),
                    hook_type=(hook["hook_type"] if hook else "") or pt.get("hook_type") or (hook["archetype"] if hook else ""),
                    format="compose-" + (layout.get("template") or layout.get("position") or "top"),
                    style=f"{layout.get('position', 'top')}/{layout.get('color', 'auto')}",
                    image_model=(img["model"] if img else "") or "",
                    prompt_version=_prompt_version(img["prompt"] if img else "", img["prompt_version"] if img else None))
        cost_src = [("image", row["image_id"])]
        if img and img["core_id"]:
            cost_src.append(("core", img["core_id"]))
    elif source == "video_render":
        v = conn.execute("SELECT * FROM adstudio_videos WHERE id=?", (row["video_id"],)).fetchone()
        hook = conn.execute("SELECT archetype, hook_type, pattern_id FROM adstudio_hooks WHERE id=?",
                            (row.get("hook_id"),)).fetchone() if row.get("hook_id") else None
        pt = pattern_tags(_pattern_name(conn, hook["pattern_id"] if hook else None))
        tags["idea_id"] = v["idea_id"] if v else None
        concept = f"V{row['video_id']}"                       # every version of one clip: one concept
        tags.update(angle=pt.get("angle", ""),
                    hook_type=(hook["hook_type"] if hook else "") or pt.get("hook_type") or (hook["archetype"] if hook else ""),
                    format=f"{(v['style'] if v else 'animate')}-{row['aspect'].replace(':', 'x')}",
                    style=(v["style"] if v else "animate") + (f"/{mode}" if (mode := _video_mode(v)) else ""), image_model=(v["model"] if v else "") or "",
                    prompt_version=(v["prompt_version"] if v else "") or "")
        cost_src = [("video", row["video_id"])]
    else:  # core
        concept = f"K-{row['kind']}"
        tags.update(angle="", hook_type="", format=row["kind"], style="", image_model=config.CI_IMAGE_MODEL,
                    prompt_version=_prompt_version(row.get("prompt"), row.get("prompt_version")))
        cost_src = [("core", row["id"])]
    tags["cost_usd"] = _cost(conn, cost_src)
    return {"concept_id": concept, "tags": tags}


def _video_mode(v: Any) -> str:
    """A styled video's mode (Fruit Story: presenter or narrated), so Results can compare the two."""
    try:
        return (json.loads(v["copy_json"] or "{}") if v else {}).get("mode") or ""
    except (ValueError, TypeError, IndexError, KeyError):
        return ""


def build_asset_id(template: str, concept: str, tags: dict[str, Any], product_name: str, version: int) -> str:
    return template.format(concept=part(concept, 12), angle=part(tags.get("angle")),
                           hook=part(tags.get("hook_type")), format=part(tags.get("format")),
                           product=part(product_name, 24), version=version)


def ensure_assets(conn, product_id: int | None = None) -> int:
    """Register every finished ad that has no asset row yet. Returns how many were added."""
    template = config.ASSET_ID_TEMPLATE
    added = 0
    products = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM adstudio_products")}
    for source, (table, img_col, _) in SOURCES.items():
        sql = (f"SELECT s.* FROM {table} s WHERE s.{img_col} IS NOT NULL"
               f" AND NOT EXISTS (SELECT 1 FROM adstudio_assets a WHERE a.source=? AND a.source_id=s.id)")
        params: list[Any] = [source]
        if source in ("textad", "core", "video_render"):
            sql += " AND s.status='done'"
        if product_id is not None:
            sql += " AND s.product_id=?"; params.append(product_id)
        for r in conn.execute(sql + " ORDER BY s.id", params).fetchall():
            row = dict(r)
            prod = products.get(row["product_id"], {"name": "product"})
            d = describe(conn, source, row, prod)
            t = d["tags"]
            # version = how many ads of this concept + format + product already have IDs, plus one
            version = conn.execute(
                "SELECT COUNT(*) FROM adstudio_assets WHERE concept_id=? AND format=? AND product_id=?",
                (d["concept_id"], t["format"], row["product_id"])).fetchone()[0] + 1
            asset_id = build_asset_id(template, d["concept_id"], t, prod.get("name", ""), version)
            while conn.execute("SELECT 1 FROM adstudio_assets WHERE asset_id=?", (asset_id,)).fetchone():
                version += 1                         # never reuse an ID, whatever the template
                asset_id = build_asset_id(template, d["concept_id"], t, prod.get("name", ""), version)
            with conn:
                conn.execute(
                    "INSERT INTO adstudio_assets (asset_id, concept_id, product_id, source, source_id,"
                    " angle, hook_type, format, style, product, product_type, source_tab, image_model,"
                    " prompt_version, run_id, cost_usd, ai_generated, idea_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (asset_id, d["concept_id"], row["product_id"], source, row["id"], t["angle"], t["hook_type"],
                     t["format"], t["style"], t["product"], t["product_type"], t["source_tab"], t["image_model"],
                     t["prompt_version"], row.get("run_id"), t["cost_usd"], 1, t.get("idea_id")))
            added += 1
    return added


def assets_for(conn, source: str, ids: list[int]) -> dict[int, dict[str, Any]]:
    """{source_id: asset dict with results} for a gallery's rows."""
    if not ids:
        return {}
    out: dict[int, dict[str, Any]] = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for r in conn.execute(
                f"SELECT * FROM adstudio_assets WHERE source=? AND source_id IN ({','.join('?' * len(chunk))})",
                [source, *chunk]):
            a = dict(r)
            a["results"] = results(conn, a["meta_ad_id"]) if a["meta_ad_id"] else None
            out[a["source_id"]] = a
    if source == "video_render" and out:
        lengths = {r["id"]: r["duration_s"] for r in conn.execute(
            f"SELECT id, duration_s FROM adstudio_video_renders WHERE id IN ({','.join('?' * len(out))})", list(out))}
    else:
        lengths = {}
    for sid, a in out.items():
        if a["results"]:
            a["results"]["diagnosis"] = diagnosis.hint(a["results"], lengths.get(sid))
    return out


def results(conn, meta_ad_id: str) -> dict[str, Any] | None:
    r = conn.execute(
        "SELECT COUNT(*) AS days, MIN(date) AS since, MAX(date) AS until, SUM(spend) AS spend,"
        " SUM(impressions) AS impressions, SUM(clicks) AS clicks, SUM(purchases) AS purchases,"
        " SUM(revenue) AS revenue, SUM(video_3s_views) AS v3, SUM(thruplays) AS thru, SUM(video_15s_views) AS v15"
        " FROM ad_daily_insights WHERE ad_id=?", (meta_ad_id,)).fetchone()
    if not r or not r["days"]:
        return None
    return metrics(dict(r))


def metrics(r: dict[str, Any]) -> dict[str, Any]:
    spend, imp, clicks = r.get("spend") or 0, r.get("impressions") or 0, r.get("clicks") or 0
    pur, rev, v3, thru = r.get("purchases") or 0, r.get("revenue") or 0, r.get("v3") or 0, r.get("thru") or 0
    v15 = r.get("v15") or 0                      # true 15-second views when the sync has them (playbook hold rate)
    held = v15 or thru
    return {
        "days": r.get("days"), "since": r.get("since"), "until": r.get("until"),
        "spend": round(spend, 2), "impressions": imp, "purchases": pur, "revenue": round(rev, 2),
        "ctr": round(clicks / imp * 100, 2) if imp else None,
        "cpa": round(spend / pur, 2) if pur else None,
        "roas": round(rev / spend, 2) if spend else None,
        "hook_rate": round(v3 / imp * 100, 2) if v3 and imp else None,
        "hold_rate": round(held / v3 * 100, 2) if held and v3 else None,
        "hold_basis": "15s" if v15 else "thruplay",
        "clicks": clicks,
    }


def link_from_meta(conn) -> int:
    """Link unlinked assets to the Meta ad whose name equals or contains their Asset ID."""
    linked = 0
    ads = conn.execute("SELECT ad_id, ad_name FROM creatives WHERE ad_name IS NOT NULL AND ad_name != ''"
                       " UNION SELECT ad_id, ad_name FROM ad_daily_insights WHERE ad_name IS NOT NULL").fetchall()
    names = [(a["ad_id"], a["ad_name"].lower()) for a in ads]
    for a in conn.execute("SELECT id, asset_id FROM adstudio_assets WHERE meta_ad_id IS NULL").fetchall():
        key = a["asset_id"].lower()
        hit = next((ad_id for ad_id, name in names if name == key), None) or \
            next((ad_id for ad_id, name in names if key in name), None)
        if hit:
            with conn:
                conn.execute("UPDATE adstudio_assets SET meta_ad_id=?, linked_by='auto', linked_at=? WHERE id=?",
                             (str(hit), datetime.utcnow().isoformat(), a["id"]))
            linked += 1
    return linked


def link_manual(conn, asset_id: str, meta_ad_id: str | None) -> bool:
    """Set (or clear, with None) the Meta ad for an asset. Returns False if the asset doesn't exist."""
    with conn:
        cur = conn.execute(
            "UPDATE adstudio_assets SET meta_ad_id=?, linked_by=?, linked_at=? WHERE asset_id=?",
            (meta_ad_id or None, "manual" if meta_ad_id else None,
             datetime.utcnow().isoformat() if meta_ad_id else None, asset_id))
    return cur.rowcount > 0


def aggregate(conn, group: str, product_id: int | None = None) -> list[dict[str, Any]]:
    """Results grouped by angle / hook_type / format / style / product, best ROAS first."""
    if group not in GROUPS:
        raise ValueError(f"group must be one of {GROUPS}")
    where, params = "", []
    if product_id is not None:
        where, params = " WHERE a.product_id=?", [product_id]
    rows = conn.execute(
        f"SELECT COALESCE(NULLIF(a.{group},''),'(none)') AS k, COUNT(DISTINCT a.id) AS assets,"
        " COUNT(DISTINCT a.meta_ad_id) AS linked, SUM(a.cost_usd) AS cost"
        f" FROM adstudio_assets a{where} GROUP BY k", params).fetchall()
    out = []
    for r in rows:
        m = conn.execute(
            f"SELECT COUNT(DISTINCT i.date) AS days, SUM(i.spend) AS spend, SUM(i.impressions) AS impressions,"
            " SUM(i.clicks) AS clicks, SUM(i.purchases) AS purchases, SUM(i.revenue) AS revenue,"
            " SUM(i.video_3s_views) AS v3, SUM(i.thruplays) AS thru, SUM(i.video_15s_views) AS v15"
            " FROM ad_daily_insights i WHERE i.ad_id IN (SELECT a.meta_ad_id FROM adstudio_assets a"
            f" WHERE a.meta_ad_id IS NOT NULL AND COALESCE(NULLIF(a.{group},''),'(none)')=?"
            + (" AND a.product_id=?" if product_id is not None else "") + ")",
            [r["k"], *params]).fetchone()
        res = metrics(dict(m))
        out.append({"key": r["k"], "assets": r["assets"], "linked": r["linked"],
                    "generation_cost": round(r["cost"] or 0, 2), **res, "diagnosis": diagnosis.hint(res)})
    out.sort(key=lambda x: (x["roas"] is None, -(x["roas"] or 0), -x["assets"]))
    return out


if __name__ == "__main__":
    if sys.argv[1:] == ["link"]:
        from creative_intelligence.db import get_connection, init_db
        init_db()
        c = get_connection()
        added = ensure_assets(c)
        n = link_from_meta(c)
        from creative_intelligence.adstudio import ideas
        ideas.sync_status(c)
        c.close()
        print(f"[OK] {added} new assets registered, {n} linked to Meta ads")
    else:
        print("usage: python -m creative_intelligence.adstudio.assets link")
        sys.exit(2)
