"""
Where Ad Studio keeps its files: the Railway volume, behind one small interface.

  storage()             the configured backend (only "volume" exists; object storage is deferred
                        by design until the volume or one service stops being enough)
  .area_dir(area)       folder for one kind of file: photos, images, ads, composites, core, textads, videos
  .put_bytes / .put_file / .put_url   store a file in an area; return its ref (today: absolute path)
  .local_path(ref)      a readable local path for a ref (PIL, send_file)
  .iter_files(areas)    every stored file, for the weekly image backup
  .usage()              bytes used / total / free on the volume, and per area

Refs are what the database stores (image_path, output_path…). An object-storage backend would
map them to keys and download on local_path(); nothing outside this module builds paths itself
except through area_dir().
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Iterator

AREAS = ("photos", "images", "ads", "composites", "core", "textads", "videos", "winners", "presenters", "research")
WARN_USED = 0.80   # Admin warns when the volume is 80% full


class VolumeStorage:
    """Files on a mounted disk (the Railway volume in production, a local folder otherwise)."""

    name = "volume"

    def __init__(self, root: Path):
        self.root = Path(root)

    def area_dir(self, area: str) -> Path:
        if area not in AREAS:
            raise ValueError(f"unknown storage area {area!r}")
        d = self.root / "adstudio" / area
        d.mkdir(parents=True, exist_ok=True)
        return d

    def put_bytes(self, area: str, name: str, data: bytes) -> str:
        dest = self.area_dir(area) / name
        tmp = dest.with_name(dest.name + ".part")
        tmp.write_bytes(data)
        os.replace(tmp, dest)            # never leave a half-written file under the real name
        return str(dest.resolve())

    def put_file(self, area: str, name: str, src: str | Path) -> str:
        dest = self.area_dir(area) / name
        shutil.copy2(Path(src), dest)
        return str(dest.resolve())

    def put_url(self, area: str, name: str, url: str, min_bytes: int = 5_000) -> str:
        """Store a generated file given as http(s) URL, file:// URI or local path."""
        if url.startswith(("http://", "https://")):
            from urllib.request import urlopen
            with urlopen(url, timeout=120) as r:  # noqa: S310 - model output URLs
                ref = self.put_bytes(area, name, r.read())
        elif url.startswith("file://"):
            from urllib.request import url2pathname
            ref = self.put_file(area, name, url2pathname(url[7:]))
        else:
            ref = self.put_file(area, name, url)
        if Path(ref).stat().st_size < min_bytes:
            raise RuntimeError(f"Saved file missing/too small: {ref}")
        return ref

    def local_path(self, ref: str) -> Path:
        return Path(ref)

    def iter_files(self, areas: tuple[str, ...] = AREAS) -> Iterator[tuple[str, Path]]:
        for area in areas:
            d = self.root / "adstudio" / area
            if d.exists():
                for p in sorted(d.iterdir()):
                    if p.is_file() and not p.name.endswith(".part"):
                        yield area, p

    def usage(self, detail: bool = True) -> dict:
        du = shutil.disk_usage(self.root)
        used = du.total - du.free
        out = {"backend": self.name, "root": str(self.root), "total_bytes": du.total, "used_bytes": used,
               "free_bytes": du.free, "used_share": round(used / du.total, 4) if du.total else 0,
               "warn": bool(du.total) and used / du.total >= WARN_USED, "measured_at": int(time.time())}
        if not detail:
            return out
        by_area = {}
        for area, p in self.iter_files():
            by_area[area] = by_area.get(area, 0) + p.stat().st_size
        db = sum(f.stat().st_size for f in self.root.glob("creative_intel.db*") if f.is_file())
        raw_dir = Path(os.getenv("CI_RAW_DATA_DIR", "") or self.root / "raw")
        raw = sum(f.stat().st_size for f in raw_dir.rglob("*") if f.is_file()) if raw_dir.exists() else 0
        return {**out, "by_area": {**by_area, "database": db, "meta_raw": raw}}


def data_root() -> Path:
    """The data folder: next to the database (the volume on Railway); honours CI_RENDER_OUTPUT_DIR."""
    from creative_intelligence import config
    configured = (config.CI_RENDER_OUTPUT_DIR or "").strip()
    if configured:
        return Path(configured).parent
    from creative_intelligence.db import get_db_path
    return get_db_path().parent


def storage() -> VolumeStorage:
    backend = os.getenv("STORAGE_BACKEND", "volume")
    if backend != "volume":
        raise RuntimeError(f"STORAGE_BACKEND={backend!r}: only 'volume' exists (object storage is deferred by design)")
    return VolumeStorage(data_root())
