"""
Storytelling videos (Video Studio V3): hook → Claude writes a 20–45 s script and storyboard from approved
facts only → shots from the library first (generating only what's missing, and only when asked) →
voice-over in a house voice → word timings → a timeline → 9:16 and 4:5 renders.

  write_script()  Claude: hook line, story lines (each with a shot type), on-screen hook headline, end-card
                  CTA. Copy is claims-checked like hooks; origin claims are flagged.
  plan()          which shots are in the library, which are missing, and the cost of the new parts
  create()        queue the video (job "story"); missing shots are queued first when allowed
  process()       the job: waits for queued shots, then voice → timings → timeline → renders → QA score

The video's script and settings live in adstudio_videos.copy_json, its 9:16 timeline in timeline_json.
"""
from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from creative_intelligence import config, prompts
from creative_intelligence.adstudio import animate, prelaunch, shots, timeline, videotools, voices

STYLE = "story"
LENGTHS = (20, 30, 45)
WORDS_PER_S = 2.6
MODEL = config.CI_CLAUDE_CHECK_MODEL
WAIT_S = 20
WAIT_MAX = timedelta(minutes=45)
ASPECTS = ("9:16", "4:5")
MUSIC_EXT = (".mp3", ".wav", ".m4a", ".aac")

SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["title", "hook_headline", "cta", "lines"],
    "properties": {
        "title": {"type": "string", "description": "a short internal title for the video"},
        "hook_headline": {"type": "string", "description": "2-6 words shown on screen during the hook"},
        "cta": {"type": "string", "description": "2-4 word call to action for the end card"},
        "lines": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["text", "shot_type"],
            "properties": {"text": {"type": "string", "description": "one spoken line"},
                           "shot_type": {"type": "string", "description": "a shot type from the list"}}}},
    }}


def music_tracks() -> list[Path]:
    d = Path(config.MUSIC_DIR)
    return sorted(p for p in d.glob("*") if p.suffix.lower() in MUSIC_EXT) if d.is_dir() else []


def music_catalog() -> list[dict[str, Any]]:
    """The tracks in music/, with what music/tracks.json says about each (title, source, where it may be used,
    vocals). Instrumentals first: vocals fight the voice-over, so they are never the default."""
    d = Path(config.MUSIC_DIR)
    try:
        meta = {t["file"]: t for t in json.loads((d / "tracks.json").read_text()).get("tracks", [])}
    except (OSError, ValueError):
        meta = {}
    out = []
    for p in music_tracks():
        m = meta.get(p.name, {})
        out.append({"file": p.name, "title": m.get("title") or p.stem, "source": m.get("source", ""),
                    "use": m.get("use", ""), "vocals": m.get("vocals"), "seconds": m.get("seconds")})
    return sorted(out, key=lambda t: (t["vocals"] is not False, t["title"].lower()))


def pick_music(choice: str) -> Path | None:
    """'none' → no music; a file name → that track; '' → the first instrumental (else none)."""
    if choice == "none":
        return None
    cat = music_catalog()
    file = choice or next((t["file"] for t in cat if t["vocals"] is False), "")
    return Path(config.MUSIC_DIR) / file if file and any(t["file"] == file for t in cat) else None


def shot_menu(conn, product_id: int) -> list[dict[str, Any]]:
    """Shots a script may cast. Cards made for one style only (card `styles`, e.g. the supermarket fruit) are left
    out: that style uses them itself."""
    held = {k for k, c in prompts.shot_cards().items() if c.get("styles")}
    return [{"type": s["type"], "label": s["label"], "summary": s["summary"], "use": s["use"],
             "in_library": s["status"] == "passing", "estimate": s["estimate"]}
            for s in shots.library(conn, product_id) if s["type"] not in held]


def write_script(conn, product: dict[str, Any], seconds: int = 30, hook: str = "", idea: str = "",
                 client: Any = None, allow_new: bool = False, repair: bool = True) -> dict[str, Any]:
    from creative_intelligence.adstudio import costs, facts
    from creative_intelligence.adstudio.chat import _client, _product_line
    from creative_intelligence.adstudio.hook_university import origin_claim
    menu = shot_menu(conn, product["id"])
    if not menu:
        raise ValueError("No shot types for this product type yet.")
    if not allow_new and any(m["in_library"] for m in menu):
        menu = [m for m in menu if m["in_library"]]          # write to the shots we have: no paid shots, no swaps
    words = round(seconds * WORDS_PER_S)
    listing = "\n".join(f"- {m['type']}: {m['label']} — {m['summary']}{' (in the library)' if m['in_library'] else ' (not made yet)'}"
                        for m in menu)
    ask = (f"{_product_line(product)}\n\n{facts.prompt_block(conn, product)}\n\n"
           f"Write a {seconds}-second storytelling video: about {words} spoken words, 4 to 8 lines.\n"
           + (f"Build the hook on this saved hook: {hook}\n" if hook else "")
           + (f"Idea to test: {idea}\n" if idea else "")
           + ("\nAVAILABLE SHOT TYPES (prefer the ones in the library; use a missing one only if the story needs it):\n"
              if allow_new else "\nAVAILABLE SHOT TYPES (use only these; write lines that fit what they show):\n") + listing)
    client = client or _client(log=False)
    resp = client.beta.messages.create(
        model=MODEL, max_tokens=3000, betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        system=prompts.text("story-script-system"),
        output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": ask}])
    if resp.stop_reason == "refusal":
        raise RuntimeError("Claude declined to write this script")
    costs.log_tokens(conn, MODEL, "copy", resp.usage, "story-script", product["id"])
    s = json.loads(next(b.text for b in resp.content if b.type == "text"))
    valid = {m["type"] for m in menu}
    fallback = next((m["type"] for m in menu if m["in_library"]), menu[0]["type"])
    for ln in s["lines"]:
        if ln["shot_type"] not in valid:
            ln["shot_type"] = fallback
    text = " ".join(ln["text"].strip() for ln in s["lines"])
    s["words"] = len(text.split())
    s["est_seconds"] = round(s["words"] / WORDS_PER_S + timeline.END_CARD_S, 1)
    s["claims"] = facts.check_copy(conn, product, {"script": text, "hook": s["hook_headline"], "cta": s["cta"]},
                                   source="story", source_id=product["id"])
    if s["claims"].get("unlisted") and repair:
        s = _repair(conn, product, s, ask, client, menu)                 # one rewrite of the flagged lines
        text = " ".join(ln["text"].strip() for ln in s["lines"])
    s["flags"] = [f"Line {i + 1} says where it comes from" for i, ln in enumerate(s["lines"])
                  if origin_claim(ln["text"], product.get("name") or "")]
    return s


def _repair(conn, product: dict[str, Any], s: dict[str, Any], ask: str, client: Any, menu: list) -> dict[str, Any]:
    """The claims check found unapproved claims: Claude rewrites the script once without them (same shots)."""
    from creative_intelligence.adstudio import costs, facts
    bad = "; ".join(f"“{u['claim']}”" for u in s["claims"]["unlisted"])
    fix = (f"{ask}\n\nYOUR DRAFT:\n{json.dumps({k: s[k] for k in ('title', 'hook_headline', 'cta', 'lines')})}\n\n"
           f"The claims check found claims that are not approved facts: {bad}. Rewrite only what is needed so the "
           "script makes no claim outside the approved facts (keep each line's shot_type and the length).")
    resp = client.beta.messages.create(
        model=MODEL, max_tokens=3000, betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        system=prompts.text("story-script-system"), output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": fix}])
    if resp.stop_reason == "refusal":
        return s
    costs.log_tokens(conn, MODEL, "copy", resp.usage, "story-script", product["id"])
    new = json.loads(next(b.text for b in resp.content if b.type == "text"))
    valid = {m["type"] for m in menu}
    for i, ln in enumerate(new["lines"]):
        if ln["shot_type"] not in valid:
            ln["shot_type"] = s["lines"][min(i, len(s["lines"]) - 1)]["shot_type"]
    text = " ".join(ln["text"].strip() for ln in new["lines"])
    out = {**s, **new, "words": len(text.split()), "est_seconds": round(len(text.split()) / WORDS_PER_S + timeline.END_CARD_S, 1),
           "repaired": [u["claim"] for u in s["claims"]["unlisted"]]}
    out["claims"] = facts.check_copy(conn, product, {"script": text, "hook": new["hook_headline"], "cta": new["cta"]},
                                     source="story", source_id=product["id"])
    return out


def plan(conn, product_id: int, script: dict[str, Any], voice_name: str) -> dict[str, Any]:
    """Shots per line (library or missing), and what the new parts cost: voice, word timings, missing shots."""
    from creative_intelligence.adstudio.costs import price
    menu = {m["type"]: m for m in shot_menu(conn, product_id)}
    rows, missing = [], []
    for i, ln in enumerate(script["lines"]):
        m = menu.get(ln["shot_type"], {})
        rows.append({"line": i + 1, "shot_type": ln["shot_type"], "label": m.get("label", ln["shot_type"]),
                     "in_library": bool(m.get("in_library"))})
        if not m.get("in_library") and ln["shot_type"] not in missing:
            missing.append(ln["shot_type"])
    text = " ".join(ln["text"] for ln in script["lines"])
    v = voices.voice(voice_name)
    new_voice = voices.estimate(conn, v, text) + price(conn, config.WORDS_MODEL.split(":")[0]) + animate.QA_ESTIMATE_USD
    new_shots = sum(menu[t]["estimate"] for t in missing if t in menu)
    return {"shots": rows, "missing": missing, "missing_labels": [menu[t]["label"] for t in missing if t in menu],
            "estimate_voice": round(new_voice, 2), "estimate_shots": round(new_shots, 2),
            "estimate": round(new_voice + new_shots, 2), "music": music_catalog()}


def create(conn, product_id: int, script: dict[str, Any], voice_name: str, generate_missing: bool = False,
           music: str = "") -> tuple[int | None, str]:
    """Queue a storytelling video. Missing shots are queued too when generate_missing; else they are swapped
    for shots already in the library. Returns (video id, '') or (None, why not)."""
    from creative_intelligence import budget, jobs
    try:
        voices.voice(voice_name)
    except ValueError as exc:
        return None, str(exc)
    if not script.get("lines"):
        return None, "The script has no lines."
    p = plan(conn, product_id, script, voice_name)
    est = p["estimate"] if generate_missing else p["estimate_voice"]
    if not generate_missing and len(p["missing"]) == len({ln["shot_type"] for ln in script["lines"]}):
        return None, "None of these shots are in the library yet. Allow generating the missing shots, or make some in Shots."
    try:
        if est > budget.caps(conn)["video_job"]:
            raise budget.BudgetExceeded(f"This video would cost about ${est:.2f}, over the video job cap.")
        budget.check(conn, "video", est)
    except budget.BudgetExceeded as exc:
        return None, str(exc)
    queued = []
    if generate_missing:
        for t in p["missing"]:
            sid, why, _ = shots.create(conn, product_id, t)
            if sid is None:
                return None, f"Couldn't start the {t} shot: {why}"
            queued.append(sid)
    settings = {**script, "voice": voice_name, "generate_missing": generate_missing, "shot_jobs": queued,
                "music": music, "plan": p}
    with conn:
        vid = conn.execute(
            "INSERT INTO adstudio_videos (product_id, source_kind, source_id, style, duration_s, model, copy_json,"
            " prompt_version, voice, created_at, idea_id) VALUES (?, 'story', 0, ?, ?, 'story', ?, ?, ?, ?, ?)",
            (product_id, STYLE, round(script.get("est_seconds") or 30), json.dumps(settings),
             prompts.stamp("story-script-system"), voice_name, datetime.utcnow().isoformat(timespec="seconds"),
             script.get("idea_id") or None)).lastrowid
    jobs.enqueue(conn, "story", {"video_id": vid}, dedupe_key=f"video:{vid}")
    return vid, ""


SIMILAR = {                                             # the closest stand-ins for a shot that isn't ready, best first
    "knife_cut": ("cross_section_macro", "plated_sharing"),
    "cross_section_macro": ("knife_cut", "plated_sharing"),
    "juice_texture_macro": ("cross_section_macro", "knife_cut"),
    "plated_sharing": ("cross_section_macro", "knife_cut"),
    "whole_fruit_hero": ("box_opening", "fruit_on_tree"),
    "hand_picking": ("fruit_on_tree", "orchard_wide"),
    "fruit_on_tree": ("hand_picking", "orchard_wide"),
    "orchard_wide": ("fruit_on_tree", "hand_picking"),
    "packing": ("box_opening", "whole_fruit_hero"),
    "box_opening": ("packing", "whole_fruit_hero"),
}


def _shot_for(conn, product_id: int, shot_type: str, used: set[int]) -> dict[str, Any] | None:
    """The passing library shot of this type; else any passing shot not used yet (swap), else any passing shot. A
    swap never brings in a shot made for one style (a clay or character shot in a Fruit story, 2026-10-26)."""
    have = shots.passing(conn, product_id, shot_type)
    if have:
        return have
    rows = [dict(r) for r in conn.execute(
        "SELECT v.* FROM adstudio_videos v JOIN qa_scores q ON q.source='video' AND q.source_id=v.id WHERE v.product_id=?"
        " AND v.style='shot' AND v.status='done' AND v.retired=0 AND q.passed=1"
        " AND instr(COALESCE(v.prompt_version, ''), '+look:')=0 AND instr(COALESCE(v.prompt_version, ''), '+cmp:')=0"
        " ORDER BY q.total DESC, v.id", (product_id,))]
    held = {k for k, c in prompts.shot_cards().items() if c.get("styles")}
    rows = [r for r in rows if r["shot_type"] not in held]
    near = SIMILAR.get(shot_type, ())                  # a stand-in that shows what the words say ("Slice it open"
    rows.sort(key=lambda r: near.index(r["shot_type"]) if r["shot_type"] in near else len(near))   # over a tree, 10-27)
    return next((r for r in rows if r["id"] not in used), rows[0] if rows else None)


def _duration(path: Path) -> float:
    return videotools.probe_audio(path)


def _hero(conn, product_id: int) -> str | None:
    r = conn.execute("SELECT path FROM adstudio_photos WHERE product_id=? ORDER BY is_hero DESC, is_primary DESC, id"
                     " LIMIT 1", (product_id,)).fetchone()
    return r[0] if r else None


def process(video_id: int, conn_factory: Callable[[], Any], client: Any = None, words_client: Any = None) -> dict[str, Any]:
    """The story job (in the worker)."""
    from creative_intelligence import jobs
    from creative_intelligence.storage import storage
    conn = conn_factory()
    try:
        v = dict(conn.execute("SELECT * FROM adstudio_videos WHERE id=?", (video_id,)).fetchone())
        s = json.loads(v["copy_json"] or "{}")
        pid = v["product_id"]
        animate._set(conn, video_id, status="running", step="Checking shots")
        # 1. shots: wait for the ones being made, swap in library shots for any that can't be had
        waiting = [t for t in {ln["shot_type"] for ln in s["lines"]} if not shots.passing(conn, pid, t) and conn.execute(
            "SELECT 1 FROM adstudio_videos WHERE product_id=? AND style='shot' AND shot_type=? AND status IN"
            " ('queued','running')", (pid, t)).fetchone()]
        started = datetime.fromisoformat(v["created_at"])
        if waiting and datetime.utcnow() - started < WAIT_MAX:
            animate._set(conn, video_id, step=f"Waiting for {len(waiting)} new shot(s)")
            jobs.enqueue(conn, "story", {"video_id": video_id}, delay_s=WAIT_S)   # no dedupe key: this job is still running
            return {"waiting": waiting}
        animate.check_stop(conn, video_id)
        notes, used, clips = [], set(), []
        for i, ln in enumerate(s["lines"]):
            shot = _shot_for(conn, pid, ln["shot_type"], used)
            if shot is None:
                raise RuntimeError("No passing shots in the library for this product.")
            if shot["shot_type"] != ln["shot_type"]:
                notes.append(f"line {i + 1}: {shot['shot_type']} instead of {ln['shot_type']}")
            used.add(shot["id"])
            box = json.loads(shot["subject_box"]) if shot.get("subject_box") else None
            clips.append({"src": shot["clip_path"], "shot_id": shot["id"], "label": shot["shot_type"], "box": box})
        # 2. voice-over and word timings
        vdir = storage().area_dir("videos")
        text = " ".join(ln["text"].strip() for ln in s["lines"])
        animate._set(conn, video_id, step="Voice-over")
        audio, _ = voices.speak(conn, text, s["voice"], vdir / f"vo_v{video_id}", "video", video_id, client=client)
        total = _duration(audio)
        animate.check_stop(conn, video_id)
        animate._set(conn, video_id, step="Caption timings")
        try:
            words = voices.word_timings(conn, audio, "video", video_id, client=words_client or client)
        except Exception as exc:
            words = voices.spread_words(text, total)
            notes.append(f"captions timed roughly ({str(exc)[:80]})")
        # 3. timeline and renders
        spans = timeline.line_spans([ln["text"] for ln in s["lines"]], words, total)
        pic = timeline.picture_plan(spans, clips)
        track = pick_music(s.get("music") or "")
        if track is None:
            notes.append("no music" if s.get("music") == "none" else "no music (add licensed tracks to music/)")
        base = {"version": 1, "fps": 30, "transition": timeline.TRANSITION, "picture": pic,
                "voice": {"src": str(audio), "words": words}, "captions": {"max_words": 3},
                "text": [],     # the captions say the spoken hook; a headline card over it said other words (2026-11-09)
                "sfx": [], "music": {"src": str(track), "gain_db": -16} if track else None,
                "end_card": {"dur": timeline.END_CARD_S, "image": _hero(conn, pid), "headline": v_name(conn, pid),
                             "cta": s["cta"]}}
        renders = []
        for aspect in ASPECTS:
            animate.check_stop(conn, video_id)
            animate._set(conn, video_id, step=f"Rendering {aspect}")
            tl = {**base, "aspect": aspect}
            out = vdir / f"story_v{video_id}_{aspect.replace(':', 'x')}.mp4"
            res = timeline.render(tl, out)
            poster = videotools.frame_at(out, out.with_suffix(".jpg"), 1.0, 540)
            now = datetime.utcnow().isoformat(timespec="seconds")
            with conn:
                rid = conn.execute(
                    "INSERT INTO adstudio_video_renders (video_id, product_id, aspect, template, headline, cta, output_path,"
                    " poster_path, status, note, duration_s, created_at) VALUES (?,?,?, 'bold_type', ?,?,?,?, 'done', ?, ?, ?)",
                    (video_id, pid, aspect, s["hook_headline"], s["cta"], str(out), str(poster), "; ".join(notes),
                     round(res["duration"], 1), now)).lastrowid
            prelaunch.record_video(conn, rid, res.get("measured"), res.get("export"))
            renders.append(rid)
            if aspect == "9:16":
                with conn:
                    conn.execute("UPDATE adstudio_videos SET timeline_json=?, clip_path=?, duration_s=? WHERE id=?",
                                 (timeline.dumps(tl), str(out), round(res["duration"]), video_id))
        for c in clips:
            shots.use(conn, c["shot_id"])
        # 4. QA on the finished 9:16 (no fruit-count check: a story cuts between shots)
        animate._set(conn, video_id, step="Quality check")
        try:
            from creative_intelligence.adstudio import video_qa
            from creative_intelligence.webapp.common import _primary_photo_uris, _product_dict
            video_qa.score(conn, video_id, str(vdir / f"story_v{video_id}_9x16.mp4"), _primary_photo_uris(conn, pid),
                           _product_dict(conn, pid) or {}, is_montage=True)
        except Exception:
            notes.append("not QA-scored")
        from creative_intelligence import budget
        spent = budget.spent_on_video(conn, video_id)
        animate._set(conn, video_id, status="done", step="", message=f"{len(renders)} renders · ${spent:.2f} new"
                     + (f" · {'; '.join(notes)}" if notes else ""), finished_at=datetime.utcnow().isoformat(timespec="seconds"))
        return {"renders": renders, "spent_usd": spent, "notes": notes}
    except animate.Stopped:
        animate._set(conn, video_id, status="failed", step="", message="Stopped.")
        return {"stopped": True}
    except Exception as exc:
        animate._set(conn, video_id, status="failed", step="", message=str(exc)[:300])
        raise
    finally:
        conn.close()


def v_name(conn, product_id: int) -> str:
    r = conn.execute("SELECT name FROM adstudio_products WHERE id=?", (product_id,)).fetchone()
    return (r[0] if r else "").title() if r and r[0].isupper() else (r[0] if r else "")
