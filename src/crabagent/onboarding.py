from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlsplit, urlunsplit

from .discovery import observed_assets


SCHEMA_VERSION = 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def state_path(workspace: Path) -> Path:
    return workspace.resolve() / ".crabagent" / "onboarding.json"


def _default_state() -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "product": "KINGCRAB",
        "provider": "codex",
        "phase": "local_setup",
        "local_ready": False,
        "codex": {"status": "unknown"},
        "assets": {"mcp_count": 0, "cli_count": 0, "skill_count": 0},
        "opencrab": {"status": "not_connected", "tier": None},
        "updated_at": _now(),
    }


def load_state(workspace: Path) -> Dict[str, Any]:
    try:
        value = json.loads(state_path(workspace).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return _default_state()
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        return _default_state()
    state = _default_state()
    state.update(value)
    return state


def save_state(workspace: Path, state: Dict[str, Any]) -> Dict[str, Any]:
    target = state_path(workspace)
    target.parent.mkdir(parents=True, exist_ok=True)
    state = dict(state)
    state["schema_version"] = SCHEMA_VERSION
    state["product"] = "KINGCRAB"
    state["updated_at"] = _now()
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)
    return state


def detect_local(workspace: Path, assets: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    assets = assets or observed_assets(workspace.resolve())
    codex = dict(assets.get("codex") or {})
    opencrab_rows = list(assets.get("mcp_inventory") or [])
    if not opencrab_rows:
        opencrab_rows = [{"name": name, "state": "unknown"} for name in assets.get("mcp_servers") or []]
    opencrab = next((row for row in opencrab_rows if str(row.get("name")) == "OpenCrab"), None)
    return {
        "codex": codex,
        "assets": {
            "mcp_count": len(opencrab_rows),
            "cli_count": len(assets.get("clis") or {}),
            "skill_count": len(assets.get("skills") or []),
        },
        "opencrab_configured": bool(opencrab and str(opencrab.get("state")) != "disabled"),
        "opencrab_state": str(opencrab.get("state") or "unknown") if opencrab else "not_configured",
    }


def status(workspace: Path, assets: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    state = load_state(workspace)
    detected = detect_local(workspace, assets)
    state["codex"] = detected["codex"]
    state["assets"] = detected["assets"]
    # A global Codex/MCP configuration is only an observed capability. It is
    # not proof that this local KINGCRAB workspace completed onboarding. Keep
    # the first-run gate visible until the user explicitly confirms Codex and
    # OpenCrab here; after that, the durable onboarding state prevents repeat
    # prompts on later launches.
    state["needs_setup"] = not bool(state.get("local_ready"))
    state["detected_opencrab"] = detected["opencrab_configured"]
    state["opencrab_state"] = detected["opencrab_state"]
    state["state_path"] = str(state_path(workspace))
    return state


def mark_local_ready(workspace: Path) -> Dict[str, Any]:
    state = status(workspace)
    if state.get("codex", {}).get("login_status") != "connected":
        raise RuntimeError("Codex is not connected. Run `codex login`, then rescan KINGCRAB.")
    state["local_ready"] = True
    state["phase"] = "opencrab_offer"
    state["opencrab"] = dict(state.get("opencrab") or {"status": "not_connected"})
    if state["opencrab"].get("status") not in {"connected", "configured"}:
        state["opencrab"]["status"] = "deferred"
    state.pop("needs_setup", None)
    state.pop("state_path", None)
    state.pop("detected_opencrab", None)
    state.pop("opencrab_state", None)
    return save_state(workspace, state)


def defer_opencrab(workspace: Path) -> Dict[str, Any]:
    state = mark_local_ready(workspace)
    state["phase"] = "opencrab_deferred"
    state["opencrab"] = {"status": "deferred", "tier": None}
    return save_state(workspace, state)


def normalize_endpoint(endpoint: str) -> str:
    value = str(endpoint or "").strip()
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("OpenCrab MCP URL must be an http(s) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Do not place credentials, query tokens, or fragments in the OpenCrab MCP URL")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def write_endpoint(workspace: Path, endpoint: str) -> str:
    normalized = normalize_endpoint(endpoint)
    target = workspace.resolve() / ".crabagent" / "opencrab" / "endpoint.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        (workspace.resolve() / ".crabagent").chmod(0o700)
        target.parent.chmod(0o700)
    except OSError:
        pass
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps({"endpoint": normalized, "updated_at": _now()}, indent=2) + "\n", encoding="utf-8")
    try:
        temporary.chmod(0o600)
    except OSError:
        pass
    temporary.replace(target)
    return normalized


def record_opencrab_connected(workspace: Path, *, tier: Optional[str] = None, scope: Optional[str] = None) -> Dict[str, Any]:
    state = load_state(workspace)
    detected = detect_local(workspace)
    state["codex"] = detected["codex"]
    state["assets"] = detected["assets"]
    # OpenCrab and Codex are independent connections. Connecting OpenCrab must
    # not silently mark a fresh workspace ready when Codex is still logged out.
    state["local_ready"] = bool(state.get("local_ready")) or detected["codex"].get("login_status") == "connected"
    state["phase"] = "ready" if state["local_ready"] else "opencrab_connected"
    state["opencrab"] = {"status": "connected", "tier": tier, "scope": scope}
    return save_state(workspace, state)
