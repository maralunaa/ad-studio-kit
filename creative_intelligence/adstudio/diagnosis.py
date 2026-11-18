"""
Diagnosis hints for ads with Meta results (Meta Creative Playbook › Metrics and diagnosis, knowledge/metrics.yaml).

An ad gets a hint once it has enough data to read (at least 7 days and about 5,000 impressions; day 1-2 numbers
are noise): the first diagnosis rule whose pattern matches, e.g. "High hook, low hold: tighten seconds 3–15".

Hold rate follows the playbook (15-second views ÷ 3-second views) when the sync has 15-second views; otherwise it
is ThruPlays ÷ 3-second views, which reads high for videos under 15 s (a ThruPlay is the whole of a shorter
video), so hold-based rules are skipped for those.
"""
from __future__ import annotations

from typing import Any

from creative_intelligence import knowledge


def levels(r: dict[str, Any], duration_s: float | None = None) -> dict[str, str]:
    """'high' / 'low' for each signal the results can tell (results rates are percentages)."""
    lv = knowledge.load("metrics")["diagnosis"]["levels"]
    out: dict[str, str] = {}
    if r.get("hook_rate") is not None:
        out["hook"] = "high" if r["hook_rate"] / 100 >= lv["hook"] else "low"
    hold_ok = r.get("hold_basis") == "15s" or (duration_s or 0) >= 15
    if r.get("hold_rate") is not None and hold_ok:
        out["hold"] = "high" if r["hold_rate"] / 100 >= lv["hold"] else "low"
    if r.get("ctr") is not None:
        out["ctr"] = "high" if r["ctr"] / 100 >= lv["ctr"] else "low"
    if r.get("cpa") is not None:
        out["conversion"] = "high" if r["cpa"] <= lv["cpa_max"] else "low"
    elif r.get("ctr") is not None and (r.get("purchases") or 0) == 0 and (r.get("spend") or 0) > lv["cpa_max"]:
        out["conversion"] = "low"                     # clicks but no purchase after a CPA's worth of spend
    if r.get("roas") is not None:
        out["roas"] = "high" if r["roas"] >= lv["roas"] else "low"
    return out


def hint(r: dict[str, Any] | None, duration_s: float | None = None) -> dict[str, str] | None:
    """{key, problem, hint} for the first matching diagnosis rule, or None (not enough data, or nothing matches)."""
    if not r:
        return None
    m = knowledge.load("metrics")
    need = m["minimum_read"]
    if (r.get("days") or 0) < need["days"] or (r.get("impressions") or 0) < need["impressions"]:
        return None
    lv = levels(r, duration_s)
    for rule in m["diagnosis"]["rules"]:
        if all(lv.get(k) == v for k, v in rule["when"].items()):
            return {"key": rule["key"], "problem": rule["problem"], "hint": rule["hint"]}
    return None
