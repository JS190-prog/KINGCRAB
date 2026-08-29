from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .opencrab import opencrab_error_details
from .ontology_route import compile_ontology_route


McpCall = Callable[[str, Dict[str, Any]], Dict[str, Any]]

_CACHE_WRITE_LOCK = threading.RLock()
_CACHE_MAX_ENTRIES = 64
# OpenCrab SaaS applies a hard 25-pack retrieval scope per MCP call. The
# collector fans out explicit selections in these bounded batches instead of
# silently dropping the tail of a user's selection.
_MCP_PACK_BATCH_LIMIT = 25
_MAX_EXPLICIT_PACKAGES = 500
_MAX_PERSISTED_EVIDENCE_ROWS = 32
_CACHE_REVISION = "evidence-ledger-v3"


def _list_value(payload: Any, *keys: str) -> List[Dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    empty: List[Dict[str, Any]] = []
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            rows = [row for row in value if isinstance(row, dict)]
            if rows:
                return rows
            empty = rows
    return empty


def _node_rows(payload: Any) -> List[Dict[str, Any]]:
    """Accept both collection and production singleton node responses."""
    rows: List[Dict[str, Any]] = []
    if isinstance(payload, dict) and isinstance(payload.get("node"), dict):
        rows.append(payload["node"])
    rows.extend(_list_value(payload, "nodes", "neighbors", "connected_nodes", "items", "results"))
    return rows


def _nested_properties(item: Dict[str, Any]) -> tuple[Dict[str, Any], Dict[str, Any]]:
    properties = item.get("properties") if isinstance(item.get("properties"), dict) else {}
    nested = properties.get("properties") if isinstance(properties.get("properties"), dict) else {}
    return properties, nested


def _first_value(item: Dict[str, Any], properties: Dict[str, Any], nested: Dict[str, Any], *keys: str) -> Any:
    for source in (item, properties, nested):
        for key in keys:
            value = source.get(key)
            if value not in (None, ""):
                return value
    return None


def _text(value: Any, limit: int = 1200) -> str:
    return " ".join(str(value or "").split())[:limit]


def _model_evidence(receipt: Dict[str, Any], limit: int = 4) -> List[Dict[str, Any]]:
    """Project a small, relevant evidence set for model context.

    The persisted receipt keeps every observed result for auditability. The
    model-facing projection should not spend context on duplicate chunks or
    unrelated pack updates that happened to rank nearby in a broad query.
    """
    rows = [row for row in (receipt.get("evidence") or []) if isinstance(row, dict)]
    query_terms = {
        token
        for token in re.findall(r"[0-9A-Za-z가-힣]{2,}", str(receipt.get("query") or "").lower())
    }
    positions = {id(row): index for index, row in enumerate(rows)}

    def rank(row: Dict[str, Any]) -> tuple[float, int]:
        text = str(row.get("text") or "").lower()
        terms = set(re.findall(r"[0-9A-Za-z가-힣]{2,}", text))
        overlap = len(query_terms & terms)
        raw_score = row.get("score")
        try:
            score = float(raw_score or 0)
        except (TypeError, ValueError):
            score = 0.0
        return (
            float(overlap) + min(max(score, 0.0), 1.0) / 1000.0,
            -positions.get(id(row), 0),
        )

    ranked = sorted(rows, key=rank, reverse=True)
    target = max(1, int(limit))
    selected: List[Dict[str, Any]] = []
    seen_text: set[str] = set()

    def append_unique(row: Dict[str, Any]) -> bool:
        lane_id = str(row.get("lane_id") or "").strip().lower()
        normalized_text = " ".join(str(row.get("text") or "").split()).lower()
        identity = "%s|%s" % (lane_id, normalized_text) if lane_id else normalized_text
        if identity and identity in seen_text:
            return False
        if identity:
            seen_text.add(identity)
        selected.append(copy.deepcopy(row))
        return True

    lane_order: List[str] = []
    lane_buckets: Dict[str, List[Dict[str, Any]]] = {}
    for row in ranked:
        lane_id = str(row.get("lane_id") or "").strip()
        if not lane_id:
            continue
        if lane_id not in lane_buckets:
            lane_order.append(lane_id)
            lane_buckets[lane_id] = []
        lane_buckets[lane_id].append(row)

    # When a Gateway request contains independent evidence lanes, keep the
    # smallest model projection balanced across those lanes. This prevents a
    # policy lane at the head of the receipt from crowding out the work lane.
    if len(lane_order) > 1:
        while len(selected) < target:
            progressed = False
            for lane_id in lane_order:
                bucket = lane_buckets[lane_id]
                while bucket:
                    if append_unique(bucket.pop(0)):
                        progressed = True
                        break
                if len(selected) >= target:
                    break
            if not progressed:
                break

    for row in ranked:
        if len(selected) >= target:
            break
        append_unique(row)
    return selected


def _normalize_evidence(
    rows: List[Dict[str, Any]],
    limit: int = _MAX_PERSISTED_EVIDENCE_ROWS,
    query: str = "",
) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    seen: set[str] = set()
    seen_content: Dict[str, int] = {}
    for item in rows:
        evidence_id = _text(item.get("id") or item.get("evidence_id"), 240)
        source = _text(item.get("source") or item.get("source_uri") or item.get("source_url"), 500)
        text = _text(item.get("text") or item.get("content"))
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        lane_id = _text(item.get("lane_id") or metadata.get("lane_id"), 120)
        # Marketplace imports can expose the same source chunk under multiple
        # evidence IDs. Keep one model-facing row, but retain every observed
        # ID for auditability. Different sources are never merged solely
        # because their prose happens to be similar.
        location = "|".join(
            _text(item.get(key) or metadata.get(key), 120)
            for key in ("document_id", "char_start", "char_end", "source_url")
        ).strip("|")
        content_key = "%s|%s|%s|%s" % (
            lane_id.lower(),
            source.lower(),
            location.lower(),
            text.lower(),
        )
        fallback_key = "%s|%s" % (source, text)
        identity = evidence_id or fallback_key
        if content_key in seen_content:
            index = seen_content[content_key]
            duplicate_ids = result[index].setdefault("duplicate_ids", [])
            if evidence_id and evidence_id not in duplicate_ids and evidence_id != result[index].get("id"):
                duplicate_ids.append(evidence_id)
            continue
        if identity in seen:
            continue
        seen.add(identity)
        seen_content[content_key] = len(result)
        normalized: Dict[str, Any] = {
            "id": evidence_id,
            "document_id": _text(item.get("document_id") or metadata.get("document_id"), 240),
            "project_id": _text(item.get("project_id") or metadata.get("project_id"), 240),
            "workspace_id": _text(item.get("workspace_id") or metadata.get("workspace_id"), 240),
            "package_id": _text(item.get("package_id") or metadata.get("package_id") or metadata.get("pack_id"), 240),
            "text": text,
            "score": item.get("score"),
            "source": source or ("opencrab://evidence/%s" % evidence_id if evidence_id else ""),
            "source_url": _text(item.get("source_url") or metadata.get("source_url"), 1000),
            "created_at": item.get("created_at"),
            "lane_id": lane_id,
            "lane_purpose": _text(item.get("lane_purpose") or metadata.get("lane_purpose"), 120),
            "profile_id": _text(item.get("profile_id") or metadata.get("profile_id"), 120),
            "configured_workspace_id": _text(
                item.get("configured_workspace_id") or metadata.get("configured_workspace_id"),
                240,
            ),
            "claim_scope": _text(item.get("claim_scope") or metadata.get("claim_scope"), 240),
            "metadata": metadata,
            "retrieval": item.get("retrieval") if isinstance(item.get("retrieval"), dict) else {},
        }
        raw_lane_workspace_ids = item.get("lane_workspace_ids") or metadata.get("lane_workspace_ids") or []
        if not isinstance(raw_lane_workspace_ids, list):
            raw_lane_workspace_ids = []
        lane_workspace_ids = [
            _text(value, 240)
            for value in raw_lane_workspace_ids
            if _text(value, 240)
        ][:8]
        if lane_workspace_ids:
            normalized["lane_workspace_ids"] = list(dict.fromkeys(lane_workspace_ids))
        provenance = item.get("provenance")
        if isinstance(provenance, dict):
            normalized["provenance"] = copy.deepcopy(provenance)
        elif isinstance(provenance, str) and provenance.strip():
            normalized["provenance"] = _text(provenance, 500)
        for key in ("char_start", "char_end", "source_chunk_index", "full_chunk_count", "evidence_index"):
            value = item.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                normalized[key] = value
        result.append(normalized)
    query_terms = {
        token
        for token in re.findall(r"[0-9A-Za-z가-힣]{2,}", str(query or "").lower())
    }

    def rank(row: Dict[str, Any]) -> tuple[float, int]:
        terms = set(re.findall(r"[0-9A-Za-z가-힣]{2,}", str(row.get("text") or "").lower()))
        overlap = len(query_terms & terms)
        try:
            score = float(row.get("score") or 0)
        except (TypeError, ValueError):
            score = 0.0
        return (float(overlap) + min(max(score, 0.0), 1.0) / 1000.0, -result.index(row))

    return sorted(result, key=rank, reverse=True)[: max(1, int(limit))]


def _normalize_observed_items(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Preserve typed MCP rows instead of flattening catalog data into prose."""
    if not isinstance(payload, dict):
        return []
    rows: List[Dict[str, Any]] = []
    for key in ("items", "packs", "projects", "packages", "results"):
        value = payload.get(key)
        if isinstance(value, dict):
            value = value.get("items") or value.get("results")
        if not isinstance(value, list):
            continue
        for item in value:
            if not isinstance(item, dict):
                continue
            item_id = _text(
                item.get("package_id")
                or item.get("pack_id")
                or item.get("project_id")
                or item.get("id")
                or item.get("node_id"),
                240,
            )
            title = _text(
                item.get("title")
                or item.get("name")
                or item.get("label")
                or item.get("project_name"),
                500,
            )
            if not item_id and not title:
                continue
            source = _text(item.get("source") or item.get("source_uri") or item.get("url"), 500)
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            rows.append(
                {
                    "id": item_id,
                    "title": title,
                    "type": _text(item.get("type") or item.get("kind") or key.rstrip("s"), 120),
                    "source": source,
                    "status": _text(item.get("status"), 80),
                    "package_id": _text(item.get("package_id") or item.get("pack_id"), 240),
                    "project_id": _text(item.get("project_id"), 240),
                    "evidence_refs": item.get("evidence_refs") if isinstance(item.get("evidence_refs"), list) else [],
                    "metadata": metadata,
                }
            )
    unique: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        key = "%s|%s|%s" % (row.get("type"), row.get("id"), row.get("title"))
        unique.setdefault(key, row)
    return list(unique.values())[:64]


def _dedupe_observed_items(rows: List[Dict[str, Any]], limit: int = 64) -> List[Dict[str, Any]]:
    unique: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = "%s|%s|%s" % (row.get("type"), row.get("id"), row.get("title"))
        if key.strip("|"):
            unique.setdefault(key, row)
    return list(unique.values())[: max(0, int(limit))]


def _payload_status(payload: Any) -> str:
    """Infer success for MCP responses that omit a redundant status field."""
    if not isinstance(payload, dict):
        return "unknown"
    explicit = str(payload.get("status") or "").strip().lower()
    if explicit:
        return explicit
    if payload.get("error"):
        return "error"
    if any(
        key in payload
        for key in (
            "evidence",
            "chunks",
            "items",
            "packs",
            "projects",
            "packages",
            "results",
            "nodes",
            "edges",
            "relations",
            "pack_scope",
            "next_cursor",
        )
    ):
        return "ok"
    return "unknown"


def _merge_query_payloads(payloads: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Merge independent scoped MCP responses without merging their claims."""
    if not payloads:
        return {"status": "error", "error": "OpenCrab returned no batch payload"}
    merged: Dict[str, Any] = dict(payloads[0])
    list_keys = ("evidence", "chunks", "items", "packs", "projects", "packages", "results", "nodes", "edges", "relations")
    for key in list_keys:
        rows: List[Any] = []
        for payload in payloads:
            value = payload.get(key)
            if isinstance(value, list):
                rows.extend(value)
        if rows:
            merged[key] = rows
    retrievals = [payload.get("retrieval") for payload in payloads if isinstance(payload.get("retrieval"), dict)]
    if retrievals:
        merged["retrieval"] = {
            "batch_count": len(retrievals),
            "scanned": sum(int(row.get("scanned") or 0) for row in retrievals),
            "candidates": sum(int(row.get("candidates") or 0) for row in retrievals),
            "partial": any(bool(row.get("partial")) for row in retrievals),
        }
    statuses = [_payload_status(payload) for payload in payloads]
    successful = [status for status in statuses if status in {"ok", "no_evidence", "cached"}]
    if successful and len(successful) < len(payloads):
        merged["status"] = "partial"
    else:
        merged["status"] = "ok" if any(status == "ok" for status in successful) else "no_evidence" if successful else "error"
    merged["batch_statuses"] = statuses
    merged["batch_count"] = len(payloads)
    merged["successful_batch_count"] = len(successful)
    merged["failed_batch_count"] = len(payloads) - len(successful)
    failures = [payload for payload in payloads if _payload_status(payload) not in {"ok", "no_evidence", "cached"}]
    if failures:
        details = opencrab_error_details(failures[0], default_stage="opencrab_query")
        merged.setdefault("error", details["message"])
        merged.setdefault("error_code", details["error_code"])
        merged.setdefault("error_stage", details["stage"])
        merged.setdefault("request_id", details["request_id"])
        merged["retryable"] = any(
            bool(opencrab_error_details(payload, default_stage="opencrab_query").get("retryable"))
            for payload in failures
        )
    return merged


def _normalize_nodes(rows: List[Dict[str, Any]], limit: int = 6) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    for item in rows[: max(0, int(limit))]:
        properties, nested = _nested_properties(item)
        node_id = _text(_first_value(item, properties, nested, "id", "node_id"), 240)
        if not node_id:
            continue
        evidence_refs = _first_value(item, properties, nested, "evidence_refs")
        metadata = dict(item.get("metadata")) if isinstance(item.get("metadata"), dict) else {}
        for key in ("external_id", "space", "source_type", "source_path", "document_id"):
            value = _first_value(item, properties, nested, key)
            if value not in (None, ""):
                metadata.setdefault(key, value)
        result.append(
            {
                "id": node_id,
                "label": _text(_first_value(item, properties, nested, "label", "name", "title"), 500),
                "type": _text(_first_value(item, properties, nested, "type", "node_type"), 120),
                "package_id": _text(_first_value(item, properties, nested, "package_id", "pack_id"), 240),
                "evidence_refs": evidence_refs if isinstance(evidence_refs, list) else [],
                "metadata": metadata,
            }
        )
    return result


def _normalize_edges(rows: List[Dict[str, Any]], limit: int = 24) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    for item in rows[:limit]:
        properties, nested = _nested_properties(item)
        source = _text(item.get("source") or item.get("from") or item.get("source_id") or item.get("from_id"), 240)
        target = _text(item.get("target") or item.get("to") or item.get("target_id") or item.get("to_id"), 240)
        evidence_refs = _first_value(item, properties, nested, "evidence_refs")
        metadata = dict(item.get("metadata")) if isinstance(item.get("metadata"), dict) else {}
        for key in (
            "source_external_id", "target_external_id", "from_space", "to_space",
            "confidence", "cloud_pack_title", "cloud_pack_source_path",
        ):
            value = _first_value(item, properties, nested, key)
            if value not in (None, ""):
                metadata.setdefault(key, value)
        result.append(
            {
                "id": _text(item.get("id") or item.get("edge_id"), 240),
                "source": source,
                "target": target,
                "source_external_id": _text(_first_value(item, properties, nested, "source_external_id", "source"), 240),
                "target_external_id": _text(_first_value(item, properties, nested, "target_external_id", "target"), 240),
                "from_space": _text(_first_value(item, properties, nested, "from_space"), 120),
                "to_space": _text(_first_value(item, properties, nested, "to_space"), 120),
                "confidence": _first_value(item, properties, nested, "confidence"),
                "relation": _text(_first_value(item, properties, nested, "relation", "type", "edge_type", "relation_type"), 160),
                "evidence_refs": evidence_refs if isinstance(evidence_refs, list) else [],
                "metadata": metadata,
            }
        )
    return result


def _bounded_paths(nodes: List[Dict[str, Any]], edges: List[Dict[str, Any]], limit: int = 12) -> List[Dict[str, Any]]:
    labels = {str(row.get("id")): row.get("label") or row.get("id") for row in nodes if row.get("id")}
    aliases = {
        str(row.get("metadata", {}).get("external_id")): str(row.get("id"))
        for row in nodes
        if row.get("id") and isinstance(row.get("metadata"), dict) and row.get("metadata", {}).get("external_id")
    }
    paths: List[Dict[str, Any]] = []
    for edge in edges:
        source = aliases.get(str(edge.get("source") or ""), str(edge.get("source") or ""))
        target = aliases.get(str(edge.get("target") or ""), str(edge.get("target") or ""))
        relation = str(edge.get("relation") or "")
        if not source or not target or not relation or source not in labels or target not in labels:
            continue
        paths.append(
            {
                "from": {"id": source, "label": labels[source]},
                "relation": relation,
                "to": {"id": target, "label": labels[target]},
                "evidence_refs": edge.get("evidence_refs") or [],
                "source_external_id": edge.get("source_external_id") or "",
                "target_external_id": edge.get("target_external_id") or "",
                "from_space": edge.get("from_space") or "",
                "to_space": edge.get("to_space") or "",
                "confidence": edge.get("confidence"),
            }
        )
        if len(paths) >= limit:
            break
    return paths


def _edge_priority(edge: Dict[str, Any], relation_bias: Optional[List[str]], index: int) -> tuple[int, int, int, int, float, int]:
    """Rank graph edges for a small semantic packet without inventing edges."""
    preferred = {str(value).strip().lower() for value in (relation_bias or []) if str(value).strip()}
    relation = str(edge.get("relation") or "").strip().lower()
    evidence_backed = bool(edge.get("evidence_refs"))
    try:
        confidence = float(edge.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    return (
        1 if evidence_backed and relation in preferred else 0,
        1 if evidence_backed else 0,
        1 if relation in preferred else 0,
        1 if relation else 0,
        confidence,
        -index,
    )


def _bounded_graph_packet(
    nodes: Dict[str, Dict[str, Any]],
    edges: Dict[str, Dict[str, Any]],
    *,
    node_limit: int = 12,
    edge_limit: int = 24,
    relation_bias: Optional[List[str]] = None,
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Keep the graph packet bounded while preserving meaningful paths first."""
    ranked = sorted(
        enumerate(edges.values()),
        key=lambda pair: _edge_priority(pair[1], relation_bias, pair[0]),
        reverse=True,
    )
    bounded_edges = [edge for _, edge in ranked[: max(0, int(edge_limit))]]
    selected_ids: set[str] = set()
    selected_order: List[str] = []
    for edge in bounded_edges:
        for endpoint in (edge.get("source"), edge.get("target")):
            endpoint = str(endpoint or "").strip()
            if endpoint and endpoint in nodes and endpoint not in selected_ids:
                selected_ids.add(endpoint)
                selected_order.append(endpoint)
    ordered_nodes: List[Dict[str, Any]] = [nodes[node_id] for node_id in selected_order]
    ordered_nodes.extend(node for node_id, node in nodes.items() if node_id not in selected_ids)
    return ordered_nodes[: max(0, int(node_limit))], bounded_edges


def _quality(
    evidence: List[Dict[str, Any]],
    nodes: List[Dict[str, Any]],
    edges: List[Dict[str, Any]],
    paths: List[Dict[str, Any]],
    relation_bias: Optional[List[str]] = None,
) -> Dict[str, Any]:
    usable_evidence = [row for row in evidence if row.get("id") and row.get("text") and row.get("source")]
    typed_edges = [row for row in edges if row.get("source") and row.get("target") and row.get("relation")]
    preferred = {str(value).strip().lower() for value in (relation_bias or []) if str(value).strip()}
    preferred_paths = [row for row in paths if str(row.get("relation") or "").strip().lower() in preferred]
    unresolved_ids = {
        str(row.get("id"))
        for row in nodes
        if str(row.get("type") or "").strip().lower() == "unresolved_endpoint" and row.get("id")
    }
    resolved_paths = [
        row
        for row in paths
        if str((row.get("from") or {}).get("id") or "") not in unresolved_ids
        and str((row.get("to") or {}).get("id") or "") not in unresolved_ids
    ]
    resolved_preferred_paths = [row for row in preferred_paths if row in resolved_paths]
    evidence_backed_paths = [row for row in resolved_paths if row.get("evidence_refs")]
    evidence_backed_preferred_paths = [row for row in resolved_preferred_paths if row.get("evidence_refs")]
    semantic_path_count = len(evidence_backed_preferred_paths)
    graph_semantic_gate = (
        "pass"
        if evidence_backed_paths and (not preferred or semantic_path_count > 0)
        else "weak" if paths else "blocked"
    )
    return {
        "evidence_count": len(evidence),
        "usable_evidence_count": len(usable_evidence),
        "evidence_coverage": round(len(usable_evidence) / len(evidence), 3) if evidence else 0.0,
        "node_count": len(nodes),
        "typed_edge_count": len(typed_edges),
        "path_count": len(paths),
        "graph_coverage": round(len(paths) / len(typed_edges), 3) if typed_edges else 0.0,
        "preferred_relation_count": len(preferred_paths),
        "preferred_relation_path_count": semantic_path_count,
        "resolved_preferred_relation_path_count": len(resolved_preferred_paths),
        "evidence_backed_path_count": len(evidence_backed_paths),
        "evidence_backed_preferred_relation_path_count": len(evidence_backed_preferred_paths),
        "resolved_path_count": len(resolved_paths),
        "unresolved_endpoint_count": len(unresolved_ids),
        "graph_semantic_gate": graph_semantic_gate,
    }


class OntologyContextCollector:
    """Collect a bounded, authoritative OpenCrab context before model work."""

    def __init__(self, call_tool: McpCall, *, cache_path: Optional[Path] = None, cache_ttl_seconds: int = 300) -> None:
        self.call_tool = call_tool
        self.cache_path = cache_path.resolve() if cache_path is not None else None
        self.cache_ttl_seconds = max(30, min(int(cache_ttl_seconds), 3600))

    def collect(
        self,
        objective: str,
        *,
        package_ids: Optional[List[str]] = None,
        graph_required: bool = False,
        retrieval_contract: Optional[Dict[str, Any]] = None,
        scope_resolution: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        route = dict(retrieval_contract or compile_ontology_route(objective, graph_required=graph_required))
        resolved_scope = dict(scope_resolution or {})
        resolved_scope_ids = [
            str(value)
            for value in (resolved_scope.get("selected_package_ids") or [])
            if str(value).strip()
        ]
        selected_all = list(dict.fromkeys(str(value) for value in (package_ids or resolved_scope_ids) if str(value).strip()))
        requested_package_ids = selected_all
        queried_package_ids = selected_all[:_MAX_EXPLICIT_PACKAGES]
        omitted_package_ids = selected_all[_MAX_EXPLICIT_PACKAGES:]
        # The SaaS MCP scope is 25 packs per call. Explicit selections are
        # therefore queried in bounded batches, preserving the user's full
        # selection without putting 152 IDs into an unsupported single call.
        query_batches = (
            [queried_package_ids[index:index + _MCP_PACK_BATCH_LIMIT] for index in range(0, len(queried_package_ids), _MCP_PACK_BATCH_LIMIT)]
            if queried_package_ids
            else [[]]
        )
        selected = query_batches[0]
        package_limit = _MCP_PACK_BATCH_LIMIT
        cache_key = hashlib.sha256(
            json.dumps(
                {
                    "collector_revision": _CACHE_REVISION,
                    "objective": objective,
                    "package_ids": selected_all,
                    "route": route,
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        cached = self._read_cache(cache_key)
        if cached is not None:
            cached["cache_hit"] = True
            cached["cache_age_seconds"] = max(0, int(time.time() - float(cached.pop("_cached_at", time.time()))))
            cached["tool_calls"] = [
                {
                    "tool": "opencrab_context_cache",
                    "arguments": {"cache_key": cache_key[:16], "ttl_seconds": self.cache_ttl_seconds},
                    "status": "cached",
                    "response_status": "cached",
                }
            ]
            cached["observed_tool_call_count"] = 0
            cached["failed_tool_call_count"] = 0
            return cached
        query_arguments: Dict[str, Any] = {
            "query": route.get("primary_query") or objective,
            "top_k": int(route.get("evidence_top_k") or 8),
            "scan_limit": 5000,
        }
        if selected:
            query_arguments["package_ids"] = selected

        calls: List[Dict[str, Any]] = []
        query_payloads: List[Dict[str, Any]] = []
        for batch_index, batch in enumerate(query_batches, start=1):
            batch_arguments = dict(query_arguments)
            if batch:
                batch_arguments["package_ids"] = batch
            query_payloads.append(self._call("opencrab_query", batch_arguments, calls))
            if calls:
                calls[-1]["selection_batch"] = batch_index
                calls[-1]["selection_batch_count"] = len(query_batches)
        query_payload = _merge_query_payloads(query_payloads)
        query_status = self._status(query_payload)
        observed_items = _dedupe_observed_items(
            [item for payload in query_payloads for item in _normalize_observed_items(payload)]
        )
        evidence_rows = _list_value(query_payload, "evidence", "chunks")
        # Keep backwards compatibility with older query responses that put
        # evidence-shaped rows under ``items``. Catalog rows without captured
        # text remain typed observations, not fabricated evidence.
        if not evidence_rows:
            evidence_rows = [
                row
                for row in _list_value(query_payload, "items")
                if _text(row.get("text") or row.get("content"))
                and _text(row.get("source") or row.get("source_uri"))
            ]
        evidence = _normalize_evidence(evidence_rows, query=objective)
        nodes: List[Dict[str, Any]] = []
        edges: List[Dict[str, Any]] = []
        list_nodes_widened = False

        gateway_graph = query_payload.get("gateway_graph") if isinstance(query_payload.get("gateway_graph"), dict) else {}
        gateway_graph_authoritative = (
            bool(gateway_graph)
            and str(gateway_graph.get("authority") or "") == "gateway_verified_mcp_response"
            and _payload_status(gateway_graph) in {"ok", "no_evidence", "cached"}
        )

        if graph_required and query_status in {"ok", "no_evidence"} and gateway_graph_authoritative:
            nodes = _normalize_nodes(_node_rows(gateway_graph), limit=12)
            edges = _normalize_edges(_list_value(gateway_graph, "edges", "items", "relations"), limit=24)
            for raw_call in gateway_graph.get("tool_calls") or []:
                if not isinstance(raw_call, dict):
                    continue
                calls.append({
                    "tool": str(raw_call.get("tool") or "gateway_graph_prefetch"),
                    "arguments": raw_call.get("arguments") if isinstance(raw_call.get("arguments"), dict) else {},
                    "status": "observed",
                    "response_status": str(raw_call.get("response_status") or raw_call.get("status") or "ok").lower(),
                    "source": "gateway_verified_mcp_response",
                })
            list_nodes_widened = True
        elif graph_required and query_status in {"ok", "no_evidence"}:
            node_arguments: Dict[str, Any] = {
                "query": objective,
                "limit": int(route.get("node_limit") or 6),
                "scan_limit": int(route.get("node_scan_limit") or 1000),
            }
            if selected:
                node_arguments["package_ids"] = selected
            node_payload = self._call("opencrab_search_nodes", node_arguments, calls)
            nodes = _normalize_nodes(
                _list_value(node_payload, "nodes", "items", "results"),
                limit=int(route.get("node_limit") or 6),
            )
            if not nodes:
                # Some deployments expose the cheap paginated list endpoint
                # while search_nodes is unavailable or times out. Keep the
                # graph route honest, but recover with a bounded fallback. A
                # small search result is not enough to resolve edge endpoints;
                # the selected package scope lets us widen this one read
                # without scanning the user's whole workspace.
                list_arguments: Dict[str, Any] = {
                    "query": objective,
                    "limit": min(250, max(100, int(route.get("node_limit") or 6))),
                }
                if selected:
                    list_arguments["package_ids"] = selected
                list_payload = self._call("opencrab_list_nodes", list_arguments, calls)
                nodes = _normalize_nodes(
                    _list_value(list_payload, "nodes", "items", "results"),
                    limit=min(250, max(100, int(route.get("node_limit") or 6))),
                )
                list_nodes_widened = True
            # Relations are the scarce semantic resource in a graph route.
            # Fetch them before expanding a node neighborhood so the bounded
            # call budget is spent on paths and endpoint resolution first.
            edge_arguments: Dict[str, Any] = {"limit": int(route.get("edge_limit") or 24)}
            if selected:
                edge_arguments["package_ids"] = selected
            edge_payload = self._call("opencrab_list_edges", edge_arguments, calls)
            edges.extend(_normalize_edges(_list_value(edge_payload, "edges", "items", "relations")))

            # A deployment may not expose list_edges. Only then spend a
            # context expansion on the first search result as a bounded
            # compatibility fallback.
            if not edges:
                context_limit = max(1, int(route.get("context_node_limit") or 0))
                for node in nodes[:context_limit]:
                    context_payload = self._call(
                        "opencrab_get_node_context",
                        {
                            "node_id": node["id"],
                            "limit": int(route.get("edge_limit") or 24),
                            **({"package_ids": selected} if selected else {}),
                        },
                        calls,
                    )
                    context_nodes = _normalize_nodes(_node_rows(context_payload))
                    nodes.extend(context_nodes)
                    edges.extend(_normalize_edges(_list_value(context_payload, "edges", "relations")))

            # Search endpoints can return a useful neighborhood while the
            # edge listing refers to nodes outside that first page. Resolve
            # those IDs from the same selected package scope before marking
            # the topology as unresolved. This remains one bounded read.
            if edges and not list_nodes_widened:
                known_node_ids = {str(row.get("id") or "") for row in nodes if row.get("id")}
                has_unresolved_edge_endpoint = any(
                    str(endpoint or "").strip() and str(endpoint or "").strip() not in known_node_ids
                    for edge in edges
                    for endpoint in (edge.get("source"), edge.get("target"))
                )
                if has_unresolved_edge_endpoint:
                    list_arguments = {
                        "query": objective,
                        "limit": min(250, max(100, int(route.get("node_limit") or 6))),
                    }
                    if selected:
                        list_arguments["package_ids"] = selected
                    list_payload = self._call("opencrab_list_nodes", list_arguments, calls)
                    nodes.extend(
                        _normalize_nodes(
                            _list_value(list_payload, "nodes", "items", "results"),
                            limit=min(250, max(100, int(route.get("node_limit") or 6))),
                        )
                    )
                    list_nodes_widened = True

        # If list_edges gives us IDs without node labels, spend at most the
        # route's bounded context budget resolving one or two endpoints. This
        # is a cheap MCP lookup and prevents UUID-only topology from looking
        # like a semantically usable path.
        if graph_required and edges and not gateway_graph_authoritative:
            known_node_ids = {str(row.get("id") or "") for row in nodes if row.get("id")}
            endpoint_ids: List[str] = []
            ranked_edges = sorted(
                enumerate(edges),
                key=lambda pair: _edge_priority(
                    pair[1],
                    route.get("relation_bias") if isinstance(route.get("relation_bias"), list) else None,
                    pair[0],
                ),
                reverse=True,
            )
            for _, edge in ranked_edges:
                for endpoint in (edge.get("source"), edge.get("target")):
                    endpoint = str(endpoint or "").strip()
                    if endpoint and endpoint not in known_node_ids and endpoint not in endpoint_ids:
                        endpoint_ids.append(endpoint)
            # A list-edges response can expose both UUID endpoints while a
            # node-context response returns only the singleton node that was
            # requested. Resolve both ends so a typed, evidence-backed edge
            # can become a real path rather than a UUID-only placeholder.
            resolve_limit = max(0, min(max(int(route.get("context_node_limit") or 1), 2), 2))
            for endpoint in endpoint_ids[:resolve_limit]:
                context_payload = self._call(
                    "opencrab_get_node_context",
                    {
                        "node_id": endpoint,
                        "limit": int(route.get("edge_limit") or 24),
                        **({"package_ids": selected} if selected else {}),
                    },
                    calls,
                )
                nodes.extend(_normalize_nodes(_node_rows(context_payload)))
                edges.extend(_normalize_edges(_list_value(context_payload, "edges", "relations")))

        unique_nodes: Dict[str, Dict[str, Any]] = {}
        for node in nodes:
            unique_nodes.setdefault(node["id"], node)
        unique_edges: Dict[str, Dict[str, Any]] = {}
        for index, edge in enumerate(edges):
            edge_key = edge["id"] or "%s|%s|%s" % (edge["source"], edge["relation"], edge["target"])
            unique_edges.setdefault(edge_key or "edge-%s" % index, edge)

        # Rank before applying the node cap. Otherwise a generic first-page
        # node can hide the only evidence-backed preferred relation.
        bounded_nodes, bounded_edges = _bounded_graph_packet(
            unique_nodes,
            unique_edges,
            node_limit=12,
            edge_limit=24,
            relation_bias=route.get("relation_bias") if isinstance(route.get("relation_bias"), list) else None,
        )
        # Some OpenCrab graph responses expose typed edges before they expose
        # node labels. Preserve the observed topology without inventing a
        # semantic label: endpoint records are explicitly marked unresolved.
        for edge in bounded_edges:
            for endpoint in (edge.get("source"), edge.get("target")):
                endpoint = str(endpoint or "").strip()
                if endpoint and endpoint not in unique_nodes:
                    unique_nodes[endpoint] = {
                        "id": endpoint,
                        "label": endpoint,
                        "type": "unresolved_endpoint",
                        "package_id": "",
                        "evidence_refs": edge.get("evidence_refs") or [],
                        "metadata": {"derived_from_edge": edge.get("id") or ""},
                    }
        # Endpoint placeholders may have been added after ranking. Rebuild
        # the packet once so selected edges remain represented in the cap.
        bounded_nodes, bounded_edges = _bounded_graph_packet(
            unique_nodes,
            {str(edge.get("id") or "%s|%s|%s" % (edge.get("source"), edge.get("relation"), edge.get("target"))): edge for edge in bounded_edges},
            node_limit=12,
            edge_limit=24,
            relation_bias=route.get("relation_bias") if isinstance(route.get("relation_bias"), list) else None,
        )
        paths = _bounded_paths(bounded_nodes, bounded_edges)
        quality = _quality(
            evidence,
            bounded_nodes,
            bounded_edges,
            paths,
            relation_bias=route.get("relation_bias") if isinstance(route.get("relation_bias"), list) else None,
        )
        observed_calls = [row for row in calls if row.get("status") == "observed"]
        failed_calls = [row for row in calls if row.get("status") == "failed"]
        grounding = "evidence" if quality["usable_evidence_count"] else "graph" if bounded_nodes else "none"
        if not graph_required:
            graph_gate = "not_required"
        elif quality["graph_semantic_gate"] == "pass":
            graph_gate = "pass"
        elif paths:
            # UUID-only or generic mention paths describe topology, not a
            # verified evidence-to-outcome semantic route.
            graph_gate = "weak"
        else:
            graph_gate = "blocked"
        # Graph collection is intentionally bounded to the first provider
        # batch. Evidence retrieval may cover every selected batch, but a
        # graph conclusion must not pretend that one graph slice represents
        # all selected packs.
        if graph_required and len(query_batches) > 1 and graph_gate == "pass":
            graph_gate = "weak"
        successful_batch_count = int(query_payload.get("successful_batch_count") or 0)
        failed_batch_count = int(query_payload.get("failed_batch_count") or 0)
        auto_scope = bool(resolved_scope_ids) and (
            not package_ids or str(resolved_scope.get("scope_mode") or "") == "workspace_auto_resolved"
        )
        explicit_scope = bool(selected_all) and not auto_scope
        if not explicit_scope or auto_scope:
            selection_gate = "not_required"
        elif not omitted_package_ids and successful_batch_count == len(query_batches):
            selection_gate = "pass"
        else:
            selection_gate = "blocked"
        scope_mode = (
            "selected_complete" if explicit_scope and selection_gate == "pass"
            else "selected_partial" if explicit_scope
            else "workspace_auto_resolved" if resolved_scope_ids
            else "workspace_auto"
        )
        receipt = {
            "schema": "crab.opencrab-context-receipt/v2",
            "source": "OpenCrab MCP",
            "authority": str(query_payload.get("authority") or "direct_mcp_response"),
            "query": objective,
            "arguments": query_arguments,
            "status": query_status,
            "error": query_payload.get("error"),
            "error_code": query_payload.get("error_code"),
            "error_stage": query_payload.get("error_stage"),
            "request_id": query_payload.get("request_id"),
            "retryable": bool(query_payload.get("retryable")),
            "observed_at": None,
            "route": route,
            "evidence": evidence,
            "evidence_count": len(evidence),
            "observed_items": observed_items,
            "observed_item_count": len(observed_items),
            "nodes": bounded_nodes,
            "node_count": len(bounded_nodes),
            "edges": bounded_edges,
            "edge_count": len(bounded_edges),
            "paths": paths,
            "quality": quality,
            "grounding": grounding,
            "claim_gate": "pass" if quality["usable_evidence_count"] and selection_gate != "blocked" else "blocked",
            # Catalog rows are an observation surface, not claim evidence. A
            # lookup may legitimately return typed pack/project metadata with
            # zero evidence chunks, so keep that result independently gated.
            "observation_gate": (
                "pass"
                if observed_items and not evidence and query_status in {"ok", "cached", "no_evidence"}
                else "not_required"
                if evidence
                else "blocked"
            ),
            "graph_gate": graph_gate,
            "selection_gate": selection_gate,
            "retrieval": query_payload.get("retrieval") if isinstance(query_payload, dict) and isinstance(query_payload.get("retrieval"), dict) else {},
            "pack_scope": {
                **(query_payload.get("pack_scope") if isinstance(query_payload, dict) and isinstance(query_payload.get("pack_scope"), dict) else {}),
                "requested_package_count": len(selected_all),
                "queried_package_count": len(queried_package_ids),
                "package_limit": package_limit,
                "omitted_package_count": len(omitted_package_ids),
                "omitted_package_ids_preview": omitted_package_ids[:8],
                "scope_mode": scope_mode,
                "scope_resolution": resolved_scope,
                "batch_count": len(query_batches),
                "successful_batch_count": successful_batch_count,
                "failed_batch_count": failed_batch_count,
                "selection_gate": selection_gate,
                "selection_digest": hashlib.sha256(
                    json.dumps(requested_package_ids, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                ).hexdigest()[:16] if requested_package_ids else None,
            },
            "tool_calls": calls,
            "observed_tool_call_count": len(observed_calls),
            "failed_tool_call_count": len(failed_calls),
            "raw_answer_observed": bool(query_payload.get("answer")) if isinstance(query_payload, dict) else False,
            "cache_hit": False,
            "cache_key": cache_key,
        }
        cacheable_graph = not graph_required or receipt.get("graph_gate") == "pass"
        if self.cache_path is not None and receipt["status"] == "ok" and receipt["claim_gate"] == "pass" and cacheable_graph:
            self._write_cache(cache_key, receipt)
        return receipt

    def _read_cache(self, cache_key: str) -> Optional[Dict[str, Any]]:
        if self.cache_path is None or not self.cache_path.is_file():
            return None
        try:
            envelope = json.loads(self.cache_path.read_text(encoding="utf-8"))
            entries = envelope.get("entries")
            if isinstance(entries, dict):
                entry = entries.get(cache_key)
                if not isinstance(entry, dict):
                    return None
                cached_at = float(entry.get("cached_at") or 0)
                payload = entry.get("payload")
            else:
                # Read the v1 single-entry envelope so upgrading does not
                # discard the most recent verified context.
                cached_at = float(envelope.get("cached_at") or 0)
                payload = envelope.get("payload")
                if envelope.get("cache_key") != cache_key:
                    return None
            if time.time() - cached_at > self.cache_ttl_seconds or not isinstance(payload, dict):
                return None
            result = copy.deepcopy(payload)
            result["_cached_at"] = cached_at
            return result
        except (OSError, ValueError, TypeError):
            return None

    def _write_cache(self, cache_key: str, receipt: Dict[str, Any]) -> None:
        if self.cache_path is None:
            return
        now = time.time()
        with _CACHE_WRITE_LOCK:
            entries: Dict[str, Dict[str, Any]] = {}
            try:
                if self.cache_path.is_file():
                    existing = json.loads(self.cache_path.read_text(encoding="utf-8"))
                    stored = existing.get("entries") if isinstance(existing, dict) else None
                    if isinstance(stored, dict):
                        entries = {
                            str(key): dict(value)
                            for key, value in stored.items()
                            if isinstance(value, dict)
                        }
                    elif isinstance(existing, dict) and existing.get("cache_key"):
                        # Promote the old single-entry format while writing
                        # the first v2 entry.
                        legacy_key = str(existing.get("cache_key"))
                        entries[legacy_key] = {
                            "cached_at": existing.get("cached_at"),
                            "payload": existing.get("payload"),
                        }
            except (OSError, ValueError, TypeError):
                entries = {}

            fresh: Dict[str, Dict[str, Any]] = {}
            for key, entry in entries.items():
                try:
                    cached_at = float(entry.get("cached_at") or 0)
                except (TypeError, ValueError):
                    continue
                if now - cached_at <= self.cache_ttl_seconds and isinstance(entry.get("payload"), dict):
                    fresh[key] = {"cached_at": cached_at, "payload": entry["payload"]}
            fresh[cache_key] = {"cached_at": now, "payload": copy.deepcopy(receipt)}
            ordered = sorted(fresh.items(), key=lambda item: float(item[1].get("cached_at") or 0), reverse=True)
            entries = dict(ordered[:_CACHE_MAX_ENTRIES])
            envelope = {
                "schema": "crab.ontology-context-cache/v2",
                "entries": entries,
                # Keep the most recent entry at the top level for humans and
                # older diagnostics that inspect the cache file directly.
                "cache_key": cache_key,
                "cached_at": now,
                "payload": copy.deepcopy(receipt),
            }
            try:
                self.cache_path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.cache_path.with_suffix(".tmp")
                temporary.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
                temporary.replace(self.cache_path)
            except OSError:
                return

    def _call(self, name: str, arguments: Dict[str, Any], calls: List[Dict[str, Any]]) -> Dict[str, Any]:
        try:
            payload = self.call_tool(name, arguments)
            status = self._status(payload)
            call_status = "observed" if status in {"ok", "no_evidence"} else "failed"
            if isinstance(payload, dict):
                normalized = dict(payload)
            else:
                normalized = {"status": "error", "error": "invalid MCP payload"}
                status = "error"
                call_status = "failed"
            details = opencrab_error_details(normalized, default_stage="tool:%s" % name)
            call = {
                "tool": name,
                "arguments": arguments,
                "status": call_status,
                "response_status": status,
            }
            if call_status == "failed":
                normalized.setdefault("error", details["message"])
                normalized.setdefault("error_code", details["error_code"])
                normalized.setdefault("error_stage", details["stage"])
                normalized.setdefault("request_id", details["request_id"])
                normalized.setdefault("retryable", details["retryable"])
                call.update(
                    {
                        "error": details["message"],
                        "error_code": details["error_code"],
                        "stage": details["stage"],
                        "request_id": details["request_id"],
                        "retryable": details["retryable"],
                    }
                )
            calls.append(call)
            return normalized
        except Exception as exc:
            details = opencrab_error_details(exc, default_stage="tool:%s" % name)
            calls.append(
                {
                    "tool": name,
                    "arguments": arguments,
                    "status": "failed",
                    "error": details["message"],
                    "error_code": details["error_code"],
                    "stage": details["stage"],
                    "request_id": details["request_id"],
                    "retryable": details["retryable"],
                    "error_type": type(exc).__name__,
                }
            )
            return {
                "status": "error",
                "error": details["message"],
                "error_code": details["error_code"],
                "error_stage": details["stage"],
                "request_id": details["request_id"],
                "retryable": details["retryable"],
            }

    @staticmethod
    def _status(payload: Any) -> str:
        return _payload_status(payload)


def compact_context(receipt: Dict[str, Any], max_chars: int = 4200) -> str:
    """Return the bounded model-facing projection, never the raw MCP payload."""
    payload = {
        "schema": receipt.get("schema"),
        "authority": receipt.get("authority"),
        "status": receipt.get("status"),
        "cache_hit": receipt.get("cache_hit", False),
        "cache_age_seconds": receipt.get("cache_age_seconds"),
        "claim_gate": receipt.get("claim_gate"),
        "graph_gate": receipt.get("graph_gate"),
        "grounding": receipt.get("grounding"),
        "route": copy.deepcopy(receipt.get("route") or {}),
        "quality": copy.deepcopy(receipt.get("quality") or {}),
        "observed_items": copy.deepcopy(receipt.get("observed_items", [])[:12]),
        "evidence": _model_evidence(receipt),
        "nodes": copy.deepcopy(receipt.get("nodes", [])[:8]),
        "edges": copy.deepcopy(receipt.get("edges", [])[:16]),
        "paths": copy.deepcopy(receipt.get("paths", [])[:8]),
        "retrieval": copy.deepcopy(receipt.get("retrieval", {})),
        "pack_scope": copy.deepcopy(receipt.get("pack_scope", {})),
        "artifact_id": receipt.get("artifact_id"),
    }

    def render() -> str:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    rendered = render()
    if len(rendered) <= max_chars:
        return rendered
    for row in payload["evidence"]:
        if isinstance(row, dict):
            row["text"] = _text(row.get("text"), 500)
    for row in payload["nodes"]:
        if isinstance(row, dict):
            row["label"] = _text(row.get("label"), 180)
    for row in payload["edges"]:
        if isinstance(row, dict):
            row["metadata"] = {}
    payload["route"] = {
        "mode": payload["route"].get("mode"),
        "spaces": payload["route"].get("spaces", [])[:6],
        "relation_bias": payload["route"].get("relation_bias", [])[:4],
    }
    payload["quality"] = {
        "usable_evidence_count": payload["quality"].get("usable_evidence_count", 0),
        "path_count": payload["quality"].get("path_count", 0),
        "graph_coverage": payload["quality"].get("graph_coverage", 0),
    }
    rendered = render()
    if len(rendered) <= max_chars:
        return rendered
    payload["evidence"] = payload["evidence"][:4]
    payload["observed_items"] = payload["observed_items"][:6]
    payload["nodes"] = payload["nodes"][:4]
    payload["edges"] = payload["edges"][:8]
    rendered = render()
    if len(rendered) <= max_chars:
        return rendered
    payload["retrieval"] = {}
    payload["pack_scope"] = {}
    payload["route"] = {"mode": payload["route"].get("mode")}
    payload["quality"] = {"usable_evidence_count": payload["quality"].get("usable_evidence_count", 0)}
    payload["evidence"] = payload["evidence"][:2]
    payload["observed_items"] = payload["observed_items"][:3]
    payload["nodes"] = payload["nodes"][:2]
    payload["edges"] = payload["edges"][:4]
    payload["paths"] = payload["paths"][:2]
    rendered = render()
    if len(rendered) <= max_chars:
        return rendered
    minimal = {
        "schema": receipt.get("schema"),
        "authority": receipt.get("authority"),
        "status": receipt.get("status"),
        "cache_hit": receipt.get("cache_hit", False),
        "cache_age_seconds": receipt.get("cache_age_seconds"),
        "claim_gate": receipt.get("claim_gate"),
        "graph_gate": receipt.get("graph_gate"),
        "grounding": receipt.get("grounding"),
        "evidence_count": receipt.get("evidence_count", 0),
        "observed_items": copy.deepcopy(receipt.get("observed_items", [])[:3]),
        # Evidence identity is the minimum viable grounding contract. Do not
        # drop it from the final compression tier just to hit a token budget;
        # otherwise Queen cannot cite what MCP actually returned.
        "evidence": [
            {
                "id": row.get("id"),
                "document_id": row.get("document_id"),
                "project_id": row.get("project_id"),
                "package_id": row.get("package_id"),
                "workspace_id": row.get("workspace_id"),
                "lane_id": row.get("lane_id"),
                "lane_purpose": row.get("lane_purpose"),
                "profile_id": row.get("profile_id"),
                "claim_scope": row.get("claim_scope"),
                "source": _text(row.get("source"), 180),
                "text": _text(row.get("text"), 480),
            }
            for row in _model_evidence(receipt, limit=2)
            if isinstance(row, dict) and row.get("id")
        ],
        "node_count": receipt.get("node_count", 0),
        "edge_count": receipt.get("edge_count", 0),
        "path_count": len(receipt.get("paths") or []),
        "artifact_id": receipt.get("artifact_id"),
    }
    rendered = json.dumps(minimal, ensure_ascii=False, separators=(",", ":"))
    if len(rendered) <= max_chars:
        return rendered
    for row in minimal["evidence"]:
        row["source"] = ""
        row["text"] = _text(row.get("text"), 320)
    minimal["evidence"] = minimal["evidence"][:1]
    minimal["observed_items"] = minimal["observed_items"][:1]
    rendered = json.dumps(minimal, ensure_ascii=False, separators=(",", ":"))
    if len(rendered) <= max_chars:
        return rendered
    return json.dumps(
        {
            "authority": minimal["authority"],
            "status": minimal["status"],
            "claim_gate": minimal["claim_gate"],
            "evidence_count": minimal["evidence_count"],
            "observed_items": [
                {
                    "id": row.get("id"),
                    "title": _text(row.get("title"), 120),
                    "type": row.get("type"),
                }
                for row in minimal["observed_items"]
                if isinstance(row, dict) and row.get("id")
            ],
            "evidence": [{"id": row["id"]} for row in minimal["evidence"] if row.get("id")],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
