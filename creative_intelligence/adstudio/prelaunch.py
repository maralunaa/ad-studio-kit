"""
Pre-launch checklists (Meta Creative Playbook › Pre-launch checklists, knowledge/rules.yaml › checklists).

Every ad passes its checklist before export. Items marked auto are checked here, each by the rubric criterion
or pre-export check it names (`check`); the rest are for a person and show as "person". Nothing here calls a
paid API: it reads what QA, the claims check, the renderers and the export already recorded.

  status  pass | warn | fail | na (nothing to check, e.g. no captions) | person
"""
from __future__ import annotations

import json
from typing import Any

from creative_intelligence import knowledge

KINDS = {"compose": ("static", "composite"), "ai": ("static", "textad"), "video": ("video", "video_render")}
SUBJECT_MAX = 0.35            # share of a text box that may look like the product before it counts as covering it


def _criterion(key: str):
    """A rubric criterion as a check: its score against its minimum."""
    def check(item: dict[str, Any], ctx: dict[str, Any]) -> tuple[str, str]:
        from creative_intelligence.adstudio import qa
        crit = next((c for c in qa.load_rubric()["criteria"] if c["key"] == key), None)
        score = ((item.get("qa") or {}).get("scores") or {}).get(key)
        if crit is None or score is None:
            return "warn", "not scored yet"
        floor = crit.get("min") or 0
        return ("pass" if score >= floor else "fail"), f"{crit['name']} {score:g} (minimum {floor:g})"
    return check


def _text_clear_of_fruit(item: dict[str, Any], ctx: dict[str, Any]) -> tuple[str, str]:
    share = (ctx.get("measured") or {}).get("over_subject")
    if share is None:
        return "na", "no copy drawn"
    return ("pass" if share <= SUBJECT_MAX else "warn"), f"{round(share * 100)}% of the copy area looks like the product"


def _claims(item: dict[str, Any], ctx: dict[str, Any]) -> tuple[str, str]:
    cc = item.get("claims_check") or ctx.get("claims")
    if not cc:
        return "warn", "copy not checked against the facts library"
    status = cc.get("status")
    if status == "ok":
        return "pass", "every claim is an approved fact"
    if status == "flagged":
        n = len(cc.get("unlisted") or []) + len(cc.get("banned") or [])
        return "fail", f"{n} claim{'s' * (n != 1)} or term{'s' * (n != 1)} to fix"
    return "warn", "the claims check didn't finish"


def _tags(item: dict[str, Any], ctx: dict[str, Any]) -> tuple[str, str]:
    a = item.get("asset") or {}
    if not a.get("asset_id"):
        return "fail", "no Asset ID (the Meta ad name) yet"
    missing = [k for k in ("hook_type", "format") if not a.get(k)]
    return ("warn", f"not tagged: {', '.join(missing)}") if missing else ("pass", a["asset_id"])


def _ai_flag(item: dict[str, Any], ctx: dict[str, Any]) -> tuple[str, str]:
    a = item.get("asset") or {}
    if not a.get("ai_generated"):
        return "fail", "AI-generated flag not set"
    return "pass", "AI-generated" + (" · AI person" if item.get("ai_person") else "")


def _caption_size(item: dict[str, Any], ctx: dict[str, Any]) -> tuple[str, str]:
    from creative_intelligence.adstudio import qa
    m = ctx.get("measured") or {}
    if not m:
        return "warn", "made before caption checks existed"
    out = qa.outside_safe_zone(m)
    if out:
        return "fail", f"outside the safe zone: {', '.join(sorted(set(out)))}"
    px = m.get("caption_px_at_phone")
    if px is None:
        return "na", "no captions; text inside the safe zone"
    need = knowledge.load("rules")["captions"]["min_px_at_phone"]
    return ("pass" if px >= need else "fail"), f"captions {px:g} px at phone width (minimum {need})"


def _export_spec(item: dict[str, Any], ctx: dict[str, Any]) -> tuple[str, str]:
    e = ctx.get("export") or {}
    if not e:
        return "warn", "made before export checks existed"
    note = f"{e.get('mbps', 0):g} Mbps (minimum {e.get('min_mbps', 0):g})"
    if e.get("ok"):
        return "pass", note + (" · re-encoded once" if e.get("reencoded") else "")
    bad = [k.replace("_", " ") for k in ("container", "video_codec", "audio_codec", "fps", "bitrate") if not e.get(k)]
    return "fail", f"{', '.join(bad)} · {note}"


CHECKS = {"text_clear_of_fruit": _text_clear_of_fruit, "claims": _claims, "tags": _tags, "ai_flag": _ai_flag,
          "caption_size": _caption_size, "export_spec": _export_spec}


def check_for(key: str):
    """The function behind a checklist item's `check`: a pre-export check here, else a rubric criterion."""
    return CHECKS.get(key) or _criterion(key)


def checklist(item: dict[str, Any], ctx: dict[str, Any] | None = None) -> dict[str, Any]:
    """The playbook checklist for one gallery item (compose, ai or video), each line with its status."""
    which = KINDS[item["kind"]][0]
    ctx = ctx or {}
    out = []
    for line in knowledge.load("rules")["checklists"][which]:
        if not line.get("auto"):
            out.append({"text": line["text"], "auto": False, "status": "person", "note": ""})
            continue
        status, note = check_for(line["check"])(item, ctx)
        out.append({"text": line["text"], "auto": True, "check": line["check"], "status": status, "note": note})
    return {"kind": which, "items": out, "fail": sum(i["status"] == "fail" for i in out),
            "warn": sum(i["status"] == "warn" for i in out)}


def contexts(conn, items: list[dict[str, Any]]) -> dict[tuple[str, int], dict[str, Any]]:
    """What the checks read beyond the gallery item: measured copy (qa_scores) and, for videos, the export and
    caption measurements (adstudio_video_renders.checks_json) and the script's claims check."""
    out: dict[tuple[str, int], dict[str, Any]] = {}
    for kind in ("compose", "ai"):
        ids = [i["id"] for i in items if i["kind"] == kind and i["id"] > 0]
        for k in range(0, len(ids), 500):
            chunk = ids[k:k + 500]
            for r in conn.execute(f"SELECT source_id, measured_json FROM qa_scores WHERE source=? AND source_id IN"
                                  f" ({','.join('?' * len(chunk))})", [KINDS[kind][1], *chunk]):
                out[(kind, r["source_id"])] = {"measured": json.loads(r["measured_json"] or "{}")}
    ids = [i["id"] for i in items if i["kind"] == "video" and i["id"] > 0]
    for k in range(0, len(ids), 500):
        chunk = ids[k:k + 500]
        for r in conn.execute("SELECT r.id, r.checks_json, v.copy_json FROM adstudio_video_renders r JOIN"
                              f" adstudio_videos v ON v.id=r.video_id WHERE r.id IN ({','.join('?' * len(chunk))})", chunk):
            checks = json.loads(r["checks_json"] or "{}")
            settings = json.loads(r["copy_json"] or "{}")
            out[("video", r["id"])] = {**checks, "claims": settings.get("claims")}
    return out


def attach(conn, items: list[dict[str, Any]]) -> None:
    """Add each finished ad's checklist (`prelaunch`) to gallery items."""
    ctx = contexts(conn, items)
    for i in items:
        if i["kind"] in KINDS and i["id"] > 0:
            i["prelaunch"] = checklist(i, ctx.get((i["kind"], i["id"]), {}))


def record_video(conn, render_id: int, measured: dict[str, Any] | None, export: dict[str, Any] | None) -> None:
    """Keep a video version's caption/text measurements and export spec for its checklist."""
    with conn:
        conn.execute("UPDATE adstudio_video_renders SET checks_json=? WHERE id=?",
                     (json.dumps({"measured": measured or {}, "export": export or {}}), render_id))
