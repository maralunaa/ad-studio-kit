"""Video Studio V7 — videos in the app: Gallery filters by style and length (each version keeps its own length),
Results can group by style, Admin shows cost per video by style and shot-library reuse. No live API."""
from __future__ import annotations

import pytest

from creative_intelligence.db import get_connection, init_db
from tests.creative_intelligence.test_gallery import client as _gallery_client  # noqa: F401


@pytest.fixture
def client(_gallery_client, tmp_path):
    c = get_connection(tmp_path / "t.db")
    with c:
        for vid, style, secs in ((1, "fruit_story", 30), (2, "shot", 6), (3, "shot", 6)):
            c.execute("INSERT INTO adstudio_videos (id, product_id, source_kind, source_id, style, duration_s, status, reuse_count,"
                      " created_at) VALUES (?, 1, 'image', 1, ?, ?, 'done', ?, 'now')", (vid, style, secs, 3 if vid == 2 else 0))
        for rid, secs, aspect in ((1, 31.5, "9:16"), (2, 14.8, "9:16"), (3, 31.4, "4:5")):
            c.execute("INSERT INTO adstudio_video_renders (id, video_id, product_id, aspect, output_path, duration_s, note, created_at)"
                      " VALUES (?, 1, 1, ?, ?, ?, '', 'now')", (rid, aspect, str(tmp_path / "x.png"), secs))
        c.execute("INSERT INTO adstudio_video_renders (id, video_id, product_id, aspect, output_path, created_at) VALUES (4, 2, 1, '9:16', ?, 'now')",
                  (str(tmp_path / "x.png"),))
        c.execute("INSERT INTO qa_scores (source, source_id, total, passed, scores_json, rubric_version)"
                  " VALUES ('video', 2, 4.2, 1, '{}', 1)")
        for sid, cost in ((1, 3.0), (1, 0.6), (2, 0.4)):
            c.execute("INSERT INTO model_calls (at, model, kind, source, source_id, cost_usd) VALUES ('now', 'm', 'video', 'video', ?, ?)",
                      (sid, cost))
    c.close()
    return _gallery_client


def vids(d):
    return sorted(i["id"] for i in d["items"] if i["kind"] == "video")


def test_gallery_filters_by_style_and_per_version_length(client):
    d = client.get("/api/adstudio/gallery?product_id=1&type=video").get_json()
    assert vids(d) == [1, 2, 3, 4]
    assert d["counts"]["style"] == {"all": 4, "fruit_story": 3, "shot": 1}
    assert d["counts"]["length"] == {"all": 4, "15": 2, "30": 2, "long": 0}      # the 15 s cut and the 6 s shot
    assert {i["id"]: i["length_s"] for i in d["items"]}[2] == 15
    assert vids(client.get("/api/adstudio/gallery?product_id=1&type=video&style=fruit_story&length=15").get_json()) == [2]
    assert vids(client.get("/api/adstudio/gallery?product_id=1&style=shot").get_json()) == [4]


def test_admin_video_costs_by_style_and_shot_reuse(client):
    o = client.get("/api/ops/video").get_json()
    story = next(s for s in o["by_style"] if s["style"] == "fruit_story")
    assert story == {"style": "fruit_story", "videos": 1, "renders": 3, "spend_usd": 3.6, "per_video_usd": 3.6,
                     "per_render_usd": 1.2}
    assert o["shots"] == {"made": 2, "passing": 1, "reuses": 3, "reuse_rate": 3.0, "spend_usd": 0.4, "per_use_usd": 0.13}


def test_pages_carry_the_new_controls(client):
    html = client.get("/gallery", follow_redirects=True).get_data(as_text=True) + client.get("/admin").get_data(as_text=True)
    assert "galStyle" in html and "galLen" in html and "['style','Style']" in html and "loadVideo();" in html
