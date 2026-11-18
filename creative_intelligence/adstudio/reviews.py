"""
The real reviews and comments library (brief S3; playbook › Compliance: reviews and testimonials must be real).

Every quote an ad shows comes from here, copied exactly from where it was posted, with its author, source and date.
Nothing here is written by AI and nothing is edited for length: a layout that can't fit a quote leaves it out.
Comments are shown with the commenter's name hidden (first initial only); reviews with first name and last initial.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

SOURCES = {"google": "Google review", "shopify": "Shopify review", "trustpilot": "Trustpilot review",
           "bbb": "BBB review", "testimonials": "Site testimonial (curated)", "meta_comment": "Facebook/Instagram comment",
           "email": "Customer email", "other": "Other"}
KINDS = ("review", "comment")
# What ads, themes, phrases and ideas may use: 4–5 star reviews; unrated only when curated (site testimonials) or a
# comment (Comment reply picks one by hand). Negative reviews and BBB complaints stay stored but are never used.
USABLE = "source != 'bbb' AND (rating >= 4 OR (rating IS NULL AND (curated=1 OR kind IN ('comment', 'question'))))"


def display_author(author: str, kind: str) -> str:
    """'Maria Lopez' → 'Maria L.' for a review, 'M.' for a comment (names hidden on comments)."""
    parts = [p for p in re.split(r"\s+", (author or "").strip()) if p]
    if kind == "question":
        return "People often ask"
    if not parts:
        return "A customer"
    if kind == "comment":
        return parts[0][0].upper() + "."
    return parts[0] + (f" {parts[-1][0].upper()}." if len(parts) > 1 else "")


def add(conn, d: dict[str, Any]) -> tuple[int | None, str]:
    text = (d.get("text") or "").strip()
    kind = d.get("kind") or "review"
    source = d.get("source") or ""
    posted = (d.get("posted_on") or "").strip()
    if kind not in KINDS:
        return None, "Kind is review or comment."
    if len(text) < 3:
        return None, "Paste the exact words of the review or comment."
    if source not in SOURCES:
        return None, "Say where it was posted."
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", posted):
        return None, "Add the date it was posted (YYYY-MM-DD)."
    if not (d.get("author") or "").strip():
        return None, "Add the author's name as posted (only a first name and initial are shown)."
    rating = d.get("rating")
    try:
        rating = round(float(rating)) if rating not in (None, "") else None          # Fera sends 5.0
    except (TypeError, ValueError):
        rating = None
    rating = rating if rating and 1 <= rating <= 5 else None
    ext = (d.get("external_id") or "").strip() or None
    if ext and conn.execute("SELECT 1 FROM reviews WHERE external_id=?", (ext,)).fetchone():
        return None, "Already in the library."
    with conn:
        rid = conn.execute(
            "INSERT INTO reviews (product_id, kind, text, author, rating, source, source_url, posted_on, added_at,"
            " external_id, curated) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (int(d["product_id"]) if d.get("product_id") else None, kind, text, d["author"].strip(), rating, source,
             (d.get("source_url") or "").strip(), posted, datetime.utcnow().isoformat(timespec="seconds"), ext,
             1 if d.get("curated") or source == "testimonials" else 0)).lastrowid
    return rid, ""


def archive(conn, review_id: int) -> bool:
    with conn:
        return conn.execute("UPDATE reviews SET archived=1 WHERE id=?", (review_id,)).rowcount == 1


def listing(conn, product_id: int | None = None, kind: str | None = None, usable: bool = True) -> list[dict[str, Any]]:
    """A product's reviews plus the shop-wide ones (product_id NULL), newest first. usable=False also lists the
    negative ones (Admin shows everything, marked)."""
    q, args = "SELECT *, (" + USABLE + ") AS usable FROM reviews WHERE archived=0" + (f" AND {USABLE}" if usable else ""), []
    if product_id is not None:
        q += " AND (product_id=? OR product_id IS NULL)"
        args.append(product_id)
    if kind:
        q += " AND kind=?"
        args.append(kind)
    out = []
    for r in conn.execute(q + " ORDER BY posted_on DESC, id DESC", args):
        d = dict(r)
        d["shown_as"] = display_author(d["author"], d["kind"])
        d["source_name"] = SOURCES.get(d["source"], d["source"])
        out.append(d)
    return out


def resolve(conn, ids: list[int], product_id: int, kind: str | None = None) -> list[dict[str, Any]]:
    """The chosen library entries for a layout, in the chosen order; anything not in the library is dropped."""
    have = {r["id"]: r for r in listing(conn, product_id, kind)}
    return [{"id": i, "text": have[i]["text"], "author": have[i]["shown_as"], "rating": have[i]["rating"],
             "source": have[i]["source_name"], "posted_on": have[i]["posted_on"], "kind": have[i]["kind"]}
            for i in ids if i in have]
