from pathlib import Path
from typing import Any, Dict, Optional
from zipfile import ZipFile

from crabagent.folder_ingest import OpenCrabPackBuilder, stage_folder


class FakeCrabAgentClient:
    def __init__(self, tier: str = "expert", result: Optional[Dict[str, Any]] = None) -> None:
        self.tier = tier
        self.result = result or {"status": "ingested", "package_id": "pack-new"}
        self.calls = []

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        self.calls.append((name, arguments))
        if name == "opencrab_status":
            return {"status": "ok", "tier": self.tier, "scopes": ["pack_write"]}
        if name == "opencrab_crab_agent":
            assert arguments["action"] == "plan"
            assert arguments["pack_name"]
            assert arguments["data_folder"]
            assert arguments["ontology_purpose"]
            return self.result
        raise AssertionError(name)


def test_stage_folder_preserves_full_sources_and_skips_generated_directories(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "README.md").write_text("# Title\n\nA real source.\n", encoding="utf-8")
    (project / "src").mkdir()
    (project / "src" / "main.py").write_text("print('hello')\n", encoding="utf-8")
    (project / ".git").mkdir()
    (project / ".git" / "secret.txt").write_text("must not enter evidence", encoding="utf-8")

    staged = stage_folder(project, tmp_path / "workspace" / ".crabagent" / "pack-runs")

    assert staged["manifest"]["source_count"] == 2
    assert staged["manifest"]["chunk_count"] == 2
    assert {row["relative_path"] for row in staged["manifest"]["sources"]} == {"README.md", "src/main.py"}
    assert all(row["evidence_id"].startswith("source-") for row in staged["chunks"])
    assert Path(staged["manifest_path"]).is_file()
    assert Path(staged["chunks_path"]).is_file()


def test_expert_gate_and_observed_remote_confirmation(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "notes.txt").write_text("Evidence one.\n", encoding="utf-8")

    expert_client = FakeCrabAgentClient("expert")
    success = OpenCrabPackBuilder(lambda: expert_client, tmp_path / "workspace").build_and_ingest(project)
    assert success["status"] == "ingested"
    assert success["package_id"] == "pack-new"
    assert [name for name, _ in expert_client.calls] == ["opencrab_status", "opencrab_crab_agent"]
    assert (Path(success["stage_path"]) / "result.json").is_file()
    assert Path(success["evidence_zip_path"]).is_file()
    with ZipFile(success["evidence_zip_path"]) as archive:
        assert set(archive.namelist()) == {"manifest.json", "chunks.jsonl", "README.txt"}
        assert "<local-folder>" in archive.read("manifest.json").decode("utf-8")

    pro_client = FakeCrabAgentClient("pro")
    blocked = OpenCrabPackBuilder(lambda: pro_client, tmp_path / "workspace-pro").build_and_ingest(project)
    assert blocked["status"] == "expert_required"
    assert [name for name, _ in pro_client.calls] == ["opencrab_status"]


def test_remote_rejection_is_not_reported_as_ingested(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "notes.txt").write_text("Evidence one.\n", encoding="utf-8")
    client = FakeCrabAgentClient("enterprise", {"status": "error", "detail": "pack validation failed"})

    result = OpenCrabPackBuilder(lambda: client, tmp_path / "workspace").build_and_ingest(project)

    assert result["status"] == "remote_rejected"
    assert result["remote"]["status"] == "error"


def test_upload_session_stays_pending_until_package_id_is_observed(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "notes.txt").write_text("Evidence one.\n", encoding="utf-8")

    class PendingClient(FakeCrabAgentClient):
        def __init__(self) -> None:
            super().__init__("enterprise", {"status": "plan_ready", "saas_ingest_handoff": {"upload_session_id": "upload-1"}})
            self.polls = 0

        def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
            self.calls.append((name, arguments))
            if name == "opencrab_status":
                return {"status": "ok", "tier": self.tier, "scopes": ["pack_write"]}
            if name != "opencrab_crab_agent":
                raise AssertionError(name)
            if arguments.get("action") == "status":
                self.polls += 1
                if self.polls == 1:
                    return {"status": "processing"}
                return {"status": "completed", "package_id": "pack-confirmed"}
            assert arguments["action"] == "plan"
            return self.result

    client = PendingClient()
    builder = OpenCrabPackBuilder(lambda: client, tmp_path / "workspace")
    result = builder.build_and_ingest(project)
    assert result["status"] == "upload_pending"
    assert result["upload_session_id"] == "upload-1"

    pending = builder.poll_upload("upload-1")
    assert pending["status"] == "processing"
    confirmed = builder.poll_upload("upload-1")
    assert confirmed["package_id"] == "pack-confirmed"
