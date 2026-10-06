from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional


_MCP_RE = re.compile(r'^\s*\[\s*mcp_servers\.(?:"(?P<quoted>[^"]+)"|(?P<bare>[^\]\s]+))\s*\]\s*$')


def _mcp_names() -> List[str]:
    path = Path.home() / ".codex" / "config.toml"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    names = []
    for line in lines:
        match = _MCP_RE.match(line)
        if match:
            name = match.group("quoted") or match.group("bare")
            if name and "." not in name:
                names.append(name)
    return list(dict.fromkeys(names))


def mcp_inventory() -> List[Dict[str, str]]:
    """Return configured MCP names and observed Codex enable state without secrets."""
    configured = _mcp_names()
    executable = shutil.which("codex")
    observed: Dict[str, str] = {}
    if executable:
        try:
            result = subprocess.run(
                [executable, "mcp", "list"],
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            result = None
        if result is not None:
            for line in result.stdout.splitlines():
                match = re.match(r"^\s*(\S+)\s+.*\s+(enabled|disabled)\s+\S+\s*$", line)
                if match:
                    observed[match.group(1)] = match.group(2)
                    if match.group(1) not in configured:
                        configured.append(match.group(1))
    return [
        {"name": name, "configured": "true", "state": observed.get(name, "unknown")}
        for name in configured
    ]


def codex_login_status(executable: Optional[str] = None) -> str:
    """Observe Codex login without returning account details or tokens."""
    executable = executable or shutil.which("codex")
    if not executable:
        return "unavailable"
    try:
        result = subprocess.run(
            [executable, "login", "status"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    if result.returncode == 0 and "logged in" in (result.stdout + result.stderr).lower():
        return "connected"
    return "not_connected"


def claude_login_status(executable: Optional[str] = None) -> str:
    """Observe Claude Code login through `claude auth status`; only the loggedIn flag is read."""
    executable = executable or shutil.which("claude")
    if not executable:
        return "unavailable"
    try:
        result = subprocess.run(
            [executable, "auth", "status"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    try:
        logged_in = bool(json.loads(result.stdout or "{}").get("loggedIn"))
    except ValueError:
        return "unknown"
    return "connected" if logged_in else "not_connected"


def _skill_names(workspace: Path) -> List[str]:
    roots = [workspace / ".codex" / "skills", workspace / "skills", Path.home() / ".codex" / "skills", Path.home() / ".agents" / "skills"]
    names = []
    seen = set()
    for root in roots:
        if not root.is_dir():
            continue
        try:
            files = root.rglob("SKILL.md")
        except OSError:
            continue
        for path in files:
            name = path.parent.name
            if name not in seen:
                seen.add(name)
                names.append(name)
            if len(names) >= 500:
                return names
    return names


def observed_assets(workspace: Path) -> Dict[str, Any]:
    """Inventory names and executable paths only; never read credentials."""
    codex = shutil.which("codex")
    mcp_rows = mcp_inventory()
    clis = {}
    for name in ("git", "gh", "node", "python3", "ffmpeg", "crawl4ai", "insane-search"):
        found = shutil.which(name)
        if found:
            clis[name] = found
    claude = shutil.which("claude")
    return {
        "codex": {
            "status": "available" if codex else "unavailable",
            "login_status": codex_login_status(codex),
            "provider": "codex",
            "executable": codex or "",
        },
        "claude": {
            "status": "available" if claude else "unavailable",
            "login_status": claude_login_status(claude),
            "provider": "claude",
            "executable": claude or "",
        },
        "mcp_servers": [item["name"] for item in mcp_rows],
        "mcp_inventory": mcp_rows,
        "skills": _skill_names(workspace.resolve()),
        "clis": clis,
        "opencrab_signup_url": "https://opencrab.sh",
        "activation": "explicit_or_codex_policy",
    }
