from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .goal import GoalPlan, classify_goal
from .inverter import SolteluInverter
from .models import Role
from .runtime import RuntimeService


def _legacy_projection(inverter: SolteluInverter) -> Dict[str, Any]:
    roles = list(Role)
    routes = [inverter.plan(role).to_dict() for role in roles]
    return {
        "workflow": "fixed_five_role",
        "roles": [role.value for role in roles],
        "planned_model_routes": len(routes),
        "local_gates": 0,
        "estimated_model_turns": len(routes),
        "token_target": None,
        "routes": routes,
    }


def _direct_projection(plan: GoalPlan, inverter: SolteluInverter) -> Dict[str, Any]:
    """Model the honest comparison target: one direct model turn."""
    route_role = Role.QUEEN if plan.ontology_required else Role.WORKER
    route = inverter.plan(route_role)
    return {
        "workflow": "direct_model_with_optional_opencrab",
        "roles": ["DIRECT"],
        "planned_model_routes": 1,
        "local_gates": 0,
        "estimated_model_turns": 1,
        "planned_mcp_calls": 1 if plan.ontology_required else 0,
        "token_target": None,
        "routes": [route.to_dict()],
    }


def _adaptive_projection(plan: GoalPlan, inverter: SolteluInverter) -> Dict[str, Any]:
    routes = []
    local_gates = 0
    for role_name in plan.stages:
        role = Role(role_name)
        local = RuntimeService._uses_local_gate(plan, role)
        routes.append(
            inverter.local(role, "benchmark local gate").to_dict()
            if local
            else inverter.plan(role, "ontology_judgment" if role is Role.SOLDIER else "default").to_dict()
        )
        local_gates += 1 if local else 0
    return {
        "workflow": "goal_compiled_kinetic",
        "roles": list(plan.stages),
        "planned_model_routes": len([row for row in routes if row["provider"] != "local"]),
        "local_gates": local_gates,
        "estimated_model_turns": plan.estimated_model_turns,
        "token_target": plan.token_budget,
        "goal_plan": plan.to_dict(),
        "routes": routes,
    }


def compare_goal(
    objective: str,
    *,
    selected_pack_count: int = 0,
    selected_project_count: int = 0,
) -> Dict[str, Any]:
    """Compare plans only; it never invokes Codex or OpenCrab."""
    inverter = SolteluInverter()
    plan = classify_goal(
        objective,
        selected_pack_count=selected_pack_count,
        selected_project_count=selected_project_count,
    )
    direct = _direct_projection(plan, inverter)
    legacy = _legacy_projection(inverter)
    adaptive = _adaptive_projection(plan, inverter)
    adaptive["planned_mcp_calls"] = 1 + (4 if plan.graph_required else 0) if plan.ontology_required else 0
    return {
        "objective": " ".join(str(objective or "").split()),
        "measurement": "planned_only",
        "direct_baseline": direct,
        "legacy_fixed_colony_reference": legacy,
        "adaptive": adaptive,
        "delta": {
            "role_count": len(direct["roles"]) - len(adaptive["roles"]),
            "planned_model_routes": direct["planned_model_routes"] - adaptive["planned_model_routes"],
            "estimated_model_turns": direct["estimated_model_turns"] - adaptive["estimated_model_turns"],
            "planned_mcp_calls": direct["planned_mcp_calls"] - adaptive["planned_mcp_calls"],
            "local_gates_added": adaptive["local_gates"],
        },
        "guardrail": "Direct baseline is one model turn with optional OpenCrab context. This is still a route estimate; superiority requires observed A/B runs with the same evidence and acceptance tests.",
    }


def compare_many(objectives: Iterable[str]) -> List[Dict[str, Any]]:
    return [compare_goal(objective) for objective in objectives if str(objective).strip()]


def observed_metrics(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Extract comparable facts from a persisted mission snapshot."""
    mission = snapshot.get("mission") if isinstance(snapshot.get("mission"), dict) else {}
    budget = snapshot.get("budget") if isinstance(snapshot.get("budget"), dict) else {}
    receipts = snapshot.get("tool_receipts") if isinstance(snapshot.get("tool_receipts"), list) else []
    artifacts = snapshot.get("artifacts") if isinstance(snapshot.get("artifacts"), list) else []
    evidence = snapshot.get("evidence") if isinstance(snapshot.get("evidence"), list) else []
    workspace_change: Dict[str, Any] = snapshot.get("workspace_change") if isinstance(snapshot.get("workspace_change"), dict) else {}
    answer_grounding = snapshot.get("answer_grounding") if isinstance(snapshot.get("answer_grounding"), dict) else {}
    structured_handoff: Dict[str, Any] = {}
    for artifact in snapshot.get("artifacts") if isinstance(snapshot.get("artifacts"), list) else []:
        if not workspace_change and isinstance(artifact, dict) and str(artifact.get("kind") or "") == "workspace_change":
            try:
                candidate_change = json.loads(Path(str(artifact.get("path") or "")).read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                candidate_change = {}
            if isinstance(candidate_change, dict):
                workspace_change = candidate_change
        if not isinstance(artifact, dict) or str(artifact.get("kind") or "") not in {"soldier_patrol", "oracle_result"}:
            continue
        path = Path(str(artifact.get("path") or ""))
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        candidate = payload.get("queen_handoff_quality") if isinstance(payload, dict) else None
        if isinstance(candidate, dict) and candidate.get("present"):
            structured_handoff = candidate
            break
    citation_count = int(answer_grounding.get("evidence_citation_count") or 0)
    if structured_handoff:
        citation_count = max(citation_count, int(structured_handoff.get("evidence_citation_count") or 0))
    successful_model_turns = sum(
        1
        for row in receipts
        if isinstance(row, dict)
        and str(row.get("tool_name") or "").startswith("codex.app-server.turn")
        and str(row.get("status") or "") == "success"
    )
    mcp_receipts = sum(
        1
        for row in receipts
        if isinstance(row, dict) and str(row.get("tool_name") or "").startswith("opencrab.mcp.")
    )
    local_gates = sum(
        1
        for row in receipts
        if isinstance(row, dict) and str(row.get("tool_name") or "") in {"crab.role_gate", "crab.soldier.local_patrol", "crab.worker.subgoal"}
    )
    accepted_artifacts = sum(
        1 for row in artifacts if isinstance(row, dict) and str(row.get("status") or "") == "accepted"
    )
    ontology_ledgers = sum(
        1 for row in artifacts if isinstance(row, dict) and str(row.get("kind") or "") == "ontology_ledger"
    )
    goal_graphs = sum(
        1 for row in artifacts if isinstance(row, dict) and str(row.get("kind") or "") == "goal_graph"
    )
    king_plans = sum(
        1 for row in artifacts if isinstance(row, dict) and str(row.get("kind") or "") == "king_plan"
    )
    ontology_contract_artifacts = sum(
        1
        for row in artifacts
        if isinstance(row, dict) and str(row.get("kind") or "") == "ontology_execution_contract"
    )
    ontology_contract = snapshot.get("ontology_execution_contract") if isinstance(snapshot.get("ontology_execution_contract"), dict) else {}
    if not ontology_contract:
        for row in reversed(artifacts):
            if not isinstance(row, dict) or str(row.get("kind") or "") != "ontology_execution_contract":
                continue
            try:
                candidate = json.loads(Path(str(row.get("path") or "")).read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                candidate = {}
            if isinstance(candidate, dict):
                ontology_contract = candidate
                break
    contract_coverage = ontology_contract.get("coverage") if isinstance(ontology_contract.get("coverage"), dict) else {}
    kinetic_workflow_artifacts = sum(
        1
        for row in artifacts
        if isinstance(row, dict) and str(row.get("kind") or "") == "kinetic_workflow"
    )
    kinetic_state_artifacts = sum(
        1
        for row in artifacts
        if isinstance(row, dict) and str(row.get("kind") or "") == "kinetic_workflow_state"
    )
    kinetic_state: Dict[str, Any] = snapshot.get("kinetic_workflow_state") if isinstance(snapshot.get("kinetic_workflow_state"), dict) else {}
    if not kinetic_state:
        for row in reversed(artifacts):
            if not isinstance(row, dict) or str(row.get("kind") or "") != "kinetic_workflow_state":
                continue
            try:
                candidate = json.loads(Path(str(row.get("path") or "")).read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                candidate = {}
            if isinstance(candidate, dict):
                kinetic_state = candidate
                break
    kinetic_summary = kinetic_state.get("summary") if isinstance(kinetic_state.get("summary"), dict) else {}
    typed_observed_items = 0
    observation_gate = "not_required"
    observation_mode = "evidence"
    for row in reversed(artifacts):
        if not isinstance(row, dict) or str(row.get("kind") or "") != "mcp_context_receipt":
            continue
        try:
            candidate = json.loads(Path(str(row.get("path") or "")).read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            candidate = {}
        if isinstance(candidate, dict):
            typed_observed_items = int(candidate.get("observed_item_count") or len(candidate.get("observed_items") or []))
            observation_gate = str(candidate.get("observation_gate") or observation_gate)
            observation_mode = "metadata_observation" if observation_gate == "pass" and not candidate.get("evidence_count") else "evidence"
            break
    automatic_revision_cycles = 0
    for row in artifacts:
        if not isinstance(row, dict) or str(row.get("kind") or "") != "soldier_patrol":
            continue
        try:
            payload = json.loads(Path(str(row.get("path") or "")).read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            payload = {}
        revision = payload.get("automatic_revision") if isinstance(payload, dict) else {}
        if isinstance(revision, dict) and revision.get("attempted"):
            automatic_revision_cycles += 1
    worker_subgoal_slots = sum(
        1
        for row in snapshot.get("tasks") or []
        if isinstance(row, dict) and str(row.get("task_kind") or "") == "king_subgoal"
    )
    worker_subgoal_receipts = sum(
        1
        for row in receipts
        if isinstance(row, dict) and str(row.get("tool_name") or "") == "crab.worker.subgoal"
    )
    prompt_chars = sum(
        int((row.get("observed") or {}).get("prompt_chars") or 0)
        for row in receipts
        if isinstance(row, dict) and isinstance(row.get("observed"), dict)
    )
    context_chars = sum(
        int((row.get("observed") or {}).get("context_chars") or 0)
        for row in receipts
        if isinstance(row, dict) and isinstance(row.get("observed"), dict)
    )
    input_tokens = 0
    input_token_observed = False
    cached_input_tokens = 0
    cached_input_token_observed = False
    for row in receipts:
        observed = row.get("observed") if isinstance(row, dict) and isinstance(row.get("observed"), dict) else {}
        usage = observed.get("usage") if isinstance(observed.get("usage"), dict) else {}
        last = usage.get("last") if isinstance(usage.get("last"), dict) else usage
        value = last.get("inputTokens") if isinstance(last, dict) else None
        if value is None and isinstance(last, dict):
            value = last.get("input_tokens")
        if isinstance(value, int):
            input_tokens += value
            input_token_observed = True
        cached_value = last.get("cachedInputTokens") if isinstance(last, dict) else None
        if cached_value is None and isinstance(last, dict):
            cached_value = last.get("cached_input_tokens")
        if isinstance(cached_value, int):
            cached_input_tokens += cached_value
            cached_input_token_observed = True
    tokens = budget.get("tokens_observed")
    observation_source = budget.get("observation_source") or "unknown"
    has_model_receipt = any(
        isinstance(row, dict)
        and str(row.get("tool_name") or "") == "codex.app-server.turn"
        for row in receipts
    )
    # A terminal path with no model receipt is an observed zero-cost local
    # gate, not an unknown provider quota. Keep the distinction in the source
    # label so a missing budget on a real model turn remains unknown.
    if (
        not isinstance(tokens, int)
        and successful_model_turns == 0
        and not has_model_receipt
        and mission.get("status") in {"completed", "failed", "cancelled"}
        and (mission.get("status") != "completed" or local_gates > 0)
    ):
        tokens = 0
        observation_source = "no_model_turn_observed"
        # A completed deterministic route has an observed zero provider
        # budget. Keep the input dimensions comparable instead of treating
        # the absence of a model call as missing usage.
        input_tokens = 0
        input_token_observed = True
        cached_input_tokens = 0
        cached_input_token_observed = True
    benchmark = snapshot.get("benchmark") if isinstance(snapshot.get("benchmark"), dict) else {}
    oracle_accepted = bool(mission.get("oracle_result_artifact_id")) and mission.get("status") == "completed"
    result_accepted = bool(benchmark.get("result_accepted")) if "result_accepted" in benchmark else oracle_accepted
    workspace_changed_file_count = int(workspace_change.get("changed_file_count") or 0)
    metrics = {
        "mission_id": mission.get("mission_id"),
        "status": mission.get("status"),
        "oracle_accepted": oracle_accepted,
        "result_accepted": result_accepted,
        "workspace_change_accepted": bool(workspace_change.get("observed")) and workspace_changed_file_count > 0,
        "workspace_changed_file_count": workspace_changed_file_count,
        "tokens_observed": tokens if isinstance(tokens, int) else None,
        "model_turns_observed": successful_model_turns,
        "mcp_receipts": mcp_receipts,
        "local_gates": local_gates,
        "evidence_refs": len(evidence),
        "evidence_citation_count": citation_count,
        "structured_handoff_accepted": bool(structured_handoff.get("accepted")),
        "accepted_artifacts": accepted_artifacts,
        "ontology_ledgers": ontology_ledgers,
        "goal_graphs": goal_graphs,
        "king_plans": king_plans,
        "ontology_contract_artifacts": ontology_contract_artifacts,
        "ontology_contract_gate": contract_coverage.get("gate"),
        "ontology_contract_decision_gate": ontology_contract.get("decision_gate"),
        "kinetic_workflow_artifacts": kinetic_workflow_artifacts,
        "kinetic_state_artifacts": kinetic_state_artifacts,
        "kinetic_step_count": int(kinetic_summary.get("step_count") or 0),
        "kinetic_completed_step_count": int(kinetic_summary.get("completed_step_count") or 0),
        "typed_observed_item_count": typed_observed_items,
        "observation_gate": observation_gate,
        "observation_mode": observation_mode,
        "ontology_required_slot_count": int(contract_coverage.get("required_slot_count") or 0),
        "ontology_filled_required_slot_count": int(contract_coverage.get("filled_required_slot_count") or 0),
        "ontology_missing_slot_count": len(contract_coverage.get("missing_slot_ids") or []),
        "automatic_revision_cycles": automatic_revision_cycles,
        "worker_subgoal_slots": worker_subgoal_slots,
        "worker_subgoal_receipts": worker_subgoal_receipts,
        "tool_receipts": len(receipts),
        "prompt_chars": prompt_chars,
        "context_chars": context_chars,
        "input_tokens_observed": input_tokens if input_token_observed else None,
        "cached_input_tokens_observed": cached_input_tokens if cached_input_token_observed else None,
        "billable_input_tokens_observed": (
            max(0, input_tokens - cached_input_tokens)
            if input_token_observed and cached_input_token_observed
            else None
        ),
        "measurement_source": observation_source,
    }
    metrics["structural_quality"] = structural_quality(metrics, snapshot)
    metrics["goal_contract_quality"] = goal_contract_quality(metrics, snapshot)
    metrics["kinetic_contract_quality"] = kinetic_contract_quality(metrics, snapshot)
    return metrics


def _artifact_payloads(snapshot: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Read small JSON role receipts without treating prose as verified data."""
    payloads: List[Dict[str, Any]] = []
    for row in snapshot.get("artifacts") if isinstance(snapshot.get("artifacts"), list) else []:
        if not isinstance(row, dict):
            continue
        path = str(row.get("path") or "")
        if not path:
            continue
        try:
            value = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if isinstance(value, dict):
            payloads.append(value)
    return payloads


def kinetic_contract_quality(metrics: Dict[str, Any], snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Measure whether the adaptive route actually carried its contracts.

    This is a workflow signal, not a semantic answer judge. It exists because
    a role label alone is not evidence that KING's plan changed downstream
    work.
    """
    plan = snapshot.get("goal_plan") if isinstance(snapshot.get("goal_plan"), dict) else {}
    adaptive = bool(plan)
    if not adaptive:
        return {
            "schema": "crab.kinetic-contract-quality/v1",
            "known": False,
            "accepted": False,
            "score": 0,
            "max_score": 100,
            "reason": "direct baseline has no KINGCRAB contract",
        }
    graph_present = metrics.get("goal_graphs", 0) > 0 or bool(plan.get("goal_graph_id"))
    king_present = metrics.get("king_plans", 0) > 0
    ontology = bool(plan.get("ontology_required"))
    ledger_present = metrics.get("ontology_ledgers", 0) > 0 if ontology else True
    handoff_present = bool(metrics.get("structured_handoff_accepted")) if ontology else True
    oracle_present = bool(metrics.get("oracle_accepted") or metrics.get("result_accepted"))
    workflow_present = metrics.get("kinetic_workflow_artifacts", 0) > 0 or metrics.get("kinetic_state_artifacts", 0) > 0
    dimensions = {
        "goal_graph": 20 if graph_present else 0,
        "king_packet": 20 if king_present else 0,
        "ontology_ledger": 15 if ledger_present else 0,
        "kinetic_workflow": 15 if workflow_present else 0,
        "structured_handoff": 20 if handoff_present else 0,
        "oracle_or_result_gate": 10 if oracle_present else 0,
    }
    accepted = all(value > 0 for value in dimensions.values())
    return {
        "schema": "crab.kinetic-contract-quality/v1",
        "known": True,
        "accepted": accepted,
        "score": sum(dimensions.values()),
        "max_score": 100,
        "dimensions": dimensions,
        "automatic_revision_cycles": metrics.get("automatic_revision_cycles", 0),
        "not_a_semantic_judge": True,
    }


def goal_contract_quality(metrics: Dict[str, Any], snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Score whether the persisted result satisfies this goal's contract.

    This is deliberately narrower than semantic evaluation. It checks the
    required shape, authority, citations, graph answerability and final gate
    for the requested goal. It never claims that a cited source is true.
    """
    plan = snapshot.get("goal_plan") if isinstance(snapshot.get("goal_plan"), dict) else {}
    ontology = bool(plan.get("ontology_required")) or metrics.get("mcp_receipts", 0) > 0
    write = bool(plan.get("requires_write"))
    payloads = _artifact_payloads(snapshot)
    oracle_payload = next(
        (row for row in reversed(payloads) if row.get("verdict") in {"accepted", "rejected"}),
        {},
    )
    context_quality = {}
    for row in reversed(payloads):
        candidate = row.get("context_quality") or row.get("quality")
        if isinstance(candidate, dict) and candidate:
            context_quality = candidate
            break
    benchmark = snapshot.get("benchmark") if isinstance(snapshot.get("benchmark"), dict) else {}
    answer_grounding = snapshot.get("answer_grounding") if isinstance(snapshot.get("answer_grounding"), dict) else {}
    if not answer_grounding:
        answer_grounding = benchmark.get("answer_grounding") if isinstance(benchmark.get("answer_grounding"), dict) else {}
    answer_shape = snapshot.get("answer_shape") if isinstance(snapshot.get("answer_shape"), dict) else {}
    if not answer_shape:
        answer_shape = benchmark.get("answer_shape") if isinstance(benchmark.get("answer_shape"), dict) else {}
    benchmark_workflow = str((snapshot.get("benchmark") or {}).get("workflow") or "")
    # A blocked adaptive mission can have no Oracle artifact too. Use the
    # explicit benchmark workflow instead of inferring route identity from
    # acceptance state.
    direct_mode = benchmark_workflow == "direct_model_with_opencrab"
    graph_required = bool(plan.get("graph_required"))
    paths = snapshot.get("paths") if isinstance(snapshot.get("paths"), list) else []
    graph_path_count = int(context_quality.get("path_count") or len(paths))
    graph_semantic_gate = str(
        context_quality.get("graph_semantic_gate")
        or oracle_payload.get("context_quality", {}).get("graph_semantic_gate")
        or "unknown"
    )
    ontology_contract_gate = str(metrics.get("ontology_contract_gate") or "unknown")
    ontology_decision_gate = str(metrics.get("ontology_contract_decision_gate") or "unknown")
    handoff = oracle_payload.get("queen_handoff_quality") if isinstance(oracle_payload.get("queen_handoff_quality"), dict) else {}
    sections = int(handoff.get("section_count") or 0)
    if not handoff and isinstance(answer_grounding, dict):
        sections = 1 if answer_grounding.get("accepted") else 0
    dimensions = {
        "goal_contract": 20 if str(plan.get("objective") or "").strip() else 0,
        "authoritative_context": 20 if (not ontology or (metrics.get("mcp_receipts", 0) > 0 and metrics.get("evidence_refs", 0) > 0)) else 0,
        "evidence_grounding": 20 if (not ontology or metrics.get("evidence_citation_count", 0) > 0) else 0,
        "goal_specific_shape": 20,
        "verification": 20 if metrics.get("result_accepted") else 0,
    }
    reasons: List[str] = []
    if ontology:
        if not direct_mode and not (
            ontology_contract_gate == "pass" and ontology_decision_gate == "pass"
        ):
            dimensions["goal_specific_shape"] = 0
            reasons.append("ontology_execution_contract_incomplete")
        if direct_mode:
            dimensions["goal_specific_shape"] = 20 if answer_grounding.get("accepted") and (not plan.get("action_required") or answer_shape.get("accepted")) else 0
        else:
            dimensions["goal_specific_shape"] = 20 if sections >= 2 and (not plan.get("action_required") or handoff.get("actionable_next_action")) else 0
        if graph_required:
            if graph_path_count <= 0:
                dimensions["goal_specific_shape"] = 0
                reasons.append("bounded_graph_path_missing")
            elif graph_semantic_gate == "weak":
                dimensions["goal_specific_shape"] = min(dimensions["goal_specific_shape"], 10)
                reasons.append("topology_observed_but_semantic_endpoint_unresolved")
            elif graph_semantic_gate == "unknown" and not direct_mode:
                dimensions["goal_specific_shape"] = min(dimensions["goal_specific_shape"], 10)
                reasons.append("graph_semantic_quality_not_observed")
        if not metrics.get("evidence_citation_count"):
            reasons.append("evidence_citation_missing")
        if not direct_mode and not metrics.get("oracle_accepted"):
            reasons.append("oracle_gate_missing")
    elif write:
        dimensions["goal_specific_shape"] = 20 if metrics.get("workspace_change_accepted") else 0
        if not metrics.get("workspace_change_accepted"):
            reasons.append("bounded_workspace_change_missing")
    accepted = bool(
        metrics.get("result_accepted")
        and all(value >= 20 for key, value in dimensions.items() if key != "goal_specific_shape" or not graph_required or graph_semantic_gate == "pass")
        and dimensions["goal_specific_shape"] >= 20
    )
    return {
        "schema": "crab.goal-contract-quality/v1",
        "known": bool(snapshot.get("schema") == "crab.observed-benchmark-snapshot/v1" or plan),
        "accepted": accepted,
        "score": sum(dimensions.values()),
        "max_score": 100,
        "dimensions": dimensions,
        "mode": "direct_baseline" if direct_mode else "goal_compiled_kinetic",
        "graph_path_count": graph_path_count,
        "graph_semantic_gate": graph_semantic_gate,
        "queen_section_count": sections,
        "ontology_contract_gate": ontology_contract_gate,
        "ontology_decision_gate": ontology_decision_gate,
        "ontology_slot_coverage": {
            "required": metrics.get("ontology_required_slot_count", 0),
            "filled": metrics.get("ontology_filled_required_slot_count", 0),
            "missing": metrics.get("ontology_missing_slot_count", 0),
        },
        "semantic_truth_unverified": True,
        "reasons": reasons,
        "limitations": [
            "Checks goal-specific receipts and shape, not whether the cited source or conclusion is factually true.",
            "A human or reference-answer judge is still required for semantic correctness.",
        ],
    }


def structural_quality(metrics: Dict[str, Any], snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Score observable workflow integrity without pretending semantic truth.

    This is intentionally a structural score. It measures provenance,
    evidence grounding, durable handoff, actual workspace change and Oracle
    acceptance. It must never be described as a human or model judge of the
    answer's meaning.
    """
    plan = snapshot.get("goal_plan") if isinstance(snapshot.get("goal_plan"), dict) else {}
    ontology = bool(plan.get("ontology_required")) or metrics.get("mcp_receipts", 0) > 0
    write = bool(plan.get("requires_write"))
    dimensions: Dict[str, int] = {
        "result": 25 if metrics.get("result_accepted") else 0,
        "provenance": 0,
        "grounding": 0,
        "kinetic_structure": 0,
        "verification": 0,
    }
    if ontology:
        dimensions["provenance"] = min(
            20,
            (10 if metrics.get("mcp_receipts", 0) > 0 else 0)
            + (10 if metrics.get("evidence_refs", 0) > 0 else 0),
        )
        dimensions["grounding"] = min(
            20,
            10 if metrics.get("evidence_citation_count", 0) > 0 else 0,
            # A structured handoff is evidence-aware only when the receipt
            # itself was observed; citation count is counted separately.
        )
        if metrics.get("structured_handoff_accepted"):
            dimensions["grounding"] = min(20, dimensions["grounding"] + 10)
        dimensions["kinetic_structure"] = min(
            20,
            (10 if metrics.get("ontology_ledgers", 0) > 0 else 0)
            + (10 if metrics.get("structured_handoff_accepted") else 0),
        )
        dimensions["verification"] = 15 if metrics.get("oracle_accepted") else 8 if metrics.get("result_accepted") else 0
    elif write:
        dimensions["provenance"] = 10 if metrics.get("workspace_change_accepted") else 0
        dimensions["kinetic_structure"] = min(20, 10 if metrics.get("accepted_artifacts", 0) > 0 else 0, 20 if metrics.get("local_gates", 0) > 0 else 10)
        dimensions["verification"] = 15 if metrics.get("oracle_accepted") else 8 if metrics.get("result_accepted") else 0
    else:
        dimensions["verification"] = 15 if metrics.get("oracle_accepted") else 8 if metrics.get("result_accepted") else 0
        dimensions["kinetic_structure"] = 10 if metrics.get("accepted_artifacts", 0) > 0 else 0
    score = sum(dimensions.values())
    return {
        "schema": "crab.structural-quality/v1",
        "score": score,
        "max_score": 100,
        "dimensions": dimensions,
        "scope": "observable_workflow_integrity",
        "not_a_semantic_judge": True,
        "limitations": [
            "Does not determine whether an answer is semantically correct beyond observed evidence and gates.",
            "Provider prompt-cache behavior is not a quality signal.",
        ],
    }


def compare_observed(adaptive_snapshot: Dict[str, Any], direct_snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Compare two observed runs without inventing missing provider usage."""
    adaptive = observed_metrics(adaptive_snapshot)
    direct = observed_metrics(direct_snapshot)
    unknowns = []
    if adaptive["tokens_observed"] is None or direct["tokens_observed"] is None:
        unknowns.append("tokens_observed")
    adaptive_plan = adaptive_snapshot.get("goal_plan") if isinstance(adaptive_snapshot.get("goal_plan"), dict) else {}
    ontology_goal = bool(adaptive_plan.get("ontology_required")) or adaptive["mcp_receipts"] > 0
    if ontology_goal and direct["mcp_receipts"] == 0 and direct["evidence_refs"] == 0:
        unknowns.append("direct_ontology_receipt")
    if ontology_goal and adaptive["result_accepted"] and adaptive["ontology_ledgers"] == 0:
        unknowns.append("adaptive_ontology_ledger")
    requires_write = bool(adaptive_plan.get("requires_write"))
    if requires_write and not adaptive["workspace_change_accepted"]:
        unknowns.append("adaptive_workspace_change")
    if requires_write and not direct["workspace_change_accepted"]:
        unknowns.append("direct_workspace_change")
    contract_enforced = bool(
        adaptive.get("goal_contract_quality", {}).get("known")
        and direct.get("goal_contract_quality", {}).get("known")
        and adaptive_snapshot.get("schema") == "crab.observed-benchmark-snapshot/v1"
        and direct_snapshot.get("schema") == "crab.observed-benchmark-snapshot/v1"
    )
    quality_parity = (
        adaptive["result_accepted"]
        and direct["result_accepted"]
        and adaptive["evidence_refs"] >= direct["evidence_refs"]
        and adaptive["accepted_artifacts"] >= direct["accepted_artifacts"]
        and (not ontology_goal or adaptive["ontology_ledgers"] >= 1)
        and (not requires_write or (adaptive["workspace_change_accepted"] and direct["workspace_change_accepted"]))
        and (
            not contract_enforced
            or (
                adaptive["goal_contract_quality"]["accepted"]
                and direct["goal_contract_quality"]["accepted"]
            )
        )
    )
    billable_known = (
        adaptive["billable_input_tokens_observed"] is not None
        and direct["billable_input_tokens_observed"] is not None
    )
    if adaptive["result_accepted"] and not direct["result_accepted"]:
        verdict = "adaptive_quality_advantage"
    elif quality_parity and not unknowns:
        adaptive_cost = (adaptive["tokens_observed"], adaptive["model_turns_observed"])
        direct_cost = (direct["tokens_observed"], direct["model_turns_observed"])
        total_advantage = adaptive_cost <= direct_cost and adaptive_cost != direct_cost
        billable_advantage = (
            not billable_known
            or adaptive["billable_input_tokens_observed"] <= direct["billable_input_tokens_observed"]
        )
        if total_advantage and billable_advantage:
            verdict = "adaptive_efficiency_advantage"
        elif total_advantage and billable_known:
            # Total tokens can fall while provider-billable input rises when
            # the direct turn receives a larger prompt-cache hit. Keep this
            # useful signal, but do not call it cost/token efficiency.
            verdict = "adaptive_total_token_advantage_cache_sensitive"
        else:
            verdict = "parity_or_inconclusive"
    else:
        verdict = "inconclusive"
    return {
        "measurement": "observed",
        "adaptive": adaptive,
        "direct_baseline": direct,
        "delta": {
            "tokens_observed": _delta(adaptive["tokens_observed"], direct["tokens_observed"]),
            "model_turns_observed": adaptive["model_turns_observed"] - direct["model_turns_observed"],
            "result_accepted": int(adaptive["result_accepted"]) - int(direct["result_accepted"]),
            "evidence_refs": adaptive["evidence_refs"] - direct["evidence_refs"],
            "accepted_artifacts": adaptive["accepted_artifacts"] - direct["accepted_artifacts"],
            "ontology_ledgers": adaptive["ontology_ledgers"] - direct["ontology_ledgers"],
            "goal_graphs": adaptive["goal_graphs"] - direct["goal_graphs"],
            "king_plans": adaptive["king_plans"] - direct["king_plans"],
            "automatic_revision_cycles": adaptive["automatic_revision_cycles"] - direct["automatic_revision_cycles"],
            "worker_subgoal_slots": adaptive["worker_subgoal_slots"] - direct["worker_subgoal_slots"],
            "worker_subgoal_receipts": adaptive["worker_subgoal_receipts"] - direct["worker_subgoal_receipts"],
            "workspace_changed_file_count": adaptive["workspace_changed_file_count"] - direct["workspace_changed_file_count"],
            "prompt_chars": adaptive["prompt_chars"] - direct["prompt_chars"],
            "context_chars": adaptive["context_chars"] - direct["context_chars"],
            "input_tokens_observed": _delta(adaptive["input_tokens_observed"], direct["input_tokens_observed"]),
            "cached_input_tokens_observed": _delta(adaptive["cached_input_tokens_observed"], direct["cached_input_tokens_observed"]),
            "billable_input_tokens_observed": _delta(adaptive["billable_input_tokens_observed"], direct["billable_input_tokens_observed"]),
            "goal_contract_score": adaptive["goal_contract_quality"]["score"] - direct["goal_contract_quality"]["score"],
        },
        "quality_parity": quality_parity,
        "contract_quality_enforced": contract_enforced,
        "goal_contract_quality": {
            "adaptive": adaptive["goal_contract_quality"],
            "direct_baseline": direct["goal_contract_quality"],
            "adaptive_advantage": adaptive["goal_contract_quality"]["score"] > direct["goal_contract_quality"]["score"],
        },
        "kinetic_contract_quality": {
            "adaptive": adaptive["kinetic_contract_quality"],
            "direct_baseline": direct["kinetic_contract_quality"],
            "adaptive_advantage": adaptive["kinetic_contract_quality"]["score"] > direct["kinetic_contract_quality"]["score"],
        },
        "structural_quality": {
            "adaptive": adaptive["structural_quality"],
            "direct_baseline": direct["structural_quality"],
            "adaptive_score_delta": adaptive["structural_quality"]["score"] - direct["structural_quality"]["score"],
            "adaptive_advantage": adaptive["structural_quality"]["score"] > direct["structural_quality"]["score"],
        },
        "grounding": {
            "adaptive_citations": adaptive["evidence_citation_count"],
            "direct_citations": direct["evidence_citation_count"],
            "adaptive_citation_advantage": adaptive["evidence_citation_count"] > direct["evidence_citation_count"],
            "adaptive_structured_handoff": adaptive["structured_handoff_accepted"],
            "adaptive_workspace_change": adaptive["workspace_change_accepted"],
            "direct_workspace_change": direct["workspace_change_accepted"],
        },
        "efficiency": {
            "total_token_advantage": (
                adaptive["tokens_observed"] is not None
                and direct["tokens_observed"] is not None
                and adaptive["tokens_observed"] < direct["tokens_observed"]
            ),
            "billable_input_advantage": (
                adaptive["billable_input_tokens_observed"] is not None
                and direct["billable_input_tokens_observed"] is not None
                and adaptive["billable_input_tokens_observed"] < direct["billable_input_tokens_observed"]
            )
            if adaptive["billable_input_tokens_observed"] is not None and direct["billable_input_tokens_observed"] is not None
            else None,
            "cache_sensitive": (
                billable_known
                and adaptive["billable_input_tokens_observed"] > direct["billable_input_tokens_observed"]
            ),
        },
        "unknowns": unknowns,
        "verdict": verdict,
        "guardrail": "Only identical-goal, same-start-state observed runs can establish superiority; missing usage stays unknown.",
    }


def _delta(left: Optional[int], right: Optional[int]) -> Optional[int]:
    if left is None or right is None:
        return None
    return left - right
