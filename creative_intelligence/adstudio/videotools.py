"""
ffmpeg helpers for Video Studio. Plain ffmpeg subprocesses (no MoviePy): the system ffmpeg on
Railway (7.x, with libass, drawtext, xfade, sidechaincompress), or the copy bundled with
imageio-ffmpeg as a fallback for local work.

Products are never cropped: changing the shape of a clip pads it (the clip fitted inside, the
rest filled with a blurred, darkened copy of itself).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

SIZES = {"9:16": (1080, 1920), "4:5": (1080, 1350), "1:1": (1080, 1080)}


def _x264(final: bool = False) -> list[str]:
    """H.264 settings; VIDEO_X264_PRESET trades speed for file size (tests: ultrafast). Working clips use
    quality-based CRF 20; a finished video (final=True) targets a bitrate above the playbook's minimum
    (8 Mbps at 1080p, knowledge/rules.yaml › export) at 30 fps, since CRF guarantees no minimum."""
    preset = ["-preset", os.getenv("VIDEO_X264_PRESET", "medium")]
    if not final:
        return ["-c:v", "libx264", "-crf", "20", *preset]
    spec = _export_spec()
    mbps = spec["min_bitrate_mbps"]
    return ["-c:v", "libx264", *preset, "-b:v", f"{mbps * 1.25:g}M", "-maxrate", f"{mbps * 1.5:g}M",
            "-bufsize", f"{mbps * 2.5:g}M", "-r", str(spec["fps"]), "-pix_fmt", "yuv420p"]


def _export_spec() -> dict:
    from creative_intelligence import knowledge
    return knowledge.load("rules")["export"]


class VideoToolError(RuntimeError):
    pass


def ffmpeg_exe() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # pragma: no cover - only without any ffmpeg
        raise VideoToolError("ffmpeg is not installed") from exc


def threads() -> int:
    """Threads per ffmpeg decoder, filter graph and encoder. The server shows 48 CPUs; letting ffmpeg start
    a thread pool per input there runs out of threads and memory on a busy render (FFMPEG_THREADS)."""
    try:
        return max(1, int(os.getenv("FFMPEG_THREADS", "4")))
    except ValueError:
        return 4


def _limited(args: list[str]) -> list[str]:
    n = str(threads())
    out = ["-filter_threads", n, "-filter_complex_threads", n]
    for a in args:
        if a == "-i":
            out += ["-threads", "2"]
        out.append(a)
    return out[:-1] + ["-threads", n, out[-1]]          # encoder threads, just before the output file


def _why(stderr: str) -> str:
    """The line that says what went wrong (ffmpeg's last line is usually just 'Conversion failed!')."""
    lines = [l.strip() for l in (stderr or "").splitlines() if l.strip()]
    for l in reversed(lines):
        if any(k in l for k in ("Error", "error", "Invalid", "Failed", "failed to", "No such", "not found", "Cannot")) \
                and "Conversion failed" not in l:
            return l[:300]
    return (lines[-1] if lines else "ffmpeg failed")[:300]


def run(args: list[str], timeout: int = 600) -> str:
    p = subprocess.run([ffmpeg_exe(), "-hide_banner", "-y", *_limited(args)], capture_output=True, text=True,
                       timeout=timeout)
    if p.returncode != 0:
        raise VideoToolError(_why(p.stderr))
    return p.stderr


def probe(path: str | Path) -> dict[str, float]:
    """{width, height, duration, fps} read from ffmpeg's own report (no ffprobe needed)."""
    p = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(path)], capture_output=True, text=True)
    err = p.stderr
    d = re.search(r"Duration: (\d+):(\d+):([\d.]+)", err)
    v = re.search(r"Video: .*?(\d{2,5})x(\d{2,5})", err)
    f = re.search(r"([\d.]+) fps", err)
    if not v:
        raise VideoToolError(f"not a video: {Path(path).name}")
    dur = int(d.group(1)) * 3600 + int(d.group(2)) * 60 + float(d.group(3)) if d else 0.0
    return {"width": int(v.group(1)), "height": int(v.group(2)), "duration": round(dur, 3),
            "fps": float(f.group(1)) if f else 0.0}


def export_spec(path: str | Path) -> dict:
    """The finished file against the playbook's export spec: MP4, H.264, AAC, 30 fps, at least 8 Mbps at 1080p
    (smaller test renders are held to the same bitrate per pixel)."""
    spec = _export_spec()
    p = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(path)], capture_output=True, text=True)
    err = p.stderr
    vline = next((ln for ln in err.splitlines() if "Video:" in ln), "")
    v = re.search(r"(\d{2,5})x(\d{2,5})", vline)
    kb = re.search(r"(\d+) kb/s", vline) or re.search(r"bitrate: (\d+) kb/s", err)
    fps = re.search(r"([\d.]+) fps", vline)
    w, h = (int(v.group(1)), int(v.group(2))) if v else (0, 0)
    mbps = round(int(kb.group(1)) / 1000, 2) if kb else 0.0
    need = round(spec["min_bitrate_mbps"] * min(1.0, (w * h) / (1080 * 1920 if h > w else 1080 * 1350)), 2) if w else 0
    out = {"container": Path(path).suffix.lower().lstrip(".") == spec["container"],
           "video_codec": "h264" in vline, "audio_codec": "Audio:" not in err or "Audio: aac" in err,     # silent clips have no audio
           "fps": bool(fps) and abs(float(fps.group(1)) - spec["fps"]) < 0.5,
           "bitrate": mbps >= need, "mbps": mbps, "min_mbps": need, "size": [w, h]}
    out["ok"] = all(out[k] for k in ("container", "video_codec", "audio_codec", "fps", "bitrate"))
    return out


def ensure_export(path: str | Path) -> dict:
    """Check a finished video; if only the bitrate is short (very still footage), re-encode it once at a
    constant bitrate above the minimum. Returns the final export_spec()."""
    spec = export_spec(path)
    if spec["ok"] or not all(spec[k] for k in ("container", "video_codec", "audio_codec", "fps")):
        return spec
    mbps = _export_spec()["min_bitrate_mbps"] * 1.5
    tmp = Path(path).with_name(Path(path).stem + ".cbr" + Path(path).suffix)
    run(["-i", str(path), "-map", "0:v", "-map", "0:a?", "-c:v", "libx264", "-preset",
         os.getenv("VIDEO_X264_PRESET", "medium"), "-b:v", f"{mbps:g}M", "-minrate", f"{mbps:g}M",
         "-maxrate", f"{mbps:g}M", "-bufsize", f"{mbps:g}M", "-x264-params", "nal-hrd=cbr", "-r",
         str(_export_spec()["fps"]), "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", str(tmp)])
    tmp.replace(path)
    return {**export_spec(path), "reencoded": True}


def probe_audio(path: str | Path) -> float:
    """Seconds of an audio (or any media) file, from ffmpeg's own report."""
    p = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(path)], capture_output=True, text=True)
    d = re.search(r"Duration: (\d+):(\d+):([\d.]+)", p.stderr)
    if not d:
        raise VideoToolError(f"can't read {Path(path).name}")
    return int(d.group(1)) * 3600 + int(d.group(2)) * 60 + float(d.group(3))


def sample_frames(path: str | Path, out_dir: str | Path, every_s: float = 1.0, width: int = 512) -> list[Path]:
    """One frame every `every_s` seconds, plus the very last frame. Returns the JPEGs in order."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    run(["-i", str(path), "-vf", f"fps=1/{every_s},scale={width}:-2", "-q:v", "3", str(out / "f%03d.jpg")])
    frames = sorted(out.glob("f*.jpg"))
    last = out / "last.jpg"
    run(["-sseof", "-0.1", "-i", str(path), "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "3", str(last)])
    if last.exists():
        frames.append(last)
    return frames


def contact_sheet(frames: list[Path], out: str | Path, cols: int = 4) -> Path:
    """Frames in order on one numbered sheet, so a reviewer sees the whole clip in one image."""
    from PIL import Image, ImageDraw, ImageFont
    ims = [Image.open(f).convert("RGB") for f in frames]
    w, h = ims[0].size
    rows = (len(ims) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * w, rows * h), (20, 20, 20))
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()
    for i, im in enumerate(ims):
        x, y = (i % cols) * w, (i // cols) * h
        sheet.paste(im.resize((w, h)), (x, y))
        draw.rectangle([x, y, x + 34, y + 18], fill=(0, 0, 0))
        draw.text((x + 5, y + 3), str(i + 1), fill=(255, 255, 255), font=font)
    sheet.save(out, "JPEG", quality=85)
    return Path(out)


def fit(path: str | Path, out: str | Path, aspect: str) -> Path:
    """The clip inside a `aspect` frame, never cropped: padded with a blurred copy of itself."""
    return fit_to(path, out, *SIZES[aspect])


def fit_to(path: str | Path, out: str | Path, W: int, H: int) -> Path:
    """The clip inside a W×H frame (e.g. half a split screen), never cropped: padded with a blurred copy of itself."""
    vf = (f"[0:v]split=2[a][b];"
          f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=30:2,eq=brightness=-0.12[bg];"
          f"[b]scale={W}:{H}:force_original_aspect_ratio=decrease[fg];"
          f"[bg][fg]overlay=(W-w)/2:(H-h)/2,format=yuv420p[v]")
    run(["-i", str(path), "-filter_complex", vf, "-map", "[v]", "-map", "0:a?", *_x264(),
         "-c:a", "aac", "-movflags", "+faststart", str(out)])
    return Path(out)


def window_for(box: list[float] | None, src_w: int, src_h: int, aspect: str, margin: float = 0.03) -> tuple[int, int] | None:
    """(top, height) of a full-width `aspect` window inside a taller clip that keeps the whole product
    box (plus a margin) in view; None when the product doesn't fit, so the caller pads instead."""
    W, H = SIZES[aspect]
    win = round(src_w * H / W)
    if not box or win >= src_h:
        return None
    y1, y2 = max(0.0, box[1] - margin) * src_h, min(1.0, box[3] + margin) * src_h
    if y2 - y1 > win:
        return None
    top = round(max(0, y2 - win))                       # as high as possible: room for a headline above
    return min(top, round(y1), src_h - win), win


def frame(path: str | Path, out: str | Path, aspect: str, box: list[float] | None) -> tuple[Path, dict]:
    """The clip as `aspect`: cropped to background only when the product box fits, else padded.
    Returns the file and how the frame maps to the source ({mode, top, height})."""
    info = probe(path)
    win = window_for(box, int(info["width"]), int(info["height"]), aspect)
    if win is None:
        W, H = SIZES[aspect]
        s = min(W / info["width"], H / info["height"])          # fit() scales down to fit inside, centred
        return fit(path, out, aspect), {"mode": "pad", "sx": info["width"] * s / W, "sy": info["height"] * s / H,
                                        "ox": (W - info["width"] * s) / 2 / W, "oy": (H - info["height"] * s) / 2 / H}
    top, h = win
    W, H = SIZES[aspect]
    run(["-i", str(path), "-vf", f"crop=iw:{h}:0:{top},scale={W}:{H},format=yuv420p", "-map", "0:v",
         "-map", "0:a?", *_x264(), "-c:a", "aac", "-movflags", "+faststart", str(out)])
    return Path(out), {"mode": "crop", "top": top / info["height"], "height": h / info["height"]}


def overlay(path: str | Path, layer_png: str | Path, out: str | Path) -> Path:
    """Lay a transparent text layer (same size as the video) over the whole clip."""
    run(["-i", str(path), "-i", str(layer_png), "-filter_complex", "[0:v][1:v]overlay=0:0,format=yuv420p[v]",
         "-map", "[v]", "-map", "0:a?", *_x264(final=True), "-c:a", "aac", "-movflags", "+faststart", str(out)])
    return Path(out)


def frame_at(path: str | Path, out: str | Path, at_s: float = 0.0, width: int | None = None) -> Path:
    vf = ["-vf", f"scale={width}:-2"] if width else []
    run(["-ss", str(at_s), "-i", str(path), "-frames:v", "1", *vf, "-q:v", "2", str(out)])
    return Path(out)


def test_clip(out: str | Path, size: str = "720x1280", seconds: float = 2.0, color: str = "0xd9708a") -> Path:
    """A small real clip for tests and local checks (no API): a coloured frame with slow motion."""
    run(["-f", "lavfi", "-i", f"color=c={color}:s={size}:d={seconds}:r=24", "-vf",
         "drawbox=x='mod(t*40,200)':y=200:w=160:h=220:color=0xf4a340@1:t=fill,noise=alls=25:allf=t,format=yuv420p",
         "-c:v", "libx264", "-preset", "ultrafast", str(out)])
    return Path(out)
