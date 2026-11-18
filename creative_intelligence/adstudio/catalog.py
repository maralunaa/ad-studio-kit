"""
Product Hub catalogue: every product in the Shopify store, kept in adstudio_products.

Read from the store's public product feed (/products.json, the same data as the shop pages; no
token, read-only). A daily job (scheduler) and the "Sync now" button run sync():

  listed      not tagged hidden / exclude-cart / buildabox (the build-a-box twins) and not a gift card
  type        "Fruit: …" → fruit · "Mix Box: …" → gift_box · "Candle: …" → candle · anything else → other
              (only for products the sync creates; a type set by hand is kept)
  stock       in stock when any variant is available
  season      tag c.abundant → in season; c.seasonal or preorder → out of season
  linking     a product made before the hub is linked by its Shopify URL, else by its exact name
  archiving   a product that leaves the feed is archived, never deleted, and comes back if it returns.
              Nothing is archived when the feed looks cut short (under half the products we know).
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from typing import Any, Callable

from creative_intelligence import brand as _brand

log = logging.getLogger("adstudio.catalog")

def _tagset(key: str) -> set[str]:
    """Store conventions from the brand pack (brand.yaml › store), read on each call."""
    return {t.lower() for t in _brand.get(f"store.{key}", [])}
PAGE = 250
MAX_PAGES = 20


def store_url() -> str:
    return (os.getenv("SHOPIFY_PUBLIC_URL") or _brand.get("store.public_url") or "").rstrip("/")


def fetch_feed(get: Callable[..., Any] | None = None) -> list[dict[str, Any]]:
    """Every product in the public feed, page by page."""
    import requests
    from creative_intelligence.adstudio.costs import _api_log
    get = get or requests.get
    out: list[dict[str, Any]] = []
    for page in range(1, MAX_PAGES + 1):
        url = f"{store_url()}/products.json?limit={PAGE}&page={page}"
        r = get(url, timeout=30, headers={"User-Agent": _brand.user_agent()})
        r.raise_for_status()
        _api_log(f"{datetime.utcnow().isoformat()} | SHOPIFY | GET {url} | {r.status_code}")
        batch = r.json().get("products") or []
        out += batch
        if len(batch) < PAGE:
            break
    return out


def _tags(p: dict[str, Any]) -> set[str]:
    t = p.get("tags") or []
    if isinstance(t, str):
        t = t.split(",")
    return {x.strip().lower() for x in t if x.strip()}


def is_listed(p: dict[str, Any]) -> bool:
    kind = (p.get("product_type") or "").lower().replace("_", " ")
    return not (_tags(p) & _tagset("hidden_tags")) and "gift card" not in kind


def map_type(product_type: str) -> str:
    head = (product_type or "").split(":")[0].strip().lower()
    return (_brand.get("store.type_map") or {}).get(head, "other")


def season(p: dict[str, Any]) -> str:
    tags = _tags(p)
    return "in" if tags & _tagset("season_in_tags") else "out" if tags & _tagset("season_out_tags") else ""


def clean_html(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).replace("&amp;", "&").strip()


def summary(p: dict[str, Any]) -> dict[str, Any]:
    """What the hub keeps from a feed product (shop_json)."""
    variants = [{"title": v.get("title", ""), "price": v.get("price", ""), "available": bool(v.get("available"))}
                for v in p.get("variants") or []]
    prices = [float(v["price"]) for v in variants if str(v["price"]).replace(".", "", 1).isdigit()]
    images = [i.get("src", "") for i in p.get("images") or [] if i.get("src")]
    return {"title": p.get("title", ""), "type": p.get("product_type", ""), "tags": sorted(_tags(p)),
            "variants": variants, "price_min": min(prices) if prices else None,
            "image": images[0] if images else "", "images": images[:10],
            "url": f"{store_url()}/products/{p.get('handle', '')}"}


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


def sync(conn, trigger: str = "manual", fetch: Callable[[], list[dict]] | None = None) -> dict[str, int]:
    """Bring adstudio_products in line with the store. Returns counts; records a shopify_syncs row."""
    now = datetime.utcnow().isoformat(timespec="seconds")
    with conn:
        sid = conn.execute("INSERT INTO shopify_syncs (trigger, started_at) VALUES (?,?)", (trigger, now)).lastrowid
    try:
        feed = (fetch or fetch_feed)()
        counts = _apply(conn, [p for p in feed if is_listed(p)], now)
        counts["feed"] = len(feed)
    except Exception as exc:
        with conn:
            conn.execute("UPDATE shopify_syncs SET status='failed', finished_at=?, error=? WHERE id=?",
                         (datetime.utcnow().isoformat(timespec="seconds"), str(exc)[:500], sid))
        raise
    with conn:
        conn.execute("UPDATE shopify_syncs SET status='done', finished_at=?, counts_json=? WHERE id=?",
                     (datetime.utcnow().isoformat(timespec="seconds"), json.dumps(counts), sid))
    return counts


def _apply(conn, listed: list[dict[str, Any]], now: str) -> dict[str, int]:
    counts = {"listed": len(listed), "added": 0, "updated": 0, "linked": 0, "archived": 0, "restored": 0}
    rows = [dict(r) for r in conn.execute("SELECT id, name, origin, shopify_handle, shopify_url, archived,"
                                          " description FROM adstudio_products")]
    by_handle = {r["shopify_handle"]: r for r in rows if r["shopify_handle"]}
    unlinked = [r for r in rows if not r["shopify_handle"]]
    seen = set()
    with conn:
        for p in listed:
            handle = p.get("handle") or ""
            if not handle or handle in seen:
                continue
            seen.add(handle)
            s = summary(p)
            stock = int(any(v["available"] for v in s["variants"]))
            row = by_handle.get(handle)
            if row is None:                              # made before the hub: link by URL, then by name
                row = next((r for r in unlinked if re.search(rf"/products/{re.escape(handle)}(?:[/?#.]|$)",
                                                              r["shopify_url"] or "")), None) \
                    or next((r for r in unlinked if _norm(r["name"]) == _norm(s["title"])), None)
                if row is not None:
                    unlinked.remove(row)
                    counts["linked"] += 1
            if row is None:
                conn.execute(
                    "INSERT INTO adstudio_products (name, description, product_type, origin, shopify_handle,"
                    " shopify_url, shop_json, in_stock, season, synced_at) VALUES (?,?,?, 'shopify', ?,?,?,?,?,?)",
                    (s["title"], clean_html(p.get("body_html", "")), map_type(s["type"]), handle, s["url"],
                     json.dumps(s), stock, season(p), now))
                counts["added"] += 1
                continue
            if row.get("archived"):
                counts["restored"] += 1
            conn.execute(
                "UPDATE adstudio_products SET origin='shopify', shopify_handle=?, shop_json=?, in_stock=?, season=?,"
                " archived=0, synced_at=?, shopify_url=CASE WHEN COALESCE(shopify_url,'')='' THEN ? ELSE shopify_url END,"
                " description=CASE WHEN COALESCE(description,'')='' THEN ? ELSE description END WHERE id=?",
                (handle, json.dumps(s), stock, season(p), now, s["url"], clean_html(p.get("body_html", "")), row["id"]))
            counts["updated"] += 1
        known = [r for r in rows if r["origin"] == "shopify" and r["shopify_handle"] and not r["archived"]]
        gone = [r for r in known if r["shopify_handle"] not in seen]
        if gone and len(seen) < len(known) / 2:
            log.warning("Shopify feed looks cut short (%s of %s products); nothing archived", len(seen), len(known))
            counts["archive_skipped"] = len(gone)
        else:
            for r in gone:
                conn.execute("UPDATE adstudio_products SET archived=1 WHERE id=?", (r["id"],))
            counts["archived"] = len(gone)
    return counts


def last_sync(conn) -> dict[str, Any] | None:
    r = conn.execute("SELECT * FROM shopify_syncs ORDER BY id DESC LIMIT 1").fetchone()
    if r is None:
        return None
    d = dict(r)
    d["counts"] = json.loads(d.pop("counts_json") or "{}")
    return d


def sync_hour() -> int:
    try:
        return max(0, min(23, int(os.getenv("SHOPIFY_SYNC_HOUR_UTC", "6"))))
    except ValueError:
        return 6


def is_due(conn, now: datetime | None = None) -> bool:
    """One scheduled sync a day, after SHOPIFY_SYNC_HOUR_UTC."""
    now = now or datetime.utcnow()
    if now.hour < sync_hour():
        return False
    return not conn.execute("SELECT 1 FROM shopify_syncs WHERE trigger='scheduled' AND started_at >= ? LIMIT 1",
                            (now.strftime("%Y-%m-%d"),)).fetchone()


def queue(conn, trigger: str) -> int:
    from creative_intelligence import jobs
    return jobs.enqueue(conn, "shopify_sync", {"trigger": trigger}, dedupe_key="shopify_sync")


def shop(product: dict[str, Any]) -> dict[str, Any]:
    try:
        return json.loads(product.get("shop_json") or "{}")
    except (TypeError, ValueError):
        return {}


GENERIC_FAMILIES = {"variety", "variety box", "bundle", "gift basket", "mystery box", "snack box", "gift", "lemonade"}


def family(product: dict[str, Any]) -> str:
    """The fruit family from the Shopify type ("Fruit: Mango, Honey" or "Mix Box: Mango, Variety"
    → "mango"), for sharing facts across a family. '' when there is none."""
    head, _, rest = (shop(product).get("type") or "").partition(":")
    if head.strip().lower() not in ("fruit", "mix box"):
        return ""
    f = rest.split(",")[0].strip().lower()
    if f.endswith(("ches", "shes")):
        f = f[:-2]
    elif f.endswith("s") and not f.endswith("ss"):
        f = f[:-1]
    return "" if not f or f in GENERIC_FAMILIES else f
