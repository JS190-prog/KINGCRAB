import json
import threading

import pytest

from crabagent.colony import ColonyExecutor
from crabagent.goal import classify_goal, workspace_writes_forbidden
from crabagent.models import Approval
from crabagent.runtime import RuntimeService
from crabagent.store import new_id


NO_WRITE_OBJECTIVE = (
    "KINGCRAB 취소 전용 수용시험. KING→QUEEN→SOLDIER→WORKER→ORACLE 5역할 계획을 유지하되 "
    "현재 호스트 모델의 첫 KING 결과를 기다린다. 점검자는 어떤 역할 결과도 제출하지 않고 이번에 생성된 이 미션만 cancel 1회로 종료한다. "
    "이 미션에는 파일·디렉터리·설정 생성·수정·삭제 권한이 전혀 없고, connector 실행·OpenCrab 조회·웹 조회·프로젝트 조회·인제스트·외부 ledger 기록도 금지한다. "
    "조회 금지 문장은 local_only scope 승인이 아니며 execution_scope는 생략한다. 기존 업무 미션과 파일을 보존한다."
)


@pytest.mark.parametrize("objective", [
    NO_WRITE_OBJECTIVE,
    "Create a durable mission. No file writes. Keep five roles.",
    "Create a mission; do not create any files.",
    "Create a mission; no permission to modify files.",
    "파일 쓰기를 전면 금지한다. 미션을 생성한다.",
    "새 미션 생성. requires_write=false; full_pipeline=true.",
    "Create a file; read_only=true.",
])
def test_explicit_no_write_keeps_five_roles_without_granting_write(objective, tmp_path):
    assert workspace_writes_forbidden(objective)
    plan = classify_goal(objective, forced="full", retrieval_mode="none")
    assert plan.stages == ["KING", "QUEEN", "SOLDIER", "WORKER", "ORACLE"]
    assert plan.requires_write is False
    assert plan.outcome_type != "workspace_change"
    result = RuntimeService(tmp_path).plan_mission(objective, adaptive=True, forced="full", retrieval_mode="none")
    assert result["approvals"] == []
    assert all(task["write_scope"] == "none" for task in result["tasks"])


@pytest.mark.parametrize("objective", [
    "Create one file. Do not modify existing files. No external changes.",
    "WORKER가 파일 한 개를 생성한다. 기존 파일 수정·삭제는 금지한다. OpenCrab 인제스트도 금지한다.",
    "WORKER가 파일 한 개를 생성하고 OpenCrab 수정·삭제는 금지한다.",
    "파일을 생성한다. 다른 파일의 쓰기는 금지한다.",
])
def test_narrow_external_or_existing_file_prohibition_preserves_bounded_local_write(objective, tmp_path):
    assert not workspace_writes_forbidden(objective)
    result = RuntimeService(tmp_path).plan_mission(objective, adaptive=True, forced="full", retrieval_mode="none")
    assert result["goal_plan"]["requires_write"] is True
    assert len(result["approvals"]) == 1
    assert next(task for task in result["tasks"] if task["role"] == "WORKER")["write_scope"] == "task_contract_only"


@pytest.mark.parametrize("objective", [NO_WRITE_OBJECTIVE, "Summarize this answer without changing state."], ids=["explicit-denial", "persisted-task-scope"])
def test_stale_builtin_approval_cannot_override_persisted_no_write_objective(tmp_path, objective):
    service = RuntimeService(tmp_path)
    session = service.store.create_session(title="No-write authority regression")
    planned = service.plan_mission(objective, session_id=session["session_id"], adaptive=True, forced="full", retrieval_mode="none")
    mission_id = planned["mission"]["mission_id"]
    service.store.add_approval(Approval(approval_id=new_id("approval"), mission_id=mission_id,
        action="write_local_demo_artifacts", decision="approved", decided_by="BUILT_IN_POLICY", reason="legacy fixture"))
    executor = ColonyExecutor(service, session["session_id"], None, threading.Event())
    executor.goal_plan = {"requires_write": True}
    task = next(row for row in planned["tasks"] if row["role"] == "WORKER")
    content = 'HOST_WORKER_ARTIFACT_V1:' + json.dumps({"relative_path": ".crabagent/artifacts/forbidden.txt", "content": "must not exist\n"})
    with pytest.raises(RuntimeError, match="HOST_WORKER_ARTIFACT_FORBIDDEN"):
        executor._apply_host_worker_artifact(mission_id, {**task, "write_scope": "task_contract_only"}, "unused-attempt", content)
    assert not (tmp_path / ".crabagent/artifacts/forbidden.txt").exists()
    assert service.store.inspect(mission_id)["tool_receipts"] == []


def test_legacy_nonadaptive_plan_also_respects_explicit_no_write(tmp_path):
    result = RuntimeService(tmp_path).plan_mission(NO_WRITE_OBJECTIVE)
    assert result["approvals"] == []
