"""
Spending caps, checked before every paid call.

  generation   images (Replicate)                      per run (generation_run) and per day (generation_day)
  video        video clips and the stills made for them per video job (video_job) and per day (video_day)
  text         Claude: hooks, briefs, chat, QA, text and claims checks           per day (text_day)
  tagging      Claude tagging during the Meta sync: never blocked; an Admin alert
               when a day's tagging passes tagging_alert_day

A day runs from 00:00 in the brand's timezone (brand.yaml › timezone). Caps live in budget_caps (edit them on Admin); defaults come from
config. check() raises BudgetExceeded with CAP_MESSAGE; the web answers 402 with that message
and a job fails with it.
"""
from __future__ import annotations

from datetime import datetime, time, timezone
from typing import Any

def _tz():
    from zoneinfo import ZoneInfo
    from creative_intelligence import brand
    return ZoneInfo(brand.get("timezone") or "UTC")


def _city() -> str:
    from creative_intelligence import brand
    return (brand.get("timezone") or "UTC").split("/")[-1].replace("_", " ")


CAP_MESSAGE = f"Daily cap reached — resets at 00:00 {_city()}. Admin can raise it."
LABELS = {"generation_run": "Images, per run", "generation_day": "Images, per day",
          "video_job": "Video, per video job", "video_day": "Video, per day",
          "text_day": "Claude text calls, per day", "tagging_alert_day": "Meta sync tagging, alert above (per day)",
          "research_month": "Research (competitor tagging, weekly brief), per month"}
GENERATION_KINDS = ("image",)
VIDEO_KINDS = ("video", "video-still")
TAGGING_KINDS = ("tagging",)


class BudgetExceeded(RuntimeError):
    pass


def defaults() -> dict[str, float]:
    from creative_intelligence import config
    return {"generation_run": config.BUDGET_RUN_USD, "generation_day": config.BUDGET_DAY_USD,
            "video_job": config.BUDGET_VIDEO_JOB_USD, "video_day": config.BUDGET_VIDEO_DAY_USD,
            "text_day": config.BUDGET_TEXT_DAY_USD, "tagging_alert_day": config.BUDGET_TAGGING_ALERT_USD,
            "research_month": config.BUDGET_RESEARCH_MONTH_USD}


def caps(conn) -> dict[str, float]:
    out = defaults()
    for r in conn.execute("SELECT key, usd FROM budget_caps"):
        if r[0] in out:
            out[r[0]] = float(r[1])
    return out


def set_cap(conn, key: str, usd: float) -> None:
    if key not in LABELS:
        raise ValueError(f"unknown cap {key!r}")
    if not 0 <= usd <= 10_000:
        raise ValueError("a cap must be between $0 and $10,000")
    with conn:
        conn.execute("INSERT INTO budget_caps (key, usd, updated_at) VALUES (?,?,?) ON CONFLICT(key)"
                     " DO UPDATE SET usd=excluded.usd, updated_at=excluded.updated_at",
                     (key, usd, datetime.utcnow().isoformat(timespec="seconds")))


def day_start_utc(now: datetime | None = None) -> str:
    """Today's 00:00 in the brand's timezone, as a naive UTC ISO string (model_calls.at is naive UTC)."""
    lis = _tz()
    now_l = (now.replace(tzinfo=timezone.utc) if now else datetime.now(timezone.utc)).astimezone(lis)
    start = datetime.combine(now_l.date(), time(0), tzinfo=lis).astimezone(timezone.utc)
    return start.replace(tzinfo=None).isoformat()


def seconds_to_reset(now: datetime | None = None) -> int:
    """Seconds until the next 00:00 in the brand's timezone, when the daily caps reset (a paused video waits for it)."""
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    lis = _tz()
    n = (now or datetime.utcnow()).replace(tzinfo=ZoneInfo("UTC")).astimezone(lis)
    nxt = (n + timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
    return max(60, int((nxt - n).total_seconds()))


def bucket_of(kind: str) -> str:
    if kind in GENERATION_KINDS:
        return "generation"
    if kind in VIDEO_KINDS:
        return "video"
    return "tagging" if kind in TAGGING_KINDS else "text"


def spent_today(conn, bucket: str, now: datetime | None = None) -> float:
    kinds = {"generation": GENERATION_KINDS, "video": VIDEO_KINDS, "tagging": TAGGING_KINDS}.get(bucket)
    since = day_start_utc(now)
    if kinds:
        q = f"SELECT COALESCE(SUM(cost_usd),0) FROM model_calls WHERE at >= ? AND kind IN ({','.join('?' * len(kinds))})"
        r = conn.execute(q, (since, *kinds)).fetchone()
    else:
        excluded = GENERATION_KINDS + VIDEO_KINDS + TAGGING_KINDS
        q = f"SELECT COALESCE(SUM(cost_usd),0) FROM model_calls WHERE at >= ? AND kind NOT IN ({','.join('?' * len(excluded))})"
        r = conn.execute(q, (since, *excluded)).fetchone()
    return round(r[0] or 0.0, 4)


def check(conn, bucket: str, estimate: float = 0.0) -> None:
    """Raise BudgetExceeded if this call would take the bucket over today's cap. Tagging never raises."""
    if bucket == "tagging":
        return
    cap = caps(conn)[{"generation": "generation_day", "video": "video_day"}.get(bucket, "text_day")]
    spent = spent_today(conn, bucket)
    if spent >= cap or spent + estimate > cap:
        raise BudgetExceeded(CAP_MESSAGE)


def research_spent_this_month(conn, now: datetime | None = None) -> float:
    """Research Claude calls (source market_research) since the 1st of this month in the brand's timezone time."""
    start = _month_start_utc(now)
    r = conn.execute("SELECT COALESCE(SUM(cost_usd),0) FROM model_calls WHERE at >= ? AND source='market_research'",
                     (start,)).fetchone()
    return round(r[0] or 0.0, 4)


def _month_start_utc(now: datetime | None = None) -> str:
    from zoneinfo import ZoneInfo
    lis = _tz()
    now_l = (now.replace(tzinfo=timezone.utc) if now else datetime.now(timezone.utc)).astimezone(lis)
    start = datetime.combine(now_l.date().replace(day=1), time(0), tzinfo=lis).astimezone(timezone.utc)
    return start.replace(tzinfo=None).isoformat()


def check_research(conn, estimate: float = 0.0) -> None:
    """The research cap ($10 a month by default; brief R) on top of the daily text cap."""
    cap = caps(conn)["research_month"]
    spent = research_spent_this_month(conn)
    if spent >= cap or spent + estimate > cap:
        raise BudgetExceeded(f"Research cap reached (${cap:.2f} this month). Admin can raise it.")
    check(conn, "text", estimate)


def check_now(bucket: str, estimate: float = 0.0) -> None:
    """check() with its own connection, for call sites that have none."""
    from creative_intelligence.db import get_connection, init_db
    init_db()
    conn = get_connection()
    try:
        check(conn, bucket, estimate)
    finally:
        conn.close()


def after_tagging(conn) -> bool:
    """Post one Admin alert a day when tagging spend passes its alert level. True if posted."""
    spent, level = spent_today(conn, "tagging"), caps(conn)["tagging_alert_day"]
    if spent <= level:
        return False
    if conn.execute("SELECT 1 FROM ops_alerts WHERE source='tagging' AND at >= ?", (day_start_utc(),)).fetchone():
        return False
    from creative_intelligence.alerts import raise_alert
    raise_alert(conn, "tagging", f"Meta sync tagging has cost ${spent:.2f} today, above the ${level:.2f} alert level. "
                "It is never blocked; check the sync if this keeps happening.", level="warn")
    return True


def overview(conn) -> dict[str, Any]:
    c = caps(conn)
    return {"caps": c, "labels": LABELS, "message": CAP_MESSAGE, "day_starts_utc": day_start_utc(),
            "today": {b: spent_today(conn, b) for b in ("generation", "video", "text", "tagging")}}


def spent_on_video(conn, video_id: int) -> float:
    r = conn.execute("SELECT COALESCE(SUM(cost_usd),0) FROM model_calls WHERE source='video' AND source_id=?",
                     (video_id,)).fetchone()
    return round(r[0] or 0.0, 4)


def check_video_job(conn, video_id: int, estimate: float) -> None:
    """A video job may not pass its own cap (video_job), on top of the daily video cap."""
    cap = caps(conn)["video_job"]
    if spent_on_video(conn, video_id) + estimate > cap:
        raise BudgetExceeded(f"This video job would pass its ${cap:.2f} cap. Admin can raise it.")
    check(conn, "video", estimate)
