import threading
import time
from pathlib import Path

import pytest

from crabagent.daemon import RuntimeServer, _king_title, _mcp_policy_with
from crabagent.protocol import DaemonClient, runtime_paths, start_daemon


def test_daemon_protocol_runs_demo_and_replays_receipts(tmp_path: Path) -> None:
    paths = runtime_paths(tmp_path)
    paths["root"].mkdir(parents=True, exist_ok=True)
    if paths["socket"].exists():
        paths["socket"].unlink()
    server = RuntimeServer(tmp_path, paths["socket"])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = DaemonClient(tmp_path)
        assert client.ping()
        session = client.request("session.ensure")
        configured = client.request(
            "session.configure",
            session_id=session["session_id"],
            model_policy="luna",
            interaction_mode="chat",
            mcp_policy="off",
            max_workers=1,
        )
        assert configured["model_policy"] == "luna"
        assert configured["interaction_mode"] == "chat"
        assert configured["mcp_policy"] == "off"
        assert configured["max_workers"] == 1
        result = client.request(
            "run_demo",
            objective="Exercise daemon persistence",
            session_id=session["session_id"],
        )
        mission_id = result["mission"]["mission_id"]
        replay = client.request("replay", mission_id=mission_id)
        assert result["mission"]["status"] == "completed"
        snapshot = client.request(
            "ui.snapshot",
            session_id=session["session_id"],
            after_message_id=0,
        )
        assert snapshot["session"]["session_id"] == session["session_id"]
        assert "colony" in snapshot
        assert "structural" in snapshot["quality"]
        assert "goal_contract" in snapshot["quality"]
        assert any(row["session_id"] == session["session_id"] for row in snapshot["colony"]["sessions"])
        overview = client.request("colony.overview")
        row = next(item for item in overview["sessions"] if item["session_id"] == session["session_id"])
        assert row["last_mission"]["mission_id"] == mission_id
        assert row["roles"]["ORACLE"]["status"] == "verified"
        assert any(event["event_type"] == "tool_receipt_recorded" for event in replay["events"])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        if paths["socket"].exists():
            paths["socket"].unlink()


def test_short_follow_up_reuses_completed_mission_instead_of_creating_new_one(tmp_path: Path) -> None:
    paths = runtime_paths(tmp_path)
    paths["root"].mkdir(parents=True, exist_ok=True)
    server = RuntimeServer(tmp_path, paths["socket"])
    try:
        session = server.dispatch({"action": "session.ensure", "payload": {}})
        completed = server.dispatch({
            "action": "run_demo",
            "payload": {
                "objective": "Create a durable portfolio artifact",
                "session_id": session["session_id"],
            },
        })
        before = len(server.service.store.list_missions(limit=100))
        result = server.dispatch({
            "action": "prompt.submit",
            "payload": {
                "session_id": session["session_id"],
                "objective": "진행해",
            },
        })
        after = len(server.service.store.list_missions(limit=100))

        assert result["interaction"] == "continuation"
        assert result["status"] == "ready"
        assert result["source_mission_id"] == completed["mission"]["mission_id"]
        assert before == after
        messages = server.service.store.messages(session["session_id"], limit=20)
        assert any("CONTINUATION READY" in str(row.get("content") or "") for row in messages)
    finally:
        server.server_close()
        if paths["socket"].exists():
            paths["socket"].unlink()


def test_short_follow_up_without_history_does_not_create_context_free_mission(tmp_path: Path) -> None:
    paths = runtime_paths(tmp_path)
    paths["root"].mkdir(parents=True, exist_ok=True)
    server = RuntimeServer(tmp_path, paths["socket"])
    try:
        session = server.dispatch({"action": "session.ensure", "payload": {}})
        result = server.dispatch({
            "action": "prompt.submit",
            "payload": {"session_id": session["session_id"], "objective": "진행해"},
        })

        assert result["status"] == "needs_input"
        assert result["interaction"] == "continuation"
        assert server.service.store.list_missions(limit=100) == []
        messages = server.service.store.messages(session["session_id"], limit=20)
        assert any("CONTINUATION NEEDS A GOAL" in str(row.get("content") or "") for row in messages)
    finally:
        server.server_close()
        if paths["socket"].exists():
            paths["socket"].unlink()


def test_auto_started_daemon_survives_client_process_scope(tmp_path: Path) -> None:
    client = start_daemon(tmp_path)
    try:
        first = client.request("ping")
        second = DaemonClient(tmp_path).request("ping")
        assert first["pid"] == second["pid"]
        assert first["status"] == "online"
    finally:
        client.request("shutdown")
        deadline = time.monotonic() + 2
        while client.ping() and time.monotonic() < deadline:
            time.sleep(0.05)


def test_mcp_policy_selection_and_king_title_are_session_scoped() -> None:
    names = ["OpenCrab", "Neo4j", "github"]
    assert _mcp_policy_with("auto", "OpenCrab", True, names) == "allow:OpenCrab"
    assert _mcp_policy_with("allow:OpenCrab,Neo4j", "OpenCrab", False, names) == "allow:Neo4j"
    assert _mcp_policy_with("allow:OpenCrab", "OpenCrab", False, names) == "off"
    assert "OpenCrab" in _king_title("OpenCrab MCP panel implementation and verification")


def test_daemon_forks_codex_context_only_when_session_is_ready(tmp_path: Path) -> None:
    paths = runtime_paths(tmp_path)
    paths["root"].mkdir(parents=True, exist_ok=True)
    server = RuntimeServer(tmp_path, paths["socket"])

    class StubBridge:
        def fork_thread(self) -> str:
            return "thread-forked"

    try:
        session = server.service.store.create_session(title="Source")
        server.service.store.update_session(session["session_id"], codex_thread_id="thread-source")
        server.service.store.add_message(session["session_id"], "user", "hello")
        server._bridge = lambda current: StubBridge()

        result = server.dispatch({"action": "session.fork", "payload": {"session_id": session["session_id"]}})

        assert result["session"]["codex_thread_id"] == "thread-forked"
        assert result["codex_context_forked"] is True
        server.service.store.update_session(session["session_id"], status="running")
        with pytest.raises(RuntimeError, match="active turn"):
            server.dispatch({"action": "session.fork", "payload": {"session_id": session["session_id"]}})
    finally:
        server.server_close()
        if paths["socket"].exists():
            paths["socket"].unlink()


def test_crab_doc_manifest_import_and_list(tmp_path: Path) -> None:
    paths = runtime_paths(tmp_path)
    paths["root"].mkdir(parents=True, exist_ok=True)
    server = RuntimeServer(tmp_path, paths["socket"])
    manifest_path = tmp_path / "sample.crabdoc.json"
    manifest_path.write_text(
        '{"protocol":"crab-doc/v1","product":"CRAB DOC","createdAt":"2026-08-03T00:00:00Z",'
        '"sourceFileName":"sample.docx","document":{"fileName":"sample.docx","extension":".docx",'
        '"kind":"document","title":"Sample brief","text":"Verified context.","parser":"OOXML DOCX",'
        '"capabilities":{"read":true,"edit":true,"export":true,"exportExtension":".docx"}},'
        '"suggestedCommand":"crab doc import ./sample.docx.crabdoc.json"}\n',
        encoding="utf-8",
    )
    try:
        imported = server.dispatch({"action": "doc.import", "payload": {"manifest_path": str(manifest_path)}})
        assert imported["status"] == "ok"
        assert imported["title"] == "Sample brief"
        assert Path(imported["manifest_path"]).is_file()
        assert Path(imported["markdown_path"]).read_text(encoding="utf-8") == "Verified context.\n"
        listed = server.dispatch({"action": "doc.list", "payload": {}})
        assert listed["count"] == 1
        assert listed["documents"][0]["title"] == "Sample brief"
    finally:
        server.server_close()
        if paths["socket"].exists():
            paths["socket"].unlink()


def test_folder_project_and_new_conversation_are_durable(tmp_path: Path) -> None:
    paths = runtime_paths(tmp_path)
    paths["root"].mkdir(parents=True, exist_ok=True)
    folder = tmp_path / "repo"
    folder.mkdir()

    class Builder:
        def build_and_ingest(self, selected: Path, **kwargs):
            return {
                "status": "expert_required",
                "reason": "test gate",
                "run_id": "packrun-test",
                "root_path": str(selected.resolve()),
                "stage_path": str(tmp_path / "stage"),
                "manifest_path": str(tmp_path / "stage" / "manifest.json"),
                "chunks_path": str(tmp_path / "stage" / "chunks.jsonl"),
                "source_count": 1,
                "chunk_count": 1,
            }

    server = RuntimeServer(tmp_path, paths["socket"], pack_builder=Builder())
    try:
        project = server.dispatch({"action": "project.create", "payload": {"name": "Repo", "root_path": str(folder)}})
        assert project["root_path"] == str(folder.resolve())
        session = server.dispatch({
            "action": "session.create",
            "payload": {
                "title": "Repo conversation",
                "project_id": project["project_id"],
                "project_name": project["name"],
                "project_root_path": project["root_path"],
            },
        })
        assert session["project_id"] == project["project_id"]
        assert session["project_root_path"] == project["root_path"]
        result = server.dispatch({"action": "pack.ingest", "payload": {"project_id": project["project_id"]}})
        assert result["status"] == "expert_required"
        stored = server.service.store.pack_ingest_run("packrun-test")
        assert stored is not None
        assert stored["status"] == "expert_required"
    finally:
        server.server_close()
        if paths["socket"].exists():
            paths["socket"].unlink()
