"""
The copy route: load a .tar.gz snapshot of the data folder (DB + images) into this
app's data folder. Used to seed a copy of the app with existing work
Ask the owner before each use.

Locked twice: only exists while ALLOW_RESTORE=1, and needs the ADMIN_TOKEN in the
X-Admin-Token header. Turn ALLOW_RESTORE off and restart the app afterwards.

  curl -X POST -H "X-Admin-Token: $ADMIN_TOKEN" -F snapshot=@data.tar.gz \
       https://<host>/admin/restore-snapshot
"""
from __future__ import annotations

import hmac
import os
import tarfile
from pathlib import Path

from flask import Flask, jsonify, request


def _data_dir() -> Path:
    from creative_intelligence.db import get_db_path
    return get_db_path().parent


def _safe_members(tar: tarfile.TarFile, dest: Path):
    root = dest.resolve()
    for m in tar.getmembers():
        if not (m.isfile() or m.isdir()):
            raise ValueError(f"refusing non-file entry: {m.name}")
        target = (root / m.name).resolve()
        if target != root and root not in target.parents:
            raise ValueError(f"refusing path outside data folder: {m.name}")
        yield m


def extract_snapshot(fileobj, dest: Path) -> int:
    """Extract a gzip tarball into dest. Returns the number of files written."""
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=fileobj, mode="r:gz") as tar:
        members = list(_safe_members(tar, dest))
        db = dest / "creative_intel.db"
        if any(m.name.lstrip("./") == db.name for m in members):
            for suffix in ("-wal", "-shm"):  # stale WAL would corrupt the restored DB
                Path(str(db) + suffix).unlink(missing_ok=True)
        tar.extractall(dest, members=members)
    return sum(1 for m in members if m.isfile())


def register(app: Flask) -> None:
    @app.post("/admin/restore-snapshot")
    def restore_snapshot():
        if os.getenv("ALLOW_RESTORE") != "1":
            return jsonify({"error": "not found"}), 404
        token = os.getenv("ADMIN_TOKEN", "")
        given = request.headers.get("X-Admin-Token", "")
        if not token or not hmac.compare_digest(given.encode(), token.encode()):
            return jsonify({"error": "unauthorized"}), 401
        f = request.files.get("snapshot")
        if f is None:
            return jsonify({"error": "upload a .tar.gz as 'snapshot'"}), 400
        try:
            n = extract_snapshot(f.stream, _data_dir())
        except (ValueError, tarfile.TarError) as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"files": n, "into": str(_data_dir()),
                        "next": "restart the app, then set ALLOW_RESTORE=0"})
