from __future__ import annotations

import hashlib
import json
import os
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional


class FolderIngestError(RuntimeError):
    """A local folder could not be staged or the remote operation was refused."""


EXPERT_TIERS = {"expert", "enterprise"}
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_REMOTE_BYTES = 48 * 1024 * 1024
CHUNK_LINES = 120
CHUNK_CHARS = 16_000
IGNORED_DIRECTORIES = {
    ".git",
    ".hg",
    ".svn",
    ".crabagent",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "dist",
    "build",
}
TEXT_SUFFIXES = {
    ".c",
    ".cc",
    ".cpp",
    ".css",
    ".csv",
    ".go",
    ".h",
    ".hpp",
    ".html",
    ".ini",
    ".java",
    ".js",
    ".json",
    ".jsx",
    ".md",
    ".mdx",
    ".py",
    ".r",
    ".rb",
    ".rs",
    ".scss",
    ".sh",
    ".sql",
    ".svg",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".vue",
    ".xml",
    ".yaml",
    ".yml",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_expert_tier(tier: Any) -> bool:
    """Enterprise is treated as a higher tier than Expert, never as a role bypass."""
    return str(tier or "").strip().lower() in EXPERT_TIERS


def remote_status(value: Any) -> str:
    """Read a provider status without treating a missing status as success."""
    if not isinstance(value, dict):
        return "unknown"
    explicit = str(value.get("status") or "").strip().lower()
    if explicit:
        return explicit
    nested = value.get("result") or value.get("data")
    if isinstance(nested, dict):
        return remote_status(nested)
    return "unknown"


def remote_package_id(value: Any) -> str:
    """Find a confirmed package identity in the small set of known MCP shapes."""
    if not isinstance(value, dict):
        return ""
    for key in ("package_id", "pack_id"):
        candidate = str(value.get(key) or "").strip()
        if candidate:
            return candidate
    for key in ("pack", "package", "result", "data"):
        nested = value.get(key)
        if isinstance(nested, dict):
            candidate = remote_package_id(nested)
            if candidate:
                return candidate
    return ""


def upload_session_id_from_result(value: Any) -> str:
    """Extract the one-time upload session from an MCP handoff response."""
    if not isinstance(value, dict):
        return ""
    for key in ("upload_session_id", "uploadSessionId"):
        candidate = str(value.get(key) or "").strip()
        if candidate:
            return candidate
    for key in ("saas_ingest_handoff", "handoff", "upload_session", "result", "data"):
        nested = value.get(key)
        if isinstance(nested, dict):
            candidate = upload_session_id_from_result(nested)
            if candidate:
                return candidate
    return ""


def _validate_staged_bundle(staged: Dict[str, Any]) -> Dict[str, Any]:
    """Reopen the local bundle before any remote call and verify its hashes."""
    manifest_path = Path(str(staged["manifest_path"]))
    chunks_path = Path(str(staged["chunks_path"]))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("protocol") != "crabagent-folder-evidence/v1":
        raise FolderIngestError("unsupported local evidence protocol")
    chunks: List[Dict[str, Any]] = []
    for line in chunks_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict) or not row.get("evidence_id"):
            raise FolderIngestError("local evidence bundle contains an invalid chunk")
        content = str(row.get("content") or "")
        if hashlib.sha256(content.encode("utf-8")).hexdigest() != str(row.get("sha256") or ""):
            raise FolderIngestError("local evidence chunk hash mismatch: %s" % row.get("evidence_id"))
        chunks.append(row)
    if int(manifest.get("source_count") or 0) != len(manifest.get("sources") or []):
        raise FolderIngestError("local evidence source count mismatch")
    if int(manifest.get("chunk_count") or 0) != len(chunks):
        raise FolderIngestError("local evidence chunk count mismatch")
    evidence_ids = [str(row["evidence_id"]) for row in chunks]
    if len(evidence_ids) != len(set(evidence_ids)):
        raise FolderIngestError("local evidence contains duplicate evidence IDs")
    return {
        "source_count": len(manifest.get("sources") or []),
        "chunk_count": len(chunks),
        "sha256": hashlib.sha256(chunks_path.read_bytes()).hexdigest(),
    }


def _build_evidence_zip(staged: Dict[str, Any]) -> Dict[str, Any]:
    """Create a deterministic, privacy-reduced handoff ZIP for the local runner."""
    validation = _validate_staged_bundle(staged)
    stage = Path(str(staged["stage_path"]))
    output = stage / "opencrab-pack.zip"
    manifest = dict(staged["manifest"])
    # The audit manifest retains the local path. The upload bundle must not.
    manifest["root_path"] = "<local-folder>"
    manifest["bundle_protocol"] = "crabagent-folder-evidence/v1"
    manifest["bundle_sha256"] = validation["sha256"]
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    chunks_bytes = Path(str(staged["chunks_path"])).read_bytes()
    readme_bytes = (
        "KINGCRAB local evidence handoff.\n"
        "This ZIP is a verified source bundle for the OpenCrab CrabAgent local runner;\n"
        "it is not reported as an ingested cloud pack until OpenCrab returns a package_id.\n"
    ).encode("utf-8")
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name, data in (("manifest.json", manifest_bytes), ("chunks.jsonl", chunks_bytes), ("README.txt", readme_bytes)):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            archive.writestr(info, data)
    size = output.stat().st_size
    if size > MAX_REMOTE_BYTES:
        raise FolderIngestError("verified evidence ZIP is too large for the remote handoff (%d bytes)" % size)
    return {
        "evidence_zip_path": str(output),
        "evidence_zip_bytes": size,
        "evidence_zip_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        **validation,
    }


def _is_probably_text(path: Path, data: bytes) -> bool:
    if path.suffix.lower() in TEXT_SUFFIXES:
        return True
    if b"\x00" in data[:8192]:
        return False
    return True


def _iter_source_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative_parts = path.relative_to(root).parts
        if any(part in IGNORED_DIRECTORIES for part in relative_parts):
            continue
        if path.name.startswith(".") and path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        yield path


def _chunks_for_text(text: str) -> Iterable[tuple[int, int, str]]:
    lines = text.splitlines()
    if not lines and text:
        lines = [text]
    start = 0
    while start < len(lines):
        end = min(len(lines), start + CHUNK_LINES)
        while end > start + 1 and len("\n".join(lines[start:end])) > CHUNK_CHARS:
            end -= 1
        content = "\n".join(lines[start:end]).strip("\n")
        if content.strip():
            yield start + 1, end, content
        start = end


def stage_folder(folder: Path, staging_root: Path, *, run_id: Optional[str] = None) -> Dict[str, Any]:
    """Read every supported text source and write an auditable local evidence bundle."""
    root = folder.expanduser().resolve()
    if not root.is_dir():
        raise FolderIngestError("folder does not exist or is not a directory: %s" % root)
    run_id = run_id or "packrun-" + uuid.uuid4().hex[:16]
    stage = staging_root.resolve() / run_id
    stage.mkdir(parents=True, exist_ok=False)
    sources: List[Dict[str, Any]] = []
    chunks: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []

    for path in _iter_source_files(root):
        relative = str(path.relative_to(root))
        try:
            size = path.stat().st_size
            if size > MAX_FILE_BYTES:
                skipped.append({"path": relative, "reason": "file_too_large", "bytes": size})
                continue
            data = path.read_bytes()
            if not _is_probably_text(path, data):
                skipped.append({"path": relative, "reason": "binary_file", "bytes": size})
                continue
            text = data.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            skipped.append({"path": relative, "reason": type(exc).__name__})
            continue

        digest = hashlib.sha256(data).hexdigest()
        source_id = "source-" + hashlib.sha256(relative.encode("utf-8")).hexdigest()[:16]
        source_chunks = list(_chunks_for_text(text))
        sources.append({
            "source_id": source_id,
            "relative_path": relative,
            "bytes": len(data),
            "sha256": digest,
            "line_count": len(text.splitlines()),
            "chunk_count": len(source_chunks),
        })
        for index, (line_start, line_end, content) in enumerate(source_chunks):
            evidence_id = "%s-%04d" % (source_id, index + 1)
            chunks.append({
                "evidence_id": evidence_id,
                "source_id": source_id,
                "source_path": relative,
                "line_start": line_start,
                "line_end": line_end,
                "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "content": content,
            })

    manifest = {
        "protocol": "crabagent-folder-evidence/v1",
        "run_id": run_id,
        "root_path": str(root),
        "collected_at": utc_now(),
        "evidence_policy": "full_supported_text_sources",
        "sources": sources,
        "source_count": len(sources),
        "chunk_count": len(chunks),
        "skipped": skipped,
    }
    manifest_path = stage / "manifest.json"
    chunks_path = stage / "chunks.jsonl"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with chunks_path.open("w", encoding="utf-8") as handle:
        for chunk in chunks:
            handle.write(json.dumps(chunk, ensure_ascii=False) + "\n")
    return {
        "run_id": run_id,
        "root_path": str(root),
        "stage_path": str(stage),
        "manifest_path": str(manifest_path),
        "chunks_path": str(chunks_path),
        "manifest": manifest,
        "chunks": chunks,
    }


class OpenCrabPackBuilder:
    """Expert-gated bridge from a local evidence bundle to OpenCrab CrabAgent."""

    def __init__(
        self,
        client_factory: Callable[[], Any],
        workspace: Path,
        *,
        tool_name: Optional[str] = None,
    ) -> None:
        self.client_factory = client_factory
        self.workspace = workspace.resolve()
        self.tool_name = str(tool_name or os.environ.get("OPENCRAB_CRAB_AGENT_TOOL") or "opencrab_crab_agent")

    def build_and_ingest(
        self,
        folder: Path,
        *,
        project_id: str = "",
        project_name: str = "",
        ontology_purpose: str = "",
        run_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        staged = stage_folder(folder, self.workspace / ".crabagent" / "pack-runs", run_id=run_id)
        manifest = staged["manifest"]
        if not manifest["source_count"]:
            return self._result(
                staged,
                "staged_empty",
                "no supported text sources were found",
                source_count=0,
                chunk_count=0,
            )

        try:
            bundle = _build_evidence_zip(staged)
        except (FolderIngestError, OSError, ValueError, json.JSONDecodeError) as exc:
            return self._result(staged, "local_validation_failed", str(exc))

        try:
            client = self.client_factory()
        except Exception as exc:
            return self._result(staged, "mcp_unavailable", str(exc), **bundle)
        try:
            account = client.call_tool("opencrab_status", {})
        except Exception as exc:
            return self._result(staged, "mcp_unavailable", str(exc), **bundle)
        tier = str(account.get("tier") or "").lower()
        if not is_expert_tier(tier):
            return self._result(
                staged,
                "expert_required",
                "Expert tier is required for local-folder pack build and ingest",
                tier=tier,
                **bundle,
            )

        pack_name = "%s ontology pack" % (project_name.strip() or Path(staged["root_path"]).name)
        output_zip = str(bundle["evidence_zip_path"])
        arguments = {
            "action": "plan",
            "pack_name": pack_name,
            "data_folder": staged["root_path"],
            "ontology_purpose": ontology_purpose.strip() or "Build an evidence-first ontology pack from this project folder for OpenCrab CrabAgent retrieval and reasoning.",
            "metaontology_purpose": "Use subject/resource/evidence/concept/claim/community/outcome/lever/policy for ontology spaces while preserving source, document, chunk, sentence, schema, and task as parsing or application layers.",
            "project_id": project_id,
            "project_name": project_name,
            "share_scope": "private",
            "quality_profile": "max",
            "output_zip": output_zip,
            "max_files": 20000,
            "chunk_chars": 1200,
            "pack_category": "crabagent",
            "evidence_manifest": staged["manifest_path"],
            "evidence_chunks": staged["chunks_path"],
            "evidence_zip_sha256": bundle["evidence_zip_sha256"],
        }
        try:
            remote = client.call_tool(self.tool_name, arguments)
        except Exception as exc:
            return self._result(staged, "mcp_call_failed", str(exc), tier=tier, tool=self.tool_name, **bundle)
        observed_remote_status = remote_status(remote)
        package_id = remote_package_id(remote)
        upload_session_id = upload_session_id_from_result(remote)
        terminal_success = {"ok", "success", "completed", "ingested"}
        if package_id and observed_remote_status in terminal_success:
            status = "ingested"
            reason = ""
        elif upload_session_id:
            status = "upload_pending"
            reason = "OpenCrab returned a one-time upload session; complete the saved local runner handoff before confirmation."
        elif observed_remote_status in {"local_build_and_upload_ready", "plan_ready"}:
            status = "plan_ready"
            reason = "OpenCrab returned a local runner plan; no cloud package has been confirmed yet."
        elif observed_remote_status in {"ok", "success", "completed", "accepted", "queued", "processing"}:
            status = "remote_accepted"
            reason = "OpenCrab accepted the request but returned no package_id or upload session; completion is not confirmed."
        else:
            status = "remote_rejected"
            reason = "OpenCrab CrabAgent did not return a usable build or ingest confirmation"
        result = self._result(
            staged,
            status,
            reason,
            tier=tier,
            tool=self.tool_name,
            pack_name=pack_name,
            output_zip=output_zip,
            remote_status=observed_remote_status,
            package_id=package_id or None,
            upload_session_id=upload_session_id or None,
            next_action=(
                "Run the local builder/upload handoff recorded in result.json, then poll `crab pack status %s`." % staged["run_id"]
                if upload_session_id or status == "plan_ready"
                else "Review result.json and correct the OpenCrab MCP response before retrying."
            ),
            **bundle,
        )
        result["remote"] = remote
        self._write_result(staged["stage_path"], result)
        return result

    def poll_upload(self, upload_session_id: str) -> Dict[str, Any]:
        """Read the explicit OpenCrab upload session status without claiming completion."""
        session_id = str(upload_session_id or "").strip()
        if not session_id:
            return {"status": "invalid", "reason": "upload_session_id is required"}
        try:
            client = self.client_factory()
            return client.call_tool(self.tool_name, {"action": "status", "upload_session_id": session_id})
        except Exception as exc:
            return {"status": "mcp_unavailable", "reason": str(exc)}

    @staticmethod
    def _result(staged: Dict[str, Any], status: str, reason: str, **extra: Any) -> Dict[str, Any]:
        result = {
            "status": status,
            "reason": reason,
            "run_id": staged["run_id"],
            "root_path": staged["root_path"],
            "stage_path": staged["stage_path"],
            "manifest_path": staged["manifest_path"],
            "chunks_path": staged["chunks_path"],
            "source_count": staged["manifest"]["source_count"],
            "chunk_count": staged["manifest"]["chunk_count"],
        }
        result.update(extra)
        OpenCrabPackBuilder._write_result(staged["stage_path"], result)
        return result

    @staticmethod
    def _write_result(stage_path: str, result: Dict[str, Any]) -> None:
        Path(stage_path, "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
