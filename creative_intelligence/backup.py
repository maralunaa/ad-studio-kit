"""
Backups of Ad Studio's database to Google Drive.

  Nightly   the scheduler (one worker, see ingest/scheduler.py) runs backup_database() once a day
            after BACKUP_HOUR_UTC: an online SQLite backup (safe while the app writes), checked with
            PRAGMA integrity_check, gzipped, uploaded to the Drive folder BACKUP_DRIVE_FOLDER_ID
            ("Backups") with the account connected for Drive export. The newest KEEP files stay;
            older ones go to the Drive trash. Every attempt is a row in ops_backups; a failure
            also posts an Admin alert.
  Restore   python -m creative_intelligence.backup restore <file.db.gz | Drive file id>
            Checks the backup first. --dry-run is on by default: it reports what would change.
            With --no-dry-run it copies the backup into the live database with SQLite's backup
            API (one locked copy, no half-written state). Run it inside the app's container
            (railway ssh) and ask before each use.

  Weekly    backup_images() copies every stored image (storage.iter_files) that Drive doesn't
            have yet into Backups/images/<area>/, once a week (BACKUP_IMAGES_WEEKDAY, 0 = Monday,
            default 6 = Sunday) after BACKUP_HOUR_UTC. Files already sent are listed in
            ops_backup_files, so each run only uploads what's new. Nothing is deleted from Drive.

Railway volume backups (daily, weekly) are on as well; they cover images too.
"""
from __future__ import annotations

import argparse
import gzip
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

KEEP = 14
PREFIX = "adstudio-db-"


def folder_id() -> str:
    return os.getenv("BACKUP_DRIVE_FOLDER_ID", "").strip()


def backup_hour() -> int:
    try:
        return max(0, min(23, int(os.getenv("BACKUP_HOUR_UTC", "3"))))
    except ValueError:
        return 3


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    return {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in sorted(tables)}


def snapshot(db_path: Path, out_dir: Path) -> tuple[Path, dict[str, int]]:
    """Online backup of the live DB into out_dir as a checked .db.gz. Returns (path, row counts)."""
    stamp = datetime.utcnow().strftime("%Y-%m-%dT%H%MZ")
    raw = out_dir / f"{PREFIX}{stamp}.db"
    src, dst = sqlite3.connect(str(db_path), timeout=30), sqlite3.connect(str(raw))
    try:
        src.backup(dst)
        ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
        if ok != "ok":
            raise RuntimeError(f"backup failed its integrity check: {ok}")
        n = counts(dst)
    finally:
        src.close()
        dst.close()
    gz = raw.with_suffix(".db.gz")
    with open(raw, "rb") as f, gzip.open(gz, "wb", compresslevel=6) as g:
        shutil.copyfileobj(f, g)
    raw.unlink()
    return gz, n


def _drive_token(conn) -> str:
    from creative_intelligence.adstudio import drive
    row = conn.execute("SELECT refresh_token FROM adstudio_drive_auth ORDER BY rowid DESC LIMIT 1").fetchone()
    if not row:
        raise RuntimeError("Google Drive isn't connected (Gallery → Connect Google Drive)")
    return drive.access_token(row[0])


def prune(token: str, parent: str, keep: int = KEEP) -> int:
    """Move all but the newest `keep` backups in the folder to the Drive trash. Returns how many."""
    import requests
    from creative_intelligence.adstudio import drive
    r = requests.get(f"{drive.API}/files", headers=drive._h(token), timeout=30, params={
        "q": f"'{parent}' in parents and name contains '{PREFIX}' and trashed = false",
        "fields": "files(id,name)", "pageSize": "1000", "orderBy": "name desc",
        "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"})
    old = sorted(drive._check(r).get("files", []), key=lambda f: f["name"], reverse=True)[keep:]
    for f in old:
        drive._check(requests.patch(f"{drive.API}/files/{f['id']}", headers=drive._h(token), timeout=30,
                                    params={"supportsAllDrives": "true"}, json={"trashed": True}))
    return len(old)


def backup_database(conn_factory: Callable[[], Any], trigger: str = "scheduled",
                    upload: Callable[..., dict] | None = None) -> dict[str, Any]:
    """Snapshot → Drive → prune, recorded in ops_backups. Never raises; failures post an Admin alert."""
    from creative_intelligence.adstudio import drive
    from creative_intelligence.db import get_db_path
    conn = conn_factory()
    started = datetime.utcnow().isoformat(timespec="seconds")
    with conn:
        bid = conn.execute("INSERT INTO ops_backups (kind, trigger, started_at) VALUES ('database', ?, ?)",
                           (trigger, started)).lastrowid
    result: dict[str, Any] = {"id": bid, "status": "failed"}
    try:
        if not folder_id():
            raise RuntimeError("BACKUP_DRIVE_FOLDER_ID is not set")
        with tempfile.TemporaryDirectory() as tmp:
            path, n = snapshot(get_db_path(), Path(tmp))
            token = _drive_token(conn)
            if not drive.get_folder(token, folder_id()):
                raise RuntimeError("the Backups folder (BACKUP_DRIVE_FOLDER_ID) is missing or in the trash")
            f = (upload or drive.upload_resumable)(token, path, path.name, folder_id(), "application/gzip")
            size = path.stat().st_size
        pruned = prune(token, folder_id())
        result.update(status="done", file_name=path.name, size_bytes=size, drive_file_id=f.get("id"),
                      message=f"{sum(n.values())} rows in {len(n)} tables; {pruned} old backup(s) moved to trash")
    except Exception as exc:
        result["message"] = f"{type(exc).__name__}: {exc}"[:500]
    with conn:
        conn.execute("UPDATE ops_backups SET status=?, finished_at=?, file_name=?, size_bytes=?, drive_file_id=?,"
                     " message=? WHERE id=?", (result["status"], datetime.utcnow().isoformat(timespec="seconds"),
                                                result.get("file_name"), result.get("size_bytes"),
                                                result.get("drive_file_id"), result.get("message"), bid))
    if result["status"] == "failed":
        from creative_intelligence.alerts import raise_alert
        raise_alert(conn, "backup", f"Nightly database backup failed: {result['message']}")
    conn.close()
    return result


def is_due(conn, now: datetime | None = None) -> bool:
    """One scheduled attempt per day, after BACKUP_HOUR_UTC, only when a Backups folder is set."""
    now = now or datetime.utcnow()
    if not folder_id() or now.hour < backup_hour():
        return False
    return not conn.execute("SELECT 1 FROM ops_backups WHERE kind='database' AND trigger='scheduled'"
                            " AND started_at >= ? LIMIT 1", (now.strftime("%Y-%m-%d"),)).fetchone()


# ── weekly image backup ─────────────────────────────────────────────────────
def images_weekday() -> int:
    try:
        return max(0, min(6, int(os.getenv("BACKUP_IMAGES_WEEKDAY", "6"))))
    except ValueError:
        return 6


def images_due(conn, now: datetime | None = None) -> bool:
    from datetime import timedelta
    now = now or datetime.utcnow()
    if not folder_id() or now.weekday() != images_weekday() or now.hour < backup_hour():
        return False
    return not conn.execute("SELECT 1 FROM ops_backups WHERE kind='images' AND trigger='scheduled'"
                            " AND started_at >= ? LIMIT 1", ((now - timedelta(days=6)).isoformat(),)).fetchone()


def _folder(token: str, name: str, parent: str, cache: dict) -> str:
    from creative_intelligence.adstudio import drive
    key = (parent, name)
    if key not in cache:
        cache[key] = drive.find_folder(token, name, parent) or drive.create_folder(token, name, parent)
    return cache[key]


def backup_images(conn_factory: Callable[[], Any], trigger: str = "scheduled",
                  upload: Callable[..., dict] | None = None) -> dict[str, Any]:
    """Copy new image files to Drive (Backups/images/<area>/). Never raises; failures alert."""
    from creative_intelligence.adstudio import drive
    from creative_intelligence.storage import storage
    conn = conn_factory()
    started = datetime.utcnow().isoformat(timespec="seconds")
    with conn:
        bid = conn.execute("INSERT INTO ops_backups (kind, trigger, started_at) VALUES ('images', ?, ?)",
                           (trigger, started)).lastrowid
    sent = sent_bytes = skipped = 0
    result: dict[str, Any] = {"id": bid, "status": "failed"}
    try:
        if not folder_id():
            raise RuntimeError("BACKUP_DRIVE_FOLDER_ID is not set")
        token = _drive_token(conn)
        done = {r[0]: r[1] for r in conn.execute("SELECT key, size_bytes FROM ops_backup_files")}
        cache: dict = {}
        for area, path in storage().iter_files():
            key, size = f"{area}/{path.name}", path.stat().st_size
            if done.get(key) == size:
                skipped += 1
                continue
            parent = _folder(token, area, _folder(token, "images", folder_id(), cache), cache)
            if upload:
                f = upload(token, path, path.name, parent)
            elif size < 4 * 1024 * 1024:
                f = drive.upload(token, path.name, parent, path.read_bytes(), "application/octet-stream")
            else:
                f = drive.upload_resumable(token, path, path.name, parent, "application/octet-stream")
            with conn:
                conn.execute("INSERT OR REPLACE INTO ops_backup_files (key, size_bytes, drive_file_id, at) VALUES (?,?,?,?)",
                             (key, size, f.get("id", ""), datetime.utcnow().isoformat(timespec="seconds")))
            sent, sent_bytes = sent + 1, sent_bytes + size
        result.update(status="done")
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"[:400]
    result.update(sent=sent, sent_bytes=sent_bytes, already=skipped)
    msg = f"{sent} new file(s) sent ({sent_bytes / 1048576:.1f} MB), {skipped} already in Drive" + (
        f"; stopped: {result['error']}" if result.get("error") else "")
    with conn:
        conn.execute("UPDATE ops_backups SET status=?, finished_at=?, size_bytes=?, message=? WHERE id=?",
                     (result["status"], datetime.utcnow().isoformat(timespec="seconds"), sent_bytes, msg, bid))
    if result["status"] == "failed":
        from creative_intelligence.alerts import raise_alert
        raise_alert(conn, "backup", f"Weekly image backup failed: {msg}")
    conn.close()
    result["message"] = msg
    return result


# ── restore ─────────────────────────────────────────────────────────────────
def _fetch(source: str, conn_factory: Callable[[], Any], tmp: Path) -> Path:
    if Path(source).exists():
        return Path(source)
    import requests
    from creative_intelligence.adstudio import drive
    conn = conn_factory()
    token = _drive_token(conn)
    conn.close()
    r = requests.get(f"{drive.API}/files/{source}", headers=drive._h(token), timeout=300,
                     params={"alt": "media", "supportsAllDrives": "true"})
    if r.status_code != 200:
        raise RuntimeError(f"could not download Drive file {source}: {r.status_code}")
    p = tmp / "download.db.gz"
    p.write_bytes(r.content)
    return p


def restore(source: str, db_path: Path, dry_run: bool = True,
            conn_factory: Callable[[], Any] | None = None) -> dict[str, Any]:
    """Check a backup and (unless dry_run) copy it over the live database."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        gz = _fetch(source, conn_factory or (lambda: sqlite3.connect(str(db_path))), tmp)
        raw = tmp / "restore.db"
        with gzip.open(gz, "rb") as g, open(raw, "wb") as f:
            shutil.copyfileobj(g, f)
        src = sqlite3.connect(str(raw))
        ok = src.execute("PRAGMA integrity_check").fetchone()[0]
        if ok != "ok":
            src.close()
            raise RuntimeError(f"backup is damaged: {ok}")
        new = counts(src)
        live = sqlite3.connect(str(db_path), timeout=30)
        old = counts(live) if db_path.exists() else {}
        changes = {t: (old.get(t), new.get(t)) for t in sorted(set(old) | set(new)) if old.get(t) != new.get(t)}
        if not dry_run:
            src.backup(live)
        src.close()
        live.close()
    return {"restored": not dry_run, "tables": len(new), "rows": sum(new.values()), "changes": changes}


def main(argv: list[str] | None = None) -> int:
    from creative_intelligence.db import get_connection, get_db_path, init_db
    ap = argparse.ArgumentParser(prog="python -m creative_intelligence.backup")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("now", help="back up the database to Drive now")
    im = sub.add_parser("images", help="copy new image files to Drive now")
    r = sub.add_parser("restore", help="restore a backup (.db.gz path or Drive file id)")
    r.add_argument("source")
    for p in (b, im, r):
        p.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=True,
                       help="on by default: report only; --no-dry-run does it")
    a = ap.parse_args(argv)
    init_db()
    if a.cmd == "images":
        if a.dry_run:
            from creative_intelligence.storage import storage
            n = sum(1 for _ in storage().iter_files())
            print(f"[dry run] would copy new files among {n} stored images to Drive folder {folder_id() or '(not set)'}/images")
            return 0
        res = backup_images(get_connection, trigger="manual")
        print(res)
        return 0 if res["status"] == "done" else 1
    if a.cmd == "now":
        if a.dry_run:
            print(f"[dry run] would back up {get_db_path()} to Drive folder {folder_id() or '(BACKUP_DRIVE_FOLDER_ID not set)'}"
                  f" and keep the newest {KEEP}")
            return 0
        res = backup_database(get_connection, trigger="manual")
        print(res)
        return 0 if res["status"] == "done" else 1
    res = restore(a.source, get_db_path(), dry_run=a.dry_run, conn_factory=get_connection)
    print(("[dry run] would restore: " if a.dry_run else "restored: ") + f"{res['tables']} tables, {res['rows']} rows")
    for t, (o, n) in res["changes"].items():
        print(f"  {t}: {o} -> {n}")
    if not a.dry_run:
        print("Restart the app so every worker reopens the database.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
