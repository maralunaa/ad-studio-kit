"""
Text check for AI Ads — the image model drew the copy, so read it back and compare.

Claude (vision) transcribes every word on the finished ad and compares each expected
field (headline, subhead, CTA, price when one is set) with what was drawn. The pass/fail
decision is made here in code, not by the model:

  fail  the headline (or price) is missing, any field is misspelled, OR its exact text is not in the transcript
        (normalised for case, spacing, quotes and dashes), OR a price / number / claim
        appears that the brief did not ask for
  pass  everything matches; any harmless extra text is listed as a note. A subhead or CTA the design left off is a
        note too (A2, 2026-10-23: four good mostly-text-free ads were re-made twice and rejected for leaving "Shop now"
        to the Meta button)

A failed ad is auto-rejected in the gallery with the reason shown. Every call is logged
with its token cost (model_calls, outputs/api.log).

Interim safety net for T3: once AI Ads renders copy through Compose, the image model no
longer draws text and this check only guards older ads.
"""
from __future__ import annotations

import base64
import io
import json
import re
from datetime import datetime
from typing import Any

from creative_intelligence import config as _config
from creative_intelligence import prompts

MODEL = _config.CI_CLAUDE_SCORE_MODEL
FIELDS = ("headline", "subhead", "cta", "price")

SCHEMA = {
    "type": "object",
    "properties": {
        "transcript": {"type": "string", "description": "Every word visible in the image, top to bottom, exactly as drawn."},
        "fields": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string", "enum": list(FIELDS)},
                    "rendered": {"type": "string", "description": "What the image actually shows for this field ('' if absent)."},
                    "status": {"type": "string", "enum": ["exact", "case_or_punctuation_only", "misspelled", "missing"]},
                },
                "required": ["field", "rendered", "status"],
                "additionalProperties": False,
            },
        },
        "extra_text": {"type": "array", "items": {"type": "string"},
                       "description": "Any visible text that is not part of the expected copy."},
        "repeated_text": {"type": "array", "items": {"type": "string"},
                          "description": "Any word or line that is drawn more than once when the copy has it once."},
    },
    "required": ["transcript", "fields", "extra_text", "repeated_text"],
    "additionalProperties": False,
}

SYSTEM = prompts.text("text-check-system")


def norm(text: str | None) -> str:
    t = (text or "").casefold()
    t = t.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    t = re.sub(r"[–—-]", "-", t)
    t = re.sub(r"[^\w$£€%'\-]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _image_block(path: str) -> dict[str, Any]:
    """Downscale to <=1568px JPEG so the request stays small."""
    from PIL import Image
    im = Image.open(path).convert("RGB")
    im.thumbnail((1568, 1568))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90)
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.standard_b64encode(buf.getvalue()).decode()}}


OPTIONAL = ("subhead", "cta")          # a design may leave these off; drawn wrong is still a failure


def decide(expected: dict[str, str], reading: dict[str, Any]) -> dict[str, Any]:
    """Pass/fail from the model's reading plus an exact check against the transcript."""
    transcript = norm(reading.get("transcript"))
    by = {f["field"]: f for f in reading.get("fields", [])}
    problems: list[str] = []
    notes: list[str] = []
    for field in FIELDS:
        want = (expected.get(field) or "").strip()
        if not want:
            continue
        got = by.get(field, {"status": "missing", "rendered": ""})
        if got["status"] == "missing" and field in OPTIONAL and norm(want) not in transcript:
            notes.append(f"{field} left off: “{want}”" + (" (the Meta button carries it)" if field == "cta" else ""))
        elif got["status"] in ("missing", "misspelled"):
            problems.append(f"{field} {got['status']}: expected “{want}”"
                            + (f", image shows “{got['rendered']}”" if got.get("rendered") else ""))
        elif norm(want) not in transcript:
            problems.append(f"{field} not found exactly: expected “{want}”, image reads “{got.get('rendered') or reading.get('transcript', '')[:80]}”")
    for rep_ in reading.get("repeated_text", []):
        problems.append(f"text drawn twice: “{rep_}”")
    allowed = norm(" ".join(expected.get(f) or "" for f in FIELDS))
    for extra in reading.get("extra_text", []):
        e = norm(extra)
        if not e or e in allowed:
            continue
        if re.search(r"[\d$£€%]", extra):
            problems.append(f"unexpected number or price in the image: “{extra}”")
        else:
            notes.append(f"extra text: “{extra}”")
    return {"status": "fail" if problems else "pass", "problems": problems, "notes": notes,
            "transcript": reading.get("transcript", "")}


def read_image(path: str, expected: dict[str, str], client: Any = None) -> tuple[dict[str, Any], Any]:
    """Ask Claude to transcribe and compare. Returns (reading, usage)."""
    from creative_intelligence.adstudio.chat import _client
    client = client or _client(log=False)
    want = "\n".join(f"- {f}: “{expected[f]}”" for f in FIELDS if (expected.get(f) or "").strip()) \
        or "- (no copy expected — the image should contain no words)"
    resp = client.beta.messages.create(
        model=MODEL,
        max_tokens=4000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=SYSTEM,
        output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": [
            _image_block(path),
            {"type": "text", "text": "Expected copy on this ad:\n" + want +
             "\n\nTranscribe the image, then report each expected field's status and any other visible text."},
        ]}],
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("Claude declined to read this image")
    text = next(b.text for b in resp.content if b.type == "text")
    return json.loads(text), resp.usage


def log_cost(conn, usage: Any, source: str, source_id: int) -> float:
    """Token cost from model_prices (Claude Opus 5.5 input/output per 1M tokens)."""
    from creative_intelligence.adstudio import costs
    costs.seed_prices(conn)
    pin = costs.price(conn, MODEL)
    pout = costs.price(conn, MODEL + ":output")
    tin = (getattr(usage, "input_tokens", 0) or 0) + (getattr(usage, "cache_read_input_tokens", 0) or 0)
    tout = getattr(usage, "output_tokens", 0) or 0
    cost = round(tin / 1e6 * pin + tout / 1e6 * pout, 5)
    now = datetime.utcnow().isoformat()
    with conn:
        conn.execute("INSERT INTO model_calls (at, model, kind, units, cost_usd, source, source_id)"
                     " VALUES (?,?,?,?,?,?,?)", (now, MODEL, "text-check", tin + tout, cost, source, source_id))
    costs._api_log(f"{now} | ANTHROPIC | {MODEL} | text-check {source}#{source_id} | {tin} in / {tout} out | ${cost:.4f}")
    return cost


def check_textad(conn, ad: dict[str, Any], client: Any = None) -> dict[str, Any]:
    """Check one AI ad, store the result, and auto-reject it if the text is wrong."""
    expected = {"headline": ad.get("headline") or "", "subhead": ad.get("subhead") or "",
                "cta": ad.get("cta") or "", "price": ad.get("price") or ""}
    from creative_intelligence.adstudio.textads import VARIANTS
    fields = (VARIANTS.get(ad.get("variant") or "") or {}).get("fields")
    if fields is not None and ad.get("kind") == "variant":     # only what the format prints (Minimal: nothing), so a
        for k, f in (("headline", "headline"), ("subhead", "sub"), ("cta", "cta")):   # retry never paints the rest
            if f not in fields:
                expected[k] = ""
    try:
        reading, usage = read_image(ad["image_path"], expected, client)
        log_cost(conn, usage, "textad", ad["id"])
        result = decide(expected, reading)
    except Exception as exc:  # network, refusal, unreadable file — never block the ad, just say so
        result = {"status": "error", "problems": [str(exc)[:200]], "notes": [], "transcript": ""}
    now = datetime.utcnow().isoformat()
    with conn:
        conn.execute("UPDATE adstudio_textads SET textcheck_status=?, textcheck_json=?, textcheck_at=? WHERE id=?",
                     (result["status"], json.dumps(result), now, ad["id"]))
        if result["status"] == "fail":
            reason = "Auto-rejected by text check: " + "; ".join(result["problems"])
            conn.execute("UPDATE adstudio_textads SET review_status='rejected', is_ready=0, text_checked=0,"
                         " review_notes=CASE WHEN COALESCE(review_notes,'')='' THEN ? ELSE review_notes || char(10) || ? END,"
                         " reviewed_at=? WHERE id=?", (reason, reason, now, ad["id"]))
    return result
