import json
import threading
import time
from pathlib import Path

import pytest

from crabagent.daemon import RuntimeServer, _king_title, _mcp_policy_with, _mission_summary
from crabagent.models import MissionContract, MissionStatus, Role, TaskSlot
from crabagent.protocol import DaemonClient, USE_UNIX_SOCKET, runtime_paths, start_daemon


def test_runtime_uses_a_supported_local_transport() -> None:
    paths = runtime_paths(Path.cwd())
    assert paths["transport"] in {"unix", "tcp"}
    if USE_UNIX_SOCKET:
        assert paths["transport"] == "unix"
    else:
        assert paths["transport"] == "tcp"
        assert 45000 <= paths["port"] < 55000
        assert paths["endpoint"].name == "crabd.endpoint.json"


def test_session_ensure_rehomes_legacy_unassigned_tb_root(tmp_path: Path) -> None:
    paths = runtime_paths(tmp_path)
    paths["root"].mkdir(parents=True, exist_ok=True)
    server = RuntimeServer(tmp_path, paths["socket"])
    try:
        server.service.store.create_session(
            project_root_path=str(tmp_path / "scratch" / "FINAL-Bench-TB-S1"),
        )
        session = server.dispatch({"action": "session.ensure", "payload": {}})
        assert session["project_root_path"] == str(tmp_path.resolve())
    finally:
        server.server_close()
        if paths["socket"].exists():
            paths["socket"].unlink()


def test_daemon_protocol_runs_demo_and_replays_receipts(tmp_path: Path) -> None:
    paths = runtime_paths(tmp_path)
    paths["root"].mkdir(parents=True, exist_ok=True)
    if paths["socket"].exists():
        paths["socket"].unlink()
    server = RuntimeServer(tmp_path, paths["socket"])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = DaemonClient(tmp_path)
        assert client.ping()
        hello = client.request("ping")
        assert hello["runtime_api"] == "kingcrab-runtime/1"
        assert set(hello["capabilities"]) >= {
            "mission.list",
            "mission.summary",
            "mission.pending_requests",
        }
        session = client.request("session.ensure")
        configured = client.request(
            "session.configure",
            session_id=session["session_id"],
            model_policy="luna",
            interaction_mode="chat",
            mcp_policy="off",
            max_workers=1,
        )
        assert configured["model_policy"] == "luna"
        assert configured["interaction_mode"] == "chat"
        assert configured["mcp_policy"] == "off"
        assert configured["max_workers"] == 1
        result = client.request(
            "run_demo",
            objective="Exercise daemon persistence",
            session_id=session["session_id"],
        )
        mission_id = result["mission"]["mission_id"]
        listed = client.request("mission.list", session_id=session["session_id"], limit=10)
        assert listed["missions"][0]["mission_id"] == mission_id
        summary = client.request("mission.summary", mission_id=mission_id)
        assert summary["mission"]["mission_id"] == mission_id
        assert set(summary["mission"]) <= {
            "mission_id", "objective", "status", "risk",
            "session_id", "execution_scope", "created_at", "updated_at",
        }
        pending = client.request("mission.pending_requests", mission_id=mission_id)
        assert pending == {"requests": []}
        replay = client.request("replay", mission_id=mission_id)
        assert result["mission"]["status"] == "completed"
        snapshot = client.request(
            "ui.snapshot",
            session_id=session["session_id"],
            after_message_id=0,
        )
        assert snapshot["session"]["session_id"] == session["session_id"]
        assert "colony" in snapshot
        assert "structural" in snapshot["quality"]
        assert "goal_contract" in snapshot["quality"]
        assert any(row["session_id"] == session["session_id"] for row in snapshot["colony"]["sessions"])
        overview = client.request("colony.overview")
        row = next(item for item in overview["sessions"] if item["session_id"] == session["session_id"])
        assert row["last_mission"]["mission_id"] == mission_id
        assert row["roles"]["ORACLE"]["status"] == "verified"
        assert any(event["event_type"] == "tool_receipt_recorded" for event in replay["events"])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        if paths["socket"].exists():
            paths["socket"].unlink()


def test_runtime_serves_ping_while_another_request_is_blocked(tmp_path: Path, monkeypatch) -> None:
    paths = runtime_paths(tmp_path)
    paths["root"].mkdir(parents=True, exist_ok=True)
    if paths["socket"].exists():
        paths["socket"].unlink()
    server = RuntimeServer(tmp_path, paths["socket"])
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    slow_started = threading.Event()
    release_slow = threading.Event()
    slow_result = {}
    original_dispatch = server.dispatch

    def dispatch(request):
        if request.get("action") == "test.blocking":
            slow_started.set()
            if not release_slow.wait(timeout=2.0):
                raise TimeoutError("test did not release blocking request")
            return {"status": "released"}
        return original_dispatch(request)

    monkeypatch.setattr(server, "dispatch", dispatch)
    server_thread.start()

    def run_slow_request() -> None:
        slow_result.update(DaemonClient(tmp_path, timeout=3.0).request("test.blocking"))

    slow_thread = threading.Thread(target=run_slow_request, daemon=True)
    try:
        slow_thread.start()
        assert slow_started.wait(timeout=1.0)
        started = time.monotonic()
        ping = DaemonClient(tmp_path, timeout=0.75).request("ping")
        elapsed = time.monotonic() - started

        assert ping["status"] == "online"
        assert elapsed < 1.0
        release_slow.set()
        slow_thread.join(timeout=1.0)
        assert slow_result == {"status": "released"}
    finally:
        release_slow.set()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2.0)
        slow_thread.join(timeout=1.0)
        if paths["socket"].exists():
            paths["socket"].unlink()


def test_mission_summary_exposes_execution_scope_for_runtime_readback() -> None:
    summary = _mission_summary({"mission_id": "mission-scope", "execution_scope": "local_only"})

    assert summary["execution_scope"] == "local_only"


def test_host_executor_runs_durable_model_turns_without_codex(tmp_path: Path) -> None:
    paths = runtime_paths(tmp_path)
    paths["root"].mkdir(parents=True, exist_ok=True)
    server = RuntimeServer(tmp_path, paths["socket"])
    try:
        session = server.dispatch({"action": "session.ensure", "payload": {}})
        assert session["project_root_path"] == str(tmp_path.resolve())
        configured = server.dispatch({
            "action": "session.configure",
            "payload": {
                "session_id": session["session_id"],
                "model_policy": "host",
                "executor_policy": "host",
                "interaction_mode": "colony",
                "mcp_policy": "off",
                "max_workers": 1,
            },
        })
        assert configured["model_policy"] == "host"
        assert configured["executor_policy"] == "host"
        hello = server.dispatch({"action": "ping", "payload": {}})
        assert "mission.host_turn" in hello["capabilities"]

        started = server.dispatch({
            "action": "prompt.submit",
            "payload": {
                "session_id": session["session_id"],
                "objective": "Create exactly one mission-local artifact .crabagent/artifacts/bounded_note.txt with one line hello",
                "disposition": "start",
                "interaction": "colony",
            },
        })
        mission_id = str(started.get("mission_id") or "")
        deadline = time.time() + 10.0
        while not mission_id and time.time() < deadline:
            rows = server.dispatch({
                "action": "mission.list",
                "payload": {"session_id": session["session_id"], "limit": 5},
            })["missions"]
            if rows:
                mission_id = str(rows[0]["mission_id"])
                break
            time.sleep(0.02)
        assert mission_id

        answered = 0
        while time.time() < deadline:
            summary = server.dispatch({"action": "mission.summary", "payload": {"mission_id": mission_id}})["mission"]
            if summary["status"] in {"completed", "failed", "cancelled"}:
                break
            host = server.dispatch({"action": "mission.host_turn", "payload": {"mission_id": mission_id}})["turn"]
            if host:
                assert host["payload"]["params"]["workspace"] == str(tmp_path.resolve())
                assert host["payload"]["params"]["projectRoot"] == str(tmp_path.resolve())
                response_text = "bounded host-model result"
                if str(host.get("prompt") or "").startswith("CRABAGENT ROLE: WORKER"):
                    assert "HOST WORKER ARTIFACT CONTRACT" in host["prompt"]
                    response_text += "\nHOST_WORKER_ARTIFACT_V1:" + json.dumps(
                        {
                            "relative_path": ".crabagent/artifacts/bounded_note.txt",
                            "content": "hello\n",
                        },
                        separators=(",", ":"),
                    )
                server.dispatch({
                    "action": "runtime.respond",
                    "payload": {
                        "session_id": session["session_id"],
                        "request_id": host["request_id"],
                        "approve": True,
                        "result": {
                            "text": response_text,
                            "model": "gpt-5.6-sol",
                            "usage": {"totalTokens": 32},
                        },
                    },
                })
                answered += 1
            else:
                time.sleep(0.02)

        snapshot = server.service.store.inspect(mission_id)
        diagnostic_events = [
            (row.event_type, row.actor, row.payload)
            for row in server.service.store.events(mission_id)
            if row.event_type in {"mission_cancelled", "soldier_stop_gate", "host_model_execution_required"}
        ]
        assert snapshot["mission"]["status"] == "completed", {
            "status": snapshot["mission"]["status"],
            "tasks": [(row["role"], row["status"]) for row in snapshot["tasks"]],
            "executors": [row["executor"] for row in snapshot["attempts"]],
            "events": diagnostic_events,
        }
        assert answered >= 1
        assert snapshot["attempts"]
        assert any(row["executor"] == "host_current_model" for row in snapshot["attempts"])
        assert not any(row["executor"] == "codex_app_server" for row in snapshot["attempts"])
        assert any(row["tool_name"] == "host.current-model.turn" for row in snapshot["tool_receipts"])
        artifact_path = tmp_path / ".crabagent" / "artifacts" / "bounded_note.txt"
        assert artifact_path.read_text(encoding="utf-8") == "hello\n"
        write_receipts = [row for row in snapshot["tool_receipts"] if row["tool_name"] == "host.worker.artifact.write"]
        assert len(write_receipts) == 1
        assert write_receipts[0]["observed"]["relative_path"] == ".crabagent/artifacts/bounded_note.txt"
        assert write_receipts[0]["observed"]["bytes"] == 6
        assert write_receipts[0]["observed"]["mode"] == "exclusive_create"
        assert any(row["tool_name"] == "crab.workspace.diff" and row["observed"].get("changed_file_count") == 1 for row in snapshot["tool_receipts"])
    finally:
        server.server_close()
        if paths["socket"].exists():
            paths["socket"].unlink()


def test_local_only_scope_crosses_daemon_preview_and_dispatch_boundary(tmp_path: Path, monkeypatch) -> None:
    paths = runtime_paths(tmp_path)
    paths["root"].mkdir(parents=True, exist_ok=True)
    server = RuntimeServer(tmp_path, paths["socket"])
    captured = {}
    objective = (
        "Blender에서 로컬 장면 파일을 만들고 저장해. "
        "OpenCrab 근거 조회 없이 외부 서비스 없이 로컬 작업만 수행해."
    )

    def fake_launch(self, session, prompt, **kwargs):
        captured.update(kwargs)
        return {
            "session_id": session["session_id"],
            "status": "starting",
            "interaction": "colony",
            "execution_scope": kwargs.get("execution_scope"),
        }

    monkeypatch.setattr(RuntimeServer, "_launch_colony", fake_launch)
    try:
        session = server.dispatch({"action": "session.ensure", "payload": {}})
        server.dispatch({
            "action": "ontology.context",
            "payload": {
                "session_id": session["session_id"],
                "project_ids": ["project-selected"],
                "package_ids": ["package-selected"],
            },
        })
        preview = server.dispatch({
            "action": "goal.preview",
            "payload": {
                "session_id": session["session_id"],
                "objective": objective,
                "execution_scope": "local_only",
            },
        })["plan"]
        assert preview["execution_scope"] == "local_only"
        assert preview["selected_pack_count"] == 0
        assert preview["selected_project_count"] == 0
        assert preview["stages"] == ["KING", "WORKER", "ORACLE"]

        started = server.dispatch({
            "action": "prompt.submit",
            "payload": {
                "session_id": session["session_id"],
                "objective": objective,
                "disposition": "start",
                "interaction": "colony",
                "execution_scope": "local_only",
            },
        })
        assert started["execution_scope"] == "local_only"
        assert captured["execution_scope"] == "local_only"
    finally:
        server.server_close()
        if paths["socket"].exists():
            paths["socket"].unlink()


def test_local_only_scope_survives_unix_socket_protocol_round_trip(tmp_path: Path, monkeypatch) -> None:
    paths = runtime_paths(tmp_path)
    paths["root"].mkdir(parents=True, exist_ok=True)
    if paths["socket"].exists():
        paths["socket"].unlink()
    server = RuntimeServer(tmp_path, paths["socket"])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    objective = "로컬 파일을 만들고 저장해. OpenCrab 근거 조회 없이 외부 서비스 없이 진행해."
    captured = {}

    def fake_launch(self, session, prompt, **kwargs):
        captured.update(kwargs)
        return {"session_id": session["session_id"], "status": "starting", "interaction": "colony", **kwargs}

    monkeypatch.setattr(RuntimeServer, "_launch_colony", fake_launch)
    try:
        client = DaemonClient(tmp_path)
        session = client.request("session.ensure")
        client.request("session.configure", session_id=session["session_id"], mcp_policy="off")
        preview = client.request(
            "goal.preview",
            session_id=session["session_id"],
            objective=objective,
            execution_scope="local_only",
        )["plan"]
        assert preview["execution_scope"] == "local_only"
        started = client.request(
            "prompt.submit",
            session_id=session["session_id"],
            objective=objective,
            interaction="colony",
            disposition="start",
            execution_scope="local_only",
        )
        assert started["execution_scope"] == "local_only"
        assert captured["execution_scope"] == "local_only"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        if paths["socket"].exists():
            paths["socket"].unlink()


def test_mission_read_api_uses_updated_order_and_bounds_pending_payload(tmp_path: Path) -> None:
    paths = runtime_paths(tmp_path)
    paths["root"].mkdir(parents=True, exist_ok=True)
    if paths["socket"].exists():
        paths["socket"].unlink()
    server = RuntimeServer(tmp_path, paths["socket"])
    try:
        session = server.dispatch({"action": "session.ensure", "payload": {}})
        first = server.dispatch({
            "action": "run_demo",
            "payload": {"objective": "First durable mission", "session_id": session["session_id"]},
        })
        second = server.dispatch({
            "action": "run_demo",
            "payload": {"objective": "Second durable mission", "session_id": session["session_id"]},
        })
        first_id = first["mission"]["mission_id"]
        second_id = second["mission"]["mission_id"]
        with server.service.store.connection() as connection:
            connection.execute("UPDATE missions SET updated_at = ? WHERE mission_id = ?", ("9999-12-31T23:59:59Z", first_id))
        listed = server.dispatch({
            "action": "mission.list",
            "payload": {"session_id": session["session_id"], "limit": 2},
        })
        assert [row["mission_id"] for row in listed["missions"]] == [first_id, second_id]

        server.service.store.add_runtime_request(
            "request-sensitive",
            session["session_id"],
            second_id,
            "approval",
            "item/commandExecution/requestApproval",
            "sensitive prompt must stay in crabd",
            {"secret": "do-not-export"},
        )
        pending = server.dispatch({"action": "mission.pending_requests", "payload": {"mission_id": second_id}})
        assert pending["requests"] == [{
            "request_id": "request-sensitive",
            "request_type": "approval",
            "method": "item/commandExecution/requestApproval",
            "created_at": pending["requests"][0]["created_at"],
        }]
        assert "payload" not in pending["requests"][0]
        assert "prompt" not in pending["requests"][0]
    finally:
        server.server_close()
        if paths["socket"].exists():
            paths["socket"].unlink()


def test_short_follow_up_reuses_completed_mission_instead_of_creating_new_one(tmp_path: Path) -> None:
    paths = runtime_paths(tmp_path)
    paths["root"].mkdir(parents=True, exist_ok=True)
    server = RuntimeServer(tmp_path, paths["socket"])
    try:
        session = server.dispatch({"action": "session.ensure", "payload": {}})
        completed = server.dispatch({
            "action": "run_demo",
            "payload": {
                "objective": "Create a durable portfolio artifact",
                "session_id": session["session_id"],
            },
        })
        before = len(server.service.store.list_missions(limit=100))
        result = server.dispatch({
            "action": "prompt.submit",
            "payload": {
                "session_id": session["session_id"],
                "objective": "진행해",
            },
        })
        after = len(server.service.store.list_missions(limit=100))

        assert result["interaction"] == "continuation"
        assert result["status"] == "ready"
        assert result["source_mission_id"] == completed["mission"]["mission_id"]
        assert before == after
        messages = server.service.store.messages(session["session_id"], limit=20)
        assert any("CONTINUATION READY" in str(row.get("content") or "") for row in messages)
    finally:
        server.server_close()
        if paths["socket"].exists():
            paths["socket"].unlink()


def test_short_follow_up_without_history_does_not_create_context_free_mission(tmp_path: Path) -> None:
    paths = runtime_paths(tmp_path)
    paths["root"].mkdir(parents=True, exist_ok=True)
    server = RuntimeServer(tmp_path, paths["socket"])
    try:
        session = server.dispatch({"action": "session.ensure", "payload": {}})
        result = server.dispatch({
            "action": "prompt.submit",
            "payload": {"session_id": session["session_id"], "objective": "진행해"},
        })

        assert result["status"] == "needs_input"
        assert result["interaction"] == "continuation"
        assert server.service.store.list_missions(limit=100) == []
        messages = server.service.store.messages(session["session_id"], limit=20)
        assert any("CONTINUATION NEEDS A GOAL" in str(row.get("content") or "") for row in messages)
    finally:
        server.server_close()
        if paths["socket"].exists():
            paths["socket"].unlink()


def test_zero_model_mission_uses_preloaded_opencrab_context_without_runtime_endpoint(tmp_path: Path, monkeypatch) -> None:
    paths = runtime_paths(tmp_path)
    paths["root"].mkdir(parents=True, exist_ok=True)
    server = RuntimeServer(tmp_path, paths["socket"])
    monkeypatch.setattr(server, "_bridge", lambda session: type("BridgeStub", (), {"thread_id": None})())
    try:
        session = server.dispatch({"action": "session.ensure", "payload": {}})
        server.dispatch({
            "action": "ontology.context",
            "payload": {
                "session_id": session["session_id"],
                "project_ids": ["project-1"],
                "package_ids": ["pack-1"],
                "project_labels": {},
                "package_labels": {},
            },
        })
        evidence_rows = []
        for index in range(24):
            duplicate_pair = index in {0, 1}
            lane_id = "actual_work" if duplicate_pair or index % 2 else "persona_policy"
            source_index = 0 if duplicate_pair else index
            evidence_rows.append(
                {
                    "id": f"ev-{index:02d}",
                    "document_id": f"doc-{source_index:02d}",
                    "project_id": "project-work" if lane_id == "actual_work" else "project-policy",
                    "workspace_id": "workspace-facility" if lane_id == "actual_work" else "workspace-main",
                    "package_id": "pack-work" if lane_id == "actual_work" else "pack-policy",
                    "text": f"bounded observed evidence {source_index:02d}",
                    "source": f"opencrab://document/doc-{source_index:02d}",
                    "lane_id": lane_id,
                    "lane_purpose": "personal_work_evidence" if lane_id == "actual_work" else "policy_only",
                    "profile_id": "facility-management" if lane_id == "actual_work" else "main",
                    "claim_scope": (
                        "personal_role_confirmation_required"
                        if lane_id == "actual_work"
                        else "policy_only_no_personal_fact_promotion"
                    ),
                }
            )
        context = {
            "status": "ok",
            "authority": "gateway_verified_mcp_response",
            "evidence": evidence_rows,
            "evidence_count": 24,
            "claim_gate": "pass",
            "graph_gate": "not_required",
            "quality": {"evidence_count": 24, "usable_evidence_count": 24},
            "tool_calls": [],
        }
        submitted = server.dispatch({
            "action": "prompt.submit",
            "payload": {
                "session_id": session["session_id"],
                "objective": "팩 목록과 상태를 보여줘",
                "disposition": "start",
                "interaction": "colony",
                "opencrab_context": context,
            },
        })
        assert submitted["status"] == "starting"
        # The mission has 24 bounded evidence rows and reaches VERIFYING only
        # after SQLite-backed local gates finish. On Windows that measured
        # lifecycle can exceed the old 10-second observer bound; keep the
        # assertion tied to the durable terminal status with a bounded budget.
        deadline = time.monotonic() + 30.0
        mission = None
        while time.monotonic() < deadline:
            rows = server.service.store.list_missions(limit=20)
            if rows:
                mission = rows[0]
                if str(mission.get("status") or "") in {"completed", "failed", "cancelled"}:
                    break
            time.sleep(0.02)
        assert mission is not None
        assert mission["status"] == "completed"
        detail = server.service.store.inspect(str(mission["mission_id"]))
        assert all(str(row.get("provider") or "") == "local" for row in detail["assignments"])
        receipts = [
            row for row in detail["tool_receipts"]
            if str(row.get("tool_name") or "") == "opencrab.mcp.opencrab_query"
        ]
        assert receipts
        assert all(str(row.get("status") or "") == "success" for row in receipts)
        assert any(str(row.get("kind") or "") == "ontology_ledger" for row in detail["artifacts"])
        opencrab_evidence = [
            row
            for row in detail["evidence"]
            if str(row.get("source_type") or "") == "opencrab_mcp_evidence"
        ]
        expected_source_ids = {f"ev-{index:02d}" for index in range(24)}
        expected_ledger_ids = {
            f"mcp:{mission['mission_id']}:{evidence_id}"
            for evidence_id in expected_source_ids
        }
        assert len(opencrab_evidence) == 24
        assert {str(row["evidence_id"]) for row in opencrab_evidence} == expected_ledger_ids
        context_artifact = next(
            row
            for row in detail["artifacts"]
            if str(row.get("kind") or "") == "mcp_context_receipt"
        )
        receipt_payload = json.loads(Path(str(context_artifact["path"])).read_text(encoding="utf-8"))
        assert receipt_payload["evidence_count"] == 23
        primary = next(row for row in receipt_payload["evidence"] if row["id"] == "ev-00")
        assert primary["duplicate_ids"] == ["ev-01"]
    finally:
        server.server_close()
        if paths["socket"].exists():
            paths["socket"].unlink()


def _seed_failed_retry_mission(
    server: RuntimeServer,
    session_id: str,
    mission_id: str,
    receipt_content: str,
) -> None:
    contract = MissionContract(
        mission_id=mission_id,
        objective="오픈크랩 근거를 바탕으로 결과를 검증해",
        acceptance=["evidence receipt is preserved"],
        workspace=str(server.workspace),
        risk="strategic",
        max_attempts=1,
        max_workers=1,
        token_budget=1000,
    )
    server.service.store.create_mission(contract, session_id=session_id)
    server.service.store.add_task(
        TaskSlot(
            task_id="task-queen",
            mission_id=mission_id,
            title="Retrieve OpenCrab evidence",
            role=Role.QUEEN,
            position=1,
            output_contract="opencrab_context.json",
        )
    )
    server.service.store.transition_mission(mission_id, MissionStatus.PLANNED, Role.KING)
    server.service.store.transition_mission(mission_id, MissionStatus.RUNNING, Role.KING)
    server.service._write_artifact(
        mission_id,
        "task-queen",
        "opencrab_context.json",
        receipt_content,
        "mcp_context_receipt",
    )
    server.service.store.transition_mission(mission_id, MissionStatus.FAILED, Role.ORACLE)


def test_retry_reuses_persisted_opencrab_context_without_live_mcp(tmp_path: Path, monkeypatch) -> None:
    paths = runtime_paths(tmp_path)
    paths["root"].mkdir(parents=True, exist_ok=True)
    server = RuntimeServer(tmp_path, paths["socket"])
    try:
        session = server.service.store.create_session(interaction_mode="colony")
        receipt = {
            "status": "ok",
            "authority": "gateway_verified_mcp_response",
            "evidence": [
                {
                    "id": "ev-retry-1",
                    "text": "persisted retry evidence",
                    "source": "opencrab://evidence/ev-retry-1",
                }
            ],
            "evidence_count": 1,
            "claim_gate": "pass",
            "graph_gate": "not_required",
            "quality": {"evidence_count": 1, "usable_evidence_count": 1},
            "tool_calls": [],
        }
        source_mission_id = "mission-retry-opencrab-context"
        _seed_failed_retry_mission(
            server,
            session["session_id"],
            source_mission_id,
            json.dumps(receipt, ensure_ascii=False),
        )
        captured = {}

        def launch(current_session, objective, **kwargs):
            captured["session_id"] = current_session["session_id"]
            captured["objective"] = objective
            captured.update(kwargs)
            return {
                "session_id": current_session["session_id"],
                "status": "starting",
                "interaction": "colony",
                "objective": objective,
                "retry_of": kwargs.get("retry_of"),
            }

        monkeypatch.setattr(server, "_launch_colony", launch)
        result = server._retry_last_mission(session)

        assert result["status"] == "starting"
        assert captured["retry_of"] == source_mission_id
        assert captured["preloaded_opencrab_context"] == receipt
        event = next(
            row
            for row in server.service.store.events(source_mission_id)
            if row.event_type == "mission_retry_requested"
        )
        assert event.payload["opencrab_context_reused"] is True
        assert event.payload["opencrab_context_status"] == "ok"
        assert event.payload["opencrab_evidence_count"] == 1
    finally:
        server.server_close()
        if paths["socket"].exists():
            paths["socket"].unlink()


def test_retry_fails_closed_when_persisted_opencrab_receipt_is_unreadable(tmp_path: Path, monkeypatch) -> None:
    paths = runtime_paths(tmp_path)
    paths["root"].mkdir(parents=True, exist_ok=True)
    server = RuntimeServer(tmp_path, paths["socket"])
    try:
        session = server.service.store.create_session(interaction_mode="colony")
        _seed_failed_retry_mission(
            server,
            session["session_id"],
            "mission-retry-unreadable-context",
            "{not-json",
        )

        def unexpected_launch(*args, **kwargs):
            raise AssertionError("retry must not launch with an unreadable evidence receipt")

        monkeypatch.setattr(server, "_launch_colony", unexpected_launch)
        with pytest.raises(RuntimeError, match="OpenCrab context receipt is unreadable"):
            server._retry_last_mission(session)
    finally:
        server.server_close()
        if paths["socket"].exists():
            paths["socket"].unlink()


def test_auto_started_daemon_survives_client_process_scope(tmp_path: Path) -> None:
    client = start_daemon(tmp_path)
    try:
        first = client.request("ping")
        second = DaemonClient(tmp_path).request("ping")
        assert first["pid"] == second["pid"]
        assert first["status"] == "online"
    finally:
        client.request("shutdown")
        deadline = time.monotonic() + 2
        while client.ping() and time.monotonic() < deadline:
            time.sleep(0.05)


def test_mcp_policy_selection_and_king_title_are_session_scoped() -> None:
    names = ["OpenCrab", "Neo4j", "github"]
    assert _mcp_policy_with("auto", "OpenCrab", True, names) == "allow:OpenCrab"
    assert _mcp_policy_with("allow:OpenCrab,Neo4j", "OpenCrab", False, names) == "allow:Neo4j"
    assert _mcp_policy_with("allow:OpenCrab", "OpenCrab", False, names) == "off"
    assert "OpenCrab" in _king_title("OpenCrab MCP panel implementation and verification")


def test_daemon_forks_codex_context_only_when_session_is_ready(tmp_path: Path) -> None:
    paths = runtime_paths(tmp_path)
    paths["root"].mkdir(parents=True, exist_ok=True)
    server = RuntimeServer(tmp_path, paths["socket"])

    class StubBridge:
        def fork_thread(self) -> str:
            return "thread-forked"

    try:
        session = server.service.store.create_session(title="Source")
        server.service.store.update_session(session["session_id"], codex_thread_id="thread-source")
        server.service.store.add_message(session["session_id"], "user", "hello")
        server._bridge = lambda current: StubBridge()

        result = server.dispatch({"action": "session.fork", "payload": {"session_id": session["session_id"]}})

        assert result["session"]["codex_thread_id"] == "thread-forked"
        assert result["codex_context_forked"] is True
        server.service.store.update_session(session["session_id"], status="running")
        with pytest.raises(RuntimeError, match="active turn"):
            server.dispatch({"action": "session.fork", "payload": {"session_id": session["session_id"]}})
    finally:
        server.server_close()
        if paths["socket"].exists():
            paths["socket"].unlink()


def test_crab_doc_manifest_import_and_list(tmp_path: Path) -> None:
    paths = runtime_paths(tmp_path)
    paths["root"].mkdir(parents=True, exist_ok=True)
    server = RuntimeServer(tmp_path, paths["socket"])
    manifest_path = tmp_path / "sample.crabdoc.json"
    manifest_path.write_text(
        '{"protocol":"crab-doc/v1","product":"CRAB DOC","createdAt":"2026-08-03T00:00:00Z",'
        '"sourceFileName":"sample.docx","document":{"fileName":"sample.docx","extension":".docx",'
        '"kind":"document","title":"Sample brief","text":"Verified context.","parser":"OOXML DOCX",'
        '"capabilities":{"read":true,"edit":true,"export":true,"exportExtension":".docx"}},'
        '"suggestedCommand":"crab doc import ./sample.docx.crabdoc.json"}\n',
        encoding="utf-8",
    )
    try:
        imported = server.dispatch({"action": "doc.import", "payload": {"manifest_path": str(manifest_path)}})
        assert imported["status"] == "ok"
        assert imported["title"] == "Sample brief"
        assert Path(imported["manifest_path"]).is_file()
        assert Path(imported["markdown_path"]).read_text(encoding="utf-8") == "Verified context.\n"
        listed = server.dispatch({"action": "doc.list", "payload": {}})
        assert listed["count"] == 1
        assert listed["documents"][0]["title"] == "Sample brief"
    finally:
        server.server_close()
        if paths["socket"].exists():
            paths["socket"].unlink()


def test_folder_project_and_new_conversation_are_durable(tmp_path: Path) -> None:
    paths = runtime_paths(tmp_path)
    paths["root"].mkdir(parents=True, exist_ok=True)
    folder = tmp_path / "repo"
    folder.mkdir()

    class Builder:
        def build_and_ingest(self, selected: Path, **kwargs):
            return {
                "status": "expert_required",
                "reason": "test gate",
                "run_id": "packrun-test",
                "root_path": str(selected.resolve()),
                "stage_path": str(tmp_path / "stage"),
                "manifest_path": str(tmp_path / "stage" / "manifest.json"),
                "chunks_path": str(tmp_path / "stage" / "chunks.jsonl"),
                "source_count": 1,
                "chunk_count": 1,
            }

    server = RuntimeServer(tmp_path, paths["socket"], pack_builder=Builder())
    try:
        project = server.dispatch({"action": "project.create", "payload": {"name": "Repo", "root_path": str(folder)}})
        assert project["root_path"] == str(folder.resolve())
        session = server.dispatch({
            "action": "session.create",
            "payload": {
                "title": "Repo conversation",
                "project_id": project["project_id"],
                "project_name": project["name"],
                "project_root_path": project["root_path"],
            },
        })
        assert session["project_id"] == project["project_id"]
        assert session["project_root_path"] == project["root_path"]
        result = server.dispatch({"action": "pack.ingest", "payload": {"project_id": project["project_id"]}})
        assert result["status"] == "expert_required"
        stored = server.service.store.pack_ingest_run("packrun-test")
        assert stored is not None
        assert stored["status"] == "expert_required"
    finally:
        server.server_close()
        if paths["socket"].exists():
            paths["socket"].unlink()
