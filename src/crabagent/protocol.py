from __future__ import annotations

import hashlib
import json
import os
import secrets
import socket
import socketserver
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict


USE_UNIX_SOCKET = hasattr(socket, "AF_UNIX") and hasattr(socketserver, "UnixStreamServer")


def _chmod_private(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError:
        pass


def _runtime_root() -> Path:
    configured = os.environ.get("CRABAGENT_RUNTIME_DIR")
    if configured:
        return Path(configured)
    getuid = getattr(os, "getuid", None)
    return Path("/tmp/crabagent-%s" % (getuid() if getuid else "user"))


def runtime_port(workspace: Path) -> int:
    digest = hashlib.sha256(str(workspace.resolve()).encode("utf-8")).hexdigest()[:8]
    return 45000 + (int(digest, 16) % 10000)


def runtime_paths(workspace: Path) -> Dict[str, Any]:
    root = workspace.resolve() / ".crabagent"
    digest = hashlib.sha256(str(workspace.resolve()).encode("utf-8")).hexdigest()[:12]
    runtime_root = _runtime_root()
    return {
        "root": root,
        "runtime_root": runtime_root,
        "socket": runtime_root / ("%s.sock" % digest),
        "auth": root / "runtime-auth.json",
        "transport": "unix" if USE_UNIX_SOCKET else "tcp",
        "port": runtime_port(workspace),
        "endpoint": root / "crabd.endpoint.json",
        "pid": root / "crabd.pid",
        "log": root / "crabd.log",
    }


def secure_runtime_paths(paths: Dict[str, Any]) -> None:
    """Create the workspace and socket directories readable only by this user."""
    paths["root"].mkdir(parents=True, exist_ok=True)
    _chmod_private(paths["root"], 0o700)
    if paths["transport"] == "unix":
        paths["runtime_root"].mkdir(parents=True, exist_ok=True)
        _chmod_private(paths["runtime_root"], 0o700)


def runtime_auth_token(workspace: Path, *, create: bool) -> str:
    """Return the shared secret every runtime request must carry; create it when asked."""
    paths = runtime_paths(workspace)
    if create:
        secure_runtime_paths(paths)
    auth_path = paths["auth"]
    try:
        payload = json.loads(auth_path.read_text(encoding="utf-8"))
        token = str(payload.get("token") or "")
    except (OSError, ValueError, TypeError, AttributeError):
        token = ""
    if len(token) >= 32:
        _chmod_private(auth_path, 0o600)
        return token
    if not create:
        return ""
    token = secrets.token_urlsafe(32)
    temporary = auth_path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"schema": "crabagent-runtime-auth/v1", "token": token}, indent=2) + "\n", encoding="utf-8")
    _chmod_private(temporary, 0o600)
    temporary.replace(auth_path)
    _chmod_private(auth_path, 0o600)
    return token


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
        token = runtime_auth_token(self.workspace, create=False)
        message = {"action": action, "payload": payload, "auth": token}
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


def legacy_socket_path(workspace: Path) -> Path:
    """The socket path used before runtime auth moved sockets into a private directory."""
    digest = hashlib.sha256(str(workspace.resolve()).encode("utf-8")).hexdigest()[:12]
    return Path("/tmp/crabagent-%s.sock" % digest)


def _stop_legacy_daemon(workspace: Path, wait_seconds: float) -> None:
    """Shut down a pre-auth crabd still bound to the old shared /tmp socket.

    Otherwise it keeps writing the same colony database next to the new daemon.
    """
    legacy = legacy_socket_path(workspace)
    if not USE_UNIX_SOCKET or not legacy.exists():
        return
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2.0)
            client.connect(str(legacy))
            client.sendall(b'{"action": "shutdown", "payload": {}}\n')
            client.recv(65536)
    except OSError:
        # Nothing is listening: a stale socket file left by a crashed daemon.
        try:
            legacy.unlink()
        except OSError:
            pass
        return
    deadline = time.monotonic() + wait_seconds
    while legacy.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    if legacy.exists():
        try:
            legacy.unlink()
        except OSError:
            pass


def start_daemon(workspace: Path, wait_seconds: float = 5.0) -> DaemonClient:
    from . import __version__

    workspace = workspace.resolve()
    paths = runtime_paths(workspace)
    secure_runtime_paths(paths)
    runtime_auth_token(workspace, create=True)
    _stop_legacy_daemon(workspace, wait_seconds)
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
    _chmod_private(paths["log"], 0o600)
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
