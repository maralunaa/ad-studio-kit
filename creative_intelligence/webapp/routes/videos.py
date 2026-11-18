"""Video Studio: animate an approved still, finished video versions (sizes, headlines), the shot library,
storytelling videos."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from flask import Blueprint, jsonify, request, send_file

from creative_intelligence.webapp import common
from creative_intelligence.webapp.common import _product_dict

bp = Blueprint("videos", __name__)
log = logging.getLogger("adstudio")


def _hook_copy(conn, hook_id: Any, template: str) -> dict[str, Any]:
    h = conn.execute("SELECT id, headline, hook_text, subhead, cta FROM adstudio_hooks WHERE id=?", (hook_id,)).fetchone()
    if not h:
        return {}
    return {"template": template, "hook_id": h["id"], "headline": h["headline"] or h["hook_text"] or "",
            "sub": h["subhead"] or "", "cta": h["cta"] or ""}


def _copy_from(conn, d: dict[str, Any]) -> dict[str, Any]:
    template = (d.get("template") or "").strip()
    if not template:
        return {}
    if d.get("hook_id"):
        return _hook_copy(conn, d["hook_id"], template)
    copy = {"template": template, "headline": (d.get("headline") or "").strip(), "sub": (d.get("sub") or "").strip(),
            "cta": (d.get("cta") or "").strip()}
    return copy if (copy["headline"] or copy["sub"] or copy["cta"]) else {}


@bp.get("/api/adstudio/animate/options")
def animate_options() -> Any:
    """?product_id &source_kind=image|core &source_id: what the Animate dialog offers, and the estimate."""
    from creative_intelligence.adstudio import animate
    from creative_intelligence.adstudio.templates import TEMPLATES
    pid = int(request.args.get("product_id") or 0)
    kind, sid = request.args.get("source_kind", "image"), int(request.args.get("source_id") or 0)
    conn = common.db()
    prod = _product_dict(conn, pid) or {}
    row = animate.source_row(conn, kind, sid)
    why = animate.check_source(row, kind)
    needs_still = bool(row and not why and not animate._is_916(row["image_path"]))
    hooks = [{"id": h["id"], "headline": h["headline"] or h["hook_text"] or ""} for h in conn.execute(
        "SELECT id, headline, hook_text FROM adstudio_hooks WHERE product_id=? ORDER BY id DESC LIMIT 40", (pid,))]
    out = {"can_animate": not why, "reason": why, "presets": animate.presets_for(prod),
           "durations": list(animate.DURATIONS), "strategy": animate.strategy_default(), "needs_still": needs_still,
           "estimate": {d: animate.estimate(conn, d, needs_still) for d in animate.DURATIONS},
           "templates": [{"key": k, "name": t.get("name", k)} for k, t in TEMPLATES.items() if k != "scene"],
           "hooks": hooks}
    conn.close()
    return jsonify(out)


@bp.post("/api/adstudio/animate")
def animate_start() -> Any:
    """{product_id, source_kind, source_id, preset | custom, duration, strategy?, template?, hook_id | headline/sub/cta}"""
    from creative_intelligence.adstudio import animate
    d = request.get_json(force=True, silent=True) or {}
    conn = common.db()
    try:
        vid, why = animate.create(conn, int(d.get("product_id") or 0), d.get("source_kind", "image"),
                                  int(d.get("source_id") or 0), d.get("preset", "push_in"), d.get("custom", ""),
                                  int(d.get("duration") or 5), _copy_from(conn, d), d.get("strategy"))
    finally:
        conn.close()
    if vid is None:
        return jsonify({"error": why}), 400
    return jsonify({"video_id": vid})


@bp.get("/api/adstudio/video/<int:vid>")
def video_status(vid: int) -> Any:
    from creative_intelligence import budget
    from creative_intelligence.adstudio import qa
    conn = common.db()
    v = conn.execute("SELECT * FROM adstudio_videos WHERE id=?", (vid,)).fetchone()
    if v is None:
        conn.close()
        return jsonify({"error": "not found"}), 404
    out = {k: v[k] for k in ("id", "status", "step", "message", "attempts", "still_strategy", "duration_s",
                             "motion_preset", "created_at", "finished_at")}
    out["spent_usd"] = budget.spent_on_video(conn, vid)
    out["qa"] = qa.scores_for(conn, "video", [vid]).get(vid)
    out["renders"] = [dict(r) for r in conn.execute(
        "SELECT id, aspect, headline, status FROM adstudio_video_renders WHERE video_id=? ORDER BY id", (vid,))]
    conn.close()
    return jsonify(out)


@bp.post("/api/adstudio/video/<int:vid>/versions")
def video_versions(vid: int) -> Any:
    """{template, hook_ids: [..]}: the same clip with other headlines, both sizes. No new generation."""
    from creative_intelligence import jobs
    d = request.get_json(force=True, silent=True) or {}
    template = (d.get("template") or "").strip()
    conn = common.db()
    v = conn.execute("SELECT status FROM adstudio_videos WHERE id=?", (vid,)).fetchone()
    if v is None or v["status"] != "done":
        conn.close()
        return jsonify({"error": "That clip isn't finished yet."}), 400
    copies = [c for c in (_hook_copy(conn, h, template) for h in (d.get("hook_ids") or [])[:9]) if c]
    if not template or not copies:
        conn.close()
        return jsonify({"error": "Pick a layout and at least one hook."}), 400
    job_id = jobs.enqueue(conn, "video_render", {"video_id": vid, "copies": copies})
    conn.close()
    return jsonify({"job_id": job_id, "versions": len(copies) * 2})


def _render(conn, rid: int):
    return conn.execute("SELECT * FROM adstudio_video_renders WHERE id=?", (rid,)).fetchone()


@bp.get("/api/adstudio/video-render/<int:rid>/file")
def video_render_file(rid: int) -> Any:
    conn = common.db()
    r = _render(conn, rid)
    conn.close()
    if r is None or not r["output_path"] or not Path(r["output_path"]).exists():
        return jsonify({"error": "not found"}), 404
    return send_file(r["output_path"], mimetype="video/mp4", conditional=True)


@bp.get("/api/adstudio/video-render/<int:rid>/poster")
def video_render_poster(rid: int) -> Any:
    conn = common.db()
    r = _render(conn, rid)
    conn.close()
    if r is None or not r["poster_path"] or not Path(r["poster_path"]).exists():
        return jsonify({"error": "not found"}), 404
    return send_file(r["poster_path"], mimetype="image/jpeg")


@bp.post("/api/adstudio/video-render/<int:rid>/review")
def video_render_review(rid: int) -> Any:
    """{action: approve|reject|ready|unready|clear} or {notes}."""
    from creative_intelligence.webapp.common import _REVIEW_SQL, _log_reviews
    d = request.get_json(force=True, silent=True) or {}
    conn = common.db()
    with conn:
        if "notes" in d:
            conn.execute("UPDATE adstudio_video_renders SET review_notes=? WHERE id=?", ((d["notes"] or "")[:500], rid))
        action = d.get("action")
        if action in _REVIEW_SQL:
            conn.execute(f"UPDATE adstudio_video_renders SET {_REVIEW_SQL[action]}, reviewed_at=datetime('now')"
                         " WHERE id=?", (rid,))
    if d.get("action") in _REVIEW_SQL:
        _log_reviews(conn, "adstudio_video_renders", [rid], d["action"])
    conn.close()
    return jsonify({"ok": True})


@bp.post("/api/adstudio/video-render/<int:rid>/favorite")
def video_render_favorite(rid: int) -> Any:
    conn = common.db()
    with conn:
        conn.execute("UPDATE adstudio_video_renders SET is_favorite=1-is_favorite WHERE id=?", (rid,))
    conn.close()
    return jsonify({"ok": True})


@bp.post("/api/adstudio/video-render/<int:rid>/delete")
def video_render_delete(rid: int) -> Any:
    conn = common.db()
    with conn:
        conn.execute("DELETE FROM adstudio_video_renders WHERE id=?", (rid,))
    conn.close()
    return jsonify({"ok": True})


@bp.get("/api/adstudio/videos")
def videos_list() -> Any:
    """?product_id: clips for a product, newest first, with their state."""
    conn = common.db()
    rows = [dict(r) for r in conn.execute(
        "SELECT id, source_kind, source_id, motion_preset, duration_s, still_strategy, status, step, message, created_at"
        " FROM adstudio_videos WHERE product_id=? ORDER BY id DESC LIMIT 50", (int(request.args.get("product_id") or 0),))]
    conn.close()
    return jsonify({"videos": rows})


# ── Shot library (V2) ──
@bp.get("/api/adstudio/shots")
def shots_library() -> Any:
    """?product_id: every recipe card with its best passing shot, or what's missing (and the estimate)."""
    from creative_intelligence.adstudio import shots
    conn = common.db()
    pid = int(request.args.get("product_id") or 0)
    lib = shots.library(conn, pid)
    real = shots.real_footage(conn, pid)
    conn.close()
    passing = [x for x in lib if x["status"] == "passing"]
    return jsonify({"cards": lib, "passing": len(passing), "total": len(lib),
                    "spent_usd": round(sum(x["spent_total"] for x in lib), 2),
                    "reuses": sum((x["shot"] or {}).get("reuse_count", 0) for x in passing),
                    "real": real, "real_kinds": shots.real_kinds()})


@bp.post("/api/adstudio/shots/real")
def shots_upload_real() -> Any:
    """Multipart: file, product_id, kind, note, filmed_by, filmed_on, consent=1 (brief S4: real footage)."""
    from creative_intelligence.adstudio import shots
    f = request.files.get("file")
    if f is None:
        return jsonify({"error": "Choose a video file."}), 400
    form = request.form
    conn = common.db()
    vid, why = shots.upload_real(conn, int(form.get("product_id") or 0), f.filename or "", f.read(), form.get("kind", ""),
                                 form.get("note", ""), form.get("filmed_by", ""), form.get("filmed_on", ""),
                                 form.get("consent") == "1")
    conn.close()
    return (jsonify({"error": why}), 400) if vid is None else jsonify({"video_id": vid})


@bp.post("/api/adstudio/shots")
def shots_generate() -> Any:
    """{product_id, shot_type}: library first; otherwise queue the shot."""
    from creative_intelligence.adstudio import shots
    d = request.get_json(force=True, silent=True) or {}
    conn = common.db()
    vid, why, reused = shots.create(conn, int(d.get("product_id") or 0), d.get("shot_type", ""))
    conn.close()
    if vid is None:
        return jsonify({"error": why}), 400
    return jsonify({"video_id": vid, "reused": reused})


@bp.post("/api/adstudio/shots/rescore")
def shots_rescore() -> Any:
    """{product_id}: score again the product's shots that were made but never scored (nothing is made again)."""
    from creative_intelligence import jobs
    from creative_intelligence.adstudio import shots
    d = request.get_json(force=True, silent=True) or {}
    conn = common.db()
    try:
        ids = shots.unscored(conn, int(d.get("product_id") or 0))
        for vid in ids:
            jobs.enqueue(conn, "shot_rescore", {"video_id": vid}, dedupe_key=f"shot_rescore:{vid}")
    finally:
        conn.close()
    return jsonify({"started": len(ids)})


@bp.post("/api/adstudio/shots/fill")
def shots_fill() -> Any:
    """{product_id, per_type?, estimate?}: Fill the library, up to per_type passing takes of every shot type."""
    from creative_intelligence.adstudio import shots
    d = request.get_json(force=True, silent=True) or {}
    pid, n = int(d.get("product_id") or 0), max(1, min(5, int(d.get("per_type") or shots.KEEP)))
    conn = common.db()
    try:
        out = shots.fill_plan(conn, pid, n) if d.get("estimate") else shots.fill(conn, pid, n)
    finally:
        conn.close()
    return (jsonify(out), 400) if out.get("error") else jsonify(out)


@bp.post("/api/adstudio/shot/<int:vid>/retire")
def shot_retire(vid: int) -> Any:
    from creative_intelligence.adstudio import shots
    conn = common.db()
    ok = shots.retire(conn, vid)
    conn.close()
    return jsonify({"ok": ok}), (200 if ok else 404)


@bp.get("/api/adstudio/video/<int:vid>/clip")
def video_clip(vid: int) -> Any:
    conn = common.db()
    v = conn.execute("SELECT clip_path FROM adstudio_videos WHERE id=?", (vid,)).fetchone()
    conn.close()
    if v is None or not v["clip_path"] or not Path(v["clip_path"]).exists():
        return jsonify({"error": "not found"}), 404
    return send_file(v["clip_path"], mimetype="video/mp4", conditional=True)


@bp.get("/api/adstudio/video/<int:vid>/poster")
def video_poster(vid: int) -> Any:
    from creative_intelligence.adstudio import videotools
    conn = common.db()
    v = conn.execute("SELECT clip_path FROM adstudio_videos WHERE id=?", (vid,)).fetchone()
    conn.close()
    if v is None or not v["clip_path"] or not Path(v["clip_path"]).exists():
        return jsonify({"error": "not found"}), 404
    poster = Path(v["clip_path"]).with_suffix(".jpg")
    if not poster.exists():
        videotools.frame_at(v["clip_path"], poster, 1.0, 540)
    return send_file(str(poster), mimetype="image/jpeg")


@bp.post("/api/adstudio/video/<int:vid>/stop")
def video_stop(vid: int) -> Any:
    """Stop a clip or shot that is queued or being made; it ends before its next paid step."""
    from creative_intelligence.adstudio import animate
    conn = common.db()
    ok = animate.request_stop(conn, vid)
    conn.close()
    return jsonify({"ok": ok}) if ok else (jsonify({"error": "It isn't running."}), 400)


# ── Storytelling videos (Video Studio V3) ───────────────────────────────────
@bp.get("/api/adstudio/story/options")
def story_options() -> Any:
    from creative_intelligence import config
    from creative_intelligence.adstudio import hub, story
    pid = int(request.args.get("product_id") or 0)
    conn = common.db()
    p = _product_dict(conn, pid)
    if p is None:
        conn.close()
        return jsonify({"error": "product not found"}), 404
    out = {"voices": [{k: v[k] for k in ("name", "gender", "tone")} for v in config.HOUSE_VOICES],
           "default_voice": config.DEFAULT_VOICE, "lengths": list(story.LENGTHS),
           "hooks": [{"id": r["id"], "headline": r["headline"] or r["hook_text"]} for r in conn.execute(
               "SELECT id, headline, hook_text FROM adstudio_hooks WHERE product_id=? ORDER BY id DESC", (pid,))],
           "shots": story.shot_menu(conn, pid), "music": story.music_catalog(),
           "readiness": hub.readiness(p, hub.stats(conn, pid).get(pid, {}))["video"]}
    conn.close()
    return jsonify(out)


@bp.post("/api/adstudio/story/script")
def story_script() -> Any:
    """Body {product_id, seconds, hook_id?, idea?, voice?, allow_new?} -> the script, its storyboard and the plan
    (one Claude call). Without allow_new the script uses only shots already in the library."""
    from creative_intelligence import config
    from creative_intelligence.adstudio import story
    d = request.get_json(force=True, silent=True) or {}
    pid = int(d.get("product_id") or 0)
    conn = common.db()
    p = _product_dict(conn, pid)
    if p is None:
        conn.close()
        return jsonify({"error": "product not found"}), 404
    hook = ""
    if d.get("hook_id"):
        r = conn.execute("SELECT headline, hook_text FROM adstudio_hooks WHERE id=? AND product_id=?",
                         (int(d["hook_id"]), pid)).fetchone()
        hook = (r["hook_text"] or r["headline"]) if r else ""
    seconds = int(d.get("seconds") or 30)
    if seconds not in story.LENGTHS:
        conn.close()
        return jsonify({"error": f"Pick a length of {', '.join(map(str, story.LENGTHS))} s."}), 400
    from creative_intelligence.adstudio import ideas
    try:
        idea, idea_id = ideas.pick_one(conn, pid, "video", (d.get("idea") or "").strip(), d.get("idea_id"),
                                       style=story.STYLE)
        s = story.write_script(conn, p, seconds, hook, idea, allow_new=bool(d.get("allow_new")))
        s["idea_id"] = idea_id
        s["plan"] = story.plan(conn, pid, s, d.get("voice") or config.DEFAULT_VOICE)
    except Exception as exc:
        log.exception("story script failed")
        conn.close()
        return jsonify({"error": str(exc)[:300]}), 400
    conn.close()
    return jsonify({"script": s})


@bp.post("/api/adstudio/story/plan")
def story_plan() -> Any:
    """Body {product_id, script, voice} -> the plan again after the script or voice was edited (no paid calls)."""
    from creative_intelligence.adstudio import story
    d = request.get_json(force=True, silent=True) or {}
    conn = common.db()
    try:
        out = story.plan(conn, int(d.get("product_id") or 0), d.get("script") or {"lines": []}, d.get("voice") or "")
    except ValueError as exc:
        conn.close()
        return jsonify({"error": str(exc)}), 400
    conn.close()
    return jsonify({"plan": out})


@bp.post("/api/adstudio/story")
def story_start() -> Any:
    """Body {product_id, script, voice, generate_missing?, music?} -> {video_id}."""
    from creative_intelligence.adstudio import story
    d = request.get_json(force=True, silent=True) or {}
    pid = int(d.get("product_id") or 0)
    conn = common.db()
    if _product_dict(conn, pid) is None:
        conn.close()
        return jsonify({"error": "product not found"}), 404
    vid, why = story.create(conn, pid, d.get("script") or {}, d.get("voice") or "", bool(d.get("generate_missing")),
                            d.get("music") or "")
    conn.close()
    return (jsonify({"error": why}), 400) if vid is None else jsonify({"video_id": vid})


# ── Winning video ads decoded into templates (Video Studio V4) ──────────────
@bp.get("/api/adstudio/winners")
def winners_list() -> Any:
    from creative_intelligence.adstudio import winners
    conn = common.db()
    out = {"candidates": winners.candidates(conn), "templates": winners.listing(conn), "estimate": winners.estimate(conn),
           "min_spend": winners.MIN_SPEND}
    conn.close()
    return jsonify(out)


@bp.post("/api/adstudio/winners/decode")
def winners_decode() -> Any:
    """Body {ad_ids?: [...]}: decode those (or the current top candidates). Read-only on Meta."""
    from creative_intelligence import budget
    from creative_intelligence.adstudio import winners
    d = request.get_json(force=True, silent=True) or {}
    conn = common.db()
    cands = winners.candidates(conn, n=50)
    want = [str(x) for x in d.get("ad_ids") or []]
    ads = [c for c in cands if c["ad_id"] in want] + [{"ad_id": a, "ad_name": ""} for a in want
                                                       if a not in {c["ad_id"] for c in cands}] if want else cands[:winners.TOP_N]
    try:
        ids = winners.queue(conn, ads)
    except budget.BudgetExceeded as exc:
        conn.close()
        return jsonify({"error": str(exc)}), 400
    conn.close()
    return jsonify({"queued": ids})


@bp.post("/api/adstudio/winners/<int:tid>/video")
def winners_upload(tid: int) -> Any:
    """Upload the ad's video (when Meta won't hand it over) and decode it again from the full video."""
    from creative_intelligence.adstudio import winners
    f = request.files.get("file")
    if f is None:
        return jsonify({"error": "no file"}), 400
    raw = f.read()
    if not raw:
        return jsonify({"error": "empty file"}), 400
    conn = common.db()
    why = winners.attach_upload(conn, tid, raw, f.filename or "video.mp4")
    conn.close()
    return (jsonify({"error": why}), 400) if why else jsonify({"ok": True})


@bp.post("/api/adstudio/winners/redecode")
def winners_redecode() -> Any:
    """Decode the thumbnail-only templates again (after a token with Page access is added)."""
    from creative_intelligence.adstudio import winners
    conn = common.db()
    ids = winners.redecode_partial(conn)
    conn.close()
    return jsonify({"queued": ids})


# ── AI presenters (Video Studio V5) ─────────────────────────────────────────
@bp.get("/api/adstudio/presenters")
def presenters_list() -> Any:
    from creative_intelligence import config
    from creative_intelligence.adstudio import presenters
    conn = common.db()
    out = {"presenters": presenters.listing(conn), "estimate_looks": presenters.estimate(conn, presenters.LOOKS_PER_RUN),
           "estimate_sheet": presenters.estimate(conn, len(presenters.SHEET)),
           "voices": [{k: v[k] for k in ("name", "gender", "tone")} for v in config.HOUSE_VOICES]}
    conn.close()
    return jsonify(out)


@bp.post("/api/adstudio/presenters/<int:pid>")
def presenters_update(pid: int) -> Any:
    from creative_intelligence.adstudio import presenters
    conn = common.db()
    why = presenters.update(conn, pid, request.get_json(force=True, silent=True) or {})
    conn.close()
    return (jsonify({"error": why}), 400) if why else jsonify({"ok": True})


@bp.post("/api/adstudio/presenters/<int:pid>/looks")
def presenters_looks(pid: int) -> Any:
    """Body {kind: portrait|sheet, n?}. Queues the images (generation cap)."""
    from creative_intelligence import budget
    from creative_intelligence.adstudio import presenters
    d = request.get_json(force=True, silent=True) or {}
    conn = common.db()
    try:
        ids = presenters.make_sheet(conn, pid) if d.get("kind") == "sheet" else presenters.make_looks(conn, pid, int(d.get("n") or 2))
    except (ValueError, budget.BudgetExceeded) as exc:
        conn.close()
        return jsonify({"error": str(exc)}), 400
    conn.close()
    return jsonify({"queued": ids})


@bp.post("/api/adstudio/presenter-look/<int:lid>")
def presenter_look_status(lid: int) -> Any:
    from creative_intelligence.adstudio import presenters
    conn = common.db()
    why = presenters.set_look_status(conn, lid, (request.get_json(force=True, silent=True) or {}).get("status", ""))
    conn.close()
    return (jsonify({"error": why}), 400) if why else jsonify({"ok": True})


@bp.get("/api/adstudio/presenter-look/<int:lid>/image")
def presenter_look_image(lid: int) -> Any:
    conn = common.db()
    r = conn.execute("SELECT path FROM presenter_looks WHERE id=?", (lid,)).fetchone()
    conn.close()
    if r is None or not r[0] or not Path(r[0]).exists():
        return ("", 404)
    return send_file(r[0], conditional=True)


@bp.get("/api/adstudio/voices/audition")
def voice_audition() -> Any:
    """?voice=<ElevenLabs voice>&line=…: hear any voice read a line, to pick a presenter's voice (made once, kept)."""
    from creative_intelligence import budget
    from creative_intelligence.adstudio import voices
    conn = common.db()
    try:
        path = voices.audition(conn, request.args.get("voice") or "", request.args.get("line") or "",
                               engine=request.args.get("engine") or "eleven", style=request.args.get("style") or "")
    except (ValueError, budget.BudgetExceeded) as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:                        # a model's own error: say which (a wrong voice name, mostly)
        return jsonify({"error": f"The voice model failed: {str(exc)[:300]}"}), 502
    finally:
        conn.close()
    return send_file(path, conditional=True)


@bp.get("/api/adstudio/voices/<name>/sample")
def voice_sample(name: str) -> Any:
    """The presenter card's voice player: made on the first play, then served from the volume."""
    from creative_intelligence import budget
    from creative_intelligence.adstudio import voices
    conn = common.db()
    try:
        path = voices.sample(conn, name)
    except (ValueError, budget.BudgetExceeded) as exc:
        return jsonify({"error": str(exc)}), 400
    finally:
        conn.close()
    return send_file(path, conditional=True)


@bp.get("/api/adstudio/talking")
def talking_list() -> Any:
    from creative_intelligence.adstudio import talking
    conn = common.db()
    out = {"clips": talking.clips(conn), "methods": {k: {"label": v["label"], "kind": v["kind"]} for k, v in talking.METHODS.items()},
           "compare": list(talking.COMPARE)}
    conn.close()
    return jsonify(out)


@bp.post("/api/adstudio/talking")
def talking_start() -> Any:
    """Body {presenter_id, product_id, line, method | compare: true}. The line must already be claims-checked."""
    from creative_intelligence.adstudio import facts, talking
    d = request.get_json(force=True, silent=True) or {}
    pid, product_id, line = int(d.get("presenter_id") or 0), int(d.get("product_id") or 0), (d.get("line") or "").strip()
    if not line:
        return jsonify({"error": "Write the line first."}), 400
    conn = common.db()
    prod = _product_dict(conn, product_id)
    if prod is None:
        conn.close()
        return jsonify({"error": "product not found"}), 404
    from creative_intelligence.adstudio import presenters
    chk = facts.check_copy(conn, prod, {"line": line}, allow=facts.MEMORIES if presenters.tells_memories(conn, pid) else "")
    if chk.get("status") == "flagged":
        conn.close()
        return jsonify({"error": "The line makes a claim that isn't an approved fact: "
                        + facts.summary_line(chk) + ". Rewrite it first."}), 400
    if d.get("compare"):
        ids, why = talking.compare(conn, pid, product_id, line)
    else:
        vid, why = talking.create(conn, pid, product_id, line, d.get("method") or talking.DEFAULT)
        ids = [vid] if vid else []
    conn.close()
    return (jsonify({"error": why, "queued": ids}), 400) if why else jsonify({"queued": ids})


@bp.get("/api/adstudio/talking/<int:vid>/file")
def talking_file(vid: int) -> Any:
    conn = common.db()
    r = conn.execute("SELECT clip_path FROM adstudio_videos WHERE id=? AND style='talking'", (vid,)).fetchone()
    conn.close()
    if r is None or not r[0] or not Path(r[0]).exists():
        return ("", 404)
    return send_file(r[0], conditional=True)


# ── Video styles and variants (Video Studio V6) ─────────────────────────────
@bp.get("/api/adstudio/styles")
def styles_options() -> Any:
    from creative_intelligence.adstudio import presenters, styles, talking, video_batch, winners
    conn = common.db()
    out = {"styles": {k: v["label"] for k, v in styles.catalog().items()}, "batch_styles": video_batch.batch_styles(),
           "catalog": styles.catalog(), "mode_labels": styles.MODE_LABELS,
           "presenters": [{"id": p["id"], "name": p["name"]} for p in presenters.listing(conn) if p["status"] == "approved"],
           "methods": {k: v["label"] for k, v in talking.METHODS.items()}, "default_method": styles.PICKED_TALKING,
           "templates": [{"id": t["id"], "name": t["ad_name"], "source": t["source"]} for t in winners.listing(conn) if t["status"] == "done"]}
    conn.close()
    return jsonify(out)


@bp.post("/api/adstudio/styles/script")
def styles_script() -> Any:
    """Body {product_id, style, seconds, template_id?, idea?} -> the script with kinds, 3 hooks and 15 s marks."""
    from creative_intelligence.adstudio import styles, winners
    d = request.get_json(force=True, silent=True) or {}
    conn = common.db()
    p = _product_dict(conn, int(d.get("product_id") or 0))
    if p is None:
        conn.close()
        return jsonify({"error": "product not found"}), 404
    tpl = None
    if d.get("style") == "template":
        tpl = next((t["template"] for t in winners.listing(conn) if t["id"] == int(d.get("template_id") or 0)), None)
        if not tpl:
            conn.close()
            return jsonify({"error": "Pick a decoded winning ad."}), 400
    from creative_intelligence import knowledge
    if "character" in (next((x for x in knowledge.load("styles")["styles"] if x["key"] == d.get("style")), {})
                       .get("inputs") or []):
        from creative_intelligence.adstudio import characters
        ready = characters.prepare(conn, p["id"])          # the character and its scene shot, made if missing
        if ready["step"] == "failed":                        # still drawing: the script doesn't need the look
            conn.close()                                     # (2026-10-30: waiting for it took minutes), the render does
            return jsonify({"error": ready["text"]}), 400
    from creative_intelligence.adstudio import style_research, video_batch
    why = style_research.prepare(conn, p, d.get("style") or "")     # How to eat, Comment reply: research if needed
    if why:
        conn.close()
        return jsonify({"error": why}), 400
    if d.get("style") == "comment_reply" and not d.get("comment_id"):
        d["comment_id"] = next(iter(video_batch._picks(conn, p["id"], "comment_reply")), None)
    try:
        from creative_intelligence.adstudio import ideas
        idea, idea_id = ideas.pick_one(conn, p["id"], "video", (d.get("idea") or "").strip(), d.get("idea_id"),
                                       style=d.get("style") or "")
        s = styles.write(conn, p, d.get("style") or "", int(d.get("seconds") or 30), tpl, idea,
                         mode=d.get("mode"), comment_id=d.get("comment_id"), plan=True,   # missing shots: made on Make
                         presenter_id=d.get("presenter_id"))
        s["idea_id"] = idea_id
        s["menu"] = [{"type": m["type"], "label": m["label"], "in_library": bool(m.get("in_library", True))}
                     for m in styles.menu_for(conn, p["id"], s["style"], plan=True)]
    except Exception as exc:
        log.exception("styled script failed")
        conn.close()
        return jsonify({"error": str(exc)[:300]}), 400
    conn.close()
    return jsonify({"script": s})


@bp.post("/api/adstudio/styles/estimate")
def styles_estimate() -> Any:
    """Body {script, voice, presenter_id?, method?, hooks, lengths, aspects, captions} -> variants and cost (no paid calls)."""
    from creative_intelligence.adstudio import styles
    d = request.get_json(force=True, silent=True) or {}
    conn = common.db()
    try:
        vs = styles.variants(d["script"], int(d.get("hooks") or 3), tuple(d.get("lengths") or ("full", "short")),
                             tuple(d.get("aspects") or ("9:16", "4:5")), tuple(d.get("captions") or ("bold",)))
        est = styles.estimate(conn, d["script"], vs, d.get("voice") or "Wade", d.get("presenter_id"), d.get("method") or styles.DEFAULT_TALKING)
        if d.get("product_id"):                          # shots the script needs that aren't made yet
            est["shots"] = sorted(styles.new_shots(conn, int(d["product_id"]), d["script"]))
            est["shots_usd"] = round(styles.new_shots_usd(conn, int(d["product_id"]), d["script"])
                                     + styles.own_shots_usd(conn, int(d["product_id"]), d["script"]), 2)
            est["total"] = round(est["total"] + est["shots_usd"], 2)
            est["first"] = round(est["first"] + est["shots_usd"], 2)
    except (KeyError, ValueError) as exc:
        conn.close()
        return jsonify({"error": str(exc)}), 400
    conn.close()
    return jsonify({"variants": vs, "estimate": est})


@bp.post("/api/adstudio/styles")
def styles_start() -> Any:
    from creative_intelligence.adstudio import styles
    d = request.get_json(force=True, silent=True) or {}
    conn = common.db()
    s = d.get("script") or {}
    try:
        vs = styles.variants(s, int(d.get("hooks") or 3), tuple(d.get("lengths") or ("full", "short")),
                             tuple(d.get("aspects") or ("9:16", "4:5")), tuple(d.get("captions") or ("bold",)))
    except (KeyError, ValueError, IndexError) as exc:
        conn.close()
        return jsonify({"error": f"bad script: {exc}"}), 400
    vid, why = styles.create(conn, int(d.get("product_id") or 0), s, d.get("voice") or "", d.get("presenter_id"),
                             d.get("method") or styles.DEFAULT_TALKING, vs, d.get("music") or "", d.get("template_id"),
                             make_shots=True)
    conn.close()
    return (jsonify({"error": why}), 400) if vid is None else jsonify({"video_id": vid})


# ── Several videos at once (F6) ─────────────────────────────────────────────
@bp.post("/api/adstudio/video-batch")
def video_batch_create() -> Any:
    from creative_intelligence.adstudio import video_batch
    conn = common.db()
    bid, why = video_batch.create(conn, request.get_json(force=True, silent=True) or {})
    conn.close()
    return (jsonify({"error": why}), 400) if bid is None else jsonify({"batch_id": bid})


@bp.get("/api/adstudio/video-batch/<int:bid>")
def video_batch_status(bid: int) -> Any:
    from creative_intelligence.adstudio import video_batch
    conn = common.db()
    out = video_batch.status(conn, bid)
    conn.close()
    return (jsonify(out), 200) if out else (jsonify({"error": "not found"}), 404)


@bp.post("/api/adstudio/video-batch/<int:bid>/edit")
def video_batch_edit(bid: int) -> Any:
    """Body {index, keep?, line?, text?, remove_line?, hook?, remove_hook?}: change the storyboard before approving it."""
    from creative_intelligence.adstudio import video_batch
    d = request.get_json(force=True, silent=True) or {}
    conn = common.db()
    why = video_batch.edit(conn, bid, int(d.get("index", -1)), d)
    out = video_batch.status(conn, bid)
    conn.close()
    return (jsonify({"error": why}), 400) if why else jsonify(out)


@bp.post("/api/adstudio/video-batch/<int:bid>/<step>")
def video_batch_step(bid: int, step: str) -> Any:
    """approve (storyboard → shots → drafts), judge {index, ok} (a draft), finals (every other version)."""
    from creative_intelligence.adstudio import video_batch
    d = request.get_json(force=True, silent=True) or {}
    conn = common.db()
    if step == "approve":
        out = video_batch.approve(conn, bid)
    elif step == "judge":
        why = video_batch.judge(conn, bid, int(d.get("index", -1)), bool(d.get("ok")))
        out = {"error": why} if why else video_batch.status(conn, bid)
    elif step == "finals":
        out = video_batch.finals(conn, bid)
    elif step == "retry":                   # only the videos that stopped are made again
        out = video_batch.retry(conn, bid)
    elif step == "remake":                  # the model test: {engine, indexes} → a new batch on that model alone
        nb, why = video_batch.remake(conn, bid, str(d.get("engine") or ""), [int(x) for x in d.get("indexes") or []])
        out = {"error": why} if why else {"batch_id": nb}
    else:
        out = {"error": "unknown step"}
    conn.close()
    return (jsonify(out), 400) if out.get("error") else jsonify(out)


# ── Fruit characters (brief S2 #1) ──────────────────────────────────────────
@bp.get("/api/adstudio/characters")
def characters_list() -> Any:
    """The product's characters with their look sheets, the one next step, and what the scene shot needs."""
    from creative_intelligence import config
    from creative_intelligence.adstudio import characters, shots
    conn = common.db()
    pid = int(request.args.get("product_id") or 0)
    scene = {"status": "none", "estimate": shots.estimate(conn, shots.card("character_scene"), True)}
    if (ch := characters.for_product(conn, pid)) is not None:
        if shots.passing(conn, pid, "character_scene"):
            scene["status"] = "passing"
        elif conn.execute("SELECT 1 FROM adstudio_videos WHERE product_id=? AND style='shot' AND shot_type='character_scene'"
                          " AND status IN ('queued','running') AND instr(COALESCE(prompt_version, ''), ?) > 0",
                          (pid, f"+character:{ch['id']}+")).fetchone():
            scene["status"] = "making"
    out = {"characters": characters.listing(conn, pid), "next": characters.next_step(conn, pid), "scene": scene,
           "estimate_sheets": characters.estimate(conn), "estimate_one": characters.estimate(conn, 1),
           "estimate_cast": characters.estimate(conn, len(characters.CAST)), "cast_size": len(characters.CAST),
           "voices": [v["name"] for v in config.HOUSE_VOICES]}
    conn.close()
    return jsonify(out)


@bp.post("/api/adstudio/characters/make")
def characters_make() -> Any:
    """Body {product_id}: "Make characters": four caricature characters from the template (no Claude call) and one
    look sheet each, queued (generation cap; the UI shows the estimate in a confirm first)."""
    from creative_intelligence.adstudio import characters
    d = request.get_json(force=True, silent=True) or {}
    conn = common.db()
    cids, sids, why = characters.make_cast(conn, int(d.get("product_id") or 0))
    conn.close()
    return (jsonify({"error": why}), 400) if not cids else jsonify({"characters": cids, "queued": sids})


@bp.post("/api/adstudio/characters")
def characters_create() -> Any:
    """Body {product_id, name, personality, voice, dos, donts}: the character's bible (a draft until a sheet is approved)."""
    from creative_intelligence.adstudio import characters
    d = request.get_json(force=True, silent=True) or {}
    conn = common.db()
    cid, why = characters.create(conn, int(d.get("product_id") or 0), d)
    conn.close()
    return (jsonify({"error": why}), 400) if cid is None else jsonify({"id": cid})


@bp.post("/api/adstudio/characters/<int:cid>/sheets")
def characters_sheets(cid: int) -> Any:
    """Body {n} (default SHEETS_PER_RUN, 1 to 4): queue look-sheet options (generation cap)."""
    from creative_intelligence import budget
    from creative_intelligence.adstudio import characters
    n = (request.get_json(force=True, silent=True) or {}).get("n") or characters.SHEETS_PER_RUN
    conn = common.db()
    try:
        ids = characters.make_sheets(conn, cid, max(1, min(int(n), 4)))
    except (ValueError, budget.BudgetExceeded) as exc:
        conn.close()
        return jsonify({"error": str(exc)}), 400
    conn.close()
    return jsonify({"queued": ids})


@bp.post("/api/adstudio/character-sheet/<int:sid>")
def character_sheet_status(sid: int) -> Any:
    """Body {status: approved|rejected}. Approving makes the character usable."""
    from creative_intelligence.adstudio import characters
    conn = common.db()
    why = characters.set_sheet_status(conn, sid, (request.get_json(force=True, silent=True) or {}).get("status", ""))
    conn.close()
    return (jsonify({"error": why}), 400) if why else jsonify({"ok": True})


@bp.get("/api/adstudio/character-sheet/<int:sid>/image")
def character_sheet_image(sid: int) -> Any:
    conn = common.db()
    r = conn.execute("SELECT path FROM character_sheets WHERE id=?", (sid,)).fetchone()
    conn.close()
    if r is None or not r[0] or not Path(r[0]).exists():
        return ("", 404)
    return send_file(r[0], conditional=True)


# ── Video › Edit: change a finished video's script, settings or shots, or ask for a change; a new version ──
@bp.get("/api/adstudio/video-edit")
def video_edit_list() -> Any:
    from creative_intelligence.adstudio import video_edit
    conn = common.db()
    try:
        return jsonify({"videos": video_edit.editable(conn, int(request.args.get("product_id") or 0))})
    finally:
        conn.close()


@bp.get("/api/adstudio/video-edit/<int:vid>")
def video_edit_load(vid: int) -> Any:
    from creative_intelligence.adstudio import video_edit
    conn = common.db()
    try:
        v = video_edit.load(conn, vid)
        return jsonify(v) if v else (jsonify({"error": "That video isn't editable."}), 404)
    finally:
        conn.close()


@bp.post("/api/adstudio/video-edit/<int:vid>/revise")
def video_edit_revise(vid: int) -> Any:
    """Body {script, change}: Claude rewrites the (edited) script around the request. Nothing is made yet."""
    from creative_intelligence.adstudio import styles, video_edit
    d = request.get_json(force=True, silent=True) or {}
    conn = common.db()
    try:
        v = video_edit.load(conn, vid)
        if v is None:
            return jsonify({"error": "That video isn't editable."}), 404
        s = {**v["script"], **(d.get("script") or {}), "style": v["script"]["style"]}
        out = styles.revise(conn, _product_dict(conn, v["product_id"]) or {}, s, d.get("change") or "")
        return jsonify({"script": out, "problems": styles.claim_problems(out.get("claims") or {})
                        if (out.get("claims") or {}).get("status") == "flagged" else []})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    finally:
        conn.close()


@bp.post("/api/adstudio/video-edit/<int:vid>/check")
def video_edit_check(vid: int) -> Any:
    """Body {script}: claims-check a hand-edited script (and price the new version) before making it."""
    from creative_intelligence.adstudio import styles, video_edit
    d = request.get_json(force=True, silent=True) or {}
    conn = common.db()
    try:
        v = video_edit.load(conn, vid)
        if v is None:
            return jsonify({"error": "That video isn't editable."}), 404
        s = {**v["script"], **(d.get("script") or {}), "style": v["script"]["style"]}
        s["claims"] = styles._check(conn, _product_dict(conn, v["product_id"]) or {}, s)
        st = {**v["settings"], **(d.get("settings") or {})}
        est = styles.estimate(conn, s, v["variants"], st.get("voice") or "Wade", st.get("presenter_id"),
                              st.get("method") or styles.DEFAULT_TALKING)
        shots_usd = styles.new_shots_usd(conn, v["product_id"], s)
        return jsonify({"claims": s["claims"], "problems": styles.claim_problems(s["claims"])
                        if s["claims"].get("status") == "flagged" else [],
                        "estimate": {**est, "shots_usd": shots_usd, "total": round(est["total"] + shots_usd, 2)}})
    finally:
        conn.close()


@bp.post("/api/adstudio/video-edit/<int:vid>/version")
def video_edit_version(vid: int) -> Any:
    """Body {script, settings, new_takes, prompts?: {shot_type: {still, motion}}}: make the edited script as a new
    video (the original stays). A shot whose prompt was edited gets a fresh take made from it."""
    from creative_intelligence.adstudio import styles, video_edit
    d = request.get_json(force=True, silent=True) or {}
    conn = common.db()
    try:
        v = video_edit.load(conn, vid)
        if v is None:
            return jsonify({"error": "That video isn't editable."}), 404
        s = {**v["script"], **(d.get("script") or {})}
        s["claims"] = styles._check(conn, _product_dict(conn, v["product_id"]) or {}, s)
        prompts = {k: v for k, v in (d.get("prompts") or {}).items() if isinstance(v, dict)}   # edited shot prompts
        new, why = video_edit.version(conn, vid, s, d.get("settings") or {},
                                      new_takes=sorted(set(d.get("new_takes") or []) | set(prompts)), prompts=prompts)
        return (jsonify({"error": why}), 400) if new is None else jsonify({"video_id": new})
    finally:
        conn.close()



# ── Which video model makes which shot best: one still, one take per model, scored alike ──
@bp.post("/api/adstudio/shots/compare")
def shots_compare() -> Any:
    """Body {product_id, shot_types: [...], routes?, confirm?}: without confirm, the cost; with it, the comparison."""
    from creative_intelligence import jobs
    from creative_intelligence.adstudio import shots
    d = request.get_json(force=True, silent=True) or {}
    pid = int(d.get("product_id") or 0)
    routes = tuple(r for r in d.get("routes") or shots.COMPARE_ROUTES if r in (*shots.COMPARE_ROUTES, "kling_pro"))
    types = [t for t in d.get("shot_types") or [] if t in __import__("creative_intelligence.prompts", fromlist=["x"]).shot_cards()]
    if not types or not routes:
        return jsonify({"error": "Pick shot types and models."}), 400
    conn = common.db()
    try:
        est = shots.compare_estimate(conn, types, routes)
        if not d.get("confirm"):
            return jsonify({"estimate": est, "takes": len(types) * len(routes)})
        for t in types:
            jobs.enqueue(conn, "shot_compare", {"product_id": pid, "shot_type": t, "routes": list(routes)},
                         dedupe_key=f"shot_compare:{pid}:{t}")
        return jsonify({"queued": len(types), "estimate": est})
    finally:
        conn.close()


@bp.get("/api/adstudio/shots/compare")
def shots_compare_results() -> Any:
    from creative_intelligence.adstudio import shots
    conn = common.db()
    try:
        return jsonify({"takes": shots.compare_results(conn, int(request.args.get("product_id") or 0))})
    finally:
        conn.close()


@bp.post("/api/adstudio/shot/<int:vid>/promote")
def shot_promote(vid: int) -> Any:
    """A comparison take that passed QA becomes the product's library shot of its type."""
    from creative_intelligence.adstudio import shots
    conn = common.db()
    try:
        why = shots.promote(conn, vid)
        return (jsonify({"error": why}), 400) if why else jsonify({"ok": True})
    finally:
        conn.close()


# ── Free: type what you want to see, straight to a video model (Seedance by default) ──
@bp.post("/api/adstudio/free-video")
def free_video_start() -> Any:
    """Body {product_id, prompt, image_id?, model?, seconds?, estimate?}: with estimate, the cost only."""
    from creative_intelligence.adstudio import free_video
    d = request.get_json(force=True, silent=True) or {}
    conn = common.db()
    try:
        route, secs = d.get("model") or "seedance", int(d.get("seconds") or 5)
        img = int(d["image_id"]) if str(d.get("image_id") or "").isdigit() else None
        sound = d.get("sound", True) is not False
        if d.get("estimate"):
            return jsonify({"estimate": free_video.estimate(conn, route if route in free_video.MODELS else "seedance",
                                                            secs, img, sound, d.get("cast") or ""), "models": free_video.MODELS,
                            "seconds": [s for s in free_video.SECONDS if s <= free_video.MAX_S.get(route, 99)],
                            "cast": free_video.cast_options(conn, int(d.get("product_id") or 0))})
        vid, why = free_video.create(conn, int(d.get("product_id") or 0), d.get("prompt") or "", img, route, secs, sound,
                                     d.get("cast") or "")
        return (jsonify({"error": why}), 400) if vid is None else jsonify({"video_id": vid})
    finally:
        conn.close()
