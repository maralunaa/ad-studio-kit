"""T5 — one-pass runs (plan, server-side worker, QA as they arrive, silent replacement, budgets)
and the Compose fan-out to three sizes.

No live API: planner, image model and scorer are faked; images are real PNGs.
"""
from __future__ import annotations

import json

import pytest
from PIL import Image

from creative_intelligence.adstudio import costs, runs
from creative_intelligence.adstudio.sizes import fit_canvas
from creative_intelligence.db import get_connection, init_db

STATES = ["whole", "cut", "in_hand", "in_box"]


def plan_of(n):
    return [{"title": f"Shot {i}", "archetype": f"arch{i % 5}", "scene": f"scene {i}", "camera": f"cam{i}",
             "lighting": f"light{i % 3}", "surface": f"surface{i % 4}", "fruit_state": STATES[i % 4],
             "label": "new_concept" if i % 3 else "iteration"} for i in range(n)]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "t.db"
    init_db(path)
    factory = lambda: get_connection(path)
    c = factory()
    with c:
        c.execute("INSERT INTO adstudio_products (id, name, product_type) VALUES (1, 'Rosy Sunshine Mango', 'fruit')")
    c.close()
    calls = {"prompts": []}

    def make_image(conn, row, prompt, refs, aspect, run_id):
        calls["prompts"].append(prompt)
        p = tmp_path / f"img{row['id']}.png"
        Image.new("RGB", (800, 1000), (200, 120, 60)).save(p)
        costs.log_image(conn, "google/nano-banana-pro", "image", row["id"], run_id=str(run_id))
        return str(p)

    def deps(score):
        return {"plan": lambda conn, prod, n, prior, refs, run_id: plan_of(n), "make_image": make_image,
                "score": score, "refs": lambda conn, pid: [], "product": lambda conn, pid: {"name": "Mango"}}

    return {"factory": factory, "deps": deps, "calls": calls, "tmp": tmp_path}


def passing(conn, img_id, path, refs, prod, run_id):
    return {"total": 4.4, "passed": True, "failures": [], "reasoning": "good"}


def _run(env, n, score=passing, aspect="4:5"):
    c = env["factory"]()
    run_id, reason = runs.create_run(c, 1, n, aspect)
    c.close()
    assert run_id, reason
    return run_id, runs.process_run(env["factory"], run_id, **env["deps"](score))


class TestRuns:
    def test_twenty_image_run_delivers_twenty_distinct_images(self, env):
        run_id, out = _run(env, 20)
        assert out["delivered"] == 20 and out["message"] == "20 of 20 delivered."
        c = env["factory"]()
        imgs = c.execute("SELECT title, concept FROM adstudio_images WHERE run_id=? AND archived=0", (run_id,)).fetchall()
        assert len(imgs) == 20 and len({i["concept"] for i in imgs}) == 20
        st = runs.status(c, run_id)
        assert st["status"] == "done" and st["done"] == 20 and {s["label"] for s in st["slots"]} == {"new_concept", "iteration"}
        assert round(st["spent_usd"], 2) == 3.60                              # 20 x $0.18

    def test_failed_slot_is_replaced_quietly_then_reported(self, env):
        tries = {}

        def flaky(conn, img_id, path, refs, prod, run_id):
            slot = conn.execute("SELECT idx FROM adstudio_run_slots WHERE image_id=?", (img_id,)).fetchone()["idx"]
            tries[slot] = tries.get(slot, 0) + 1
            if slot == 0 and tries[slot] == 1:                              # fails once, then passes
                return {"total": 3.0, "passed": False, "failures": ["Fruit accuracy 2 is below the minimum 3"], "reasoning": "round red mango"}
            if slot == 1:                                                    # never passes
                return {"total": 2.5, "passed": False, "failures": ["No artefacts 2 is below the minimum 3"], "reasoning": "warped hand"}
            return passing(conn, img_id, path, refs, prod, run_id)

        run_id, out = _run(env, 8, flaky)
        assert out["delivered"] == 7 and tries[0] == 2 and tries[1] == runs.MAX_ATTEMPTS
        assert out["message"].startswith("7 of 8 delivered.") and "No artefacts 2 is below the minimum 3" in out["message"]
        assert any("A previous attempt failed quality review: Fruit accuracy 2" in p for p in env["calls"]["prompts"])
        c = env["factory"]()
        assert c.execute("SELECT COUNT(*) FROM adstudio_images WHERE run_id=? AND archived=0", (run_id,)).fetchone()[0] == 7

    def test_run_budget_stops_the_run_and_never_overspends(self, env, monkeypatch):
        from creative_intelligence import config
        monkeypatch.setattr(config, "BUDGET_RUN_USD", 1.0)                 # ~4 tries at $0.21 each
        run_id, out = _run(env, 12)
        assert out["delivered"] < 12 and "the run budget of $1.00 is reached" in out["message"]
        c = env["factory"]()
        assert runs.spent_on_run(c, run_id) <= 1.0
        assert runs.status(c, run_id)["status"] == "stopped"

    def test_daily_cap_blocks_a_new_run(self, env, monkeypatch):
        from creative_intelligence import config
        monkeypatch.setattr(config, "BUDGET_DAY_USD", 0.10)
        c = env["factory"]()
        run_id, reason = runs.create_run(c, 1, 8, "4:5")
        assert run_id is None and "Daily cap reached — resets at 00:00 London. Admin can raise it." == reason

    def test_only_valid_sizes(self, env):
        c = env["factory"]()
        assert runs.create_run(c, 1, 6, "4:5") == (None, "Pick a run size of 8, 12, 16, 20.")

    def test_a_run_is_only_processed_once(self, env):
        run_id, _ = _run(env, 8)
        assert runs.process_run(env["factory"], run_id, **env["deps"](passing)) == {"skipped": True}

    def test_planner_safety_net_dedupes(self):
        p = plan_of(4)
        assert len(runs.dedupe(p + [dict(p[0])])) == 4

    def test_interrupted_runs_are_closed_at_startup(self, env):
        c = env["factory"]()
        run_id, _ = runs.create_run(c, 1, 8, "4:5")
        with c:
            c.execute("UPDATE adstudio_runs SET status='running' WHERE id=?", (run_id,))
        assert runs.mark_interrupted(c) == 1
        assert "Interrupted by an app restart" in runs.status(c, run_id)["message"]


class TestFanout:
    def test_fit_canvas_never_crops(self, tmp_path):
        src = tmp_path / "s.png"
        Image.new("RGB", (800, 1000), (10, 200, 10)).save(src)
        for aspect, size in (("1:1", (1080, 1080)), ("4:5", (1080, 1350)), ("9:16", (1080, 1920))):
            out = Image.open(fit_canvas(str(src), aspect, str(tmp_path / f"o{aspect.replace(':', '')}.png")))
            assert out.size == size
            assert out.getpixel((size[0] // 2, size[1] // 2)) == (10, 200, 10)  # the photo itself, centred

    def test_fanout_makes_headlines_x_layouts_x_sizes(self, env, monkeypatch):
        import creative_intelligence.webapp.app as app_module
        from creative_intelligence.adstudio import store
        monkeypatch.setattr("creative_intelligence.webapp.common.db", env["factory"])
        monkeypatch.setattr(store, "composites_dir", lambda: env["tmp"])
        c = env["factory"]()
        img = env["tmp"] / "base.png"
        Image.new("RGB", (800, 1000), (90, 60, 40)).save(img)
        with c:
            iid = c.execute("INSERT INTO adstudio_images (product_id, title, concept, prompt, status, image_path)"
                            " VALUES (1, 'Cut half', 'c', 'p', 'done', ?)", (str(img),)).lastrowid
            for i in range(4):
                c.execute("INSERT INTO adstudio_hooks (product_id, hook_text, headline, cta) VALUES (1, ?, ?, 'Get Yours')",
                          (f"h{i}", f"Headline {i}"))
        client = app_module.app.test_client()
        d = client.post(f"/api/adstudio/image/{iid}/fanout", json={}).get_json()
        assert (d["made"], d["hooks"], len(d["templates"])) == (27, 3, 3)
        sizes = {Image.open(r[0]).size for r in c.execute("SELECT output_path FROM adstudio_composites")}
        assert sizes == {(1080, 1080), (1080, 1350), (1080, 1920)}
        listed = client.get("/api/adstudio/images?product_id=1").get_json()["images"]
        assert [i["id"] for i in listed] == [iid]                           # size variants stay out of Images
        assert client.post(f"/api/adstudio/image/{iid}/fanout", json={"templates": ["bold_type"]}).status_code == 400


class TestRunRoutes:
    def test_start_status_and_one_at_a_time(self, env, monkeypatch):
        import creative_intelligence.webapp.app as app_module
        monkeypatch.setattr("creative_intelligence.webapp.common.db", env["factory"])
        started = []
        monkeypatch.setattr(runs, "start_in_background", lambda f, rid, **d: started.append(rid))
        client = app_module.app.test_client()
        assert client.post("/api/adstudio/product/1/runs", json={"n": 5}).status_code == 400
        rid = client.post("/api/adstudio/product/1/runs", json={"n": 12, "aspect_ratio": "4:5"}).get_json()["run_id"]
        assert started == [rid]
        assert client.post("/api/adstudio/product/1/runs", json={"n": 8}).status_code == 409
        st = client.get(f"/api/adstudio/run/{rid}").get_json()
        assert (st["status"], st["size"], st["budget_usd"]) == ("queued", 12, 10.0)
        assert client.get("/api/adstudio/product/1/runs").get_json()["runs"][0]["id"] == rid


def test_cancel_stops_new_images(env):
    c = env["factory"]()
    run_id, reason = runs.create_run(c, 1, 8, "4:5")
    assert run_id, reason
    assert runs.cancel(c, run_id) and not runs.cancel(c, run_id)                    # queued: stopped at once
    assert tuple(c.execute("SELECT status, message FROM adstudio_runs WHERE id=?", (run_id,)).fetchone()) == (
        "stopped", "Cancelled before it started.")
    run_id, _ = runs.create_run(c, 1, 8, "4:5")
    c.close()
    deps = env["deps"](passing)
    plan = deps["plan"]

    def plan_then_cancel(conn, *a):                                                  # cancelled while planning
        assert runs.cancel(conn, run_id)
        return plan(conn, *a)

    out = runs.process_run(env["factory"], run_id, **{**deps, "plan": plan_then_cancel})
    assert out["delivered"] == 0 and "Cancelled" in out["message"]
    c = env["factory"]()
    assert c.execute("SELECT status FROM adstudio_runs WHERE id=?", (run_id,)).fetchone()[0] == "stopped"
    c.close()


def test_a_slot_not_delivered_shows_its_last_try_and_the_reviewers_note(env):
    """Feedback 2026-11-13: "i want to see what is failing" — the failed try and why, not only the score line."""
    def failing(conn, img_id, path, refs, prod, run_id):
        with conn:
            conn.execute("INSERT INTO qa_scores (source, source_id, total, passed, reasoning) VALUES ('image', ?, 3.1, 0,"
                         " 'The candle label is misspelled.')", (img_id,))
        return {"total": 3.1, "passed": False, "failures": ["total 3.1 is below the pass mark 3.6"],
                "reasoning": "The candle label is misspelled."}
    run_id, _ = _run(env, 8, score=failing)
    c = env["factory"]()
    sl = runs.status(c, run_id)["slots"][0]
    assert sl["status"] == "failed" and sl["has_image"] and sl["qa_note"] == "The candle label is misspelled."
    assert "below the pass mark" in sl["last_error"]


def test_the_image_reviewer_sees_the_scene_it_was_asked_for(env, monkeypatch):
    """2026-11-13: candle images with their scent ingredients, a hand or a box failed "props beyond what is specified"
    because the reviewer never saw what was specified."""
    from creative_intelligence.adstudio import qa
    seen = {}

    class Msgs:
        def create(self, **kw):
            seen["ask"] = kw["messages"][0]["content"][-1]["text"]
            raise RuntimeError("stop")

    class Client:
        beta = type("B", (), {"messages": Msgs()})()

    Image.new("RGB", (100, 100), (200, 120, 60)).save(env["tmp"] / "x.png")
    monkeypatch.setattr("creative_intelligence.adstudio.chat._client", lambda **k: Client())
    c = env["factory"]()
    with c:
        c.execute("INSERT INTO adstudio_images (id, product_id, title, concept, status) VALUES (7, 1, 't',"
                  " 'A hand lights the candle beside halved mangosteens', 'done')")
    qa.score(c, "image", 7, str(env["tmp"] / "x.png"), [], {"name": "Candle", "product_type": "candle"}, client=Client())
    assert "THE SCENE THIS IMAGE WAS ASKED FOR" in seen["ask"] and "halved mangosteens" in seen["ask"]
