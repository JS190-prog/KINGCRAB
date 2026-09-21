from __future__ import annotations

import hashlib
import json
from pathlib import Path

from crabagent.artifact_integrity import verify_mission_artifacts
from crabagent.daemon import RuntimeServer
from crabagent.models import Artifact, ArtifactStatus, MissionContract, Role, TaskSlot
from crabagent.protocol import runtime_paths
from crabagent.release_provenance import current_release_provenance
from crabagent.store import ColonyStore


def _seed_artifact(store: ColonyStore, path: Path, *, sha256: str = "") -> str:
    mission_id = "mission-integrity"
    task_id = "task-worker"
    store.initialize()
    store.create_mission(
        MissionContract(
            mission_id=mission_id,
            objective="Verify durable artifact bytes",
            acceptance=["stored hash matches current bytes"],
            workspace=str(store.workspace),
            risk="low",
            max_attempts=1,
            max_workers=1,
            token_budget=100,
        )
    )
    store.add_task(
        TaskSlot(
            task_id=task_id,
            mission_id=mission_id,
            title="Write artifact",
            role=Role.WORKER,
            position=1,
            output_contract="artifact.txt",
        )
    )
    store.add_artifact(
        Artifact(
            artifact_id="artifact-integrity",
            mission_id=mission_id,
            task_id=task_id,
            kind="worker_output",
            path=str(path),
            sha256=sha256 or hashlib.sha256(path.read_bytes()).hexdigest(),
            status=ArtifactStatus.ACCEPTED,
        )
    )
    return mission_id


def test_verify_mission_artifacts_reads_original_bytes_and_detects_tampering(tmp_path: Path) -> None:
    store = ColonyStore(tmp_path)
    path = store.artifacts_dir / "mission-integrity" / "artifact.txt"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"durable bytes\n")
    mission_id = _seed_artifact(store, path)

    verified = verify_mission_artifacts(store, mission_id)

    assert verified["status"] == "ok"
    assert verified["artifact_count"] == 1
    assert verified["original_bytes_read_count"] == 1
    assert verified["hash_match_count"] == 1
    assert verified["artifacts"][0]["observed_sha256"] == hashlib.sha256(b"durable bytes\n").hexdigest()

    path.write_bytes(b"tampered bytes\n")
    tampered = verify_mission_artifacts(store, mission_id)

    assert tampered["status"] == "mismatch"
    assert tampered["hash_mismatch_count"] == 1
    assert tampered["artifacts"][0]["hash_matches"] is False


def test_verify_mission_artifacts_blocks_paths_outside_mission_root(tmp_path: Path) -> None:
    store = ColonyStore(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside")
    mission_id = _seed_artifact(store, outside)

    result = verify_mission_artifacts(store, mission_id)

    assert result["status"] == "mismatch"
    assert result["original_bytes_read_count"] == 0
    assert result["artifacts"][0]["read_status"] == "blocked"
    assert "escapes" in result["artifacts"][0]["error"]


def test_runtime_dispatch_exposes_artifact_integrity_and_release_provenance(tmp_path: Path) -> None:
    paths = runtime_paths(tmp_path)
    paths["root"].mkdir(parents=True, exist_ok=True)
    server = RuntimeServer(tmp_path, paths["socket"])
    try:
        path = server.service.store.artifacts_dir / "mission-integrity" / "artifact.txt"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"runtime readback")
        mission_id = _seed_artifact(server.service.store, path)

        hello = server.dispatch({"action": "ping", "payload": {}})
        result = server.dispatch(
            {"action": "mission.artifact_integrity", "payload": {"mission_id": mission_id}}
        )

        assert "mission.artifact_integrity" in hello["capabilities"]
        assert hello["release_provenance"]["status"] in {"available", "unavailable"}
        assert result["status"] == "ok"
        assert result["original_bytes_read_count"] == 1
    finally:
        server.server_close()
        if paths["socket"].exists():
            paths["socket"].unlink()


def test_release_provenance_reads_only_valid_bounded_fields(tmp_path: Path) -> None:
    prefix = tmp_path / "release" / ".venv"
    prefix.mkdir(parents=True)
    payload = {
        "release_id": "0.7.30-test",
        "source_commit": "a" * 40,
        "source_tree": "b" * 40,
        "source_version": "0.7.30",
        "wheel_sha256": "c" * 64,
        "ignored": {"secret": "not public"},
    }
    (prefix.parent / "release-provenance.json").write_text(json.dumps(payload), encoding="utf-8")

    result = current_release_provenance(prefix)

    assert result == {
        "status": "available",
        "release_id": "0.7.30-test",
        "source_commit": "a" * 40,
        "source_tree": "b" * 40,
        "source_version": "0.7.30",
        "wheel_sha256": "c" * 64,
    }
