from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List


GRAPH_SCHEMA = "crab.goal-graph/v1"


def _slug(value: Any, limit: int = 48) -> str:
    text = "-".join(str(value or "").strip().lower().split())
    safe = "".join(char if char.isalnum() or char in "-_" else "-" for char in text)
    return safe.strip("-")[:limit] or "item"


def _node(node_id: str, node_type: str, label: str, *, required: bool = True) -> Dict[str, Any]:
    return {
        "id": node_id,
        "type": node_type,
        "label": " ".join(str(label or "").split())[:240],
        "required": bool(required),
    }


def _edge(source: str, relation: str, target: str, *, required: bool = True) -> Dict[str, Any]:
    return {"from": source, "relation": relation, "to": target, "required": bool(required)}


def compile_goal_graph(plan: Dict[str, Any]) -> Dict[str, Any]:
    """Compile one objective into a small, inspectable kinetic graph.

    This is deterministic and model-free. It is the shared contract between
    KING's plan, QUEEN's evidence slots, WORKER's bounded action and ORACLE's
    verification gate. It does not claim that a node is true; receipts fill
    the slots later.
    """
    objective = " ".join(str(plan.get("objective") or "").split())
    digest = hashlib.sha256(objective.encode("utf-8")).hexdigest()[:16]
    graph_id = "goal-%s" % digest
    kind = str(plan.get("kind") or "bounded_execution")
    action_mode = str(plan.get("action_mode") or "answer")
    ontology = bool(plan.get("ontology_required"))
    action_required = bool(plan.get("action_required"))
    route = plan.get("retrieval_contract") if isinstance(plan.get("retrieval_contract"), dict) else {}

    goal_id = "%s.goal" % graph_id
    intent_id = "%s.intent.%s" % (graph_id, _slug(action_mode))
    scope_id = "%s.scope" % graph_id
    decision_id = "%s.decision" % graph_id
    action_id = "%s.action" % graph_id
    verify_id = "%s.verify" % graph_id
    feedback_id = "%s.feedback" % graph_id

    nodes: List[Dict[str, Any]] = [
        _node(goal_id, "goal", objective),
        _node(intent_id, "intent", "%s / %s" % (kind, action_mode)),
        _node(
            scope_id,
            "ontology_scope" if ontology else "workspace_scope",
            "selected packs/projects" if plan.get("selected_pack_count") or plan.get("selected_project_count") else "connected workspace",
        ),
        _node(decision_id, "decision", "supported claims and gaps"),
        _node(action_id, "action", "ordered next action" if action_required else "answer or bounded result", required=action_required),
        _node(verify_id, "verification", "Oracle and artifact acceptance"),
        _node(feedback_id, "feedback", "revise or stop at the next boundary", required=False),
    ]
    edges: List[Dict[str, Any]] = [
        _edge(goal_id, "has_intent", intent_id),
        _edge(intent_id, "constrained_by", scope_id),
        _edge(scope_id, "feeds", decision_id),
        _edge(decision_id, "produces", action_id, required=action_required),
        _edge(action_id, "checked_by", verify_id, required=action_required),
        _edge(decision_id, "checked_by", verify_id, required=not action_required),
        _edge(verify_id, "opens", feedback_id, required=False),
    ]

    evidence_slots: List[Dict[str, Any]] = []
    if ontology:
        spaces = [str(value) for value in route.get("spaces") or plan.get("ontology_spaces") or [] if str(value).strip()]
        requirements = [str(value) for value in route.get("claim_requirements") or [] if str(value).strip()]
        for value in spaces[:8]:
            slot_id = "%s.evidence.%s" % (graph_id, _slug(value))
            nodes.append(_node(slot_id, "evidence_slot", value))
            edges.append(_edge(intent_id, "requires_evidence", slot_id))
            evidence_slots.append({"id": slot_id, "space": value, "required": True})
        for value in requirements[:8]:
            slot_id = "%s.claim.%s" % (graph_id, _slug(value))
            nodes.append(_node(slot_id, "claim_requirement", value))
            edges.append(_edge(decision_id, "must_satisfy", slot_id))
            evidence_slots.append({"id": slot_id, "requirement": value, "required": True})

    if plan.get("requires_write"):
        artifact_id = "%s.artifact" % graph_id
        test_id = "%s.tests" % graph_id
        nodes.extend([
            _node(artifact_id, "artifact", "bounded workspace change"),
            _node(test_id, "verification_input", "focused tests and workspace diff"),
        ])
        edges.extend([
            _edge(action_id, "creates", artifact_id),
            _edge(artifact_id, "measured_by", test_id),
            _edge(test_id, "feeds", verify_id),
        ])

    model_turns = max(0, int(plan.get("estimated_model_turns") or 0))
    loop = ["restate_goal", "scope_context"]
    if ontology:
        loop.append("retrieve_evidence")
    if plan.get("requires_write"):
        loop.extend(["execute_bounded_change", "inspect_diff_and_tests"])
    else:
        loop.append("draft_or_project_result")
    loop.extend(["judge_against_goal_graph", "publish_or_stop"])
    return {
        "schema": GRAPH_SCHEMA,
        "graph_id": graph_id,
        "objective_digest": digest,
        "objective": objective,
        "kind": kind,
        "ontology_mode": plan.get("ontology_mode") or "none",
        "nodes": nodes,
        "edges": edges,
        "evidence_slots": evidence_slots,
        "decision_slots": list(plan.get("response_contract") or ["answer"]),
        "verification_loop": {
            "steps": loop,
            "max_automatic_revision_cycles": max(0, int(plan.get("max_automatic_revision_cycles") or 0)),
            "manual_retry_preserves_lineage": True,
            "repair_boundary": "before_workspace_write" if plan.get("requires_write") else "before_publish",
        },
        "token_policy": {
            "planned_model_turns": model_turns,
            "context_char_budget": int(plan.get("context_char_budget") or 0),
            "local_gate_preferred": True,
            "no_role_invocation_without_a_missing_contract": True,
        },
    }


def compact_goal_graph(
    graph: Dict[str, Any],
    *,
    max_chars: int = 2400,
    include_topology: bool = False,
) -> str:
    """Render the operational graph surface for a provider prompt.

    Full nodes and edges stay in `goal_graph.json`. Provider prompts receive
    slots and loop state by default; topology is opt-in for an audit/debug
    view so the graph itself does not become another transcript dump.
    """
    nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
    edges = graph.get("edges") if isinstance(graph.get("edges"), list) else []
    payload = {
        "graph_id": graph.get("graph_id"),
        "kind": graph.get("kind"),
        "ontology_mode": graph.get("ontology_mode"),
        "decision_slots": graph.get("decision_slots") or [],
        "verification_loop": graph.get("verification_loop") or {},
        "token_policy": graph.get("token_policy") or {},
        "evidence_slots": (graph.get("evidence_slots") or [])[:16],
    }
    if include_topology:
        payload["nodes"] = nodes[:18]
        payload["edges"] = edges[:24]
    value = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return value if len(value) <= max_chars else value[: max_chars - 24] + "...[bounded]"
