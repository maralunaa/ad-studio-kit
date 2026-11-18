"""The video process (F6 + feedback 2026-10-22): storyboards first (Claude text only), then only the missing shots,
one draft per video, and every version of the approved drafts. Claude, shots and rendering are faked."""
from __future__ import annotations

import json

import pytest

from creative_intelligence.adstudio import shots, styles, video_batch as VB
from creative_intelligence.db import get_connection, init_db


@pytest.fixture
def conn(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setenv("CI_DB_PATH", str(db))
    init_db(db)
    c = get_connection(db)
    with c:
        c.execute("INSERT INTO adstudio_products (id, name, product_type) VALUES (1, 'Rosy Sunshine Mango', 'fruit')")
        c.execute("INSERT INTO adstudio_products (id, name, product_type) VALUES (2, 'Lychee', 'fruit')")
    monkeypatch.setattr("creative_intelligence.jobs.enqueue", lambda *a, **k: 1)
    monkeypatch.setattr("creative_intelligence.budget.check", lambda *a, **k: None)
    monkeypatch.setattr(VB, "_shot_cost", lambda conn, pid, t: 1.5)
    monkeypatch.setattr(styles, "estimate", lambda conn, s, vs, *a, **k: {"total": round(0.05 * len(vs), 2)})
    monkeypatch.setattr(styles, "_check", lambda *a, **k: {"status": "ok"})
    yield c
    c.close()


def _write(conn, prod, style, seconds, tpl, idea, plan=False, n_hooks=3, **k):
    assert plan and n_hooks == VB.HOOKS
    if prod["id"] == 2:
        raise ValueError("Add the approved ripeness facts in the Product Hub first.")
    s = {"style": style, "lines": [{"text": f"Line {i}", "kind": "narrator", "shot_type": t, "short": True}
                                   for i, t in enumerate(("whole_fruit_hero", "knife_cut", "cross_section_macro"))],
         "hooks": [f"Hook {i}" for i in range(VB.HOOKS)], "hook_headline": "Hook 0", "cta": "Shop now",
         "claims": {"status": "ok"}, "missing_shots": ["knife_cut"]}
    s["hook_shots"] = styles.hook_shots(s, ["whole_fruit_hero", "cross_section_macro"])
    return s


def test_storyboard_shots_drafts_finals(conn, tmp_path, monkeypatch):
    style = next(k for k in VB.batch_styles() if k not in ("template", "comment_reply", VB.ANIMATE))
    assert {"template", VB.ANIMATE} <= set(VB.batch_styles())       # every live format can be batched
    assert "comment_reply" not in VB.batch_styles()                       # retired 2026-10-30
    assert VB.create(conn, {"product_ids": [1], "styles": {}})[1] == "Pick at least one style."
    monkeypatch.setattr(styles, "write", _write)
    monkeypatch.setattr(shots, "estimate", lambda *a, **k: 1.0)
    bid, _ = VB.create(conn, {"product_ids": [1, 2], "styles": {style: 2}})
    db = lambda: get_connection(tmp_path / "t.db")
    assert VB.plan(db, bid) == {"batch_id": bid, "planned": 2, "of": 4}
    st = VB.status(conn, bid)
    assert st["status"] == "planned" and "ripeness" in st["items"][2]["error"]
    it = st["items"][0]
    assert it["missing_shots"] == ["knife_cut"] and len(it["hooks"]) == 6 and len(set(it["hook_shots"][:3])) == 3
    assert it["own_shots"] == ["whole_fruit_hero", "knife_cut", "cross_section_macro"]   # its opening + two beats
    assert st["total_usd"] == 7.6              # one knife-cut library shot + 3 own shots per video + 2 drafts
    # 1 · edit the storyboard
    assert VB.edit(conn, bid, 0, {"line": 1, "text": "Cut it like a cheek"}) == ""
    assert VB.edit(conn, bid, 0, {"remove_line": 0}).startswith("The first and last")
    assert VB.edit(conn, bid, 1, {"keep": False}) == ""
    assert VB.status(conn, bid)["items"][0]["lines"][1]["text"] == "Cut it like a cheek"
    # 2 · approve: only the missing shot is made
    made = []
    monkeypatch.setattr(shots, "create", lambda conn, pid, t, look="", style="": made.append((pid, t, look)) or (9, "", False))
    assert VB.approve(conn, bid)["shots"] == 4
    assert made == [(1, "knife_cut", ""), (1, "whole_fruit_hero", f"b{bid}.0"), (1, "knife_cut", f"b{bid}.0"),
                    (1, "cross_section_macro", f"b{bid}.0")]           # the dropped video's own shots aren't made
    assert VB.status(conn, bid)["status"] == "shooting"
    # 3 · when the shot is in, one draft per kept video
    monkeypatch.setattr(shots, "passing", lambda conn, pid, t, look="": {"id": 9})
    monkeypatch.setattr(VB, "_any_passing", lambda conn, pid: True)
    drafts = []

    def fake_create(c, pid, s, v, p, m, vs, *a, **k):
        drafts.append((vs, k))
        with c:
            vid = c.execute("INSERT INTO adstudio_videos (product_id, source_kind, source_id, style, status, copy_json,"
                            " created_at) VALUES (?, 'styled', 0, ?, 'queued', ?, 'now')",
                            (pid, style, json.dumps({"pieces": {"k": {"secs": 1}}}))).lastrowid
        return vid, ""
    monkeypatch.setattr(styles, "create", fake_create)
    VB.advance(db, bid)
    assert [d[0] for d in drafts] == [[VB.DRAFT]] and VB.status(conn, bid)["status"] == "drafting"
    with conn:
        conn.execute("UPDATE adstudio_videos SET status='done'")
    assert VB.advance(db, bid) == {"status": "drafts"}
    # 4 · finals for the approved draft, reusing its pieces, without the draft's own version
    assert VB.finals(conn, bid)["error"].startswith("Approve at least one")
    assert VB.judge(conn, bid, 0, True) == ""
    assert VB.finals(conn, bid)["started"] == 1
    vs, kw = drafts[-1]
    assert VB.DRAFT not in vs and len(vs) == 6 * 2 * 2 - 1 and kw["pieces"] == {"k": {"secs": 1}}


def test_hooks_open_on_different_shots():
    s = {"lines": [{"shot_type": "a"}, {"shot_type": "b"}], "hooks": ["h"] * 5}
    assert styles.hook_shots(s, ["c", "d"])[:4] == ["a", "c", "d", "b"]
    first = styles.lines_for({**s, "hooks": ["one", "two"], "hook_shots": ["a", "c"]}, 1, "full")[0]
    assert first["text"] == "two" and first["shot_type"] == "c"


def test_routes(conn, tmp_path, monkeypatch):
    import creative_intelligence.webapp.app as app_module
    monkeypatch.setattr("creative_intelligence.webapp.common.db", lambda: get_connection(tmp_path / "t.db"))
    c = app_module.app.test_client()
    assert "batch_styles" in c.get("/api/adstudio/styles").get_json()
    style = next(k for k in VB.batch_styles() if k not in ("template", "comment_reply", VB.ANIMATE))
    bid = c.post("/api/adstudio/video-batch", json={"product_ids": [1], "styles": {style: 1}}).get_json()["batch_id"]
    assert c.get(f"/api/adstudio/video-batch/{bid}").get_json()["status"] == "planning"
    assert c.post(f"/api/adstudio/video-batch/{bid}/approve", json={}).status_code == 400
    assert c.post(f"/api/adstudio/video-batch/{bid}/edit", json={"index": 0, "keep": True}).status_code == 400
    assert c.post(f"/api/adstudio/video-batch/{bid}/nope", json={}).status_code == 400


def test_formats_that_need_a_pick_get_one_in_turn(conn, tmp_path, monkeypatch):
    """Feedback 2026-10-23: Animated still, From a winning ad and Comment reply had no quantity in the batch."""
    from PIL import Image
    for i, score in ((1, 3.9), (2, 4.6)):
        p = tmp_path / f"i{i}.png"
        Image.new("RGB", (1080, 1350), (200, 100, 60)).save(p)
        with conn:
            conn.execute("INSERT INTO adstudio_images (id, product_id, title, status, image_path, review_status, archived)"
                         " VALUES (?, 1, 'x', 'done', ?, 'approved', 0)", (i, str(p)))
            conn.execute("INSERT INTO qa_scores (source, source_id, total, passed, scores_json, rubric_version)"
                         " VALUES ('image', ?, ?, 1, '{}', 1)", (i, score))
    monkeypatch.setattr("creative_intelligence.adstudio.animate.estimate", lambda conn, d, still: 1.5)
    monkeypatch.setattr("creative_intelligence.adstudio.style_research.research", lambda *a, **k: [])   # found none
    bid, _ = VB.create(conn, {"product_ids": [1], "styles": {VB.ANIMATE: 3}})
    VB.plan(lambda: get_connection(tmp_path / "t.db"), bid)
    st = VB.status(conn, bid)
    stills = [i for i in st["items"] if i["style"] == VB.ANIMATE]
    assert [i["image_id"] for i in stills] == [2, 1, 2] and all(i["keep"] for i in stills)    # best first, in turn
    assert st["total_usd"] == 4.5
    made = []
    monkeypatch.setattr("creative_intelligence.adstudio.animate.create", lambda c, pid, kind, iid, *a: made.append(iid) or (70 + iid, ""))
    assert VB.edit(conn, bid, 0, {"keep": False}) == "" and VB.edit(conn, bid, 1, {"line": 0, "text": "x"}).startswith("An animated")
    VB.approve(conn, bid)
    assert made == [1, 2]                                                            # the unticked one isn't made


def test_fruit_characters_make_and_use_their_own_character(conn, tmp_path, monkeypatch):
    """Feedback 2026-10-23: "this is an auto generated character, with the option to edit / change under Characters"."""
    from creative_intelligence.adstudio import characters
    queued = []
    monkeypatch.setattr("creative_intelligence.jobs.enqueue", lambda c, kind, p, **k: queued.append(kind) or 1)
    monkeypatch.setattr("creative_intelligence.budget.check", lambda *a, **k: None)
    ch = characters.for_plan(conn, 1)                                       # none yet: the four are made
    names = [c["name"] for c in characters.listing(conn, 1)]
    assert len(names) == 5 and ch["name"] == names[0] and "character_sheet" in queued
    assert characters.auto_approve(conn, ch["id"]) == "wait"                # sheets still being drawn
    sheet = conn.execute("SELECT id FROM character_sheets WHERE character_id=?", (ch["id"],)).fetchone()[0]
    with conn:
        conn.execute("UPDATE character_sheets SET status='done', path='/tmp/x.png' WHERE id=?", (sheet,))
    assert characters.auto_approve(conn, ch["id"]) == "" and characters.for_product(conn, 1)["id"] == ch["id"]
    assert characters.for_plan(conn, 1)["id"] == ch["id"]                   # the one in use (changeable in Characters)


def test_one_video_prepares_its_character_and_scene_shot(conn, monkeypatch):
    """Feedback 2026-10-23: One video › Fruit characters stopped at "no fruit character yet". Write the script now makes
    the characters and uses the first finished look; the scene shot is started but never waited for (2026-10-26:
    "takes a long time"), and tried once more if it fails QA."""
    from creative_intelligence.adstudio import characters, shots as S
    monkeypatch.setattr("creative_intelligence.budget.check", lambda *a, **k: None)
    made = []
    monkeypatch.setattr(S, "create", lambda c, pid, t, *a, **k: made.append(t) or (40 + len(made), "", False))
    monkeypatch.setattr(S, "estimate", lambda *a, **k: 1.07)
    assert characters.prepare(conn, 1)["step"] == "wait" and len(characters.listing(conn, 1)) == 5   # drawing looks
    ch = characters.listing(conn, 1)[0]
    with conn:
        conn.execute("UPDATE character_sheets SET status='done', path='/tmp/x.png' WHERE character_id=?", (ch["id"],))
    assert characters.prepare(conn, 1) == {"step": "ready", "text": ""} and made == ["character_scene"]
    tag = f"shot:x+character:{ch['id']}+"
    with conn:                                                     # two scene shots, both failed QA
        for _ in range(2):
            conn.execute("INSERT INTO adstudio_videos (product_id, source_kind, source_id, style, shot_type, status,"
                         " prompt_version, created_at) VALUES (1, 'card', 0, 'shot', 'character_scene', 'done', ?, '')",
                         (tag,))
    assert characters.prepare(conn, 1)["step"] == "failed"
    monkeypatch.setattr(S, "passing", lambda *a, **k: {"id": 1})
    assert characters.prepare(conn, 1) == {"step": "ready", "text": ""}


def test_a_voice_that_comes_back_as_wav_is_converted_to_a_new_file(tmp_path, monkeypatch):
    """Job 200: Kokoro returns .wav, saved as <name>.wav, the very file the 48 kHz copy was written to (ffmpeg:
    "Error opening output files: Invalid argument")."""
    from creative_intelligence.adstudio import voices
    runs = []
    monkeypatch.setattr(voices, "speak", lambda conn, text, v, base, *a, **k: (base.with_suffix(".wav"), 0.01))
    monkeypatch.setattr(styles.videotools, "run", lambda args: runs.append(args))
    monkeypatch.setattr(styles.videotools, "probe_audio", lambda p: 1.0)
    styles.make_piece(None, 7, {"kind": "narrator", "text": "Hi"}, {"voice": "Wade"}, tmp_path)
    assert runs[0][1] != runs[0][-1]                                       # input and output differ


def test_a_failed_or_cut_off_shot_never_stops_the_video(conn, tmp_path, monkeypatch):
    """Feedback 2026-10-23: one failed 'whole fruit hero' (and three cut off by a deploy) stopped every video."""
    monkeypatch.setattr(styles, "write", _write)
    monkeypatch.setattr(shots, "estimate", lambda *a, **k: 1.0)
    style = next(k for k in VB.batch_styles() if k not in ("template", "comment_reply", VB.ANIMATE))
    bid, _ = VB.create(conn, {"product_ids": [1], "styles": {style: 1}})
    db = lambda: get_connection(tmp_path / "t.db")
    VB.plan(db, bid)
    remade = []
    monkeypatch.setattr(shots, "create", lambda conn, pid, t, look="", style="": remade.append((t, look)) or (9, "", False))
    VB.approve(conn, bid)
    remade.clear()
    with conn:                                        # library knife cut failed QA; the own opening was cut off
        conn.execute("INSERT INTO adstudio_videos (product_id, source_kind, source_id, style, shot_type, status, message,"
                     " prompt_version, created_at) VALUES (1, 'card', 0, 'shot', 'knife_cut', 'done', 'did not pass QA', 'x', '')")
        for t in ("whole_fruit_hero", "knife_cut", "cross_section_macro"):
            msg = "Interrupted by an app restart; start it again." if t == "whole_fruit_hero" else "did not pass QA"
            conn.execute("INSERT INTO adstudio_videos (product_id, source_kind, source_id, style, shot_type, status,"
                         " message, prompt_version, created_at) VALUES (1, 'card', 0, 'shot', ?, 'failed', ?, ?, '')",
                         (t, msg, f"x+look:b{bid}.0:{style}+"))
    monkeypatch.setattr(VB, "_any_passing", lambda conn, pid: True)
    drafts = []
    monkeypatch.setattr(styles, "create", lambda *a, **k: drafts.append(1) or (None, ""))
    VB.advance(db, bid)
    assert remade == [("whole_fruit_hero", f"b{bid}.0")] and not drafts      # made again once, then wait for it
    with conn:
        conn.execute("UPDATE adstudio_videos SET status='done', message='did not pass QA' WHERE id=(SELECT MAX(id)"
                     " FROM adstudio_videos)")
        conn.execute("INSERT INTO adstudio_videos (product_id, source_kind, source_id, style, shot_type, status, message,"
                     " prompt_version, created_at) VALUES (1, 'card', 0, 'shot', 'whole_fruit_hero', 'done',"
                     " 'did not pass QA', ?, '')", (f"x+look:b{bid}.0:{style}+",))
    VB.advance(db, bid)
    st = VB.status(conn, bid)
    assert drafts == [1] and not st["items"][0]["error"] and len(st["items"][0]["swaps"]) == 4
    assert {x["state"] for x in st["shots"]} == {"failed"}


def test_own_shots_skip_cards_reserved_for_a_style():
    s = {"style": "claymation", "hook_shots": ["clay_hero"],
         "lines": [{"shot_type": t} for t in ("clay_hero", "knife_cut", "knife_cut", "plated_sharing", "box_opening")]}
    assert VB.own_shots(s, "b1.0") == {"knife_cut": "b1.0", "plated_sharing": "b1.0", "box_opening": "b1.0"}
    c = shots.with_look(shots.card("knife_cut"), "sensory")
    assert "macro" in c["still"]["composition"] and c["motion"]["camera"] == shots.LOOKS["sensory"]["camera"]
    assert c["still"]["locks"] == shots.card("knife_cut")["still"]["locks"]          # the fruit stays locked


def test_a_flagged_script_says_what_to_fix_and_comes_back_once_fixed(conn, tmp_path, monkeypatch):
    """Live test 2026-10-26: five of fourteen videos were dropped for one borderline line, with no way to fix it."""
    style = next(k for k in VB.batch_styles() if k not in ("template", "comment_reply", VB.ANIMATE))
    monkeypatch.setattr(styles, "write", _write)
    bid, _ = VB.create(conn, {"product_ids": [1], "styles": {style: 1}})
    VB.plan(lambda: get_connection(tmp_path / "t.db"), bid)
    _, req, items = VB._load(conn, bid)
    items[0]["script"]["claims"] = {"status": "flagged", "unlisted": [{"claim": "Hook 2"}], "banned": []}
    VB._flag(items[0], items[0]["script"])
    VB._save(conn, bid, items)
    st = VB.status(conn, bid)["items"][0]
    assert not st["keep"] and st["claim_flags"] == ["“Hook 2”"] and "Edit or remove" in st["error"]
    monkeypatch.setattr(styles, "_check", lambda *a, **k: {"status": "ok", "unlisted": [], "banned": []})
    assert VB.edit(conn, bid, 0, {"remove_hook": 2}) == ""
    st = VB.status(conn, bid)["items"][0]
    assert st["keep"] and not st["error"] and "Hook 2" not in st["hooks"] and len(st["hook_shots"]) == VB.HOOKS - 1
    assert VB.edit(conn, bid, 0, {"keep": False}) == "" and VB.edit(conn, bid, 0, {"hook": 1, "text": "New hook"}) == ""
    assert not VB.status(conn, bid)["items"][0]["keep"]                # one you dropped stays dropped


def test_problems_name_the_claim_and_the_other_fruit():
    p = styles.claim_problems({"unlisted": [{"claim": "Ships in 24 hours"}],
                               "banned": [{"term": "peach", "rule": "other_fruit"}]})
    assert p == ["“Ships in 24 hours”", "“peach” (another fruit)"]


def test_a_pack_direction_steers_every_script(conn, tmp_path, monkeypatch):
    """Ad Pack (feedback 2026-10-26): "make this pack Christmas ads" must reach the video scripts too."""
    seen = []

    def write(conn, prod, style, seconds, tpl, idea, **k):
        seen.append(idea)
        return _write(conn, prod, style, seconds, tpl, idea, **k)
    monkeypatch.setattr(styles, "write", write)
    style = next(k for k in VB.batch_styles() if k not in ("template", "comment_reply", VB.ANIMATE))
    bid, _ = VB.create(conn, {"product_ids": [1], "styles": {style: 1}, "direction": "Christmas: warm, festive"})
    VB.plan(lambda: get_connection(tmp_path / "t.db"), bid)
    assert seen and "CREATIVE DIRECTION FOR THIS PACK" in seen[0] and "Christmas" in seen[0]
