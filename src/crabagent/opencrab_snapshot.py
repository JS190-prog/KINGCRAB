from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional


SNAPSHOT_SCHEMA = "opencrab-inventory/1"


class SnapshotPersistenceError(RuntimeError):
    pass


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _canonical(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    return value


def _without_runtime_fields(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    value = copy.deepcopy(snapshot)
    value.pop("collected_at", None)
    value.pop("sync_attempted_at", None)
    value.pop("snapshot_hash", None)
    return value


def snapshot_hash(snapshot: Dict[str, Any]) -> str:
    encoded = json.dumps(_canonical(_without_runtime_fields(snapshot)), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _items(group: Any) -> Iterable[Dict[str, Any]]:
    if not isinstance(group, dict):
        return []
    value = group.get("items")
    return value if isinstance(value, list) else []


def _item_map(group: Any, key: str) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    for item in _items(group):
        identity = str(item.get(key) or "")
        if identity:
            result[identity] = item
    return result


def _field_delta(before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    fields: Dict[str, Dict[str, Any]] = {}
    for field in sorted(set(before) | set(after)):
        if field in {"content_hash", "collected_at", "snapshot_hash"}:
            continue
        old = before.get(field)
        new = after.get(field)
        if _canonical(old) != _canonical(new):
            fields[field] = {"before": old, "after": new}
    return fields


def diff_snapshots(previous: Optional[Dict[str, Any]], current: Dict[str, Any]) -> Dict[str, Any]:
    previous = previous or {}
    groups = {
        "packs": ("package_id", "pack"),
        "projects": ("project_id", "project"),
        "workflows": ("workflow_id", "workflow"),
    }
    result: Dict[str, Any] = {
        "schema": SNAPSHOT_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "from_hash": snapshot_hash(previous) if previous else None,
        "to_hash": snapshot_hash(current),
        "groups": {},
    }
    total = {"added": 0, "removed": 0, "changed": 0}
    for group, (identity_key, label) in groups.items():
        old_map = _item_map(previous.get(group), identity_key)
        new_map = _item_map(current.get(group), identity_key)
        added = sorted(set(new_map) - set(old_map))
        removed = sorted(set(old_map) - set(new_map))
        changed = []
        for identity in sorted(set(old_map) & set(new_map)):
            fields = _field_delta(old_map[identity], new_map[identity])
            if fields:
                changed.append({identity_key: identity, "fields": fields})
        result["groups"][group] = {
            "identity": identity_key,
            "label": label,
            "added": [{identity_key: identity} for identity in added],
            "removed": [{identity_key: identity} for identity in removed],
            "changed": changed,
        }
        total["added"] += len(added)
        total["removed"] += len(removed)
        total["changed"] += len(changed)
    result["summary"] = total
    result["has_changes"] = any(total.values())
    return result


def snapshot_paths(workspace: Path) -> Dict[str, Path]:
    root = workspace.resolve() / ".crabagent" / "opencrab"
    return {
        "root": root,
        "current": root / "current.json",
        "previous": root / "previous.json",
        "diff": root / "latest-diff.json",
        "manifest": root / "manifest.json",
    }


def _write_json(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def load_current(workspace: Path) -> Optional[Dict[str, Any]]:
    return load_json(snapshot_paths(workspace)["current"])


def load_diff(workspace: Path) -> Optional[Dict[str, Any]]:
    return load_json(snapshot_paths(workspace)["diff"])


def compact_snapshot(snapshot: Dict[str, Any], *, item_limit: int = 12) -> Dict[str, Any]:
    """Keep socket responses small; the complete inventory remains on disk."""
    result = {
        key: snapshot.get(key)
        for key in ("schema", "snapshot_hash", "collected_at", "sync_attempted_at", "sync_status", "sync_warnings", "status", "source", "access_scope", "display_scope", "scope_warning", "account", "ontology")
        if key in snapshot
    }
    for group in ("packs", "projects", "workflows"):
        value = snapshot.get(group) or {}
        items = value.get("items") if isinstance(value, dict) else []
        sample = []
        for item in (items if isinstance(items, list) else [])[:item_limit]:
            if not isinstance(item, dict):
                continue
            identity = item.get("package_id") or item.get("project_id") or item.get("workflow_id")
            label = item.get("title") or item.get("name") or "untitled"
            sample.append({"id": identity, "label": label, "status": item.get("status")})
        reported_total = value.get("total")
        local_total = len(items) if isinstance(items, list) else 0
        total = max(int(reported_total or 0), local_total) if reported_total is not None else local_total
        result[group] = {
            "status": value.get("status"),
            "total": total,
            "has_more": bool(value.get("has_more")),
            "error": value.get("error"),
            "items": sample,
        }
    return result


def compact_diff(diff: Dict[str, Any], *, item_limit: int = 20) -> Dict[str, Any]:
    """Return delta counts and samples; full delta IDs stay in latest-diff.json."""
    result = {key: diff.get(key) for key in ("schema", "generated_at", "from_hash", "to_hash", "summary", "has_changes") if key in diff}
    groups: Dict[str, Any] = {}
    for group, value in (diff.get("groups") or {}).items():
        identity = value.get("identity")
        groups[group] = {
            "identity": identity,
            "label": value.get("label"),
            "counts": {
                "added": len(value.get("added") or []),
                "removed": len(value.get("removed") or []),
                "changed": len(value.get("changed") or []),
            },
            "added": (value.get("added") or [])[:item_limit],
            "removed": (value.get("removed") or [])[:item_limit],
            "changed": (value.get("changed") or [])[:item_limit],
        }
    result["groups"] = groups
    return result


def persist_sync(workspace: Path, current: Dict[str, Any]) -> Dict[str, Any]:
    paths = snapshot_paths(workspace)
    previous = load_current(workspace)
    previous_packs = (previous or {}).get("packs") or {}
    previous_status = str(previous_packs.get("status") or "")
    previous_has_items = isinstance(previous_packs.get("items"), list)
    if not previous or (previous_status not in {"ok", "stale", ""} or not previous_has_items):
        previous = load_json(paths["previous"])
    current = copy.deepcopy(current)
    attempted_at = current.get("sync_attempted_at") or current.get("collected_at")
    current["sync_attempted_at"] = attempted_at
    warnings = []
    for group in ("packs", "projects", "workflows"):
        value = current.get(group) or {}
        status = str(value.get("status") or "")
        if status == "ok" or (not status and isinstance(value.get("items"), list)):
            continue
        old_value = (previous or {}).get(group) or {}
        old_items = old_value.get("items") if isinstance(old_value, dict) else None
        if previous and isinstance(old_items, list):
            preserved = copy.deepcopy(old_value)
            preserved["status"] = "stale"
            preserved["error"] = value.get("error") or "Remote inventory group was unavailable"
            preserved["stale_at"] = attempted_at
            current[group] = preserved
            warnings.append({"group": group, "error": preserved["error"]})
        elif group == "packs":
            raise SnapshotPersistenceError("Cannot initialize OpenCrab inventory while the pack list is unavailable")
    current["sync_status"] = "partial" if warnings else "complete"
    current["sync_warnings"] = warnings
    current["schema"] = SNAPSHOT_SCHEMA
    current["snapshot_hash"] = snapshot_hash(current)
    if previous:
        _write_json(paths["previous"], previous)
    diff = diff_snapshots(previous, current)
    _write_json(paths["current"], current)
    _write_json(paths["diff"], diff)
    manifest = {
        "schema": SNAPSHOT_SCHEMA,
        "snapshot_hash": current["snapshot_hash"],
        "collected_at": current.get("collected_at"),
        "sync_attempted_at": current.get("sync_attempted_at"),
        "access_scope": current.get("access_scope"),
        "scope_warning": current.get("scope_warning"),
        "counts": {
            group: len(list(_items(current.get(group))))
            for group in ("packs", "projects", "workflows")
        },
        "diff_summary": diff["summary"],
        "sync_status": current.get("sync_status"),
        "sync_warnings": current.get("sync_warnings") or [],
        "paths": {name: str(path) for name, path in paths.items() if name != "root"},
    }
    _write_json(paths["manifest"], manifest)
    return {
        "snapshot": current,
        "diff": diff,
        "paths": {name: str(path) for name, path in paths.items()},
    }


def local_snapshot_result(workspace: Path) -> Dict[str, Any]:
    paths = snapshot_paths(workspace)
    snapshot = load_current(workspace)
    if snapshot is None:
        return {"status": "unavailable", "payload": {}, "error": "OpenCrab local snapshot is not initialized.", "paths": {name: str(path) for name, path in paths.items()}}
    return {"status": "ok", "source": "local_snapshot", "payload": snapshot, "paths": {name: str(path) for name, path in paths.items()}}


def local_diff_result(workspace: Path) -> Dict[str, Any]:
    paths = snapshot_paths(workspace)
    diff = load_diff(workspace)
    if diff is None:
        return {"status": "unavailable", "diff": {}, "error": "OpenCrab local diff is not initialized.", "paths": {name: str(path) for name, path in paths.items()}}
    return {"status": "ok", "source": "local_snapshot", "diff": diff, "paths": {name: str(path) for name, path in paths.items()}}
