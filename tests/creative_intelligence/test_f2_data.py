"""F2 — data safety: busy_timeout, numbered migrations with schema_version, nightly database
backup to Drive (keep 14), and a tested restore. Drive is faked; no live API."""
from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest

from creative_intelligence import backup
from creative_intelligence import db as D


@pytest.fixture
def live(tmp_path, monkeypatch):
    path = tmp_path / "data" / "creative_intel.db"
    monkeypatch.setenv("CI_DB_PATH", str(path))
    D._UP_TO_DATE.clear()
    D.init_db(path)
    c = D.get_connection(path)
    with c:
        c.execute("INSERT INTO adstudio_products (id, name, product_type) VALUES (1, 'Honey Mango', 'fruit')")
        c.execute("INSERT INTO adstudio_drive_auth (id, email, refresh_token) VALUES (1, 'ops@example.com', 'r')")
    c.close()
    return path


# ── SQLite settings and migrations ────────────────────────────────────────
def test_connections_wait_for_other_writers(live):
    c = D.get_connection(live)
    assert c.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_migrations_are_numbered_recorded_and_applied_once(live):
    c = D.get_connection(live)
    rows = c.execute("SELECT version, name FROM schema_version ORDER BY version").fetchall()
    assert [tuple(r) for r in rows][:3] == [(1, "baseline"), (2, "ops_alerts"), (3, "ops_backups")]
    assert D.migrate(c) == []                                     # nothing left to apply
    assert D.schema_version(c) == max(v for v, _, _ in D.migrations())


def test_new_migration_file_is_applied_in_order(live, tmp_path, monkeypatch):
    mig = tmp_path / "mig"
    mig.mkdir()
    for f in D.MIGRATIONS_DIR.glob("*.sql"):
        (mig / f.name).write_text(f.read_text())
    (mig / "0999_test_table.sql").write_text("CREATE TABLE IF NOT EXISTS zz_test (id INTEGER);")
    monkeypatch.setattr(D, "MIGRATIONS_DIR", mig)
    c = D.get_connection(live)
    assert D.migrate(c) == [999]
    assert c.execute("SELECT name FROM schema_version WHERE version=999").fetchone()[0] == "test_table"


def test_duplicate_migration_numbers_are_refused(tmp_path, monkeypatch):
    (tmp_path / "0002_a.sql").write_text("")
    (tmp_path / "0002_b.sql").write_text("")
    monkeypatch.setattr(D, "MIGRATIONS_DIR", tmp_path)
    with pytest.raises(RuntimeError):
        D.migrations()


# ── backup ────────────────────────────────────────────────────────────────
@pytest.fixture
def fake_drive(monkeypatch):
    from creative_intelligence.adstudio import drive
    up = []
    monkeypatch.setenv("BACKUP_DRIVE_FOLDER_ID", "folder-backups")
    monkeypatch.setattr(drive, "access_token", lambda r: "tok")
    monkeypatch.setattr(drive, "get_folder", lambda t, f: {"id": f})
    monkeypatch.setattr(backup, "prune", lambda t, p, keep=backup.KEEP: 2)
    return up


def test_backup_uploads_a_checked_snapshot_and_records_it(live, fake_drive, tmp_path):
    def upload(token, path, name, parent, mime):
        fake_drive.append((name, parent, mime, path.stat().st_size))
        assert path.name.endswith(".db.gz")
        return {"id": "drive-file-1"}
    res = backup.backup_database(lambda: D.get_connection(live), trigger="manual", upload=upload)
    assert res["status"] == "done" and res["drive_file_id"] == "drive-file-1"
    assert fake_drive[0][1:3] == ("folder-backups", "application/gzip")
    row = D.get_connection(live).execute("SELECT status, trigger, file_name, message FROM ops_backups").fetchone()
    assert row["status"] == "done" and row["trigger"] == "manual" and "2 old backup(s)" in row["message"]


def test_failed_backup_is_recorded_and_alerts(live, monkeypatch):
    monkeypatch.delenv("BACKUP_DRIVE_FOLDER_ID", raising=False)
    res = backup.backup_database(lambda: D.get_connection(live))
    c = D.get_connection(live)
    assert res["status"] == "failed" and "BACKUP_DRIVE_FOLDER_ID" in res["message"]
    assert "backup failed" in c.execute("SELECT message FROM ops_alerts WHERE source='backup'").fetchone()[0]


def test_due_once_a_day_after_the_hour(live, monkeypatch):
    monkeypatch.setenv("BACKUP_DRIVE_FOLDER_ID", "f")
    monkeypatch.setenv("BACKUP_HOUR_UTC", "3")
    c = D.get_connection(live)
    assert not backup.is_due(c, datetime(2026, 10, 16, 2, 0))
    assert backup.is_due(c, datetime(2026, 10, 16, 3, 5))
    with c:
        c.execute("INSERT INTO ops_backups (kind, trigger, started_at) VALUES ('database', 'scheduled', '2026-10-16T03:05:00')")
    assert not backup.is_due(c, datetime(2026, 10, 16, 23, 0))
    monkeypatch.delenv("BACKUP_DRIVE_FOLDER_ID")
    assert not backup.is_due(c, datetime(2026, 10, 17, 4, 0))          # no folder, no backup


def test_prune_keeps_the_newest_14(monkeypatch):
    from creative_intelligence.adstudio import drive
    files = [{"id": f"id{i}", "name": f"adstudio-db-2026-10-{i:02d}T0300Z.db.gz"} for i in range(1, 18)]
    trashed = []

    class R:
        def __init__(self, d): self.d, self.status_code = d, 200
        def json(self): return self.d
    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: R({"files": files}))
    monkeypatch.setattr(requests, "patch", lambda url, **k: (trashed.append(url.rsplit("/", 1)[1]), R({}))[1])
    monkeypatch.setattr(drive, "_check", lambda r: r.json())
    assert backup.prune("tok", "folder") == 3
    assert sorted(trashed) == ["id1", "id2", "id3"]                    # the three oldest go to the trash


# ── restore ───────────────────────────────────────────────────────────────
def test_restore_dry_run_reports_and_changes_nothing_then_restores(live, tmp_path):
    gz, counts = backup.snapshot(live, tmp_path)
    c = D.get_connection(live)
    with c:
        c.execute("INSERT INTO adstudio_products (name, product_type) VALUES ('Mistake', 'fruit')")
    c.close()
    dry = backup.restore(str(gz), live)
    assert dry["restored"] is False and dry["changes"]["adstudio_products"] == (2, 1)
    assert sqlite3.connect(live).execute("SELECT COUNT(*) FROM adstudio_products").fetchone()[0] == 2
    done = backup.restore(str(gz), live, dry_run=False)
    assert done["restored"] is True
    assert [r[0] for r in sqlite3.connect(live).execute("SELECT name FROM adstudio_products")] == ["Honey Mango"]


def test_restore_refuses_a_damaged_backup(live, tmp_path):
    import gzip
    bad = tmp_path / "bad.db.gz"
    with gzip.open(bad, "wb") as g:
        g.write(b"not a database" * 100)
    with pytest.raises(Exception):
        backup.restore(str(bad), live, dry_run=False)
    assert sqlite3.connect(live).execute("SELECT COUNT(*) FROM adstudio_products").fetchone()[0] == 1


def test_cli_defaults_to_dry_run(live, capsys, monkeypatch):
    monkeypatch.setenv("BACKUP_DRIVE_FOLDER_ID", "f")
    assert backup.main(["now"]) == 0
    assert "[dry run] would back up" in capsys.readouterr().out
