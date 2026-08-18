from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional

from .models import (
    Approval,
    Artifact,
    ArtifactStatus,
    AttemptStatus,
    Budget,
    Checkpoint,
    EvidenceRef,
    EventRecord,
    LeaseStatus,
    MissionContract,
    MissionStatus,
    ModelRoute,
    Role,
    RoleAssignment,
    TaskSlot,
    TaskStatus,
    WorkerPolicy,
    worker_capacity,
    utc_now,
)
from .identity import COLONY_PROTOCOL_VERSION
from .ontology_contract import ONTOLOGY_LEDGER_KIND, validate_ontology_ledger


SCHEMA = """
CREATE TABLE IF NOT EXISTS missions (
    mission_id TEXT PRIMARY KEY,
    objective TEXT NOT NULL,
    acceptance_json TEXT NOT NULL,
    workspace TEXT NOT NULL,
    risk TEXT NOT NULL,
    max_attempts INTEGER NOT NULL,
    max_workers INTEGER NOT NULL,
    worker_policy TEXT NOT NULL DEFAULT 'fixed',
    token_budget INTEGER,
    status TEXT NOT NULL,
    oracle_result_artifact_id TEXT,
    session_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_slots (
    task_id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL REFERENCES missions(mission_id),
    title TEXT NOT NULL,
    role TEXT NOT NULL,
    position INTEGER NOT NULL,
    output_contract TEXT NOT NULL,
    status TEXT NOT NULL,
    depends_on_json TEXT NOT NULL,
    task_kind TEXT NOT NULL DEFAULT 'stage',
    subgoal_id TEXT NOT NULL DEFAULT '',
    execution_mode TEXT NOT NULL DEFAULT 'serial',
    write_scope TEXT NOT NULL DEFAULT 'none',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(mission_id, position)
);

CREATE TABLE IF NOT EXISTS role_assignments (
    assignment_id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL REFERENCES missions(mission_id),
    task_id TEXT NOT NULL REFERENCES task_slots(task_id),
    role TEXT NOT NULL,
    provider TEXT NOT NULL,
    profile TEXT NOT NULL,
    model TEXT NOT NULL,
    effort TEXT NOT NULL,
    invocation_status TEXT NOT NULL,
    reason TEXT NOT NULL,
    assigned_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS attempts (
    attempt_id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL REFERENCES missions(mission_id),
    task_id TEXT NOT NULL REFERENCES task_slots(task_id),
    executor TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    error TEXT
);

CREATE TABLE IF NOT EXISTS leases (
    lease_id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL REFERENCES missions(mission_id),
    task_id TEXT NOT NULL REFERENCES task_slots(task_id),
    attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
    holder TEXT NOT NULL,
    status TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    released_at TEXT
);

CREATE TABLE IF NOT EXISTS events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_id TEXT,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL REFERENCES missions(mission_id),
    task_id TEXT NOT NULL REFERENCES task_slots(task_id),
    kind TEXT NOT NULL,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence_refs (
    evidence_id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL REFERENCES missions(mission_id),
    artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
    source_type TEXT NOT NULL,
    source_uri TEXT NOT NULL,
    digest TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
    approval_id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL REFERENCES missions(mission_id),
    action TEXT NOT NULL,
    decision TEXT NOT NULL,
    decided_by TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL REFERENCES missions(mission_id),
    sequence INTEGER NOT NULL,
    state_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(mission_id, sequence)
);

CREATE TABLE IF NOT EXISTS budgets (
    mission_id TEXT PRIMARY KEY REFERENCES missions(mission_id),
    currency TEXT NOT NULL,
    token_limit INTEGER,
    tokens_observed INTEGER,
    cost_observed REAL,
    observation_source TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    root_path TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pack_ingest_runs (
    run_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    folder_path TEXT NOT NULL,
    status TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tool_receipts (
    receipt_id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL REFERENCES missions(mission_id),
    task_id TEXT NOT NULL REFERENCES task_slots(task_id),
    attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
    tool_name TEXT NOT NULL,
    status TEXT NOT NULL,
    observed_json TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    codex_thread_id TEXT NOT NULL DEFAULT '',
    active_mission_id TEXT,
    model_policy TEXT NOT NULL DEFAULT 'auto',
    interaction_mode TEXT NOT NULL DEFAULT 'auto',
    executor_policy TEXT NOT NULL DEFAULT 'codex',
    mcp_policy TEXT NOT NULL DEFAULT 'auto',
    cli_policy TEXT NOT NULL DEFAULT 'auto',
    max_workers INTEGER NOT NULL DEFAULT 3,
    worker_policy TEXT NOT NULL DEFAULT 'auto',
    project_id TEXT NOT NULL DEFAULT '',
    project_name TEXT NOT NULL DEFAULT '',
    project_root_path TEXT NOT NULL DEFAULT '',
    ontology_context_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS session_ontology_context (
    session_id TEXT PRIMARY KEY REFERENCES sessions(session_id) ON DELETE CASCADE,
    project_ids_json TEXT NOT NULL,
    package_ids_json TEXT NOT NULL,
    project_labels_json TEXT NOT NULL,
    package_labels_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS integration_cache (
    integration_key TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    error TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    message_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    mission_id TEXT,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS input_queue (
    queue_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    content TEXT NOT NULL,
    disposition TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    delivered_at TEXT
);

CREATE TABLE IF NOT EXISTS runtime_requests (
    request_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    mission_id TEXT,
    request_type TEXT NOT NULL,
    method TEXT NOT NULL,
    prompt TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_mission ON events(mission_id, event_id);
CREATE INDEX IF NOT EXISTS idx_tasks_mission ON task_slots(mission_id, position);
CREATE INDEX IF NOT EXISTS idx_artifacts_mission ON artifacts(mission_id, created_at);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, message_id);
CREATE INDEX IF NOT EXISTS idx_queue_session ON input_queue(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_missions_session_updated ON missions(session_id, updated_at DESC);
"""


MISSION_TRANSITIONS = {
    MissionStatus.CREATED: {MissionStatus.PLANNED, MissionStatus.CANCELLED},
    MissionStatus.PLANNED: {MissionStatus.RUNNING, MissionStatus.CANCELLED},
    MissionStatus.RUNNING: {MissionStatus.VERIFYING, MissionStatus.FAILED, MissionStatus.CANCELLED},
    MissionStatus.VERIFYING: {MissionStatus.COMPLETED, MissionStatus.RUNNING, MissionStatus.FAILED},
    MissionStatus.COMPLETED: set(),
    MissionStatus.FAILED: set(),
    MissionStatus.CANCELLED: set(),
}


class ColonyStore:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()
        self.home = self.workspace / ".crabagent"
        self.database = self.home / "state.sqlite3"
        self.artifacts_dir = self.home / "artifacts"

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        self.home.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.database), timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> Dict[str, str]:
        first_initialize = not self.database.exists()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            connection.executescript(SCHEMA)
            mission_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(missions)").fetchall()
            }
            if "session_id" not in mission_columns:
                connection.execute("ALTER TABLE missions ADD COLUMN session_id TEXT")
            if "worker_policy" not in mission_columns:
                connection.execute("ALTER TABLE missions ADD COLUMN worker_policy TEXT NOT NULL DEFAULT 'fixed'")
            connection.execute("UPDATE missions SET worker_policy = 'fixed' WHERE worker_policy IS NULL OR worker_policy = ''")
            task_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(task_slots)").fetchall()
            }
            if "task_kind" not in task_columns:
                connection.execute("ALTER TABLE task_slots ADD COLUMN task_kind TEXT NOT NULL DEFAULT 'stage'")
            if "subgoal_id" not in task_columns:
                connection.execute("ALTER TABLE task_slots ADD COLUMN subgoal_id TEXT NOT NULL DEFAULT ''")
            if "execution_mode" not in task_columns:
                connection.execute("ALTER TABLE task_slots ADD COLUMN execution_mode TEXT NOT NULL DEFAULT 'serial'")
            if "write_scope" not in task_columns:
                connection.execute("ALTER TABLE task_slots ADD COLUMN write_scope TEXT NOT NULL DEFAULT 'none'")
            session_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(sessions)").fetchall()
            }
            if "model_policy" not in session_columns:
                connection.execute("ALTER TABLE sessions ADD COLUMN model_policy TEXT NOT NULL DEFAULT 'auto'")
            if "interaction_mode" not in session_columns:
                connection.execute("ALTER TABLE sessions ADD COLUMN interaction_mode TEXT NOT NULL DEFAULT 'auto'")
            if "executor_policy" not in session_columns:
                connection.execute("ALTER TABLE sessions ADD COLUMN executor_policy TEXT NOT NULL DEFAULT 'codex'")
            connection.execute("UPDATE sessions SET executor_policy = 'codex' WHERE executor_policy IS NULL OR executor_policy = ''")
            if "cli_policy" not in session_columns:
                connection.execute("ALTER TABLE sessions ADD COLUMN cli_policy TEXT NOT NULL DEFAULT 'auto'")
            if "project_id" not in session_columns:
                connection.execute("ALTER TABLE sessions ADD COLUMN project_id TEXT NOT NULL DEFAULT ''")
            if "project_name" not in session_columns:
                connection.execute("ALTER TABLE sessions ADD COLUMN project_name TEXT NOT NULL DEFAULT ''")
            if "project_root_path" not in session_columns:
                connection.execute("ALTER TABLE sessions ADD COLUMN project_root_path TEXT NOT NULL DEFAULT ''")
            if "ontology_context_count" not in session_columns:
                connection.execute("ALTER TABLE sessions ADD COLUMN ontology_context_count INTEGER NOT NULL DEFAULT 0")
            if "worker_policy" not in session_columns:
                connection.execute("ALTER TABLE sessions ADD COLUMN worker_policy TEXT NOT NULL DEFAULT 'auto'")
            connection.execute("UPDATE sessions SET worker_policy = 'auto' WHERE worker_policy IS NULL OR worker_policy = ''")
            project_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(projects)").fetchall()
            }
            if "root_path" not in project_columns:
                connection.execute("ALTER TABLE projects ADD COLUMN root_path TEXT NOT NULL DEFAULT ''")
        config = self.home / "config.json"
        if not config.exists():
            config.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "provider": "codex",
                        "router": "soltelu",
                        "runtime": "crabd",
                        "colony_protocol": COLONY_PROTOCOL_VERSION,
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        if first_initialize:
            self.append_event(None, "workspace_initialized", "SYSTEM", {"workspace": str(self.workspace)})
        return {"workspace": str(self.workspace), "database": str(self.database), "config": str(config)}

    def append_event(
        self,
        mission_id: Optional[str],
        event_type: str,
        actor: str,
        payload: Dict[str, Any],
        connection: Optional[sqlite3.Connection] = None,
    ) -> int:
        def insert(target: sqlite3.Connection) -> int:
            cursor = target.execute(
                "INSERT INTO events(mission_id, event_type, actor, payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (mission_id, event_type, actor, json.dumps(payload, ensure_ascii=False), utc_now()),
            )
            return int(cursor.lastrowid)

        if connection is not None:
            return insert(connection)
        with self.connection() as owned:
            return insert(owned)

    def create_mission(self, contract: MissionContract, session_id: Optional[str] = None) -> str:
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO missions(
                    mission_id, objective, acceptance_json, workspace, risk,
                    max_attempts, max_workers, worker_policy, token_budget, status, session_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    contract.mission_id,
                    contract.objective,
                    json.dumps(contract.acceptance, ensure_ascii=False),
                    contract.workspace,
                    contract.risk,
                    contract.max_attempts,
                    contract.max_workers,
                    contract.worker_policy,
                    contract.token_budget,
                    MissionStatus.CREATED.value,
                    session_id,
                    contract.created_at,
                    contract.created_at,
                ),
            )
            self.append_event(
                contract.mission_id,
                "mission_created",
                Role.KING.value,
                contract.to_dict(),
                connection,
            )
        return contract.mission_id

    def create_session(
        self,
        title: str = "CrabAgent session",
        model_policy: str = "auto",
        interaction_mode: str = "auto",
        executor_policy: str = "codex",
        mcp_policy: str = "auto",
        cli_policy: str = "auto",
        max_workers: int = 3,
        worker_policy: str = WorkerPolicy.AUTO.value,
        project_id: str = "",
        project_name: str = "",
        project_root_path: str = "",
    ) -> Dict[str, Any]:
        self.initialize()
        session_id = new_id("session")
        now = utc_now()
        policy = worker_policy if worker_policy in {item.value for item in WorkerPolicy} else WorkerPolicy.AUTO.value
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO sessions(session_id, title, status, model_policy, interaction_mode, executor_policy, mcp_policy, cli_policy, max_workers, worker_policy, project_id, project_name, project_root_path, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (session_id, title, "ready", model_policy, interaction_mode, executor_policy, mcp_policy, cli_policy, max(1, min(max_workers, 8)), policy, project_id, project_name, project_root_path, now, now),
            )
            self.append_event(None, "session_created", "SYSTEM", {"session_id": session_id, "title": title}, connection)
        return self.session(session_id) or {}

    def fork_session(
        self,
        source_session_id: str,
        title: str = "",
        codex_thread_id: str = "",
    ) -> Dict[str, Any]:
        """Copy a conversation into a ready session without copying live runtime state."""
        self.initialize()
        source = self.session(source_session_id)
        if source is None:
            raise ValueError("unknown session: %s" % source_session_id)
        session_id = new_id("session")
        now = utc_now()
        branch_title = title.strip() or "%s branch" % source["title"]
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO sessions(
                    session_id, title, status, codex_thread_id, active_mission_id,
                    model_policy, interaction_mode, executor_policy, mcp_policy, cli_policy, max_workers,
                    worker_policy, project_id, project_name, project_root_path,
                    ontology_context_count, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    branch_title,
                    "ready",
                    codex_thread_id,
                    None,
                    source["model_policy"],
                    source["interaction_mode"],
                    source.get("executor_policy") or "codex",
                    source["mcp_policy"],
                    source.get("cli_policy") or "auto",
                    source["max_workers"],
                    source.get("worker_policy") or WorkerPolicy.AUTO.value,
                    source.get("project_id") or "",
                    source.get("project_name") or "",
                    source.get("project_root_path") or "",
                    source.get("ontology_context_count") or 0,
                    now,
                    now,
                ),
            )
            rows = connection.execute(
                "SELECT role, content, metadata_json, created_at FROM messages WHERE session_id = ? ORDER BY message_id",
                (source_session_id,),
            ).fetchall()
            for row in rows:
                metadata = json.loads(row["metadata_json"])
                metadata["forked_from_session"] = source_session_id
                connection.execute(
                    "INSERT INTO messages(session_id, mission_id, role, content, metadata_json, created_at) VALUES (?, NULL, ?, ?, ?, ?)",
                    (session_id, row["role"], row["content"], json.dumps(metadata, ensure_ascii=False), row["created_at"]),
                )
            context = connection.execute(
                "SELECT project_ids_json, package_ids_json, project_labels_json, package_labels_json FROM session_ontology_context WHERE session_id = ?",
                (source_session_id,),
            ).fetchone()
            if context is not None:
                connection.execute(
                    "INSERT INTO session_ontology_context(session_id, project_ids_json, package_ids_json, project_labels_json, package_labels_json, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (session_id, context["project_ids_json"], context["package_ids_json"], context["project_labels_json"], context["package_labels_json"], now),
                )
            self.append_event(
                None,
                "session_forked",
                "USER",
                {"source_session_id": source_session_id, "session_id": session_id, "copied_messages": len(rows)},
                connection,
            )
        session = self.session(session_id)
        if session is None:
            raise RuntimeError("forked session was not persisted")
        return {
            "session": session,
            "copied_messages": len(rows),
            "source_session_id": source_session_id,
            "codex_context_forked": bool(codex_thread_id),
        }

    def session(self, session_id: str) -> Optional[Dict[str, Any]]:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        return dict(row) if row is not None else None

    def latest_session(self) -> Optional[Dict[str, Any]]:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM sessions ORDER BY updated_at DESC LIMIT 1").fetchone()
        return dict(row) if row is not None else None

    def list_sessions(self, limit: int = 40) -> List[Dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM sessions ORDER BY updated_at DESC LIMIT ?", (max(1, min(limit, 200)),)
            ).fetchall()
        return [dict(row) for row in rows]

    def project(self, project_id: str = "", name: str = "") -> Optional[Dict[str, Any]]:
        with self.connection() as connection:
            if project_id:
                row = connection.execute("SELECT * FROM projects WHERE project_id = ?", (project_id,)).fetchone()
            else:
                row = connection.execute("SELECT * FROM projects WHERE name = ?", (name,)).fetchone()
        return dict(row) if row is not None else None

    def list_projects(self) -> List[Dict[str, Any]]:
        with self.connection() as connection:
            orphaned = connection.execute(
                """
                SELECT DISTINCT s.project_id, s.project_name
                FROM sessions s
                WHERE s.project_id <> '' AND s.project_name <> ''
                  AND NOT EXISTS (
                      SELECT 1 FROM projects p WHERE p.project_id = s.project_id
                  )
                """
            ).fetchall()
            now = utc_now()
            for row in orphaned:
                connection.execute(
                    "INSERT OR IGNORE INTO projects(project_id, name, root_path, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (row["project_id"], row["project_name"], "", now, now),
                )
            rows = connection.execute(
                """
                SELECT p.project_id, p.name, p.root_path, p.created_at, p.updated_at,
                       COUNT(s.session_id) AS session_count
                FROM projects p
                LEFT JOIN sessions s ON s.project_id = p.project_id
                GROUP BY p.project_id
                ORDER BY lower(p.name), p.project_id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def create_project(self, name: str, project_id: str = "", root_path: str = "") -> Dict[str, Any]:
        self.initialize()
        clean_name = name.strip()
        if not clean_name or clean_name.lower() == "unassigned":
            raise ValueError("a non-empty project name is required")
        existing = self.project(name=clean_name)
        if existing:
            if root_path and str(existing.get("root_path") or "") != str(Path(root_path).expanduser().resolve()):
                return self.set_project_root(str(existing["project_id"]), root_path)
            return existing
        normalized_root = self._normalize_project_root(root_path)
        project_id = project_id.strip() or new_id("project")
        now = utc_now()
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO projects(project_id, name, root_path, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (project_id, clean_name, normalized_root, now, now),
            )
            self.append_event(None, "project_created", "USER", {"project_id": project_id, "name": clean_name, "root_path": normalized_root}, connection)
        return self.project(project_id=project_id) or {}

    def ensure_project(self, name: str, project_id: str = "", root_path: str = "") -> Dict[str, Any]:
        if project_id:
            existing = self.project(project_id=project_id)
            if existing:
                return existing
            return self.create_project(name, project_id=project_id, root_path=root_path)
        return self.create_project(name, root_path=root_path)

    @staticmethod
    def _normalize_project_root(root_path: str) -> str:
        value = str(root_path or "").strip()
        if not value:
            return ""
        path = Path(value).expanduser().resolve()
        if not path.is_dir():
            raise ValueError("project folder does not exist or is not a directory: %s" % path)
        return str(path)

    def set_project_root(self, project_id: str, root_path: str) -> Dict[str, Any]:
        project = self.project(project_id=project_id)
        if project is None:
            raise ValueError("unknown project: %s" % project_id)
        normalized_root = self._normalize_project_root(root_path)
        now = utc_now()
        with self.connection() as connection:
            connection.execute("UPDATE projects SET root_path = ?, updated_at = ? WHERE project_id = ?", (normalized_root, now, project_id))
            connection.execute("UPDATE sessions SET project_root_path = ?, updated_at = ? WHERE project_id = ?", (normalized_root, now, project_id))
            self.append_event(None, "project_folder_changed", "USER", {"project_id": project_id, "root_path": normalized_root}, connection)
        return self.project(project_id=project_id) or {}

    def record_pack_ingest_run(
        self,
        run_id: str,
        project_id: str,
        folder_path: str,
        status: str,
        result: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        now = utc_now()
        payload = json.dumps(result or {}, ensure_ascii=False)
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO pack_ingest_runs(run_id, project_id, folder_path, status, result_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    project_id = excluded.project_id,
                    folder_path = excluded.folder_path,
                    status = excluded.status,
                    result_json = excluded.result_json,
                    updated_at = excluded.updated_at
                """,
                (run_id, project_id, folder_path, status, payload, now, now),
            )
            self.append_event(
                None,
                "pack_ingest_%s" % status,
                "QUEEN",
                {"run_id": run_id, "project_id": project_id, "folder_path": folder_path, "status": status},
                connection,
            )
        return self.pack_ingest_run(run_id) or {}

    def pack_ingest_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM pack_ingest_runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        try:
            result["result"] = json.loads(result.pop("result_json"))
        except (TypeError, ValueError):
            result["result"] = {}
        return result

    def list_pack_ingest_runs(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM pack_ingest_runs ORDER BY updated_at DESC LIMIT ?",
                (max(1, min(int(limit), 100)),),
            ).fetchall()
        values = []
        for row in rows:
            item = dict(row)
            try:
                item["result"] = json.loads(item.pop("result_json"))
            except (TypeError, ValueError):
                item["result"] = {}
            values.append(item)
        return values

    def rename_project(self, project_id: str, name: str) -> Dict[str, Any]:
        project = self.project(project_id=project_id)
        if project is None:
            raise ValueError("unknown project: %s" % project_id)
        clean_name = name.strip()
        if not clean_name or clean_name.lower() == "unassigned":
            raise ValueError("a non-empty project name is required")
        now = utc_now()
        with self.connection() as connection:
            connection.execute(
                "UPDATE projects SET name = ?, updated_at = ? WHERE project_id = ?",
                (clean_name, now, project_id),
            )
            connection.execute(
                "UPDATE sessions SET project_name = ?, updated_at = ? WHERE project_id = ?",
                (clean_name, now, project_id),
            )
            self.append_event(None, "project_renamed", "USER", {"project_id": project_id, "name": clean_name}, connection)
        return self.project(project_id=project_id) or {}

    def delete_project(self, project_id: str) -> Dict[str, Any]:
        project = self.project(project_id=project_id)
        if project is None:
            raise ValueError("unknown project: %s" % project_id)
        now = utc_now()
        with self.connection() as connection:
            connection.execute(
                "DELETE FROM session_ontology_context WHERE session_id IN (SELECT session_id FROM sessions WHERE project_id = ?)",
                (project_id,),
            )
            connection.execute(
                "UPDATE sessions SET project_id = '', project_name = '', project_root_path = '', ontology_context_count = 0, updated_at = ? WHERE project_id = ?",
                (now, project_id),
            )
            connection.execute("DELETE FROM projects WHERE project_id = ?", (project_id,))
            self.append_event(None, "project_deleted", "USER", {"project_id": project_id, "name": project["name"]}, connection)
        return project

    def update_session(self, session_id: str, **changes: Any) -> Dict[str, Any]:
        allowed = {
            "title", "status", "codex_thread_id", "active_mission_id", "model_policy",
            "interaction_mode", "executor_policy", "mcp_policy", "cli_policy", "max_workers", "project_id", "project_name",
            "project_root_path", "ontology_context_count", "worker_policy",
        }
        clean = {key: value for key, value in changes.items() if key in allowed}
        if "worker_policy" in clean and clean["worker_policy"] not in {item.value for item in WorkerPolicy}:
            clean["worker_policy"] = WorkerPolicy.AUTO.value
        if not clean:
            return self.session(session_id) or {}
        clean["updated_at"] = utc_now()
        assignments = ", ".join("%s = ?" % key for key in clean)
        with self.connection() as connection:
            connection.execute(
                "UPDATE sessions SET %s WHERE session_id = ?" % assignments,
                [*clean.values(), session_id],
            )
        session = self.session(session_id)
        if session is None:
            raise ValueError("unknown session: %s" % session_id)
        return session

    def ontology_context(self, session_id: str) -> Dict[str, Any]:
        """Return the small user-selected live catalog context for one session."""
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM session_ontology_context WHERE session_id = ?", (session_id,)
            ).fetchone()
        if row is None:
            return {
                "session_id": session_id,
                "project_ids": [],
                "package_ids": [],
                "project_labels": {},
                "package_labels": {},
                "count": 0,
                "updated_at": None,
            }

        def decode(value: Any, default: Any) -> Any:
            try:
                parsed = json.loads(str(value or ""))
            except (TypeError, ValueError):
                return default
            return parsed

        package_ids = decode(row["package_ids_json"], [])
        if not isinstance(package_ids, list):
            package_ids = []
        project_ids = decode(row["project_ids_json"], [])
        project_labels = decode(row["project_labels_json"], {})
        package_labels = decode(row["package_labels_json"], {})
        if not isinstance(project_ids, list):
            project_ids = []
        if not isinstance(project_labels, dict):
            project_labels = {}
        if not isinstance(package_labels, dict):
            package_labels = {}
        return {
            "session_id": session_id,
            "project_ids": project_ids,
            "package_ids": package_ids,
            "project_labels": project_labels,
            "package_labels": package_labels,
            "count": len(package_ids),
            "updated_at": row["updated_at"],
        }

    def set_ontology_context(
        self,
        session_id: str,
        project_ids: Iterable[str],
        package_ids: Iterable[str],
        project_labels: Optional[Dict[str, str]] = None,
        package_labels: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        self.initialize()
        if self.session(session_id) is None:
            raise ValueError("unknown session: %s" % session_id)
        projects = list(dict.fromkeys(str(value).strip() for value in project_ids if str(value).strip()))[:200]
        packages = list(dict.fromkeys(str(value).strip() for value in package_ids if str(value).strip()))[:2000]
        project_names = {
            str(key): str(value)[:160]
            for key, value in (project_labels or {}).items()
            if str(key).strip() and str(value).strip()
        }
        package_names = {
            str(key): str(value)[:240]
            for key, value in (package_labels or {}).items()
            if str(key).strip() and str(value).strip()
        }
        now = utc_now()
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO session_ontology_context(session_id, project_ids_json, package_ids_json, project_labels_json, package_labels_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    project_ids_json = excluded.project_ids_json,
                    package_ids_json = excluded.package_ids_json,
                    project_labels_json = excluded.project_labels_json,
                    package_labels_json = excluded.package_labels_json,
                    updated_at = excluded.updated_at
                """,
                (
                    session_id,
                    json.dumps(projects, ensure_ascii=False),
                    json.dumps(packages, ensure_ascii=False),
                    json.dumps(project_names, ensure_ascii=False),
                    json.dumps(package_names, ensure_ascii=False),
                    now,
                ),
            )
            connection.execute(
                "UPDATE sessions SET ontology_context_count = ?, updated_at = ? WHERE session_id = ?",
                (len(packages), now, session_id),
            )
            self.append_event(
                None,
                "ontology_context_updated",
                "QUEEN",
                {"session_id": session_id, "project_count": len(projects), "package_count": len(packages)},
                connection,
            )
        return self.ontology_context(session_id)

    def clear_ontology_context(self, session_id: str) -> Dict[str, Any]:
        return self.set_ontology_context(session_id, [], [])

    def set_assignment_invocation(
        self,
        task_id: str,
        status: str,
        *,
        model: Optional[str] = None,
        effort: Optional[str] = None,
        profile: Optional[str] = None,
    ) -> None:
        fields = ["invocation_status = ?"]
        values: List[Any] = [status]
        if model is not None:
            fields.append("model = ?")
            values.append(model)
        if effort is not None:
            fields.append("effort = ?")
            values.append(effort)
        if profile is not None:
            fields.append("profile = ?")
            values.append(profile)
        values.append(task_id)
        with self.connection() as connection:
            row = connection.execute(
                "SELECT mission_id, role FROM role_assignments WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise ValueError("unknown assignment task: %s" % task_id)
            connection.execute(
                "UPDATE role_assignments SET %s WHERE task_id = ?" % ", ".join(fields), values
            )
            self.append_event(
                row["mission_id"],
                "route_%s" % status,
                row["role"],
                {"task_id": task_id, "model": model, "effort": effort},
                connection,
            )

    def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        mission_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> int:
        now = utc_now()
        with self.connection() as connection:
            cursor = connection.execute(
                "INSERT INTO messages(session_id, mission_id, role, content, metadata_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, mission_id, role, content, json.dumps(metadata or {}, ensure_ascii=False), now),
            )
            connection.execute("UPDATE sessions SET updated_at = ? WHERE session_id = ?", (now, session_id))
            return int(cursor.lastrowid)

    def messages(self, session_id: str, after_id: int = 0, limit: int = 500) -> List[Dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM messages WHERE session_id = ? AND message_id > ? ORDER BY message_id LIMIT ?",
                (session_id, max(0, after_id), max(1, min(limit, 10000))),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["metadata"] = json.loads(item.pop("metadata_json"))
            result.append(item)
        return result

    def message_count(self, session_id: str) -> int:
        with self.connection() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM messages WHERE session_id = ?", (session_id,)).fetchone()
        return int(row["count"] if row is not None else 0)

    def latest_message(self, session_id: str) -> Optional[Dict[str, Any]]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY message_id DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["metadata"] = json.loads(item.pop("metadata_json"))
        return item

    def set_integration_cache(
        self,
        integration_key: str,
        status: str,
        payload: Dict[str, Any],
        error: str = "",
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO integration_cache(integration_key, status, payload_json, error, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(integration_key) DO UPDATE SET
                    status = excluded.status,
                    payload_json = excluded.payload_json,
                    error = excluded.error,
                    updated_at = excluded.updated_at
                """,
                (integration_key, status, json.dumps(payload, ensure_ascii=False), error, utc_now()),
            )

    def integration_cache(self, integration_key: str) -> Optional[Dict[str, Any]]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM integration_cache WHERE integration_key = ?", (integration_key,)
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["payload"] = json.loads(item.pop("payload_json"))
        return item

    def colony_overview(self) -> Dict[str, Any]:
        """Build the panel summary with bounded SQL reads.

        The old implementation called ``inspect`` once per session. That is
        correct but unnecessarily expensive for a screen that refreshes often:
        one session could trigger a dozen short-lived SQLite connections. The
        panel only needs the latest mission and role/task status, so fetch those
        projections in two set-based queries and keep full inspection for the
        explicit inspect/replay commands.
        """
        with self.connection() as connection:
            session_rows = connection.execute(
                "SELECT * FROM sessions ORDER BY updated_at DESC LIMIT ?", (200,)
            ).fetchall()
            mission_rows = connection.execute(
                """
                SELECT * FROM missions
                WHERE session_id IS NOT NULL AND session_id <> ''
                ORDER BY updated_at DESC
                """
            ).fetchall()
            task_rows = connection.execute(
                """
                SELECT t.mission_id, t.role, t.title, t.status
                FROM task_slots t
                JOIN missions m ON m.mission_id = t.mission_id
                WHERE m.session_id IS NOT NULL AND m.session_id <> ''
                ORDER BY t.position
                """
            ).fetchall()

        sessions = [dict(row) for row in session_rows]
        missions = [dict(row) for row in mission_rows]
        tasks_by_mission: Dict[str, List[Dict[str, Any]]] = {}
        for row in task_rows:
            item = dict(row)
            tasks_by_mission.setdefault(str(item["mission_id"]), []).append(item)

        missions_by_session: Dict[str, List[Dict[str, Any]]] = {}
        missions_by_id: Dict[str, Dict[str, Any]] = {}
        for mission in missions:
            mission_id = str(mission["mission_id"])
            missions_by_id[mission_id] = mission
            missions_by_session.setdefault(str(mission["session_id"]), []).append(mission)

        active_workers = 0
        soldier_activity = "idle"
        king_rows: List[Dict[str, Any]] = []
        for session in sessions:
            session_id = str(session["session_id"])
            session_missions = missions_by_session.get(session_id, [])
            latest = session_missions[0] if session_missions else {}
            mission_id = str(session.get("active_mission_id") or latest.get("mission_id") or "")
            mission = missions_by_id.get(mission_id)
            roles: Dict[str, Dict[str, Any]] = {}
            if mission:
                for task in tasks_by_mission.get(mission_id, []):
                    role = str(task["role"])
                    roles[role] = {"status": task["status"], "title": task["title"]}
                    if role == Role.WORKER.value and task["status"] == TaskStatus.RUNNING.value:
                        active_workers += 1
                    if role == Role.SOLDIER.value and task["status"] == TaskStatus.RUNNING.value:
                        soldier_activity = str(task["title"])
                    elif role == Role.SOLDIER.value and task["status"] in {
                        TaskStatus.COMPLETED.value,
                        TaskStatus.VERIFIED.value,
                        TaskStatus.FAILED.value,
                        TaskStatus.STOPPED.value,
                    } and soldier_activity == "idle":
                        soldier_activity = task["status"]
            last_mission = {
                "mission_id": latest.get("mission_id") or (mission or {}).get("mission_id"),
                "status": latest.get("status") or (mission or {}).get("status"),
                "objective": latest.get("objective") or (mission or {}).get("objective"),
                "updated_at": latest.get("updated_at") or (mission or {}).get("updated_at"),
                "oracle_result_artifact_id": latest.get("oracle_result_artifact_id") or (mission or {}).get("oracle_result_artifact_id"),
            } if latest or mission else None
            king_rows.append(
                {
                    "session_id": session_id,
                    "king_title": session["title"],
                    "status": session["status"],
                    "project_id": session.get("project_id") or "",
                    "project_name": session.get("project_name") or "Unassigned",
                    "queen_ontology_count": int(session.get("ontology_context_count") or 0),
                    "worker_policy": str(session.get("worker_policy") or WorkerPolicy.AUTO.value),
                    "worker_limit": int(session.get("max_workers") or 3),
                    "worker_capacity": 0,
                    "runnable_slots": 0,
                    "roles": roles,
                    "active_mission_id": session.get("active_mission_id") or "",
                    "last_mission": last_mission,
                }
            )
            runnable = sum(
                1
                for task in tasks_by_mission.get(mission_id, [])
                if str(task.get("status")) in {TaskStatus.READY.value, TaskStatus.RUNNING.value}
            )
            king_rows[-1]["runnable_slots"] = runnable
            king_rows[-1]["worker_capacity"] = worker_capacity(
                king_rows[-1]["worker_policy"],
                king_rows[-1]["worker_limit"],
                runnable,
            )
        return {
            "projects": self.list_projects(),
            "sessions": king_rows,
            "active_workers": active_workers,
            "soldier_activity": soldier_activity,
        }

    def events_after(self, event_id: int = 0, limit: int = 500) -> List[Dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM events WHERE event_id > ? ORDER BY event_id LIMIT ?",
                (max(0, event_id), max(1, min(limit, 2000))),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result

    def mission_for_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM missions WHERE session_id = ? ORDER BY updated_at DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def missions_for_session(self, session_id: str, limit: int = 40) -> List[Dict[str, Any]]:
        """Return a bounded durable mission history for continuation routing."""
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM missions WHERE session_id = ? ORDER BY updated_at DESC LIMIT ?",
                (session_id, max(1, min(int(limit), 200))),
            ).fetchall()
        return [dict(row) for row in rows]

    def cancel_active_mission(self, mission_id: str, reason: str) -> None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT status FROM missions WHERE mission_id = ?", (mission_id,)
            ).fetchone()
            if row is None:
                raise ValueError("unknown mission: %s" % mission_id)
            if row["status"] in {MissionStatus.COMPLETED.value, MissionStatus.FAILED.value, MissionStatus.CANCELLED.value}:
                return
            connection.execute(
                "UPDATE missions SET status = ?, updated_at = ? WHERE mission_id = ?",
                (MissionStatus.CANCELLED.value, utc_now(), mission_id),
            )
            self.append_event(
                mission_id,
                "mission_cancelled",
                "USER",
                {"from": row["status"], "to": MissionStatus.CANCELLED.value, "reason": reason},
                connection,
            )

    def enqueue_input(self, session_id: str, content: str, disposition: str) -> Dict[str, Any]:
        queue_id = new_id("queue")
        now = utc_now()
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO input_queue(queue_id, session_id, content, disposition, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (queue_id, session_id, content, disposition, "queued", now),
            )
            self.append_event(None, "input_queued", "USER", {"queue_id": queue_id, "session_id": session_id, "disposition": disposition}, connection)
        return {"queue_id": queue_id, "session_id": session_id, "content": content, "disposition": disposition, "status": "queued", "created_at": now}

    def queued_inputs(self, session_id: str) -> List[Dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM input_queue WHERE session_id = ? AND status = 'queued' ORDER BY created_at",
                (session_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_input(self, queue_id: str, status: str) -> None:
        with self.connection() as connection:
            connection.execute(
                "UPDATE input_queue SET status = ?, delivered_at = ? WHERE queue_id = ?",
                (status, utc_now() if status == "delivered" else None, queue_id),
            )

    def add_runtime_request(
        self,
        request_id: str,
        session_id: str,
        mission_id: Optional[str],
        request_type: str,
        method: str,
        prompt: str,
        payload: Dict[str, Any],
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO runtime_requests(request_id, session_id, mission_id, request_type, method, prompt, payload_json, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (request_id, session_id, mission_id, request_type, method, prompt, json.dumps(payload, ensure_ascii=False), "pending", utc_now()),
            )

    def resolve_runtime_request(self, request_id: str, status: str) -> None:
        with self.connection() as connection:
            connection.execute(
                "UPDATE runtime_requests SET status = ?, resolved_at = ? WHERE request_id = ?",
                (status, utc_now(), request_id),
            )

    def expire_pending_requests(self, session_id: Optional[str] = None) -> int:
        with self.connection() as connection:
            if session_id:
                cursor = connection.execute(
                    "UPDATE runtime_requests SET status = 'expired', resolved_at = ? WHERE session_id = ? AND status = 'pending'",
                    (utc_now(), session_id),
                )
            else:
                cursor = connection.execute(
                    "UPDATE runtime_requests SET status = 'expired', resolved_at = ? WHERE status = 'pending'",
                    (utc_now(),),
                )
            return int(cursor.rowcount)

    def reconcile_interrupted_runtime(self) -> int:
        """Close states that cannot still be live after a fresh daemon process."""
        repaired = 0
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT session_id, active_mission_id FROM sessions WHERE status = 'running'"
            ).fetchall()
            for row in rows:
                mission_id = row["active_mission_id"]
                if mission_id:
                    mission = connection.execute(
                        "SELECT status FROM missions WHERE mission_id = ?", (mission_id,)
                    ).fetchone()
                    if mission and mission["status"] not in {
                        MissionStatus.COMPLETED.value,
                        MissionStatus.FAILED.value,
                        MissionStatus.CANCELLED.value,
                    }:
                        connection.execute(
                            "UPDATE missions SET status = ?, updated_at = ? WHERE mission_id = ?",
                            (MissionStatus.CANCELLED.value, utc_now(), mission_id),
                        )
                        self.append_event(
                            mission_id,
                            "mission_cancelled",
                            "RUNTIME",
                            {"from": mission["status"], "to": "cancelled", "reason": "runtime_restart"},
                            connection,
                        )
                connection.execute(
                    "UPDATE sessions SET status = 'ready', active_mission_id = NULL, updated_at = ? WHERE session_id = ?",
                    (utc_now(), row["session_id"]),
                )
                repaired += 1
        return repaired

    def pending_requests(self, session_id: str) -> List[Dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM runtime_requests WHERE session_id = ? AND status = 'pending' ORDER BY created_at",
                (session_id,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result

    def transition_mission(self, mission_id: str, target: MissionStatus, actor: Role) -> None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT status FROM missions WHERE mission_id = ?", (mission_id,)
            ).fetchone()
            if row is None:
                raise ValueError("unknown mission: %s" % mission_id)
            current = MissionStatus(row["status"])
            if target not in MISSION_TRANSITIONS[current]:
                raise ValueError("invalid mission transition: %s -> %s" % (current.value, target.value))
            now = utc_now()
            connection.execute(
                "UPDATE missions SET status = ?, updated_at = ? WHERE mission_id = ?",
                (target.value, now, mission_id),
            )
            self.append_event(
                mission_id,
                "mission_%s" % target.value,
                actor.value,
                {"from": current.value, "to": target.value},
                connection,
            )

    def set_oracle_result(self, mission_id: str, artifact_id: str) -> None:
        with self.connection() as connection:
            connection.execute(
                "UPDATE missions SET oracle_result_artifact_id = ?, updated_at = ? WHERE mission_id = ?",
                (artifact_id, utc_now(), mission_id),
            )

    def add_task(self, task: TaskSlot) -> None:
        now = utc_now()
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO task_slots(
                    task_id, mission_id, title, role, position, output_contract,
                    status, depends_on_json, task_kind, subgoal_id, execution_mode,
                    write_scope, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.task_id,
                    task.mission_id,
                    task.title,
                    task.role.value,
                    task.position,
                    task.output_contract,
                    task.status.value,
                    json.dumps(task.depends_on),
                    task.task_kind,
                    task.subgoal_id,
                    task.execution_mode,
                    task.write_scope,
                    now,
                    now,
                ),
            )
            self.append_event(
                task.mission_id,
                "task_slot_created",
                Role.KING.value,
                task.to_dict(),
                connection,
            )

    def insert_tasks_before(
        self,
        mission_id: str,
        before_task_id: str,
        tasks: Iterable[TaskSlot],
        assignments: Iterable[RoleAssignment],
    ) -> None:
        """Insert runtime-discovered slots while preserving durable order.

        KING may discover a small set of independent read-only subgoals only
        after its model turn. SQLite's unique mission/position constraint
        means the existing tail is moved through temporary negative positions
        before the new slots are inserted.
        """
        task_list = list(tasks)
        assignment_list = list(assignments)
        if not task_list:
            return
        if len(task_list) != len(assignment_list):
            raise ValueError("tasks and assignments must have equal length")
        now = utc_now()
        with self.connection() as connection:
            before = connection.execute(
                "SELECT position FROM task_slots WHERE mission_id = ? AND task_id = ?",
                (mission_id, before_task_id),
            ).fetchone()
            if before is None:
                raise ValueError("unknown insertion point: %s" % before_task_id)
            before_position = int(before["position"])
            tail = connection.execute(
                "SELECT task_id, position FROM task_slots WHERE mission_id = ? AND position >= ? ORDER BY position DESC",
                (mission_id, before_position),
            ).fetchall()
            connection.execute(
                "UPDATE task_slots SET position = -position - 1 WHERE mission_id = ? AND position >= ?",
                (mission_id, before_position),
            )
            for row in tail:
                connection.execute(
                    "UPDATE task_slots SET position = ? WHERE task_id = ?",
                    (int(row["position"]) + len(task_list), row["task_id"]),
                )
            for offset, task in enumerate(task_list):
                position = before_position + offset
                connection.execute(
                    """
                    INSERT INTO task_slots(
                        task_id, mission_id, title, role, position, output_contract,
                        status, depends_on_json, task_kind, subgoal_id, execution_mode,
                        write_scope, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task.task_id,
                        task.mission_id,
                        task.title,
                        task.role.value,
                        position,
                        task.output_contract,
                        task.status.value,
                        json.dumps(task.depends_on),
                        task.task_kind,
                        task.subgoal_id,
                        task.execution_mode,
                        task.write_scope,
                        now,
                        now,
                    ),
                )
                self.append_event(mission_id, "task_slot_created", Role.KING.value, task.to_dict(), connection)
            for assignment in assignment_list:
                route = assignment.route
                connection.execute(
                    """
                    INSERT INTO role_assignments(
                        assignment_id, mission_id, task_id, role, provider, profile,
                        model, effort, invocation_status, reason, assigned_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        assignment.assignment_id,
                        assignment.mission_id,
                        assignment.task_id,
                        route.role.value,
                        route.provider,
                        route.profile,
                        route.model,
                        route.effort,
                        route.invocation_status,
                        route.reason,
                        assignment.assigned_at,
                    ),
                )
                self.append_event(mission_id, "route_planned", Role.KING.value, assignment.to_dict(), connection)
    def set_task_status(self, task_id: str, status: TaskStatus, actor: Role) -> None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT mission_id, status FROM task_slots WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise ValueError("unknown task: %s" % task_id)
            connection.execute(
                "UPDATE task_slots SET status = ?, updated_at = ? WHERE task_id = ?",
                (status.value, utc_now(), task_id),
            )
            self.append_event(
                row["mission_id"],
                "task_%s" % status.value,
                actor.value,
                {"task_id": task_id, "from": row["status"], "to": status.value},
                connection,
            )

    def assign_role(self, assignment: RoleAssignment) -> None:
        route = assignment.route
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO role_assignments(
                    assignment_id, mission_id, task_id, role, provider, profile,
                    model, effort, invocation_status, reason, assigned_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    assignment.assignment_id,
                    assignment.mission_id,
                    assignment.task_id,
                    route.role.value,
                    route.provider,
                    route.profile,
                    route.model,
                    route.effort,
                    route.invocation_status,
                    route.reason,
                    assignment.assigned_at,
                ),
            )
            self.append_event(
                assignment.mission_id,
                "route_planned",
                Role.KING.value,
                assignment.to_dict(),
                connection,
            )

    def start_attempt(self, mission_id: str, task_id: str, executor: str) -> Dict[str, str]:
        attempt_id = "attempt-%s" % uuid.uuid4().hex[:12]
        lease_id = "lease-%s" % uuid.uuid4().hex[:12]
        now = utc_now()
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO attempts(attempt_id, mission_id, task_id, executor, status, started_at) VALUES (?, ?, ?, ?, ?, ?)",
                (attempt_id, mission_id, task_id, executor, AttemptStatus.STARTED.value, now),
            )
            connection.execute(
                "INSERT INTO leases(lease_id, mission_id, task_id, attempt_id, holder, status, acquired_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (lease_id, mission_id, task_id, attempt_id, executor, LeaseStatus.ACTIVE.value, now),
            )
            self.append_event(
                mission_id,
                "attempt_started",
                "RUNTIME",
                {"attempt_id": attempt_id, "task_id": task_id, "executor": executor, "lease_id": lease_id},
                connection,
            )
        return {"attempt_id": attempt_id, "lease_id": lease_id}

    def finish_attempt(
        self,
        mission_id: str,
        attempt_id: str,
        lease_id: str,
        status: AttemptStatus,
        error: Optional[str] = None,
    ) -> None:
        now = utc_now()
        with self.connection() as connection:
            connection.execute(
                "UPDATE attempts SET status = ?, finished_at = ?, error = ? WHERE attempt_id = ?",
                (status.value, now, error, attempt_id),
            )
            connection.execute(
                "UPDATE leases SET status = ?, released_at = ? WHERE lease_id = ?",
                (LeaseStatus.RELEASED.value, now, lease_id),
            )
            self.append_event(
                mission_id,
                "attempt_%s" % status.value,
                "RUNTIME",
                {"attempt_id": attempt_id, "lease_id": lease_id, "error": error},
                connection,
            )

    def add_tool_receipt(
        self,
        mission_id: str,
        task_id: str,
        attempt_id: str,
        tool_name: str,
        status: str,
        observed: Dict[str, Any],
    ) -> str:
        receipt_id = "receipt-%s" % uuid.uuid4().hex[:12]
        now = utc_now()
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO tool_receipts(
                    receipt_id, mission_id, task_id, attempt_id, tool_name,
                    status, observed_json, started_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt_id,
                    mission_id,
                    task_id,
                    attempt_id,
                    tool_name,
                    status,
                    json.dumps(observed, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            self.append_event(
                mission_id,
                "tool_receipt_recorded",
                "RUNTIME",
                {"receipt_id": receipt_id, "task_id": task_id, "tool": tool_name, "status": status},
                connection,
            )
        return receipt_id

    def add_artifact(self, artifact: Artifact) -> None:
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO artifacts(artifact_id, mission_id, task_id, kind, path, sha256, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    artifact.artifact_id,
                    artifact.mission_id,
                    artifact.task_id,
                    artifact.kind,
                    artifact.path,
                    artifact.sha256,
                    artifact.status.value,
                    artifact.created_at,
                ),
            )
            self.append_event(
                artifact.mission_id,
                "artifact_recorded",
                "RUNTIME",
                {
                    "artifact_id": artifact.artifact_id,
                    "task_id": artifact.task_id,
                    "kind": artifact.kind,
                    "path": artifact.path,
                    "sha256": artifact.sha256,
                    "status": artifact.status.value,
                },
                connection,
            )

    def persist_ontology_ledger(
        self,
        mission_id: str,
        *args: Any,
        ledger: Optional[Dict[str, Any]] = None,
        task_id: str = "",
        filename: str = "ontology_ledger.json",
    ) -> Dict[str, Any]:
        """Persist one immutable ontology-ledger revision and read it back.

        The positional compatibility forms are ``(mission_id, ledger)`` and
        ``(mission_id, task_id, ledger)``.  The returned mapping exposes both
        artifact metadata and the validated readback payload so callers cannot
        accidentally continue with only the pre-persist in-memory object.
        """
        if args:
            if len(args) == 1:
                if isinstance(args[0], dict) and ledger is None:
                    ledger = args[0]
                elif not task_id:
                    task_id = str(args[0] or "")
            elif len(args) == 2:
                if task_id or ledger is not None:
                    raise TypeError("ontology ledger task_id/ledger supplied twice")
                task_id = str(args[0] or "")
                ledger = args[1]
            else:
                raise TypeError("persist_ontology_ledger accepts at most task_id and ledger")
        if not isinstance(ledger, dict):
            raise ValueError("ledger is required")
        mission_id = str(mission_id or "").strip()
        if not mission_id:
            raise ValueError("mission_id is required")
        validate_ontology_ledger(ledger, mission_id=mission_id)
        if self.mission(mission_id) is None:
            raise ValueError("unknown mission: %s" % mission_id)
        task_id = str(task_id or "").strip()
        if not task_id:
            with self.connection() as connection:
                task_row = connection.execute(
                    "SELECT task_id FROM task_slots WHERE mission_id = ? ORDER BY position DESC LIMIT 1",
                    (mission_id,),
                ).fetchone()
            task_id = str(task_row["task_id"] or "") if task_row is not None else ""
        if not task_id:
            raise ValueError("task_id is required to persist an ontology ledger")
        filename = Path(str(filename or "ontology_ledger.json")).name
        if filename in {"", ".", ".."} or not filename.endswith(".json"):
            raise ValueError("ontology ledger filename must be a JSON filename")

        encoded = json.dumps(ledger, ensure_ascii=False, indent=2) + "\n"
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        revision = int(ledger["revision"])
        graph_id = str(ledger["goal_graph_id"])
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM artifacts WHERE mission_id = ? AND kind = ? ORDER BY created_at DESC, rowid DESC",
                (mission_id, ONTOLOGY_LEDGER_KIND),
            ).fetchall()
        versioned_filename = "ontology_ledger_r%04d.json" % revision
        for row in rows:
            if str(row["status"] or "") not in {
                ArtifactStatus.CANDIDATE.value,
                ArtifactStatus.ACCEPTED.value,
            }:
                if Path(str(row["path"])).name in {filename, versioned_filename}:
                    raise RuntimeError("ontology ledger artifact is not in a readable state")
                continue
            try:
                existing = json.loads(Path(str(row["path"])).read_text(encoding="utf-8"))
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                if Path(str(row["path"])).name in {filename, versioned_filename}:
                    raise RuntimeError("existing ontology ledger revision is unreadable")
                continue
            try:
                same_identity = (
                    str(existing.get("mission_id") or "") == mission_id
                    and str(existing.get("goal_graph_id") or "") == graph_id
                    and int(existing.get("revision") or 0) == revision
                )
            except (TypeError, ValueError):
                same_identity = False
            if not same_identity:
                continue
            try:
                validate_ontology_ledger(
                    existing,
                    mission_id=mission_id,
                    goal_graph_id=graph_id,
                    revision=revision,
                )
            except (TypeError, ValueError) as exc:
                raise RuntimeError("existing ontology ledger revision is invalid") from exc
            if str(row["sha256"]) != digest:
                raise ValueError(
                    "ontology ledger revision already exists with a different payload: %s/%s/%s"
                    % (mission_id, graph_id, revision)
                )
            artifact = dict(row)
            readback = self.get_latest_ontology_ledger(
                mission_id,
                goal_graph_id=graph_id,
                revision=revision,
            )
            if readback is None:
                raise RuntimeError("ontology ledger idempotent readback failed")
            return {
                **artifact,
                "artifact": artifact,
                "ledger": readback,
                "readback": readback,
                "idempotent": True,
            }

        directory = self.artifacts_dir / mission_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / filename
        if path.exists():
            # Preserve an older revision's path.  The first revision retains
            # the stable public filename; later revisions get a deterministic
            # suffix rather than invalidating an earlier artifact row.
            filename = "ontology_ledger_r%04d.json" % revision
            path = directory / filename
        # Keep the digest byte-exact across Windows newline translation.  The
        # artifact contract hashes the bytes that ORACLE reads back.
        with path.open("w", encoding="utf-8", newline="") as handle:
            handle.write(encoded)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        artifact = Artifact(
            artifact_id=new_id("artifact"),
            mission_id=mission_id,
            task_id=task_id,
            kind=ONTOLOGY_LEDGER_KIND,
            path=str(path),
            sha256=digest,
            status=ArtifactStatus.CANDIDATE,
        )
        self.add_artifact(artifact)
        readback = self.get_latest_ontology_ledger(
            mission_id,
            goal_graph_id=graph_id,
            revision=revision,
        )
        if readback is None:
            raise RuntimeError("ontology ledger persisted but readback validation failed")
        artifact_dict = {
            "artifact_id": artifact.artifact_id,
            "mission_id": artifact.mission_id,
            "task_id": artifact.task_id,
            "kind": artifact.kind,
            "path": artifact.path,
            "sha256": artifact.sha256,
            "status": artifact.status.value,
            "created_at": artifact.created_at,
        }
        return {
            **artifact_dict,
            "artifact": artifact_dict,
            "ledger": readback,
            "readback": readback,
            "idempotent": False,
        }

    def get_latest_ontology_ledger(
        self,
        mission_id: str,
        *,
        goal_graph_id: str = "",
        revision: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """Return only a hash- and identity-validated persisted ledger."""
        mission_id = str(mission_id or "").strip()
        if not mission_id:
            return None
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM artifacts WHERE mission_id = ? AND kind = ? ORDER BY created_at DESC, rowid DESC",
                (mission_id, ONTOLOGY_LEDGER_KIND),
            ).fetchall()
        expected_graph = str(goal_graph_id or "").strip()
        expected_revision = int(revision) if revision is not None else None
        for index, row in enumerate(rows):
            if str(row["status"] or "") not in {
                ArtifactStatus.CANDIDATE.value,
                ArtifactStatus.ACCEPTED.value,
            }:
                if index == 0 and expected_revision is None:
                    return None
                continue
            try:
                path = Path(str(row["path"]))
                payload = json.loads(path.read_text(encoding="utf-8"))
                validate_ontology_ledger(
                    payload,
                    mission_id=mission_id,
                    goal_graph_id=expected_graph,
                    revision=expected_revision,
                )
                actual_digest = hashlib.sha256(path.read_bytes()).hexdigest()
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                # Without an explicit revision, never fall back to an older
                # ledger when the newest durable row is unreadable.
                if index == 0 and expected_revision is None:
                    return None
                continue
            if actual_digest != str(row["sha256"]):
                if index == 0 and expected_revision is None:
                    return None
                continue
            result = dict(payload)
            result["_artifact"] = {
                "artifact_id": row["artifact_id"],
                "mission_id": row["mission_id"],
                "task_id": row["task_id"],
                "kind": row["kind"],
                "path": row["path"],
                "sha256": row["sha256"],
                "status": row["status"],
                "created_at": row["created_at"],
            }
            result["readback"] = {
                "validated": True,
                "artifact_id": row["artifact_id"],
                "sha256": row["sha256"],
                "mission_id": mission_id,
                "goal_graph_id": result["goal_graph_id"],
                "revision": int(result["revision"]),
            }
            return result
        return None

    def set_artifact_status(self, artifact_id: str, status: ArtifactStatus, actor: Role) -> None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT mission_id, status FROM artifacts WHERE artifact_id = ?", (artifact_id,)
            ).fetchone()
            if row is None:
                raise ValueError("unknown artifact: %s" % artifact_id)
            connection.execute(
                "UPDATE artifacts SET status = ? WHERE artifact_id = ?",
                (status.value, artifact_id),
            )
            self.append_event(
                row["mission_id"],
                "artifact_%s" % status.value,
                actor.value,
                {"artifact_id": artifact_id, "from": row["status"], "to": status.value},
                connection,
            )

    def add_evidence(self, evidence: EvidenceRef) -> None:
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO evidence_refs(evidence_id, mission_id, artifact_id, source_type, source_uri, digest, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    evidence.evidence_id,
                    evidence.mission_id,
                    evidence.artifact_id,
                    evidence.source_type,
                    evidence.source_uri,
                    evidence.digest,
                    evidence.created_at,
                ),
            )

    def add_approval(self, approval: Approval) -> None:
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO approvals(approval_id, mission_id, action, decision, decided_by, reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    approval.approval_id,
                    approval.mission_id,
                    approval.action,
                    approval.decision,
                    approval.decided_by,
                    approval.reason,
                    approval.created_at,
                ),
            )
            self.append_event(
                approval.mission_id,
                "approval_recorded",
                approval.decided_by,
                {
                    "approval_id": approval.approval_id,
                    "action": approval.action,
                    "decision": approval.decision,
                    "reason": approval.reason,
                },
                connection,
            )

    def add_checkpoint(self, checkpoint: Checkpoint) -> None:
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO checkpoints(checkpoint_id, mission_id, sequence, state_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (
                    checkpoint.checkpoint_id,
                    checkpoint.mission_id,
                    checkpoint.sequence,
                    json.dumps(checkpoint.state, ensure_ascii=False),
                    checkpoint.created_at,
                ),
            )
            self.append_event(
                checkpoint.mission_id,
                "checkpoint_saved",
                "RUNTIME",
                {"checkpoint_id": checkpoint.checkpoint_id, "sequence": checkpoint.sequence},
                connection,
            )

    def set_budget(self, budget: Budget) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO budgets(
                    mission_id, currency, token_limit, tokens_observed,
                    cost_observed, observation_source, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(mission_id) DO UPDATE SET
                    currency = excluded.currency,
                    token_limit = excluded.token_limit,
                    tokens_observed = excluded.tokens_observed,
                    cost_observed = excluded.cost_observed,
                    observation_source = excluded.observation_source,
                    updated_at = excluded.updated_at
                """,
                (
                    budget.mission_id,
                    budget.currency,
                    budget.token_limit,
                    budget.tokens_observed,
                    budget.cost_observed,
                    budget.observation_source,
                    budget.updated_at,
                ),
            )

    def mission(self, mission_id: str) -> Optional[Dict[str, Any]]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM missions WHERE mission_id = ?", (mission_id,)
            ).fetchone()
        return self._mission_row(row) if row is not None else None

    def latest_mission(self) -> Optional[Dict[str, Any]]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM missions ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        return self._mission_row(row) if row is not None else None

    def list_missions(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM missions ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 100)),)
            ).fetchall()
        return [self._mission_row(row) for row in rows]

    def recent_missions(self, limit: int = 20, session_id: str = "") -> List[Dict[str, Any]]:
        """Return bounded mission history ordered by most recent durable activity."""
        bounded = max(1, min(int(limit), 100))
        with self.connection() as connection:
            if session_id:
                rows = connection.execute(
                    "SELECT * FROM missions WHERE session_id = ? ORDER BY updated_at DESC LIMIT ?",
                    (session_id, bounded),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM missions ORDER BY updated_at DESC LIMIT ?",
                    (bounded,),
                ).fetchall()
        return [self._mission_row(row) for row in rows]

    def inspect(self, mission_id: str) -> Dict[str, Any]:
        mission = self.mission(mission_id)
        if mission is None:
            raise ValueError("unknown mission: %s" % mission_id)
        with self.connection() as connection:
            tasks = [dict(row) for row in connection.execute(
                "SELECT * FROM task_slots WHERE mission_id = ? ORDER BY position", (mission_id,)
            ).fetchall()]
            assignments = [dict(row) for row in connection.execute(
                "SELECT * FROM role_assignments WHERE mission_id = ? ORDER BY assigned_at", (mission_id,)
            ).fetchall()]
            attempts = [dict(row) for row in connection.execute(
                "SELECT * FROM attempts WHERE mission_id = ? ORDER BY started_at", (mission_id,)
            ).fetchall()]
            leases = [dict(row) for row in connection.execute(
                "SELECT * FROM leases WHERE mission_id = ? ORDER BY acquired_at", (mission_id,)
            ).fetchall()]
            artifacts = [dict(row) for row in connection.execute(
                "SELECT * FROM artifacts WHERE mission_id = ? ORDER BY created_at", (mission_id,)
            ).fetchall()]
            evidence = [dict(row) for row in connection.execute(
                "SELECT * FROM evidence_refs WHERE mission_id = ? ORDER BY created_at", (mission_id,)
            ).fetchall()]
            approvals = [dict(row) for row in connection.execute(
                "SELECT * FROM approvals WHERE mission_id = ? ORDER BY created_at", (mission_id,)
            ).fetchall()]
            checkpoints = [dict(row) for row in connection.execute(
                "SELECT * FROM checkpoints WHERE mission_id = ? ORDER BY sequence", (mission_id,)
            ).fetchall()]
            receipts = [dict(row) for row in connection.execute(
                "SELECT * FROM tool_receipts WHERE mission_id = ? ORDER BY started_at", (mission_id,)
            ).fetchall()]
            budget_row = connection.execute(
                "SELECT * FROM budgets WHERE mission_id = ?", (mission_id,)
            ).fetchone()
        for task in tasks:
            task["depends_on"] = json.loads(task.pop("depends_on_json"))
        for checkpoint in checkpoints:
            checkpoint["state"] = json.loads(checkpoint.pop("state_json"))
        for receipt in receipts:
            receipt["observed"] = json.loads(receipt.pop("observed_json"))
        def artifact_json(kind: str) -> Optional[Dict[str, Any]]:
            artifact = next((row for row in reversed(artifacts) if row.get("kind") == kind), None)
            if not artifact:
                return None
            try:
                value = json.loads(Path(str(artifact["path"])).read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                return None
            return value if isinstance(value, dict) else None

        goal_plan = artifact_json("goal_plan")
        goal_graph = artifact_json("goal_graph")
        kinetic_workflow = artifact_json("kinetic_workflow")
        ontology_execution_contract = artifact_json("ontology_execution_contract")
        ontology_ledger = self.get_latest_ontology_ledger(
            mission_id,
            goal_graph_id=str((goal_graph or {}).get("graph_id") or ""),
        )
        king_plan = artifact_json("king_plan")
        subgoal_plan = artifact_json("subgoal_plan")
        return {
            "mission": mission,
            "goal_plan": goal_plan,
            "goal_graph": goal_graph,
            "kinetic_workflow": kinetic_workflow,
            "ontology_execution_contract": ontology_execution_contract,
            "ontology_ledger": ontology_ledger,
            "king_plan": king_plan,
            "subgoal_plan": subgoal_plan,
            "tasks": tasks,
            "assignments": assignments,
            "attempts": attempts,
            "leases": leases,
            "artifacts": artifacts,
            "evidence": evidence,
            "approvals": approvals,
            "checkpoints": checkpoints,
            "budget": dict(budget_row) if budget_row is not None else None,
            "tool_receipts": receipts,
        }

    def events(self, mission_id: Optional[str] = None, limit: int = 500) -> List[EventRecord]:
        query = "SELECT * FROM events"
        params: List[Any] = []
        if mission_id is not None:
            query += " WHERE mission_id = ?"
            params.append(mission_id)
        query += " ORDER BY event_id ASC LIMIT ?"
        params.append(max(1, min(limit, 5000)))
        with self.connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [
            EventRecord(
                event_id=row["event_id"],
                mission_id=row["mission_id"],
                event_type=row["event_type"],
                actor=row["actor"],
                payload=json.loads(row["payload_json"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    @staticmethod
    def _mission_row(row: sqlite3.Row) -> Dict[str, Any]:
        result = dict(row)
        result["acceptance"] = json.loads(result.pop("acceptance_json"))
        return result


def new_id(prefix: str) -> str:
    return "%s-%s" % (prefix, uuid.uuid4().hex[:12])
