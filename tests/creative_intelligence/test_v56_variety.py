"""v56 (feedback 2026-11-03, Cherry Mango): "make sure the texts are different from each video and the shots are
different too, we are reusing them too many times"."""
from __future__ import annotations

import json
import sqlite3

from creative_intelligence import prompts
from creative_intelligence.adstudio import shots, styles, video_batch


def _line(text: str, shot: str = "whole_fruit_hero", kind: str = "voice") -> dict:
    return {"text": text, "shot_type": shot, "kind": kind}


def test_a_line_a_sibling_already_says_counts_as_a_repeat():
    s = {"lines": [_line("Cherry Mango. Chill it, then dig in."), _line("Sweet like candy, no strings.")]}
    got = video_batch.repeats(s, ["Cherry mango: chill it, then dig in!", "Something else entirely."])
    assert got == ["Cherry Mango. Chill it, then dig in."]
    assert video_batch.repeats(s, ["The skin blushes red when it's ready."]) == []


def test_siblings_are_the_other_videos_of_the_same_product():
    a = {"product_id": 1, "script": {"lines": [_line("One."), _line("On screen", kind="text")]}}
    b = {"product_id": 1, "script": {"lines": [_line("Two.")]}}
    c = {"product_id": 2, "script": {"lines": [_line("Three.")]}}
    me = {"product_id": 1, "script": None}
    assert video_batch.sibling_lines([a, b, c, me], me) == ["One.", "Two."]


def test_a_video_has_its_own_take_of_every_shot_it_shows():
    own = [k for k in prompts.shot_cards() if shots.own_ok(k)]
    assert len(own) >= 5
    s = {"style": "fast_cut", "hook_shots": [own[0]], "lines": [_line("x", t) for t in own[:5]]}
    assert list(video_batch.own_shots(s, "b1.0")) == own[:5]


def test_the_batch_being_written_comes_before_older_videos():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE adstudio_videos (id INTEGER PRIMARY KEY, product_id INT, source_kind TEXT, copy_json TEXT)")
    conn.execute("CREATE TABLE video_batches (id INTEGER PRIMARY KEY, items_json TEXT)")
    for n in range(12):
        conn.execute("INSERT INTO adstudio_videos (product_id, source_kind, copy_json) VALUES (1, 'styled', ?)",
                     (json.dumps({"lines": [_line(f"old {n}.{k}") for k in range(5)]}),))
    conn.execute("INSERT INTO video_batches (items_json) VALUES (?)",
                 (json.dumps([{"product_id": 1, "script": {"lines": [_line("sibling line")]}}]),))
    assert styles.used_lines(conn, 1)[0] == "sibling line"
