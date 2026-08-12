from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


SCHEMA = "crab.orchestration/v1"
TERMINAL_CHILD_STATUSES = {"completed", "failed", "cancelled"}
TERMINAL_STATUSES = {"completed", "failed", "cancelled", "interrupted"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def orchestration_id() -> str:
    return "orch-" + uuid.uuid4().hex[:12]


def child_id() -> str:
    return "child-" + uuid.uuid4().hex[:12]


def _clean_text(value: Any, limit: int = 4000) -> str:
    return " ".join(str(value or "").split())[:limit].strip()


def normalize_children(children: Iterable[Any], *, default_session_id: str = "") -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    seen_sessions = set()
    for raw in children:
        if isinstance(raw, str):
            raw = {"session_id": default_session_id, "objective": raw}
        if not isinstance(raw, dict):
            raise ValueError("each orchestration child must be an object")
        session_id = _clean_text(raw.get("session_id") or default_session_id, 160)
        objective = _clean_text(raw.get("objective"), 4000)
        if not session_id:
            raise ValueError("each orchestration child needs a session_id")
        if not objective:
            raise ValueError("each orchestration child needs an objective")
        if session_id in seen_sessions:
            raise ValueError("a session may appear only once in one orchestration: %s" % session_id)
        seen_sessions.add(session_id)
        normalized.append(
            {
                "child_id": _clean_text(raw.get("child_id"), 80) or child_id(),
                "session_id": session_id,
                "objective": objective,
                "interaction": _clean_text(raw.get("interaction"), 32),
                "project_root": _clean_text(raw.get("project_root"), 1000),
                "status": "pending",
                "dispatch": None,
                "error": None,
                "queued_at": utc_now(),
                "started_at": None,
                "finished_at": None,
            }
        )
    if not normalized:
        raise ValueError("at least one orchestration child is required")
    if len(normalized) > 16:
        raise ValueError("an orchestration may contain at most 16 child missions")
    return normalized


class OrchestrationStore:
    """Small atomic JSON state files for the coordinator control plane.

    Mission truth remains in SQLite. This file is the durable fan-out/fan-in
    index, which lets a fresh daemon inspect a multi-session run without
    inventing a second mission schema or pretending that a child is active.
    """

    def __init__(self, workspace: Path) -> None:
        self.root = workspace.resolve() / ".crabagent" / "orchestrations"
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def path(self, run_id: str) -> Path:
        return self.root / ("%s.json" % run_id)

    def save(self, state: Dict[str, Any]) -> Dict[str, Any]:
        run_id = _clean_text(state.get("orchestration_id"), 80)
        if not run_id:
            raise ValueError("orchestration_id is required")
        payload = dict(state)
        payload["schema"] = SCHEMA
        payload["updated_at"] = utc_now()
        with self._lock:
            temp = self.path(run_id).with_suffix(".json.tmp-%s" % os.getpid())
            temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(str(temp), str(self.path(run_id)))
        return payload

    def load(self, run_id: str) -> Optional[Dict[str, Any]]:
        try:
            value = json.loads(self.path(run_id).read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def list(self, limit: int = 50) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for path in sorted(self.root.glob("orch-*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                rows.append(value)
            if len(rows) >= max(1, min(int(limit), 200)):
                break
        return rows

    def reconcile_after_restart(self) -> List[str]:
        """Mark in-flight coordinators interrupted; never auto-resume work."""
        interrupted: List[str] = []
        for state in self.list(limit=200):
            if str(state.get("status") or "") != "running":
                continue
            state["status"] = "interrupted"
            state["stop_reason"] = "crabd restarted; resume requires an explicit orchestration.run"
            for child in state.get("children") or []:
                if str(child.get("status") or "") == "running":
                    child["status"] = "pending"
                    child["dispatch"] = None
                    child["started_at"] = None
            self.save(state)
            interrupted.append(str(state.get("orchestration_id") or ""))
        return interrupted


def summarize(state: Dict[str, Any]) -> Dict[str, Any]:
    children = state.get("children") or []
    counts: Dict[str, int] = {}
    for child in children:
        status = str(child.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return {
        "orchestration_id": state.get("orchestration_id"),
        "status": state.get("status"),
        "max_parallel": state.get("max_parallel"),
        "child_count": len(children),
        "counts": counts,
        "created_at": state.get("created_at"),
        "updated_at": state.get("updated_at"),
        "stop_reason": state.get("stop_reason"),
    }
