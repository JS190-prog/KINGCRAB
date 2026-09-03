from __future__ import annotations

import json
from pathlib import Path

import pytest

from crabagent import opencrab as opencrab_module
from crabagent.opencrab import OpenCrabMcpClient, OpenCrabUnavailable, opencrab_url


def _config(tmp_path: Path, section: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        "[mcp_servers.other]\nurl = \"http://127.0.0.1:1/mcp\"\n\n"
        "[mcp_servers.%s]\nenabled = true\nurl = \"http://127.0.0.1:18007/mcp\"\n" % section,
        encoding="utf-8",
    )
    return path


def _write_override(root: Path, endpoint: str) -> None:
    target = root / ".crabagent" / "opencrab"
    target.mkdir(parents=True, exist_ok=True)
    (target / "endpoint.json").write_text(json.dumps({"endpoint": endpoint}), encoding="utf-8")


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep the real user-level override out of these assertions."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(opencrab_module.Path, "home", staticmethod(lambda: home))
    return home


@pytest.mark.parametrize("section", ["opencrab", "OpenCrab", "OPENCRAB"])
def test_endpoint_resolves_regardless_of_section_case(tmp_path: Path, section: str) -> None:
    assert opencrab_url(config_path=_config(tmp_path, section)) == "http://127.0.0.1:18007/mcp"


def test_missing_section_still_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(OpenCrabUnavailable):
        opencrab_url(config_path=_config(tmp_path, "something-else"))


def test_user_level_override_wins_over_codex_config(tmp_path: Path, _isolated_home: Path) -> None:
    _write_override(_isolated_home, "https://opencrab.example/api/mcp/tok")
    assert opencrab_url(config_path=_config(tmp_path, "opencrab")) == "https://opencrab.example/api/mcp/tok"


def test_workspace_override_wins_over_user_level(tmp_path: Path, _isolated_home: Path) -> None:
    _write_override(_isolated_home, "https://user.example/mcp")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _write_override(workspace, "https://workspace.example/mcp")
    assert opencrab_url(config_path=_config(tmp_path, "opencrab"), workspace=workspace) == "https://workspace.example/mcp"


def test_handshake_runs_once_per_client() -> None:
    """A session-less server must not re-handshake on every tool call."""
    calls: list[str] = []

    def fake_post(self, method, params, *, expect_result=True, timeout=20.0):
        calls.append(method)
        if method == "tools/call":
            return {"content": [{"type": "text", "text": "{\"ok\": true}"}]}
        return {}

    client = OpenCrabMcpClient("https://example.invalid/mcp")
    original = OpenCrabMcpClient._post
    try:
        OpenCrabMcpClient._post = fake_post  # type: ignore[method-assign]
        client.call_tool("opencrab_query", {"question": "a"})
        client.call_tool("opencrab_query", {"question": "b"})
        client.call_tool("opencrab_status", {})
    finally:
        OpenCrabMcpClient._post = original  # type: ignore[method-assign]

    assert client.session_id == ""
    assert calls.count("initialize") == 1
    assert calls.count("notifications/initialized") == 1
    assert calls.count("tools/call") == 3
