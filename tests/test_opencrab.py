from pathlib import Path
from typing import Any, Dict
from urllib.error import URLError
import json
import time

import crabagent.opencrab as opencrab
import pytest


class FakeClient:
    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        if name == "opencrab_status":
            return {
                "status": "ok",
                "tier": "enterprise",
                "auth_mode": "mcp_key",
                "scopes": ["read_graph"],
                "admin_all_customers": True,
                "documents": 0,
                "chunks": 0,
                "nodes": 0,
                "edges": 0,
            }
        if name == "opencrab_search_packs":
            assert arguments == {"limit": 30}
            return {"total": 12, "has_more": False, "packs": [{"package_id": "pack-1", "title": "Fable5xGLM5.2", "category": "agent"}]}
        if name == "opencrab_project_manage":
            assert arguments == {"action": "list"}
            return {"total": 1, "projects": [{"project_id": "project-1", "name": "ALEXAI", "package_count": 87}]}
        if name == "opencrab_list_workflows":
            assert arguments == {"status": "any"}
            return {"total": 1, "workflows": [{"workflow_id": "workflow-1", "name": "Research", "status": "active"}]}
        raise AssertionError(name)


def test_opencrab_inspector_uses_bounded_read_only_normalized_data(monkeypatch) -> None:
    monkeypatch.setattr(opencrab, "mcp_inventory", lambda: [{"name": "OpenCrab", "state": "enabled"}])
    snapshot = opencrab.OpenCrabInspector(client_factory=lambda: FakeClient()).refresh()

    assert snapshot["access_scope"] == "admin_all_customers"
    assert snapshot["account"]["tier"] == "enterprise"
    assert snapshot["packs"]["items"][0]["title"] == "Fable5xGLM5.2"
    assert snapshot["projects"]["items"][0]["package_count"] == 87
    assert snapshot["workflows"]["items"][0]["status"] == "active"


class LiveCatalogClient:
    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        if name == "opencrab_status":
            return {"status": "ok", "tier": "enterprise", "auth_mode": "mcp_key", "scopes": ["query"], "admin_all_customers": False}
        if name == "opencrab_project_manage":
            assert arguments == {"action": "list", "limit": 25}
            return {
                "status": "ok",
                "total": 1,
                "truncated": False,
                "workspace_scope": "connected",
                "projects": [
                    {
                        "project_id": "project-live",
                        "name": "ALEXAI",
                        "package_count": 1,
                        "packages": [{"package_id": "pack-live", "title": "Fable5xGLM5.2", "origin": "personal"}],
                    }
                ],
            }
        raise AssertionError(name)


def test_live_catalog_uses_workspace_projects_and_embedded_packs_without_full_pack_search(monkeypatch) -> None:
    monkeypatch.setattr(opencrab, "mcp_inventory", lambda: [{"name": "OpenCrab", "state": "enabled"}])
    inspector = opencrab.OpenCrabInspector(client_factory=lambda: LiveCatalogClient())
    catalog = inspector.catalog()

    assert catalog["mode"] == "live_catalog"
    assert catalog["projects"]["total"] == 1
    assert catalog["packs"]["total"] == 1
    assert catalog["packs"]["items"][0]["project_name"] == "ALEXAI"


def test_ambient_scope_resolver_simplifies_goal_and_ranks_visible_packs(monkeypatch, tmp_path: Path) -> None:
    class ScopeClient:
        def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
            if name == "opencrab_status":
                return {"status": "ok", "tier": "enterprise", "admin_all_customers": False}
            if name == "opencrab_project_manage":
                assert arguments == {"action": "list", "limit": 25, "query": "사업 전략"}
                return {
                    "status": "ok",
                    "projects": [
                        {
                            "project_id": "strategy-project",
                            "name": "ALEXAI",
                            "packages": [
                                {"package_id": "pack-generic", "title": "General Notes", "description": "misc"},
                                {"package_id": "pack-strategy", "title": "사업 전략 설계", "description": "사업 모델과 전략"},
                                {"package_id": "pack-duplicate", "title": "사업 전략 설계 duplicate", "duplicate_of": "pack-strategy"},
                                {"package_id": "pack-derived", "title": "사업 전략 설계 derived", "metadata": {"derived_from": "pack-strategy"}},
                                {"package_id": "pack-fable", "title": "Fable5xGLM5.2", "description": "agent context"},
                            ],
                        }
                    ],
                }
            raise AssertionError(name)

    monkeypatch.setattr(opencrab, "mcp_inventory", lambda: [{"name": "OpenCrab", "state": "enabled"}])
    result = opencrab.resolve_opencrab_package_scope(
        "내 사업 전략을 추천해줘",
        workspace=tmp_path,
        client_factory=lambda: ScopeClient(),
        max_packages=2,
    )

    assert result["catalog_queries"] == ["사업 전략"]
    assert result["catalog_status"] == "ok"
    assert result["scope_mode"] == "workspace_auto_resolved"
    assert result["selected_package_ids"] == ["pack-strategy", "pack-fable"]
    assert result["selected_titles"] == ["사업 전략 설계", "Fable5xGLM5.2"]
    assert result["candidate_count"] == 5
    assert result["excluded_noncanonical_count"] == 2


def test_default_scope_excludes_same_source_derived_pack(monkeypatch, tmp_path: Path) -> None:
    class ScopeClient:
        def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
            if name == "opencrab_status":
                return {"status": "ok", "tier": "enterprise", "admin_all_customers": False}
            if name == "opencrab_project_manage":
                return {
                    "status": "ok",
                    "projects": [{
                        "project_id": "project-1",
                        "packages": [
                            {"package_id": "pack-canonical", "title": "원문 전략", "source_package_id": "pack-canonical"},
                            {"package_id": "pack-derived", "title": "파생 전략", "lineage": {"source_package_id": "pack-canonical"}},
                        ],
                    }],
                }
            raise AssertionError(name)

    monkeypatch.setattr(opencrab, "mcp_inventory", lambda: [{"name": "OpenCrab", "state": "enabled"}])
    result = opencrab.resolve_opencrab_package_scope(
        "전략을 찾아줘",
        workspace=tmp_path,
        client_factory=lambda: ScopeClient(),
    )

    assert result["selected_package_ids"] == ["pack-canonical"]
    assert result["excluded_noncanonical_count"] == 1


class CompleteLiveCatalogClient:
    def __init__(self) -> None:
        self.calls = []

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        self.calls.append((name, dict(arguments)))
        if name == "opencrab_status":
            return {"status": "ok", "tier": "enterprise", "admin_all_customers": True}
        if name == "opencrab_project_manage":
            return {
                "status": "ok",
                "total": 1,
                "truncated": False,
                "projects": [
                    {
                        "project_id": "project-complete",
                        "name": "ALEXAI",
                        "linked_workspace_ids": ["workspace-one"],
                        "packages": [
                            {"package_id": "pack-embedded", "title": "Embedded", "workspace_id": "workspace-two"},
                        ],
                    }
                ],
            }
        if name == "opencrab_search_packs":
            workspace_id = arguments["workspace_id"]
            return {
                "status": "ok",
                "total": 2,
                "has_more": False,
                "packs": [
                    {"package_id": "pack-embedded", "title": "Embedded full", "workspace_id": workspace_id},
                    {"package_id": "pack-%s" % workspace_id[-1], "title": "Workspace %s" % workspace_id[-1], "workspace_id": workspace_id},
                ],
            }
        raise AssertionError(name)


def test_complete_catalog_hydrates_linked_workspaces_and_reuses_persistent_cache(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(opencrab, "mcp_inventory", lambda: [{"name": "OpenCrab", "state": "enabled"}])
    first_client = CompleteLiveCatalogClient()
    first = opencrab.OpenCrabInspector(client_factory=lambda: first_client, workspace=tmp_path).catalog(complete=True)

    assert first["complete"] is True
    assert first["linked_workspace_count"] == 2
    assert first["hydration"]["fetched"] == 2
    assert first["hydration"]["errors"] == []
    assert first["packs"]["total"] == 3
    assert (tmp_path / ".crabagent" / "opencrab" / "live-catalog-cache.json").exists()
    assert (tmp_path / ".crabagent" / "opencrab" / "workspace-pack-cache.json").exists()

    second_client = CompleteLiveCatalogClient()
    cached = opencrab.OpenCrabInspector(client_factory=lambda: second_client, workspace=tmp_path).catalog(complete=True)
    assert cached["cache_hit"] is True
    assert cached["packs"]["total"] == 3
    assert second_client.calls == []


def test_partial_catalog_does_not_replace_complete_catalog_cache(tmp_path: Path) -> None:
    inspector = opencrab.OpenCrabInspector(workspace=tmp_path)
    inspector._write_catalog_cache(
        {"status": "ok", "complete": True, "packs": {"total": 389}},
        query="",
        complete=True,
    )
    inspector._write_catalog_cache(
        {"status": "ok", "complete": False, "packs": {"total": 259}},
        query="",
        complete=False,
    )

    cached = json.loads(
        (tmp_path / ".crabagent" / "opencrab" / "live-catalog-cache.json").read_text(encoding="utf-8")
    )
    assert cached["complete"] is True
    assert cached["payload"]["packs"]["total"] == 389


class AdminLiveCatalogClient:
    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        if name == "opencrab_status":
            return {"status": "ok", "tier": "enterprise", "admin_all_customers": True}
        if name == "opencrab_project_manage":
            assert arguments == {"action": "list", "limit": 25}
            return {
                "status": "ok",
                "total": 1,
                "truncated": False,
                "projects": [
                    {
                        "project_id": "project-live-admin",
                        "name": "ALEXAI",
                        "packages": [
                            {"package_id": "pack-personal", "title": "Created here", "license_scope": "personal"},
                            {"package_id": "pack-purchased", "title": "Installed here", "license_scope": "purchased_public"},
                            {"package_id": "pack-external", "title": "External commercial", "license_scope": "commercial"},
                        ],
                    }
                ],
            }
        raise AssertionError(name)


def test_live_admin_catalog_keeps_all_packs_from_the_linked_workspace(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(opencrab, "mcp_inventory", lambda: [{"name": "OpenCrab", "state": "enabled"}])
    inspector = opencrab.OpenCrabInspector(client_factory=lambda: AdminLiveCatalogClient(), workspace=tmp_path)
    catalog = inspector.catalog()

    assert [row["package_id"] for row in catalog["packs"]["items"]] == ["pack-personal", "pack-external", "pack-purchased"]
    assert catalog["projects"]["total"] == 1


class FallbackLiveCatalogClient:
    def __init__(self) -> None:
        self.calls = []

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        if name == "opencrab_status":
            return {"status": "ok", "tier": "enterprise", "admin_all_customers": True}
        if name == "opencrab_project_manage":
            self.calls.append(arguments)
            if "query" not in arguments:
                return {"status": "error", "detail": "canceling statement due to statement timeout"}
            return {
                "status": "ok",
                "total": 1,
                "projects": [{"project_id": "fallback-project", "name": "ALEXAI", "packages": [{"package_id": "fallback-pack", "title": "Fallback", "license_scope": "personal"}] }],
            }
        raise AssertionError(name)


def test_live_catalog_falls_back_to_bounded_account_query_after_workspace_timeout(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(opencrab, "mcp_inventory", lambda: [{"name": "OpenCrab", "state": "enabled"}])
    client = FallbackLiveCatalogClient()
    catalog = opencrab.OpenCrabInspector(client_factory=lambda: client, workspace=tmp_path).catalog()

    assert catalog["mode"] == "live_catalog_fallback"
    assert catalog["requested_query"] == ""
    assert catalog["query"] == "ALEXAI"
    assert catalog["packs"]["total"] == 1
    assert client.calls == [{"action": "list", "limit": 25}, {"action": "list", "limit": 20, "query": "ALEXAI"}]


def test_live_catalog_uses_recent_stale_cache_when_statement_timeout_repeats(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(opencrab, "mcp_inventory", lambda: [{"name": "OpenCrab", "state": "enabled"}])
    cache_path = tmp_path / ".crabagent" / "opencrab" / "live-catalog-cache.json"
    cache_path.parent.mkdir(parents=True)
    payload = {
        "status": "ok",
        "mode": "live_catalog",
        "complete": True,
        "display_scope": "admin_personal",
        "account": {"tier": "enterprise"},
        "projects": {"total": 1, "items": [{"project_id": "cached-project", "name": "ALEXAI"}]},
        "packs": {"total": 1, "items": [{"package_id": "cached-pack", "title": "Cached Fable5xGLM5.2"}]},
        "scope_warning": "cached scope",
    }
    cache_path.write_text(
        json.dumps({"query": "ALEXAI", "complete": True, "cached_at": time.time() - 3600, "payload": payload}),
        encoding="utf-8",
    )
    (cache_path.parent / "workspace-pack-cache.json").write_text(
        json.dumps(
            {
                "workspaces": {
                    "workspace-cached": {
                        "cached_at": time.time() - 3600,
                        "packs": [
                            {"package_id": "cached-pack", "title": "Cached Fable5xGLM5.2"},
                            {"package_id": "cached-pack-2", "title": "Cached companion pack"},
                        ],
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    class TimeoutClient:
        def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
            if name == "opencrab_status":
                return {"status": "ok", "tier": "enterprise", "admin_all_customers": True}
            if name == "opencrab_project_manage":
                return {"status": "error", "detail": "canceling statement due to statement timeout"}
            raise AssertionError(name)

    catalog = opencrab.OpenCrabInspector(
        client_factory=lambda: TimeoutClient(),
        workspace=tmp_path,
    ).catalog(query="ALEXAI", force=True, complete=True)

    assert catalog["mode"] == "live_catalog_stale"
    assert catalog["status"] == "stale"
    assert catalog["cache_status"] == "stale"
    assert catalog["packs"]["total"] == 2
    assert catalog["stale_pack_cache_merged"] is True
    assert "statement timeout" in catalog["catalog_error"]


def test_live_catalog_prefer_stale_returns_without_remote_round_trip(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(opencrab, "mcp_inventory", lambda: [{"name": "OpenCrab", "state": "enabled"}])
    cache_path = tmp_path / ".crabagent" / "opencrab" / "live-catalog-cache.json"
    cache_path.parent.mkdir(parents=True)
    payload = {
        "status": "ok",
        "mode": "live_catalog",
        "complete": True,
        "display_scope": "admin_personal",
        "account": {"tier": "enterprise"},
        "projects": {"total": 1, "items": [{"project_id": "cached-project", "name": "ALEXAI"}]},
        "packs": {"total": 1, "items": [{"package_id": "cached-pack", "title": "Cached Fable5xGLM5.2"}]},
    }
    cache_path.write_text(
        json.dumps({"query": "", "complete": False, "cached_at": time.time() - 3600, "payload": payload}),
        encoding="utf-8",
    )

    class NoRoundTripClient:
        def __init__(self) -> None:
            self.calls = []

        def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
            self.calls.append((name, arguments))
            raise AssertionError("prefer_stale must not contact OpenCrab before the UI is rendered")

    client = NoRoundTripClient()
    catalog = opencrab.OpenCrabInspector(client_factory=lambda: client, workspace=tmp_path).catalog(
        prefer_stale=True,
    )

    assert catalog["mode"] == "live_catalog_stale"
    assert catalog["refresh_deferred"] is True
    assert catalog["stale_reason"] == "cached catalog shown while live refresh runs"
    assert catalog["packs"]["items"][0]["title"] == "Cached Fable5xGLM5.2"
    assert client.calls == []


def test_opencrab_transport_errors_do_not_expose_endpoint_credentials() -> None:
    secret = "mcp_key=do-not-render"

    def failing_opener(*args: Any, **kwargs: Any) -> Any:
        raise URLError("https://example.test/mcp?%s" % secret)

    client = opencrab.OpenCrabMcpClient(
        "https://example.test/mcp?%s" % secret,
        opener=failing_opener,
    )

    with pytest.raises(opencrab.OpenCrabUnavailable) as caught:
        client.initialize()

    assert str(caught.value) == "OpenCrab MCP request failed"
    assert secret not in str(caught.value)


class EventStreamResponse:
    def __init__(self, payload: str) -> None:
        self.headers = {"Content-Type": "text/event-stream", "Mcp-Session-Id": "session-1"}
        self.lines = [b"event: message\n", ("data: %s\n" % payload).encode("utf-8"), b"\n"]

    def readline(self) -> bytes:
        return self.lines.pop(0) if self.lines else b""

    def read(self) -> bytes:
        raise AssertionError("SSE response should not call read()")

    def close(self) -> None:
        return None


def test_streamable_http_reads_one_sse_event_without_waiting_for_stream_close() -> None:
    def opener(*args: Any, **kwargs: Any) -> EventStreamResponse:
        return EventStreamResponse('{"result":{"status":"ok"}}')

    client = opencrab.OpenCrabMcpClient("https://example.test/mcp", opener=opener)
    assert client._post("initialize", {}) == {"status": "ok"}
    assert client.session_id == "session-1"


class KeepAliveJsonResponse:
    def __init__(self, payload: str) -> None:
        self.headers = {"Content-Type": "application/json"}
        self.chunks = [payload[:3].encode("utf-8"), payload[3:].encode("utf-8")]
        self.read_called = False

    def read1(self, size: int) -> bytes:
        return self.chunks.pop(0) if self.chunks else b""

    def read(self, size: int = -1) -> bytes:
        self.read_called = True
        raise AssertionError("keep-alive JSON should use read1()")

    def close(self) -> None:
        return None


def test_json_response_stops_after_complete_message_without_waiting_for_keep_alive_close() -> None:
    response = KeepAliveJsonResponse('{"result":{"status":"ok"}}')
    client = opencrab.OpenCrabMcpClient("https://example.test/mcp", opener=lambda *args, **kwargs: response)

    assert client._post("initialize", {}) == {"status": "ok"}
    assert response.read_called is False


class FullInventoryClient:
    def __init__(self) -> None:
        self.pack_calls = []

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        if name == "opencrab_status":
            return {"status": "ok", "tier": "enterprise", "auth_mode": "mcp_key", "scopes": ["read_graph"], "admin_all_customers": True}
        if name == "opencrab_search_packs":
            self.pack_calls.append(arguments)
            if not arguments.get("cursor"):
                return {"total": 2, "has_more": True, "next_cursor": "1", "packs": [{"package_id": "pack-1", "title": "One", "version": "1"}]}
            return {"total": 2, "has_more": False, "packs": [{"package_id": "pack-2", "title": "Two", "version": "1"}]}
        if name == "opencrab_project_manage":
            assert arguments == {"action": "list"}
            return {"total": 1, "projects": [{"project_id": "project-1", "name": "ALEXAI", "packages": [{"package_id": "pack-1", "title": "One"}]}]}
        if name == "opencrab_list_workflows":
            assert arguments == {"status": "any"}
            return {"total": 1, "workflows": [{"workflow_id": "workflow-1", "name": "Research", "status": "active", "steps": []}]}
        raise AssertionError(name)


def test_opencrab_full_refresh_exhausts_pack_cursor_and_keeps_relationship_metadata(monkeypatch) -> None:
    monkeypatch.setattr(opencrab, "mcp_inventory", lambda: [{"name": "OpenCrab", "state": "enabled"}])
    client = FullInventoryClient()
    snapshot = opencrab.OpenCrabInspector(client_factory=lambda: client).refresh(full=True)

    assert [row["package_id"] for row in snapshot["packs"]["items"]] == ["pack-1", "pack-2"]
    assert snapshot["packs"]["total"] == 2
    assert client.pack_calls == [{"limit": 500}, {"limit": 500, "cursor": "1"}]
    assert snapshot["projects"]["items"][0]["package_ids"] == ["pack-1"]
    assert snapshot["workflows"]["items"][0]["steps"] == []
