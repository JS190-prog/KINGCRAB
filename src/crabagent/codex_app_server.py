from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Deque, Dict, Optional

from . import __version__
from .models import utc_now


class CodexAppServerError(RuntimeError):
    pass


def can_fallback_to_session_default(error: str) -> bool:
    """Allow one route fallback only for a rejected model/effort selection.

    A timeout or a closed server may already have consumed work. Retrying the
    same prompt in those cases duplicates cost and can repeat workspace edits.
    """
    message = " ".join(str(error or "").lower().split())
    if not message:
        return False
    non_retryable = (
        "timeout",
        "timed out",
        "server closed",
        "connection",
        "cancel",
        "interrupt",
        "permission",
        "approval",
    )
    if any(marker in message for marker in non_retryable):
        return False
    model_rejection = (
        "unknown model",
        "unsupported model",
        "model not found",
        "model is not available",
        "invalid model",
        "invalid effort",
        "unsupported effort",
        "model",
    )
    return any(marker in message for marker in model_rejection)


@dataclass(frozen=True)
class CodexLiveEvent:
    kind: str
    text: str = ""
    data: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CodexLiveTurn:
    thread_id: str
    turn_id: str
    status: str
    text: str
    usage: Dict[str, Any]
    started_at: str
    finished_at: str
    error: str = ""


class CodexAppServerSession:
    """Persistent JSON-RPC bridge to the locally authenticated Codex CLI."""

    def __init__(
        self,
        executable: str,
        cwd: Path,
        *,
        thread_id: str = "",
        mcp_policy: str = "auto",
        mcp_servers: Optional[list[str]] = None,
        popen_factory: Callable[..., Any] = subprocess.Popen,
    ) -> None:
        self.executable = executable
        self.cwd = cwd.resolve()
        self.thread_id = thread_id
        self.mcp_policy = mcp_policy
        self.mcp_servers = list(mcp_servers or [])
        self._popen_factory = popen_factory
        self._process: Optional[Any] = None
        self._pending: Dict[str, queue.Queue] = {}
        self._notifications: queue.Queue = queue.Queue()
        self._requests: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._started = False
        self._active_turn_id = ""
        self._stderr: Deque[str] = deque(maxlen=80)
        self.last_start_mode = "unstarted"

    def _app_server_command(self) -> list[str]:
        command = [self.executable, "app-server"]
        safe_names = [name for name in self.mcp_servers if re.fullmatch(r"[A-Za-z0-9_-]+", name)]
        if self.mcp_policy == "off":
            enabled = set()
        elif self.mcp_policy == "all":
            enabled = set(safe_names)
        elif self.mcp_policy.startswith("allow:"):
            enabled = {name for name in self.mcp_policy[6:].split(",") if name}
        else:
            return [*command, "--stdio"]
        for name in safe_names:
            value = "true" if name in enabled else "false"
            command.extend(["-c", "mcp_servers.%s.enabled=%s" % (name, value)])
        return [*command, "--stdio"]

    @property
    def active_turn_id(self) -> str:
        return self._active_turn_id

    @property
    def stderr_tail(self) -> str:
        return "".join(self._stderr)[-4000:]

    @staticmethod
    def _child_environment() -> Dict[str, str]:
        """Keep Python tool commands out of macOS's protected global cache."""
        environment = os.environ.copy()
        environment.setdefault("PYTHONPYCACHEPREFIX", "/tmp/crabagent-pycache")
        return environment

    def _send(self, payload: Dict[str, Any]) -> None:
        if not self._process or not self._process.stdin:
            raise CodexAppServerError("Codex App Server is not running")
        with self._lock:
            self._process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self._process.stdin.flush()

    def _request(self, method: str, params: Dict[str, Any], timeout: float = 30.0) -> Dict[str, Any]:
        request_id = str(uuid.uuid4())
        inbox: queue.Queue = queue.Queue(maxsize=1)
        self._pending[request_id] = inbox
        try:
            self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            try:
                response = inbox.get(timeout=timeout)
            except queue.Empty as exc:
                raise CodexAppServerError("Codex timed out during %s" % method) from exc
        finally:
            self._pending.pop(request_id, None)
        if response.get("error"):
            error = response["error"]
            raise CodexAppServerError(str(error.get("message") if isinstance(error, dict) else error))
        result = response.get("result")
        if not isinstance(result, dict):
            raise CodexAppServerError("Codex returned no result for %s" % method)
        return result

    def _reader_loop(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        for raw in self._process.stdout:
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            request_id = message.get("id")
            if request_id is not None and "method" not in message:
                inbox = self._pending.get(str(request_id))
                if inbox is not None:
                    inbox.put(message)
                continue
            if request_id is not None and "method" in message:
                request = {
                    "request_id": str(request_id),
                    "method": str(message.get("method") or ""),
                    "params": dict(message.get("params") or {}),
                }
                self._requests[request["request_id"]] = request
                self._notifications.put(CodexLiveEvent("runtime_request", request["method"], request))
                continue
            self._notifications.put(message)
        self._notifications.put({"method": "server/closed", "params": {}})

    def _stderr_loop(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        for raw in self._process.stderr:
            self._stderr.append(raw)

    def start(self) -> str:
        if self._started and self._process is not None and self._process.poll() is None:
            return self.thread_id
        self._started = False
        self._active_turn_id = ""
        while not self._notifications.empty():
            try:
                self._notifications.get_nowait()
            except queue.Empty:
                break
        try:
            self._process = self._popen_factory(
                self._app_server_command(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                cwd=str(self.cwd),
                env=self._child_environment(),
            )
        except OSError as exc:
            raise CodexAppServerError("Codex App Server could not start: %s" % exc) from exc
        threading.Thread(target=self._reader_loop, daemon=True, name="crab-codex-reader").start()
        threading.Thread(target=self._stderr_loop, daemon=True, name="crab-codex-stderr").start()
        self._request(
            "initialize",
            {"clientInfo": {"name": "CrabAgent", "version": __version__}, "capabilities": {"experimentalApi": False}},
        )
        self._send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
        policy = {
            "off": "Do not use MCP servers.",
            "auto": "Use configured MCP servers only when necessary and expose requests for user approval.",
            "all": "Configured MCP servers may be used when necessary and must remain observable.",
        }.get(self.mcp_policy, "Use only MCP server %s when necessary." % self.mcp_policy)
        if self.mcp_policy.startswith("allow:"):
            names = ", ".join(name for name in self.mcp_policy[6:].split(",") if name)
            policy = "Use only these configured MCP servers when necessary: %s. Do not use any other MCP server." % (names or "none")
        options = {
            "cwd": str(self.cwd),
            "approvalPolicy": "on-request",
            "sandbox": "workspace-write",
            "ephemeral": False,
            "developerInstructions": (
                "You are the persistent Codex intelligence inside CrabAgent. Continue the existing conversation, "
                "work in the current workspace when action is requested, preserve user changes, and never claim an unobserved action. "
                + policy
            ),
        }
        if self.thread_id:
            try:
                result = self._request("thread/resume", {"threadId": self.thread_id, **options})
            except CodexAppServerError as exc:
                raise CodexAppServerError(
                    "Existing Codex thread %s could not be resumed; CrabAgent refused to create a silent replacement: %s"
                    % (self.thread_id, exc)
                ) from exc
            self.last_start_mode = "resumed"
        else:
            result = self._request("thread/start", options)
            self.last_start_mode = "started"
        thread = result.get("thread")
        if not isinstance(thread, dict) or not thread.get("id"):
            raise CodexAppServerError("Codex did not return a thread id")
        self.thread_id = str(thread["id"])
        self._started = True
        return self.thread_id

    def run_turn(
        self,
        prompt: str,
        emit: Callable[[CodexLiveEvent], None],
        *,
        model: str = "",
        effort: str = "",
        cancel_event: Optional[threading.Event] = None,
        timeout_seconds: Optional[float] = None,
        idle_timeout_seconds: Optional[float] = 1800.0,
    ) -> CodexLiveTurn:
        self.start()
        started_at = utc_now()
        params: Dict[str, Any] = {"threadId": self.thread_id, "input": [{"type": "text", "text": prompt}]}
        if model and model not in {"auto", "codex-session-default"}:
            params["model"] = model
        if effort and effort != "auto":
            params["effort"] = effort
        result = self._request("turn/start", params, timeout=120.0)
        turn = result.get("turn")
        if not isinstance(turn, dict) or not turn.get("id"):
            raise CodexAppServerError("Codex did not return a turn id")
        self._active_turn_id = str(turn["id"])
        parts = []
        usage: Dict[str, Any] = {}
        interrupted = False
        started_monotonic = time.monotonic()
        last_activity = started_monotonic
        # `timeout_seconds` remains a compatibility hard deadline for callers
        # that explicitly pass it. CrabAgent itself uses idle timeout only.
        hard_deadline = started_monotonic + timeout_seconds if timeout_seconds is not None else None
        server_closed = False
        idle_timed_out = False
        while True:
            if cancel_event is not None and cancel_event.is_set() and not interrupted:
                interrupted = True
                self.interrupt()
            now = time.monotonic()
            if hard_deadline is not None and now >= hard_deadline:
                break
            if idle_timeout_seconds is not None and idle_timeout_seconds > 0 and now - last_activity >= idle_timeout_seconds:
                idle_timed_out = True
                break
            try:
                wait_for = 0.2
                if hard_deadline is not None:
                    wait_for = min(wait_for, max(0.01, hard_deadline - now))
                if idle_timeout_seconds is not None and idle_timeout_seconds > 0:
                    wait_for = min(wait_for, max(0.01, idle_timeout_seconds - (now - last_activity)))
                message = self._notifications.get(timeout=wait_for)
            except queue.Empty:
                continue
            last_activity = time.monotonic()
            if isinstance(message, CodexLiveEvent):
                emit(message)
                continue
            method = str(message.get("method") or "")
            event_params = message.get("params") if isinstance(message.get("params"), dict) else {}
            if method == "item/agentMessage/delta" and event_params.get("turnId") == self._active_turn_id:
                delta = str(event_params.get("delta") or "")
                parts.append(delta)
                emit(CodexLiveEvent("assistant_delta", delta, event_params))
            elif method == "thread/tokenUsage/updated" and event_params.get("turnId") == self._active_turn_id:
                observed = event_params.get("tokenUsage")
                if isinstance(observed, dict):
                    usage = observed
                    emit(CodexLiveEvent("usage", data=usage))
            elif method in {"item/started", "item/completed"}:
                item = event_params.get("item") if isinstance(event_params.get("item"), dict) else {}
                item_type = str(item.get("type") or "")
                if item_type and item_type not in {"agentMessage", "reasoning", "userMessage"}:
                    emit(CodexLiveEvent("activity", item_type, {"completed": method == "item/completed", **item}))
            elif method == "turn/completed" and isinstance(event_params.get("turn"), dict) and event_params["turn"].get("id") == self._active_turn_id:
                completed = event_params["turn"]
                self._active_turn_id = ""
                return CodexLiveTurn(
                    self.thread_id,
                    str(completed.get("id") or ""),
                    "cancelled" if interrupted else str(completed.get("status") or "completed"),
                    "".join(parts).strip(),
                    usage,
                    started_at,
                    utc_now(),
                    str(completed.get("error") or ""),
                )
            elif method == "server/closed":
                server_closed = True
                break
        active = self._active_turn_id
        self._active_turn_id = ""
        if active:
            try:
                self.interrupt()
            except CodexAppServerError:
                pass
        if server_closed:
            error = self.stderr_tail or "Codex session closed"
        elif idle_timed_out:
            error = "Codex turn idle timed out after %ss without an event" % ("%g" % (idle_timeout_seconds or 0))
        elif hard_deadline is not None and time.monotonic() >= hard_deadline:
            error = "Codex turn hard timed out after %ss" % ("%g" % timeout_seconds)
        else:
            error = self.stderr_tail or "Codex turn ended without a completion event"
        return CodexLiveTurn(self.thread_id, active, "failed", "".join(parts).strip(), usage, started_at, utc_now(), error)

    def fork_thread(self) -> str:
        """Fork the persisted Codex context without starting a new turn."""
        if not self.thread_id:
            raise CodexAppServerError("Cannot fork a session without a Codex thread")
        self.start()
        result = self._request(
            "thread/fork",
            {
                "threadId": self.thread_id,
                "cwd": str(self.cwd),
                "ephemeral": False,
                "excludeTurns": True,
            },
        )
        thread = result.get("thread")
        if not isinstance(thread, dict) or not thread.get("id"):
            raise CodexAppServerError("Codex did not return a forked thread id")
        return str(thread["id"])

    def respond(self, request_id: str, result: Dict[str, Any]) -> None:
        if request_id not in self._requests:
            raise CodexAppServerError("runtime request is no longer pending")
        self._send({"jsonrpc": "2.0", "id": request_id, "result": result})
        self._requests.pop(request_id, None)

    def reject(self, request_id: str, message: str) -> None:
        if request_id not in self._requests:
            raise CodexAppServerError("runtime request is no longer pending")
        self._send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32001, "message": message}})
        self._requests.pop(request_id, None)

    def interrupt(self) -> None:
        if self._started and self._active_turn_id:
            try:
                self._request("turn/interrupt", {"threadId": self.thread_id, "turnId": self._active_turn_id}, timeout=8)
            except CodexAppServerError:
                pass

    def close(self) -> None:
        process = self._process
        self._process = None
        self._started = False
        self._active_turn_id = ""
        if not process or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
