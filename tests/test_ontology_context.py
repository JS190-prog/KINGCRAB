from __future__ import annotations

from typing import Any, Dict, List
import json

from crabagent.opencrab import OpenCrabUnavailable
from crabagent.ontology_context import OntologyContextCollector, compact_context


def test_context_receipt_preserves_structured_transient_tool_failure() -> None:
    def call_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        raise OpenCrabUnavailable(
            "MCP query timed out",
            error_code="OPENCRAB_MCP_TIMEOUT",
            stage="tool:opencrab_query",
            request_id="req-context-1",
            retryable=True,
        )

    receipt = OntologyContextCollector(call_tool).collect("transient context")

    assert receipt["status"] == "error"
    assert receipt["error_code"] == "OPENCRAB_MCP_TIMEOUT"
    assert receipt["error_stage"] == "tool:opencrab_query"
    assert receipt["request_id"] == "req-context-1"
    assert receipt["retryable"] is True
    failed = receipt["tool_calls"][0]
    assert failed["error_code"] == "OPENCRAB_MCP_TIMEOUT"
    assert failed["stage"] == "tool:opencrab_query"
    assert failed["request_id"] == "req-context-1"


def test_graph_context_uses_real_mcp_tools_only_when_goal_requires_graph() -> None:
    calls: List[str] = []

    def call_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        calls.append(name)
        if name == "opencrab_query":
            return {
                "status": "ok",
                "evidence": [{"id": "ev-1", "text": "A grounded claim.", "score": 0.9}],
                "retrieval": {"scanned": 1},
                "pack_scope": {"packages": 1},
            }
        if name == "opencrab_search_nodes":
            return {"status": "ok", "nodes": [{"id": "node-1", "label": "Claim", "type": "claim"}]}
        if name == "opencrab_get_node_context":
            return {
                "status": "ok",
                "nodes": [{"id": "node-2", "label": "Outcome", "type": "outcome"}],
                "edges": [{"id": "edge-1", "source": "node-1", "target": "node-2", "relation": "supports", "evidence_refs": ["ev-1"]}],
            }
        raise AssertionError(name)

    receipt = OntologyContextCollector(call_tool).collect(
        "오픈크랩 그래프의 관계를 확인해줘",
        package_ids=["pack-1"],
        graph_required=True,
    )

    assert calls == ["opencrab_query", "opencrab_search_nodes", "opencrab_list_edges", "opencrab_get_node_context"]
    assert receipt["authority"] == "direct_mcp_response"
    assert receipt["claim_gate"] == "pass"
    assert receipt["graph_gate"] == "pass"
    assert receipt["evidence_count"] == 1
    assert receipt["node_count"] == 2
    assert receipt["edge_count"] == 1
    assert len(receipt["tool_calls"]) == 4
    assert receipt["quality"]["path_count"] == 1
    assert receipt["quality"]["graph_semantic_gate"] == "pass"
    assert '"authority":"direct_mcp_response"' in compact_context({**receipt, "artifact_id": "artifact-1"})


def test_duplicate_evidence_ids_are_collapsed_before_persistence() -> None:
    def call_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        assert name == "opencrab_query"
        return {
            "status": "ok",
            "evidence": [
                {"id": "same", "text": "first", "source": "doc://same"},
                {"id": "same", "text": "duplicate", "source": "doc://same"},
                {"id": "other", "text": "second", "source": "doc://other"},
            ],
        }

    receipt = OntologyContextCollector(call_tool).collect("deduplicate evidence")
    assert receipt["evidence_count"] == 2
    assert [row["id"] for row in receipt["evidence"]] == ["same", "other"]


def test_typed_catalog_rows_are_observations_not_claim_evidence() -> None:
    def call_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        assert name == "opencrab_query"
        return {
            "packs": [
                {
                    "package_id": "pack-brand",
                    "project_id": "project-alexai",
                    "title": "Brand Strategy Pack",
                    "type": "pack",
                    "source": "opencrab://catalog/pack-brand",
                },
            ],
            "projects": [
                {
                    "project_id": "project-alexai",
                    "title": "ALEXAI Workspace",
                    "type": "project",
                    "source": "opencrab://catalog/project-alexai",
                }
            ],
        }

    receipt = OntologyContextCollector(call_tool).collect("팩과 프로젝트 목록")

    assert receipt["evidence_count"] == 0
    assert receipt["status"] == "ok"
    assert receipt["claim_gate"] == "blocked"
    assert receipt["observation_gate"] == "pass"
    assert receipt["observed_item_count"] == 2
    assert {row["title"] for row in receipt["observed_items"]} == {
        "Brand Strategy Pack",
        "ALEXAI Workspace",
    }
    compact = compact_context(receipt, max_chars=4200)
    assert "Brand Strategy Pack" in compact


def test_duplicate_marketplace_chunk_ids_are_collapsed_but_preserved_for_audit() -> None:
    def call_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        assert name == "opencrab_query"
        return {
            "status": "ok",
            "evidence": [
                {"id": "copy-a", "text": "same source chunk", "source": "doc://pack/update", "document_id": "doc-1", "metadata": {"char_start": 10, "char_end": 28}},
                {"id": "copy-b", "text": "same source chunk", "source": "doc://pack/update", "document_id": "doc-1", "metadata": {"char_start": 10, "char_end": 28}},
                {"id": "other-source", "text": "same source chunk", "source": "doc://other", "document_id": "doc-2"},
            ],
        }

    receipt = OntologyContextCollector(call_tool).collect("same chunk")

    assert receipt["evidence_count"] == 2
    assert receipt["evidence"][0]["id"] == "copy-a"
    assert receipt["evidence"][0]["duplicate_ids"] == ["copy-b"]
    assert receipt["evidence"][1]["id"] == "other-source"


def test_graph_context_falls_back_to_bounded_list_endpoints() -> None:
    calls: List[str] = []

    def call_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        calls.append(name)
        if name == "opencrab_query":
            return {"status": "ok", "evidence": [{"id": "ev-1", "text": "Grounded", "source": "doc://1"}]}
        if name == "opencrab_search_nodes":
            return {"status": "error", "error": "search unavailable"}
        if name == "opencrab_list_nodes":
            return {"status": "ok", "nodes": [{"id": "node-1", "label": "Claim", "type": "claim"}]}
        if name == "opencrab_get_node_context":
            return {"status": "ok", "nodes": [{"id": "node-2", "label": "Outcome", "type": "outcome"}], "edges": [{"source": "node-1", "target": "node-2", "relation": "supports", "evidence_refs": ["ev-1"]}]}
        raise AssertionError(name)

    receipt = OntologyContextCollector(call_tool).collect("graph relation", graph_required=True)
    assert calls == ["opencrab_query", "opencrab_search_nodes", "opencrab_list_nodes", "opencrab_list_edges", "opencrab_get_node_context"]
    assert receipt["graph_gate"] == "pass"
    assert receipt["claim_gate"] == "pass"


def test_graph_context_does_not_turn_no_evidence_into_a_claim() -> None:
    def call_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        if name == "opencrab_query":
            return {"status": "no_evidence", "evidence": []}
        if name == "opencrab_search_nodes":
            return {"status": "ok", "nodes": [{"id": "node-1", "label": "Unproven", "type": "claim"}]}
        if name == "opencrab_get_node_context":
            return {"status": "ok", "nodes": [], "edges": []}
        if name == "opencrab_list_edges":
            return {"status": "ok", "edges": []}
        raise AssertionError(name)

    receipt = OntologyContextCollector(call_tool).collect("graph relation", graph_required=True)
    assert receipt["status"] == "no_evidence"
    assert receipt["node_count"] == 1
    assert receipt["grounding"] == "graph"
    assert receipt["claim_gate"] == "blocked"


def test_graph_context_normalizes_production_edge_shape_and_derives_unresolved_endpoints() -> None:
    def call_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        if name == "opencrab_query":
            return {"evidence": [{"id": "ev-1", "text": "Grounded", "source": "doc://1"}]}
        if name == "opencrab_search_nodes":
            return {"nodes": []}
        if name == "opencrab_list_nodes":
            return {"nodes": [], "next_cursor": None}
        if name == "opencrab_list_edges":
            return {
                "edges": [{
                    "id": "edge-1",
                    "from_id": "node-claim",
                    "to_id": "node-outcome",
                    "relation": "supports",
                }],
                "pack_scope": {"packages": 1},
            }
        if name == "opencrab_get_node_context":
            return {"nodes": [], "edges": []}
        raise AssertionError(name)

    receipt = OntologyContextCollector(call_tool).collect("graph relation", graph_required=True)
    assert receipt["graph_gate"] == "weak"
    assert receipt["edge_count"] == 1
    assert receipt["quality"]["path_count"] == 1
    assert {row["type"] for row in receipt["nodes"]} == {"unresolved_endpoint"}
    assert receipt["quality"]["graph_semantic_gate"] == "weak"
    assert receipt["tool_calls"][-1]["status"] == "observed"
    assert receipt["tool_calls"][-1]["tool"] == "opencrab_get_node_context"


def test_graph_context_widens_node_listing_to_resolve_edge_endpoints() -> None:
    calls: List[str] = []

    def call_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        calls.append(name)
        if name == "opencrab_query":
            return {"status": "ok", "evidence": [{"id": "ev-1", "text": "Grounded", "source": "doc://1"}]}
        if name == "opencrab_search_nodes":
            return {"status": "ok", "nodes": [{"id": "unrelated", "label": "Unrelated", "type": "topic"}]}
        if name == "opencrab_get_node_context":
            return {"status": "ok", "nodes": [], "edges": []}
        if name == "opencrab_list_edges":
            return {"status": "ok", "edges": [{"id": "edge-1", "source": "node-claim", "target": "node-outcome", "relation": "supports", "evidence_refs": ["ev-1"]}]}
        if name == "opencrab_list_nodes":
            assert arguments["limit"] >= 100
            return {
                "status": "ok",
                "nodes": [
                    {"id": "noise-%d" % index, "label": "Noise %d" % index, "type": "topic"}
                    for index in range(6)
                ]
                + [
                    {"id": "node-claim", "label": "Claim", "type": "claim"},
                    {"id": "node-outcome", "label": "Outcome", "type": "outcome"},
                ],
            }
        raise AssertionError(name)

    receipt = OntologyContextCollector(call_tool).collect("graph relation", graph_required=True)

    assert receipt["graph_gate"] == "pass"
    assert receipt["quality"]["resolved_path_count"] == 1
    assert calls == ["opencrab_query", "opencrab_search_nodes", "opencrab_list_edges", "opencrab_list_nodes"]


def test_graph_packet_preserves_preferred_evidence_edge_before_node_cap() -> None:
    def call_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        if name == "opencrab_query":
            return {"status": "ok", "evidence": [{"id": "ev-1", "text": "Grounded", "source": "doc://1"}]}
        if name == "opencrab_search_nodes":
            return {
                "status": "ok",
                "nodes": [
                    {"id": "noise-%d" % index, "label": "Noise %d" % index, "type": "topic"}
                    for index in range(6)
                ],
            }
        if name == "opencrab_list_edges":
            return {
                "status": "ok",
                "edges": [
                    {"id": "generic-%d" % index, "source": "noise-0", "target": "noise-1", "relation": "mentions"}
                    for index in range(23)
                ]
                + [{"id": "preferred", "source": "claim-1", "target": "outcome-1", "relation": "supports", "evidence_refs": ["ev-1"]}],
            }
        if name == "opencrab_list_nodes":
            return {
                "status": "ok",
                "nodes": [
                    {"id": "noise-%d" % index, "label": "Noise %d" % index, "type": "topic"}
                    for index in range(6)
                ]
                + [
                    {"id": "claim-1", "label": "Claim", "type": "claim"},
                    {"id": "outcome-1", "label": "Outcome", "type": "outcome"},
                ],
            }
        raise AssertionError(name)

    receipt = OntologyContextCollector(call_tool).collect("graph relation", graph_required=True)

    assert receipt["graph_gate"] == "pass"
    assert receipt["quality"]["preferred_relation_path_count"] == 1
    assert receipt["quality"]["evidence_backed_preferred_relation_path_count"] == 1
    assert receipt["paths"][0]["relation"] == "supports"


def test_graph_context_accepts_singleton_node_and_nested_edge_provenance() -> None:
    def call_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        if name == "opencrab_query":
            return {"status": "ok", "evidence": [{"id": "ev-1", "text": "Grounded", "source": "doc://1"}]}
        if name == "opencrab_search_nodes":
            return {"status": "ok", "nodes": []}
        if name == "opencrab_list_nodes":
            return {"status": "ok", "nodes": []}
        if name == "opencrab_list_edges":
            return {
                "status": "ok",
                "edges": [{
                    "id": "edge-1",
                    "from_id": "uuid-source",
                    "to_id": "uuid-target",
                    "relation": "supports",
                    "properties": {
                        "source": "evidence:chunk-1",
                        "target": "claim:claim-1",
                        "from_space": "evidence",
                        "to_space": "claim",
                        "evidence_refs": ["chunk-1"],
                        "confidence": 0.91,
                    },
                }],
            }
        if name == "opencrab_get_node_context":
            node_id = arguments["node_id"]
            if node_id == "uuid-source":
                return {
                    "status": "ok",
                    "node": {
                        "id": "uuid-source",
                        "label": "Source evidence",
                        "node_type": "Evidence",
                        "properties": {"external_id": "evidence:chunk-1", "space": "evidence"},
                    },
                    "edges": [],
                }
            return {
                "status": "ok",
                "node": {
                    "id": "uuid-target",
                    "label": "Supported claim",
                    "node_type": "Claim",
                    "properties": {"external_id": "claim:claim-1", "space": "claim"},
                },
                "edges": [],
            }
        raise AssertionError(name)

    receipt = OntologyContextCollector(call_tool).collect("graph relation", graph_required=True)

    assert receipt["graph_gate"] == "pass"
    assert receipt["quality"]["evidence_backed_preferred_relation_path_count"] == 1
    path = receipt["paths"][0]
    assert path["from"]["label"] == "Source evidence"
    assert path["to"]["label"] == "Supported claim"
    assert path["evidence_refs"] == ["chunk-1"]
    assert path["from_space"] == "evidence"
    assert path["to_space"] == "claim"
    assert path["confidence"] == 0.91


def test_compact_context_always_stays_valid_and_bounded() -> None:
    from crabagent.ontology_context import compact_context

    receipt = {
        "schema": "crab.opencrab-context-receipt/v2",
        "authority": "direct_mcp_response",
        "status": "ok",
        "claim_gate": "pass",
        "grounding": "evidence",
        "evidence_count": 8,
        "node_count": 12,
        "edge_count": 24,
        "evidence": [{"id": str(i), "text": "x" * 2000} for i in range(8)],
        "nodes": [{"id": str(i), "label": "node" * 500} for i in range(12)],
        "edges": [{"id": str(i), "metadata": {"payload": "x" * 2000}} for i in range(24)],
        "retrieval": {"large": "x" * 3000},
        "pack_scope": {"large": "x" * 3000},
    }
    compact = compact_context(receipt, max_chars=500)
    assert len(compact) <= 500
    compact_data = json.loads(compact)
    assert compact_data["authority"] == "direct_mcp_response"
    assert compact_data["evidence"][0]["id"] == "0"


def test_compact_context_deduplicates_model_evidence_but_keeps_receipt_intact() -> None:
    receipt = {
        "schema": "crab.opencrab-context-receipt/v2",
        "authority": "direct_mcp_response",
        "status": "ok",
        "query": "오픈크랩 워크플로우 요약",
        "evidence_count": 3,
        "evidence": [
            {"id": "duplicate-a", "text": "오픈크랩 워크플로우는 근거를 사용한다.", "score": 0.8},
            {"id": "duplicate-b", "text": "오픈크랩 워크플로우는 근거를 사용한다.", "score": 0.7},
            {"id": "other", "text": "광고 운영 숫자와 예산 기록.", "score": 0.9},
        ],
    }
    compact = json.loads(compact_context(receipt, max_chars=4000))
    assert [row["id"] for row in compact["evidence"]] == ["duplicate-a", "other"]
    assert [row["id"] for row in receipt["evidence"]] == ["duplicate-a", "duplicate-b", "other"]


def test_compact_context_preserves_a_relevant_sequence_at_queen_budget() -> None:
    source = (
        "복잡한 작업에서는 작업 유형을 분류하고, 목표/제약/리스크를 확인하고, WorkPattern을 선택한 뒤 "
        "plan-act-observe-revise로 실행하고, 중요한 주장은 근거에 연결하며, 최종 전에 검증 루프를 거쳐 "
        "바로 쓸 수 있는 산출물을 만들어라."
    )
    receipt = {
        "schema": "crab.opencrab-context-receipt/v2",
        "authority": "direct_mcp_response",
        "status": "ok",
        "query": "오픈크랩 팩의 근거를 바탕으로 목표중심 워크플로우를 요약해줘",
        "claim_gate": "pass",
        "grounding": "evidence",
        "evidence_count": 8,
        "evidence": [{"id": "relevant", "text": source, "score": 0.9}]
        + [{"id": "other-%d" % i, "text": "무관한 운영 기록 " * 60, "score": 0.1} for i in range(7)],
        "retrieval": {"large": "x" * 3000},
        "pack_scope": {"large": "x" * 3000},
    }
    compact = compact_context(receipt, max_chars=2600)
    assert "plan-act-observe-revise" in compact


def test_successful_context_is_reused_from_a_short_lived_local_cache(tmp_path) -> None:
    calls: List[str] = []

    def call_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        calls.append(name)
        return {
            "status": "ok",
            "evidence": [{"id": "ev-1", "text": "Grounded", "source": "doc://1"}],
        }

    cache = tmp_path / "ontology-context-cache.json"
    first = OntologyContextCollector(call_tool, cache_path=cache).collect("cached ontology")
    second = OntologyContextCollector(call_tool, cache_path=cache).collect("cached ontology")
    assert first["cache_hit"] is False
    assert second["cache_hit"] is True
    assert second["tool_calls"][0]["status"] == "cached"
    assert calls == ["opencrab_query"]


def test_context_cache_keeps_multiple_goal_scope_entries(tmp_path) -> None:
    calls: List[str] = []

    def call_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        assert name == "opencrab_query"
        calls.append(str(arguments.get("query")))
        return {
            "status": "ok",
            "evidence": [{
                "id": "ev-%s" % len(calls),
                "text": "Grounded %s" % arguments.get("query"),
                "source": "doc://cache",
            }],
        }

    cache = tmp_path / "ontology-context-cache.json"
    collector = OntologyContextCollector(call_tool, cache_path=cache)
    first = collector.collect("first goal", package_ids=["pack-a"])
    second = collector.collect("second goal", package_ids=["pack-b"])
    first_again = collector.collect("first goal", package_ids=["pack-a"])

    assert first["cache_hit"] is False
    assert second["cache_hit"] is False
    assert first_again["cache_hit"] is True
    assert calls == ["first goal", "second goal"]
    envelope = json.loads(cache.read_text(encoding="utf-8"))
    assert envelope["schema"] == "crab.ontology-context-cache/v2"
    assert len(envelope["entries"]) == 2


def test_selected_scope_is_queried_completely_when_within_batch_limit() -> None:
    observed: List[Dict[str, Any]] = []

    def call_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        assert name == "opencrab_query"
        observed.append(arguments)
        return {"status": "no_evidence", "evidence": []}

    package_ids = ["pack-%02d" % index for index in range(20)]
    receipt = OntologyContextCollector(call_tool).collect("bounded retrieval", package_ids=package_ids)

    assert observed[0]["package_ids"] == package_ids
    assert receipt["pack_scope"]["requested_package_count"] == 20
    assert receipt["pack_scope"]["queried_package_count"] == 20
    assert receipt["pack_scope"]["omitted_package_count"] == 0
    assert receipt["pack_scope"]["scope_mode"] == "selected_complete"
    assert receipt["pack_scope"]["selection_gate"] == "pass"


def test_selected_scope_is_fanned_out_in_saas_bounded_batches() -> None:
    observed: List[Dict[str, Any]] = []

    def call_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        assert name == "opencrab_query"
        observed.append(dict(arguments))
        batch = list(arguments.get("package_ids") or [])
        return {
            "status": "ok",
            "evidence": [
                {
                    "id": "ev-%s" % batch[0],
                    "text": "Evidence for %s" % batch[0],
                    "source": "doc://%s" % batch[0],
                    "package_id": batch[0],
                }
            ],
        }

    package_ids = ["pack-%03d" % index for index in range(52)]
    receipt = OntologyContextCollector(call_tool).collect("complete selected retrieval", package_ids=package_ids)

    assert len(observed) == 3
    assert all(1 <= len(arguments["package_ids"]) <= 25 for arguments in observed)
    assert [item for arguments in observed for item in arguments["package_ids"]] == package_ids
    scope = receipt["pack_scope"]
    assert scope["requested_package_count"] == 52
    assert scope["queried_package_count"] == 52
    assert scope["omitted_package_count"] == 0
    assert scope["batch_count"] == 3
    assert scope["successful_batch_count"] == 3
    assert scope["failed_batch_count"] == 0
    assert scope["scope_mode"] == "selected_complete"
    assert scope["selection_gate"] == "pass"
    assert receipt["claim_gate"] == "pass"


def test_partial_selected_batch_blocks_claims_instead_of_reporting_success() -> None:
    observed: List[Dict[str, Any]] = []

    def call_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        assert name == "opencrab_query"
        observed.append(dict(arguments))
        if len(observed) == 2:
            raise TimeoutError("simulated MCP batch timeout")
        return {
            "status": "ok",
            "evidence": [{"id": "ev-1", "text": "Grounded first batch", "source": "doc://1"}],
        }

    package_ids = ["pack-%03d" % index for index in range(26)]
    receipt = OntologyContextCollector(call_tool).collect("partial selected retrieval", package_ids=package_ids)

    assert receipt["status"] == "partial"
    assert receipt["claim_gate"] == "blocked"
    assert receipt["selection_gate"] == "blocked"
    assert receipt["pack_scope"]["successful_batch_count"] == 1
    assert receipt["pack_scope"]["failed_batch_count"] == 1
    assert receipt["pack_scope"]["omitted_package_count"] == 0


def test_workspace_auto_resolved_scope_is_recorded_and_used_for_query() -> None:
    observed = []

    def call_tool(name, arguments):
        observed.append((name, dict(arguments)))
        if name == "opencrab_query":
            return {
                "status": "ok",
                "evidence": [{"id": "ev-1", "text": "strategy evidence", "source": "doc://1"}],
            }
        return {"status": "ok", "nodes": [], "edges": []}

    receipt = OntologyContextCollector(call_tool).collect(
        "사업 전략",
        scope_resolution={
            "scope_mode": "workspace_auto_resolved",
            "catalog_query": "사업 전략",
            "selected_package_ids": ["pack-strategy"],
            "selected_titles": ["사업 전략"],
        },
    )

    query = next(arguments for name, arguments in observed if name == "opencrab_query")
    assert query["package_ids"] == ["pack-strategy"]
    assert "pack_query" not in query
    assert receipt["pack_scope"]["scope_mode"] == "workspace_auto_resolved"
    assert receipt["pack_scope"]["scope_resolution"]["selected_titles"] == ["사업 전략"]


def test_gateway_preloaded_graph_avoids_runtime_graph_endpoint() -> None:
    calls: List[str] = []

    def call_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        calls.append(name)
        assert name == "opencrab_query"
        return {
            "status": "ok",
            "authority": "gateway_verified_mcp_response",
            "evidence": [{"id": "ev-1", "text": "Grounded graph evidence", "source": "Evidence Document"}],
            "gateway_graph": {
                "status": "ok",
                "authority": "gateway_verified_mcp_response",
                "nodes": [
                    {"id": "doc-1", "label": "Evidence Document", "node_type": "document"},
                    {"id": "concept-1", "label": "Verified concept", "node_type": "concept"},
                ],
                "edges": [{"id": "edge-1", "from_id": "doc-1", "to_id": "concept-1", "relation": "mentions", "evidence_refs": ["ev-1"]}],
                "tool_calls": [
                    {"tool": "opencrab_search_nodes", "arguments": {"package_ids": ["pack-1"]}, "response_status": "ok"},
                    {"tool": "opencrab_get_node_context", "arguments": {"node_id": "concept-1"}, "response_status": "ok"},
                ],
            },
        }

    receipt = OntologyContextCollector(call_tool).collect(
        "show the mentions graph path",
        package_ids=["pack-1"],
        graph_required=True,
        retrieval_contract={
            "primary_query": "show the mentions graph path",
            "evidence_top_k": 8,
            "node_limit": 6,
            "node_scan_limit": 1000,
            "context_node_limit": 1,
            "edge_limit": 24,
            "relation_bias": ["mentions"],
            "scope_mode": "selected",
            "package_limit": 8,
        },
    )

    assert calls == ["opencrab_query"]
    assert receipt["authority"] == "gateway_verified_mcp_response"
    assert receipt["graph_gate"] == "pass"
    assert receipt["quality"]["graph_semantic_gate"] == "pass"
    assert receipt["paths"][0]["relation"] == "mentions"
    assert receipt["paths"][0]["evidence_refs"] == ["ev-1"]
    assert [row["tool"] for row in receipt["tool_calls"][1:]] == ["opencrab_search_nodes", "opencrab_get_node_context"]
