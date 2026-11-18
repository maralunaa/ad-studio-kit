"""
Kling 3.0 straight from Kling's own API (2026-11-02: about half Replicate's price per second; the user's priority).

Checked against the Kling API docs (kling.ai/document-api, 2026-11-02):
  domain       https://api-singapore.klingai.com (servers outside China)
  auth         one API key, header "Authorization: Bearer <key>" (KLING_API_KEY; the user sets it in Railway)
  create       POST /image-to-video/kling-3.0 {contents: [prompt, first_frame, last_frame?], settings: {resolution,
               duration 3–15, audio native|off, multi_shot}} or POST /text-to-video/kling-3.0 {prompt, settings}
               → data.id
  query        GET /tasks?task_ids=<id> → data[0].status submitted|processing|succeeded|failed, outputs[0].url
  concurrency  per account and resource pack; over it, code 1303 "parallel task over resource pack limit": wait
               and try again (the docs ask for a back-off).
Results are kept by Kling for 30 days only: callers store the file at once (storage().put_url), as with Replicate.
Every call is appended to outputs/api.log with its timestamp and endpoint, like the other providers.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any

BASE = "https://api-singapore.klingai.com"
MODEL = "kling-direct/kling-3.0"        # the name costs and logs use (priced in config.MODEL_PRICES_SEED)
POLL_S = 6
TIME_LIMIT_S = 20 * 60
BUSY = 1303                             # parallel task over resource pack limit
# 2026-11-02: avatar tasks sat in "submitted" for hours, holding every slot of the pack. A task Kling hasn't started
# after STALL_S goes to the fallback at once, and that kind of task skips Kling for PAUSE_S (no 20-minute waits).
STALL_S = 180
PAUSE_S = 30 * 60
_paused: dict[str, float] = {}


def paused(kind: str) -> bool:
    return _paused.get(kind, 0) > time.monotonic()


def _stalled(kind: str, task: str) -> KlingError:
    _paused[kind] = time.monotonic() + PAUSE_S
    _log(f"task {task} | {kind} not started after {STALL_S // 60} min: {kind} goes to the fallback for {PAUSE_S // 60} min")
    return KlingError(f"Kling didn't start the {kind} task in {STALL_S // 60} min")


class KlingError(RuntimeError):
    pass


def enabled(kind: str = "video") -> bool:
    from creative_intelligence import config
    return bool(config.KLING_API_KEY) and not paused(kind)


def _log(line: str) -> None:
    from creative_intelligence.adstudio import costs
    costs._api_log(f"{datetime.utcnow().isoformat()} | KLING | {line}")


def _call(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    from creative_intelligence import config
    req = urllib.request.Request(BASE + path, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"Authorization": f"Bearer {config.KLING_API_KEY}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            out = json.loads(r.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            out = json.loads(exc.read().decode())
        except Exception:                                   # noqa: BLE001
            raise KlingError(f"HTTP {exc.code} from {path}") from exc
    if out.get("code") not in (0, None):
        raise KlingError(f"{out.get('code')}: {out.get('message', '')}".strip())
    return out


BUSY_WAIT_S = 120     # every slot busy for this long: the fallback, not more waiting


def _submit(path: str, body: dict[str, Any], id_key: str, kind: str) -> str:
    """Create the task; while every slot of the pack is busy (1303), back off and try again for BUSY_WAIT_S."""
    end, n = time.monotonic() + BUSY_WAIT_S, 0
    while True:
        try:
            return _call("POST", path, body)["data"][id_key]
        except KlingError as exc:
            if not str(exc).startswith(f"{BUSY}:"):
                _log(f"POST {path} | failed | {exc}")
                raise
            if time.monotonic() > end:
                raise _stalled(kind, "-") from exc
            n += 1
            time.sleep(min(15, 2 * n))


def _image(uri: str) -> str:
    """Kling takes a URL or plain Base64: a data URI loses its 'data:image/png;base64,' head."""
    return uri.split(",", 1)[1] if uri.startswith("data:") else uri


def generate(prompt: str, start_image: str | None, duration: int, aspect_ratio: str = "9:16", audio: bool = False,
             negative: str = "", end_image: str | None = None, pro: bool = False) -> tuple[str, int]:
    """One Kling 3.0 clip. Returns (video URL, seconds). Raises KlingError (the caller falls back to Replicate)."""
    secs = max(3, min(15, int(duration)))
    text = prompt + (f"\nAvoid: {negative}." if negative else "")     # the prompt carries the negatives too
    settings = {"resolution": "1080p" if pro else "720p", "duration": secs, "audio": "native" if audio else "off",
                "multi_shot": False}
    if start_image:
        contents = [{"type": "prompt", "text": text[:3000]}, {"type": "first_frame", "url": _image(start_image)}]
        if end_image:
            contents.append({"type": "last_frame", "url": _image(end_image)})
        path, body = "/image-to-video/kling-3.0", {"contents": contents, "settings": settings}
    else:
        path, body = "/text-to-video/kling-3.0", {"prompt": text[:3000], "settings": {**settings,
                                                                                     "aspect_ratio": aspect_ratio}}
    task = _submit(path, body, "id", "video")
    _log(f"POST {path} | task {task} | {secs} s{' | 1080p' if pro else ''}{' | sound' if audio else ''}")
    start = time.monotonic()
    end = start + TIME_LIMIT_S
    while time.monotonic() < end:
        time.sleep(POLL_S)
        d = _call("GET", f"/tasks?task_ids={task}")["data"][0]
        if d["status"] == "submitted" and time.monotonic() - start > STALL_S:
            raise _stalled("video", task)
        if d["status"] == "succeeded":
            v = next((o for o in d.get("outputs") or [] if o.get("type") == "video"), None)
            if not v or not v.get("url"):
                raise KlingError(f"task {task} succeeded with no video")
            _log(f"GET /tasks | task {task} | succeeded")
            return v["url"], int(float(v.get("duration") or secs))
        if d["status"] == "failed":
            _log(f"GET /tasks | task {task} | failed | {d.get('message', '')}")
            raise KlingError(f"task failed: {d.get('message') or 'no reason given'}")
    raise KlingError(f"task {task} timed out after {TIME_LIMIT_S // 60} min")


AVATAR = "kling-direct/avatar"          # presenter clips (talking.make), priced per second in MODEL_PRICES_SEED


def avatar(image: str, audio_path: str, prompt: str = "", mode: str = "std") -> tuple[str, float]:
    """A picture made to say a recorded voice track (Kling's Avatar API, docs 2026-11-02:
    POST /v1/videos/avatar/image2video {image, sound_file (Base64, ≤5 MB, 2–300 s), prompt, mode std|pro},
    GET /v1/videos/avatar/image2video/{id} → task_status succeed|failed, task_result.videos[0].url).
    Returns (video URL, seconds). Raises KlingError (the caller falls back to Replicate)."""
    import base64
    from pathlib import Path
    data = Path(audio_path).read_bytes()
    if len(data) > 5 * 1024 * 1024:
        raise KlingError("voice track over Kling's 5 MB limit")
    body = {"image": _image(image), "sound_file": base64.b64encode(data).decode(), "mode": mode}
    if prompt:
        body["prompt"] = prompt[:2500]
    path = "/v1/videos/avatar/image2video"
    task = _submit(path, body, "task_id", "avatar")
    _log(f"POST {path} | task {task} | avatar {mode}")
    start = time.monotonic()
    end = start + TIME_LIMIT_S
    while time.monotonic() < end:
        time.sleep(POLL_S)
        d = _call("GET", f"{path}/{task}")["data"]
        if d["task_status"] == "submitted" and time.monotonic() - start > STALL_S:
            raise _stalled("avatar", task)
        if d["task_status"] == "succeed":
            v = ((d.get("task_result") or {}).get("videos") or [{}])[0]
            if not v.get("url"):
                raise KlingError(f"avatar task {task} succeeded with no video")
            _log(f"GET {path} | task {task} | succeeded | {d.get('final_unit_deduction', '')} units")
            return v["url"], float(v.get("duration") or 0)
        if d["task_status"] == "failed" and "resource pack limit" in (d.get("task_status_msg") or ""):
            raise _stalled("avatar", task)                # every slot held: don't queue more behind it
        if d["task_status"] == "failed":
            _log(f"GET {path} | task {task} | failed | {d.get('task_status_msg', '')}")
            raise KlingError(f"avatar task failed: {d.get('task_status_msg') or 'no reason given'}")
    raise KlingError(f"avatar task {task} timed out after {TIME_LIMIT_S // 60} min")
