"""F6 — prompts live in creative_intelligence/prompts/ as versioned files; every generated picture
records which prompt files and versions made it, and its Asset tags show that."""
from __future__ import annotations

import pytest

from creative_intelligence import prompts
from creative_intelligence import db as D


def test_every_prompt_file_loads_and_has_no_stray_placeholders():
    names = prompts.versions()
    assert len(names) >= 17 and {"fruit-reference-lock", "qa-score-system", "meta-tagging-system"} <= set(names)
    for n in names:
        t = prompts.text(n)
        assert t.strip()
        for ph in ("${box}", "${taxonomy}", "${motion}", "${aspect}", "${product}", "${look}", "${setting}", "${change}", "${framing}",
                   "${character}", "${personality}", "${people}", "${realism}"):
            t = t.replace(ph, "")
        assert "$" not in t, n


def test_newest_version_wins_and_old_ones_stay_readable(tmp_path, monkeypatch):
    (tmp_path / "hello.v1.txt").write_text("Hi ${who}")
    (tmp_path / "hello.v2.txt").write_text("Hello ${who}")
    monkeypatch.setattr(prompts, "DIR", tmp_path)
    prompts.versions.cache_clear()
    prompts._raw.cache_clear()
    try:
        assert prompts.version("hello") == 2 and prompts.text("hello", who="Mara") == "Hello Mara"
        assert prompts.text("hello", v=1, who="Mara") == "Hi Mara"
        assert prompts.stamp("hello") == "hello@v2"
        with pytest.raises(KeyError):
            prompts.version("missing")
    finally:
        prompts.versions.cache_clear()
        prompts._raw.cache_clear()


def test_modules_use_the_files():
    from creative_intelligence.adstudio import core, profiles
    from creative_intelligence.tagging import ai_tagger
    assert profiles.FRUIT_REFERENCE_LOCK == prompts.text("fruit-reference-lock")
    assert core.BOX in profiles.FRUIT_CONCEPT_RULES and "${box}" not in profiles.FRUIT_CONCEPT_RULES
    assert '"hook_type"' in ai_tagger.SYSTEM and "${taxonomy}" not in ai_tagger.SYSTEM


def test_stamp_depends_on_product_type_and_purpose():
    from creative_intelligence.adstudio.profiles import prompt_stamp
    from creative_intelligence import knowledge
    kv = f"+knowledge@{knowledge.version()}"                # prompts that read the knowledge base record its version
    assert prompt_stamp({"product_type": "fruit"}, "run") == "run-plan-system@v2+fruit-concept-rules@v1+fruit-reference-lock@v2" + kv
    assert prompt_stamp({"product_type": "candle"}, "aiad") == "ai-ad-scene-system@v1+ai-ad-copy-rules@v3+label-reference-lock@v1" + kv
    assert prompt_stamp({"product_type": "other"}, "image") == "image-concepts-system@v2+other-reference-lock@v1" + kv
    assert prompt_stamp({"product_type": "fruit"}, "core") == "core-brief-system@v1"


def test_assets_show_the_recorded_prompt_version(tmp_path, monkeypatch):
    from creative_intelligence.adstudio import assets
    path = tmp_path / "t.db"
    monkeypatch.setenv("CI_DB_PATH", str(path))
    D._UP_TO_DATE.clear()
    D.init_db(path)
    c = D.get_connection(path)
    with c:
        c.execute("INSERT INTO adstudio_products (id, name, product_type) VALUES (1, 'Mango', 'fruit')")
        c.execute("INSERT INTO adstudio_textads (id, product_id, brief_id, kind, variant, title, status, image_path, prompt,"
                  " prompt_version) VALUES (1, 1, 1, 'scene', 'cut_reveal', 'Cut', 'done', '/x.png', 'p', 'ai-ad-scene-system@v1')")
        c.execute("INSERT INTO adstudio_textads (id, product_id, brief_id, kind, variant, title, status, image_path, prompt)"
                  " VALUES (2, 1, 1, 'scene', 'cut_reveal', 'Old', 'done', '/y.png', 'old prompt')")
    assets.ensure_assets(c, 1)
    got = assets.assets_for(c, "textad", [1, 2])
    assert got[1]["prompt_version"] == "ai-ad-scene-system@v1"
    assert len(got[2]["prompt_version"]) == 8                   # older rows: hash of the prompt
