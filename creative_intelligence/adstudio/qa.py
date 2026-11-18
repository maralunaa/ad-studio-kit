"""
Quality scoring (T4) — every new image is scored against config/qa_rubric.yaml.

  Claude criteria   Claude Opus 5.5 looks at the image next to the product's reference
                    photos and scores each criterion 1–5 with one sentence of reasoning.
  Measured criteria scored by code from what the renderers report (`metrics`): legibility =
                    contrast of the drawn text against what is behind it, at phone size; word_count,
                    safe_zone and cta_match = the playbook's limits in knowledge/rules.yaml.

The total is the weighted average of the criteria that apply; an image passes when the
total reaches the rubric's threshold and no criterion is below its `min`. The rubric is
re-read on every call, so editing the YAML changes scoring with no code change.

Sources scored: image (Images tab), textad (AI Ads: the clean image when the copy is drawn
by code), core (Core shots) and composite (Compose: inherits its base image's Claude scores,
plus measured legibility).

Overrules: approving a QA-failed item, or rejecting one at or above `high_score`, is logged
in qa_reviews (every review of a scored item is logged, so the rate can be computed).
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from creative_intelligence import config as _config
from creative_intelligence import knowledge, prompts

MODEL = _config.CI_CLAUDE_SCORE_MODEL
RUBRIC_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "qa_rubric.yaml"
SOURCE_KIND = {"image": "images", "textad": "textads", "core": "core", "composite": "composites", "video": "videos"}


# ── rubric ────────────────────────────────────────────────────────────────
def load_rubric(path: str | Path | None = None) -> dict[str, Any]:
    import yaml
    p = Path(path or os.getenv("QA_RUBRIC_PATH") or RUBRIC_PATH)
    r = yaml.safe_load(p.read_text())
    if path is None:                                 # approved taste criteria (Phase 2a R6, swipe.py) join the rubric
        from creative_intelligence.adstudio import swipe
        have = {c["key"] for c in r["criteria"]}
        taste = [dict(c) for c in swipe.approved() if c["key"] not in have]
        r["criteria"] += taste
        r["taste"] = [c["key"] for c in taste]
    for c in r["criteria"]:
        c.setdefault("weight", 1)
        c.setdefault("applies_to", ["images", "textads", "core", "videos"])
    return r


def applies(c: dict[str, Any], source: str) -> bool:
    return SOURCE_KIND[source] in c["applies_to"]


def total(rubric: dict[str, Any], scores: dict[str, float], source: str) -> tuple[float | None, bool, list[str]]:
    """Weighted total over the criteria that apply and were scored; (total, passed, failures)."""
    num = den = 0.0
    fails: list[str] = []
    for c in rubric["criteria"]:
        if not applies(c, source) or scores.get(c["key"]) is None:
            continue
        s = float(scores[c["key"]])
        num += s * c["weight"]
        den += c["weight"]
        if c.get("min") is not None and s < c["min"]:
            fails.append(f"{c['name']} {s:g} is below the minimum {c['min']}")
    if not den:
        return None, False, ["nothing to score"]
    t = round(num / den, 2)
    if t < rubric["threshold"]:
        fails.insert(0, f"total {t} is below the pass mark {rubric['threshold']}")
    return t, not fails, fails


# ── measured legibility ───────────────────────────────────────────────────
def _lum(rgb: tuple[float, float, float]) -> float:
    def ch(v: float) -> float:
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = rgb
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return round((la + 0.05) / (lb + 0.05), 2)


def word_count_score(metrics: dict[str, Any] | None) -> float | None:
    """5 within the playbook's limits; 3 for a headline under the minimum (a warning); 2 for too many words.
    A real review quote is never shortened, so it doesn't count toward the total."""
    words = (metrics or {}).get("words")
    if not words:
        return None
    rules = knowledge.load("rules")
    lim = rules["on_image_text"]
    cap = rules["lofi_text"]["total_words_max"] if (metrics or {}).get("lofi") else lim["total_words_max"]
    head = words.get("headline", 0)
    total = sum(n for f, n in words.items() if f not in ("review", "author"))
    if head > lim["headline_words"]["max"] or total > cap:
        return 2.0
    if head and head < lim["headline_words"]["min"]:
        return 3.0
    return 5.0


def outside_safe_zone(metrics: dict[str, Any] | None) -> list[str]:
    """The drawn copy (by field) that falls outside the safe zone for its frame size."""
    from creative_intelligence.adstudio.render_template import aspect_key
    from creative_intelligence.adstudio.templates import safe_area
    m = metrics or {}
    if not m.get("boxes") or not m.get("frame"):
        return []
    w, h = m["frame"]
    top, bottom, side = safe_area()[aspect_key(w, h)]
    tol = 2
    return [b[4] for b in m["boxes"] if b[0] < side * w - tol or b[2] > w - side * w + tol
            or b[1] < top * h - tol or b[3] > bottom * h + tol]


def safe_zone_score(metrics: dict[str, Any] | None) -> float | None:
    if not (metrics or {}).get("boxes"):
        return None
    return 1.0 if outside_safe_zone(metrics) else 5.0


def cta_family(cta: str) -> str | None:
    """The allowed CTA (knowledge/rules.yaml) this on-image CTA is, by key; None if it isn't one."""
    norm = " ".join((cta or "").lower().replace("→", "").split()).strip(" .!")
    for key, c in knowledge.load("rules")["cta"]["allowed"].items():
        if norm == c["label"].lower():
            return key
    return None


def cta_score(metrics: dict[str, Any] | None) -> float | None:
    """5 when the on-image CTA is one of the allowed CTAs (so the Meta button can say the same thing), else 2."""
    m = metrics or {}
    if not m.get("cta"):
        return None
    return 5.0 if cta_family(m["cta"]) else 2.0


MEASURED = {"legibility": lambda m: legibility_score(m), "word_count": word_count_score,
            "safe_zone": safe_zone_score, "cta_match": cta_score}


def measured_scores(metrics: dict[str, Any] | None) -> dict[str, float]:
    """Every measured criterion the renderer gave enough to score."""
    out = {}
    for key, fn in MEASURED.items():
        v = fn(metrics)
        if v is not None:
            out[key] = v
    return out


def legibility_score(metrics: dict[str, Any] | None) -> float | None:
    """1–5 from the worst text contrast (WCAG ratio) and smallest text size at phone width."""
    if not metrics or metrics.get("min_contrast") is None:
        return None
    cr = metrics["min_contrast"]
    s = 5 if cr >= 7 else 4 if cr >= 4.5 else 3 if cr >= 3 else 2 if cr >= 2 else 1
    if (metrics.get("min_text_px_at_phone") or 99) < 9:
        s = min(s, 2)
    return float(s)


# ── Claude scoring ────────────────────────────────────────────────────────
def _schema(keys: list[str]) -> dict[str, Any]:
    return {"type": "object", "additionalProperties": False, "required": ["scores", "reasoning"],
            "properties": {
                "scores": {"type": "object", "additionalProperties": False, "required": keys,
                           "properties": {k: {"type": "integer", "enum": [1, 2, 3, 4, 5]} for k in keys}},
                "reasoning": {"type": "string", "description": "One sentence: the main reason for the score."}}}


def ask_claude(image_path: str, ref_uris: list[str], product: dict[str, Any], rubric: dict[str, Any],
               source: str, client: Any = None, scene: str = "") -> tuple[dict[str, Any], Any]:
    from creative_intelligence.adstudio.chat import _client, _image_blocks, _product_line
    from creative_intelligence.adstudio.profiles import profile
    from creative_intelligence.adstudio.textcheck import _image_block
    crit = [c for c in rubric["criteria"] if applies(c, source) and not c.get("measured")]
    client = client or _client(log=False, kind="qa")
    refs = _image_blocks(ref_uris[:3])
    lines = "\n".join(f"- {c['key']} ({c['name']}): {c['question']}" for c in crit)
    # the scene the image was asked for (2026-11-13: a candle's scent-ingredient props, a hand lighting the wick and an
    # unboxing box all failed "props beyond what is specified": the reviewer never saw what was specified)
    asked = (f"THE SCENE THIS IMAGE WAS ASKED FOR: the props, hands, people, packaging and setting it names are "
             f"specified, so they are not clutter and break no never-include rule; the product itself must still "
             f"follow every rule (its label, its look, no extra units):\n{scene.strip()[:1500]}\n\n") if scene.strip() else ""
    ask = (f"{_product_line(product)}\n\nNEVER-INCLUDE RULES for this product type:\n{profile(product)['negative']}\n\n"
           + asked +
           f"The first {len(refs)} image(s) are the product's reference photos. The LAST image is the ad image "
           f"to score.\n\nScore it 1–5 on each criterion:\n{lines}")
    resp = client.beta.messages.create(
        model=MODEL, max_tokens=4000,
        betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        system=(prompts.text("qa-score-system")),
        output_config={"format": {"type": "json_schema", "schema": _schema([c["key"] for c in crit])}},
        messages=[{"role": "user", "content": refs + [_image_block(image_path), {"type": "text", "text": ask}]}],
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("Claude declined to score this image")
    return json.loads(next(b.text for b in resp.content if b.type == "text")), resp.usage


def _log_cost(conn, usage: Any, source: str, source_id: int, run_id: int | None = None) -> float:
    from creative_intelligence.adstudio import costs
    pin, pout = costs.price(conn, MODEL), costs.price(conn, MODEL + ":output")
    tin = (getattr(usage, "input_tokens", 0) or 0) + (getattr(usage, "cache_read_input_tokens", 0) or 0)
    tout = getattr(usage, "output_tokens", 0) or 0
    cost = round(tin / 1e6 * pin + tout / 1e6 * pout, 5)
    now = datetime.utcnow().isoformat()
    with conn:
        conn.execute("INSERT INTO model_calls (at, model, kind, units, cost_usd, source, source_id, run_id)"
                     " VALUES (?,?,?,?,?,?,?,?)", (now, MODEL, "qa-score", tin + tout, cost, source, source_id,
                                                   str(run_id) if run_id else None))
    costs._api_log(f"{now} | ANTHROPIC | {MODEL} | qa-score {source}#{source_id} | {tin} in / {tout} out | ${cost:.4f}")
    return cost


def save(conn, source: str, source_id: int, rubric: dict[str, Any], scores: dict[str, Any],
         reasoning: str, cost: float = 0.0, error: str = "", measured: dict[str, Any] | None = None) -> dict[str, Any]:
    """Store the scores; `measured` (what the renderer reported) is kept for the pre-launch checklist
    (None keeps what is stored)."""
    t, passed, fails = total(rubric, scores, source)
    row = {"source": source, "source_id": source_id, "rubric_version": rubric.get("version"),
           "scores": scores, "total": t, "passed": passed, "failures": fails, "reasoning": reasoning,
           "error": error}
    with conn:
        conn.execute(
            "INSERT INTO qa_scores (source, source_id, rubric_version, scores_json, total, passed, failures_json,"
            " reasoning, error, model, cost_usd, at, measured_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(source, source_id) DO UPDATE SET rubric_version=excluded.rubric_version,"
            " scores_json=excluded.scores_json, total=excluded.total, passed=excluded.passed,"
            " failures_json=excluded.failures_json, reasoning=excluded.reasoning, error=excluded.error,"
            " model=excluded.model, cost_usd=qa_scores.cost_usd+excluded.cost_usd, at=excluded.at,"
            " measured_json=CASE WHEN ? THEN excluded.measured_json ELSE qa_scores.measured_json END",
            (source, source_id, rubric.get("version"), json.dumps(scores), t, int(passed), json.dumps(fails),
             reasoning, error, MODEL, cost, datetime.utcnow().isoformat(), json.dumps(measured or {}),
             int(measured is not None)))
    return row


def score(conn, source: str, source_id: int, image_path: str, ref_uris: list[str], product: dict[str, Any],
          legibility: dict[str, Any] | None = None, client: Any = None, run_id: int | None = None) -> dict[str, Any]:
    """Score one image (Claude criteria + measured legibility) and store it."""
    rubric = load_rubric()
    scores: dict[str, Any] = measured_scores(legibility)
    try:
        scene = ""
        if source == "image":
            row = conn.execute("SELECT concept FROM adstudio_images WHERE id=?", (source_id,)).fetchone()
            scene = (row[0] if row else "") or ""
        reading, usage = ask_claude(image_path, ref_uris, product, rubric, source, client, **({"scene": scene} if scene else {}))
        cost = _log_cost(conn, usage, source, source_id, run_id)
        scores.update(reading["scores"])
        return save(conn, source, source_id, rubric, scores, reading.get("reasoning", ""), cost, measured=legibility)
    except Exception as exc:                       # never block the image; it just stays unscored
        return save(conn, source, source_id, rubric, scores, "", error=str(exc)[:200], measured=legibility)


def inherit(conn, source: str, source_id: int, base_source: str, base_id: int,
            legibility: dict[str, Any] | None, leg_value: float | None = None) -> dict[str, Any]:
    """A drawn ad (composite) = its base image's Claude scores + its own measured legibility."""
    rubric = load_rubric()
    base = conn.execute("SELECT scores_json, reasoning FROM qa_scores WHERE source=? AND source_id=?",
                        (base_source, base_id)).fetchone()
    scores = json.loads(base["scores_json"]) if base else {}
    for k in MEASURED:
        scores.pop(k, None)
    if legibility:
        scores.update(measured_scores(legibility))
    elif leg_value is not None:
        scores["legibility"] = leg_value
    note = (base["reasoning"] if base else "Base image not scored yet; legibility only.")
    if legibility and legibility.get("min_contrast") is not None:
        note += f" Text contrast {legibility['min_contrast']}:1."
    if legibility and (out := outside_safe_zone(legibility)):
        note += f" Outside the safe zone: {', '.join(out)}."
    return save(conn, source, source_id, rubric, scores, note, measured=legibility if legibility else None)


def set_legibility(conn, source: str, source_id: int, legibility: dict[str, Any] | None) -> dict[str, Any] | None:
    """Re-score only the measured criteria (e.g. after the copy was re-drawn)."""
    r = conn.execute("SELECT scores_json, reasoning FROM qa_scores WHERE source=? AND source_id=?",
                     (source, source_id)).fetchone()
    if r is None:
        return None
    scores = json.loads(r["scores_json"] or "{}")
    for k in MEASURED:
        scores.pop(k, None)
    scores.update(measured_scores(legibility))
    return save(conn, source, source_id, load_rubric(), scores, r["reasoning"], measured=legibility or {})


def scores_for(conn, source: str, ids: list[int]) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for r in conn.execute(f"SELECT * FROM qa_scores WHERE source=? AND source_id IN ({','.join('?' * len(chunk))})",
                              [source, *chunk]):
            out[r["source_id"]] = {"total": r["total"], "passed": bool(r["passed"]), "reasoning": r["reasoning"],
                                   "scores": json.loads(r["scores_json"] or "{}"),
                                   "failures": json.loads(r["failures_json"] or "[]"), "error": r["error"],
                                   "rubric_version": r["rubric_version"]}
    return out


# ── overrules ─────────────────────────────────────────────────────────────
def log_review(conn, source: str, source_id: int, action: str) -> str | None:
    """Record a human review of a scored item; returns the overrule kind, if it is one."""
    q = conn.execute("SELECT total, passed FROM qa_scores WHERE source=? AND source_id=?", (source, source_id)).fetchone()
    if q is None or q["total"] is None or action not in ("approve", "reject", "ready"):
        return None
    rubric = load_rubric()
    kind = None
    if action in ("approve", "ready") and not q["passed"]:
        kind = "approved_qa_reject"
    elif action == "reject" and q["total"] >= rubric.get("high_score", 4.3):
        kind = "rejected_high_score"
    with conn:
        conn.execute("INSERT INTO qa_reviews (source, source_id, action, qa_total, qa_passed, overrule, at)"
                     " VALUES (?,?,?,?,?,?,?)", (source, source_id, action, q["total"], q["passed"], kind,
                                                 datetime.utcnow().isoformat()))
    return kind


def overrule_report(conn) -> dict[str, Any]:
    r = conn.execute("SELECT COUNT(*) AS n, COUNT(overrule) AS o,"
                     " SUM(overrule='approved_qa_reject') AS a, SUM(overrule='rejected_high_score') AS h"
                     " FROM qa_reviews").fetchone()
    recent = [dict(x) for x in conn.execute(
        "SELECT source, source_id, action, qa_total, overrule, at FROM qa_reviews WHERE overrule IS NOT NULL"
        " ORDER BY id DESC LIMIT 20")]
    n = r["n"] or 0
    return {"reviews": n, "overrules": r["o"] or 0, "approved_qa_reject": r["a"] or 0,
            "rejected_high_score": r["h"] or 0, "rate": round((r["o"] or 0) / n * 100, 1) if n else None,
            "recent": recent}
