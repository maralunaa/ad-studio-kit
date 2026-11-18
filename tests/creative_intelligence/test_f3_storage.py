"""F3 — files behind a storage interface on the volume, weekly image backup to Drive (only new
files), volume usage with an 80% alert, and raw Meta responses kept on the volume. No live API."""
from __future__ import annotations

from datetime import datetime

import pytest

from creative_intelligence import backup, storage as S
from creative_intelligence import db as D


@pytest.fixture
def live(tmp_path, monkeypatch):
    path = tmp_path / "data" / "creative_intel.db"
    monkeypatch.setenv("CI_DB_PATH", str(path))
    monkeypatch.delenv("CI_RENDER_OUTPUT_DIR", raising=False)
    monkeypatch.setattr("creative_intelligence.config.CI_RENDER_OUTPUT_DIR", "")
    D._UP_TO_DATE.clear()
    D.init_db(path)
    c = D.get_connection(path)
    with c:
        c.execute("INSERT INTO adstudio_drive_auth (id, email, refresh_token) VALUES (1, 'ops@example.com', 'r')")
    c.close()
    return path


def test_files_live_next_to_the_database_by_area(live):
    st = S.storage()
    ref = st.put_bytes("photos", "p1.png", b"x" * 10)
    assert ref == str((live.parent / "adstudio" / "photos" / "p1.png").resolve())
    assert st.local_path(ref).read_bytes() == b"x" * 10
    with pytest.raises(ValueError):
        st.area_dir("../etc")


def test_store_helpers_go_through_storage(live):
    from creative_intelligence.adstudio import store
    ref = store.save_reference_photo(b"png" * 3000, "a.JPG", 7)
    assert ref.startswith(str(live.parent / "adstudio" / "photos")) and ref.endswith(".jpg")
    assert store.images_dir() == live.parent / "adstudio" / "images"


def test_put_url_refuses_tiny_files(live, tmp_path):
    small = tmp_path / "s.png"
    small.write_bytes(b"x")
    with pytest.raises(RuntimeError):
        S.storage().put_url("images", "img.png", str(small))


def test_only_the_volume_backend_exists(monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "s3")
    with pytest.raises(RuntimeError, match="deferred by design"):
        S.storage()


def test_usage_reports_areas_and_warns_at_80(live, monkeypatch):
    S.storage().put_bytes("images", "a.png", b"x" * 2000)
    u = S.storage().usage()
    assert u["by_area"]["images"] == 2000 and u["by_area"]["database"] > 0
    import shutil
    from collections import namedtuple
    U = namedtuple("U", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda p: U(100, 85, 15))
    assert S.storage().usage(detail=False)["warn"] is True


def test_volume_alert_is_posted_once(live, monkeypatch):
    from creative_intelligence.ingest import scheduler
    import shutil
    from collections import namedtuple
    U = namedtuple("U", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda p: U(100, 90, 10))
    factory = lambda: D.get_connection(live)
    assert scheduler.storage_check(factory) is True
    assert scheduler.storage_check(factory) is False          # already showing on Admin
    msg = D.get_connection(live).execute("SELECT message FROM ops_alerts WHERE source='storage'").fetchone()[0]
    assert "90% full" in msg


@pytest.fixture
def fake_drive(monkeypatch):
    from creative_intelligence.adstudio import drive
    monkeypatch.setenv("BACKUP_DRIVE_FOLDER_ID", "backups")
    monkeypatch.setattr(drive, "access_token", lambda r: "tok")
    monkeypatch.setattr(drive, "find_folder", lambda t, name, parent: None)
    monkeypatch.setattr(drive, "create_folder", lambda t, name, parent: f"{parent}/{name}")


def test_image_backup_sends_only_new_files(live, fake_drive):
    st = S.storage()
    st.put_bytes("images", "a.png", b"a" * 100)
    st.put_bytes("composites", "c.png", b"c" * 100)
    sent = []
    up = lambda token, path, name, parent: (sent.append((parent, name)), {"id": "f-" + name})[1]
    factory = lambda: D.get_connection(live)
    r = backup.backup_images(factory, trigger="manual", upload=up)
    assert r["status"] == "done" and r["sent"] == 2
    assert sorted(sent) == [("backups/images/composites", "c.png"), ("backups/images/images", "a.png")]
    st.put_bytes("images", "b.png", b"b" * 100)
    sent.clear()
    r = backup.backup_images(factory, trigger="manual", upload=up)
    assert r["sent"] == 1 and r["already"] == 2 and sent == [("backups/images/images", "b.png")]


def test_image_backup_is_weekly(live, monkeypatch):
    monkeypatch.setenv("BACKUP_DRIVE_FOLDER_ID", "f")
    monkeypatch.setenv("BACKUP_IMAGES_WEEKDAY", "6")
    monkeypatch.setenv("BACKUP_HOUR_UTC", "3")
    c = D.get_connection(live)
    sunday = datetime(2026, 10, 18, 4, 0)
    assert backup.images_due(c, sunday) and not backup.images_due(c, datetime(2026, 10, 17, 4, 0))
    with c:
        c.execute("INSERT INTO ops_backups (kind, trigger, started_at) VALUES ('images', 'scheduled', '2026-10-18T04:00:00')")
    assert not backup.images_due(c, datetime(2026, 10, 18, 9, 0))
    assert backup.images_due(c, datetime(2026, 10, 25, 4, 0))


def test_raw_meta_responses_go_where_configured(tmp_path, monkeypatch):
    import importlib
    import sys
    sys.path.insert(0, "src")
    api = importlib.import_module("api")
    monkeypatch.setenv("CI_RAW_DATA_DIR", str(tmp_path / "vol" / "raw"))
    out = api.save_raw([{"ad_id": "1"}], "insights_daily_2026-10-16.json")
    assert out == tmp_path / "vol" / "raw" / "insights_daily_2026-10-16.json" and out.exists()
    from creative_intelligence.ingest import daily_insights
    assert daily_insights.latest_raw_file() == out
