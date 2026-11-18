"""
End-to-end smoke (mocked), run in CI on every push: the main Ad Studio flow through the real HTTP
API, the jobs table and the worker, with only the outside services faked (Claude, the image model).

Product → reference photo → hook → 8-image run (queued, then worked by the worker) → gallery →
Compose ad → approve + ready → launch kit, plus Admin, Jobs and costs. No live API is reachable:
the root conftest blocks Claude and Replicate.
"""
from __future__ import annotations

import io
import zipfile

import pytest
from PIL import Image

from creative_intelligence import db as D
from creative_intelligence import jobs, worker


def png(color=(220, 120, 140), size=(800, 1000)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
def app(tmp_path, monkeypatch):
    path = tmp_path / "data" / "creative_intel.db"
    monkeypatch.setenv("CI_DB_PATH", str(path))
    monkeypatch.setattr("creative_intelligence.config.CI_RENDER_OUTPUT_DIR", "")
    monkeypatch.setenv("FACTS_CHECK", "0")
    D._UP_TO_DATE.clear()
    D.init_db(path)
    factory = lambda: D.get_connection(path)
    monkeypatch.setattr("creative_intelligence.webapp.common.db", factory)
    from creative_intelligence.webapp.app import app as flask_app
    return flask_app.test_client(), factory


def fake_run_deps():
    """Planner, image maker and scorer that never leave the machine; images go through storage."""
    from creative_intelligence.adstudio import costs
    from creative_intelligence.storage import storage

    def plan(c, prod, n, prior, refs, run_id):
        return [{"title": f"Shot {i + 1}", "archetype": "cut_reveal", "scene": "Mango halves on linen",
                 "camera": "45° close-up", "lighting": "soft morning", "surface": "linen",
                 "fruit_state": "cut", "label": "new_concept"} for i in range(n)]

    def make_image(c, row, prompt, refs, aspect, run_id):
        ref = storage().put_bytes("images", f"img{row['id']}.png", png())
        costs.log_image(c, "google/nano-banana-pro", "image", row["id"], run_id=str(run_id))
        return ref

    def score(c, img_id, path, refs, prod, run_id):
        with c:
            c.execute("INSERT INTO qa_scores (source, source_id, total, passed, scores_json, rubric_version)"
                      " VALUES ('image', ?, 4.2, 1, '{}', 1)", (img_id,))
        return {"total": 4.2, "passed": True}

    return {"plan": plan, "make_image": make_image, "score": score,
            "refs": lambda c, pid: [], "product": lambda c, pid: {"id": pid, "name": "Rosy Sunshine Mango",
                                                                 "product_type": "fruit"}}


def drain(factory, limit=20):
    """Work the queue like the worker process does, until it is empty."""
    for _ in range(limit):
        c = factory()
        job = jobs.claim(c, "e2e:1")
        c.close()
        if job is None:
            return
        assert worker.run_one(job, factory) == "done", job


def test_main_flow_end_to_end(app, monkeypatch):
    client, factory = app
    monkeypatch.setattr("creative_intelligence.webapp.common.run_deps", fake_run_deps)
    monkeypatch.setitem(jobs.HANDLERS, "qa_score", lambda p, f: None)        # scores come from the fake scorer

    # pages load
    for page in ("/products", "/images/create", "/images/shots", "/video/create", "/gallery/assets", "/admin", "/healthz"):
        assert client.get(page).status_code == 200, page

    # product, photo, hook
    pid = client.post("/api/adstudio/products", json={"name": "Rosy Sunshine Mango", "product_type": "fruit"}).get_json()["id"]
    r = client.post(f"/api/adstudio/product/{pid}/photo", data={"file": (io.BytesIO(png()), "mango.png")},
                    content_type="multipart/form-data")
    assert r.status_code == 200
    hook = client.post(f"/api/adstudio/product/{pid}/hook", json={"headline": "Custard, not fibre", "cta": "Shop now"}).get_json()
    detail = client.get(f"/api/adstudio/product/{pid}").get_json()
    assert len(detail["photos"]) == 1 and detail["hooks"][0]["headline"] == "Custard, not fibre"

    # an 8-image run is queued by the web, then made by the worker
    run_id = client.post(f"/api/adstudio/product/{pid}/runs", json={"n": 8, "aspect_ratio": "4:5"}).get_json()["run_id"]
    assert [j["kind"] for j in client.get("/api/ops/jobs").get_json()["jobs"]] == ["image_run"]
    drain(factory)
    run = client.get(f"/api/adstudio/run/{run_id}").get_json()
    assert run["status"] == "done" and run["done"] == 8 and run["spent_usd"] == pytest.approx(8 * 0.18)

    # the gallery shows them, with QA scores and the prompt versions recorded
    gal = client.get(f"/api/adstudio/gallery?product_id={pid}&type=image").get_json()
    assert gal["counts"]["type"]["image"] == 8 and all(i["qa"]["passed"] for i in gal["items"])
    c = factory()
    assert c.execute("SELECT COUNT(*) FROM adstudio_images WHERE prompt_version LIKE 'run-plan-system@v%'").fetchone()[0] == 8
    c.close()

    # compose a finished ad from one image and the hook, approve it, mark it ready
    image_id = gal["items"][0]["id"]
    comp = client.post("/api/adstudio/compose", json={"image_id": image_id, "hook_id": hook["id"]}).get_json()
    assert comp.get("id"), comp
    for action in ("approve", "ready"):
        assert client.post(f"/api/adstudio/composite/{comp['id']}/review", json={"action": action}).status_code == 200

    # launch kit: the ready ad, named by its Asset ID, with the manifest
    z = zipfile.ZipFile(io.BytesIO(client.get(f"/api/adstudio/export-gallery?product_id={pid}&ready=1").data))
    names = z.namelist()
    assert "manifest.csv" in names and len(names) == 2 and names[0].startswith("EXB_")
    assert "Custard, not fibre" in z.read("manifest.csv").decode()

    # Admin sees the spend against the caps; nothing failed
    costs = client.get("/api/ops/costs").get_json()
    assert costs["budget"]["today"]["generation"] == pytest.approx(1.44)
    assert all(j["status"] == "done" for j in client.get("/api/ops/jobs").get_json()["jobs"])
    assert client.get("/api/ops/alerts").get_json()["alerts"] == []


def test_over_the_cap_a_run_is_refused_with_the_plain_message(app):
    client, factory = app
    pid = client.post("/api/adstudio/products", json={"name": "Mango", "product_type": "fruit"}).get_json()["id"]
    assert client.post("/api/ops/caps", json={"key": "generation_day", "usd": 0}).status_code == 200
    r = client.post(f"/api/adstudio/product/{pid}/runs", json={"n": 8, "aspect_ratio": "4:5"})
    assert "Daily cap reached — resets at 00:00 London. Admin can raise it." in r.get_json()["error"]
