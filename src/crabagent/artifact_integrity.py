"""Bounded, read-only integrity readback for durable mission artifacts."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import stat
from typing import Any, Dict


MAX_ARTIFACTS = 256
MAX_ARTIFACT_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _hash_stable_file(path: Path) -> tuple[int, str]:
    if path.is_symlink():
        raise ValueError("artifact path is a symbolic link")
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError("artifact must be a single-link regular file")
        if before.st_size > MAX_ARTIFACT_BYTES:
            raise ValueError("artifact exceeds the per-file integrity limit")
        digest = hashlib.sha256()
        observed = 0
        while True:
            chunk = stream.read(128 * 1024)
            if not chunk:
                break
            observed += len(chunk)
            digest.update(chunk)
        after = os.fstat(stream.fileno())
    current = path.stat()
    def identity(value: os.stat_result) -> tuple[int, ...]:
        fields = (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
        # On Windows, ``Path.stat`` may report creation-time precision
        # differently from ``fstat`` for the same unchanged handle.  File ID,
        # size and last-write time still give a stable replacement/write guard.
        return fields if os.name == "nt" else (*fields, value.st_ctime_ns)
    if observed != before.st_size or identity(before) != identity(after) or identity(after) != identity(current):
        raise ValueError("artifact changed during integrity readback")
    return observed, digest.hexdigest()


def verify_mission_artifacts(store: Any, mission_id: str) -> Dict[str, Any]:
    mission_id = str(mission_id or "").strip()
    if not mission_id:
        raise ValueError("mission_id is required")
    snapshot = store.inspect(mission_id)
    artifacts = snapshot.get("artifacts") or []
    if not isinstance(artifacts, list) or len(artifacts) > MAX_ARTIFACTS:
        raise ValueError("mission artifact count exceeds the integrity limit")
    mission_root = (Path(store.artifacts_dir) / mission_id).resolve()
    rows = []
    total_bytes = 0
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        row = {
            key: artifact.get(key)
            for key in ("artifact_id", "kind", "status", "created_at")
            if artifact.get(key) is not None
        }
        stored_sha256 = str(artifact.get("sha256") or "").lower()
        row["stored_sha256"] = stored_sha256
        try:
            raw_path = Path(str(artifact.get("path") or ""))
            if not raw_path.is_absolute():
                raise ValueError("artifact path must be absolute")
            if raw_path.is_symlink():
                raise ValueError("artifact path is a symbolic link")
            target = raw_path.resolve(strict=True)
            if not _within(target, mission_root):
                raise ValueError("artifact path escapes the mission artifact root")
            size, observed_sha256 = _hash_stable_file(target)
            total_bytes += size
            if total_bytes > MAX_TOTAL_BYTES:
                raise ValueError("mission artifacts exceed the total integrity limit")
            row.update(
                {
                    "exists": True,
                    "bytes": size,
                    "observed_sha256": observed_sha256,
                    "hash_matches": observed_sha256 == stored_sha256,
                    "read_status": "verified",
                }
            )
        except FileNotFoundError:
            row.update({"exists": False, "hash_matches": False, "read_status": "missing"})
        except (OSError, ValueError) as exc:
            row.update(
                {
                    "exists": bool(str(artifact.get("path") or "")),
                    "hash_matches": False,
                    "read_status": "blocked",
                    "error": str(exc)[:300],
                }
            )
        rows.append(row)
    match_count = sum(1 for row in rows if row.get("hash_matches") is True)
    mismatch_count = len(rows) - match_count
    return {
        "status": "ok" if mismatch_count == 0 else "mismatch",
        "read_only": True,
        "source": "live_runtime_artifact_bytes",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "mission_id": mission_id,
        "artifact_count": len(rows),
        "original_bytes_read_count": sum(1 for row in rows if row.get("read_status") == "verified"),
        "hash_match_count": match_count,
        "hash_mismatch_count": mismatch_count,
        "total_bytes": total_bytes,
        "limits": {
            "max_artifacts": MAX_ARTIFACTS,
            "max_artifact_bytes": MAX_ARTIFACT_BYTES,
            "max_total_bytes": MAX_TOTAL_BYTES,
        },
        "artifacts": rows,
    }
