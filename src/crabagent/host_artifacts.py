"""Fresh, bounded readback for the host WORKER's exclusive-create artifacts."""

import hashlib
import json
import os
import stat
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from .colony import _parse_host_worker_artifact_directive


MAX_BYTES = 16 * 1024


def normalize_artifact_paths(paths: Any) -> List[str]:
    if not isinstance(paths, list) or not 1 <= len(paths) <= 8:
        raise ValueError("artifact probe requires 1 to 8 paths")
    normalized = []
    for path in paths:
        if not isinstance(path, str):
            raise ValueError("artifact probe paths must be strings")
        # Reuse the actual writer's path contract, including Unicode handling.
        _, spec = _parse_host_worker_artifact_directive(
            "HOST_WORKER_ARTIFACT_V1:" + json.dumps({"relative_path": path, "content": "probe"})
        )
        normalized.append(spec["relative_path"])
    if len(set(normalized)) != len(normalized):
        raise ValueError("artifact probe paths must be distinct")
    return normalized


def probe_host_artifacts(workspace: Path, paths: Any) -> Dict[str, Any]:
    normalized = normalize_artifact_paths(paths)
    if os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"):
        raise ValueError("secure artifact probe requires directory descriptor support")
    rows = []
    with ExitStack() as handles:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

        def open_dir(name, parent=None):
            descriptor = os.open(name, flags, dir_fd=parent)
            handles.callback(os.close, descriptor)
            return descriptor

        workspace_fd = open_dir(workspace)
        try:
            runtime_fd = open_dir(".crabagent", workspace_fd)
            artifact_fd = open_dir("artifacts", runtime_fd)
        except FileNotFoundError:
            artifact_fd = None
        for path in normalized:
            row = {"relative_path": path, "exists": False}
            rows.append(row)
            if artifact_fd is None:
                continue
            name = path.rsplit("/", 1)[1]
            try:
                descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=artifact_fd)
            except FileNotFoundError:
                continue
            with os.fdopen(descriptor, "rb") as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                    raise ValueError("artifact probe requires a single-link regular file")
                if before.st_size > MAX_BYTES:
                    raise ValueError("artifact probe exceeds 16 KiB")
                content = stream.read(MAX_BYTES + 1)
                after = os.fstat(stream.fileno())
                current = os.stat(name, dir_fd=artifact_fd, follow_symlinks=False)
                identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                if len(content) > MAX_BYTES or identity(before) != identity(after) or identity(after) != identity(current):
                    raise ValueError("artifact changed during readback")
            try:
                content.decode("utf-8")
                utf8_valid = True
            except UnicodeDecodeError:
                utf8_valid = False
            row.update({
                "exists": True,
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "utf8_valid": utf8_valid,
                "utf8_bom": content.startswith(b"\xef\xbb\xbf"),
                "lf_count": content.count(b"\n"),
                "cr_count": content.count(b"\r"),
                "trailing_lf_count": len(content) - len(content.rstrip(b"\n")),
            })
    return {
        "status": "ok",
        "read_only": True,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "workspace": str(workspace),
        "path_contract": {
            "root": ".crabagent/artifacts",
            "direct_children_only": True,
            "write_mode": "exclusive_create",
            "max_bytes": MAX_BYTES,
        },
        "files": rows,
    }
