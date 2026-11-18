"""
Talking-head clips (Video Studio V5): an approved presenter says one line.

Two ways, picked per line by length and quality (the comparison sample decides the defaults):
  native   a video model with its own speech (Veo 3.1 Fast with sound): short lines, up to 8 s. The voice
           is the model's, not the house voice.
  avatar   the house voice reads the line (voices.py), then an image+audio model makes the presenter say it
           (OmniHuman 1.5, Kling Avatar, Wan 2.2 S2V): any length, the presenter's own voice.

Lines must come from approved facts (scripts are claims-checked before they get here). Every clip is an
adstudio_videos row of style 'talking' with ai_person = 1, QA-scored against the approved portrait.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from creative_intelligence.adstudio import animate, videotools, voices

METHODS = {
    "veo": {"label": "Veo 3.1 Fast, its own voice", "kind": "native", "model": "google/veo-3.1-fast", "max_s": 8},
    "omnihuman": {"label": "OmniHuman 1.5 + house voice", "kind": "avatar", "model": "bytedance/omni-human-1.5"},
    "kling_avatar": {"label": "Kling Avatar + house voice", "kind": "avatar", "model": "kwaivgi/kling-avatar-v2"},
    "wan_s2v": {"label": "Wan 2.2 S2V + house voice", "kind": "avatar", "model": "wan-video/wan-2.2-s2v"},
    # Feedback 2026-11-04: "seedance 2.5 … the absolute best results (especially for talking people)". The house voice
    # goes in as a black-screen video carrying the audio, with the presenter's picture as a reference image (not a
    # first frame): the method the user found gives the most natural speech.
    "seedance": {"label": "Seedance 2.5 + house voice", "kind": "avatar", "model": "bytedance/seedance-2.5"},
    # 2026-11-04: Seedance refuses any realistic face as a reference (ByteDance's deepfake guard, by design). Here it
    # draws the presenter itself from their written look, same seed every time, lip-synced to the black-screen voice.
    "seedance_text": {"label": "Seedance 2.5, presenter drawn from the description + house voice", "kind": "avatar",
                      "model": "bytedance/seedance-2.5"},
}
COMPARE = ("veo", "omnihuman", "kling_avatar")
DEFAULT = "veo"          # the user's pick from the comparison sample (2026-10-21)
FALLBACK = "kling_avatar"   # for lines longer than Veo's 8 s; $0.09/s against OmniHuman's $0.14 (2026-11-02)
LIPSYNC = "omnihuman"       # a fruit character: OmniHuman animates any picture, not only a human face
WORDS_PER_S = 2.6


def seconds_for(text: str) -> float:
    return round(len(text.split()) / WORDS_PER_S + 0.6, 1)


def veo_seconds(text: str) -> int:
    """Veo makes 4, 6 or 8 s clips: the shortest that fits the line."""
    return next((d for d in (4, 6, 8) if d >= seconds_for(text)), 8)


def portrait(conn, presenter_id: int) -> tuple[dict[str, Any], str]:
    p = conn.execute("SELECT p.*, l.path AS face FROM presenters p LEFT JOIN presenter_looks l ON l.id=p.approved_look_id"
                     " WHERE p.id=?", (presenter_id,)).fetchone()
    if p is None:
        raise ValueError("presenter not found")
    if p["status"] != "approved" or not p["face"]:
        raise ValueError(f"Approve a look for {p['name']} first.")
    return dict(p), p["face"]


def fit(method: str, text: str) -> str:
    """The method that can say this line: Veo makes at most 8 s, so a longer line goes to the house-voice avatar."""
    m = METHODS[method]
    return FALLBACK if m["kind"] == "native" and seconds_for(text) > m["max_s"] else method


def estimate(conn, method: str, text: str) -> float:
    from creative_intelligence.adstudio.costs import price
    method = fit(method, text)
    m = METHODS[method]
    secs = seconds_for(text)
    if m["kind"] == "native":
        return round(price(conn, m["model"] + ":audio") * veo_seconds(text), 2)
    from creative_intelligence.providers import kling
    model = kling.AVATAR if m["model"] == "kwaivgi/kling-avatar-v2" and kling.enabled() else m["model"]
    return round(price(conn, model) * secs, 2)


def create(conn, presenter_id: int, product_id: int, text: str, method: str, batch: str = "") -> tuple[int | None, str]:
    from creative_intelligence import budget, jobs
    if method not in METHODS:
        return None, "unknown method"
    try:
        p, _ = portrait(conn, presenter_id)
    except ValueError as exc:
        return None, str(exc)
    method = fit(method, text)
    est = estimate(conn, method, text)
    try:
        budget.check(conn, "video", est)
    except budget.BudgetExceeded as exc:
        return None, str(exc)
    copy = {"line": text, "presenter_id": presenter_id, "presenter": p["name"], "method": method, "batch": batch}
    with conn:
        vid = conn.execute(
            "INSERT INTO adstudio_videos (product_id, source_kind, source_id, style, duration_s, model, copy_json, voice,"
            " ai_person, created_at) VALUES (?, 'presenter', ?, 'talking', ?, ?, ?, ?, 1, ?)",
            (product_id, presenter_id, round(seconds_for(text)), METHODS[method]["model"], json.dumps(copy), p["voice"],
             datetime.utcnow().isoformat(timespec="seconds"))).lastrowid
    jobs.enqueue(conn, "talking", {"video_id": vid}, dedupe_key=f"video:{vid}")
    return vid, ""


def compare(conn, presenter_id: int, product_id: int, text: str) -> tuple[list[int], str]:
    """The comparison sample: the same line by each method in COMPARE."""
    batch = f"compare-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
    ids = []
    for m in COMPARE:
        vid, why = create(conn, presenter_id, product_id, text, m, batch)
        if vid is None:
            return ids, why
        ids.append(vid)
    return ids, ""


# Presenter at the table: what the presenter does with the fruit while saying a line, in turn.
TABLE_ACTIONS = ("gestures with one hand toward the fruit on the counter in front of them",
                 "picks up the whole fruit and holds it toward the camera to show it",
                 "holds up the cut half to show the inside, then puts it back down",
                 "points at the cut fruit on the board and smiles")


# A fruit character's lines (feedback 2026-11-02: "has to have movement that is not always the same"): each line its
# own move, the mouth always speaking the words.
CHARACTER_MOVES = ("bounces up and down with excitement", "leans in close toward the camera as if sharing a secret",
                   "waves both arms wide open", "spins halfway round, then turns back to the camera",
                   "points at the viewer and winks", "hops from side to side",
                   "puts its hands on its cheeks in delight", "tilts its head and shrugs playfully")


def _native_prompt(p: dict[str, Any], text: str, action: str = "") -> str:
    from creative_intelligence.adstudio.presenters import split_look
    person, setting = split_look(p["look"])
    if action:                                   # at the table: the fruit is in the picture and they show it
        setting = "the same kitchen counter with the fruit in front of them"
    shot = " Filmed handheld at arm's length like a casual phone selfie, with a little natural camera movement." \
        if p.get("framing") == "selfie" else ""
    return (f"The person in the image, {person}, looks into the camera and says, warmly and naturally, in clear American "
            f"English: \"{text}\" Natural lip movement matching the words, small natural head movement and blinks; "
            + (f"while talking they {action}; the fruit keeps exactly its shape, colour and number. " if action
               else "hands relaxed. ")
            + f"Same face, hair and clothes as the image; background stays {setting}. No captions, subtitles, text, "
            "logos or music; only the voice and soft room tone.{shot}")


def table_scene(conn, presenter_id: int, product_id: int) -> str:
    """Presenter at the table: the approved presenter at a bright kitchen counter, the product in front of them whole
    and cut open, chest-up, 9:16. Made once per presenter and product (one image), then every line starts from it."""
    from creative_intelligence.adstudio import costs
    from creative_intelligence.adstudio.store import to_data_uri
    from creative_intelligence.providers import model, replicate
    from creative_intelligence.storage import storage
    from creative_intelligence.webapp.common import _primary_photo_uris, _product_dict
    out = storage().area_dir("videos") / f"table_pr{presenter_id}_p{product_id}.png"
    if out.exists():
        return str(out)
    p, face = portrait(conn, presenter_id)
    name = (_product_dict(conn, product_id) or {}).get("name", "the fruit")
    from creative_intelligence.adstudio.shots import realism
    prompt = (f"A vertical 9:16 frame from a phone video. The person in the first reference photo, exactly as they look "
              f"there (same face, hair and clothes), sits at a bright home kitchen counter facing the camera, framed from "
              f"the chest up, looking into the lens, hands resting on the counter. On the white marble counter in front "
              f"of them, close to the camera and filling the lower third of the frame edge to edge, LARGE and sharp: "
              f"{name} exactly as in the other reference photos, a few whole and one cut open on a small board, flesh "
              f"facing the camera; the fruit reads clearly on a phone screen (2026-11-03: it came out too small). Window light, a "
              f"real home kitchen behind them. {realism('person')} No text, logos or captions.")
    url = replicate.generate_image(prompt, [to_data_uri(face)] + _primary_photo_uris(conn, product_id, limit=2), "9:16",
                                   bucket="video")
    path = storage().put_url("videos", out.name, url)
    costs.log_image(conn, model("image"), "video", 0, kind="video-still")
    return str(path)


def cutout_face(conn, presenter_id: int) -> str:
    """Talking head (feedback 2026-11-02, with an example: the presenter cut out, head and shoulders in the bottom-left
    corner, no circle): the approved look on a flat green backdrop, so the clip's background can be keyed out. Made
    once per approved look (one image), then every line starts from it."""
    from creative_intelligence.adstudio import costs
    from creative_intelligence.adstudio.store import to_data_uri
    from creative_intelligence.providers import model, replicate
    from creative_intelligence.storage import storage
    p, face = portrait(conn, presenter_id)
    out = storage().area_dir("videos") / f"cutout2_pr{presenter_id}_l{p.get('approved_look_id') or 0}.png"
    if out.exists():
        return str(out)
    prompt = ("A photorealistic vertical 9:16 video frame. The person in the reference photo, exactly as they look there "
              "(same face, hair and clothes), framed from the chest up, centred, facing the camera and looking into the "
              "lens. Behind them only a flat, evenly lit, pure chroma-key green (#00FF00) backdrop filling the whole "
              "background: no room, no shadows on it, no green on the person or their clothes. No text or logos.")
    url = replicate.generate_image(prompt, [to_data_uri(face)], "9:16", bucket="video")
    path = storage().put_url("videos", out.name, url)
    costs.log_image(conn, model("image"), "video", 0, kind="video-still")
    # 2026-11-03: the image model kept the presenter's real room instead of the green, so nothing was keyed out and
    # the head sat in a square. The person is cut out by a background remover and laid on pure green here.
    cut = replicate.run(BG_REMOVER, {"image": to_data_uri(str(path))}, bucket="video")
    costs.log_image(conn, BG_REMOVER, "video", 0, kind="video-still")
    return str(on_green(storage().put_url("videos", out.stem + "_alpha.png", cut), Path(path)))


BG_REMOVER = "851-labs/background-remover:a029dff38972b5fda4ec5d75d7d1cd25aeff621d2cf4946a41055d7db66b80bc"


def on_green(alpha_png: str | Path, out: Path) -> Path:
    """The cut-out person (a PNG with transparency) on flat chroma-key green (timeline.GREEN_KEY's colour)."""
    from PIL import Image
    im = Image.open(alpha_png).convert("RGBA")
    bg = Image.new("RGBA", im.size, (0, 255, 0, 255))
    bg.alpha_composite(im)
    bg.convert("RGB").save(out)
    return out


def make(conn, presenter_id: int, line: str, method: str, out_name: str, source_id: int,
         client: Any = None, face: str | None = None, action: str = "",
         context: tuple[str, str] = ("", "")) -> tuple[str, float, str]:
    """One talking-head clip, synchronously (callers are jobs). Returns (path, seconds, model). Costs are logged
    against video `source_id`."""
    from creative_intelligence.adstudio import costs
    from creative_intelligence.adstudio.store import to_data_uri
    from creative_intelligence.providers import replicate
    from creative_intelligence.storage import storage
    p, look = portrait(conn, presenter_id)
    face = face or look                          # Presenter at the table starts from its table scene
    m = METHODS[fit(method, line)]
    vdir = storage().area_dir("videos")
    if m["kind"] == "native":
        url, model, secs, sound = replicate.generate_clip(_native_prompt(p, line, action), to_data_uri(face), veo_seconds(line),
                                                         "9:16", route="veo", audio=True)
        costs.log_video(conn, model, secs, "video", source_id, audio=sound)
        if not sound:       # Veo failed and its fallback has no voice: a face talking in silence (2026-10-26)
            return make(conn, presenter_id, line, FALLBACK, out_name, source_id, client, face, action, context)
    else:
        audio, _ = voices.speak(conn, line, p["voice"], vdir / f"{out_name}_vo", "video", source_id, client=client,
                                context=context)
        secs = videotools.probe_audio(audio)
        from creative_intelligence.providers import kling
        prompt = ("A friendly presenter talking to the camera, natural expressions and head movement"
                  + (f"; while talking they {action}." if action else "."))
        if m["model"] == "kwaivgi/kling-avatar-v2" and kling.enabled("avatar"):   # Kling's own API, from the pack
            from creative_intelligence import budget
            budget.check_now("video", costs.price(conn, kling.AVATAR) * secs)
            slot = replicate._slot("kwaivgi/kling-v3-video")      # the pack's parallel limit, shared with shots
            slot.acquire()
            try:
                url, _ = kling.avatar(to_data_uri(face), str(audio), prompt)
                costs.log_video(conn, kling.AVATAR, secs, "video", source_id)
                return storage().put_url("videos", f"{out_name}.mp4", url), secs, kling.AVATAR
            except kling.KlingError as exc:
                import logging
                logging.getLogger("adstudio").warning("kling avatar failed, using OmniHuman: %s", str(exc)[:300])
            finally:
                slot.release()
        if m["model"] == "bytedance/seedance-2.5":
            from creative_intelligence import budget
            budget.check_now("video", costs.price(conn, m["model"]) * secs)
            with open(black_video(audio), "rb") as black:
                payload = (seedance_text_payload(p, black, secs, action) if fit(method, line) == "seedance_text"
                           else seedance_talking_payload(to_data_uri(face), black, secs, action))
                url = replicate.run(m["model"], payload, units=secs, bucket="video")
            costs.log_video(conn, m["model"], secs, "video", source_id)
            got = storage().put_url("videos", f"{out_name}_raw.mp4", url)
            return str(with_voice(got, audio, vdir / f"{out_name}.mp4")), secs, m["model"]
        if m["model"] == "kwaivgi/kling-avatar-v2" and kling.paused("avatar"):
            # Kling's avatar service is stalled (2026-11-02: hours on Kling direct AND on Replicate, which runs it on
            # Kling too): OmniHuman, another provider, makes the clip instead of waiting in the same queue
            m = METHODS["omnihuman"]
        with open(audio, "rb") as fh:
            payload = {"image": to_data_uri(face), "audio": fh}
            if m["model"] != "bytedance/omni-human":
                payload["prompt"] = ("A friendly presenter talking to the camera, natural expressions and head movement"
                                     + (f"; while talking they {action}." if action else "."))
            if m["model"] == "kwaivgi/kling-avatar-v2":
                payload["mode"] = "std"
            url = replicate.run(m["model"], payload, units=secs, bucket="video")
        costs.log_video(conn, m["model"], secs, "video", source_id)
        model = m["model"]
    return storage().put_url("videos", f"{out_name}.mp4", url), secs, model


def black_video(audio: str | Path) -> Path:
    """A black 9:16 video carrying the voice track (the reference Seedance lip-syncs to)."""
    import subprocess
    out = Path(audio).with_suffix(".black.mp4")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=720x1280:r=24", "-i", str(audio),
                    "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(out)], check=True)
    return out


def with_voice(clip: str | Path, audio: str | Path, out: Path) -> Path:
    """The clip with the house voice track in place of the model's own sound (it lip-synced to that track)."""
    import subprocess
    # re-encoded, not copied: Seedance's own stream made the QA frame grab fail ("mjpeg … Invalid argument", 2026-11-04)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(clip), "-i", str(audio), "-map", "0:v", "-map", "1:a",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast", "-crf", "18", "-c:a", "aac",
                    "-shortest", str(out)], check=True)
    return out


def seedance_talking_payload(face_uri: str, black: Any, secs: float, action: str = "") -> dict[str, Any]:
    # The user's own prompt (2026-11-04), kept short: "simple prompts usually do well … provided the image is good"
    return {"prompt": (f"The person in [Image1] {action + ', ' if action else 'is '}naturally and perfectly "
                       "lip syncing to speak the dialogue from [Video1]. Static camera, with natural hand movements, head "
                       "movements and natural eye movements. The background moves only a little, naturally."),
            "reference_images": [face_uri], "reference_videos": [black],
            "duration": max(4, min(30, int(secs + 0.99))), "resolution": "720p", "aspect_ratio": "9:16",
            "generate_audio": True}


def seedance_text_payload(p: dict[str, Any], black: Any, secs: float, action: str = "") -> dict[str, Any]:
    """The presenter described, not shown: no face goes in, so Seedance's face guard has nothing to refuse. The same
    seed per presenter keeps them looking alike from clip to clip."""
    from creative_intelligence.adstudio.presenters import split_look
    person, setting = split_look(p.get("look") or "")
    return {"prompt": (f"A real handheld phone video. {person[:1].upper() + person[1:]}, in {setting}, talks to the "
                       f"camera{', and ' + action if action else ''}, naturally and perfectly lip syncing to speak the "
                       "dialogue from [Video1]. Static camera, with natural hand movements, head movements and natural "
                       "eye movements. One person only. No text."),
            "reference_videos": [black], "seed": 1000 + int(p.get("id") or 0),
            "duration": max(4, min(30, int(secs + 0.99))), "resolution": "720p", "aspect_ratio": "9:16",
            "generate_audio": True}


def lipsync(conn, face: str, audio: str | Path, out_name: str, source_id: int, prompt: str = "") -> str:
    """A picture made to speak a voice track already recorded (a fruit character, feedback 2026-10-30: "the fruit
    should be the one talking, like lip synced"). The clip is as long as the voice. Returns the stored path."""
    from creative_intelligence.adstudio import costs
    from creative_intelligence.adstudio.store import to_data_uri
    from creative_intelligence.providers import replicate
    from creative_intelligence.storage import storage
    m = METHODS[LIPSYNC]
    secs = videotools.probe_audio(audio)
    from creative_intelligence.providers import kling
    if kling.enabled("avatar"):                  # Kling's own Avatar API takes any picture, from the resource pack
        from creative_intelligence import budget
        budget.check_now("video", costs.price(conn, kling.AVATAR) * secs)
        slot = replicate._slot("kwaivgi/kling-v3-video")
        slot.acquire()
        try:
            url, _ = kling.avatar(to_data_uri(face), str(audio), prompt)
            costs.log_video(conn, kling.AVATAR, secs, "video", source_id)
            return storage().put_url("videos", f"{out_name}.mp4", url)
        except kling.KlingError as exc:          # OmniHuman on Replicate stands in
            import logging
            logging.getLogger("adstudio").warning("kling avatar (character) failed, using OmniHuman: %s", str(exc)[:300])
        finally:
            slot.release()
    with open(audio, "rb") as fh:
        payload = {"image": to_data_uri(face), "audio": fh}
        if prompt and m["model"] != "bytedance/omni-human":
            payload["prompt"] = prompt
        url = replicate.run(m["model"], payload, units=secs, bucket="video")
    costs.log_video(conn, m["model"], secs, "video", source_id)
    return storage().put_url("videos", f"{out_name}.mp4", url)


def lipsync_estimate(conn, text: str) -> float:
    from creative_intelligence.adstudio.costs import price
    from creative_intelligence.providers import kling
    return round(price(conn, kling.AVATAR if kling.enabled() else METHODS[LIPSYNC]["model"]) * seconds_for(text), 2)


PRODUCT_CRITERIA = ("appetite", "box_label", "focal_point", "fruit_accuracy", "prominence")


def process(video_id: int, db: Callable[[], Any], client: Any = None) -> dict[str, Any]:
    """The talking job (in the worker)."""
    from creative_intelligence.adstudio import video_qa
    from creative_intelligence.adstudio.store import to_data_uri
    conn = db()
    try:
        v = dict(conn.execute("SELECT * FROM adstudio_videos WHERE id=?", (video_id,)).fetchone())
        c = json.loads(v["copy_json"])
        p, face = portrait(conn, c["presenter_id"])
        animate._set(conn, video_id, status="running", step="Making the clip")
        out, secs, model = make(conn, c["presenter_id"], c["line"], c["method"], f"talk_v{video_id}", video_id, client)
        animate._set(conn, video_id, clip_path=out, model=model, step="Quality check", has_audio=1)
        # a single presenter clip shows no product: judge the person only (2026-11-04: Jack's clip lost points for "no
        # jackfruit in frame", the presenter's name read as the product)
        drawn = c["method"] == "seedance_text"      # drawn from the description: no face to match
        video_qa.score(conn, video_id, out, [] if drawn else [to_data_uri(face)],
                       {"name": "a presenter (no product in this clip)", "product_type": "other"},
                       count_check=False, is_montage=False, presenter=True, skip=list(PRODUCT_CRITERIA),
                       note="There is no reference face: the presenter was drawn from a description. Judge only that "
                            "the person looks real and stays the same from first frame to last." if drawn else "")
        animate._set(conn, video_id, status="done", step="", finished_at=datetime.utcnow().isoformat(timespec="seconds"))
        return {"video_id": video_id, "model": model, "seconds": secs}
    except Exception as exc:
        animate._set(conn, video_id, status="failed", step="", message=str(exc)[:300])
        raise
    finally:
        conn.close()


def clips(conn, presenter_id: int | None = None) -> list[dict[str, Any]]:
    from creative_intelligence import budget
    from creative_intelligence.adstudio import qa
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM adstudio_videos WHERE style='talking'" + (" AND source_id=?" if presenter_id else "") + " ORDER BY id DESC",
        (presenter_id,) if presenter_id else ())]
    scores = qa.scores_for(conn, "video", [r["id"] for r in rows])
    for r in rows:
        r["copy"] = json.loads(r.pop("copy_json") or "{}")
        r["qa"], r["spent_usd"] = scores.get(r["id"]), budget.spent_on_video(conn, r["id"])
        r["method_label"] = METHODS.get(r["copy"].get("method"), {}).get("label", r["model"])
    return rows
