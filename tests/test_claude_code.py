import io
import json
from pathlib import Path
from typing import Any, List

from crabagent import daemon
from crabagent.claude_code import ClaudeCodeSession, is_claude_thread


class FakeProcess:
    def __init__(self, events: List[dict], returncode: int = 0) -> None:
        self.stdin = io.StringIO()
        self.stdout = io.StringIO("".join(json.dumps(event) + "\n" for event in events))
        self.stderr = io.StringIO("")
        self.returncode = returncode

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        pass

    def kill(self):
        pass


class FakePopen:
    def __init__(self, events: List[dict]) -> None:
        self.events = events
        self.commands: List[List[str]] = []

    def __call__(self, command, **kwargs):
        self.commands.append(list(command))
        return FakeProcess(self.events)


EVENTS = [
    {"type": "system", "subtype": "init", "session_id": "s-123"},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "Plan: "}, {"type": "tool_use", "name": "Read"}]}},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "done."}]}},
    {"type": "result", "subtype": "success", "is_error": False, "result": "Plan: done.", "session_id": "s-123", "usage": {"input_tokens": 10, "output_tokens": 4}},
]


def test_turn_streams_text_activity_and_usage_and_resumes(tmp_path: Path) -> None:
    popen = FakePopen(EVENTS)
    session = ClaudeCodeSession("claude", tmp_path, popen_factory=popen)
    events: List[Any] = []
    turn = session.run_turn("hello", events.append, model="gpt-5.6-luna")

    assert turn.status == "completed"
    assert turn.text == "Plan: done."
    assert turn.usage["output_tokens"] == 4
    assert session.thread_id == "claude:s-123"
    kinds = [event.kind for event in events]
    assert kinds.count("assistant_delta") == 2 and "activity" in kinds and "usage" in kinds
    first = popen.commands[0]
    assert "--session-id" in first and "--model" not in first

    session.run_turn("again", lambda _e: None, model="sonnet")
    second = popen.commands[1]
    assert second[second.index("--resume") + 1] == "s-123"
    assert second[second.index("--model") + 1] == "sonnet"


def test_error_result_is_a_failed_turn(tmp_path: Path) -> None:
    popen = FakePopen([{"type": "result", "subtype": "error_max_turns", "is_error": True, "result": "limit"}])
    turn = ClaudeCodeSession("claude", tmp_path, popen_factory=popen).run_turn("x", lambda _e: None)
    assert turn.status == "failed"
    assert turn.error == "limit"


def test_codex_threads_are_not_resumed_by_claude(tmp_path: Path) -> None:
    assert not is_claude_thread("thr_codex")
    session = ClaudeCodeSession("claude", tmp_path, thread_id="claude:abc", popen_factory=FakePopen(EVENTS))
    assert session.thread_id == "claude:abc"
    session.run_turn("x", lambda _e: None)
    assert "--resume" in session._popen_factory.commands[0]


def test_mcp_off_uses_an_empty_strict_config(tmp_path: Path) -> None:
    popen = FakePopen(EVENTS)
    ClaudeCodeSession("claude", tmp_path, mcp_policy="off", popen_factory=popen).run_turn("x", lambda _e: None)
    assert "--strict-mcp-config" in popen.commands[0]


def test_provider_selection_order(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(daemon, "provider_file", lambda: tmp_path / "provider")
    found = {"codex": "/bin/codex", "claude": "/bin/claude"}
    monkeypatch.setattr(daemon.shutil, "which", lambda name: found.get(name))
    monkeypatch.delenv("CRAB_PROVIDER", raising=False)
    assert daemon.select_provider()[0] == "codex"
    (tmp_path / "provider").write_text("claude\n", encoding="utf-8")
    assert daemon.select_provider()[0] == "claude"
    monkeypatch.setenv("CRAB_PROVIDER", "codex")
    assert daemon.select_provider()[0] == "codex"
    assert daemon.select_provider("claude")[0] == "claude"
    found.pop("codex")
    assert daemon.select_provider("codex")[0] == "claude"
