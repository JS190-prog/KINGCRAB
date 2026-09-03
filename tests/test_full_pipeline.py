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


def test_a_plan_without_oracle_still_terminates(tmp_path: Path) -> None:
    """No ORACLE stage must not leave the mission RUNNING and the session bound."""
    import time

    from crabagent.daemon import RuntimeServer
    from crabagent.protocol import runtime_paths

    paths = runtime_paths(tmp_path)
    paths["root"].mkdir(parents=True, exist_ok=True)
    server = RuntimeServer(tmp_path, paths["socket"])
    session_id = ""
    try:
        session_id = str(server.dispatch({"action": "session.ensure", "payload": {}})["session_id"])
        server.dispatch({
            "action": "session.configure",
            "payload": {
                "session_id": session_id, "model_policy": "host", "executor_policy": "host",
                "interaction_mode": "colony", "mcp_policy": "off", "max_workers": 1,
            },
        })
        # A bounded request that publishes nothing: classify_goal omits ORACLE.
        plan = server.dispatch({
            "action": "goal.preview",
            "payload": {"session_id": session_id, "objective": "안녕하세요 오늘 날씨 어때요"},
        })["plan"]
        assert "ORACLE" not in plan["stages"], plan["stages"]

        server.dispatch({
            "action": "prompt.submit",
            "payload": {
                "session_id": session_id,
                "objective": "안녕하세요 오늘 날씨 어때요",
                "disposition": "start",
                "interaction": "colony",
            },
        })
        deadline = time.time() + 20.0
        status = ""
        while time.time() < deadline:
            rows = server.dispatch({
                "action": "mission.list", "payload": {"session_id": session_id, "limit": 1},
            })["missions"]
            if rows:
                status = str(rows[0].get("status") or "")
                if status in {"completed", "failed", "cancelled"}:
                    break
            time.sleep(0.05)
        # Not just "terminal": a plan that ran every task must not be reported
        # as a failure. RUNNING -> COMPLETED is an illegal edge, and taking it
        # surfaced in production as "Mission failed: invalid mission transition".
        assert status == "completed", f"mission ended {status!r}"
        assert not (server.service.store.session(session_id) or {}).get("active_mission_id")
    finally:
        if session_id:
            server.dispatch({"action": "session.interrupt", "payload": {"session_id": session_id}})
