from __future__ import annotations

import json

import pytest

from crabagent.goal import classify_goal
from crabagent.goal_graph import compile_goal_graph
from crabagent.ontology_contract import (
    compile_ontology_execution_contract,
    promote_execution_contract_to_ledger,
    update_decision_gate,
    validate_ontology_ledger,
)


def _plan_and_graph(objective: str):
    plan = classify_goal(objective).to_dict()
    return plan, compile_goal_graph(plan)


def test_contract_maps_observed_evidence_to_required_slots() -> None:
    plan, graph = _plan_and_graph("오픈크랩 팩의 근거를 비교해서 전략을 추천해줘")
    receipt = {
        "status": "ok",
        "claim_gate": "pass",
        "graph_gate": "not_required",
        "evidence": [
            {
                "id": "ev-12345678",
                "source": "opencrab://pack/chunk-1",
                "text": "관측된 근거",
            }
        ],
    }

    contract = compile_ontology_execution_contract(plan, graph, receipt)

    assert contract["coverage"]["gate"] == "pass"
    assert contract["coverage"]["missing_slot_ids"] == []
    assert all(
        slot["observed_evidence_ids"]
        for slot in contract["evidence_slots"]
        if slot["required"]
    )


def test_contract_blocks_evidence_without_source_or_captured_text() -> None:
    plan, graph = _plan_and_graph("오픈크랩 팩의 근거를 비교해서 전략을 추천해줘")
    receipt = {
        "status": "ok",
        "claim_gate": "pass",
        "graph_gate": "not_required",
        "evidence": [{"id": "ev-12345678"}],
    }

    contract = compile_ontology_execution_contract(plan, graph, receipt)
    missing = set(contract["coverage"]["missing_slot_ids"])

    assert contract["coverage"]["gate"] == "blocked"
    assert any(slot_id.endswith("claim.source_uri") for slot_id in missing)
    assert any(slot_id.endswith("claim.captured_text") for slot_id in missing)


def test_decision_gate_reads_json_local_interpretation() -> None:
    contract = {
        "decision_slots": [
            {"id": "observed_items", "label": "observed_items", "required": True, "status": "missing"},
            {"id": "source_refs", "label": "source_refs", "required": True, "status": "missing"},
        ]
    }
    local_contract = {
        "interpretation": (
            "SELECTED_PATH\nresource -> evidence\n"
            "SUPPORTED_CLAIMS\nObserved item [evidence_id: ev-12345678]\n"
            "GAPS\nNone\nNEXT_ACTION\nSTOP"
        )
    }

    update_decision_gate(contract, json.dumps(local_contract))

    assert contract["decision_gate"] == "pass"
    assert all(slot["status"] == "filled" for slot in contract["decision_slots"])
    assert contract["decision_observation"]["sections"]["selected_path"] is True


def test_decision_gate_stays_blocked_without_a_decision_section() -> None:
    contract = {
        "decision_slots": [
            {"id": "observed_items", "label": "observed_items", "required": True, "status": "missing"},
        ]
    }

    update_decision_gate(contract, "근거를 확인했다.")

    assert contract["decision_gate"] == "blocked"
    assert contract["decision_slots"][0]["status"] == "missing"


@pytest.mark.parametrize("heading", ["{name}:", "## {name}", "**{name}:**", "**{name}**:", "{name}: inline detail"])
def test_host_decision_headings_accept_standard_markdown_and_colons(heading):
    contract = {"decision_slots": [{"label": name, "required": True} for name in
                                  ("selected_path", "bounded_change", "verification", "next_action")]}
    text = "\n\n".join(heading.format(name=name) + "\nObserved detail." for name in
                       ("SELECTED_PATH", "BOUNDED_CHANGE", "VERIFICATION", "NEXT_ACTION"))
    update_decision_gate(contract, text)
    assert contract["decision_gate"] == "pass"


def test_decision_heading_names_embedded_in_prose_do_not_fill_slots():
    contract = {"decision_slots": [{"label": "selected_path", "required": True}]}
    update_decision_gate(contract, "This sentence mentions SELECTED_PATH: but is not a decision section.")
    assert contract["decision_gate"] == "blocked"


def test_execution_contract_promotes_to_identity_bound_ledger() -> None:
    plan, graph = _plan_and_graph("오픈크랩 팩의 근거를 비교해서 전략을 추천해줘")
    contract = compile_ontology_execution_contract(
        plan,
        graph,
        {
            "status": "ok",
            "claim_gate": "pass",
            "graph_gate": "not_required",
            "evidence": [
                {
                    "id": "ev-12345678",
                    "source": "opencrab://pack/chunk-1",
                    "text": "관측된 근거",
                }
            ],
        },
        mission_id="mission-ledger",
        revision=7,
    )

    ledger = promote_execution_contract_to_ledger(
        contract,
        mission_id="mission-ledger",
        source_artifact_id="artifact-contract",
        observed_receipt={"status": "ok", "claim_gate": "pass", "evidence": []},
    )

    assert ledger["artifact_kind"] == "ontology_ledger"
    assert ledger["mission_id"] == "mission-ledger"
    assert ledger["goal_graph_id"] == graph["graph_id"]
    assert ledger["revision"] == 7
    assert ledger["source"]["artifact_kind"] == "ontology_execution_contract"
    assert ledger["execution_contract"]["revision"] == 7
    assert validate_ontology_ledger(ledger, mission_id="mission-ledger", revision=7)
