"""
Alerts: things someone should look at (a failed deploy, backup or job, the volume filling up,
unusual tagging spend). Each is a row in ops_alerts, shown as a banner on every page until it is
dismissed on Admin, and, when SLACK_WEBHOOK_URL is set, posted to that Slack channel too.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime

log = logging.getLogger("adstudio.alerts")


def raise_alert(conn, source: str, message: str, level: str = "error") -> int:
    """Record an alert (and post it to Slack if configured). Returns its id."""
    with conn:
        aid = conn.execute("INSERT INTO ops_alerts (at, level, source, message) VALUES (?,?,?,?)",
                           (datetime.utcnow().isoformat(timespec="seconds"), level, source, message[:1000])).lastrowid
    log.warning("alert %s: [%s] %s", level, source, message[:300])
    notify_slack(source, message, level)
    return aid


CREDIT_WORDS = ("credit balance is too low", "insufficient credit", "insufficient_quota", "billing", "payment required",
                "spend limit", "402")
WHERE = {"Anthropic": "console.anthropic.com → Plans & Billing", "Replicate": "replicate.com/account/billing"}


def credit_out(provider: str, exc: BaseException) -> bool:
    """When an AI account runs out of credit, one alert says which and where to top it up (2026-10-26: the
    Anthropic balance ran out and QA, scripts and checks failed quietly). Never raises; True if it was a credit error."""
    if not any(w in str(exc).lower() for w in CREDIT_WORDS):
        return False
    try:
        from creative_intelligence.webapp.common import db
        conn = db()
        try:
            source = f"{provider} credit"
            if conn.execute("SELECT 1 FROM ops_alerts WHERE source=? AND resolved_at IS NULL", (source,)).fetchone() is None:
                raise_alert(conn, source, f"The {provider} account is out of credit: making stops until it's topped "
                                          f"up ({WHERE.get(provider, provider)}).")
        finally:
            conn.close()
    except Exception:
        log.exception("credit alert not recorded")
    return True


def notify_slack(source: str, message: str, level: str = "error") -> bool:
    """Post to the Slack incoming webhook in SLACK_WEBHOOK_URL. Never raises; False if not sent."""
    url = os.getenv("SLACK_WEBHOOK_URL", "").strip()
    if not url:
        return False
    try:
        import requests
        icon = {"error": ":red_circle:", "info": ":bulb:"}.get(level, ":large_yellow_circle:")
        r = requests.post(url, json={"text": f"{icon} *Ad Studio* · {source}: {message[:1500]}"}, timeout=5)
        return r.status_code < 300
    except Exception:
        log.exception("Slack alert not sent")
        return False
