"""
Shared-password login for the whole web app.

  ADSTUDIO_PASSWORD   the shared password. On Railway the app refuses every
                      request until it is set; locally (no RAILWAY_ENVIRONMENT)
                      an unset password leaves the app open, so dev and tests
                      work unchanged.
  FLASK_SECRET_KEY    signs the session cookie. Set it on Railway so logins
                      survive restarts; a random key is used if missing.

Open without login: /login, /logout, /healthz, /static/*, /brand.css and /brand/* (stylesheet, colours, logo),
and /admin/* (those keep their own ADMIN_TOKEN check). Pages redirect to /login; /api/* answers 401 JSON.
"""
from __future__ import annotations

import hmac
import os
import secrets
import time
from datetime import timedelta
from urllib.parse import urlparse

from flask import Flask, jsonify, redirect, render_template, request, session, url_for

OPEN_PATHS = {"/login", "/logout", "/healthz", "/brand.css"}
OPEN_PREFIXES = ("/admin/", "/static/", "/brand/")  # the stylesheet, brand colours and logo the login page needs


def _password() -> str:
    return os.getenv("ADSTUDIO_PASSWORD", "")


def _on_railway() -> bool:
    return bool(os.getenv("RAILWAY_ENVIRONMENT"))


def _safe_next(target: str | None) -> str:
    """Only allow same-site relative redirects after login."""
    if not target:
        return "/products"
    p = urlparse(target)
    if p.scheme or p.netloc or not target.startswith("/") or target.startswith("//"):
        return "/products"
    return target


def check_password(given: str) -> bool:
    expected = _password()
    return bool(expected) and hmac.compare_digest(given.encode(), expected.encode())


def register(app: Flask) -> None:
    app.secret_key = os.getenv("FLASK_SECRET_KEY") or secrets.token_hex(32)
    app.permanent_session_lifetime = timedelta(days=30)
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=_on_railway(),
    )

    @app.before_request
    def _require_login():
        path = request.path
        if path in OPEN_PATHS or path.startswith(OPEN_PREFIXES):
            return None
        if not _password():
            if _on_railway():
                return jsonify({"error": "ADSTUDIO_PASSWORD is not set — app locked"}), 503
            return None  # local dev / tests
        if session.get("auth"):
            return None
        if path.startswith("/api/"):
            return jsonify({"error": "login required"}), 401
        return redirect(url_for("login", next=request.full_path.rstrip("?")))

    @app.get("/")
    def home():
        return redirect("/products")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = ""
        nxt = _safe_next(request.values.get("next"))
        if request.method == "POST":
            if check_password(request.form.get("password", "")):
                session.clear()
                session.permanent = True
                session["auth"] = True
                return redirect(nxt)
            time.sleep(1)  # slow down guessing
            error = "Wrong password."
        return render_template("login.html", error=error, next=nxt), (401 if error else 200)

    @app.get("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))
