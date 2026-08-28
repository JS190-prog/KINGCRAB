from __future__ import annotations

import json
from typing import Any, Dict, List


KINETIC_WORKFLOW_SCHEMA = "crab.kinetic-workflow/v1"


def _step(
    step_id: str,
    operator: str,
    role: str,
    inputs: List[str],
    outputs: List[str],
    *,
    model_required: bool = False,
    mcp_calls: int = 0,
    write_scope: str = "none",
    gate: str = "always",
) -> Dict[str, Any]:
    return {
        "id": step_id,
        "operator": operator,
        "role": role,
        "inputs": list(inputs),
        "outputs": list(outputs),
        "model_required": bool(model_required),
        "mcp_calls": max(0, int(mcp_calls)),
        "write_scope": write_scope,
        "gate": gate,
        "status": "planned",
    }


def compile_kinetic_workflow(
    plan: Dict[str, Any],
    graph: Dict[str, Any],
) -> Dict[str, Any]:
    """Compile the goal into an ontology-to-action execution path.

    The workflow is intentionally deterministic. It is not another model
    plan: it is the runtime contract that explains which state is allowed to
    move from one role to the next and where provider tokens may be spent.
    """
    plan = plan if isinstance(plan, dict) else {}
    graph = graph if isinstance(graph, dict) else {}
    ontology = bool(plan.get("ontology_required"))
    action_mode = str(plan.get("action_mode") or "answer")
    graph_required = bool(plan.get("graph_required"))
    requires_write = bool(plan.get("requires_write"))
    requires_worker_output = bool(plan.get("requires_worker_output"))
    worker_required = requires_write or requires_worker_output
    requires_model_queen = bool(plan.get("requires_model_queen"))
    requires_model_planning = bool(plan.get("requires_model_planning"))
    requires_oracle_model = bool(plan.get("requires_oracle_model"))
    route = plan.get("retrieval_contract") if isinstance(plan.get("retrieval_contract"), dict) else {}
    steps: List[Dict[str, Any]] = [
        _step(
            "goal_bind",
            "bind_goal",
            "KING",
            [],
            ["goal_graph", "goal_contract"],
            model_required=requires_model_planning,
        ),
        _step(
            "scope_lock",
            "lock_scope",
            "QUEEN" if ontology else "KING",
            ["goal_graph"],
            ["scope_contract"],
            gate="ontology_required" if ontology else "always",
        ),
    ]
    if ontology:
        steps.extend(
            [
                _step(
                    "retrieve_evidence",
                    "retrieve_evidence",
                    "QUEEN",
                    ["scope_contract"],
                    ["mcp_receipt"],
                    mcp_calls=int(route.get("max_mcp_calls") or 1),
                    gate="authoritative_mcp_receipt",
                ),
                _step(
                    "bind_ontology_slots",
                    "bind_evidence_slots",
                    "QUEEN",
                    ["mcp_receipt", "goal_graph"],
                    ["ontology_execution_contract"],
                    gate="required_evidence_slots",
                ),
            ]
        )
        if graph_required:
            steps.append(
                _step(
                    "traverse_graph",
                    "traverse_typed_path",
                    "QUEEN",
                    ["ontology_execution_contract"],
                    ["bounded_graph_path"],
                    mcp_calls=max(0, int(route.get("max_mcp_calls") or 1) - 1),
                    gate="typed_evidence_backed_path",
                )
            )
        if action_mode in {"explain", "research"}:
            operator = "explain_from_evidence_path"
        elif action_mode == "compare":
            operator = "compare_claims_and_paths"
        elif action_mode == "lookup":
            operator = "project_observed_items"
        elif action_mode == "plan":
            operator = "derive_bounded_action_path"
        elif action_mode == "execute":
            operator = "compile_worker_action_contract"
        else:
            operator = "synthesize_grounded_answer"
        steps.append(
            _step(
                "decide_from_ontology",
                operator,
                "QUEEN",
                ["ontology_execution_contract"] + (["bounded_graph_path"] if graph_required else []),
                ["decision_packet"],
                model_required=requires_model_queen,
                gate="decision_slots",
            )
        )
        steps.append(
            _step(
                "persist_ontology_ledger",
                "persist_ontology_ledger",
                "QUEEN",
                ["ontology_execution_contract", "decision_packet"],
                ["ontology_ledger"],
                gate="durable_ledger_readback",
            )
        )
    if bool(plan.get("external_scouting")):
        steps.append(
            _step(
                "scout_external",
                "scout_and_deduplicate",
                "SOLDIER",
                ["decision_packet" if ontology else "goal_contract"],
                ["scout_receipt"],
                model_required=True,
                mcp_calls=0,
                gate="policy_bound_external_scope",
            )
        )
    steps.append(
        _step(
            "patrol_quality",
            "patrol_waste_and_gates",
            "SOLDIER",
            (["decision_packet", "ontology_ledger"] if ontology else ["goal_contract"]),
            ["soldier_report"],
            gate="no_unresolved_required_gate",
        )
    )
    if worker_required:
        if requires_write:
            steps.append(
                _step(
                    "execute_action",
                    "execute_bounded_change",
                    "WORKER",
                    ["decision_packet", "soldier_report"],
                    ["workspace_change", "worker_receipt"],
                    model_required=True,
                    write_scope="task_contract_only",
                    gate="worker_approval_and_scope",
                )
            )
        else:
            steps.append(
                _step(
                    "produce_deliverable",
                    "produce_evidence_bound_deliverable",
                    "WORKER",
                    ["decision_packet", "soldier_report"],
                    ["worker_result", "worker_receipt"],
                    model_required=True,
                    write_scope="none",
                    gate="worker_evidence_and_scope",
                )
            )
    steps.append(
        _step(
            "verify_result",
            "verify_and_publish_or_stop",
            "ORACLE",
            (["decision_packet", "ontology_ledger", "soldier_report"] if ontology else ["decision_packet", "soldier_report"])
            + (["workspace_change"] if requires_write else [])
            + (["worker_result"] if requires_worker_output and not requires_write else []),
            ["oracle_verdict", "goal_outcome"],
            model_required=requires_oracle_model,
            gate="all_required_receipts",
        )
    )
    return {
        "schema": KINETIC_WORKFLOW_SCHEMA,
        "goal_graph_id": graph.get("graph_id"),
        "objective": str(plan.get("objective") or ""),
        "ontology_centric": ontology,
        "operator": action_mode,
        "steps": steps,
        "role_order": [step["role"] for step in steps],
        "token_policy": {
            "planned_model_turns": int(plan.get("estimated_model_turns") or 0),
            "context_char_budget": int(plan.get("context_char_budget") or 0),
            "mcp_call_budget": sum(int(step.get("mcp_calls") or 0) for step in steps),
            "reuse_authoritative_receipt": ontology,
            "allow_full_catalog_in_prompt": False,
            "allow_unbounded_role_retry": False,
        },
        "state_invariants": [
            "evidence_can_only_enter_from_observed_mcp_receipt",
            "decision_requires_bound_evidence_slots",
            "worker_receives_decision_packet_not_full_catalog",
            "oracle_publishes_only_after_required_receipts",
            "non_mutating_worker_never_emits_workspace_change",
        ],
        "output_contract": {
            "decision_slots": list(graph.get("decision_slots") or []),
            "action_required": bool(plan.get("action_required")),
            "requires_write": requires_write,
            "requires_worker_output": requires_worker_output,
            "ontology_ledger_required": ontology,
        },
    }


def compact_kinetic_workflow(workflow: Dict[str, Any], max_chars: int = 2200) -> str:
    """Render the operator path for a provider without replaying receipts."""
    payload = {
        "goal_graph_id": workflow.get("goal_graph_id"),
        "operator": workflow.get("operator"),
        "steps": [
            {
                "id": step.get("id"),
                "operator": step.get("operator"),
                "role": step.get("role"),
                "inputs": step.get("inputs") or [],
                "outputs": step.get("outputs") or [],
                "model_required": step.get("model_required"),
                "gate": step.get("gate"),
            }
            for step in workflow.get("steps") or []
        ],
        "token_policy": workflow.get("token_policy") or {},
        "state_invariants": workflow.get("state_invariants") or [],
    }
    value = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return value if len(value) <= max_chars else value[: max_chars - 20] + "...[bounded]"


def workflow_summary(workflow: Dict[str, Any]) -> Dict[str, Any]:
    """Return a small stable summary for snapshots and panel clients."""
    steps = workflow.get("steps") if isinstance(workflow.get("steps"), list) else []
    completed = [step for step in steps if isinstance(step, dict) and step.get("status") == "completed"]
    return {
        "schema": workflow.get("schema"),
        "operator": workflow.get("operator"),
        "ontology_centric": bool(workflow.get("ontology_centric")),
        "step_count": len(steps),
        "completed_step_count": len(completed),
        "planned_model_turns": (workflow.get("token_policy") or {}).get("planned_model_turns", 0),
        "mcp_call_budget": (workflow.get("token_policy") or {}).get("mcp_call_budget", 0),
        "invariants": list(workflow.get("state_invariants") or []),
    }
