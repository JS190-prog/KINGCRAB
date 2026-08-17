from __future__ import annotations

import hashlib
import json
import socket
import socketserver
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict


USE_UNIX_SOCKET = hasattr(socket, "AF_UNIX") and hasattr(socketserver, "UnixStreamServer")


def runtime_port(workspace: Path) -> int:
    digest = hashlib.sha256(str(workspace.resolve()).encode("utf-8")).hexdigest()[:8]
    return 45000 + (int(digest, 16) % 10000)


def runtime_paths(workspace: Path) -> Dict[str, Any]:
    root = workspace.resolve() / ".crabagent"
    digest = hashlib.sha256(str(workspace.resolve()).encode("utf-8")).hexdigest()[:12]
    return {
        "root": root,
        "socket": Path("/tmp/crabagent-%s.sock" % digest),
        "transport": "unix" if USE_UNIX_SOCKET else "tcp",
        "port": runtime_port(workspace),
        "endpoint": root / "crabd.endpoint.json",
        "pid": root / "crabd.pid",
        "log": root / "crabd.log",
    }


def _compute_runtime_revision() -> str:
    """Compute the source fingerprint once for the current process."""
    package_root = Path(__file__).resolve().parent
    parts = []
    for path in sorted(package_root.glob("*.py")):
        try:
            source = path.read_bytes()
        except OSError:
            continue
        parts.append(path.name.encode("utf-8") + b":" + hashlib.sha256(source).digest())
    return hashlib.sha256(b"|".join(parts)).hexdigest()[:16]


# This must be captured at import time. Re-reading file mtimes on every ping
# lets an old daemon observe new source and falsely claim to be up to date.
_RUNTIME_REVISION = _compute_runtime_revision()


def runtime_revision() -> str:
    """Return this process's source fingerprint for stale-daemon detection."""
    return _RUNTIME_REVISION


class DaemonClient:
    def __init__(self, workspace: Path, timeout: float = 5.0) -> None:
        self.workspace = workspace.resolve()
        self.timeout = timeout
        self.paths = runtime_paths(self.workspace)

    def request(self, action: str, **payload: Any) -> Dict[str, Any]:
        message = {"action": action, "payload": payload}
        transport = self.paths["transport"]
        if transport == "unix":
            family = socket.AF_UNIX
            address: Any = str(self.paths["socket"])
        else:
            family = socket.AF_INET
            address = ("127.0.0.1", int(self.paths["port"]))
            try:
                endpoint = json.loads(self.paths["endpoint"].read_text(encoding="utf-8"))
                if endpoint.get("transport") == "tcp" and 1 <= int(endpoint.get("port")) <= 65535:
                    address = ("127.0.0.1", int(endpoint["port"]))
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                pass
        with socket.socket(family, socket.SOCK_STREAM) as client:
            # Read-only OpenCrab inventory calls can legitimately take longer than
            # a local state request; the TUI invokes them on a background thread.
            if action in {"ontology.refresh", "ontology.sync", "ontology.catalog", "pack.ingest"}:
                request_timeout = 300.0
            elif action in {"assets", "mcp.list", "cli.list"}:
                request_timeout = max(15.0, self.timeout)
            else:
                request_timeout = self.timeout
            client.settimeout(request_timeout)
            client.connect(address)
            client.sendall((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
            chunks = bytearray()
            while True:
                data = client.recv(65536)
                if not data:
                    break
                chunks.extend(data)
                if b"\n" in data:
                    break
        response = json.loads(chunks.decode("utf-8"))
        if not response.get("ok"):
            raise RuntimeError(str(response.get("error", "unknown daemon error")))
        return response["result"]

    def ping(self) -> bool:
        try:
            result = self.request("ping")
            return result.get("status") == "online"
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
            return False


def start_daemon(workspace: Path, wait_seconds: float = 5.0) -> DaemonClient:
    from . import __version__

    workspace = workspace.resolve()
    paths = runtime_paths(workspace)
    paths["root"].mkdir(parents=True, exist_ok=True)
    client = DaemonClient(workspace)
    if client.ping():
        current = client.request("ping")
        if str(current.get("version") or "") == __version__ and str(current.get("runtime_revision") or "") == runtime_revision():
            return client
        client.request("shutdown")
        restart_deadline = time.monotonic() + wait_seconds
        while client.ping() and time.monotonic() < restart_deadline:
            time.sleep(0.05)
    log_handle = paths["log"].open("ab")
    subprocess.Popen(
        [sys.executable, "-m", "crabagent.daemon", "--workspace", str(workspace)],
        stdin=subprocess.DEVNULL,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        close_fds=True,
    )
    log_handle.close()
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if client.ping():
            return client
        time.sleep(0.05)
    raise RuntimeError("crabd did not become ready; inspect %s" % paths["log"])
