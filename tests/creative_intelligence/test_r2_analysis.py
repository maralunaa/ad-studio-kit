"""R2: competitor ads tagged with the full taxonomy our own ads use plus why it might work (screenshots shown to
Claude), the crowded vs gap view over Proven and Evergreen ads, and competitor videos decoded into structure templates
labelled competitor-derived (structure only). Claude is faked; ffmpeg probing is faked."""
from __future__ import annotations

import json
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from creative_intelligence.adstudio import market, research, winners
from creative_intelligence.db import get_connection, init_db


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("CI_DB_PATH", str(tmp_path / "data" / "t.db"))
    monkeypatch.setattr("creative_intelligence.config.CI_RENDER_OUTPUT_DIR", str(tmp_path / "vol" / "renders"))
    monkeypatch.chdir(tmp_path)
    init_db(tmp_path / "data" / "t.db")
    c = get_connection(tmp_path / "data" / "t.db")
    yield c
    c.close()


class _Claude:
    def __init__(self, body):
        self.body, self.sent = body, []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.sent.append(kw)
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(self.body))],
                               usage=SimpleNamespace(input_tokens=1000, output_tokens=200, cache_read_input_tokens=0))


def _ad(conn, page, days, **tags):
    rid, why = research.add_manual(conn, {"page_name": page, "body": f"{page} words {days}",
                                          "started_on": (date.today() - timedelta(days=days)).isoformat()})
    assert rid, why
    if tags:
        with conn:
            conn.execute(f"UPDATE competitor_ads SET {', '.join(k + '=?' for k in tags)}, tagged_at='now' WHERE id=?",
                         (*tags.values(), rid))
    return rid


def _tags(i, **kw):
    return {"id": i, "style": "us_vs_supermarket", "hook_type": "contrarian", "angle": "gifting", "format": "unboxing",
            "offer_type": "bundle_variety", "product_type": "gift_box", "visual_style": "natural_lifestyle",
            "why_it_works": "the reveal sells the gift", **kw}


def test_full_taxonomy_and_screenshots(conn, tmp_path):
    from creative_intelligence.storage import storage
    path = storage().put_bytes("research", "s.png", b"\x89PNG fake")
    rid, _ = research.add_manual(conn, {"page_name": "Hill & Dale", "body": "The gift they remember.", "media_path": path})
    fake = _Claude({"ads": [_tags(rid)]})
    assert research.tag(conn, client=fake) == {"tagged": 1}
    props = fake.sent[0]["output_config"]["format"]["schema"]["properties"]["ads"]["items"]["properties"]
    assert {"format", "offer_type", "product_type", "visual_style", "why_it_works"} <= set(props)
    assert "unboxing" in props["format"]["enum"] and "gift_box" in props["product_type"]["enum"]
    assert any(b["type"] == "image" for b in fake.sent[0]["messages"][0]["content"])
    row = dict(conn.execute("SELECT * FROM competitor_ads WHERE id=?", (rid,)).fetchone())
    assert row["format"] == "unboxing" and row["why_it_works"] == "the reveal sells the gift" and row["family"]


def test_crowded_vs_gap(conn):
    for page in ("Hill & Dale", "Orchard Gift Co", "Sunny Groves"):
        _ad(conn, page, 60, angle="gifting", hook_type="contrarian", format="unboxing", style="us_vs_supermarket")
    _ad(conn, "Coastal Fruit Co", 120, angle="tree_ripened_quality", hook_type="curiosity", format="cutting_demo", style="claymation")
    _ad(conn, "Kumo Berry", 5, angle="flavor_experience", format="product_hero", style="claymation")      # too new: left out
    c = research.crowding(conn)
    angles = {g["key"]: g for g in c["angle"]}
    assert angles["gifting"]["brands"] == 3 and angles["gifting"]["label"] == "crowded"
    assert angles["tree_ripened_quality"]["label"] == "gap" and angles["tree_ripened_quality"]["longest"] == 120
    assert "flavor_experience" not in angles
    assert research.patterns(conn)["crowding"]["format"][0]["key"] == "unboxing"


def test_competitor_videos_decode_only_when_proven(conn, monkeypatch):
    monkeypatch.setattr("creative_intelligence.adstudio.videotools.probe", lambda p: {"duration": 12})
    queued = []
    monkeypatch.setattr("creative_intelligence.jobs.enqueue", lambda c, kind, p, **k: queued.append((kind, p)) or 1)
    new = _ad(conn, "Kumo Berry", 5)
    out = market.attach_video(conn, new, b"video", "ad.mp4")
    assert out["decoded"] is False and "New test" in out["why"] and queued == []
    assert market.attach_video(conn, new, b"x", "ad.gif")["error"].startswith("Upload an mp4")
    old = _ad(conn, "Coastal Fruit Co", 45)
    out = market.attach_video(conn, old, b"video", "ad.mp4")
    t = dict(conn.execute("SELECT * FROM video_templates WHERE id=?", (out["template_id"],)).fetchone())
    assert t["origin"] == "competitor" and t["ad_name"] == "Coastal Fruit Co (competitor-derived)" and t["upload_path"]
    assert queued == [("decode", {"template_id": out["template_id"]})]
    assert market.queue_decode(conn, old) == {"template_id": out["template_id"]}                 # not twice
    with conn:
        conn.execute("UPDATE video_templates SET status='done', template_json='{\"shots\": []}' WHERE id=?", (t["id"],))
    tpl = next(x for x in winners.listing(conn) if x["id"] == t["id"])
    assert tpl["template"]["origin"].startswith("competitor-derived")


def test_the_decoder_is_told_structure_only_for_competitors(conn):
    fake = _Claude({"shots": []})
    import creative_intelligence.adstudio.winners as W
    orig = W.SCHEMA
    try:
        W.ask_claude([], [], {"ad_name": "Coastal Fruit Co (competitor-derived)", "competitor": True}, False, fake)
    except Exception:
        pass
    text = fake.sent[0]["messages"][0]["content"][-1]["text"]
    assert "ANOTHER BRAND'S AD" in text and "Never" in text and W.SCHEMA is orig


def test_routes(conn, tmp_path, monkeypatch):
    import creative_intelligence.webapp.app as app_module
    monkeypatch.setattr("creative_intelligence.webapp.common.db", lambda: get_connection(tmp_path / "data" / "t.db"))
    c = app_module.app.test_client()
    rid = _ad(conn, "Kumo Berry", 5)
    assert c.post(f"/api/adstudio/market/ad/{rid}/video").status_code == 400
    assert c.post(f"/api/adstudio/market/ad/{rid}/decode").get_json()["error"] == "Upload the ad's video first."
    assert "crowding" in c.get("/api/adstudio/research").get_json()["patterns"]
    assert b"mkBy" in c.get("/ideas/market").data
