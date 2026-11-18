"""
Video › Edit (feedback 2026-10-26: "we want to be able to edit prompts / make video more this or that, change things").

  editable()   the product's finished scripted videos (styled), newest first, with a poster
  load()       one video's script and settings, plus what can be changed: the shots it may use, voices, music
  version()    a new video from the edited script: unchanged voice lines and presenter clips are reused (pieces),
               shots not in the library are made first, a line marked for a new take gets a fresh shot of its own;
               it lands in Review next to the original, which stays
Rewriting around a request ("make it more playful") is styles.revise.
"""
from __future__ import annotations

import json
import time
from typing import Any

SETTINGS = ("voice", "presenter_id", "method", "variants", "music", "template_id", "estimate", "pieces",
            "waiting_shots")


def editable(conn, pid: int) -> list[dict[str, Any]]:
    from creative_intelligence.adstudio import styles
    out = []
    for r in conn.execute("SELECT id, style, copy_json, created_at FROM adstudio_videos WHERE product_id=? AND"
                          " source_kind='styled' AND status='done' ORDER BY id DESC LIMIT 40", (pid,)):
        st = json.loads(r["copy_json"] or "{}")
        if not st.get("lines"):
            continue
        rid = conn.execute("SELECT id FROM adstudio_video_renders WHERE video_id=? AND status='done' ORDER BY id"
                           " LIMIT 1", (r["id"],)).fetchone()
        out.append({"id": r["id"], "style": r["style"], "label": styles.label(r["style"], st.get("mode")),
                    "hook": (st.get("hooks") or [st["lines"][0]["text"]])[0], "created_at": r["created_at"],
                    "render_id": rid[0] if rid else None, "parent_id": st.get("parent_id"),
                    "change": st.get("change", "")})
    return out


def load(conn, vid: int) -> dict[str, Any] | None:
    from creative_intelligence import config
    from creative_intelligence.adstudio import story, styles
    r = conn.execute("SELECT * FROM adstudio_videos WHERE id=? AND source_kind='styled'", (vid,)).fetchone()
    if r is None:
        return None
    st = json.loads(r["copy_json"] or "{}")
    script = {k: v for k, v in st.items() if k not in SETTINGS}
    script["style"] = script.get("style") or r["style"]
    return {"id": vid, "product_id": r["product_id"], "label": styles.label(script["style"], script.get("mode")),
            "script": script, "settings": {k: st.get(k) for k in ("voice", "presenter_id", "method", "music")},
            "variants": st.get("variants") or [],
            "shots": [{"type": m["type"], "label": m["label"], "in_library": bool(m.get("in_library", True))}
                      for m in styles.menu_for(conn, r["product_id"], script["style"], plan=True)],
            "voices": [v["name"] for v in config.HOUSE_VOICES],
            "music": story.music_catalog(), "prompts": shot_prompts(conn, r["product_id"], script)}


def shot_prompts(conn, product_id: int, script: dict[str, Any]) -> dict[str, dict[str, str]]:
    """{shot_type: {still, motion}} for each shot in the video that can get a fresh take of its own (feedback
    2026-11-04: "on the edit part … we should be able to see the prompt and edit")."""
    from creative_intelligence.adstudio import shots
    out = {}
    for ln in script.get("lines") or []:
        t = ln.get("shot_type") or ""
        if t and t not in out and shots.own_ok(t):
            try:
                out[t] = shots.prompts_for(conn, product_id, t, script.get("style") or "")
            except ValueError:
                continue
    return out


def version(conn, vid: int, script: dict[str, Any], settings: dict[str, Any], vs: list[dict[str, Any]] | None = None,
            new_takes: list[str] | None = None, prompts: dict[str, dict[str, str]] | None = None) -> tuple[int | None, str]:
    """The edited script as a new video. new_takes: shot types to shoot afresh for this version; prompts: the picture
    and motion prompts the person wrote for some of them."""
    from creative_intelligence.adstudio import shots, styles
    base = load(conn, vid)
    if base is None:
        return None, "That video isn't editable."
    old = json.loads(conn.execute("SELECT copy_json FROM adstudio_videos WHERE id=?", (vid,)).fetchone()[0] or "{}")
    s = {**script, "style": base["script"]["style"], "parent_id": vid}
    for k in ("character", "comment", "mode"):              # what the video is about stays as it was
        if base["script"].get(k) is not None:
            s[k] = base["script"][k]
    tag = f"e{vid}.{int(time.time())}"
    for t in new_takes or []:
        if shots.own_ok(t):
            vid_t, why, _ = shots.create(conn, base["product_id"], t, look=tag, style=s["style"],
                                         prompt=(prompts or {}).get(t))
            if vid_t is None:
                return None, why
            s.setdefault("own_shots", {})[t] = tag
    st = {**base["settings"], **{k: v for k, v in (settings or {}).items() if k in base["settings"]}}
    new, why = styles.create(conn, base["product_id"], s, st.get("voice") or "", st.get("presenter_id"),
                             st.get("method") or styles.DEFAULT_TALKING, vs or base["variants"], st.get("music") or "",
                             old.get("template_id"), pieces=old.get("pieces"), make_shots=True)
    if new and s.get("own_shots"):
        with conn:                                          # the render waits for the new takes like any new shot
            row = json.loads(conn.execute("SELECT copy_json FROM adstudio_videos WHERE id=?", (new,)).fetchone()[0])
            row["waiting_shots"] = sorted(set(row.get("waiting_shots") or []) | set(s["own_shots"]))
            conn.execute("UPDATE adstudio_videos SET copy_json=? WHERE id=?", (json.dumps(row), new))
    return new, why
