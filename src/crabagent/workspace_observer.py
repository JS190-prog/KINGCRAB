from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, List


_EXCLUDED_DIRS = {
    ".crabagent",
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    ".venv",
    "venv",
    "dist",
    "build",
}
_MAX_HASH_BYTES = 8 * 1024 * 1024
_MAX_CHANGED_ROWS = 200


def _file_fingerprint(path: Path) -> str:
    try:
        stat = path.stat()
        if stat.st_size > _MAX_HASH_BYTES:
            return "large:%s:%s" % (stat.st_size, stat.st_mtime_ns)
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return "sha256:%s" % digest.hexdigest()
    except OSError as exc:
        return "unreadable:%s:%s" % (type(exc).__name__, exc)


def capture_workspace(root: Path) -> Dict[str, Any]:
    """Capture a bounded source-tree manifest without reading runtime state.

    Runtime artifacts, VCS internals and common dependency/build directories
    are deliberately excluded. The manifest is used as an observed change
    receipt, never as a claim about what a model said it changed.
    """
    root = root.resolve()
    files: Dict[str, str] = {}
    if root.exists():
        for current, directories, filenames in os.walk(root, followlinks=False):
            directories[:] = sorted(name for name in directories if name not in _EXCLUDED_DIRS)
            for name in sorted(filenames):
                path = Path(current) / name
                try:
                    relative = path.relative_to(root).as_posix()
                except ValueError:
                    continue
                if path.is_symlink():
                    try:
                        files[relative] = "symlink:%s" % os.readlink(path)
                    except OSError as exc:
                        files[relative] = "symlink-error:%s" % exc
                else:
                    files[relative] = _file_fingerprint(path)
    manifest = hashlib.sha256(
        json.dumps(sorted(files.items()), ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {"root": str(root), "file_count": len(files), "files": files, "manifest": manifest}


def diff_workspace(before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, Any]:
    before_files = before.get("files") if isinstance(before.get("files"), dict) else {}
    after_files = after.get("files") if isinstance(after.get("files"), dict) else {}
    rows: List[Dict[str, Any]] = []
    for relative in sorted(set(before_files) | set(after_files)):
        previous = before_files.get(relative)
        current = after_files.get(relative)
        if previous == current:
            continue
        status = "added" if previous is None else "deleted" if current is None else "modified"
        rows.append({"path": relative, "status": status, "before": previous, "after": current})
    added = sum(1 for row in rows if row["status"] == "added")
    modified = sum(1 for row in rows if row["status"] == "modified")
    deleted = sum(1 for row in rows if row["status"] == "deleted")
    return {
        "schema": "crab.workspace-change-receipt/v1",
        "before_manifest": before.get("manifest"),
        "after_manifest": after.get("manifest"),
        "before_file_count": int(before.get("file_count") or 0),
        "after_file_count": int(after.get("file_count") or 0),
        "changed_file_count": len(rows),
        "added_count": added,
        "modified_count": modified,
        "deleted_count": deleted,
        "changed_files": rows[:_MAX_CHANGED_ROWS],
        "changed_files_truncated": len(rows) > _MAX_CHANGED_ROWS,
        "observed": True,
    }
