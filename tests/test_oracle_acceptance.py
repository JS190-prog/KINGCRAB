import hashlib
import json
import threading
from pathlib import Path

import pytest

from crabagent.codex_app_server import CodexLiveTurn
from crabagent.colony import ColonyExecutor
from crabagent.oracle_verdict import parse_oracle_verdict
from crabagent.runtime import RuntimeService
from crabagent.workspace_observer import capture_workspace


class Bridge:
    executor_name = "host_current_model"
    supports_session_fallback = False
    text = ""

    def run_turn(self, *args, **kwargs):
        return CodexLiveTurn(thread_id="test-host", turn_id="test-turn", status="completed", text=self.text,
                             usage={}, started_at="test", finished_at="test")


def prepared(tmp_path, write=False):
    service = RuntimeService(tmp_path)
    session = service.store.create_session(model_policy="host", executor_policy="host")
    objective = ("Create exactly .crabagent/artifacts/expected.txt. Do not modify existing files." if write else
                 "Read the provided text. No file writes.")
    planned = service.plan_mission(objective, session_id=session["session_id"], adaptive=True, forced="full", retrieval_mode="none")
    executor = ColonyExecutor(service, session["session_id"], Bridge(), threading.Event())
    executor.goal_plan = planned["goal_plan"]
    return executor, planned, objective


def run_role(executor, planned, objective, role, text):
    task = next(row for row in planned["tasks"] if row["role"] == role)
    assignment = next(row for row in planned["assignments"] if row["task_id"] == task["task_id"])
    executor.bridge.text = text
    return executor._run_codex_role(planned["mission"]["mission_id"], task, assignment, objective, [])


@pytest.mark.parametrize("text", [
    "VERDICT\nFAIL\nThe requested file does not exist.", "VERDICT: blocked", "VERDICT: unknown",
    'ORACLE_VERDICT_V1:{"verdict":"fail"}', 'ORACLE_VERDICT_V1:{"verdict":"pass","verdict":"fail"}',
    'ORACLE_VERDICT_V1:{"verdict":"pass"}\nVERDICT: FAIL', "The role call succeeded; everything is fine.",
])
def test_successful_host_turn_cannot_accept_a_failed_or_missing_verdict(tmp_path, text):
    executor, planned, objective = prepared(tmp_path)
    mission_id = planned["mission"]["mission_id"]
    with pytest.raises(RuntimeError, match="ORACLE_ACCEPTANCE_REJECTED"):
        run_role(executor, planned, objective, "ORACLE", text)
    snapshot = executor.store.inspect(mission_id)
    assert next(row for row in snapshot["tasks"] if row["role"] == "ORACLE")["status"] == "failed"
    assert snapshot["attempts"][-1]["status"] == "succeeded"
    assert not snapshot["mission"].get("oracle_result_artifact_id")
    artifact = next(row for row in snapshot["artifacts"] if row["kind"] == "oracle_result")
    assert artifact["status"] == "rejected"
    assert artifact["sha256"] == hashlib.sha256((text + "\n").encode()).hexdigest()
    assert Path(artifact["path"]).read_text() == text + "\n"


@pytest.mark.parametrize("text", ['ORACLE_VERDICT_V1:{"verdict":"pass"}', 'VERDICT\nPASS', 'Verdict: accepted'])
def test_explicit_pass_accepts_a_nonwrite_result(tmp_path, text):
    executor, planned, objective = prepared(tmp_path)
    run_role(executor, planned, objective, "ORACLE", text)
    snapshot = executor.store.inspect(planned["mission"]["mission_id"])
    assert next(row for row in snapshot["tasks"] if row["role"] == "ORACLE")["status"] == "verified"
    assert snapshot["mission"]["oracle_result_artifact_id"]


def test_pass_without_worker_change_receipt_is_rejected(tmp_path):
    executor, planned, objective = prepared(tmp_path, write=True)
    with pytest.raises(RuntimeError, match="missing_worker_change_receipt"):
        run_role(executor, planned, objective, "ORACLE", 'VERDICT: PASS')
    assert not (tmp_path / '.crabagent/artifacts/expected.txt').exists()


@pytest.mark.parametrize("tamper", [False, True])
def test_oracle_pass_requires_fresh_bytes_matching_real_worker_receipt(tmp_path, tamper):
    executor, planned, objective = prepared(tmp_path, write=True)
    executor._worker_baseline = capture_workspace(tmp_path)
    directive = 'HOST_WORKER_ARTIFACT_V1:' + json.dumps({'relative_path': '.crabagent/artifacts/expected.txt', 'content': 'actual output\n'})
    run_role(executor, planned, objective, "WORKER", directive)
    worker = next(row for row in planned["tasks"] if row["role"] == "WORKER")
    executor._record_workspace_change(planned["mission"]["mission_id"], worker)
    target = tmp_path / '.crabagent/artifacts/expected.txt'
    assert target.read_bytes() == b'actual output\n'
    if tamper:
        target.write_bytes(b'changed after receipt\n')
        with pytest.raises(RuntimeError, match="final_bytes_missing_or_changed"):
            run_role(executor, planned, objective, "ORACLE", 'VERDICT: PASS')
    else:
        run_role(executor, planned, objective, "ORACLE", 'VERDICT: PASS')


def test_host_worker_cannot_change_the_requested_output_path(tmp_path):
    executor, planned, objective = prepared(tmp_path, write=True)
    directive = 'HOST_WORKER_ARTIFACT_V1:' + json.dumps({'relative_path': '.crabagent/artifacts/different.txt', 'content': 'wrong target'})
    with pytest.raises(RuntimeError, match="target is not an artifact requested"):
        run_role(executor, planned, objective, "WORKER", directive)
    assert not (tmp_path / '.crabagent/artifacts/different.txt').exists()


@pytest.mark.parametrize('text', ['VERDICT\nFAIL\nMissing file.', 'No explicit decision.'])
def test_continuation_rejects_legacy_false_completed_source_without_rewriting_history(tmp_path, text):
    from types import SimpleNamespace
    from crabagent.daemon import RuntimeServer

    mission_id = 'mission-legacy'
    source = {'mission_id': mission_id, 'session_id': 'session-legacy', 'status': 'completed', 'oracle_result_artifact_id': 'oracle'}
    path = tmp_path / '.crabagent/artifacts' / mission_id / 'oracle.md'
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding='utf-8')
    detail = {'tasks': [{'role': 'ORACLE'}], 'artifacts': [{'artifact_id': 'oracle', 'kind': 'oracle_result', 'status': 'accepted',
               'path': str(path), 'sha256': hashlib.sha256(text.encode()).hexdigest()}]}
    store = SimpleNamespace(mission=lambda key: source, missions_for_session=lambda *a, **kw: [source], inspect=lambda key: detail)
    server = SimpleNamespace(service=SimpleNamespace(store=store), workspace=tmp_path)
    with pytest.raises(RuntimeError, match='stored completion alone is insufficient'):
        RuntimeServer._continuation_handoff(server, 'session-legacy', mission_id)
    assert source['status'] == 'completed' and path.read_text() == text
