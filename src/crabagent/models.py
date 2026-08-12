from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StrEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class Role(StrEnum):
    KING = "KING"
    QUEEN = "QUEEN"
    WORKER = "WORKER"
    SOLDIER = "SOLDIER"
    ORACLE = "ORACLE"


class WorkerPolicy(StrEnum):
    AUTO = "auto"
    FIXED = "fixed"


AUTO_WORKER_CAP = 8


def worker_capacity(policy: str, configured_limit: int, runnable_slots: int) -> int:
    """Resolve worker capacity from runnable work, never from pack count."""
    slots = max(0, int(runnable_slots))
    if slots == 0:
        return 0
    if str(policy) == WorkerPolicy.AUTO.value:
        return min(AUTO_WORKER_CAP, slots)
    return min(max(1, min(int(configured_limit), AUTO_WORKER_CAP)), slots)


class MissionStatus(StrEnum):
    CREATED = "created"
    PLANNED = "planned"
    RUNNING = "running"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"
    VERIFIED = "verified"
    FAILED = "failed"
    STOPPED = "stopped"


class AttemptStatus(StrEnum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    STOPPED = "stopped"


class LeaseStatus(StrEnum):
    ACTIVE = "active"
    RELEASED = "released"
    EXPIRED = "expired"


class ArtifactStatus(StrEnum):
    CANDIDATE = "candidate"
    ACCEPTED = "accepted"
    QUARANTINED = "quarantined"
    REJECTED = "rejected"


@dataclass(frozen=True)
class ModelRoute:
    role: Role
    provider: str
    profile: str
    model: str
    effort: str
    invocation_status: str = "planned"
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MissionContract:
    mission_id: str
    objective: str
    acceptance: List[str]
    workspace: str
    risk: str
    max_attempts: int
    max_workers: int
    token_budget: Optional[int]
    worker_policy: str = WorkerPolicy.FIXED.value
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TaskSlot:
    task_id: str
    mission_id: str
    title: str
    role: Role
    position: int
    output_contract: str
    status: TaskStatus = TaskStatus.PENDING
    depends_on: List[str] = field(default_factory=list)
    task_kind: str = "stage"
    subgoal_id: str = ""
    execution_mode: str = "serial"
    write_scope: str = "none"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RoleAssignment:
    assignment_id: str
    mission_id: str
    task_id: str
    route: ModelRoute
    assigned_at: str = field(default_factory=utc_now)

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["route"] = self.route.to_dict()
        return payload


@dataclass(frozen=True)
class Attempt:
    attempt_id: str
    mission_id: str
    task_id: str
    executor: str
    status: AttemptStatus
    started_at: str = field(default_factory=utc_now)
    finished_at: Optional[str] = None
    error: Optional[str] = None


@dataclass(frozen=True)
class Lease:
    lease_id: str
    mission_id: str
    task_id: str
    attempt_id: str
    holder: str
    status: LeaseStatus
    acquired_at: str = field(default_factory=utc_now)
    released_at: Optional[str] = None


@dataclass(frozen=True)
class EventRecord:
    event_id: int
    mission_id: Optional[str]
    event_type: str
    actor: str
    payload: Dict[str, Any]
    created_at: str


@dataclass(frozen=True)
class Artifact:
    artifact_id: str
    mission_id: str
    task_id: str
    kind: str
    path: str
    sha256: str
    status: ArtifactStatus
    created_at: str = field(default_factory=utc_now)


@dataclass(frozen=True)
class EvidenceRef:
    evidence_id: str
    mission_id: str
    artifact_id: str
    source_type: str
    source_uri: str
    digest: str
    created_at: str = field(default_factory=utc_now)


@dataclass(frozen=True)
class Approval:
    approval_id: str
    mission_id: str
    action: str
    decision: str
    decided_by: str
    reason: str
    created_at: str = field(default_factory=utc_now)


@dataclass(frozen=True)
class Checkpoint:
    checkpoint_id: str
    mission_id: str
    sequence: int
    state: Dict[str, Any]
    created_at: str = field(default_factory=utc_now)


@dataclass(frozen=True)
class Budget:
    mission_id: str
    currency: str
    token_limit: Optional[int]
    tokens_observed: Optional[int]
    cost_observed: Optional[float]
    observation_source: str
    updated_at: str = field(default_factory=utc_now)
