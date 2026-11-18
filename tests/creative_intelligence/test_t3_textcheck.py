"""T3 (interim) — AI Ads text check: read the drawn copy back and auto-reject mismatches.

No live API: Claude's reading is faked.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from PIL import Image

from creative_intelligence.adstudio import textcheck as tc
from creative_intelligence.db import get_connection, init_db

EXPECTED = {"headline": "Tastes Like Mango Custard", "subhead": "Because the texture actually is", "cta": "Get Yours"}


def reading(transcript, fields, extra=()):
    return {"transcript": transcript,
            "fields": [{"field": f, "rendered": r, "status": s} for f, r, s in fields],
            "extra_text": list(extra)}


GOOD = reading("TASTES LIKE MANGO CUSTARD Because the texture actually is GET YOURS",
               [("headline", "TASTES LIKE MANGO CUSTARD", "case_or_punctuation_only"),
                ("subhead", "Because the texture actually is", "exact"), ("cta", "GET YOURS", "case_or_punctuation_only")])


class TestDecide:
    def test_pass_ignores_case_and_quotes(self):
        assert tc.decide(EXPECTED, GOOD)["status"] == "pass"

    def test_misspelling_fails_with_reason(self):
        r = tc.decide(EXPECTED, reading("TASTES LIKE MANGO CUSTRAD Because the texture actually is GET YOURS",
                                        [("headline", "TASTES LIKE MANGO CUSTRAD", "misspelled"),
                                         ("subhead", "Because the texture actually is", "exact"), ("cta", "GET YOURS", "exact")]))
        assert r["status"] == "fail" and "headline misspelled" in r["problems"][0] and "CUSTRAD" in r["problems"][0]

    def test_model_says_exact_but_transcript_disagrees(self):
        r = tc.decide(EXPECTED, reading("TASTES LIKE MANGO CUSTAD Because the texture actually is GET YOURS",
                                        [("headline", "TASTES LIKE MANGO CUSTARD", "exact"),
                                         ("subhead", "Because the texture actually is", "exact"), ("cta", "GET YOURS", "exact")]))
        assert r["status"] == "fail" and "not found exactly" in r["problems"][0]

    def test_a_left_off_cta_is_a_note(self):                      # the Meta button carries it (A2, 2026-10-23)
        r = tc.decide(EXPECTED, reading("TASTES LIKE MANGO CUSTARD Because the texture actually is",
                                        [("headline", "TASTES LIKE MANGO CUSTARD", "exact"),
                                         ("subhead", "Because the texture actually is", "exact"), ("cta", "", "missing")]))
        assert r["status"] == "pass" and r["notes"] == ["cta left off: “Get Yours” (the Meta button carries it)"]

    def test_invented_price_fails_other_extra_text_is_a_note(self):
        r = tc.decide(EXPECTED, {**GOOD, "extra_text": ["Only $29", "Test Fruit Co"]})
        assert r["status"] == "fail" and "Only $29" in r["problems"][0]
        assert r["notes"] == ["extra text: “Test Fruit Co”"]

    def test_expected_price_must_match(self):
        exp = {**EXPECTED, "price": "$35"}
        ok = tc.decide(exp, {**GOOD, "transcript": GOOD["transcript"] + " $35",
                             "fields": GOOD["fields"] + [{"field": "price", "rendered": "$35", "status": "exact"}]})
        bad = tc.decide(exp, {**GOOD, "transcript": GOOD["transcript"] + " $53",
                              "fields": GOOD["fields"] + [{"field": "price", "rendered": "$53", "status": "misspelled"}]})
        assert ok["status"] == "pass" and bad["status"] == "fail"

    def test_repeated_line_fails(self):
        r = tc.decide(EXPECTED, {**GOOD, "repeated_text": ["Because the texture actually is"]})
        assert r["status"] == "fail" and r["problems"] == ["text drawn twice: “Because the texture actually is”"]

    def test_minimal_ad_expects_no_words(self):
        assert tc.decide({}, reading("", []))["status"] == "pass"
        assert tc.decide({}, reading("50% OFF", [], ["50% OFF"]))["status"] == "fail"


class FakeClient:
    def __init__(self, result, stop="end_turn"):
        self.result, self.stop, self.calls = result, stop, []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(stop_reason=self.stop,
                               content=[SimpleNamespace(type="text", text=json.dumps(self.result))],
                               usage=SimpleNamespace(input_tokens=1500, output_tokens=200, cache_read_input_tokens=0))


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    init_db(tmp_path / "t.db")
    c = get_connection(tmp_path / "t.db")
    img = tmp_path / "ad.png"
    Image.new("RGB", (900, 1600), "orange").save(img)
    with c:
        c.execute("INSERT INTO adstudio_products (id, name) VALUES (1, 'Mango')")
        c.execute("INSERT INTO adstudio_textads (id, product_id, brief_id, headline, subhead, cta, status, image_path,"
                  " review_status, is_ready, text_checked) VALUES (1,1,1,?,?,?,'done',?,'approved',1,1)",
                  (EXPECTED["headline"], EXPECTED["subhead"], EXPECTED["cta"], str(img)))
    yield c
    c.close()


def _ad(c):
    return dict(c.execute("SELECT * FROM adstudio_textads WHERE id=1").fetchone())


class TestCheckTextad:
    def test_misspelled_ad_is_auto_rejected_with_reason(self, conn):
        bad = reading("TASTES LIKE MANGO CUSTRAD Because the texture actually is GET YOURS",
                      [("headline", "TASTES LIKE MANGO CUSTRAD", "misspelled"),
                       ("subhead", "Because the texture actually is", "exact"), ("cta", "GET YOURS", "exact")])
        client = FakeClient(bad)
        r = tc.check_textad(conn, _ad(conn), client)
        a = _ad(conn)
        assert r["status"] == "fail" and a["textcheck_status"] == "fail"
        assert (a["review_status"], a["is_ready"], a["text_checked"]) == ("rejected", 0, 0)
        assert a["review_notes"].startswith("Auto-rejected by text check: headline misspelled")
        kw = client.calls[0]
        assert kw["model"] == "claude-sonnet-5-5" and kw["fallbacks"] == "default"
        assert kw["output_config"]["format"]["type"] == "json_schema"
        assert kw["messages"][0]["content"][0]["source"]["media_type"] == "image/jpeg"

    def test_pass_leaves_review_alone_and_logs_cost(self, conn):
        r = tc.check_textad(conn, _ad(conn), FakeClient(GOOD))
        a = _ad(conn)
        assert r["status"] == "pass" and a["review_status"] == "approved" and a["is_ready"] == 1
        cost = conn.execute("SELECT model, kind, cost_usd FROM model_calls").fetchone()
        assert tuple(cost) == ("claude-sonnet-5-5", "text-check", 0.005)    # 1500 in x $2/M + 200 out x $10/M

    def test_refusal_or_error_never_rejects(self, conn):
        r = tc.check_textad(conn, _ad(conn), FakeClient(GOOD, stop="refusal"))
        assert r["status"] == "error" and _ad(conn)["review_status"] == "approved"


def test_a_design_may_leave_off_the_subhead_and_cta_but_not_the_headline():
    """A2 (2026-10-23): ads that left "Shop now" to the Meta button were re-made twice and rejected."""
    from creative_intelligence.adstudio.textcheck import decide
    exp = {"headline": "Tastes Like Peach Custard", "subhead": "Tree-ripened", "cta": "Shop now", "price": ""}
    reading = {"transcript": "TASTES LIKE PEACH CUSTARD", "fields": [
        {"field": "headline", "status": "case_or_punctuation_only", "rendered": "TASTES LIKE PEACH CUSTARD"},
        {"field": "subhead", "status": "missing", "rendered": ""}, {"field": "cta", "status": "missing", "rendered": ""}]}
    r = decide(exp, reading)
    assert r["status"] == "pass" and any("Meta button" in n for n in r["notes"])
    bad = {**reading, "fields": reading["fields"][:2] + [{"field": "cta", "status": "misspelled", "rendered": "Shpo now"}]}
    assert decide(exp, bad)["status"] == "fail"                                   # drawn wrong still fails
    gone = {"transcript": "Shop now", "fields": [{"field": "headline", "status": "missing", "rendered": ""}]}
    assert decide(exp, gone)["status"] == "fail"                                  # the headline is required

def test_a_format_is_only_checked_for_the_words_it_prints(monkeypatch):
    """A2: Minimal (no words) failed for a missing "Shop now"; its retry then painted "Shop now" on the photo."""
    seen = {}
    monkeypatch.setattr(tc, "read_image", lambda path, expected, client=None: (seen.update(expected) or
                        {"transcript": "", "fields": []}, None))
    monkeypatch.setattr(tc, "log_cost", lambda *a, **k: None)

    class Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a): return self
    r = tc.check_textad(Conn(), {"id": 1, "kind": "variant", "variant": "minimal", "headline": "Honey",
                                 "subhead": "", "cta": "Shop now", "image_path": "x.png"})
    assert seen == {"headline": "", "subhead": "", "cta": "", "price": ""} and r["status"] == "pass"
