"""
Add licensed music tracks to music/ for Video Studio: each file is converted to AAC (.m4a), cut to its first 90 s
(with a fade-out), levelled to -16 LUFS so every track sits the same under the voice, and listed in music/tracks.json with its source and
where it may be used. Run locally, then commit music/.

    .venv/bin/python scripts/add_music.py --source "Meta Sound Collection" \
        --use "Facebook and Instagram only" ~/path/to/*.mp4

A title with "(Instrumental)" or "(Loop)" is marked vocals: false; "feat." marks vocals: true; anything
else is left unknown (null) until someone listens. Only instrumentals are picked by default.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from creative_intelligence.adstudio import videotools as V  # noqa: E402

MUSIC = Path(__file__).resolve().parent.parent / "music"


def slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


def vocals(title: str) -> bool | None:
    t = title.lower()
    if "(instrumental)" in t or "(loop)" in t:
        return False
    if "feat." in t:
        return True
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--source", required=True)
    ap.add_argument("--use", required=True, help="where the licence allows use, e.g. 'Facebook and Instagram only'")
    ap.add_argument("--max-seconds", type=float, default=90, help="keep the first N s (videos are under a minute)")
    ap.add_argument("--bitrate", default="64k", help="AAC bitrate; library downloads are ~48 kbps, more adds only size")
    a = ap.parse_args()
    MUSIC.mkdir(exist_ok=True)
    man = MUSIC / "tracks.json"
    data = json.loads(man.read_text()) if man.exists() else {"tracks": []}
    by_file = {t["file"]: t for t in data["tracks"]}
    for f in a.files:
        src = Path(f)
        title = src.stem
        out = MUSIC / f"{slug(title)}.m4a"
        full = V.probe_audio(src)
        keep = min(full, a.max_seconds)
        fade = f",afade=t=out:st={keep - 2:.2f}:d=2" if full > keep else ""
        V.run(["-i", str(src), "-vn", "-t", f"{keep:.2f}", "-af", f"loudnorm=I=-16:TP=-1.5:LRA=11{fade}", "-ar", "44100",
               "-ac", "2", "-c:a", "aac", "-b:a", a.bitrate, str(out)])
        secs = V.probe_audio(out)
        by_file[out.name] = {"file": out.name, "title": title, "source": a.source, "use": a.use,
                             "vocals": vocals(title), "seconds": round(secs, 1), "added": date.today().isoformat()}
        print(f"{out.name}: {secs:.0f} s, vocals={vocals(title)}")
    data["tracks"] = sorted(by_file.values(), key=lambda t: t["title"].lower())
    man.write_text(json.dumps(data, indent=1) + "\n")


if __name__ == "__main__":
    main()
