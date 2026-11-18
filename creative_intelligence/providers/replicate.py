"""Replicate adapter for images and video: the generation budget is checked before every call
(with the call's estimated cost), transient errors are retried, the output is one URL/file URI."""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any

log = logging.getLogger("adstudio")

# Replicate throttles an account (e.g. 6 requests a minute, one at a time, while its credit is under $5). A 429 now
# waits and tries again instead of failing the image (feedback 2026-10-23: 7 of 8 images failed at once), and every
# thread in this process waits out the same cool-down so parallel slots stop hammering the limit.
THROTTLE_RETRIES = 8
THROTTLE_WAIT_S = 12.0
_cool_lock = threading.Lock()
_cool_until = [0.0]


def _cool_down(exc: Exception, n: int) -> float:
    m = re.search(r"retry[_ -]after\D{0,5}(\d+(?:\.\d+)?)", str(exc), re.I)
    wait = float(m.group(1)) if m else THROTTLE_WAIT_S * (1 + n / 2)
    with _cool_lock:
        _cool_until[0] = max(_cool_until[0], time.monotonic() + wait)
    return wait


def _wait_turn() -> None:
    while True:
        with _cool_lock:
            left = _cool_until[0] - time.monotonic()
        if left <= 0:
            return
        time.sleep(min(left, 5))


# A prediction that never comes back held a video on "Making the pieces" for over an hour (2026-11-02): each call has
# a time limit, then counts as a transient failure (retried, at most 3 tries in all).
TIME_LIMIT_S = {"video": 20 * 60, "generation": 8 * 60}


def _cancel(p: Any) -> None:
    """Best effort. The prediction's own cancel link: the client's cancel() 404s on partner models (Kling Avatar,
    2026-11-02), and some of those can't be cancelled once Kling has them at all."""
    try:
        import httpx
        from creative_intelligence import config
        url = (getattr(p, "urls", None) or {}).get("cancel")
        if url:
            httpx.post(url, headers={"Authorization": f"Bearer {config.CI_REPLICATE_API_KEY}"}, timeout=20)
        else:
            p.cancel()
    except Exception:                                    # noqa: BLE001
        pass


class ModelError(RuntimeError):
    """A prediction the model itself failed (retried once more, like Replicate's own ModelError)."""


def _run_limited(client: Any, model: str, payload: dict[str, Any], limit: float) -> Any:
    """client.run with a time limit. With the predictions API (the real client), a prediction that runs past the limit
    is cancelled on Replicate (2026-11-02: timed-out Kling Avatar clips kept running and piled up, 37 at once)."""
    preds = getattr(client, "predictions", None)
    if preds is not None and hasattr(preds, "create") and hasattr(client, "models"):
        try:
            if ":" in model:
                p = preds.create(version=model.split(":", 1)[1], input=payload)
            else:
                p = client.models.predictions.create(model=model, input=payload)
        except Exception as exc:                             # noqa: BLE001
            if "404" not in str(exc) and "not found" not in str(exc).lower():
                raise
            p = None                                         # not an official model: client.run resolves its version
        end = time.monotonic() + limit
        while p is not None and p.status not in ("succeeded", "failed", "canceled"):
            if time.monotonic() > end:
                _cancel(p)
                raise TimeoutError(f"{model} timed out after {round(limit / 60)} min (cancelled)")
            time.sleep(3)
            p.reload()
        if p is not None:
            if p.status != "succeeded":
                raise ModelError(str(p.error or p.status))
            return p.output
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        return pool.submit(client.run, model, input=payload).result(timeout=limit)
    except FutureTimeout:
        raise TimeoutError(f"{model} timed out after {round(limit / 60)} min") from None
    finally:
        pool.shutdown(wait=False)


# Models whose provider caps parallel work for the account (2026-11-02, Kling through Replicate: "parallel task over
# resource pack limit", and clips stuck in its queue timed out): at most this many at once from this process; the rest
# wait their turn here, before their time limit starts.
MODEL_LIMITS = {"kwaivgi/kling-v3-video": 4,     # one queue for Kling direct and Kling on Replicate (trial pack: 5)
                "kwaivgi/kling-avatar-v2": 4,     # 37 avatar clips at once jammed Kling's queue (2026-11-02)
                "bytedance/omni-human-1.5": 10}   # no queue trouble seen; 4 made two presenter videos wait in rounds
_model_slots: dict[str, threading.BoundedSemaphore] = {}
_slots_lock = threading.Lock()


def _slot(model: str) -> threading.BoundedSemaphore | None:
    base = model.split(":")[0]
    if base not in MODEL_LIMITS:
        return None
    with _slots_lock:
        return _model_slots.setdefault(base, threading.BoundedSemaphore(MODEL_LIMITS[base]))


class GenerationError(RuntimeError):
    pass


def estimate(model: str, units: float = 1) -> float:
    from creative_intelligence.adstudio import costs
    from creative_intelligence.db import get_connection, init_db
    init_db()
    conn = get_connection()
    try:
        return costs.price(conn, model) * units
    finally:
        conn.close()


def run(model: str, payload: dict[str, Any], units: float = 1, bucket: str = "generation") -> str:
    """Run a Replicate model after the budget check. Returns a URL or file:// URI."""
    import replicate
    from creative_intelligence import budget, config
    if not config.CI_REPLICATE_API_KEY:
        raise GenerationError("CI_REPLICATE_API_KEY is not set.")
    budget.check_now(bucket, estimate(model, units))
    client = replicate.Client(api_token=config.CI_REPLICATE_API_KEY)
    last: Exception | None = None
    attempt = throttled = 0
    while attempt < 3:
        _wait_turn()
        try:
            slot = _slot(model)
            if slot:
                slot.acquire()
            try:
                return output_url(_run_limited(client, model, payload, TIME_LIMIT_S.get(bucket, 20 * 60)))
            finally:
                if slot:
                    slot.release()
        except Exception as exc:
            last = exc
            m = str(exc).lower()
            if "429" in m or "throttled" in m or "resource pack limit" in m or "parallel task" in m:
                if throttled >= THROTTLE_RETRIES:
                    raise GenerationError("Replicate kept throttling (add credit to the Replicate account): "
                                          + str(exc)) from exc
                _cool_down(exc, throttled)
                throttled += 1
                continue                                   # a throttle isn't a failed attempt
            attempt += 1
            # a prediction the model itself failed (ModelError, often with no message) or Replicate interrupted
            # ("Prediction interrupted; please retry", 2026-11-02) is worth one more try, like a network error
            if type(exc).__name__ == "ModelError" or any(k in m for k in (
                    "timed out", "timeout", "read operation", "connection", "502", "503", "504", "temporarily",
                    "failed to upload", "interrupted", "please retry")):
                time.sleep(3 * (attempt + 1))              # transient: retry
                continue
            from creative_intelligence.alerts import credit_out
            credit_out("Replicate", exc)
            raise GenerationError(str(exc)) from exc
    raise GenerationError(f"failed after retries: {last}")


def generate_image(prompt: str, reference_data_uris: list[str], aspect_ratio: str = "4:5",
                   bucket: str = "generation") -> str:
    from creative_intelligence.providers import model
    payload: dict[str, Any] = {"prompt": prompt, "aspect_ratio": aspect_ratio, "output_format": "png"}
    if reference_data_uris:
        payload["image_input"] = reference_data_uris
    return run(model("image"), payload, bucket=bucket)


ROUTES = {"kling": "CI_VIDEO_MODEL", "kling_pro": "CI_VIDEO_MODEL", "veo": "CI_VIDEO_MODEL_AMBIENT",
          "seedance": "CI_VIDEO_MODEL_MULTIREF", "seedance25": "CI_VIDEO_MODEL_SEEDANCE25", "wan3": "CI_VIDEO_MODEL_WAN3"}
ENGINES = ("seedance25", "wan3")   # whole-video model test: every shot of a video on one of these, no fallback
PRO = "kling_pro"                # Kling at 1080p 'pro': steadier camera and motion per Kling's guide, priced higher


def route_model(route: str) -> str:
    from creative_intelligence import config
    return getattr(config, ROUTES.get(route, "CI_VIDEO_MODEL"))


def price_key(model: str, audio: bool, pro: bool = False) -> str:
    """The price row a clip is quoted at: Kling's own API when its key is set (that is where Kling clips go)."""
    from creative_intelligence.providers import kling
    if "kling-v3" in model and kling.enabled():
        model = kling.MODEL
    return model + (":pro" if pro else "") + (":audio" if audio else "")


def _clip_payload(model: str, prompt: str, start_image: str | None, duration: int, aspect: str, audio: bool,
                  refs: list[str] | None, negative: str, end_image: str | None = None,
                  pro: bool = False) -> tuple[dict[str, Any], int]:
    """Each model's own input fields (checked against Replicate's schemas). Returns (payload, seconds)."""
    if "veo" in model:
        secs = min((4, 6, 8), key=lambda d: abs(d - duration))
        p: dict[str, Any] = {"prompt": prompt, "duration": secs, "aspect_ratio": aspect, "resolution": "720p",
                             "generate_audio": audio}
        if start_image:
            p["image"] = start_image
            if end_image:
                p["last_frame"] = end_image
        if negative:
            p["negative_prompt"] = negative
        return p, secs
    if "wan-3" in model:                 # alibaba/wan-3 (Replicate schema 2026-11-04): no end frame; with an image the
        p = {"prompt": prompt, "duration": duration, "resolution": "720p",      # image's own ratio is used
             "aspect_ratio": aspect}
        if start_image:
            p["image"] = start_image
        if negative:
            p["negative_prompt"] = negative
        return p, duration
    if "seedance" in model:
        if negative:                # Seedance has no negative field: its guide asks for constraint sentences instead
            prompt = f"{prompt}\nConstraints: one continuous shot; keep exactly the items the photo shows. Avoid: {negative}."
        p = {"prompt": prompt, "duration": duration, "aspect_ratio": aspect, "resolution": "720p", "generate_audio": audio}
        if start_image and "2.5" in model:
            p["aspect_ratio"] = "adaptive"              # Seedance 2.5: first/last-frame mode needs 'adaptive'
        if refs and not start_image:
            p["reference_images"] = refs[:9]            # Seedance can't mix a first frame with references
        elif start_image:
            p["image"] = start_image
            if end_image:
                p["last_frame_image"] = end_image
        return p, duration
    p = {"prompt": prompt, "duration": duration, "aspect_ratio": aspect, "mode": "pro" if pro else "standard",
         "generate_audio": audio}
    if start_image:
        p["start_image"] = start_image
        if end_image:
            p["end_image"] = end_image
    if negative:
        p["negative_prompt"] = negative
    return p, duration


def generate_clip(prompt: str, start_image: str | None = None, duration: int = 5, aspect_ratio: str = "9:16",
                  route: str = "kling", audio: bool = False, refs: list[str] | None = None,
                  negative: str = "", end_image: str | None = None) -> tuple[str, str, int, bool]:
    """A clip through the route's model, budget-checked as video; on a model failure (not a budget
    stop) the route's fallback (VIDEO_FALLBACKS) is tried once, without sound (a fallback model's
    sound rate can be much higher). Returns (url, model used, seconds, with sound)."""
    from creative_intelligence import budget, config
    from creative_intelligence.providers import kling
    fallbacks = dict(x.split(":") for x in config.VIDEO_FALLBACKS.split(",") if ":" in x)
    tried: list[str] = []
    for i, r in enumerate([route] + ([fallbacks[route]] if fallbacks.get(route) and fallbacks[route] != route else [])):
        model, sound, pro = route_model(r), audio and i == 0, r == PRO
        if "kling" in model and kling.enabled():         # Kling's own API first (half the price); Replicate if it fails
            secs = max(3, min(15, int(duration)))
            budget.check_now("video", estimate(price_key(kling.MODEL, sound, pro), secs))
            slot = _slot(model)
            if slot:
                slot.acquire()
            try:
                url, secs = kling.generate(prompt, start_image, secs, aspect_ratio, sound, negative, end_image, pro)
                return url, kling.MODEL + (":pro" if pro else ""), secs, sound
            except kling.KlingError as exc:
                tried.append(f"kling direct: {exc}")
                log.warning("kling direct failed, using Replicate: %s", str(exc)[:300])
            finally:
                if slot:
                    slot.release()
        payload, secs = _clip_payload(model, prompt, start_image, duration, aspect_ratio, sound, refs, negative, end_image,
                                      pro)
        if not config.CI_REPLICATE_API_KEY:
            raise GenerationError("CI_REPLICATE_API_KEY is not set.")
        budget.check_now("video", estimate(price_key(model, sound, pro), secs))
        try:
            return run(model, payload, units=secs, bucket="video"), model + (":pro" if pro else ""), secs, sound
        except budget.BudgetExceeded:
            raise
        except GenerationError as exc:
            tried.append(f"{model}: {exc}")
            log.warning("video route %s failed, %s: %s", r, "trying the fallback" if i == 0 else "no fallback left",
                        str(exc)[:300])                  # 2026-10-26: plated fell back with no reason kept
    raise GenerationError("; ".join(tried)[:500])


def generate_video(prompt: str, start_image: str | None = None, duration: int = 5,
                   aspect_ratio: str = "9:16", mode: str = "standard") -> str:
    """Kling 3.0 on Replicate (kwaivgi/kling-v3-video). Priced per second of video."""
    from creative_intelligence.providers import model
    payload: dict[str, Any] = {"prompt": prompt, "duration": duration, "aspect_ratio": aspect_ratio,
                               "mode": mode, "generate_audio": False}
    if start_image:
        payload["start_image"] = start_image
    return run(model("video"), payload, units=duration, bucket="video")


def output_url(out: Any) -> str:
    """Normalise Replicate output (FileOutput, list, URL string, bytes) to one URL / file URI."""
    import tempfile
    from pathlib import Path

    def bytes_to_uri(data: bytes) -> str:
        suffix = ".png" if data[:4] == b"\x89PNG" else ".mp4" if data[4:8] == b"ftyp" else ".bin"
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix, dir=tempfile.gettempdir(), prefix="adstudio_")
        tmp.write(data)
        tmp.close()
        return Path(tmp.name).as_uri()

    def one(item: Any) -> str:
        url = getattr(item, "url", None)
        if callable(url):
            try:
                return url()
            except Exception:
                pass
        elif isinstance(url, str):
            return url
        read = getattr(item, "read", None)
        if callable(read):
            return bytes_to_uri(read())
        if isinstance(item, (bytes, bytearray)):
            return bytes_to_uri(bytes(item))
        return str(item)

    if out is None or (isinstance(out, (list, tuple, str)) and not out):
        raise GenerationError("The model returned nothing (usually its safety filter); change the prompt and try again")
    if isinstance(out, (list, tuple)):
        return one(out[0])
    return one(out)
