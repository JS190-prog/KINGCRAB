"""`crab doctor`: what the local pack build and the model providers need, and how to fix gaps."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List

from .discovery import claude_login_status, codex_login_status
from .runner_pipeline import REQUIREMENTS_FILE, RUNNER_FILE, kordoc_status, runner_home


def _check(name: str, status: str, detail: str = "", fix: str = "") -> Dict[str, str]:
    return {"name": name, "status": status, "detail": detail, "fix": fix}


def diagnose(which: Callable[[str], Any] = shutil.which, run: Callable[..., Any] = subprocess.run) -> Dict[str, Any]:
    checks: List[Dict[str, str]] = []
    version = sys.version_info
    checks.append(_check(
        "python",
        "ok" if version >= (3, 9) else "outdated",
        "%d.%d.%d" % version[:3],
        "" if version >= (3, 9) else "Install Python 3.9 or newer.",
    ))
    home = runner_home()
    runner = home / RUNNER_FILE
    checks.append(_check(
        "opencrab_runner",
        "ok" if runner.is_file() else "missing",
        str(runner),
        "" if runner.is_file() else "Runs automatically on the first `crab pack build` (downloaded from opencrab.sh and SHA-256 checked).",
    ))
    requirements = home / REQUIREMENTS_FILE
    checks.append(_check(
        "runner_python_packages",
        "unknown" if requirements.is_file() else "missing",
        str(requirements),
        "%s -m pip install -r %s" % (sys.executable, requirements),
    ))
    kordoc = kordoc_status(which=which, run=run)
    checks.append(_check(
        "kordoc",
        kordoc["status"],
        ("%s %s" % (kordoc["path"], kordoc["version"])).strip(),
        kordoc["install"] and "%s  (parses HWP/HWPX, DOCX, XLSX, PPTX, PDF)" % kordoc["install"],
    ))
    if not which("npm") and kordoc["status"] != "ok":
        checks.append(_check("npm", "missing", "", "Install Node.js (npm) to install Kordoc."))
    tesseract = which("tesseract")
    checks.append(_check("ocr_tesseract", "ok" if tesseract else "optional", tesseract or "", "" if tesseract else "brew install tesseract tesseract-lang  (only for scanned PDFs)"))
    codex = which("codex")
    claude = which("claude")
    checks.append(_check("codex", codex_login_status(codex) if codex else "missing", codex or "", "" if codex else "npm install --global @openai/codex"))
    checks.append(_check("claude", claude_login_status(claude) if claude else "missing", claude or "", "" if claude else "npm install --global @anthropic-ai/claude-code"))
    if not codex and not claude:
        checks.append(_check("provider", "missing", "", "Install Codex or Claude Code; KINGCRAB needs one model provider."))
    blocking = [row for row in checks if row["status"] in {"missing", "outdated"} and row["name"] in {"python", "kordoc", "provider"}]
    return {"status": "ready" if not blocking else "needs_setup", "checks": checks}


def install_missing(run: Callable[..., Any] = subprocess.run, which: Callable[[str], Any] = shutil.which) -> List[Dict[str, Any]]:
    """Install the runner's Python packages and Kordoc 4.x; credentials are never touched."""
    steps: List[Dict[str, Any]] = []
    requirements = runner_home() / REQUIREMENTS_FILE
    if requirements.is_file():
        result = run([sys.executable, "-m", "pip", "install", "--upgrade", "-r", str(requirements)], check=False)
        steps.append({"step": "runner_python_packages", "exit_code": result.returncode})
    if kordoc_status(which=which, run=run)["status"] != "ok":
        npm = which("npm")
        if npm:
            result = run([npm, "install", "--global", "kordoc@latest"], check=False)
            steps.append({"step": "kordoc", "exit_code": result.returncode})
        else:
            steps.append({"step": "kordoc", "exit_code": None, "reason": "npm is missing"})
    return steps


def requirements_path() -> Path:
    return runner_home() / REQUIREMENTS_FILE
