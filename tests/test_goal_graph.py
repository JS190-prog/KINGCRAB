from pathlib import Path
import threading

from crabagent.colony import ColonyExecutor
from crabagent.goal import classify_goal
from crabagent.goal_graph import compile_goal_graph, compact_goal_graph
from crabagent.models import Role
from crabagent.runtime import RuntimeService


def test_goal_graph_turns_ontology_goal_into_evidence_and_verification_slots() -> None:
    plan = classify_goal("오픈크랩 팩의 근거를 비교해서 실행 전략을 추천해줘")
    graph = compile_goal_graph(plan.to_dict())

    assert graph["schema"] == "crab.goal-graph/v1"
    assert graph["graph_id"].startswith("goal-")
    assert graph["evidence_slots"]
    assert "retrieve_evidence" in graph["verification_loop"]["steps"]
    assert "judge_against_goal_graph" in graph["verification_loop"]["steps"]
    assert "supported_claims" in graph["decision_slots"]
    assert len(compact_goal_graph(graph, max_chars=5000)) <= 5000


def test_adaptive_plan_persists_goal_graph_artifact(tmp_path: Path) -> None:
    service = RuntimeService(tmp_path)
    snapshot = service.plan_mission(
        "오픈크랩 팩의 근거를 바탕으로 목표중심 워크플로우를 설계해줘",
        adaptive=True,
    )

    assert snapshot["goal_graph"]["schema"] == "crab.goal-graph/v1"
    assert snapshot["goal_plan"]["goal_graph_id"] == snapshot["goal_graph"]["graph_id"]
    graph_artifacts = [row for row in snapshot["artifacts"] if row["kind"] == "goal_graph"]
    assert len(graph_artifacts) == 1
    assert Path(graph_artifacts[0]["path"]).exists()


def test_provider_prompt_contains_goal_graph_contract(tmp_path: Path) -> None:
    service = RuntimeService(tmp_path)
    session = service.store.create_session(interaction_mode="colony")
    executor = ColonyExecutor(service, session["session_id"], object(), threading.Event())
    executor.goal_plan = classify_goal("오픈크랩 팩의 근거를 비교해서 전략을 추천해줘").to_dict()

    prompt = executor._prompt(Role.QUEEN, executor.goal_plan["objective"], [])

    assert "GOAL GRAPH" in prompt
    assert "evidence_slots" in prompt
    assert "judge_against_goal_graph" in prompt


def test_provider_prompt_contains_ontology_execution_contract(tmp_path: Path) -> None:
    service = RuntimeService(tmp_path)
    session = service.store.create_session(interaction_mode="colony")
    planned = service.plan_mission(
        "오픈크랩 팩의 근거를 비교해서 전략을 추천해줘",
        session_id=session["session_id"],
        adaptive=True,
    )
    executor = ColonyExecutor(service, session["session_id"], object(), threading.Event())
    executor.goal_plan = dict(planned["goal_plan"])
    executor.goal_graph = dict(planned["goal_graph"])
    executor.ontology_contract = dict(planned["ontology_execution_contract"])

    prompt = executor._prompt(Role.QUEEN, executor.goal_plan["objective"], [])

    assert "ONTOLOGY EXECUTION CONTRACT" in prompt
    assert "decision_gate" in prompt
