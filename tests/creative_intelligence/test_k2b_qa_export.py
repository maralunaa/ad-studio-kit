"""K2b: QA and export follow the playbook. Copy stays inside the safe zones (9:16: top 14%, bottom 35%, sides 6%),
measured criteria (contrast 4.5:1, word counts, safe zone, allowed CTA) score every drawn ad, every "(auto)"
checklist item maps to a rubric criterion or a pre-export check, and finished videos meet the export spec."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from creative_intelligence import knowledge
from creative_intelligence.adstudio import prelaunch, qa, timeline, videotools
from creative_intelligence.adstudio.overlay import compose
from creative_intelligence.adstudio.render_template import render_to
from creative_intelligence.adstudio.templates import TEMPLATES, safe_area

SIZES = {"9:16": (1080, 1920), "4:5": (1080, 1350), "1:1": (1080, 1080)}
COPY = {"headline": "Custard in a skin", "sub": "Picked ripe, shipped the same week", "cta": "Shop now",
        "review": "The best mango I have had in years", "author": "Ann R.", "badge": "New season", "price": "$54"}


def _photo(tmp_path: Path, size, fruit: bool = False) -> str:
    img = Image.new("RGB", size, (214, 204, 188))
    if fruit:                                   # a saturated "fruit" filling the middle of the frame
        ImageDraw.Draw(img).ellipse([size[0] * 0.1, size[1] * 0.1, size[0] * 0.9, size[1] * 0.9], fill=(236, 112, 40))
    p = tmp_path / f"p{size[0]}x{size[1]}{'f' if fruit else ''}.png"
    img.save(p)
    return str(p)


def _ffmpeg() -> bool:
    try:
        videotools.ffmpeg_exe()
        return True
    except videotools.VideoToolError:
        return False


# ── safe zones ─────────────────────────────────────────────────────────────
def test_safe_area_comes_from_the_playbook():
    assert safe_area() == {"4:5": (0.05, 0.95, 0.05), "9:16": (0.14, 0.65, 0.06), "1:1": (0.05, 0.95, 0.05)}


@pytest.mark.parametrize("aspect", list(SIZES))
def test_every_template_keeps_copy_in_the_safe_zone(tmp_path, aspect):
    src = _photo(tmp_path, SIZES[aspect])
    for key in TEMPLATES:
        m: dict = {}
        render_to(src, str(tmp_path / "out.png"), key, COPY, m)
        assert qa.outside_safe_zone(m) == [], key
        if m.get("boxes"):
            assert qa.safe_zone_score(m) == 5.0


@pytest.mark.parametrize("position", ["top", "center", "bottom"])
def test_simple_overlay_stays_in_the_9x16_safe_band(tmp_path, position):
    m: dict = {}
    compose(_photo(tmp_path, SIZES["9:16"]), str(tmp_path / "o.png"), headline="Custard in a skin",
            subhead="Picked ripe", cta="Shop now", layout={"position": position}, metrics=m)
    x1, y1, x2, y2, _ = m["boxes"][0]
    assert y1 >= 0.14 * 1920 - 1 and y2 <= 0.65 * 1920 + 1 and qa.outside_safe_zone(m) == []
    assert m["words"] == {"headline": 4, "sub": 2, "cta": 2} and m["cta"] == "Shop now"


def test_a_box_outside_the_zone_fails():
    m = {"frame": [1080, 1920], "boxes": [[100, 300, 900, 400, "headline"], [100, 1500, 900, 1600, "cta"]]}
    assert qa.outside_safe_zone(m) == ["cta"] and qa.safe_zone_score(m) == 1.0


@pytest.mark.parametrize("aspect", ["9:16", "4:5"])
def test_video_captions_and_end_card_stay_in_the_zone(tmp_path, aspect):
    W, H = SIZES[aspect]
    m: dict = {"frame": [W, H]}
    timeline.end_card_png({"image": _photo(tmp_path, (1080, 1350), fruit=True), "headline": "Rosy Sunshine Mango, picked ripe",
                           "cta": "Shop now", "price": "$54"}, W, H, tmp_path / "card.png", m)
    timeline.caption_png("a caption long enough to wrap onto two lines", W, H, tmp_path / "cap.png", aspect, "boxed", m)
    timeline.kinetic_png("Custard in a skin", W, H, tmp_path / "k.png", m)
    fields = {b[4] for b in m["boxes"]}
    assert {"end_card_headline", "end_card_cta", "end_card_price", "caption", "kinetic"} <= fields
    assert qa.outside_safe_zone(m) == []
    assert m["caption_px_at_phone"] >= knowledge.load("rules")["captions"]["min_px_at_phone"]
    assert m["min_contrast"] >= 4.5 and m["cta"] == "Shop now"
    cap = next(b for b in m["boxes"] if b[4] == "caption")
    assert cap[3] <= safe_area()[aspect][1] * H                     # 9:16: above the bottom 35% (was 0.70 down)


# ── measured criteria ──────────────────────────────────────────────────────
def test_word_counts():
    assert qa.word_count_score({"words": {"headline": 4, "sub": 6, "cta": 2}}) == 5.0
    assert qa.word_count_score({"words": {"headline": 9, "cta": 2}}) == 2.0                 # headline over 7
    assert qa.word_count_score({"words": {"headline": 6, "sub": 13, "cta": 2}}) == 2.0      # 21 words in all
    assert qa.word_count_score({"words": {"headline": 2, "cta": 2}}) == 3.0                 # short: a warning
    assert qa.word_count_score({"words": {"headline": 5, "review": 30, "author": 2}}) == 5.0  # real quotes not cut
    assert qa.word_count_score({}) is None


def test_cta_must_be_an_allowed_one():
    assert qa.cta_family("Shop now") == "shop_now" and qa.cta_family(" ORDER NOW → ") == "order_now"
    assert qa.cta_family("Taste the difference") is None
    assert qa.cta_score({"cta": "Learn more"}) == 5.0 and qa.cta_score({"cta": "Grab yours"}) == 2.0


def test_rubric_v4_fails_low_contrast_and_extra_words():
    r = qa.load_rubric()
    assert r["version"] == 5
    keys = {c["key"]: c for c in r["criteria"]}
    assert keys["legibility"]["min"] == 4 and all(keys[k]["measured"] for k in ("word_count", "safe_zone", "cta_match"))
    good = {"fruit_accuracy": 5, "artefacts": 5, "box_label": 5, "prominence": 5, "appetite": 5, "focal_point": 5,
            "brand_fit": 5, "never_include": 5}
    ok = {**good, **qa.measured_scores({"min_contrast": 5.0, "words": {"headline": 4, "cta": 2}, "cta": "Shop now",
                                        "frame": [1080, 1350], "boxes": [[100, 100, 900, 300, "headline"]]})}
    assert qa.total(r, ok, "composite")[1]
    assert not qa.total(r, {**ok, "legibility": qa.legibility_score({"min_contrast": 4.0})}, "composite")[1]  # under 4.5:1
    assert not qa.total(r, {**ok, "word_count": 2.0}, "composite")[1]
    assert not qa.total(r, {**ok, "safe_zone": 1.0}, "composite")[1]
    assert not qa.total(r, {**ok, "cta_match": 2.0}, "composite")[1]
    assert qa.total(r, {**good, "word_count": 2.0}, "image")[1]                  # photos have no copy to count


def test_every_auto_checklist_item_maps_to_a_rubric_criterion_or_pre_export_check():
    rubric = {c["key"] for c in qa.load_rubric()["criteria"]}
    for kind in ("static", "video"):
        for line in knowledge.load("rules")["checklists"][kind]:
            if line["auto"]:
                assert line["check"] in prelaunch.CHECKS or line["check"] in rubric, line


# ── the checklist ──────────────────────────────────────────────────────────
def test_static_checklist(tmp_path):
    m: dict = {}
    render_to(_photo(tmp_path, SIZES["4:5"], fruit=True), str(tmp_path / "a.png"), "bold_type", COPY, m)
    scores = {"fruit_accuracy": 5, **qa.measured_scores(m)}
    item = {"kind": "compose", "id": 1, "qa": {"scores": scores},
            "asset": {"asset_id": "EXB_H1_x", "hook_type": "curiosity", "format": "compose-bold_type", "ai_generated": 1},
            "claims_check": {"status": "ok"}}
    cl = prelaunch.checklist(item, {"measured": m})
    by = {x.get("check"): x for x in cl["items"] if x["auto"]}
    assert by["fruit_accuracy"]["status"] == "pass" and by["claims"]["status"] == "pass"
    assert by["tags"]["status"] == "pass" and by["ai_flag"]["status"] == "pass"
    assert by["safe_zone"]["status"] == "pass" and by["cta_match"]["status"] == "pass"
    assert by["text_clear_of_fruit"]["status"] == "warn"            # a headline over the saturated "fruit"
    assert [x["status"] for x in cl["items"] if not x["auto"]] == ["person"]
    bad = {**item, "claims_check": {"status": "flagged", "unlisted": [{"claim": "x"}], "banned": []},
           "asset": {"asset_id": "EXB_H1_x", "format": "f", "ai_generated": 1}}
    cl = prelaunch.checklist(bad, {"measured": m})
    assert cl["fail"] == 1 and {x["check"] for x in cl["items"] if x["status"] == "warn"} >= {"tags"}


def test_video_checklist():
    measured = {"frame": [1080, 1920], "caption_px_at_phone": 28.0, "boxes": [[96, 1000, 984, 1200, "caption"]]}
    export = {"ok": True, "mbps": 10.2, "min_mbps": 8}
    item = {"kind": "video", "id": 3, "qa": {"scores": {"consistency": 5}}, "ai_person": True,
            "asset": {"asset_id": "EXB_V1_x", "hook_type": "gift", "format": "fruit_story-9x16", "ai_generated": 1}}
    by = {x.get("check"): x for x in prelaunch.checklist(item, {"measured": measured, "export": export,
                                                                 "claims": {"status": "ok"}})["items"] if x["auto"]}
    assert by["caption_size"]["status"] == "pass" and by["export_spec"]["status"] == "pass"
    assert by["consistency"]["status"] == "pass" and by["claims"]["status"] == "pass"
    low = {**measured, "boxes": [[96, 1400, 984, 1500, "caption"]]}
    by = {x.get("check"): x for x in prelaunch.checklist(item, {"measured": low, "export": {"ok": False, "bitrate": False,
          "container": True, "video_codec": True, "audio_codec": True, "fps": True, "mbps": 3, "min_mbps": 8}})["items"]
          if x["auto"]}
    assert by["caption_size"]["status"] == "fail" and "bitrate" in by["export_spec"]["note"]


def test_manifest_names_the_matching_meta_button_and_open_checks():
    from creative_intelligence.webapp.routes.gallery import _check_list, _meta_button
    assert _meta_button("shop now") == "Shop now" and _meta_button("Grab yours") == ""
    item = {"prelaunch": {"items": [{"text": "A", "status": "fail", "note": "n"}, {"text": "B", "status": "pass"}]}}
    assert _check_list(item, "fail") == "A (n)" and _check_list(item, "warn") == ""


# ── export ─────────────────────────────────────────────────────────────────
def test_finished_videos_target_the_minimum_bitrate():
    args = videotools._x264(final=True)
    assert "-crf" not in args and args[args.index("-b:v") + 1] == "10M" and args[args.index("-r") + 1] == "30"
    assert "-crf" in videotools._x264()                                  # working clips stay quality-based


@pytest.mark.skipif(not _ffmpeg(), reason="needs ffmpeg")
def test_export_spec_checks_and_fixes_a_low_bitrate(tmp_path, monkeypatch):
    monkeypatch.setenv("VIDEO_X264_PRESET", "ultrafast")
    still = tmp_path / "still.mp4"
    videotools.run(["-f", "lavfi", "-i", "color=c=0x7a5030:s=1080x1920:d=2:r=30", "-f", "lavfi", "-i",
                    "anullsrc=r=48000:cl=stereo", "-t", "2", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
                    "-c:a", "aac", str(still)])
    before = videotools.export_spec(still)
    assert before["video_codec"] and before["audio_codec"] and before["fps"] and not before["bitrate"]
    after = videotools.ensure_export(still)
    assert after["ok"] and after["reencoded"] and after["mbps"] >= 8


# ── asset tags ─────────────────────────────────────────────────────────────
def test_assets_are_tagged_with_the_playbook_hook_type(tmp_path, monkeypatch):
    from creative_intelligence.adstudio import assets
    from creative_intelligence.db import get_connection, init_db
    monkeypatch.setenv("CI_DB_PATH", str(tmp_path / "t.db"))
    init_db(tmp_path / "t.db")
    c = get_connection(tmp_path / "t.db")
    with c:
        c.execute("INSERT INTO adstudio_products (id, name, product_type) VALUES (1, 'Rosy Sunshine Mango', 'fruit')")
        c.execute("INSERT INTO adstudio_hooks (id, product_id, hook_text, archetype, hook_type) VALUES (5, 1, 'h', 'A3', 'gift')")
        c.execute("INSERT INTO adstudio_images (id, product_id, image_path) VALUES (7, 1, 'x.png')")
    d = assets.describe(c, "composite", {"id": 2, "hook_id": 5, "image_id": 7,
                                         "layout_json": json.dumps({"template": "bold_type"})}, {"name": "Rosy Sunshine Mango"})
    assert d["tags"]["hook_type"] == "gift" and d["tags"]["format"] == "compose-bold_type"
    c.close()
