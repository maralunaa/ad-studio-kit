"""
Voice-over for Video Studio (V3): the brand's house voices (config.HOUSE_VOICES, picked by the owner in a
blind test) through text-to-speech on Replicate, and word timings for captions (WhisperX on Replicate).

  speak()         one voice-over file for a script; budget-checked first (video bucket), cost logged
  word_timings()  [{word, start, end}] for the file; if the timing model fails, timings are spread
                  over the audio by word length so captions still line up roughly (noted on the video)

Voices are brand narrators: scripts never make them a real customer or the brand's real staff.
"""
from __future__ import annotations

import re
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from creative_intelligence import config

from creative_intelligence import brand as _brand

UA = {"User-Agent": _brand.user_agent()}


def house() -> list[dict[str, str]]:
    return list(config.HOUSE_VOICES)


def voice(name: str) -> dict[str, str]:
    v = next((v for v in config.HOUSE_VOICES if v["name"].lower() == (name or "").lower()), None)
    if v is None:
        raise ValueError(f"unknown voice {name!r}")
    return v


def _base(model: str) -> str:
    return model.split(":")[0]


# ElevenLabs v3 voices on Replicate (its schema's enum, 2026-11-04); auditions may only use these
ELEVEN_VOICES = ("Rachel", "Drew", "Clyde", "Paul", "Aria", "Domi", "Dave", "Roger", "Fin", "Sarah", "James", "Jane",
                 "Juniper", "Arabella", "Hope", "Bradford", "Reginald", "Austin", "Kuon", "Blondie", "Priyanka",
                 "Alexandra", "Monika", "Mark")


def payload(v: dict[str, str], text: str, context: tuple[str, str] = ("", "")) -> dict[str, Any]:
    """The input each text-to-speech family takes (checked against the Replicate schemas). context: the spoken lines
    before and after, so ElevenLabs reads this one as part of the same talk, not cold (feedback 2026-11-04: the voice
    "still not 100%"; each line was read on its own)."""
    base = _base(v["model"])
    if base.startswith("elevenlabs/"):
        out = {"prompt": text, "voice": v["voice"], "stability": 0.4}   # a little livelier than 0.5 (2026-11-02)
        if v.get("expressive"):                     # a character or a storyteller: livelier still
            out.update(stability=0.3, style=0.5)
        if base != "elevenlabs/v3":                 # v3 refuses them ("not yet supported", live 2026-11-04)
            if context[0]:
                out["previous_text"] = context[0]
            if context[1]:
                out["next_text"] = context[1]
        return out
    if base.startswith("minimax/"):
        return {"text": text, "voice_id": v["voice"], "emotion": "auto"}
    if base.startswith("inworld/"):                 # steered with a bracketed direction before the words
        return {"text": (f"[{v['style']}] " if v.get("style") else "") + text, "voice_id": v["voice"]}
    if base.startswith("google/"):                  # Gemini TTS: a delivery direction for the whole line
        return {"text": text, "voice": v["voice"], "output_format": "mp3", **({"style": v["style"]} if v.get("style") else {})}
    return {"text": text, "voice": v["voice"]}                      # Kokoro


def estimate(conn, v: dict[str, str], text: str) -> float:
    from creative_intelligence.adstudio.costs import price
    base = _base(v["model"])
    unit = price(conn, base)
    per_k = base.startswith(("elevenlabs/", "minimax/", "inworld/", "google/"))
    return round(unit * len(text) / 1000, 4) if per_k else round(unit, 4)


def _log(conn, model: str, units: float, cost: float, source: str, source_id: int, note: str) -> float:
    from creative_intelligence.adstudio.costs import _api_log
    now = datetime.utcnow().isoformat()
    with conn:
        conn.execute("INSERT INTO model_calls (at, model, kind, units, cost_usd, source, source_id, run_id)"
                     " VALUES (?,?,?,?,?,?,?,NULL)", (now, model, "video", units, cost, source, source_id))
    _api_log(f"{now} | REPLICATE | {model} | {source}#{source_id} {note} | {units:g} | ${cost:.4f}")
    return cost


def _client():
    import replicate
    if not config.CI_REPLICATE_API_KEY:
        raise RuntimeError("CI_REPLICATE_API_KEY is not set.")
    return replicate.Client(api_token=config.CI_REPLICATE_API_KEY)


def _run(client: Any, model: str, inp: dict[str, Any]) -> Any:
    """client.run that waits out a Replicate throttle (429) like the image and clip calls do (2026-11-03: three of
    batch #13's videos failed when their voice lines all hit "rate limit … 60 requests per minute" at once)."""
    from creative_intelligence.providers import replicate as rp
    for n in range(rp.THROTTLE_RETRIES + 1):
        rp._wait_turn()
        try:
            return client.run(model, input=inp)
        except Exception as exc:                     # noqa: BLE001
            m = str(exc).lower()
            if n >= rp.THROTTLE_RETRIES or not ("429" in m or "throttled" in m):
                raise
            rp._cool_down(exc, n)
            for f in inp.values():                   # a file sent once is read again from the start
                if hasattr(f, "seek"):
                    f.seek(0)


def _url(out: Any) -> str:
    """A model's output as one URL: Gemini TTS answers with a list of files (2026-11-04), the others with one."""
    from creative_intelligence.providers.replicate import output_url
    return output_url(out)


def _download(url: str, out: Path) -> Path:
    out.write_bytes(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120).read())
    return out


def speak(conn, text: str, name: str, out: Path, source: str = "video", source_id: int = 0,
          client: Any = None, context: tuple[str, str] = ("", ""), style: str = "") -> tuple[Path, float]:
    """Read `text` in house voice `name` into `out` (mp3/wav as the model gives it). Returns (file, cost).
    style: how this format wants it said (a cartoon character, a clay storyteller), in place of the voice's own:
    Gemini takes it as words; ElevenLabs reads it as more expressive."""
    from creative_intelligence import budget
    v = voice(name)
    if style:
        v = {**v, "style": style, "expressive": True}
    est = estimate(conn, v, text)
    budget.check(conn, "video", est)
    client = client or _client()
    url = _url(_run(client, v["model"], payload(v, text, context)))
    ext = Path(url.split("?")[0]).suffix or ".mp3"
    path = _download(url, out.with_suffix(ext))
    return path, _log(conn, _base(v["model"]), len(text), est, source, source_id, f"voice {v['name']}")


SAMPLE_LINE = ("Hey there. Let's open the box and see what's ripe this week. "
               "Honestly, you're going to love this one.")


def sample(conn, name: str, client: Any = None) -> Path:
    """A short listen of house voice `name` for the presenter cards: made once (a cent or two), then kept."""
    from creative_intelligence.storage import storage
    v = voice(name)
    d = storage().area_dir("presenters")
    stem = f"voice_{v['name'].lower()}_{v['voice'].lower()}"        # a new voice for the name gets a new sample
    have = sorted(d.glob(stem + ".*"))
    if have:
        return have[0]
    path, _ = speak(conn, SAMPLE_LINE, v["name"], d / stem, "voice_sample", 0, client=client)
    return path


ENGINES = {"eleven": config.ELEVENLABS_MODEL, "inworld": "inworld/realtime-tts-2",
           "gemini": "google/gemini-3.8-flash-tts", "minimax": "minimax/speech-2.8-hd"}


def audition(conn, voice_name: str, text: str, client: Any = None, engine: str = "eleven", style: str = "") -> Path:
    """A voice from any engine reading `text` (with an optional delivery direction), to pick a persona's voice by
    ear (2026-11-04). Kept once made."""
    import hashlib
    from creative_intelligence.storage import storage
    if engine not in ENGINES:
        raise ValueError(f"unknown voice engine {engine!r}")
    if engine == "eleven" and voice_name not in ELEVEN_VOICES:
        raise ValueError(f"unknown ElevenLabs voice {voice_name!r}")
    if not re.fullmatch(r"[A-Za-z0-9_\-]{2,60}", voice_name or ""):
        raise ValueError("unknown voice")
    text, style = (text or "").strip()[:300], (style or "").strip()[:200]
    if not text:
        raise ValueError("Write the line first.")
    d = storage().area_dir("presenters")
    stem = (f"audition_{engine}_{voice_name.lower()}_"
            f"{hashlib.sha1((text + '|' + style).encode()).hexdigest()[:10]}")
    if have := sorted(d.glob(stem + ".*")):
        return have[0]
    v = {"name": voice_name, "model": ENGINES[engine], "voice": voice_name, "style": style}
    from creative_intelligence import budget
    est = estimate(conn, v, text)
    budget.check(conn, "video", est)
    url = _url(_run(client or _client(), v["model"], payload(v, text)))
    path = _download(url, (d / stem).with_suffix(Path(url.split("?")[0]).suffix or ".mp3"))
    _log(conn, _base(v["model"]), len(text), est, "voice_audition", 0, f"audition {engine} {voice_name}")
    return path


def word_timings(conn, audio: Path, source: str = "video", source_id: int = 0, client: Any = None) -> list[dict[str, Any]]:
    """[{word, start, end}] from WhisperX. Raises if the model fails (callers fall back to spread_words)."""
    from creative_intelligence import budget
    from creative_intelligence.adstudio.costs import price
    est = price(conn, _base(config.WORDS_MODEL))
    budget.check(conn, "video", est)
    client = client or _client()
    with open(audio, "rb") as fh:
        res = _run(client, config.WORDS_MODEL, {"audio_file": fh, "align_output": True, "language": "en"})
    _log(conn, _base(config.WORDS_MODEL), 1, est, source, source_id, "word timings")
    segs = res.get("segments") if isinstance(res, dict) else res
    words = [{"word": w["word"].strip(), "start": float(w["start"]), "end": float(w["end"])}
             for s in segs or [] for w in s.get("words", []) if "start" in w and "end" in w and w.get("word")]
    if not words:
        raise RuntimeError("no word timings returned")
    return words


def spread_words(text: str, duration: float) -> list[dict[str, Any]]:
    """Rough timings when no timing model is available: words spread over the audio by length."""
    words = text.split()
    total = sum(len(w) + 2 for w in words) or 1
    t, out = 0.15, []
    span = max(0.1, duration - 0.3)
    for w in words:
        d = span * (len(w) + 2) / total
        out.append({"word": w, "start": round(t, 3), "end": round(t + d * 0.9, 3)})
        t += d
    return out


def norm(w: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", w.lower())
