from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .benchmark import compare_goal


DEFAULT_CASES: List[Dict[str, str]] = [
    {
        "id": "code_write",
        "category": "write",
        "objective": "Create exactly one file named result.txt containing exactly OK on one line. Do not modify any other file.",
    },
    {
        "id": "exact_ontology_lookup",
        "category": "exact_lookup",
        "objective": "내 오픈크랩 팩 목록을 보여줘",
    },
    {
        "id": "semantic_ontology",
        "category": "semantic_ontology",
        "objective": "오픈크랩 팩의 근거를 바탕으로 목표중심 워크플로우를 요약해줘",
    },
    {
        "id": "graph_ontology",
        "category": "graph_ontology",
        "objective": "오픈크랩 그래프에서 근거와 결과를 연결하는 노드와 엣지 경로를 확인해줘",
    },
    {
        "id": "ontology_backed_write",
        "category": "write_ontology",
        "objective": "오픈크랩 팩의 근거를 활용해서 정확히 하나의 파일 ontology-result.txt를 만들고 한 줄에 OK만 기록해. 다른 파일은 수정하지 마.",
    },
]


def select_cases(case_ids: Optional[Iterable[str]] = None) -> List[Dict[str, str]]:
    selected = [str(value).strip() for value in (case_ids or []) if str(value).strip()]
    if not selected or "all" in selected:
        return [dict(case) for case in DEFAULT_CASES]
    available = {case["id"]: case for case in DEFAULT_CASES}
    unknown = [value for value in selected if value not in available]
    if unknown:
        raise ValueError("unknown benchmark case(s): %s" % ", ".join(unknown))
    return [dict(available[value]) for value in selected]


def plan_suite(
    *,
    case_ids: Optional[Iterable[str]] = None,
    selected_pack_count: int = 0,
    selected_project_count: int = 0,
) -> Dict[str, Any]:
    cases = select_cases(case_ids)
    planned = []
    for case in cases:
        plan = compare_goal(
            case["objective"],
            selected_pack_count=selected_pack_count,
            selected_project_count=selected_project_count,
        )
        planned.append({"id": case["id"], "category": case["category"], "objective": case["objective"], "plan": plan})
    return {
        "schema": "crab.benchmark-suite-plan/v1",
        "measurement": "planned_only",
        "cases": planned,
        "guardrail": "A planned suite does not invoke Codex or prove quality or efficiency.",
    }


def summarize_suite(results: List[Dict[str, Any]], *, suite_id: str = "") -> Dict[str, Any]:
    verdicts: Dict[str, int] = {}
    quality_wins = 0
    contract_wins = 0
    contract_accepted = 0
    accepted = 0
    total_token_advantages = 0
    billable_advantages = 0
    deltas: List[int] = []
    for result in results:
        comparison = result.get("comparison") if isinstance(result.get("comparison"), dict) else {}
        verdict = str(comparison.get("verdict") or "inconclusive")
        verdicts[verdict] = verdicts.get(verdict, 0) + 1
        if comparison.get("quality_parity"):
            accepted += 1
        quality = comparison.get("structural_quality") if isinstance(comparison.get("structural_quality"), dict) else {}
        if quality.get("adaptive_advantage"):
            quality_wins += 1
        contract = comparison.get("goal_contract_quality") if isinstance(comparison.get("goal_contract_quality"), dict) else {}
        if contract.get("adaptive_advantage"):
            contract_wins += 1
        adaptive_contract = contract.get("adaptive") if isinstance(contract.get("adaptive"), dict) else {}
        if adaptive_contract.get("accepted"):
            contract_accepted += 1
        efficiency = comparison.get("efficiency") if isinstance(comparison.get("efficiency"), dict) else {}
        if efficiency.get("total_token_advantage"):
            total_token_advantages += 1
        if efficiency.get("billable_input_advantage"):
            billable_advantages += 1
        delta = (comparison.get("delta") or {}).get("tokens_observed")
        if isinstance(delta, int):
            deltas.append(delta)
    return {
        "schema": "crab.benchmark-suite-result/v1",
        "suite_id": suite_id,
        "case_count": len(results),
        "accepted_case_count": accepted,
        "structural_quality_advantage_count": quality_wins,
        "goal_contract_advantage_count": contract_wins,
        "goal_contract_accepted_count": contract_accepted,
        "total_token_advantage_count": total_token_advantages,
        "billable_input_advantage_count": billable_advantages,
        "token_delta_sum": sum(deltas) if deltas else None,
        "verdicts": verdicts,
        "claim": (
            "adaptive_structural_advantage_observed"
            if quality_wins > 0 and accepted == len(results) and results
            else "inconclusive"
        ),
        "limitations": [
            "The suite compares observable workflow integrity, not unrestricted semantic correctness.",
            "Provider prompt-cache behavior can make billable input differ from total tokens.",
            "A suite result is evidence for these cases, not a universal claim over every task.",
        ],
        "results": results,
    }


def run_observed_suite(
    workspace: Path,
    *,
    source_session_id: str = "",
    case_ids: Optional[Iterable[str]] = None,
    timeout_seconds: float = 900.0,
    output_dir: Optional[Path] = None,
    order: str = "adaptive-first",
) -> Dict[str, Any]:
    """Run the same benchmark contract over a small, explicit goal matrix."""
    from .benchmark_runner import run_observed_benchmark

    cases = select_cases(case_ids)
    suite_id = "suite-%s" % uuid.uuid4().hex[:12]
    root = (output_dir or (workspace / ".crabagent" / "benchmarks" / suite_id)).resolve()
    root.mkdir(parents=True, exist_ok=True)
    results: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []
    for case in cases:
        try:
            result = run_observed_benchmark(
                workspace,
                case["objective"],
                source_session_id=source_session_id,
                timeout_seconds=timeout_seconds,
                output_dir=root / case["id"],
                order=order,
            )
            result["case"] = case
            results.append(result)
        except Exception as exc:
            failures.append({"case": case, "error_type": type(exc).__name__, "error": str(exc)})
    summary = summarize_suite(results, suite_id=suite_id)
    summary.update(
        {
            "measurement": "observed",
            "order": order,
            "source_session_id": source_session_id or None,
            "failures": failures,
            "paths": {"root": str(root), "suite": str(root / "suite.json")},
        }
    )
    (root / "suite.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary
