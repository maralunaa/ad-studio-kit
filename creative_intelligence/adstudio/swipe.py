"""
Phase 2a R6: the swipe file and the taste rubric refresh.

  Swipe file   save any competitor ad, any of our own finished ads, or a screenshot or link, with notes. Inspiration
               only: never reuse another brand's footage, images, words or marks.
  Taste        monthly (and on demand), Claude looks at the swipe file and our winners and proposes taste criteria
               for the QA rubric ("macro cross-section with one bold line beats a busy flat lay"). Each proposal is
               approved or rejected one at a time; approved ones are added to the rubric through an overlay on the
               volume (taste_rules.json next to the knowledge overlay), so a redeploy keeps them. export() lists them
               for the PR that folds them into config/qa_rubric.yaml.

Claude calls are logged as source "market_research" under the research cap.
"""
from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from creative_intelligence import config

MODEL = config.CI_CLAUDE_CHECK_MODEL
KINDS = ("competitor_ad", "own_asset", "screenshot", "link")
PROPOSE_ESTIMATE_USD = 0.30
PROPOSE_EVERY = timedelta(days=28)
MAX_IMAGES = 10
APPLIES = ("images", "textads", "core", "composites", "videos")


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


# ── the swipe file ──────────────────────────────────────────────────────────
def save(conn, d: dict[str, Any]) -> tuple[int | None, str]:
    from creative_intelligence.adstudio import market
    kind = d.get("kind") or ""
    if kind not in KINDS:
        return None, "unknown kind"
    ref = int(d["ref_id"]) if str(d.get("ref_id") or "").isdigit() else None
    title, url, media = (d.get("title") or "").strip(), (d.get("url") or "").strip(), ""
    if kind == "competitor_ad":
        a = conn.execute("SELECT page_name, headline, body, url, media_path FROM competitor_ads WHERE id=?", (ref,)).fetchone()
        if a is None:
            return None, "ad not found"
        title = title or f"{a['page_name']}: {(a['headline'] or a['body'])[:80]}"
        url, media = url or a["url"], a["media_path"]
    elif kind == "own_asset":
        a = conn.execute("SELECT asset_id FROM adstudio_assets WHERE id=?", (ref,)).fetchone()
        if a is None:
            return None, "ad not found"
        title, media = title or a["asset_id"], _asset_image(conn, ref) or ""
    elif kind == "screenshot":
        media = market.safe_media(d.get("media_path") or "")
        if not media:
            return None, "Upload the screenshot first."
    elif not re.match(r"https?://\S+", url):
        return None, "Paste the full link (https://…)."
    if kind in ("competitor_ad", "own_asset") and conn.execute(
            "SELECT 1 FROM swipe_items WHERE kind=? AND ref_id=?", (kind, ref)).fetchone():
        return None, "Already in the swipe file."
    with conn:
        sid = conn.execute("INSERT INTO swipe_items (kind, ref_id, url, media_path, title, notes, created_at)"
                           " VALUES (?,?,?,?,?,?,?)", (kind, ref, url, media, title or url,
                                                       (d.get("notes") or "").strip(), _now())).lastrowid
    return sid, ""


def save_upload(conn, data: bytes, mime: str, notes: str) -> tuple[int | None, str]:
    from creative_intelligence.adstudio.market import SHOT_TYPES
    from creative_intelligence.storage import storage
    if mime not in SHOT_TYPES:
        return None, "Upload a PNG, JPEG or WebP screenshot."
    if len(data) > 8_000_000:
        return None, "That screenshot is over 8 MB."
    path = storage().put_bytes("research", f"swipe_{datetime.utcnow():%Y%m%d_%H%M%S}.{SHOT_TYPES[mime]}", data)
    return save(conn, {"kind": "screenshot", "media_path": path, "notes": notes, "title": "Screenshot"})


def _asset_image(conn, asset_pk: int) -> str | None:
    from creative_intelligence.adstudio.assets import SOURCES
    a = conn.execute("SELECT source, source_id FROM adstudio_assets WHERE id=?", (asset_pk,)).fetchone()
    if a is None or a["source"] not in SOURCES:
        return None
    table, col, _ = SOURCES[a["source"]]
    r = conn.execute(f"SELECT {col} FROM {table} WHERE id=?", (a["source_id"],)).fetchone()
    return r[0] if r and r[0] else None


def listing(conn) -> list[dict[str, Any]]:
    out = []
    for r in conn.execute("SELECT * FROM swipe_items ORDER BY id DESC"):
        d = dict(r)
        d["has_image"] = bool(d["media_path"]) and Path(d["media_path"]).suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")
        out.append(d)
    return out


def update_notes(conn, sid: int, notes: str) -> bool:
    with conn:
        return conn.execute("UPDATE swipe_items SET notes=? WHERE id=?", ((notes or "").strip(), sid)).rowcount == 1


def remove(conn, sid: int) -> None:
    with conn:
        conn.execute("DELETE FROM swipe_items WHERE id=?", (sid,))


# ── taste rubric proposals ──────────────────────────────────────────────────
def _overlay() -> Path:
    from creative_intelligence import knowledge
    return knowledge.overlay_dir() / "taste_rules.json"


def approved() -> list[dict[str, Any]]:
    """The approved taste criteria, in rubric criterion shape (the QA rubric loader adds them)."""
    p = _overlay()
    return json.loads(p.read_text()).get("criteria", []) if p.exists() else []


def _write(items: list[dict[str, Any]]) -> None:
    p = _overlay()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"criteria": items}, indent=1))
    tmp.replace(p)


PROPOSAL_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["summary", "criteria"], "properties": {
    "summary": {"type": "string"},
    "criteria": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                            "required": ["name", "question", "weight", "applies_to", "evidence"],
                                            "properties": {
                                                "name": {"type": "string", "description": "2 to 5 words"},
                                                "question": {"type": "string", "description": "scored 1 to 5 from the image"},
                                                "weight": {"type": "number", "description": "0.5 to 1"},
                                                "applies_to": {"type": "array", "items": {"type": "string", "enum": list(APPLIES)}},
                                                "evidence": {"type": "string"}}}}}}


def _image_block(path: str) -> dict[str, Any] | None:
    p = Path(path or "")
    if not p.is_file() or p.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
        return None
    from creative_intelligence.adstudio.store import to_data_uri
    uri = to_data_uri(str(p), max_dim=768)
    mime, data = uri[5:].split(";base64,", 1)
    return {"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}}


def propose(conn, client: Any = None, trigger: str = "manual") -> dict[str, Any]:
    """Claude proposes up to 5 taste criteria from the swipe file and our winners. Nothing changes until each one is
    approved."""
    from creative_intelligence import budget
    from creative_intelligence.adstudio import costs, qa
    from creative_intelligence.adstudio.chat import _client
    swipe = listing(conn)
    winners = [dict(r) for r in conn.execute(
        "SELECT a.id, a.asset_id, a.format, a.style, a.angle, a.hook_type FROM adstudio_assets a"
        " WHERE a.meta_ad_id IS NOT NULL")]
    from creative_intelligence.adstudio.assets import results
    scored = []
    for w in winners:
        r = results(conn, conn.execute("SELECT meta_ad_id FROM adstudio_assets WHERE id=?", (w["id"],)).fetchone()[0])
        if r and r.get("roas") and (r.get("spend") or 0) >= 30:
            scored.append({**w, "roas": r["roas"], "ctr": r["ctr"]})
    scored.sort(key=lambda w: -w["roas"])
    if len(swipe) + len(scored) < 3:
        return {"error": "Save at least 3 ads to the swipe file (or link winners to Meta) first."}
    budget.check_research(conn, PROPOSE_ESTIMATE_USD)
    rubric = qa.load_rubric()
    content: list[dict[str, Any]] = [{"type": "text", "text": "CURRENT RUBRIC CRITERIA (don't repeat these):\n" + "\n".join(
        f"- {c['key']}: {c['name']}: {c['question']}" for c in rubric["criteria"])}]
    shown = 0
    for s in swipe[:20]:
        content.append({"type": "text", "text": f"SWIPE ({s['kind']}): {s['title']}\nNotes: {s['notes'] or '(none)'}"})
        if shown < MAX_IMAGES and (b := _image_block(s["media_path"])):
            content.append(b)
            shown += 1
    for w in scored[:6]:
        content.append({"type": "text", "text": f"OUR WINNER {w['asset_id']}: ROAS {w['roas']}, CTR {w['ctr']}%, "
                                                f"format {w['format']}, style {w['style']}, angle {w['angle']}"})
        if shown < MAX_IMAGES and (b := _image_block(_asset_image(conn, w["id"]) or "")):
            content.append(b)
            shown += 1
    content.append({"type": "text", "text": "Propose 0 to 5 taste criteria to add to the rubric."})
    client = client or _client(log=False)
    resp = client.beta.messages.create(
        model=MODEL, max_tokens=4000, betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        system=("You refresh the brand's ad taste rubric. From the saved ads (with the operator's notes) and our "
                "winning ads, find visual or copy choices that recur in what works, and write each as a rubric "
                "criterion an image can be scored on, 1 to 5. Be specific and visual ('one macro cross-section with "
                "a single bold line', not 'looks good'). Only propose what the examples show; never one that would "
                "reward copying another brand. Weight 0.5 to 1: taste refines, it never outweighs accuracy."),
        output_config={"format": {"type": "json_schema", "schema": PROPOSAL_SCHEMA}},
        messages=[{"role": "user", "content": content}])
    cost = costs.log_tokens(conn, MODEL, "copy", resp.usage, "market_research", 0)
    if resp.stop_reason == "refusal":
        return {"error": "Claude declined to propose criteria."}
    got = json.loads(next(b.text for b in resp.content if b.type == "text"))
    have = {c["key"] for c in rubric["criteria"]}
    items = []
    for c in got["criteria"][:5]:
        key = "taste_" + (re.sub(r"[^a-z0-9]+", "_", c["name"].lower()).strip("_")[:30] or "rule")
        if key in have:
            continue
        have.add(key)
        items.append({"key": key, "name": c["name"].strip(), "question": c["question"].strip(),
                      "weight": min(1.0, max(0.5, float(c["weight"]))),
                      "applies_to": [a for a in c["applies_to"] if a in APPLIES] or ["images", "textads"],
                      "evidence": c["evidence"].strip(), "status": "pending"})
    with conn:
        pid = conn.execute("INSERT INTO taste_proposals (created_at, trigger, summary, items_json, cost_usd) VALUES (?,?,?,?,?)",
                           (_now(), trigger, got["summary"], json.dumps(items), cost)).lastrowid
    return {"id": pid, "summary": got["summary"], "items": items, "cost_usd": cost}


def proposals(conn, limit: int = 6) -> list[dict[str, Any]]:
    return [dict(r, items=json.loads(r["items_json"])) for r in
            conn.execute("SELECT id, created_at, trigger, summary, items_json, cost_usd FROM taste_proposals"
                         " ORDER BY id DESC LIMIT ?", (limit,))]


def decide(conn, proposal_id: int, index: int, approve: bool) -> str:
    """Approve (into the rubric overlay) or reject one proposed criterion. Returns '' or why not."""
    r = conn.execute("SELECT items_json FROM taste_proposals WHERE id=?", (proposal_id,)).fetchone()
    if r is None:
        return "proposal not found"
    items = json.loads(r["items_json"])
    if not 0 <= index < len(items):
        return "no such item"
    it = items[index]
    if it["status"] != "pending":
        return "Already decided."
    it["status"] = "approved" if approve else "rejected"
    it["decided_at"] = _now()
    if approve:
        rules = [c for c in approved() if c["key"] != it["key"]]
        rules.append({k: it[k] for k in ("key", "name", "question", "weight", "applies_to")}
                     | {"taste": True, "proposal": proposal_id, "approved_at": it["decided_at"]})
        _write(rules)
    with conn:
        conn.execute("UPDATE taste_proposals SET items_json=? WHERE id=?", (json.dumps(items), proposal_id))
    return ""


def retire(key: str) -> bool:
    rules = approved()
    left = [c for c in rules if c["key"] != key]
    if len(left) == len(rules):
        return False
    _write(left)
    return True


def export() -> str:
    """The approved taste criteria as YAML, for the PR that folds them into config/qa_rubric.yaml."""
    import yaml
    return yaml.safe_dump([{k: c[k] for k in ("key", "name", "question", "weight", "applies_to")} for c in approved()],
                          sort_keys=False, allow_unicode=True)


def propose_due(conn, now: datetime | None = None) -> bool:
    if conn.execute("SELECT COUNT(*) FROM swipe_items").fetchone()[0] < 3:
        return False
    r = conn.execute("SELECT MAX(created_at) FROM taste_proposals").fetchone()
    return not r[0] or datetime.fromisoformat(r[0]) <= (now or datetime.utcnow()) - PROPOSE_EVERY
