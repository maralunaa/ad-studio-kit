"""
Generation cost log.

Every successful image generation is recorded in model_calls with its estimated
cost, priced from the model_prices table (seeded from config.MODEL_PRICES_SEED and
editable afterwards; each price keeps where it came from and when it was checked).
Each call is also appended to outputs/api.log with timestamp and endpoint, like the
Meta calls. No secrets are written anywhere.

Asset costs (assets.py) are the sum of the calls for the ad's source row.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

API_LOG = Path("outputs/api.log")   # relative to the working directory, like src/api.py's Meta log


def seed_prices(conn) -> None:
    from creative_intelligence import config
    with conn:
        for p in config.MODEL_PRICES_SEED:
            conn.execute("INSERT OR IGNORE INTO model_prices (model, unit, usd, source, checked_at)"
                         " VALUES (?,?,?,?,?)", (p["model"], p["unit"], p["usd"], p["source"], p["checked_at"]))


def price(conn, model: str) -> float:
    seed_prices(conn)
    r = conn.execute("SELECT usd FROM model_prices WHERE model=?", (model,)).fetchone()
    if r is None and ":" in model:        # a variant with no price of its own (seedance-2.0:audio was logged as $0,
        return price(conn, model.rsplit(":", 1)[0])           # 2026-10-26): never cheaper than the plain model
    return float(r["usd"]) if r else 0.0


def _api_log(line: str) -> None:
    try:
        API_LOG.parent.mkdir(parents=True, exist_ok=True)
        with API_LOG.open("a") as f:
            f.write(line + "\n")
    except OSError:
        pass  # logging must never break a generation


def log_image(conn, model: str, source: str, source_id: int, units: int = 1,
              run_id: str | None = None, kind: str = "image") -> float:
    """Record `units` generated images for a source row. Returns the cost in USD."""
    cost = round(price(conn, model) * units, 4)
    now = datetime.utcnow().isoformat()
    with conn:
        conn.execute("INSERT INTO model_calls (at, model, kind, units, cost_usd, source, source_id, run_id)"
                     " VALUES (?,?,?,?,?,?,?,?)", (now, model, kind, units, cost, source, source_id, run_id))
    _api_log(f"{now} | REPLICATE | {model} | {source}#{source_id} | {units} image(s) | ${cost:.4f}")
    return cost


def log_video(conn, model: str, seconds: float, source: str, source_id: int = 0, audio: bool = False) -> float:
    """Record a generated clip, priced per second of video (the ':audio' rate with sound). Returns USD."""
    cost = round(price(conn, model + (":audio" if audio else "")) * seconds, 4)
    now = datetime.utcnow().isoformat()
    with conn:
        conn.execute("INSERT INTO model_calls (at, model, kind, units, cost_usd, source, source_id, run_id)"
                     " VALUES (?,?,?,?,?,?,?,NULL)", (now, model, "video", seconds, cost, source, source_id))
    _api_log(f"{now} | REPLICATE | {model} | {source}#{source_id} | {seconds:g} s video | ${cost:.4f}")
    return cost


def spend_since(conn, since_iso: str) -> float:
    r = conn.execute("SELECT COALESCE(SUM(cost_usd),0) FROM model_calls WHERE at >= ?", (since_iso,)).fetchone()
    return round(r[0] or 0.0, 4)


def summary(conn) -> dict[str, Any]:
    today = datetime.utcnow().strftime("%Y-%m-%d")
    return {"today_usd": spend_since(conn, today),
            "prices": [dict(r) for r in conn.execute("SELECT * FROM model_prices ORDER BY model")]}


def log_tokens(conn, model: str, kind: str, usage: Any, source: str = "", source_id: int = 0,
               run_id: str | None = None) -> float:
    """Record one Claude call from its usage, priced from model_prices (input / ':output' per 1M)."""
    tin = (getattr(usage, "input_tokens", 0) or 0) + (getattr(usage, "cache_read_input_tokens", 0) or 0) \
        + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
    tout = getattr(usage, "output_tokens", 0) or 0
    cost = round(tin / 1e6 * price(conn, model) + tout / 1e6 * price(conn, model + ":output"), 5)
    now = datetime.utcnow().isoformat()
    with conn:
        conn.execute("INSERT INTO model_calls (at, model, kind, units, cost_usd, source, source_id, run_id)"
                     " VALUES (?,?,?,?,?,?,?,?)", (now, model, kind, tin + tout, cost, source, source_id, run_id))
    _api_log(f"{now} | ANTHROPIC | {model} | {kind} | {tin} in / {tout} out | ${cost:.4f}")
    return cost


# Scoring what was already paid for is never stopped by the daily text cap (2026-11-10: three packs used the $15 on
# scripts, then $23 of clips and 50 AI ads went unscored, so every video failed "no shot passed QA"). It still counts.
NEVER_BLOCKED = ("qa",)


class LoggedClient:
    """Wraps an Anthropic client: every messages.create (plain or beta) is budget-checked first
    (budget.py; tagging is never blocked) and, with log=True, logged with its cost afterwards.

    Logging opens its own DB connection and never raises into the caller; the budget check raises
    BudgetExceeded on purpose."""

    def __init__(self, client: Any, kind: str, log: bool = True):
        self._client, self._kind = client, kind
        self.messages = _LoggedMessages(client.messages, kind, log)
        self.beta = type("Beta", (), {"messages": _LoggedMessages(client.beta.messages, kind, log)})()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


class _LoggedMessages:
    def __init__(self, messages: Any, kind: str, log: bool = True):
        self._m, self._kind, self._log = messages, kind, log

    def create(self, **kw: Any) -> Any:
        from creative_intelligence import budget
        if self._kind not in NEVER_BLOCKED:
            budget.check_now(budget.bucket_of(self._kind))
        try:
            resp = self._m.create(**kw)
        except Exception as exc:
            from creative_intelligence.alerts import credit_out
            credit_out("Anthropic", exc)
            raise
        if self._log:
            try:
                from creative_intelligence.db import get_connection
                conn = get_connection()
                try:
                    log_tokens(conn, kw.get("model", ""), self._kind, resp.usage)
                    if self._kind == "tagging":
                        budget.after_tagging(conn)
                finally:
                    conn.close()
            except Exception:
                pass                                   # cost logging must never break a feature
        return resp

    def __getattr__(self, name: str) -> Any:
        return getattr(self._m, name)
