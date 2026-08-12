from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .protocol import DaemonClient
from .store import ColonyStore


ROLE_STYLES = {
    "KING": "bold bright_red",
    "QUEEN": "bold magenta",
    "WORKER": "cyan",
    "SOLDIER": "yellow",
    "ORACLE": "bold bright_green",
}


def build_panel(workspace: Path) -> Panel:
    store = ColonyStore(workspace)
    store.initialize()
    client = DaemonClient(workspace)
    online = client.ping()
    latest = store.latest_mission()

    header = Table.grid(expand=True)
    header.add_column(ratio=2)
    header.add_column(justify="right")
    header.add_row(
        Text("CRABAGENT / COMMAND DECK", style="bold bright_red"),
        Text("crabd ONLINE" if online else "crabd OFFLINE", style="green" if online else "red"),
    )
    if latest is None:
        return Panel(Group(header, Text("No observed missions.")), border_style="red")

    mission_id = str(latest["mission_id"])
    snapshot = store.inspect(mission_id)
    mission = snapshot["mission"]
    summary = Table.grid(expand=True)
    summary.add_column(style="dim", width=12)
    summary.add_column()
    summary.add_row("Mission", mission_id)
    summary.add_row("Status", str(mission["status"]))
    summary.add_row("Objective", str(mission["objective"]))

    tasks = Table(expand=True, box=None)
    tasks.add_column("#", width=3)
    tasks.add_column("Role", width=9)
    tasks.add_column("Task")
    tasks.add_column("State", width=11)
    tasks.add_column("Route", width=24)
    assignment_by_task = {row["task_id"]: row for row in snapshot["assignments"]}
    for task in snapshot["tasks"]:
        assignment = assignment_by_task.get(task["task_id"], {})
        route = "%s/%s · %s" % (
            assignment.get("profile", "-"),
            assignment.get("effort", "-"),
            assignment.get("invocation_status", "unobserved"),
        )
        tasks.add_row(
            str(task["position"]),
            Text(str(task["role"]), style=ROLE_STYLES.get(str(task["role"]), "white")),
            str(task["title"]),
            str(task["status"]),
            route,
        )

    budget = snapshot.get("budget") or {}
    footer = Text(
        "Attempts %d  Receipts %d  Artifacts %d  Tokens %s (%s)"
        % (
            len(snapshot["attempts"]),
            len(snapshot["tool_receipts"]),
            len(snapshot["artifacts"]),
            budget.get("tokens_observed", "unknown"),
            budget.get("observation_source", "not_observed"),
        ),
        style="dim",
    )
    return Panel(Group(header, summary, tasks, footer), border_style="bright_red", title="CrabAgent")
