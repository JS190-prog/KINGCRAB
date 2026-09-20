import hashlib
import os

import pytest

from crabagent.host_artifacts import normalize_artifact_paths, probe_host_artifacts


@pytest.mark.parametrize("paths", [None, [], [".env"], [".crabagent/artifacts/../secret.txt"],
    [".crabagent/artifacts/nested/a.txt"], [".crabagent/artifacts/mission-a.txt"],
    [".crabagent/artifacts/a.exe"], [".crabagent/artifacts/a.txt"] * 2, [1]])
def test_probe_reuses_writer_scope_and_rejects_invalid_paths(paths):
    with pytest.raises(ValueError):
        normalize_artifact_paths(paths)


requires_dirfd = pytest.mark.skipif(os.open not in os.supports_dir_fd, reason="POSIX runtime probe")


@requires_dirfd
def test_probe_reads_fresh_bytes_without_creating_or_changing_files(tmp_path):
    path = ".crabagent/artifacts/probe.txt"
    absent = probe_host_artifacts(tmp_path, [path])
    assert absent["files"] == [{"relative_path": path, "exists": False}]
    assert not (tmp_path / ".crabagent").exists()
    target = tmp_path / path
    target.parent.mkdir(parents=True)
    target.write_bytes(b"first\n")
    result = probe_host_artifacts(tmp_path, [path])
    row = result["files"][0]
    assert result["read_only"] is True
    assert row["sha256"] == hashlib.sha256(b"first\n").hexdigest()
    assert row["bytes"] == 6 and row["lf_count"] == row["trailing_lf_count"] == 1
    assert row["cr_count"] == 0 and row["utf8_valid"] is True and row["utf8_bom"] is False
    assert "content" not in row
    target.write_bytes(b"second\r\n")
    changed = probe_host_artifacts(tmp_path, [path])["files"][0]
    assert changed["sha256"] != row["sha256"] and changed["cr_count"] == 1
    assert target.read_bytes() == b"second\r\n"


@requires_dirfd
@pytest.mark.parametrize("kind", ["leaf_symlink", "root_symlink", "hardlink", "directory", "oversize", "fifo"])
def test_probe_never_follows_links_or_reads_unbounded_or_special_files(tmp_path, kind):
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"private")
    root = tmp_path / ".crabagent/artifacts"
    root.parent.mkdir(parents=True)
    if kind != "root_symlink":
        root.mkdir()
    target = root / "probe.txt"
    if kind == "leaf_symlink":
        target.symlink_to(outside)
    elif kind == "root_symlink":
        other = tmp_path / "other"
        other.mkdir()
        root.symlink_to(other, target_is_directory=True)
    elif kind == "hardlink":
        os.link(outside, target)
    elif kind == "directory":
        target.mkdir()
    elif kind == "oversize":
        target.write_bytes(b"a" * (16 * 1024 + 1))
    else:
        os.mkfifo(target)
    with pytest.raises((ValueError, OSError)):
        probe_host_artifacts(tmp_path, [".crabagent/artifacts/probe.txt"])
    assert outside.read_bytes() == b"private"


@requires_dirfd
def test_daemon_probe_dispatch_requires_no_mission_or_store(tmp_path):
    from types import SimpleNamespace
    from crabagent.daemon import RUNTIME_CAPABILITIES, RuntimeServer

    assert "artifact.probe" in RUNTIME_CAPABILITIES
    result = RuntimeServer.dispatch(SimpleNamespace(workspace=tmp_path), {
        "action": "artifact.probe", "payload": {"paths": [".crabagent/artifacts/new.txt"]},
    })
    assert result["files"] == [{"relative_path": ".crabagent/artifacts/new.txt", "exists": False}]
    assert not (tmp_path / ".crabagent").exists()
