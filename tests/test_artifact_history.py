import hashlib
from pathlib import Path

import pytest

from crabagent.runtime import RuntimeService
from crabagent.store import ColonyStore


@pytest.mark.parametrize("contents", [["revision one\n", "revision two\n", "revision three\n"], ["same\n", "same\n"]])
def test_every_recorded_artifact_keeps_its_original_bytes_after_refresh(tmp_path: Path, contents: list[str]) -> None:
    service = RuntimeService(tmp_path)
    planned = service.plan_mission("Preserve each observed receipt revision")
    mission_id = planned["mission"]["mission_id"]
    task_id = planned["tasks"][0]["task_id"]
    artifacts = [service._write_artifact(mission_id, task_id, "contract.json", content, "contract") for content in contents]

    reopened = ColonyStore(tmp_path).inspect(mission_id)
    rows = {row["artifact_id"]: row for row in reopened["artifacts"]}
    assert len({artifact.path for artifact in artifacts}) == len(contents)
    for artifact, content in zip(artifacts, contents):
        row = rows[artifact.artifact_id]
        actual = Path(row["path"]).read_bytes()
        assert actual == content.encode("utf-8")
        assert hashlib.sha256(actual).hexdigest() == row["sha256"]


def test_recording_artifact_does_not_replace_an_existing_unregistered_file(tmp_path: Path) -> None:
    service = RuntimeService(tmp_path)
    planned = service.plan_mission("Preserve preexisting artifact bytes")
    mission_id = planned["mission"]["mission_id"]
    directory = service.store.artifacts_dir / mission_id
    directory.mkdir(parents=True, exist_ok=True)
    existing = directory / "contract.json"
    existing.write_bytes(b"existing evidence\n")

    artifact = service._write_artifact(mission_id, planned["tasks"][0]["task_id"], "contract.json", "new evidence\n", "contract")

    assert existing.read_bytes() == b"existing evidence\n"
    assert Path(artifact.path) != existing
    assert Path(artifact.path).read_bytes() == b"new evidence\n"
