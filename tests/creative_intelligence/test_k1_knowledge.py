"""K1: the knowledge files derived from the playbook exist, validate, trace every value to a playbook section,
agree with what Ad Studio can make today, and a broken file fails /healthz."""
from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from creative_intelligence import knowledge
from creative_intelligence.adstudio import styles as video_styles
from creative_intelligence.adstudio.templates import TEMPLATES, safe_area

PROMPTS = Path(knowledge.__file__).resolve().parent / "prompts"


@pytest.fixture
def kdir(tmp_path, monkeypatch):
    """A writable copy of knowledge/ for tests that break it."""
    d = tmp_path / "knowledge"
    shutil.copytree(knowledge.KNOWLEDGE_DIR, d)
    monkeypatch.setenv("KNOWLEDGE_DIR", str(d))
    return d


def test_files_load_validate_and_trace_to_the_playbook():
    assert knowledge.errors() == []
    assert knowledge.playbook_info() == {"exported": "2026-10-20", "revision": 16}
    assert re.fullmatch(r"pb16-[0-9a-f]{8}", knowledge.version())
    srcs = knowledge.sources()
    assert len(srcs) > 40
    assert {n for n, _, _ in srcs} == set(knowledge.FILES)


def test_every_source_is_a_real_section():
    secs = knowledge._sections(knowledge.playbook())
    assert knowledge.source_found("Hooks › Hook types, with examples", secs)
    assert knowledge.source_found("Pre-launch checklists", secs)
    assert not knowledge.source_found("Hooks › Hook vibes", secs)
    assert not knowledge.source_found("Made-up section", secs)
    assert knowledge.source_found("house: anything the user decided", secs)


def test_a_value_whose_section_disappears_is_reported(kdir):
    md = kdir / knowledge.PLAYBOOK
    md.write_text(md.read_text().replace("**Field rules**", "**Field limits**"))
    errs = knowledge.errors()
    assert errs and all("Field rules" in e for e in errs)


def test_broken_or_missing_files_are_one_clear_error(kdir):
    (kdir / "metrics.yaml").write_text("version: [unclosed")
    assert any(e.startswith("knowledge failed to load") for e in knowledge.errors())
    (kdir / "metrics.yaml").unlink()
    assert any("FileNotFoundError" in e for e in knowledge.errors())


def test_style_validation_catches_bad_entries(kdir):
    p = kdir / "styles.yaml"
    p.write_text(p.read_text().replace("family: animation", "family: cartoon", 1)
                 .replace("default_mode: presenter", "default_mode: hologram"))
    errs = knowledge.errors()
    assert any("fruit_characters: family 'cartoon'" in e for e in errs)
    assert any("fruit_story: modes" in e for e in errs)


def test_styles_match_what_ad_studio_makes_today():
    live_video = {s["key"] for s in knowledge.styles("video", "live") if s["route"]["kind"] != "animate"}
    assert live_video == set(video_styles.catalog())                # every video style in the app, no more
    for s in knowledge.styles(status="live"):
        r = s["route"]
        if r["kind"] == "compose":
            assert set(r["templates"]) <= set(TEMPLATES), s["key"]
        if r.get("prompt"):
            assert list(PROMPTS.glob(f"{r['prompt']}.v*.txt")), s["key"]
    used = {t for s in knowledge.styles(status="live") if s["route"]["kind"] == "compose" for t in s["route"]["templates"]}
    assert set(TEMPLATES) <= used                                      # every Compose template sits under a style
    fs = knowledge.style("fruit_story")
    assert fs["modes"] == ["presenter", "narrated"] and fs["default_mode"] == "presenter"
    assert knowledge.style("us_vs_supermarket")["status"] == "retired"        # S2 (decision g); removed 2026-10-30


def test_hooks_are_the_playbook_types_and_map_to_the_tagger():
    from creative_intelligence.tagging.ai_tagger import TAXONOMY
    types = knowledge.hook_types()
    assert [t["key"] for t in types] == ["visual_sensory", "curiosity", "contrarian", "problem", "social_proof_fomo",
                                         "discovery_list", "gift", "offer"]
    assert all(t["tagger"] in TAXONOMY["hook_type"] for t in types)


def test_rules_carry_the_decided_numbers():
    r = knowledge.load("rules")
    assert r["safe_zones"]["9:16"] == {"size": [1080, 1920], "top": 0.14, "bottom": 0.65, "side": 0.06}
    assert r["on_image_text"]["contrast_min"] == 4.5
    assert r["on_image_text"]["headline_words"] == {"min": 3, "max": 7} and r["on_image_text"]["total_words_max"] == 20
    assert r["export"]["min_bitrate_mbps"] == 8 and r["export"]["fps"] == 30
    assert (r["emoji"]["share_of_ads"], r["emoji"]["max_per_ad"], r["emoji"]["fields"]) == (0.15, 2, ["primary_text"])
    assert r["captions"]["min_px_at_phone"] * 1080 / r["captions"]["phone_width_px"] == 72
    assert safe_area()["9:16"] == (0.14, 0.65, 0.06)                   # the renderers read it from here (K2b)


def test_every_auto_checklist_item_names_its_check():
    r = knowledge.load("rules")["checklists"]
    for kind in ("static", "video"):
        assert all(i.get("check") for i in r[kind] if i["auto"]), kind
    assert sum(i["auto"] for i in r["static"]) == 9 and sum(i["auto"] for i in r["video"]) == 4


def test_compliance_keeps_the_house_rules_and_flags_personal_attributes():
    c = knowledge.load("compliance")
    keys = {i["key"] for i in c["rules"]["items"]} | {i["key"] for i in c["house"]["items"]}
    assert {"facts_only", "shopify_prices", "real_reviews_only", "no_personal_attribute", "health_wording",
            "ai_disclosure", "no_fake_scarcity", "no_origin_claims", "no_ai_drawn_text"} <= keys
    pats = [re.compile(p, re.I) for p in c["personal_attribute"]["patterns"]]
    hit = lambda t: any(p.search(t) for p in pats)                   # noqa: E731
    assert hit("Are you diabetic?") and hit("Struggling with your weight?") and hit("People like you who love fruit")
    assert not hit("Tired of fruit that never ripens?") and not hit("Are you ready for lychee season?")


def test_healthz_reports_knowledge(kdir, tmp_path, monkeypatch):
    from creative_intelligence.webapp import app as webapp
    monkeypatch.setenv("CI_DB_PATH", str(tmp_path / "data" / "creative_intel.db"))
    webapp.app.config["TESTING"] = True
    client = webapp.app.test_client()
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json["checks"]["knowledge"] == "ok"
    (kdir / "hooks.yaml").unlink()
    r = client.get("/healthz")
    assert r.status_code == 503 and r.json["checks"]["knowledge"].startswith("failed")


def test_grows_in_naturally_is_not_a_place():
    """Live test 2026-10-26: "that pink skin grows in naturally" was flagged as saying where the fruit comes from."""
    from creative_intelligence.adstudio.hook_university import origin_claim
    assert not origin_claim("That pink skin grows in naturally.", "Rosy Sunshine Mango")
    assert not origin_claim("My pink blush grows in all by itself.")
    assert origin_claim("Grown in Florida sunshine") and origin_claim("It grows in the hills of Peru")
