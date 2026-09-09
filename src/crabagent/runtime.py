from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .goal import GoalPlan, classify_goal, normalize_execution_scope
from .goal_graph import compile_goal_graph
from .kinetic_workflow import compile_kinetic_workflow
from .inverter import SolteluInverter
from .ontology_contract import compile_ontology_execution_contract
from .opencrab import opencrab_is_configured
from .models import (
    Approval,
    Artifact,
    ArtifactStatus,
    AttemptStatus,
    Budget,
    Checkpoint,
    EvidenceRef,
    MissionContract,
    MissionStatus,
    Role,
    RoleAssignment,
    TaskSlot,
    TaskStatus,
    WorkerPolicy,
)
from .store import ColonyStore, new_id


DEFAULT_ACCEPTANCE = [
    "Every task has a persisted attempt and released lease.",
    "Every produced artifact has a SHA-256 digest and tool receipt.",
    "ORACLE verifies the final artifact before mission completion.",
    "No external model or crawler call is claimed without an observed receipt.",
]


class RuntimeService:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()
        self.store = ColonyStore(self.workspace)
        self.inverter = SolteluInverter()

    def initialize(self) -> Dict[str, str]:
        return self.store.initialize()

    def plan_mission(
        self,
        objective: str,
        max_workers: int = 3,
        worker_policy: str = WorkerPolicy.FIXED.value,
        token_budget: Optional[int] = None,
        session_id: Optional[str] = None,
        adaptive: bool = False,
        execution_scope: Optional[str] = None,
        forced: Optional[str] = None,
        retrieval_mode: str = "auto",
    ) -> Dict[str, Any]:
        self.initialize()
        normalized_execution_scope = normalize_execution_scope(execution_scope)
        mission_id = new_id("mission")
        selected_pack_count = 0
        selected_project_count = 0
        if session_id:
            context = self.store.ontology_context(session_id)
            selected_pack_count = len(context.get("package_ids") or [])
            selected_project_count = len(context.get("project_ids") or [])
            session = self.store.session(session_id) or {}
            knowledge_available = (
                opencrab_is_configured(self.workspace, str(session.get("mcp_policy") or "auto"))
                if retrieval_mode == "auto" else False
            )
        else:
            knowledge_available = False
        goal_plan = classify_goal(
            objective,
            selected_pack_count=selected_pack_count,
            selected_project_count=selected_project_count,
            knowledge_available=knowledge_available,
            execution_scope=normalized_execution_scope,
            forced=forced,
            retrieval_mode=retrieval_mode,
        ) if adaptive else None
        effective_token_budget = token_budget
        if effective_token_budget is None and goal_plan is not None:
            effective_token_budget = goal_plan.token_budget
        contract = MissionContract(
            mission_id=mission_id,
            objective=objective.strip(),
            acceptance=list(DEFAULT_ACCEPTANCE),
            workspace=str(self.workspace),
            risk=(goal_plan.complexity if goal_plan is not None else "local_demo" if token_budget == 0 else "bounded"),
            max_attempts=2,
            max_workers=max(1, min(max_workers, 8)),
            token_budget=effective_token_budget,
            worker_policy=worker_policy if worker_policy in {item.value for item in WorkerPolicy} else WorkerPolicy.FIXED.value,
            execution_scope=normalized_execution_scope,
        )
        self.store.create_mission(contract, session_id=session_id)

        task_specs: List[Tuple[Role, str, str]] = self._task_specs(goal_plan)
        previous: List[str] = []
        tasks: List[TaskSlot] = []
        for position, (role, title, output_contract) in enumerate(task_specs, start=1):
            task = TaskSlot(
                task_id=new_id("task"),
                mission_id=mission_id,
                title=title,
                role=role,
                position=position,
                output_contract=output_contract,
                status=TaskStatus.PENDING,
                depends_on=list(previous[-1:]),
                write_scope=(
                    "task_contract_only"
                    if role is Role.WORKER and goal_plan is not None and goal_plan.requires_write
                    else "none"
                ),
            )
            self.store.add_task(task)
            route_kind = "ontology_judgment" if role is Role.SOLDIER else (
                "bounded_ontology"
                if role is Role.QUEEN and goal_plan is not None and goal_plan.complexity == "bounded"
                else "default"
            )
            route = self.inverter.plan(role, route_kind)
            if goal_plan is not None and self._uses_local_gate(goal_plan, role):
                route = self.inverter.local(role, self._local_gate_reason(goal_plan, role))
            self.store.assign_role(
                RoleAssignment(
                    assignment_id=new_id("assignment"),
                    mission_id=mission_id,
                    task_id=task.task_id,
                    route=route,
                )
            )
            tasks.append(task)
            previous.append(task.task_id)

        goal_graph: Optional[Dict[str, Any]] = None
        goal_plan_payload: Optional[Dict[str, Any]] = None
        ontology_contract: Optional[Dict[str, Any]] = None
        kinetic_workflow: Optional[Dict[str, Any]] = None
        if goal_plan is not None:
            king_task = next(task for task in tasks if task.role is Role.KING)
            goal_plan_payload = goal_plan.to_dict()
            goal_graph = compile_goal_graph(goal_plan_payload)
            goal_plan_payload["goal_graph_id"] = goal_graph["graph_id"]
            self._write_artifact(
                mission_id=mission_id,
                task_id=king_task.task_id,
                filename="goal_plan.json",
                content=json.dumps(goal_plan_payload, ensure_ascii=False, indent=2) + "\n",
                kind="goal_plan",
            )
            self._write_artifact(
                mission_id=mission_id,
                task_id=king_task.task_id,
                filename="goal_graph.json",
                content=json.dumps(goal_graph, ensure_ascii=False, indent=2) + "\n",
                kind="goal_graph",
            )
            kinetic_workflow = compile_kinetic_workflow(goal_plan_payload, goal_graph)
            self._write_artifact(
                mission_id=mission_id,
                task_id=king_task.task_id,
                filename="kinetic_workflow.json",
                content=json.dumps(kinetic_workflow, ensure_ascii=False, indent=2) + "\n",
                kind="kinetic_workflow",
            )
            if goal_plan_payload.get("ontology_required"):
                ontology_contract = compile_ontology_execution_contract(
                    goal_plan_payload,
                    goal_graph,
                    mission_id=mission_id,
                    revision=1,
                )
                self._write_artifact(
                    mission_id=mission_id,
                    task_id=king_task.task_id,
                    filename="ontology_execution_contract.json",
                    content=json.dumps(ontology_contract, ensure_ascii=False, indent=2) + "\n",
                    kind="ontology_execution_contract",
                )
            self.store.append_event(mission_id, "goal_plan_compiled", Role.KING.value, goal_plan_payload)
            self.store.append_event(
                mission_id,
                "goal_graph_compiled",
                Role.KING.value,
                {"graph_id": goal_graph["graph_id"], "node_count": len(goal_graph["nodes"]), "edge_count": len(goal_graph["edges"])},
            )

        self.store.set_budget(
            Budget(
                mission_id=mission_id,
                currency="USD",
                token_limit=effective_token_budget,
                tokens_observed=None,
                cost_observed=None,
                observation_source="not_observed",
            )
        )
        self.store.add_approval(
            Approval(
                approval_id=new_id("approval"),
                mission_id=mission_id,
                action="write_local_demo_artifacts",
                decision="approved",
                decided_by="BUILT_IN_POLICY",
                reason="Writes are isolated to .crabagent/artifacts in the selected workspace.",
            )
        )
        self.store.transition_mission(mission_id, MissionStatus.PLANNED, Role.KING)
        self.store.add_checkpoint(
            Checkpoint(
                checkpoint_id=new_id("checkpoint"),
                mission_id=mission_id,
                sequence=0,
                state={
                    "objective": objective,
                    "status": MissionStatus.PLANNED.value,
                    "completed_tasks": [],
                    "next_task_id": tasks[0].task_id,
                    **({
                        "goal_plan": goal_plan_payload,
                        "goal_graph_id": goal_graph["graph_id"],
                        "kinetic_workflow_schema": kinetic_workflow.get("schema") if kinetic_workflow else None,
                    } if goal_plan_payload and goal_graph else {}),
                },
            )
        )
        snapshot = self.store.inspect(mission_id)
        if goal_plan_payload is not None:
            snapshot["goal_plan"] = goal_plan_payload
        if goal_graph is not None:
            snapshot["goal_graph"] = goal_graph
        if ontology_contract is not None:
            snapshot["ontology_execution_contract"] = ontology_contract
        if kinetic_workflow is not None:
            snapshot["kinetic_workflow"] = kinetic_workflow
        return snapshot

    @staticmethod
    def _task_specs(goal_plan: Optional[GoalPlan]) -> List[Tuple[Role, str, str]]:
        if goal_plan is None:
            return [
                (Role.KING, "Design mission workflow", "mission_contract.json"),
                (Role.QUEEN, "Provision ontology context", "ontology_context.json"),
                (Role.SOLDIER, "Run waste and scope patrol", "soldier_report.json"),
                (Role.WORKER, "Execute bounded work", "worker_result.md"),
                (Role.ORACLE, "Verify and publish conclusion", "oracle_result.md"),
            ]
        filenames = {
            Role.KING: ("Compile goal and acceptance gates", "mission_contract.json"),
            Role.QUEEN: ("Select the minimum ontology path", "ontology_context.json"),
            Role.SOLDIER: ("Patrol scope, evidence and waste", "soldier_report.json"),
            Role.WORKER: ("Execute the bounded deliverable", "worker_result.md"),
            Role.ORACLE: ("Verify the evidence-backed conclusion", "oracle_result.md"),
        }
        return [(role, *filenames[role]) for role_name in goal_plan.stages for role in [Role(role_name)]]

    @staticmethod
    def _uses_local_gate(goal_plan: GoalPlan, role: Role) -> bool:
        if goal_plan.full_pipeline:
            return False
        if role is Role.KING:
            return not goal_plan.requires_model_planning
        if role is Role.SOLDIER:
            return not goal_plan.external_scouting
        if role is Role.QUEEN:
            return not goal_plan.requires_model_queen
        if role is Role.ORACLE:
            return not goal_plan.requires_oracle_model
        return False

    @staticmethod
    def _local_gate_reason(goal_plan: GoalPlan, role: Role) -> str:
        if role is Role.KING:
            return "clear goal compiled locally before any model turn"
        if role is Role.SOLDIER:
            return "local receipt and scope patrol; no external scouting requested"
        if role is Role.QUEEN:
            return "local evidence projection from the observed OpenCrab receipt; semantic synthesis was not requested"
        if role is Role.ORACLE:
            return "bounded local artifact gate is sufficient; no semantic or high-risk conclusion"
        return "deterministic local gate"

    def run_demo(self, objective: str, session_id: Optional[str] = None) -> Dict[str, Any]:
        planned = self.plan_mission(
            objective,
            max_workers=1,
            worker_policy=WorkerPolicy.FIXED.value,
            token_budget=0,
            session_id=session_id,
            adaptive=False,
        )
        mission_id = planned["mission"]["mission_id"]
        if session_id:
            self.store.update_session(
                session_id,
                status="running",
                active_mission_id=mission_id,
            )
        self.store.transition_mission(mission_id, MissionStatus.RUNNING, Role.KING)
        tasks = self.store.inspect(mission_id)["tasks"]
        completed: List[str] = []
        artifact_ids: List[str] = []

        for sequence, task in enumerate(tasks, start=1):
            role = Role(task["role"])
            if role is Role.ORACLE:
                self.store.transition_mission(mission_id, MissionStatus.VERIFYING, Role.ORACLE)
            self.store.set_task_status(task["task_id"], TaskStatus.RUNNING, role)
            attempt = self.store.start_attempt(
                mission_id=mission_id,
                task_id=task["task_id"],
                executor="deterministic_demo",
            )
            content = self._demo_content(role, mission_id, objective, artifact_ids)
            artifact = self._write_artifact(
                mission_id=mission_id,
                task_id=task["task_id"],
                filename=task["output_contract"],
                content=content,
                kind="oracle_result" if role is Role.ORACLE else "demo_output",
            )
            artifact_ids.append(artifact.artifact_id)
            self.store.add_tool_receipt(
                mission_id=mission_id,
                task_id=task["task_id"],
                attempt_id=attempt["attempt_id"],
                tool_name="crab.demo.%s" % role.value.lower(),
                status="success",
                observed={
                    "executor": "deterministic_demo",
                    "artifact_id": artifact.artifact_id,
                    "sha256": artifact.sha256,
                    "external_call": False,
                },
            )
            if role is Role.QUEEN:
                self.store.add_evidence(
                    EvidenceRef(
                        evidence_id=new_id("evidence"),
                        mission_id=mission_id,
                        artifact_id=artifact.artifact_id,
                        source_type="built_in_product_contract",
                        source_uri="crabagent://roles/v1",
                        digest=artifact.sha256,
                    )
                )
            self.store.finish_attempt(
                mission_id,
                attempt["attempt_id"],
                attempt["lease_id"],
                AttemptStatus.SUCCEEDED,
            )
            final_task_status = TaskStatus.VERIFIED if role is Role.ORACLE else TaskStatus.COMPLETED
            self.store.set_task_status(task["task_id"], final_task_status, role)
            completed.append(task["task_id"])
            self.store.add_checkpoint(
                Checkpoint(
                    checkpoint_id=new_id("checkpoint"),
                    mission_id=mission_id,
                    sequence=sequence,
                    state={
                        "objective": objective,
                        "status": MissionStatus.VERIFYING.value if role is Role.ORACLE else MissionStatus.RUNNING.value,
                        "completed_tasks": list(completed),
                        "last_artifact_id": artifact.artifact_id,
                    },
                )
            )
            if role is Role.ORACLE:
                for artifact_id in artifact_ids:
                    self.store.set_artifact_status(artifact_id, ArtifactStatus.ACCEPTED, Role.ORACLE)
                self.store.set_oracle_result(mission_id, artifact.artifact_id)

        self.store.set_budget(
            Budget(
                mission_id=mission_id,
                currency="USD",
                token_limit=0,
                tokens_observed=0,
                cost_observed=0.0,
                observation_source="deterministic_demo_meter",
            )
        )
        self.store.transition_mission(mission_id, MissionStatus.COMPLETED, Role.ORACLE)
        if session_id:
            self.store.update_session(
                session_id,
                status="ready",
                active_mission_id=None,
            )
        return self.store.inspect(mission_id)

    def status(self) -> Dict[str, Any]:
        latest = self.store.latest_mission()
        session = self.store.latest_session()
        latest_message = self.store.latest_message(str(session["session_id"])) if session else None
        return {
            "workspace": str(self.workspace),
            "database": str(self.store.database),
            "latest_mission": latest,
            "latest_session": session,
            "latest_message": latest_message,
            "mission_count": len(self.store.list_missions(limit=100)),
        }

    def _write_artifact(
        self,
        mission_id: str,
        task_id: str,
        filename: str,
        content: str,
        kind: str,
    ) -> Artifact:
        directory = self.store.artifacts_dir / mission_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / filename
        encoded = content.encode("utf-8")
        path.write_bytes(encoded)
        digest = hashlib.sha256(encoded).hexdigest()
        artifact = Artifact(
            artifact_id=new_id("artifact"),
            mission_id=mission_id,
            task_id=task_id,
            kind=kind,
            path=str(path),
            sha256=digest,
            status=ArtifactStatus.CANDIDATE,
        )
        self.store.add_artifact(artifact)
        return artifact

    @staticmethod
    def _demo_content(role: Role, mission_id: str, objective: str, prior_artifacts: List[str]) -> str:
        if role is Role.KING:
            return json.dumps(
                {
                    "mission_id": mission_id,
                    "objective": objective,
                    "workflow": ["KING", "QUEEN", "SOLDIER", "WORKER", "ORACLE"],
                    "completion": "oracle_verified",
                    "executor": "deterministic_demo",
                },
                indent=2,
                ensure_ascii=False,
            ) + "\n"
        if role is Role.QUEEN:
            return json.dumps(
                {
                    "ontology_contract": "crabagent://roles/v1",
                    "knowledge_policy": "evidence_before_claim",
                    "context_provisioned": True,
                    "external_opencrab_query": False,
                },
                indent=2,
                ensure_ascii=False,
            ) + "\n"
        if role is Role.SOLDIER:
            return json.dumps(
                {
                    "waste_loops_detected": 0,
                    "quarantined_artifacts": [],
                    "external_scouting": "disabled_in_demo",
                    "decision": "continue",
                },
                indent=2,
                ensure_ascii=False,
            ) + "\n"
        if role is Role.WORKER:
            return (
                "# Worker result\n\n"
                "Objective: %s\n\n"
                "A bounded local artifact was produced by the deterministic demo executor.\n"
                "No model, crawler, network service, or external tool was invoked.\n" % objective
            )
        return (
            "# Oracle result\n\n"
            "Mission `%s` passed the deterministic vertical-slice gate.\n\n"
            "- Prior artifacts observed: %d\n"
            "- Attempts persisted and leases released: yes\n"
            "- Tool receipts persisted: yes\n"
            "- External model calls: none\n"
            "- Verdict: accepted\n" % (mission_id, len(prior_artifacts))
        )
