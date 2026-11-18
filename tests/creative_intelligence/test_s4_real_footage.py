"""S4: the brand's own footage (customer reactions, interviews, testimonials, the founder story) uploads to the shot
library, tagged real, with who filmed it and on-camera consent; AI styles never use it and it is never AI-checked."""
from __future__ import annotations

import io
import json

import pytest

from creative_intelligence.adstudio import shots, story, videotools
from creative_intelligence.db import get_connection, init_db


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CI_DB_PATH", str(tmp_path / "data" / "t.db"))
    monkeypatch.setenv("CI_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr("creative_intelligence.storage.storage",
                        lambda: __import__("creative_intelligence.storage", fromlist=["VolumeStorage"]).VolumeStorage(tmp_path / "vol"))
    monkeypatch.setattr(videotools, "probe", lambda p: {"width": 1080, "height": 1920, "duration": 12.4, "fps": 30.0})
    init_db(tmp_path / "data" / "t.db")
    c = get_connection(tmp_path / "data" / "t.db")
    with c:
        c.execute("INSERT INTO adstudio_products (id, name, product_type) VALUES (1, 'Honey Mango', 'fruit')")
    yield c
    c.close()


def test_kinds_come_from_the_playbook():
    assert shots.real_kinds()["founder_story"] == "Farmer or founder story" and "customer_reaction" in shots.real_kinds()


def test_upload_needs_kind_consent_and_who_filmed_it(env):
    up = lambda **k: shots.upload_real(env, 1, k.pop("name", "a.mp4"), b"x" * 100, **k)   # noqa: E731
    assert "kind" in up(kind="ai_actor", consent=True, filmed_by="Mara")[1]
    assert "agreed to appear" in up(kind="testimonial", filmed_by="Mara")[1]
    assert "who filmed" in up(kind="testimonial", consent=True)[1]
    assert "MP4 or MOV" in up(kind="testimonial", consent=True, filmed_by="Mara", name="clip.gif")[1]
    vid, why = up(kind="testimonial", consent=True, filmed_by="Mara", filmed_on="2026-10-18", note="Ana at the stand")
    assert why == "" and vid
    v = dict(env.execute("SELECT * FROM adstudio_videos WHERE id=?", (vid,)).fetchone())
    assert v["real"] == 1 and v["ai_person"] == 0 and v["shot_type"] == "real:testimonial" and v["model"] == "real footage"
    assert json.loads(v["real_json"])["consent"] is True and v["duration_s"] == 12
    lst = shots.real_footage(env, 1)
    assert lst[0]["kind_name"] == "Testimonial on camera" and lst[0]["filmed_by"] == "Mara"


def test_ai_styles_never_see_real_footage(env, monkeypatch):
    shots.upload_real(env, 1, "a.mp4", b"x" * 100, "customer_reaction", consent=True, filmed_by="Mara")
    lib = shots.library(env, 1)
    assert lib and not any(c["type"].startswith("real") for c in lib)          # AI recipe cards only
    assert not any(m["type"].startswith("real") for m in story.shot_menu(env, 1))
    assert all(shots.passing(env, 1, c["type"]) is None for c in lib)          # real clips never fill an AI shot


def test_upload_route(env, tmp_path, monkeypatch):
    import creative_intelligence.webapp.app as app_module
    monkeypatch.setattr("creative_intelligence.webapp.common.db", lambda: get_connection(tmp_path / "data" / "t.db"))
    c = app_module.app.test_client()
    r = c.post("/api/adstudio/shots/real", data={"file": (io.BytesIO(b"x" * 100), "clip.mp4"), "product_id": "1",
                                                "kind": "founder_story", "filmed_by": "Mara", "consent": "1"},
               content_type="multipart/form-data")
    assert r.status_code == 200 and r.get_json()["video_id"]
    assert c.post("/api/adstudio/shots/real", data={"product_id": "1"}, content_type="multipart/form-data").status_code == 400
    d = c.get("/api/adstudio/shots?product_id=1").get_json()
    assert d["real"][0]["kind"] == "founder_story" and "testimonial" in d["real_kinds"]
