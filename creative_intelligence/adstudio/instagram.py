"""
Our own Instagram posts, read only (feedback 2026-10-27: "add a section for our own organic content, pulled from IG").

  sync()    the account's latest posts via the Instagram Graph API (through the Facebook Page the account is linked
            to): caption, type, link, date, likes, comments, and reach / saves / shares where Instagram gives them;
            each picture (a video's cover) is cached on the volume, since Instagram's links expire
  status()  the last sync and, when the token can't read Instagram yet, what to do about it

Needs a token with instagram_basic, instagram_manage_insights, pages_show_list and pages_read_engagement, and an
Instagram business account linked to the Page. Every call is a GET, logged to outputs/api.log; raw responses are
kept in data/raw (timestamped, never overwritten).
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

POSTS = 60
FIELDS = "id,caption,media_type,media_url,thumbnail_url,permalink,timestamp,like_count,comments_count"
NOT_CONNECTED = ("Instagram isn't connected yet: the Meta token needs instagram_basic and instagram_manage_insights "
                 "(plus pages_show_list and pages_read_engagement), and the Instagram account must be a business "
                 "account linked to the Facebook Page. Then click Refresh from Instagram.")


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def _get(path: str, params: dict[str, Any], http: Any = None) -> dict[str, Any]:
    import requests
    from creative_intelligence.adstudio.winners import _log
    http = http or requests
    url = f"https://graph.facebook.com/{os.getenv('META_API_VERSION', 'v26.0')}/{path}"
    _log(url)
    r = http.get(url, params={**params, "access_token": os.getenv("META_ACCESS_TOKEN", "")}, timeout=60)
    return r.json()


def _cache_dir() -> Path:
    from creative_intelligence.storage import storage
    d = Path(storage().area_dir("research")) / "instagram"
    d.mkdir(parents=True, exist_ok=True)
    return d


def account(http: Any = None) -> tuple[str | None, str]:
    """(Instagram business account id, why not) from the Pages the token can see."""
    d = _get("me/accounts", {"fields": "id,name,instagram_business_account{id,username}"}, http)
    if "error" in d:
        return None, NOT_CONNECTED + f" (Meta said: {d['error'].get('message', '')[:160]})"
    for p in d.get("data") or []:
        ig = p.get("instagram_business_account") or {}
        if ig.get("id"):
            return ig["id"], ""
    return None, NOT_CONNECTED


def _insights(media_id: str, kind: str, http: Any = None) -> dict[str, int]:
    metric = "reach,saved,shares" if kind != "VIDEO" else "reach,saved,shares,views"
    d = _get(f"{media_id}/insights", {"metric": metric}, http)
    out: dict[str, int] = {}
    for m in d.get("data") or []:
        v = (m.get("values") or [{}])[0].get("value", m.get("total_value", {}).get("value"))
        if isinstance(v, int):
            out[m.get("name", "")] = v
    return out


def _picture(post: dict[str, Any], http: Any = None) -> str:
    import requests
    url = post.get("thumbnail_url") if post.get("media_type") == "VIDEO" else post.get("media_url")
    if not url:
        return ""
    p = _cache_dir() / f"{post['id']}.jpg"
    if not p.exists():
        r = (http or requests).get(url, timeout=30)          # our own post's picture on Instagram's CDN (read only)
        if getattr(r, "status_code", 0) != 200 or not r.content:
            return ""
        p.write_bytes(r.content)
    return str(p)


def sync(conn, http: Any = None) -> dict[str, Any]:
    from creative_intelligence.adstudio.winners import save_raw
    with conn:
        run = conn.execute("INSERT INTO ig_sync_runs (started_at) VALUES (?)", (_now(),)).lastrowid
    try:
        ig, why = account(http)
        if ig is None:
            raise RuntimeError(why)
        d = _get(f"{ig}/media", {"fields": FIELDS, "limit": POSTS}, http)
        if "error" in d:
            raise RuntimeError(NOT_CONNECTED + f" (Meta said: {d['error'].get('message', '')[:160]})")
        save_raw("instagram_media", d)
        added = 0
        for post in d.get("data") or []:
            ins = _insights(post["id"], post.get("media_type", ""), http)
            new = conn.execute("SELECT 1 FROM ig_posts WHERE id=?", (post["id"],)).fetchone() is None
            with conn:
                conn.execute(
                    "INSERT INTO ig_posts (id, caption, media_type, permalink, posted_at, likes, comments, reach, saves,"
                    " shares, media_path, synced_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET"
                    " caption=excluded.caption, likes=excluded.likes, comments=excluded.comments, reach=excluded.reach,"
                    " saves=excluded.saves, shares=excluded.shares, synced_at=excluded.synced_at,"
                    " media_path=CASE WHEN excluded.media_path<>'' THEN excluded.media_path ELSE ig_posts.media_path END",
                    (post["id"], post.get("caption") or "", post.get("media_type") or "", post.get("permalink") or "",
                     post.get("timestamp") or "", int(post.get("like_count") or 0), int(post.get("comments_count") or 0),
                     ins.get("reach"), ins.get("saved"), ins.get("shares"), _picture(post, http), _now()))
            added += new
        with conn:
            conn.execute("UPDATE ig_sync_runs SET status='done', finished_at=?, added=?, message=? WHERE id=?",
                         (_now(), added, f"{len(d.get('data') or [])} posts read, {added} new", run))
        return {"posts": len(d.get("data") or []), "added": added}
    except Exception as exc:
        with conn:
            conn.execute("UPDATE ig_sync_runs SET status='failed', finished_at=?, message=? WHERE id=?",
                         (_now(), str(exc)[:600], run))
        raise


def status(conn) -> dict[str, Any]:
    r = conn.execute("SELECT * FROM ig_sync_runs ORDER BY id DESC LIMIT 1").fetchone()
    n = conn.execute("SELECT COUNT(*) FROM ig_posts").fetchone()[0]
    return {"posts": n, "last": dict(r) if r else None}


def due(conn) -> bool:
    r = conn.execute("SELECT started_at FROM ig_sync_runs ORDER BY id DESC LIMIT 1").fetchone()
    return r is None or datetime.fromisoformat(r[0]) < datetime.utcnow() - timedelta(hours=23)
