"""
Free (feedback 2026-10-26: "a 'free' format where we just prompt what we would like to test and have it connected
to Seedance"): type what you want to see; it goes to the video model as written, Seedance by default (Kling or Veo
to compare). It can start from one of the product's approved images; without one, Seedance takes the product
photos as references. One take, QA-scored for information, shown in Review. The quickest way to try an idea before
it becomes a format.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from creative_intelligence import budget
from creative_intelligence.adstudio import animate, costs, videotools

MODELS = {"seedance": "Seedance 2.0", "kling": "Kling 3.0", "veo": "Veo 3.1 Fast"}
SECONDS = (5, 8, 10, 12, 15)         # Seedance and Kling make up to 15 s (2026-11-02: "the option to make it longer")
MAX_S = {"veo": 8}                  # Veo makes 4, 6 or 8 s
NEGATIVE = "text, captions, logos, watermarks, split screen, extra limbs"


CLAY = ("clay_hero", "clay_reveal", "clay_cut")


STYLIZED_WORDS = ("cartoon", "clay", "claymation", "animated", "animation", "character", "3d")


def stylized(prompt: str, cast: str | None) -> bool:
    """A free clip that is cartoon or clay on purpose: QA judges it as such (2026-11-02, "a cartoon mango wakes up…"
    failed for being a cartoon)."""
    import re
    return (cast or "").split(":")[0] in ("character", "clay") or bool(
        set(re.findall(r"[a-z0-9]+", (prompt or "").lower())) & set(STYLIZED_WORDS))


def cast_options(conn, product_id: int) -> list[dict[str, str]]:
    """Who can be in a free clip (2026-11-02: "use our cartoon characters, clay and presenters as well"): the approved
    presenters, the product's fruit character in use, and its clay fruit once a clay shot has passed."""
    from creative_intelligence.adstudio import characters, shots
    out = [{"key": f"presenter:{r['id']}", "label": f"{r['name']} (presenter)"} for r in conn.execute(
        "SELECT id, name FROM presenters WHERE status='approved' AND approved_look_id IS NOT NULL ORDER BY name")]
    if ch := characters.for_product(conn, product_id):
        out.append({"key": f"character:{ch['id']}", "label": f"{ch['name']} (cartoon character)"})
    if any(shots.passing(conn, product_id, t) for t in CLAY):
        out.append({"key": "clay:0", "label": "The clay fruit"})
    return out


def cast_picture(conn, product_id: int, key: str, out: Path) -> str:
    """The reference picture for a cast choice: the presenter's approved face, the character's look sheet, or a frame
    of the product's passing clay shot."""
    from creative_intelligence.adstudio import characters, shots, talking
    kind, _, ref = (key or "").partition(":")
    if kind == "presenter":
        return talking.portrait(conn, int(ref))[1]
    if kind == "character":
        ch = characters.approved(conn, int(ref))
        if ch is None:
            raise ValueError("That character is no longer approved.")
        return ch["sheet_path"]
    if kind == "clay":
        shot = next((s for t in CLAY if (s := shots.passing(conn, product_id, t))), None)
        if shot is None:
            raise ValueError("No clay shot has passed for this product yet.")
        return str(videotools.frame_at(shot["clip_path"], out, 0.5))
    raise ValueError("Pick who is in it.")


def put_in(conn, video_id: int, still: str, pic: str, key: str) -> str:
    """Both a start image and who is in it (2026-11-02): the cast is put into the image first (one image edit), so the
    clip starts from them in that scene."""
    from creative_intelligence.adstudio.store import to_data_uri
    from creative_intelligence.adstudio.shots import realism
    from creative_intelligence.providers import model, replicate
    from creative_intelligence.storage import storage
    who = {"presenter": "the person in the second picture, exactly as they look there",
           "character": "the cartoon character in the second picture, exactly as drawn there",
           "clay": "the clay fruit in the second picture, exactly as it looks there"}[key.partition(":")[0]]
    url = replicate.generate_image(f"Put {who} into the first picture, naturally part of the scene, at a size that fits "
                                   "it. Keep everything else in the first picture exactly as it is: the same framing, "
                                   "light, fruit, box and props. No text."
                                   + (" " + realism("person") if key.partition(":")[0] == "presenter" else ""),
                                   [to_data_uri(still), to_data_uri(pic)],
                                   "9:16", bucket="video")
    out = storage().put_url("videos", f"free_v{video_id}_cast_start.png", url)
    costs.log_image(conn, model("image"), "video", video_id, kind="video-still")
    return str(out)


def estimate(conn, route: str, seconds: int, image_id: int | None, sound: bool = True, cast: str = "") -> float:
    from creative_intelligence.providers import model, replicate
    m = replicate.route_model(route)
    secs = min((4, 6, 8), key=lambda d: abs(d - seconds)) if "veo" in m else seconds
    still = costs.price(conn, model("image")) * (1 + bool(cast)) if image_id else 0.0   # extended to 9:16 (+ the cast)
    return round(still + costs.price(conn, replicate.price_key(m, sound)) * secs + animate.QA_ESTIMATE_USD, 2)


def create(conn, product_id: int, prompt: str, image_id: int | None = None, route: str = "seedance",
           seconds: int = 5, sound: bool = True, cast: str = "") -> tuple[int | None, str]:
    """sound (2026-11-02: "we are missing the sound"): the model makes the clip's own sound, as it hears the prompt."""
    from creative_intelligence import jobs
    from creative_intelligence.providers import replicate
    prompt = (prompt or "").strip()[:1500]
    if not prompt:
        return None, "Write what you want to see."
    if route not in MODELS:
        return None, "Pick a model."
    if route == "kling" and not image_id:
        return None, "Kling needs a start image: pick one, or use Seedance."
    if cast and cast not in {c["key"] for c in cast_options(conn, product_id)}:
        return None, "Pick who is in it from the list."
    if cast and route != "seedance" and not image_id:
        return None, "Without a start image, a presenter, character or the clay fruit needs Seedance (it takes them as a reference)."
    seconds = min(seconds if seconds in SECONDS else 5, MAX_S.get(route, 99))
    if image_id and not conn.execute("SELECT 1 FROM adstudio_images WHERE id=? AND product_id=? AND review_status="
                                     "'approved'", (image_id, product_id)).fetchone():
        return None, "Pick one of this product's approved images."
    est = estimate(conn, route, seconds, image_id, sound, cast)
    try:
        if est > budget.caps(conn)["video_job"]:
            raise budget.BudgetExceeded(f"About ${est:.2f}: over the video job cap.")
        budget.check(conn, "video", est)
    except budget.BudgetExceeded as exc:
        return None, str(exc)
    with conn:
        vid = conn.execute(
            "INSERT INTO adstudio_videos (product_id, source_kind, source_id, style, motion_prompt, duration_s,"
            " still_strategy, model, prompt_version, route, has_audio, copy_json, created_at) VALUES (?,?,?, 'free', ?,?,?,?,"
            " 'free', ?, ?, ?, ?)",
            (product_id, "image" if image_id else "card", image_id or 0, prompt, seconds,
             animate.strategy_default() if image_id else "references", replicate.route_model(route), route, int(sound),
             json.dumps({"cast": cast} if cast else {}), datetime.utcnow().isoformat(timespec="seconds"))).lastrowid
    jobs.enqueue(conn, "free_video", {"video_id": vid}, dedupe_key=f"video:{vid}")
    return vid, ""


def process(video_id: int, db: Callable[[], Any]) -> dict[str, Any]:
    from creative_intelligence.adstudio import video_qa
    from creative_intelligence.providers import replicate
    from creative_intelligence.storage import storage
    from creative_intelligence.webapp.common import _primary_photo_uris, _product_dict
    conn = db()
    try:
        v = dict(conn.execute("SELECT * FROM adstudio_videos WHERE id=?", (video_id,)).fetchone())
        if v["status"] != "queued":
            return {"skipped": v["status"]}
        animate._set(conn, video_id, status="running", step="Making the clip")
        product = _product_dict(conn, v["product_id"]) or {}
        refs = _primary_photo_uris(conn, v["product_id"], limit=3)
        pic = None
        if cast := json.loads(v.get("copy_json") or "{}").get("cast"):
            from creative_intelligence.adstudio.store import to_data_uri
            from creative_intelligence.storage import storage as _st
            pic = cast_picture(conn, v["product_id"], cast, _st().area_dir("videos") / f"free_v{video_id}_cast.png")
            if v["source_kind"] != "image":              # who is in it leads, the fruit photos follow
                refs = [to_data_uri(pic)] + refs[:2]
        if v["source_kind"] == "image":
            still = animate.make_still(conn, v, animate.source_row(conn, "image", v["source_id"]), refs)
            if pic:
                animate._set(conn, video_id, step="Putting them in the picture")
                still = put_in(conn, video_id, still, pic, cast)
            clip = animate.make_clip(conn, v, still, 1, route=v["route"], negative=NEGATIVE, audio=bool(v["has_audio"]))
        else:                                    # Seedance or Veo from the prompt, the product photos as references
            budget.check_video_job(conn, video_id, estimate(conn, v["route"], v["duration_s"], None, bool(v["has_audio"])))
            url, used, secs, sound = replicate.generate_clip(v["motion_prompt"], None, duration=v["duration_s"],
                                                             aspect_ratio="9:16", route=v["route"], refs=refs,
                                                             negative=NEGATIVE, audio=bool(v["has_audio"]))
            clip = storage().put_url("videos", f"free_v{video_id}.mp4", url, min_bytes=10_000)
            costs.log_video(conn, used, secs, "video", video_id, audio=sound)
            animate._set(conn, video_id, model=used)
            if abs((i := videotools.probe(clip))["width"] / i["height"] - 9 / 16) > 0.02:
                clip = str(videotools.fit(clip, Path(clip).with_name(Path(clip).stem + "_916.mp4"), "9:16"))
        animate._set(conn, video_id, clip_path=clip, step="Checking it")
        q = video_qa.score(conn, video_id, clip, refs, product, count_check=False,   # a cartoon or clay cast is intended
                           note=video_qa.STYLIZED_NOTE if stylized(v["motion_prompt"], cast) else "")
        poster = videotools.frame_at(clip, Path(clip).with_suffix(".jpg"), 1.0, 540)
        info = videotools.probe(clip)
        model = conn.execute("SELECT model FROM adstudio_videos WHERE id=?", (video_id,)).fetchone()[0]
        with conn:
            conn.execute("INSERT INTO adstudio_video_renders (video_id, product_id, aspect, template, headline, output_path,"
                         " poster_path, status, note, duration_s, created_at) VALUES (?,?, '9:16', 'free', ?,?,?, 'done',"
                         " ?,?,?)", (video_id, v["product_id"], v["motion_prompt"][:80], str(clip), str(poster),
                                     f"Free · {model} · {info['duration']:.0f} s", round(info["duration"], 1),
                                     datetime.utcnow().isoformat(timespec="seconds")))
        spent = budget.spent_on_video(conn, video_id)
        animate._set(conn, video_id, status="done", step="", finished_at=datetime.utcnow().isoformat(timespec="seconds"),
                     message=f"{'passed QA' if q.get('passed') else 'did not pass QA' if q.get('total') is not None else 'not scored'}; ${spent:.2f}")
        return {"video_id": video_id, "spent": spent}
    except Exception as exc:
        animate._set(conn, video_id, status="failed", step="", message=f"{type(exc).__name__}: {exc}"[:400])
        raise
    finally:
        conn.close()
