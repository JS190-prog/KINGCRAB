import pytest

from crabagent.goal import classify_goal, objective_artifact_paths


@pytest.mark.parametrize("objective", [
    "SemIf 근거를 인계한다. KING→QUEEN→SOLDIER→WORKER→ORACLE 전체 경로를 수행한다.",
    "OpenCrab 근거를 확인하고 허용된 workspace 실행 경로에서 파일을 생성한다.",
    "Use OpenCrab evidence and create a file at the workspace path.",
    "Use OpenCrab evidence to verify the full execution path of five roles.",
])
def test_execution_and_file_path_words_do_not_require_graph_edges(objective):
    plan = classify_goal(objective, selected_pack_count=1, forced="full")
    assert plan.ontology_required is True
    assert plan.stages == ["KING", "QUEEN", "SOLDIER", "WORKER", "ORACLE"]
    assert plan.graph_required is False
    assert plan.retrieval_contract["mode"] == "evidence_first"
    assert "graph_path_has_bounded_nodes_or_edges" not in plan.acceptance_checks


@pytest.mark.parametrize("objective", [
    "OpenCrab 그래프의 연결 경로를 검증한다.",
    "Use OpenCrab evidence to inspect the bounded node-to-node path.",
    "Use OpenCrab evidence to inspect the edge path.",
    "OpenCrab 근거를 확인한다. graph_required=true.",
])
def test_explicit_graph_requests_keep_the_required_graph_gate(objective):
    plan = classify_goal(objective, selected_pack_count=1, forced="full")
    assert plan.graph_required is True
    assert plan.retrieval_contract["mode"] == "graph_path"
    assert "graph_path_has_bounded_nodes_or_edges" in plan.acceptance_checks


@pytest.mark.parametrize("suffix", ["를", "에", "에서", "만", "으로", "는", "와", "도"])
def test_korean_case_particles_are_outside_the_exact_artifact_locator(suffix):
    path = ".crabagent/artifacts/source.txt"
    objective = f"WORKER가 새 파일 {path}{suffix} 독점 생성한다. 다른 사용자 파일 수정·삭제는 금지한다."
    assert objective_artifact_paths(objective) == [path]
    plan = classify_goal(objective, forced="full", retrieval_mode="none")
    assert plan.write_authority["artifact_paths"] == [path]
    assert plan.write_authority["requires_write"] is True


@pytest.mark.parametrize("locator", [
    ".crabagent/artifacts/source.txt보고서",
    ".crabagent/artifacts/source.txt를/child",
    ".crabagent/artifacts/source.txt를.exe",
    ".crabagent/artifacts/source.txt.exe를",
    ".crabagent/artifacts/../source.txt를",
    ".crabagent/artifacts/source.txt/child",
])
def test_particle_support_does_not_authorize_different_or_nested_paths(locator):
    assert objective_artifact_paths(f"파일 {locator} 생성한다.") == []
