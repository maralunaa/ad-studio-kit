"""Tests for the shared-password login (and that the one-off restore route is gone)."""
from __future__ import annotations

import pytest

from creative_intelligence.webapp import app as webapp


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CI_DB_PATH", str(tmp_path / "data" / "creative_intel.db"))
    webapp.app.config["TESTING"] = True
    return webapp.app.test_client()


# ── login ────────────────────────────────────────────────────────────────
class TestLogin:
    def test_open_locally_without_password(self, client, monkeypatch):
        monkeypatch.delenv("ADSTUDIO_PASSWORD", raising=False)
        monkeypatch.delenv("RAILWAY_ENVIRONMENT", raising=False)
        assert client.get("/healthz").status_code == 200
        assert client.get("/api/adstudio/products").status_code == 200

    def test_locked_on_railway_without_password(self, client, monkeypatch):
        monkeypatch.delenv("ADSTUDIO_PASSWORD", raising=False)
        monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
        assert client.get("/api/adstudio/products").status_code == 503
        assert client.get("/adstudio").status_code == 503

    def test_requires_login_when_password_set(self, client, monkeypatch):
        monkeypatch.setenv("ADSTUDIO_PASSWORD", "s3cret")
        r = client.get("/adstudio")
        assert r.status_code == 302 and "/login" in r.headers["Location"]
        assert client.get("/api/adstudio/products").status_code == 401
        assert client.post("/api/adstudio/product/1/gen-images", json={}).status_code == 401
        assert client.get("/healthz").status_code == 200

    def test_wrong_password_rejected(self, client, monkeypatch):
        monkeypatch.setenv("ADSTUDIO_PASSWORD", "s3cret")
        monkeypatch.setattr("creative_intelligence.webapp.auth.time.sleep", lambda s: None)
        r = client.post("/login", data={"password": "nope"})
        assert r.status_code == 401
        assert client.get("/api/adstudio/products").status_code == 401

    def test_login_then_access_then_logout(self, client, monkeypatch):
        monkeypatch.setenv("ADSTUDIO_PASSWORD", "s3cret")
        r = client.post("/login", data={"password": "s3cret", "next": "/core"})
        assert r.status_code == 302 and r.headers["Location"].endswith("/core")
        assert client.get("/api/adstudio/products").status_code == 200
        client.get("/logout")
        assert client.get("/api/adstudio/products").status_code == 401

    def test_next_cannot_redirect_offsite(self, client, monkeypatch):
        monkeypatch.setenv("ADSTUDIO_PASSWORD", "s3cret")
        for bad in ("https://evil.example", "//evil.example", "javascript:alert(1)"):
            r = client.post("/login", data={"password": "s3cret", "next": bad})
            assert r.headers["Location"].endswith("/products")

    def test_home_goes_to_adstudio(self, client, monkeypatch):
        monkeypatch.setenv("ADSTUDIO_PASSWORD", "s3cret")
        client.post("/login", data={"password": "s3cret", "next": "/"})
        r = client.get("/")
        assert r.status_code == 302 and r.headers["Location"].endswith("/products")

    def test_admin_routes_keep_token_check(self, client, monkeypatch):
        monkeypatch.setenv("ADSTUDIO_PASSWORD", "s3cret")
        monkeypatch.setenv("ADMIN_TOKEN", "tok")
        r = client.post("/admin/meta-sync", headers={"X-Admin-Token": "wrong"})
        assert r.status_code == 401  # reached the handler, not the login redirect


def test_copy_route_is_off_unless_allowed(client, monkeypatch):
    monkeypatch.delenv("ALLOW_RESTORE", raising=False)
    assert client.post("/admin/restore-snapshot").status_code == 404
    monkeypatch.setenv("ALLOW_RESTORE", "1")
    assert client.post("/admin/restore-snapshot").status_code == 401        # and needs ADMIN_TOKEN
