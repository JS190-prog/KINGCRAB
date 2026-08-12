from __future__ import annotations

import threading
from typing import Any, Dict, Optional

from .codex_app_server import CodexAppServerError, CodexAppServerSession, CodexLiveEvent, can_fallback_to_session_default
from .goal import interaction_kind_for_goal
from .identity import lecture_brand_context
from .ontology_context import compact_context
from .store import ColonyStore, new_id


ACTION_SIGNALS = (
    "build",
    "create",
    "fix",
    "implement",
    "install",
    "deploy",
    "edit",
    "add",
    "remove",
    "update",
    "refactor",
    "write file",
    "만들어",
    "고쳐",
    "수정",
    "구현",
    "설치",
    "배포",
    "추가",
    "삭제",
    "변경",
    "작성해",
    "개발",
)


def interaction_kind(
    prompt: str,
    mode: str = "auto",
    *,
    selected_pack_count: int = 0,
    selected_project_count: int = 0,
    knowledge_available: bool = False,
) -> str:
    return interaction_kind_for_goal(
        prompt,
        mode,
        selected_pack_count=selected_pack_count,
        selected_project_count=selected_project_count,
        knowledge_available=knowledge_available,
    )


def _last_tokens(usage: Dict[str, Any]) -> Any:
    last = usage.get("last") if isinstance(usage.get("last"), dict) else usage
    return last.get("totalTokens") if isinstance(last, dict) else None


def _label_preview(values: Any, limit: int = 18) -> str:
    mapping = values if isinstance(values, dict) else {}
    labels = sorted((str(value) for value in mapping.values()), key=str.lower)
    preview = ", ".join(labels[:limit]) or "none"
    suffix = " ... (+%d more)" % (len(labels) - limit) if len(labels) > limit else ""
    return "%s%s" % (preview, suffix)


class ConversationExecutor:
    """Runs one direct conversational turn on the session's persistent Codex thread."""

    def __init__(
        self,
        store: ColonyStore,
        session_id: str,
        bridge: CodexAppServerSession,
        cancel_event: threading.Event,
    ) -> None:
        self.store = store
        self.session_id = session_id
        self.bridge = bridge
        self.cancel_event = cancel_event

    def run(
        self,
        prompt: str,
        ontology_context: Optional[Dict[str, Any]] = None,
        *,
        ontology_receipt: Optional[Dict[str, Any]] = None,
        benchmark_direct: bool = False,
    ) -> Dict[str, Any]:
        model_prompt = prompt
        context = ontology_context or {}
        package_ids = [str(value) for value in context.get("package_ids") or [] if str(value)]
        project_ids = [str(value) for value in context.get("project_ids") or [] if str(value)]
        brand_context = lecture_brand_context(len(package_ids))
        if package_ids or project_ids:
            package_labels = context.get("package_labels") or {}
            project_labels = context.get("project_labels") or {}
            model_prompt = (
                "%s\n\n[CRABAGENT QUEEN CONTEXT]\n"
                "Use the selected OpenCrab Workspace context when relevant.\n"
                "Projects: %s\nPacks: %s\n"
                "Do not claim pack contents were read unless the connected MCP actually returns them.\n%s"
                % (
                    prompt,
                    _label_preview(project_labels) if project_labels else "%d selected" % len(project_ids),
                    _label_preview(package_labels) if package_labels else "%d selected" % len(package_ids),
                    brand_context,
                )
            )
        context_chars = 0
        if isinstance(ontology_receipt, dict):
            bounded_receipt = compact_context(ontology_receipt, max_chars=4200)
            context_chars = len(bounded_receipt)
            model_prompt = (
                "%s\n\n[DIRECT OPENCRAB MCP RECEIPT]\n"
                "This is the bounded read-only response observed from OpenCrab MCP. "
                "Use only its evidence and paths; if a gate is blocked, say so.\n%s"
                % (model_prompt, bounded_receipt)
            )
            if benchmark_direct:
                model_prompt += (
                    "\n\n[DIRECT BENCHMARK EVIDENCE CONTRACT]\n"
                    "Answer only from this observed MCP receipt. For every material claim, "
                    "cite at least one exact evidence ID or a unique leading eight-character "
                    "evidence ID prefix in the form [evidence_id: ...]. If the receipt cannot "
                    "support the answer, say that it is blocked."
                )
        self.store.update_session(self.session_id, status="running", active_mission_id=None)
        self.store.add_message(self.session_id, "user", prompt, metadata={"interaction": "chat"})
        try:
            thread_id = self.bridge.start()
            self.store.update_session(self.session_id, codex_thread_id=thread_id)
            self.store.append_event(
                None,
                "codex_thread_%s" % self.bridge.last_start_mode,
                "RUNTIME",
                {"session_id": self.session_id, "thread_id": thread_id},
            )
            session = self.store.session(self.session_id) or {}
            policy = str(session.get("model_policy") or "auto")
            cli_policy = str(session.get("cli_policy") or "auto")
            if cli_policy != "auto":
                model_prompt = (
                    "%s\n\n[CRABAGENT CLI ROUTE]\n"
                    "Prefer the observed allowlisted CLI `%s` for matching local work when it is appropriate. "
                    "Do not invoke unobserved or arbitrary executables."
                    % (model_prompt, cli_policy)
                )
            route = {
                "auto": ("gpt-5.6-terra", "medium", "terra"),
                "sol": ("gpt-5.6-sol", "high", "sol"),
                "terra": ("gpt-5.6-terra", "medium", "terra"),
                "luna": ("gpt-5.6-luna", "low", "luna"),
            }[policy if policy in {"auto", "sol", "terra", "luna"} else "auto"]

            def emit(event: CodexLiveEvent) -> None:
                if event.kind == "runtime_request":
                    request_id = str(event.data.get("request_id") or new_id("request"))
                    self.store.add_runtime_request(
                        request_id,
                        self.session_id,
                        None,
                        "approval_or_input",
                        str(event.data.get("method") or event.text),
                        event.text,
                        event.data,
                    )
                elif event.kind == "activity":
                    self.store.append_event(
                        None,
                        "codex_chat_activity",
                        "CODEX",
                        {"session_id": self.session_id, "thread_id": thread_id, "activity": event.text, "data": event.data},
                    )

            model, effort, profile = route
            turn = self.bridge.run_turn(model_prompt, emit, model=model, effort=effort, cancel_event=self.cancel_event)
            if turn.status in {"failed", "timeout"} and can_fallback_to_session_default(turn.error):
                self.store.append_event(
                    None,
                    "chat_route_fallback",
                    "RUNTIME",
                    {"session_id": self.session_id, "requested_model": model, "reason": turn.error},
                )
                model, profile = "codex-session-default", "default"
                turn = self.bridge.run_turn(model_prompt, emit, effort=effort, cancel_event=self.cancel_event)
            elif turn.status in {"failed", "timeout"}:
                self.store.append_event(
                    None,
                    "chat_route_fallback_skipped",
                    "RUNTIME",
                    {"session_id": self.session_id, "requested_model": model, "reason": turn.error or turn.status, "duplicate_work_protected": True},
                )
            if turn.status in {"failed", "timeout"}:
                raise CodexAppServerError(turn.error or "Codex chat turn failed")
            if turn.status == "cancelled":
                self.store.add_message(self.session_id, "system", "Conversation turn interrupted.", metadata={"interaction": "chat"})
            else:
                self.store.add_message(
                    self.session_id,
                    "assistant",
                    turn.text or "Codex completed without a textual response.",
                    metadata={
                        "interaction": "chat",
                        "thread_id": turn.thread_id,
                        "turn_id": turn.turn_id,
                        "model": model,
                        "profile": profile,
                        "tokens_observed": _last_tokens(turn.usage),
                        "prompt_chars": len(model_prompt),
                        "context_chars": context_chars,
                        "benchmark_direct": benchmark_direct,
                    },
                )
            self.store.append_event(
                None,
                "conversation_turn_%s" % turn.status,
                "CODEX",
                {
                    "session_id": self.session_id,
                    "thread_id": turn.thread_id,
                    "turn_id": turn.turn_id,
                    "model": model,
                    "tokens_observed": _last_tokens(turn.usage),
                    "usage": turn.usage,
                    "prompt_chars": len(model_prompt),
                    "context_chars": context_chars,
                    "benchmark_direct": benchmark_direct,
                    "opencrab_receipt": _benchmark_receipt(ontology_receipt) if benchmark_direct else None,
                },
            )
            return {"status": turn.status, "thread_id": turn.thread_id, "turn_id": turn.turn_id}
        except Exception as exc:
            self.store.add_message(
                self.session_id,
                "system",
                "Conversation failed: %s" % exc,
                metadata={"interaction": "chat", "error": type(exc).__name__},
            )
            return {"status": "failed", "error": str(exc), "thread_id": self.bridge.thread_id}
        finally:
            self.store.update_session(
                self.session_id,
                status="ready",
                active_mission_id=None,
                codex_thread_id=self.bridge.thread_id,
            )


def _benchmark_receipt(receipt: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Keep enough direct MCP evidence for an auditable benchmark snapshot."""
    if not isinstance(receipt, dict):
        return None
    return {
        "schema": receipt.get("schema"),
        "status": receipt.get("status"),
        "claim_gate": receipt.get("claim_gate"),
        "graph_gate": receipt.get("graph_gate"),
        "grounding": receipt.get("grounding"),
        "quality": receipt.get("quality") if isinstance(receipt.get("quality"), dict) else {},
        "evidence": receipt.get("evidence") if isinstance(receipt.get("evidence"), list) else [],
        "nodes": receipt.get("nodes") if isinstance(receipt.get("nodes"), list) else [],
        "edges": receipt.get("edges") if isinstance(receipt.get("edges"), list) else [],
        "paths": receipt.get("paths") if isinstance(receipt.get("paths"), list) else [],
        "tool_calls": receipt.get("tool_calls") if isinstance(receipt.get("tool_calls"), list) else [],
    }
