import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from crabagent.folder_ingest import OpenCrabPackBuilder
from crabagent.runner_pipeline import (
    RunnerPipeline,
    RunnerPipelineError,
    parse_judgments,
    upload_token_from_command,
)

RUNNER_BYTES = b"print('runner')\n"
RUNNER_SHA = hashlib.sha256(RUNNER_BYTES).hexdigest()


class FakeClient:
    def __init__(self, tier: str = "expert") -> None:
        self.tier = tier
        self.calls: List[tuple] = []

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        self.calls.append((name, dict(arguments)))
        if name == "opencrab_status":
            return {"tier": self.tier}
        action = arguments.get("action")
        if action == "update_runner":
            return {"runner_release": {"version": "2026.10.06.2", "sha256": RUNNER_SHA, "download_url": "https://opencrab.sh/r.py"}}
        if action == "create_upload_session":
            return {
                "saas_ingest_handoff": {
                    "upload_session_id": "sess-1",
                    "upload_url": "https://storage.example/upload",
                    "upload_finalize_url": "https://opencrab.sh/api/mcp/crab-agent-upload/sess-1",
                    "upload_method": "PUT_THEN_POST_FINALIZE",
                    "upload_max_bytes": 10_000_000,
                    "upload_command": "python3 x --verify-upload-zip z && curl -X PUT ... && curl -X POST -H 'X-OpenCrab-Upload-Token: tok_ABC-123' https://opencrab.sh/api/mcp/crab-agent-upload/sess-1",
                }
            }
        raise AssertionError((name, arguments))


class FakeRun:
    """Stands in for subprocess.run: a build writes the ZIP and a judgment request."""

    def __init__(self) -> None:
        self.commands: List[List[str]] = []

    def __call__(self, command, **kwargs):
        self.commands.append(list(command))
        if "--output-zip" in command:
            output = Path(command[command.index("--output-zip") + 1])
            output.write_bytes(b"PK\x05\x06" + b"\0" * 18)
            reports = output.with_suffix("") / "reports"
            reports.mkdir(parents=True, exist_ok=True)
            (reports / "judgment_request.json").write_text(json.dumps({"chunks": [{"id": "c1", "text": "We decided to ship."}]}), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")


def make_pipeline(tmp_path: Path, run: Any = None, http_log: List[tuple] = None) -> RunnerPipeline:
    log = http_log if http_log is not None else []

    def http(method, url, data=b"", headers=None, timeout=0):
        log.append((method, url, dict(headers or {}), len(data)))
        return {"status_code": 200, "body": {"status": "queued"}}

    return RunnerPipeline(home=tmp_path / "bin", python="python3", fetch=lambda url: RUNNER_BYTES, http=http, run=run or FakeRun())


def test_token_is_read_from_the_upload_command() -> None:
    assert upload_token_from_command("curl -H 'X-OpenCrab-Upload-Token: abc.DEF_1' u") == "abc.DEF_1"
    assert upload_token_from_command("curl u") == ""


def test_runner_is_installed_only_when_the_sha_matches(tmp_path: Path) -> None:
    pipeline = make_pipeline(tmp_path)
    first = pipeline.ensure_runner(FakeClient())
    assert first["updated"] is True
    assert Path(first["path"]).read_bytes() == RUNNER_BYTES
    assert pipeline.ensure_runner(FakeClient())["updated"] is False

    bad = RunnerPipeline(home=tmp_path / "other", fetch=lambda url: b"tampered")
    with pytest.raises(RunnerPipelineError, match="SHA-256"):
        bad.ensure_runner(FakeClient())
    assert not (tmp_path / "other" / "opencrab_desktop_build_runner.py").exists()


def test_build_command_carries_layer_judgments_and_kordoc(tmp_path: Path) -> None:
    pipeline = make_pipeline(tmp_path)
    command = pipeline.build_command("r.py", tmp_path, tmp_path / "o.zip", title="T", purpose="P", semantic_layer="lean", judgments=tmp_path / "j.json", kordoc_bin="/k")
    assert command[command.index("--semantic-layer") + 1] == "lean"
    assert command[command.index("--judgments") + 1].endswith("j.json")
    assert command[command.index("--kordoc-bin") + 1] == "/k"
    assert "--semantic-layer" not in pipeline.build_command("r.py", tmp_path, tmp_path / "o.zip", title="T")
    with pytest.raises(RunnerPipelineError):
        pipeline.build_command("r.py", tmp_path, tmp_path / "o.zip", title="T", semantic_layer="tiny")


def test_build_with_runner_uploads_and_finalizes_with_judgments(tmp_path: Path) -> None:
    folder = tmp_path / "reports"
    folder.mkdir()
    (folder / "report.pdf").write_bytes(b"%PDF-1.4")
    http_log: List[tuple] = []
    run = FakeRun()
    pipeline = make_pipeline(tmp_path, run=run, http_log=http_log)
    client = FakeClient()
    judged: List[Dict[str, Any]] = []

    def judge(request):
        judged.append(request)
        return {"version": 1, "judgments": [{"chunk_id": "c1", "kind": "decision", "quote": "We decided to ship."}]}

    builder = OpenCrabPackBuilder(lambda: client, tmp_path / "ws", pipeline=pipeline)
    result = builder.build_with_runner(folder, semantic_layer="lean", origin="source", judge=judge)

    assert result["status"] == "upload_pending"
    assert result["upload_session_id"] == "sess-1"
    assert judged and judged[0]["chunks"][0]["id"] == "c1"
    builds = [c for c in run.commands if "--output-zip" in c]
    assert len(builds) == 2 and "--judgments" in builds[1] and "--semantic-layer" in builds[0]
    assert any("--verify-upload-zip" in c for c in run.commands)
    session_args = [args for name, args in client.calls if args.get("action") == "create_upload_session"][0]
    assert session_args["origin"] == "source"
    assert [entry[0] for entry in http_log] == ["PUT", "POST"]
    assert http_log[1][2]["X-OpenCrab-Upload-Token"] == "tok_ABC-123"


def test_build_with_runner_stops_before_upload_when_the_build_fails(tmp_path: Path) -> None:
    folder = tmp_path / "f"
    folder.mkdir()

    def failing(command, **kwargs):
        return SimpleNamespace(returncode=3, stdout="", stderr="kordoc failed on a.hwp")

    http_log: List[tuple] = []
    builder = OpenCrabPackBuilder(lambda: FakeClient(), tmp_path / "ws", pipeline=make_pipeline(tmp_path, run=failing, http_log=http_log))
    result = builder.build_with_runner(folder)
    assert result["status"] == "local_build_failed"
    assert "kordoc failed" in result["reason"]
    assert http_log == []


def test_non_expert_accounts_are_refused_before_any_build(tmp_path: Path) -> None:
    folder = tmp_path / "f"
    folder.mkdir()
    run = FakeRun()
    builder = OpenCrabPackBuilder(lambda: FakeClient("pro"), tmp_path / "ws", pipeline=make_pipeline(tmp_path, run=run))
    assert builder.build_with_runner(folder)["status"] == "expert_required"
    assert run.commands == []


def test_parse_judgments_finds_the_json_object_in_model_text() -> None:
    text = 'Here you go:\n```json\n{"version": 1, "judgments": [{"quote": "x"}]}\n```'
    assert parse_judgments(text)["judgments"][0]["quote"] == "x"
    assert parse_judgments('{"other": 1}') is None
    assert parse_judgments("") is None
