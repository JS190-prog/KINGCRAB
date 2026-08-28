from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .codex_app_server import (
    CodexAppServerError,
    CodexAppServerSession,
    CodexLiveEvent,
    CodexLiveTurn,
    can_fallback_to_session_default,
)
from .models import (
    ArtifactStatus,
    AttemptStatus,
    Budget,
    Checkpoint,
    EvidenceRef,
    MissionStatus,
    Role,
    RoleAssignment,
    TaskSlot,
    TaskStatus,
    WorkerPolicy,
    worker_capacity,
    utc_now,
)
from .opencrab import (
    OpenCrabMcpClient,
    OpenCrabUnavailable,
    opencrab_error_details,
    opencrab_url,
    resolve_opencrab_package_scope,
)
from .ontology_context import OntologyContextCollector, _model_evidence, compact_context
from .ontology_contract import (
    ONTOLOGY_LEDGER_KIND,
    attach_subgoal_slots,
    compact_ontology_execution_contract,
    compile_ontology_execution_contract,
    promote_execution_contract_to_ledger,
    update_decision_gate,
)
from .goal_graph import compact_goal_graph, compile_goal_graph
from .kinetic_contract import compact_king_plan, compile_king_plan
from .kinetic_workflow import compact_kinetic_workflow, workflow_summary
from .runtime import RuntimeService
from .workspace_observer import capture_workspace, diff_workspace
from .opencrab_snapshot import load_diff
from .store import new_id


ROLE_INSTRUCTIONS = {
    Role.KING: "Compile the objective into an acceptance-gated workflow. Do not edit files or retrieve broad context.",
    Role.QUEEN: "Retrieve and shape only the minimum ontology path needed for this goal. Preserve evidence references and do not invent sources.",
    Role.WORKER: "Produce only the bounded deliverable. When requires_write is false, return the requested evidence-backed result without editing workspace or external state. When requires_write is true, use the Queen handoff, preserve user changes, and run focused verification.",
    Role.SOLDIER: "Patrol evidence, scope, duplicate work, and waste. Stop or quarantine only what the receipts justify.",
    Role.ORACLE: "Inspect actual artifacts, receipts, evidence and verification output. Publish a concise conclusion only when the gates pass.",
}

ROLE_IDLE_TIMEOUT_SECONDS = {
    Role.KING: 1800.0,
    Role.QUEEN: 1800.0,
    Role.WORKER: 2400.0,
    Role.ORACLE: 1800.0,
}

_HOST_WORKER_ARTIFACT_PREFIX = "HOST_WORKER_ARTIFACT_V1:"
_HOST_WORKER_ARTIFACT_ROOT = ".crabagent/artifacts/"
_HOST_WORKER_ARTIFACT_MAX_BYTES = 16 * 1024
_HOST_WORKER_ARTIFACT_SUFFIXES = frozenset({".md", ".txt", ".json"})
_HOST_WORKER_ARTIFACT_FILENAME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")
_HOST_WORKER_CODE_FENCE_PREFIXES = ("```", "~~~")


def _parse_host_worker_artifact_directive(text: str) -> tuple[str, Optional[Dict[str, Any]]]:
    lines = str(text or "").splitlines()
    matches = []
    for index, line in enumerate(lines):
        candidate = line.strip()
        if candidate.startswith(_HOST_WORKER_ARTIFACT_PREFIX):
            matches.append((index, candidate[len(_HOST_WORKER_ARTIFACT_PREFIX):], False))
            continue
        # Models sometimes wrap the required physical line in a one-line
        # fenced JSON block. Accept only a complete fence whose inner text is
        # still the directive; prose and Markdown bullets remain invalid.
        for fence in _HOST_WORKER_CODE_FENCE_PREFIXES:
            if candidate.startswith(fence) and candidate.endswith(fence) and len(candidate) > len(fence) * 2:
                inner = candidate[len(fence):-len(fence)].strip()
                if inner.startswith(_HOST_WORKER_ARTIFACT_PREFIX):
                    matches.append((index, inner[len(_HOST_WORKER_ARTIFACT_PREFIX):], True))
                    break
        else:
            continue
    if not matches:
        return str(text or "").strip(), None
    if len(matches) != 1:
        raise ValueError("HOST_WORKER_ARTIFACT_V1 requires exactly one directive line")
    index, raw_payload, one_line_fence = matches[0]
    try:
        payload = json.loads(raw_payload)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("HOST_WORKER_ARTIFACT_V1 payload must be one valid JSON object") from exc
    if not isinstance(payload, dict) or set(payload) != {"relative_path", "content"}:
        raise ValueError("HOST_WORKER_ARTIFACT_V1 supports only relative_path and content")
    relative_path = str(payload.get("relative_path") or "").strip()
    artifact_content = payload.get("content")
    if not isinstance(artifact_content, str) or not artifact_content:
        raise ValueError("HOST_WORKER_ARTIFACT_V1 content must be non-empty UTF-8 text")
    if not relative_path.startswith(_HOST_WORKER_ARTIFACT_ROOT):
        raise ValueError("HOST_WORKER_ARTIFACT_V1 path must be under .crabagent/artifacts")
    filename = relative_path[len(_HOST_WORKER_ARTIFACT_ROOT):]
    if (
        not filename
        or len(filename) > 128
        or filename.startswith(".")
        or filename.startswith("mission-")
        or "/" in filename
        or "\\" in filename
        or any(character not in _HOST_WORKER_ARTIFACT_FILENAME_CHARS for character in filename)
    ):
        raise ValueError("HOST_WORKER_ARTIFACT_V1 requires one safe direct-child artifact filename")
    if Path(filename).suffix.lower() not in _HOST_WORKER_ARTIFACT_SUFFIXES:
        raise ValueError("HOST_WORKER_ARTIFACT_V1 allows only .md, .txt, or .json artifacts")
    encoded = artifact_content.encode("utf-8")
    if len(encoded) > _HOST_WORKER_ARTIFACT_MAX_BYTES:
        raise ValueError("HOST_WORKER_ARTIFACT_V1 content exceeds 16 KiB")
    remove_indices = {index}
    if not one_line_fence:
        # Remove only fence delimiters directly enclosing the directive so a
        # Korean explanation or verification text remains available as the
        # role result after artifact creation.
        before = index - 1
        after = index + 1
        if before >= 0 and lines[before].strip().startswith(_HOST_WORKER_CODE_FENCE_PREFIXES):
            remove_indices.add(before)
        if after < len(lines) and lines[after].strip().startswith(_HOST_WORKER_CODE_FENCE_PREFIXES):
            remove_indices.add(after)
    cleaned = "\n".join(line for line_index, line in enumerate(lines) if line_index not in remove_indices).strip()
    if not cleaned:
        cleaned = "Bounded host-worker artifact prepared."
    return cleaned, {
        "relative_path": _HOST_WORKER_ARTIFACT_ROOT + filename,
        "content": artifact_content,
        "bytes": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _opencrab_retry_details(receipt: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return one transient context failure, never retry a successful receipt."""
    if not isinstance(receipt, dict) or str(receipt.get("status") or "").lower() in {"ok", "cached", "no_evidence"}:
        return None
    calls = receipt.get("tool_calls") if isinstance(receipt.get("tool_calls"), list) else []
    candidates = [row for row in calls if isinstance(row, dict) and str(row.get("status") or "") == "failed"]
    candidates.append(receipt)
    for candidate in candidates:
        details = opencrab_error_details(candidate, default_stage="context_collection")
        if details.get("retryable"):
            return details
    return None


def _usage_total(usage: Dict[str, Any]) -> Optional[int]:
    last = usage.get("last") if isinstance(usage.get("last"), dict) else usage
    for key in ("totalTokens", "total_tokens"):
        value = last.get(key) if isinstance(last, dict) else None
        if isinstance(value, int):
            return value
    return None


def _usage_billable_input(usage: Dict[str, Any]) -> Optional[int]:
    """Return the uncached input portion used by the mission token gate.

    Codex app-server reports ``totalTokens`` for the whole persistent thread
    context. That value is useful for observation, but it is not a bounded
    per-mission allowance: a second role would be blocked merely because it
    inherited the already-cached conversation. The goal budget therefore uses
    the provider-reported uncached input when available, with total tokens as
    a compatibility fallback for test doubles and older providers.
    """
    last = usage.get("last") if isinstance(usage.get("last"), dict) else usage
    if not isinstance(last, dict):
        return None
    input_tokens = last.get("inputTokens", last.get("input_tokens"))
    cached_tokens = last.get("cachedInputTokens", last.get("cached_input_tokens"))
    if isinstance(input_tokens, int) and isinstance(cached_tokens, int):
        return max(0, input_tokens - cached_tokens)
    return _usage_total(usage)


def _compact_label_map(values: Any, limit: int = 18) -> Dict[str, Any]:
    """Keep selected ontology references useful without dumping a catalog."""
    mapping = values if isinstance(values, dict) else {}
    rows = sorted(
        ((str(key), str(value)) for key, value in mapping.items()),
        key=lambda item: item[1].lower(),
    )
    return {
        "count": len(rows),
        "preview": [{"id": key, "label": value} for key, value in rows[:limit]],
        "truncated": len(rows) > limit,
    }


class ColonyExecutor:
    """Runs one durable mission while the private runtime owns the Codex thread."""

    def __init__(
        self,
        service: RuntimeService,
        session_id: str,
        bridge: CodexAppServerSession,
        cancel_event: threading.Event,
        on_runtime_request: Optional[Callable[[Dict[str, Any]], None]] = None,
        opencrab_context_loader: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
        retry_of: str = "",
    ) -> None:
        self.service = service
        self.store = service.store
        self.session_id = session_id
        self.bridge = bridge
        self.cancel_event = cancel_event
        self.on_runtime_request = on_runtime_request
        self.opencrab_context_loader = opencrab_context_loader
        self.observed_tokens: Optional[int] = None
        self.opencrab_delta = load_diff(self.service.workspace)
        self.ontology_context = self.store.ontology_context(session_id)
        self.goal_plan: Dict[str, Any] = {}
        self.goal_graph: Dict[str, Any] = {}
        self.kinetic_workflow: Dict[str, Any] = {}
        self.kinetic_trace: List[Dict[str, Any]] = []
        self.ontology_contract: Dict[str, Any] = {}
        self.king_plan: Dict[str, Any] = {}
        self.opencrab_receipt: Dict[str, Any] = {}
        self.observed_model_turns = 0
        self.observed_billable_input_tokens: Optional[int] = None
        self._worker_baseline: Optional[Dict[str, Any]] = None
        self.automatic_revision_cycles = 0
        self._repair_mode = ""
        self.retry_of = str(retry_of or "")

    def _record_kinetic_transition(
        self,
        mission_id: str,
        task: Optional[Dict[str, Any]],
        step_ids: List[str],
        status: str,
        *,
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Persist the actual ontology-to-action state transition."""
        if not self.kinetic_workflow or not step_ids:
            return
        allowed = {str(value) for value in step_ids if str(value).strip()}
        observed_at = utc_now()
        for step in self.kinetic_workflow.get("steps") or []:
            if not isinstance(step, dict) or str(step.get("id") or "") not in allowed:
                continue
            step["status"] = status
            step["observed_at"] = observed_at
            if detail:
                step["observation"] = dict(detail)
            self.kinetic_trace.append(
                {
                    "step_id": step.get("id"),
                    "operator": step.get("operator"),
                    "role": step.get("role"),
                    "status": status,
                    "task_id": (task or {}).get("task_id"),
                    "observed_at": observed_at,
                    "detail": dict(detail or {}),
                }
            )
        self.store.append_event(
            mission_id,
            "kinetic_step_%s" % status,
            str((task or {}).get("role") or "RUNTIME"),
            {
                "step_ids": sorted(allowed),
                "task_id": (task or {}).get("task_id"),
                "detail": dict(detail or {}),
            },
        )

    def _persist_kinetic_state(self, mission_id: str, task_id: str) -> str:
        """Persist the executed path separately from the immutable plan."""
        if not self.kinetic_workflow or not task_id:
            return ""
        payload = {
            "schema": "crab.kinetic-workflow-state/v1",
            "mission_id": mission_id,
            "workflow": self.kinetic_workflow,
            "trace": self.kinetic_trace,
            "summary": workflow_summary(self.kinetic_workflow),
        }
        artifact = self.service._write_artifact(
            mission_id,
            task_id,
            "kinetic_workflow_state.json",
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            "kinetic_workflow_state",
        )
        return artifact.artifact_id

    @staticmethod
    def _artifact_payload(row: Dict[str, Any]) -> Dict[str, Any]:
        """Read a small JSON role receipt without treating prose as structured data."""
        try:
            value = json.loads(Path(str(row.get("path") or "")).read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _outcome_next_action(conclusion: str, *, accepted: bool, failure: str = "") -> str:
        value = str(conclusion or "")
        if "NEXT_ACTION" in value:
            action = value.split("NEXT_ACTION", 1)[1].strip().split("\n", 1)[0].strip()
            if action:
                return action[:360]
        if accepted:
            return "완료: Oracle이 결과와 검증 영수증을 승인했습니다."
        if failure:
            return failure[:360]
        return "미션 영수증을 확인하고 필요한 입력 또는 근거를 보완한 뒤 재실행하세요."

    def _capture_king_plan(
        self,
        mission_id: str,
        task: Dict[str, Any],
        content: str,
        *,
        source: str,
    ) -> Dict[str, Any]:
        """Turn KING's advisory output into the packet consumed by later roles."""
        self.king_plan = compile_king_plan(
            objective=str(self.goal_plan.get("objective") or ""),
            goal_plan=self.goal_plan,
            goal_graph=self.goal_graph or compile_goal_graph(self.goal_plan),
            source=source,
            model_text=content if source == "codex_turn" else "",
        )
        artifact = self.service._write_artifact(
            mission_id,
            task["task_id"],
            "king_plan.json",
            json.dumps(self.king_plan, ensure_ascii=False, indent=2) + "\n",
            "king_plan",
        )
        self.store.append_event(
            mission_id,
            "king_plan_captured",
            Role.KING.value,
            {
                "artifact_id": artifact.artifact_id,
                "source": source,
                "goal_graph_id": self.king_plan.get("goal_graph_id"),
                "subgoal_count": len(self.king_plan.get("subgoals") or []),
                "decision_slot_count": len(self.king_plan.get("decision_slots") or []),
                "scope_locked": True,
            },
        )
        self._refresh_ontology_contract(mission_id, task["task_id"])
        return self.king_plan

    def _refresh_ontology_contract(
        self,
        mission_id: str,
        task_id: str,
        *,
        decision_text: str = "",
    ) -> Dict[str, Any]:
        """Persist the slot ledger after every authoritative phase.

        The receipt is the only source that can fill evidence slots. KING may
        assign subgoals and Queen may fill decision headings, but neither can
        promote model prose into evidence.
        """
        if not self.goal_plan.get("ontology_required"):
            return {}
        previous_revision = int(self.ontology_contract.get("revision") or 0)
        contract = compile_ontology_execution_contract(
            self.goal_plan,
            self.goal_graph or compile_goal_graph(self.goal_plan),
            self.opencrab_receipt or None,
            king_plan=self.king_plan,
            mission_id=mission_id,
            revision=previous_revision + 1 if previous_revision else 1,
        )
        if self.king_plan.get("subgoals"):
            contract["king_subgoals"] = []
            attach_subgoal_slots(contract, self.king_plan.get("subgoals") or [])
        if decision_text:
            update_decision_gate(contract, decision_text)
        elif self.ontology_contract.get("decision_observation"):
            update_decision_gate(
                contract,
                str(self.ontology_contract.get("decision_observation", {}).get("raw_text") or ""),
            )
        self.ontology_contract = contract
        artifact = self.service._write_artifact(
            mission_id,
            task_id,
            "ontology_execution_contract.json",
            json.dumps(contract, ensure_ascii=False, indent=2) + "\n",
            "ontology_execution_contract",
        )
        self.store.append_event(
            mission_id,
            "ontology_execution_contract_updated",
            Role.QUEEN.value,
            {
                "artifact_id": artifact.artifact_id,
                "coverage_gate": contract.get("coverage", {}).get("gate"),
                "decision_gate": contract.get("decision_gate"),
                "missing_slot_count": len(contract.get("coverage", {}).get("missing_slot_ids") or []),
                "evidence_authority": "opencrab_mcp_receipt_only",
            },
        )
        return contract

    def _persist_ontology_ledger(
        self,
        mission_id: str,
        task_id: str,
        context: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Promote QUEEN's contract, persist it, and require readback."""
        if not self.goal_plan.get("ontology_required"):
            return {}
        contract = self.ontology_contract if isinstance(self.ontology_contract, dict) else {}
        graph_id = str(self.goal_graph.get("graph_id") or contract.get("goal_graph_id") or "")
        revision = int(contract.get("revision") or 0)
        if not graph_id or revision < 1:
            raise RuntimeError("ontology ledger promotion requires goal_graph_id and contract revision")
        existing = self.store.get_latest_ontology_ledger(
            mission_id,
            goal_graph_id=graph_id,
            revision=revision,
        )
        if existing is not None:
            if existing.get("execution_contract") != contract:
                raise RuntimeError("ontology ledger revision already exists with a different execution contract")
            return existing
        contract_artifacts = [
            row
            for row in self.store.inspect(mission_id).get("artifacts") or []
            if str(row.get("kind") or "") == "ontology_execution_contract"
        ]
        source_artifact_id = str(contract_artifacts[-1].get("artifact_id") or "") if contract_artifacts else ""
        queen_handoff = self._queen_handoff_quality(context or [])
        ledger = promote_execution_contract_to_ledger(
            contract,
            mission_id=mission_id,
            revision=revision,
            source_artifact_id=source_artifact_id,
            observed_receipt=self.opencrab_receipt,
            queen_handoff=queen_handoff,
            promoted_at=utc_now(),
        )
        persisted = self.store.persist_ontology_ledger(
            mission_id,
            ledger,
            task_id=task_id,
        )
        readback = persisted.get("readback") if isinstance(persisted, dict) else None
        if not isinstance(readback, dict) or not readback.get("readback", {}).get("validated"):
            raise RuntimeError("ontology ledger readback validation failed")
        artifact_id = str(readback.get("_artifact", {}).get("artifact_id") or "")
        source_uri = "crab://ontology-ledger/%s" % artifact_id
        if artifact_id and not any(
            str(row.get("artifact_id") or "") == artifact_id
            or str(row.get("source_uri") or "") == source_uri
            for row in self.store.inspect(mission_id).get("evidence") or []
        ):
            self.store.add_evidence(
                EvidenceRef(
                    evidence_id=new_id("ontology-ledger"),
                    mission_id=mission_id,
                    artifact_id=artifact_id,
                    source_type="local_ontology_ledger",
                    source_uri=source_uri,
                    digest=str(readback.get("_artifact", {}).get("sha256") or ""),
                )
            )
        self.store.append_event(
            mission_id,
            "ontology_ledger_persisted",
            Role.QUEEN.value,
            {
                "artifact_id": artifact_id,
                "artifact_kind": ONTOLOGY_LEDGER_KIND,
                "mission_id": mission_id,
                "goal_graph_id": graph_id,
                "revision": revision,
                "readback_validated": True,
                "idempotent": bool(persisted.get("idempotent")) if isinstance(persisted, dict) else False,
            },
        )
        return readback

    def _refine_retrieval_from_king_plan(self, mission_id: str, task: Dict[str, Any]) -> None:
        """Let a model KING sharpen the next MCP query without adding facts."""
        if not self.king_plan.get("model_advisory") or not self.goal_plan.get("ontology_required"):
            return
        route = self.goal_plan.get("retrieval_contract")
        if not isinstance(route, dict):
            return
        hints = [
            str(value).strip()
            for value in (
                self.king_plan.get("subgoals") or [],
                self.king_plan.get("decision_slots") or [],
            )
            for value in (value if isinstance(value, list) else [value])
            if str(value).strip()
        ]
        hint = " ".join(hints[:8])[:420]
        if not hint:
            return
        original_query = str(route.get("primary_query") or self.goal_plan.get("objective") or "").strip()
        refined_query = "%s %s" % (original_query, hint)
        route = dict(route)
        route["primary_query"] = refined_query[:900]
        route["query_origin"] = "goal_plus_king_plan"
        route["king_plan_advisory"] = True
        self.goal_plan["retrieval_contract"] = route
        artifact = self.service._write_artifact(
            mission_id,
            task["task_id"],
            "goal_plan_refined.json",
            json.dumps(self.goal_plan, ensure_ascii=False, indent=2) + "\n",
            "goal_plan_refined",
        )
        self.store.append_event(
            mission_id,
            "retrieval_contract_refined",
            Role.KING.value,
            {
                "artifact_id": artifact.artifact_id,
                "query_origin": route["query_origin"],
                "query_chars": len(route["primary_query"]),
                "hint_count": len(hints[:8]),
                "scope_locked": True,
                "evidence_authority": "opencrab_mcp_receipt_only",
            },
        )

    def _materialize_king_subgoals(
        self,
        mission_id: str,
        king_task: Dict[str, Any],
        after_task: Dict[str, Any],
        task_rows: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Turn bounded KING subgoals into read-only Worker slots.

        This is deliberately a local fan-out. Each Worker projects a small
        slice of the already observed MCP receipt, so a strategic KING can
        divide ontology work without multiplying provider calls. Workspace
        writes and semantic synthesis remain serialized in their existing
        roles.
        """
        if self.goal_plan.get("ontology_required") and (
            not self.ontology_contract
            or (self.opencrab_receipt and not self.ontology_contract.get("observed_receipt"))
        ):
            # Keep direct unit/integration callers honest too: a King plan
            # cannot materialize workers without the same slot ledger used by
            # the normal run loop.
            self._refresh_ontology_contract(mission_id, str(king_task.get("task_id") or ""))
        if not (
            self.king_plan.get("model_advisory")
            and self.goal_plan.get("ontology_required")
            and self.goal_plan.get("action_mode") in {"plan", "research"}
            and not self.goal_plan.get("requires_write")
        ):
            return []
        if any(str(row.get("task_kind") or "") == "king_subgoal" for row in task_rows):
            return []
        candidates: List[str] = []
        for value in self.king_plan.get("subgoals") or []:
            clean = " ".join(str(value or "").split())[:240]
            if clean and clean.lower() not in {item.lower() for item in candidates}:
                candidates.append(clean)
        if not candidates:
            return []
        mission = self.store.mission(mission_id) or {}
        admitted = worker_capacity(
            str(mission.get("worker_policy") or WorkerPolicy.FIXED.value),
            int(mission.get("max_workers") or 1),
            min(len(candidates), 3),
        )
        if admitted <= 0:
            return []
        candidates = candidates[:admitted]
        before = next(
            (row for row in task_rows if str(row.get("role") or "") == Role.ORACLE.value),
            None,
        )
        if not before:
            return []
        start_position = int(before.get("position") or 1)
        tasks: List[TaskSlot] = []
        assignments: List[RoleAssignment] = []
        for index, subgoal in enumerate(candidates, start=1):
            subgoal_id = "subgoal-%s" % hashlib.sha256(
                (self.goal_graph.get("graph_id", mission_id) + ":" + subgoal).encode("utf-8")
            ).hexdigest()[:12]
            task = TaskSlot(
                task_id=new_id("task"),
                mission_id=mission_id,
                title="Project ontology subgoal: %s" % subgoal,
                role=Role.WORKER,
                position=start_position + index - 1,
                output_contract="worker_subgoal_%02d.json" % index,
                status=TaskStatus.PENDING,
                depends_on=[str(after_task.get("task_id") or "")],
                task_kind="king_subgoal",
                subgoal_id=subgoal_id,
                execution_mode="serial_read_only",
                write_scope="read_only",
            )
            tasks.append(task)
            assignments.append(
                RoleAssignment(
                    assignment_id=new_id("assignment"),
                    mission_id=mission_id,
                    task_id=task.task_id,
                    route=self.service.inverter.local(
                        Role.WORKER,
                        "read-only evidence projection for KING subgoal; no provider turn and no workspace write",
                    ),
                )
            )
        self.store.insert_tasks_before(mission_id, str(before["task_id"]), tasks, assignments)
        plan_payload = {
            "schema": "crab.king-subgoal-plan/v1",
            "authority": "KING advisory over deterministic goal graph",
            "goal_graph_id": self.goal_graph.get("graph_id"),
            "worker_policy": mission.get("worker_policy") or WorkerPolicy.FIXED.value,
            "configured_worker_limit": mission.get("max_workers"),
            "candidate_count": len(self.king_plan.get("subgoals") or []),
            "admitted_count": len(tasks),
            "execution_mode": "serial_read_only",
            "write_scope": "read_only",
            "subgoals": [
                {
                    "task_id": task.task_id,
                    "subgoal_id": task.subgoal_id,
                    "title": task.title,
                    "subgoal": candidates[index - 1],
                    "evidence_slot_ids": next(
                        (
                            list(item.get("slot_ids") or [])
                            for item in self.ontology_contract.get("king_subgoals") or []
                            if str(item.get("text") or "").strip().lower() == candidates[index - 1].strip().lower()
                        ),
                        [],
                    ),
                    "depends_on": task.depends_on,
                }
                for index, task in enumerate(tasks, start=1)
            ],
        }
        self.service._write_artifact(
            mission_id,
            king_task["task_id"],
            "subgoal_plan.json",
            json.dumps(plan_payload, ensure_ascii=False, indent=2) + "\n",
            "subgoal_plan",
        )
        self.store.append_event(
            mission_id,
            "king_subgoal_slots_materialized",
            Role.KING.value,
            {
                "candidate_count": plan_payload["candidate_count"],
                "admitted_count": plan_payload["admitted_count"],
                "worker_policy": plan_payload["worker_policy"],
                "configured_worker_limit": plan_payload["configured_worker_limit"],
                "execution_mode": plan_payload["execution_mode"],
                "write_scope": plan_payload["write_scope"],
            },
        )
        current = self.store.inspect(mission_id).get("tasks") or []
        task_ids = {task.task_id for task in tasks}
        return [row for row in current if str(row.get("task_id") or "") in task_ids]

    def _maybe_repair_queen_handoff(
        self,
        mission_id: str,
        objective: str,
        context: Optional[List[str]],
    ) -> Dict[str, Any]:
        """Run at most one pre-write Queen repair when the handoff is malformed."""
        quality = self._queen_handoff_quality(context)
        limit = int(self.goal_plan.get("max_automatic_revision_cycles") or 0)
        if quality.get("accepted") or not self.goal_plan.get("requires_model_queen"):
            return {"attempted": False, "reason": "handoff_ok_or_local_queen", "quality": quality}
        if self.automatic_revision_cycles >= limit:
            return {"attempted": False, "reason": "revision_limit_reached", "quality": quality}
        # A malformed turn that already consumed a large uncached input is a
        # provider-side cost signal, not an invitation to spend another turn.
        # Preserve the failed receipt and let the user retry deliberately.
        if self.observed_billable_input_tokens is not None and self.observed_billable_input_tokens >= 5500:
            return {"attempted": False, "reason": "repair_budget_guard", "quality": quality}
        snapshot = self.store.inspect(mission_id)
        if self.goal_plan.get("requires_write"):
            changed = []
            for row in snapshot.get("artifacts") or []:
                if str(row.get("kind") or "") != "workspace_change":
                    continue
                try:
                    payload = json.loads(Path(str(row.get("path") or "")).read_text(encoding="utf-8"))
                except (OSError, TypeError, ValueError):
                    payload = {}
                if int(payload.get("changed_file_count") or 0) > 0:
                    changed.append(payload)
            if changed:
                return {"attempted": False, "reason": "workspace_change_already_observed", "quality": quality}
        queen_task = next(
            (row for row in snapshot.get("tasks") or [] if str(row.get("role") or "") == Role.QUEEN.value),
            None,
        )
        assignment = next(
            (row for row in snapshot.get("assignments") or [] if row.get("task_id") == (queen_task or {}).get("task_id")),
            None,
        )
        if not queen_task or not assignment:
            return {"attempted": False, "reason": "queen_assignment_missing", "quality": quality}
        try:
            self._enforce_token_gate(mission_id, Role.QUEEN, str(queen_task["task_id"]))
            self._repair_mode = "queen_handoff"
            self.automatic_revision_cycles += 1
            repair_task = dict(queen_task)
            repair_task["output_contract"] = "ontology_context_repair_%d.json" % self.automatic_revision_cycles
            result = self._run_codex_role(mission_id, repair_task, assignment, objective, context or [])
            repaired = self._normalize_queen_interpretation(result.text or "")
            if context is not None:
                context.append("QUEEN INTERPRETATION:\n%s" % repaired[-5000:])
            self._refresh_ontology_contract(
                mission_id,
                str(repair_task["task_id"]),
                decision_text=repaired,
            )
            self.store.append_event(
                mission_id,
                "queen_handoff_repaired",
                Role.QUEEN.value,
                {
                    "cycle": self.automatic_revision_cycles,
                    "previous_quality": quality,
                    "quality": self._queen_handoff_quality(context),
                    "bounded": True,
                },
            )
            return {
                "attempted": True,
                "reason": "bounded_queen_repair",
                "quality": self._queen_handoff_quality(context),
            }
        except Exception as exc:
            self.store.append_event(
                mission_id,
                "queen_handoff_repair_skipped",
                Role.SOLDIER.value,
                {"reason": str(exc), "cycle": self.automatic_revision_cycles, "bounded": True},
            )
            return {"attempted": True, "reason": "repair_failed", "error": str(exc), "quality": quality}
        finally:
            self._repair_mode = ""

    def _publish_goal_outcome(self, mission_id: str, objective: str, failure: str = "") -> str:
        """Publish one user-facing outcome contract after every colony run.

        Role artifacts remain detailed internal receipts. This compact artifact
        is the stable handoff for the panel, mobile clients and benchmark
        harness: it says what was accepted, which evidence was observed, what
        the kinetic path did, and what the next action is. It never promotes a
        blocked or failed mission to an accepted conclusion.
        """
        snapshot = self.store.inspect(mission_id)
        existing = next(
            (row for row in snapshot.get("artifacts") or [] if str(row.get("kind") or "") == "goal_outcome"),
            None,
        )
        if existing:
            return str(existing.get("artifact_id") or "")
        mission = snapshot.get("mission") or {}
        status = str(mission.get("status") or "unknown")
        accepted = status == MissionStatus.COMPLETED.value and bool(mission.get("oracle_result_artifact_id"))
        verdict = "accepted" if accepted else "cancelled" if status == MissionStatus.CANCELLED.value else "blocked" if failure.startswith(("OpenCrab", "Oracle ontology", "OpenCrab graph")) else "failed"
        artifacts = [row for row in snapshot.get("artifacts") or [] if isinstance(row, dict)]
        oracle_rows = [row for row in artifacts if str(row.get("kind") or "") in {"oracle_result", "oracle_blocked"}]
        conclusion = ""
        next_action = ""
        for row in reversed(oracle_rows):
            payload = self._artifact_payload(row)
            conclusion = str(payload.get("final_conclusion") or payload.get("interpretation") or "").strip()
            next_action = str(payload.get("next_action") or "").strip()
            if not conclusion and str(row.get("path") or ""):
                try:
                    raw = Path(str(row["path"])).read_text(encoding="utf-8").strip()
                except OSError:
                    raw = ""
                if raw and not raw.startswith("{"):
                    conclusion = raw
            if conclusion or next_action:
                break
        if not next_action:
            next_action = self._outcome_next_action(conclusion, accepted=accepted, failure=failure)
        receipt = self.opencrab_receipt if isinstance(self.opencrab_receipt, dict) else {}
        scope = receipt.get("pack_scope") if isinstance(receipt.get("pack_scope"), dict) else {}
        if not scope:
            scope = receipt.get("scope_resolution") if isinstance(receipt.get("scope_resolution"), dict) else {}
        if not scope:
            scope = {
                "scope_mode": "selected" if self.ontology_context.get("package_ids") else "none",
                "selected_package_count": len(self.ontology_context.get("package_ids") or []),
                "selected_project_count": len(self.ontology_context.get("project_ids") or []),
            }
        receipts = [row for row in snapshot.get("tool_receipts") or [] if isinstance(row, dict)]
        model_turns = sum(1 for row in receipts if str(row.get("tool_name") or "") == "codex.app-server.turn" and row.get("status") == "success")
        mcp_calls = sum(1 for row in receipts if str(row.get("tool_name") or "").startswith("opencrab.mcp."))
        local_gates = sum(
            1
            for row in receipts
            if str(row.get("tool_name") or "") in {"crab.role_gate", "crab.soldier.local_patrol", "crab.worker.subgoal"}
        )
        budget = snapshot.get("budget") or {}
        task_rows = [
            {
                "role": row.get("role"),
                "status": row.get("status"),
                "title": row.get("title"),
                "task_kind": row.get("task_kind"),
                "subgoal_id": row.get("subgoal_id") or None,
                "execution_mode": row.get("execution_mode"),
                "write_scope": row.get("write_scope"),
            }
            for row in snapshot.get("tasks") or []
        ]
        remote_evidence = [
            str(row.get("id"))
            for row in receipt.get("evidence") or []
            if isinstance(row, dict) and row.get("id")
        ][:12]
        plan = snapshot.get("goal_plan") if isinstance(snapshot.get("goal_plan"), dict) else self.goal_plan
        ontology_contract = self.ontology_contract or snapshot.get("ontology_execution_contract") or {}
        contract_coverage = ontology_contract.get("coverage") if isinstance(ontology_contract.get("coverage"), dict) else {}
        payload = {
            "schema": "crab.goal-outcome/v1",
            "mission_id": mission_id,
            "retry_of": self.retry_of or None,
            "objective": objective,
            "status": status,
            "verdict": verdict,
            "accepted": accepted,
            "workflow": {
                "kind": plan.get("kind"),
                "ontology_mode": plan.get("ontology_mode"),
                "goal_graph_id": self.goal_graph.get("graph_id") or plan.get("goal_graph_id"),
                "stages": plan.get("stages") or [],
                "tasks": task_rows,
                "decision_slots": self.goal_graph.get("decision_slots") or [],
                "ontology_contract": {
                    "schema": ontology_contract.get("schema"),
                    "coverage_gate": contract_coverage.get("gate"),
                    "coverage_mode": contract_coverage.get("mode"),
                    "observation_gate": contract_coverage.get("observation_gate"),
                    "required_slot_count": contract_coverage.get("required_slot_count", 0),
                    "filled_required_slot_count": contract_coverage.get("filled_required_slot_count", 0),
                    "missing_slot_ids": contract_coverage.get("missing_slot_ids") or [],
                    "decision_gate": ontology_contract.get("decision_gate"),
                },
                "verification_loop": self.goal_graph.get("verification_loop") or {},
                "retrieval_query_origin": (
                    (plan.get("retrieval_contract") or {}).get("query_origin")
                    if isinstance(plan.get("retrieval_contract"), dict)
                    else None
                ),
                "king_plan": {
                    "present": bool(self.king_plan),
                    "source": self.king_plan.get("source"),
                    "next_action": self.king_plan.get("next_action"),
                    "subgoal_count": len(self.king_plan.get("subgoals") or []),
                },
                "subgoal_workers": {
                    "count": sum(1 for row in task_rows if row.get("task_kind") == "king_subgoal"),
                    "execution_mode": "serial_read_only",
                    "write_scope": "read_only",
                },
            },
            "scope": {
                "mode": scope.get("scope_mode") or scope.get("mode") or "unknown",
                "pack_count": scope.get("selected_package_count", len(self.ontology_context.get("package_ids") or [])),
                "project_count": scope.get("selected_project_count", len(self.ontology_context.get("project_ids") or [])),
                "titles": list(scope.get("selected_titles") or [])[:12],
            },
            "evidence": {
                "count": int(receipt.get("evidence_count") or len(receipt.get("evidence") or []) or len(snapshot.get("evidence") or [])),
                "ids": remote_evidence,
                "claim_gate": receipt.get("claim_gate", "not_required"),
                "graph_gate": receipt.get("graph_gate", "not_required"),
            },
            "observed_items": {
                "count": int(receipt.get("observed_item_count") or len(receipt.get("observed_items") or [])),
                "observation_gate": receipt.get("observation_gate", "not_required"),
                "items": [
                    {
                        "id": row.get("id") or row.get("package_id") or row.get("project_id"),
                        "title": row.get("title"),
                        "type": row.get("type"),
                        "source": row.get("source"),
                        "package_id": row.get("package_id"),
                        "project_id": row.get("project_id"),
                    }
                    for row in (receipt.get("observed_items") or [])[:24]
                    if isinstance(row, dict)
                ],
            },
            "execution": {
                "model_turns": model_turns,
                "local_gates": local_gates,
                "mcp_calls": mcp_calls,
                "planned_model_turns": int(plan.get("estimated_model_turns") or 0),
                "automatic_revision_cycles": self.automatic_revision_cycles,
                "automatic_revision_limit": int(plan.get("max_automatic_revision_cycles") or 0),
                "subgoal_worker_receipts": sum(1 for row in receipts if str(row.get("tool_name") or "") == "crab.worker.subgoal"),
                "tokens_observed": budget.get("tokens_observed", self.observed_tokens),
                "billable_input_tokens_observed": self.observed_billable_input_tokens,
                "cache_hit": bool(receipt.get("cache_hit")),
            },
            "conclusion": conclusion[-4000:],
            "next_action": next_action,
            "failure": failure or None,
        }
        task_id = str((snapshot.get("tasks") or [{}])[-1].get("task_id") or "")
        if not task_id:
            return ""
        artifact = self.service._write_artifact(
            mission_id,
            task_id,
            "goal_outcome.json",
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            "goal_outcome",
        )
        if accepted:
            self.store.set_artifact_status(artifact.artifact_id, ArtifactStatus.ACCEPTED, Role.ORACLE)
        self.store.append_event(
            mission_id,
            "goal_outcome_published",
            Role.ORACLE.value,
            {"artifact_id": artifact.artifact_id, "verdict": verdict, "accepted": accepted, "model_turns": model_turns},
        )
        headline = "ORACLE RESULT" if accepted else "ORACLE BLOCKED" if verdict == "blocked" else "MISSION %s" % verdict.upper()
        evidence_count = payload["evidence"]["count"]
        next_line = "" if next_action.strip().upper() == "STOP" else "\n\n다음: %s" % next_action
        self.store.add_message(
            self.session_id,
            "assistant",
            "%s\n\n목표: %s\n근거 %s개 · 모델 턴 %s · 로컬 게이트 %s\n\n%s%s"
            % (
                headline,
                objective[:240],
                evidence_count,
                model_turns,
                local_gates,
                conclusion[-1800:] or "결론 텍스트는 없고 영수증만 보존되었습니다.",
                next_line,
            ),
            mission_id,
            {
                "role": Role.ORACLE.value,
                "status": verdict,
                "artifact_id": artifact.artifact_id,
                "goal_outcome": True,
                "user_facing": True,
            },
        )
        return artifact.artifact_id

    def _record_blocked_oracle_outcome(
        self,
        mission_id: str,
        objective: str,
        task: Optional[Dict[str, Any]],
        reason: str,
    ) -> None:
        """Persist an honest local conclusion when evidence gates stop the mission.

        This is deliberately not an Oracle acceptance: no model conclusion is
        promoted and ``oracle_result_artifact_id`` remains empty. The artifact
        gives the panel a useful next action while preserving the observed
        model-call count of the gate.
        """
        snapshot = self.store.inspect(mission_id)
        if any(str(row.get("kind") or "") == "oracle_blocked" for row in snapshot.get("artifacts") or []):
            return
        task_id = str((task or {}).get("task_id") or "oracle-blocked")
        receipt = self.opencrab_receipt or {}
        status = str(receipt.get("status") or "unknown")
        evidence_count = int(receipt.get("evidence_count") or 0)
        graph_gate = str(receipt.get("graph_gate") or "not_required")
        if "graph gate" in reason.lower() or graph_gate == "weak":
            next_action = "Resolve OpenCrab graph endpoint labels and evidence-backed relation types, then retry the graph mission."
        elif status in {"error", "unknown"}:
            next_action = "Reconnect the OpenCrab MCP and retry the mission after authoritative evidence is available."
        else:
            next_action = "Ingest or select a source-backed OpenCrab pack, then retry the mission."
        partial_graph = {
            "graph_gate": graph_gate,
            "quality": receipt.get("quality") if isinstance(receipt.get("quality"), dict) else {},
            "paths": (receipt.get("paths") or [])[:12] if isinstance(receipt.get("paths"), list) else [],
            "nodes": (receipt.get("nodes") or [])[:12] if isinstance(receipt.get("nodes"), list) else [],
            "edges": (receipt.get("edges") or [])[:24] if isinstance(receipt.get("edges"), list) else [],
            "accepted": False,
        }
        model_invocation = self.observed_model_turns > 0
        payload = {
            "schema": "crab.oracle-blocked-result/v1",
            "verdict": "blocked",
            "accepted": False,
            "model_invocation": model_invocation,
            "objective": objective,
            "reason": reason,
            "opencrab_status": status,
            "evidence_count": evidence_count,
            "claim_gate": receipt.get("claim_gate", "blocked"),
            "graph_gate": receipt.get("graph_gate", "not_required"),
            "partial_graph": partial_graph if graph_gate != "not_required" else None,
            "context_artifact_id": receipt.get("artifact_id"),
            "next_action": next_action,
        }
        artifact = self.service._write_artifact(
            mission_id,
            task_id,
            "oracle_blocked.json",
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            "oracle_blocked",
        )
        attempt_id = next(
            (
                str(row.get("attempt_id"))
                for row in snapshot.get("attempts") or []
                if str(row.get("task_id") or "") == task_id and row.get("attempt_id")
            ),
            None,
        )
        if attempt_id:
            self.store.add_tool_receipt(
                mission_id,
                task_id,
                attempt_id,
                "crab.oracle.local_block",
                "success",
                {**payload, "artifact_id": artifact.artifact_id},
            )
        self.store.append_event(
            mission_id,
            "oracle_blocked_result",
            Role.ORACLE.value,
            {"artifact_id": artifact.artifact_id, "reason": reason, "model_invocation": model_invocation},
        )
        partial_note = ""
        if graph_gate != "not_required":
            path_count = len(partial_graph.get("paths") or [])
            partial_note = " 관측된 그래프 경로 %d개는 부분 결과로 보존했지만 의미 게이트를 통과하지 못했습니다." % path_count
        self.store.add_message(
            self.session_id,
            "assistant",
            "오라클 차단: 권위 있는 오픈크랩 근거가 없어 결론을 확정하지 않았습니다.%s %s" % (partial_note, next_action),
            mission_id,
            {
                "role": Role.ORACLE.value,
                "status": "blocked",
                "artifact_id": artifact.artifact_id,
                "user_facing": True,
            },
        )

    def run(self, objective: str, max_workers: int = 3, worker_policy: str = WorkerPolicy.FIXED.value) -> Dict[str, Any]:
        planned = self.service.plan_mission(
            objective,
            max_workers=max_workers,
            worker_policy=worker_policy,
            session_id=self.session_id,
            adaptive=True,
        )
        mission_id = str(planned["mission"]["mission_id"])
        self.goal_plan = dict(planned.get("goal_plan") or {})
        self.goal_graph = dict(planned.get("goal_graph") or compile_goal_graph(self.goal_plan))
        self.kinetic_workflow = dict(planned.get("kinetic_workflow") or {})
        self.kinetic_trace = []
        self.ontology_contract = dict(planned.get("ontology_execution_contract") or {})
        if self.retry_of:
            self.store.append_event(
                mission_id,
                "mission_retry_started",
                Role.KING.value,
                {"retry_of": self.retry_of, "objective": objective},
            )
        self.store.update_session(self.session_id, status="running", active_mission_id=mission_id)
        self.store.add_message(self.session_id, "user", objective, mission_id)
        self.store.transition_mission(mission_id, MissionStatus.RUNNING, Role.KING)
        context: List[str] = []
        active_task: Optional[Dict[str, Any]] = None
        try:
            snapshot = self.store.inspect(mission_id)
            assignments = {row["task_id"]: row for row in snapshot["assignments"]}
            model_required = any(str(row.get("provider") or "") != "local" for row in assignments.values())
            session = self.store.session(self.session_id) or {}
            model_policy = str(session.get("model_policy") or "auto")
            executor_policy = str(session.get("executor_policy") or "codex")
            if model_required and model_policy == "host" and executor_policy != "host":
                self.store.append_event(
                    mission_id,
                    "host_model_execution_required",
                    "RUNTIME",
                    {
                        "session_id": self.session_id,
                        "model_policy": model_policy,
                        "executor_policy": executor_policy,
                        "reason": "caller-selected host model requires executor_policy=host; Codex fallback is disabled",
                    },
                )
                raise RuntimeError(
                    "HOST_MODEL_EXECUTOR_UNAVAILABLE: model_policy=host requires executor_policy=host"
                )
            if model_required:
                thread_id = self.bridge.start()
                self.store.update_session(self.session_id, codex_thread_id=thread_id)
                bridge_executor = str(getattr(self.bridge, "executor_name", "codex_app_server"))
                thread_event_prefix = "host_model_thread" if bridge_executor == "host_current_model" else "codex_thread"
                self.store.append_event(
                    mission_id,
                    "%s_%s" % (thread_event_prefix, self.bridge.last_start_mode),
                    "RUNTIME",
                    {"session_id": self.session_id, "thread_id": thread_id, "executor": bridge_executor},
                )
            else:
                self.store.append_event(
                    mission_id,
                    "codex_thread_not_required",
                    "RUNTIME",
                    {"session_id": self.session_id, "reason": "all assigned roles use deterministic local gates"},
                )
            tasks = list(snapshot["tasks"])
            sequence = 0
            task_index = 0
            while task_index < len(tasks):
                sequence += 1
                task = tasks[task_index]
                active_task = task
                role = Role(task["role"])
                assignment = assignments[task["task_id"]]
                if role is Role.WORKER and self.goal_plan.get("requires_write"):
                    self._worker_baseline = capture_workspace(self.service.workspace)
                if self.cancel_event.is_set():
                    self.store.cancel_active_mission(mission_id, "user_interrupt")
                    break
                if role is Role.ORACLE:
                    self.store.transition_mission(mission_id, MissionStatus.VERIFYING, Role.ORACLE)
                if role is Role.SOLDIER and str(assignment.get("provider") or "") == "local":
                    artifact_id = self._run_soldier(mission_id, task, context)
                    context.append("SOLDIER receipt: %s" % artifact_id)
                elif str(assignment.get("provider") or "") == "local":
                    content = self._run_local_contract(
                        mission_id,
                        task,
                        objective,
                        reason=str(assignment.get("reason") or "deterministic local gate"),
                        context=context,
                    )
                    if role is Role.KING:
                        king_plan = self._capture_king_plan(
                            mission_id,
                            task,
                            content,
                            source="local_goal_compiler",
                        )
                        self._refine_retrieval_from_king_plan(mission_id, task)
                        self._record_kinetic_transition(
                            mission_id,
                            task,
                            ["goal_bind", "scope_lock"],
                            "completed",
                            detail={"source": "local_goal_compiler", "subgoal_count": len(king_plan.get("subgoals") or [])},
                        )
                        context.append("KING PLAN:\n%s" % compact_king_plan(king_plan, 1800))
                    else:
                        context.append("%s:\n%s" % (role.value, content))
                        if role is Role.QUEEN:
                            self._refresh_ontology_contract(
                                mission_id,
                                task["task_id"],
                                decision_text=content,
                            )
                elif role is Role.SOLDIER and not self._requires_model(role, objective):
                    artifact_id = self._run_soldier(mission_id, task, context)
                    context.append("SOLDIER receipt: %s" % artifact_id)
                else:
                    if role is Role.ORACLE:
                        self._enforce_ontology_contract_gate(mission_id)
                    self._enforce_token_gate(mission_id, role, task["task_id"])
                    result = self._run_codex_role(mission_id, task, assignment, objective, context)
                    if role is Role.KING:
                        king_plan = self._capture_king_plan(
                            mission_id,
                            task,
                            result.text or "",
                            source="codex_turn",
                        )
                        self._refine_retrieval_from_king_plan(mission_id, task)
                        self._record_kinetic_transition(
                            mission_id,
                            task,
                            ["goal_bind", "scope_lock"],
                            "completed",
                            detail={"source": "codex_turn", "subgoal_count": len(king_plan.get("subgoals") or [])},
                        )
                        context.append("KING PLAN:\n%s" % compact_king_plan(king_plan, 1800))
                    elif role is Role.QUEEN and self.opencrab_receipt:
                        queen_text = self._normalize_queen_interpretation(result.text)
                        context.append(
                            "OPENCRAB MCP CONTEXT RECEIPT:\n%s\n\nQUEEN INTERPRETATION:\n%s"
                            % (
                                self._compact_opencrab_receipt(),
                                queen_text[-3500:],
                            )
                        )
                        self._refresh_ontology_contract(
                            mission_id,
                            task["task_id"],
                            decision_text=queen_text,
                        )
                    else:
                        context.append("%s:\n%s" % (role.value, result.text[-5000:]))
                if role is Role.QUEEN and self.goal_plan.get("ontology_required"):
                    ledger = self._persist_ontology_ledger(
                        mission_id,
                        task["task_id"],
                        context,
                    )
                    context.append(
                        "ONTOLOGY LEDGER READBACK:\n%s"
                        % json.dumps(
                            {
                                "artifact_id": ledger.get("_artifact", {}).get("artifact_id"),
                                "mission_id": ledger.get("mission_id"),
                                "goal_graph_id": ledger.get("goal_graph_id"),
                                "revision": ledger.get("revision"),
                                "validated": ledger.get("readback", {}).get("validated"),
                            },
                            ensure_ascii=False,
                        )
                    )
                    self._record_kinetic_transition(
                        mission_id,
                        task,
                        ["persist_ontology_ledger"],
                        "completed",
                        detail={
                            "artifact_id": ledger.get("_artifact", {}).get("artifact_id"),
                            "revision": ledger.get("revision"),
                            "readback_validated": ledger.get("readback", {}).get("validated"),
                        },
                    )
                    decision_gate = str(self.ontology_contract.get("decision_gate") or "blocked")
                    self._record_kinetic_transition(
                        mission_id,
                        task,
                        ["decide_from_ontology"],
                        "completed" if decision_gate == "pass" else "blocked",
                        detail={"decision_gate": decision_gate},
                    )
                if role is Role.WORKER:
                    if self.goal_plan.get("requires_write"):
                        change_artifact = self._record_workspace_change(mission_id, task)
                        context.append("WORKSPACE CHANGE RECEIPT: %s" % change_artifact)
                        self._record_kinetic_transition(
                            mission_id,
                            task,
                            ["execute_action"],
                            "completed",
                            detail={"workspace_change_artifact_id": change_artifact},
                        )
                    elif self.goal_plan.get("requires_worker_output"):
                        self._record_kinetic_transition(
                            mission_id,
                            task,
                            ["produce_deliverable"],
                            "completed",
                            detail={"requires_write": False, "output_contract": task.get("output_contract")},
                        )
                if role is Role.SOLDIER:
                    soldier_snapshot = self.store.inspect(mission_id)
                    soldier_task_state = next(
                        (
                            row.get("status")
                            for row in soldier_snapshot.get("tasks") or []
                            if str(row.get("task_id") or "") == str(task.get("task_id") or "")
                        ),
                        "failed",
                    )
                    if self.goal_plan.get("external_scouting"):
                        self._record_kinetic_transition(
                            mission_id,
                            task,
                            ["scout_external"],
                            "completed" if soldier_task_state == TaskStatus.COMPLETED.value else "blocked",
                            detail={"task_status": soldier_task_state},
                        )
                    self._record_kinetic_transition(
                        mission_id,
                        task,
                        ["patrol_quality"],
                        "completed" if soldier_task_state == TaskStatus.COMPLETED.value else "blocked",
                        detail={"task_status": soldier_task_state},
                    )
                    dynamic_tasks = self._materialize_king_subgoals(
                        mission_id,
                        next(
                            (row for row in tasks if str(row.get("role") or "") == Role.KING.value),
                            {},
                        ),
                        task,
                        tasks,
                    )
                    if dynamic_tasks:
                        oracle_index = next(
                            (index for index in range(task_index + 1, len(tasks))
                             if str(tasks[index].get("role") or "") == Role.ORACLE.value),
                            len(tasks),
                        )
                        tasks[oracle_index:oracle_index] = dynamic_tasks
                        assignments.update({row["task_id"]: row for row in self.store.inspect(mission_id)["assignments"]})
                        context.append(
                            "KING SUBGOAL WORKERS:\n%s"
                            % "\n".join(
                                "- %s (%s)" % (row.get("title"), row.get("subgoal_id"))
                                for row in dynamic_tasks
                            )
                        )
                if role is Role.ORACLE:
                    oracle_snapshot = self.store.inspect(mission_id)
                    oracle_task_state = next(
                        (
                            row.get("status")
                            for row in oracle_snapshot.get("tasks") or []
                            if str(row.get("task_id") or "") == str(task.get("task_id") or "")
                        ),
                        "failed",
                    )
                    self._record_kinetic_transition(
                        mission_id,
                        task,
                        ["verify_result"],
                        "completed" if oracle_task_state == TaskStatus.VERIFIED.value else "blocked",
                        detail={"task_status": oracle_task_state},
                    )
                mission_after_role = self.store.inspect(mission_id)["mission"]["status"]
                if mission_after_role in {MissionStatus.CANCELLED.value, MissionStatus.FAILED.value}:
                    self.store.add_checkpoint(
                        Checkpoint(
                            checkpoint_id=new_id("checkpoint"),
                            mission_id=mission_id,
                            sequence=sequence,
                            state={"role": role.value, "task_id": task["task_id"], "stopped_by": role.value},
                        )
                    )
                    break
                self.store.add_checkpoint(
                    Checkpoint(
                        checkpoint_id=new_id("checkpoint"),
                        mission_id=mission_id,
                        sequence=sequence,
                        state={"role": role.value, "task_id": task["task_id"], "cancelled": self.cancel_event.is_set()},
                    )
                )
                task_index += 1
            current = self.store.inspect(mission_id)["mission"]
            if current["status"] == MissionStatus.VERIFYING.value:
                oracle_snapshot = self.store.inspect(mission_id)
                if oracle_snapshot["mission"].get("oracle_result_artifact_id"):
                    self.store.transition_mission(mission_id, MissionStatus.COMPLETED, Role.ORACLE)
                else:
                    self.store.transition_mission(mission_id, MissionStatus.FAILED, Role.ORACLE)
                    self.store.add_message(
                        self.session_id,
                        "system",
                        "Mission stopped: Oracle could not verify a final artifact.",
                        mission_id,
                        {"error": "oracle_gate_rejected"},
                    )
                self.store.update_session(self.session_id, status="ready", active_mission_id=None, codex_thread_id=str(getattr(self.bridge, "thread_id", "") or ""))
            elif current["status"] == MissionStatus.CANCELLED.value:
                self.store.update_session(self.session_id, status="ready", active_mission_id=None, codex_thread_id=str(getattr(self.bridge, "thread_id", "") or ""))
            self.store.set_budget(
                Budget(
                    mission_id=mission_id,
                    currency="USD",
                    token_limit=self.goal_plan.get("token_budget") or None,
                    tokens_observed=self.observed_tokens,
                    cost_observed=None,
                    observation_source=(
                        str(getattr(self.bridge, "observation_source", "codex_app_server"))
                        if self.observed_tokens is not None
                        else "not_observed"
                    ),
                )
            )
            self._persist_kinetic_state(
                mission_id,
                str((active_task or {}).get("task_id") or (tasks[-1].get("task_id") if tasks else "")),
            )
            self._publish_goal_outcome(mission_id, objective)
        except Exception as exc:
            current = self.store.inspect(mission_id)["mission"]
            if current["status"] in {MissionStatus.RUNNING.value, MissionStatus.VERIFYING.value}:
                self.store.transition_mission(mission_id, MissionStatus.FAILED, Role.ORACLE)
            reason = str(exc)
            if reason.startswith("OpenCrab MCP") or reason.startswith("OpenCrab graph gate") or reason.startswith("Oracle ontology gate"):
                self._record_blocked_oracle_outcome(mission_id, objective, active_task, reason)
            self.store.add_message(self.session_id, "system", "Mission failed: %s" % exc, mission_id, {"error": type(exc).__name__})
            self.store.update_session(self.session_id, status="ready", active_mission_id=None, codex_thread_id=str(getattr(self.bridge, "thread_id", "") or ""))
            self._persist_kinetic_state(
                mission_id,
                str((active_task or {}).get("task_id") or ""),
            )
            self._publish_goal_outcome(mission_id, objective, str(exc))
        return self.store.inspect(mission_id)

    @staticmethod
    def _requires_model(role: Role, objective: str) -> bool:
        text = objective.lower()
        if role is Role.KING:
            signals = ("architecture", "migration", "production", "security", "distributed", "refactor", "database", "설계", "마이그레이션", "배포", "보안", "리팩터")
            return len(objective) > 320 or any(signal in text for signal in signals)
        if role is Role.QUEEN:
            signals = ("ontology", "opencrab", "evidence", "knowledge graph", "schema", "rag", "mcp", "온톨로지", "오픈크랩", "근거", "지식 그래프", "스키마")
            return any(signal in text for signal in signals)
        return True

    def _enforce_token_gate(self, mission_id: str, role: Role, task_id: str) -> None:
        """Stop before a model turn once the uncached input target is spent."""
        limit = int(self.goal_plan.get("token_budget") or 0)
        spent = self.observed_billable_input_tokens
        basis = "uncached_input_tokens"
        if spent is None:
            spent = self.observed_tokens
            basis = "total_tokens_fallback"
        if limit <= 0 or spent is None or spent < limit:
            return
        payload = {
            "task_id": task_id,
            "role": role.value,
            "decision": "stop_before_model_call",
            "tokens_observed": self.observed_tokens,
            "budget_spent": spent,
            "budget_basis": basis,
            "token_limit": limit,
            "essential_role": True,
        }
        self.store.append_event(mission_id, "token_budget_gate", role.value, payload)
        raise RuntimeError(
            "token budget exhausted before %s: %s/%s %s" % (role.value, spent, limit, basis)
        )

    def _enforce_ontology_contract_gate(self, mission_id: str = "") -> None:
        """Refuse final synthesis when the goal's required slots are empty."""
        if not self.goal_plan.get("ontology_required"):
            return
        if mission_id:
            ledger = self.store.get_latest_ontology_ledger(
                mission_id,
                goal_graph_id=str(self.goal_graph.get("graph_id") or ""),
                revision=int(self.ontology_contract.get("revision") or 0) or None,
            )
            if ledger is None or not ledger.get("readback", {}).get("validated"):
                raise RuntimeError(
                    "Oracle ontology gate rejected: persisted/readback ontology ledger missing"
                )
            if ledger.get("execution_contract") != self.ontology_contract:
                raise RuntimeError(
                    "Oracle ontology gate rejected: persisted ontology ledger does not match execution contract"
                )
        coverage = self.ontology_contract.get("coverage") if isinstance(self.ontology_contract.get("coverage"), dict) else {}
        coverage_gate = str(coverage.get("gate") or "blocked")
        decision_gate = str(self.ontology_contract.get("decision_gate") or "blocked")
        if coverage_gate != "pass":
            missing = ", ".join(str(value) for value in coverage.get("missing_slot_ids") or []) or "unknown"
            raise RuntimeError("Oracle ontology slot gate rejected: missing required slots %s" % missing)
        if decision_gate != "pass":
            raise RuntimeError("Oracle decision slot gate rejected: Queen handoff is incomplete")

    @staticmethod
    def _extract_queen_interpretation(context: Optional[List[str]]) -> str:
        for item in reversed(context or []):
            value = str(item)
            if value.startswith("QUEEN:\n"):
                raw = value.split("\n", 1)[1].strip()
                try:
                    parsed = json.loads(raw)
                except (TypeError, ValueError):
                    parsed = {}
                if isinstance(parsed, dict) and str(parsed.get("interpretation") or "").strip():
                    return str(parsed["interpretation"]).strip()
                return raw
            marker = "QUEEN INTERPRETATION:\n"
            if marker in value:
                return value.split(marker, 1)[1].strip()
        return ""

    def _queen_handoff_quality(self, context: Optional[List[str]]) -> Dict[str, Any]:
        """Check that Queen output is an evidence-citing handoff, not prose noise."""
        interpretation = self._extract_queen_interpretation(context)
        evidence_ids = [
            str(row.get("id") or "")
            for row in self.opencrab_receipt.get("evidence") or []
            if isinstance(row, dict) and row.get("id")
        ]
        observed_item_ids = [
            str(row.get("id") or row.get("package_id") or row.get("project_id") or "")
            for row in self.opencrab_receipt.get("observed_items") or []
            if isinstance(row, dict) and (row.get("id") or row.get("package_id") or row.get("project_id"))
        ]
        metadata_only = (
            self.goal_plan.get("action_mode") == "lookup"
            and bool(observed_item_ids)
            and not evidence_ids
        )
        citation_ids = observed_item_ids if metadata_only else evidence_ids
        prefix_counts: Dict[str, int] = {}
        for value in citation_ids:
            if len(value) >= 8:
                prefix_counts[value[:8]] = prefix_counts.get(value[:8], 0) + 1
        cited_ids: List[str] = []
        citation_modes: Dict[str, str] = {}
        for value in citation_ids:
            if value in interpretation:
                cited_ids.append(value)
                citation_modes[value] = "item_id" if metadata_only else "full_id"
            elif len(value) >= 8 and prefix_counts.get(value[:8]) == 1 and value[:8] in interpretation:
                cited_ids.append(value)
                citation_modes[value] = "unique_item_id_prefix" if metadata_only else "unique_id_prefix"
        sections = [
            name
            for name in ("SELECTED_PATH", "SUPPORTED_CLAIMS", "GAPS", "NEXT_ACTION")
            if name in interpretation
        ]
        claim_section = ""
        if "SUPPORTED_CLAIMS" in interpretation:
            claim_section = interpretation.split("SUPPORTED_CLAIMS", 1)[1]
            for marker in ("GAPS", "NEXT_ACTION"):
                if marker in claim_section:
                    claim_section = claim_section.split(marker, 1)[0]
        claims_present = bool(" ".join(claim_section.split()).strip(" -:"))
        next_action = ""
        if "NEXT_ACTION" in interpretation:
            next_action = interpretation.split("NEXT_ACTION", 1)[1].strip().split("\n", 1)[0][:240]
        action_required = bool(self.goal_plan.get("action_required"))
        actionable_next_action = bool(next_action.strip()) and next_action.strip().upper() not in {"STOP", "NONE"}
        return {
            "present": bool(interpretation.strip()),
            "chars": len(interpretation),
            "section_count": len(sections),
            "sections": sections,
            "evidence_citation_count": len(cited_ids),
            "cited_evidence_ids": cited_ids[:8],
            "observed_item_citation_count": len(cited_ids) if metadata_only else 0,
            "cited_observed_item_ids": cited_ids[:8] if metadata_only else [],
            "metadata_only_lookup": metadata_only,
            "citation_modes": {key: citation_modes[key] for key in cited_ids[:8]},
            "accepted": (
                bool(interpretation.strip())
                and len(sections) >= 2
                and bool(cited_ids)
                and claims_present
                and (not action_required or actionable_next_action)
            ),
            "next_action_present": "NEXT_ACTION" in interpretation,
            "next_action": next_action,
            "claims_present": claims_present,
            "action_required": action_required,
            "actionable_next_action": actionable_next_action,
            "interpretation_preview": interpretation[-1200:],
        }

    def _normalize_queen_interpretation(self, content: str) -> str:
        """Keep internal coordination instructions out of answer-mode output."""
        value = str(content or "").strip()
        if self.goal_plan.get("action_required") or "NEXT_ACTION" not in value:
            return value
        prefix = value.split("NEXT_ACTION", 1)[0].rstrip()
        return "%s\n\nNEXT_ACTION\nSTOP" % prefix

    def _run_local_contract(
        self,
        mission_id: str,
        task: Dict[str, Any],
        objective: str,
        reason: str = "",
        context: Optional[List[str]] = None,
    ) -> str:
        role = Role(task["role"])
        if (
            role is Role.WORKER
            and str(task.get("task_kind") or "") == "king_subgoal"
            and self.goal_plan.get("ontology_required")
            and self.opencrab_receipt
            and not self.ontology_contract.get("observed_receipt")
        ):
            self._refresh_ontology_contract(mission_id, str(task.get("task_id") or ""))
        self.store.set_task_status(task["task_id"], TaskStatus.RUNNING, role)
        attempt = self.store.start_attempt(mission_id, task["task_id"], "crab.local_contract")
        if role is Role.QUEEN and self.goal_plan.get("ontology_required"):
            try:
                self._load_opencrab_context(mission_id, task, attempt["attempt_id"], objective)
            except Exception as exc:
                self.store.finish_attempt(
                    mission_id,
                    attempt["attempt_id"],
                    attempt["lease_id"],
                    AttemptStatus.FAILED,
                    str(exc),
                )
                raise
        if role is Role.KING:
            data = {
                "objective": objective,
                "outcome_type": self.goal_plan.get("outcome_type", "bounded_execution"),
                "workflow": self.goal_plan.get("stages") or ["KING", "WORKER", "ORACLE"],
                "ontology_mode": self.goal_plan.get("ontology_mode", "none"),
                "goal_graph_id": self.goal_graph.get("graph_id"),
                "decision_slots": self.goal_graph.get("decision_slots") or [],
                "acceptance_checks": self.goal_plan.get("acceptance_checks") or [],
                "stop_conditions": self.goal_plan.get("stop_conditions") or [],
                "risk": self.goal_plan.get("complexity", "bounded"),
                "estimated_model_turns": self.goal_plan.get("estimated_model_turns"),
                "token_budget": self.goal_plan.get("token_budget"),
                "model_gate": reason or "skipped_for_clear_low_complexity_mission",
                "verification_loop": self.goal_graph.get("verification_loop") or {},
            }
        elif role is Role.QUEEN:
            evidence_rows = _model_evidence(self.opencrab_receipt, limit=4)
            observed_items = [
                row
                for row in (self.opencrab_receipt.get("observed_items") or [])
                if isinstance(row, dict)
            ]
            route = self.goal_plan.get("retrieval_contract") if isinstance(self.goal_plan.get("retrieval_contract"), dict) else {}
            route_spaces = [str(value) for value in route.get("spaces") or [] if str(value).strip()]
            path_label = " -> ".join(route_spaces[:5]) or "resource -> evidence -> claim -> outcome"
            lines = [
                "SELECTED_PATH",
                "%s (%s)" % (path_label, self.goal_plan.get("ontology_mode") or "evidence_query"),
            ]
            if self.goal_plan.get("action_mode") == "lookup":
                lines.append("OBSERVED_ITEMS")
                if observed_items:
                    for row in observed_items[:12]:
                        item_id = row.get("id") or row.get("package_id") or row.get("project_id") or "unknown"
                        title = " ".join(str(row.get("title") or "(untitled)").split())[:180]
                        item_type = " ".join(str(row.get("type") or "item").split())[:60]
                        source = " ".join(str(row.get("source") or "").split())[:180]
                        lines.append(
                            "- %s [item_id: %s] [type: %s]%s"
                            % (
                                title,
                                item_id,
                                item_type,
                                " [source: %s]" % source if source else "",
                            )
                        )
                else:
                    lines.append("- none observed as typed rows")
            for row in evidence_rows[:8]:
                source = " ".join(str(row.get("source") or "").split())[:180]
                lines.append(
                    "- %s [evidence_id: %s]%s"
                    % (
                        " ".join(str(row.get("text") or "").split())[:240] or "(evidence text unavailable)",
                        row.get("id"),
                        " [source: %s]" % source if source else "",
                    )
                )
            lines.append("SOURCE_REFS")
            if observed_items and self.goal_plan.get("action_mode") == "lookup":
                for row in observed_items[:12]:
                    item_id = row.get("id") or row.get("package_id") or row.get("project_id") or "unknown"
                    source = " ".join(str(row.get("source") or "").split())[:180]
                    lines.append(
                        "- [item_id: %s]%s"
                        % (item_id, " [source: %s]" % source if source else "")
                    )
            for row in evidence_rows[:8]:
                source = " ".join(str(row.get("source") or "").split())[:180]
                lines.append(
                    "- [evidence_id: %s]%s"
                    % (row.get("id"), " [source: %s]" % source if source else "")
                )
            lines.append("SLOT_COVERAGE")
            for slot in self.ontology_contract.get("evidence_slots") or []:
                if not isinstance(slot, dict):
                    continue
                ids = ", ".join(str(value) for value in (slot.get("observed_evidence_ids") or [])[:3])
                lines.append(
                    "- %s: %s%s"
                    % (
                        slot.get("id"),
                        slot.get("status"),
                        " [evidence_id: %s]" % ids if ids else "",
                    )
                )
            lines.extend(
                [
                    "SUPPORTED_CLAIMS",
                    "- Observed evidence is listed with its source and stable ID; no unsupported semantic claim was generated.",
                    "GAPS",
                    "- Model synthesis was not requested; the bounded action is handed to WORKER after the evidence gate.",
                    "NEXT_ACTION",
                    (
                        "WORKER: Execute the bounded workspace change exactly as specified: %s"
                        % objective
                        if self.goal_plan.get("requires_write")
                        else "STOP"
                    ),
                ]
            )
            data = {
                "knowledge_policy": "evidence_before_claim",
                "ontology_required": True,
                "external_retrieval": False,
                "evidence_count": len(evidence_rows),
                "observed_item_count": len(observed_items),
                "observation_gate": self.opencrab_receipt.get("observation_gate", "not_required"),
                "authority": self.opencrab_receipt.get("authority"),
                "projection_mode": self.goal_plan.get("ontology_mode") or "evidence_query",
                "interpretation": "\n".join(lines),
                "model_gate": reason or "exact_lookup_projection",
            }
        elif role is Role.WORKER and str(task.get("task_kind") or "") == "king_subgoal":
            raw_title = str(task.get("title") or "")
            subgoal = raw_title.split(":", 1)[1].strip() if ":" in raw_title else raw_title
            evidence_rows = _model_evidence(self.opencrab_receipt, limit=12)
            subgoal_entry = next(
                (
                    item
                    for item in self.ontology_contract.get("king_subgoals") or []
                    if str(item.get("text") or "").strip().lower() == subgoal.strip().lower()
                ),
                {},
            )
            slot_ids = [str(value) for value in subgoal_entry.get("slot_ids") or [] if str(value).strip()]
            slot_rows = {
                str(slot.get("id")): slot
                for slot in self.ontology_contract.get("evidence_slots") or []
                if isinstance(slot, dict) and slot.get("id")
            }
            slot_evidence_ids = {
                str(evidence_id)
                for slot_id in slot_ids
                for evidence_id in slot_rows.get(slot_id, {}).get("observed_evidence_ids") or []
            }
            terms = [
                token.lower()
                for token in subgoal.replace(",", " ").split()
                if len(token) >= 2
            ][:8]
            matched = []
            for row in evidence_rows:
                if slot_evidence_ids and str(row.get("id") or "") in slot_evidence_ids:
                    matched.append(row)
                    continue
                haystack = " ".join(
                    str(row.get(key) or "") for key in ("text", "source", "title")
                ).lower()
                if terms and any(term in haystack for term in terms):
                    matched.append(row)
            selected = (matched or evidence_rows)[:4]
            data = {
                "schema": "crab.worker-subgoal-result/v1",
                "task_kind": "king_subgoal",
                "subgoal_id": task.get("subgoal_id"),
                "subgoal": subgoal,
                "evidence_slot_ids": slot_ids,
                "slot_coverage": [
                    {
                        "slot_id": slot_id,
                        "status": slot_rows.get(slot_id, {}).get("status", "missing"),
                        "evidence_ids": (slot_rows.get(slot_id, {}).get("observed_evidence_ids") or [])[:8],
                    }
                    for slot_id in slot_ids
                ],
                "projection": "observed_mcp_evidence_only",
                "authority": self.opencrab_receipt.get("authority"),
                "evidence_count": len(selected),
                "evidence": [
                    {
                        "id": row.get("id"),
                        "source": row.get("source"),
                        "text": " ".join(str(row.get("text") or "").split())[:360],
                    }
                    for row in selected
                ],
                "semantic_claims": [],
                "next_action": "Oracle가 이 투영을 다른 근거와 함께 검증",
                "model_invocation": False,
            }
        elif role is Role.SOLDIER:
            handoff_quality = self._queen_handoff_quality(context) if self.goal_plan.get("ontology_required") else {}
            data = {
                "waste_loops_detected": 0,
                "scope_check": "local_receipts_only",
                "external_scouting": False,
                "decision": "continue",
                "queen_handoff_quality": handoff_quality,
                "model_gate": reason or "local_patrol",
            }
        else:
            mission_snapshot = self.store.inspect(mission_id)
            if self.goal_plan.get("ontology_required"):
                queen_task_ids = {
                    str(row["task_id"])
                    for row in mission_snapshot.get("tasks") or []
                    if str(row.get("role") or "") == Role.QUEEN.value
                }
                queen_artifacts = [
                    row for row in mission_snapshot.get("artifacts") or []
                    if str(row.get("task_id") or "") in queen_task_ids
                ]
                mcp_receipts = [
                    row for row in mission_snapshot.get("tool_receipts") or []
                    if str(row.get("tool_name") or "").startswith("opencrab.mcp.") and str(row.get("status") or "") == "success"
                ]
                ledger_artifacts = [
                    row for row in mission_snapshot.get("artifacts") or []
                    if str(row.get("kind") or "") == ONTOLOGY_LEDGER_KIND
                ]
                ledger_readback = self.store.get_latest_ontology_ledger(
                    mission_id,
                    goal_graph_id=str(self.goal_graph.get("graph_id") or ""),
                    revision=int(self.ontology_contract.get("revision") or 0) or None,
                )
                ledger_ok = bool(
                    ledger_readback
                    and ledger_readback.get("readback", {}).get("validated")
                    and ledger_readback.get("execution_contract") == self.ontology_contract
                )
                subgoal_task_ids = {
                    str(row.get("task_id") or "")
                    for row in mission_snapshot.get("tasks") or []
                    if str(row.get("task_kind") or "") == "king_subgoal"
                }
                subgoal_artifacts = [
                    row for row in mission_snapshot.get("artifacts") or []
                    if str(row.get("task_id") or "") in subgoal_task_ids
                    and str(row.get("kind") or "") == "worker_subgoal"
                ]
                subgoal_receipts = [
                    row for row in mission_snapshot.get("tool_receipts") or []
                    if str(row.get("task_id") or "") in subgoal_task_ids
                    and str(row.get("status") or "") == "success"
                ]
                subgoal_gate = (
                    not subgoal_task_ids
                    or len(subgoal_artifacts) == len(subgoal_task_ids)
                    and len(subgoal_receipts) >= len(subgoal_task_ids)
                )
                goal_graph_artifacts = [
                    row for row in mission_snapshot.get("artifacts") or []
                    if str(row.get("kind") or "") == "goal_graph"
                ]
                change_artifacts = [
                    row for row in mission_snapshot.get("artifacts") or []
                    if str(row.get("kind") or "") == "workspace_change"
                ]
                change_data: Dict[str, Any] = {}
                if change_artifacts:
                    try:
                        change_data = json.loads(Path(change_artifacts[-1]["path"]).read_text(encoding="utf-8"))
                    except (OSError, ValueError, TypeError):
                        change_data = {}
                workspace_change_ok = not self.goal_plan.get("requires_write") or bool(change_data.get("changed_file_count"))
                worker_output_required = bool(self.goal_plan.get("requires_worker_output"))
                worker_task_ids = {
                    str(row.get("task_id") or "")
                    for row in mission_snapshot.get("tasks") or []
                    if str(row.get("role") or "") == Role.WORKER.value
                    and str(row.get("task_kind") or "") != "king_subgoal"
                }
                worker_artifacts = [
                    row for row in mission_snapshot.get("artifacts") or []
                    if str(row.get("task_id") or "") in worker_task_ids
                ]
                worker_receipts = [
                    row for row in mission_snapshot.get("tool_receipts") or []
                    if str(row.get("task_id") or "") in worker_task_ids
                    and str(row.get("status") or "") == "success"
                ]
                worker_output_ok = not worker_output_required or bool(worker_artifacts and worker_receipts)
                soldier_reports = [
                    row for row in mission_snapshot.get("artifacts") or []
                    if str(row.get("kind") or "") == "soldier_patrol"
                ]
                soldier_report_data: Dict[str, Any] = {}
                if soldier_reports:
                    try:
                        soldier_report_data = json.loads(Path(soldier_reports[-1]["path"]).read_text(encoding="utf-8"))
                    except (OSError, ValueError, TypeError):
                        soldier_report_data = {}
                soldier_ok = bool(soldier_report_data.get("decision") == "continue") and all(
                    bool(item.get("passed")) for item in soldier_report_data.get("judge_checks") or []
                )
                ontology_contract_coverage = self.ontology_contract.get("coverage") if isinstance(self.ontology_contract.get("coverage"), dict) else {}
                ontology_contract_ok = (
                    str(ontology_contract_coverage.get("gate") or "blocked") == "pass"
                    and str(self.ontology_contract.get("decision_gate") or "blocked") == "pass"
                )
                graph_ok = not self.goal_plan.get("graph_required") or self.opencrab_receipt.get("graph_gate") == "pass"
                goal_graph_ok = not self.goal_plan.get("goal_graph_id") or bool(goal_graph_artifacts)
                metadata_only_lookup = (
                    self.goal_plan.get("action_mode") == "lookup"
                    and bool(self.opencrab_receipt.get("observed_items"))
                    and not bool(self.opencrab_receipt.get("evidence"))
                )
                context_observation_ok = bool(self.opencrab_receipt.get("evidence_count")) or metadata_only_lookup
                verified = bool(
                    context_observation_ok
                    and queen_artifacts
                    and mcp_receipts
                    and ledger_ok
                    and goal_graph_ok
                    and soldier_ok
                    and graph_ok
                    and subgoal_gate
                    and ontology_contract_ok
                    and workspace_change_ok
                    and worker_output_ok
                )
                data = {
                    "verdict": "accepted" if verified else "rejected",
                    "verification": "mcp_receipt_ledger_and_queen_interpretation" if verified else "missing_mcp_receipt_ledger_or_queen_artifact",
                    "evidence_required": not metadata_only_lookup,
                    "observation_mode": "metadata_observation" if metadata_only_lookup else "evidence",
                    "observed_item_count": len(self.opencrab_receipt.get("observed_items") or []),
                    "evidence_count": self.opencrab_receipt.get("evidence_count", 0),
                    "graph_gate": self.opencrab_receipt.get("graph_gate", "not_required"),
                    "context_quality": self.opencrab_receipt.get("quality", {}),
                    "queen_artifact_count": len(queen_artifacts),
                    "mcp_receipt_count": len(mcp_receipts),
                    "ontology_ledger_count": len(ledger_artifacts),
                    "ontology_ledger_readback": {
                        "validated": ledger_ok,
                        "artifact_id": (ledger_readback or {}).get("_artifact", {}).get("artifact_id"),
                        "mission_id": (ledger_readback or {}).get("mission_id"),
                        "goal_graph_id": (ledger_readback or {}).get("goal_graph_id"),
                        "revision": (ledger_readback or {}).get("revision"),
                    },
                    "king_subgoal_count": len(subgoal_task_ids),
                    "king_subgoal_artifact_count": len(subgoal_artifacts),
                    "king_subgoal_receipt_count": len(subgoal_receipts),
                    "king_subgoal_gate": subgoal_gate,
                    "goal_graph_count": len(goal_graph_artifacts),
                    "goal_graph_accepted": goal_graph_ok,
                    "workspace_change_receipt_count": len(change_artifacts),
                    "workspace_changed_file_count": int(change_data.get("changed_file_count") or 0),
                    "workspace_change_accepted": workspace_change_ok,
                    "worker_output_required": worker_output_required,
                    "worker_artifact_count": len(worker_artifacts),
                    "worker_receipt_count": len(worker_receipts),
                    "worker_output_accepted": worker_output_ok,
                    "soldier_report_count": len(soldier_reports),
                    "soldier_judge_accepted": soldier_ok,
                    "ontology_contract_coverage_gate": ontology_contract_coverage.get("gate"),
                    "ontology_contract_decision_gate": self.ontology_contract.get("decision_gate"),
                    "ontology_contract_accepted": ontology_contract_ok,
                    "model_gate": reason or "local_ontology_oracle_gate",
                }
                queen_interpretation = self._extract_queen_interpretation(context)
                queen_handoff_quality = self._queen_handoff_quality(context)
                verified = verified and bool(queen_handoff_quality.get("accepted"))
                data["verdict"] = "accepted" if verified else "rejected"
                data["verification"] = "mcp_receipt_ledger_queen_handoff_and_interpretation" if verified else "missing_mcp_receipt_ledger_or_grounded_queen_handoff"
                data["queen_handoff_quality"] = queen_handoff_quality
                data["evidence_refs"] = [
                    str(row.get("evidence_id") or "")
                    for row in mission_snapshot.get("evidence") or []
                    if row.get("evidence_id")
                ][:24]
                data["final_conclusion"] = queen_interpretation[-6000:] if queen_interpretation else ""
            else:
                worker_task_ids = {
                    str(row["task_id"])
                    for row in mission_snapshot.get("tasks") or []
                    if str(row.get("role") or "") == Role.WORKER.value
                }
                worker_artifacts = [
                    row for row in mission_snapshot.get("artifacts") or []
                    if str(row.get("task_id") or "") in worker_task_ids
                ]
                worker_receipts = [
                    row for row in mission_snapshot.get("tool_receipts") or []
                    if str(row.get("task_id") or "") in worker_task_ids and str(row.get("status") or "") == "success"
                ]
                verified = bool(worker_artifacts and worker_receipts)
                goal_graph_artifacts = [
                    row for row in mission_snapshot.get("artifacts") or []
                    if str(row.get("kind") or "") == "goal_graph"
                ]
                goal_graph_ok = not self.goal_plan.get("goal_graph_id") or bool(goal_graph_artifacts)
                change_artifacts = [
                    row for row in mission_snapshot.get("artifacts") or []
                    if str(row.get("kind") or "") == "workspace_change"
                ]
                change_data: Dict[str, Any] = {}
                if change_artifacts:
                    try:
                        change_data = json.loads(Path(change_artifacts[-1]["path"]).read_text(encoding="utf-8"))
                    except (OSError, ValueError, TypeError):
                        change_data = {}
                workspace_change_ok = not self.goal_plan.get("requires_write") or bool(change_data.get("changed_file_count"))
                verified = verified and workspace_change_ok and goal_graph_ok
                data = {
                    "verdict": "accepted" if verified else "rejected",
                    "verification": "worker_artifact_and_receipt" if verified else "missing_worker_artifact_or_receipt",
                    "evidence_required": True,
                    "worker_artifact_count": len(worker_artifacts),
                    "worker_receipt_count": len(worker_receipts),
                    "goal_graph_count": len(goal_graph_artifacts),
                    "goal_graph_accepted": goal_graph_ok,
                    "workspace_change_receipt_count": len(change_artifacts),
                    "workspace_changed_file_count": int(change_data.get("changed_file_count") or 0),
                    "workspace_change_accepted": workspace_change_ok,
                    "model_gate": reason or "local_oracle_gate",
                }
        content = json.dumps(data, ensure_ascii=False, indent=2)
        kind = (
            "oracle_result"
            if role is Role.ORACLE
            else "worker_subgoal"
            if role is Role.WORKER and str(task.get("task_kind") or "") == "king_subgoal"
            else "local_role_contract"
        )
        artifact = self.service._write_artifact(mission_id, task["task_id"], task["output_contract"], content + "\n", kind)
        self.store.add_tool_receipt(
            mission_id,
            task["task_id"],
            attempt["attempt_id"],
            "crab.worker.subgoal" if role is Role.WORKER and str(task.get("task_kind") or "") == "king_subgoal" else "crab.role_gate",
            "success",
            {"role": role.value, "model_invocation": False, "artifact_id": artifact.artifact_id},
        )
        self.store.finish_attempt(mission_id, attempt["attempt_id"], attempt["lease_id"], AttemptStatus.SUCCEEDED)
        local_oracle_accepted = role is Role.ORACLE and data.get("verdict") == "accepted"
        self.store.set_task_status(
            task["task_id"],
            TaskStatus.VERIFIED if local_oracle_accepted else TaskStatus.FAILED if role is Role.ORACLE else TaskStatus.COMPLETED,
            role,
        )
        self.store.set_assignment_invocation(task["task_id"], "local_rule", model="none", effort="deterministic")
        self.store.append_event(
            mission_id,
            "oracle_local_verdict" if role is Role.ORACLE else "local_gate_recorded",
            role.value,
            {"task_id": task["task_id"], "accepted": local_oracle_accepted if role is Role.ORACLE else True, "reason": reason},
        )
        if local_oracle_accepted:
            for row in self.store.inspect(mission_id)["artifacts"]:
                self.store.set_artifact_status(row["artifact_id"], ArtifactStatus.ACCEPTED, Role.ORACLE)
            self.store.set_oracle_result(mission_id, artifact.artifact_id)
            if role is Role.ORACLE and data.get("final_conclusion"):
                self.store.add_message(
                    self.session_id,
                    "assistant",
                    "오라클 승인\n\n%s" % data["final_conclusion"],
                    mission_id,
                    {"role": Role.ORACLE.value, "status": "accepted", "artifact_id": artifact.artifact_id},
                )
        return content

    def _record_workspace_change(self, mission_id: str, task: Dict[str, Any]) -> str:
        """Persist the actual Worker file delta before Oracle reviews it."""
        before = self._worker_baseline or capture_workspace(self.service.workspace)
        after = capture_workspace(self.service.workspace)
        change = diff_workspace(before, after)
        change["mission_id"] = mission_id
        change["task_id"] = task["task_id"]
        change["role"] = Role.WORKER.value
        change["requires_write"] = True
        artifact = self.service._write_artifact(
            mission_id,
            task["task_id"],
            "workspace_change_receipt.json",
            json.dumps(change, ensure_ascii=False, indent=2) + "\n",
            "workspace_change",
        )
        snapshot = self.store.inspect(mission_id)
        attempt = next(
            (row for row in reversed(snapshot.get("attempts") or []) if str(row.get("task_id") or "") == task["task_id"]),
            None,
        )
        if attempt:
            self.store.add_tool_receipt(
                mission_id,
                task["task_id"],
                str(attempt["attempt_id"]),
                "crab.workspace.diff",
                "success",
                {"artifact_id": artifact.artifact_id, **change},
            )
        self.store.append_event(
            mission_id,
            "workspace_change_observed",
            Role.WORKER.value,
            {"task_id": task["task_id"], "artifact_id": artifact.artifact_id, "changed_file_count": change["changed_file_count"]},
        )
        self._worker_baseline = None
        return artifact.artifact_id

    def _load_opencrab_context(
        self,
        mission_id: str,
        task: Dict[str, Any],
        attempt_id: str,
        objective: str,
    ) -> None:
        """Read the authoritative ontology context before Queen interpretation."""
        if self.opencrab_receipt or not self.goal_plan.get("ontology_required"):
            return
        self._record_kinetic_transition(mission_id, task, ["scope_lock"], "started")
        # The collector owns the SaaS batch boundary. Do not truncate the
        # session selection here or the panel's selected count will diverge
        # from the MCP request set before Queen gets a chance to batch it.
        package_ids = [str(value) for value in self.ontology_context.get("package_ids") or [] if str(value).strip()]
        client: Optional[OpenCrabMcpClient] = None
        scope_resolution: Dict[str, Any] = {}

        def call_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
            nonlocal client
            if name == "opencrab_query" and self.opencrab_context_loader is not None:
                return self.opencrab_context_loader(arguments)
            if name == "opencrab_status" and self.opencrab_context_loader is not None:
                # Test/local loaders may implement the canary without
                # opening a second live connection. The marker is outside the
                # normal query contract and is never sent to OpenCrab SaaS.
                return self.opencrab_context_loader({"__kingcrab_tool__": "opencrab_status", **arguments})
            if client is None:
                client = OpenCrabMcpClient(opencrab_url(workspace=self.service.workspace))
            return client.call_tool(name, arguments)

        retrieval_contract = self.goal_plan.get("retrieval_contract") or {}

        def collect_once() -> Dict[str, Any]:
            nonlocal package_ids, scope_resolution
            # Selected packs are already an explicit user dependency. With
            # no selection, Queen first resolves a bounded metadata scope so
            # the evidence query is not forced to search the entire account.
            # Injected loaders remain authoritative in tests and controlled
            # local integrations, so they bypass the live catalog resolver.
            if not package_ids and retrieval_contract.get("scope_mode") == "workspace_auto" and self.opencrab_context_loader is None:
                scope_resolution = resolve_opencrab_package_scope(
                    objective,
                    workspace=self.service.workspace,
                    client_factory=lambda: client or OpenCrabMcpClient(opencrab_url(workspace=self.service.workspace)),
                    max_packages=int(retrieval_contract.get("package_limit") or 8),
                )
                package_ids = [
                    str(value)
                    for value in (scope_resolution.get("selected_package_ids") or [])
                    if str(value).strip()
                ]
            return OntologyContextCollector(
                call_tool,
                cache_path=self.service.workspace / ".crabagent" / "opencrab" / "ontology-context-cache.json",
            ).collect(
                objective,
                package_ids=package_ids,
                graph_required=bool(self.goal_plan.get("graph_required")),
                retrieval_contract=retrieval_contract or None,
                scope_resolution=scope_resolution,
            )

        def error_receipt(exc: Exception, stage: str) -> Dict[str, Any]:
            details = opencrab_error_details(exc, default_stage=stage)
            return {
                "schema": "crab.opencrab-context-receipt/v2",
                "source": "OpenCrab MCP",
                "authority": "direct_mcp_response",
                "query": objective,
                "status": "error",
                "error": details["message"],
                "error_code": details["error_code"],
                "error_stage": details["stage"],
                "request_id": details["request_id"],
                "retryable": details["retryable"],
                "evidence": [],
                "evidence_count": 0,
                "nodes": [],
                "node_count": 0,
                "edges": [],
                "edge_count": 0,
                "tool_calls": [],
                "claim_gate": "blocked",
            }

        def context_attempt_summary(attempt_number: int, value: Dict[str, Any]) -> Dict[str, Any]:
            status = str(value.get("status") or "unknown").lower()
            details = opencrab_error_details(value, default_stage="context_collection")
            return {
                "attempt": attempt_number,
                "status": status,
                "error_code": value.get("error_code") if status not in {"ok", "cached", "no_evidence"} else None,
                "error_stage": value.get("error_stage") if status not in {"ok", "cached", "no_evidence"} else None,
                "request_id": value.get("request_id") if status not in {"ok", "cached", "no_evidence"} else None,
                "retryable": bool(details.get("retryable")) if status not in {"ok", "cached", "no_evidence"} else False,
            }

        def run_canary() -> Dict[str, Any]:
            nonlocal client
            # A failed session may carry a stale MCP session id. Recreate the
            # read-only client before canary and retry; never reuse a failed
            # transport session blindly.
            client = None
            try:
                payload = call_tool("opencrab_status", {})
            except (OpenCrabUnavailable, OSError, RuntimeError, ValueError) as exc:
                details = opencrab_error_details(exc, default_stage="connection_canary")
                return {**details, "status": "error", "scope_revalidated": False}
            if not isinstance(payload, dict):
                details = opencrab_error_details(payload, default_stage="connection_canary")
                return {**details, "status": "error", "scope_revalidated": False}
            status = str(payload.get("status") or "").lower()
            request_id = str(payload.get("request_id") or getattr(client, "last_request_id", "") or "")
            if status not in {"ok", "cached", "success"}:
                details = opencrab_error_details(payload, default_stage="connection_canary", request_id=request_id)
                return {**details, "status": "error", "scope_revalidated": False}
            expected_workspace = str(
                self.ontology_context.get("workspace_id")
                or self.ontology_context.get("workspace")
                or ""
            ).strip()
            reported_scope = payload.get("scope") if isinstance(payload.get("scope"), dict) else payload
            reported_workspace = str(reported_scope.get("workspace_id") or "").strip()
            if expected_workspace and reported_workspace and expected_workspace != reported_workspace:
                return {
                    "error_code": "OPENCRAB_WORKSPACE_MISMATCH",
                    "message": "OpenCrab canary workspace does not match the mission scope",
                    "stage": "connection_canary",
                    "request_id": request_id,
                    "retryable": False,
                    "status": "error",
                    "scope_revalidated": False,
                }
            expected_projects = {
                str(value).strip()
                for value in self.ontology_context.get("project_ids") or []
                if str(value).strip()
            }
            reported_projects = {
                str(value).strip()
                for value in reported_scope.get("project_ids") or []
                if str(value).strip()
            }
            if expected_projects and reported_projects and expected_projects != reported_projects:
                return {
                    "error_code": "OPENCRAB_PROJECT_SCOPE_MISMATCH",
                    "message": "OpenCrab canary project scope does not match the mission scope",
                    "stage": "connection_canary",
                    "request_id": request_id,
                    "retryable": False,
                    "status": "error",
                    "scope_revalidated": False,
                }
            return {
                "status": "ok",
                "error_code": None,
                "message": "OpenCrab read-only canary passed",
                "stage": "connection_canary",
                "request_id": request_id,
                "retryable": False,
                "scope_revalidated": True,
                "profile_revalidated": True,
                "selected_package_count": len(package_ids),
                "selected_project_count": len(expected_projects),
            }

        try:
            receipt = collect_once()
        except (OpenCrabUnavailable, OSError, RuntimeError, ValueError) as exc:
            receipt = error_receipt(exc, "context_collection")

        attempts = [context_attempt_summary(1, receipt)]
        retry = {
            "policy": "read_only_bounded_once",
            "max_attempts": 2,
            "attempt_count": 1,
            "eligible": False,
            "retried": False,
            "canary": None,
        }
        initial_failure = _opencrab_retry_details(receipt)
        if initial_failure:
            retry["eligible"] = True
            retry["initial_error"] = dict(initial_failure)
            retry["initial_tool_calls"] = [
                dict(row)
                for row in (receipt.get("tool_calls") or [])
                if isinstance(row, dict)
            ]
            canary = run_canary()
            retry["canary"] = {
                key: canary.get(key)
                for key in (
                    "status",
                    "error_code",
                    "message",
                    "stage",
                    "request_id",
                    "retryable",
                    "scope_revalidated",
                    "profile_revalidated",
                    "selected_package_count",
                    "selected_project_count",
                )
                if canary.get(key) is not None
            }
            canary_call = {
                "tool": "opencrab_status",
                "arguments": {},
                "status": "observed" if canary.get("status") == "ok" else "failed",
                "response_status": canary.get("status"),
                "error_code": canary.get("error_code"),
                "stage": canary.get("stage"),
                "request_id": canary.get("request_id"),
                "retryable": bool(canary.get("retryable")),
            }
            if canary.get("status") == "ok":
                retry["retried"] = True
                try:
                    receipt = collect_once()
                except (OpenCrabUnavailable, OSError, RuntimeError, ValueError) as exc:
                    receipt = error_receipt(exc, "context_retry")
                retry["attempt_count"] = 2
                attempts.append(context_attempt_summary(2, receipt))
            else:
                retry["blocked_reason"] = "connection_canary_failed"
            receipt.setdefault("tool_calls", []).insert(0, canary_call)
            receipt["observed_tool_call_count"] = sum(
                1 for row in receipt.get("tool_calls") or [] if isinstance(row, dict) and row.get("status") == "observed"
            )
            receipt["failed_tool_call_count"] = sum(
                1 for row in receipt.get("tool_calls") or [] if isinstance(row, dict) and row.get("status") == "failed"
            )
        retry["attempts"] = attempts
        receipt["retry"] = retry
        receipt["observed_at"] = utc_now()
        artifact = self.service._write_artifact(
            mission_id,
            task["task_id"],
            "opencrab_context.json",
            json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
            "mcp_context_receipt",
        )
        self.opencrab_receipt = {**receipt, "artifact_id": artifact.artifact_id, "artifact_sha256": artifact.sha256}
        self._refresh_ontology_contract(mission_id, task["task_id"])
        retrieval_ok = receipt.get("status") in {"ok", "cached", "no_evidence"}
        evidence_rows = receipt.get("evidence") if isinstance(receipt.get("evidence"), list) else []
        observed_items = receipt.get("observed_items") if isinstance(receipt.get("observed_items"), list) else []
        metadata_only_lookup = (
            self.goal_plan.get("action_mode") == "lookup"
            and bool(observed_items)
            and not evidence_rows
        )
        coverage = self.ontology_contract.get("coverage") if isinstance(self.ontology_contract.get("coverage"), dict) else {}
        self._record_kinetic_transition(mission_id, task, ["scope_lock"], "completed")
        self._record_kinetic_transition(
            mission_id,
            task,
            ["retrieve_evidence"],
            "completed" if retrieval_ok else "blocked",
            detail={
                "evidence_count": receipt.get("evidence_count", 0),
                "observed_item_count": len(observed_items),
                "observation_gate": receipt.get("observation_gate", "blocked"),
                "claim_gate": receipt.get("claim_gate"),
                "graph_gate": receipt.get("graph_gate"),
                "cache_hit": bool(receipt.get("cache_hit")),
            },
        )
        self._record_kinetic_transition(
            mission_id,
            task,
            ["bind_ontology_slots"],
            "completed" if coverage.get("gate") == "pass" else "blocked",
            detail={
                "required_slot_count": coverage.get("required_slot_count", 0),
                "filled_required_slot_count": coverage.get("filled_required_slot_count", 0),
                "missing_slot_ids": coverage.get("missing_slot_ids") or [],
            },
        )
        if self.goal_plan.get("graph_required"):
            self._record_kinetic_transition(
                mission_id,
                task,
                ["traverse_graph"],
                "completed" if receipt.get("graph_gate") == "pass" else "blocked",
                detail={
                    "path_count": len(receipt.get("paths") or []),
                    "semantic_path_count": (receipt.get("quality") or {}).get("evidence_backed_preferred_relation_path_count", 0),
                },
            )
        for index, row in enumerate(evidence_rows, start=1):
            if not isinstance(row, dict):
                continue
            evidence_id = row.get("id") or "mcp-evidence-%s" % index
            source_uri = row.get("source") or "opencrab://evidence/%s" % evidence_id
            self.store.add_evidence(
                EvidenceRef(
                    # OpenCrab evidence IDs are stable across missions. The
                    # local evidence table has a global primary key, so keep
                    # the source ID in the URI/receipt and namespace storage
                    # IDs by mission to avoid cross-mission collisions.
                    evidence_id="mcp:%s:%s" % (mission_id, evidence_id),
                    mission_id=mission_id,
                    artifact_id=artifact.artifact_id,
                    source_type="opencrab_mcp_evidence",
                    source_uri=str(source_uri),
                    digest=artifact.sha256,
                )
            )
        nodes = receipt.get("nodes") if isinstance(receipt.get("nodes"), list) else []
        for node in nodes[:12]:
            if not isinstance(node, dict) or not node.get("id"):
                continue
            self.store.add_evidence(
                EvidenceRef(
                    evidence_id="mcp:%s:node:%s" % (mission_id, node["id"]),
                    mission_id=mission_id,
                    artifact_id=artifact.artifact_id,
                    source_type="opencrab_mcp_graph",
                    source_uri="opencrab://node/%s" % node["id"],
                    digest=artifact.sha256,
                )
            )
        status = str(receipt.get("status") or "unknown").lower()
        tool_calls = receipt.get("tool_calls") if isinstance(receipt.get("tool_calls"), list) else []
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            response_status = str(call.get("response_status") or "unknown").lower()
            call_status = "success" if call.get("status") in {"observed", "cached"} and response_status in {"ok", "no_evidence", "cached"} else "failed"
            self.store.add_tool_receipt(
                mission_id,
                task["task_id"],
                attempt_id,
                "opencrab.mcp.%s" % call.get("tool", "unknown"),
                call_status,
                {"response_status": response_status, "artifact_id": artifact.artifact_id, "arguments": call.get("arguments", {})},
            )
        observed_status = "cached" if receipt.get("cache_hit") else "observed" if status in {"ok", "no_evidence"} else "failed"
        self.store.append_event(
            mission_id,
            "opencrab_context_cached" if observed_status == "cached" else "opencrab_context_observed" if observed_status == "observed" else "opencrab_context_failed",
            Role.QUEEN.value,
            {
                "task_id": task["task_id"],
                "tool": "opencrab_context_collection",
                "status": status,
                "evidence_count": receipt.get("evidence_count", 0),
                "observed_item_count": len(observed_items),
                "observation_gate": receipt.get("observation_gate", "blocked"),
                "node_count": receipt.get("node_count", 0),
                "edge_count": receipt.get("edge_count", 0),
                "graph_gate": receipt.get("graph_gate", "not_required"),
                "quality": receipt.get("quality") or {},
                "tool_call_count": len(tool_calls),
                "artifact_id": artifact.artifact_id,
            },
        )
        if observed_status not in {"observed", "cached"}:
            details = opencrab_error_details(receipt, default_stage="context_collection")
            raise RuntimeError(
                "OpenCrab MCP context failed: %s (error_code=%s stage=%s request_id=%s)"
                % (
                    details["message"],
                    details["error_code"],
                    details["stage"],
                    details["request_id"] or "none",
                )
            )
        if not evidence_rows and not metadata_only_lookup:
            raise RuntimeError("OpenCrab MCP returned no evidence; Queen turn was not spent")
        if self.goal_plan.get("graph_required") and receipt.get("graph_gate") != "pass":
            raise RuntimeError("OpenCrab graph gate blocked: no bounded node-to-node path was observed")

    def _compact_opencrab_receipt(self, max_chars: Optional[int] = None) -> str:
        budget = int(max_chars or self.goal_plan.get("context_char_budget") or 4200)
        return compact_context(self.opencrab_receipt or {}, max_chars=max(1200, min(budget, 7000)))

    def _apply_host_worker_artifact(
        self,
        mission_id: str,
        task: Dict[str, Any],
        attempt_id: str,
        content: str,
    ) -> str:
        cleaned, spec = _parse_host_worker_artifact_directive(content)
        if spec is None:
            raise RuntimeError("HOST_WORKER_ARTIFACT_REQUIRED: write-required host WORKER must return one bounded artifact directive")
        snapshot = self.store.inspect(mission_id)
        approved = any(
            str(row.get("action") or "") == "write_local_demo_artifacts"
            and str(row.get("decision") or "").casefold() == "approved"
            for row in (snapshot.get("approvals") or [])
        )
        if not approved:
            raise RuntimeError("HOST_WORKER_ARTIFACT_NOT_APPROVED: mission lacks write_local_demo_artifacts approval")

        runtime_root = self.service.workspace / ".crabagent"
        artifact_root = runtime_root / "artifacts"
        if runtime_root.exists() and runtime_root.is_symlink():
            raise RuntimeError("HOST_WORKER_ARTIFACT_PATH_UNSAFE: .crabagent cannot be a symlink")
        if artifact_root.exists() and artifact_root.is_symlink():
            raise RuntimeError("HOST_WORKER_ARTIFACT_PATH_UNSAFE: artifacts cannot be a symlink")
        artifact_root.mkdir(parents=True, exist_ok=True)
        resolved_root = artifact_root.resolve()
        target = self.service.workspace / str(spec["relative_path"])
        if target.parent.resolve() != resolved_root:
            raise RuntimeError("HOST_WORKER_ARTIFACT_PATH_UNSAFE: target escaped artifact root")
        if target.exists() or target.is_symlink():
            raise RuntimeError("HOST_WORKER_ARTIFACT_EXISTS: bounded host artifact never overwrites an existing path")

        encoded = str(spec["content"]).encode("utf-8")
        created = False
        try:
            with target.open("xb") as handle:
                created = True
                handle.write(encoded)
                handle.flush()
        except Exception:
            if created:
                try:
                    target.unlink()
                except OSError:
                    pass
            raise

        receipt_id = self.store.add_tool_receipt(
            mission_id,
            str(task["task_id"]),
            attempt_id,
            "host.worker.artifact.write",
            "success",
            {
                "relative_path": str(spec["relative_path"]),
                "sha256": str(spec["sha256"]),
                "bytes": int(spec["bytes"]),
                "mode": "exclusive_create",
                "source": "host_current_model",
            },
        )
        self.store.append_event(
            mission_id,
            "host_worker_artifact_written",
            Role.WORKER.value,
            {
                "task_id": str(task["task_id"]),
                "receipt_id": receipt_id,
                "relative_path": str(spec["relative_path"]),
                "sha256": str(spec["sha256"]),
                "bytes": int(spec["bytes"]),
            },
        )
        return cleaned

    def _prompt(self, role: Role, objective: str, context: List[str]) -> str:
        prior_parts = [item[-1400:] for item in context[-3:]]
        prior = "\n\n".join(prior_parts) or "No prior role output."
        goal_contract = ""
        if self.goal_plan:
            plan_fields = {
                "kind": self.goal_plan.get("kind"),
                "complexity": self.goal_plan.get("complexity"),
                "ontology_required": self.goal_plan.get("ontology_required"),
                "ontology_mode": self.goal_plan.get("ontology_mode"),
                "graph_required": self.goal_plan.get("graph_required"),
                "outcome_type": self.goal_plan.get("outcome_type"),
                "action_mode": self.goal_plan.get("action_mode"),
                "action_required": self.goal_plan.get("action_required"),
                "response_contract": self.goal_plan.get("response_contract") or [],
                "stages": self.goal_plan.get("stages") or [],
                "evidence_gate": self.goal_plan.get("evidence_gate"),
                "estimated_model_turns": self.goal_plan.get("estimated_model_turns"),
                "max_automatic_revision_cycles": self.goal_plan.get("max_automatic_revision_cycles", 0),
            }
            if role is Role.QUEEN:
                route = self.goal_plan.get("retrieval_contract") or {}
                plan_fields.update(
                    {
                        "ontology_spaces": self.goal_plan.get("ontology_spaces") or [],
                        "route_mode": route.get("mode"),
                        "claim_requirements": route.get("claim_requirements") or [],
                        "package_limit": route.get("package_limit"),
                    }
                )
            else:
                plan_fields.update(
                    {
                        "requires_write": self.goal_plan.get("requires_write"),
                        "requires_worker_output": self.goal_plan.get("requires_worker_output"),
                        "worker_write_scope": "task_contract_only" if self.goal_plan.get("requires_write") else "none",
                        "requires_oracle_model": self.goal_plan.get("requires_oracle_model"),
                        "acceptance_checks": self.goal_plan.get("acceptance_checks") or [],
                        "stop_conditions": self.goal_plan.get("stop_conditions") or [],
                    }
                )
            goal_contract = "\n\nGOAL PLAN (compiled before model calls; honor it):\n%s" % json.dumps(
                plan_fields,
                ensure_ascii=False,
            )
        graph = self.goal_graph or compile_goal_graph(self.goal_plan)
        graph_contract = "\n\nGOAL GRAPH (operational contract; fill slots, do not invent them):\n%s" % compact_goal_graph(
            graph,
            max_chars=1100,
        )
        ontology_contract = ""
        kinetic_contract = ""
        if self.goal_plan.get("ontology_required"):
            ontology_contract = (
                "\n\nONTOLOGY EXECUTION CONTRACT (slot ledger; observed MCP evidence is the only authority):\n%s"
                % compact_ontology_execution_contract(self.ontology_contract, max_chars=1900)
            )
            if self.ontology_contract.get("king_subgoals"):
                ontology_contract += "\n\nKING SUBGOAL WORKERS:\n%s" % json.dumps(
                    self.ontology_contract.get("king_subgoals")[:4],
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
        if self.kinetic_workflow:
            kinetic_contract = (
                "\n\nKINETIC WORKFLOW (operators are executed in this order; do not skip a gate):\n%s"
                % compact_kinetic_workflow(self.kinetic_workflow, max_chars=1800)
            )
        king_contract = ""
        if self.king_plan and role is not Role.KING:
            king_contract = "\n\nKING EXECUTION PACKET (advisory; scope and evidence authority remain locked):\n%s" % compact_king_plan(
                self.king_plan,
                1500,
            )
        repair_contract = ""
        if role is Role.QUEEN and self._repair_mode == "queen_handoff":
            repair_contract = (
                "\n\nREPAIR PASS: The previous Queen handoff failed its structure gate. "
                "Use the same observed evidence IDs, repair only missing sections or citations, "
                "and return no new facts. Preserve the exact four sections."
            )
        if role is Role.QUEEN:
            receipt = self._compact_opencrab_receipt(max_chars=2600) if self.opencrab_receipt else "No observed OpenCrab receipt."
            write_boundary = ""
            if self.goal_plan.get("requires_write"):
                write_boundary = (
                    "\n\nWRITE BOUNDARY:\n"
                    "QUEEN IS READ-ONLY FOR THIS MISSION. Do not create, edit, delete, rename, or generate any workspace file. "
                    "Do not execute the requested deliverable yourself. Inspect only what is needed and return the exact bounded "
                    "WORKER task in NEXT_ACTION; WORKER alone may change the workspace.\n"
                )
            return (
                "CRABAGENT ROLE: QUEEN\n"
                "MISSION: %s\n\n"
                "Use only the authoritative OpenCrab MCP receipt below. Extract the minimum ontology path; do not invent a source, "
                "catalog item, graph edge, or model-created authority layer. Cite at least one exact evidence ID when possible; "
                "a unique leading ID prefix is acceptable only when the full ID is too long.\n"
                "%s\n"
                "AUTHORITATIVE OPENCRAB MCP RECEIPT (bounded):\n%s\n\n"
                "%s\n"
                "Return exactly four compact sections: SELECTED_PATH, SUPPORTED_CLAIMS, GAPS, NEXT_ACTION. "
                "For every required evidence slot in the ontology execution contract, cite observed evidence IDs or put the slot in GAPS. "
                "Fill every decision slot explicitly; do not collapse a missing slot into a generic recommendation. "
                "For plan/execute goals, SELECTED_PATH must contain an ordered actionable path and NEXT_ACTION must be "
                "the first concrete task the user can perform, not a request to repeat KING/QUEEN coordination. "
                "For research/lookup/explain goals, answer the objective directly from the observed evidence; do not turn "
                "the answer into a meta-instruction for a future role. Preserve a directly observed sequence when the evidence "
                "contains it, and do not call that sequence missing. Do not mention internal model routing/profile names "
                "unless the objective asks about them. STOP is allowed after that observed result is complete."
                "%s" % (objective, goal_contract, receipt, graph_contract + ontology_contract + kinetic_contract + king_contract + repair_contract, write_boundary)
            )
        receipt_summary = "No active mission receipt."
        if role is not Role.QUEEN:
            receipt_summary = self._receipt_summary()
        inventory = ""
        if role is Role.QUEEN:
            inventory += (
                "\n\nQUEEN RETRIEVAL:\n"
                "The runtime already received the authoritative OpenCrab MCP context below. Treat its evidence IDs, text, "
                "retrieval stats, graph nodes/edges, and pack scope as facts. Do not invent evidence or replace it with a "
                "model-created JSON authority layer. Interpret the bounded context into the minimum ontology path and state any gap."
            )
            inventory += (
                "\n\nOUTPUT:\n"
                "Return four compact sections: SELECTED_PATH, SUPPORTED_CLAIMS, GAPS, NEXT_ACTION. "
                "For plan/execute goals, give an ordered action path and a concrete imperative NEXT_ACTION; for "
                "research/lookup/explain goals, give the answer directly and use STOP after the observed result. "
                "Preserve directly observed sequences from the evidence and do not mention internal routing/profile names. "
                "Do not repeat a full catalog or return a role-coordination instruction as the answer."
            )
            if self.opencrab_receipt:
                inventory += "\n\nAUTHORITATIVE OPENCRAB MCP RECEIPT:\n%s" % self._compact_opencrab_receipt()
        if role is Role.WORKER and self.goal_plan.get("requires_worker_output") and not self.goal_plan.get("requires_write"):
            inventory += (
                "\n\nNON-MUTATING WORKER BOUNDARY:\n"
                "Produce the requested evidence-backed deliverable in the role response only. Do not create, edit, delete, rename, "
                "or save workspace, OpenCrab, user, or external-state artifacts. Preserve every evidence ID used by a claim and mark "
                "unconfirmed personal-role assertions as gaps."
            )
        if role is Role.ORACLE:
            inventory += (
                "\n\nORACLE GATE:\n"
                "Check the persisted OpenCrab MCP context receipt, evidence IDs, graph receipts, local artifacts, and tool receipts first. "
                "When requires_worker_output is true, require the WORKER artifact and its successful receipt and verify that no workspace-change receipt was required. "
                "The Queen interpretation is not evidence. Re-query only a missing or contradictory evidence edge; never compensate "
                "for missing MCP evidence with a plausible claim."
            )
        rendered = (
            "CRABAGENT ROLE: %s\nMISSION: %s\n\n%s\n\n"
            "PERSISTED RECEIPTS (bounded):\n%s\n\n"
            "PRIOR OBSERVED CONTEXT (bounded handoff):\n%s%s%s\n\n"
            "Return a compact evidence-backed result for the next role. Never pretend a tool or test ran."
            % (role.value, objective, ROLE_INSTRUCTIONS[role], receipt_summary, prior, goal_contract, graph_contract + ontology_contract + kinetic_contract + king_contract + inventory)
        )
        if role is Role.KING:
            rendered += (
                "\n\nKING OUTPUT CONTRACT:\n"
                "Return exactly these sections: GOAL_RESTATEMENT, SUBGOALS, CONSTRAINTS, SUCCESS_CHECKS, NEXT_ACTION. "
                "The user objective and goal graph are authoritative. Do not claim evidence retrieval or tool execution."
            )
        if self._host_worker_artifact_required(role):
            rendered += (
                "\n\nHOST WORKER ARTIFACT CONTRACT:\n"
                "The host text channel cannot directly edit arbitrary workspace files. If this bounded task can be satisfied by exactly one mission-local text artifact, include exactly one physical line in your response using this syntax: "
                "HOST_WORKER_ARTIFACT_V1:{\"relative_path\":\".crabagent/artifacts/<safe-name>.md\",\"content\":\"<UTF-8 text with JSON escapes>\"}. "
                "Only a direct-child .md, .txt, or .json artifact is supported, maximum 16 KiB, and existing files are never overwritten. "
                "The directive may be indented or enclosed by a Markdown code fence, but it must remain one complete physical line and must not be wrapped in another JSON envelope. "
                "Do not emit this directive for any other path or for a task that needs source/user/external mutation; fail closed instead."
            )
        if role is Role.QUEEN and repair_contract:
            rendered += repair_contract
        prompt_budget = max(3600, min(12000, int(self.goal_plan.get("context_char_budget") or 4200) + 1800))
        if len(rendered) <= prompt_budget:
            return rendered
        # Preserve the goal contract and the tail of the authoritative
        # receipt, while dropping repeated role narration first. This is a
        # real input bound, not merely a display hint.
        compact_prior = prior[-700:]
        inventory_budget = max(1400, prompt_budget // 2)
        compact_inventory = graph_contract + ontology_contract + kinetic_contract + king_contract + inventory
        if len(compact_inventory) > inventory_budget:
            compact_inventory = "[bounded inventory omitted]\n" + compact_inventory[-inventory_budget:]
        compact_result = (
            "CRABAGENT ROLE: %s\nMISSION: %s\n\n%s\n\n"
            "PERSISTED RECEIPTS (bounded):\n%s\n\n"
            "PRIOR OBSERVED CONTEXT (compressed):\n%s%s%s\n\n"
            "Return a compact evidence-backed result for the next role. Never pretend a tool or test ran."
            % (role.value, objective, ROLE_INSTRUCTIONS[role], receipt_summary[-1200:], compact_prior, goal_contract, compact_inventory)
        )
        if role is Role.KING:
            compact_result += (
                "\n\nKING OUTPUT CONTRACT:\n"
                "Return exactly GOAL_RESTATEMENT, SUBGOALS, CONSTRAINTS, SUCCESS_CHECKS, NEXT_ACTION."
            )
        if self._host_worker_artifact_required(role):
            compact_result += (
                "\n\nHOST WORKER ARTIFACT CONTRACT: include exactly one one-line "
                "HOST_WORKER_ARTIFACT_V1 JSON directive for one direct-child .md/.txt/.json under .crabagent/artifacts, max 16 KiB, no overwrite. "
                "Indentation or a surrounding Markdown code fence is accepted, but do not wrap the directive in another JSON envelope. "
                "If the task cannot fit that boundary, fail closed."
            )
        if role is Role.QUEEN and repair_contract:
            compact_result += repair_contract
        return compact_result

    def _receipt_summary(self) -> str:
        """Expose operational proof without replaying full artifact contents."""
        session = self.store.session(self.session_id) or {}
        mission_id = str(session.get("active_mission_id") or "")
        if not mission_id:
            return "No active mission receipt."
        snapshot = self.store.inspect(mission_id)
        payload = {
            "mission_status": (snapshot.get("mission") or {}).get("status"),
            "tasks": [
                {"role": row.get("role"), "status": row.get("status"), "title": row.get("title")}
                for row in snapshot.get("tasks") or []
            ],
            "artifacts": [
                {
                    "kind": row.get("kind"),
                    "status": row.get("status"),
                    "file": Path(str(row.get("path") or "")).name,
                    "sha256": str(row.get("sha256") or "")[:16],
                }
                for row in snapshot.get("artifacts") or []
            ][-12:],
            "evidence_refs": len(snapshot.get("evidence") or []),
            "tool_receipts": [
                {"tool": row.get("tool_name"), "status": row.get("status")}
                for row in snapshot.get("tool_receipts") or []
            ][-12:],
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def _host_worker_artifact_required(self, role: Role) -> bool:
        return (
            role is Role.WORKER
            and bool(self.goal_plan.get("requires_write"))
            and str(getattr(self.bridge, "executor_name", "")) == "host_current_model"
        )

    def _align_worker_write_scope(self, mission_id: str, task: Dict[str, Any]) -> None:
        """Keep persisted task metadata aligned with the compiled write gate."""
        if Role(task["role"]) is not Role.WORKER or not self.goal_plan.get("requires_write"):
            return
        required_scope = "task_contract_only"
        if str(task.get("write_scope") or "none") == required_scope:
            return
        self.store.set_task_write_scope(task["task_id"], required_scope, Role.WORKER)
        task["write_scope"] = required_scope
        self.store.append_event(
            mission_id,
            "worker_write_scope_aligned",
            Role.WORKER.value,
            {"task_id": task["task_id"], "write_scope": required_scope, "requires_write": True},
        )

    def _run_codex_role(
        self,
        mission_id: str,
        task: Dict[str, Any],
        assignment: Dict[str, Any],
        objective: str,
        context: List[str],
    ) -> CodexLiveTurn:
        role = Role(task["role"])
        session = self.store.session(self.session_id) or {}
        executor_name = str(getattr(self.bridge, "executor_name", "codex_app_server"))
        if str(session.get("model_policy") or "auto") == "host" and executor_name != "host_current_model":
            self.store.append_event(
                mission_id,
                "host_model_execution_required",
                role.value,
                {
                    "task_id": task["task_id"],
                    "role": role.value,
                    "executor": executor_name,
                    "reason": "host model policy requires the host_current_model executor; Codex fallback is disabled",
                },
            )
            raise RuntimeError(
                "HOST_MODEL_EXECUTOR_UNAVAILABLE: host policy cannot use %s" % executor_name
            )
        self._align_worker_write_scope(mission_id, task)
        self.store.set_task_status(task["task_id"], TaskStatus.RUNNING, role)
        receipt_tool_name = str(getattr(self.bridge, "receipt_tool_name", "codex.app-server.turn"))
        observation_source = str(getattr(self.bridge, "observation_source", executor_name))
        supports_session_fallback = bool(getattr(self.bridge, "supports_session_fallback", True))
        attempt = self.store.start_attempt(mission_id, task["task_id"], executor_name)
        self.store.set_assignment_invocation(task["task_id"], "invoking")

        def emit(event: CodexLiveEvent) -> None:
            if event.kind == "runtime_request":
                request_id = str(event.data.get("request_id") or new_id("request"))
                method = str(event.data.get("method") or event.text)
                request_type = "host_model_turn" if method == "hostModel/turn" else "approval_or_input"
                self.store.add_runtime_request(
                    request_id,
                    self.session_id,
                    mission_id,
                    request_type,
                    method,
                    event.text,
                    event.data,
                )
                if self.on_runtime_request:
                    self.on_runtime_request(event.data)
            elif event.kind == "activity":
                self.store.append_event(mission_id, "codex_activity", role.value, {"task_id": task["task_id"], "activity": event.text, "data": event.data})
            elif event.kind == "usage":
                self.store.append_event(mission_id, "token_usage_observed", role.value, {"task_id": task["task_id"], "usage": event.data})

        session = self.store.session(self.session_id) or {}
        policy = str(session.get("model_policy") or "auto")
        forced = {
            "sol": ("gpt-5.6-sol", "high"),
            "terra": (
                "gpt-5.6-terra",
                "medium" if role is Role.QUEEN and self.goal_plan.get("complexity") == "bounded" else "high",
            ),
            "luna": ("gpt-5.6-luna", "low"),
        }.get(policy)
        model, effort = forced or (str(assignment["model"]), str(assignment["effort"]))
        actual_profile = policy if forced else str(assignment["profile"])
        actual_model = model
        if executor_name == "host_current_model":
            model = "current-model"
            actual_model = "host-current-model"
            actual_profile = "host"
        try:
            if role is Role.QUEEN and self.goal_plan.get("ontology_required"):
                self._load_opencrab_context(mission_id, task, attempt["attempt_id"], objective)
            prompt = self._prompt(role, objective, context)
            try:
                turn = self.bridge.run_turn(
                    prompt,
                    emit,
                    model=model,
                    effort=effort,
                    cancel_event=self.cancel_event,
                    idle_timeout_seconds=ROLE_IDLE_TIMEOUT_SECONDS.get(role, 1800.0),
                )
            except CodexAppServerError as first_error:
                if role is Role.ORACLE or not supports_session_fallback or not can_fallback_to_session_default(str(first_error)):
                    self.store.append_event(
                        mission_id,
                        "route_fallback_skipped",
                        role.value,
                        {"requested_model": model, "reason": str(first_error), "duplicate_work_protected": True},
                    )
                    raise
                self.store.append_event(mission_id, "route_fallback", role.value, {"requested_model": model, "fallback": "codex-session-default", "reason": str(first_error)})
                actual_model = "codex-session-default"
                actual_profile = "default"
                turn = self.bridge.run_turn(
                    prompt,
                    emit,
                    effort=effort,
                    cancel_event=self.cancel_event,
                    idle_timeout_seconds=ROLE_IDLE_TIMEOUT_SECONDS.get(role, 1800.0),
                )
            if (
                turn.status in {"failed", "timeout"}
                and actual_model != "codex-session-default"
                and role is not Role.ORACLE
                and supports_session_fallback
                and can_fallback_to_session_default(turn.error)
            ):
                self.store.append_event(
                    mission_id,
                    "route_fallback",
                    role.value,
                    {"requested_model": model, "fallback": "codex-session-default", "reason": turn.error or "requested route failed"},
                )
                actual_model = "codex-session-default"
                actual_profile = "default"
                turn = self.bridge.run_turn(
                    prompt,
                    emit,
                    effort=effort,
                    cancel_event=self.cancel_event,
                    idle_timeout_seconds=ROLE_IDLE_TIMEOUT_SECONDS.get(role, 1800.0),
                )
            elif turn.status in {"failed", "timeout"} and actual_model != "codex-session-default":
                self.store.append_event(
                    mission_id,
                    "route_fallback_skipped",
                    role.value,
                    {"requested_model": model, "reason": turn.error or turn.status, "duplicate_work_protected": True},
                )
            if turn.status in {"failed", "timeout"}:
                raise CodexAppServerError(turn.error or "Codex turn %s" % turn.status)
            if turn.status == "cancelled":
                self.store.finish_attempt(mission_id, attempt["attempt_id"], attempt["lease_id"], AttemptStatus.STOPPED, "user_interrupt")
                self.store.set_task_status(task["task_id"], TaskStatus.STOPPED, role)
                self.store.set_assignment_invocation(task["task_id"], "cancelled", model=actual_model)
                self.store.cancel_active_mission(mission_id, "user_interrupt")
                return turn
            reported_model = str(getattr(self.bridge, "last_reported_model", "") or "").strip()
            if reported_model:
                actual_model = reported_model
            content = turn.text or "Model turn completed without a textual response. Inspect tool receipts and workspace changes."
            if executor_name == "host_current_model" and _HOST_WORKER_ARTIFACT_PREFIX in content and role is not Role.WORKER:
                raise RuntimeError("HOST_WORKER_ARTIFACT_ROLE_INVALID: artifact directives are accepted only from WORKER")
            if self._host_worker_artifact_required(role):
                content = self._apply_host_worker_artifact(mission_id, task, attempt["attempt_id"], content)
            if role is Role.QUEEN and self.goal_plan.get("ontology_required"):
                content = self._normalize_queen_interpretation(content)
            turn_tokens = _usage_total(turn.usage)
            if turn_tokens is not None:
                self.observed_tokens = (self.observed_tokens or 0) + turn_tokens
                self.observed_model_turns += 1
                billable_input = _usage_billable_input(turn.usage)
                if billable_input is not None:
                    self.observed_billable_input_tokens = (self.observed_billable_input_tokens or 0) + billable_input
                self.store.append_event(
                    mission_id,
                    "mission_budget_observed",
                    role.value,
                    {
                        "task_id": task["task_id"],
                        "total_tokens": turn_tokens,
                        "uncached_input_tokens": billable_input,
                        "uncached_input_tokens_cumulative": self.observed_billable_input_tokens,
                        "budget_basis": "uncached_input_tokens" if self.observed_billable_input_tokens is not None else "total_tokens_fallback",
                    },
                )
                self.store.set_budget(
                    Budget(
                        mission_id=mission_id,
                        currency="USD",
                        token_limit=self.goal_plan.get("token_budget") or None,
                        tokens_observed=self.observed_tokens,
                        cost_observed=None,
                        observation_source=observation_source,
                    )
                )
            artifact_content = content + "\n"
            artifact_kind = "oracle_result" if role is Role.ORACLE else "codex_role_output"
            contract_observation: Dict[str, Any] = {}
            if role is Role.QUEEN and self.goal_plan.get("ontology_required"):
                contract_observation = {
                    "authority": "opencrab_mcp_receipt",
                    "context_artifact_id": self.opencrab_receipt.get("artifact_id"),
                    "evidence_count": self.opencrab_receipt.get("evidence_count", 0),
                }
            artifact = self.service._write_artifact(mission_id, task["task_id"], task["output_contract"], artifact_content, artifact_kind)
            self.store.add_tool_receipt(
                mission_id,
                task["task_id"],
                attempt["attempt_id"],
                receipt_tool_name,
                "success",
                {
                    "thread_id": turn.thread_id,
                    "turn_id": turn.turn_id,
                    "status": turn.status,
                    "model": actual_model,
                    "effort": effort,
                    "usage": turn.usage,
                    "prompt_chars": len(prompt),
                    "context_chars": len(self._compact_opencrab_receipt()) if self.opencrab_receipt else 0,
                    "artifact_id": artifact.artifact_id,
                    **contract_observation,
                },
            )
            self.store.finish_attempt(mission_id, attempt["attempt_id"], attempt["lease_id"], AttemptStatus.SUCCEEDED)
            final_status = TaskStatus.VERIFIED if role is Role.ORACLE else TaskStatus.COMPLETED
            self.store.set_task_status(task["task_id"], final_status, role)
            self.store.set_assignment_invocation(task["task_id"], "invoked", model=actual_model, effort=effort, profile=actual_profile)
            self.store.add_message(self.session_id, "assistant", content, mission_id, {"role": role.value, "model": actual_model, "turn_id": turn.turn_id})
            if role is Role.ORACLE:
                metadata_only_lookup = (
                    self.goal_plan.get("action_mode") == "lookup"
                    and bool(self.opencrab_receipt.get("observed_items"))
                    and not bool(self.opencrab_receipt.get("evidence"))
                )
                if self.goal_plan.get("ontology_required") and not self.opencrab_receipt.get("evidence_count") and not metadata_only_lookup:
                    raise RuntimeError("Oracle ontology gate rejected: no observed OpenCrab evidence")
                if self.goal_plan.get("ontology_required"):
                    ledger_readback = self.store.get_latest_ontology_ledger(
                        mission_id,
                        goal_graph_id=str(self.goal_graph.get("graph_id") or ""),
                        revision=int(self.ontology_contract.get("revision") or 0) or None,
                    )
                    if (
                        ledger_readback is None
                        or not ledger_readback.get("readback", {}).get("validated")
                        or ledger_readback.get("execution_contract") != self.ontology_contract
                    ):
                        raise RuntimeError(
                            "Oracle ontology gate rejected: persisted/readback ontology ledger mismatch"
                        )
                    mission_snapshot = self.store.inspect(mission_id)
                    subgoal_task_ids = {
                        str(row.get("task_id") or "")
                        for row in mission_snapshot.get("tasks") or []
                        if str(row.get("task_kind") or "") == "king_subgoal"
                    }
                    if subgoal_task_ids:
                        subgoal_artifacts = {
                            str(row.get("task_id") or "")
                            for row in mission_snapshot.get("artifacts") or []
                            if str(row.get("kind") or "") == "worker_subgoal"
                        }
                        subgoal_receipts = {
                            str(row.get("task_id") or "")
                            for row in mission_snapshot.get("tool_receipts") or []
                            if str(row.get("tool_name") or "") == "crab.worker.subgoal"
                            and str(row.get("status") or "") == "success"
                        }
                        if not subgoal_task_ids.issubset(subgoal_artifacts) or not subgoal_task_ids.issubset(subgoal_receipts):
                            raise RuntimeError("Oracle ontology gate rejected: incomplete King subgoal worker receipts")
                    if self.goal_plan.get("graph_required") and self.opencrab_receipt.get("graph_gate") != "pass":
                        raise RuntimeError("Oracle ontology gate rejected: graph path gate blocked")
                for row in self.store.inspect(mission_id)["artifacts"]:
                    self.store.set_artifact_status(row["artifact_id"], ArtifactStatus.ACCEPTED, Role.ORACLE)
                self.store.set_oracle_result(mission_id, artifact.artifact_id)
            return turn
        except Exception as exc:
            self.store.finish_attempt(mission_id, attempt["attempt_id"], attempt["lease_id"], AttemptStatus.FAILED, str(exc))
            self.store.set_task_status(task["task_id"], TaskStatus.FAILED, role)
            self.store.set_assignment_invocation(task["task_id"], "failed", model=actual_model, profile=actual_profile)
            raise

    def _run_soldier(self, mission_id: str, task: Dict[str, Any], context: Optional[List[str]] = None) -> str:
        role = Role.SOLDIER
        self.store.set_task_status(task["task_id"], TaskStatus.RUNNING, role)
        attempt = self.store.start_attempt(mission_id, task["task_id"], "soldier_local_patrol")
        snapshot = self.store.inspect(mission_id)
        active_leases = [row for row in snapshot.get("leases") or [] if str(row.get("status") or "") == "active"]
        failed_attempts = [row for row in snapshot.get("attempts") or [] if str(row.get("status") or "") == "failed"]
        digests = [str(row.get("sha256") or "") for row in snapshot.get("artifacts") or [] if row.get("sha256")]
        duplicate_artifacts = len(digests) - len(set(digests))
        claim_gate = str(self.opencrab_receipt.get("claim_gate") or "none")
        graph_gate = str(self.opencrab_receipt.get("graph_gate") or "not_required")
        context_quality = self.opencrab_receipt.get("quality") if isinstance(self.opencrab_receipt.get("quality"), dict) else {}
        queen_handoff_quality = self._queen_handoff_quality(context) if self.goal_plan.get("ontology_required") else {}
        metadata_only_lookup = (
            self.goal_plan.get("action_mode") == "lookup"
            and bool(self.opencrab_receipt.get("observed_items"))
            and not bool(self.opencrab_receipt.get("evidence"))
        )
        contract_coverage = self.ontology_contract.get("coverage") if isinstance(self.ontology_contract.get("coverage"), dict) else {}
        contract_gate = str(contract_coverage.get("gate") or "not_required")
        decision_gate = str(self.ontology_contract.get("decision_gate") or "not_required")
        ledger_readback = (
            self.store.get_latest_ontology_ledger(
                mission_id,
                goal_graph_id=str(self.goal_graph.get("graph_id") or ""),
                revision=int(self.ontology_contract.get("revision") or 0) or None,
            )
            if self.goal_plan.get("ontology_required")
            else None
        )
        ledger_ok = bool(
            ledger_readback
            and ledger_readback.get("readback", {}).get("validated")
            and ledger_readback.get("execution_contract") == self.ontology_contract
        )
        repair = {"attempted": False, "reason": "not_needed"}
        if (
            self.goal_plan.get("ontology_required")
            and not queen_handoff_quality.get("accepted")
            and self.goal_plan.get("requires_model_queen")
            and self.opencrab_receipt.get("evidence_count")
            and claim_gate != "blocked"
            and (not self.goal_plan.get("graph_required") or graph_gate == "pass")
        ):
            repair = self._maybe_repair_queen_handoff(mission_id, str(self.goal_plan.get("objective") or ""), context)
            queen_handoff_quality = repair.get("quality") or self._queen_handoff_quality(context)
            contract_coverage = self.ontology_contract.get("coverage") if isinstance(self.ontology_contract.get("coverage"), dict) else {}
            contract_gate = str(contract_coverage.get("gate") or "not_required")
            decision_gate = str(self.ontology_contract.get("decision_gate") or "not_required")
            if repair.get("attempted"):
                # A bounded Queen repair creates a new execution-contract
                # revision; promote that revision before Soldier can hand off
                # to ORACLE.
                ledger_readback = self._persist_ontology_ledger(
                    mission_id,
                    task["task_id"],
                    context,
                )
                ledger_ok = bool(
                    ledger_readback
                    and ledger_readback.get("readback", {}).get("validated")
                    and ledger_readback.get("execution_contract") == self.ontology_contract
                )
        judge_checks = [
            {
                "check": "authoritative_evidence",
                "passed": (
                    metadata_only_lookup
                    or (bool(self.opencrab_receipt.get("evidence_count")) and claim_gate == "pass")
                ),
                "reason": "typed OpenCrab observation gate" if metadata_only_lookup else "observed OpenCrab evidence and claim gate",
            },
            {
                "check": "queen_handoff",
                "passed": bool(queen_handoff_quality.get("accepted")) if self.goal_plan.get("ontology_required") else True,
                "reason": "structured Queen interpretation cites a unique evidence ID",
            },
            {
                "check": "ontology_slot_contract",
                "passed": (
                    contract_gate == "pass" and decision_gate == "pass"
                    if self.goal_plan.get("ontology_required")
                    else True
                ),
                "reason": "required evidence slots and decision slots are explicitly covered",
            },
            {
                "check": "ontology_ledger_readback",
                "passed": ledger_ok if self.goal_plan.get("ontology_required") else True,
                "reason": "QUEEN promotion is persisted and validated from the artifact path",
            },
            {
                "check": "next_action",
                "passed": (
                    bool(queen_handoff_quality.get("actionable_next_action"))
                    if self.goal_plan.get("action_required")
                    else bool(queen_handoff_quality.get("next_action_present"))
                ) if self.goal_plan.get("ontology_required") else True,
                "reason": "Queen supplied a bounded executable next action for this goal",
            },
            {
                "check": "graph_path",
                "passed": not self.goal_plan.get("graph_required") or graph_gate == "pass",
                "reason": "bounded typed path when the goal requires graph reasoning",
            },
        ]
        stop_reasons = []
        if len(active_leases) > 1:
            stop_reasons.append("multiple_active_leases")
        if len(failed_attempts) >= 2:
            stop_reasons.append("repeated_failed_attempts")
        if duplicate_artifacts:
            stop_reasons.append("duplicate_artifact_digest")
        if claim_gate == "blocked" and not metadata_only_lookup:
            stop_reasons.append("authoritative_claim_gate_blocked")
        if self.goal_plan.get("graph_required") and graph_gate != "pass":
            stop_reasons.append("graph_path_gate_blocked")
        if self.goal_plan.get("ontology_required") and not queen_handoff_quality.get("accepted"):
            stop_reasons.append("queen_handoff_gate_blocked")
        if self.goal_plan.get("ontology_required") and contract_gate != "pass":
            stop_reasons.append("ontology_slot_coverage_blocked")
        if self.goal_plan.get("ontology_required") and decision_gate != "pass":
            stop_reasons.append("decision_slot_gate_blocked")
        if self.goal_plan.get("ontology_required") and not ledger_ok:
            stop_reasons.append("ontology_ledger_readback_blocked")
        if self.goal_plan.get("action_required") and not queen_handoff_quality.get("actionable_next_action"):
            stop_reasons.append("goal_actionability_gate_blocked")
        if not all(bool(item["passed"]) for item in judge_checks):
            stop_reasons.append("judge_gate_blocked")
        decision = "stop" if stop_reasons else "continue"
        report = {
            "decision": decision,
            "duplicate_active_attempts": max(0, len(active_leases) - 1),
            "active_lease_count": len(active_leases),
            "failed_attempt_count": len(failed_attempts),
            "duplicate_artifact_count": duplicate_artifacts,
            "graph_gate": graph_gate,
            "observation_gate": self.opencrab_receipt.get("observation_gate", "not_required"),
            "observation_mode": "metadata_observation" if metadata_only_lookup else "evidence",
            "ontology_contract": {
                "coverage_gate": contract_gate,
                "decision_gate": decision_gate,
                "missing_slot_ids": contract_coverage.get("missing_slot_ids") or [],
                "filled_required_slot_count": contract_coverage.get("filled_required_slot_count", 0),
                "required_slot_count": contract_coverage.get("required_slot_count", 0),
            },
            "ontology_ledger": {
                "artifact_kind": ONTOLOGY_LEDGER_KIND,
                "artifact_id": (ledger_readback or {}).get("_artifact", {}).get("artifact_id"),
                "mission_id": (ledger_readback or {}).get("mission_id"),
                "goal_graph_id": (ledger_readback or {}).get("goal_graph_id"),
                "revision": (ledger_readback or {}).get("revision"),
                "readback_validated": ledger_ok,
            },
            "context_quality": context_quality,
            "queen_handoff_quality": queen_handoff_quality,
            "automatic_revision": repair,
            "judge_checks": judge_checks,
            "queen_next_action": queen_handoff_quality.get("next_action", ""),
            "external_scouting": False,
            "model_invocation": False,
            "stop_reasons": stop_reasons,
            "reason": "local receipts and claim gate were healthy" if not stop_reasons else "local patrol found a stop condition",
        }
        if self.goal_plan.get("ontology_required"):
            report["ontology_ledger_artifact_id"] = (
                (ledger_readback or {}).get("_artifact", {}).get("artifact_id")
            )
        artifact = self.service._write_artifact(mission_id, task["task_id"], task["output_contract"], json.dumps(report, ensure_ascii=False, indent=2) + "\n", "soldier_patrol")
        self.store.add_tool_receipt(mission_id, task["task_id"], attempt["attempt_id"], "crab.soldier.local_patrol", "success", {**report, "artifact_id": artifact.artifact_id})
        if decision == "stop":
            self.store.finish_attempt(mission_id, attempt["attempt_id"], attempt["lease_id"], AttemptStatus.STOPPED, ";".join(stop_reasons))
            self.store.set_task_status(task["task_id"], TaskStatus.STOPPED, role)
            self.store.append_event(mission_id, "soldier_stop_gate", role.value, report)
            self.store.cancel_active_mission(mission_id, "soldier_stop_gate")
        else:
            self.store.finish_attempt(mission_id, attempt["attempt_id"], attempt["lease_id"], AttemptStatus.SUCCEEDED)
            self.store.set_task_status(task["task_id"], TaskStatus.COMPLETED, role)
        self.store.set_assignment_invocation(task["task_id"], "local_rule", model="none", effort="deterministic")
        return artifact.artifact_id
