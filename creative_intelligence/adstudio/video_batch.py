"""
Several videos at once, storyboard first (feedback 2026-10-22, F6 + the video process the user approved):

  1 storyboard  a worker job writes every script as a storyboard: each line on a shot, made or not yet, with 6 hooks
                that each open on a different shot. Only Claude text is spent. Lines can be edited or removed, whole
                videos dropped.
  2 shots       approving the storyboard makes the shots the kept videos use that aren't in the library yet, plus each
                video's own: its opening and two beats in its format's look (shots.LOOKS; feedback 2026-10-23: one
                library clip per shot type made every format look alike). A shot cut off by a restart is made again
                once; a shot that fails QA never stops a video: its beat takes another passing shot (listed as a swap).
  3 drafts      when the shots are in, each video renders one draft (first hook, full length, 9:16) to watch.
  4 finals      approved drafts get every other version (all hooks × lengths × sizes), reusing the draft's voice
                lines and presenter clips; everything else runs through the usual styled-video job (QA, captions).
A job (`video_batch`, step advance) moves the batch from shots to drafts on its own, checking every minute.
Styles that need a hand-picked input (a winning ad, a real comment) stay single-video.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Callable

MAX_VIDEOS = 24
HOOKS = 6
SKIP_STYLES = {"listicle_video"}
ANIMATE = "animate"            # Animated still: one of the best approved images, gently moved (no script)
DRAFT = {"hook": 0, "length": "full", "aspect": "9:16", "captions": "bold"}
CHECK_EVERY_S = 20            # how often a batch looks for finished shots and drafts (was 60)
# per video: its own take of each shot type it shows (feedback 2026-11-03: "we are reusing them too many times";
# with only the opening and two beats its own, one Cherry Mango macro played in 7 videos). Was 3.
OWN_SHOTS = 6
WRITERS = 4                    # storyboards written at once
RESTARTED = "Interrupted by an app restart"


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def batch_styles() -> dict[str, str]:
    """Every format can be made several at a time (feedback 2026-10-23): those that need a pick get one in turn
    (the best approved images, our decoded winners, real comments)."""
    from creative_intelligence.adstudio import styles
    return {**{k: v["label"] for k, v in styles.catalog().items() if k not in SKIP_STYLES}, ANIMATE: "Animated still"}


def _picks(conn, pid: int, style: str) -> list[Any]:
    """What a format that needs a pick rotates through, best first."""
    if style == ANIMATE:
        return [r[0] for r in conn.execute(
            "SELECT i.id FROM adstudio_images i LEFT JOIN qa_scores q ON q.source='image' AND q.source_id=i.id"
            " WHERE i.product_id=? AND i.review_status='approved' AND i.image_path IS NOT NULL AND i.archived=0"
            " ORDER BY COALESCE(q.total, 0) DESC, i.id DESC", (pid,))]
    if style == "template":
        from creative_intelligence.adstudio import winners
        return [t for t in winners.listing(conn) if t["status"] == "done" and t.get("origin", "own") == "own"]
    if style == "comment_reply":                      # real Page comments first, then real questions people ask
        from creative_intelligence.adstudio import reviews
        return [r["id"] for k in ("comment", "question") for r in reviews.listing(conn, pid, k)]
    return [None]


def fit_length(style: str, seconds: int) -> int:
    """The batch's length, kept inside the format's own range (knowledge/styles.yaml length_s). Feedback 2026-11-11:
    every format was written at 30 s, so a clay video with three clay clips ran 28 s and looped one of them."""
    from creative_intelligence.adstudio import styles
    rng = styles.catalog().get(style, {}).get("length_s") or []
    if len(rng) == 2:
        return max(int(rng[0]), min(int(rng[1]), seconds))
    return seconds

NO_PICK = {ANIMATE: "Approve some images first (Images › Review).",
           "template": "Decode a winning video ad first (Video › Create › Our winners).",
           "comment_reply": "No real comment or question to answer yet: import the Page comments."}


def _load(conn, bid: int) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]] | None:
    b = conn.execute("SELECT * FROM video_batches WHERE id=?", (bid,)).fetchone()
    return (dict(b), json.loads(b["request_json"]), json.loads(b["items_json"])) if b else None


def _save(conn, bid: int, items: list[dict[str, Any]], **cols: Any) -> None:
    sets = ", ".join(f"{k}=?" for k in ["items_json", *cols])
    with conn:
        conn.execute(f"UPDATE video_batches SET {sets} WHERE id=?", (json.dumps(items), *cols.values(), bid))


# ── who speaks: a voice or presenter the request names, or each in turn (Ad Pack, feedback 2026-11-09: "how is voice
# and presenter chosen? there should be equal usage of all … or an option to choose") ──────────────────────────────
ROTATE = "rotate"


def narrator_voices() -> list[str]:
    """The house voices that may narrate anyone's video (a persona's own voice only speaks for that persona). Only the
    new engines narrate: Kokoro (Jack's old voice, kept for him on camera) read a Honey Mango video "very robotic"
    (feedback 2026-11-11, "should use new voices we have available")."""
    from creative_intelligence import config
    return [v["name"] for v in config.HOUSE_VOICES if v["name"] not in config.PERSONA_ONLY_VOICES
            and v["model"] != config.KOKORO_MODEL]


def usage(conn) -> tuple[dict[str, int], dict[int, int]]:
    """How often each voice and presenter has been used: made videos, plus the batch videos given one but not made yet."""
    voices: dict[str, int] = {}
    people: dict[int, int] = {}
    for v, cj, ai in conn.execute("SELECT voice, copy_json, ai_person FROM adstudio_videos WHERE source_kind='styled'"):
        voices[v or ""] = voices.get(v or "", 0) + 1
        if ai:
            try:
                p = int(json.loads(cj or "{}").get("presenter_id") or 0)
            except (TypeError, ValueError):
                p = 0
            people[p] = people.get(p, 0) + 1
    for (raw,) in conn.execute("SELECT items_json FROM video_batches WHERE items_json IS NOT NULL AND items_json<>''"):
        for i in json.loads(raw or "[]"):
            if i.get("draft_id"):
                continue
            if i.get("voice"):
                voices[i["voice"]] = voices.get(i["voice"], 0) + 1
            if i.get("presenter_id"):
                people[int(i["presenter_id"])] = people.get(int(i["presenter_id"]), 0) + 1
    return voices, people


def cast(conn, req: dict[str, Any], items: list[dict[str, Any]]) -> None:
    """Give each video its voice and, when its format has one, its presenter: the request's choice, or with "rotate"
    the least used so far (ties in list order), so every voice and presenter gets equal use."""
    from creative_intelligence.adstudio import styles
    rot_v, rot_p = req.get("voice") == ROTATE, req.get("presenter_id") == ROTATE
    if not (rot_v or rot_p):
        return
    voices, people = usage(conn)
    names = narrator_voices()
    ids = [r[0] for r in conn.execute("SELECT id FROM presenters WHERE status='approved' ORDER BY id")]
    cat = styles.catalog()
    for it in items:
        if it.get("style") not in cat:
            continue
        if cat[it["style"]].get("presenter") and rot_p and ids:      # a presenter-led video speaks in theirs
            pick = min(ids, key=lambda p: (people.get(p, 0), ids.index(p)))
            it["presenter_id"] = pick
            people[pick] = people.get(pick, 0) + 1
        if rot_v and names:                                            # the narrator when nobody's on camera
            pick = min(names, key=lambda v: (voices.get(v, 0), names.index(v)))
            it["voice"] = pick
            voices[pick] = voices.get(pick, 0) + 1


def who(it: dict[str, Any], req: dict[str, Any], key: str) -> Any:
    """The item's own voice or presenter, else the request's (never the word "rotate")."""
    from creative_intelligence import config
    got = it.get(key) or req.get(key)
    if got == ROTATE:
        return config.DEFAULT_VOICE if key == "voice" else None
    return got


def create(conn, req: dict[str, Any]) -> tuple[int | None, str]:
    """req {product_ids, styles: {style: count}, seconds, voice, presenter_id, method, lengths, aspects}."""
    from creative_intelligence import jobs
    pids = [int(p) for p in req.get("product_ids") or [] if str(p).isdigit()]
    counts = {k: max(0, min(3, int(v or 0))) for k, v in (req.get("styles") or {}).items() if k in batch_styles()}
    counts = {k: v for k, v in counts.items() if v}
    if not pids:
        return None, "Pick at least one product."
    if not counts:
        return None, "Pick at least one style."
    n = len(pids) * sum(counts.values())
    if n > MAX_VIDEOS:
        return None, f"{n} videos is too many at once (max {MAX_VIDEOS})."
    req = {**req, "product_ids": pids, "styles": counts}
    with conn:
        bid = conn.execute("INSERT INTO video_batches (request_json, created_at) VALUES (?,?)",
                           (json.dumps(req), _now())).lastrowid
    jobs.enqueue(conn, "video_batch", {"batch_id": bid}, dedupe_key=f"video_batch:{bid}")
    return bid, ""


def own_shots(s: dict[str, Any], tag: str, cap: int = OWN_SHOTS) -> dict[str, str]:
    """{shot_type: tag} for a video's own takes: the first hook's opening shot, then every other shot it shows."""
    from creative_intelligence.adstudio import shots, styles
    if s.get("style") in styles.NO_OWN_SHOTS:
        return {}
    order = [(s.get("hook_shots") or [None])[0]] + [ln["shot_type"] for ln in s.get("lines") or []]
    picked = [t for t in dict.fromkeys(t for t in order if t) if shots.own_ok(t)][:cap]
    return {t: tag for t in picked}


def _own_cost(conn, pid: int, s: dict[str, Any]) -> float:
    from creative_intelligence.adstudio import shots
    eng, stills = s.get("engine"), s.get("engine_stills") or {}
    return round(sum(shots.estimate(conn, {**shots.with_look(shots.card(t), s.get("style") or ""),
                                           **({"route": eng} if eng else {})}, t not in stills)
                     for t in s.get("own_shots") or {}), 2)


def retry(conn, batch_id: int) -> dict[str, Any]:
    """Make again only the videos that stopped, or whose draft was judged bad: a draft that failed (2026-11-04: Jack's voice lines all failed on a
    model error, and there was no way to redo that one video) or a model-test video stopped on a take. Shots are not
    made again; the voice lines and presenter clips the failed draft already has are reused."""
    from creative_intelligence import jobs
    got = _load(conn, batch_id)
    if got is None:
        return {"error": "Batch not found."}
    _, _, items = got
    n = 0
    for i in items:
        if not (i.get("script") and i.get("keep")):
            continue
        failed = bool(i.get("draft_id")) and (conn.execute(
            "SELECT status FROM adstudio_videos WHERE id=?", (i["draft_id"],)).fetchone() or ["failed"])[0] == "failed"
        bad = bool(i.get("draft_id")) and i.get("draft_ok") is False    # judged bad: made again from scratch
        if i.get("error") or failed or bad:
            i["error"], n = "", n + 1
            if failed or bad:
                i["redo_of"] = i.pop("draft_id")
            if bad:
                i["fresh"] = True                    # its voice lines and presenter clips aren't reused
                i.pop("draft_ok")
    if not n:
        return {"error": "Nothing stopped in this batch."}
    _save(conn, batch_id, items, status="shooting")
    jobs.enqueue(conn, "video_batch", {"batch_id": batch_id, "step": "advance"}, delay_s=1)
    return {"retried": n}


def _engine_args(s: dict[str, Any], shot_type: str) -> dict[str, str]:
    """The model test's take settings for shots.create ({} for a normal video)."""
    return {"engine": s["engine"], "still": (s.get("engine_stills") or {}).get(shot_type, "")} if s.get("engine") else {}


def remake(conn, batch_id: int, engine: str, indexes: list[int]) -> tuple[int | None, str]:
    """The model test (feedback 2026-11-04: "the quality needs to be the same on the same video … test a couple videos
    with seedance 2.5 and wan 3 and compare with current"): a new storyboard batch of the same videos, word for word
    and in the same voice, every shot made on `engine` alone from the same still the current model's take started
    from. Only videos whose every line plays over its own take can be remade this way."""
    import copy
    from creative_intelligence.adstudio import shots, styles
    from creative_intelligence.providers import replicate
    if engine not in replicate.ENGINES:
        return None, f"Unknown model {engine!r}: one of {', '.join(replicate.ENGINES)}."
    got = _load(conn, batch_id)
    if got is None:
        return None, "Batch not found."
    _, req, items = got
    out = []
    for n in indexes:
        if not 0 <= n < len(items) or not items[n].get("script"):
            return None, f"Video {n + 1} has no storyboard."
        src = items[n]
        s = copy.deepcopy(src["script"])
        talks = any(ln["kind"] == "presenter" for ln in s["lines"])
        if src["style"] in styles.OWN_SHOTS_ONLY or s.get("character") or (talks and engine != "seedance25") or \
                (src["style"] in styles.NO_OWN_SHOTS and engine != "seedance25"):
            return None, (f"{src['label']}: only videos made of shots alone can be remade on {engine} (a presenter "
                          "can speak through Seedance 2.5).")
        own = s.get("own_shots") or {}
        shown = {ln["shot_type"] for ln in s["lines"] if ln["kind"] != "text" and not (
            ln["kind"] == "presenter" and src["style"] not in styles.PIP)}    # a presenter on camera needs no shot
        if missing := sorted(shown - set(own)):
            return None, f"{src['label']}: {', '.join(missing)} had no take of its own, so it would mix models."
        s["engine_stills"] = {t: (shots.passing(conn, src["product_id"], t, tag) or {}).get("still_path") or ""
                              for t, tag in own.items()}
        s["engine_stills"] = {t: v for t, v in s["engine_stills"].items() if v}
        s["missing_shots"], s["repeat_takes"] = [], {}        # every line plays over its own take on `engine`
        was = conn.execute("SELECT copy_json FROM adstudio_videos WHERE id=?",
                           ((src.get("draft") or {}).get("video_id") or src.get("draft_id") or 0,)).fetchone()
        if was and (track := json.loads(was[0] or "{}").get("music_used")):
            s["music"] = track                                  # the same music too, so only the model differs
        s["engine"], s["engine_of"] = engine, (src.get("draft") or {}).get("video_id") or src.get("draft_id")
        out.append({**{k: src[k] for k in ("product_id", "product", "style", "label") if k in src},
                    "label": f"{src['label']} · {engine}", "keep": True, "error": "", "script": s,
                    "draft_usd": src.get("draft_usd"), "final_usd": src.get("final_usd")})
    if not out:
        return None, "Pick at least one video."
    with conn:
        bid = conn.execute("INSERT INTO video_batches (request_json, created_at) VALUES (?,?)",
                           (json.dumps({**req, "engine": engine, "remake_of": batch_id,
                                        # a presenter speaks through Seedance 2.5 too (feedback 2026-11-04)
                                        **({"method": "seedance"} if engine == "seedance25" else {})}), _now())).lastrowid
    for k, it in enumerate(out):
        tag = f"b{bid}.{k}"
        it["script"]["own_shots"] = {t: tag for t in it["script"]["own_shots"]}
        it["own_usd"] = _own_cost(conn, it["product_id"], it["script"])
        it["shots_usd"] = it["own_usd"]
    _save(conn, bid, out, status="planned", total_usd=_total(conn, out, "plan"))
    return bid, ""


def _flag(it: dict[str, Any], s: dict[str, Any]) -> None:
    """A script the claims check still flags stays on the storyboard, saying what to fix, and can be edited back in
    (live test 2026-10-26: five of fourteen videos were dropped with no way to fix the one line)."""
    from creative_intelligence.adstudio import styles
    flagged = (s.get("claims") or {}).get("status") == "flagged"
    it["claim_flags"] = styles.claim_problems(s.get("claims") or {}) if flagged else []
    it["error"] = ("Not approved: " + "; ".join(it["claim_flags"] or ["a claim"]) + ". Edit or remove that line or hook."
                   if flagged else "")
    it["keep"] = not flagged


def _shot_cost(conn, pid: int, shot_type: str) -> float:
    from creative_intelligence.adstudio import shots
    return next((s["estimate"] for s in shots.library(conn, pid) if s["type"] == shot_type), 0.0)


def _price(conn, it: dict[str, Any], req: dict[str, Any], vs: list[dict[str, Any]]) -> float:
    from creative_intelligence.adstudio import styles
    return styles.estimate(conn, it["script"], vs, who(it, req, "voice") or "Wade", who(it, req, "presenter_id"),
                           req.get("method") or styles.DEFAULT_TALKING)["total"]


def _final_variants(it: dict[str, Any], req: dict[str, Any]) -> list[dict[str, Any]]:
    from creative_intelligence.adstudio import styles
    vs = styles.variants(it["script"], HOOKS, tuple(req.get("lengths") or ("full", "short")),
                         tuple(req.get("aspects") or ("9:16", "4:5")), ("bold",))
    return [v for v in vs if v != DRAFT]


SAME = 0.8                     # two lines this alike (difflib ratio) count as the same line


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()


def sibling_lines(items: list[dict[str, Any]], it: dict[str, Any]) -> list[str]:
    """The spoken lines the batch's other videos of the same product already have."""
    return [ln["text"] for o in items if o is not it and o.get("product_id") == it["product_id"] and o.get("script")
            for ln in o["script"].get("lines") or [] if ln.get("text") and ln.get("kind") != "text"]


def repeats(s: dict[str, Any], others: list[str]) -> list[str]:
    """This script's spoken lines that another video already says (near enough word for word)."""
    from difflib import SequenceMatcher
    seen = [_norm(o) for o in others]
    return [ln["text"] for ln in s.get("lines") or [] if ln.get("text") and ln.get("kind") != "text"
            and any(SequenceMatcher(None, _norm(ln["text"]), o).ratio() >= SAME for o in seen)]


def _write_one(db: Callable, batch_id: int, req: dict[str, Any], it: dict[str, Any], pick: Any, idx: int,
               items: list[dict[str, Any]] | None = None) -> None:
    """One storyboard, on its own connection (several are written at once)."""
    from creative_intelligence.adstudio import ideas, styles
    from creative_intelligence.webapp.common import _product_dict
    conn = db()
    try:
        pid, style = it["product_id"], it["style"]
        prod = _product_dict(conn, pid)
        idea, idea_id = ideas.pick_one(conn, pid, "video", style=style)
        if req.get("direction"):            # Ad Pack: the pack's creative direction steers every script
            idea = (f"{idea}\n\n" if idea else "") + (f"CREATIVE DIRECTION FOR THIS PACK (shape the script and the hooks "
                                                     f"around it; it never adds a claim, a date or urgency the approved "
                                                     f"facts don't give): {req['direction']}")
        s = styles.write(conn, prod, style, fit_length(style, int(req.get("seconds") or 30)),
                         pick["template"] if style == "template" else None, idea,
                         comment_id=pick if style == "comment_reply" else None, plan=True, n_hooks=HOOKS,
                         angle=styles.angle_for(style, batch_id + idx),
                         presenter_id=who(it, req, "presenter_id"))
        # written side by side, the batch's videos can't see each other's lines: one that repeats a sibling's is
        # written again, once, told what the others say (feedback 2026-11-03: "the texts different from each video")
        if repeats(s, siblings := sibling_lines(items or [], it)):
            s = styles.write(conn, prod, style, fit_length(style, int(req.get("seconds") or 30)),
                             pick["template"] if style == "template" else None, idea,
                             comment_id=pick if style == "comment_reply" else None, plan=True, n_hooks=HOOKS,
                             angle=styles.angle_for(style, batch_id + idx), avoid=siblings,
                             presenter_id=who(it, req, "presenter_id"))
        if style == "template":
            it["template_id"] = pick["id"]
        if s.get("character"):
            it["character_id"] = s["character"]["id"]
        s["idea_id"] = idea_id
        # a chosen model makes every shot type the video shows (2026-11-04: seven types, six own takes, so both
        # Seedance videos fell back to Kling)
        s["own_shots"] = own_shots(s, f"b{batch_id}.{idx}", cap=99 if req.get("engine") else OWN_SHOTS)
        apply_engine(it, s, req.get("engine") or "")
        it["script"] = s
        it["own_usd"] = _own_cost(conn, pid, s)
        s["repeat_takes"] = styles.repeat_takes(conn, pid, s)
        it["shots_usd"] = round(sum(_shot_cost(conn, pid, t) for t in s.get("missing_shots") or []) + it["own_usd"]
                                + sum(_shot_cost(conn, pid, t) * n for t, n in s["repeat_takes"].items()), 2)
        it["draft_usd"] = _price(conn, it, req, [DRAFT])
        it["final_usd"] = _price(conn, it, req, _final_variants(it, req))
        _flag(it, s)
    except Exception as exc:
        it["error"] = str(exc)[:200]
    finally:
        conn.close()


def plan(db: Callable, batch_id: int) -> dict[str, Any]:
    """Stage 1, the worker job: every storyboard written and priced, WRITERS at a time (live test 2026-10-26: 14
    written one by one took ten minutes). A video that can't be written says why."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from creative_intelligence.adstudio import style_research, styles
    from creative_intelligence.webapp.common import _product_dict
    conn = db()
    try:
        _, req, _ = _load(conn, batch_id)
        items: list[dict[str, Any]] = []
        todo: list[tuple[int, Any]] = []
        for pid in req["product_ids"]:
            prod = _product_dict(conn, pid)
            for style, count in req["styles"].items():
                why = style_research.prepare(conn, prod, style) if prod else ""   # How to eat, Comment reply
                picks = _picks(conn, pid, style)
                if prod and "character" in (knowledge_style(style).get("inputs") or []):
                    from creative_intelligence.adstudio import characters
                    try:
                        characters.for_plan(conn, pid)          # made once, before the writers run side by side
                    except ValueError:
                        pass                                    # the writer says why
                for n in range(count):
                    label = "Animated still" if style == ANIMATE else styles.label(style, styles.mode_for(style, None))
                    it: dict[str, Any] = {"product_id": pid, "product": (prod or {}).get("name", ""), "style": style,
                                          "label": label, "keep": False}
                    items.append(it)
                    try:
                        if prod is None:
                            raise ValueError("product not found")
                        if why:
                            raise ValueError(why)
                        if not picks:
                            raise ValueError(NO_PICK[style])
                        pick = picks[n % len(picks)]
                        if style == ANIMATE:
                            from creative_intelligence.adstudio import animate
                            path = conn.execute("SELECT image_path FROM adstudio_images WHERE id=?", (pick,)).fetchone()[0]
                            it.update(image_id=pick, keep=True, error="", draft_usd=0.0,
                                      final_usd=animate.estimate(conn, 5, not animate._is_916(path)))
                            continue
                        todo.append((len(items) - 1, pick))
                    except Exception as exc:
                        it["error"] = str(exc)[:200]
        cast(conn, req, [x for x in items if not x.get("error")])     # before the scripts: they're written for them
        _save(conn, batch_id, items)
        with ThreadPoolExecutor(max_workers=WRITERS) as pool:
            for f in as_completed([pool.submit(_write_one, db, batch_id, req, items[i], pick, i, items) for i, pick in todo]):
                f.result()
                _save(conn, batch_id, items)
        _save(conn, batch_id, items, status="planned", total_usd=_total(conn, items, "plan"))
        return {"batch_id": batch_id, "planned": sum(i["keep"] for i in items), "of": len(items)}
    except Exception as exc:
        with conn:
            conn.execute("UPDATE video_batches SET status='failed', message=? WHERE id=?", (str(exc)[:300], batch_id))
        raise
    finally:
        conn.close()


def knowledge_style(style: str) -> dict[str, Any]:
    from creative_intelligence import knowledge
    try:
        return knowledge.style(style)
    except KeyError:
        return {}


def _total(conn, items: list[dict[str, Any]], stage: str) -> float:
    """Storyboard stage: shots (each counted once per product) + drafts. Finals stage: the remaining versions."""
    kept = [i for i in items if i.get("keep") and i.get("script") and not i.get("error")]
    if stage == "finals":
        return round(sum(i.get("final_usd") or 0 for i in kept if i.get("draft_ok")), 2)
    shots = {(i["product_id"], t) for i in kept for t in i["script"].get("missing_shots") or []}
    own = sum(i.get("own_usd") or 0 for i in kept)
    stills = [i for i in items if i.get("keep") and i.get("image_id") and not i.get("error")]
    return round(sum(_shot_cost(conn, p, t) for p, t in shots) + own + sum(i.get("draft_usd") or 0 for i in kept)
                 + sum(i.get("final_usd") or 0 for i in stills), 2)


def edit(conn, batch_id: int, idx: int, d: dict[str, Any]) -> str:
    """While the storyboard waits: {keep}, {line: n, text}, {remove_line: n}, {hook: n, text}, {remove_hook: n}.
    Edited lines and hooks are claims-checked again; a script that passes comes back in."""
    from creative_intelligence.adstudio import styles
    from creative_intelligence.webapp.common import _product_dict
    got = _load(conn, batch_id)
    if got is None or got[0]["status"] != "planned":
        return "This storyboard can't be changed now."
    _, req, items = got
    if not 0 <= idx < len(items) or not (items[idx].get("script") or items[idx].get("image_id")):
        return "No such video."
    if items[idx].get("image_id") and set(d) - {"index", "keep"}:
        return "An animated still has no lines to edit."
    it, s = items[idx], items[idx].get("script") or {}
    if "keep" in d:
        it["keep"] = bool(d["keep"]) and not it.get("error")
    if "prompt_shot" in d:                          # {prompt_shot, still?, motion?, reset?}: a take's own prompts
        t = str(d["prompt_shot"])
        if t not in (s.get("own_shots") or {}):
            return "That shot isn't made for this video."
        if d.get("reset"):
            (s.get("prompts") or {}).pop(t, None)
        else:
            cur = s.setdefault("prompts", {}).setdefault(t, {})
            for k in ("still", "motion"):
                if isinstance(d.get(k), str):
                    cur[k] = d[k].strip()[:4000]
        _save(conn, batch_id, items)
        return ""
    if "remove_line" in d:
        n = int(d["remove_line"])
        if len(s["lines"]) <= 2 or not 0 < n < len(s["lines"]) - 1:
            return "The first and last lines stay; a video needs at least two."
        s["lines"].pop(n)
    if "line" in d:
        n, text = int(d["line"]), (d.get("text") or "").strip()
        if not text or not 0 <= n < len(s["lines"]):
            return "Write the line."
        s["lines"][n]["text"] = text
        if n == 0 and s.get("hooks"):
            s["hooks"][0] = text
    if "hook" in d:
        n, text = int(d["hook"]), (d.get("text") or "").strip()
        if not text or not 0 <= n < len(s.get("hooks") or []):
            return "Write the hook."
        s["hooks"][n] = text
        if n == 0:
            s["lines"][0]["text"] = text
    if "remove_hook" in d:
        n = int(d["remove_hook"])
        if not 0 < n < len(s.get("hooks") or []):
            return "The first hook is the first line; edit it instead."
        s["hooks"].pop(n)
        if n < len(s.get("hook_shots") or []):
            s["hook_shots"].pop(n)
    if {"line", "remove_line", "hook", "remove_hook"} & set(d):
        was = bool(it.get("claim_flags"))
        s["claims"] = styles._check(conn, _product_dict(conn, it["product_id"]) or {}, s)
        keep = it["keep"]
        _flag(it, s)
        it["keep"] = it["keep"] and (keep or was)       # a fixed script comes back in; one you dropped stays dropped
        it["draft_usd"] = _price(conn, it, req, [DRAFT])
        it["final_usd"] = _price(conn, it, req, _final_variants(it, req))
    _save(conn, batch_id, items, total_usd=_total(conn, items, "plan"))
    return ""


def approve(conn, batch_id: int) -> dict[str, Any]:
    """Stage 2: one cap check for shots + drafts, then only the missing shots of the kept videos are made."""
    from creative_intelligence import budget, jobs
    from creative_intelligence.adstudio import shots, styles
    got = _load(conn, batch_id)
    if got is None or got[0]["status"] != "planned":
        return {"error": "This storyboard isn't waiting for approval."}
    _, _, items = got
    kept = [i for i in items if i.get("keep") and i.get("script") and not i.get("error")]
    stills = [i for i in items if i.get("keep") and i.get("image_id") and not i.get("error")]
    if not kept and not stills:
        return {"error": "Nothing to make: every video was removed or couldn't be written."}
    total = _total(conn, items, "plan")
    try:
        budget.check(conn, "video", total)
    except budget.BudgetExceeded as exc:
        return {"error": str(exc)}
    from creative_intelligence.adstudio import animate
    for i in stills:                               # animated stills need no storyboard or draft: made now
        vid, why = animate.create(conn, i["product_id"], "image", i["image_id"], "push_in", "", 5, {})
        i["final_id"], i["error"] = vid, why
    queued = 0
    for p, t in sorted({(i["product_id"], t) for i in kept for t in i["script"].get("missing_shots") or []}):
        if _waits_for_character(conn, kept, p, t):
            continue                                     # made by advance() once the look sheet is in
        if shots.keeps_failing(conn, p, t):              # another passing shot stands in (render), nothing is paid
            for i in kept:
                if i["product_id"] == p and t in (i["script"].get("missing_shots") or []):
                    i.setdefault("swaps", []).append(f"{t.replace('_', ' ')} keeps failing QA: another passing shot is used")
            continue
        vid, why, reused = shots.create(conn, p, t)
        queued += bool(vid and not reused)
        if vid is None:
            for i in kept:
                if i["product_id"] == p and t in (i["script"].get("missing_shots") or []):
                    i["error"] = f"Couldn't start the {t.replace('_', ' ')} shot: {why}"
    for i in kept:                                       # more takes where a few shots carry many lines (clay)
        i["extra_takes"] = styles.make_repeat_takes(conn, i["product_id"], i["script"])
        queued += len(i["extra_takes"])
    for i in kept:                                       # each video's own opening and beats, in its format's look
        eng = i["script"].get("engine") or ""
        for t, tag in (i["script"].get("own_shots") or {}).items():
            if not eng and shots.keeps_failing(conn, i["product_id"], t):
                continue                                 # its library take stands in
            mine = (i["script"].get("prompts") or {}).get(t)
            vid, why, reused = shots.create(conn, i["product_id"], t, look=tag, style=i["style"],
                                            **({"prompt": mine} if mine else {}), **_engine_args(i["script"], t))
            queued += bool(vid and not reused)
            if vid is None:
                i.setdefault("swaps", []).append(f"own {t.replace('_', ' ')} not started ({why}): the library shot is used")
    _save(conn, batch_id, items, status="shooting", total_usd=total, started_at=_now())
    jobs.enqueue(conn, "video_batch", {"batch_id": batch_id, "step": "advance"}, delay_s=5)
    return {"shots": queued, "total": total}


def _waits_for_character(conn, kept: list[dict[str, Any]], pid: int, shot_type: str) -> bool:
    """A character's scene shot needs its approved look: approve the first finished sheet, or wait for one."""
    from creative_intelligence.adstudio import characters, shots
    if not shots.card(shot_type).get("character"):
        return False
    cid = next((i.get("character_id") for i in kept if i["product_id"] == pid and i.get("character_id")), None)
    why = characters.auto_approve(conn, cid) if cid else ""
    if why and why != "wait":
        for i in kept:
            if i["product_id"] == pid and i.get("character_id") == cid:
                i["error"] = why
    return bool(why)


def advance(db: Callable, batch_id: int) -> dict[str, Any]:
    """The job between stages: when every needed shot is in (or failed), each kept video renders its draft; then it
    waits for the drafts. Checks again in a minute until there is nothing left to wait for."""
    from creative_intelligence import jobs
    from creative_intelligence.adstudio import shots, styles
    conn = db()
    try:
        got = _load(conn, batch_id)
        if got is None:
            return {"gone": True}
        b, req, items = got
        kept = [i for i in items if i.get("keep") and i.get("script") and not i.get("error")]
        if b["status"] == "shooting":
            waiting = False
            for i in kept:
                if any(conn.execute("SELECT 1 FROM adstudio_videos WHERE id=? AND status IN ('queued','running')",
                                    (x,)).fetchone() for x in i.get("extra_takes") or []):
                    waiting = waiting or not i.get("error")
                    continue
                needs = [(t, "") for t in i["script"].get("missing_shots") or []] + \
                        list((i["script"].get("own_shots") or {}).items())
                for t, tag in needs:
                    if shots.passing(conn, i["product_id"], t, tag):
                        continue
                    if not tag and _waits_for_character(conn, kept, i["product_id"], t):
                        waiting = waiting or not i.get("error")
                        continue
                    if not tag and shots.card(t).get("character") and not i.get("character_shot"):
                        shots.create(conn, i["product_id"], t)   # the look is approved now: make its scene shot (once)
                        i["character_shot"] = True
                    last = _last_shot(conn, i["product_id"], t, tag)
                    if last and last["status"] in ("queued", "running"):
                        waiting = True
                    elif last and RESTARTED in (last["message"] or "") and f"{t}|{tag}" not in i.setdefault("remade", []):
                        i["remade"].append(f"{t}|{tag}")             # cut off by a deploy: made again, once
                        shots.create(conn, i["product_id"], t, look=tag, style=i["style"], **_engine_args(i["script"], t))
                        waiting = True
                    elif tag and i["script"].get("engine"):
                        # the model test never mixes models in one video: its own best take plays even when it failed
                        # QA (marked), so the whole video can be watched; no take at all stops the video
                        eng = i["script"]["engine"]
                        if shots.best_take(conn, i["product_id"], t, tag):
                            note = f"{t.replace('_', ' ')} on {eng} didn't pass QA: its best take is used (one model)"
                            if note not in i.setdefault("swaps", []):
                                i["swaps"].append(note)
                        else:
                            i["error"] = (f"The {t.replace('_', ' ')} take on {eng} failed: this video can't be made "
                                          "on that model alone.")
                    else:
                        note = (f"own {t.replace('_', ' ')} didn't pass QA: the library one is used" if tag else
                                f"{t.replace('_', ' ')} didn't pass QA: another passing shot is used")
                        if note not in i.setdefault("swaps", []):
                            i["swaps"].append(note)
            if not waiting:
                for i in kept:
                    if i.get("error") or i.get("draft_id"):        # a draft already made (a retry redoes the rest)
                        continue
                    if i.get("style") not in styles.NO_OWN_SHOTS and not _any_passing(conn, i["product_id"]):
                        i["error"] = "No shot passed QA for this product: make one in Video › Shots."
                        continue
                    _fit_swapped(conn, i)
                    was = conn.execute("SELECT copy_json FROM adstudio_videos WHERE id=?",
                                       (0 if i.get("fresh") else i.get("redo_of") or 0,)).fetchone()
                    vid, why = styles.create(conn, i["product_id"], i["script"], who(i, req, "voice") or "",
                                             who(i, req, "presenter_id"), req.get("method") or styles.DEFAULT_TALKING, [DRAFT],
                                             "", i.get("template_id"),
                                             pieces=json.loads(was[0] or "{}").get("pieces") if was else None)
                    i["draft_id"], i["error"] = vid, why
                b["status"] = "drafting"
            _save(conn, batch_id, items, status=b["status"])
        if b["status"] == "drafting":
            ids = [i["draft_id"] for i in kept if i.get("draft_id")]
            open_ = conn.execute(f"SELECT COUNT(*) FROM adstudio_videos WHERE id IN ({','.join('?' * len(ids))})"
                                 " AND status IN ('queued','running')", ids).fetchone()[0] if ids else 0
            if not open_:
                _save(conn, batch_id, items, status="drafts")
                return {"status": "drafts"}
        jobs.enqueue(conn, "video_batch", {"batch_id": batch_id, "step": "advance"}, delay_s=CHECK_EVERY_S)
        return {"status": b["status"]}
    finally:
        conn.close()


def _fit_swapped(conn, i: dict[str, Any]) -> None:
    """Before the draft's voice is made: each line whose shot didn't make it gets the stand-in that will play, and its
    words are rewritten for that shot (styles.fit_shots). Formats with their own fixed shots are left alone."""
    from creative_intelligence.adstudio import shots, story, styles
    from creative_intelligence.webapp.common import _product_dict
    if i.get("style") in styles.NO_OWN_SHOTS or i.get("style") in styles.OWN_SHOTS_ONLY or i["script"].get("engine"):
        return                                           # (the model test: every line has its own take)
    pid, s, used, plays = i["product_id"], i["script"], set(), {}
    for n, ln in enumerate(s["lines"]):
        if ln["kind"] == "text" or shots.passing(conn, pid, ln["shot_type"]):
            continue
        sub = story._shot_for(conn, pid, ln["shot_type"], used)
        if sub:
            used.add(sub["id"])
            plays[n] = sub["shot_type"]
    if plays:
        before = [ln["text"] for ln in s["lines"]]
        i["script"] = styles.fit_shots(conn, s, _product_dict(conn, pid) or {}, plays)
        i.setdefault("swaps", []).extend(f"line {n + 1} now plays over {t.replace('_', ' ')}"
                                         + (": its words were fitted to it" if i["script"]["lines"][n]["text"] != before[n] else "")
                                         for n, t in plays.items())


def _last_shot(conn, pid: int, shot_type: str, tag: str) -> dict[str, Any] | None:
    """The newest library shot (tag '') or video's own take (its tag) of this type."""
    lk = f"+look:{tag}:" if tag else "+look:"
    r = conn.execute("SELECT status, message FROM adstudio_videos WHERE product_id=? AND style='shot' AND shot_type=?"
                     f" AND instr(COALESCE(prompt_version, ''), ?) {'>' if tag else '='} 0 ORDER BY id DESC LIMIT 1",
                     (pid, shot_type, lk)).fetchone()
    return dict(r) if r else None


def _any_passing(conn, pid: int) -> bool:
    return conn.execute("SELECT 1 FROM adstudio_videos v JOIN qa_scores q ON q.source='video' AND q.source_id=v.id"
                        " WHERE v.product_id=? AND v.style='shot' AND v.status='done' AND v.retired=0 AND q.passed=1"
                        " LIMIT 1", (pid,)).fetchone() is not None


def judge(conn, batch_id: int, idx: int, ok: bool) -> str:
    got = _load(conn, batch_id)
    if got is None or got[0]["status"] != "drafts":
        return "The drafts aren't ready."
    _, _, items = got
    if not 0 <= idx < len(items) or not items[idx].get("draft_id"):
        return "No draft for that video."
    items[idx]["draft_ok"] = bool(ok)
    _save(conn, batch_id, items, total_usd=_total(conn, items, "finals"))
    return ""


def finals(conn, batch_id: int) -> dict[str, Any]:
    """Stage 4: every other version of each approved draft, reusing its pieces."""
    from creative_intelligence import budget
    from creative_intelligence.adstudio import styles
    got = _load(conn, batch_id)
    if got is None or got[0]["status"] != "drafts":
        return {"error": "The drafts aren't ready."}
    _, req, items = got
    chosen = [i for i in items if i.get("draft_ok") and i.get("draft_id")]
    if not chosen:
        return {"error": "Approve at least one draft first."}
    total = _total(conn, items, "finals")
    try:
        budget.check(conn, "video", total)
    except budget.BudgetExceeded as exc:
        return {"error": str(exc)}
    made = 0
    for i in chosen:
        d = conn.execute("SELECT copy_json FROM adstudio_videos WHERE id=?", (i["draft_id"],)).fetchone()
        pieces = json.loads(d["copy_json"]).get("pieces") if d else {}
        vid, why = styles.create(conn, i["product_id"], i["script"], who(i, req, "voice") or "", who(i, req, "presenter_id"),
                                 req.get("method") or styles.DEFAULT_TALKING, _final_variants(i, req),
                                 template_id=i.get("template_id"), pieces=pieces)
        i["final_id"], i["error"] = vid, why or i.get("error", "")
        made += bool(vid)
    _save(conn, batch_id, items, status="finals", total_usd=total)
    return {"started": made, "total": total}


def status(conn, batch_id: int) -> dict[str, Any] | None:
    got = _load(conn, batch_id)
    if got is None:
        return None
    b, req, items = got
    from creative_intelligence.adstudio import shots, styles
    out = []
    for it in items:
        s = it.get("script") or {}
        lines = []
        for ln in s.get("lines") or []:
            on_camera = s.get("style") in styles.NO_OWN_SHOTS
            have = None if on_camera else shots.passing(conn, it["product_id"], ln["shot_type"])
            lines.append({"text": ln["text"], "kind": ln.get("kind"), "shot_type": ln["shot_type"],
                          "shot_id": have["id"] if have else None, "on_camera": on_camera})
        draft = None
        if it.get("draft_id"):
            r = conn.execute("SELECT r.id, v.status FROM adstudio_videos v LEFT JOIN adstudio_video_renders r ON"
                             " r.video_id=v.id WHERE v.id=? ORDER BY r.id LIMIT 1", (it["draft_id"],)).fetchone()
            draft = {"video_id": it["draft_id"], "render_id": r["id"] if r else None, "status": r["status"] if r else ""}
        out.append({k: it.get(k) for k in ("product_id", "product", "style", "label", "keep", "error", "claim_flags",
                                           "shots_usd", "draft_usd", "final_usd", "draft_ok", "final_id", "image_id", "own_usd")}
                   | {"lines": lines, "hooks": s.get("hooks") or [], "hook_shots": s.get("hook_shots") or [],
                      "missing_shots": s.get("missing_shots") or [], "own_shots": list(s.get("own_shots") or {}),
                      "repeat_takes": sum((s.get("repeat_takes") or {}).values()),
                      "swaps": it.get("swaps") or [], "draft": draft, "engine": s.get("engine") or "",
                      "engine_note": it.get("engine_note") or "",
                      "prompts": _prompts(conn, it) if b["status"] == "planned" else {}})
    return {"id": b["id"], "status": b["status"], "total_usd": b["total_usd"], "message": b["message"],
            "request": req, "items": out, "shots": _progress(conn, items) if b["status"] != "planned" else []}


def _prompts(conn, it: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """{shot_type: {still, motion, edited}} for the video's own takes: the person's text where they changed it."""
    from creative_intelligence.adstudio import shots
    s = it.get("script") or {}
    mine = s.get("prompts") or {}
    out = {}
    for t in s.get("own_shots") or {}:
        try:
            base = shots.prompts_for(conn, it["product_id"], t, it["style"])
        except ValueError:
            continue
        out[t] = {**base, **{k: v for k, v in (mine.get(t) or {}).items() if v}, "edited": bool(mine.get(t))}
    return out


ENGINE_LABELS = {"seedance25": "Seedance 2.5", "wan3": "Wan 3.0"}


def apply_engine(it: dict[str, Any], s: dict[str, Any], engine: str) -> None:
    """The model chosen for the batch (feedback 2026-11-04: "the option of doing seedance or kling when generating"):
    a video whose every shown line has a take of its own is made on it alone; one built on the library or on a
    format's own cards (clay, the character, ripening) stays on Kling and says so. A presenter speaks through the
    batch's own presenter method."""
    from creative_intelligence.adstudio import styles
    if not engine:
        return
    own = set(s.get("own_shots") or {})
    shown = {ln["shot_type"] for ln in s.get("lines") or [] if ln["kind"] != "text" and not (
        ln["kind"] == "presenter" and s.get("style") not in styles.PIP)}
    if s.get("style") in styles.OWN_SHOTS_ONLY or s.get("character") or not own or shown - own:
        it["engine_note"] = (f"Made on Kling: this format uses shots {ENGINE_LABELS.get(engine, engine)} can't make "
                             "for it alone (library, clay, character or ripening shots).")
        return
    s["engine"], s["missing_shots"] = engine, []


def _progress(conn, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every shot the batch makes, once: whose it is, and where it is (ready, making, failed QA, waiting)."""
    from creative_intelligence.adstudio import shots
    seen, out = set(), []
    for i in items:
        s = i.get("script") or {}
        if not (i.get("keep") and s):
            continue
        for t, tag in [(t, "") for t in s.get("missing_shots") or []] + list((s.get("own_shots") or {}).items()):
            if (i["product_id"], t, tag) in seen:
                continue
            seen.add((i["product_id"], t, tag))
            last = _last_shot(conn, i["product_id"], t, tag)
            state = ("ready" if shots.passing(conn, i["product_id"], t, tag) else "waiting" if last is None else
                     "making" if last["status"] in ("queued", "running") else "failed")
            out.append({"product": i["product"], "type": t, "label": (shots.card(t) or {}).get("label", t),
                        "for": i["label"] if tag else "shared", "state": state,
                        "message": (last or {}).get("message") or ""})
    return out
