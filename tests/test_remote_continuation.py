from __future__ import annotations

import json

import pytest

from crabagent.daemon import RuntimeServer
from crabagent.goal import classify_goal
from crabagent.protocol import runtime_paths


@pytest.mark.parametrize("verb", ["작성한다", "생성한다"])
def test_korean_declarative_deliverable_keeps_worker_write_scope(verb):
    objective = f"Core 근거의 출처를 확인한다. WORKER는 새 검증 파일 .crabagent/artifacts/probe.md 한 개를 {verb}. 그 밖의 파일과 외부 데이터는 변경하지 않는다."
    plan = classify_goal(objective, forced="full", retrieval_mode="evidence_first")
    assert plan.requires_write is True
    assert plan.action_mode == "execute"
    assert "WORKER" in plan.stages and "ORACLE" in plan.stages


def test_korean_declarative_negated_write_stays_read_only():
    plan = classify_goal("Core 근거를 확인한다. 파일을 작성하지 않는다.", retrieval_mode="evidence_first")
    assert plan.requires_write is False


@pytest.fixture
def server(tmp_path):
    paths = runtime_paths(tmp_path)
    paths["root"].mkdir(parents=True)
    instance = RuntimeServer(tmp_path, paths["socket"])
    try:
        yield instance
    finally:
        instance.server_close()


def _source(server, *, legacy=False, graph=False, local=False):
    session = server.service.store.create_session(executor_policy="host", interaction_mode="colony")
    context = {
        "status": "ok", "authority": "gateway_verified_mcp_response",
        "evidence": [{"id": "ev-source", "text": "A scoped review rule.",
                      "source": "opencrab://evidence/ev-source", "package_id": "pack-source"}],
        "claim_gate": "pass", "graph_gate": "not_required", "evidence_count": 1,
        "pack_scope": {"package_ids": ["pack-source"], "project_ids": ["project-source"]},
    }
    if graph:
        context["graph_gate"] = "pending_runtime_semantic_validation"
        context["gateway_graph"] = {"authority": "gateway_verified_mcp_response", "status": "ok",
                                    "nodes": [{"id": "a"}, {"id": "b"}],
                                    "edges": [{"source": "a", "target": "b", "relation": "supports"}]}
    objective = ("로컬 파일 수정. OpenCrab 근거 조회 없이 진행해." if local else "OpenCrab 근거로 원고를 수정하고 검수해.")
    planned = server.service.plan_mission(objective, session_id=session["session_id"], adaptive=True,
        execution_scope="local_only" if local else "", retrieval_mode="none" if local else "graph_path" if graph else "evidence_first")
    mid = planned["mission"]["mission_id"]
    if not local:
        kind = "mcp_context_receipt" if legacy else "opencrab_handoff"
        artifact = server.service._write_artifact(mid, planned["tasks"][0]["task_id"], kind + ".json",
                                                  json.dumps(context), kind)
    else:
        artifact = None
    return session, mid, context, artifact


@pytest.mark.parametrize("mode,ontology,graph", [("none", False, False), ("evidence_first", True, False), ("graph_path", True, True)])
def test_preflight_retrieval_mode_is_the_execution_authority(mode, ontology, graph):
    plan = classify_goal("OpenCrab 노드 관계 그래프 경로를 포함한 원고의 문장을 수정해.",
                         selected_pack_count=3, knowledge_available=True, retrieval_mode=mode)
    assert plan.ontology_required is ontology
    assert plan.graph_required is graph
    assert plan.retrieval_mode == mode
    assert "WORKER" in plan.stages and "ORACLE" in plan.stages


@pytest.mark.parametrize("legacy", [False, True])
def test_followup_preserves_source_evidence_without_daemon_credentials(server, monkeypatch, legacy):
    session, mid, context, _ = _source(server, legacy=legacy)
    captured = {}
    def launch(current_session, objective, **kwargs):
        captured.update(kwargs)
        return {"status": "starting"}
    monkeypatch.setattr(server, "_launch_colony", launch)
    result = server.dispatch({"action": "prompt.submit", "payload": {
        "session_id": session["session_id"], "source_mission_id": mid,
        "objective": "기존 후보의 반복 문장을 줄여 새 파일로 작성하고 다시 검수하라.", "interaction": "colony",
    }})
    assert result["status"] == "starting"
    assert captured["preloaded_opencrab_context"] == context
    assert captured["retrieval_mode"] == "evidence_first"
    assert captured["continuation_of"] == mid


def test_original_prefetched_graph_survives_continuation(server):
    session, mid, context, _ = _source(server, graph=True)
    handoff = server._continuation_handoff(session["session_id"], mid)
    assert handoff["preloaded_opencrab_context"] == context
    assert handoff["retrieval_mode"] == "graph_path"


def test_changed_selected_scope_cannot_relabel_source_evidence(server):
    session, mid, _, _ = _source(server)
    server.service.store.set_ontology_context(session["session_id"], ["project-other"], ["pack-other"])
    with pytest.raises(RuntimeError, match="scope changed"):
        server._continuation_handoff(session["session_id"], mid)


def test_queued_followup_preserves_the_same_source_boundary(server, monkeypatch):
    session, mid, context, _ = _source(server)
    captured = {}
    def launch(current_session, objective, **kwargs):
        captured.update(kwargs)
        return {"status": "starting"}
    monkeypatch.setattr(server, "_launch_colony", launch)
    server._jobs[session["session_id"]] = type("Running", (), {"is_alive": lambda self: True})()
    queued = server.dispatch({"action": "prompt.submit", "payload": {
        "session_id": session["session_id"], "source_mission_id": mid,
        "objective": "기존 후보를 추가 수정하고 검수해", "disposition": "wait", "interaction": "colony",
    }})
    assert queued["queue_id"]
    assert not captured
    server._finish_job(session["session_id"])
    assert captured["continuation_of"] == mid
    assert captured["preloaded_opencrab_context"] == context
    assert captured["retrieval_mode"] == "evidence_first"


def test_tampered_evidence_blocks_followup_before_launch(server, monkeypatch):
    session, mid, _, artifact = _source(server)
    from pathlib import Path
    Path(artifact.path).write_text('{"status":"ok","evidence":[]}', encoding="utf-8")
    monkeypatch.setattr(server, "_launch_colony", lambda *a, **k: pytest.fail("must not launch"))
    with pytest.raises(RuntimeError, match="digest mismatch"):
        server._dispatch(session, "새 수정 후보를 작성하라", "colony", source_mission_id=mid)


def test_unrelated_session_and_stale_source_are_rejected(server):
    session, mid, _, _ = _source(server)
    other, _, _, _ = _source(server)
    with pytest.raises(RuntimeError, match="does not belong"):
        server._continuation_handoff(other["session_id"], mid)
    server.service.plan_mission("A different mission", session_id=session["session_id"], adaptive=True)
    with pytest.raises(RuntimeError, match="stale"):
        server._continuation_handoff(session["session_id"], mid)


def test_local_followup_keeps_local_authority(server):
    session, mid, _, _ = _source(server, local=True)
    handoff = server._continuation_handoff(session["session_id"], mid)
    assert handoff["execution_scope"] == "local_only"
    assert handoff["preloaded_opencrab_context"] is None
    assert handoff["retrieval_mode"] == "none"
    next_plan = classify_goal("기존 파일의 오타를 수정해", execution_scope=handoff["execution_scope"], retrieval_mode=handoff["retrieval_mode"])
    assert not next_plan.ontology_required
    assert "ORACLE" in next_plan.stages


def test_artifact_digest_matches_the_bytes_written_on_every_platform(server):
    import hashlib
    from pathlib import Path
    session, mid, _, _ = _source(server)
    detail = server.service.store.inspect(mid)
    artifact = server.service._write_artifact(mid, detail["tasks"][0]["task_id"], "multiline.txt", "첫 줄\n둘째 줄\n", "test")
    raw = Path(artifact.path).read_bytes()
    assert raw == "첫 줄\n둘째 줄\n".encode("utf-8")
    assert hashlib.sha256(raw).hexdigest() == artifact.sha256


def test_missing_remote_context_is_rejected_before_mission_creation(server):
    session = server.service.store.create_session(executor_policy="host")
    with pytest.raises(RuntimeError, match="requires an observed"):
        server.dispatch({"action": "prompt.submit", "payload": {
            "session_id": session["session_id"], "objective": "원고를 작성해",
            "retrieval_mode": "evidence_first", "interaction": "colony",
        }})
    assert server.service.store.missions_for_session(session["session_id"]) == []


def test_durable_followup_reaches_oracle_without_any_runtime_opencrab_client(server, monkeypatch):
    import crabagent.colony as colony
    import crabagent.runtime as runtime
    monkeypatch.setattr(colony, "opencrab_url", lambda **kw: pytest.fail("must use the persisted gateway handoff"))
    monkeypatch.setattr(runtime, "opencrab_is_configured", lambda *a, **kw: pytest.fail("remote execution must not depend on Codex MCP config"))
    monkeypatch.setattr(server, "_knowledge_available", lambda *a: pytest.fail("remote dispatch must use its retrieval contract"))
    monkeypatch.setattr(server, "_bridge", lambda session: type("BridgeStub", (), {"thread_id": None})())
    session = server.service.store.create_session(executor_policy="host", interaction_mode="colony")
    context = {
        "status": "ok", "authority": "gateway_verified_mcp_response",
        "evidence": [{"id": "ev-scope", "text": "The selected package is available.", "source": "opencrab://evidence/ev-scope"}],
        "evidence_count": 1, "claim_gate": "pass", "graph_gate": "not_required", "tool_calls": [],
    }
    def finish(payload):
        server.dispatch({"action": "prompt.submit", "payload": payload})
        job = server._jobs.get(session["session_id"])
        if job is not None:
            job.join(timeout=15)
            assert not job.is_alive(), "the bounded mission did not terminate"
        rows = server.service.store.missions_for_session(session["session_id"], limit=1)
        detail = server.service.store.inspect(rows[0]["mission_id"])
        assert detail["mission"]["status"] == "completed"
        assert next(t for t in detail["tasks"] if t["role"] == "ORACLE")["status"] == "verified"
        handoff = next(a for a in detail["artifacts"] if a["kind"] == "opencrab_handoff")
        from pathlib import Path
        assert json.loads(Path(handoff["path"]).read_text(encoding="utf-8")) == context
        return detail["mission"]["mission_id"]
    source = finish({"session_id": session["session_id"], "objective": "팩 목록과 상태를 보여줘",
                     "interaction": "colony", "opencrab_context": context, "retrieval_mode": "evidence_first"})
    followup = finish({"session_id": session["session_id"], "objective": "팩 목록과 상태를 다시 보여줘",
                       "interaction": "colony", "source_mission_id": source})
    assert source != followup
    event = next(e for e in server.service.store.events(followup) if e.event_type == "mission_continuation_bound")
    assert event.payload["source_mission_id"] == source


@pytest.mark.parametrize("valid_handoff", [True, False])
def test_host_queen_heading_gate_is_enforced_before_worker_mutation(server, valid_handoff):
    import threading
    from crabagent.colony import ColonyExecutor
    from crabagent.codex_app_server import CodexLiveTurn

    session = server.service.store.create_session(executor_policy="host", model_policy="host", interaction_mode="colony")
    target = server.workspace / ".crabagent" / "artifacts" / "heading-check.md"
    roles = []
    queen = ("SELECTED_PATH:\nUse observed evidence ev-heading-1234.\n"
             "SUPPORTED_CLAIMS:\nThe source receipt contains ev-heading-1234.\n"
             "GAPS:\nNo provenance gap.\nNEXT_ACTION:\nWrite the single scoped artifact.") if valid_handoff else "No structured decision handoff."

    class Bridge:
        thread_id = "host-heading-regression"
        last_start_mode = "started"
        executor_name = "host_current_model"
        last_reported_model = "test-fixture"

        def start(self):
            return self.thread_id

        def run_turn(self, prompt, emit, **kwargs):
            role = prompt.splitlines()[0].rsplit(" ", 1)[-1]
            roles.append(role)
            if role == "WORKER":
                result = 'HOST_WORKER_ARTIFACT_V1:{"relative_path":".crabagent/artifacts/heading-check.md","content":"observed test artifact\\n"}'
            elif role == "QUEEN":
                result = queen
            else:
                result = "GOAL_RESTATEMENT\nCreate the bounded artifact.\nSUBGOALS\nUse observed source.\nCONSTRAINTS\nOne file.\nSUCCESS_CHECKS\nFile receipt.\nNEXT_ACTION\nProceed within the bound."
            return CodexLiveTurn(thread_id=self.thread_id, turn_id="turn-" + role, status="completed",
                                 text=result, usage={}, started_at="now", finished_at="now")

    context = {"status": "ok", "authority": "gateway_verified_mcp_response", "claim_gate": "pass",
               "graph_gate": "not_required", "evidence_count": 1, "tool_calls": [],
               "evidence": [{"id": "ev-heading-1234", "source": "opencrab://test/source", "text": "Observed source record."}]}
    snapshot = ColonyExecutor(server.service, session["session_id"], Bridge(), threading.Event(),
                              opencrab_context_loader=lambda args: context, opencrab_handoff=context,
                              forced="full", retrieval_mode="evidence_first").run("Use evidence and create one bounded artifact", max_workers=1)
    if valid_handoff:
        assert snapshot["mission"]["status"] == "completed"
        assert "ORACLE" in roles
        assert target.read_text(encoding="utf-8") == "observed test artifact\n"
    else:
        assert snapshot["mission"]["status"] == "failed"
        assert "WORKER" not in roles
        assert not target.exists()
        assert not any(row["tool_name"] == "host.worker.artifact.write" for row in snapshot["tool_receipts"])
