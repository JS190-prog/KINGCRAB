from __future__ import annotations

import time
from pathlib import Path

from crabagent.daemon import RuntimeServer
from crabagent.protocol import runtime_paths


def _server(tmp_path: Path) -> RuntimeServer:
    paths = runtime_paths(tmp_path)
    paths["root"].mkdir(parents=True, exist_ok=True)
    return RuntimeServer(tmp_path, paths["socket"])


def _configure(server: RuntimeServer, session_id: str, mode: str) -> dict:
    return server.dispatch({
        "action": "session.configure",
        "payload": {
            "session_id": session_id,
            "model_policy": "host",
            "executor_policy": "host",
            "interaction_mode": mode,
            "mcp_policy": "off",
            "max_workers": 1,
        },
    })


def test_full_session_mode_is_consumed_by_one_mission(tmp_path: Path) -> None:
    """A sticky `full` session mode must not keep spending five roles forever."""
    server = _server(tmp_path)
    try:
        session_id = str(server.dispatch({"action": "session.ensure", "payload": {}})["session_id"])
        assert _configure(server, session_id, "full")["interaction_mode"] == "full"

        started = server.dispatch({
            "action": "prompt.submit",
            "payload": {
                "session_id": session_id,
                "objective": "Write one bounded local note file",
                "disposition": "start",
            },
        })
        assert started["interaction"] == "colony"
        assert started["forced"] == "full"
        assert started["full_pipeline_consumed"] is True

        server.dispatch({"action": "session.interrupt", "payload": {"session_id": session_id}})
        deadline = time.time() + 10.0
        while time.time() < deadline:
            if not server.dispatch({"action": "session.snapshot", "payload": {"session_id": session_id}}):
                break
            row = server.service.store.session(session_id) or {}
            if str(row.get("interaction_mode") or "") == "auto":
                break
            time.sleep(0.02)

        row = server.service.store.session(session_id) or {}
        assert row["interaction_mode"] == "auto", "full must fall back to auto after one mission"
    finally:
        server.dispatch({"action": "session.interrupt", "payload": {"session_id": session_id}})


def test_colony_mode_stays_sticky(tmp_path: Path) -> None:
    """Only `full` is one-shot; the cheap colony mode keeps its old behaviour."""
    server = _server(tmp_path)
    session_id = ""
    try:
        session_id = str(server.dispatch({"action": "session.ensure", "payload": {}})["session_id"])
        _configure(server, session_id, "colony")
        started = server.dispatch({
            "action": "prompt.submit",
            "payload": {
                "session_id": session_id,
                "objective": "Write one bounded local note file",
                "disposition": "start",
            },
        })
        assert "full_pipeline_consumed" not in started
        assert (server.service.store.session(session_id) or {})["interaction_mode"] == "colony"
    finally:
        if session_id:
            server.dispatch({"action": "session.interrupt", "payload": {"session_id": session_id}})
