"""
One-pass image runs (T5): ask for 8, 12, 16 or 20 finished images and get exactly that many,
or a clear shortfall message — without the user re-running anything.

  1. Plan     Claude plans the run as a grid across scene, camera/framing, lighting,
              surface/props and fruit state (whole / cut / in hand / in box), avoiding the
              product's scene log, and labels each slot "new concept" or "iteration".
  2. Make     a server-side worker (not the browser tab) generates 3 slots at a time with
              Nano Banana Pro and the product's reference photos, at full resolution.
  3. Check    each image is QA-scored as it arrives (qa.py). A failed slot is regenerated
              quietly with QA's reasons as corrections — at most MAX_ATTEMPTS per slot.
  4. Stop     before every generation the run budget and the daily cap are checked; the run
              stops (and says so) rather than overspend. It never loops.

Only one worker ever processes a run: it claims the run by moving it from 'queued' to
'running' in one UPDATE (a DB lock row), so a second process or click can't start it twice.
"""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Callable
from creative_intelligence import config, prompts
from creative_intelligence.adstudio import ideas

SIZES = (8, 12, 16, 20)
MAX_ATTEMPTS = 3            # first try + 2 silent replacements
PARALLEL = 3
QA_ESTIMATE_USD = 0.03      # per score; for budget checks before a slot starts
FRUIT_STATES = ("whole", "cut", "in_hand", "in_box")


# ── budget ────────────────────────────────────────────────────────────────
def budgets() -> tuple[float, float]:
    """(per-run cap, per-day generation cap), as set on Admin (budget.py)."""
    from creative_intelligence import budget
    from creative_intelligence.db import get_connection, init_db
    init_db()
    conn = get_connection()
    try:
        c = budget.caps(conn)
    finally:
        conn.close()
    return c["generation_run"], c["generation_day"]


def spent_on_run(conn, run_id: int) -> float:
    r = conn.execute("SELECT COALESCE(SUM(cost_usd),0) FROM model_calls WHERE run_id=?", (str(run_id),)).fetchone()
    return round(r[0] or 0.0, 4)


def spent_today(conn) -> float:
    """Today's image and video spend (the day starts 00:00 London)."""
    from creative_intelligence import budget
    return budget.spent_today(conn, "generation")


# ── planning ──────────────────────────────────────────────────────────────
PLAN_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["slots"],
    "properties": {"slots": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["title", "archetype", "scene", "camera", "lighting", "surface", "fruit_state", "label"],
        "properties": {
            "title": {"type": "string", "description": "3-6 words naming the shot"},
            "archetype": {"type": "string"},
            "scene": {"type": "string", "description": "One or two sentences: what the photograph shows"},
            "camera": {"type": "string", "description": "angle and framing, e.g. 'overhead flat-lay', '45° close-up'"},
            "lighting": {"type": "string", "description": "light quality and time of day"},
            "surface": {"type": "string", "description": "surface and props"},
            "fruit_state": {"type": "string", "enum": list(FRUIT_STATES)},
            "label": {"type": "string", "enum": ["new_concept", "iteration"]},
        }}}},
}


def plan_run(product: dict[str, Any], n: int, prior: list[dict[str, Any]], ref_uris: list[str],
             conn=None, run_id: int | None = None, client: Any = None) -> list[dict[str, Any]]:
    from creative_intelligence.adstudio import qa
    from creative_intelligence.adstudio.chat import _client, _image_blocks, _product_line
    from creative_intelligence.adstudio.profiles import profile
    client = client or _client(log=False)
    menu = "\n".join(f"- {k}: {v['name']} — {v['blurb']}" for k, v in profile(product)["formats"].items())
    log = "\n".join(f"- [{p.get('archetype', '')}] {p.get('title', '')}: {(p.get('concept') or '')[:140]}"
                    + (f" (QA {p['qa_total']})" if p.get("qa_total") else "") for p in prior) or "(none yet)"
    ask = (f"{_product_line(product)}\n\nArchetype menu:\n{menu}\n\nScene log — already made, do not repeat:\n{log}\n\n"
           f"Plan exactly {n} slots. Spread fruit_state across whole, cut, in_hand and in_box.")
    picks = ideas.assign(conn, product["id"], "image", n) if conn is not None and product.get("id") else [None] * n
    led = [(j, i) for j, i in enumerate(picks) if i]
    if led:                                      # the idea share of this run (rules.ideas_mix); the rest as usual
        ask += ("\n\nSome slots follow an idea from the idea bank: the photograph shows the idea (no words in it). "
                "The other slots are planned as usual.\n" + "\n\n".join(
                    f"Slot {j + 1}:\n{ideas.prompt_block(i)}" for j, i in led))
    resp = client.beta.messages.create(
        model=config.CI_CLAUDE_CHECK_MODEL, max_tokens=16000, betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        system=prompts.text("run-plan-system"), output_config={"format": {"type": "json_schema", "schema": PLAN_SCHEMA}},
        messages=[{"role": "user", "content": _image_blocks(ref_uris[:2]) + [{"type": "text", "text": ask}]}])
    if resp.stop_reason == "refusal":
        raise RuntimeError("Claude declined to plan this run")
    if conn is not None:
        log_claude(conn, resp.usage, "run-plan", "run", run_id or 0, run_id, config.CI_CLAUDE_CHECK_MODEL)
    slots = json.loads(next(b.text for b in resp.content if b.type == "text"))["slots"]
    for j, i in led:
        if j < len(slots):
            slots[j]["idea_id"] = i["id"]
            ideas.touch(conn, i["id"])
    return dedupe(slots)[:n]


def dedupe(slots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop exact repeats of (archetype, camera, lighting, surface, state) — the planner's safety net."""
    seen, out = set(), []
    for s in slots:
        key = tuple((s.get(k) or "").strip().lower() for k in ("archetype", "camera", "lighting", "surface", "fruit_state"))
        if key not in seen:
            seen.add(key)
            out.append(s)
    return out


def scene_text(slot: dict[str, Any]) -> str:
    state = {"in_hand": "held in a hand", "in_box": "in our plain white box"}.get(slot["fruit_state"], slot["fruit_state"])
    return (f"{slot['scene']}\nCamera and framing: {slot['camera']}. Lighting: {slot['lighting']}. "
            f"Surface and props: {slot['surface']}. Fruit: {state}.")


def log_claude(conn, usage: Any, kind: str, source: str, source_id: int, run_id: int | None,
               model: str | None = None) -> float:
    from creative_intelligence.adstudio import costs, qa
    model = model or qa.MODEL
    tin = (getattr(usage, "input_tokens", 0) or 0) + (getattr(usage, "cache_read_input_tokens", 0) or 0)
    tout = getattr(usage, "output_tokens", 0) or 0
    cost = round(tin / 1e6 * costs.price(conn, model) + tout / 1e6 * costs.price(conn, model + ":output"), 5)
    now = datetime.utcnow().isoformat()
    with conn:
        conn.execute("INSERT INTO model_calls (at, model, kind, units, cost_usd, source, source_id, run_id)"
                     " VALUES (?,?,?,?,?,?,?,?)", (now, model, kind, tin + tout, cost, source, source_id,
                                                   str(run_id) if run_id else None))
    costs._api_log(f"{now} | ANTHROPIC | {model} | {kind} {source}#{source_id} | {tin} in / {tout} out | ${cost:.4f}")
    return cost


# ── the run ───────────────────────────────────────────────────────────────
def create_run(conn, product_id: int, n: int, aspect: str) -> tuple[int | None, str]:
    """Queue a run if the daily cap leaves room for it. Returns (run_id, '') or (None, reason)."""
    from creative_intelligence.adstudio import costs
    from creative_intelligence.adstudio.generate import MODEL
    if n not in SIZES:
        return None, f"Pick a run size of {', '.join(map(str, SIZES))}."
    run_cap, day_cap = budgets()
    per = costs.price(conn, MODEL) + QA_ESTIMATE_USD
    if spent_today(conn) + per > day_cap:
        from creative_intelligence.budget import CAP_MESSAGE
        return None, CAP_MESSAGE
    with conn:
        cur = conn.execute("INSERT INTO adstudio_runs (product_id, size, aspect_ratio, status, budget_usd, created_at)"
                           " VALUES (?,?,?, 'queued', ?, ?)", (product_id, n, aspect, run_cap, datetime.utcnow().isoformat()))
    return cur.lastrowid, ""


def claim(conn, run_id: int) -> bool:
    with conn:
        cur = conn.execute("UPDATE adstudio_runs SET status='planning', started_at=? WHERE id=? AND status='queued'",
                           (datetime.utcnow().isoformat(), run_id))
    return cur.rowcount == 1


def process_run(conn_factory: Callable, run_id: int, *, plan: Callable, make_image: Callable,
                score: Callable, refs: Callable, product: Callable) -> dict[str, Any]:
    """Run end to end. The callables are injected so the worker is testable without APIs:

      plan(conn, product, n, prior, ref_uris, run_id) -> [slot]
      make_image(conn, image_row, prompt, ref_uris, aspect, run_id) -> image path   (logs its own cost)
      score(conn, image_id, path, ref_uris, product, run_id) -> qa result dict
      refs(conn, product_id) -> [data uri];  product(conn, product_id) -> dict
    """
    from creative_intelligence.adstudio import costs
    from creative_intelligence.adstudio.concepts import build_image_prompt
    from creative_intelligence.adstudio.generate import MODEL
    conn = conn_factory()
    if not claim(conn, run_id):
        conn.close()
        return {"skipped": True}
    run = dict(conn.execute("SELECT * FROM adstudio_runs WHERE id=?", (run_id,)).fetchone())
    pid, n, aspect = run["product_id"], run["size"], run["aspect_ratio"]
    prod, ref_uris = product(conn, pid), refs(conn, pid)
    from creative_intelligence.adstudio.profiles import prompt_stamp
    stamp = prompt_stamp(prod, "run")
    run_cap, day_cap = run["budget_usd"], budgets()[1]
    per_try = costs.price(conn, MODEL) + QA_ESTIMATE_USD
    stop: dict[str, str] = {}
    budget_lock = threading.Lock()
    inflight = [0]

    def may_spend(c) -> bool:
        """Reserve one attempt's estimated cost, or record why the run must stop."""
        with budget_lock:
            if stop:
                return False
            if cancelled(c, run_id):
                stop["reason"] = "Cancelled: no new images were started after you cancelled."
                return False
            if spent_on_run(c, run_id) + (inflight[0] + 1) * per_try > run_cap:
                stop["reason"] = f"Stopped: the run budget of ${run_cap:.2f} is reached."
                return False
            if spent_today(c) + (inflight[0] + 1) * per_try > day_cap:
                stop["reason"] = f"Stopped: today's generation cap of ${day_cap:.2f} is reached."
                return False
            inflight[0] += 1
            return True

    def release() -> None:
        with budget_lock:
            inflight[0] -= 1

    try:
        prior = [dict(r) for r in conn.execute(
            "SELECT i.archetype, i.title, i.concept, q.total AS qa_total FROM adstudio_images i"
            " LEFT JOIN qa_scores q ON q.source='image' AND q.source_id=i.id"
            " WHERE i.product_id=? AND i.archived=0 ORDER BY i.id DESC LIMIT 60", (pid,))]
        slots = plan(conn, prod, n, prior, ref_uris, run_id)
        with conn:
            for i, s in enumerate(slots):
                conn.execute("INSERT INTO adstudio_run_slots (run_id, idx, plan_json, status) VALUES (?,?,?, 'queued')",
                             (run_id, i, json.dumps(s)))
            conn.execute("UPDATE adstudio_runs SET status=CASE status WHEN 'cancelling' THEN status ELSE 'running' END,"
                         " plan_json=? WHERE id=?", (json.dumps(slots), run_id))
    except Exception as exc:
        with conn:
            conn.execute("UPDATE adstudio_runs SET status='failed', message=?, finished_at=? WHERE id=?",
                         (f"Couldn't plan the run: {str(exc)[:200]}", datetime.utcnow().isoformat(), run_id))
        conn.close()
        return {"failed": True}

    def work(slot_id: int) -> None:
        c = conn_factory()
        try:
            s = dict(c.execute("SELECT * FROM adstudio_run_slots WHERE id=?", (slot_id,)).fetchone())
            plan_ = json.loads(s["plan_json"])
            corrections, last_reason = "", ""
            for attempt in range(1, MAX_ATTEMPTS + 1):
                if not may_spend(c):
                    last_reason = stop.get("reason", last_reason)
                    break
                try:
                    prompt = build_image_prompt(prod, scene_text(plan_), aspect, corrections)
                    with c:
                        img_id = c.execute(
                            "INSERT INTO adstudio_images (product_id, archetype, title, concept, prompt, aspect_ratio,"
                            " model, status, run_id, idea_id) VALUES (?,?,?,?,?,?,?, 'generating', ?, ?)",
                            (pid, plan_["archetype"], plan_["title"], scene_text(plan_), prompt, aspect, MODEL,
                             run_id, plan_.get("idea_id"))).lastrowid
                        c.execute("UPDATE adstudio_run_slots SET status='generating', attempts=?, image_id=? WHERE id=?",
                                  (attempt, img_id, slot_id))
                        c.execute("UPDATE adstudio_images SET prompt_version=? WHERE id=?", (stamp, img_id))
                    path = make_image(c, {"id": img_id, "product_id": pid}, prompt, ref_uris, aspect, run_id)
                    with c:
                        c.execute("UPDATE adstudio_images SET status='done', image_path=? WHERE id=?", (path, img_id))
                        c.execute("UPDATE adstudio_run_slots SET status='scoring' WHERE id=?", (slot_id,))
                    q = score(c, img_id, path, ref_uris, prod, run_id)
                finally:
                    release()
                if q.get("total") is not None and q.get("passed"):
                    with c:
                        c.execute("UPDATE adstudio_run_slots SET status='done', qa_total=?, last_error='' WHERE id=?",
                                  (q["total"], slot_id))
                    return
                last_reason = "; ".join(q.get("failures") or []) or q.get("error") or "did not pass QA"
                corrections = (f"A previous attempt failed quality review: {last_reason}. "
                               f"Reviewer's note: {q.get('reasoning', '')}").strip()
                with c:                                  # keep the failed try out of the gallery
                    c.execute("UPDATE adstudio_images SET archived=1 WHERE id=?", (img_id,))
                    c.execute("UPDATE adstudio_run_slots SET qa_total=?, last_error=? WHERE id=?",
                              (q.get("total"), last_reason, slot_id))
            with c:
                c.execute("UPDATE adstudio_run_slots SET status='failed', last_error=? WHERE id=?",
                          (last_reason or "not attempted", slot_id))
        except Exception as exc:                         # one broken slot never takes the run down
            with c:
                c.execute("UPDATE adstudio_run_slots SET status='failed', last_error=? WHERE id=?",
                          (str(exc)[:200], slot_id))
        finally:
            c.close()

    ids = [r[0] for r in conn.execute("SELECT id FROM adstudio_run_slots WHERE run_id=? ORDER BY idx", (run_id,))]
    with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        list(pool.map(work, ids))

    rows = conn.execute("SELECT status, last_error FROM adstudio_run_slots WHERE run_id=?", (run_id,)).fetchall()
    done = sum(r["status"] == "done" for r in rows)
    reasons = sorted({r["last_error"] for r in rows if r["status"] != "done" and r["last_error"]})
    msg = f"{done} of {n} delivered." + (f" {stop['reason']}" if stop else "") + \
          (f" Not delivered: {' | '.join(reasons)[:400]}" if done < n and reasons else "")
    spent = spent_on_run(conn, run_id)
    with conn:
        conn.execute("UPDATE adstudio_runs SET status=?, delivered=?, spent_usd=?, message=?, finished_at=? WHERE id=?",
                     ("stopped" if stop else "done", done, spent, msg, datetime.utcnow().isoformat(), run_id))
    conn.close()
    return {"delivered": done, "of": n, "message": msg, "spent": spent}


def cancelled(conn, run_id: int) -> bool:
    r = conn.execute("SELECT status FROM adstudio_runs WHERE id=?", (run_id,)).fetchone()
    return bool(r) and r[0] == "cancelling"


def cancel(conn, run_id: int) -> bool:
    """Stop a run: images already being made finish (they are paid for); no new ones start."""
    with conn:
        if conn.execute("UPDATE adstudio_runs SET status='stopped', message='Cancelled before it started.', finished_at=?"
                        " WHERE id=? AND status='queued'", (datetime.utcnow().isoformat(), run_id)).rowcount:
            return True
        return conn.execute("UPDATE adstudio_runs SET status='cancelling' WHERE id=? AND status IN"
                            " ('planning','running')", (run_id,)).rowcount == 1


def status(conn, run_id: int) -> dict[str, Any] | None:
    r = conn.execute("SELECT * FROM adstudio_runs WHERE id=?", (run_id,)).fetchone()
    if r is None:
        return None
    run = dict(r)
    run["plan_json"] = None
    slots = [dict(x) for x in conn.execute(
        "SELECT s.id, s.idx, s.status, s.attempts, s.qa_total, s.last_error, s.image_id, s.plan_json"
        " FROM adstudio_run_slots s WHERE s.run_id=? ORDER BY s.idx", (run_id,))]
    for s in slots:
        p = json.loads(s.pop("plan_json") or "{}")
        s.update(title=p.get("title", ""), label=p.get("label", ""), fruit_state=p.get("fruit_state", ""))
        if s["status"] == "failed" and s["image_id"]:   # what failed, to see (feedback 2026-11-13: "i want to see
            img = conn.execute("SELECT image_path FROM adstudio_images WHERE id=?", (s["image_id"],)).fetchone()
            q = conn.execute("SELECT reasoning FROM qa_scores WHERE source='image' AND source_id=? ORDER BY id DESC"
                             " LIMIT 1", (s["image_id"],)).fetchone()   # what is failing"): its last try and why
            s["has_image"] = bool(img and img["image_path"])
            s["qa_note"] = (q["reasoning"] if q else "") or ""
    run["slots"] = slots
    run["done"] = sum(s["status"] == "done" for s in slots)
    run["spent_usd"] = spent_on_run(conn, run_id)
    finished = [s for s in slots if s["status"] in ("done", "failed")]
    if run.get("started_at") and finished and run["status"] == "running":
        elapsed = (datetime.utcnow() - datetime.fromisoformat(run["started_at"])).total_seconds()
        left = len(slots) - len(finished)
        run["eta_seconds"] = int(elapsed / len(finished) * left)
    return run


def start_in_background(conn_factory: Callable, run_id: int) -> int:
    """Queue the run for the worker process. Returns the job id."""
    from creative_intelligence import jobs
    conn = conn_factory()
    try:
        return jobs.enqueue(conn, "image_run", {"run_id": run_id}, dedupe_key=f"run:{run_id}")
    finally:
        conn.close()


def mark_interrupted(conn) -> int:
    """At startup: runs left 'planning'/'running' by a restart are closed with a clear message."""
    with conn:
        cur = conn.execute("UPDATE adstudio_runs SET status='stopped', message=COALESCE(message,'') ||"
                           " ' Interrupted by an app restart.', finished_at=? WHERE status IN ('planning','running','cancelling')",
                           (datetime.utcnow().isoformat(),))
        conn.execute("UPDATE adstudio_run_slots SET status='failed', last_error='interrupted by an app restart'"
                     " WHERE status IN ('queued','generating','scoring') AND run_id IN"
                     " (SELECT id FROM adstudio_runs WHERE status='stopped')")
    return cur.rowcount
