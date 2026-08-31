from __future__ import annotations

import json
import threading
from pathlib import Path

from crabagent.codex_app_server import CodexLiveTurn
from crabagent.benchmark import observed_metrics
from crabagent.colony import ColonyExecutor
from crabagent.goal import classify_goal
from crabagent.kinetic_workflow import compile_kinetic_workflow
from crabagent.kinetic_contract import compact_king_plan, compile_king_plan
from crabagent.runtime import RuntimeService


def _mcp_context(_args):
    return {
        "status": "ok",
        "authority": "direct_mcp_response",
        "evidence": [
            {
                "id": "ev-12345678-aaaa-bbbb-cccc-ddddeeeeffff",
                "text": "관측된 근거는 전략의 일관된 실행을 지지한다.",
                "source": "opencrab://pack/one",
            }
        ],
        "evidence_count": 1,
        "claim_gate": "pass",
        "graph_gate": "not_required",
        "quality": {"evidence_count": 1, "usable_evidence_count": 1},
        "tool_calls": [],
    }


def test_lookup_compiles_to_typed_observation_operator_without_write_or_model() -> None:
    plan = classify_goal("오픈크랩 팩과 프로젝트 목록을 보여줘")
    graph = {
        "graph_id": "goal-lookup",
        "decision_slots": plan.response_contract,
    }

    workflow = compile_kinetic_workflow(plan.to_dict(), graph)
    steps = {row["id"]: row for row in workflow["steps"]}

    assert plan.action_mode == "lookup"
    assert steps["decide_from_ontology"]["operator"] == "project_observed_items"
    assert steps["decide_from_ontology"]["model_required"] is False
    assert steps["persist_ontology_ledger"]["operator"] == "persist_ontology_ledger"
    assert steps["persist_ontology_ledger"]["gate"] == "durable_ledger_readback"
    assert workflow["output_contract"]["ontology_ledger_required"] is True
    assert "execute_action" not in steps
    assert workflow["token_policy"]["allow_full_catalog_in_prompt"] is False
    assert "evidence_can_only_enter_from_observed_mcp_receipt" in workflow["state_invariants"]


def test_read_only_worker_compiles_non_mutating_deliverable_step() -> None:
    objective = (
        "OpenCrab 자료는 읽기만 하며 프로젝트·팩·문서를 수정하지 않는다. "
        "WORKER는 evidence ID가 포함된 실제 업무 후보표만 작성한다. "
        "ORACLE은 WORKER 결과와 provenance만 검증한다. graph_required=false."
    )
    plan = classify_goal(objective, selected_project_count=1)
    graph = {"graph_id": "goal-read-only-worker", "decision_slots": plan.response_contract}

    workflow = compile_kinetic_workflow(plan.to_dict(), graph)
    steps = {row["id"]: row for row in workflow["steps"]}

    assert plan.requires_write is False
    assert plan.requires_worker_output is True
    assert "execute_action" not in steps
    assert steps["produce_deliverable"]["operator"] == "produce_evidence_bound_deliverable"
    assert steps["produce_deliverable"]["outputs"] == ["worker_result", "worker_receipt"]
    assert steps["produce_deliverable"]["write_scope"] == "none"
    assert "worker_result" in steps["verify_result"]["inputs"]
    assert "workspace_change" not in steps["verify_result"]["inputs"]
    assert workflow["output_contract"]["requires_write"] is False
    assert workflow["output_contract"]["requires_worker_output"] is True
    assert "non_mutating_worker_never_emits_workspace_change" in workflow["state_invariants"]


def test_metadata_lookup_runs_to_oracle_with_zero_model_turns(tmp_path: Path) -> None:
    service = RuntimeService(tmp_path)
    session = service.store.create_session(interaction_mode="colony")

    def loader(_args):
        return {
            "status": "no_evidence",
            "items": [
                {
                    "package_id": "pack-a",
                    "project_id": "project-a",
                    "title": "ALEXAI Brand Pack",
                    "type": "pack",
                    "source": "opencrab://catalog/pack-a",
                },
                {
                    "project_id": "project-a",
                    "title": "ALEXAI Workspace",
                    "type": "project",
                    "source": "opencrab://catalog/project-a",
                },
            ],
        }

    class NoModelBridge:
        thread_id = "no-model-thread"

    snapshot = ColonyExecutor(
        service,
        session["session_id"],
        NoModelBridge(),
        threading.Event(),
        opencrab_context_loader=loader,
    ).run("오픈크랩 팩과 프로젝트 목록을 보여줘")

    assert snapshot["mission"]["status"] == "completed"
    assert snapshot["budget"]["tokens_observed"] is None
    assert snapshot["kinetic_workflow"]["operator"] == "lookup"
    state = next(row for row in snapshot["artifacts"] if row["kind"] == "kinetic_workflow_state")
    state_payload = json.loads(Path(state["path"]).read_text(encoding="utf-8"))
    assert state_payload["summary"]["completed_step_count"] >= 6
    assert any(row["step_id"] == "retrieve_evidence" and row["status"] == "completed" for row in state_payload["trace"])
    assert any(row["step_id"] == "persist_ontology_ledger" and row["status"] == "completed" for row in state_payload["trace"])
    ledger = next(row for row in snapshot["artifacts"] if row["kind"] == "ontology_ledger")
    ledger_payload = json.loads(Path(ledger["path"]).read_text(encoding="utf-8"))
    assert ledger_payload["mission_id"] == snapshot["mission"]["mission_id"]
    assert ledger_payload["goal_graph_id"] == snapshot["goal_graph"]["graph_id"]
    assert ledger_payload["revision"] >= 1
    assert snapshot["ontology_ledger"]["readback"]["validated"] is True
    receipt = next(row for row in snapshot["artifacts"] if row["kind"] == "mcp_context_receipt")
    receipt_payload = json.loads(Path(receipt["path"]).read_text(encoding="utf-8"))
    assert receipt_payload["evidence_count"] == 0
    assert receipt_payload["observation_gate"] == "pass"
    assert receipt_payload["observed_item_count"] == 2
    outcome = next(row for row in snapshot["artifacts"] if row["kind"] == "goal_outcome")
    outcome_payload = json.loads(Path(outcome["path"]).read_text(encoding="utf-8"))
    assert outcome_payload["observed_items"]["count"] == 2
    assert outcome_payload["evidence"]["claim_gate"] == "blocked"
    metrics = observed_metrics(snapshot)
    assert metrics["kinetic_workflow_artifacts"] == 1
    assert metrics["kinetic_state_artifacts"] == 1
    assert metrics["typed_observed_item_count"] == 2
    assert metrics["observation_mode"] == "metadata_observation"


def test_king_plan_parsing_keeps_goal_graph_as_authority() -> None:
    plan = classify_goal("오픈크랩 팩의 근거를 비교해서 전략을 추천해줘")
    graph = {"graph_id": "goal-test", "decision_slots": plan.response_contract}
    king = compile_king_plan(
        objective=plan.objective,
        goal_plan=plan.to_dict(),
        goal_graph=graph,
        source="codex_turn",
        model_text=(
            "GOAL_RESTATEMENT\n근거에 기반한 전략 비교\n"
            "SUBGOALS\n- 근거 추출\n- 차이 비교\n"
            "CONSTRAINTS\n- 확인된 자료만 사용\n"
            "SUCCESS_CHECKS\n- 각 주장에 출처\n"
            "NEXT_ACTION\nQUEEN: 근거 경로를 채워라"
        ),
    )

    assert king["model_advisory"] is True
    assert king["scope_locked"] is True
    assert king["evidence_authority"] == "opencrab_mcp_receipt_only"
    assert king["subgoals"] == ["근거 추출", "차이 비교"]
    assert king["decision_slots"] == plan.response_contract


def test_local_mcpworld_execution_has_no_opencrab_or_queen_handoff() -> None:
    objective = (
        "Kingcrab MCP를 사용해 MCPWorld 홍보영상 폴더의 1~4번 로컬 영상만으로 최고의 홍보 영상을 "
        "만들 계획을 작성하고 제작해. OpenCrab과 외부 근거는 필요하지 않다. 기존 최종 영상과 제작 계획을 "
        "우선 검토하고, 필요한 경우에만 같은 로컬 폴더에서 개선하며 1920×1080 MP4 결과를 검증해."
    )
    plan = classify_goal(objective)
    graph = {"graph_id": "goal-local-media", "decision_slots": plan.response_contract}

    king = compile_king_plan(
        objective=objective,
        goal_plan=plan.to_dict(),
        goal_graph=graph,
    )
    workflow = compile_kinetic_workflow(plan.to_dict(), graph)
    compact = compact_king_plan(king)
    steps = {row["id"]: row for row in workflow["steps"]}

    assert plan.ontology_required is False
    assert king["evidence_authority"] == "local_observed_receipts_only"
    assert king["next_action"].startswith("WORKER:")
    assert "opencrab_mcp_receipt_only" not in compact
    assert "QUEEN:" not in compact
    assert "patrol_quality" not in steps
    assert steps["execute_action"]["inputs"] == ["goal_contract"]
    assert steps["verify_result"]["inputs"] == ["goal_contract", "workspace_change"]
    assert "local_work_never_requires_opencrab_receipts" in workflow["state_invariants"]
    assert not any(step["role"] in {"QUEEN", "SOLDIER"} for step in workflow["steps"])


def test_malformed_queen_handoff_gets_one_bounded_repair(tmp_path: Path) -> None:
    service = RuntimeService(tmp_path)
    session = service.store.create_session(interaction_mode="colony")

    class Bridge:
        thread_id = "thread-repair"
        last_start_mode = "started"

        def __init__(self) -> None:
            self.calls = []

        def start(self) -> str:
            return self.thread_id

        def run_turn(self, prompt, emit, **kwargs):
            self.calls.append(prompt)
            text = "bad handoff" if len(self.calls) == 1 else (
                "SELECTED_PATH\n근거 -> 주장\n"
                "SUPPORTED_CLAIMS\n일관된 실행을 지지한다. [evidence_id: ev-12345678]\n"
                "GAPS\n추가 범위는 확인하지 않았다.\n"
                "NEXT_ACTION\nSTOP"
            )
            return CodexLiveTurn(
                thread_id=self.thread_id,
                turn_id="turn-%d" % len(self.calls),
                status="completed",
                text=text,
                usage={"last": {"totalTokens": 100}},
                started_at="now",
                finished_at="now",
            )

    bridge = Bridge()
    snapshot = ColonyExecutor(
        service,
        session["session_id"],
        bridge,
        threading.Event(),
        opencrab_context_loader=_mcp_context,
    ).run("오픈크랩 팩의 근거를 비교해서 전략을 추천해줘")

    assert snapshot["mission"]["status"] == "completed"
    assert len(bridge.calls) == 2
    assert any("REPAIR PASS" in prompt for prompt in bridge.calls)
    report = next(row for row in snapshot["artifacts"] if row["kind"] == "soldier_patrol")
    payload = json.loads(Path(report["path"]).read_text(encoding="utf-8"))
    assert payload["automatic_revision"]["attempted"] is True
    assert payload["queen_handoff_quality"]["accepted"] is True
    assert payload["automatic_revision"]["quality"]["accepted"] is True


def test_model_king_refines_the_next_ontology_query(tmp_path: Path) -> None:
    service = RuntimeService(tmp_path)
    session = service.store.create_session(interaction_mode="colony")
    snapshot = service.plan_mission(
        "오픈크랩 온톨로지를 바탕으로 장기적인 브랜드 전략과 실행 파이프라인을 설계하고 비교해줘",
        session_id=session["session_id"],
        adaptive=True,
    )
    executor = ColonyExecutor(service, session["session_id"], object(), threading.Event())
    executor.goal_plan = dict(snapshot["goal_plan"])
    executor.goal_graph = dict(snapshot["goal_graph"])
    king_task = next(row for row in snapshot["tasks"] if row["role"] == "KING")
    executor._capture_king_plan(
        snapshot["mission"]["mission_id"],
        king_task,
        "GOAL_RESTATEMENT\n장기 전략\nSUBGOALS\n- 실행 파이프라인\n- 브랜드 레버\nSUCCESS_CHECKS\n- 근거 인용\nNEXT_ACTION\nQUEEN: 경로 조회",
        source="codex_turn",
    )
    executor._refine_retrieval_from_king_plan(snapshot["mission"]["mission_id"], king_task)

    route = executor.goal_plan["retrieval_contract"]
    assert route["query_origin"] == "goal_plus_king_plan"
    assert "실행 파이프라인" in route["primary_query"]
    assert any(row["kind"] == "goal_plan_refined" for row in service.store.inspect(snapshot["mission"]["mission_id"])["artifacts"])


def test_model_king_materializes_bounded_read_only_worker_slots(tmp_path: Path) -> None:
    service = RuntimeService(tmp_path)
    session = service.store.create_session(interaction_mode="colony")
    objective = "오픈크랩 온톨로지를 바탕으로 장기적인 브랜드 전략과 실행 파이프라인을 설계하고 비교해줘"
    snapshot = service.plan_mission(
        objective,
        max_workers=2,
        worker_policy="fixed",
        session_id=session["session_id"],
        adaptive=True,
    )
    executor = ColonyExecutor(service, session["session_id"], object(), threading.Event())
    executor.goal_plan = dict(snapshot["goal_plan"])
    executor.goal_graph = dict(snapshot["goal_graph"])
    king_task = next(row for row in snapshot["tasks"] if row["role"] == "KING")
    soldier_task = next(row for row in snapshot["tasks"] if row["role"] == "SOLDIER")
    executor.king_plan = compile_king_plan(
        objective=objective,
        goal_plan=executor.goal_plan,
        goal_graph=executor.goal_graph,
        source="codex_turn",
        model_text=(
            "GOAL_RESTATEMENT\n전략 설계\n"
            "SUBGOALS\n- 시장 근거 정리\n- 실행 파이프라인 비교\n- 위험 조건 점검\n"
            "SUCCESS_CHECKS\n- 각 단계에 근거\nNEXT_ACTION\nQUEEN: 경로 조회"
        ),
    )
    dynamic = executor._materialize_king_subgoals(
        snapshot["mission"]["mission_id"],
        king_task,
        soldier_task,
        snapshot["tasks"],
    )

    assert len(dynamic) == 2
    reopened = service.store.inspect(snapshot["mission"]["mission_id"])
    assert [row["role"] for row in reopened["tasks"]] == ["KING", "QUEEN", "SOLDIER", "WORKER", "WORKER", "ORACLE"]
    assert all(row["task_kind"] == "king_subgoal" for row in dynamic)
    assert all(row["execution_mode"] == "serial_read_only" for row in dynamic)
    assert all(row["write_scope"] == "read_only" for row in dynamic)
    assert all(row["provider"] == "local" for row in reopened["assignments"] if row["task_id"] in {item["task_id"] for item in dynamic})

    executor.opencrab_receipt = _mcp_context({})
    worker_text = executor._run_local_contract(
        snapshot["mission"]["mission_id"],
        dynamic[0],
        objective,
        reason="bounded read-only subgoal projection",
    )
    assert "observed_mcp_evidence_only" in worker_text
    worker_artifact = next(
        row for row in service.store.inspect(snapshot["mission"]["mission_id"])["artifacts"]
        if row["kind"] == "worker_subgoal"
    )
    payload = json.loads(Path(worker_artifact["path"]).read_text(encoding="utf-8"))
    assert payload["model_invocation"] is False
    assert payload["evidence_count"] == 1
    assert payload["evidence_slot_ids"]
    assert all(row["evidence_ids"] for row in payload["slot_coverage"])
    assert any(event.event_type == "king_subgoal_slots_materialized" for event in service.store.events(snapshot["mission"]["mission_id"]))


def test_strategic_ontology_run_executes_subgoal_workers_before_oracle(tmp_path: Path) -> None:
    service = RuntimeService(tmp_path)
    session = service.store.create_session(interaction_mode="colony")

    class Bridge:
        thread_id = "thread-subgoals"
        last_start_mode = "started"

        def __init__(self) -> None:
            self.calls = []

        def start(self) -> str:
            return self.thread_id

        def run_turn(self, prompt, emit, **kwargs):
            self.calls.append(prompt)
            if "CRABAGENT ROLE: KING" in prompt:
                text = (
                    "GOAL_RESTATEMENT\n전략 설계\n"
                    "SUBGOALS\n- 시장 근거 정리\n- 실행 파이프라인 비교\n"
                    "CONSTRAINTS\n- 관측 근거만 사용\n"
                    "SUCCESS_CHECKS\n- 각 단계에 근거\n"
                    "NEXT_ACTION\nQUEEN: 경로 조회"
                )
            elif "CRABAGENT ROLE: QUEEN" in prompt:
                text = (
                    "SELECTED_PATH\nresource -> evidence -> claim -> outcome\n"
                    "SUPPORTED_CLAIMS\n확인된 근거가 전략 경로를 지지한다. [evidence_id: ev-12345678-aaaa-bbbb-cccc-ddddeeeeffff]\n"
                    "GAPS\n추가 자료는 확인하지 않았다.\n"
                    "NEXT_ACTION\nSTOP"
                )
            else:
                text = "FINAL_CONCLUSION\n근거와 하위 목표 영수증을 확인했다.\nNEXT_ACTION\nSTOP"
            return CodexLiveTurn(
                thread_id=self.thread_id,
                turn_id="turn-%d" % len(self.calls),
                status="completed",
                text=text,
                usage={"last": {"totalTokens": 100}},
                started_at="now",
                finished_at="now",
            )

    bridge = Bridge()
    snapshot = ColonyExecutor(
        service,
        session["session_id"],
        bridge,
        threading.Event(),
        opencrab_context_loader=lambda args: _mcp_context({}),
    ).run(
        "오픈크랩 온톨로지를 바탕으로 장기적인 브랜드 전략과 실행 파이프라인을 설계하고 비교해줘",
        max_workers=2,
        worker_policy="fixed",
    )

    assert snapshot["mission"]["status"] == "completed"
    assert len(bridge.calls) == 3
    subgoal_tasks = [row for row in snapshot["tasks"] if row["task_kind"] == "king_subgoal"]
    assert len(subgoal_tasks) == 2
    assert all(row["status"] == "completed" for row in subgoal_tasks)
    assert len([row for row in snapshot["artifacts"] if row["kind"] == "worker_subgoal"]) == 2
    assert len([row for row in snapshot["tool_receipts"] if row["tool_name"] == "crab.worker.subgoal"]) == 2
    assert any("KING SUBGOAL WORKERS" in prompt for prompt in bridge.calls if "CRABAGENT ROLE: ORACLE" in prompt)
    assert snapshot["goal_graph"]["graph_id"]
    assert snapshot["king_plan"]["model_advisory"] is True
    assert snapshot["subgoal_plan"]["admitted_count"] == 2
