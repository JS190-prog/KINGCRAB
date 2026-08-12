from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Optional


ONTOLOGY_CONTRACT_SCHEMA = "crab.ontology-execution-contract/v1"


def _clean(value: Any, limit: int = 420) -> str:
    return " ".join(str(value or "").split())[:limit]


def _terms(value: Any) -> set[str]:
    return {
        token.lower()
        for token in re.findall(r"[0-9A-Za-z가-힣]{2,}", str(value or ""))
    }


def _rows(receipt: Optional[Dict[str, Any]], key: str) -> List[Dict[str, Any]]:
    if not isinstance(receipt, dict) or not isinstance(receipt.get(key), list):
        return []
    return [row for row in receipt[key] if isinstance(row, dict)]


def _evidence_identity(row: Dict[str, Any]) -> str:
    return _clean(row.get("id") or row.get("evidence_id"), 240)


def _evidence_metadata(row: Dict[str, Any]) -> Dict[str, Any]:
    value = row.get("metadata")
    return value if isinstance(value, dict) else {}


def _slot_terms(slot: Dict[str, Any]) -> set[str]:
    values = [slot.get("space"), slot.get("requirement"), slot.get("label")]
    return set().union(*(_terms(value) for value in values))


def _space_match(slot: Dict[str, Any], row: Dict[str, Any]) -> bool:
    space = _clean(slot.get("space"), 80).lower()
    if not space:
        return False
    metadata = _evidence_metadata(row)
    metadata_space = _clean(
        metadata.get("space")
        or metadata.get("ontology_space")
        or metadata.get("node_type")
        or row.get("space"),
        80,
    ).lower()
    if metadata_space and metadata_space == space:
        return True
    # A generic evidence/source slot can be proven from the shape of the
    # observed MCP row. Do not promote prose into a typed ontology space.
    if space == "evidence":
        return bool(_evidence_identity(row) and _clean(row.get("text") or row.get("content")))
    if space == "resource":
        return bool(_clean(row.get("source") or row.get("source_uri")))
    return False


def _requirement_match(requirement: str, receipt: Dict[str, Any]) -> tuple[List[str], List[str], int]:
    evidence = _rows(receipt, "evidence")
    nodes = _rows(receipt, "nodes")
    edges = _rows(receipt, "edges")
    paths = _rows(receipt, "paths")
    key = _clean(requirement, 80).lower()
    if key == "evidence_id":
        ids = [_evidence_identity(row) for row in evidence if _evidence_identity(row)]
        return ids[:12], [], 0
    if key == "source_uri":
        ids = [
            _evidence_identity(row)
            for row in evidence
            if _evidence_identity(row) and _clean(row.get("source") or row.get("source_uri"))
        ]
        return ids[:12], [], 0
    if key == "captured_text":
        ids = [
            _evidence_identity(row)
            for row in evidence
            if _evidence_identity(row) and _clean(row.get("text") or row.get("content"))
        ]
        return ids[:12], [], 0
    if key == "node_id":
        ids = [_clean(row.get("id") or row.get("node_id"), 240) for row in nodes]
        return [], [value for value in ids if value][:12], 0
    if key == "typed_relation":
        relations = [
            _clean(row.get("relation") or row.get("type") or row.get("label"), 120)
            for row in edges
        ]
        return [], [value for value in relations if value][:12], 0
    if key == "bounded_path":
        return [], [], len(paths)
    return [], [], 0


def _explicit_spaces(route: Dict[str, Any]) -> set[str]:
    values = route.get("explicit_spaces") if isinstance(route.get("explicit_spaces"), list) else []
    return {_clean(value, 80).lower() for value in values if _clean(value, 80)}


def _required_space(slot: Dict[str, Any], route: Dict[str, Any]) -> bool:
    space = _clean(slot.get("space"), 80).lower()
    # The route always includes a default ontology spine. Evidence and its
    # source/resource anchor are hard gates. Claim/outcome/concept spaces are
    # decision material, not facts that can be declared present merely because
    # a query used a related word; they remain visible as gaps for Queen/Oracle.
    return space in {"evidence", "resource"}


def _metadata_only_lookup(plan: Dict[str, Any], receipt: Dict[str, Any]) -> bool:
    """Identify a typed catalog observation that has no content evidence.

    A pack/project listing is useful metadata, but it must never satisfy a
    claim-evidence gate. It gets its own observation gate instead.
    """
    return (
        str(plan.get("action_mode") or "") == "lookup"
        and bool(_rows(receipt, "observed_items"))
        and not bool(_rows(receipt, "evidence"))
    )


def _build_evidence_slots(
    plan: Dict[str, Any],
    graph: Dict[str, Any],
    receipt: Dict[str, Any],
) -> List[Dict[str, Any]]:
    route = plan.get("retrieval_contract") if isinstance(plan.get("retrieval_contract"), dict) else {}
    metadata_only = _metadata_only_lookup(plan, receipt)
    result: List[Dict[str, Any]] = []
    graph_slots = graph.get("evidence_slots") if isinstance(graph.get("evidence_slots"), list) else []
    for raw in graph_slots:
        if not isinstance(raw, dict):
            continue
        slot = {
            "id": _clean(raw.get("id"), 180),
            "label": _clean(raw.get("space") or raw.get("requirement") or raw.get("id"), 180),
            "kind": "requirement" if raw.get("requirement") else "space",
            "space": _clean(raw.get("space"), 80) or None,
            "requirement": _clean(raw.get("requirement"), 80) or None,
            "required": (
                not metadata_only
                and (bool(raw.get("requirement")) or _required_space(raw, route))
            ),
            "observed_evidence_ids": [],
            "observed_node_ids": [],
            "observed_relations": [],
            "observed_path_count": 0,
            "status": "pending",
        }
        if not slot["id"]:
            continue
        if metadata_only:
            slot["status"] = "not_required"
            result.append(slot)
            continue
        if slot["kind"] == "requirement":
            ids, nodes, path_count = _requirement_match(str(slot["requirement"] or ""), receipt)
            slot["observed_evidence_ids"] = ids
            slot["observed_node_ids"] = nodes
            slot["observed_path_count"] = path_count
            if str(slot["requirement"] or "") == "typed_relation":
                _, relations, _ = _requirement_match("typed_relation", receipt)
                slot["observed_relations"] = relations
            filled = bool(ids or nodes or path_count)
        else:
            matched = [row for row in _rows(receipt, "evidence") if _space_match(slot, row)]
            slot["observed_evidence_ids"] = [_evidence_identity(row) for row in matched if _evidence_identity(row)][:12]
            filled = bool(matched)
        slot["status"] = "filled" if filled else "missing" if slot["required"] else "unresolved"
        result.append(slot)
    return result


def _build_decision_slots(graph: Dict[str, Any]) -> List[Dict[str, Any]]:
    values = graph.get("decision_slots") if isinstance(graph.get("decision_slots"), list) else []
    return [
        {
            "id": _clean(value, 120),
            "label": _clean(value, 120),
            "required": True,
            "status": "pending",
        }
        for value in values
        if _clean(value, 120)
    ]


def _coverage(
    plan: Dict[str, Any],
    receipt: Dict[str, Any],
    evidence_slots: List[Dict[str, Any]],
) -> Dict[str, Any]:
    if not receipt:
        return {
            "gate": "pending",
            "required_slot_count": sum(1 for slot in evidence_slots if slot.get("required")),
            "filled_required_slot_count": 0,
            "missing_slot_ids": [],
            "evidence_id_count": 0,
            "path_count": 0,
        }
    required = [slot for slot in evidence_slots if slot.get("required")]
    missing = [slot for slot in required if slot.get("status") != "filled"]
    graph_required = bool(plan.get("graph_required"))
    claim_gate = str(receipt.get("claim_gate") or "blocked")
    graph_gate = str(receipt.get("graph_gate") or "not_required")
    metadata_only = _metadata_only_lookup(plan, receipt)
    if metadata_only:
        observation_gate = (
            "pass"
            if str(receipt.get("status") or "") in {"ok", "cached", "no_evidence"}
            and bool(_rows(receipt, "observed_items"))
            and not graph_required
            else "blocked"
        )
        return {
            "gate": observation_gate,
            "mode": "metadata_observation",
            "observation_gate": observation_gate,
            "required_slot_count": 0,
            "filled_required_slot_count": 0,
            "missing_slot_ids": [],
            "evidence_id_count": 0,
            "observed_item_count": len(_rows(receipt, "observed_items")),
            "path_count": len(_rows(receipt, "paths")),
        }
    gate = "pass" if (
        str(receipt.get("status") or "") in {"ok", "cached", "no_evidence"}
        and claim_gate == "pass"
        and not missing
        and (not graph_required or graph_gate == "pass")
    ) else "blocked"
    return {
        "gate": gate,
        "required_slot_count": len(required),
        "filled_required_slot_count": sum(1 for slot in required if slot.get("status") == "filled"),
        "missing_slot_ids": [str(slot.get("id")) for slot in missing],
        "evidence_id_count": len({_evidence_identity(row) for row in _rows(receipt, "evidence") if _evidence_identity(row)}),
        "mode": "evidence",
        "observation_gate": "not_required",
        "observed_item_count": len(_rows(receipt, "observed_items")),
        "path_count": len(_rows(receipt, "paths")),
    }


def compile_ontology_execution_contract(
    plan: Dict[str, Any],
    graph: Dict[str, Any],
    receipt: Optional[Dict[str, Any]] = None,
    *,
    king_plan: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Compile the goal's ontology work into a slot-level execution ledger.

    This is the boundary that makes KINGCRAB meaningfully different from a
    direct prompt: evidence is admitted to a named goal slot before a claim
    or action can be accepted. Missing optional ontology spaces remain visible
    as gaps; only explicit anchors and evidence requirements block the gate.
    """
    plan = plan if isinstance(plan, dict) else {}
    graph = graph if isinstance(graph, dict) else {}
    observed = receipt if isinstance(receipt, dict) else {}
    route = plan.get("retrieval_contract") if isinstance(plan.get("retrieval_contract"), dict) else {}
    evidence_slots = _build_evidence_slots(plan, graph, observed)
    decision_slots = _build_decision_slots(graph)
    return {
        "schema": ONTOLOGY_CONTRACT_SCHEMA,
        "authority": "user_goal_and_opencrab_mcp_receipt",
        "goal_graph_id": graph.get("graph_id"),
        "objective": _clean(plan.get("objective"), 800),
        "route": {
            "mode": route.get("mode"),
            "primary_query": _clean(route.get("primary_query") or plan.get("objective"), 900),
            "query_terms": [str(value) for value in route.get("query_terms") or []][:16],
            "spaces": [str(value) for value in route.get("spaces") or []][:12],
            "relation_bias": [str(value) for value in route.get("relation_bias") or []][:8],
            "scope_mode": route.get("scope_mode"),
            "package_limit": route.get("package_limit"),
        },
        "evidence_slots": evidence_slots,
        "decision_slots": decision_slots,
        "action_contract": {
            "mode": plan.get("action_mode"),
            "required": bool(plan.get("action_required")),
            "requires_write": bool(plan.get("requires_write")),
            "response_contract": [str(value) for value in plan.get("response_contract") or []],
            "next_action_authority": "queen_handoff_or_oracle_gate",
        },
        "coverage": _coverage(plan, observed, evidence_slots),
        "observation": {
            "mode": "metadata_observation" if _metadata_only_lookup(plan, observed) else "evidence",
            "observed_item_count": len(_rows(observed, "observed_items")),
            "content_evidence_count": len(_rows(observed, "evidence")),
        },
        "king_subgoals": [
            {"text": _clean(value, 240), "slot_ids": []}
            for value in (king_plan or {}).get("subgoals") or []
            if _clean(value, 240)
        ],
        "decision_gate": "pending",
        "observed_receipt": bool(receipt),
    }


def attach_subgoal_slots(contract: Dict[str, Any], subgoals: Iterable[str]) -> Dict[str, Any]:
    """Associate each KING subgoal with observable evidence slots."""
    slots = contract.get("evidence_slots") if isinstance(contract.get("evidence_slots"), list) else []
    for value in subgoals:
        text = _clean(value, 240)
        if not text:
            continue
        tokens = _terms(text)
        scored = []
        for slot in slots:
            haystack = _slot_terms(slot)
            score = len(tokens & haystack)
            if slot.get("required") and slot.get("status") == "missing":
                score += 1
            scored.append((score, str(slot.get("id") or "")))
        selected = [slot_id for score, slot_id in sorted(scored, reverse=True) if score > 0 and slot_id][:2]
        if not selected:
            selected = [str(slot.get("id")) for slot in slots if slot.get("required")][:2]
        contract.setdefault("king_subgoals", []).append({"text": text, "slot_ids": selected})
    return contract


def update_decision_gate(contract: Dict[str, Any], text: str) -> Dict[str, Any]:
    """Record structural decision-slot coverage without judging truth."""
    raw_value = str(text or "")
    value = raw_value
    # Local role contracts are JSON envelopes whose human-readable decision
    # sections live in ``interpretation``. Model turns usually return the
    # sections directly. Normalize both forms before applying the same gate.
    try:
        parsed = json.loads(raw_value)
    except (TypeError, ValueError):
        parsed = None
    if isinstance(parsed, dict):
        candidates = [
            parsed.get("interpretation"),
            parsed.get("answer"),
            parsed.get("result"),
            parsed.get("conclusion"),
        ]
        value = "\n".join(str(item) for item in candidates if item)
        if parsed.get("next_action"):
            value += "\nNEXT_ACTION\n%s" % parsed.get("next_action")
    section_names = (
        "SELECTED_PATH",
        "OBSERVED_ITEMS",
        "SOURCE_REFS",
        "SUPPORTED_CLAIMS",
        "GAPS",
        "NEXT_ACTION",
        "BOUNDED_CHANGE",
        "VERIFICATION",
        "ORDERED_ACTIONS",
    )
    sections = {
        name.lower(): bool(
            re.search(r"(?:^|\n)\s*%s\s*(?:\n|$)" % re.escape(name), value, re.IGNORECASE)
        )
        for name in section_names
    }
    for slot in contract.get("decision_slots") or []:
        label = str(slot.get("label") or "").lower()
        aliases = {
            "ordered_actions": ("ordered_actions", "selected_path"),
            "bounded_change": ("bounded_change", "next_action", "selected_path"),
            "verification": ("verification", "supported_claims"),
            "answer": ("answer", "supported_claims"),
            "observed_items": ("observed_items", "selected_path", "supported_claims"),
            "source_refs": ("source_refs", "supported_claims", "verification", "selected_path"),
        }.get(label, (label,))
        slot["status"] = "filled" if any(sections.get(alias, False) for alias in aliases) else "missing"
    required = [slot for slot in contract.get("decision_slots") or [] if slot.get("required")]
    contract["decision_gate"] = "pass" if required and all(slot.get("status") == "filled" for slot in required) else "blocked"
    contract["decision_observation"] = {
        "section_count": sum(1 for present in sections.values() if present),
        "evidence_id_count": len(set(re.findall(r"(?:evidence_id:\s*|\b)([A-Za-z0-9][A-Za-z0-9_-]{7,})", value))),
        "sections": sections,
        "raw_text": raw_value[-5000:],
    }
    return contract


def compact_ontology_execution_contract(contract: Dict[str, Any], max_chars: int = 2800) -> str:
    """Render the slot ledger, never the whole evidence receipt, to a prompt."""
    payload = {
        "goal_graph_id": contract.get("goal_graph_id"),
        "coverage": contract.get("coverage") or {},
        "decision_gate": contract.get("decision_gate"),
        "decision_slots": contract.get("decision_slots") or [],
        "route": contract.get("route") or {},
        "evidence_slots": [
            {
                "id": slot.get("id"),
                "label": slot.get("label"),
                "required": slot.get("required"),
                "status": slot.get("status"),
                "evidence_ids": (slot.get("observed_evidence_ids") or [])[:4],
                "node_ids": (slot.get("observed_node_ids") or [])[:3],
                "path_count": slot.get("observed_path_count", 0),
            }
            for slot in contract.get("evidence_slots") or []
        ],
        "action_contract": contract.get("action_contract") or {},
    }
    value = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return value if len(value) <= max_chars else value[: max_chars - 20] + "...[bounded]"
