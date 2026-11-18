"""
Video winner decoder (Video Studio V4): our own best video ads, turned into structure templates.

  candidates()  the top video ads by 90-day spend (at least MIN_SPEND), ranked by cost per purchase,
                with 7-day hook and hold rates when the daily insights have them
  fetch()       Meta, read-only (GET): the ad's creative → its video → the mp4. Raw responses go to the
                raw data folder (timestamped, never overwritten), every call to outputs/api.log. If the
                token can't read the video (Page-owned videos need Page access), the thumbnail and the
                ad copy are used instead and the template is marked partial.
  decode()      scenes (ffmpeg scene detection) → frames → speech with word timings (WhisperX) →
                Claude writes the template: hook type, each shot's type and timing, presenter time,
                when the product, price and CTA appear, the on-screen text, why it works, how to rebuild
                it with our shot library.

Templates are presets to build from, not the only way: new, untested AI styles stay in the mix.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from creative_intelligence import config, prompts
from creative_intelligence.adstudio import videotools, voices

MIN_SPEND = 300.0
TOP_N = 5
MAX_FRAMES = 16
MODEL = config.CI_CLAUDE_CHECK_MODEL
SHOT_TYPES = ["presenter", "ugc_talking", "hands_product", "unboxing", "whole_fruit", "cut_reveal", "macro_texture",
              "eating", "orchard_tree", "packing", "text_card", "price_offer", "end_card", "other"]


# ── which ads ───────────────────────────────────────────────────────────────
def candidates(conn, min_spend: float = MIN_SPEND, n: int = TOP_N) -> list[dict[str, Any]]:
    """Top video ads by 90-day spend (latest snapshot), at least `min_spend`, then ranked by CPA."""
    rows = [dict(r) for r in conn.execute(
        "SELECT p.ad_id, c.ad_name, c.primary_text, c.headline, c.cta, p.spend, p.purchases, p.revenue, p.roas, p.cpa,"
        " p.snapshot_date FROM creative_performance p JOIN creatives c ON c.id=p.creative_id"
        " WHERE c.format='video' AND p.date_range='90d' AND p.snapshot_date=(SELECT MAX(snapshot_date)"
        " FROM creative_performance q WHERE q.ad_id=p.ad_id AND q.date_range='90d')")]
    seen, top = set(), []
    for r in sorted(rows, key=lambda r: -(r["spend"] or 0)):
        key = (r["ad_name"] or "").split(" - ")[0].strip().lower() or r["ad_id"]
        if (r["spend"] or 0) < min_spend or r["ad_id"] in seen or key in seen:
            continue
        seen |= {r["ad_id"], key}
        r["cpa"] = round(r["spend"] / r["purchases"], 2) if r["purchases"] else None
        top.append(r)
        if len(top) == n:
            break
    for r in top:
        d = conn.execute("SELECT SUM(impressions), SUM(video_3s_views), SUM(thruplays) FROM ad_daily_insights"
                         " WHERE ad_id=?", (r["ad_id"],)).fetchone()
        imp, v3, tp = (d[0] or 0), (d[1] or 0), (d[2] or 0)
        r["hook_rate"] = round(v3 / imp, 3) if imp else None
        r["hold_rate"] = round(tp / v3, 3) if v3 else None
    return sorted(top, key=lambda r: (r["cpa"] is None, r["cpa"] or 0))


# ── Meta, read-only ─────────────────────────────────────────────────────────
def _raw_dir() -> Path:
    return Path(os.getenv("CI_RAW_DATA_DIR", "").strip() or config.RAW_DATA_DIR)


def _log(url: str) -> None:
    log = Path("outputs/api.log")
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as f:
        f.write(f"{datetime.now().isoformat()} GET {url.split('?', 1)[0]}\n")


def meta_get(path: str, fields: str, http: Any = None) -> dict[str, Any]:
    """One read-only Graph API GET. Returns the JSON (an {"error": ...} dict on an API error)."""
    import requests
    http = http or requests
    url = f"https://graph.facebook.com/{os.getenv('META_API_VERSION', 'v26.0')}/{path}"
    _log(url)
    r = http.get(url, params={"fields": fields, "access_token": os.getenv("META_ACCESS_TOKEN", "")}, timeout=60)
    return r.json()


def save_raw(name: str, data: Any) -> Path:
    d = _raw_dir()
    d.mkdir(parents=True, exist_ok=True)
    out = d / f"{name}_{datetime.utcnow().strftime('%Y%m%dT%H%M%S%f')}.json"     # never overwrites
    out.write_text(json.dumps(data, indent=2))
    return out


def video_id_of(creative: dict[str, Any]) -> str | None:
    if creative.get("video_id"):
        return str(creative["video_id"])
    vd = (creative.get("object_story_spec") or {}).get("video_data") or {}
    if vd.get("video_id"):
        return str(vd["video_id"])
    vids = (creative.get("asset_feed_spec") or {}).get("videos") or []
    return str(vids[0]["video_id"]) if vids and vids[0].get("video_id") else None


def fetch(ad_id: str, out_dir: Path, http: Any = None, download: Callable[[str, Path], Path] | None = None) -> dict[str, Any]:
    """{source: video|thumbnail, video_path?, thumb_path?, copy, video_id, error?}. Never writes to Meta."""
    ad = meta_get(ad_id, "name,creative{id,video_id,thumbnail_url,body,title,call_to_action_type,object_story_spec,"
                         "asset_feed_spec}", http)
    save_raw(f"winner_ad_{ad_id}", ad)
    if "error" in ad:
        raise RuntimeError(f"Meta: {ad['error'].get('message', 'error')[:160]}")
    cr = ad.get("creative") or {}
    vd = (cr.get("object_story_spec") or {}).get("video_data") or {}
    copy = {"body": cr.get("body") or vd.get("message") or "", "title": cr.get("title") or vd.get("title") or "",
            "cta": cr.get("call_to_action_type") or ((vd.get("call_to_action") or {}).get("type") or "")}
    vid = video_id_of(cr)
    out = {"video_id": vid, "copy": copy, "source": "thumbnail", "thumb_url": cr.get("thumbnail_url") or vd.get("image_url")}
    download = download or _download
    if vid:
        v = meta_get(vid, "source,length,picture,permalink_url", http)
        save_raw(f"winner_video_{vid}", {k: x for k, x in v.items() if k != "source"} | {"has_source": "source" in v})
        if v.get("source"):
            out.update(source="video", video_path=str(download(v["source"], out_dir / f"winner_{ad_id}.mp4")),
                       length=v.get("length"))
            return out
        out["error"] = (v.get("error") or {}).get("message", "no video source")[:200]
        out["thumb_url"] = out["thumb_url"] or v.get("picture")
    if out.get("thumb_url"):
        out["thumb_path"] = str(download(out["thumb_url"], out_dir / f"winner_{ad_id}.jpg"))
    return out


def _download(url: str, out: Path) -> Path:
    import urllib.request
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(urllib.request.urlopen(urllib.request.Request(url, headers=voices.UA), timeout=180).read())
    return out


# ── decoding ────────────────────────────────────────────────────────────────
def scenes(path: str | Path, threshold: float = 0.3) -> list[float]:
    """Cut times (s) by ffmpeg scene detection, always starting at 0."""
    p = subprocess.run([videotools.ffmpeg_exe(), "-hide_banner", "-i", str(path), "-vf",
                        f"select='gt(scene,{threshold})',showinfo", "-an", "-f", "null", "-"],
                       capture_output=True, text=True, timeout=600)
    cuts = sorted({round(float(t), 2) for t in re.findall(r"pts_time:([\d.]+)", p.stderr)})
    return [0.0] + [c for c in cuts if c > 0.3]


def frames_for(path: str | Path, cuts: list[float], duration: float, out_dir: Path) -> list[tuple[float, Path]]:
    """One frame just after each cut (max MAX_FRAMES, spread evenly if there are more), plus the last second."""
    picks = cuts if len(cuts) <= MAX_FRAMES - 1 else [cuts[round(i * (len(cuts) - 1) / (MAX_FRAMES - 2))]
                                                       for i in range(MAX_FRAMES - 1)]
    times = sorted({min(duration - 0.1, t + 0.4) for t in picks} | {max(0.0, duration - 1.0)})
    return [(t, videotools.frame_at(path, out_dir / f"w{i:02d}.jpg", t, 480)) for i, t in enumerate(times)]


SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["summary", "hook", "shots", "presenter_share", "product_first_s", "price_first_s", "cta",
                 "on_screen_text", "pacing", "why_it_works", "how_to_rebuild"],
    "properties": {
        "summary": {"type": "string", "description": "2 sentences: what the ad is and how it is built"},
        "hook": {"type": "object", "additionalProperties": False, "required": ["type", "what_happens", "spoken", "text"],
                 "properties": {"type": {"type": "string", "description": "e.g. negative hook, question, POV, listicle, "
                                                                          "unboxing, before/after, bold claim"},
                                "what_happens": {"type": "string"}, "spoken": {"type": "string"}, "text": {"type": "string"}}},
        "shots": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["start", "end", "type", "what", "presenter", "text"],
            "properties": {"start": {"type": "number"}, "end": {"type": "number"},
                           "type": {"type": "string", "enum": SHOT_TYPES}, "what": {"type": "string"},
                           "presenter": {"type": "boolean"}, "text": {"type": "string", "description": "on-screen text, or ''"}}}},
        "presenter_share": {"type": "number", "description": "0-1: share of the runtime with a person talking on screen"},
        "product_first_s": {"type": ["number", "null"]}, "price_first_s": {"type": ["number", "null"]},
        "cta": {"type": "object", "additionalProperties": False, "required": ["text", "at_s"],
                "properties": {"text": {"type": "string"}, "at_s": {"type": ["number", "null"]}}},
        "on_screen_text": {"type": "array", "items": {"type": "string"}},
        "pacing": {"type": "string", "description": "e.g. 'a cut every 1.8 s, faster in the hook'"},
        "why_it_works": {"type": "array", "items": {"type": "string"}},
        "how_to_rebuild": {"type": "array", "items": {"type": "string"},
                           "description": "steps to rebuild this structure with product shots, a narrator voice and code-drawn text"},
    }}


COMPETITOR_RULE = ("THIS IS ANOTHER BRAND'S AD (competitor-derived): decode its structure only (shot order, pacing, "
                   "hook mechanics, where the offer and call to action land). Describe each shot in general terms. Never "
                   "carry over its brand, product names, claims, footage or exact words.")


def ask_claude(frames: list[tuple[float, Path]], words: list[dict[str, Any]], ad: dict[str, Any], partial: bool,
               client: Any = None) -> tuple[dict[str, Any], Any]:
    from creative_intelligence.adstudio.chat import _client, _image_blocks
    from creative_intelligence.adstudio.store import to_data_uri
    content: list[dict[str, Any]] = []
    for t, f in frames:
        content += [{"type": "text", "text": f"Frame at {t:.1f} s:"}, *_image_blocks([to_data_uri(str(f), max_dim=640)])]
    said = " ".join(f"[{w['start']:.1f}] {w['word']}" for w in words) if words else "(no speech found)"
    metrics = {k: ad.get(k) for k in ("spend", "purchases", "cpa", "roas", "hook_rate", "hold_rate")}
    content.append({"type": "text", "text": (
        f"AD: {ad.get('ad_name')}\nRESULTS (90 days): {json.dumps(metrics)}\nAD COPY: {json.dumps(ad.get('copy') or {})}\n"
        f"SPEECH WITH TIMES (s): {said}\n\n"
        + ("ONLY THE THUMBNAIL IS AVAILABLE: infer the structure from it and the copy, and say what is uncertain.\n"
           if partial else "")
        + (COMPETITOR_RULE + "\n" if ad.get("competitor") else "")
        + "Decode this ad into its structure template.")})
    client = client or _client(log=False)
    resp = client.beta.messages.create(
        model=MODEL, max_tokens=5000, betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        system=prompts.text("winner-decode-system"),
        output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": content}])
    if resp.stop_reason == "refusal":
        raise RuntimeError("Claude declined to decode this ad")
    return json.loads(next(b.text for b in resp.content if b.type == "text")), resp.usage


def estimate(conn) -> float:
    from creative_intelligence.adstudio.costs import price
    pin, pout = price(conn, MODEL), price(conn, MODEL + ":output")
    return round(price(conn, config.WORDS_MODEL.split(":")[0]) + (30_000 * pin + 3_000 * pout) / 1e6, 2)


def queue(conn, ads: list[dict[str, Any]]) -> list[int]:
    """One template row and one decode job per ad (an ad already decoded or decoding is skipped)."""
    from creative_intelligence import budget, jobs
    budget.check(conn, "text", estimate(conn) * len(ads))         # mostly Claude; the transcript is a cent
    ids = []
    for a in ads:
        if conn.execute("SELECT 1 FROM video_templates WHERE ad_id=? AND status IN ('queued','running','done')",
                        (a["ad_id"],)).fetchone():
            continue
        with conn:
            tid = conn.execute("INSERT INTO video_templates (ad_id, ad_name, metrics_json, created_at) VALUES (?,?,?,?)",
                               (a["ad_id"], a.get("ad_name") or "", json.dumps(a, default=str),
                                datetime.utcnow().isoformat(timespec="seconds"))).lastrowid
        jobs.enqueue(conn, "decode", {"template_id": tid}, dedupe_key=f"decode:{tid}")
        ids.append(tid)
    return ids


def _set(conn, tid: int, **f: Any) -> None:
    with conn:
        conn.execute(f"UPDATE video_templates SET {', '.join(k + '=?' for k in f)} WHERE id=?", (*f.values(), tid))


def decode(db: Callable[[], Any], tid: int, client: Any = None, words_client: Any = None, http: Any = None,
           download: Callable[[str, Path], Path] | None = None) -> dict[str, Any]:
    """The decode job (in the worker)."""
    from creative_intelligence.adstudio import costs
    from creative_intelligence.storage import storage
    conn = db()
    try:
        row = dict(conn.execute("SELECT * FROM video_templates WHERE id=?", (tid,)).fetchone())
        ad = json.loads(row["metrics_json"] or "{}")
        if row.get("upload_path"):                                   # the video was uploaded by hand
            got = {"source": "video", "video_path": row["upload_path"], "video_id": row["video_id"] or "",
                   "copy": ad.get("copy") or {}}
            _set(conn, tid, status="running", step="Reading the uploaded video")
        else:
            _set(conn, tid, status="running", step="Fetching from Meta")
            got = fetch(row["ad_id"], storage().area_dir("winners"), http, download)
        ad["copy"] = got["copy"]
        words: list[dict[str, Any]] = []
        cuts: list[float] = []
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            if got["source"] == "video":
                path = got["video_path"]
                dur = videotools.probe(path)["duration"]
                _set(conn, tid, step="Finding the cuts")
                cuts = scenes(path)
                frames = frames_for(path, cuts, dur, tmp)
                _set(conn, tid, step="Transcribing")
                wav = tmp / "speech.wav"
                try:
                    videotools.run(["-i", path, "-vn", "-ac", "1", "-ar", "16000", str(wav)])
                    words = voices.word_timings(conn, wav, "winner", tid, client=words_client)
                except Exception:
                    words = []                              # music-only or silent ads are fine
            elif got.get("thumb_path"):
                frames, dur = [(0.0, Path(got["thumb_path"]))], None
            else:
                raise RuntimeError(got.get("error") or "Meta returned neither the video nor a thumbnail")
            _set(conn, tid, step="Decoding with Claude")
            tpl, usage = ask_claude(frames, words, ad, got["source"] != "video", client)
        cost = costs.log_tokens(conn, MODEL, "copy", usage, "winner", tid)
        _set(conn, tid, status="done", step="", source="upload" if row.get("upload_path") else got["source"], video_id=got.get("video_id") or "",
             video_path=got.get("video_path") or got.get("thumb_path") or "", duration_s=dur,
             scenes_json=json.dumps(cuts), transcript_json=json.dumps(words), template_json=json.dumps(tpl),
             error=got.get("error") or "", cost_usd=round(cost, 4), finished_at=datetime.utcnow().isoformat(timespec="seconds"))
        return {"template_id": tid, "source": got["source"], "shots": len(tpl.get("shots") or [])}
    except Exception as exc:
        _set(conn, tid, status="failed", step="", error=str(exc)[:300])
        raise
    finally:
        conn.close()


def attach_upload(conn, tid: int, raw: bytes, filename: str) -> str:
    """Keep an uploaded copy of the ad's video and decode it again from the full video. Returns '' or why not."""
    from creative_intelligence import jobs
    from creative_intelligence.storage import storage
    row = conn.execute("SELECT * FROM video_templates WHERE id=?", (tid,)).fetchone()
    if row is None:
        return "template not found"
    if row["status"] in ("queued", "running"):
        return "This ad is being decoded already."
    ext = (Path(filename).suffix or ".mp4").lower()
    if ext not in (".mp4", ".mov", ".m4v", ".webm"):
        return "Upload an mp4, mov or webm video."
    path = storage().put_bytes("winners", f"upload_{row['ad_id']}_{int(datetime.utcnow().timestamp())}{ext}", raw)
    try:
        videotools.probe(path)
    except videotools.VideoToolError:
        return "That file isn't a video ffmpeg can read."
    with conn:
        conn.execute("UPDATE video_templates SET upload_path=?, status='queued', step='', error='' WHERE id=?", (path, tid))
    jobs.enqueue(conn, "decode", {"template_id": tid}, dedupe_key=f"decode:{tid}")
    return ""


def redecode_partial(conn) -> list[int]:
    """Queue every thumbnail-only template again (e.g. after a token with Page access is added)."""
    from creative_intelligence import jobs
    ids = [r[0] for r in conn.execute("SELECT id FROM video_templates WHERE source='thumbnail' AND status='done'")]
    for tid in ids:
        _set(conn, tid, status="queued", step="", error="")
        jobs.enqueue(conn, "decode", {"template_id": tid}, dedupe_key=f"decode:{tid}")
    return ids


def listing(conn) -> list[dict[str, Any]]:
    out = []
    for r in conn.execute("SELECT * FROM video_templates ORDER BY id DESC"):
        d = dict(r)
        for k in ("metrics_json", "template_json", "scenes_json"):
            d[k[:-5]] = json.loads(d.pop(k) or ("[]" if k == "scenes_json" else "{}"))
        d.pop("transcript_json", None)
        if d.get("origin") == "competitor":           # travels into the script prompt with the template
            d["template"]["origin"] = "competitor-derived: copy the structure only, never its words, brand or claims"
        out.append(d)
    return out
