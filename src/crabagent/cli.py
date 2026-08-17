from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import typer
from rich.console import Console
from rich.live import Live
from rich.table import Table

from . import __version__
from .benchmark import compare_goal, compare_observed
from .benchmark_runner import run_observed_benchmark
from .benchmark_suite import plan_suite, run_observed_suite
from .inverter import SolteluInverter
from .models import Role
from .mobile_gateway import MobileGateway, ensure_pairing
from .orchestration import summarize
from .panel import build_panel
from .onboarding import defer_opencrab, mark_local_ready, status as onboarding_status
from .protocol import DaemonClient, start_daemon
from .runtime import RuntimeService
from .store import ColonyStore
from .tui import run_tui
from .workspace_defaults import default_workspace


console = Console()
app = typer.Typer(add_completion=False, help="KINGCRAB - durable ontology-first agent runtime.")
doc_app = typer.Typer(add_completion=False, help="Open and route CRAB DOC manifests.")
pack_app = typer.Typer(add_completion=False, help="Build and ingest ontology packs from local project folders.")
orchestration_app = typer.Typer(add_completion=False, help="Plan and run bounded multi-session KINGCRAB orchestration.")
mobile_app = typer.Typer(add_completion=False, help="Pair a phone with the existing local crabd runtime.")
app.add_typer(doc_app, name="doc")
app.add_typer(pack_app, name="pack")
app.add_typer(orchestration_app, name="orchestrate")
app.add_typer(mobile_app, name="mobile")


@doc_app.command("import")
def import_crab_doc(
    manifest_path: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True, resolve_path=True),
    workspace: Path = typer.Option(default_workspace(), "--workspace", "-w", resolve_path=True),
) -> None:
    """Import a CRAB DOC `crab-doc/v1` manifest into this KINGCRAB workspace."""
    result = start_daemon(workspace).request("doc.import", manifest_path=str(manifest_path))
    console.print("[bold green]CRAB DOC imported[/bold green]")
    console.print("Title:    %s" % result.get("title", ""))
    console.print("Format:   %s" % result.get("format", ""))
    console.print("Manifest: %s" % result.get("manifest_path", ""))
    console.print("Context:  %s" % result.get("markdown_path", ""))


@doc_app.command("list")
def list_crab_docs(
    workspace: Path = typer.Option(default_workspace(), "--workspace", "-w", resolve_path=True),
    raw_json: bool = typer.Option(False, "--json", help="Print the document inventory as JSON."),
) -> None:
    """List CRAB DOC documents connected to this KINGCRAB workspace."""
    result = start_daemon(workspace).request("doc.list")
    if raw_json:
        console.print_json(json.dumps(result, ensure_ascii=False))
        return
    console.print("CRAB DOC documents: %s" % result.get("count", 0))
    for document in result.get("documents") or []:
        console.print("- %s  %s  %s" % (document.get("title", ""), document.get("format", ""), document.get("markdown_path", "")))


@pack_app.command("ingest")
def ingest_folder(
    folder_path: Path = typer.Argument(..., exists=True, file_okay=False, dir_okay=True, readable=True, resolve_path=True),
    project_id: str = typer.Option("", "--project-id", help="Existing CrabAgent project ID."),
    project_name: str = typer.Option("", "--project-name", help="Create or use a local project name."),
    ontology_purpose: str = typer.Option("", "--purpose", help="Purpose of the ontology pack; a safe default is used when omitted."),
    workspace: Path = typer.Option(default_workspace(), "--workspace", "-w", resolve_path=True),
) -> None:
    """Ask the Expert-gated OpenCrab CrabAgent MCP to build and ingest a folder."""
    result = start_daemon(workspace).request(
        "pack.ingest",
        folder_path=str(folder_path),
        project_id=project_id,
        project_name=project_name,
        ontology_purpose=ontology_purpose,
    )
    status = str(result.get("status") or "unknown")
    console.print("Pack build status: %s" % status)
    console.print("Sources: %s  Evidence chunks: %s" % (result.get("source_count", 0), result.get("chunk_count", 0)))
    console.print("Stage: %s" % result.get("stage_path", ""))
    console.print("Evidence ZIP: %s" % result.get("evidence_zip_path", "not created"))
    if result.get("package_id"):
        console.print("Package: %s" % result["package_id"])
    if result.get("reason"):
        console.print("Reason: %s" % result["reason"])
    if status in {"plan_ready", "upload_pending"}:
        console.print("OpenCrab returned a local build/upload handoff in result.json.")
        console.print("Complete that handoff, then run: crab pack status %s" % result.get("run_id", "RUN_ID"))
    elif status == "ingested":
        console.print("OpenCrab CrabAgent confirmed the cloud package identity.")
    elif status != "ingested":
        console.print("No remote ingest is claimed without an observed OpenCrab MCP confirmation.")


@pack_app.command("status")
def pack_status(
    run_id: str = typer.Argument(..., help="Pack ingest run ID returned by `crab pack ingest`."),
    workspace: Path = typer.Option(default_workspace(), "--workspace", "-w", resolve_path=True),
) -> None:
    """Poll the OpenCrab upload session saved with a local pack build plan."""
    try:
        result = start_daemon(workspace).request("pack.ingest.status", run_id=run_id)
    except Exception as exc:
        console.print("Pack ingest status unavailable: %s" % exc)
        raise typer.Exit(code=1)
    nested = result.get("result") if isinstance(result.get("result"), dict) else result
    console.print("Pack ingest status: %s" % nested.get("status", result.get("status", "unknown")))
    if nested.get("evidence_zip_path"):
        console.print("Evidence ZIP: %s" % nested["evidence_zip_path"])
    if nested.get("package_id"):
        console.print("Package: %s" % nested["package_id"])
    if nested.get("reason"):
        console.print("Reason: %s" % nested["reason"])


def _orchestration_child_spec(spec: str, default_session_id: str) -> Dict[str, str]:
    value = str(spec or "").strip()
    if "::" in value:
        session_id, objective = value.split("::", 1)
    else:
        session_id, objective = default_session_id, value
    if not session_id.strip() or not objective.strip():
        raise typer.BadParameter("child format is SESSION_ID::OBJECTIVE")
    return {"session_id": session_id.strip(), "objective": objective.strip()}


@orchestration_app.command("plan")
def orchestration_plan(
    objective: str = typer.Argument("", help="Objective for the current session when --child is omitted."),
    child: List[str] = typer.Option([], "--child", help="Child mission in SESSION_ID::OBJECTIVE form; repeatable."),
    max_parallel: int = typer.Option(2, "--max-parallel", min=1, max=8),
    title: str = typer.Option("KINGCRAB orchestration", "--title"),
    workspace: Path = typer.Option(default_workspace(), "--workspace", "-w", resolve_path=True),
    raw_json: bool = typer.Option(False, "--json", help="Print the durable orchestration state as JSON."),
) -> None:
    """Create a durable fan-out plan without starting provider work."""
    client = start_daemon(workspace)
    session = client.request("session.ensure")
    specs = child or ([objective] if objective.strip() else [])
    if not specs:
        raise typer.BadParameter("provide an objective or at least one --child")
    children = [_orchestration_child_spec(spec, str(session["session_id"])) for spec in specs]
    result = client.request(
        "orchestration.plan",
        session_id=session["session_id"],
        children=children,
        max_parallel=max_parallel,
        title=title,
    )
    if raw_json:
        console.print_json(json.dumps(result, ensure_ascii=False))
        return
    state = result["state"]
    console.print("Orchestration planned: %s" % state["orchestration_id"])
    console.print("Children: %d  Max parallel: %d" % (len(state.get("children") or []), state.get("max_parallel", 1)))
    console.print("Run: crab orchestrate run %s --workspace %s" % (state["orchestration_id"], workspace))


@orchestration_app.command("run")
def orchestration_run(
    orchestration_id: str = typer.Argument(...),
    workspace: Path = typer.Option(default_workspace(), "--workspace", "-w", resolve_path=True),
) -> None:
    """Start a previously planned orchestration; only real child jobs are counted."""
    result = start_daemon(workspace).request("orchestration.run", orchestration_id=orchestration_id)
    console.print("Orchestration %s: %s" % (orchestration_id, result.get("status", "started")))
    console.print("Children: %s" % json.dumps(result.get("summary") or summarize(result.get("state") or {}), ensure_ascii=False))


@orchestration_app.command("status")
def orchestration_status(
    orchestration_id: str = typer.Argument("", help="Optional orchestration ID."),
    workspace: Path = typer.Option(default_workspace(), "--workspace", "-w", resolve_path=True),
    raw_json: bool = typer.Option(False, "--json"),
) -> None:
    """Inspect persisted orchestration state after a fresh process."""
    result = start_daemon(workspace).request("orchestration.status", orchestration_id=orchestration_id)
    if raw_json:
        console.print_json(json.dumps(result, ensure_ascii=False))
        return
    if orchestration_id:
        state = result["state"]
        console.print("%s  %s" % (state["orchestration_id"], state.get("status")))
        for child in state.get("children") or []:
            console.print("- %s  %s  %s" % (child.get("child_id"), child.get("status"), child.get("objective")))
        return
    for summary in result.get("summaries") or []:
        console.print("- %s  %s  %s child(ren)" % (summary.get("orchestration_id"), summary.get("status"), summary.get("child_count", 0)))


@orchestration_app.command("stop")
def orchestration_stop(
    orchestration_id: str = typer.Argument(...),
    workspace: Path = typer.Option(default_workspace(), "--workspace", "-w", resolve_path=True),
) -> None:
    """Stop the coordinator and interrupt currently running child sessions."""
    result = start_daemon(workspace).request("orchestration.stop", orchestration_id=orchestration_id)
    console.print("Orchestration %s: %s" % (orchestration_id, result.get("status", "stopping")))


@mobile_app.command("pair")
def mobile_pair(
    workspace: Path = typer.Option(default_workspace(), "--workspace", "-w", resolve_path=True),
) -> None:
    """Create or read the local phone pairing token without starting a server."""
    pair = ensure_pairing(workspace)
    console.print("Pairing file: %s" % pair["path"])
    console.print("Pair token: %s" % pair["token"])
    console.print("The token is stored locally with owner-only permissions.")


@mobile_app.command("status")
def mobile_status(
    workspace: Path = typer.Option(default_workspace(), "--workspace", "-w", resolve_path=True),
) -> None:
    """Show pairing state and whether the shared crabd runtime is online."""
    pair = ensure_pairing(workspace)
    client = DaemonClient(workspace)
    console.print("Pairing: ready (%s)" % pair["path"])
    console.print("crabd: %s" % ("online" if client.ping() else "offline"))


@mobile_app.command("start")
def mobile_start(
    host: str = typer.Option("127.0.0.1", "--host", help="Use 0.0.0.0 only on a trusted LAN."),
    port: int = typer.Option(8787, "--port", min=0, max=65535),
    workspace: Path = typer.Option(default_workspace(), "--workspace", "-w", resolve_path=True),
) -> None:
    """Run the authenticated mobile panel/API over the existing crabd socket."""
    client = start_daemon(workspace)
    gateway = MobileGateway(workspace, client=client)
    if host in {"0.0.0.0", "::", ""}:
        console.print("[yellow]LAN mode exposes the panel to the local network; keep the token private.[/yellow]")
    try:
        gateway.serve_forever(host, port)
    except KeyboardInterrupt:
        console.print("Mobile gateway stopped")


@app.command("setup")
def setup(
    action: str = typer.Argument("status", help="status, codex, local, later, or opencrab"),
    endpoint: str = typer.Argument("", help="OpenCrab MCP URL when action is opencrab."),
    workspace: Path = typer.Option(default_workspace(), "--workspace", "-w", resolve_path=True),
) -> None:
    """Run the local-first setup checks without requiring OpenCrab login."""
    state = onboarding_status(workspace)
    action = action.lower()
    if action == "status":
        codex = state.get("codex") or {}
        assets = state.get("assets") or {}
        opencrab = state.get("opencrab") or {}
        console.print("KINGCRAB setup")
        console.print("Codex:     %s" % (codex.get("login_status") or codex.get("status", "unknown")))
        console.print("MCP:       %s observed" % assets.get("mcp_count", 0))
        console.print("CLI:       %s observed" % assets.get("cli_count", 0))
        console.print("Skills:    %s discovered" % assets.get("skill_count", 0))
        console.print("OpenCrab:  %s" % opencrab.get("status", "not_connected"))
        console.print("Phase:     %s" % state.get("phase", "local_setup"))
        return
    if action == "codex":
        if (state.get("codex") or {}).get("login_status") != "connected":
            console.print("Codex is not connected. Run `codex login`, then run `crab setup codex` again.")
            raise typer.Exit(code=1)
        result = mark_local_ready(workspace)
        console.print("KINGCRAB local setup ready")
        console.print("OpenCrab: %s" % (result.get("opencrab") or {}).get("status", "deferred"))
        return
    if action == "local":
        result = mark_local_ready(workspace)
        console.print("KINGCRAB local mode ready")
        console.print("OpenCrab connection remains %s" % (result.get("opencrab") or {}).get("status", "deferred"))
        return
    if action == "later":
        result = defer_opencrab(workspace)
        console.print("OpenCrab connection deferred")
        console.print("Phase: %s" % result.get("phase"))
        return
    if action == "opencrab":
        if not endpoint:
            raise typer.BadParameter("provide an OpenCrab MCP URL after `opencrab`")
        try:
            result = start_daemon(workspace).request("onboarding.opencrab.connect", endpoint=endpoint)
        except Exception as exc:
            console.print("OpenCrab connection not confirmed: %s" % exc)
            raise typer.Exit(code=1)
        console.print("OpenCrab connected")
        console.print("Tier: %s" % (result.get("account") or "unknown"))
        return
    raise typer.BadParameter("setup action must be status, codex, local, later, or opencrab")


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version", help="Show the version and exit."),
) -> None:
    if version:
        console.print("crab, version %s" % __version__)
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        if sys.stdin.isatty() and sys.stdout.isatty():
            run_tui(default_workspace())
        else:
            console.print(build_panel(default_workspace()))


@app.command("init")
def initialize(
    workspace: Path = typer.Option(default_workspace(), "--workspace", "-w", resolve_path=True),
) -> None:
    """Initialize durable CrabAgent state in a workspace."""
    result = RuntimeService(workspace).initialize()
    console.print("[bold green]Initialized CrabAgent[/bold green]")
    console.print("Workspace: %s" % result["workspace"])
    console.print("Database:  %s" % result["database"])


@app.command()
def run(
    objective: str = typer.Argument(..., help="Mission objective."),
    demo: bool = typer.Option(False, "--demo", help="Use the zero-cost deterministic executor."),
    plan_only: bool = typer.Option(False, "--plan-only", help="Persist routes without invoking Codex."),
    max_workers: int = typer.Option(3, min=1, max=8),
    workspace: Path = typer.Option(default_workspace(), "--workspace", "-w", resolve_path=True),
) -> None:
    """Submit a real durable mission; `crab` opens the live command deck."""
    RuntimeService(workspace).initialize()
    client = start_daemon(workspace)
    if demo:
        session = client.request("session.ensure")
        result = client.request(
            "run_demo",
            objective=objective,
            session_id=session["session_id"],
        )
    elif plan_only:
        result = client.request("plan", objective=objective, max_workers=max_workers)
    else:
        session = client.request("session.ensure")
        session = client.request(
            "session.configure",
            session_id=session["session_id"],
            model_policy=str(session.get("model_policy") or "auto"),
            interaction_mode=str(session.get("interaction_mode") or "auto"),
            mcp_policy=str(session.get("mcp_policy") or "auto"),
            cli_policy=str(session.get("cli_policy") or "auto"),
            max_workers=max_workers,
        )
        queued = client.request("prompt.submit", session_id=session["session_id"], objective=objective, disposition="start", interaction="colony")
        console.print("[bold green]Mission submitted[/bold green] to session %s" % session["session_id"])
        console.print("Status: %s" % queued["status"])
        console.print("Open `crab` to watch, guide, queue, or interrupt it.")
        return
    mission = result["mission"]
    console.print("[bold]Mission[/bold] %s" % mission["mission_id"])
    console.print("Status: %s" % mission["status"])
    goal_plan = result.get("goal_plan") or {}
    if goal_plan:
        console.print(
            "Goal: %s  ·  stages: %s  ·  estimated model turns: %s  ·  token target: %s"
            % (
                goal_plan.get("kind", "unknown"),
                " -> ".join(goal_plan.get("stages") or []),
                goal_plan.get("estimated_model_turns", "unknown"),
                goal_plan.get("token_budget", "unknown"),
            )
        )
    if demo:
        console.print("Executor: deterministic_demo (0 observed model tokens)")
        console.print("Oracle: %s" % mission.get("oracle_result_artifact_id"))
    else:
        console.print("[yellow]Routes are planned only; no Codex invocation occurred.[/yellow]")


@app.command()
def status(
    workspace: Path = typer.Option(default_workspace(), "--workspace", "-w", resolve_path=True),
) -> None:
    """Show persisted mission status and observed daemon state."""
    service = RuntimeService(workspace)
    service.initialize()
    client = DaemonClient(workspace)
    result = service.status()
    console.print("Runtime: %s" % ("online" if client.ping() else "offline"))
    console.print("Database: %s" % result["database"])
    console.print("Missions: %s" % result["mission_count"])
    session = result.get("latest_session")
    message = result.get("latest_message")
    if session:
        thread = str(session.get("codex_thread_id") or "")
        console.print("Session: %s  %s" % (session["session_id"], session["status"]))
        console.print("Codex thread: %s" % (thread or "not connected"))
    if message and (message.get("metadata") or {}).get("interaction") == "chat":
        console.print("Current: persistent chat ready")
        return
    latest = result.get("latest_mission")
    if latest:
        console.print("Latest: %s  %s" % (latest["mission_id"], latest["status"]))
        console.print("Objective: %s" % latest["objective"])


@app.command("retry")
def retry_mission(
    force: bool = typer.Option(False, "--force", help="Allow retry after an observed workspace change."),
    workspace: Path = typer.Option(default_workspace(), "--workspace", "-w", resolve_path=True),
) -> None:
    """Retry the latest failed/cancelled mission through the durable runtime."""
    client = start_daemon(workspace)
    session = client.request("session.ensure")
    result = client.request("mission.retry", session_id=session["session_id"], force=force)
    console.print("Retry submitted: %s" % result.get("objective", ""))
    console.print("Source mission: %s" % (result.get("retry_of") or "unknown"))
    console.print("Status: %s" % result.get("status", "starting"))


@app.command("ontology")
def ontology_inventory(
    action: str = typer.Argument("show", help="show the live Workspace catalog, diff local cache, or explicitly sync."),
    raw_json: bool = typer.Option(False, "--json", help="Print the returned diff or snapshot as JSON."),
    workspace: Path = typer.Option(default_workspace(), "--workspace", "-w", resolve_path=True),
) -> None:
    """Read the live OpenCrab Workspace catalog; `sync` remains an explicit full audit."""
    action = action.lower()
    client = start_daemon(workspace)
    if action == "sync":
        result = client.request("ontology.sync")
        if result.get("status") != "ok":
            raise typer.BadParameter(str(result.get("error") or "OpenCrab sync failed"))
        if raw_json:
            console.print_json(json.dumps({"diff": result.get("diff"), "paths": result.get("paths")}, ensure_ascii=False))
            return
        diff = result.get("diff") or {}
        summary = diff.get("summary") or {}
        console.print("OpenCrab inventory synchronized")
        console.print("Added: %s  Removed: %s  Changed: %s" % (summary.get("added", 0), summary.get("removed", 0), summary.get("changed", 0)))
        console.print("Current: %s" % (result.get("paths") or {}).get("current", ""))
        console.print("Diff:    %s" % (result.get("paths") or {}).get("diff", ""))
        return
    if action == "show":
        result = client.request("ontology.catalog", complete=True)
        if raw_json:
            console.print_json(json.dumps(result, ensure_ascii=False))
            return
        if result.get("status") != "ok":
            console.print("OpenCrab live Workspace catalog unavailable: %s" % result.get("error", "unknown error"))
            return
        payload = result.get("payload") or {}
        account = payload.get("account") or {}
        console.print("OpenCrab live Workspace catalog")
        console.print("Collected: %s" % payload.get("collected_at", "unknown"))
        console.print("Tier: %s  Scope: %s" % (account.get("tier", "unknown"), payload.get("access_scope", "unknown")))
        for group in ("packs", "projects"):
            value = payload.get(group) or {}
            status = value.get("status") or "unknown"
            count = value.get("total") if value.get("total") is not None else len(value.get("items") or [])
            suffix = " (%s)" % status if status not in {"ok", "unknown"} else ""
            console.print("%s: %s%s" % (group.capitalize(), count, suffix))
        console.print("Selection lives in the current CrabAgent session; no full JSON snapshot was written.")
        return
    if action == "diff":
        result = client.request("ontology.diff")
        if raw_json:
            console.print_json(json.dumps(result, ensure_ascii=False))
            return
        if result.get("status") != "ok":
            console.print("OpenCrab diff unavailable: %s" % result.get("error", "unknown error"))
            return
        diff = result.get("diff") or {}
        summary = diff.get("summary") or {}
        console.print("OpenCrab inventory diff")
        console.print("Added: %s  Removed: %s  Changed: %s" % (summary.get("added", 0), summary.get("removed", 0), summary.get("changed", 0)))
        for group, value in (diff.get("groups") or {}).items():
            added = [str(row.get(value.get("identity"))) for row in value.get("added", [])[:10]]
            removed = [str(row.get(value.get("identity"))) for row in value.get("removed", [])[:10]]
            changed = [str(row.get(value.get("identity"))) for row in value.get("changed", [])[:10]]
            if added or removed or changed:
                console.print("%s: +%s -%s ~%s" % (group, ", ".join(added) or "0", ", ".join(removed) or "0", ", ".join(changed) or "0"))
        console.print("Diff: %s" % (result.get("paths") or {}).get("diff", ""))
        return
    raise typer.BadParameter("Use show, diff, or sync")


def _resolve_mission(store: ColonyStore, mission_id: Optional[str], latest: bool) -> str:
    if mission_id:
        return mission_id
    if latest:
        mission = store.latest_mission()
        if mission is not None:
            return str(mission["mission_id"])
    raise typer.BadParameter("Provide MISSION_ID or use --latest.")


@app.command("inspect")
def inspect_mission(
    mission_id: Optional[str] = typer.Argument(None),
    latest: bool = typer.Option(False, "--latest"),
    raw_json: bool = typer.Option(False, "--json"),
    workspace: Path = typer.Option(default_workspace(), "--workspace", "-w", resolve_path=True),
) -> None:
    """Inspect the complete persisted state for one mission."""
    store = ColonyStore(workspace)
    store.initialize()
    resolved = _resolve_mission(store, mission_id, latest)
    snapshot = store.inspect(resolved)
    if raw_json:
        console.print_json(json.dumps(snapshot, ensure_ascii=False))
        return
    console.print(build_panel(workspace))
    console.print("\nArtifacts:")
    for artifact in snapshot["artifacts"]:
        console.print("- %s  %s  %s" % (artifact["status"], artifact["sha256"][:12], artifact["path"]))


@app.command()
def replay(
    mission_id: Optional[str] = typer.Argument(None),
    latest: bool = typer.Option(False, "--latest"),
    workspace: Path = typer.Option(default_workspace(), "--workspace", "-w", resolve_path=True),
) -> None:
    """Replay the ordered, append-only mission event log."""
    store = ColonyStore(workspace)
    store.initialize()
    resolved = _resolve_mission(store, mission_id, latest)
    table = Table("ID", "Time", "Actor", "Event", "Receipt")
    for event in store.events(resolved):
        receipt = event.payload.get("receipt_id") or event.payload.get("artifact_id") or ""
        table.add_row(
            str(event.event_id),
            event.created_at[11:19],
            event.actor,
            event.event_type,
            str(receipt),
        )
    console.print(table)


@app.command()
def panel(
    once: bool = typer.Option(False, "--once", help="Render one observed snapshot and exit."),
    interval: float = typer.Option(1.0, min=0.2, max=30.0),
    workspace: Path = typer.Option(default_workspace(), "--workspace", "-w", resolve_path=True),
) -> None:
    """Show the read-only operational command deck."""
    if once:
        console.print(build_panel(workspace))
        return
    with Live(build_panel(workspace), console=console, refresh_per_second=4) as live:
        try:
            while True:
                time.sleep(interval)
                live.update(build_panel(workspace))
        except KeyboardInterrupt:
            return


@app.command()
def route(
    role: Role = typer.Argument(..., case_sensitive=False),
) -> None:
    """Show a Codex/SOLTELU route intention without invoking a model."""
    decision = SolteluInverter().plan(role)
    console.print_json(json.dumps(decision.to_dict(), ensure_ascii=False))


@app.command("benchmark")
def benchmark(
    objective: str = typer.Argument(..., help="One goal to compare against the direct-turn baseline."),
    selected_pack_count: int = typer.Option(0, "--packs", min=0, max=200),
    selected_project_count: int = typer.Option(0, "--projects", min=0, max=50),
    raw_json: bool = typer.Option(False, "--json"),
) -> None:
    """Compare goal-centered routing with an honest direct-turn baseline."""
    result = compare_goal(
        objective,
        selected_pack_count=selected_pack_count,
        selected_project_count=selected_project_count,
    )
    if raw_json:
        console.print_json(json.dumps(result, ensure_ascii=False))
        return
    direct = result["direct_baseline"]
    adaptive = result["adaptive"]
    delta = result["delta"]
    console.print("KINGCRAB route benchmark (planned only)")
    console.print("Goal: %s" % result["objective"])
    console.print("Direct:   %s model turn · %s OpenCrab MCP calls" % (direct["estimated_model_turns"], direct["planned_mcp_calls"]))
    console.print("Adaptive: %s roles · %s model routes · %s estimated turns · %s MCP calls · %s local gates" % (len(adaptive["roles"]), adaptive["planned_model_routes"], adaptive["estimated_model_turns"], adaptive["planned_mcp_calls"], adaptive["local_gates"]))
    console.print("Delta:    %s model turns vs direct · %s MCP calls vs direct" % (delta["estimated_model_turns"], delta["planned_mcp_calls"]))
    console.print("Stages:   %s" % " -> ".join(adaptive["roles"]))
    console.print("Guard:    %s" % result["guardrail"])


@app.command("benchmark-observed")
def benchmark_observed(
    adaptive_snapshot: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True, resolve_path=True),
    direct_snapshot: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True, resolve_path=True),
    raw_json: bool = typer.Option(False, "--json"),
) -> None:
    """Compare two already-observed mission JSON snapshots."""
    try:
        adaptive = json.loads(adaptive_snapshot.read_text(encoding="utf-8"))
        direct = json.loads(direct_snapshot.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise typer.BadParameter("snapshot JSON cannot be read: %s" % exc)
    if not isinstance(adaptive, dict) or not isinstance(direct, dict):
        raise typer.BadParameter("each snapshot must contain a JSON object")
    result = compare_observed(adaptive, direct)
    if raw_json:
        console.print_json(json.dumps(result, ensure_ascii=False))
        return
    console.print("KINGCRAB observed benchmark")
    console.print("Adaptive: %s" % result["adaptive"])
    console.print("Direct:   %s" % result["direct_baseline"])
    console.print("Delta:    %s" % result["delta"])
    console.print("Verdict:  %s" % result["verdict"])
    if result["unknowns"]:
        console.print("Unknown:  %s" % ", ".join(result["unknowns"]))
    console.print("Guard:    %s" % result["guardrail"])


@app.command("benchmark-run")
def benchmark_run(
    objective: str = typer.Argument(..., help="The identical goal used by both paths."),
    execute: bool = typer.Option(False, "--execute", help="Actually invoke Codex twice and record observed receipts."),
    session_id: str = typer.Option("", "--session-id", help="Reuse this session's selected OpenCrab context."),
    timeout_seconds: float = typer.Option(900.0, "--timeout", min=10.0, max=7200.0),
    order: str = typer.Option("direct-first", "--order", help="Run order: direct-first or adaptive-first."),
    output_dir: Optional[Path] = typer.Option(None, "--output-dir", resolve_path=True),
    raw_json: bool = typer.Option(False, "--json"),
    workspace: Path = typer.Option(default_workspace(), "--workspace", "-w", resolve_path=True),
) -> None:
    """Run a same-context direct-vs-KINGCRAB benchmark.

    This command is intentionally opt-in because it spends provider tokens on
    two real runs. Without --execute it only explains the boundary and does
    not claim a benchmark result.
    """
    if not execute:
        result = compare_goal(objective)
        if raw_json:
            console.print_json(json.dumps({"measurement": "planned_only", "benchmark_execute_required": True, "plan": result}, ensure_ascii=False))
            return
        console.print("No provider call made.")
        console.print("Run again with --execute to compare the same goal through direct and KINGCRAB paths.")
        console.print("Guard: observed tokens, same context, and both result gates are required.")
        return
    try:
        result = run_observed_benchmark(
            workspace,
            objective,
            source_session_id=session_id,
            timeout_seconds=timeout_seconds,
            output_dir=output_dir,
            order=order,
        )
    except Exception as exc:
        console.print("Benchmark failed before a valid comparison: %s: %s" % (type(exc).__name__, exc))
        raise typer.Exit(code=1)
    if raw_json:
        console.print_json(json.dumps(result, ensure_ascii=False))
        return
    comparison = result.get("comparison") or {}
    console.print("KINGCRAB observed A/B benchmark")
    console.print("Run:      %s" % result.get("run_id"))
    console.print("Order:    %s" % result.get("order"))
    console.print("Verdict:  %s" % comparison.get("verdict", "inconclusive"))
    console.print("Direct:   %s" % comparison.get("direct_baseline"))
    console.print("Adaptive: %s" % comparison.get("adaptive"))
    if comparison.get("unknowns"):
        console.print("Unknown:  %s" % ", ".join(comparison["unknowns"]))
    console.print("Artifacts: %s" % result.get("paths", {}).get("root", ""))


@app.command("benchmark-suite")
def benchmark_suite(
    execute: bool = typer.Option(False, "--execute", help="Run every selected case through Codex twice."),
    cases: str = typer.Option("all", "--cases", help="Comma-separated cases: code_write, exact_ontology_lookup, semantic_ontology, graph_ontology."),
    session_id: str = typer.Option("", "--session-id", help="Reuse this session's selected OpenCrab context."),
    timeout_seconds: float = typer.Option(900.0, "--timeout", min=10.0, max=7200.0),
    order: str = typer.Option("adaptive-first", "--order", help="Run order for every case: direct-first or adaptive-first."),
    output_dir: Optional[Path] = typer.Option(None, "--output-dir", resolve_path=True),
    raw_json: bool = typer.Option(False, "--json"),
    workspace: Path = typer.Option(default_workspace(), "--workspace", "-w", resolve_path=True),
) -> None:
    """Run or explain the explicit KINGCRAB goal matrix."""
    case_ids = [] if cases.strip().lower() in {"", "all"} else [value.strip() for value in cases.split(",") if value.strip()]
    try:
        if not execute:
            result = plan_suite(case_ids=case_ids)
            if raw_json:
                console.print_json(json.dumps(result, ensure_ascii=False))
                return
            console.print("KINGCRAB benchmark suite (planned only)")
            for case in result["cases"]:
                adaptive = case["plan"]["adaptive"]
                console.print("- %s: %s -> %s" % (case["id"], case["plan"]["objective"], " -> ".join(adaptive["roles"])))
            console.print(result["guardrail"])
            return
        result = run_observed_suite(
            workspace,
            source_session_id=session_id,
            case_ids=case_ids,
            timeout_seconds=timeout_seconds,
            output_dir=output_dir,
            order=order,
        )
    except Exception as exc:
        console.print("Benchmark suite failed: %s: %s" % (type(exc).__name__, exc))
        raise typer.Exit(code=1)
    if raw_json:
        console.print_json(json.dumps(result, ensure_ascii=False))
        return
    console.print("KINGCRAB observed benchmark suite")
    console.print("Cases:    %s / %s" % (result.get("case_count", 0), result.get("case_count", 0) + len(result.get("failures") or [])))
    console.print("Claim:    %s" % result.get("claim", "inconclusive"))
    console.print("Quality:  %s structural advantages" % result.get("structural_quality_advantage_count", 0))
    console.print("Contract: %s accepted · %s advantages" % (result.get("goal_contract_accepted_count", 0), result.get("goal_contract_advantage_count", 0)))
    console.print("Tokens:   %s total-token advantages · %s billable-input advantages" % (result.get("total_token_advantage_count", 0), result.get("billable_input_advantage_count", 0)))
    console.print("Artifacts: %s" % result.get("paths", {}).get("root", ""))


if __name__ == "__main__":
    app()
