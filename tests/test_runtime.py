import sqlite3
from pathlib import Path

import pytest

from crabagent.models import MissionStatus, Role, WorkerPolicy, worker_capacity
from crabagent.colony import ColonyExecutor, _parse_host_worker_artifact_directive, _usage_total
from crabagent.runtime import RuntimeService
from crabagent.store import ColonyStore


EXPECTED_TABLES = {
    "missions",
    "task_slots",
    "role_assignments",
    "attempts",
    "leases",
    "events",
    "artifacts",
    "evidence_refs",
    "approvals",
    "checkpoints",
    "budgets",
    "tool_receipts",
    "sessions",
    "projects",
    "messages",
    "input_queue",
    "runtime_requests",
    "integration_cache",
    "session_ontology_context",
}


def test_host_worker_artifact_directive_is_strict_and_bounded() -> None:
    cleaned, spec = _parse_host_worker_artifact_directive(
        "RESULT: verified\nHOST_WORKER_ARTIFACT_V1:"
        + __import__("json").dumps(
            {"relative_path": ".crabagent/artifacts/canary.md", "content": "hello\n"},
            separators=(",", ":"),
        )
    )
    assert cleaned == "RESULT: verified"
    assert spec is not None
    assert spec["relative_path"] == ".crabagent/artifacts/canary.md"
    assert spec["bytes"] == 6
    assert len(spec["sha256"]) == 64

    invalid = [
        'HOST_WORKER_ARTIFACT_V1:{"relative_path":"../escape.md","content":"x"}',
        'HOST_WORKER_ARTIFACT_V1:{"relative_path":".crabagent/artifacts/nested/x.md","content":"x"}',
        'HOST_WORKER_ARTIFACT_V1:{"relative_path":".crabagent/artifacts/run.py","content":"x"}',
        'HOST_WORKER_ARTIFACT_V1:{"relative_path":".crabagent/artifacts/mission-hidden.md","content":"x"}',
        'HOST_WORKER_ARTIFACT_V1:{"relative_path":".crabagent/artifacts/a.md","content":"x"}\nHOST_WORKER_ARTIFACT_V1:{"relative_path":".crabagent/artifacts/b.md","content":"y"}',
    ]
    for text in invalid:
        with pytest.raises(ValueError):
            _parse_host_worker_artifact_directive(text)
    oversized = 'x' * (16 * 1024 + 1)
    with pytest.raises(ValueError, match="16 KiB"):
        _parse_host_worker_artifact_directive(
            'HOST_WORKER_ARTIFACT_V1:' + __import__("json").dumps({"relative_path": ".crabagent/artifacts/too-big.txt", "content": oversized})
        )


def test_host_worker_artifact_directive_accepts_indentation_fences_and_korean_text() -> None:
    payload = __import__("json").dumps(
        {
            "relative_path": ".crabagent/artifacts/korean-verification.md",
            "content": "실물 검증 결과: ledger readback 통과\n",
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    cleaned, spec = _parse_host_worker_artifact_directive(
        "검증을 마쳤습니다.\n```json\n  HOST_WORKER_ARTIFACT_V1:" + payload + "\n```\n다음 단계는 Oracle 확인입니다."
    )
    assert cleaned == "검증을 마쳤습니다.\n다음 단계는 Oracle 확인입니다."
    assert spec is not None
    assert spec["content"].startswith("실물 검증 결과")

    one_line_cleaned, one_line_spec = _parse_host_worker_artifact_directive(
        "  ```HOST_WORKER_ARTIFACT_V1:" + payload + "```  "
    )
    assert one_line_cleaned == "Bounded host-worker artifact prepared."
    assert one_line_spec is not None

    with pytest.raises(ValueError, match="exactly one"):
        _parse_host_worker_artifact_directive(
            "```\nHOST_WORKER_ARTIFACT_V1:" + payload + "\n```\n"
            "HOST_WORKER_ARTIFACT_V1:" + payload + "\n"
        )


def test_initialize_creates_all_durable_contract_tables(tmp_path: Path) -> None:
    service = RuntimeService(tmp_path)
    result = service.initialize()
    assert Path(result["database"]).exists()
    with sqlite3.connect(result["database"]) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert EXPECTED_TABLES <= tables


def test_planned_mission_does_not_claim_model_invocation(tmp_path: Path) -> None:
    snapshot = RuntimeService(tmp_path).plan_mission("Plan a real agent kernel")
    assert snapshot["mission"]["status"] == "planned"
    assert len(snapshot["assignments"]) == 5
    assert {row["invocation_status"] for row in snapshot["assignments"]} == {"planned"}
    assert snapshot["attempts"] == []
    assert snapshot["tool_receipts"] == []


def test_demo_runs_full_persisted_lifecycle(tmp_path: Path) -> None:
    snapshot = RuntimeService(tmp_path).run_demo("Produce a verified local result")
    mission = snapshot["mission"]
    assert mission["status"] == "completed"
    assert mission["oracle_result_artifact_id"]
    assert len(snapshot["tasks"]) == 5
    assert len(snapshot["attempts"]) == 5
    assert len(snapshot["leases"]) == 5
    assert len(snapshot["artifacts"]) == 5
    assert len(snapshot["tool_receipts"]) == 5
    assert all(row["executor"] == "deterministic_demo" for row in snapshot["attempts"])
    assert all(row["status"] == "released" for row in snapshot["leases"])
    assert all(row["status"] == "accepted" for row in snapshot["artifacts"])
    assert snapshot["budget"]["tokens_observed"] == 0
    assert snapshot["budget"]["cost_observed"] == 0.0


def test_demo_survives_fresh_store_process_boundary(tmp_path: Path) -> None:
    first = RuntimeService(tmp_path).run_demo("Persist this mission")
    mission_id = first["mission"]["mission_id"]
    reopened = ColonyStore(tmp_path).inspect(mission_id)
    assert reopened["mission"]["status"] == "completed"
    assert len(reopened["events"] if "events" in reopened else []) == 0
    assert len(ColonyStore(tmp_path).events(mission_id)) >= 30
    oracle = Path(
        next(row["path"] for row in reopened["artifacts"] if row["kind"] == "oracle_result")
    )
    assert oracle.exists()
    assert "Verdict: accepted" in oracle.read_text(encoding="utf-8")


def test_demo_attaches_last_role_result_to_session(tmp_path: Path) -> None:
    service = RuntimeService(tmp_path)
    session = service.store.create_session(title="Demo panel")
    snapshot = service.run_demo("Show the panel a completed colony", session_id=session["session_id"])

    reopened = ColonyStore(tmp_path)
    current = reopened.session(session["session_id"])
    overview = reopened.colony_overview()
    row = next(item for item in overview["sessions"] if item["session_id"] == session["session_id"])

    assert snapshot["mission"]["session_id"] == session["session_id"]
    assert current["status"] == "ready"
    assert current["active_mission_id"] is None
    assert row["last_mission"]["mission_id"] == snapshot["mission"]["mission_id"]
    assert row["last_mission"]["status"] == "completed"
    assert row["roles"]["KING"]["status"] == "completed"
    assert row["roles"]["QUEEN"]["status"] == "completed"
    assert row["roles"]["SOLDIER"]["status"] == "completed"
    assert row["roles"]["WORKER"]["status"] == "completed"
    assert row["roles"]["ORACLE"]["status"] == "verified"


def test_invalid_mission_transition_is_rejected(tmp_path: Path) -> None:
    service = RuntimeService(tmp_path)
    snapshot = service.plan_mission("Reject invalid state changes")
    mission_id = snapshot["mission"]["mission_id"]
    with pytest.raises(ValueError, match="invalid mission transition"):
        service.store.transition_mission(mission_id, MissionStatus.COMPLETED, Role.ORACLE)


def test_session_messages_and_wait_queue_survive_reopen(tmp_path: Path) -> None:
    store = ColonyStore(tmp_path)
    session = store.create_session(max_workers=4)
    store.add_message(session["session_id"], "user", "continue after current work")
    queued = store.enqueue_input(session["session_id"], "next mission", "wait")
    reopened = ColonyStore(tmp_path)
    assert reopened.session(session["session_id"])["max_workers"] == 4
    assert reopened.messages(session["session_id"])[0]["content"] == "continue after current work"
    assert reopened.queued_inputs(session["session_id"])[0]["queue_id"] == queued["queue_id"]


def test_selected_ontology_context_survives_fresh_store_process(tmp_path: Path) -> None:
    store = ColonyStore(tmp_path)
    session = store.create_session()
    context = store.set_ontology_context(
        session["session_id"],
        ["project-live"],
        ["pack-live"],
        {"project-live": "ALEXAI"},
        {"pack-live": "Fable5xGLM5.2"},
    )
    reopened = ColonyStore(tmp_path)
    assert reopened.ontology_context(session["session_id"]) == context
    assert reopened.session(session["session_id"])["ontology_context_count"] == 1


def test_session_fork_copies_conversation_without_runtime_or_mission_state(tmp_path: Path) -> None:
    store = ColonyStore(tmp_path)
    source = store.create_session(title="Original", interaction_mode="chat")
    store.update_session(source["session_id"], codex_thread_id="thread-source", status="ready")
    store.add_message(source["session_id"], "user", "source question")
    store.add_message(source["session_id"], "assistant", "source answer", metadata={"interaction": "chat"})

    fork = store.fork_session(source["session_id"], codex_thread_id="thread-forked")
    branch = fork["session"]

    assert fork["copied_messages"] == 2
    assert branch["session_id"] != source["session_id"]
    assert branch["status"] == "ready"
    assert branch["codex_thread_id"] == "thread-forked"
    assert fork["codex_context_forked"] is True
    assert branch["active_mission_id"] is None
    copied = store.messages(branch["session_id"])
    assert [row["content"] for row in copied] == ["source question", "source answer"]
    assert copied[0]["metadata"]["forked_from_session"] == source["session_id"]


def test_status_prioritizes_latest_persistent_chat_state(tmp_path: Path) -> None:
    service = RuntimeService(tmp_path)
    service.initialize()
    session = service.store.create_session()
    service.store.update_session(session["session_id"], codex_thread_id="thread-123")
    service.store.add_message(
        session["session_id"],
        "assistant",
        "connected",
        metadata={"interaction": "chat", "thread_id": "thread-123"},
    )

    status = service.status()

    assert status["latest_session"]["session_id"] == session["session_id"]
    assert status["latest_message"]["metadata"]["interaction"] == "chat"


def test_colony_overview_keeps_project_and_queen_context_observed(tmp_path: Path) -> None:
    store = ColonyStore(tmp_path)
    session = store.create_session(title="Initial")
    store.update_session(
        session["session_id"],
        title="OpenCrab MCP Panel",
        project_id="project-1",
        project_name="ALEXAI",
        ontology_context_count=87,
    )

    overview = store.colony_overview()

    assert overview["active_workers"] == 0
    assert overview["sessions"][0]["project_name"] == "ALEXAI"
    assert overview["sessions"][0]["queen_ontology_count"] == 87


def test_worker_policy_is_not_derived_from_ontology_pack_count(tmp_path: Path) -> None:
    store = ColonyStore(tmp_path)
    auto = store.create_session(worker_policy=WorkerPolicy.AUTO.value, max_workers=3)
    fixed = store.create_session(worker_policy=WorkerPolicy.FIXED.value, max_workers=3)
    assert auto["worker_policy"] == "auto"
    assert fixed["worker_policy"] == "fixed"
    assert worker_capacity("auto", 3, 4) == 4
    assert worker_capacity("fixed", 3, 40) == 3
    assert worker_capacity("auto", 3, 40) == 8


def test_projects_are_durable_and_reassign_sessions_on_rename_or_delete(tmp_path: Path) -> None:
    store = ColonyStore(tmp_path)
    session = store.create_session(title="Project session")
    project = store.create_project("Launch")
    assigned = store.update_session(
        session["session_id"],
        project_id=project["project_id"],
        project_name=project["name"],
        ontology_context_count=12,
    )
    assert assigned["project_name"] == "Launch"
    renamed = store.rename_project(project["project_id"], "Launch v2")
    assert renamed["name"] == "Launch v2"
    assert store.session(session["session_id"])["project_name"] == "Launch v2"
    deleted = store.delete_project(project["project_id"])
    assert deleted["name"] == "Launch v2"
    cleared = store.session(session["session_id"])
    assert cleared["project_id"] == ""
    assert cleared["project_name"] == ""
    assert cleared["ontology_context_count"] == 0


def test_role_gate_avoids_planning_calls_for_clear_small_work() -> None:
    objective = "Create hello.txt with one exact line"
    assert ColonyExecutor._requires_model(Role.KING, objective) is False
    assert ColonyExecutor._requires_model(Role.QUEEN, objective) is False
    assert ColonyExecutor._requires_model(Role.KING, "Design a production database migration") is True
    assert ColonyExecutor._requires_model(Role.QUEEN, "Build an OpenCrab ontology schema") is True


def test_usage_meter_uses_last_turn_not_cumulative_thread_total() -> None:
    usage = {"total": {"totalTokens": 700000}, "last": {"totalTokens": 1234}, "modelContextWindow": 258400}
    assert _usage_total(usage) == 1234


def test_runtime_restart_reconciles_unobservable_running_state(tmp_path: Path) -> None:
    service = RuntimeService(tmp_path)
    session = service.store.create_session()
    snapshot = service.plan_mission("Interrupted mission", session_id=session["session_id"])
    mission_id = snapshot["mission"]["mission_id"]
    service.store.transition_mission(mission_id, MissionStatus.RUNNING, Role.KING)
    service.store.update_session(session["session_id"], status="running", active_mission_id=mission_id)
    assert service.store.reconcile_interrupted_runtime() == 1
    assert service.store.session(session["session_id"])["status"] == "ready"
    assert service.store.inspect(mission_id)["mission"]["status"] == "cancelled"
