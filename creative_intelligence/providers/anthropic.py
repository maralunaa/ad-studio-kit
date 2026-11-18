"""Claude adapter: every call is budget-checked first (text, or tagging which is never blocked)
and logged with its cost afterwards (adstudio/costs.py)."""
from __future__ import annotations

import os
from typing import Any


def client(kind: str = "copy", log: bool = True) -> Any:
    """kind names the call in the cost log ("copy", "tagging", …). With log=False the caller logs
    the call itself (with more detail); the budget check still runs."""
    import anthropic
    from creative_intelligence.adstudio.costs import LoggedClient
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    return LoggedClient(anthropic.Anthropic(api_key=key, default_headers=workspace_headers()), kind, log=log)


def workspace_headers() -> dict[str, str]:
    """An organisation-level API key (not scoped to a workspace) must name the workspace to bill on every request:
    set ANTHROPIC_WORKSPACE_ID (wrkspc_…, Console › Settings › Workspaces). A workspace key needs nothing."""
    ws = os.getenv("ANTHROPIC_WORKSPACE_ID", "").strip()
    return {"anthropic-workspace-id": ws} if ws else {}
