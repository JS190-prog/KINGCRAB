from __future__ import annotations

from crabagent.goal import classify_goal, interaction_kind_for_goal
from crabagent.models import Role
from crabagent.runtime import RuntimeService


def test_full_forces_every_role_and_disables_local_gates() -> None:
    plan = classify_goal("로컬 스크립트 하나 정리해줘", forced="full")

    assert plan.full_pipeline is True
    assert plan.stages == ["KING", "QUEEN", "SOLDIER", "WORKER", "ORACLE"]
    assert plan.estimated_model_turns == 5
    # Calibrated from observed runs: 42,935 uncached input tokens after 2 turns
    # and 132,903 after 4, so five full turns need far more than the lean
    # allowance and more than a flat per-turn estimate.
    assert plan.token_budget >= 5 * 40000
    for role in (Role.KING, Role.QUEEN, Role.SOLDIER, Role.ORACLE):
        assert RuntimeService._uses_local_gate(plan, role) is False
    assert "oracle_model_review_observed" in plan.acceptance_checks


def test_full_never_overrides_a_local_only_scope() -> None:
    plan = classify_goal(
        "로컬 파일만 정리해. OpenCrab 근거 조회 없이 외부 서비스 없이 로컬 작업만 수행해.",
        forced="full",
        execution_scope="local_only",
    )

    assert plan.full_pipeline is False
    assert "QUEEN" not in plan.stages
    assert "SOLDIER" not in plan.stages


def test_full_routes_to_the_colony_interaction() -> None:
    assert interaction_kind_for_goal("아무 질문", "full") == "colony"


def test_default_routing_is_unchanged() -> None:
    plan = classify_goal("오픈크랩 팩을 조회해서 목표중심 워크플로우의 근거를 요약해줘")

    assert plan.full_pipeline is False
    assert plan.stages == ["KING", "QUEEN", "SOLDIER", "ORACLE"]
    assert RuntimeService._uses_local_gate(plan, Role.KING) is True
