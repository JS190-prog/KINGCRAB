"""Claude Code provider: the same turn interface as the Codex App Server bridge.

Each turn runs `claude -p --output-format stream-json` in the workspace and resumes
the same Claude Code session, so KING/QUEEN/WORKER/ORACLE turns keep one context.
Text arrives as assistant_delta events, tool calls as activity events, and the
final result carries usage. The session id is stored with a ``claude:`` prefix so a
saved thread is never handed to the other provider.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional

from .codex_app_server import CodexAppServerError, CodexLiveEvent, CodexLiveTurn, utc_now

THREAD_PREFIX = "claude:"


def is_claude_thread(thread_id: str) -> bool:
    return str(thread_id or "").startswith(THREAD_PREFIX)


class ClaudeCodeSession:
    """Turn-by-turn bridge to the locally authenticated Claude Code CLI."""

    provider = "claude"

    def __init__(
        self,
        executable: str,
        cwd: Path,
        *,
        thread_id: str = "",
        mcp_policy: str = "auto",
        mcp_servers: Optional[List[str]] = None,
        popen_factory: Callable[..., Any] = subprocess.Popen,
        permission_mode: Optional[str] = None,
    ) -> None:
        self.executable = executable
        self.cwd = cwd.resolve()
        self._session_id = thread_id[len(THREAD_PREFIX):] if is_claude_thread(thread_id) else ""
        self.mcp_policy = mcp_policy
        self.mcp_servers = list(mcp_servers or [])
        self._popen_factory = popen_factory
        self.permission_mode = permission_mode or os.environ.get("CRAB_CLAUDE_PERMISSION_MODE") or "acceptEdits"
        self._process: Optional[Any] = None
        self._lock = threading.Lock()
        self._resume = bool(self._session_id)
        self._fork_next = False
        self._active_turn_id = ""
        self._stderr: Deque[str] = deque(maxlen=80)
        self.last_start_mode = "unstarted"

    @property
    def thread_id(self) -> str:
        return THREAD_PREFIX + self._session_id if self._session_id else ""

    @property
    def active_turn_id(self) -> str:
        return self._active_turn_id

    @property
    def stderr_tail(self) -> str:
        return "\n".join(self._stderr)

    def start(self) -> str:
        if not self._session_id:
            self._session_id = str(uuid.uuid4())
            self.last_start_mode = "started"
        elif self.last_start_mode == "unstarted":
            self.last_start_mode = "resumed"
        return self.thread_id

    def _command(self, model: str) -> List[str]:
        command = [self.executable, "-p", "--output-format", "stream-json", "--verbose", "--permission-mode", self.permission_mode]
        if self._resume:
            command += ["--resume", self._session_id]
            if self._fork_next:
                command.append("--fork-session")
        else:
            command += ["--session-id", self._session_id]
        if model and not model.startswith("gpt") and model not in {"auto", "codex-session-default"}:
            command += ["--model", model]
        if self.mcp_policy == "off":
            command += ["--strict-mcp-config", "--mcp-config", '{"mcpServers": {}}']
        return command

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
        turn_id = "claude-turn-%s" % uuid.uuid4().hex[:12]
        self._active_turn_id = turn_id
        command = self._command(model)
        try:
            process = self._popen_factory(
                command,
                cwd=str(self.cwd),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            self._active_turn_id = ""
            raise CodexAppServerError("Claude Code could not start: %s" % exc)
        with self._lock:
            self._process = process
        threading.Thread(target=self._drain_stderr, args=(process,), daemon=True).start()
        try:
            process.stdin.write(prompt)
            process.stdin.close()
        except (OSError, ValueError):
            pass

        lines = _LineQueue(process.stdout)
        parts: List[str] = []
        usage: Dict[str, Any] = {}
        result_text = ""
        status = "failed"
        error = ""
        interrupted = False
        started = time.monotonic()
        last_activity = started
        while True:
            if cancel_event is not None and cancel_event.is_set() and not interrupted:
                interrupted = True
                self.interrupt()
            now = time.monotonic()
            if timeout_seconds is not None and now - started >= timeout_seconds:
                error = "Claude turn hard timed out after %ss" % ("%g" % timeout_seconds)
                self.interrupt()
                break
            if idle_timeout_seconds and now - last_activity >= idle_timeout_seconds:
                error = "Claude turn idle timed out after %ss without an event" % ("%g" % idle_timeout_seconds)
                self.interrupt()
                break
            line = lines.get(0.2)
            if line is None:
                continue
            if line is _LineQueue.EOF:
                break
            last_activity = time.monotonic()
            try:
                event = json.loads(line)
            except ValueError:
                continue
            kind = event.get("type")
            if kind == "system" and event.get("session_id"):
                self._session_id = str(event["session_id"])
            elif kind == "assistant":
                for block in (event.get("message") or {}).get("content") or []:
                    if block.get("type") == "text" and block.get("text"):
                        parts.append(block["text"])
                        emit(CodexLiveEvent("assistant_delta", block["text"], {"turnId": turn_id}))
                    elif block.get("type") == "tool_use":
                        emit(CodexLiveEvent("activity", str(block.get("name") or "tool"), {"completed": False, "type": "toolCall", "name": block.get("name")}))
            elif kind == "result":
                usage = dict(event.get("usage") or {})
                if event.get("total_cost_usd") is not None:
                    usage["total_cost_usd"] = event.get("total_cost_usd")
                emit(CodexLiveEvent("usage", data=usage))
                result_text = str(event.get("result") or "")
                if event.get("session_id"):
                    self._session_id = str(event["session_id"])
                if event.get("is_error") or event.get("subtype") not in (None, "success"):
                    error = result_text or str(event.get("subtype") or "Claude turn failed")
                    status = "failed"
                else:
                    status = "completed"
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        with self._lock:
            self._process = None
        self._active_turn_id = ""
        if status == "completed" or result_text:
            # A turn that ran created the session; later turns resume it.
            self._resume = True
            self._fork_next = False
        if interrupted:
            status = "cancelled"
        if status == "failed" and not error:
            error = self.stderr_tail or "Claude turn ended without a result event"
        text = ("".join(parts) or result_text).strip()
        return CodexLiveTurn(self.thread_id, turn_id, status, text, usage, started_at, utc_now(), error)

    def _drain_stderr(self, process: Any) -> None:
        try:
            for line in process.stderr:
                self._stderr.append(line.rstrip())
        except (OSError, ValueError):
            pass

    def fork_thread(self) -> str:
        """The next turn resumes this session as a fork; the original stays untouched."""
        self.start()
        self._fork_next = self._resume
        return self.thread_id

    def respond(self, request_id: str, result: Dict[str, Any]) -> None:
        """Claude Code print mode has no mid-turn approval requests; permissions follow permission_mode."""

    def reject(self, request_id: str, message: str) -> None:
        """See respond."""

    def interrupt(self) -> None:
        with self._lock:
            process = self._process
        if process is not None and process.poll() is None:
            process.terminate()

    def close(self) -> None:
        self.interrupt()


class _LineQueue:
    """Reads a text stream on a thread so the turn loop can poll with a timeout."""

    EOF = object()

    def __init__(self, stream: Any) -> None:
        import queue

        self._queue: "queue.Queue[Any]" = queue.Queue()
        threading.Thread(target=self._pump, args=(stream,), daemon=True).start()

    def _pump(self, stream: Any) -> None:
        try:
            for line in stream:
                self._queue.put(line)
        except (OSError, ValueError):
            pass
        self._queue.put(self.EOF)

    def get(self, timeout: float) -> Any:
        import queue

        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None
