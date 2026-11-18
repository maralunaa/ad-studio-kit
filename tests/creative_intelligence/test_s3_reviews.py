"""Which real reviews ads may use (4–5 stars, never BBB). No live API."""
from __future__ import annotations


def test_only_positive_reviews_are_usable(tmp_path):
    """Feedback 2026-10-22: negative reviews and BBB complaints never reach an ad, a theme, a phrase or an idea."""
    from creative_intelligence.adstudio import reviews
    from creative_intelligence.db import get_connection, init_db
    init_db(tmp_path / "u.db")
    c = get_connection(tmp_path / "u.db")
    rows = [("review", "google", 5, 0, "Loved it"), ("review", "google", 2, 0, "Arrived bruised"),
            ("review", "shopify", 3, 0, "Fine"), ("review", "bbb", 5, 0, "Complaint filed"),
            ("review", "testimonials", None, 1, "Best gift"), ("review", "email", None, 0, "Unrated email"),
            ("comment", "meta_comment", None, 0, "Do you ship to Texas?")]
    with c:
        for kind, src, stars, cur, text in rows:
            c.execute("INSERT INTO reviews (kind, source, rating, curated, text, author, posted_on, added_at)"
                      " VALUES (?,?,?,?,?, 'Sam Kay', '2026-10-01', 'now')", (kind, src, stars, cur, text))
    assert {r["text"] for r in reviews.listing(c)} == {"Loved it", "Best gift", "Do you ship to Texas?"}
    assert len(reviews.listing(c, usable=False)) == 7
    c.close()
