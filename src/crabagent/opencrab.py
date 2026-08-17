from __future__ import annotations

import copy
import json
import os
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from . import __version__
from .discovery import mcp_inventory


class OpenCrabUnavailable(RuntimeError):
    pass


_MCP_SECTION = re.compile(r'^\s*\[\s*mcp_servers\.(?:"(?P<quoted>[^"]+)"|(?P<bare>[^\]\s]+))\s*\]\s*$')
_URL = re.compile(r'^\s*url\s*=\s*"(?P<url>[^"]+)"\s*$')


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def opencrab_url(config_path: Optional[Path] = None, workspace: Optional[Path] = None) -> str:
    """Read only the OpenCrab MCP endpoint. The value is never returned to UI/logs."""
    if workspace is not None:
        endpoint_path = workspace.resolve() / ".crabagent" / "opencrab" / "endpoint.json"
        try:
            endpoint = json.loads(endpoint_path.read_text(encoding="utf-8")).get("endpoint")
        except (OSError, ValueError, TypeError):
            endpoint = None
        if endpoint:
            return str(endpoint)
    path = config_path or Path.home() / ".codex" / "config.toml"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise OpenCrabUnavailable("Codex MCP config is unavailable") from exc
    active = False
    for line in lines:
        section = _MCP_SECTION.match(line)
        if section:
            active = (section.group("quoted") or section.group("bare")) == "OpenCrab"
            continue
        if active:
            url = _URL.match(line)
            if url:
                return str(url.group("url"))
    raise OpenCrabUnavailable("OpenCrab is not configured in Codex MCP")


def opencrab_is_configured(workspace: Optional[Path] = None, policy: str = "auto") -> bool:
    """Report configuration presence without making a network request.

    This is a routing hint only. A later MCP call remains authoritative and
    can still fail or return no evidence. Keeping the check local lets the
    goal compiler use an already-connected OpenCrab workspace without forcing
    users to manually select a pack first.
    """
    normalized = str(policy or "auto").strip().lower()
    if normalized == "off":
        return False
    if normalized.startswith("allow:"):
        allowed = {value.strip().lower() for value in normalized[6:].split(",") if value.strip()}
        if "opencrab" not in allowed:
            return False
    try:
        opencrab_url(workspace=workspace)
        return True
    except OpenCrabUnavailable:
        return any(
            str(row.get("name") or "").lower() == "opencrab"
            and str(row.get("state") or "").lower() != "disabled"
            for row in mcp_inventory()
        )


_SCOPE_QUERY_STOP_WORDS = {
    "내", "나", "나의", "저", "저의", "우리", "것", "거", "좀", "관련", "대해",
    "위해", "어떤", "어떻게", "무엇", "무슨", "을", "를", "이", "가", "은", "는",
    "에", "의", "와", "과", "도", "만", "에서", "에게", "으로", "로", "해", "줘",
    "해줘", "해주세요", "알려줘", "찾아줘", "보여줘", "추천해줘", "만들어줘", "진행해줘",
    "please", "help", "show", "tell", "find", "give", "make", "create", "build",
}


def _scope_query_terms(objective: str) -> list[str]:
    """Extract catalog terms without sending the user's whole sentence upstream."""
    raw = re.findall(r"[A-Za-z0-9_가-힣][A-Za-z0-9_가-힣+./-]{1,}", str(objective or "").lower())
    terms: list[str] = []
    for value in raw:
        value = value.strip("._/-")
        if not value or value in _SCOPE_QUERY_STOP_WORDS:
            continue
        # Korean particles and polite request endings often make an exact
        # catalog query miss otherwise useful pack titles.
        for suffix in ("해주세요", "해줘", "으로", "에서", "에게", "을", "를", "은", "는", "이", "가", "의", "와", "과", "에", "로"):
            if value.endswith(suffix) and len(value) - len(suffix) >= 2:
                value = value[: -len(suffix)]
                break
        if value in _SCOPE_QUERY_STOP_WORDS or len(value) < 2 or value in terms:
            continue
        terms.append(value)
    return terms[:6]


_PACK_LINEAGE_ID_FIELDS = {
    "duplicate_of",
    "duplicate_package_id",
    "duplicate_pack_id",
    "derived_from",
    "derived_from_package_id",
    "derived_from_pack_id",
    "source_package_id",
    "source_pack_id",
    "parent_package_id",
    "parent_pack_id",
    "canonical_package_id",
    "canonical_pack_id",
}
_PACK_LINEAGE_FLAG_FIELDS = {
    "duplicate",
    "is_duplicate",
    "is_duplicate_pack",
    "derived",
    "is_derived",
    "is_derivative",
}
_PACK_LINEAGE_STATUS_FIELDS = {
    "derivation_status",
    "lineage_type",
    "raw_or_derived",
    "pack_kind",
}
_PACK_NONCANONICAL_STATUSES = {"copy", "derived", "derivative", "duplicate", "fork", "generated"}


def _pack_scope_exclusion_reason(row: Dict[str, Any]) -> str:
    """Return a reason when a catalog row is not a canonical default-scope pack."""
    package_id = str(row.get("package_id") or row.get("id") or "").strip()
    pending: list[Any] = [row]
    visited: set[int] = set()
    while pending:
        current = pending.pop()
        if not isinstance(current, (dict, list)) or id(current) in visited:
            continue
        visited.add(id(current))
        if isinstance(current, list):
            pending.extend(current)
            continue
        for key, value in current.items():
            normalized_key = re.sub(r"(?<!^)(?=[A-Z])", "_", str(key)).replace("-", "_").lower()
            if normalized_key in _PACK_LINEAGE_FLAG_FIELDS and bool(value):
                return f"noncanonical:{normalized_key}"
            if normalized_key in _PACK_LINEAGE_ID_FIELDS:
                values = value if isinstance(value, list) else [value]
                if any(str(candidate or "").strip() and str(candidate).strip() != package_id for candidate in values):
                    return f"noncanonical:{normalized_key}"
            if normalized_key in _PACK_LINEAGE_STATUS_FIELDS:
                status = str(value or "").strip().lower()
                if status in _PACK_NONCANONICAL_STATUSES:
                    return f"noncanonical:{normalized_key}"
            if isinstance(value, (dict, list)):
                pending.append(value)
    origin = str(row.get("origin") or "").strip().lower()
    if origin in _PACK_NONCANONICAL_STATUSES:
        return "noncanonical:origin"
    return ""


def _rank_catalog_packs(packs: Iterable[Dict[str, Any]], terms: list[str], limit: int) -> list[Dict[str, Any]]:
    """Rank visible catalog metadata only; never inspect pack contents here."""
    scored: list[tuple[float, int, Dict[str, Any]]] = []
    for index, raw in enumerate(packs):
        if not isinstance(raw, dict) or not raw.get("package_id"):
            continue
        if _pack_scope_exclusion_reason(raw):
            continue
        title = str(raw.get("title") or "").lower()
        description = str(raw.get("description") or "").lower()
        category = str(raw.get("category") or "").lower()
        tags = " ".join(str(value).lower() for value in (raw.get("tags") or []) if value)
        searchable = " ".join((title, description, category, tags))
        score = 0.0
        matched = 0
        for term in terms:
            if term in title:
                score += 8.0
                matched += 1
            elif term in tags or term in category:
                score += 5.0
                matched += 1
            elif term in description:
                score += 2.0
                matched += 1
        if terms and " ".join(terms[:2]) in searchable:
            score += 2.0
        # Keep the project-wide Fable context discoverable without allowing it
        # to displace a strongly relevant domain pack.
        if "fable5xglm5.2" in title:
            score += 0.25
        scored.append((score, matched, {**raw, "_scope_match_score": round(score, 2), "_scope_terms_matched": matched, "_scope_index": index}))
    scored.sort(key=lambda item: (-item[0], -item[1], item[2].get("_scope_index", 0)))
    # A bounded catalog query can return visible but unrelated rows when the
    # SaaS search falls back to a broad account scope. Once at least one pack
    # matches a goal term, zero-score rows are noise rather than useful
    # diversity. Keep the fallback only for genuinely unmatchable catalogs.
    positive = [item for item in scored if item[0] > 0]
    if positive:
        scored = positive
    result: list[Dict[str, Any]] = []
    for _, _, row in scored[: max(1, int(limit))]:
        result.append({key: value for key, value in row.items() if not key.startswith("_scope_")})
    return result


def resolve_opencrab_package_scope(
    objective: str,
    *,
    workspace: Optional[Path] = None,
    client_factory: Optional[Callable[[], OpenCrabMcpClient]] = None,
    max_packages: int = 8,
) -> Dict[str, Any]:
    """Resolve a small pack scope for an unselected, knowledge-shaped goal.

    This is deliberately metadata-only. It uses the existing bounded live
    catalog and never treats a title, tag, or project membership as evidence.
    The returned IDs are only a routing hint for the subsequent evidence
    query, which remains the authoritative gate.
    """
    terms = _scope_query_terms(objective)
    base = {
        "schema": "crab.opencrab-scope-resolution/v1",
        "source": "OpenCrab MCP Workspace catalog",
        "scope_mode": "workspace_auto",
        "catalog_queries": [],
        "catalog_status": "skipped",
        "candidate_count": 0,
        "excluded_noncanonical_count": 0,
        "selected_package_count": 0,
        "selected_package_ids": [],
        "selected_titles": [],
        "cache_hit": False,
        "cache_status": None,
    }
    if not terms:
        base["reason"] = "no bounded catalog terms extracted from objective"
        return base

    inspector = OpenCrabInspector(client_factory=client_factory, workspace=workspace)
    query_candidates = [" ".join(terms[:3])]
    if len(terms) > 2:
        query_candidates.append(" ".join(terms[:2]))
    if len(terms) > 1:
        query_candidates.append(terms[0])
    query_candidates = list(dict.fromkeys(value for value in query_candidates if value))

    payload: Optional[Dict[str, Any]] = None
    last_error = ""
    for query in query_candidates[:2]:
        base["catalog_queries"].append(query)
        try:
            candidate = inspector.catalog(query=query, complete=False, prefer_stale=True)
        except (OpenCrabUnavailable, OSError, RuntimeError, ValueError) as exc:
            last_error = str(exc)
            continue
        if isinstance(candidate, dict):
            payload = candidate
        packs = (candidate.get("packs") or {}).get("items") if isinstance(candidate, dict) else []
        if isinstance(packs, list) and packs:
            break

    if payload is None:
        base.update({"catalog_status": "unavailable", "error": last_error or "catalog returned no payload"})
        return base
    packs = (payload.get("packs") or {}).get("items") or []
    candidate_packs = [
        row for row in packs
        if isinstance(row, dict) and row.get("package_id")
    ]
    canonical_packs = [row for row in candidate_packs if not _pack_scope_exclusion_reason(row)]
    ranked = _rank_catalog_packs(canonical_packs, terms, max(1, min(int(max_packages), 12)))
    base.update(
        {
            "catalog_status": str(payload.get("status") or "unknown"),
            "catalog_mode": payload.get("mode"),
            "catalog_query": payload.get("query") or base["catalog_queries"][-1],
            "candidate_count": len(candidate_packs),
            "excluded_noncanonical_count": len(candidate_packs) - len(canonical_packs),
            "selected_package_count": len(ranked),
            "selected_package_ids": [str(row.get("package_id")) for row in ranked],
            "selected_titles": [str(row.get("title") or "") for row in ranked],
            "cache_hit": bool(payload.get("cache_hit")),
            "cache_status": payload.get("cache_status"),
            "scope_mode": "workspace_auto_resolved" if ranked else "workspace_auto",
        }
    )
    if not ranked:
        base["reason"] = "catalog returned no visible pack candidates"
    return base


def _decode_mcp_payload(raw: bytes) -> Dict[str, Any]:
    text = raw.decode("utf-8", errors="replace").strip()
    if text.startswith("data:") or "\ndata:" in text:
        chunks = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
        text = chunks[-1] if chunks else "{}"
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise OpenCrabUnavailable("OpenCrab MCP returned an unreadable response") from exc
    if not isinstance(payload, dict):
        raise OpenCrabUnavailable("OpenCrab MCP returned an invalid response")
    if payload.get("error"):
        error = payload["error"]
        raise OpenCrabUnavailable(str(error.get("message") if isinstance(error, dict) else error))
    result = payload.get("result")
    if not isinstance(result, dict):
        raise OpenCrabUnavailable("OpenCrab MCP returned no result")
    return result


class OpenCrabMcpClient:
    """Small read-only Streamable HTTP MCP client; credentials remain in Codex config."""

    def __init__(
        self,
        endpoint: str,
        *,
        opener: Callable[..., Any] = urlopen,
    ) -> None:
        self.endpoint = endpoint
        self.opener = opener
        self.session_id = ""

    @staticmethod
    def _read_json_message(response: Any) -> bytes:
        decoder = json.JSONDecoder()
        buffer = bytearray()
        while len(buffer) <= 8 * 1024 * 1024:
            read1 = getattr(response, "read1", None)
            chunk = read1(65536) if callable(read1) else response.read(1)
            if not chunk:
                break
            buffer.extend(chunk)
            text = bytes(buffer).decode("utf-8", errors="replace")
            start = len(text) - len(text.lstrip())
            if not text[start:]:
                continue
            try:
                _, end = decoder.raw_decode(text, start)
            except ValueError:
                continue
            return text[:end].encode("utf-8")
        raise OpenCrabUnavailable("OpenCrab MCP returned no complete JSON-RPC message")

    def _post(self, method: str, params: Dict[str, Any], *, expect_result: bool = True) -> Dict[str, Any]:
        request_id = str(uuid.uuid4())
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-03-26",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        body = json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}).encode("utf-8")
        try:
            response = self.opener(Request(self.endpoint, data=body, headers=headers, method="POST"), timeout=20)
            content_type = str(response.headers.get("Content-Type") or "").lower()
            if not expect_result:
                raw = b""
            elif "text/event-stream" in content_type:
                # Streamable HTTP may keep the connection open after delivering
                # one JSON-RPC message. Read exactly the first SSE event.
                chunks = []
                total = 0
                while True:
                    line = response.readline()
                    if not line:
                        break
                    chunks.append(line)
                    total += len(line)
                    if total > 8 * 1024 * 1024:
                        raise OpenCrabUnavailable("OpenCrab MCP event exceeded the local response limit")
                    if line in {b"\n", b"\r\n"}:
                        break
                raw = b"".join(chunks)
            else:
                raw = self._read_json_message(response)
            session_id = response.headers.get("Mcp-Session-Id")
            if session_id:
                self.session_id = session_id
            close = getattr(response, "close", None)
            if callable(close):
                close()
        except (HTTPError, URLError, OSError) as exc:
            # urllib errors can embed the full endpoint, including credentials.
            # Keep the concrete exception chained for local debugging without
            # returning it through the daemon or TUI boundary.
            raise OpenCrabUnavailable("OpenCrab MCP request failed") from exc
        return _decode_mcp_payload(raw) if expect_result else {}

    def initialize(self) -> None:
        self._post(
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "CrabAgent", "version": __version__},
            },
        )
        self._post("notifications/initialized", {}, expect_result=False)

    def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if not self.session_id:
            self.initialize()
        result = self._post("tools/call", {"name": name, "arguments": arguments or {}})
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            return structured
        for item in result.get("content") or []:
            if isinstance(item, dict) and item.get("type") == "text":
                try:
                    parsed = json.loads(str(item.get("text") or ""))
                except ValueError:
                    continue
                if isinstance(parsed, dict):
                    return parsed
        return {"raw_content": result.get("content") or []}


def _items(value: Any) -> Iterable[Dict[str, Any]]:
    return value if isinstance(value, list) else []


def _identifier_tail(value: Any) -> str:
    compact = re.sub(r"[^a-zA-Z0-9]", "", str(value or "")).lower()
    return compact[-8:] if compact else ""


def load_opencrab_scope(workspace: Optional[Path] = None, status: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Load local non-secret display scope and the bounded live-catalog query."""
    config: Dict[str, Any] = {}
    if workspace is not None:
        path = workspace.resolve() / ".crabagent" / "opencrab" / "scope.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            value = {}
        if isinstance(value, dict):
            config.update(value)

    status = status or {}
    owner_tail = str(
        os.environ.get("OPENCRAB_OWNER_ID_TAIL")
        or config.get("owner_id_tail")
        or status.get("owner_id_tail")
        or status.get("user_id_tail")
        or ""
    ).strip().lower()
    if len(owner_tail) > 8:
        owner_tail = _identifier_tail(owner_tail)

    workspace_values: list[str] = []
    configured_workspaces = config.get("workspace_ids")
    if isinstance(configured_workspaces, list):
        workspace_values.extend(str(value).strip() for value in configured_workspaces if str(value).strip())
    configured_workspace = os.environ.get("OPENCRAB_WORKSPACE_ID") or config.get("workspace_id") or status.get("workspace_id")
    if configured_workspace:
        workspace_values.extend(value.strip() for value in str(configured_workspace).split(",") if value.strip())
    catalog_query = str(
        os.environ.get("OPENCRAB_CATALOG_QUERY")
        or config.get("catalog_query")
        or ""
    ).strip()
    try:
        catalog_limit = max(1, min(int(os.environ.get("OPENCRAB_CATALOG_LIMIT") or config.get("catalog_limit") or 25), 25))
    except (TypeError, ValueError):
        catalog_limit = 25
    try:
        catalog_cache_ttl = max(30, min(int(os.environ.get("OPENCRAB_CATALOG_CACHE_TTL") or config.get("catalog_cache_ttl") or 300), 3600))
    except (TypeError, ValueError):
        catalog_cache_ttl = 300
    catalog_fallback_query = str(
        os.environ.get("OPENCRAB_CATALOG_FALLBACK_QUERY")
        or config.get("catalog_fallback_query")
        or "ALEXAI"
    ).strip()
    return {
        "owner_id_tail": owner_tail or None,
        "workspace_ids": sorted(set(workspace_values)),
        "configured": bool(owner_tail or workspace_values),
        "catalog_query": catalog_query,
        "catalog_limit": catalog_limit,
        "catalog_fallback_query": catalog_fallback_query,
        "catalog_cache_ttl": catalog_cache_ttl,
        "source": "local_scope" if config or os.environ.get("OPENCRAB_OWNER_ID_TAIL") or os.environ.get("OPENCRAB_WORKSPACE_ID") else "mcp_status",
    }


def filter_inventory_for_display(snapshot: Dict[str, Any], workspace: Optional[Path] = None) -> Dict[str, Any]:
    """Return the personal display slice without mutating the admin snapshot."""
    filtered = copy.deepcopy(snapshot)
    if snapshot.get("access_scope") != "admin_all_customers":
        filtered["display_scope"] = "connected_account"
        return filtered

    scope = load_opencrab_scope(workspace)
    owner_tail = str(scope.get("owner_id_tail") or "").lower()
    workspace_ids = set(scope.get("workspace_ids") or [])
    packs_value = snapshot.get("packs") or {}
    all_packs = [row for row in (packs_value.get("items") or []) if isinstance(row, dict)]

    def is_personal(row: Dict[str, Any]) -> bool:
        row_owner = str(row.get("owner_id_tail") or "").lower()
        row_workspace = str(row.get("workspace_id") or "")
        return bool((owner_tail and row_owner and row_owner == owner_tail) or (row_workspace and row_workspace in workspace_ids))

    visible_packs = [row for row in all_packs if is_personal(row)] if scope.get("configured") else []
    visible_pack_ids = {str(row.get("package_id")) for row in visible_packs if row.get("package_id")}

    projects_value = snapshot.get("projects") or {}
    all_projects = [row for row in (projects_value.get("items") or []) if isinstance(row, dict)]
    visible_projects = []
    for project in all_projects:
        project_workspace = str(project.get("workspace_id") or "")
        project_pack_ids = {str(value) for value in project.get("package_ids") or []}
        if (scope.get("configured") and project_workspace in workspace_ids) or project_pack_ids & visible_pack_ids:
            visible_projects.append(project)

    filtered["display_scope"] = "admin_personal" if scope.get("configured") else "admin_personal_unconfigured"
    filtered["display_scope_config"] = scope
    filtered["scope_warning"] = (
        "Admin inventory is filtered to the configured owner/workspace. External packs are hidden."
        if scope.get("configured")
        else "Admin inventory is connected, but owner scope is not configured. External packs are hidden until a local owner tail or workspace is supplied."
    )
    filtered["packs"] = dict(packs_value)
    filtered["packs"]["items"] = visible_packs
    filtered["packs"]["total"] = len(visible_packs)
    filtered["packs"]["has_more"] = False
    filtered["packs"]["hidden_admin_count"] = max(0, len(all_packs) - len(visible_packs))
    filtered["projects"] = dict(projects_value)
    filtered["projects"]["items"] = visible_projects
    filtered["projects"]["total"] = len(visible_projects)
    filtered["projects"]["has_more"] = False
    filtered["projects"]["hidden_admin_count"] = max(0, len(all_projects) - len(visible_projects))
    return filtered


class OpenCrabInspector:
    """Collect bounded, read-only OpenCrab summaries for the CrabAgent panel."""

    def __init__(self, client_factory: Optional[Callable[[], OpenCrabMcpClient]] = None, workspace: Optional[Path] = None) -> None:
        self.workspace = workspace.resolve() if workspace is not None else None
        self.client_factory = client_factory or (lambda: OpenCrabMcpClient(opencrab_url(workspace=self.workspace)))
        self._catalog_cache: Optional[Dict[str, Any]] = None
        self._catalog_cache_at = 0.0
        self._catalog_cache_key: Optional[tuple[str, bool]] = None

    def refresh(self, *, full: bool = False) -> Dict[str, Any]:
        configured = next((row for row in mcp_inventory() if row["name"] == "OpenCrab"), None)
        endpoint_configured = False
        if self.workspace is not None:
            endpoint_configured = (self.workspace / ".crabagent" / "opencrab" / "endpoint.json").is_file()
        if (configured is None and not endpoint_configured) or (configured is not None and configured.get("state") == "disabled" and not endpoint_configured):
            raise OpenCrabUnavailable("OpenCrab MCP is not enabled in Codex")
        client = self.client_factory()
        status = client.call_tool("opencrab_status", {})
        admin_scope = bool(status.get("admin_all_customers"))
        scope = "admin_all_customers" if admin_scope else "connected_account"
        owner_scope = load_opencrab_scope(self.workspace, status)
        result: Dict[str, Any] = {
            "source": "OpenCrab MCP",
            "collected_at": _now(),
            "status": str(status.get("status") or "unknown"),
            "access_scope": scope,
            "scope_warning": (
                "Connected MCP is admin-scoped. Pack, project, and workflow lists may include other owners."
                if admin_scope
                else "Connected account scope."
            ),
            "account": {
                "tier": status.get("tier"),
                "auth_mode": status.get("auth_mode"),
                "scopes": status.get("scopes") or [],
            },
            "owner_scope": owner_scope,
            "ontology": {key: status.get(key) for key in ("documents", "chunks", "nodes", "edges")},
            "packs": {"status": "unavailable", "items": [], "total": None},
            "projects": {"status": "unavailable", "items": [], "total": None},
            "workflows": {"status": "unavailable", "items": [], "total": None},
        }
        self._collect_packs(client, result, full=full)
        self._collect_projects(client, result, full=full)
        self._collect_workflows(client, result, full=full)
        return result

    @staticmethod
    def _capture(target: Dict[str, Any], key: str, call: Callable[[], Dict[str, Any]], normalizer: Callable[[Dict[str, Any]], Dict[str, Any]]) -> None:
        try:
            payload = call()
            if str(payload.get("status") or "").lower() == "error":
                detail = payload.get("detail") or payload.get("error") or "OpenCrab read failed"
                bug_report = payload.get("bug_report")
                code = bug_report.get("error_code") if isinstance(bug_report, dict) else None
                if code:
                    detail = "%s (%s)" % (detail, code)
                raise OpenCrabUnavailable(str(detail))
            target[key] = normalizer(payload)
        except OpenCrabUnavailable as exc:
            target[key] = {"status": "unavailable", "items": [], "total": None, "error": str(exc)}

    @staticmethod
    def _pack_row(row: Dict[str, Any]) -> Dict[str, Any]:
        normalized = {
            "package_id": row.get("package_id") or row.get("id"),
            "workspace_id": row.get("workspace_id") or row.get("workspace"),
            "owner_id_tail": row.get("owner_id_tail"),
            "title": row.get("title"),
            "description": row.get("description"),
            "category": row.get("category"),
            "version": row.get("version"),
            "visibility": row.get("visibility"),
            "license_scope": row.get("license_scope"),
            "created_at": row.get("created_at"),
            "origin": row.get("origin"),
            "snapshot": row.get("snapshot") or {},
            "tags": row.get("tags") or [],
            "marketplace_import": row.get("marketplace_import"),
            "metadata": row.get("metadata") or {},
            "lineage": row.get("lineage") or {},
            "provenance": row.get("provenance") or {},
        }
        for key in (
            "duplicate_of", "duplicate_package_id", "duplicate_pack_id",
            "derived_from", "derived_from_package_id", "derived_from_pack_id",
            "source_package_id", "source_pack_id", "parent_package_id", "parent_pack_id",
            "canonical_package_id", "canonical_pack_id", "duplicate", "is_duplicate",
            "is_duplicate_pack", "derived", "is_derived", "is_derivative",
            "derivation_status", "lineage_type", "raw_or_derived", "pack_kind",
        ):
            if key in row:
                normalized[key] = row.get(key)
        return normalized

    @classmethod
    def _project_row(cls, row: Dict[str, Any]) -> Dict[str, Any]:
        packages = [cls._pack_row(package) for package in _items(row.get("packages"))]
        linked_workspace_ids = [str(value) for value in _items(row.get("linked_workspace_ids")) if str(value).strip()]
        return {
            "project_id": row.get("project_id") or row.get("id"),
            "workspace_id": row.get("workspace_id") or row.get("workspace"),
            "name": row.get("name") or row.get("title"),
            "description": row.get("description"),
            "project_type": row.get("project_type"),
            "status": row.get("status"),
            "metadata": row.get("metadata") or {},
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
            "workspace_scope": row.get("workspace_scope"),
            "linked_workspace_ids": sorted(set(linked_workspace_ids)),
            "package_count": row.get("package_count") if row.get("package_count") is not None else len(packages),
            "package_ids": [str(package.get("package_id")) for package in packages if package.get("package_id")],
            "packages": packages,
        }

    @staticmethod
    def _catalog_pack_is_visible(row: Dict[str, Any], scope: Dict[str, Any], admin_scope: bool) -> bool:
        """Keep the live Workspace view account-scoped even with an admin MCP key.

        A purchased marketplace pack can have the seller as its owner, and the
        live Workspace project rows do not always include ``owner_id_tail``.
        Prefer an explicit local owner/workspace match. When no workspace UUID
        is configured, use the Workspace license scope as the bounded fallback:
        personal and purchased_public are visible; public commercial inventory
        is not.
        """
        if not admin_scope:
            return True
        owner_tail = str(scope.get("owner_id_tail") or "").lower()
        workspace_ids = set(scope.get("workspace_ids") or [])
        row_owner = str(row.get("owner_id_tail") or "").lower()
        row_workspace = str(row.get("workspace_id") or "")
        if bool(
            (owner_tail and row_owner and row_owner == owner_tail)
            or (row_workspace and row_workspace in workspace_ids)
        ):
            return True
        if workspace_ids:
            return False
        return str(row.get("license_scope") or "").strip().lower() in {"personal", "purchased_public"}

    def _catalog_cache_path(self) -> Optional[Path]:
        if self.workspace is None:
            return None
        return self.workspace / ".crabagent" / "opencrab" / "live-catalog-cache.json"

    def _workspace_cache_path(self) -> Optional[Path]:
        if self.workspace is None:
            return None
        return self.workspace / ".crabagent" / "opencrab" / "workspace-pack-cache.json"

    def _workspace_scope_cache_path(self) -> Optional[Path]:
        if self.workspace is None:
            return None
        return self.workspace / ".crabagent" / "opencrab" / "workspace-scope-cache.json"

    def _read_workspace_scope_cache(self, *, ttl: int) -> list[str]:
        path = self._workspace_scope_cache_path()
        if path is None:
            return []
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
            cached_at = float(envelope.get("cached_at") or 0)
            workspace_ids = envelope.get("workspace_ids")
        except (OSError, ValueError, TypeError):
            return []
        if time.time() - cached_at > ttl or not isinstance(workspace_ids, list):
            return []
        return sorted({str(value).strip() for value in workspace_ids if str(value).strip()})

    def _write_workspace_scope_cache(self, workspace_ids: Iterable[str]) -> None:
        path = self._workspace_scope_cache_path()
        if path is None:
            return
        values = sorted({str(value).strip() for value in workspace_ids if str(value).strip()})
        if not values:
            return
        envelope = {
            "schema": "crabagent-workspace-scope-cache/1",
            "cached_at": time.time(),
            "workspace_ids": values,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
            temporary.replace(path)
        except OSError:
            return

    def _catalog_log_path(self) -> Optional[Path]:
        if self.workspace is None:
            return None
        return self.workspace / ".crabagent" / "opencrab" / "catalog.log"

    def _record_catalog_log(self, event: Dict[str, Any]) -> None:
        path = self._catalog_log_path()
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"at": _now(), **event}, ensure_ascii=False) + "\n")
        except OSError:
            return

    def _read_catalog_cache(
        self,
        *,
        query: str,
        complete: bool,
        ttl: int,
        allow_stale: bool = False,
        max_stale_ttl: int = 86400,
    ) -> Optional[Dict[str, Any]]:
        path = self._catalog_cache_path()
        if path is None:
            return None
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
            payload = envelope.get("payload") if isinstance(envelope, dict) else None
            cached_query = str(envelope.get("query") or "") if isinstance(envelope, dict) else ""
            cached_complete = bool(envelope.get("complete")) if isinstance(envelope, dict) else False
            cached_at = float(envelope.get("cached_at") or 0) if isinstance(envelope, dict) else 0
        except (OSError, ValueError, TypeError):
            return None
        if not isinstance(payload, dict) or cached_query != query or (complete and not cached_complete and not allow_stale):
            return None
        age = max(0, time.time() - cached_at)
        if age > ttl and not allow_stale:
            return None
        if age > max_stale_ttl:
            return None
        result = copy.deepcopy(payload)
        result["cache_hit"] = True
        result["cache_age_seconds"] = int(age)
        result["cache_status"] = "stale" if age > ttl else "fresh"
        return result

    def _merge_stale_workspace_packs(self, result: Dict[str, Any], *, max_stale_ttl: int = 86400) -> None:
        """Recover pack coverage from the local workspace cache during outages."""
        if self.workspace is None:
            return
        cached_workspaces = self._read_workspace_cache()
        now = time.time()
        packs = (result.get("packs") or {}).get("items") or []
        merged: Dict[str, Dict[str, Any]] = {
            str(row.get("package_id")): dict(row)
            for row in packs
            if isinstance(row, dict) and row.get("package_id")
        }
        for workspace_id, entry in cached_workspaces.items():
            cached_at = float(entry.get("cached_at") or 0)
            if now - cached_at > max_stale_ttl:
                continue
            for raw in entry.get("packs") or []:
                if not isinstance(raw, dict) or not raw.get("package_id"):
                    continue
                package_id = str(raw["package_id"])
                if package_id in merged:
                    continue
                row = dict(raw)
                row.setdefault("workspace_id", workspace_id)
                row.setdefault("project_name", "Cached workspace pack")
                merged[package_id] = row
        if not merged:
            return
        pack_group = dict(result.get("packs") or {})
        pack_group["items"] = sorted(
            merged.values(),
            key=lambda row: (str(row.get("title") or "").lower(), str(row.get("package_id") or "")),
        )
        pack_group["total"] = len(merged)
        pack_group["status"] = "stale"
        result["packs"] = pack_group
        result["stale_pack_cache_merged"] = True

    def _stale_catalog_result(
        self,
        cached: Optional[Dict[str, Any]],
        error: OpenCrabUnavailable,
        *,
        query: str,
        complete: bool,
    ) -> Optional[Dict[str, Any]]:
        if cached is None:
            return None
        result = copy.deepcopy(cached)
        self._merge_stale_workspace_packs(result)
        result["status"] = "stale"
        result["mode"] = "live_catalog_stale"
        result["query"] = query
        result["requested_query"] = query
        result["complete"] = bool(result.get("complete"))
        result["catalog_error"] = str(error)
        result["stale_reason"] = "remote catalog read failed; last successful catalog retained"
        result["stale_checked_at"] = _now()
        result["cache_status"] = "stale"
        warning = str(result.get("scope_warning") or "").strip()
        result["scope_warning"] = (
            "%s %s"
            % (
                warning,
                "실시간 OpenCrab 조회가 실패해 마지막 성공 카탈로그를 표시합니다.",
            )
        ).strip()
        cache_key = (query, complete)
        self._catalog_cache = copy.deepcopy(result)
        self._catalog_cache_at = time.monotonic()
        self._catalog_cache_key = cache_key
        self._record_catalog_log({
            "event": "stale_cache_fallback",
            "complete": complete,
            "projects": (result.get("projects") or {}).get("total"),
            "packs": (result.get("packs") or {}).get("total"),
            "age_seconds": result.get("cache_age_seconds"),
            "error": str(error),
        })
        return result

    def _write_catalog_cache(self, payload: Dict[str, Any], *, query: str, complete: bool) -> None:
        path = self._catalog_cache_path()
        if path is None:
            return
        if not complete and path.exists():
            # A fast project response is a partial view. Never replace a
            # previously verified full catalog with that smaller response.
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(existing, dict) and bool(existing.get("complete")):
                    return
            except (OSError, ValueError, TypeError):
                pass
        envelope = {
            "schema": "crabagent-live-catalog-cache/2",
            "query": query,
            "complete": complete,
            "cached_at": time.time(),
            "payload": payload,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
            temporary.replace(path)
        except OSError:
            return

    def _read_workspace_cache(self) -> Dict[str, Dict[str, Any]]:
        path = self._workspace_cache_path()
        if path is None:
            return {}
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        workspaces = envelope.get("workspaces") if isinstance(envelope, dict) else None
        return workspaces if isinstance(workspaces, dict) else {}

    def _write_workspace_cache(self, workspaces: Dict[str, Dict[str, Any]]) -> None:
        path = self._workspace_cache_path()
        if path is None:
            return
        envelope = {"schema": "crabagent-workspace-pack-cache/1", "workspaces": workspaces}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
            temporary.replace(path)
        except OSError:
            return

    def _search_workspace_packs(self, workspace_id: str) -> list[Dict[str, Any]]:
        client = self.client_factory()
        items: list[Dict[str, Any]] = []
        cursor = ""
        seen_cursors: set[str] = set()
        while True:
            arguments: Dict[str, Any] = {"limit": 500, "workspace_id": workspace_id}
            if cursor:
                arguments["cursor"] = cursor
            page = client.call_tool("opencrab_search_packs", arguments)
            if str(page.get("status") or "").lower() == "error":
                raise OpenCrabUnavailable(str(page.get("detail") or page.get("error") or "Workspace pack search failed"))
            items.extend(self._pack_row(row) for row in _items(page.get("packs")))
            next_cursor = str(page.get("next_cursor") or "")
            if not page.get("has_more") or not next_cursor or next_cursor in seen_cursors:
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor
            if len(items) > 100000:
                raise OpenCrabUnavailable("Workspace pack search exceeded the local safety limit")
        return items

    def _hydrate_workspace_packs(
        self,
        workspace_ids: list[str],
        *,
        force: bool,
        ttl: int,
    ) -> tuple[list[Dict[str, Any]], Dict[str, Any]]:
        cached_workspaces = self._read_workspace_cache()
        usable: Dict[str, list[Dict[str, Any]]] = {}
        pending: list[str] = []
        cache_hits = 0
        now = time.time()
        for workspace_id in workspace_ids:
            entry = cached_workspaces.get(workspace_id) or {}
            age = now - float(entry.get("cached_at") or 0)
            packs = entry.get("packs")
            if not force and isinstance(packs, list) and age <= ttl:
                usable[workspace_id] = [row for row in packs if isinstance(row, dict)]
                cache_hits += 1
            else:
                pending.append(workspace_id)

        errors: list[Dict[str, str]] = []
        if pending:
            # Each workspace is an independent, read-only query. A small pool
            # keeps the first complete catalog responsive without flooding MCP.
            workers = min(6, len(pending))
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="crab-opencrab") as pool:
                futures = {pool.submit(self._search_workspace_packs, workspace_id): workspace_id for workspace_id in pending}
                for future in as_completed(futures):
                    workspace_id = futures[future]
                    try:
                        packs = future.result()
                        usable[workspace_id] = packs
                        cached_workspaces[workspace_id] = {"cached_at": time.time(), "packs": packs}
                    except Exception as exc:
                        errors.append({"workspace_id": workspace_id, "error": str(exc)})
                        if workspace_id in cached_workspaces and isinstance(cached_workspaces[workspace_id].get("packs"), list):
                            usable[workspace_id] = [row for row in cached_workspaces[workspace_id]["packs"] if isinstance(row, dict)]
        if pending:
            self._write_workspace_cache(cached_workspaces)
        packs_by_id: Dict[str, Dict[str, Any]] = {}
        for workspace_id in workspace_ids:
            for pack in usable.get(workspace_id, []):
                package_id = str(pack.get("package_id") or "")
                if package_id:
                    packs_by_id[package_id] = pack
        return list(packs_by_id.values()), {
            "workspace_count": len(workspace_ids),
            "cache_hits": cache_hits,
            "fetched": len(pending),
            "errors": errors,
            "complete": not errors and len(usable) == len(workspace_ids),
        }

    def catalog(
        self,
        *,
        query: Optional[str] = None,
        force: bool = False,
        complete: bool = False,
        prefer_stale: bool = False,
    ) -> Dict[str, Any]:
        """Read the live Workspace catalog without creating a full JSON snapshot.

        OpenCrab's project endpoint already embeds the packs assigned to each
        project. It is the fast, user-facing catalog boundary: one bounded
        project request, in-memory for a short TTL, and no local full-inventory
        write. The explicit ``ontology.sync`` command remains available for
        audit/delta workflows.
        """
        configured_scope = load_opencrab_scope(self.workspace)
        normalized_query = configured_scope.get("catalog_query", "") if query is None else str(query or "").strip()
        normalized_query = str(normalized_query or "").strip()
        cache_key = (normalized_query, complete)
        catalog_ttl = int(configured_scope.get("catalog_cache_ttl") or 300)
        if not force and self._catalog_cache is not None and self._catalog_cache_key == cache_key and time.monotonic() - self._catalog_cache_at < 5:
            return copy.deepcopy(self._catalog_cache)
        if not force:
            persistent = self._read_catalog_cache(query=normalized_query, complete=complete, ttl=catalog_ttl)
            if persistent is not None:
                self._catalog_cache = copy.deepcopy(persistent)
                self._catalog_cache_at = time.monotonic()
                self._catalog_cache_key = cache_key
                self._record_catalog_log({"event": "cache_hit", "complete": complete, "projects": (persistent.get("projects") or {}).get("total"), "packs": (persistent.get("packs") or {}).get("total"), "age_seconds": persistent.get("cache_age_seconds")})
                return persistent
        stale_cache = self._read_catalog_cache(
            query=normalized_query,
            complete=complete,
            ttl=catalog_ttl,
            allow_stale=True,
            max_stale_ttl=max(86400, catalog_ttl),
        )
        if prefer_stale and not force and stale_cache is not None:
            # The command deck should become usable immediately. A stale
            # catalog is an honest read-only view; the caller can launch one
            # explicit background refresh and surface its result later.
            deferred = self._stale_catalog_result(
                error=OpenCrabUnavailable("background refresh deferred after cached catalog"),
                cached=stale_cache,
                query=normalized_query,
                complete=complete,
            )
            if deferred is not None:
                deferred["catalog_error"] = ""
                deferred["refresh_deferred"] = True
                deferred["stale_reason"] = "cached catalog shown while live refresh runs"
                return deferred
        configured = next((row for row in mcp_inventory() if row["name"] == "OpenCrab"), None)
        if configured is None or configured.get("state") == "disabled":
            error = OpenCrabUnavailable("OpenCrab MCP is not enabled in Codex")
            fallback = self._stale_catalog_result(error=error, cached=stale_cache, query=normalized_query, complete=complete)
            if fallback is not None:
                return fallback
            raise error
        client = self.client_factory()
        try:
            status = client.call_tool("opencrab_status", {})
            if str(status.get("status") or "").lower() == "error":
                raise OpenCrabUnavailable(str(status.get("detail") or status.get("error") or "OpenCrab status failed"))
        except OpenCrabUnavailable as error:
            fallback = self._stale_catalog_result(error=error, cached=stale_cache, query=normalized_query, complete=complete)
            if fallback is not None:
                return fallback
            raise
        admin_scope = bool(status.get("admin_all_customers"))
        owner_scope = load_opencrab_scope(self.workspace, status)
        requested_query = normalized_query
        arguments: Dict[str, Any] = {"action": "list", "limit": int(owner_scope.get("catalog_limit") or 25)}
        if normalized_query:
            arguments["query"] = normalized_query
        fallback_used = False
        fallback_error = ""
        try:
            project_payload = client.call_tool("opencrab_project_manage", arguments)
            if str(project_payload.get("status") or "").lower() == "error":
                raise OpenCrabUnavailable(str(project_payload.get("detail") or project_payload.get("error") or "OpenCrab Workspace catalog failed"))
        except OpenCrabUnavailable as exc:
            fallback_query = str(owner_scope.get("catalog_fallback_query") or "").strip()
            if normalized_query or not fallback_query:
                fallback = self._stale_catalog_result(error=exc, cached=stale_cache, query=normalized_query, complete=complete)
                if fallback is not None:
                    return fallback
                raise
            fallback_used = True
            fallback_error = str(exc)
            normalized_query = fallback_query
            arguments = {"action": "list", "limit": min(int(owner_scope.get("catalog_limit") or 25), 20), "query": fallback_query}
            try:
                project_payload = client.call_tool("opencrab_project_manage", arguments)
                if str(project_payload.get("status") or "").lower() == "error":
                    raise OpenCrabUnavailable(str(project_payload.get("detail") or project_payload.get("error") or "OpenCrab Workspace catalog failed"))
            except OpenCrabUnavailable as fallback_error_detail:
                fallback = self._stale_catalog_result(
                    error=fallback_error_detail,
                    cached=stale_cache if normalized_query == requested_query else None,
                    query=normalized_query,
                    complete=complete,
                )
                if fallback is not None:
                    return fallback
                raise

        projects = []
        packs_by_id: Dict[str, Dict[str, Any]] = {}
        project_by_pack_id: Dict[str, Dict[str, Any]] = {}
        catalog_ttl = int(owner_scope.get("catalog_cache_ttl") or catalog_ttl)
        linked_workspace_ids: set[str] = set(str(value) for value in owner_scope.get("workspace_ids") or [] if str(value).strip())
        linked_workspace_ids.update(self._read_workspace_scope_cache(ttl=catalog_ttl))
        for raw_project in _items(project_payload.get("projects")):
            project = self._project_row(raw_project)
            linked_workspace_ids.update(project.get("linked_workspace_ids") or [])
            if project.get("workspace_id"):
                linked_workspace_ids.add(str(project["workspace_id"]))
            project_packs = []
            for package in project.get("packages") or []:
                if not isinstance(package, dict):
                    continue
                package = dict(package)
                package_id = str(package.get("package_id") or "")
                if not package_id:
                    continue
                package["project_id"] = project.get("project_id")
                package["project_name"] = project.get("name")
                if package.get("workspace_id"):
                    linked_workspace_ids.add(str(package["workspace_id"]))
                project_packs.append(package)
                packs_by_id[package_id] = package
                project_by_pack_id[package_id] = project
            project["packages"] = project_packs
            project["package_ids"] = [str(row.get("package_id")) for row in project_packs if row.get("package_id")]
            project["package_count"] = len(project_packs)
            projects.append(project)

        self._write_workspace_scope_cache(linked_workspace_ids)

        hydration = {"workspace_count": len(linked_workspace_ids), "cache_hits": 0, "fetched": 0, "errors": [], "complete": False}
        if complete and linked_workspace_ids:
            hydrated_packs, hydration = self._hydrate_workspace_packs(
                sorted(linked_workspace_ids),
                force=force,
                ttl=int(owner_scope.get("catalog_cache_ttl") or 300),
            )
            for pack in hydrated_packs:
                package_id = str(pack.get("package_id") or "")
                if not package_id:
                    continue
                merged = dict(packs_by_id.get(package_id) or {})
                merged.update(pack)
                project = project_by_pack_id.get(package_id)
                if project is not None:
                    merged["project_id"] = project.get("project_id")
                    merged["project_name"] = project.get("name")
                else:
                    merged["project_name"] = "Unassigned workspace pack"
                packs_by_id[package_id] = merged

        result = {
            "status": "ok",
            "source": "OpenCrab MCP Workspace catalog",
            "mode": "live_catalog",
            "collected_at": _now(),
            "query": normalized_query,
            "requested_query": requested_query,
            "fallback_used": fallback_used,
            "complete": bool(complete and hydration.get("complete")),
            "linked_workspace_count": len(linked_workspace_ids),
            "hydration": hydration,
            "access_scope": "admin_all_customers" if admin_scope else "connected_account",
            "display_scope": "admin_personal" if admin_scope else "connected_account",
            "scope_warning": (
                "Admin MCP is bounded to the Workspace projects and their linked Workspace IDs; no global admin pack search is displayed."
                if admin_scope
                else "Live catalog is read from the connected OpenCrab Workspace."
            ),
            "account": {
                "tier": status.get("tier"),
                "auth_mode": status.get("auth_mode"),
                "scopes": status.get("scopes") or [],
            },
            "owner_scope": owner_scope,
            "workspace_scope": project_payload.get("workspace_scope"),
            "projects": {
                "status": "ok",
                "total": len(projects),
                "items": projects,
                "has_more": bool(project_payload.get("truncated") or project_payload.get("has_more")),
            },
            "packs": {
                "status": "ok",
                "total": len(packs_by_id),
                "items": sorted(packs_by_id.values(), key=lambda row: (str(row.get("title") or "").lower(), str(row.get("package_id") or ""))),
                "has_more": bool(hydration.get("errors")),
            },
        }
        if fallback_used:
            result["mode"] = "live_catalog_fallback"
            result["scope_warning"] = "전체 Workspace 목록이 지연되어 계정 검색 fallback을 사용했습니다."
            result["catalog_error"] = fallback_error
        if complete and not hydration.get("complete"):
            result["scope_warning"] = "일부 Workspace 팩 조회가 지연되었습니다. 캐시된 항목과 연결된 프로젝트를 먼저 표시합니다."
        self._catalog_cache = copy.deepcopy(result)
        self._catalog_cache_at = time.monotonic()
        self._catalog_cache_key = cache_key
        self._write_catalog_cache(result, query=normalized_query, complete=bool(result.get("complete")))
        self._record_catalog_log({
            "event": "live_catalog",
            "complete": bool(result.get("complete")),
            "force": force,
            "fallback": fallback_used,
            "projects": len(projects),
            "packs": len(packs_by_id),
            "linked_workspaces": len(linked_workspace_ids),
            "cache_hits": hydration.get("cache_hits", 0),
            "fetched": hydration.get("fetched", 0),
            "errors": len(hydration.get("errors") or []),
        })
        return result

    @staticmethod
    def _workflow_row(row: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "workflow_id": row.get("workflow_id"),
            "name": row.get("name"),
            "description": row.get("description"),
            "status": row.get("status"),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
            "steps": row.get("steps") if isinstance(row.get("steps"), list) else [],
            "edges": row.get("edges") if isinstance(row.get("edges"), list) else [],
        }

    def _collect_packs(self, client: OpenCrabMcpClient, result: Dict[str, Any], *, full: bool = False) -> None:
        if not full:
            self._capture(
                result,
                "packs",
                lambda: client.call_tool("opencrab_search_packs", {"limit": 30}),
                lambda payload: {
                    "status": "ok",
                    "total": payload.get("total"),
                    "has_more": bool(payload.get("has_more")),
                    "items": [self._pack_row(row) for row in _items(payload.get("packs"))][:30],
                },
            )
            return

        def collect() -> Dict[str, Any]:
            items = []
            cursor = ""
            seen_cursors = set()
            while True:
                arguments: Dict[str, Any] = {"limit": 500}
                if cursor:
                    arguments["cursor"] = cursor
                page = client.call_tool("opencrab_search_packs", arguments)
                if str(page.get("status") or "").lower() == "error":
                    detail = page.get("detail") or page.get("error") or "OpenCrab pack inventory failed"
                    raise OpenCrabUnavailable(str(detail))
                items.extend(self._pack_row(row) for row in _items(page.get("packs")))
                next_cursor = str(page.get("next_cursor") or "")
                if not page.get("has_more") or not next_cursor or next_cursor in seen_cursors:
                    break
                seen_cursors.add(next_cursor)
                cursor = next_cursor
                if len(items) > 100000:
                    raise OpenCrabUnavailable("OpenCrab pack inventory exceeded the local safety limit")
            return {"status": "ok", "total": len(items), "has_more": False, "items": items}

        self._capture(result, "packs", collect, lambda payload: payload)

    def _collect_projects(self, client: OpenCrabMcpClient, result: Dict[str, Any], *, full: bool = False) -> None:
        self._capture(
            result,
            "projects",
            lambda: client.call_tool("opencrab_project_manage", {"action": "list"}),
            lambda payload: {
                "status": "ok",
                "total": payload.get("total") if not full else len(_items(payload.get("projects"))),
                "items": [self._project_row(row) for row in _items(payload.get("projects"))][: 1000 if full else 30],
            },
        )

    def _collect_workflows(self, client: OpenCrabMcpClient, result: Dict[str, Any], *, full: bool = False) -> None:
        self._capture(
            result,
            "workflows",
            lambda: client.call_tool("opencrab_list_workflows", {"status": "any"}),
            lambda payload: {
                "status": "ok",
                "total": payload.get("total") if not full else len(_items(payload.get("workflows"))),
                "items": [self._workflow_row(row) for row in _items(payload.get("workflows"))][: 1000 if full else 30],
            },
        )
