"""
Retired pages and endpoints answer with a clear message instead of a bare 404.

Removed in the Ad Studio v2 cleanup: Test Lab, Copilot, Render, Collection,
the old Video page (code kept in creative_intelligence/legacy/; /video is now the Video Studio section), Candle Label Studio, the old
admin/maintenance endpoints and Ad Studio's unused v1 "ads" endpoints.
"""
from __future__ import annotations

from flask import Flask, jsonify, request

RETIRED = (
    "/test", "/copilot", "/render", "/collection", "/labels",
    "/api/products", "/api/generate", "/api/rate", "/api/flag", "/api/history",
    "/api/copilot", "/api/production", "/api/render", "/api/learnings", "/api/score",
    "/api/collection", "/api/video", "/api/labels", "/api/admin",
    "/admin/sync-products", "/admin/refresh-pipeline", "/admin/import-data",
    "/api/adstudio/prompt", "/api/adstudio/queue", "/api/adstudio/ad",
    "/api/adstudio/ads", "/api/adstudio/export",
)

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Page retired</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600;700&display=swap">
<link rel="stylesheet" href="/static/studio.css"><link rel="stylesheet" href="/brand.css"></head>
<body><main class="center-page"><div class="panel">
<img src="/brand/logo.png" alt="">
<h1>This page was retired</h1>
<p>It was part of an older version of the tool and is no longer available.</p>
<a class="btn btn-p" href="/products">Go to Ad Studio</a></div></main></body></html>"""


def is_retired(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in RETIRED)


def register(app: Flask) -> None:
    @app.errorhandler(404)
    def _not_found(exc):
        if is_retired(request.path):
            if request.path.startswith(("/api/", "/admin/")):
                return jsonify({"error": "This endpoint was retired."}), 410
            return PAGE, 410
        return exc
