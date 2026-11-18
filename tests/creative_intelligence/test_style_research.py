"""How to eat and Comment reply get what they need by research (feedback 2026-10-26). Claude and search are faked."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from creative_intelligence.adstudio import facts, hub, reviews, style_research as SR
from creative_intelligence.db import get_connection, init_db


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("CI_DB_PATH", str(tmp_path / "t.db"))
    init_db(tmp_path / "t.db")
    c = get_connection(tmp_path / "t.db")
    with c:
        c.execute("INSERT INTO adstudio_products (id, name, product_type) VALUES (1, 'Honey Mango', 'fruit')")
    monkeypatch.setattr("creative_intelligence.budget.check", lambda *a, **k: None)
    yield c
    c.close()


class Fake:
    def __init__(self, items):
        self.items, self.searched = items, 0
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._format))

    def _format(self, **kw):
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps({"items": self.items}))],
                               usage=SimpleNamespace(input_tokens=100, output_tokens=50))


@pytest.fixture
def searched(monkeypatch):
    calls = []
    monkeypatch.setattr(hub, "_ask_research", lambda client, product, page, **k: calls.append(k["task"]) or
                        ("notes", [SimpleNamespace(input_tokens=1000, output_tokens=100)]))
    return calls


def product(conn):
    return dict(conn.execute("SELECT * FROM adstudio_products WHERE id=1").fetchone())


def test_how_to_eat_steps_are_researched_once_and_numbers_wait_for_ok(conn, searched):
    fake = Fake([{"text": "Press gently: a ripe one gives a little.", "source_url": "https://ext.edu/mango"},
                 {"text": "Slice the two cheeks off either side of the flat seed.", "source_url": "https://ext.edu/mango"},
                 {"text": "Ripens in 3 to 5 days at room temperature.", "source_url": "https://ext.edu/mango"},
                 {"text": "No source, so dropped.", "source_url": ""}])
    assert SR.ensure_how_to_eat(conn, product(conn), fake) == ""
    got = {f["text"]: f["status"] for f in conn.execute("SELECT text, status FROM facts WHERE category='how_to_eat'")}
    assert len(got) == 3 and got["Ripens in 3 to 5 days at room temperature."] == "pending"
    assert len([f for f in facts.list_facts(conn, 1) if f["category"] == "how_to_eat"]) == 2
    assert SR.ensure_how_to_eat(conn, product(conn), fake) == "" and len(searched) == 1      # not researched again


def test_questions_come_from_customer_language_first_never_invented(conn, searched):
    with conn:
        conn.execute("UPDATE adstudio_products SET customer_language=? WHERE id=1",
                     ('"Is it really pink inside?" (question) — https://reddit.com/r/mango/1\n'
                      '"So creamy" (praise) — https://shop.example/reviews',))
    assert SR.ensure_questions(conn, product(conn), Fake([])) == "" and not searched
    q = reviews.listing(conn, 1, "question")
    assert [x["text"] for x in q] == ["Is it really pink inside?"] and q[0]["shown_as"] == "People often ask"
    assert q[0]["source_url"] == "https://reddit.com/r/mango/1"


def test_questions_are_researched_when_there_are_none(conn, searched):
    assert SR.ensure_questions(conn, product(conn), Fake([{"text": "How do you know when it's ripe?",
                                                          "source_url": "https://forum.example/t/9"}])) == ""
    assert len(searched) == 1 and reviews.listing(conn, 1, "question")[0]["text"] == "How do you know when it's ripe?"
    assert SR.prepare(conn, product(conn), "fruit_story") == ""          # other formats need nothing


def test_a_question_card_is_never_labelled_as_a_customer(tmp_path):
    from PIL import Image
    from creative_intelligence.adstudio import timeline
    out = timeline.comment_sticker_png("Is it pink inside?", 1080, 1920, "9:16", tmp_path / "s.png",
                                       label="People often ask")
    assert Image.open(out).size == (1080, 1920)
