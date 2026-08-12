import threading
from pathlib import Path

import pytest

from crabagent.codex_app_server import CodexAppServerError, CodexAppServerSession, CodexLiveTurn, can_fallback_to_session_default
from crabagent.conversation import ConversationExecutor, interaction_kind
from crabagent.identity import lecture_brand_context
from crabagent.store import ColonyStore


def test_auto_mode_keeps_conversation_out_of_colony_runtime() -> None:
    assert interaction_kind("안녕") == "chat"
    assert interaction_kind("아까 말한 내용을 조금 더 설명해줘") == "chat"
    assert interaction_kind("What did you mean by that?") == "chat"


def test_model_route_fallback_does_not_repeat_a_timed_out_turn() -> None:
    assert can_fallback_to_session_default("model gpt-5.6-sol is not available") is True
    assert can_fallback_to_session_default("Codex turn idle timed out after 1800s without an event") is False
    assert can_fallback_to_session_default("Codex server closed while the turn was active") is False


def test_auto_mode_routes_explicit_work_to_colony() -> None:
    assert interaction_kind("이 버그를 고쳐줘") == "colony"
    assert interaction_kind("Implement the login flow") == "colony"
    assert interaction_kind("배포해") == "colony"


def test_explicit_mode_overrides_heuristic() -> None:
    assert interaction_kind("안녕", "colony") == "colony"
    assert interaction_kind("이 기능을 구현해", "chat") == "chat"


def test_lecture_brand_mode_is_exactly_for_19_selected_packs() -> None:
    assert "CRABAGENT ACADEMY" in lecture_brand_context(19)
    assert "19 selected knowledge packs" in lecture_brand_context(19)
    assert lecture_brand_context(18) == ""


class FakeBridge:
    thread_id = "thread-persistent-1234"
    last_start_mode = "started"

    def start(self) -> str:
        return self.thread_id

    def run_turn(self, prompt, emit, **kwargs):
        self.last_start_mode = "resumed"
        return CodexLiveTurn(
            thread_id=self.thread_id,
            turn_id="turn-%s" % prompt,
            status="completed",
            text="reply:%s" % prompt,
            usage={"last": {"totalTokens": 10}},
            started_at="now",
            finished_at="now",
        )


def test_19_selected_packs_reach_the_model_as_grounded_brand_context(tmp_path: Path) -> None:
    class CapturingBridge(FakeBridge):
        prompt = ""

        def run_turn(self, prompt, emit, **kwargs):
            self.prompt = prompt
            return super().run_turn(prompt, emit, **kwargs)

    store = ColonyStore(tmp_path)
    session = store.create_session(interaction_mode="chat")
    bridge = CapturingBridge()

    result = ConversationExecutor(store, session["session_id"], bridge, threading.Event()).run(
        "AI 강의 브랜드를 만들어줘",
        {"package_ids": ["pack-%02d" % index for index in range(19)]},
    )

    assert result["status"] == "completed"
    assert "[CRABAGENT ACADEMY BRAND MODE]" in bridge.prompt
    assert "do not infer individual pack contents" in bridge.prompt


def test_two_chat_turns_reuse_one_persisted_codex_thread(tmp_path: Path) -> None:
    store = ColonyStore(tmp_path)
    session = store.create_session(interaction_mode="chat")
    bridge = FakeBridge()
    for prompt in ("첫 질문", "그 내용 이어서"):
        result = ConversationExecutor(store, session["session_id"], bridge, threading.Event()).run(prompt)
        assert result["thread_id"] == bridge.thread_id
    reopened = ColonyStore(tmp_path)
    assert reopened.session(session["session_id"])["codex_thread_id"] == bridge.thread_id
    assert [row["content"] for row in reopened.messages(session["session_id"])] == [
        "첫 질문",
        "reply:첫 질문",
        "그 내용 이어서",
        "reply:그 내용 이어서",
    ]


class EmptyStream:
    def __iter__(self):
        return iter(())


class FakeProcess:
    stdin = object()
    stdout = EmptyStream()
    stderr = EmptyStream()

    def poll(self):
        return None


def test_resume_failure_never_silently_starts_a_new_thread(tmp_path: Path) -> None:
    methods = []
    session = CodexAppServerSession("codex", tmp_path, thread_id="thread-existing", popen_factory=lambda *a, **k: FakeProcess())
    session._send = lambda payload: None

    def request(method, params, timeout=30):
        methods.append(method)
        if method == "initialize":
            return {}
        if method == "thread/resume":
            raise CodexAppServerError("resume denied")
        raise AssertionError(method)

    session._request = request
    with pytest.raises(CodexAppServerError, match="refused to create a silent replacement"):
        session.start()
    assert methods == ["initialize", "thread/resume"]


def test_mcp_allowlist_becomes_process_level_codex_overrides(tmp_path: Path) -> None:
    session = CodexAppServerSession(
        "codex",
        tmp_path,
        mcp_policy="allow:OpenCrab",
        mcp_servers=["OpenCrab", "Neo4j", "crab-archi-design"],
    )

    command = session._app_server_command()

    assert "mcp_servers.OpenCrab.enabled=true" in command
    assert "mcp_servers.Neo4j.enabled=false" in command
    assert "mcp_servers.crab-archi-design.enabled=false" in command


def test_mcp_auto_preserves_global_codex_configuration(tmp_path: Path) -> None:
    session = CodexAppServerSession(
        "codex",
        tmp_path,
        mcp_policy="auto",
        mcp_servers=["OpenCrab"],
    )

    assert session._app_server_command() == ["codex", "app-server", "--stdio"]


def test_codex_child_environment_uses_writable_python_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PYTHONPYCACHEPREFIX", raising=False)
    session = CodexAppServerSession("codex", tmp_path)

    assert session._child_environment()["PYTHONPYCACHEPREFIX"] == "/tmp/crabagent-pycache"


def test_codex_turn_reports_timeout_separately_from_session_close(tmp_path: Path) -> None:
    session = CodexAppServerSession("codex", tmp_path)
    session.start = lambda: "thread-timeout"
    session._request = lambda method, params, timeout=30: {"turn": {"id": "turn-timeout"}}
    session.interrupt = lambda: None

    result = session.run_turn("wait", lambda event: None, timeout_seconds=0.01)

    assert result.status == "failed"
    assert result.error == "Codex turn hard timed out after 0.01s"


def test_codex_turn_can_use_idle_timeout_without_absolute_deadline(tmp_path: Path) -> None:
    session = CodexAppServerSession("codex", tmp_path)
    session.start = lambda: "thread-idle"
    session._request = lambda method, params, timeout=30: {"turn": {"id": "turn-idle"}}
    session.interrupt = lambda: None

    result = session.run_turn("wait", lambda event: None, idle_timeout_seconds=0.01)

    assert result.status == "failed"
    assert result.error == "Codex turn idle timed out after 0.01s without an event"


def test_codex_thread_fork_returns_independent_context_id(tmp_path: Path) -> None:
    session = CodexAppServerSession("codex", tmp_path, thread_id="thread-source")
    session.start = lambda: "thread-source"
    session._request = lambda method, params: {
        "thread": {"id": "thread-forked", "forkedFromId": params["threadId"]}
    }

    assert session.fork_thread() == "thread-forked"
