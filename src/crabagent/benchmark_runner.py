from __future__ import annotations

import json
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .benchmark import compare_observed
from .goal import classify_goal
from .opencrab import opencrab_is_configured
from .protocol import DaemonClient, start_daemon
from .runtime import RuntimeService
from .workspace_observer import capture_workspace, diff_workspace


TERMINAL_MISSION_STATUSES = {"completed", "failed", "cancelled"}


def _session_event(events: List[Dict[str, Any]], session_id: str) -> Optional[Dict[str, Any]]:
    candidates = [
        row
        for row in events
        if isinstance(row, dict)
        and (row.get("payload") or {}).get("session_id") == session_id
        and str(row.get("event_type") or "").startswith("conversation_turn_")
    ]
    return candidates[-1] if candidates else None


def _session_events(client: DaemonClient, session_id: str) -> List[Dict[str, Any]]:
    # The daemon caps one response page at 2,000 rows. Walk the durable event
    # cursor instead of silently stopping at the first page; otherwise a
    # completion event can be missed and the runner waits forever.
    observed: List[Dict[str, Any]] = []
    cursor = 0
    for _ in range(20):
        result = client.request("events.since", event_id=cursor, limit=2000)
        page = [row for row in result.get("events") or [] if isinstance(row, dict)]
        if not page:
            break
        observed.extend(page)
        next_cursor = max(int(row.get("event_id") or 0) for row in page)
        if next_cursor <= cursor or len(page) < 2000:
            break
        cursor = next_cursor
    return [
        row
        for row in observed
        if (row.get("payload") or {}).get("session_id") == session_id
    ]


def _wait_for_direct(client: DaemonClient, session_id: str, timeout: float) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    deadline = time.monotonic() + max(10.0, timeout)
    while time.monotonic() < deadline:
        events = _session_events(client, session_id)
        event = _session_event(events, session_id)
        if event is not None:
            return client.request("session.snapshot", session_id=session_id), events
        snapshot = client.request("session.snapshot", session_id=session_id)
        session = snapshot.get("session") or {}
        messages = snapshot.get("messages") or []
        latest = messages[-1] if messages else {}
        # A completed turn is durable in the message log even if an old or
        # truncated event page omitted its completion event. Returning here
        # keeps the result honest: _direct_snapshot will mark missing usage or
        # receipt data unknown instead of waiting indefinitely.
        if (
            str(session.get("status") or "") == "ready"
            and str(latest.get("role") or "") == "assistant"
            and isinstance(latest.get("metadata"), dict)
            and latest.get("metadata", {}).get("turn_id")
        ):
            return snapshot, events
        time.sleep(0.5)
    raise TimeoutError("direct baseline did not finish within %.0fs" % timeout)


def _wait_for_adaptive(client: DaemonClient, session_id: str, timeout: float) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    deadline = time.monotonic() + max(10.0, timeout)
    submitted_at = time.monotonic()
    while time.monotonic() < deadline:
        snapshot = client.request("session.snapshot", session_id=session_id)
        mission = snapshot.get("mission") or {}
        mission_row = mission.get("mission") if isinstance(mission, dict) else {}
        if str((mission_row or {}).get("status") or "") in TERMINAL_MISSION_STATUSES:
            return snapshot, _session_events(client, session_id)
        # The daemon starts the worker thread before the mission row is
        # created. Allow that short durable-runtime handoff window instead of
        # mistaking a healthy start for a failed benchmark.
        if (
            time.monotonic() - submitted_at > 8.0
            and str((snapshot.get("session") or {}).get("status") or "") == "ready"
            and not mission
        ):
            raise RuntimeError("KINGCRAB adaptive session returned ready without creating a mission")
        time.sleep(0.5)
    raise TimeoutError("KINGCRAB mission did not finish within %.0fs" % timeout)


def _receipt_evidence(receipt: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [
        {
            "evidence_id": row.get("id"),
            "source": row.get("source"),
            "text": row.get("text"),
        }
        for row in receipt.get("evidence") or []
        if isinstance(row, dict) and row.get("id")
    ]


def _answer_grounding_quality(answer: str, receipt: Dict[str, Any]) -> Dict[str, Any]:
    """Measure whether a direct answer cites the observed MCP evidence."""
    evidence_ids = [
        str(row.get("id") or "")
        for row in receipt.get("evidence") or []
        if isinstance(row, dict) and row.get("id")
    ]
    prefix_counts: Dict[str, int] = {}
    for evidence_id in evidence_ids:
        if len(evidence_id) >= 8:
            prefix_counts[evidence_id[:8]] = prefix_counts.get(evidence_id[:8], 0) + 1
    cited: List[str] = []
    modes: Dict[str, str] = {}
    text = str(answer or "")
    for evidence_id in evidence_ids:
        if evidence_id in text:
            cited.append(evidence_id)
            modes[evidence_id] = "full_id"
        elif len(evidence_id) >= 8 and prefix_counts.get(evidence_id[:8]) == 1 and evidence_id[:8] in text:
            cited.append(evidence_id)
            modes[evidence_id] = "unique_id_prefix"
    return {
        "accepted": bool(cited),
        "evidence_citation_count": len(cited),
        "cited_evidence_ids": cited[:8],
        "citation_modes": {key: modes[key] for key in cited[:8]},
    }


def _answer_shape_quality(answer: str, plan: Dict[str, Any]) -> Dict[str, Any]:
    """Check the minimum executable shape without judging semantic truth."""
    if not plan.get("action_required"):
        return {"accepted": True, "ordered_step_count": 0, "reason": "not_required"}
    lines = [line.strip() for line in str(answer or "").splitlines() if line.strip()]
    ordered_steps = [line for line in lines if re.match(r"^(?:[-*]|\d+[.)]|[가-힣A-Za-z]\.)\s+", line)]
    accepted = len(ordered_steps) >= 2 or bool(re.search(r"plan\s*[-→>]\s*act|plan\s*→\s*observe", str(answer), re.IGNORECASE))
    return {
        "accepted": accepted,
        "ordered_step_count": len(ordered_steps),
        "reason": "ordered_action_path_observed" if accepted else "ordered_action_path_missing",
    }


def _direct_result_accepted(plan: Dict[str, Any], receipt: Dict[str, Any], event: Optional[Dict[str, Any]], response: str) -> bool:
    if not response.strip() or str((event or {}).get("event_type") or "") != "conversation_turn_completed":
        return False
    if not plan.get("ontology_required"):
        return True
    if str(receipt.get("claim_gate") or "blocked") != "pass":
        return False
    if not _answer_grounding_quality(response, receipt).get("accepted"):
        return False
    if not _answer_shape_quality(response, plan).get("accepted"):
        return False
    return not plan.get("graph_required") or str(receipt.get("graph_gate") or "blocked") == "pass"


def _direct_snapshot(
    root: Path,
    run_id: str,
    objective: str,
    plan: Dict[str, Any],
    session_snapshot: Dict[str, Any],
    events: List[Dict[str, Any]],
    workspace_change: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    session = session_snapshot.get("session") or {}
    event = _session_event(events, str(session.get("session_id") or ""))
    payload = (event or {}).get("payload") if isinstance(event, dict) else {}
    payload = payload if isinstance(payload, dict) else {}
    receipt = payload.get("opencrab_receipt") if isinstance(payload.get("opencrab_receipt"), dict) else {}
    messages = session_snapshot.get("messages") or []
    answer = ""
    for message in reversed(messages):
        if str(message.get("role") or "") == "assistant":
            answer = str(message.get("content") or "")
            break
    answer_path = root / "direct-answer.txt"
    answer_path.write_text(answer + ("\n" if answer else ""), encoding="utf-8")
    answer_grounding = _answer_grounding_quality(answer, receipt) if plan.get("ontology_required") else {
        "accepted": True,
        "evidence_citation_count": 0,
        "cited_evidence_ids": [],
        "citation_modes": {},
    }
    answer_shape = _answer_shape_quality(answer, plan)
    tool_receipts: List[Dict[str, Any]] = []
    for call in receipt.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        tool_receipts.append(
            {
                "tool_name": "opencrab.mcp.%s" % str(call.get("tool") or "unknown"),
                "status": "success" if call.get("status") in {"observed", "cached"} else "failed",
                "observed": call,
            }
        )
    if event is not None:
        tool_receipts.append(
            {
                "tool_name": "codex.app-server.turn",
                "status": "success" if event.get("event_type") == "conversation_turn_completed" else "failed",
                "observed": {
                    "thread_id": payload.get("thread_id"),
                    "turn_id": payload.get("turn_id"),
                    "model": payload.get("model"),
                    "usage": payload.get("usage") or {},
                    "prompt_chars": payload.get("prompt_chars", 0),
                    "context_chars": payload.get("context_chars", 0),
                },
            }
        )
    tokens = payload.get("tokens_observed")
    result_accepted = _direct_result_accepted(plan, receipt, event, answer)
    if plan.get("requires_write"):
        result_accepted = result_accepted and bool((workspace_change or {}).get("changed_file_count"))
    artifacts = [
        {
            "kind": "direct_answer",
            "status": "accepted" if answer else "candidate",
            "path": str(answer_path),
        }
    ]
    if workspace_change is not None:
        artifacts.append(
            {
                "kind": "workspace_change",
                "status": "accepted" if workspace_change.get("changed_file_count") else "candidate",
                "path": str(root / "direct-workspace-change.json"),
            }
        )
        (root / "direct-workspace-change.json").write_text(
            json.dumps(workspace_change, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return {
        "schema": "crab.observed-benchmark-snapshot/v1",
        "benchmark": {
            "run_id": run_id,
            "workflow": "direct_model_with_opencrab",
            "session_id": session.get("session_id"),
            "result_accepted": result_accepted,
            "answer_path": str(answer_path),
            "answer_grounding": answer_grounding,
            "answer_shape": answer_shape,
        },
        "mission": {
            "mission_id": "benchmark-direct-%s" % run_id,
            "objective": objective,
            "status": "completed" if event and event.get("event_type") == "conversation_turn_completed" else "failed",
            "oracle_result_artifact_id": None,
        },
        "goal_plan": plan,
        "budget": {
            "tokens_observed": tokens if isinstance(tokens, int) else None,
            "observation_source": "codex_app_server" if isinstance(tokens, int) else "not_observed",
        },
        "tool_receipts": tool_receipts,
        "artifacts": artifacts,
        "evidence": _receipt_evidence(receipt),
        "answer_grounding": answer_grounding,
        "answer_shape": answer_shape,
        "workspace_change": workspace_change,
        "paths": receipt.get("paths") if isinstance(receipt.get("paths"), list) else [],
        "events": events,
    }


def _same_context(client: DaemonClient, source_session_id: str, target_session_id: str, source: Dict[str, Any]) -> Dict[str, Any]:
    context = source.get("session", {}).get("ontology_context") if isinstance(source.get("session"), dict) else {}
    context = context if isinstance(context, dict) else {}
    client.request(
        "ontology.context",
        session_id=target_session_id,
        project_ids=list(context.get("project_ids") or []),
        package_ids=list(context.get("package_ids") or []),
        project_labels=dict(context.get("project_labels") or {}),
        package_labels=dict(context.get("package_labels") or {}),
    )
    return {
        "source_session_id": source_session_id or None,
        "project_ids": list(context.get("project_ids") or []),
        "package_ids": list(context.get("package_ids") or []),
        "project_count": len(context.get("project_ids") or []),
        "package_count": len(context.get("package_ids") or []),
    }


def _copy_for_write_benchmark(source: Path, destination: Path, *, excluded_top_level: Optional[List[str]] = None) -> None:
    """Create a clean sibling source tree while excluding CrabAgent runtime state."""
    if destination.exists():
        raise RuntimeError("benchmark destination already exists: %s" % destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not source.is_dir():
        raise RuntimeError("write benchmark workspace is not a directory: %s" % source)
    ignored = [".crabagent", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"]
    ignored.extend(str(name) for name in (excluded_top_level or []) if str(name).strip())
    shutil.copytree(
        source,
        destination,
        symlinks=True,
        ignore=shutil.ignore_patterns(*sorted(set(ignored))),
    )


def _mission_workspace_change(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    change = snapshot.get("workspace_change")
    if isinstance(change, dict):
        return change
    for artifact in snapshot.get("artifacts") or []:
        if not isinstance(artifact, dict) or str(artifact.get("kind") or "") != "workspace_change":
            continue
        try:
            value = json.loads(Path(str(artifact.get("path") or "")).read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if isinstance(value, dict):
            return value
    return {}


def run_observed_benchmark(
    workspace: Path,
    objective: str,
    *,
    source_session_id: str = "",
    timeout_seconds: float = 900.0,
    output_dir: Optional[Path] = None,
    order: str = "direct-first",
) -> Dict[str, Any]:
    """Run a same-context direct-vs-colony comparison with real receipts.

    The caller must explicitly choose this function through the CLI's
    ``--execute`` flag because it can spend provider tokens twice.
    """
    order = str(order or "direct-first").strip().lower()
    if order not in {"direct-first", "adaptive-first"}:
        raise ValueError("order must be direct-first or adaptive-first")
    workspace = workspace.resolve()
    RuntimeService(workspace).initialize()
    source_client = start_daemon(workspace)
    source = source_client.request("session.snapshot", session_id=source_session_id)
    source_session = source.get("session") or {}
    context = source_session.get("ontology_context") if isinstance(source_session, dict) else {}
    plan = classify_goal(
        objective,
        selected_pack_count=len((context or {}).get("package_ids") or []),
        selected_project_count=len((context or {}).get("project_ids") or []),
        knowledge_available=opencrab_is_configured(
            workspace,
            str(source_session.get("mcp_policy") or "auto"),
        ),
    ).to_dict()
    run_id = "bench-%s" % uuid.uuid4().hex[:12]
    root = (output_dir or (workspace / ".crabagent" / "benchmarks" / run_id)).resolve()
    root.mkdir(parents=True, exist_ok=True)
    isolated_write = bool(plan.get("requires_write"))
    direct_workspace = workspace
    adaptive_workspace = workspace
    owned_clients: List[DaemonClient] = []
    direct_client = source_client
    adaptive_client = source_client
    isolation: Dict[str, Any] = {
        "same_start_state": True,
        "mode": "shared_read_only_workspace" if not isolated_write else "isolated_workspace_copies",
        "reason": "write goals are copied before either route runs" if isolated_write else "goal has no planned workspace write",
    }
    if isolated_write:
        direct_workspace = root / "direct-workspace"
        adaptive_workspace = root / "adaptive-workspace"
        excluded_top_level: List[str] = []
        try:
            relative_root = root.relative_to(workspace)
            if relative_root.parts:
                excluded_top_level.append(relative_root.parts[0])
        except ValueError:
            pass
        _copy_for_write_benchmark(workspace, direct_workspace, excluded_top_level=excluded_top_level)
        _copy_for_write_benchmark(workspace, adaptive_workspace, excluded_top_level=excluded_top_level)
        direct_client = start_daemon(direct_workspace)
        adaptive_client = start_daemon(adaptive_workspace)
        owned_clients.extend([direct_client, adaptive_client])
        isolation.update({"direct_workspace": str(direct_workspace), "adaptive_workspace": str(adaptive_workspace)})

    def create_session(client: DaemonClient, title: str, interaction_mode: str) -> Dict[str, Any]:
        return client.request(
            "session.create",
            title=title,
            model_policy=str(source_session.get("model_policy") or "auto"),
            interaction_mode=interaction_mode,
            mcp_policy=str(source_session.get("mcp_policy") or "auto"),
            cli_policy=str(source_session.get("cli_policy") or "auto"),
            max_workers=int(source_session.get("max_workers") or 3),
            worker_policy=str(source_session.get("worker_policy") or "auto"),
        )

    try:
        direct_session = create_session(direct_client, "%s · direct baseline" % objective[:48], "chat")
        adaptive_session = create_session(adaptive_client, "%s · KINGCRAB adaptive" % objective[:40], "colony")
        direct_context = _same_context(direct_client, source_session_id, str(direct_session["session_id"]), source)
        adaptive_context = _same_context(adaptive_client, source_session_id, str(adaptive_session["session_id"]), source)
        same_context = {
            "direct": direct_context,
            "adaptive": adaptive_context,
            "equal_scope": direct_context.get("project_ids") == adaptive_context.get("project_ids")
            and direct_context.get("package_ids") == adaptive_context.get("package_ids"),
        }

        direct_id = str(direct_session["session_id"])
        adaptive_id = str(adaptive_session["session_id"])

        def run_direct() -> Dict[str, Any]:
            before = capture_workspace(direct_workspace) if isolated_write else None
            direct_client.request("prompt.submit", session_id=direct_id, objective=objective, disposition="start", interaction="chat", benchmark_direct=True)
            direct_live, direct_events = _wait_for_direct(direct_client, direct_id, timeout_seconds)
            change = diff_workspace(before, capture_workspace(direct_workspace)) if before is not None else None
            return _direct_snapshot(root, run_id, objective, plan, direct_live, direct_events, change)

        def run_adaptive() -> Dict[str, Any]:
            adaptive_client.request("prompt.submit", session_id=adaptive_id, objective=objective, disposition="start", interaction="colony")
            adaptive_live, adaptive_events = _wait_for_adaptive(adaptive_client, adaptive_id, timeout_seconds)
            adaptive_snapshot = adaptive_live.get("mission") or {}
            adaptive_snapshot["schema"] = "crab.observed-benchmark-snapshot/v1"
            change = _mission_workspace_change(adaptive_snapshot) if isolated_write else {}
            accepted = bool(
                ((adaptive_snapshot.get("mission") or {}).get("status") == "completed")
                and ((adaptive_snapshot.get("mission") or {}).get("oracle_result_artifact_id"))
            )
            if isolated_write:
                accepted = accepted and bool(change.get("changed_file_count"))
                adaptive_snapshot["workspace_change"] = change
            adaptive_snapshot["benchmark"] = {
                "run_id": run_id,
                "workflow": "goal_compiled_kinetic",
                "session_id": adaptive_id,
                "result_accepted": accepted,
            }
            adaptive_snapshot["events"] = adaptive_events
            return adaptive_snapshot

        if order == "adaptive-first":
            adaptive_snapshot = run_adaptive()
            direct_snapshot = run_direct()
        else:
            direct_snapshot = run_direct()
            adaptive_snapshot = run_adaptive()

        (root / "direct.json").write_text(json.dumps(direct_snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (root / "adaptive.json").write_text(json.dumps(adaptive_snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        comparison = compare_observed(adaptive_snapshot, direct_snapshot)
        result = {
            "schema": "crab.observed-benchmark/v1",
            "run_id": run_id,
            "objective": objective,
            "order": order,
            "same_context": same_context,
            "isolation": isolation,
            "plan": plan,
            "comparison": comparison,
            "paths": {
                "root": str(root),
                "direct": str(root / "direct.json"),
                "adaptive": str(root / "adaptive.json"),
                "comparison": str(root / "comparison.json"),
            },
            "guardrail": "Observed provider usage, same-start-state isolation for writes, and both result gates are required before claiming KINGCRAB superiority.",
        }
        (root / "comparison.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return result
    finally:
        for owned_client in owned_clients:
            try:
                owned_client.request("shutdown")
            except (OSError, RuntimeError, ValueError):
                pass
