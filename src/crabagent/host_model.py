from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Callable, Dict, Optional

from .codex_app_server import CodexLiveEvent, CodexLiveTurn
from .models import utc_now


class HostModelSession:
    """Host-driven model bridge used by ChatGPT-origin durable missions.

    The runtime never calls an external model provider.  Instead it emits one
    ``hostModel/turn`` runtime request and waits for the connected host to
    submit the current-model result through ``runtime.respond``.  This keeps
    mission state, receipts and Oracle gates durable while making the caller's
    current model the actual reasoning executor.
    """

    executor_name = "host_current_model"
    receipt_tool_name = "host.current-model.turn"
    observation_source = "host_current_model"
    supports_session_fallback = False

    def __init__(self, *, thread_id: str = "", mcp_policy: str = "auto", project_root: str = "") -> None:
        self.thread_id = thread_id
        self.mcp_policy = mcp_policy
        self.project_root = str(project_root or "")
        self.last_start_mode = "unstarted"
        self.last_reported_model = ""
        self._condition = threading.Condition()
        self._pending: Dict[str, Optional[Dict[str, Any]]] = {}
        self._closed = False
        self._interrupted = False

    def start(self) -> str:
        with self._condition:
            if self._closed:
                raise RuntimeError("host model bridge is closed")
            if not self.thread_id:
                self.thread_id = "host-thread-%s" % uuid.uuid4().hex
                self.last_start_mode = "started"
            else:
                self.last_start_mode = "resumed"
            self._interrupted = False
            return self.thread_id

    def run_turn(
        self,
        prompt: str,
        emit: Callable[[CodexLiveEvent], None],
        *,
        model: Optional[str] = None,
        effort: Optional[str] = None,
        cancel_event: Optional[threading.Event] = None,
        timeout_seconds: Optional[float] = None,
        idle_timeout_seconds: Optional[float] = None,
    ) -> CodexLiveTurn:
        thread_id = self.start()
        turn_id = "host-turn-%s" % uuid.uuid4().hex
        request_id = "host-request-%s" % uuid.uuid4().hex
        started_at = utc_now()
        with self._condition:
            self._pending[request_id] = None

        emit(
            CodexLiveEvent(
                kind="runtime_request",
                text=prompt,
                data={
                    "request_id": request_id,
                    "method": "hostModel/turn",
                    "params": {
                        "prompt": prompt,
                        "model": str(model or "current-model"),
                        "effort": str(effort or ""),
                        "thread_id": thread_id,
                        "turn_id": turn_id,
                        "workspace": self.project_root,
                        "projectRoot": self.project_root,
                    },
                },
            )
        )

        timeout = idle_timeout_seconds if idle_timeout_seconds is not None else timeout_seconds
        deadline = time.monotonic() + float(timeout) if timeout is not None else None
        response: Optional[Dict[str, Any]] = None
        while True:
            if cancel_event is not None and cancel_event.is_set():
                with self._condition:
                    self._pending.pop(request_id, None)
                return CodexLiveTurn(
                    thread_id=thread_id,
                    turn_id=turn_id,
                    status="cancelled",
                    text="",
                    usage={},
                    started_at=started_at,
                    finished_at=utc_now(),
                    error="host model turn cancelled",
                )
            with self._condition:
                if self._interrupted:
                    self._pending.pop(request_id, None)
                    return CodexLiveTurn(
                        thread_id=thread_id,
                        turn_id=turn_id,
                        status="cancelled",
                        text="",
                        usage={},
                        started_at=started_at,
                        finished_at=utc_now(),
                        error="host model turn interrupted",
                    )
                response = self._pending.get(request_id)
                if response is not None:
                    self._pending.pop(request_id, None)
                    break
                if self._closed:
                    self._pending.pop(request_id, None)
                    return CodexLiveTurn(
                        thread_id=thread_id,
                        turn_id=turn_id,
                        status="failed",
                        text="",
                        usage={},
                        started_at=started_at,
                        finished_at=utc_now(),
                        error="host model bridge closed",
                    )
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        self._pending.pop(request_id, None)
                        return CodexLiveTurn(
                            thread_id=thread_id,
                            turn_id=turn_id,
                            status="timeout",
                            text="",
                            usage={},
                            started_at=started_at,
                            finished_at=utc_now(),
                            error="Host model turn idle timed out after %.2fs" % float(timeout or 0.0),
                        )
                    self._condition.wait(timeout=min(0.25, remaining))
                else:
                    self._condition.wait(timeout=0.25)

        result = response.get("result") if isinstance(response, dict) else {}
        if not isinstance(result, dict):
            result = {}
        if str(response.get("status") or "") != "approved":
            return CodexLiveTurn(
                thread_id=thread_id,
                turn_id=turn_id,
                status="failed",
                text="",
                usage={},
                started_at=started_at,
                finished_at=utc_now(),
                error=str(response.get("reason") or "host model turn rejected"),
            )
        text = str(result.get("text") or "")
        if not text.strip():
            return CodexLiveTurn(
                thread_id=thread_id,
                turn_id=turn_id,
                status="failed",
                text="",
                usage={},
                started_at=started_at,
                finished_at=utc_now(),
                error="host model response text is required",
            )
        usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        self.last_reported_model = str(result.get("model") or "host-current-model")
        return CodexLiveTurn(
            thread_id=thread_id,
            turn_id=turn_id,
            status="completed",
            text=text,
            usage=dict(usage),
            started_at=started_at,
            finished_at=utc_now(),
        )

    def respond(self, request_id: str, result: Dict[str, Any]) -> None:
        with self._condition:
            if request_id not in self._pending:
                raise RuntimeError("unknown host model request: %s" % request_id)
            self._pending[request_id] = {"status": "approved", "result": dict(result or {})}
            self._condition.notify_all()

    def reject(self, request_id: str, reason: str) -> None:
        with self._condition:
            if request_id not in self._pending:
                raise RuntimeError("unknown host model request: %s" % request_id)
            self._pending[request_id] = {"status": "declined", "reason": str(reason or "Declined by host")}
            self._condition.notify_all()

    def interrupt(self) -> None:
        with self._condition:
            self._interrupted = True
            self._condition.notify_all()

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()
