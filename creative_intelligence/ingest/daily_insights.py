"""
Per-ad daily Meta results → ad_daily_insights (one row per ad per day).

Reads the newest data/raw/insights_daily_*.json (from
`src/fetch_insights.py --recent-daily N`) and upserts it, so re-running a sync
updates the same rows instead of adding duplicates. Meta keeps revising recent
conversions, which is why each sync re-fetches the last few days.

Hook rate = 3-second video views / impressions. Hold rate = 15-second views / 3-second
views (the playbook's definition) when Meta returns 15-second views, else thruplays / 3-second
views. Both are computed on read, never stored.

  python -m creative_intelligence.ingest.daily_insights   # ingest newest raw file
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from creative_intelligence.ingest.insights_reader import _parse_action_value

def _raw_dir() -> Path:
    from creative_intelligence import config
    return Path(os.getenv("CI_RAW_DATA_DIR", "").strip() or config.RAW_DATA_DIR)
PURCHASE = "omni_purchase"
COLUMNS = ("ad_id", "date", "campaign_id", "adset_id", "ad_name", "spend", "impressions",
           "clicks", "ctr", "purchases", "revenue", "cpa", "roas", "video_3s_views",
           "thruplays", "video_p25", "video_p50", "video_p75", "video_p100", "video_15s_views")


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _first_value(items: list[dict] | None) -> int | None:
    """Video fields arrive as [{"action_type": "video_view", "value": "123"}]."""
    for it in items or []:
        n = _num(it.get("value"))
        if n is not None:
            return int(n)
    return None


def _revenue(row: dict) -> float | None:
    for rv in row.get("action_values") or []:
        if rv.get("action_type") == PURCHASE:
            return _num(rv.get("value"))
    return None


def parse_row(row: dict) -> dict[str, Any] | None:
    ad_id, day = row.get("ad_id"), row.get("date_start")
    if not ad_id or not day:
        return None
    spend = _num(row.get("spend")) or 0.0
    impressions = int(_num(row.get("impressions")) or 0)
    clicks = int(_num(row.get("clicks")) or 0)
    purchases = _parse_action_value(row.get("actions"), PURCHASE) or 0
    revenue = _revenue(row) or 0.0
    ctr = _num(row.get("ctr"))
    if ctr is None and impressions:
        ctr = clicks / impressions * 100
    return {
        "ad_id": str(ad_id), "date": day,
        "campaign_id": row.get("campaign_id"), "adset_id": row.get("adset_id"),
        "ad_name": row.get("ad_name"),
        "spend": spend, "impressions": impressions, "clicks": clicks, "ctr": ctr,
        "purchases": purchases, "revenue": revenue,
        "cpa": spend / purchases if purchases else None,
        "roas": revenue / spend if spend else None,
        "video_3s_views": _parse_action_value(row.get("actions"), "video_view"),
        "thruplays": _first_value(row.get("video_thruplay_watched_actions")),
        "video_p25": _first_value(row.get("video_p25_watched_actions")),
        "video_p50": _first_value(row.get("video_p50_watched_actions")),
        "video_p75": _first_value(row.get("video_p75_watched_actions")),
        "video_p100": _first_value(row.get("video_p100_watched_actions")),
        "video_15s_views": _first_value(row.get("video_15_sec_watched_actions")),
    }


def upsert(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """Insert or update by (ad_id, date). Returns rows written."""
    parsed = [p for p in (parse_row(r) for r in rows) if p]
    if not parsed:
        return 0
    cols = ", ".join(COLUMNS)
    ph = ", ".join("?" for _ in COLUMNS)
    updates = ", ".join(f"{c}=excluded.{c}" for c in COLUMNS[2:])
    now = datetime.utcnow().isoformat()
    with conn:
        conn.executemany(
            f"INSERT INTO ad_daily_insights ({cols}, updated_at) VALUES ({ph}, ?)"
            f" ON CONFLICT(ad_id, date) DO UPDATE SET {updates}, updated_at=excluded.updated_at",
            [[p[c] for c in COLUMNS] + [now] for p in parsed])
    return len(parsed)


def latest_raw_file(raw_dir: Path | None = None) -> Path | None:
    raw_dir = raw_dir or _raw_dir()
    files = sorted(raw_dir.glob("insights_daily_*.json"))
    return files[-1] if files else None


def ingest_latest(conn: sqlite3.Connection, raw_dir: Path | None = None) -> int:
    raw_dir = raw_dir or _raw_dir()
    path = latest_raw_file(raw_dir)
    if path is None:
        return 0
    return upsert(conn, json.loads(path.read_text()))


def rates(row: dict) -> dict[str, float | None]:
    """Hook rate and hold rate (as %) for one ad's summed results."""
    imp, v3, tp = row.get("impressions"), row.get("video_3s_views"), row.get("thruplays")
    return {
        "hook_rate": round(v3 / imp * 100, 2) if v3 and imp else None,
        "hold_rate": round(tp / v3 * 100, 2) if tp and v3 else None,
    }


if __name__ == "__main__":
    from creative_intelligence.db import get_connection, init_db
    init_db()
    c = get_connection()
    n = ingest_latest(c)
    c.close()
    print(f"[OK] {n} ad-day rows saved to ad_daily_insights")
    sys.exit(0)
