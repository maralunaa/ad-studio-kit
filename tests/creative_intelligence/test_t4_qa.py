"""T4 — quality scoring: editable rubric, Claude scores, measured legibility, QA filter, overrules.

No live API: Claude is faked.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from PIL import Image

from creative_intelligence.adstudio import qa
from creative_intelligence.adstudio.render_template import render
from creative_intelligence.db import get_connection, init_db

GOOD = {"fruit_accuracy": 5, "artefacts": 5, "box_label": 5, "prominence": 4, "appetite": 5,
        "focal_point": 4, "brand_fit": 4, "never_include": 5}
BAD_FRUIT = {**GOOD, "fruit_accuracy": 2}


class TestRubric:
    def test_loads_repo_rubric(self):
        r = qa.load_rubric()
        assert r["threshold"] and {c["key"] for c in r["criteria"]} >= {"fruit_accuracy", "legibility", "never_include"}

    def test_weighted_total_and_pass(self):
        r = qa.load_rubric()
        t, passed, fails = qa.total(r, GOOD, "image")
        assert passed and t >= r["threshold"] and fails == []

    def test_hard_minimum_fails_even_with_good_total(self):
        t, passed, fails = qa.total(qa.load_rubric(), BAD_FRUIT, "image")
        assert t > 3.6 and not passed and "Fruit accuracy 2 is below the minimum 3" in fails

    def test_legibility_only_counts_where_it_applies(self):
        r = qa.load_rubric()
        assert qa.total(r, {**GOOD, "legibility": 1}, "image")[1]          # images: legibility ignored
        assert not qa.total(r, {**GOOD, "legibility": 1}, "composite")[1]  # composites: below min 2

    def test_editing_the_rubric_changes_scoring_without_code(self, tmp_path, monkeypatch):
        strict = tmp_path / "strict.yaml"
        strict.write_text(qa.RUBRIC_PATH.read_text().replace("threshold: 3.6", "threshold: 4.9"))
        monkeypatch.setenv("QA_RUBRIC_PATH", str(strict))
        t, passed, fails = qa.total(qa.load_rubric(), GOOD, "image")
        assert not passed and fails[0].startswith(f"total {t} is below the pass mark 4.9")


class TestLegibility:
    def test_score_from_contrast_and_size(self):
        assert qa.legibility_score({"min_contrast": 12, "min_text_px_at_phone": 14}) == 5
        assert qa.legibility_score({"min_contrast": 3.2, "min_text_px_at_phone": 14}) == 3
        assert qa.legibility_score({"min_contrast": 12, "min_text_px_at_phone": 7}) == 2
        assert qa.legibility_score({}) is None

    def test_wcag_contrast(self):
        assert qa.contrast((0, 0, 0), (255, 255, 255)) == 21.0

    def test_renderer_reports_contrast(self, tmp_path):
        dark, light = tmp_path / "dark.png", tmp_path / "light.png"
        Image.new("RGB", (1080, 1350), (20, 20, 20)).save(dark)
        Image.new("RGB", (1080, 1350), (250, 248, 240)).save(light)
        m1, m2 = {}, {}
        render(str(dark), "lifestyle_tagline", {"headline": "Ripe on arrival"}, m1)   # white on dark
        render(str(light), "premium", {"headline": "Rare"}, m2)                      # cream adapts to ink on light
        assert m1["min_contrast"] > 7 and qa.legibility_score(m1) == 5
        assert m2["min_contrast"] > 7 and qa.legibility_score(m2) == 5


class FakeClient:
    def __init__(self, scores):
        self.calls = []
        self.scores = scores
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        out = {"scores": self.scores, "reasoning": "Accurate blush skin and custard flesh; slight clutter."}
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(out))],
                               usage=SimpleNamespace(input_tokens=6000, output_tokens=150, cache_read_input_tokens=0))


class Conn:
    """The real connection plus the test image path (sqlite3 connections take no attributes)."""
    def __init__(self, c, img):
        self._c, self.img = c, img

    def __getattr__(self, name):
        return getattr(self._c, name)

    def __enter__(self):
        return self._c.__enter__()

    def __exit__(self, *a):
        return self._c.__exit__(*a)


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    init_db(tmp_path / "t.db")
    img = tmp_path / "img.png"
    Image.new("RGB", (800, 1000), "orange").save(img)
    c = Conn(get_connection(tmp_path / "t.db"), str(img))
    yield c
    c.close()


class TestScoring:
    def test_claude_scores_with_reference_photos_and_rubric(self, conn):
        client = FakeClient(GOOD)
        r = qa.score(conn, "image", 1, conn.img, ["data:image/png;base64,iVBORw0KGgo="],
                     {"name": "Rosy Sunshine Mango", "product_type": "fruit"}, client=client)
        assert r["passed"] and r["reasoning"].startswith("Accurate")
        kw = client.calls[0]
        assert kw["model"] == "claude-sonnet-5-5" and kw["fallbacks"] == "default"
        schema_keys = kw["output_config"]["format"]["schema"]["properties"]["scores"]["required"]
        assert "legibility" not in schema_keys and "fruit_accuracy" in schema_keys   # measured, not asked
        text = kw["messages"][0]["content"][-1]["text"]
        assert "NEVER-INCLUDE RULES" in text and "reference photos" in text
        cost = conn.execute("SELECT kind, cost_usd FROM model_calls").fetchone()
        assert tuple(cost) == ("qa-score", 0.0135)                          # 6000 in x $2/M + 150 out x $10/M

    def test_error_leaves_image_unscored_not_failed(self, conn):
        class Boom:
            beta = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: (_ for _ in ()).throw(OSError("down"))))
        r = qa.score(conn, "image", 2, conn.img, [], {"name": "x"}, client=Boom())
        assert r["total"] is None and r["error"] == "down"

    def test_composite_inherits_base_plus_measured_legibility(self, conn):
        qa.score(conn, "image", 1, conn.img, [], {"name": "x"}, client=FakeClient(GOOD))
        r = qa.inherit(conn, "composite", 9, "image", 1, {"min_contrast": 2.4, "min_text_px_at_phone": 12})
        assert r["scores"]["legibility"] == 2 and r["scores"]["fruit_accuracy"] == 5
        assert "Text contrast 2.4:1." in r["reasoning"]
        qa.set_legibility(conn, "composite", 9, {"min_contrast": 9, "min_text_px_at_phone": 12})
        assert qa.scores_for(conn, "composite", [9])[9]["scores"]["legibility"] == 5


class TestOverrules:
    def test_overrules_are_logged_and_counted(self, conn):
        qa.score(conn, "image", 1, conn.img, [], {"name": "x"}, client=FakeClient(BAD_FRUIT))   # QA fail
        qa.score(conn, "image", 2, conn.img, [], {"name": "x"}, client=FakeClient(GOOD))        # high score
        assert qa.log_review(conn, "image", 1, "approve") == "approved_qa_reject"
        assert qa.log_review(conn, "image", 2, "reject") == "rejected_high_score"
        assert qa.log_review(conn, "image", 2, "approve") is None
        assert qa.log_review(conn, "image", 99, "approve") is None            # unscored: not logged
        rep = qa.overrule_report(conn)
        assert (rep["reviews"], rep["overrules"], rep["rate"]) == (3, 2, 66.7)


class TestRoutes:
    @pytest.fixture
    def client(self, tmp_path, monkeypatch, conn):
        import creative_intelligence.webapp.app as app_module
        monkeypatch.setattr("creative_intelligence.webapp.common.db", lambda: get_connection(tmp_path / "t.db"))
        monkeypatch.setattr("creative_intelligence.webapp.common.qa_later", lambda jobs: None)
        with conn:
            conn.execute("INSERT INTO adstudio_products (id, name) VALUES (1, 'Mango')")
            for i in (1, 2):
                conn.execute("INSERT INTO adstudio_images (id, product_id, concept, prompt, status, image_path)"
                             " VALUES (?, 1, 'c', 'p', 'done', ?)", (i, conn.img))
        qa.score(conn, "image", 1, conn.img, [], {"name": "x"}, client=FakeClient(BAD_FRUIT))
        qa.score(conn, "image", 2, conn.img, [], {"name": "x"}, client=FakeClient(GOOD))
        return app_module.app.test_client()

    def test_images_list_carries_scores_and_filters(self, client):
        d = client.get("/api/adstudio/images?product_id=1").get_json()
        assert {i["id"]: i["qa"]["passed"] for i in d["images"]} == {1: False, 2: True} and d["qa_rejected"] == 1
        assert [i["id"] for i in client.get("/api/adstudio/images?product_id=1&view=").get_json()["images"]] == [2]
        assert [i["id"] for i in client.get("/api/adstudio/images?product_id=1&view=qa_rejected").get_json()["images"]] == [1]
        assert [i["id"] for i in client.get("/api/adstudio/images?product_id=1&sort=score").get_json()["images"]] == [2, 1]

    def test_image_review_logs_overrule(self, client):
        assert client.post("/api/adstudio/image/1/review", json={"action": "approve"}).get_json()["review_status"] == "approved"
        rep = client.get("/api/ops/qa").get_json()
        assert rep["overrules"]["approved_qa_reject"] == 1 and rep["scored"]["scored"] == 2
