from pathlib import Path
from tempfile import TemporaryDirectory
import json
import threading
import pytest

from crabagent.conversation import interaction_kind
from crabagent.codex_app_server import CodexLiveTurn
from crabagent.colony import ColonyExecutor
from crabagent.benchmark import compare_goal, compare_observed, observed_metrics
from crabagent.benchmark_suite import plan_suite, select_cases, summarize_suite
from crabagent.benchmark_runner import _answer_grounding_quality, _copy_for_write_benchmark, _direct_result_accepted, _session_events, run_observed_benchmark
from crabagent.goal import classify_goal
from crabagent.models import Role
from crabagent.runtime import RuntimeService
from crabagent.inverter import SolteluInverter
from crabagent.workspace_observer import capture_workspace, diff_workspace


def test_workspace_observer_reports_real_added_modified_and_deleted_files(tmp_path: Path) -> None:
    (tmp_path / "keep.txt").write_text("before\n", encoding="utf-8")
    (tmp_path / "delete.txt").write_text("gone\n", encoding="utf-8")
    before = capture_workspace(tmp_path)
    (tmp_path / "keep.txt").write_text("after\n", encoding="utf-8")
    (tmp_path / "delete.txt").unlink()
    (tmp_path / "add.txt").write_text("new\n", encoding="utf-8")
    change = diff_workspace(before, capture_workspace(tmp_path))
    assert change["changed_file_count"] == 3
    assert change["added_count"] == 1
    assert change["modified_count"] == 1
    assert change["deleted_count"] == 1


def test_workspace_observer_tracks_approved_crabagent_artifact_only(tmp_path: Path) -> None:
    runtime = tmp_path / ".crabagent"
    artifacts = runtime / "artifacts"
    mission_artifacts = artifacts / "mission-internal"
    mission_artifacts.mkdir(parents=True)
    (runtime / "state.sqlite3").write_text("before\n", encoding="utf-8")
    (mission_artifacts / "worker_result.md").write_text("before\n", encoding="utf-8")

    before = capture_workspace(tmp_path)
    (runtime / "state.sqlite3").write_text("after\n", encoding="utf-8")
    (mission_artifacts / "worker_result.md").write_text("after\n", encoding="utf-8")
    target = artifacts / "host_current_model_e2e.txt"
    target.write_text("host-current-model-e2e-ok\n", encoding="utf-8")

    after = capture_workspace(tmp_path)
    change = diff_workspace(before, after)

    assert ".crabagent/state.sqlite3" not in after["files"]
    assert not any(path.startswith(".crabagent/artifacts/mission-") for path in after["files"])
    assert ".crabagent/artifacts/host_current_model_e2e.txt" in after["files"]
    assert change["changed_file_count"] == 1
    assert change["added_count"] == 1
    assert change["modified_count"] == 0
    assert change["deleted_count"] == 0
    assert [(row["path"], row["status"]) for row in change["changed_files"]] == [
        (".crabagent/artifacts/host_current_model_e2e.txt", "added")
    ]

def test_write_benchmark_copy_excludes_runtime_and_nested_output(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "notes.txt").write_text("source\n", encoding="utf-8")
    (source / ".crabagent").mkdir()
    (source / ".crabagent" / "state.sqlite3").write_text("runtime\n", encoding="utf-8")
    output = source / "results"
    output.mkdir()
    (output / "old.json").write_text("result\n", encoding="utf-8")
    destination = output / "direct-workspace"
    _copy_for_write_benchmark(source, destination, excluded_top_level=["results"])
    assert (destination / "notes.txt").read_text(encoding="utf-8") == "source\n"
    assert not (destination / ".crabagent").exists()
    assert not (destination / "results").exists()


def test_clear_code_goal_uses_one_model_turn_and_local_gates(tmp_path: Path) -> None:
    plan = classify_goal("Create hello.txt with one exact line")

    assert plan.kind == "code_execution"
    assert plan.stages == ["KING", "WORKER", "ORACLE"]
    assert plan.estimated_model_turns == 1
    assert plan.ontology_required is False

    snapshot = RuntimeService(tmp_path).plan_mission(
        plan.objective,
        adaptive=True,
    )
    assert [row["role"] for row in snapshot["tasks"]] == plan.stages
    assert [row["provider"] for row in snapshot["assignments"]] == ["local", "codex", "local"]
    worker = next(row for row in snapshot["tasks"] if row["role"] == "WORKER")
    assert worker["write_scope"] == "task_contract_only"
    assert snapshot["goal_plan"]["estimated_model_turns"] == 1


def test_selected_context_does_not_force_ontology_for_plain_code_goal() -> None:
    plan = classify_goal(
        "Create hello.txt with one exact line",
        selected_pack_count=19,
        selected_project_count=2,
    )

    assert plan.kind == "code_execution"
    assert plan.ontology_required is False
    assert plan.stages == ["KING", "WORKER", "ORACLE"]
    assert plan.selected_pack_count == 19
    assert plan.selected_project_count == 2
    assert any("without forcing an ontology mission" in item for item in plan.rationale)


def test_selected_context_keeps_casual_chat_local_to_chat_mode() -> None:
    plan = classify_goal(
        "안녕, 오늘 뭐해?",
        selected_pack_count=19,
        selected_project_count=2,
    )

    assert plan.ontology_required is False
    assert plan.kind == "conversation"
    assert interaction_kind("안녕, 오늘 뭐해?", selected_pack_count=19, selected_project_count=2) == "chat"
    assert interaction_kind("안녕, 오늘 뭐해?", "chat", selected_pack_count=19, selected_project_count=2) == "chat"
    assert interaction_kind("Create hello.txt with one exact line", "chat", selected_pack_count=19, selected_project_count=2) == "chat"


def test_selected_context_routes_implicit_recommendation_through_colony() -> None:
    plan = classify_goal(
        "내 사업 전략을 추천해줘",
        selected_pack_count=1,
    )

    assert plan.ontology_required is True
    assert plan.kind == "ontology_research"
    assert plan.requires_model_queen is True
    assert interaction_kind("내 사업 전략을 추천해줘", selected_pack_count=1) == "colony"
    assert any("active dependency" in item for item in plan.rationale)


def test_selected_travel_pack_routes_itinerary_request_to_queen_synthesis() -> None:
    plan = classify_goal(
        "그리스 여행팩을 바탕으로 5박 6일 여행 코스 짜줘",
        selected_pack_count=1,
    )

    assert plan.ontology_required is True
    assert plan.requires_model_queen is True
    assert plan.action_mode == "explain"
    assert plan.estimated_model_turns >= 1


def test_connected_opencrab_routes_knowledge_goal_without_pack_selection() -> None:
    plan = classify_goal("내 사업 전략을 추천해줘", knowledge_available=True)

    assert plan.ontology_required is True
    assert plan.kind == "ontology_research"
    assert plan.selected_pack_count == 0
    assert plan.retrieval_contract["scope_mode"] == "workspace_auto"
    assert any("default knowledge source" in item for item in plan.rationale)
    assert interaction_kind("내 사업 전략을 추천해줘", knowledge_available=True) == "colony"


def test_connected_opencrab_does_not_intercept_plain_code_edit() -> None:
    plan = classify_goal("Create hello.txt with one exact line", knowledge_available=True)

    assert plan.ontology_required is False
    assert plan.kind == "code_execution"
    assert interaction_kind("Create hello.txt with one exact line", knowledge_available=True) == "colony"


def test_ontology_goal_builds_a_kinetic_evidence_path(tmp_path: Path) -> None:
    objective = "오픈크랩 팩을 조회해서 목표중심 워크플로우의 근거를 요약해줘"
    plan = classify_goal(objective)

    assert plan.kind == "ontology_research"
    assert plan.ontology_required is True
    assert plan.ontology_spaces[:3] == ["subject", "resource", "evidence"]
    assert plan.stages == ["KING", "QUEEN", "SOLDIER", "ORACLE"]
    assert plan.estimated_model_turns == 1
    assert plan.requires_model_queen is True
    assert plan.requires_oracle_model is False
    assert plan.retrieval_contract["mode"] == "evidence_first"
    assert "evidence_id" in plan.retrieval_contract["claim_requirements"]

    snapshot = RuntimeService(tmp_path).plan_mission(objective, adaptive=True)
    assert [row["role"] for row in snapshot["tasks"]] == plan.stages
    assert [row["provider"] for row in snapshot["assignments"]] == ["local", "codex", "local", "local"]
    assert any(row["kind"] == "goal_plan" for row in snapshot["artifacts"])


def test_bounded_ontology_queen_uses_medium_route_and_strategic_queen_stays_high() -> None:
    inverter = SolteluInverter()
    bounded = inverter.plan(Role.QUEEN, "bounded_ontology")
    strategic = inverter.plan(Role.QUEEN)
    assert bounded.model == "gpt-5.6-terra"
    assert bounded.effort == "medium"
    assert strategic.effort == "high"


def test_bounded_ontology_plan_persists_the_medium_queen_route(tmp_path: Path) -> None:
    plan = classify_goal("오픈크랩 팩의 근거를 분석해서 전략을 추천해줘")
    snapshot = RuntimeService(tmp_path).plan_mission(plan.objective, adaptive=True)
    queen = next(row for row in snapshot["assignments"] if row["role"] == "QUEEN")
    assert queen["effort"] == "medium"


def test_ontology_summary_answers_directly_without_forcing_a_next_action() -> None:
    summary = classify_goal("오픈크랩 팩의 근거를 바탕으로 목표중심 워크플로우를 요약해줘")
    assert summary.action_mode == "explain"
    assert summary.action_required is False
    assert summary.response_contract == ["selected_path", "supported_claims", "gaps"]
    assert summary.ontology_mode == "evidence_query"
    assert summary.requires_model_queen is True
    assert summary.estimated_model_turns == 1

    plan = classify_goal("오픈크랩 팩을 바탕으로 목표중심 워크플로우를 설계해줘")
    assert plan.action_mode == "plan"
    assert plan.action_required is True
    assert "next_action" in plan.response_contract


def test_bounded_ontology_write_uses_local_oracle_after_worker() -> None:
    plan = classify_goal("오픈크랩 팩을 바탕으로 파일을 만들어줘")
    assert plan.requires_write is True
    assert plan.requires_model_queen is False
    assert plan.requires_oracle_model is False
    assert plan.estimated_model_turns == 1


def test_ontology_backed_write_keeps_queen_and_worker_in_the_same_goal_contract() -> None:
    plan = classify_goal(
        "오픈크랩 팩의 근거를 활용해서 정확히 하나의 파일 ontology-result.txt를 만들고 한 줄에 OK만 기록해. 다른 파일은 수정하지 마."
    )
    assert plan.kind == "ontology_execution"
    assert plan.stages == ["KING", "QUEEN", "SOLDIER", "WORKER", "ORACLE"]
    assert plan.requires_model_queen is False
    assert plan.requires_write is True
    assert plan.estimated_model_turns == 1
    assert plan.response_contract == ["selected_path", "bounded_change", "verification", "next_action"]


def test_negated_write_signal_does_not_create_workspace_change() -> None:
    plan = classify_goal(
        "오픈크랩 근거로 D01 상태만 확인하고 어떤 파일이나 OpenCrab 데이터도 변경하지 마."
    )
    assert plan.ontology_required is True
    assert plan.requires_write is False
    assert "WORKER" not in plan.stages
    assert plan.action_mode in {"lookup", "explain", "research"}


def test_sejong_read_only_worker_contract_overrides_write_and_graph_heuristics() -> None:
    objective = (
        "새 durable 증거감사 미션을 생성한다. "
        "OpenCrab 자료는 읽기만 하며 변경·인제스트·연결수정은 하지 않는다. "
        "WORKER는 독후감을 작성하지 말고 실제 업무 근거 후보표만 작성하고 개인 역할과 조직 업무를 분리한다. "
        "ORACLE은 provenance, 개인 역할 확인 여부, 민감정보, 수치 충돌만 검증한다. "
        "독후감 수정본은 작성하지 않는다. "
        "그래프 경로가 없어도 lexical·vector·document evidence로 계속하며 graph_required=false로 판정한다."
    )

    plan = classify_goal(objective, selected_project_count=3, knowledge_available=True)

    assert plan.kind == "ontology_research"
    assert plan.ontology_required is True
    assert plan.graph_required is False
    assert plan.ontology_mode != "graph_path"
    assert plan.requires_write is False
    assert plan.requires_worker_output is True
    assert plan.outcome_type == "ontology_brief"
    assert plan.action_mode != "execute"
    assert plan.action_required is False
    assert plan.stages == ["KING", "QUEEN", "SOLDIER", "WORKER", "ORACLE"]
    assert "worker_result_observed" in plan.acceptance_checks
    assert "graph_path_has_bounded_nodes_or_edges" not in plan.acceptance_checks
    assert "workspace_change_receipt_observed" not in plan.acceptance_checks
    assert "workspace_change_is_bounded" not in plan.acceptance_checks


def test_explicit_graph_contract_controls_graph_gate_in_both_directions() -> None:
    disabled = classify_goal("Use OpenCrab evidence; graph_required=false and continue without a graph path.")
    enabled = classify_goal("Use OpenCrab evidence; graph_required=true and inspect the evidence path.")

    assert disabled.graph_required is False
    assert disabled.ontology_mode != "graph_path"
    assert enabled.graph_required is True
    assert enabled.ontology_mode == "graph_path"


def test_positive_write_survives_negative_scope_constraint() -> None:
    plan = classify_goal(
        "오픈크랩 근거로 result.txt를 만들어. 다른 파일은 수정하지 마."
    )
    assert plan.requires_write is True
    assert "WORKER" in plan.stages


def test_negated_opencrab_and_external_scope_do_not_activate_routing() -> None:
    plan = classify_goal(
        "Create exactly one synthetic file named current_model_e2e_20260817.txt in the KINGCRAB mission workspace containing exactly the single line CURRENT_MODEL_E2E_OK. Do not access or modify OpenCrab, external services, user data, or any other file. Verify the bounded workspace change and complete through the normal Oracle gate."
    )
    assert plan.requires_write is True
    assert plan.ontology_required is False
    assert plan.external_scouting is False
    assert "QUEEN" not in plan.stages
    assert "WORKER" in plan.stages


def test_without_opencrab_or_web_does_not_activate_knowledge_scope() -> None:
    plan = classify_goal("Create local.txt with OK only, without OpenCrab, web, internet, or external research.")
    assert plan.requires_write is True
    assert plan.ontology_required is False
    assert plan.external_scouting is False


def test_korean_negated_opencrab_scope_does_not_activate_ontology() -> None:
    plan = classify_goal("result.txt를 만들어. 오픈크랩을 사용하지 말고 외부 서비스 없이 로컬 파일만 수정해.")
    assert plan.requires_write is True
    assert plan.ontology_required is False
    assert plan.external_scouting is False


def test_positive_opencrab_and_external_signals_still_activate_routing() -> None:
    plan = classify_goal("Use OpenCrab evidence and external web research to analyze the architecture.")
    assert plan.ontology_required is True
    assert plan.external_scouting is True


def test_english_do_not_create_is_not_write_intent() -> None:
    plan = classify_goal("Check OpenCrab evidence and do not create files.")
    assert plan.requires_write is False


def test_external_change_prohibition_and_host_synthesis_are_not_workspace_write() -> None:
    plan = classify_goal(
        "외부 변경 금지, 정확히 5문장으로 합성",
        selected_pack_count=1,
    )

    assert plan.requires_write is False
    assert plan.outcome_type != "workspace_change"
    assert plan.action_mode == "explain"
    assert plan.external_scouting is False
    assert plan.requires_model_queen is True
    assert "WORKER" not in plan.stages


@pytest.mark.parametrize(
    "objective",
    [
        "변경 내역을 확인해줘",
        "수정된 파일 목록을 보여줘",
        "변경된 내용을 읽어줘",
        "Show the update history",
        "Show the modified files",
        "List the changes",
    ],
)
def test_change_history_and_observed_changes_are_read_only(objective: str) -> None:
    assert classify_goal(objective).requires_write is False


def test_imperative_change_request_remains_write_intent() -> None:
    assert classify_goal("프로젝트 설정을 수정해줘").requires_write is True
    assert classify_goal("Update the project config").requires_write is True


def test_ontology_requests_leave_direct_chat_in_auto_mode() -> None:
    assert interaction_kind("오픈크랩 팩을 찾아서 근거를 비교해줘") == "colony"
    assert interaction_kind("안녕, 오늘 뭐해?") == "chat"


def test_goal_prompt_has_a_real_bounded_handoff(tmp_path: Path) -> None:
    service = RuntimeService(tmp_path)
    session = service.store.create_session(interaction_mode="colony")
    executor = ColonyExecutor(service, session["session_id"], object(), threading.Event())
    executor.goal_plan = classify_goal("오픈크랩 팩의 근거를 찾아 브랜드 전략으로 정리해줘").to_dict()
    prompt = executor._prompt(Role.QUEEN, executor.goal_plan["objective"], ["previous result " * 500] * 3)
    assert len(prompt) <= executor.goal_plan["context_char_budget"] + 1800


def test_queen_write_prompt_is_read_only_and_hands_off_to_worker(tmp_path: Path) -> None:
    service = RuntimeService(tmp_path)
    session = service.store.create_session(interaction_mode="colony")
    executor = ColonyExecutor(service, session["session_id"], object(), threading.Event())
    executor.goal_plan = classify_goal(
        "오픈크랩 팩의 근거를 활용해서 정확히 하나의 파일 result.txt를 만들고 한 줄에 OK만 기록해"
    ).to_dict()
    prompt = executor._prompt(Role.QUEEN, executor.goal_plan["objective"], [])
    assert "QUEEN IS READ-ONLY" in prompt
    assert "WORKER alone may change the workspace" in prompt


def test_host_worker_prompt_always_carries_artifact_contract(tmp_path: Path) -> None:
    service = RuntimeService(tmp_path)
    session = service.store.create_session(interaction_mode="colony")

    class HostBridge:
        executor_name = "host_current_model"

    executor = ColonyExecutor(service, session["session_id"], HostBridge(), threading.Event())
    executor.goal_plan = classify_goal("오픈크랩 근거를 바탕으로 result.txt를 만들어줘").to_dict()
    prompt = executor._prompt(Role.WORKER, executor.goal_plan["objective"], ["prior output " * 1000] * 3)
    assert "HOST WORKER ARTIFACT CONTRACT" in prompt
    assert 'HOST_WORKER_ARTIFACT_V1:{"relative_path":".crabagent/artifacts/<safe-name>.md","content":"<UTF-8 text with JSON escapes>"}' in prompt
    assert "Use only relative_path and content" in prompt
    assert "never use path, overwrite, extra keys" in prompt
    assert "outer JSON envelope" in prompt


def test_non_mutating_worker_and_oracle_prompts_preserve_read_only_boundary() -> None:
    objective = (
        "OpenCrab 자료는 읽기만 하며 프로젝트·팩·문서를 수정하지 않는다. "
        "WORKER는 evidence ID가 포함된 실제 업무 후보표만 작성한다. "
        "ORACLE은 WORKER 결과와 provenance만 검증한다. graph_required=false."
    )
    with TemporaryDirectory(prefix="kingcrab-readonly-worker-") as temp_dir:
        service = RuntimeService(Path(temp_dir))
        session = service.store.create_session(interaction_mode="colony")
        executor = ColonyExecutor(service, session["session_id"], object(), threading.Event())
        executor.goal_plan = classify_goal(objective, selected_project_count=1).to_dict()

        worker_prompt = executor._prompt(Role.WORKER, objective, ["QUEEN evidence handoff"])
        oracle_prompt = executor._prompt(Role.ORACLE, objective, ["WORKER candidate table"])

        assert executor.goal_plan["requires_write"] is False
        assert executor.goal_plan["requires_worker_output"] is True
        assert "NON-MUTATING WORKER BOUNDARY" in worker_prompt
        assert "Do not create, edit, delete, rename" in worker_prompt
        assert "HOST WORKER ARTIFACT CONTRACT" not in worker_prompt
        assert "require the WORKER artifact" in oracle_prompt


def test_token_gate_uses_uncached_input_not_persistent_thread_total(tmp_path: Path) -> None:
    service = RuntimeService(tmp_path)
    session = service.store.create_session(interaction_mode="colony")
    executor = ColonyExecutor(service, session["session_id"], object(), threading.Event())
    executor.goal_plan = {"token_budget": 3200}
    executor.observed_tokens = 29607
    executor.observed_billable_input_tokens = 1186
    executor._enforce_token_gate("mission-budget", Role.WORKER, "task-worker")


def test_explain_mode_closes_queen_next_action_without_leaking_coordination(tmp_path: Path) -> None:
    service = RuntimeService(tmp_path)
    session = service.store.create_session(interaction_mode="colony")
    executor = ColonyExecutor(service, session["session_id"], object(), threading.Event())
    executor.goal_plan = classify_goal("오픈크랩 팩의 근거를 요약해줘").to_dict()
    normalized = executor._normalize_queen_interpretation(
        "SELECTED_PATH\nobserved path\nSUPPORTED_CLAIMS\nobserved claim\nGAPS\nnone\n"
        "NEXT_ACTION\nKING 단계에서 다시 목표를 확인하라"
    )
    assert normalized.endswith("NEXT_ACTION\nSTOP")


def test_benchmark_event_reader_pages_past_the_daemon_limit() -> None:
    from crabagent.benchmark_runner import _session_events

    class Client:
        def __init__(self) -> None:
            self.cursors = []

        def request(self, action, **payload):
            assert action == "events.since"
            self.cursors.append(payload["event_id"])
            start = int(payload["event_id"]) + 1
            if start == 1:
                return {"events": [{"event_id": index, "payload": {}} for index in range(1, 2001)]}
            return {"events": [{"event_id": 2001, "payload": {"session_id": "target"}}]}

    client = Client()
    events = _session_events(client, "target")
    assert client.cursors == [0, 2000]
    assert [row["event_id"] for row in events] == [2001]


def test_queen_handoff_accepts_a_unique_abbreviated_evidence_id(tmp_path: Path) -> None:
    service = RuntimeService(tmp_path)
    session = service.store.create_session(interaction_mode="colony")
    executor = ColonyExecutor(service, session["session_id"], object(), threading.Event())
    executor.opencrab_receipt = {
        "evidence": [{"id": "12345678-aaaa-bbbb-cccc-ddddeeeeffff"}],
    }
    quality = executor._queen_handoff_quality([
        "QUEEN INTERPRETATION:\n"
        "SELECTED_PATH\npath\n"
        "SUPPORTED_CLAIMS\nclaim [evidence_id: 12345678-…]\n"
        "NEXT_ACTION\noracle_review"
    ])
    assert quality["accepted"] is True
    assert quality["citation_modes"]["12345678-aaaa-bbbb-cccc-ddddeeeeffff"] == "unique_id_prefix"


def test_goal_benchmark_exposes_planned_route_reduction() -> None:
    result = compare_goal("Create hello.txt with one exact line")
    assert result["measurement"] == "planned_only"
    assert result["direct_baseline"]["planned_model_routes"] == 1
    assert result["adaptive"]["planned_model_routes"] == 1
    assert result["delta"]["planned_model_routes"] == 0
    assert result["adaptive"]["local_gates"] == 2
    assert "superiority requires observed" in result["guardrail"]


def test_benchmark_suite_is_explicit_and_plan_only() -> None:
    selected = select_cases(["code_write", "semantic_ontology"])
    assert [row["id"] for row in selected] == ["code_write", "semantic_ontology"]
    result = plan_suite(case_ids=["code_write", "semantic_ontology"], selected_pack_count=1)
    assert result["measurement"] == "planned_only"
    assert [row["id"] for row in result["cases"]] == ["code_write", "semantic_ontology"]
    assert result["cases"][1]["plan"]["adaptive"]["roles"] == ["KING", "QUEEN", "SOLDIER", "ORACLE"]


def test_structural_quality_reports_kinetic_advantage_without_semantic_claim() -> None:
    adaptive = {
        "mission": {"mission_id": "adaptive", "status": "completed", "oracle_result_artifact_id": "oracle-a"},
        "goal_plan": {"ontology_required": True},
        "benchmark": {"result_accepted": True},
        "budget": {"tokens_observed": 900, "observation_source": "test"},
        "tool_receipts": [
            {"tool_name": "codex.app-server.turn", "status": "success"},
            {"tool_name": "opencrab.mcp.opencrab_query", "status": "success"},
            {"tool_name": "crab.role_gate", "status": "success"},
        ],
        "artifacts": [{"status": "accepted", "kind": "ontology_ledger"}],
        "evidence": [{"evidence_id": "mcp:ev-1"}],
    }
    direct = {
        "mission": {"mission_id": "direct", "status": "completed"},
        "goal_plan": {"ontology_required": True},
        "benchmark": {"result_accepted": True},
        "budget": {"tokens_observed": 1800, "observation_source": "test"},
        "tool_receipts": [{"tool_name": "opencrab.mcp.opencrab_query", "status": "success"}],
        "artifacts": [],
        "evidence": [{"evidence_id": "direct:ev-1"}],
    }
    result = compare_observed(adaptive, direct)
    quality = result["structural_quality"]
    assert quality["adaptive_advantage"] is True
    assert quality["adaptive"]["not_a_semantic_judge"] is True


def test_benchmark_suite_summary_does_not_claim_universal_superiority() -> None:
    result = summarize_suite(
        [{
            "comparison": {
                "verdict": "adaptive_total_token_advantage_cache_sensitive",
                "quality_parity": True,
                "structural_quality": {"adaptive_advantage": True},
                "efficiency": {"total_token_advantage": True, "billable_input_advantage": False},
                "delta": {"tokens_observed": -100},
            }
        }],
        suite_id="suite-test",
    )
    assert result["claim"] == "adaptive_structural_advantage_observed"
    assert result["goal_contract_accepted_count"] == 0
    assert "universal claim" in result["limitations"][-1]


def test_ledger_lookup_does_not_trigger_edge_graph_signal() -> None:
    plan = classify_goal(
        "Use OpenCrab evidence to verify E4 Promotion Decision Ledger exists in this project. Read-only."
    )
    assert plan.ontology_required is True
    assert plan.graph_required is False
    assert plan.ontology_mode != "graph_path"
    assert plan.requires_model_queen is False
    assert plan.estimated_model_turns == 0


def test_explicit_english_edge_path_still_requires_graph() -> None:
    plan = classify_goal("Use OpenCrab evidence to inspect the edge path for E4.")
    assert plan.graph_required is True
    assert plan.ontology_mode == "graph_path"


def test_simple_graph_goal_keeps_oracle_local_for_token_efficiency() -> None:
    plan = classify_goal("오픈크랩 그래프의 관계와 연결 경로를 확인해줘")
    assert plan.graph_required is True
    assert plan.requires_oracle_model is False
    assert plan.estimated_model_turns == 1


def test_exact_ontology_lookup_uses_local_queen_projection() -> None:
    plan = classify_goal("내 오픈크랩 팩 목록을 보여줘")
    assert plan.ontology_required is True
    assert plan.requires_model_queen is False
    assert plan.estimated_model_turns == 0
    assert RuntimeService._uses_local_gate(plan, Role.QUEEN) is True


def test_observed_benchmark_requires_quality_before_efficiency_claim() -> None:
    adaptive = {
        "mission": {"mission_id": "adaptive", "status": "completed", "oracle_result_artifact_id": "oracle-a"},
        "goal_plan": {"ontology_required": True},
        "budget": {"tokens_observed": 900, "observation_source": "test"},
        "tool_receipts": [
            {"tool_name": "codex.app-server.turn", "status": "success"},
            {"tool_name": "opencrab.mcp.opencrab_query", "status": "success"},
        ],
        "artifacts": [{"status": "accepted", "kind": "ontology_ledger"}],
        "evidence": [{"evidence_id": "mcp:ev-1"}],
    }
    direct = {
        "mission": {"mission_id": "direct", "status": "completed", "oracle_result_artifact_id": "oracle-d"},
        "tool_receipts": [{"tool_name": "opencrab.mcp.opencrab_query", "status": "success"}],
        "budget": {"tokens_observed": 1800, "observation_source": "test"},
        "artifacts": [{"status": "accepted"}],
        "evidence": [{"evidence_id": "direct:ev-1"}],
    }
    result = compare_observed(adaptive, direct)
    assert result["verdict"] == "adaptive_efficiency_advantage"
    assert result["delta"]["tokens_observed"] == -900

    direct["budget"]["tokens_observed"] = None
    assert compare_observed(adaptive, direct)["verdict"] == "inconclusive"
    direct["budget"]["tokens_observed"] = 1800
    direct["tool_receipts"] = []
    direct["evidence"] = []
    missing = compare_observed(adaptive, direct)
    assert missing["verdict"] == "inconclusive"
    assert "direct_ontology_receipt" in missing["unknowns"]


def test_direct_benchmark_result_gate_does_not_accept_unverified_ontology_answer() -> None:
    plan = classify_goal("오픈크랩 그래프의 관계와 연결 경로를 확인해줘").to_dict()
    completed = {
        "event_type": "conversation_turn_completed",
        "payload": {"tokens_observed": 20},
    }
    blocked = {"status": "no_evidence", "claim_gate": "blocked", "graph_gate": "blocked"}
    passed = {
        "status": "ok",
        "claim_gate": "pass",
        "graph_gate": "pass",
        "evidence": [{"id": "ev-12345678-aaaa"}],
    }

    assert not _direct_result_accepted(plan, blocked, completed, "그럴듯한 답변")
    assert not _direct_result_accepted(plan, passed, completed, "근거가 있는 답변")
    assert _direct_result_accepted(
        plan,
        passed,
        completed,
        "1. 근거 경로를 확인한다 [evidence_id: ev-12345678]\n2. 결과를 검증한다 [evidence_id: ev-12345678]",
    )


def test_direct_answer_grounding_accepts_full_or_unique_evidence_prefix() -> None:
    receipt = {"evidence": [{"id": "ev-12345678-aaaa"}, {"id": "different-9999"}]}
    quality = _answer_grounding_quality("claim [evidence_id: ev-12345678]", receipt)
    assert quality["accepted"] is True
    assert quality["evidence_citation_count"] == 1


def test_observed_benchmark_counts_verified_zero_model_gate_as_zero_tokens() -> None:
    adaptive = {
        "mission": {"mission_id": "adaptive-gate", "status": "failed"},
        "goal_plan": {"ontology_required": True},
        "budget": {"tokens_observed": None, "observation_source": "not_observed"},
        "tool_receipts": [{"tool_name": "opencrab.mcp.opencrab_query", "status": "success"}],
        "artifacts": [],
        "evidence": [],
    }
    direct = {
        "mission": {"mission_id": "direct", "status": "completed"},
        "budget": {"tokens_observed": 1000, "observation_source": "test"},
        "tool_receipts": [{"tool_name": "codex.app-server.turn", "status": "success"}],
        "artifacts": [],
        "evidence": [],
    }
    result = compare_observed(adaptive, direct)
    assert result["adaptive"]["tokens_observed"] == 0
    assert result["adaptive"]["measurement_source"] == "no_model_turn_observed"
    assert "adaptive_ontology_ledger" not in result["unknowns"]


def test_completed_local_ontology_projection_counts_as_zero_model_tokens() -> None:
    adaptive = {
        "mission": {"mission_id": "adaptive-list", "status": "completed", "oracle_result_artifact_id": "oracle-a"},
        "benchmark": {"result_accepted": True},
        "goal_plan": {"ontology_required": True},
        "budget": {"tokens_observed": None, "observation_source": "not_observed"},
        "tool_receipts": [
            {"tool_name": "opencrab.mcp.opencrab_query", "status": "success"},
            {"tool_name": "crab.role_gate", "status": "success"},
        ],
        "artifacts": [{"status": "accepted", "kind": "ontology_ledger"}],
        "evidence": [{"evidence_id": "mcp:ev-1"}],
    }
    direct = {
        "mission": {"mission_id": "direct-list", "status": "completed"},
        "benchmark": {"result_accepted": True},
        "goal_plan": {"ontology_required": True},
        "budget": {"tokens_observed": 1200, "observation_source": "test"},
        "tool_receipts": [
            {"tool_name": "opencrab.mcp.opencrab_query", "status": "success"},
            {"tool_name": "codex.app-server.turn", "status": "success", "observed": {"usage": {"last": {"inputTokens": 1200, "cachedInputTokens": 0}}}},
        ],
        "artifacts": [{"status": "accepted"}],
        "evidence": [{"evidence_id": "direct:ev-1"}],
    }

    result = compare_observed(adaptive, direct)
    assert result["adaptive"]["tokens_observed"] == 0
    assert result["adaptive"]["billable_input_tokens_observed"] == 0
    assert result["verdict"] == "adaptive_efficiency_advantage"


def test_goal_contract_quality_does_not_accept_uuid_only_graph_paths(tmp_path: Path) -> None:
    oracle_path = tmp_path / "oracle_result.md"
    oracle_path.write_text(
        json.dumps(
            {
                "verdict": "accepted",
                "context_quality": {
                    "path_count": 1,
                    "graph_semantic_gate": "weak",
                },
                "queen_handoff_quality": {
                    "accepted": True,
                    "section_count": 4,
                    "evidence_citation_count": 1,
                },
            }
        ),
        encoding="utf-8",
    )
    snapshot = {
        "schema": "crab.observed-benchmark-snapshot/v1",
        "mission": {"status": "completed", "oracle_result_artifact_id": "oracle-1"},
        "goal_plan": {
            "objective": "오픈크랩 그래프의 근거 경로를 확인해줘",
            "ontology_required": True,
            "graph_required": True,
        },
        "benchmark": {"result_accepted": True},
        "budget": {"tokens_observed": 100},
        "tool_receipts": [
            {"tool_name": "opencrab.mcp.opencrab_query", "status": "success"},
            {"tool_name": "crab.role_gate", "status": "success"},
        ],
        "artifacts": [
            {"kind": "oracle_result", "status": "accepted", "path": str(oracle_path)},
            {"kind": "ontology_ledger", "status": "accepted", "path": str(oracle_path)},
        ],
        "evidence": [{"evidence_id": "mcp:ev-1"}],
    }
    quality = observed_metrics(snapshot)["goal_contract_quality"]
    assert quality["accepted"] is False
    assert quality["graph_semantic_gate"] == "weak"
    assert "topology_observed_but_semantic_endpoint_unresolved" in quality["reasons"]


def test_observed_benchmark_rejects_unknown_run_order_before_starting_runtime(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="order must be direct-first or adaptive-first"):
        run_observed_benchmark(tmp_path, "bounded goal", order="randomized")


def _mcp_context() -> dict:
    return {
        "status": "ok",
        "answer": "브랜드 관련 evidence가 확인되었습니다.",
        "evidence": [{
            "id": "ev-1",
            "document_id": "doc-1",
            "workspace_id": "ws-1",
            "text": "브랜드 일관성은 신뢰 형성에 기여한다.",
            "score": 0.86,
            "source": "opencrab://pack-brand/chunk-1",
            "metadata": {"package_id": "pack-brand"},
            "retrieval": {"lexical": 0.9, "vector": 0.8},
        }],
        "retrieval": {"scanned": 1, "top_k": 1},
        "pack_scope": {"packages": 1, "package_ids": ["pack-brand"]},
    }


def test_transient_opencrab_context_failure_revalidates_and_retries_once(tmp_path: Path) -> None:
    service = RuntimeService(tmp_path)
    session = service.store.create_session(interaction_mode="colony")
    calls = []

    def loader(arguments):
        if arguments.get("__kingcrab_tool__") == "opencrab_status":
            calls.append("opencrab_status")
            return {"status": "ok"}
        calls.append("opencrab_query")
        if calls.count("opencrab_query") == 1:
            raise TimeoutError("simulated MCP context timeout")
        return _mcp_context()

    class Bridge:
        thread_id = "thread-opencrab-retry"
        last_start_mode = "started"

        def start(self) -> str:
            return self.thread_id

        def run_turn(self, prompt, emit, **kwargs):
            return CodexLiveTurn(
                thread_id=self.thread_id,
                turn_id="turn-queen",
                status="completed",
                text=(
                    "SELECTED_PATH\n브랜드 근거 경로\n"
                    "SUPPORTED_CLAIMS\n브랜드 근거가 확인됨 [evidence_id: ev-1]\n"
                    "GAPS\n추가 공백 없음\n"
                    "NEXT_ACTION\nSTOP"
                ),
                usage={"last": {"totalTokens": 25}},
                started_at="now",
                finished_at="now",
            )

    executor = ColonyExecutor(
        service,
        session["session_id"],
        Bridge(),
        threading.Event(),
        opencrab_context_loader=loader,
    )
    snapshot = executor.run(
        "오픈크랩 팩을 조회해서 브랜드 전략에 도움이 될 근거를 찾아줘",
        max_workers=1,
        worker_policy="fixed",
    )

    assert snapshot["mission"]["status"] == "completed"
    assert calls == ["opencrab_query", "opencrab_status", "opencrab_query"]
    context_artifact = next(row for row in snapshot["artifacts"] if row["kind"] == "mcp_context_receipt")
    receipt = json.loads(Path(context_artifact["path"]).read_text(encoding="utf-8"))
    assert receipt["retry"]["policy"] == "read_only_bounded_once"
    assert receipt["retry"]["retried"] is True
    assert receipt["retry"]["attempt_count"] == 2
    assert receipt["retry"]["canary"]["scope_revalidated"] is True
    assert receipt["retry"]["attempts"][0]["error_code"] == "OPENCRAB_MCP_TIMEOUT"
    assert receipt["retry"]["attempts"][0]["retryable"] is True
    before_reuse = list(calls)
    executor._load_opencrab_context("already-completed", {}, "attempt-unused", "same context")
    assert calls == before_reuse


def test_adaptive_colony_executes_only_the_required_model_turns(tmp_path: Path) -> None:
    service = RuntimeService(tmp_path)
    session = service.store.create_session(interaction_mode="colony")

    class Bridge:
        thread_id = "thread-adaptive"
        last_start_mode = "started"

        def __init__(self) -> None:
            self.calls = []

        def start(self) -> str:
            return self.thread_id

        def run_turn(self, prompt, emit, **kwargs):
            self.calls.append({"prompt": prompt, **kwargs})
            (tmp_path / "hello.txt").write_text("hello\n", encoding="utf-8")
            return CodexLiveTurn(
                thread_id=self.thread_id,
                turn_id="turn-%d" % len(self.calls),
                status="completed",
                text="bounded worker result",
                usage={"last": {"totalTokens": 111}},
                started_at="now",
                finished_at="now",
            )

    bridge = Bridge()
    snapshot = ColonyExecutor(
        service,
        session["session_id"],
        bridge,
        threading.Event(),
    ).run("Create hello.txt with one exact line", max_workers=1, worker_policy="fixed")

    assert snapshot["mission"]["status"] == "completed"
    assert len(bridge.calls) == 1
    assert [row["role"] for row in snapshot["assignments"]] == ["KING", "WORKER", "ORACLE"]
    assert snapshot["budget"]["tokens_observed"] == 111
    assert snapshot["mission"]["oracle_result_artifact_id"]
    model_receipt = next(row for row in snapshot["tool_receipts"] if row["tool_name"] == "codex.app-server.turn")
    assert model_receipt["observed"]["prompt_chars"] > 0


def test_host_model_policy_never_starts_codex_or_creates_codex_attempt(tmp_path: Path) -> None:
    service = RuntimeService(tmp_path)
    session = service.store.create_session(interaction_mode="colony", model_policy="host")

    class Bridge:
        thread_id = ""
        last_start_mode = "not_started"

        def start(self) -> str:
            raise AssertionError("host-model sessions must not start Codex")

        def run_turn(self, *args, **kwargs):
            raise AssertionError("host-model sessions must not invoke Codex")

    snapshot = ColonyExecutor(
        service,
        session["session_id"],
        Bridge(),
        threading.Event(),
    ).run(
        "Design a security architecture migration and verify the result",
        max_workers=1,
        worker_policy="fixed",
    )

    assert snapshot["mission"]["status"] == "failed"
    assert snapshot["attempts"] == []
    assert not any(row["tool_name"] == "codex.app-server.turn" for row in snapshot["tool_receipts"])
    assert any(
        row.event_type == "host_model_execution_required"
        for row in service.store.events(snapshot["mission"]["mission_id"])
    )
    messages = service.store.messages(session["session_id"])
    assert any("HOST_MODEL_EXECUTOR_UNAVAILABLE" in row["content"] for row in messages)


def test_ontology_colony_requires_direct_mcp_receipt(tmp_path: Path) -> None:
    service = RuntimeService(tmp_path)
    session = service.store.create_session(interaction_mode="colony")

    class Bridge:
        thread_id = "thread-ontology"
        last_start_mode = "started"

        def __init__(self, responses):
            self.responses = list(responses)
            self.calls = []

        def start(self) -> str:
            return self.thread_id

        def run_turn(self, prompt, emit, **kwargs):
            self.calls.append({"prompt": prompt, **kwargs})
            text = self.responses.pop(0)
            return CodexLiveTurn(
                thread_id=self.thread_id,
                turn_id="turn-%d" % len(self.calls),
                status="completed",
                text=text,
                usage={"last": {"totalTokens": 100}},
                started_at="now",
                finished_at="now",
            )

    bridge = Bridge([
        "SELECTED_PATH\n"
        "Fable pack -> brand consistency -> evidence\n"
        "SUPPORTED_CLAIMS\n"
        "확인된 브랜드 evidence가 일관성 경로를 지지한다. [evidence_id: ev-1]\n"
        "GAPS\n"
        "추가 맥락은 확인하지 않았다.\n"
        "NEXT_ACTION\n"
        "oracle_review"
    ])
    snapshot = ColonyExecutor(service, session["session_id"], bridge, threading.Event(), opencrab_context_loader=lambda args: _mcp_context()).run(
        "오픈크랩 팩을 조회해서 브랜드 전략에 도움이 될 근거를 찾아줘",
        max_workers=1,
        worker_policy="fixed",
    )

    assert snapshot["mission"]["status"] == "completed"
    assert len(bridge.calls) == 1
    assert any(row["kind"] == "mcp_context_receipt" for row in snapshot["artifacts"])
    assert any(row["kind"] == "ontology_ledger" for row in snapshot["artifacts"])
    assert any(row.event_type == "opencrab_context_observed" for row in service.store.events(snapshot["mission"]["mission_id"]))
    assert snapshot["evidence"]
    assert any(
        row.get("metadata", {}).get("role") == "ORACLE" and row.get("metadata", {}).get("status") == "accepted"
        for row in service.store.messages(session["session_id"])
    )
    oracle_messages = [
        row["content"]
        for row in service.store.messages(session["session_id"])
        if row.get("metadata", {}).get("role") == "ORACLE"
    ]
    assert oracle_messages and oracle_messages[-1].rstrip().endswith("NEXT_ACTION\nSTOP")


def test_exact_ontology_lookup_avoids_model_turn_but_keeps_mcp_and_oracle_receipts(tmp_path: Path) -> None:
    service = RuntimeService(tmp_path)
    session = service.store.create_session(interaction_mode="colony")

    class Bridge:
        thread_id = "thread-local-lookup"
        last_start_mode = "not_started"

        def start(self) -> str:
            raise AssertionError("exact lookup should not start Codex")

    snapshot = ColonyExecutor(
        service,
        session["session_id"],
        Bridge(),
        threading.Event(),
        opencrab_context_loader=lambda args: _mcp_context(),
    ).run(
        "내 오픈크랩 팩 목록을 보여줘",
        max_workers=1,
        worker_policy="fixed",
    )

    assert snapshot["mission"]["status"] == "completed"
    assert snapshot["mission"]["oracle_result_artifact_id"]
    assert not any(row["tool_name"] == "codex.app-server.turn" for row in snapshot["tool_receipts"])
    assert any(row["tool_name"] == "opencrab.mcp.opencrab_query" for row in snapshot["tool_receipts"])
    queen_assignment = next(row for row in snapshot["assignments"] if row["role"] == "QUEEN")
    assert queen_assignment["provider"] == "local"
    assert any(row["kind"] == "ontology_ledger" for row in snapshot["artifacts"])


def test_completed_colony_publishes_one_user_facing_goal_outcome(tmp_path: Path) -> None:
    service = RuntimeService(tmp_path)
    session = service.store.create_session(interaction_mode="colony")

    class Bridge:
        thread_id = "thread-outcome"
        last_start_mode = "not_started"

        def start(self) -> str:
            raise AssertionError("bounded evidence lookup must stay local")

    snapshot = ColonyExecutor(
        service,
        session["session_id"],
        Bridge(),
        threading.Event(),
        opencrab_context_loader=lambda args: _mcp_context(),
    ).run(
        "내 오픈크랩 팩 목록을 보여줘",
        max_workers=1,
        worker_policy="fixed",
    )

    outcome = [row for row in snapshot["artifacts"] if row["kind"] == "goal_outcome"]
    assert len(outcome) == 1
    payload = json.loads(Path(outcome[0]["path"]).read_text(encoding="utf-8"))
    assert payload["schema"] == "crab.goal-outcome/v1"
    assert payload["accepted"] is True
    assert payload["evidence"]["count"] > 0
    assert payload["execution"]["model_turns"] == 0
    assert any(row.get("metadata", {}).get("goal_outcome") for row in service.store.messages(session["session_id"]))


def test_bounded_ontology_brief_avoids_model_turn_but_keeps_source_path(tmp_path: Path) -> None:
    service = RuntimeService(tmp_path)
    session = service.store.create_session(interaction_mode="colony")

    class Bridge:
        thread_id = "thread-local-brief"
        last_start_mode = "not_started"

        def start(self) -> str:
            raise AssertionError("bounded evidence brief should not start Codex")

    snapshot = ColonyExecutor(
        service,
        session["session_id"],
        Bridge(),
        threading.Event(),
        opencrab_context_loader=lambda args: _mcp_context(),
    ).run(
        "오픈크랩 팩의 근거를 요약해줘",
        max_workers=1,
        worker_policy="fixed",
    )

    assert snapshot["mission"]["status"] == "completed"
    assert not any(row["tool_name"] == "codex.app-server.turn" for row in snapshot["tool_receipts"])
    queen_artifact = next(row for row in snapshot["artifacts"] if row["kind"] == "local_role_contract" and row["task_id"] in {
        task["task_id"] for task in snapshot["tasks"] if task["role"] == "QUEEN"
    })
    content = Path(queen_artifact["path"]).read_text(encoding="utf-8")
    assert "evidence_brief" in content
    assert "evidence_id" in content
    assert "source:" in content


def test_ontology_evidence_ids_are_namespaced_per_mission(tmp_path: Path) -> None:
    service = RuntimeService(tmp_path)

    class Bridge:
        thread_id = "thread-reused-evidence"
        last_start_mode = "started"

        def start(self) -> str:
            return self.thread_id

        def run_turn(self, prompt, emit, **kwargs):
            return CodexLiveTurn(
                thread_id=self.thread_id,
                turn_id="turn-reused-evidence",
                status="completed",
                text=(
                    "SELECTED_PATH\n"
                    "brand -> evidence\n"
                    "SUPPORTED_CLAIMS\n"
                    "근거 기반 경로다. [evidence_id: ev-1]\n"
                    "GAPS\n"
                    "없음\n"
                    "NEXT_ACTION\n"
                    "oracle_review"
                ),
                usage={"last": {"totalTokens": 25}},
                started_at="now",
                finished_at="now",
            )

    session = service.store.create_session(interaction_mode="colony")
    for _ in range(2):
        snapshot = ColonyExecutor(
            service,
            session["session_id"],
            Bridge(),
            threading.Event(),
            opencrab_context_loader=lambda args: _mcp_context(),
        ).run(
            "오픈크랩 팩을 조회해서 브랜드 전략에 도움이 될 근거를 찾아줘",
            max_workers=1,
            worker_policy="fixed",
        )
        assert snapshot["mission"]["status"] == "completed"

    with service.store.connection() as connection:
        evidence = connection.execute(
            "SELECT evidence_id FROM evidence_refs ORDER BY created_at"
        ).fetchall()
    evidence_ids = [row["evidence_id"] for row in evidence]
    assert len(evidence_ids) == len(set(evidence_ids))
    assert sum(value.startswith("mcp:mission-") for value in evidence_ids) == 2


def test_soldier_rejects_a_queen_handoff_without_grounded_structure(tmp_path: Path) -> None:
    service = RuntimeService(tmp_path)
    session = service.store.create_session(interaction_mode="colony")

    class Bridge:
        thread_id = "thread-bad-queen-handoff"
        last_start_mode = "started"

        def start(self) -> str:
            return self.thread_id

        def run_turn(self, prompt, emit, **kwargs):
            return CodexLiveTurn(
                thread_id=self.thread_id,
                turn_id="turn-bad-queen-handoff",
                status="completed",
                text="브랜드 전략을 추천합니다.",
                usage={"last": {"totalTokens": 30}},
                started_at="now",
                finished_at="now",
            )

    snapshot = ColonyExecutor(
        service,
        session["session_id"],
        Bridge(),
        threading.Event(),
        opencrab_context_loader=lambda args: _mcp_context(),
    ).run(
        "오픈크랩 팩을 조회해서 브랜드 전략에 도움이 될 근거를 찾아줘",
        max_workers=1,
        worker_policy="fixed",
    )

    assert snapshot["mission"]["status"] == "cancelled"
    assert snapshot["mission"]["oracle_result_artifact_id"] is None
    report = next(row for row in snapshot["artifacts"] if row["kind"] == "soldier_patrol")
    report_data = json.loads(Path(report["path"]).read_text(encoding="utf-8"))
    assert report_data["decision"] == "stop"
    assert "queen_handoff_gate_blocked" in report_data["stop_reasons"]


def test_soldier_rejects_uuid_only_graph_topology(tmp_path: Path) -> None:
    service = RuntimeService(tmp_path)
    session = service.store.create_session(interaction_mode="colony")

    class Bridge:
        thread_id = "thread-weak-graph"
        last_start_mode = "started"

        def start(self) -> str:
            return self.thread_id

        def run_turn(self, prompt, emit, **kwargs):
            return CodexLiveTurn(
                thread_id=self.thread_id,
                turn_id="turn-weak-graph",
                status="completed",
                text=(
                    "SELECTED_PATH\nUUID topology only\n"
                    "SUPPORTED_CLAIMS\nObserved [evidence_id: ev-1]\n"
                    "GAPS\nNode labels unavailable\n"
                    "NEXT_ACTION\nResolve node labels"
                ),
                usage={"last": {"totalTokens": 20}},
                started_at="now",
                finished_at="now",
            )

    context = _mcp_context()
    context.update(
        {
            "graph_gate": "weak",
            "paths": [{"from": {"id": "node-a"}, "relation": "mentions", "to": {"id": "node-b"}}],
            "quality": {
                "path_count": 1,
                "graph_semantic_gate": "weak",
                "resolved_path_count": 0,
                "unresolved_endpoint_count": 2,
            },
        }
    )
    snapshot = ColonyExecutor(
        service,
        session["session_id"],
        Bridge(),
        threading.Event(),
        opencrab_context_loader=lambda args: context,
    ).run(
        "오픈크랩 그래프에서 근거와 결과를 연결하는 노드와 엣지 경로를 확인해줘",
        max_workers=1,
        worker_policy="fixed",
    )

    assert snapshot["mission"]["status"] == "failed"
    assert snapshot["mission"]["oracle_result_artifact_id"] is None
    blocked = next(row for row in snapshot["artifacts"] if row["kind"] == "oracle_blocked")
    blocked_data = json.loads(Path(blocked["path"]).read_text(encoding="utf-8"))
    assert "OpenCrab graph gate blocked" in blocked_data["reason"]
    assert blocked_data["partial_graph"]["accepted"] is False
    assert blocked_data["next_action"].startswith("Resolve OpenCrab graph endpoint labels")


def test_ontology_colony_stops_without_mcp_evidence_before_queen_turn(tmp_path: Path) -> None:
    service = RuntimeService(tmp_path)
    session = service.store.create_session(interaction_mode="colony")

    class Bridge:
        thread_id = "thread-invalid-ontology"
        last_start_mode = "started"

        def __init__(self):
            self.calls = 0

        def start(self) -> str:
            return self.thread_id

        def run_turn(self, prompt, emit, **kwargs):
            self.calls += 1
            return CodexLiveTurn(
                thread_id=self.thread_id,
                turn_id="turn-invalid",
                status="completed",
                text="근거가 충분하므로 브랜드 전략을 추천합니다.",
                usage={"last": {"totalTokens": 100}},
                started_at="now",
                finished_at="now",
            )

    bridge = Bridge()
    snapshot = ColonyExecutor(service, session["session_id"], bridge, threading.Event(), opencrab_context_loader=lambda args: {"status": "ok", "evidence": []}).run(
        "오픈크랩 팩을 조회해서 브랜드 전략에 도움이 될 근거를 찾아줘",
        max_workers=1,
        worker_policy="fixed",
    )

    assert snapshot["mission"]["status"] == "failed"
    assert bridge.calls == 0
    assert snapshot["mission"]["oracle_result_artifact_id"] is None
    assert any(row.event_type == "opencrab_context_observed" for row in service.store.events(snapshot["mission"]["mission_id"]))
    blocked_artifact = next(row for row in snapshot["artifacts"] if row["kind"] == "oracle_blocked")
    assert json.loads(Path(blocked_artifact["path"]).read_text(encoding="utf-8"))["model_invocation"] is False
    messages = service.store.messages(session["session_id"])
    assert any(row.get("metadata", {}).get("status") == "blocked" for row in messages)


def test_token_gate_stops_before_oracle_model_call(tmp_path: Path) -> None:
    service = RuntimeService(tmp_path)
    session = service.store.create_session(interaction_mode="colony")

    class Bridge:
        thread_id = "thread-budget"
        last_start_mode = "started"

        def __init__(self):
            self.calls = 0

        def start(self) -> str:
            return self.thread_id

        def run_turn(self, prompt, emit, **kwargs):
            self.calls += 1
            return CodexLiveTurn(
                thread_id=self.thread_id,
                turn_id="turn-budget",
                status="completed",
                text="퀸 해석은 직접 MCP receipt를 기준으로 진행한다.",
                usage={"last": {"totalTokens": 20000}},
                started_at="now",
                finished_at="now",
            )

    bridge = Bridge()
    snapshot = ColonyExecutor(service, session["session_id"], bridge, threading.Event(), opencrab_context_loader=lambda args: _mcp_context()).run(
        "오픈크랩 팩을 외부 연구와 비교해서 브랜드 전략에 도움이 될 근거를 찾아줘",
        max_workers=1,
        worker_policy="fixed",
    )

    assert snapshot["mission"]["status"] == "failed"
    assert bridge.calls == 1
    assert snapshot["budget"]["tokens_observed"] == 20000
    assert any(row.event_type == "token_budget_gate" for row in service.store.events(snapshot["mission"]["mission_id"]))
