from pathlib import Path

from crabagent.opencrab_snapshot import diff_snapshots, load_diff, load_current, persist_sync


def inventory(pack_title: str, *, collected_at: str) -> dict:
    return {
        "schema": "opencrab-inventory/1",
        "collected_at": collected_at,
        "access_scope": "connected_account",
        "packs": {"items": [{"package_id": "pack-1", "title": pack_title}]},
        "projects": {"items": [{"project_id": "project-1", "name": "ALEXAI"}]},
        "workflows": {"items": [{"workflow_id": "workflow-1", "name": "Research"}]},
    }


def test_diff_ignores_collection_time_and_reports_only_delta() -> None:
    before = inventory("Old", collected_at="2026-08-01T00:00:00+00:00")
    after = inventory("New", collected_at="2026-08-01T01:00:00+00:00")

    delta = diff_snapshots(before, after)

    assert delta["summary"] == {"added": 0, "removed": 0, "changed": 1}
    assert delta["groups"]["packs"]["changed"][0]["package_id"] == "pack-1"
    assert delta["groups"]["packs"]["changed"][0]["fields"]["title"]["before"] == "Old"


def test_persist_sync_keeps_previous_current_and_diff_files(tmp_path: Path) -> None:
    persist_sync(tmp_path, inventory("Old", collected_at="2026-08-01T00:00:00+00:00"))
    result = persist_sync(tmp_path, inventory("New", collected_at="2026-08-01T01:00:00+00:00"))

    assert load_current(tmp_path)["packs"]["items"][0]["title"] == "New"
    assert (tmp_path / ".crabagent/opencrab/previous.json").exists()
    assert load_diff(tmp_path)["summary"]["changed"] == 1
    assert result["diff"]["has_changes"] is True


def test_persist_sync_preserves_last_good_group_when_remote_group_is_unavailable(tmp_path: Path) -> None:
    good = inventory("Old", collected_at="2026-08-01T00:00:00+00:00")
    good["packs"]["status"] = "ok"
    persist_sync(tmp_path, good)
    attempted = inventory("Ignored", collected_at="2026-08-01T00:01:00+00:00")
    attempted["packs"] = {"status": "unavailable", "items": [], "error": "timeout"}

    result = persist_sync(tmp_path, attempted)

    assert result["snapshot"]["sync_status"] == "partial"
    assert result["snapshot"]["packs"]["status"] == "stale"
    assert result["snapshot"]["packs"]["items"][0]["package_id"] == "pack-1"
    assert result["diff"]["summary"] == {"added": 0, "removed": 0, "changed": 0}
