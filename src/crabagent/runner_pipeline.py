"""Local OpenCrab pack build: managed runner, Kordoc parsing, verification and upload.

The OpenCrab MCP server cannot read the user's folder. This module closes the loop
on the user's machine: it installs the release runner the server names (HTTPS +
SHA-256), runs it over the folder (the runner parses PDF, HWP/HWPX, DOCX, XLSX and
PPTX through Kordoc, with OCR for scanned pages), verifies the ZIP, uploads it to
the one-time signed upload session and asks the server to process it. A pack is
reported as ingested only after the server returns a package_id.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.request import Request, urlopen


class RunnerPipelineError(RuntimeError):
    """A local build, verification or upload step failed; the message says which."""


RUNNER_FILE = "opencrab_desktop_build_runner.py"
REQUIREMENTS_FILE = "vector-requirements.txt"
TOKEN_HEADER = "X-OpenCrab-Upload-Token"
_TOKEN_RE = re.compile(r"X-OpenCrab-Upload-Token:\s*([A-Za-z0-9._~+/=-]+)")
SEMANTIC_LAYERS = ("full", "lean")
ORIGINS = ("source", "ai_generated")


def runner_home() -> Path:
    return Path(os.environ.get("OPENCRAB_RUNNER_HOME") or Path.home() / ".opencrab" / "bin")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _https(url: str) -> str:
    if not str(url).startswith("https://"):
        raise RunnerPipelineError("runner downloads require HTTPS: %s" % url)
    return str(url)


def default_fetch(url: str, timeout: float = 120.0) -> bytes:
    with urlopen(Request(_https(url), headers={"User-Agent": "kingcrab-runner"}), timeout=timeout) as response:
        return response.read()


def default_http(method: str, url: str, *, data: bytes = b"", headers: Optional[Dict[str, str]] = None, timeout: float = 600.0) -> Dict[str, Any]:
    request = Request(_https(url), data=data if method != "GET" else None, method=method, headers=headers or {})
    with urlopen(request, timeout=timeout) as response:
        body = response.read()
        try:
            parsed = json.loads(body.decode("utf-8") or "{}")
        except ValueError:
            parsed = {"raw": body[:400].decode("utf-8", "replace")}
        return {"status_code": response.status, "body": parsed}


def upload_token_from_command(command: str) -> str:
    match = _TOKEN_RE.search(command or "")
    return match.group(1) if match else ""


def kordoc_status(which: Callable[[str], Optional[str]] = shutil.which, run: Callable[..., Any] = subprocess.run) -> Dict[str, Any]:
    """Report the Kordoc parser the runner will use (4.x routes HWPX/DOCX/XLSX)."""
    configured = os.environ.get("KORDOC_BIN", "").strip()
    path = configured or which("kordoc") or ""
    if not path:
        return {"status": "missing", "path": "", "version": "", "install": "npm install --global kordoc@latest"}
    try:
        result = run([path, "--version"], capture_output=True, text=True, timeout=20, check=False)
        version = (result.stdout or result.stderr or "").strip().splitlines()[0] if (result.stdout or result.stderr) else ""
    except (OSError, subprocess.TimeoutExpired):
        version = ""
    major = re.search(r"(\d+)", version)
    ok = bool(major and int(major.group(1)) >= 4)
    return {
        "status": "ok" if ok else "outdated",
        "path": path,
        "version": version,
        "install": "" if ok else "npm install --global kordoc@latest",
    }


class RunnerPipeline:
    """Runs the OpenCrab release runner locally and hands its ZIP to an upload session."""

    def __init__(
        self,
        *,
        home: Optional[Path] = None,
        python: str = sys.executable,
        fetch: Callable[[str], bytes] = default_fetch,
        http: Callable[..., Dict[str, Any]] = default_http,
        run: Callable[..., Any] = subprocess.run,
        tool_name: str = "opencrab_crab_agent",
    ) -> None:
        self.home = (home or runner_home()).expanduser()
        self.python = python
        self.fetch = fetch
        self.http = http
        self.run = run
        self.tool_name = tool_name

    # ---------- runner release ----------
    def ensure_runner(self, client: Any) -> Dict[str, Any]:
        """Install the server's runner release when the local copy is missing or differs."""
        reply = client.call_tool(self.tool_name, {"action": "update_runner"})
        release = reply.get("runner_release") if isinstance(reply, dict) else None
        if not isinstance(release, dict) or not release.get("download_url") or not release.get("sha256"):
            raise RunnerPipelineError("OpenCrab did not return a runner release")
        self.home.mkdir(parents=True, exist_ok=True)
        runner = self.home / RUNNER_FILE
        updated = False
        if not runner.is_file() or sha256_file(runner) != release["sha256"]:
            data = self.fetch(str(release["download_url"]))
            if hashlib.sha256(data).hexdigest() != release["sha256"]:
                raise RunnerPipelineError("runner download failed its SHA-256 check; the existing runner was kept")
            self._atomic_write(runner, data)
            updated = True
        requirements = self.home / REQUIREMENTS_FILE
        if release.get("requirements_download_url") and release.get("requirements_sha256"):
            if not requirements.is_file() or sha256_file(requirements) != release["requirements_sha256"]:
                data = self.fetch(str(release["requirements_download_url"]))
                if hashlib.sha256(data).hexdigest() != release["requirements_sha256"]:
                    raise RunnerPipelineError("runner requirements failed their SHA-256 check")
                self._atomic_write(requirements, data)
        return {"path": str(runner), "requirements_path": str(requirements), "version": str(release.get("version") or ""), "updated": updated}

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".%s." % path.name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    # ---------- local build ----------
    def build_command(
        self,
        runner: str,
        source: Path,
        output_zip: Path,
        *,
        title: str,
        purpose: str = "",
        semantic_layer: str = "full",
        judgments: Optional[Path] = None,
        kordoc_bin: str = "",
    ) -> List[str]:
        if semantic_layer not in SEMANTIC_LAYERS:
            raise RunnerPipelineError("semantic_layer must be one of %s" % ", ".join(SEMANTIC_LAYERS))
        command = [self.python, runner, "--source", str(source), "--output-zip", str(output_zip), "--title", title]
        if purpose:
            command += ["--purpose", purpose]
        if semantic_layer != "full":
            command += ["--semantic-layer", semantic_layer]
        if judgments is not None:
            command += ["--judgments", str(judgments)]
        if kordoc_bin:
            command += ["--kordoc-bin", kordoc_bin]
        return command

    def _run_checked(self, command: List[str], step: str, timeout: float) -> str:
        try:
            result = self.run(command, capture_output=True, text=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            raise RunnerPipelineError("%s timed out after %ss" % (step, int(timeout)))
        except OSError as exc:
            raise RunnerPipelineError("%s could not start: %s" % (step, exc))
        if result.returncode != 0:
            tail = "\n".join(((result.stderr or "") + (result.stdout or "")).strip().splitlines()[-8:])
            raise RunnerPipelineError("%s failed (exit %s): %s" % (step, result.returncode, tail))
        return result.stdout or ""

    def build(self, runner: str, source: Path, output_zip: Path, *, timeout: float = 7200.0, **options: Any) -> Dict[str, Any]:
        output_zip.parent.mkdir(parents=True, exist_ok=True)
        command = self.build_command(runner, source, output_zip, **options)
        self._run_checked(command, "local pack build", timeout)
        if not output_zip.is_file():
            raise RunnerPipelineError("local pack build finished without writing %s" % output_zip)
        build_dir = output_zip.with_suffix("")
        judgment_request = next((p for p in (build_dir / "reports" / "judgment_request.json", output_zip.parent / "reports" / "judgment_request.json") if p.is_file()), None)
        return {
            "output_zip": str(output_zip),
            "zip_bytes": output_zip.stat().st_size,
            "zip_sha256": sha256_file(output_zip),
            "judgment_request": str(judgment_request) if judgment_request else None,
        }

    def verify(self, runner: str, output_zip: Path, *, timeout: float = 1800.0) -> str:
        return self._run_checked([self.python, runner, "--verify-upload-zip", str(output_zip)], "upload ZIP verification", timeout)

    # ---------- upload ----------
    def create_session(self, client: Any, *, pack_name: str, purpose: str, project_id: str = "", project_name: str = "", origin: str = "") -> Dict[str, Any]:
        if origin and origin not in ORIGINS:
            raise RunnerPipelineError("origin must be source or ai_generated")
        arguments: Dict[str, Any] = {"action": "create_upload_session", "pack_name": pack_name, "ontology_purpose": purpose, "share_scope": "private"}
        if project_id:
            arguments["project_id"] = project_id
        if project_name:
            arguments["project_name"] = project_name
        if origin:
            arguments["origin"] = origin
        reply = client.call_tool(self.tool_name, arguments)
        handoff = reply.get("saas_ingest_handoff") if isinstance(reply, dict) else None
        if not isinstance(handoff, dict) or not handoff.get("upload_session_id") or not handoff.get("upload_url"):
            status = reply.get("status") if isinstance(reply, dict) else None
            raise RunnerPipelineError("OpenCrab did not open an upload session (status=%s)" % status)
        token = upload_token_from_command(str(handoff.get("upload_command") or ""))
        if not token:
            raise RunnerPipelineError("the upload session did not include a finalize token")
        return {
            "upload_session_id": str(handoff["upload_session_id"]),
            "upload_url": str(handoff["upload_url"]),
            "finalize_url": str(handoff.get("upload_finalize_url") or ""),
            "method": str(handoff.get("upload_method") or "PUT_THEN_POST_FINALIZE"),
            "max_bytes": int(handoff.get("upload_max_bytes") or 0),
            "token": token,
        }

    def upload(self, session: Dict[str, Any], output_zip: Path) -> Dict[str, Any]:
        size = output_zip.stat().st_size
        if session.get("max_bytes") and size > int(session["max_bytes"]):
            raise RunnerPipelineError("pack ZIP is %d bytes; the upload limit is %d" % (size, session["max_bytes"]))
        put = self.http("PUT", session["upload_url"], data=output_zip.read_bytes(), headers={"Content-Type": "application/zip"})
        if int(put.get("status_code") or 0) >= 300:
            raise RunnerPipelineError("ZIP upload was refused (HTTP %s)" % put.get("status_code"))
        finalized: Dict[str, Any] = {}
        if session.get("method") == "PUT_THEN_POST_FINALIZE":
            if not session.get("finalize_url"):
                raise RunnerPipelineError("the upload session has no finalize URL")
            finalized = self.http("POST", session["finalize_url"], headers={TOKEN_HEADER: session["token"]})
            if int(finalized.get("status_code") or 0) >= 300:
                raise RunnerPipelineError("upload finalize was refused (HTTP %s)" % finalized.get("status_code"))
        return {"uploaded_bytes": size, "finalize": finalized.get("body") or {}}


def parse_judgments(text: str) -> Optional[Dict[str, Any]]:
    """Read the first JSON object with a judgments list from model text; None when absent."""
    if not text:
        return None
    decoder = json.JSONDecoder()
    index = text.find("{")
    while index >= 0:
        try:
            value, _ = decoder.raw_decode(text, index)
        except ValueError:
            index = text.find("{", index + 1)
            continue
        if isinstance(value, dict) and isinstance(value.get("judgments"), list):
            value.setdefault("version", 1)
            return value
        index = text.find("{", index + 1)
    return None
