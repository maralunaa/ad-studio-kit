"""The copy route: loads a data snapshot into this app. Off unless ALLOW_RESTORE=1, needs ADMIN_TOKEN."""
from __future__ import annotations

import io
import tarfile

import pytest

from creative_intelligence.webapp import app as webapp
from creative_intelligence.webapp.restore import extract_snapshot


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CI_DB_PATH", str(tmp_path / "data" / "creative_intel.db"))
    webapp.app.config["TESTING"] = True
    return webapp.app.test_client()


def _tarball(files: dict[str, bytes]) -> io.BytesIO:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    buf.seek(0)
    return buf


# ── restore ──────────────────────────────────────────────────────────────
class TestRestore:
    def test_disabled_by_default(self, client, monkeypatch):
        monkeypatch.delenv("ALLOW_RESTORE", raising=False)
        assert client.post("/admin/restore-snapshot").status_code == 404

    def test_needs_token(self, client, monkeypatch):
        monkeypatch.setenv("ALLOW_RESTORE", "1")
        monkeypatch.setenv("ADMIN_TOKEN", "tok")
        r = client.post("/admin/restore-snapshot", headers={"X-Admin-Token": "bad"})
        assert r.status_code == 401

    def test_restores_files(self, client, monkeypatch, tmp_path):
        monkeypatch.setenv("ALLOW_RESTORE", "1")
        monkeypatch.setenv("ADMIN_TOKEN", "tok")
        data = {"snapshot": (_tarball({"creative_intel.db": b"db", "adstudio/a.png": b"png"}),
                             "s.tar.gz")}
        r = client.post("/admin/restore-snapshot", headers={"X-Admin-Token": "tok"},
                        data=data, content_type="multipart/form-data")
        assert r.status_code == 200 and r.get_json()["files"] == 2
        assert (tmp_path / "data" / "adstudio" / "a.png").read_bytes() == b"png"

    def test_rejects_path_traversal(self, tmp_path):
        with pytest.raises(ValueError):
            extract_snapshot(_tarball({"../escape.txt": b"x"}), tmp_path / "d")
        assert not (tmp_path / "escape.txt").exists()

    def test_clears_stale_wal(self, tmp_path):
        d = tmp_path / "d"
        d.mkdir()
        (d / "creative_intel.db-wal").write_bytes(b"old")
        extract_snapshot(_tarball({"creative_intel.db": b"new"}), d)
        assert not (d / "creative_intel.db-wal").exists()
