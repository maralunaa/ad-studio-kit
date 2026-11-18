"""
Post-deploy smoke for Ad Studio v2 (run by .github/workflows/deploy-smoke.yml after Railway deploys).

  1. Wait until /healthz answers ok and reports the deployed commit.
  2. Call /admin/smoke (ADMIN_TOKEN): the app loads its main pages and APIs as a signed-in user.
     On a pass, check (read-only) that the Railway token can see a deployment to roll back to,
     and warn on Admin if it can't.
  3. If the deploy or the smoke failed (and --no-dry-run): roll back to the last good Railway deployment (project token,
     only for the newest commit on the branch, so a rollback never cascades), wait for it to be
     healthy, and post an alert that shows as a banner on the Admin page. Exit 1 so GitHub emails.

--dry-run is on by default: it checks and reports, and only prints the rollback and alert it would
make. Read-only apart from those. No paid calls. Standard library only.

  python scripts/deploy_smoke.py --url https://… --sha <commit> --state success --head-sha <branch head> [--no-dry-run]
  env: ADMIN_TOKEN, RAILWAY_TOKEN (project token), RAILWAY_PROJECT_ID, RAILWAY_ENVIRONMENT_ID, RAILWAY_SERVICE_ID
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

RAILWAY_API = "https://backboard.railway.com/graphql/v2"
USER_AGENT = "ad-studio-deploy-smoke/1.0"
BAD_STATES = {"FAILED", "CRASHED", "BUILDING", "DEPLOYING", "INITIALIZING", "QUEUED", "WAITING", "SKIPPED"}


def http(method: str, url: str, body: dict | None = None, headers: dict | None = None, timeout: int = 60):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 # Railway's firewall rejects Python's default user agent (Cloudflare 1010)
                                 headers={"Content-Type": "application/json", "User-Agent": USER_AGENT, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        return 0, {"error": str(e)}


def wait_healthy(url: str, sha: str, minutes: float = 6) -> tuple[bool, str]:
    deadline, last = time.time() + minutes * 60, ""
    while time.time() < deadline:
        code, d = http("GET", f"{url}/healthz", timeout=20)
        if code == 200 and d.get("ok") and d.get("version") in (sha[:7], "local", ""):
            return True, f"healthz ok, version {d.get('version')}"
        last = f"healthz {code}: {json.dumps(d)[:300]}"
        time.sleep(10)
    return False, last


def smoke(url: str, sha: str) -> tuple[bool, str]:
    ok, msg = wait_healthy(url, sha)
    if not ok:
        return False, msg
    code, d = http("GET", f"{url}/admin/smoke", headers={"X-Admin-Token": os.getenv("ADMIN_TOKEN", "")}, timeout=120)
    if code != 200 or not d.get("ok"):
        bad = {k: v for k, v in (d.get("checks") or {}).items() if v != 200}
        return False, f"/admin/smoke {code}: " + (json.dumps(bad) if bad else json.dumps(d)[:300])
    return True, f"{msg}; {len(d['checks'])} pages ok"


def railway(query: str, variables: dict) -> dict:
    code, d = http("POST", RAILWAY_API, {"query": query, "variables": variables},
                   headers={"Project-Access-Token": os.environ["RAILWAY_TOKEN"]})
    if code != 200 or d.get("errors"):
        raise RuntimeError(f"Railway API {code}: {json.dumps(d.get('errors') or d)[:300]}")
    return d["data"]


def _good_deployments() -> list[dict]:
    data = railway("""query($input: DeploymentListInput!, $first: Int) {
      deployments(input: $input, first: $first) { edges { node { id status createdAt canRollback meta } } } }""",
                   {"first": 20, "input": {"projectId": os.environ["RAILWAY_PROJECT_ID"],
                                           "environmentId": os.environ["RAILWAY_ENVIRONMENT_ID"],
                                           "serviceId": os.environ["RAILWAY_SERVICE_ID"]}})
    # Railway rejects the documented status filter ("Problem processing request"), so filter here:
    # deployments that ran fine and that Railway says can be rolled back to, newest first.
    return [e["node"] for e in data["deployments"]["edges"]
            if e["node"].get("status") not in BAD_STATES and e["node"].get("canRollback")]


def rollback_ready(current_sha: str) -> tuple[bool, str]:
    """Read-only: can the project token see an earlier good deployment to roll back to?"""
    if not os.getenv("RAILWAY_TOKEN"):
        return False, "RAILWAY_TOKEN is not set"
    try:
        for node in _good_deployments():
            commit = (node.get("meta") or {}).get("commitHash") or ""
            if commit and commit != current_sha and node.get("canRollback"):
                return True, f"rollback ready: last good deployment before this one is {commit[:7]}"
        return False, "the token works, but there is no earlier deployment to roll back to"
    except Exception as exc:
        return False, f"the Railway token doesn't work: {exc}"


def rollback(bad_sha: str) -> str:
    """Roll back to the newest successful deployment of another commit. Returns its commit."""
    for node in _good_deployments():
        commit = (node.get("meta") or {}).get("commitHash") or ""
        if commit and commit != bad_sha and node.get("canRollback"):
            railway("mutation($id: String!) { deploymentRollback(id: $id) }", {"id": node["id"]})   # returns Boolean!
            return commit
    raise RuntimeError("no earlier successful deployment to roll back to")


def alert(url: str, message: str, level: str = "error") -> None:
    code, _ = http("POST", f"{url}/admin/alert", {"source": "deploy", "level": level, "message": message},
                   headers={"X-Admin-Token": os.getenv("ADMIN_TOKEN", "")})
    print(f"alert posted ({code})")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--sha", required=True)
    ap.add_argument("--state", default="success", help="the Railway deployment's state on GitHub")
    ap.add_argument("--head-sha", default="", help="newest commit on the branch; only it is ever rolled back")
    ap.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=True,
                    help="on by default: report only; --no-dry-run rolls back and posts the alert")
    a = ap.parse_args()
    url, short = a.url.rstrip("/"), a.sha[:7]

    if a.state == "success":
        ok, msg = smoke(url, a.sha)
        print(("PASS " if ok else "FAIL ") + msg)
        if ok:
            ready, note = rollback_ready(a.sha)
            print(note)
            if not ready and "token" in note:
                warn = f"Deploy of {short} is live, but automatic rollback is not available: {note}."
                print(f"[dry run] would alert: {warn}") if a.dry_run else alert(url, warn, level="warn")
            return 0
        reason = f"Deploy of {short} failed the smoke check: {msg}"
    else:
        reason = f"Railway reported the deploy of {short} as {a.state} (build or healthcheck failed)"
        code, d = http("GET", f"{url}/healthz", timeout=30)
        if code == 200 and isinstance(d, dict) and d.get("ok"):
            # A failed build never goes live: Railway keeps the running deployment. Rolling back would swap it for an
            # older snapshot (older code and old variables), as happened on 2026-10-22, so only say so.
            note = reason + f". Nothing changed: the app keeps running {d.get('version', 'the previous version')}. Retry the deploy."
            print(note)
            print(f"[dry run] would alert: {note}") if a.dry_run else alert(url, note, level="warn")
            return 1
    print(reason)

    if a.head_sha and a.sha != a.head_sha:
        note = reason + ". Not rolled back: it is not the newest commit (likely a rollback itself)."
        print(f"[dry run] would alert: {note}") if a.dry_run else alert(url, note)
        return 1
    if a.dry_run:
        print(f"[dry run] would roll back to the last good deployment and alert: {reason}")
        return 1
    try:
        to = rollback(a.sha)
    except Exception as exc:
        print(f"rollback failed: {exc}")
        alert(url, reason + f". Rollback failed: {exc}. Check Railway.")
        return 1
    healthy, msg = wait_healthy(url, to, minutes=8)
    alert(url, reason + f". Rolled back to {to[:7]}" + ("." if healthy else f", which is not healthy yet ({msg}). Check Railway."))
    return 1


if __name__ == "__main__":
    sys.exit(main())
