import json
import os
import socket
import stat
import threading
from pathlib import Path

import pytest

from crabagent.daemon import RuntimeServer
from crabagent.onboarding import write_endpoint
from crabagent.protocol import USE_UNIX_SOCKET, DaemonClient, _stop_legacy_daemon, legacy_socket_path, runtime_auth_token, runtime_paths, secure_runtime_paths, start_daemon
from crabagent.store import ColonyStore


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def test_runtime_paths_are_private_and_authenticated(tmp_path: Path) -> None:
    paths = runtime_paths(tmp_path)
    secure_runtime_paths(paths)
    token = runtime_auth_token(tmp_path, create=True)
    assert len(token) >= 32
    assert _mode(paths["root"]) == 0o700
    assert _mode(paths["runtime_root"]) == 0o700
    assert _mode(paths["auth"]) == 0o600
    assert paths["socket"].parent == paths["runtime_root"]


@pytest.mark.skipif(not USE_UNIX_SOCKET, reason="raw Unix socket test")
def test_daemon_rejects_unauthenticated_socket_request(tmp_path: Path) -> None:
    paths = runtime_paths(tmp_path)
    secure_runtime_paths(paths)
    if paths["socket"].exists():
        paths["socket"].unlink()
    server = RuntimeServer(tmp_path, paths["socket"])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert DaemonClient(tmp_path).ping()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(str(paths["socket"]))
            client.sendall(json.dumps({"action": "ping", "payload": {}}).encode("utf-8") + b"\n")
            response = json.loads(client.recv(65536).decode("utf-8"))
        assert response["ok"] is False
        assert "PermissionError" in response["error"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        if paths["socket"].exists():
            paths["socket"].unlink()


@pytest.mark.skipif(not USE_UNIX_SOCKET, reason="socket file modes apply to the Unix transport")
def test_auto_started_daemon_uses_private_runtime_files(tmp_path: Path) -> None:
    client = start_daemon(tmp_path)
    paths = runtime_paths(tmp_path)
    try:
        assert client.request("ping")["status"] == "online"
        assert _mode(paths["root"]) == 0o700
        assert _mode(paths["runtime_root"]) == 0o700
        assert _mode(paths["auth"]) == 0o600
        assert _mode(paths["pid"]) == 0o600
        assert _mode(paths["log"]) == 0o600
        assert _mode(paths["socket"]) == 0o600
    finally:
        try:
            client.request("shutdown")
        except Exception:
            pass


def test_store_and_endpoint_files_are_private(tmp_path: Path) -> None:
    store = ColonyStore(tmp_path)
    initialized = store.initialize()
    database = Path(initialized["database"])
    config = Path(initialized["config"])
    assert _mode(tmp_path / ".crabagent") == 0o700
    assert _mode(database) == 0o600
    assert _mode(config) == 0o600

    write_endpoint(tmp_path, "https://opencrab.example/mcp")
    endpoint = tmp_path / ".crabagent" / "opencrab" / "endpoint.json"
    assert _mode(endpoint.parent) == 0o700
    assert _mode(endpoint) == 0o600


@pytest.mark.skipif(not USE_UNIX_SOCKET, reason="legacy sockets exist only on the Unix transport")
def test_pre_auth_daemon_on_the_legacy_socket_is_shut_down(tmp_path: Path) -> None:
    legacy = legacy_socket_path(tmp_path)
    if legacy.exists():
        legacy.unlink()
    received = []
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(legacy))
    listener.listen(1)

    def old_daemon() -> None:
        connection, _ = listener.accept()
        with connection:
            received.append(json.loads(connection.recv(65536).decode("utf-8")))
            connection.sendall(b'{"ok": true, "result": {"status": "stopping"}}\n')
        listener.close()
        legacy.unlink()

    thread = threading.Thread(target=old_daemon, daemon=True)
    thread.start()
    _stop_legacy_daemon(tmp_path, 2.0)
    thread.join(timeout=2)
    assert received and received[0]["action"] == "shutdown"
    assert not legacy.exists()


@pytest.mark.skipif(not USE_UNIX_SOCKET, reason="legacy sockets exist only on the Unix transport")
def test_stale_legacy_socket_file_is_removed_without_waiting(tmp_path: Path) -> None:
    legacy = legacy_socket_path(tmp_path)
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(legacy))
    stale.close()  # file remains, nothing listens
    _stop_legacy_daemon(tmp_path, 5.0)
    assert not legacy.exists()
