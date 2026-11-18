"""
Updating the knowledge base from a new playbook export (Admin → Knowledge, K3).

  1. upload()    the owner pastes or uploads the new markdown export. It is stored, never live, with a section-by-
                 section diff against the playbook in force and the knowledge values whose source changed.
  2. propose()   A worker job: Claude reads the changed sections and the values, rubric criteria and prompts
                 that come from them, and proposes updates, each a separate item (before, after, why).
  3. decide()    Each item is approved or rejected by hand. Nothing changes before that:
                   playbook   adopt the new export as the playbook in force (overlay on the volume)
                   knowledge  the new value goes live through the overlay (knowledge.py), checked first
                   rubric / prompt  approved for a PR (wording lives in files under review); shown as such
                 Approving several knowledge items together applies them at once, so a renamed section and
                 the values that cite it can move together.
  4. export()    Everything approved, as one JSON bundle, for the PR that folds it into the repo.

The diff is plain code; only propose() calls Claude (budget-checked, cost logged).
"""
from __future__ import annotations

import difflib
import json
import re
from datetime import datetime
from typing import Any

from creative_intelligence import config, knowledge

MODEL = config.CI_CLAUDE_CHECK_MODEL
TARGETS = ("playbook", "knowledge", "rubric", "prompt")

SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["proposals"],
    "properties": {"proposals": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["target", "key", "after", "section", "reason"],
        "properties": {
            "target": {"type": "string", "enum": ["knowledge", "rubric", "prompt"]},
            "key": {"type": "string", "description": "knowledge: dotted key (rules.cta.allowed.shop_now.label); "
                                                      "rubric: criterion key; prompt: prompt file name"},
            "after": {"type": "string", "description": "knowledge: the new value as YAML; rubric/prompt: the new wording"},
            "section": {"type": "string", "description": "the playbook section the change comes from"},
            "reason": {"type": "string", "description": "one sentence: what changed in the playbook"}}}}}}

SYSTEM = (
    "You keep the brand's ad app in step with its Meta Creative Playbook. You get the playbook sections that "
    "changed (old and new) and the machine-readable values, rubric criteria and prompts that come from them. "
    "Propose the smallest set of updates that makes them match the new playbook: one item per value. For a "
    "knowledge value give the full new value as YAML, keeping its shape (same keys and types). Never propose "
    "anything the playbook didn't change, never weaken the house rules (no origin claims, approved facts only, "
    "prices only from Shopify, real reviews only, AI disclosure, presenters are never customers or a farmer), "
    "and never propose invented reviews or testimonials.")


def _sections(md: str) -> dict[str, str]:
    return {k: v.strip() for k, v in knowledge._sections(md).items()}


def diff(old_md: str, new_md: str) -> list[dict[str, Any]]:
    """Per H2 section: added, removed or changed, with a unified diff of the change."""
    old, new = _sections(old_md), _sections(new_md)
    out = []
    for name in list(old) + [n for n in new if n not in old]:
        if name not in new:
            out.append({"section": name, "change": "removed", "diff": ""})
        elif name not in old:
            out.append({"section": name, "change": "added", "diff": new[name][:4000]})
        elif old[name] != new[name]:
            lines = difflib.unified_diff(old[name].splitlines(), new[name].splitlines(), lineterm="", n=1)
            out.append({"section": name, "change": "changed", "diff": "\n".join(list(lines)[2:])[:6000]})
    return out


def affected(changes: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Knowledge values whose source section changed or went away."""
    names = {c["section"] for c in changes}
    return [{"file": f, "where": w, "source": s} for f, w, s in knowledge.sources()
            if s.split("›")[0].strip() in names]


def upload(conn, markdown: str) -> int:
    """Store a new export (never live) with its diff; queue Claude's proposals."""
    from creative_intelligence import jobs
    md = (markdown or "").strip()
    if "\n## " not in "\n" + md:
        raise ValueError("That doesn't look like the playbook export (no sections).")
    m = re.search(r"Exported: (\d{4}-\d{2}-\d{2}) · doc revision (\d+)", md)
    changes = diff(knowledge.playbook(), md)
    with conn:
        uid = conn.execute(
            "INSERT INTO knowledge_updates (created_at, revision, exported, playbook_md, diff_json, status)"
            " VALUES (?,?,?,?,?, ?)",
            (datetime.utcnow().isoformat(timespec="seconds"), int(m[2]) if m else None, m[1] if m else None, md + "\n",
             json.dumps(changes), "queued" if changes else "ready")).lastrowid
    if changes:
        jobs.enqueue(conn, "knowledge_update", {"update_id": uid}, dedupe_key=f"knowledge:{uid}")
    return uid


def _context(changes: list[dict[str, Any]]) -> str:
    """What comes from the changed sections: knowledge values, rubric criteria and prompts that read them."""
    from creative_intelligence.adstudio import qa
    import yaml
    vals = affected(changes)
    lines = []
    keys = [_key_for(v["where"], v["file"]) for v in vals]
    for v, key in zip(vals, keys):
        try:
            value = knowledge.get(key)
        except KeyError:
            value = "(see file)"
        lines.append(f"- {key} (from {v['source']}):\n" + yaml.safe_dump(value, sort_keys=False, allow_unicode=True)[:1500])
    users = knowledge.used_by()
    prompts_using = sorted({p for k, ps in users.items() for p in ps
                            if any(k == key or k.startswith(key + ".") or key.startswith(k + ".") for key in keys)})
    crit = [f"- {c['key']}: {c.get('question', '').strip()} (min {c.get('min', '—')})" for c in qa.load_rubric()["criteria"]]
    return ("KNOWLEDGE VALUES FROM THE CHANGED SECTIONS:\n" + ("\n".join(lines) or "(none)")
            + "\n\nRUBRIC CRITERIA (config/qa_rubric.yaml):\n" + "\n".join(crit)
            + "\n\nPROMPTS THAT READ THESE VALUES: " + (", ".join(prompts_using) or "(none)"))


def _key_for(where: str, file: str) -> str:
    """knowledge.sources() path → the dotted key get()/set_value() take ("styles[fruit_story].when" → styles.fruit_story.when)."""
    k = f"{file}.{where}"
    k = re.sub(r"^styles\.styles\[([a-z0-9_]+)\]", r"styles.\1", k)
    return re.sub(r"\[([a-z0-9_]+)\]", r".\1", k)


def propose(db, update_id: int, client: Any = None) -> dict[str, Any]:
    """Worker job: ask Claude for the updates, one reviewable item each; the first item adopts the export."""
    import yaml
    from creative_intelligence.adstudio import costs
    from creative_intelligence.adstudio.chat import _client
    conn = db()
    try:
        u = conn.execute("SELECT * FROM knowledge_updates WHERE id=?", (update_id,)).fetchone()
        changes = json.loads(u["diff_json"])
        with conn:
            conn.execute("UPDATE knowledge_updates SET status='running' WHERE id=?", (update_id,))
        items: list[dict[str, Any]] = [{"target": "playbook", "key": "meta-creative-playbook.md",
                                        "before": f"revision {knowledge.playbook_info()['revision']}",
                                        "after": f"revision {u['revision'] or '?'} ({u['exported'] or 'no date'})",
                                        "section": ", ".join(c["section"] for c in changes),
                                        "reason": "The new export becomes the playbook in force.", "status": "pending"}]
        try:
            client = client or _client(log=False)
            ask = ("CHANGED PLAYBOOK SECTIONS (unified diff, old → new):\n\n"
                   + "\n\n".join(f"## {c['section']} ({c['change']})\n{c['diff']}" for c in changes)
                   + "\n\n" + _context(changes))
            resp = client.beta.messages.create(
                model=MODEL, max_tokens=6000, betas=["server-side-fallback-2026-07-01"], fallbacks="default",
                system=SYSTEM, output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
                messages=[{"role": "user", "content": ask}])
            cost = costs.log_tokens(conn, MODEL, "copy", resp.usage, "knowledge-update", update_id)
            raw = json.loads(next(b.text for b in resp.content if b.type == "text"))["proposals"]
        except Exception as exc:
            with conn:
                conn.execute("UPDATE knowledge_updates SET status='failed', error=?, proposals_json=? WHERE id=?",
                             (str(exc)[:300], json.dumps(items), update_id))
            return {"status": "failed", "error": str(exc)[:300]}
        for p in raw:
            item = {**p, "status": "pending"}
            if p["target"] == "knowledge":
                try:
                    item["before"] = _yaml(knowledge.get(p["key"]))
                    yaml.safe_load(p["after"])
                except Exception:                    # a key that doesn't exist or YAML that doesn't parse: drop it
                    continue
            elif p["target"] == "rubric":
                from creative_intelligence.adstudio import qa
                c = next((c for c in qa.load_rubric()["criteria"] if c["key"] == p["key"]), None)
                item["before"] = (c or {}).get("question", "(new criterion)")
            else:
                from creative_intelligence import prompts
                item["before"] = f"{p['key']} v{prompts.versions().get(p['key'], 0)}"
            items.append(item)
        with conn:
            conn.execute("UPDATE knowledge_updates SET status='ready', proposals_json=?, cost_usd=cost_usd+? WHERE id=?",
                         (json.dumps(items), cost, update_id))
        return {"status": "ready", "proposals": len(items)}
    finally:
        conn.close()


def _yaml(value: Any) -> str:
    import yaml
    return yaml.safe_dump(value, sort_keys=False, allow_unicode=True).removesuffix("...\n").strip()


def get(conn, update_id: int) -> dict[str, Any] | None:
    r = conn.execute("SELECT * FROM knowledge_updates WHERE id=?", (update_id,)).fetchone()
    if r is None:
        return None
    d = dict(r)
    d.pop("playbook_md")
    d["diff"], d["proposals"] = json.loads(d.pop("diff_json")), json.loads(d.pop("proposals_json"))
    d["affected"] = affected(d["diff"])
    return d


def listing(conn, limit: int = 10) -> list[dict[str, Any]]:
    ids = [r[0] for r in conn.execute("SELECT id FROM knowledge_updates ORDER BY id DESC LIMIT ?", (limit,))]
    return [get(conn, i) for i in ids]


def _apply(conn, u: Any, items: list[dict[str, Any]], indexes: list[int]) -> str | None:
    """Make the approved playbook/knowledge items live together; None, or why not (nothing changed then)."""
    import yaml
    before = knowledge.overrides()
    adopt = any(items[i]["target"] == "playbook" for i in indexes)
    new = list(before)
    for i in indexes:
        it = items[i]
        if it["target"] == "knowledge":
            new = [o for o in new if o["key"] != it["key"]]
            new.append({"file": it["key"].split(".")[0], "key": it["key"], "value": yaml.safe_load(it["after"]),
                        "update": u["id"], "proposal": i, "approved_at": datetime.utcnow().isoformat(timespec="seconds")})
    d = knowledge.overlay_dir()
    d.mkdir(parents=True, exist_ok=True)
    md = d / knowledge.PLAYBOOK
    old_md = md.read_text() if md.exists() else None
    try:
        knowledge.save_overrides(new)
        if adopt:
            md.write_text(u["playbook_md"])
        errs = knowledge.errors()
    except Exception as exc:
        errs = [f"{type(exc).__name__}: {exc}"]
    if errs:                                         # put everything back: a bad overlay would fail /healthz
        knowledge.save_overrides(before)
        if adopt:
            md.write_text(old_md) if old_md is not None else md.unlink()
        return "; ".join(errs[:3]) + (f" (+{len(errs) - 3} more)" if len(errs) > 3 else "")
    return None


def decide(conn, update_id: int, indexes: list[int], approve: bool) -> dict[str, Any]:
    """Approve or reject items. Approved playbook and knowledge items go live together after a check."""
    u = conn.execute("SELECT * FROM knowledge_updates WHERE id=?", (update_id,)).fetchone()
    if u is None:
        return {"error": "not found"}
    items = json.loads(u["proposals_json"])
    indexes = [i for i in indexes if 0 <= i < len(items) and items[i]["status"] == "pending"]
    if not indexes:
        return {"error": "Nothing left to decide there."}
    if approve:
        live = [i for i in indexes if items[i]["target"] in ("playbook", "knowledge")]
        if live and (why := _apply(conn, u, items, live)):
            return {"error": f"Not applied, nothing changed: {why}. Approve the related items together "
                             "(e.g. adopt the playbook with the values that cite a renamed section)."}
    now = datetime.utcnow().isoformat(timespec="seconds")
    for i in indexes:
        items[i]["status"] = ("live" if items[i]["target"] in ("playbook", "knowledge") else "approved") if approve \
            else "rejected"
        items[i]["decided_at"] = now
    with conn:
        conn.execute("UPDATE knowledge_updates SET proposals_json=? WHERE id=?", (json.dumps(items), update_id))
    return {"ok": True, "update": get(conn, update_id)}


def remove_override(key: str) -> bool:
    """Undo an approved value: the repo value applies again."""
    before = knowledge.overrides()
    after = [o for o in before if o["key"] != key]
    if len(after) == len(before):
        return False
    knowledge.save_overrides(after)
    return True


def export(conn, update_id: int) -> dict[str, Any] | None:
    """Everything approved for this update, for the PR that folds it into the repo."""
    u = conn.execute("SELECT * FROM knowledge_updates WHERE id=?", (update_id,)).fetchone()
    if u is None:
        return None
    items = json.loads(u["proposals_json"])
    return {"update": update_id, "revision": u["revision"], "exported": u["exported"],
            "playbook_md": u["playbook_md"] if any(i["target"] == "playbook" and i["status"] == "live" for i in items) else None,
            "live_overrides": [o for o in knowledge.overrides() if o.get("update") == update_id],
            "approved_for_pr": [i for i in items if i["status"] == "approved"],
            "rejected": [i for i in items if i["status"] == "rejected"]}


def overview() -> dict[str, Any]:
    """Admin → Knowledge: the playbook in force, every derived value with its source and what uses it."""
    info = knowledge.playbook_info()
    prompt_users = knowledge.used_by()
    files = []
    for name in knowledge.FILES:
        rows = []
        for f, where, src in knowledge.sources():
            if f != name:
                continue
            key = _key_for(where, f)
            uses = sorted({p for k, ps in prompt_users.items() for p in ps if k == key or k.startswith(key + ".")
                           or key.startswith(k + ".")})
            code = sorted({c for k, cs in knowledge.CODE_USES.items() for c in cs if key == k or key.startswith(k + ".")
                           or k.startswith(key + ".")})
            rows.append({"key": key, "source": src, "prompts": uses, "code": code})
        files.append({"file": f"{name}.yaml", "version": knowledge.load(name).get("version"), "values": rows})
    return {"playbook": {**info, "adopted": (knowledge.overlay_dir() / knowledge.PLAYBOOK).exists()},
            "version": knowledge.version(), "files": files, "overrides": knowledge.overrides(),
            "checklists": knowledge.load("rules")["checklists"], "errors": knowledge.errors()}
