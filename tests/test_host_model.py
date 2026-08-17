from __future__ import annotations

import threading
import time
from pathlib import Path

from crabagent.host_model import HostModelSession
from crabagent.runtime import RuntimeService


def _wait_for_event(events: list, timeout: float = 2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if events:
            return events[0]
        time.sleep(0.01)
    raise AssertionError("host model runtime request was not emitted")


def test_host_model_session_round_trip_uses_host_result() -> None:
    bridge = HostModelSession()
    events = []
    observed = {}

    def run() -> None:
        observed["turn"] = bridge.run_turn(
            "KING role prompt",
            events.append,
            model="current-model",
            effort="high",
            timeout_seconds=2.0,
        )

    worker = threading.Thread(target=run)
    worker.start()
    event = _wait_for_event(events)
    assert event.kind == "runtime_request"
    assert event.data["method"] == "hostModel/turn"
    assert event.data["params"]["prompt"] == "KING role prompt"
    request_id = event.data["request_id"]

    bridge.respond(
        request_id,
        {
            "text": "host-model answer",
            "model": "gpt-5.6-sol",
            "usage": {"totalTokens": 321},
        },
    )
    worker.join(timeout=2.0)
    assert not worker.is_alive()
    turn = observed["turn"]
    assert turn.status == "completed"
    assert turn.text == "host-model answer"
    assert turn.usage == {"totalTokens": 321}
    assert bridge.last_reported_model == "gpt-5.6-sol"
    assert bridge.executor_name == "host_current_model"
    assert bridge.supports_session_fallback is False


def test_host_model_session_requires_nonempty_text() -> None:
    bridge = HostModelSession()
    events = []
    observed = {}

    def run() -> None:
        observed["turn"] = bridge.run_turn("ORACLE prompt", events.append, timeout_seconds=2.0)

    worker = threading.Thread(target=run)
    worker.start()
    event = _wait_for_event(events)
    bridge.respond(event.data["request_id"], {"text": "", "model": "gpt-5.6-sol"})
    worker.join(timeout=2.0)
    assert observed["turn"].status == "failed"
    assert "response text is required" in observed["turn"].error


def test_host_model_session_interrupt_cancels_waiting_turn() -> None:
    bridge = HostModelSession()
    events = []
    observed = {}

    def run() -> None:
        observed["turn"] = bridge.run_turn("KING wait prompt", events.append, timeout_seconds=5.0)

    worker = threading.Thread(target=run)
    worker.start()
    _wait_for_event(events)
    bridge.interrupt()
    worker.join(timeout=2.0)
    assert not worker.is_alive()
    assert observed["turn"].status == "cancelled"
    assert "interrupted" in observed["turn"].error


def test_session_executor_policy_defaults_codex_and_can_be_host(tmp_path: Path) -> None:
    service = RuntimeService(tmp_path)
    default_session = service.store.create_session()
    host_session = service.store.create_session(model_policy="host", executor_policy="host")

    assert default_session["executor_policy"] == "codex"
    assert host_session["model_policy"] == "host"
    assert host_session["executor_policy"] == "host"

    updated = service.store.update_session(default_session["session_id"], executor_policy="host")
    assert updated["executor_policy"] == "host"
