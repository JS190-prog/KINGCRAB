from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List


KING_PLAN_SCHEMA = "crab.king-plan/v1"


def _clean(value: Any, limit: int = 420) -> str:
    return " ".join(str(value or "").split())[:limit]


def _items(value: Any, limit: int = 8) -> List[str]:
    if isinstance(value, str):
        raw = re.split(r"\n|;|•", value)
    elif isinstance(value, (list, tuple)):
        raw = list(value)
    else:
        raw = []
    result: List[str] = []
    for item in raw:
        text = _clean(item, 300).lstrip("-:*0123456789. ")
        if text and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return result


def _section(text: str, name: str, names: Iterable[str]) -> str:
    match = re.search(r"(?:^|\n)\s*%s\s*\n?" % re.escape(name), text, flags=re.IGNORECASE)
    if not match:
        return ""
    tail = text[match.end():]
    end = len(tail)
    for other in names:
        if other.lower() == name.lower():
            continue
        candidate = re.search(r"(?:^|\n)\s*%s\s*\n?" % re.escape(other), tail, flags=re.IGNORECASE)
        if candidate:
            end = min(end, candidate.start())
    return tail[:end].strip()


def _json_payload(text: str) -> Dict[str, Any]:
    value = text.strip()
    candidates = [value]
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", value, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        candidates.insert(0, fenced.group(1))
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def compile_king_plan(
    *,
    objective: str,
    goal_plan: Dict[str, Any],
    goal_graph: Dict[str, Any],
    source: str = "local_goal_compiler",
    model_text: str = "",
) -> Dict[str, Any]:
    """Normalize KING output into a bounded advisory execution contract.

    The user goal and deterministic goal graph remain authoritative. Model
    output may refine the order and wording, but cannot create evidence,
    change the requested scope, or add an unplanned write.
    """
    raw = _json_payload(model_text) if model_text else {}
    text = str(model_text or "").strip()[:12000]
    section_names = ["GOAL_RESTATEMENT", "SUBGOALS", "CONSTRAINTS", "SUCCESS_CHECKS", "NEXT_ACTION"]
    restatement = _clean(raw.get("goal_restatement") or raw.get("restatement"))
    if not restatement:
        restatement = _clean(_section(text, "GOAL_RESTATEMENT", section_names))
    subgoals = _items(raw.get("subgoals") or raw.get("sub_goals"))
    if not subgoals:
        subgoals = _items(_section(text, "SUBGOALS", section_names))
    constraints = _items(raw.get("constraints"))
    if not constraints:
        constraints = _items(_section(text, "CONSTRAINTS", section_names))
    success_checks = _items(raw.get("success_checks") or raw.get("success"))
    if not success_checks:
        success_checks = _items(_section(text, "SUCCESS_CHECKS", section_names))
    next_action = _clean(raw.get("next_action"))
    if not next_action:
        next_action = _clean(_section(text, "NEXT_ACTION", section_names).splitlines()[0] if _section(text, "NEXT_ACTION", section_names) else "")

    graph_slots = goal_graph.get("decision_slots") if isinstance(goal_graph.get("decision_slots"), list) else []
    if not restatement:
        restatement = _clean(objective)
    if not subgoals:
        subgoals = [
            "선택된 범위와 권위 있는 근거를 확인한다.",
            "근거가 지지하는 결정과 빈틈을 분리한다.",
            "검증 가능한 결과와 다음 행동을 만든다.",
        ]
    if not constraints:
        constraints = [
            "goal_graph의 범위와 사용자가 준 목표를 변경하지 않는다.",
            "관측되지 않은 팩·근거·도구 실행을 사실로 만들지 않는다.",
            "쓰기 작업은 계획된 WORKER 경계 안에서만 수행한다.",
        ]
    if not success_checks:
        success_checks = [_clean(item, 260) for item in goal_plan.get("acceptance_checks") or []][:8]
    if not next_action:
        next_action = "QUEEN: goal_graph의 evidence_slots를 채우고 decision_slots별 근거를 정리한다."

    return {
        "schema": KING_PLAN_SCHEMA,
        "authority": "user_goal_and_goal_graph",
        "source": source,
        "model_advisory": bool(model_text),
        "objective": _clean(objective, 800),
        "goal_graph_id": goal_graph.get("graph_id"),
        "goal_restatement": restatement,
        "subgoals": subgoals[:8],
        "constraints": constraints[:8],
        "success_checks": success_checks[:8],
        "decision_slots": [str(item) for item in graph_slots[:12]],
        "next_action": next_action,
        "scope_locked": True,
        "evidence_authority": "opencrab_mcp_receipt_only",
    }


def compact_king_plan(plan: Dict[str, Any], max_chars: int = 1500) -> str:
    """Render only the fields later roles need; keep full receipt on disk."""
    payload = {
        "goal_graph_id": plan.get("goal_graph_id"),
        "goal_restatement": plan.get("goal_restatement"),
        "subgoals": (plan.get("subgoals") or [])[:5],
        "constraints": (plan.get("constraints") or [])[:5],
        "success_checks": (plan.get("success_checks") or [])[:5],
        "decision_slots": (plan.get("decision_slots") or [])[:8],
        "next_action": plan.get("next_action"),
        "scope_locked": True,
        "evidence_authority": "opencrab_mcp_receipt_only",
    }
    value = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return value if len(value) <= max_chars else value[: max_chars - 18] + "...[bounded]"
