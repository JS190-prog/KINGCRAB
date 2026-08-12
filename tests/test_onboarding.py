from pathlib import Path

import pytest

from crabagent import onboarding


def _local_assets(login_status: str = "connected"):
    return {
        "codex": {"status": "available", "login_status": login_status, "provider": "codex"},
        "mcp_inventory": [{"name": "LocalMcp", "state": "unknown"}],
        "clis": {"git": "/usr/bin/git"},
        "skills": ["example"],
    }


def test_local_first_state_is_durable_and_opencrab_is_deferred(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(onboarding, "observed_assets", lambda workspace: _local_assets())

    state = onboarding.status(tmp_path)
    assert state["needs_setup"] is True
    assert state["opencrab"]["status"] == "not_connected"

    saved = onboarding.defer_opencrab(tmp_path)
    assert saved["local_ready"] is True
    assert saved["phase"] == "opencrab_deferred"

    reopened = onboarding.status(tmp_path)
    assert reopened["local_ready"] is True
    assert reopened["opencrab"]["status"] == "deferred"
    assert reopened["assets"] == {"mcp_count": 1, "cli_count": 1, "skill_count": 1}


def test_local_ready_requires_observed_codex_login(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(onboarding, "observed_assets", lambda workspace: _local_assets("not_connected"))
    with pytest.raises(RuntimeError, match="Codex is not connected"):
        onboarding.mark_local_ready(tmp_path)


def test_fresh_workspace_does_not_skip_explicit_connection_when_global_assets_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        onboarding,
        "observed_assets",
        lambda workspace: {
            **_local_assets(),
            "mcp_inventory": [{"name": "OpenCrab", "state": "enabled"}],
        },
    )

    fresh = onboarding.status(tmp_path)
    assert fresh["needs_setup"] is True
    assert fresh["local_ready"] is False
    assert fresh["opencrab"]["status"] == "not_connected"
    assert fresh["detected_opencrab"] is True

    connected = onboarding.record_opencrab_connected(tmp_path, tier="expert", scope="connected_account")
    assert connected["phase"] == "ready"
    assert connected["opencrab"]["status"] == "connected"

    reopened = onboarding.status(tmp_path)
    assert reopened["needs_setup"] is False
    assert reopened["opencrab"]["status"] == "connected"


def test_opencrab_connection_does_not_complete_codex_setup_when_codex_is_logged_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(onboarding, "observed_assets", lambda workspace: _local_assets("not_connected"))

    connected = onboarding.record_opencrab_connected(tmp_path, tier="free", scope="connected_account")

    assert connected["opencrab"]["status"] == "connected"
    assert connected["local_ready"] is False
    assert connected["phase"] == "opencrab_connected"


def test_opencrab_endpoint_rejects_embedded_credentials_or_query_tokens() -> None:
    assert onboarding.normalize_endpoint("https://opencrab.example/mcp/") == "https://opencrab.example/mcp"
    with pytest.raises(ValueError, match="credentials"):
        onboarding.normalize_endpoint("https://user:secret@opencrab.example/mcp")
    with pytest.raises(ValueError, match="query tokens"):
        onboarding.normalize_endpoint("https://opencrab.example/mcp?token=secret")
