from __future__ import annotations

import json
import hashlib
import os
import signal
import shutil
import socketserver
import threading
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional

import typer

from . import __version__
from .identity import COLONY_PROTOCOL_VERSION
from .codex_app_server import CodexAppServerSession
from .colony import ColonyExecutor
from .conversation import ConversationExecutor, interaction_kind
from .continuation import continuation_intent
from .discovery import mcp_inventory, observed_assets
from .folder_ingest import OpenCrabPackBuilder, remote_package_id, remote_status, upload_session_id_from_result
from .goal import classify_goal
from .opencrab import OpenCrabInspector, OpenCrabMcpClient, OpenCrabUnavailable, filter_inventory_for_display, opencrab_is_configured, opencrab_url
from .opencrab_snapshot import SnapshotPersistenceError, compact_diff, compact_snapshot, local_diff_result, local_snapshot_result, persist_sync
from .ontology_context import OntologyContextCollector
from .onboarding import (
    defer_opencrab,
    mark_local_ready,
    record_opencrab_connected,
    status as onboarding_status,
    write_endpoint,
)
from .orchestration import OrchestrationStore, normalize_children, orchestration_id, summarize, utc_now
from .protocol import runtime_paths, runtime_revision
from .runtime import RuntimeService


class RuntimeRequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        try:
            raw = self.rfile.readline(1024 * 1024)
            request = json.loads(raw.decode("utf-8"))
            result = self.server.dispatch(request)  # type: ignore[attr-defined]
            response = {"ok": True, "result": result}
        except Exception as exc:
            response = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
        try:
            self.wfile.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
        except (BrokenPipeError, ConnectionResetError):
            # A client may close its panel while a remote catalog request is
            # still finishing. The durable daemon must keep serving other clients.
            return


def _king_title(objective: str) -> str:
    words = re.findall(r"[A-Za-z0-9가-힣][A-Za-z0-9가-힣._-]*", objective)
    ignored = {"the", "and", "for", "with", "이", "그", "를", "을", "은", "는", "에", "의", "좀", "해", "하기"}
    keywords = [word for word in words if word.lower() not in ignored][:5]
    return " ".join(keywords)[:72] or "Untitled mission"


def _mcp_policy_with(policy: str, server: str, enabled: bool, configured: list[str]) -> str:
    if not enabled:
        if policy.startswith("allow:"):
            names = [name for name in policy[6:].split(",") if name and name != server]
            return "allow:" + ",".join(names) if names else "off"
        if policy == "off":
            return "off"
        return "allow:" + ",".join(name for name in configured if name != server)
    names = [] if policy in {"auto", "all", "off"} else [name for name in policy[6:].split(",") if name]
    if server not in names:
        names.append(server)
    return "allow:" + ",".join(names)


def _crab_doc_root(workspace: Path) -> Path:
    return workspace / ".crabagent" / "documents"


def _crab_doc_markdown(document: Dict[str, Any]) -> str:
    text = str(document.get("text") or "").strip()
    if text:
        return text + "\n"
    sections = ["# %s" % (document.get("title") or document.get("fileName") or "CRAB DOC document"), ""]
    for sheet in document.get("sheets") or []:
        sections.append("## %s" % (sheet.get("name") or "Sheet"))
        for row in sheet.get("rows") or []:
            sections.append("| %s |" % " | ".join(str(cell or "").replace("|", "\\|") for cell in row))
        sections.append("")
    for slide in document.get("slides") or []:
        sections.extend(["## Slide %s" % slide.get("index", ""), str(slide.get("text") or ""), ""])
    return "\n".join(sections).rstrip() + "\n"


def _validate_crab_doc_manifest(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("CRAB DOC manifest must be an object")
    if value.get("protocol") != "crab-doc/v1":
        raise ValueError("unsupported CRAB DOC protocol")
    if value.get("product") != "CRAB DOC":
        raise ValueError("manifest product must be CRAB DOC")
    document = value.get("document")
    if not isinstance(document, dict):
        raise ValueError("CRAB DOC manifest document is missing")
    return value


def _crab_doc_import(workspace: Path, manifest: Dict[str, Any]) -> Dict[str, Any]:
    document = manifest["document"]
    source = str(manifest.get("sourceFileName") or document.get("fileName") or "document")
    title = str(document.get("title") or source)
    digest = hashlib.sha256(json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    doc_id = "doc-" + digest[:16]
    root = _crab_doc_root(workspace)
    root.mkdir(parents=True, exist_ok=True)
    manifest_path = root / (doc_id + ".crabdoc.json")
    markdown_path = root / (doc_id + ".md")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown_path.write_text(_crab_doc_markdown(document), encoding="utf-8")
    return {
        "status": "ok",
        "doc_id": doc_id,
        "title": title,
        "source_file_name": source,
        "format": str(document.get("extension") or "").lower(),
        "parser": str(document.get("parser") or "unknown"),
        "manifest_path": str(manifest_path),
        "markdown_path": str(markdown_path),
    }


def _crab_doc_list(workspace: Path) -> Dict[str, Any]:
    root = _crab_doc_root(workspace)
    documents = []
    if root.exists():
        for path in sorted(root.glob("*.crabdoc.json"), key=lambda item: item.stat().st_mtime, reverse=True):
            try:
                manifest = _validate_crab_doc_manifest(json.loads(path.read_text(encoding="utf-8")))
                document = manifest["document"]
                documents.append({
                    "doc_id": path.name.removesuffix(".crabdoc.json"),
                    "title": document.get("title") or document.get("fileName"),
                    "source_file_name": manifest.get("sourceFileName") or document.get("fileName"),
                    "format": document.get("extension"),
                    "parser": document.get("parser"),
                    "manifest_path": str(path),
                    "markdown_path": str(path.with_suffix("").with_suffix(".md")),
                    "created_at": manifest.get("createdAt"),
                })
            except (OSError, ValueError, json.JSONDecodeError):
                continue
    return {"status": "ok", "count": len(documents), "documents": documents, "root": str(root)}


class RuntimeServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, workspace: Path, socket_path: Path, *, opencrab_inspector: Optional[OpenCrabInspector] = None, pack_builder: Optional[OpenCrabPackBuilder] = None) -> None:
        self.workspace = workspace.resolve()
        self.service = RuntimeService(self.workspace)
        self.service.initialize()
        self.service.store.expire_pending_requests()
        self.service.store.reconcile_interrupted_runtime()
        self._bridges: Dict[str, CodexAppServerSession] = {}
        self._jobs: Dict[str, threading.Thread] = {}
        self._cancels: Dict[str, threading.Event] = {}
        self._pack_watchers: Dict[str, threading.Thread] = {}
        self._pack_watch_stop = threading.Event()
        self._orchestrations = OrchestrationStore(self.workspace)
        self._orchestration_threads: Dict[str, threading.Thread] = {}
        self._orchestration_stops: Dict[str, threading.Event] = {}
        self._reconnect_sessions: set[str] = set()
        self._state_lock = threading.RLock()
        self.opencrab_inspector = opencrab_inspector or OpenCrabInspector(workspace=self.workspace)
        self.pack_builder = pack_builder or OpenCrabPackBuilder(self.opencrab_inspector.client_factory, self.workspace)
        self._orchestrations.reconcile_after_restart()
        super().__init__(str(socket_path), RuntimeRequestHandler)

    def server_close(self) -> None:
        self._pack_watch_stop.set()
        with self._state_lock:
            for stop in self._orchestration_stops.values():
                stop.set()
        super().server_close()

    def _observe_pack_upload(self, record: Dict[str, Any], upload_session_id: str = "") -> Dict[str, Any]:
        """Poll one saved upload session and persist the observed terminal state."""
        saved = record.get("result") or {}
        remote = saved.get("remote") or {}
        session_id = str(
            upload_session_id
            or saved.get("upload_session_id")
            or upload_session_id_from_result(remote)
            or ""
        )
        if not session_id:
            return record
        observed = self.pack_builder.poll_upload(session_id)
        observed_status = remote_status(observed)
        package_id = remote_package_id(observed)
        updated = dict(saved)
        updated["upload_session_id"] = session_id
        updated["remote_status"] = observed_status
        updated["remote_upload"] = observed
        if package_id and observed_status in {"ok", "success", "completed", "ingested"}:
            updated["status"] = "ingested"
            updated["package_id"] = package_id
            updated["reason"] = "OpenCrab returned a confirmed package_id."
        elif observed_status in {"error", "failed", "rejected", "cancelled"}:
            updated["status"] = "upload_failed"
            updated["reason"] = str(
                observed.get("reason")
                or observed.get("detail")
                or "OpenCrab upload or reverse ingest failed"
            )
        else:
            updated["status"] = "upload_pending"
            updated["reason"] = "OpenCrab has not returned a confirmed package_id yet."
        self.service.store.record_pack_ingest_run(
            str(record["run_id"]),
            str(record.get("project_id") or ""),
            str(record.get("folder_path") or ""),
            str(updated["status"]),
            updated,
        )
        return self.service.store.pack_ingest_run(str(record["run_id"])) or record

    def _start_pack_upload_watch(self, record: Dict[str, Any]) -> None:
        """Keep a short, durable background watch independent of the TUI panel."""
        run_id = str(record.get("run_id") or "")
        saved = record.get("result") or {}
        remote = saved.get("remote") or {}
        upload_session_id = str(
            saved.get("upload_session_id")
            or upload_session_id_from_result(remote)
            or ""
        )
        if not run_id or not upload_session_id:
            return
        with self._state_lock:
            existing = self._pack_watchers.get(run_id)
            if existing is not None and existing.is_alive():
                return

            def watch() -> None:
                try:
                    # The local builder/upload handoff may require user action;
                    # watch for a bounded window and leave the durable record
                    # available for an explicit `crab pack status` afterwards.
                    for _ in range(30):
                        if self._pack_watch_stop.is_set():
                            return
                        time.sleep(2.0)
                        if self._pack_watch_stop.is_set():
                            return
                        current = self.service.store.pack_ingest_run(run_id)
                        if current is None:
                            return
                        current_status = str(current.get("status") or "")
                        if current_status in {"ingested", "upload_failed", "cancelled"}:
                            return
                        observed = self._observe_pack_upload(current, upload_session_id)
                        if str(observed.get("status") or "") in {"ingested", "upload_failed", "cancelled"}:
                            return
                finally:
                    with self._state_lock:
                        self._pack_watchers.pop(run_id, None)

            watcher = threading.Thread(
                target=watch,
                daemon=True,
                name="crab-pack-watch-%s" % run_id[-8:],
            )
            self._pack_watchers[run_id] = watcher
            watcher.start()

    def _session(self, session_id: str = "") -> Dict[str, Any]:
        session = self.service.store.session(session_id) if session_id else self.service.store.latest_session()
        session = session or self.service.store.create_session()
        session["ontology_context"] = self.service.store.ontology_context(str(session["session_id"]))
        return session

    def _bridge(self, session: Dict[str, Any]) -> CodexAppServerSession:
        session_id = str(session["session_id"])
        with self._state_lock:
            bridge = self._bridges.get(session_id)
            if bridge is None:
                executable = shutil.which("codex")
                if not executable:
                    raise RuntimeError("Codex CLI is unavailable; install or expose `codex` on PATH")
                bridge = CodexAppServerSession(
                    executable,
                    self.workspace,
                    thread_id=str(session.get("codex_thread_id") or ""),
                    mcp_policy=str(session.get("mcp_policy") or "auto"),
                    mcp_servers=[str(row["name"]) for row in mcp_inventory()],
                )
                self._bridges[session_id] = bridge
            return bridge

    def _finish_job(self, session_id: str) -> None:
        cancel = self._cancels.get(session_id)
        if cancel is not None and cancel.is_set():
            self.service.store.expire_pending_requests(session_id)
        with self._state_lock:
            self._jobs.pop(session_id, None)
            self._cancels.pop(session_id, None)
            if session_id in self._reconnect_sessions:
                bridge = self._bridges.pop(session_id, None)
                self._reconnect_sessions.discard(session_id)
                if bridge:
                    bridge.close()
        queued = self.service.store.queued_inputs(session_id)
        if queued:
            next_input = queued[0]
            self.service.store.mark_input(str(next_input["queue_id"]), "delivered")
            refreshed = self.service.store.session(session_id)
            if refreshed:
                self._dispatch(refreshed, str(next_input["content"]))

    def _launch_colony(self, session: Dict[str, Any], objective: str, *, retry_of: str = "") -> Dict[str, Any]:
        session_id = str(session["session_id"])
        self.service.store.update_session(session_id, title=_king_title(objective))
        with self._state_lock:
            running = self._jobs.get(session_id)
            if running is not None and running.is_alive():
                raise RuntimeError("session already has a running mission")
            cancel = threading.Event()
            self._cancels[session_id] = cancel

            def target() -> None:
                try:
                    executor = ColonyExecutor(
                        self.service,
                        session_id,
                        self._bridge(session),
                        cancel,
                        retry_of=retry_of,
                    )
                    executor.run(
                        objective,
                        int(session.get("max_workers") or 3),
                        str(session.get("worker_policy") or "auto"),
                    )
                finally:
                    self._finish_job(session_id)

            job = threading.Thread(target=target, daemon=True, name="crab-mission-%s" % session_id[-8:])
            self._jobs[session_id] = job
            job.start()
        return {
            "session_id": session_id,
            "status": "starting",
            "interaction": "colony",
            "objective": objective,
            "retry_of": retry_of or None,
        }

    def _retry_last_mission(self, session: Dict[str, Any], *, force: bool = False) -> Dict[str, Any]:
        """Retry only a failed, non-writing mission without duplicating edits."""
        session_id = str(session["session_id"])
        with self._state_lock:
            running = self._jobs.get(session_id)
            if running is not None and running.is_alive():
                raise RuntimeError("wait for the active turn before retrying")
        candidates = [
            row
            for row in self.service.store.list_missions(limit=100)
            if str(row.get("session_id") or "") == session_id
            and str(row.get("status") or "") in {"failed", "cancelled"}
        ]
        if not candidates:
            raise RuntimeError("no failed or cancelled mission is available for retry")
        source = candidates[0]
        detail = self.service.store.inspect(str(source["mission_id"]))
        changed_files = 0
        for artifact in detail.get("artifacts") or []:
            if str(artifact.get("kind") or "") != "workspace_change":
                continue
            try:
                payload = json.loads(Path(str(artifact.get("path") or "")).read_text(encoding="utf-8"))
            except (OSError, TypeError, ValueError):
                payload = {}
            changed_files = max(changed_files, int(payload.get("changed_file_count") or 0))
        if changed_files and not force:
            raise RuntimeError(
                "retry blocked: the previous mission changed %d file(s); review the workspace or use /retry force" % changed_files
            )
        objective = str(source.get("objective") or "").strip()
        if not objective:
            raise RuntimeError("retry blocked: previous objective is empty")
        self.service.store.append_event(
            str(source["mission_id"]),
            "mission_retry_requested",
            "USER",
            {"session_id": session_id, "force": bool(force), "changed_files": changed_files},
        )
        return self._launch_colony(session, objective, retry_of=str(source["mission_id"]))

    @staticmethod
    def _artifact_payload(detail: Dict[str, Any], kind: str) -> Dict[str, Any]:
        for row in reversed(detail.get("artifacts") or []):
            if str(row.get("kind") or "") != kind:
                continue
            try:
                value = json.loads(Path(str(row.get("path") or "")).read_text(encoding="utf-8"))
            except (OSError, TypeError, ValueError):
                return {}
            return value if isinstance(value, dict) else {}
        return {}

    @staticmethod
    def _changed_file_count(detail: Dict[str, Any]) -> int:
        changed = 0
        for row in detail.get("artifacts") or []:
            if str(row.get("kind") or "") != "workspace_change":
                continue
            try:
                payload = json.loads(Path(str(row.get("path") or "")).read_text(encoding="utf-8"))
            except (OSError, TypeError, ValueError):
                continue
            changed = max(changed, int(payload.get("changed_file_count") or 0))
        return changed

    def _continuation_source(self, session_id: str, prompt: str) -> Optional[Dict[str, Any]]:
        """Find the last substantive mission behind a terse follow-up.

        Older panel clients sent ``진행해``/``제대로 다시 실행해`` as a new
        prompt. That discarded the original objective and let the classifier
        create a meaningless mission. Skip those aliases when walking the
        durable history and recover the last mission with a real plan or
        outcome artifact.
        """
        if continuation_intent(prompt) is None:
            return None
        for row in self.service.store.missions_for_session(session_id, limit=40):
            objective = str(row.get("objective") or "").strip()
            if not objective or continuation_intent(objective) is not None:
                continue
            try:
                detail = self.service.store.inspect(str(row.get("mission_id") or ""))
            except (OSError, ValueError):
                continue
            has_contract = bool(
                self._artifact_payload(detail, "goal_outcome")
                or self._artifact_payload(detail, "goal_plan")
            )
            # Deterministic/demo and older missions may predate the compact
            # goal outcome artifact. Their durable mission/tasks/artifacts are
            # still a valid continuation anchor.
            has_durable_work = bool(
                str(row.get("status") or "") in {"completed", "failed", "cancelled"}
                and ((detail.get("tasks") or []) or (detail.get("artifacts") or []))
            )
            if has_contract or has_durable_work:
                return {"mission": row, "detail": detail}
        return None

    def _dispatch_continuation(self, session: Dict[str, Any], prompt: str) -> Optional[Dict[str, Any]]:
        """Resolve terse follow-ups without creating a context-free mission."""
        intent = continuation_intent(prompt)
        if intent is None:
            return None
        session_id = str(session.get("session_id") or "")
        source = self._continuation_source(session_id, prompt)
        if not source:
            message = (
                "CONTINUATION NEEDS A GOAL\n\n"
                "짧은 실행 명령만으로는 이어갈 원래 미션을 찾지 못했습니다.\n"
                "새 작업의 목표나 수정할 파일을 한 문장으로 입력해 주세요."
            )
            self.service.store.add_message(
                session_id,
                "user",
                prompt,
                None,
                {"interaction": "continuation", "intent": intent, "source_mission_id": None},
            )
            self.service.store.append_event(
                None,
                "mission_continuation_missing_source",
                "RUNTIME",
                {"session_id": session_id, "prompt": prompt, "intent": intent},
            )
            self.service.store.add_message(
                session_id,
                "assistant",
                message,
                None,
                {"interaction": "continuation", "status": "needs_input"},
            )
            return {
                "session_id": session_id,
                "status": "needs_input",
                "interaction": "continuation",
                "source_mission_id": None,
                "resumed": False,
                "next_action": "provide_objective",
            }
        mission = source["mission"]
        detail = source["detail"]
        mission_id = str(mission.get("mission_id") or "")
        objective = str(mission.get("objective") or "").strip()
        status = str(mission.get("status") or "unknown")
        outcome = self._artifact_payload(detail, "goal_outcome")
        next_action = str(outcome.get("next_action") or "").strip()
        if not next_action:
            next_action = str(outcome.get("conclusion") or "").strip()
        changed_files = self._changed_file_count(detail)

        self.service.store.add_message(
            session_id,
            "user",
            prompt,
            mission_id,
            {"interaction": "continuation", "intent": intent, "source_mission_id": mission_id},
        )

        if status == "completed":
            message = (
                "CONTINUATION READY\n\n"
                "이전 미션은 이미 완료되어 새 미션을 만들지 않았습니다.\n"
                "원래 목표: %s\n"
                "검증된 산출물과 Oracle 영수증은 그대로 보존되어 있습니다."
                % objective[:240]
            )
            if next_action and next_action.upper() != "STOP":
                message += "\n다음: %s" % next_action[:360]
            if intent == "retry":
                message += "\n처음부터 다시 실행하려면 원래 목표를 구체적으로 다시 보내세요."
            self.service.store.append_event(
                mission_id,
                "mission_continuation_resolved",
                "RUNTIME",
                {"session_id": session_id, "prompt": prompt, "intent": intent, "status": status},
            )
            self.service.store.add_message(
                session_id,
                "assistant",
                message,
                mission_id,
                {"interaction": "continuation", "source_mission_id": mission_id, "status": "ready"},
            )
            return {
                "session_id": session_id,
                "status": "ready",
                "interaction": "continuation",
                "source_mission_id": mission_id,
                "resumed": False,
                "next_action": next_action,
            }

        if status in {"failed", "cancelled"}:
            verdict = str(outcome.get("verdict") or "").lower()
            failure = str(outcome.get("failure") or "").strip()
            if changed_files or verdict == "blocked" or failure.startswith(("OpenCrab", "Oracle ontology")):
                reason = (
                    "기존 미션에 이미 %d개 파일 변경이 있어 자동 재실행을 막았습니다."
                    % changed_files
                    if changed_files
                    else "기존 미션의 근거/권한 게이트가 차단되어 자동 재시도를 막았습니다."
                )
                message = "CONTINUATION BLOCKED\n\n%s\n원래 목표: %s" % (reason, objective[:240])
                if failure:
                    message += "\n원인: %s" % failure[:360]
                self.service.store.append_event(
                    mission_id,
                    "mission_continuation_blocked",
                    "RUNTIME",
                    {
                        "session_id": session_id,
                        "prompt": prompt,
                        "intent": intent,
                        "status": status,
                        "changed_file_count": changed_files,
                        "failure": failure,
                    },
                )
                self.service.store.add_message(
                    session_id,
                    "assistant",
                    message,
                    mission_id,
                    {"interaction": "continuation", "source_mission_id": mission_id, "status": "blocked"},
                )
                return {
                    "session_id": session_id,
                    "status": "blocked",
                    "interaction": "continuation",
                    "source_mission_id": mission_id,
                    "resumed": False,
                    "next_action": "review_source_mission",
                }
            self.service.store.append_event(
                mission_id,
                "mission_continuation_retry_requested",
                "RUNTIME",
                {"session_id": session_id, "prompt": prompt, "intent": intent},
            )
            return self._launch_colony(session, objective, retry_of=mission_id)
        return None

    def _knowledge_available(self, session: Dict[str, Any]) -> bool:
        return opencrab_is_configured(
            self.workspace,
            str(session.get("mcp_policy") or "auto"),
        )

    def _launch_chat(self, session: Dict[str, Any], prompt: str, *, direct_opencrab: bool = False) -> Dict[str, Any]:
        session_id = str(session["session_id"])
        with self._state_lock:
            running = self._jobs.get(session_id)
            if running is not None and running.is_alive():
                raise RuntimeError("session already has a running turn")
            cancel = threading.Event()
            self._cancels[session_id] = cancel

            def target() -> None:
                try:
                    context = self.service.store.ontology_context(session_id)
                    ontology_receipt = None
                    if direct_opencrab:
                        plan = classify_goal(
                            prompt,
                            selected_pack_count=len(context.get("package_ids") or []),
                            selected_project_count=len(context.get("project_ids") or []),
                            knowledge_available=True,
                        )
                        if plan.ontology_required:
                            try:
                                mcp_client = OpenCrabMcpClient(opencrab_url(workspace=self.workspace))
                                collector = OntologyContextCollector(mcp_client.call_tool)
                                ontology_receipt = collector.collect(
                                    prompt,
                                    package_ids=[str(value) for value in context.get("package_ids") or []],
                                    graph_required=plan.graph_required,
                                    retrieval_contract=plan.retrieval_contract,
                                )
                            except Exception as exc:
                                ontology_receipt = {
                                    "schema": "crab.opencrab-context-receipt/v2",
                                    "authority": "direct_mcp_response",
                                    "status": "error",
                                    "claim_gate": "blocked",
                                    "graph_gate": "blocked" if plan.graph_required else "not_required",
                                    "grounding": "none",
                                    "evidence": [],
                                    "nodes": [],
                                    "edges": [],
                                    "paths": [],
                                    "quality": {},
                                    "tool_calls": [],
                                    "error_type": type(exc).__name__,
                                }
                            self.service.store.append_event(
                                None,
                                "benchmark_direct_opencrab_context",
                                "QUEEN",
                                {
                                    "session_id": session_id,
                                    "status": (ontology_receipt or {}).get("status"),
                                    "claim_gate": (ontology_receipt or {}).get("claim_gate"),
                                    "graph_gate": (ontology_receipt or {}).get("graph_gate"),
                                    "evidence_count": (ontology_receipt or {}).get("evidence_count", 0),
                                    "node_count": (ontology_receipt or {}).get("node_count", 0),
                                    "path_count": len((ontology_receipt or {}).get("paths") or []),
                                },
                            )
                    executor = ConversationExecutor(self.service.store, session_id, self._bridge(session), cancel)
                    executor.run(
                        prompt,
                        ontology_context=context,
                        ontology_receipt=ontology_receipt,
                        benchmark_direct=direct_opencrab,
                    )
                finally:
                    self._finish_job(session_id)

            job = threading.Thread(target=target, daemon=True, name="crab-chat-%s" % session_id[-8:])
            self._jobs[session_id] = job
            job.start()
        return {"session_id": session_id, "status": "starting", "interaction": "chat", "prompt": prompt}

    def _dispatch(self, session: Dict[str, Any], prompt: str, explicit: str = "", *, direct_opencrab: bool = False) -> Dict[str, Any]:
        continuation = self._dispatch_continuation(session, prompt)
        if continuation is not None:
            return continuation
        mode = explicit or str(session.get("interaction_mode") or "auto")
        context = self.service.store.ontology_context(str(session.get("session_id") or ""))
        kind = interaction_kind(
            prompt,
            mode,
            selected_pack_count=len(context.get("package_ids") or []),
            selected_project_count=len(context.get("project_ids") or []),
            knowledge_available=self._knowledge_available(session),
        )
        return self._launch_chat(session, prompt, direct_opencrab=direct_opencrab) if kind == "chat" else self._launch_colony(session, prompt)

    def _interrupt(self, session_id: str) -> Dict[str, Any]:
        with self._state_lock:
            cancel = self._cancels.get(session_id)
            bridge = self._bridges.get(session_id)
            if cancel:
                cancel.set()
            if bridge:
                bridge.interrupt()
        return {"session_id": session_id, "interrupted": bool(cancel)}

    def _apply_mcp_policy(self, session: Dict[str, Any], policy: str) -> Dict[str, Any]:
        updated = self.service.store.update_session(str(session["session_id"]), mcp_policy=policy)
        session_id = str(updated["session_id"])
        with self._state_lock:
            bridge = self._bridges.get(session_id)
            running = bool(self._jobs.get(session_id) and self._jobs[session_id].is_alive())
            if bridge is not None:
                bridge.mcp_policy = policy
                if running:
                    self._reconnect_sessions.add(session_id)
                else:
                    self._bridges.pop(session_id, None)
                    bridge.close()
        return updated

    def _orchestration_project_key(self, child: Dict[str, Any]) -> str:
        root = str(child.get("project_root") or "").strip()
        if not root:
            session = self.service.store.session(str(child.get("session_id") or "")) or {}
            root = str(session.get("project_root_path") or "").strip()
        if root:
            try:
                return "path:" + str(Path(root).expanduser().resolve())
            except OSError:
                return "path:" + root
        return "session:" + str(child.get("session_id") or "")

    def _orchestration_plan(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        default_session = str(payload.get("session_id") or "")
        raw_children = payload.get("children")
        if not isinstance(raw_children, list):
            objective = str(payload.get("objective") or "").strip()
            raw_children = [{"session_id": default_session, "objective": objective}]
        if not default_session:
            session = self._session("")
            default_session = str(session["session_id"])
        children = normalize_children(raw_children, default_session_id=default_session)
        for child in children:
            session = self.service.store.session(str(child["session_id"]))
            if session is None:
                raise ValueError("unknown orchestration session: %s" % child["session_id"])
            child["project_root"] = str(session.get("project_root_path") or "")
        max_parallel = max(1, min(int(payload.get("max_parallel") or 2), 8))
        run_id = str(payload.get("orchestration_id") or "").strip() or None
        if run_id and self._orchestrations.load(run_id) is not None:
            raise ValueError("orchestration already exists: %s" % run_id)
        state = {
            "schema": "crab.orchestration/v1",
            "orchestration_id": run_id or orchestration_id(),
            "title": str(payload.get("title") or "KINGCRAB orchestration")[:160],
            "status": "planned",
            "max_parallel": max_parallel,
            "created_at": utc_now(),
            "updated_at": None,
            "children": children,
            "stop_reason": None,
        }
        saved = self._orchestrations.save(state)
        self.service.store.append_event(
            None,
            "orchestration_planned",
            "KING",
            {"orchestration_id": saved["orchestration_id"], "child_count": len(children), "max_parallel": max_parallel},
        )
        return {"state": saved, "summary": summarize(saved)}

    def _orchestration_child_running(self, session_id: str) -> bool:
        with self._state_lock:
            job = self._jobs.get(session_id)
            return bool(job is not None and job.is_alive())

    def _orchestration_mark_child_finished(self, child: Dict[str, Any]) -> str:
        session_id = str(child.get("session_id") or "")
        if self._orchestration_child_running(session_id):
            return "running"
        mission = self.service.store.mission_for_session(session_id)
        mission_status = str((mission or {}).get("status") or "")
        if mission_status in {"failed", "cancelled", "stopped"}:
            child["error"] = child.get("error") or "child mission %s" % mission_status
            return "failed"
        return "completed"

    def _run_orchestration(self, run_id: str) -> None:
        stop = self._orchestration_stops.get(run_id)
        if stop is None:
            return
        try:
            while not stop.is_set():
                state = self._orchestrations.load(run_id)
                if state is None:
                    return
                children = state.get("children") or []
                active = [child for child in children if str(child.get("status") or "") == "running"]
                active_scopes = {self._orchestration_project_key(child) for child in active}
                for child in children:
                    if len(active) >= int(state.get("max_parallel") or 1):
                        break
                    if str(child.get("status") or "") != "pending":
                        continue
                    if self._orchestration_project_key(child) in active_scopes:
                        continue
                    session = self.service.store.session(str(child.get("session_id") or ""))
                    if session is None:
                        child["status"] = "failed"
                        child["error"] = "session no longer exists"
                        child["finished_at"] = utc_now()
                        continue
                    try:
                        result = self._dispatch(
                            session,
                            str(child.get("objective") or ""),
                            str(child.get("interaction") or ""),
                        )
                        child["status"] = "running" if self._orchestration_child_running(str(session["session_id"])) else "completed"
                        child["dispatch"] = result
                        child["started_at"] = child.get("started_at") or utc_now()
                        if child["status"] == "completed":
                            child["finished_at"] = utc_now()
                        active.append(child)
                        active_scopes.add(self._orchestration_project_key(child))
                        self.service.store.append_event(
                            None,
                            "orchestration_child_started",
                            "KING",
                            {"orchestration_id": run_id, "child_id": child.get("child_id"), "session_id": session["session_id"]},
                        )
                    except Exception as exc:
                        child["status"] = "failed"
                        child["error"] = "%s: %s" % (type(exc).__name__, exc)
                        child["finished_at"] = utc_now()
                for child in children:
                    if str(child.get("status") or "") != "running":
                        continue
                    if self._orchestration_child_running(str(child.get("session_id") or "")):
                        continue
                    child["status"] = self._orchestration_mark_child_finished(child)
                    child["finished_at"] = utc_now()
                    self.service.store.append_event(
                        None,
                        "orchestration_child_finished",
                        "ORACLE",
                        {"orchestration_id": run_id, "child_id": child.get("child_id"), "status": child.get("status")},
                    )
                if all(str(child.get("status") or "") in {"completed", "failed", "cancelled"} for child in children):
                    state["status"] = "failed" if any(str(child.get("status")) == "failed" for child in children) else "completed"
                    state["children"] = children
                    self._orchestrations.save(state)
                    self.service.store.append_event(None, "orchestration_finished", "ORACLE", summarize(state))
                    return
                state["children"] = children
                self._orchestrations.save(state)
                time.sleep(0.2)
            state = self._orchestrations.load(run_id)
            if state is not None:
                state["status"] = "cancelled"
                state["stop_reason"] = state.get("stop_reason") or "stopped by user"
                for child in state.get("children") or []:
                    if str(child.get("status") or "") == "pending":
                        child["status"] = "cancelled"
                self._orchestrations.save(state)
                self.service.store.append_event(None, "orchestration_cancelled", "USER", summarize(state))
        finally:
            with self._state_lock:
                self._orchestration_threads.pop(run_id, None)
                self._orchestration_stops.pop(run_id, None)

    def _orchestration_run(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        run_id = str(payload.get("orchestration_id") or "").strip()
        state = self._orchestrations.load(run_id) if run_id else None
        if state is None:
            raise ValueError("unknown orchestration: %s" % run_id)
        if str(state.get("status") or "") == "running":
            return {"status": "already_running", "state": state, "summary": summarize(state)}
        if str(state.get("status") or "") == "completed":
            return {"status": "already_completed", "state": state, "summary": summarize(state)}
        for child in state.get("children") or []:
            if str(child.get("status") or "") in {"failed", "cancelled"}:
                child["status"] = "pending"
                child["error"] = None
                child["finished_at"] = None
        state["status"] = "running"
        state["stop_reason"] = None
        self._orchestrations.save(state)
        stop = threading.Event()
        with self._state_lock:
            self._orchestration_stops[run_id] = stop
            thread = threading.Thread(target=self._run_orchestration, args=(run_id,), daemon=True, name="crab-orch-%s" % run_id[-8:])
            self._orchestration_threads[run_id] = thread
            thread.start()
        self.service.store.append_event(None, "orchestration_started", "KING", summarize(state))
        return {"status": "started", "state": state, "summary": summarize(state)}

    def _orchestration_status(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        run_id = str(payload.get("orchestration_id") or "").strip()
        if run_id:
            state = self._orchestrations.load(run_id)
            if state is None:
                raise ValueError("unknown orchestration: %s" % run_id)
            return {"state": state, "summary": summarize(state)}
        rows = self._orchestrations.list(limit=int(payload.get("limit") or 50))
        return {"orchestrations": rows, "summaries": [summarize(row) for row in rows]}

    def _orchestration_stop(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        run_id = str(payload.get("orchestration_id") or "").strip()
        state = self._orchestrations.load(run_id) if run_id else None
        if state is None:
            raise ValueError("unknown orchestration: %s" % run_id)
        state["stop_reason"] = str(payload.get("reason") or "stopped by user")[:240]
        with self._state_lock:
            stop = self._orchestration_stops.get(run_id)
            if stop is not None:
                stop.set()
        for child in state.get("children") or []:
            if str(child.get("status") or "") == "running":
                self._interrupt(str(child.get("session_id") or ""))
        if stop is None:
            state["status"] = "cancelled"
            for child in state.get("children") or []:
                if str(child.get("status") or "") == "pending":
                    child["status"] = "cancelled"
            self._orchestrations.save(state)
        return {"status": "stopping" if stop is not None else "cancelled", "state": state, "summary": summarize(state)}

    def _session_snapshot(self, session_id: str = "", after_message_id: int = 0) -> Dict[str, Any]:
        """Return the live session slice used by the command deck.

        Keeping this projection in one place lets the TUI request its session
        and colony state in one IPC round-trip while the public snapshot action
        remains available to older clients and diagnostics.
        """
        session = self._session(session_id)
        mission = self.service.store.mission_for_session(str(session["session_id"]))
        detail = self.service.store.inspect(str(mission["mission_id"])) if mission else None
        quality: Dict[str, Any] = {}
        outcome: Dict[str, Any] = {}
        if detail:
            # This is a read-only projection. It never invokes an LLM or
            # replays a mission while a mobile/desktop panel refreshes.
            from .benchmark import observed_metrics

            metrics = observed_metrics(detail)
            quality = {
                "structural": metrics.get("structural_quality") or {},
                "goal_contract": metrics.get("goal_contract_quality") or {},
            }
            for artifact in reversed(detail.get("artifacts") or []):
                if str(artifact.get("kind") or "") != "goal_outcome":
                    continue
                try:
                    parsed = json.loads(Path(str(artifact.get("path") or "")).read_text(encoding="utf-8"))
                except (OSError, TypeError, ValueError):
                    parsed = {}
                if isinstance(parsed, dict):
                    outcome = parsed
                break
        return {
            "session": session,
            "messages": self.service.store.messages(str(session["session_id"]), int(after_message_id or 0)),
            "pending_requests": self.service.store.pending_requests(str(session["session_id"])),
            "queue": self.service.store.queued_inputs(str(session["session_id"])),
            "mission": detail,
            "outcome": outcome,
            "quality": quality,
        }

    def dispatch(self, request: Dict[str, Any]) -> Dict[str, Any]:
        action = str(request.get("action", ""))
        payload = request.get("payload") or {}
        if action == "ping":
            return {
                "status": "online",
                "pid": os.getpid(),
                "version": __version__,
                "runtime_revision": runtime_revision(),
                "colony_protocol": COLONY_PROTOCOL_VERSION,
                "workspace": str(self.workspace),
            }
        if action == "initialize":
            return self.service.initialize()
        if action == "assets":
            return observed_assets(self.workspace)
        if action == "onboarding.status":
            return onboarding_status(self.workspace)
        if action == "onboarding.local_ready":
            return mark_local_ready(self.workspace)
        if action == "onboarding.opencrab.defer":
            return defer_opencrab(self.workspace)
        if action == "onboarding.opencrab.connect":
            endpoint = write_endpoint(self.workspace, str(payload.get("endpoint") or ""))
            checker = OpenCrabInspector(workspace=self.workspace)
            status_payload = checker.client_factory().call_tool("opencrab_status", {})
            connected = record_opencrab_connected(
                self.workspace,
                tier=str(status_payload.get("tier") or "") or None,
                scope="admin_all_customers" if status_payload.get("admin_all_customers") else "connected_account",
            )
            self.service.store.set_integration_cache("opencrab", "ok", {"status": "connected", "account": {"tier": status_payload.get("tier")}})
            return {"status": "connected", "endpoint": endpoint, "account": status_payload.get("tier"), "onboarding": connected}
        if action == "doc.list":
            return _crab_doc_list(self.workspace)
        if action == "doc.import":
            manifest = payload.get("manifest")
            manifest_path = str(payload.get("manifest_path") or "").strip()
            if manifest is None:
                if not manifest_path:
                    raise ValueError("manifest or manifest_path is required")
                source_path = Path(manifest_path).expanduser().resolve()
                if not source_path.is_file():
                    raise ValueError("CRAB DOC manifest not found: %s" % source_path)
                manifest = json.loads(source_path.read_text(encoding="utf-8"))
            return _crab_doc_import(self.workspace, _validate_crab_doc_manifest(manifest))
        if action == "mcp.list":
            return {"servers": mcp_inventory()}
        if action == "mcp.configure":
            session = self._session(str(payload.get("session_id") or ""))
            mode = str(payload.get("mode") or "")
            if mode in {"auto", "all", "off"}:
                return self._apply_mcp_policy(session, mode)
            server = str(payload.get("server") or "")
            enabled = bool(payload.get("enabled"))
            inventory = mcp_inventory()
            names = [str(row["name"]) for row in inventory]
            if server not in names:
                raise ValueError("unknown configured MCP server: %s" % server)
            policy = _mcp_policy_with(str(session.get("mcp_policy") or "auto"), server, enabled, names)
            return self._apply_mcp_policy(session, policy)
        if action == "cli.list":
            assets = observed_assets(self.workspace)
            return {"clis": assets.get("clis") or {}, "codex": assets.get("codex") or {}}
        if action == "ontology.snapshot":
            local = local_snapshot_result(self.workspace)
            if local.get("status") == "ok":
                display_payload = filter_inventory_for_display(local.get("payload") or {}, self.workspace)
                local["payload"] = compact_snapshot(display_payload)
                return local
            cached = self.service.store.integration_cache("opencrab")
            return cached or {"status": "unavailable", "payload": {}, "error": "OpenCrab local snapshot is not initialized."}
        if action == "ontology.catalog":
            try:
                catalog = self.opencrab_inspector.catalog(
                    query=None if "query" not in payload else str(payload.get("query") or ""),
                    force=bool(payload.get("force")),
                    complete=bool(payload.get("complete")),
                    prefer_stale=bool(payload.get("prefer_stale")),
                )
            except OpenCrabUnavailable as exc:
                return {"status": "unavailable", "payload": {}, "error": str(exc)}
            return {"status": "ok", "payload": catalog}
        if action == "ontology.diff":
            result = local_diff_result(self.workspace)
            if result.get("status") == "ok":
                result["diff"] = compact_diff(result.get("diff") or {})
            return result
        if action == "ontology.sync":
            try:
                payload = self.opencrab_inspector.refresh(full=True)
                synced = persist_sync(self.workspace, payload)
            except (OpenCrabUnavailable, SnapshotPersistenceError) as exc:
                return {"status": "unavailable", "payload": {}, "error": str(exc)}
            display_snapshot = filter_inventory_for_display(synced["snapshot"], self.workspace)
            compacted = compact_snapshot(display_snapshot)
            self.service.store.set_integration_cache("opencrab", "ok", compacted)
            return {
                "status": "ok",
                "source": "OpenCrab MCP",
                "payload": compacted,
                "diff": compact_diff(synced["diff"]),
                "paths": synced["paths"],
            }
        if action == "ontology.refresh":
            try:
                payload = self.opencrab_inspector.refresh()
            except OpenCrabUnavailable as exc:
                self.service.store.set_integration_cache("opencrab", "unavailable", {}, str(exc))
                return {"status": "unavailable", "payload": {}, "error": str(exc)}
            display_payload = filter_inventory_for_display(payload, self.workspace)
            self.service.store.set_integration_cache("opencrab", "ok", compact_snapshot(display_payload))
            return self.service.store.integration_cache("opencrab") or {"status": "unavailable", "payload": {}, "error": "cache write failed"}
        if action == "project.list":
            overview = self.service.store.colony_overview()
            return {"projects": self.service.store.list_projects(), "sessions": overview.get("sessions") or []}
        if action == "project.create":
            return self.service.store.create_project(
                str(payload.get("name") or ""),
                root_path=str(payload.get("root_path") or ""),
            )
        if action == "project.folder":
            return self.service.store.set_project_root(
                str(payload.get("project_id") or ""),
                str(payload.get("root_path") or ""),
            )
        if action == "project.rename":
            return self.service.store.rename_project(
                str(payload.get("project_id") or ""),
                str(payload.get("name") or ""),
            )
        if action == "project.delete":
            return self.service.store.delete_project(str(payload.get("project_id") or ""))
        if action == "project.assign":
            session = self._session(str(payload.get("session_id") or ""))
            project_name = str(payload.get("project_name") or "").strip()
            if not project_name:
                raise ValueError("project_name is required")
            project = self.service.store.ensure_project(
                project_name,
                project_id=str(payload.get("project_id") or ""),
                root_path=str(payload.get("root_path") or ""),
            )
            return self.service.store.update_session(
                str(session["session_id"]),
                project_id=str(project.get("project_id") or ""),
                project_name=str(project.get("name") or project_name),
                project_root_path=str(project.get("root_path") or ""),
                ontology_context_count=max(0, int(payload.get("ontology_context_count") or 0)),
            )
        if action == "project.clear":
            session = self._session(str(payload.get("session_id") or ""))
            updated = self.service.store.update_session(
                str(session["session_id"]), project_id="", project_name="", project_root_path="", ontology_context_count=0
            )
            self.service.store.clear_ontology_context(str(session["session_id"]))
            updated["ontology_context"] = self.service.store.ontology_context(str(session["session_id"]))
            return updated
        if action == "ontology.context":
            session = self._session(str(payload.get("session_id") or ""))
            project_ids = payload.get("project_ids") if isinstance(payload.get("project_ids"), list) else []
            package_ids = payload.get("package_ids") if isinstance(payload.get("package_ids"), list) else []
            project_labels = payload.get("project_labels") if isinstance(payload.get("project_labels"), dict) else {}
            package_labels = payload.get("package_labels") if isinstance(payload.get("package_labels"), dict) else {}
            context = self.service.store.set_ontology_context(
                str(session["session_id"]), project_ids, package_ids, project_labels, package_labels
            )
            updated = self.service.store.session(str(session["session_id"])) or session
            updated["ontology_context"] = context
            return {"session": updated, "context": context}
        if action == "colony.overview":
            overview = self.service.store.colony_overview()
            cached = self.service.store.integration_cache("opencrab")
            local = local_snapshot_result(self.workspace)
            raw_ontology = (local.get("payload") if local.get("status") == "ok" else (cached or {}).get("payload")) or {}
            ontology = filter_inventory_for_display(raw_ontology, self.workspace)
            overview["ontology"] = {
                "status": (cached or {}).get("status", "unavailable"),
                "access_scope": ontology.get("display_scope") or ontology.get("access_scope"),
                "available_pack_count": (ontology.get("packs") or {}).get("total"),
                "has_more_packs": bool((ontology.get("packs") or {}).get("has_more")),
            }
            return overview
        if action == "session.ensure":
            return self._session(str(payload.get("session_id") or ""))
        if action == "session.create":
            created = self.service.store.create_session(
                title=str(payload.get("title") or "CrabAgent session"),
                model_policy=str(payload.get("model_policy") or "auto"),
                interaction_mode=str(payload.get("interaction_mode") or "auto"),
                mcp_policy=str(payload.get("mcp_policy") or "auto"),
                cli_policy=str(payload.get("cli_policy") or "auto"),
                max_workers=int(payload.get("max_workers") or 3),
                worker_policy=str(payload.get("worker_policy") or "auto"),
                project_id=str(payload.get("project_id") or ""),
                project_name=str(payload.get("project_name") or ""),
                project_root_path=str(payload.get("project_root_path") or ""),
            )
            project_id = str(payload.get("project_id") or "")
            project_name = str(payload.get("project_name") or "").strip()
            if project_id or project_name:
                project = self.service.store.ensure_project(
                    project_name or "Project",
                    project_id=project_id,
                    root_path=str(payload.get("project_root_path") or ""),
                )
                created = self.service.store.update_session(
                    str(created["session_id"]),
                    project_id=str(project.get("project_id") or ""),
                    project_name=str(project.get("name") or ""),
                    project_root_path=str(project.get("root_path") or ""),
                )
            return created
        if action == "session.fork":
            session = self._session(str(payload.get("session_id") or ""))
            if str(session.get("status") or "") == "running":
                raise RuntimeError("wait for the active turn before branching")
            forked_thread_id = ""
            if session.get("codex_thread_id"):
                forked_thread_id = self._bridge(session).fork_thread()
            return self.service.store.fork_session(
                str(session["session_id"]),
                str(payload.get("title") or ""),
                codex_thread_id=forked_thread_id,
            )
        if action == "session.list":
            return {"sessions": self.service.store.list_sessions()}
        if action == "orchestration.plan":
            return self._orchestration_plan(payload)
        if action == "orchestration.run":
            return self._orchestration_run(payload)
        if action == "orchestration.status":
            return self._orchestration_status(payload)
        if action == "orchestration.stop":
            return self._orchestration_stop(payload)
        if action == "pack.ingest":
            project_id = str(payload.get("project_id") or "")
            requested_run_id = str(payload.get("run_id") or "").strip()
            if requested_run_id:
                existing = self.service.store.pack_ingest_run(requested_run_id)
                if existing is not None:
                    saved_existing = dict(existing.get("result") or {})
                    saved_existing.setdefault("run_id", requested_run_id)
                    saved_existing.setdefault("status", existing.get("status") or "unknown")
                    saved_existing["deduplicated"] = True
                    return saved_existing
            project = self.service.store.project(project_id=project_id) if project_id else None
            folder = str(payload.get("folder_path") or (project or {}).get("root_path") or "").strip()
            if not folder:
                raise ValueError("a project folder is required; create or select a folder-based project")
            if project is None and payload.get("project_name"):
                project = self.service.store.ensure_project(str(payload.get("project_name")), root_path=folder)
                project_id = str(project.get("project_id") or "")
            result = self.pack_builder.build_and_ingest(
                Path(folder),
                project_id=project_id,
                project_name=str((project or {}).get("name") or payload.get("project_name") or ""),
                ontology_purpose=str(payload.get("ontology_purpose") or ""),
                run_id=requested_run_id or None,
            )
            self.service.store.record_pack_ingest_run(
                str(result["run_id"]), project_id, str(result["root_path"]), str(result["status"]), result
            )
            saved_record = self.service.store.pack_ingest_run(str(result["run_id"])) or {}
            self._start_pack_upload_watch(saved_record)
            return result
        if action == "pack.ingest.status":
            run_id = str(payload.get("run_id") or "")
            if not run_id:
                return {"runs": self.service.store.list_pack_ingest_runs()}
            record = self.service.store.pack_ingest_run(run_id)
            if record is None:
                raise ValueError("unknown pack ingest run: %s" % run_id)
            saved = record.get("result") or {}
            remote = saved.get("remote") or {}
            upload_session_id = str(
                payload.get("upload_session_id")
                or saved.get("upload_session_id")
                or upload_session_id_from_result(remote)
                or ""
            )
            if not upload_session_id:
                return record
            return self._observe_pack_upload(record, upload_session_id)
        if action == "conversation.export":
            session = self._session(str(payload.get("session_id") or ""))
            total = self.service.store.message_count(str(session["session_id"]))
            limit = min(max(int(payload.get("limit") or 10000), 1), 10000)
            messages = self.service.store.messages(str(session["session_id"]), limit=limit)
            return {"messages": messages, "total": total, "truncated": total > len(messages)}
        if action == "session.configure":
            session_id = str(payload["session_id"])
            session = self._session(session_id)
            updated = self.service.store.update_session(
                session_id,
                model_policy=str(payload.get("model_policy") or "auto"),
                interaction_mode=str(payload.get("interaction_mode") or "auto"),
                cli_policy=str(payload.get("cli_policy") or session.get("cli_policy") or "auto"),
                max_workers=max(1, min(int(payload.get("max_workers") or 3), 8)),
                worker_policy=str(payload.get("worker_policy") or session.get("worker_policy") or "auto"),
            )
            requested_policy = str(payload.get("mcp_policy") or "auto")
            if requested_policy != str(session.get("mcp_policy") or "auto"):
                return self._apply_mcp_policy(updated, requested_policy)
            return updated
        if action == "session.snapshot":
            return self._session_snapshot(
                str(payload.get("session_id") or ""),
                int(payload.get("after_message_id") or 0),
            )
        if action == "ui.snapshot":
            session_snapshot = self._session_snapshot(
                str(payload.get("session_id") or ""),
                int(payload.get("after_message_id") or 0),
            )
            session_snapshot["colony"] = self.dispatch({"action": "colony.overview", "payload": {}})
            return session_snapshot
        if action == "prompt.submit":
            session = self._session(str(payload.get("session_id") or ""))
            objective = str(payload.get("objective") or "").strip()
            if not objective:
                raise ValueError("objective is empty")
            disposition = str(payload.get("disposition") or "start")
            session_id = str(session["session_id"])
            with self._state_lock:
                running = bool(self._jobs.get(session_id) and self._jobs[session_id].is_alive())
            if running:
                if disposition not in {"now", "wait"}:
                    raise RuntimeError("mission is running; choose disposition now or wait")
                queued = self.service.store.enqueue_input(session_id, objective, disposition)
                if disposition == "now":
                    self._interrupt(session_id)
                return queued
            return self._dispatch(
                session,
                objective,
                str(payload.get("interaction") or ""),
                direct_opencrab=bool(payload.get("benchmark_direct")),
            )
        if action == "mission.retry":
            session = self._session(str(payload.get("session_id") or ""))
            return self._retry_last_mission(session, force=bool(payload.get("force")))
        if action == "session.interrupt":
            return self._interrupt(str(payload["session_id"]))
        if action == "runtime.respond":
            session_id = str(payload["session_id"])
            request_id = str(payload["request_id"])
            bridge = self._bridges.get(session_id)
            if bridge is None:
                raise RuntimeError("no live Codex bridge for session")
            if bool(payload.get("approve")):
                pending = next((row for row in self.service.store.pending_requests(session_id) if row["request_id"] == request_id), {})
                method = str(pending.get("method") or "")
                if method == "mcpServer/elicitation/request":
                    result = {"action": "accept", **dict(payload.get("result") or {})}
                elif method == "item/tool/requestUserInput":
                    result = dict(payload.get("result") or {})
                    if "answers" not in result:
                        raise RuntimeError("tool user input requires structured answers")
                else:
                    result = {"decision": "accept", **dict(payload.get("result") or {})}
                bridge.respond(request_id, result)
                self.service.store.resolve_runtime_request(request_id, "approved")
            else:
                bridge.reject(request_id, str(payload.get("reason") or "Declined by user"))
                self.service.store.resolve_runtime_request(request_id, "declined")
            return {"request_id": request_id, "resolved": True}
        if action == "events.since":
            return {
                "events": self.service.store.events_after(
                    int(payload.get("event_id") or 0),
                    limit=max(1, min(int(payload.get("limit") or 500), 2000)),
                )
            }
        if action == "goal.preview":
            session_id = str(payload.get("session_id") or "")
            session = self._session(session_id) if session_id else {}
            context = self.service.store.ontology_context(session_id) if session_id else {}
            plan = classify_goal(
                str(payload.get("objective") or ""),
                selected_pack_count=len(context.get("package_ids") or []),
                selected_project_count=len(context.get("project_ids") or []),
                knowledge_available=self._knowledge_available(session),
            )
            return {"plan": plan.to_dict()}
        if action == "plan":
            return self.service.plan_mission(
                objective=str(payload["objective"]),
                max_workers=int(payload.get("max_workers", 3)),
                token_budget=payload.get("token_budget"),
                adaptive=True,
            )
        if action == "run_demo":
            return self.service.run_demo(
                str(payload["objective"]),
                session_id=str(payload["session_id"]) if payload.get("session_id") else None,
            )
        if action == "status":
            return self.service.status()
        if action == "inspect":
            return self.service.store.inspect(str(payload["mission_id"]))
        if action == "replay":
            events = self.service.store.events(str(payload["mission_id"]))
            return {
                "events": [
                    {
                        "event_id": event.event_id,
                        "mission_id": event.mission_id,
                        "event_type": event.event_type,
                        "actor": event.actor,
                        "payload": event.payload,
                        "created_at": event.created_at,
                    }
                    for event in events
                ]
            }
        if action == "shutdown":
            for bridge in list(self._bridges.values()):
                bridge.close()
            threading.Thread(target=self.shutdown, daemon=True).start()
            return {"status": "stopping"}
        raise ValueError("unknown action: %s" % action)


app = typer.Typer(add_completion=False, help="CrabAgent durable local runtime.")


@app.command()
def run(
    workspace: Path = typer.Option(Path.cwd(), "--workspace", "-w", resolve_path=True),
) -> None:
    """Run crabd in the foreground."""
    paths = runtime_paths(workspace)
    paths["root"].mkdir(parents=True, exist_ok=True)
    socket_path = paths["socket"]
    if socket_path.exists():
        socket_path.unlink()
    paths["pid"].write_text(str(os.getpid()) + "\n", encoding="utf-8")
    server: Optional[RuntimeServer] = None
    try:
        server = RuntimeServer(workspace, socket_path)

        def stop_server(_signum: int, _frame: Any) -> None:
            if server is not None:
                threading.Thread(target=server.shutdown, daemon=True).start()

        signal.signal(signal.SIGTERM, stop_server)
        signal.signal(signal.SIGINT, stop_server)
        server.serve_forever(poll_interval=0.1)
    finally:
        if server is not None:
            server.server_close()
        try:
            owns_runtime_files = paths["pid"].read_text(encoding="utf-8").strip() == str(os.getpid())
        except OSError:
            owns_runtime_files = False
        if owns_runtime_files:
            if socket_path.exists():
                socket_path.unlink()
            if paths["pid"].exists():
                paths["pid"].unlink()


if __name__ == "__main__":
    app()
