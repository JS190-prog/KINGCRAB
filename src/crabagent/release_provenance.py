"""Read the bounded release provenance written by the canonical deployer."""

from __future__ import annotations

import json
from pathlib import Path
import re
import sys
from typing import Any, Dict


_FIELDS = (
    "release_id",
    "source_commit",
    "source_tree",
    "source_version",
    "source_package_tree_sha256",
    "wheel_sha256",
    "wheel_name",
)


def current_release_provenance(prefix: str | Path | None = None) -> Dict[str, Any]:
    release_root = Path(prefix or sys.prefix).resolve().parent
    path = release_root / "release-provenance.json"
    try:
        if path.stat().st_size > 32 * 1024:
            raise ValueError("release provenance exceeds the bounded size")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {"status": "unavailable"}
    if not isinstance(value, dict):
        return {"status": "unavailable"}
    result: Dict[str, Any] = {"status": "available"}
    for key in _FIELDS:
        item = value.get(key)
        if isinstance(item, str) and 0 < len(item) <= 256 and "\x00" not in item:
            result[key] = item
    for key in ("source_commit", "source_tree"):
        if key in result and not re.fullmatch(r"[0-9a-fA-F]{40}", str(result[key])):
            return {"status": "unavailable"}
    for key in ("source_package_tree_sha256", "wheel_sha256"):
        if key in result and not re.fullmatch(r"[0-9a-fA-F]{64}", str(result[key])):
            return {"status": "unavailable"}
    return result
