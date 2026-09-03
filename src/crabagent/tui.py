from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from rich.cells import cell_len
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.events import MouseDown, MouseMove, MouseUp, Resize
from textual.screen import ModalScreen
from textual.strip import Strip
from textual.widgets import Button, Footer, Input, Label, OptionList, Select, SelectionList, Static, TextArea, Tree
from textual.widgets.option_list import Option

from . import __version__
from .discovery import codex_login_status
from .opencrab import filter_inventory_for_display
from .opencrab_snapshot import load_current, snapshot_paths
from .onboarding import status as local_onboarding_status
from .protocol import DaemonClient, start_daemon


_CRAB_PIXEL_PALETTE = {
    "l": "#ffd2ad",
    "o": "#ff875c",
    "d": "#ff4f4a",
    "p": "#d65cff",
    "w": "#f4fdff",
    "b": "#14161c",
    "r": "#ff3f61",
}

_BRAILLE_DOTS = (
    (0, 0, 0x01), (0, 1, 0x02), (0, 2, 0x04),
    (1, 0, 0x08), (1, 1, 0x10), (1, 2, 0x20),
    (0, 3, 0x40), (1, 3, 0x80),
)
_CRAB_COLOR_PRIORITY = ("b", "w", "r", "l", "o", "d", "p")

_CONVERSATION_ROLE_COLORS = {
    "YOU": "#62b5ff",
    "CRAB": "#ff795f",
    "KING": "#ff795f",
    "QUEEN": "#ff795f",
    "WORKER": "#ff795f",
    "SOLDIER": "#ff795f",
    "ORACLE": "#ff795f",
    "SYSTEM": "#f4cc63",
}


def _pixel_ellipse(grid: list[list[str]], cx: int, cy: int, rx: int, ry: int, color: str) -> None:
    for y in range(max(0, cy - ry), min(len(grid), cy + ry + 1)):
        for x in range(max(0, cx - rx), min(len(grid[0]), cx + rx + 1)):
            dx = x - cx
            dy = y - cy
            if (dx * dx * ry * ry) + (dy * dy * rx * rx) <= rx * rx * ry * ry:
                grid[y][x] = color


def _pixel_rect(grid: list[list[str]], x1: int, y1: int, x2: int, y2: int, color: str) -> None:
    for y in range(max(0, y1), min(len(grid), y2 + 1)):
        for x in range(max(0, x1), min(len(grid[0]), x2 + 1)):
            grid[y][x] = color


def _pixel_line(grid: list[list[str]], x1: int, y1: int, x2: int, y2: int, color: str, radius: int = 0) -> None:
    steps = max(abs(x2 - x1), abs(y2 - y1), 1)
    for step in range(steps + 1):
        ratio = step / steps
        x = round(x1 + (x2 - x1) * ratio)
        y = round(y1 + (y2 - y1) * ratio)
        for oy in range(-radius, radius + 1):
            for ox in range(-radius, radius + 1):
                if 0 <= y + oy < len(grid) and 0 <= x + ox < len(grid[0]):
                    grid[y + oy][x + ox] = color


def _build_crab_braille_sprite(frame: int, facing_right: bool) -> tuple[str, ...]:
    """Build a 50x20 pixel crab that occupies the same 25x5 terminal cells."""
    width, height = 50, 20
    grid = [["."] * width for _ in range(height)]
    step = (frame // 2) % 3

    def path(points: tuple[tuple[int, int], ...], color: str) -> None:
        for start, end in zip(points, points[1:]):
            _pixel_line(grid, start[0], start[1], end[0], end[1], color)

    # Thin dotted silhouette: raised claws, side arms, shell, and walking legs.
    for start, end, target in (
        ((18, 16), (16, 18), (14, 19 - step)),
        ((22, 16), (22, 18), (21, 19 + step)),
    ):
        path((start, end, target), "d")
        _pixel_line(grid, start[0], start[1] + 1, target[0], target[1] - 1, "o")
    path(((14, 12), (10, 12), (7, 13), (3, 13), (2, 14), (4, 14)), "d")
    path(((14, 12), (10, 12), (7, 13), (3, 13)), "o")
    _pixel_rect(grid, 3, 13, 5, 14, "l")

    # Left claw is drawn once and mirrored so both claws share the same pixel grammar.
    path(((14, 10), (11, 8), (9, 6), (8, 3), (9, 1), (12, 1)), "d")
    path(((12, 1), (14, 3)), "d")
    path(((9, 3), (11, 2), (12, 3)), "l")
    path(((9, 6), (10, 7), (12, 7)), "o")
    path(((11, 8), (14, 9)), "o")
    for x, y, color in ((10, 4, "r"), (9, 5, "o"), (11, 6, "l"), (10, 8, "r")):
        grid[y][x] = color

    # Sparse shell pixels create the reference's dotted red/orange texture.
    for x in range(14, 37):
        grid[9][x] = "d"
        grid[16][x] = "d"
    for y in range(9, 17):
        grid[y][14] = "d"
        grid[y][36] = "d"
    for y in range(10, 16):
        for x in range(15, 36):
            if (x + y * 2) % 4 == 0:
                grid[y][x] = "o"
            if (x * 2 + y) % 11 == 0:
                grid[y][x] = "l"
    for x in range(18, 33, 3):
        grid[15][x] = "r"

    # Mirror the left legs, side arm, and claw into the right half.
    source = [row[:] for row in grid]
    for y in range(height):
        for x in range(width // 2):
            if source[y][x] != ".":
                grid[y][width - 1 - x] = source[y][x]

    if not facing_right:
        grid = [list(reversed(row)) for row in grid]
    return tuple("".join(row) for row in grid)


def _shrink_braille_sprite(rows: tuple[str, ...]) -> tuple[str, ...]:
    """Reduce the dot map by 2x2 while retaining its dominant palette pixels."""
    width = max(len(row) for row in rows)
    normalized = [row.ljust(width, ".") for row in rows]
    scaled: list[str] = []
    for y in range(0, len(normalized), 2):
        row = []
        for x in range(0, width, 2):
            pixels = [normalized[y][x]]
            if x + 1 < width:
                pixels.append(normalized[y][x + 1])
            if y + 1 < len(normalized):
                pixels.append(normalized[y + 1][x])
                if x + 1 < width:
                    pixels.append(normalized[y + 1][x + 1])
            filled = [pixel for pixel in pixels if pixel != "."]
            if not filled:
                row.append(".")
            else:
                row.append(max(
                    set(filled),
                    key=lambda pixel: (filled.count(pixel), -_CRAB_COLOR_PRIORITY.index(pixel)),
                ))
        scaled.append("".join(row))
    if len(scaled[0]) % 2:
        scaled = [row + "." for row in scaled]
    while len(scaled) % 4:
        scaled.append("." * len(scaled[0]))
    return tuple(scaled)


class ConversationTranscript(TextArea):
    """Read-only conversation text with local selection instead of terminal-wide selection."""

    BINDINGS = [
        *TextArea.BINDINGS,
        Binding("ctrl+a", "select_all", "Select conversation", show=False, priority=True),
        Binding("super+a", "select_all", "Select conversation", show=False, priority=True),
    ]

    @staticmethod
    def _replace_line_style(line: Strip, style: Style) -> Strip:
        """Replace TextArea's base style so role colours survive terminal rendering."""
        segments = [
            Segment(segment.text, style, segment.control)
            for segment in line._segments
        ]
        return Strip(segments, line.cell_length)

    def _logical_line_for_visual_line(self, y: int) -> Optional[int]:
        """Map a visual line to its logical document line."""
        wrapped_document = self.wrapped_document
        visual_line = max(0, int(y) + int(self.scroll_offset[1]))
        if visual_line >= wrapped_document.height:
            return None
        try:
            line_index, _section_offset = wrapped_document._offset_to_line_info[visual_line]
        except (AttributeError, IndexError, TypeError):
            return None
        return max(0, int(line_index))

    def _role_for_visual_line(self, y: int) -> Optional[str]:
        """Resolve a wrapped screen line to the nearest explicit message header.

        Textual passes a visual line index to ``render_line``.  That index is
        different from the logical document line index whenever soft wrapping
        is active, which is the normal state for the conversation pane.
        Looking up ``self.text.splitlines()[y]`` therefore causes a long answer
        to inherit the next message's colour.  The wrapped document is the
        source of truth for that visual-to-logical mapping.
        """
        lines = self.text.splitlines()
        if not lines:
            return None
        line_index = self._logical_line_for_visual_line(y)
        if line_index is None:
            return None
        line_index = min(line_index, len(lines) - 1)
        # A message header is an exact protocol label on its own logical line.
        # Do not infer a role from arbitrary body text or from the previous
        # visual line; that is what previously caused colour bleed on wraps.
        for index in range(line_index, -1, -1):
            role = lines[index].strip()
            if role in _CONVERSATION_ROLE_COLORS:
                return role
        return None

    def render_line(self, y: int):
        line = super().render_line(y)
        role = self._role_for_visual_line(y)
        color = _CONVERSATION_ROLE_COLORS.get(role)
        if color:
            # Keep the transcript flat like Codex: role and body are colored
            # text, while the conversation surface remains one uninterrupted pane.
            logical_line = self._logical_line_for_visual_line(y)
            lines = self.text.splitlines()
            is_header = (
                logical_line is not None
                and logical_line < len(lines)
                and lines[logical_line].strip() == role
            )
            style = Style(color=color, bold=is_header)
            return self._replace_line_style(line, style)
        return line


class OntologyInventory(TextArea):
    """Scrollable read-only inventory; long pack lists must not become a wall."""

    BINDINGS = [*TextArea.BINDINGS]


def _write_macos_clipboard(text: str) -> bool:
    executable = shutil.which("pbcopy")
    if not executable:
        return False
    try:
        subprocess.run([executable], input=text, text=True, timeout=2, check=True)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


class PanelResizeHandle(Static):
    """Mouse target for resizing a side panel without moving the transcript."""

    def __init__(self, panel: str, **kwargs: Any) -> None:
        super().__init__("│", **kwargs)
        self.panel = panel

    def on_mouse_down(self, event: MouseDown) -> None:
        if event.button != 1:
            return
        self.capture_mouse()
        self.app._begin_panel_resize(self.panel, event.screen_x)
        event.stop()

    def on_mouse_move(self, event: MouseMove) -> None:
        if self.app._resizing_panel == self.panel:
            self.app._drag_panel_resize(self.panel, event.screen_x)
            event.stop()

    def on_mouse_up(self, event: MouseUp) -> None:
        if self.app._resizing_panel == self.panel:
            self.app._end_panel_resize()
            self.release_mouse()
            event.stop()


class ProjectList(Vertical):
    """Recomposable project rows; the tree below remains the session navigator."""

    def __init__(self, projects: Optional[list[Dict[str, Any]]] = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.projects = projects or []

    def compose(self) -> ComposeResult:
        if not self.projects:
            yield Static("No projects yet", classes="project-empty")
            return
        for project in self.projects:
            project_id = str(project.get("project_id") or "")
            name = str(project.get("name") or "Unassigned")
            count = int(project.get("session_count") or 0)
            with Horizontal(classes="project-row"):
                root = str(project.get("root_path") or "")
                folder = Path(root).name if root else "no folder"
                yield Static("%s  %d\n  %s" % (name[:22], count, folder[:24]), classes="project-name")
                if project_id and name != "Unassigned":
                    yield Button("+", id="project-new-session-%s" % project_id, compact=True)
                    yield Button("X", id="project-delete-%s" % project_id, compact=True)
                    yield Button("-", id="project-rename-%s" % project_id, compact=True)

    def set_projects(self, projects: list[Dict[str, Any]]) -> None:
        self.projects = projects
        self.refresh(recompose=True)


class ProjectNamePrompt(ModalScreen[Optional[str]]):
    def __init__(self, heading: str, initial: str = "", submit_label: str = "Save") -> None:
        super().__init__()
        self.heading = heading
        self.initial = initial
        self.submit_label = submit_label

    CSS = """
    ProjectNamePrompt { align: center middle; background: rgba(0,0,0,0.62); }
    #project-name-box { width: 64; height: auto; border: tall $accent; background: $surface; padding: 1 2; }
    #project-name-actions { height: 3; margin-top: 1; }
    #project-name-actions Button { width: 1fr; margin-right: 1; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="project-name-box"):
            yield Label(self.heading)
            yield Input(value=self.initial, placeholder="Project name", id="project-name-input")
            with Horizontal(id="project-name-actions"):
                yield Button(self.submit_label, id="project-name-save", variant="primary")
                yield Button("Cancel", id="project-name-cancel")

    def _dismiss_name(self) -> None:
        name = self.query_one("#project-name-input", Input).value.strip()
        self.dismiss(name or None)

    @on(Input.Submitted, "#project-name-input")
    def submit_input(self) -> None:
        self._dismiss_name()

    @on(Button.Pressed)
    def choose(self, event: Button.Pressed) -> None:
        if event.button.id == "project-name-save":
            self._dismiss_name()
        elif event.button.id == "project-name-cancel":
            self.dismiss(None)


class ProjectFolderPrompt(ModalScreen[Optional[Dict[str, str]]]):
    def __init__(self, heading: str, initial_name: str = "", initial_path: str = "", submit_label: str = "Create") -> None:
        super().__init__()
        self.heading = heading
        self.initial_name = initial_name
        self.initial_path = initial_path
        self.submit_label = submit_label

    CSS = """
    ProjectFolderPrompt { align: center middle; background: rgba(0,0,0,0.62); }
    #project-folder-box { width: 76; height: auto; border: tall $accent; background: $surface; padding: 1 2; }
    #project-folder-actions { height: 3; margin-top: 1; }
    #project-folder-actions Button { width: 1fr; margin-right: 1; }
    .project-folder-label { height: 1; color: $text-muted; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="project-folder-box"):
            yield Label(self.heading)
            yield Static("Project name", classes="project-folder-label")
            yield Input(value=self.initial_name, placeholder="Project name", id="project-folder-name")
            yield Static("Local folder", classes="project-folder-label")
            yield Input(value=self.initial_path, placeholder="/path/to/project", id="project-folder-path")
            with Horizontal(id="project-folder-actions"):
                yield Button(self.submit_label, id="project-folder-save", variant="primary")
                yield Button("Cancel", id="project-folder-cancel")

    def _dismiss_folder(self) -> None:
        name = self.query_one("#project-folder-name", Input).value.strip()
        path = self.query_one("#project-folder-path", Input).value.strip()
        if not name or not path:
            self.notify("Project name and folder are required.", severity="warning")
            return
        self.dismiss({"name": name, "root_path": path})

    @on(Input.Submitted, "#project-folder-path")
    def submit_input(self) -> None:
        self._dismiss_folder()

    @on(Button.Pressed)
    def choose(self, event: Button.Pressed) -> None:
        if event.button.id == "project-folder-save":
            self._dismiss_folder()
        elif event.button.id == "project-folder-cancel":
            self.dismiss(None)


class ProjectDeletePrompt(ModalScreen[bool]):
    def __init__(self, name: str) -> None:
        super().__init__()
        # ModalScreen already owns the read-only DOM ``name`` property.
        self.project_name = name

    CSS = """
    ProjectDeletePrompt { align: center middle; background: rgba(0,0,0,0.62); }
    #project-delete-box { width: 64; height: auto; border: tall $error; background: $surface; padding: 1 2; }
    #project-delete-actions { height: 3; margin-top: 1; }
    #project-delete-actions Button { width: 1fr; margin-right: 1; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="project-delete-box"):
            yield Label("Delete project '%s'? Its sessions will become Unassigned." % self.project_name)
            with Horizontal(id="project-delete-actions"):
                yield Button("Delete", id="project-delete-confirm", variant="error")
                yield Button("Cancel", id="project-delete-cancel")

    @on(Button.Pressed)
    def choose(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "project-delete-confirm")


class QueueChoice(ModalScreen[str]):
    CSS = """
    QueueChoice { align: center middle; background: rgba(0,0,0,0.55); }
    #choice-box { width: 58; height: auto; border: tall $accent; background: $surface; padding: 1 2; }
    #choice-buttons { height: 3; margin-top: 1; }
    #choice-buttons Button { width: 1fr; margin-right: 1; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="choice-box"):
            yield Label("A mission is already running. Where should this instruction go?")
            with Horizontal(id="choice-buttons"):
                yield Button("Now", id="now", variant="warning")
                yield Button("Wait", id="wait", variant="primary")
                yield Button("Cancel", id="cancel")

    @on(Button.Pressed)
    def choose(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id or "cancel")


class ApprovalChoice(ModalScreen[Dict[str, Any]]):
    def __init__(self, prompt: str) -> None:
        super().__init__()
        self.prompt = prompt

    CSS = """
    ApprovalChoice { align: center middle; background: rgba(0,0,0,0.6); }
    #approval-box { width: 72; height: auto; border: tall $warning; background: $surface; padding: 1 2; }
    #approval-actions { height: 3; margin-top: 1; }
    #approval-actions Button { width: 1fr; margin-right: 1; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="approval-box"):
            yield Label("Codex requests permission")
            yield Static(self.prompt)
            yield Input(placeholder="Optional answer for a Codex question", id="request-answer")
            with Horizontal(id="approval-actions"):
                yield Button("Approve", id="approve", variant="success")
                yield Button("Decline", id="decline", variant="error")

    @on(Button.Pressed)
    def choose(self, event: Button.Pressed) -> None:
        self.dismiss({"approved": event.button.id == "approve", "text": self.query_one("#request-answer", Input).value})


class McpSettingsScreen(ModalScreen[Optional[list[str]]]):
    """Show real configured MCP servers and choose the session allowlist."""

    def __init__(self, servers: list[Dict[str, Any]], enabled: set[str], policy: str) -> None:
        super().__init__()
        self.servers = servers
        self.enabled = enabled
        self.policy = policy

    CSS = """
    McpSettingsScreen { align: center middle; background: rgba(0,0,0,0.68); }
    #mcp-settings-box { width: 78; height: 30; border: tall $accent; background: $surface; padding: 1 2; }
    #mcp-settings-title { height: 2; color: #ff7762; text-style: bold; }
    #mcp-settings-note { height: 3; color: #aeb8c8; }
    #mcp-selection { height: 1fr; border: solid #303743; scrollbar-color: #b84535 #161b22; }
    #mcp-settings-actions { height: 3; margin-top: 1; }
    #mcp-settings-actions Button { width: 1fr; margin-right: 1; }
    """

    def compose(self) -> ComposeResult:
        selections = []
        for row in self.servers:
            name = str(row.get("name") or "unknown")
            state = str(row.get("state") or "unknown").upper()
            selections.append(("%s  ·  %s" % (name, state), name, name in self.enabled))
        with Vertical(id="mcp-settings-box"):
            yield Static("MCP SERVER SETTINGS", id="mcp-settings-title")
            yield Static(
                "Session policy: %s\nSelect the connected servers available to this session. Credentials stay in Codex configuration."
                % self.policy,
                id="mcp-settings-note",
            )
            yield SelectionList(*selections, id="mcp-selection")
            with Horizontal(id="mcp-settings-actions"):
                yield Button("Apply", id="mcp-settings-apply", variant="primary")
                yield Button("Cancel", id="mcp-settings-cancel")

    @on(Button.Pressed)
    def choose(self, event: Button.Pressed) -> None:
        if event.button.id == "mcp-settings-apply":
            selected = [str(value) for value in self.query_one("#mcp-selection", SelectionList).selected]
            self.dismiss(selected)
        elif event.button.id == "mcp-settings-cancel":
            self.dismiss(None)


class CliSettingsScreen(ModalScreen[Optional[str]]):
    """Show observed CLI paths and persist one preferred allowlisted route."""

    def __init__(self, clis: Dict[str, str], codex: Dict[str, Any], selected: str) -> None:
        super().__init__()
        self.clis = clis
        self.codex = codex
        self.selected = selected

    CSS = """
    CliSettingsScreen { align: center middle; background: rgba(0,0,0,0.68); }
    #cli-settings-box { width: 78; height: 28; border: tall $accent; background: $surface; padding: 1 2; }
    #cli-settings-title { height: 2; color: #ff7762; text-style: bold; }
    #cli-inventory { height: 1fr; border: solid #303743; padding: 1; color: #c5ccda; overflow-y: auto; }
    #cli-preferred { height: 3; margin-top: 1; }
    #cli-settings-actions { height: 3; margin-top: 1; }
    #cli-settings-actions Button { width: 1fr; margin-right: 1; }
    """

    def compose(self) -> ComposeResult:
        options = [("Auto", "auto")]
        options.extend((name, name) for name in sorted(self.clis))
        observed = ["Codex  ·  %s" % self.codex.get("status", "unknown")]
        observed.extend("%s\n%s" % (name, path) for name, path in sorted(self.clis.items()))
        with Vertical(id="cli-settings-box"):
            yield Static("CLI SLOTS / SETTINGS", id="cli-settings-title")
            yield Static("Observed allowlisted executables. Select a preferred route for this session.", id="cli-settings-note")
            yield Static("\n\n".join(observed), id="cli-inventory")
            yield Select(options, value=self.selected if self.selected in {"auto", *self.clis} else "auto", id="cli-preferred", allow_blank=False)
            with Horizontal(id="cli-settings-actions"):
                yield Button("Apply", id="cli-settings-apply", variant="primary")
                yield Button("Cancel", id="cli-settings-cancel")

    @on(Button.Pressed)
    def choose(self, event: Button.Pressed) -> None:
        if event.button.id == "cli-settings-apply":
            self.dismiss(str(self.query_one("#cli-preferred", Select).value or "auto"))
        elif event.button.id == "cli-settings-cancel":
            self.dismiss(None)


class FirstRunScreen(ModalScreen[str]):
    """Connect Codex or OpenCrab from the first-run surface without fake state."""

    def __init__(self, state: Dict[str, Any]) -> None:
        super().__init__()
        self.state = state
        self._codex_login_process: Optional[subprocess.Popen[Any]] = None
        self._codex_poll_stop = threading.Event()
        self._codex_poll_started = False

    CSS = """
    FirstRunScreen { align: center middle; background: rgba(0,0,0,0.72); }
    #first-run-box { width: 78; height: auto; border: tall #ff7762; background: #11151b; padding: 1 2; }
    #first-run-title { height: 2; color: #ff7762; text-style: bold; }
    #first-run-copy { height: auto; color: #c5ccda; margin-bottom: 1; }
    #first-run-state { height: 7; border: solid #303743; padding: 1; color: #d9dee8; }
    #first-run-note { height: auto; color: #aeb8c8; margin-top: 1; }
    #first-run-connect-actions { height: 3; margin-top: 1; }
    #first-run-connect-actions Button { width: 1fr; margin-right: 1; }
    #first-run-actions { height: 3; margin-top: 1; }
    #first-run-actions Button { width: 1fr; margin-right: 1; }
    """

    def _codex_ready(self) -> bool:
        return str((self.state.get("codex") or {}).get("login_status") or "") == "connected"

    def _state_text(self) -> str:
        codex = self.state.get("codex") or {}
        assets = self.state.get("assets") or {}
        login_status = str(codex.get("login_status") or codex.get("status") or "unknown")
        codex_label = "connected" if login_status == "connected" else (
            "install required" if login_status == "unavailable" else "login required"
        )
        return (
            "Codex        %s\nMCP          %s observed\nCLI          %s observed\nSkills       %s discovered"
            % (
                codex_label,
                assets.get("mcp_count", 0),
                assets.get("cli_count", 0),
                assets.get("skill_count", 0),
            )
        )

    def compose(self) -> ComposeResult:
        ready = self._codex_ready()
        with Vertical(id="first-run-box"):
            yield Static("KINGCRAB / CONNECT SERVICES", id="first-run-title")
            yield Static(
                "사용할 서비스를 연결하세요. 연결이 끝나면 KINGCRAB이 상태를 다시 확인합니다.",
                id="first-run-copy",
            )
            yield Static(self._state_text(), id="first-run-state")
            if ready:
                yield Static("Codex가 연결되었습니다. 이제 OpenCrab에 로그인할 수 있습니다.", id="first-run-note")
            else:
                yield Static("Codex 연결 버튼을 누르면 공식 Codex 로그인 흐름이 시작됩니다.", id="first-run-note")
            with Horizontal(id="first-run-connect-actions"):
                yield Button("Codex 연결", id="first-run-codex", variant="primary", disabled=ready)
                yield Button("OpenCrab 로그인", id="first-run-opencrab")
            with Horizontal(id="first-run-actions"):
                yield Button("Continue local", id="first-run-local", variant="primary", disabled=not ready)
                yield Button("Later", id="first-run-later", disabled=not ready)

    def _start_codex_login(self) -> None:
        executable = shutil.which("codex")
        if not executable:
            self.query_one("#first-run-note", Static).update("Codex CLI를 찾을 수 없습니다. 먼저 Codex를 설치하세요.")
            return
        if self._codex_login_process is not None and self._codex_login_process.poll() is None:
            self.query_one("#first-run-note", Static).update("Codex 로그인 흐름이 이미 실행 중입니다. 브라우저를 확인하세요.")
            return
        try:
            self._codex_login_process = subprocess.Popen(
                [executable, "login"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            self.query_one("#first-run-note", Static).update("Codex 로그인 흐름을 시작하지 못했습니다: %s" % exc)
            return
        self.query_one("#first-run-note", Static).update("Codex 로그인 흐름을 열었습니다. 브라우저에서 완료하면 자동으로 반영됩니다.")
        if not self._codex_poll_started:
            self._codex_poll_started = True
            threading.Thread(target=self._poll_codex_login, args=(executable,), daemon=True, name="crab-codex-onboarding").start()

    def _poll_codex_login(self, executable: str) -> None:
        for _ in range(60):
            if self._codex_poll_stop.wait(1.0):
                return
            if codex_login_status(executable) == "connected":
                try:
                    self.app.call_from_thread(self._codex_connected)
                except Exception:
                    return
                return

    def _codex_connected(self) -> None:
        if not self.is_mounted:
            return
        self.state.setdefault("codex", {})["login_status"] = "connected"
        self.query_one("#first-run-state", Static).update(self._state_text())
        self.query_one("#first-run-note", Static).update("Codex가 연결되었습니다. 이제 OpenCrab에 로그인할 수 있습니다.")
        self.query_one("#first-run-codex", Button).disabled = True
        self.query_one("#first-run-local", Button).disabled = False
        self.query_one("#first-run-later", Button).disabled = False

    def on_unmount(self) -> None:
        self._codex_poll_stop.set()

    @on(Button.Pressed)
    def choose(self, event: Button.Pressed) -> None:
        choices = {
            "first-run-local": "local",
            "first-run-later": "later",
        }
        button_id = str(event.button.id or "")
        if button_id == "first-run-codex":
            self._start_codex_login()
            return
        if button_id == "first-run-opencrab":
            self.dismiss("opencrab_login")
            return
        choice = choices.get(button_id)
        if choice:
            self.dismiss(choice)


class OpenCrabEndpointScreen(ModalScreen[Optional[Dict[str, str]]]):
    """Collect a clean endpoint; authentication remains in the browser."""

    CSS = """
    OpenCrabEndpointScreen { align: center middle; background: rgba(0,0,0,0.72); }
    #opencrab-endpoint-box { width: 78; height: auto; border: tall #ff7762; background: #11151b; padding: 1 2; }
    #opencrab-endpoint-title { height: 2; color: #ff7762; text-style: bold; }
    #opencrab-endpoint-copy { height: auto; color: #aeb8c8; margin-bottom: 1; }
    #opencrab-endpoint-input { height: 3; }
    #opencrab-endpoint-actions { height: 3; margin-top: 1; }
    #opencrab-endpoint-actions Button { width: 1fr; margin-right: 1; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="opencrab-endpoint-box"):
            yield Static("OPENCRAB / CONNECT LAST", id="opencrab-endpoint-title")
            yield Static(
                "OpenCrab에서 가입 또는 로그인한 뒤 MCP URL을 입력하세요.\n"
                "토큰과 비밀번호는 URL에 넣지 않습니다.",
                id="opencrab-endpoint-copy",
            )
            yield Input(placeholder="https://your-opencrab-mcp.example/mcp", id="opencrab-endpoint-input")
            with Horizontal(id="opencrab-endpoint-actions"):
                yield Button("Open sign in", id="opencrab-open-browser")
                yield Button("Connect", id="opencrab-connect", variant="primary")
                yield Button("Later", id="opencrab-later")

    def _submit(self) -> None:
        value = self.query_one("#opencrab-endpoint-input", Input).value.strip()
        if not value:
            self.notify("OpenCrab MCP URL is required.", severity="warning")
            return
        self.dismiss({"endpoint": value})

    @on(Input.Submitted, "#opencrab-endpoint-input")
    def submit_input(self) -> None:
        self._submit()

    @on(Button.Pressed)
    def choose(self, event: Button.Pressed) -> None:
        button_id = str(event.button.id or "")
        if button_id == "opencrab-open-browser":
            webbrowser.open("https://opencrab.sh")
        elif button_id == "opencrab-connect":
            self._submit()
        elif button_id == "opencrab-later":
            self.dismiss(None)


class CrabAgentApp(App[None]):
    TITLE = "KINGCRAB"
    SUB_TITLE = "OpenCrab kinetic agent workspace"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("ctrl+j", "submit_prompt", "Send", show=True),
        Binding("ctrl+x", "interrupt", "Stop", show=True),
        Binding("ctrl+n", "new_session", "New", show=True),
        Binding("ctrl+shift+c", "copy_conversation", "Copy", show=False),
        Binding("ctrl+shift+b", "branch_conversation", "Branch", show=False),
        Binding("ctrl+q", "quit", "Quit", show=True),
        Binding("f1", "help", "Help", show=True),
    ]
    CSS = """
    Screen { background: #090b0f; color: #d9dee8; }
    #topbar { height: 5; border-bottom: solid #b84535; background: #11151b; padding: 0 1; }
    #brand-stack { width: 20; min-width: 20; height: 5; content-align: left middle; }
    #brand-king { width: 20; min-width: 20; height: 5; border: none; background: transparent; color: #ff7762; text-style: bold; content-align: left middle; }
    #connection { width: 0; height: 0; opacity: 0; }
    #crab-pet { width: 1fr; min-width: 24; height: 5; margin: 0 1; padding: 0 1; border: none; background: transparent; color: #d56b58; content-align: left middle; }
    #top-controls { width: 1fr; min-width: 77; height: 5; align: right middle; }
    .control { width: 14; height: 5; margin-left: 1; }
    .control-label { height: 1; color: #687386; text-style: bold; content-align: center middle; }
    .selector { width: 100%; height: 3; margin: 0; }
    #mode-control { width: 13; }
    #mcp-control { width: 16; }
    #cli-control { width: 15; }
    #workers-control { width: 12; min-width: 12; }
    #mission-strip { height: 3; border-bottom: solid #303743; background: #0f141b; padding: 0 2; }
    #mission-strip-label { width: 25; color: #ff7762; text-style: bold; content-align: left middle; }
    #mission-title { width: 1fr; color: #d9dee8; content-align: left middle; }
    #body { height: 1fr; min-height: 0; }
    #left { width: 34; border-right: solid #303743; background: #0d1117; padding: 1; }
    #left-divider, #right-divider { width: 2; height: 1fr; min-height: 0; background: #11161d; color: #596273; content-align: center middle; }
    #left-divider:hover, #right-divider:hover { background: #3a2020; color: #ff7762; }
    #center { width: 1fr; height: 1fr; min-width: 24; min-height: 0; padding: 0 1; }
    #right { width: 36; min-height: 0; border-left: solid #303743; background: #0d1117; padding: 1; overflow-y: hidden; }
    .panel-heading { height: 2; min-height: 2; }
    .panel-heading .panel-title { width: 1fr; margin-bottom: 0; content-align: left middle; }
    .panel-hide { width: 3; min-width: 3; height: 2; margin: 0; }
    .panel-title { color: #ff7762; text-style: bold; margin-bottom: 1; }
    #colony-summary { height: 9; min-height: 8; border: solid #303743; padding: 1; }
    #queen-ontology { height: 7; min-height: 6; border: solid #303743; padding: 0 1; scrollbar-color: #b84535 #161b22; }
    #ontology-toolbar { height: 3; min-height: 3; margin-top: 1; }
    #ontology-search { width: 1fr; height: 3; }
    #ontology-view { width: 11; height: 3; margin-left: 1; }
    #ontology-actions { height: 3; min-height: 3; margin-top: 1; }
    #ontology-refresh { width: 8; height: 3; margin-left: 1; }
    #ontology-options { height: 1fr; min-height: 5; border: solid #303743; scrollbar-color: #b84535 #161b22; overflow-x: hidden; }
    #ontology-selection { height: 2; min-height: 2; color: #aeb8c8; content-align: left middle; }
    #ontology-apply { width: 1fr; height: 3; }
    #assets { height: 7; min-height: 5; border: solid #303743; padding: 0 1; overflow-y: auto; }
    #projects-header { height: 2; min-height: 2; margin-top: 1; }
    #projects-label { width: 1fr; color: #ff7762; text-style: bold; content-align: left middle; }
    #project-add { width: 3; min-width: 3; height: 2; margin: 0; }
    #project-list { height: auto; max-height: 9; min-height: 2; overflow-y: auto; }
    .project-row { height: 4; min-height: 4; }
    .project-name { width: 1fr; color: #c5ccda; content-align: left middle; overflow-x: hidden; }
    .project-row Button { width: 3; min-width: 3; height: 2; margin: 0 0 0 1; }
    .project-empty { height: 2; color: #687386; content-align: left middle; }
    #kings { height: 1fr; min-height: 8; background: #0d1117; color: #d9dee8; scrollbar-color: #b84535 #161b22; }
    #conversation-tools { height: 5; min-height: 5; border-bottom: solid #303743; padding: 0 1; align: center middle; }
    #conversation-title { width: 15; min-width: 15; height: 5; color: #ff7762; text-style: bold; content-align: left middle; }
    #conversation-actions { width: 1fr; min-width: 28; height: 5; align: center middle; }
    #copy-conversation, #branch-conversation, #show-panels { width: 8; min-width: 8; margin: 0 1; }
    #activity { width: 15; min-width: 15; height: 5; color: #aeb8c8; content-align: right middle; }
  #transcript { height: 1fr; min-height: 0; border: none; background: #090b0f; padding: 0 2; text-wrap: wrap; scrollbar-color: #b84535 #161b22; }
    #composer-row { height: 5; min-height: 5; border-top: solid #303743; padding: 1 0 0 0; align: left middle; }
    #composer { width: 1fr; min-width: 20; height: 3; min-height: 3; border: tall #596273; background: #10141a; }
    #send { width: 10; min-width: 10; height: 3; margin-left: 1; }
    #hint { height: 1; min-height: 1; color: #687386; }
    Footer { background: #11151b; }
    """

    def __init__(
        self,
        workspace: Path,
        client: Optional[DaemonClient] = None,
        clipboard_writer: Callable[[str], bool] = _write_macos_clipboard,
        interactive_onboarding: Optional[bool] = None,
    ) -> None:
        super().__init__()
        self.workspace = workspace.resolve()
        self._interactive_onboarding = client is None if interactive_onboarding is None else interactive_onboarding
        self.client = client or start_daemon(self.workspace)
        self.session: Dict[str, Any] = {}
        self.assets: Dict[str, Any] = {}
        self.snapshot: Dict[str, Any] = {}
        self.last_message_id = 0
        self.active_request_id = ""
        self.active_request: Dict[str, Any] = {}
        self.last_interaction = ""
        self.colony: Dict[str, Any] = {}
        self._colony_digest = ""
        self._project_digest = ""
        self._ontology_inventory_mtime: Optional[int] = None
        self._ontology_refreshing = False
        self._ontology_catalog: Dict[str, Any] = {}
        self._ontology_catalog_digest = ""
        self._ontology_catalog_revision = 0
        self._ontology_catalog_error = ""
        self._ontology_reflow_scheduled = False
        self._ontology_selected_projects: set[str] = set()
        self._ontology_selected_packs: set[str] = set()
        self._ontology_selection_reset = False
        self._catalog_refresh_attempted = False
        self._activity_frame = 0
        self._pet_frame = 0
        self._left_panel_hidden = False
        self._right_panel_hidden = False
        self._narrow_mode = False
        self._resizing_panel = ""
        self._resize_start_x = 0
        self._resize_start_width = 0
        self._submitted_activity = ""
        self._saw_active_after_submit = False
        self._submitted_at = 0.0
        self._last_snapshot_poll_at = 0.0
        self._pack_watch_runs: set[str] = set()
        self._transcript_text = ""
        self._clipboard_writer = clipboard_writer
        self._runtime_ready = False
        self._runtime_bootstrap_in_flight = False
        self._startup_retry_timer = None
        self._startup_error = ""
        self._onboarding_prompted = False

    def compose(self) -> ComposeResult:
        with Horizontal(id="topbar"):
            with Vertical(id="brand-stack"):
                # Terminals do not expose point-size controls; full-width glyphs give the title a two-cell visual scale.
                yield Static("ＫＩＮＧＣＲＡＢ", id="brand-king")
                yield Static("connecting...", id="connection")
            yield Static("", id="crab-pet")
            with Horizontal(id="top-controls"):
                with Vertical(classes="control", id="model-control"):
                    yield Static("MODEL", classes="control-label")
                    yield Select([("Auto", "auto"), ("Sol", "sol"), ("Terra", "terra"), ("Luna", "luna")], value="auto", id="model", classes="selector", allow_blank=False)
                with Vertical(classes="control", id="mode-control"):
                    yield Static("MODE", classes="control-label")
                    yield Select([("Auto", "auto"), ("Chat", "chat"), ("Colony", "colony"), ("Full x1", "full")], value="auto", id="mode", classes="selector", allow_blank=False)
                with Vertical(classes="control", id="mcp-control"):
                    yield Static("MCP", classes="control-label")
                    yield Button("0 · SET", id="mcp-settings", classes="selector", compact=True)
                with Vertical(classes="control", id="cli-control"):
                    yield Static("CLI", classes="control-label")
                    yield Button("0 · SET", id="cli-settings", classes="selector", compact=True)
                with Vertical(classes="control", id="workers-control"):
                    yield Static("WORKERS", classes="control-label")
                    yield Select([("Auto", "auto")] + [(str(n), n) for n in range(1, 9)], value="auto", id="workers", classes="selector", allow_blank=False)
        with Horizontal(id="mission-strip"):
            yield Static("CURRENT MISSION", id="mission-strip-label")
            yield Static("READY / awaiting mission", id="mission-title")
        with Horizontal(id="body"):
            with Vertical(id="left"):
                with Horizontal(classes="panel-heading"):
                    yield Static("KING'S COLONY MISSION", classes="panel-title")
                    yield Button("X", id="hide-left", classes="panel-hide", compact=True)
                yield Static("Loading observed colony state...", id="colony-summary")
                with Horizontal(id="projects-header"):
                    yield Static("PROJECTS", id="projects-label")
                    yield Button("+", id="project-add", compact=True)
                yield ProjectList(id="project-list")
                yield Static("KINGS / SESSIONS", classes="panel-title")
                yield Tree("PROJECTS", id="kings")
            yield PanelResizeHandle("left", id="left-divider")
            with Vertical(id="center"):
                with Horizontal(id="conversation-tools"):
                    yield Static("CONVERSATION", id="conversation-title")
                    with Horizontal(id="conversation-actions"):
                        yield Button("Copy", id="copy-conversation")
                        yield Button("Branch", id="branch-conversation")
                        yield Button("Panels", id="show-panels")
                    yield Static("READY", id="activity")
                yield ConversationTranscript("", id="transcript", read_only=True, show_cursor=False, soft_wrap=True)
                with Horizontal(id="composer-row"):
                    yield Input(placeholder="Talk, build, or use /goal /doc /mcp /ontology /cli /ingest", id="composer")
                    yield Button("Send", id="send", variant="primary")
                yield Static("/goal  /doc  /mcp  /ontology  /cli  /ingest  /retry  /panels  /project  /new  /stop", id="hint")
            yield PanelResizeHandle("right", id="right-divider")
            with Vertical(id="right"):
                with Horizontal(classes="panel-heading"):
                    yield Static("QUEEN'S ONTOLOGY", classes="panel-title")
                    yield Button("X", id="hide-right", classes="panel-hide", compact=True)
                yield OntologyInventory("", id="queen-ontology", read_only=True, show_cursor=False, soft_wrap=True)
                with Horizontal(id="ontology-toolbar"):
                    yield Input(placeholder="Search live projects / packs", id="ontology-search")
                    yield Select([("All", "all"), ("Projects", "projects"), ("Packs", "packs"), ("Selected", "selected")], value="all", id="ontology-view", allow_blank=False)
                with Horizontal(id="ontology-actions"):
                    yield Button("Use selected", id="ontology-apply", variant="primary", compact=True)
                    yield Button("Refresh", id="ontology-refresh", compact=True)
                yield OptionList(id="ontology-options")
                yield Static("No live selection", id="ontology-selection")
                yield Static("WORKER'S ASSETS", classes="panel-title")
                yield Static("Loading observed assets...", id="assets")
        yield Footer()

    def on_mount(self) -> None:
        self.set_interval(0.65, self._poll)
        self.set_interval(0.35, self._tick_activity)
        self.set_interval(0.45, self._tick_crab_pet)
        self._tick_crab_pet()
        self._apply_panel_layout(self.size.width)
        self.query_one("#composer", Input).focus()
        self._start_runtime_bootstrap()

    def _start_runtime_bootstrap(self) -> None:
        """Fetch daemon state off the Textual event loop."""
        if self._runtime_ready or self._runtime_bootstrap_in_flight:
            return
        self._runtime_bootstrap_in_flight = True

        def worker() -> None:
            try:
                session = self.client.request("session.ensure")
                assets = self.client.request("assets")
                exported = self.client.request("conversation.export", session_id=session["session_id"])
            except Exception as exc:
                self.call_from_thread(self._runtime_bootstrap_failed, exc)
                return
            self.call_from_thread(self._runtime_bootstrap_ready, session, assets, exported)

        threading.Thread(target=worker, daemon=True, name="crab-tui-bootstrap").start()

    def _runtime_bootstrap_ready(
        self,
        session: Dict[str, Any],
        assets: Dict[str, Any],
        exported: Dict[str, Any],
    ) -> None:
        if not self.is_mounted:
            return
        self._runtime_bootstrap_in_flight = False
        self.session = session
        self.assets = assets
        self.query_one("#model", Select).value = str(self.session.get("model_policy") or "auto")
        self.query_one("#mode", Select).value = str(self.session.get("interaction_mode") or "auto")
        worker_policy = str(self.session.get("worker_policy") or "auto")
        self.query_one("#workers", Select).value = "auto" if worker_policy == "auto" else int(self.session.get("max_workers") or 3)
        self._render_assets()
        self._render_control_slots()
        self.query_one("#kings", Tree).root.expand()
        self._apply_conversation_export(exported)
        self._runtime_ready = True
        self._startup_error = ""
        self._poll(force=True)
        onboarding = local_onboarding_status(self.workspace, self.assets)
        if onboarding.get("opencrab", {}).get("status") in {"connected", "configured"}:
            self._load_live_catalog(prefer_stale=True)
        else:
            self._render_ontology_inventory()
        if self._interactive_onboarding and onboarding.get("needs_setup") and not self._onboarding_prompted:
            self._onboarding_prompted = True
            self.push_screen(FirstRunScreen(onboarding), self._finish_first_run)
        if self._startup_retry_timer is not None:
            self._startup_retry_timer.stop()
            self._startup_retry_timer = None

    def _runtime_bootstrap_failed(self, error: Exception) -> None:
        if not self.is_mounted:
            return
        self._runtime_bootstrap_in_flight = False
        self._show_startup_error(error)
        if self._startup_retry_timer is None:
            self._startup_retry_timer = self.set_interval(1.5, self._retry_runtime)

    def _show_startup_error(self, error: Exception) -> None:
        self._runtime_ready = False
        self.session = {}
        message = str(error)
        self.query_one("#connection", Static).update("crabd reconnecting")
        self.query_one("#mission-title", Static).update("STARTING / waiting for crabd response")
        if message != self._startup_error:
            self._startup_error = message
            self._append_note("Startup connection pending: %s" % message)

    def _retry_runtime(self) -> None:
        if self._runtime_ready:
            return
        self._start_runtime_bootstrap()

    def _finish_first_run(self, choice: Optional[str]) -> None:
        if not choice:
            return
        if choice in {"local", "later"}:
            try:
                action = "onboarding.opencrab.defer" if choice == "later" else "onboarding.local_ready"
                self.client.request(action)
                self._append_note(
                    "KINGCRAB LOCAL READY\nCodex and local MCP / CLI / Skill inventory are available.\n"
                    + (
                        "OpenCrab connection deferred until you need ontology context."
                        if choice == "later"
                        else "OpenCrab remains available from /ontology or the connection panel."
                    )
                )
            except Exception as exc:
                self._append_note("Local onboarding failed: %s" % exc)
                self.notify("Could not finish local setup: %s" % exc, severity="error")
            return
        if choice == "opencrab_login":
            webbrowser.open("https://opencrab.sh")
            self._append_note("OPENCRAB\n로그인 페이지를 열었습니다. 로그인 후 MCP URL을 연결하세요.")
            self.push_screen(OpenCrabEndpointScreen(), self._connect_opencrab)

    def _connect_opencrab(self, data: Optional[Dict[str, str]]) -> None:
        if not data:
            return
        endpoint = str(data.get("endpoint") or "")
        self._append_note("OPENCRAB\nChecking the supplied MCP endpoint...")

        def worker() -> None:
            try:
                result = self.client.request("onboarding.opencrab.connect", endpoint=endpoint)
            except Exception as exc:
                self.call_from_thread(self._opencrab_connection_failed, exc)
                return
            self.call_from_thread(self._opencrab_connection_ready, result)

        threading.Thread(target=worker, daemon=True, name="crab-opencrab-onboarding").start()

    def _opencrab_connection_ready(self, result: Dict[str, Any]) -> None:
        self._append_note(
            "OPENCRAB CONNECTED\nTier: %s\nThe live ontology catalog is now available."
            % (result.get("account") or "unknown")
        )
        self._load_live_catalog(force=True, announce=False, complete=False)
        self._poll(force=True)

    def _opencrab_connection_failed(self, error: Exception) -> None:
        self._append_note("OPENCRAB CONNECTION NOT CONFIRMED\n%s" % error)
        self.notify("OpenCrab was not connected; local mode remains available.", severity="warning")

    def on_resize(self, event: Resize) -> None:
        if self.is_mounted:
            self._apply_panel_layout(event.size.width)

    def _apply_panel_layout(self, width: Optional[int] = None) -> None:
        """Keep the conversation primary when a terminal becomes narrow."""
        try:
            viewport_width = int(width or self.size.width)
            narrow = viewport_width < 112
            self._narrow_mode = narrow
            left_visible = not narrow and not self._left_panel_hidden
            right_visible = not narrow and not self._right_panel_hidden
            self.query_one("#left").styles.display = "block" if left_visible else "none"
            self.query_one("#right").styles.display = "block" if right_visible else "none"
            self.query_one("#left-divider").styles.display = "block" if left_visible else "none"
            self.query_one("#right-divider").styles.display = "block" if right_visible else "none"
        except Exception:
            # The first resize can arrive while the composition is still mounting.
            return

    def _begin_panel_resize(self, panel: str, screen_x: int) -> None:
        if self._narrow_mode:
            return
        selector = "#left" if panel == "left" else "#right"
        panel_widget = self.query_one(selector)
        self._resizing_panel = panel
        self._resize_start_x = screen_x
        self._resize_start_width = panel_widget.region.width

    def _drag_panel_resize(self, panel: str, screen_x: int) -> None:
        if self._resizing_panel != panel or self._narrow_mode:
            return
        delta = screen_x - self._resize_start_x
        width = self._resize_start_width + delta if panel == "left" else self._resize_start_width - delta
        # There is no arbitrary side-panel cap. The only upper boundary is the
        # current terminal width after reserving the other side panel, both
        # dividers, and a recoverable center conversation pane.
        body_width = self.query_one("#body").region.width
        other_selector = "#right" if panel == "left" else "#left"
        other_width = self.query_one(other_selector).region.width
        center_min_width = 24
        divider_width = self.query_one("#left-divider").region.width + self.query_one("#right-divider").region.width
        width = min(max(22, width), max(22, body_width - other_width - divider_width - center_min_width))
        selector = "#left" if panel == "left" else "#right"
        self.query_one(selector).styles.width = width
        if panel == "right":
            self._schedule_ontology_reflow()

    def _end_panel_resize(self) -> None:
        self._resizing_panel = ""

    def _schedule_ontology_reflow(self) -> None:
        """Rebuild width-sensitive catalog labels after panel layout settles."""
        if not self._ontology_catalog or self._ontology_reflow_scheduled:
            return
        self._ontology_reflow_scheduled = True

        def reflow() -> None:
            self._ontology_reflow_scheduled = False
            if self.is_mounted:
                self._render_ontology_inventory()

        self.call_after_refresh(reflow)

    def _set_panel_hidden(self, panel: str, hidden: bool) -> None:
        if panel == "left":
            self._left_panel_hidden = hidden
        else:
            self._right_panel_hidden = hidden
        self._apply_panel_layout(self.size.width)

    def _restore_panels(self) -> None:
        """Restore both panels from the always-visible center toolbar."""
        self._left_panel_hidden = False
        self._right_panel_hidden = False
        self._apply_panel_layout(self.size.width)

    def _tick_crab_pet(self) -> None:
        """Walk one detailed crab across the header and turn it around at each edge."""
        if not self.is_mounted:
            return
        try:
            pet = self.query_one("#crab-pet", Static)
        except Exception:
            return
        width = max(18, int(pet.region.width) - 2)
        frame = self._pet_frame
        sprite = _shrink_braille_sprite(_build_crab_braille_sprite(frame, facing_right=True))
        sprite_width = len(sprite[0]) // 2
        travel = max(1, width - sprite_width)
        phase = (frame * 2) % (travel * 2)
        facing_right = phase <= travel
        offset = phase if facing_right else (travel * 2) - phase
        if not facing_right:
            sprite = _shrink_braille_sprite(_build_crab_braille_sprite(frame, facing_right=False))
        scene = Text()
        for cell_y in range(0, len(sprite), 4):
            if cell_y:
                scene.append("\n")
            scene.append(" " * offset)
            for cell_x in range(0, len(sprite[0]), 2):
                bits = 0
                colors = []
                for dot_x, dot_y, bit in _BRAILLE_DOTS:
                    pixel = sprite[cell_y + dot_y][cell_x + dot_x]
                    if pixel != ".":
                        bits |= bit
                        colors.append(pixel)
                if not bits:
                    scene.append(" ")
                    continue
                color = max(
                    set(colors),
                    key=lambda pixel: (colors.count(pixel), -_CRAB_COLOR_PRIORITY.index(pixel)),
                )
                scene.append(chr(0x2800 + bits), Style(color=_CRAB_PIXEL_PALETTE[color]))
        pet.update(scene)
        self._pet_frame = frame + 1

    def _poll(self, *, force: bool = False) -> None:
        if not self.is_mounted or not self._runtime_ready or not self.session.get("session_id"):
            return
        now = time.monotonic()
        if not force and self._last_snapshot_poll_at:
            active = self.session.get("status") == "running" or bool(self._submitted_activity)
            minimum_interval = 0.35 if active else 1.15
            if now - self._last_snapshot_poll_at < minimum_interval:
                return
        self._last_snapshot_poll_at = now
        try:
            try:
                bundle = self.client.request(
                    "ui.snapshot",
                    session_id=self.session["session_id"],
                    after_message_id=self.last_message_id,
                )
                self.colony = bundle.pop("colony", {})
            except Exception:
                # Keep compatibility with a daemon from the previous local
                # install while the new combined projection is rolling out.
                bundle = self.client.request(
                    "session.snapshot",
                    session_id=self.session["session_id"],
                    after_message_id=self.last_message_id,
                )
                self.colony = self.client.request("colony.overview")
            self.snapshot = bundle
        except Exception as exc:
            self.query_one("#connection", Static).update("runtime offline: %s" % exc)
            return
        if not self.is_mounted:
            return
        self.session = self.snapshot["session"]
        try:
            self.query_one("#branch-conversation", Button).disabled = self.session.get("status") == "running"
        except Exception:
            # A final timer tick can race with Textual tearing down the DOM.
            return
        if self.session.get("status") == "running":
            self._saw_active_after_submit = True
        elif self._saw_active_after_submit:
            self._submitted_activity = ""
            self._saw_active_after_submit = False
        elif self._submitted_activity and time.monotonic() - self._submitted_at > 10:
            self._submitted_activity = ""
        thread = str(self.session.get("codex_thread_id") or "")
        thread_label = thread[-8:] if thread else "connecting"
        self.query_one("#connection", Static).update("● %s | %s | %s" % (self.session["session_id"][-6:], thread_label, self.session["status"]))
        for message in self.snapshot.get("messages", []):
            self.last_message_id = max(self.last_message_id, int(message["message_id"]))
            self._write_message(message)
        self._render_colony()
        self._render_mission()
        pending = self.snapshot.get("pending_requests") or []
        if pending and not self.active_request_id:
            request = pending[0]
            self.active_request_id = str(request["request_id"])
            self.active_request = request
            self.push_screen(ApprovalChoice(str(request.get("method") or request.get("prompt") or "Permission request")), self._approval_result)

    @staticmethod
    def _format_message(message: Dict[str, Any]) -> str:
        role = str(message.get("role") or "system")
        metadata = message.get("metadata") or {}
        if role == "user":
            label = "YOU"
        elif role == "assistant":
            label = str(metadata.get("role") or "CRAB")
        else:
            label = "SYSTEM"
        content = str(message.get("content") or "")
        if role == "system":
            content = CrabAgentApp._friendly_system_text(content)
        elif role == "assistant":
            content = CrabAgentApp._friendly_role_text(content) if label in {
                "KING", "QUEEN", "WORKER", "SOLDIER", "ORACLE"
            } else CrabAgentApp._strip_internal_markers(content)
        return "%s\n%s" % (label, content)

    @staticmethod
    def _is_user_facing_message(message: Dict[str, Any]) -> bool:
        """Keep role handoffs out of the chat while preserving Oracle receipts."""
        if str(message.get("role") or "") != "assistant":
            return True
        metadata = message.get("metadata") or {}
        role = str(metadata.get("role") or "CRAB").upper()
        if role not in {"KING", "QUEEN", "WORKER", "SOLDIER", "ORACLE"}:
            return True
        return bool(metadata.get("goal_outcome") or metadata.get("user_facing"))

    @staticmethod
    def _strip_internal_markers(value: str) -> str:
        """Remove protocol identifiers from text intended for the conversation pane."""
        text = str(value or "")
        text = re.sub(r"\s*\[(?:evidence_id|item_id|source|source_uri|type):[^\]]*\]", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\bopencrab://[^\s\]]+", "", text, flags=re.IGNORECASE)
        text = re.sub(
            r"\b(?:goal|session|thread|turn|request|task|artifact|mission)-[0-9a-f]{8,}\b",
            "",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(r"[ \t]{2,}", " ", text)
        return "\n".join(line.rstrip() for line in text.splitlines()).strip()

    @staticmethod
    def _section_lines(value: str, name: str, next_names: tuple[str, ...]) -> list[str]:
        lines = str(value or "").splitlines()
        start = next((index for index, line in enumerate(lines) if line.strip() == name), None)
        if start is None:
            return []
        end = len(lines)
        for index in range(start + 1, len(lines)):
            if lines[index].strip() in next_names:
                end = index
                break
        return [line.strip() for line in lines[start + 1:end] if line.strip()]

    @staticmethod
    def _friendly_role_text(content: str) -> str:
        """Turn role handoffs into readable prose without weakening stored receipts."""
        value = str(content or "").strip()
        protocol_sections = {"SELECTED_PATH", "SOURCE_REFS", "SLOT_COVERAGE", "SUPPORTED_CLAIMS", "GAPS", "NEXT_ACTION", "RCE_REFS"}
        if not any(section in value for section in protocol_sections):
            clean = CrabAgentApp._strip_internal_markers(value)
            clean = re.sub(r"^(?:ORACLE RESULT|ORACLE BLOCKED|MISSION\s+\w+)\s*\n?", "", clean, flags=re.IGNORECASE)
            clean = re.sub(r"^목표:\s*[^\n]+\n?", "", clean)
            clean = re.sub(r"^근거\s+\d+개\s+·[^\n]+\n?", "", clean)
            return clean.strip() or "작업 결과를 확인하고 있습니다."

        selected = CrabAgentApp._section_lines(
            value,
            "SELECTED_PATH",
            ("SOURCE_REFS", "SLOT_COVERAGE", "SUPPORTED_CLAIMS", "GAPS", "NEXT_ACTION"),
        )
        claims = CrabAgentApp._section_lines(
            value,
            "SUPPORTED_CLAIMS",
            ("GAPS", "NEXT_ACTION"),
        )
        gaps = CrabAgentApp._section_lines(value, "GAPS", ("NEXT_ACTION",))
        next_action = CrabAgentApp._section_lines(value, "NEXT_ACTION", ())

        # The first line of SELECTED_PATH is an internal graph route. The
        # remaining bullets are the useful evidence-led content.
        useful_selected = [line for line in selected[1:] if not line.startswith(("subject ->", "resource ->", "concept ->"))]
        useful_selected = [CrabAgentApp._strip_internal_markers(line).lstrip("- ") for line in useful_selected]
        useful_selected = [line for line in useful_selected if line]
        if not useful_selected:
            useful_selected = [CrabAgentApp._strip_internal_markers(line).lstrip("- ") for line in claims if line.startswith("-")]

        cleaned_claims = []
        for line in claims:
            clean = CrabAgentApp._strip_internal_markers(line).lstrip("- ")
            if clean and "observed evidence is listed" not in clean.lower():
                cleaned_claims.append(clean)

        cleaned_gaps = []
        for line in gaps:
            clean = CrabAgentApp._strip_internal_markers(line).lstrip("- ")
            if not clean or "model synthesis was not requested" in clean.lower():
                continue
            if "unresolved" in clean.lower() or "filled [" in clean.lower():
                continue
            cleaned_gaps.append(clean)

        cleaned_next = []
        for line in next_action:
            clean = CrabAgentApp._strip_internal_markers(line).lstrip("- ")
            if not clean or clean.upper() in {"STOP", "NONE"}:
                continue
            clean = re.sub(r"^(?:WORKER|QUEEN|SOLDIER|ORACLE)\s*:\s*", "", clean, flags=re.IGNORECASE)
            cleaned_next.append(clean)

        # Avoid printing the same evidence sentence twice when the protocol
        # contains both a selected-path preview and a claims section.
        bullets: list[str] = []
        for line in useful_selected + cleaned_claims:
            if line not in bullets:
                bullets.append(line)
        summary: list[str] = []
        if "SELECTED_PATH" in value:
            prefix = value.split("SELECTED_PATH", 1)[0]
            for line in prefix.splitlines():
                clean = CrabAgentApp._strip_internal_markers(line)
                if not clean or clean.upper() in {"ORACLE RESULT", "ORACLE BLOCKED"}:
                    continue
                evidence_summary = re.match(r"^근거\s+(\d+)개", clean)
                if evidence_summary:
                    summary.append("근거 자료 %s개를 확인했습니다." % evidence_summary.group(1))
                elif clean.startswith(("목표:", "Mission ")):
                    summary.append(clean)

        parts: list[str] = []
        if summary:
            parts.append("\n".join(summary))
        if bullets:
            parts.append("확인된 내용\n" + "\n".join("- %s" % line for line in bullets[:8]))
        if cleaned_gaps:
            parts.append("추가 확인 필요\n" + "\n".join("- %s" % line for line in cleaned_gaps[:3]))
        if cleaned_next:
            parts.append("다음 단계\n" + "\n".join("- %s" % line for line in cleaned_next[:2]))
        return "\n\n".join(parts) or "근거를 확인했지만 사용자에게 보여줄 자연어 결론은 아직 없습니다."

    @staticmethod
    def _friendly_system_text(content: str) -> str:
        """Keep runtime state useful without exposing implementation identifiers."""
        value = str(content or "").strip()
        if value.lower().startswith("new durable session"):
            return "새 대화를 준비했습니다."
        if value == "Conversation turn interrupted.":
            return "대화를 잠시 멈췄습니다. 다음 메시지부터 이어서 진행할 수 있습니다."
        if value.startswith("Conversation failed:"):
            detail = CrabAgentApp._strip_internal_markers(value.split(":", 1)[1].strip())
            return "대화를 완료하지 못했습니다.\n%s" % (detail or "연결 상태를 확인한 뒤 다시 시도해 주세요.")
        if value.startswith("Mission failed:"):
            detail = CrabAgentApp._strip_internal_markers(value.split(":", 1)[1].strip())
            return "작업을 완료하지 못했습니다.\n%s" % (detail or "필요한 근거와 연결 상태를 확인해 주세요.")
        if value.startswith("Mission stopped:"):
            detail = CrabAgentApp._strip_internal_markers(value.split(":", 1)[1].strip())
            return "작업을 멈췄습니다.\n%s" % (detail or "결과를 확인한 뒤 다시 시도해 주세요.")
        if value.startswith("QUEEN CONTEXT APPLIED"):
            projects = re.search(r"Projects:\s*(\d+)", value)
            packs = re.search(r"Packs:\s*(\d+)", value)
            return (
                "온톨로지 컨텍스트를 연결했습니다.\n"
                "프로젝트 %s개 · 팩 %s개\n"
                "다음 작업부터 이 컨텍스트를 사용합니다."
                % ((projects.group(1) if projects else "0"), (packs.group(1) if packs else "0"))
            )
        return CrabAgentApp._strip_internal_markers(value)

    def _set_transcript(self, text: str, *, follow: bool = True) -> None:
        self._transcript_text = text
        transcript = self.query_one("#transcript", ConversationTranscript)
        transcript.load_text(text)
        if follow:
            transcript.scroll_end(animate=False, immediate=True)

    def _append_transcript(self, text: str) -> None:
        self._set_transcript("%s%s%s" % (self._transcript_text, "\n\n" if self._transcript_text else "", text))

    def _append_note(self, content: str) -> None:
        self._append_transcript("SYSTEM\n%s" % self._friendly_system_text(content))

    def _apply_conversation_export(self, exported: Dict[str, Any]) -> None:
        messages = exported.get("messages") or []
        self.last_message_id = max((int(message["message_id"]) for message in messages), default=0)
        blocks = [
            self._format_message(message)
            for message in messages
            if self._is_user_facing_message(message)
        ]
        if exported.get("truncated"):
            blocks.append("SYSTEM\nOlder conversation messages are outside the 10,000-message selection window.")
        self._set_transcript("\n\n".join(blocks), follow=True)

    def _load_conversation(self) -> None:
        exported = self.client.request("conversation.export", session_id=self.session["session_id"])
        self._apply_conversation_export(exported)

    def _observed_activity(self) -> str:
        if self.session.get("status") != "running":
            if self._submitted_activity:
                return self._submitted_activity
            return "READY  thread preserved"
        pending = self.snapshot.get("pending_requests") or []
        if pending:
            return "ACTION REQUIRED  input needed"
        mission = self.snapshot.get("mission") or {}
        tasks = mission.get("tasks") or []
        running = next((task for task in tasks if task.get("status") == "running"), None)
        if running:
            return "%s  %s" % (str(running.get("role") or "WORKER"), str(running.get("title") or "active task")[:58])
        if mission:
            return "MISSION  %s" % str((mission.get("mission") or {}).get("status") or "active")
        return "CODEX TURN  active"

    def _tick_activity(self) -> None:
        if not self.is_mounted:
            return
        self._activity_frame = (self._activity_frame + 1) % 4
        observed = self._observed_activity()
        if self.session.get("status") == "running" or self._submitted_activity:
            dots = "." * self._activity_frame
            text = "WORKING%s  %s" % (dots, observed)
        else:
            text = observed
        try:
            self.query_one("#activity", Static).update(text)
        except Exception:
            return

    def _write_message(self, message: Dict[str, Any]) -> None:
        if not self._is_user_facing_message(message):
            return
        role = str(message["role"])
        metadata = message.get("metadata") or {}
        if role == "assistant" and metadata.get("interaction"):
            self.last_interaction = str(metadata["interaction"])
        self._append_transcript(self._format_message(message))

    def _render_assets(self) -> None:
        codex = self.assets.get("codex") or {}
        mcps = self.assets.get("mcp_servers") or []
        skills = self.assets.get("skills") or []
        clis = self.assets.get("clis") or {}
        text = (
            "Codex  %s\nMCP    %d  ·  Skills %d\nCLIs   %d observed\nOpenCrab\n%s"
            % (codex.get("status", "unknown"), len(mcps), len(skills), len(clis), self.assets.get("opencrab_signup_url", "https://opencrab.sh"))
        )
        self.query_one("#assets", Static).update(text)

    def _render_control_slots(self) -> None:
        """Keep header slots honest: show observed counts, not policy placeholders."""
        mcp_rows = self.assets.get("mcp_inventory") or []
        mcp_count = len(mcp_rows) or len(self.assets.get("mcp_servers") or [])
        cli_count = len(self.assets.get("clis") or {})
        self.query_one("#mcp-settings", Button).label = "%d · SET" % mcp_count
        self.query_one("#cli-settings", Button).label = "%d · SET" % cli_count

    @staticmethod
    def _enabled_mcp_names(policy: str, servers: list[Dict[str, Any]]) -> set[str]:
        names = {str(row.get("name") or "") for row in servers if row.get("name")}
        if policy == "all":
            return names
        if policy == "off":
            return set()
        if policy.startswith("allow:"):
            return {name for name in policy[6:].split(",") if name in names}
        return {str(row.get("name")) for row in servers if str(row.get("state") or "") == "enabled"}

    def _render_project_list(self) -> None:
        projects = [dict(row) for row in (self.colony.get("projects") or [])]
        if not projects:
            grouped: Dict[str, Dict[str, Any]] = {}
            for session in self.colony.get("sessions") or []:
                name = str(session.get("project_name") or "Unassigned")
                row = grouped.setdefault(
                    name,
                    {
                        "project_id": str(session.get("project_id") or ""),
                        "name": name,
                        "session_count": 0,
                    },
                )
                row["session_count"] += 1
            projects = list(grouped.values())
        projects.sort(key=lambda row: (str(row.get("name") or "").lower(), str(row.get("project_id") or "")))
        digest = json.dumps(projects, ensure_ascii=False, sort_keys=True)
        if digest == self._project_digest:
            return
        self._project_digest = digest
        self.query_one("#project-list", ProjectList).set_projects(projects)

    @staticmethod
    def _catalog_label(value: Any, limit: int) -> str:
        text = " ".join(str(value or "untitled").split())
        if cell_len(text) <= limit:
            return text
        truncated = Text(text)
        truncated.truncate(max(1, limit), overflow="ellipsis")
        return truncated.plain

    def _catalog_option_width(self) -> int:
        """Reserve enough width for the option list so labels stay one-line."""
        try:
            width = int(self.query_one("#ontology-options", OptionList).region.width)
        except Exception:
            width = 31
        return max(24, width - 3)

    def _catalog_option(self, prefix: str, value: Any, suffix: str, *, option_id: str, disabled: bool = False) -> Option:
        width = self._catalog_option_width()
        label = "%s%s%s" % (
            prefix,
            self._catalog_label(value, max(4, width - cell_len(prefix) - cell_len(suffix))),
            suffix,
        )
        # Keep each project/pack row on one line. The label is rebuilt when the
        # right panel width changes, so a wider panel reveals more of the title.
        return Option(Text(label, no_wrap=True), id=option_id, disabled=disabled)

    def _project_pack_ids(self, project_id: str) -> set[str]:
        return {
            str(row.get("package_id"))
            for row in (self._ontology_catalog.get("packs") or {}).get("items") or []
            if row.get("package_id") and str(row.get("project_id") or "") == project_id
        }

    def _render_live_catalog_options(self) -> None:
        payload = self._ontology_catalog or {}
        query = str(self.query_one("#ontology-search", Input).value or "").strip().lower()
        view = str(self.query_one("#ontology-view", Select).value or "all")
        options: list[Option] = []
        projects = payload.get("projects") or {}
        packs = payload.get("packs") or {}
        grouped: Dict[str, list[Dict[str, Any]]] = {}
        for package in packs.get("items") or []:
            if isinstance(package, dict):
                grouped.setdefault(str(package.get("project_id") or "unassigned"), []).append(package)
        seen_projects: set[str] = set()
        for project in projects.get("items") or []:
            project_id = str(project.get("project_id") or "")
            if not project_id:
                continue
            seen_projects.add(project_id)
            name = str(project.get("name") or "untitled")
            project_packs = grouped.get(project_id, [])
            matching_packs = [
                row for row in project_packs
                if not query or query in ("%s %s %s" % (row.get("title"), name, row.get("package_id"))).lower()
            ]
            project_matches = not query or query in ("%s %s" % (name, project_id)).lower()
            if view == "selected" and project_id not in self._ontology_selected_projects and not any(str(row.get("package_id")) in self._ontology_selected_packs for row in project_packs):
                continue
            if view == "packs" and not matching_packs:
                continue
            if view == "projects" and not project_matches:
                continue
            if view == "all" and not project_matches and not matching_packs:
                continue
            options.append(self._catalog_option(
                "PROJECT  ", name, "  (%d)" % len(project_packs),
                option_id="project-header:%s" % project_id, disabled=True,
            ))
            project_pack_ids = {str(row.get("package_id")) for row in project_packs if row.get("package_id")}
            selected_count = len(project_pack_ids & self._ontology_selected_packs)
            marker = "[x]" if project_pack_ids and selected_count == len(project_pack_ids) else "[-]" if selected_count else "[ ]"
            if view != "packs":
                options.append(self._catalog_option(
                    "%s PROJECT  " % marker, name, "  ALL",
                    option_id="project:%s" % project_id,
                ))
            if view != "projects":
                rows = matching_packs if query else project_packs
                for package in rows:
                    package_id = str(package.get("package_id") or "")
                    if view == "selected" and package_id not in self._ontology_selected_packs:
                        continue
                    marker = "[x]" if package_id in self._ontology_selected_packs else "[ ]"
                    options.append(self._catalog_option(
                        "%s PACK " % marker,
                        package.get("title") or package.get("name"),
                        "",
                        option_id="pack:%s" % package_id,
                    ))

        unassigned = grouped.get("unassigned", [])
        if view in {"all", "packs", "selected"}:
            rows = [row for row in unassigned if not query or query in ("%s %s %s" % (row.get("title"), row.get("project_name"), row.get("package_id"))).lower()]
            if view == "selected":
                rows = [row for row in rows if str(row.get("package_id")) in self._ontology_selected_packs]
            if rows:
                options.append(self._catalog_option(
                    "PACKS  ", "UNASSIGNED WORKSPACE", "  (%d)" % len(unassigned),
                    option_id="project-header:unassigned", disabled=True,
                ))
                for package in rows:
                    package_id = str(package.get("package_id") or "")
                    marker = "[x]" if package_id in self._ontology_selected_packs else "[ ]"
                    options.append(self._catalog_option(
                        "%s PACK " % marker,
                        package.get("title") or package.get("name"), "",
                        option_id="pack:%s" % package_id,
                    ))
        if not options:
            options = [Option("No matches", id="catalog-empty", disabled=True)]
        self.query_one("#ontology-options", OptionList).set_options(options)

    def _render_live_catalog(self) -> None:
        payload = self._ontology_catalog or {}
        account = payload.get("account") or {}
        projects = payload.get("projects") or {}
        packs = payload.get("packs") or {}
        selected = len(self._ontology_selected_packs)
        selected_projects = len(self._ontology_selected_projects)
        status = "complete" if payload.get("complete") else "linked projects first"
        if payload.get("cache_status") == "stale":
            status += "  · STALE %ss" % payload.get("cache_age_seconds", 0)
            if payload.get("refresh_deferred"):
                status += "  · refreshing"
        elif payload.get("cache_hit"):
            status += "  · cache %ss" % payload.get("cache_age_seconds", 0)
        lines = [
            "LIVE WORKSPACE",
            "Tier   %s" % (account.get("tier") or "unknown"),
            "Scope  %s" % (payload.get("display_scope") or payload.get("access_scope") or "unknown"),
            "Projects  %s    Packs  %s" % (projects.get("total", 0), packs.get("total", 0)),
            "State   %s" % status,
        ]
        if not payload.get("complete"):
            lines.append("Hydrating %s linked workspaces..." % payload.get("linked_workspace_count", 0))
        elif payload.get("linked_workspace_count"):
            lines.append("Source  %s linked workspaces" % payload.get("linked_workspace_count"))
        if payload.get("fallback_used"):
            lines.append("Mode    account query fallback")
        if payload.get("scope_warning"):
            lines.append(self._catalog_label(payload["scope_warning"], 90))
        if payload.get("catalog_error"):
            lines.append("Refresh failed: %s" % self._catalog_label(payload["catalog_error"], 90))
        self.query_one("#queen-ontology", OntologyInventory).load_text("\n".join(lines))
        selection = "Selected  %d projects  ·  %d packs" % (selected_projects, selected)
        if selected or selected_projects:
            selection += "  ·  click Use selected below"
        try:
            self.query_one("#ontology-selection", Static).update(selection)
            self.query_one("#ontology-apply", Button).label = "Use selected (%d)" % selected
        except Exception:
            # Test/minimal panels and teardown can receive a final background
            # response after their optional catalog widgets are gone.
            pass
        self._render_live_catalog_options()

    def _render_ontology_inventory(self) -> None:
        """Render live Workspace context, with the old local snapshot as fallback."""
        if self._ontology_catalog:
            # The catalog can contain hundreds of packs. Do not serialize the
            # whole payload on every 650ms state poll; revision changes are
            # recorded when a remote response arrives, while query/view and
            # selection are already small local values.
            digest = "%d|%d|%s|%s|%s|%s" % (
                self._ontology_catalog_revision,
                self._catalog_option_width(),
                self.query_one("#ontology-search", Input).value,
                self.query_one("#ontology-view", Select).value,
                ",".join(sorted(self._ontology_selected_projects)),
                ",".join(sorted(self._ontology_selected_packs)),
            )
            if digest != self._ontology_catalog_digest:
                self._ontology_catalog_digest = digest
                self._render_live_catalog()
            return
        current_path = snapshot_paths(self.workspace)["current"]
        try:
            mtime = current_path.stat().st_mtime_ns
        except OSError:
            mtime = -1
        if mtime == self._ontology_inventory_mtime:
            return
        self._ontology_inventory_mtime = mtime
        snapshot = load_current(self.workspace)
        if snapshot is None:
            if self._ontology_refreshing:
                self.query_one("#queen-ontology", OntologyInventory).load_text(
                    "Loading live OpenCrab Workspace catalog...\n"
                    "The panel will keep the current selection until the observed response arrives.\n\n"
                    "Context   0 packs\nProjects  pending"
                )
                return
            detail = "\n\n%s" % self._ontology_catalog_error if self._ontology_catalog_error else ""
            self.query_one("#queen-ontology", OntologyInventory).load_text(
                "Live OpenCrab Workspace catalog unavailable\nUse /ontology refresh%s\n\nContext   0 packs\nProjects  0" % detail
            )
            return

        display_snapshot = filter_inventory_for_display(snapshot, self.workspace)
        account = display_snapshot.get("account") or {}
        lines = [
            "Tier   %s" % (account.get("tier") or "unknown"),
            "Scope  %s" % (display_snapshot.get("display_scope") or display_snapshot.get("access_scope") or "unknown"),
            "Sync   %s" % (display_snapshot.get("sync_status") or display_snapshot.get("status") or "unknown"),
        ]
        if display_snapshot.get("scope_warning") and display_snapshot.get("display_scope", "").startswith("admin_"):
            lines.extend(["", str(display_snapshot["scope_warning"])])
        for group, label, identity_keys in (
            ("projects", "PROJECTS", ("name", "title")),
            ("packs", "PACKS", ("title", "name")),
        ):
            value = display_snapshot.get(group) or {}
            items = value.get("items") if isinstance(value, dict) else []
            items = items if isinstance(items, list) else []
            reported_total = value.get("total") if isinstance(value, dict) else None
            total = reported_total if reported_total is not None else len(items)
            suffix = "  stale" if isinstance(value, dict) and value.get("status") == "stale" else ""
            lines.append("")
            lines.append("%s (%s)%s" % (label, total, suffix))
            for item in items:
                if not isinstance(item, dict):
                    continue
                name = next((str(item.get(key) or "").strip() for key in identity_keys if item.get(key)), "untitled")
                lines.append("- %s" % name)
            if value.get("has_more"):
                lines.append("- ... more items in the local inventory")
        self.query_one("#queen-ontology", OntologyInventory).load_text("\n".join(lines))

    def _render_colony(self) -> None:
        ontology = self.colony.get("ontology") or {}
        if self._ontology_catalog:
            live_packs = (self._ontology_catalog.get("packs") or {}).get("total")
            packs = "unavailable" if live_packs is None else str(live_packs)
            ontology = dict(ontology)
            ontology["access_scope"] = self._ontology_catalog.get("display_scope") or self._ontology_catalog.get("access_scope")
        else:
            count = ontology.get("available_pack_count")
            packs = "unavailable" if count is None else "%s%s" % (count, "+" if ontology.get("has_more_packs") else "")
        self._render_project_list()
        active = bool(self.session.get("active_mission_id"))
        current = next(
            (
                row
                for row in self.colony.get("sessions") or []
                if str(row.get("session_id") or "") == str(self.session.get("session_id") or "")
            ),
            {},
        )
        current_roles = current.get("roles") or {}
        current_worker_active = sum(
            1
            for role_name, role in current_roles.items()
            if role_name == "WORKER" and str(role.get("status") or "") == "running"
        )
        soldier = current_roles.get("SOLDIER") or {}
        soldier_status = str(soldier.get("status") or "idle")
        soldier_label = str(soldier.get("title") or soldier_status) if soldier_status == "running" else soldier_status
        latest = next(
            (
                row.get("last_mission") or {}
                for row in self.colony.get("sessions") or []
                if str(row.get("session_id") or "") == str(self.session.get("session_id") or "")
            ),
            {},
        )
        if active:
            heading = "KING'S COLONY MISSION"
        elif latest.get("mission_id"):
            heading = "LAST COLONY RESULT"
        else:
            heading = "COLONY READY / DIRECT CHAT"
        last_status = str(latest.get("status") or "none").upper()
        last_line = "Mission  %s" % last_status if latest.get("mission_id") else "Mission  none"
        outcome = self.snapshot.get("outcome") if isinstance(self.snapshot.get("outcome"), dict) else {}
        outcome_line = "Oracle  %s" % str(outcome.get("verdict") or "pending").upper()
        if outcome:
            outcome_line += " · evidence %s · model turns %s" % (
                outcome.get("evidence", {}).get("count", 0),
                outcome.get("execution", {}).get("model_turns", 0),
            )
        self.query_one("#colony-summary", Static).update(
            "%s\nOntology  %s packs · shared context\nWorkers   %s active · cap %s\nPlan      %s runnable slots · no 1:1 pack mapping\nSoldier   %s\n%s\n%s\nScope     %s"
            % (
                heading,
                packs,
                current_worker_active,
                "AUTO" if str(current.get("worker_policy") or self.session.get("worker_policy") or "auto") == "auto" else str(current.get("worker_limit") or self.session.get("max_workers") or 3),
                current.get("worker_capacity", 0),
                soldier_label[:58],
                last_line,
                outcome_line,
                ontology.get("access_scope") or "unavailable",
            )
        )
        self._render_ontology_inventory()
        digest = json.dumps(self.colony.get("sessions") or [], ensure_ascii=False, sort_keys=True)
        if digest == self._colony_digest:
            return
        self._colony_digest = digest
        tree = self.query_one("#kings", Tree)
        tree.clear()
        groups: Dict[str, list[Dict[str, Any]]] = {}
        for king in self.colony.get("sessions") or []:
            groups.setdefault(str(king.get("project_name") or "Unassigned"), []).append(king)
        for project_name, kings in groups.items():
            project = tree.root.add("%s  ·  %d king%s" % (project_name, len(kings), "" if len(kings) == 1 else "s"), expand=True)
            for king in kings:
                title = str(king.get("king_title") or "Untitled mission")[:48]
                node = project.add("KING  ·  %s  [%s]" % (title, king.get("status", "unknown")), data={"session_id": king["session_id"]}, expand=False)
                roles = king.get("roles") or {}
                queen_count = king.get("queen_ontology_count", 0)
                node.add_leaf("QUEEN   %s packs in context" % queen_count)
                worker = roles.get("WORKER") or {"status": "idle", "title": "No active worker"}
                soldier = roles.get("SOLDIER") or {"status": "idle", "title": "No active patrol"}
                oracle = roles.get("ORACLE") or {"status": "idle", "title": "No Oracle result"}
                node.add_leaf("WORKER  %s · %s" % (worker.get("status"), worker.get("title")))
                node.add_leaf("SOLDIER %s · %s" % (soldier.get("status"), soldier.get("title")))
                node.add_leaf("ORACLE  %s · %s" % (oracle.get("status"), oracle.get("title")))

    def _load_live_catalog(
        self,
        *,
        force: bool = False,
        announce: bool = False,
        complete: bool = False,
        prefer_stale: bool = False,
    ) -> None:
        if self._ontology_refreshing:
            return
        if announce:
            self._catalog_refresh_attempted = False
        policy = str(self.session.get("mcp_policy") or "auto")
        if policy == "off" or (policy.startswith("allow:") and "OpenCrab" not in policy[6:].split(",")):
            self._ontology_catalog_error = "OpenCrab is disabled for this session"
            self._render_ontology_inventory()
            return
        self._ontology_refreshing = True
        if announce:
            self._append_note("OPENCRAB\nLoading the live Workspace catalog. Full pack JSON sync is not used here...")
        self._ontology_catalog_error = "Loading live OpenCrab Workspace catalog..."
        self._ontology_inventory_mtime = None
        self._render_ontology_inventory()

        def worker() -> None:
            try:
                result = self.client.request(
                    "ontology.catalog",
                    force=force,
                    complete=complete,
                    prefer_stale=prefer_stale,
                )
            except Exception as exc:
                result = {"status": "unavailable", "payload": {}, "error": str(exc)}
            self.call_from_thread(self._receive_live_catalog, result)

        threading.Thread(target=worker, daemon=True, name="crab-opencrab-catalog").start()

    def _receive_live_catalog(self, result: Dict[str, Any]) -> None:
        self._ontology_refreshing = False
        if not self.is_mounted:
            return
        if result.get("status") != "ok":
            self._ontology_catalog_error = str(result.get("error") or "unknown error")
            if self._ontology_catalog:
                # A background refresh is an optimization, not the source of
                # truth for the panel. Keep the last observed catalog visible
                # when the remote response is malformed, timed out, or empty.
                self._ontology_catalog = dict(self._ontology_catalog)
                self._ontology_catalog["refresh_deferred"] = False
                self._ontology_catalog["catalog_error"] = self._ontology_catalog_error
                self._ontology_catalog_revision += 1
                self._render_ontology_inventory()
                self._render_colony()
                self._append_note("OPENCRAB refresh failed; retained last catalog: %s" % self._ontology_catalog_error)
            else:
                self._render_ontology_inventory()
                self._append_note("OPENCRAB live catalog unavailable: %s" % self._ontology_catalog_error)
            return
        payload = result.get("payload") or {}
        self._ontology_catalog = payload
        self._ontology_catalog_error = ""
        if not payload.get("refresh_deferred"):
            self._catalog_refresh_attempted = False
        self._ontology_catalog_revision += 1
        context = self.session.get("ontology_context") or {}
        if not self._ontology_selection_reset and not self._ontology_selected_projects and not self._ontology_selected_packs:
            self._ontology_selected_projects = {str(value) for value in context.get("project_ids") or [] if str(value)}
            self._ontology_selected_packs = {str(value) for value in context.get("package_ids") or [] if str(value)}
        self._ontology_selection_reset = False
        self._ontology_catalog_digest = ""
        self._render_ontology_inventory()
        self._render_colony()
        if payload.get("refresh_deferred") and not self._catalog_refresh_attempted:
            self._catalog_refresh_attempted = True
            self._load_live_catalog(force=True, complete=True)
        elif not payload.get("complete"):
            self._load_live_catalog(force=False, complete=True)

    @on(Input.Changed, "#ontology-search")
    def search_ontology_catalog(self, event: Input.Changed) -> None:
        if self._ontology_catalog:
            self._ontology_catalog_digest = ""
            self._render_ontology_inventory()

    @on(Select.Changed, "#ontology-view")
    def change_ontology_view(self, event: Select.Changed) -> None:
        if self._ontology_catalog:
            self._ontology_catalog_digest = ""
            self._render_ontology_inventory()

    @on(OptionList.OptionSelected, "#ontology-options")
    def select_ontology_context(self, event: OptionList.OptionSelected) -> None:
        option_id = str(event.option_id or "")
        if option_id.startswith("project:"):
            project_id = option_id[len("project:"):]
            pack_ids = self._project_pack_ids(project_id)
            if pack_ids and pack_ids.issubset(self._ontology_selected_packs):
                self._ontology_selected_packs.difference_update(pack_ids)
                self._ontology_selected_projects.discard(project_id)
            else:
                self._ontology_selected_packs.update(pack_ids)
                self._ontology_selected_projects.add(project_id)
        elif option_id.startswith("pack:"):
            package_id = option_id[len("pack:"):]
            if package_id in self._ontology_selected_packs:
                self._ontology_selected_packs.remove(package_id)
            else:
                self._ontology_selected_packs.add(package_id)
            project_id = next((str(row.get("project_id")) for row in (self._ontology_catalog.get("packs") or {}).get("items") or [] if str(row.get("package_id")) == package_id), "")
            if project_id:
                project_pack_ids = self._project_pack_ids(project_id)
                if project_pack_ids and project_pack_ids.issubset(self._ontology_selected_packs):
                    self._ontology_selected_projects.add(project_id)
                else:
                    self._ontology_selected_projects.discard(project_id)
        else:
            return
        self._ontology_catalog_digest = ""
        self._render_ontology_inventory()

    def _reset_ontology_selection(self) -> None:
        """Clear visible selection while keeping the applied session context intact."""
        self._ontology_selected_projects.clear()
        self._ontology_selected_packs.clear()
        self._ontology_selection_reset = True
        self._ontology_catalog_digest = ""
        self._render_ontology_inventory()

    def _apply_ontology_context(self) -> None:
        catalog = self._ontology_catalog or {}
        project_labels = {
            str(row.get("project_id")): str(row.get("name") or "untitled")
            for row in (catalog.get("projects") or {}).get("items") or []
            if row.get("project_id") and str(row.get("project_id")) in self._ontology_selected_projects
        }
        package_labels = {
            str(row.get("package_id")): str(row.get("title") or row.get("name") or "untitled")
            for row in (catalog.get("packs") or {}).get("items") or []
            if row.get("package_id") and str(row.get("package_id")) in self._ontology_selected_packs
        }
        result = self.client.request(
            "ontology.context",
            session_id=self.session["session_id"],
            project_ids=sorted(self._ontology_selected_projects),
            package_ids=sorted(self._ontology_selected_packs),
            project_labels=project_labels,
            package_labels=package_labels,
        )
        self.session = result.get("session") or self.session
        self.session["ontology_context"] = result.get("context") or self.session.get("ontology_context") or {}
        self._append_note(
            "QUEEN CONTEXT APPLIED\nProjects: %d\nPacks: %d\nSelected references will be carried into the next Chat/Colony turn."
            % (len(self._ontology_selected_projects), len(self._ontology_selected_packs))
        )
        self._poll(force=True)

    @on(Tree.NodeSelected, "#kings")
    def select_king(self, event: Tree.NodeSelected) -> None:
        data = event.node.data
        if not isinstance(data, dict) or not data.get("session_id"):
            return
        if str(data["session_id"]) == str(self.session.get("session_id")):
            return
        self.session = self.client.request("session.ensure", session_id=str(data["session_id"]))
        self.last_message_id = 0
        self.last_interaction = ""
        self._load_conversation()
        self._poll(force=True)

    def _render_mission(self) -> None:
        mission = self.snapshot.get("mission")
        if self.session.get("status") == "running" and not self.session.get("active_mission_id"):
            self.query_one("#colony-summary", Static).update("DIRECT CHAT\n\nPersistent Codex thread\nNo colony spawned")
            self.query_one("#mission-title", Static).update("DIRECT CHAT  ·  persistent thread")
            return
        if not self.session.get("active_mission_id") and self.last_interaction == "chat":
            self.query_one("#colony-summary", Static).update("DIRECT CHAT\n\nPersistent Codex thread\nReady for the next turn")
            self.query_one("#mission-title", Static).update("CONVERSATION READY  ·  thread preserved")
            return
        if not mission:
            self.query_one("#mission-title", Static).update("READY / awaiting mission")
            return
        root = mission["mission"]
        assignments = {row["task_id"]: row for row in mission.get("assignments", [])}
        budget = mission.get("budget") or {}
        queue_count = len(self.snapshot.get("queue") or [])
        outcome = self.snapshot.get("outcome") if isinstance(self.snapshot.get("outcome"), dict) else {}
        outcome_suffix = ""
        if outcome:
            outcome_suffix = "  ·  outcome %s" % str(outcome.get("verdict") or "pending").upper()
        self.query_one("#mission-title", Static).update(
            "%s  ·  %s  ·  %s tokens  ·  queue %d  ·  Oracle %s%s"
            % (
                root["status"].upper(),
                str(root["objective"]).replace("\n", " ")[:108],
                budget.get("tokens_observed", "unobserved"),
                queue_count,
                "ready" if root.get("oracle_result_artifact_id") else "pending",
                outcome_suffix,
            )
        )

    @on(Button.Pressed, "#send")
    def send_button(self) -> None:
        self.action_submit_prompt()

    @on(Button.Pressed, "#copy-conversation")
    def copy_button(self) -> None:
        self.action_copy_conversation()

    @on(Button.Pressed, "#branch-conversation")
    def branch_button(self) -> None:
        self.action_branch_conversation()

    @on(Button.Pressed, "#hide-left")
    def hide_left_panel(self) -> None:
        self._set_panel_hidden("left", True)

    @on(Button.Pressed, "#hide-right")
    def hide_right_panel(self) -> None:
        self._set_panel_hidden("right", True)

    @on(Button.Pressed, "#show-panels")
    def show_panels(self) -> None:
        self._restore_panels()

    @on(Button.Pressed, "#mcp-settings")
    def mcp_settings_button(self) -> None:
        self._open_mcp_settings()

    @on(Button.Pressed, "#cli-settings")
    def cli_settings_button(self) -> None:
        self._open_cli_settings()

    @on(Button.Pressed)
    def project_button(self, event: Button.Pressed) -> None:
        button_id = str(event.button.id or "")
        if button_id == "show-panels":
            self._restore_panels()
        elif button_id == "ontology-refresh":
            self._reset_ontology_selection()
            self._load_live_catalog(force=True, announce=True, complete=True)
        elif button_id == "ontology-apply":
            try:
                self._apply_ontology_context()
            except Exception as exc:
                self._append_note("Queen context apply failed: %s" % exc)
                self.notify("Could not apply ontology context: %s" % exc, severity="error")
        elif button_id == "project-add":
            self.push_screen(
                ProjectFolderPrompt("Create a folder-based project", initial_path=str(self.workspace), submit_label="Create"),
                self._create_project,
            )
        elif button_id.startswith("project-new-session-"):
            project_id = button_id[len("project-new-session-"):]
            self._new_project_session(project_id)
        elif button_id.startswith("project-rename-"):
            project_id = button_id[len("project-rename-"):]
            project = self._project_by_id(project_id)
            if project:
                self.push_screen(
                    ProjectNamePrompt("Rename project", initial=str(project.get("name") or "")),
                    lambda name, project_id=project_id: self._rename_project(project_id, name),
                )
        elif button_id.startswith("project-delete-"):
            project_id = button_id[len("project-delete-"):]
            project = self._project_by_id(project_id)
            if project:
                self.push_screen(
                    ProjectDeletePrompt(str(project.get("name") or "project")),
                    lambda confirmed, project_id=project_id: self._delete_project(project_id, confirmed),
                )

    def _open_mcp_settings(self) -> None:
        try:
            result = self.client.request("mcp.list")
            servers = [dict(row) for row in result.get("servers") or [] if isinstance(row, dict) and row.get("name")]
            policy = str(self.session.get("mcp_policy") or "auto")
            enabled = self._enabled_mcp_names(policy, servers)
            self.push_screen(McpSettingsScreen(servers, enabled, policy), self._apply_mcp_settings)
        except Exception as exc:
            self._append_note("MCP settings unavailable: %s" % exc)
            self.notify("Could not load MCP settings: %s" % exc, severity="error")

    def _apply_mcp_settings(self, selected: Optional[list[str]]) -> None:
        if selected is None:
            return
        try:
            result = self.client.request("mcp.list")
            servers = [dict(row) for row in result.get("servers") or [] if isinstance(row, dict) and row.get("name")]
            names = {str(row["name"]) for row in servers}
            desired = {str(name) for name in selected if str(name) in names}
            if desired == names:
                self.session = self.client.request("mcp.configure", session_id=self.session["session_id"], mode="all")
            elif not desired:
                self.session = self.client.request("mcp.configure", session_id=self.session["session_id"], mode="off")
            else:
                self.session = self.client.request("mcp.configure", session_id=self.session["session_id"], mode="off")
                for name in sorted(desired):
                    self.session = self.client.request(
                        "mcp.configure",
                        session_id=self.session["session_id"],
                        server=name,
                        enabled=True,
                    )
            self._append_note("MCP SETTINGS\nSession servers: %s" % (", ".join(sorted(desired)) or "none"))
            self._render_control_slots()
            self._poll(force=True)
            if desired:
                self._load_live_catalog(force=True, announce=False)
        except Exception as exc:
            self._append_note("MCP settings apply failed: %s" % exc)
            self.notify("Could not apply MCP settings: %s" % exc, severity="error")

    def _open_cli_settings(self) -> None:
        try:
            result = self.client.request("cli.list")
            clis = {str(name): str(path) for name, path in (result.get("clis") or {}).items()}
            selected = str(self.session.get("cli_policy") or "auto")
            self.push_screen(CliSettingsScreen(clis, dict(result.get("codex") or {}), selected), self._apply_cli_settings)
        except Exception as exc:
            self._append_note("CLI settings unavailable: %s" % exc)
            self.notify("Could not load CLI settings: %s" % exc, severity="error")

    def _apply_cli_settings(self, selected: Optional[str]) -> None:
        if selected is None:
            return
        try:
            self.session = self.client.request(
                "session.configure",
                session_id=self.session["session_id"],
                model_policy=str(self.session.get("model_policy") or "auto"),
                interaction_mode=str(self.session.get("interaction_mode") or "auto"),
                mcp_policy=str(self.session.get("mcp_policy") or "auto"),
                cli_policy=str(selected or "auto"),
                max_workers=int(self.session.get("max_workers") or 3),
            )
            self._append_note("CLI SETTINGS\nPreferred route: %s" % (selected or "auto"))
            self._render_control_slots()
            self._poll(force=True)
        except Exception as exc:
            self._append_note("CLI settings apply failed: %s" % exc)
            self.notify("Could not apply CLI settings: %s" % exc, severity="error")
    def _project_by_id(self, project_id: str) -> Optional[Dict[str, Any]]:
        for project in self.query_one("#project-list", ProjectList).projects:
            if str(project.get("project_id") or "") == project_id:
                return project
        return None

    def _create_project(self, data: Optional[Any]) -> None:
        if not data:
            return
        if isinstance(data, str):
            name, root_path = data, ""
        else:
            name = str(data.get("name") or "").strip()
            root_path = str(data.get("root_path") or "").strip()
        if not name:
            return
        try:
            project = self.client.request("project.create", name=name, root_path=root_path)
            self.session = self.client.request(
                "project.assign",
                session_id=self.session["session_id"],
                project_id=str(project.get("project_id") or ""),
                project_name=name,
                root_path=str(project.get("root_path") or root_path),
            )
            self._append_note("Project created: %s\nFolder: %s" % (name, project.get("root_path") or root_path or "not set"))
            self._poll(force=True)
        except Exception as exc:
            self._append_note("Project create failed: %s" % exc)
            self.notify("Could not create project: %s" % exc, severity="error")

    def _rename_project(self, project_id: str, name: Optional[str]) -> None:
        if not name:
            return
        try:
            result = self.client.request("project.rename", project_id=project_id, name=name)
            self._append_note("Project renamed: %s" % result.get("name", name))
            self._poll(force=True)
        except Exception as exc:
            self._append_note("Project rename failed: %s" % exc)
            self.notify("Could not rename project: %s" % exc, severity="error")

    def _delete_project(self, project_id: str, confirmed: bool) -> None:
        if not confirmed:
            return
        project = self._project_by_id(project_id) or {}
        try:
            self.client.request("project.delete", project_id=project_id)
            if str(self.session.get("project_id") or "") == project_id:
                self.session = self.client.request("project.clear", session_id=self.session["session_id"])
            self._append_note("Project deleted: %s" % (project.get("name") or project_id))
            self._poll(force=True)
        except Exception as exc:
            self._append_note("Project delete failed: %s" % exc)
            self.notify("Could not delete project: %s" % exc, severity="error")

    def _new_project_session(self, project_id: str) -> None:
        if self.session.get("status") == "running":
            self.notify("Stop or finish the current turn before creating a new conversation.", severity="warning")
            return
        project = self._project_by_id(project_id)
        if not project:
            self.notify("Project is no longer available.", severity="error")
            return
        try:
            self.session = self.client.request(
                "session.create",
                title="%s conversation" % str(project.get("name") or "Project"),
                project_id=project_id,
                project_name=str(project.get("name") or ""),
                project_root_path=str(project.get("root_path") or ""),
            )
            self.last_message_id = 0
            self.last_interaction = ""
            self._submitted_activity = ""
            self._set_transcript("")
            self._append_note("New conversation in %s\nFolder: %s" % (project.get("name"), project.get("root_path") or "not set"))
            self._poll(force=True)
        except Exception as exc:
            self._append_note("New project conversation failed: %s" % exc)
            self.notify("Could not create the project conversation: %s" % exc, severity="error")

    @on(Input.Submitted, "#composer")
    def enter_submit(self) -> None:
        self.action_submit_prompt()

    def action_submit_prompt(self) -> None:
        composer = self.query_one("#composer", Input)
        prompt = composer.value.strip()
        if not prompt:
            return
        if prompt.startswith("/"):
            composer.value = ""
            self._slash(prompt)
            return
        running = self.session.get("status") == "running"
        if running:
            self.push_screen(QueueChoice(), lambda choice: self._submit_with(choice, prompt))
        else:
            self._submit_with("start", prompt)

    def _submit_with(self, disposition: Optional[str], prompt: str) -> None:
        if not disposition or disposition == "cancel":
            return
        try:
            result = self.client.request("prompt.submit", session_id=self.session["session_id"], objective=prompt, disposition=disposition)
        except Exception as exc:
            self._append_note("Prompt submit failed: %s" % exc)
            self.notify("Could not submit prompt: %s" % exc, severity="error")
            return
        status = str(result.get("status") or "submitted")
        self._submitted_activity = "SUBMITTED  %s" % status
        self._saw_active_after_submit = False
        self._submitted_at = time.monotonic()
        self.query_one("#composer", Input).value = ""
        self.query_one("#composer", Input).focus()
        self._poll(force=True)

    def _approval_result(self, response: Optional[Dict[str, Any]]) -> None:
        request_id = self.active_request_id
        self.active_request_id = ""
        request = self.active_request
        self.active_request = {}
        if response is None:
            return
        result: Dict[str, Any] = {}
        method = str(request.get("method") or "")
        if method == "item/tool/requestUserInput" and response.get("approved"):
            questions = (request.get("payload") or {}).get("params", {}).get("questions") or []
            answers = {}
            for question in questions:
                question_id = str(question.get("id") or "") if isinstance(question, dict) else ""
                if question_id:
                    answers[question_id] = {"answers": [str(response.get("text") or "")]}
            result = {"answers": answers}
        self.client.request("runtime.respond", session_id=self.session["session_id"], request_id=request_id, approve=bool(response.get("approved")), result=result)

    @on(Select.Changed)
    def configure(self, event: Select.Changed) -> None:
        if not self.session or event.select.id not in {"model", "mode", "workers"}:
            return
        model = str(self.query_one("#model", Select).value or "auto")
        mode = str(self.query_one("#mode", Select).value or "auto")
        mcp = str(self.session.get("mcp_policy") or "auto")
        cli = str(self.session.get("cli_policy") or "auto")
        worker_value = str(self.query_one("#workers", Select).value or "auto")
        worker_policy = "auto" if worker_value == "auto" else "fixed"
        workers = 3 if worker_policy == "auto" else int(worker_value)
        try:
            self.session = self.client.request(
                "session.configure",
                session_id=self.session["session_id"],
                model_policy=model,
                interaction_mode=mode,
                mcp_policy=mcp,
                cli_policy=cli,
                max_workers=workers,
                worker_policy=worker_policy,
            )
        except Exception as exc:
            self._append_note("Session configuration failed: %s" % exc)
            self.notify("Could not update session settings: %s" % exc, severity="error")

    def _slash(self, command: str) -> None:
        try:
            self._slash_impl(command)
        except Exception as exc:
            self._append_note("Command failed: %s" % exc)
            name = command.split()[0] if command.split() else "/command"
            self.notify("Could not run %s: %s" % (name, exc), severity="error")

    def _slash_impl(self, command: str) -> None:
        parts = command.split()
        name = parts[0].lower()
        if name == "/stop":
            self.action_interrupt()
        elif name == "/new":
            self.action_new_session()
        elif name == "/assets":
            self._append_note(str(self.assets))
        elif name == "/status":
            self._append_note(str(self.colony or "No observed colony state"))
        elif name == "/goal":
            self._slash_goal(parts)
        elif name == "/mcp":
            self._slash_mcp(parts)
        elif name == "/doc":
            self._slash_doc(parts)
        elif name == "/ontology":
            self._slash_ontology(parts)
        elif name == "/opencrab":
            self._offer_opencrab_connection()
        elif name == "/cli":
            self._slash_cli()
        elif name in {"/ingest", "/pack"}:
            self._slash_ingest(parts[1:])
        elif name in {"/orchestrate", "/orchestration"}:
            self._slash_orchestrate(parts[1:])
        elif name == "/retry":
            self._slash_retry(parts)
        elif name == "/copy":
            self.action_copy_conversation()
        elif name == "/branch":
            self.action_branch_conversation()
        elif name == "/panels":
            self.show_panels()
        elif name == "/project":
            self._slash_project(parts)
        else:
            self._append_note("/goal OBJECTIVE  /doc [list|import PATH]  /mcp [auto|all|off|on NAME|off NAME]  /ontology  /opencrab  /cli  /ingest [FOLDER]  /orchestrate [plan|run|status|stop]  /retry [force]  /panels  /copy  /branch  /project [set NAME|opencrab NAME|clear]  /new  /stop")

    def _slash_retry(self, parts: list[str]) -> None:
        force = len(parts) > 1 and parts[1].lower() == "force"
        try:
            result = self.client.request("mission.retry", session_id=self.session["session_id"], force=force)
        except Exception as exc:
            self._append_note("RETRY BLOCKED\n%s" % exc)
            self.notify("Retry was not started: %s" % exc, severity="warning")
            return
        self._append_note(
            "RETRY STARTED\nMission: %s\nSource: %s"
            % (result.get("objective", ""), result.get("retry_of", "previous mission"))
        )
        self._poll(force=True)

    def _slash_orchestrate(self, parts: list[str]) -> None:
        """Expose bounded multi-session fan-out without hiding runtime truth."""
        action = parts[0].lower() if parts else "status"
        if action == "status":
            run_id = parts[1] if len(parts) > 1 else ""
            result = self.client.request("orchestration.status", orchestration_id=run_id)
            if run_id:
                state = result.get("state") or {}
                lines = ["ORCHESTRATION %s" % state.get("orchestration_id"), "Status: %s" % state.get("status")]
                lines.extend("%s  %s  %s" % (row.get("child_id"), row.get("status"), row.get("objective")) for row in state.get("children") or [])
            else:
                lines = [
                    "%s  %s  %s child(ren)" % (row.get("orchestration_id"), row.get("status"), row.get("child_count", 0))
                    for row in result.get("summaries") or []
                ] or ["No orchestrations yet"]
            self._append_note("ORCHESTRATIONS\n" + "\n".join(lines))
            return
        if action == "plan":
            specification = " ".join(parts[1:]).strip()
            if not specification:
                self._append_note("Usage: /orchestrate plan OBJECTIVE or /orchestrate plan SESSION::OBJECTIVE | SESSION::OBJECTIVE")
                return
            children = []
            for item in specification.split("|"):
                item = item.strip()
                if "::" in item:
                    session_id, objective = item.split("::", 1)
                else:
                    session_id, objective = self.session["session_id"], item
                children.append({"session_id": session_id.strip(), "objective": objective.strip()})
            result = self.client.request("orchestration.plan", session_id=self.session["session_id"], children=children, max_parallel=2)
            state = result["state"]
            self._append_note("ORCHESTRATION PLANNED\n%s\nChildren: %d\nRun: /orchestrate run %s" % (state["orchestration_id"], len(state.get("children") or []), state["orchestration_id"]))
            return
        if action == "run" and len(parts) > 1:
            result = self.client.request("orchestration.run", orchestration_id=parts[1])
            self._append_note("ORCHESTRATION %s" % result.get("status", "started"))
            return
        if action == "stop" and len(parts) > 1:
            result = self.client.request("orchestration.stop", orchestration_id=parts[1])
            self._append_note("ORCHESTRATION %s" % result.get("status", "stopping"))
            return
        self._append_note("Usage: /orchestrate plan OBJECTIVE | /orchestrate run ID | /orchestrate status [ID] | /orchestrate stop ID")

    def _slash_goal(self, parts: list[str]) -> None:
        objective = " ".join(parts[1:]).strip()
        if not objective:
            self._append_note("Usage: /goal OBJECTIVE")
            return
        result = self.client.request("goal.preview", session_id=self.session["session_id"], objective=objective)
        plan = result.get("plan") or {}
        self._append_note(
            "GOAL PLAN\nKind: %s\nComplexity: %s\nStages: %s\nOntology: %s\nEstimated model turns: %s\nToken target: %s\nReason: %s"
            % (
                plan.get("kind", "unknown"),
                plan.get("complexity", "unknown"),
                " -> ".join(plan.get("stages") or []),
                "required" if plan.get("ontology_required") else "not required",
                plan.get("estimated_model_turns", "unknown"),
                plan.get("token_budget", "unknown"),
                "; ".join(plan.get("rationale") or []) or "none",
            )
        )

    def _slash_mcp(self, parts: list[str]) -> None:
        if len(parts) == 1:
            result = self.client.request("mcp.list")
            rows = result.get("servers") or []
            policy = self.session.get("mcp_policy") or "auto"
            listing = "\n".join("%s  %s" % (row.get("name"), row.get("state")) for row in rows) or "No configured MCP servers"
            self._append_note("MCP session policy: %s\n%s" % (policy, listing))
            return
        action = parts[1].lower()
        if action in {"auto", "all", "off"} and len(parts) == 2:
            self.session = self.client.request("mcp.configure", session_id=self.session["session_id"], mode=action)
        elif action in {"on", "off"} and len(parts) >= 3:
            self.session = self.client.request("mcp.configure", session_id=self.session["session_id"], server=" ".join(parts[2:]), enabled=action == "on")
        else:
            self._append_note("Usage: /mcp [auto|all|off|on NAME|off NAME]")
            return
        policy = str(self.session.get("mcp_policy") or "auto")
        self._render_control_slots()
        self._append_note("MCP routing policy: %s" % policy)
        if policy not in {"off"} and not (policy.startswith("allow:") and "OpenCrab" not in policy[6:].split(",")):
            self._load_live_catalog(force=True, announce=False)

    def _slash_ontology(self, parts: list[str]) -> None:
        onboarding = local_onboarding_status(self.workspace)
        if onboarding.get("opencrab", {}).get("status") not in {"connected", "configured"}:
            self._append_note("OpenCrab is not connected yet. Local mode remains available.")
            self._offer_opencrab_connection()
            return
        policy = str(self.session.get("mcp_policy") or "auto")
        if policy == "off" or (policy.startswith("allow:") and "OpenCrab" not in policy[6:].split(",")):
            self._append_note("OpenCrab is disabled for this session. Run /mcp on OpenCrab first.")
            return
        action = parts[1].lower() if len(parts) > 1 else "show"
        if action == "diff":
            self._show_ontology_diff(self.client.request("ontology.diff"))
            return
        if action not in {"sync"}:
            self._load_live_catalog(force=True, announce=True, complete=True)
            return
        if self._ontology_refreshing:
            self._append_note("OpenCrab sync already running.")
            return
        self._ontology_refreshing = True
        self._append_note("OPENCRAB\nSyncing the full read-only inventory once; later turns use local deltas...")
        threading.Thread(target=self._refresh_ontology, daemon=True, name="crab-opencrab-refresh").start()

    def _offer_opencrab_connection(self) -> None:
        if self.session.get("status") == "running":
            self.notify("Finish or stop the current turn before changing OpenCrab connection.", severity="warning")
            return
        self.push_screen(OpenCrabEndpointScreen(), self._connect_opencrab)

    def _refresh_ontology(self) -> None:
        try:
            snapshot = self.client.request("ontology.sync")
            self.call_from_thread(self._show_ontology, snapshot)
        except Exception as exc:
            self.call_from_thread(self._show_ontology, {"status": "unavailable", "payload": {}, "error": str(exc)})

    def _show_ontology(self, cached: Dict[str, Any]) -> None:
        self._ontology_refreshing = False
        if cached.get("status") != "ok":
            self._append_note("OPENCRAB unavailable: %s" % cached.get("error", "unknown error"))
            return
        payload = cached.get("payload") or {}
        account = payload.get("account") or {}
        packs = payload.get("packs") or {}
        projects = payload.get("projects") or {}
        workflows = payload.get("workflows") or {}
        def names(group: Dict[str, Any], label: str) -> str:
            values = [str(item.get("title") or item.get("name") or "untitled") for item in group.get("items") or []]
            count = group.get("total", "?")
            suffix = "+" if group.get("has_more") else ""
            return "%s (%s%s): %s" % (label, count, suffix, ", ".join(values[:12]) or "none")
        self._append_note(
            "OPENCRAB  %s\nTier: %s  Scope: %s\n%s\n%s\n%s\n%s"
            % (
                payload.get("status"), account.get("tier", "unknown"), payload.get("access_scope", "unknown"),
                payload.get("scope_warning", ""), names(packs, "Packs"), names(projects, "Projects"), names(workflows, "Workflows"),
            )
        )
        self._poll(force=True)

    def _show_ontology_diff(self, result: Dict[str, Any]) -> None:
        if result.get("status") != "ok":
            self._append_note("OPENCRAB diff unavailable: %s" % result.get("error", "run /ontology sync first"))
            return
        diff = result.get("diff") or {}
        summary = diff.get("summary") or {}
        lines = ["OPENCRAB DELTA", "Added: %s  Removed: %s  Changed: %s" % (summary.get("added", 0), summary.get("removed", 0), summary.get("changed", 0))]
        for group, value in (diff.get("groups") or {}).items():
            identity = value.get("identity")
            added = [str(row.get(identity)) for row in value.get("added", [])[:8]]
            removed = [str(row.get(identity)) for row in value.get("removed", [])[:8]]
            changed = [str(row.get(identity)) for row in value.get("changed", [])[:8]]
            if added or removed or changed:
                lines.append("%s +%s -%s ~%s" % (group, ", ".join(added) or "0", ", ".join(removed) or "0", ", ".join(changed) or "0"))
        self._append_note("\n".join(lines))

    def _slash_cli(self) -> None:
        result = self.client.request("cli.list")
        clis = result.get("clis") or {}
        lines = ["%s  available" % name for name in sorted(clis)]
        lines.append("Codex  %s" % (result.get("codex") or {}).get("status", "unknown"))
        lines.append("Control boundary: CrabAgent only invokes its allowlisted runtime paths.")
        self._append_note("CLI\n%s" % "\n".join(lines))

    def _slash_ingest(self, parts: list[str]) -> None:
        if parts and parts[0].lower() == "status":
            if len(parts) < 2:
                self._append_note("PACK STATUS\nUsage: /ingest status RUN_ID")
                return
            try:
                record = self.client.request("pack.ingest.status", run_id=parts[1])
                saved = record.get("result") if isinstance(record.get("result"), dict) else record
                self._append_note(
                    "PACK STATUS\nRun: %s\nStatus: %s\nPackage: %s\nEvidence ZIP: %s\n%s"
                    % (
                        parts[1],
                        saved.get("status", record.get("status", "unknown")),
                        saved.get("package_id", "pending"),
                        saved.get("evidence_zip_path", "not created"),
                        saved.get("next_action") or saved.get("reason") or "",
                    )
                )
            except Exception as exc:
                self._append_note("PACK STATUS unavailable: %s" % exc)
            return
        project_id = str(self.session.get("project_id") or "")
        project = self._project_by_id(project_id) if project_id else None
        folder = " ".join(parts).strip() if parts else str((project or {}).get("root_path") or self.session.get("project_root_path") or "")
        if not folder:
            self._append_note("PACK BUILD\nSelect a folder-based project first, or use /ingest /path/to/folder")
            return
        self._append_note("PACK BUILD\nReading full supported text sources from %s\nExpert tier check and OpenCrab CrabAgent ingest pending..." % folder)
        threading.Thread(
            target=self._run_pack_ingest,
            args=(folder, project_id, str((project or {}).get("name") or self.session.get("project_name") or "")),
            daemon=True,
            name="crab-pack-ingest",
        ).start()

    def _run_pack_ingest(self, folder: str, project_id: str, project_name: str) -> None:
        try:
            result = self.client.request(
                "pack.ingest",
                folder_path=folder,
                project_id=project_id,
                project_name=project_name,
            )
        except Exception as exc:
            result = {"status": "failed", "reason": str(exc), "root_path": folder}
        self.call_from_thread(self._show_pack_ingest, result)
        if str(result.get("status") or "") == "upload_pending":
            self.call_from_thread(self._start_pack_status_watch, result)

    @staticmethod
    def _pack_status_payload(record: Dict[str, Any]) -> Dict[str, Any]:
        saved = dict(record.get("result") or {}) if isinstance(record, dict) else {}
        if not saved:
            saved = dict(record or {})
        if isinstance(record, dict):
            for key in ("run_id", "project_id", "folder_path", "status"):
                if record.get(key) is not None:
                    saved.setdefault(key, record.get(key))
            if record.get("status"):
                saved["status"] = record.get("status")
        return saved

    def _start_pack_status_watch(self, result: Dict[str, Any]) -> None:
        """Reflect remote completion in the open panel without blocking the UI."""
        run_id = str(result.get("run_id") or "")
        if not run_id or run_id in self._pack_watch_runs:
            return
        self._pack_watch_runs.add(run_id)

        def watch() -> None:
            latest: Dict[str, Any] = result
            try:
                for _ in range(18):
                    time.sleep(2.0)
                    record = self.client.request("pack.ingest.status", run_id=run_id)
                    latest = self._pack_status_payload(record)
                    if str(latest.get("status") or "") in {"ingested", "upload_failed", "cancelled"}:
                        self.call_from_thread(self._show_pack_ingest, latest)
                        return
                latest = dict(latest)
                latest.setdefault("status", "upload_pending")
                latest.setdefault("reason", "The upload is still pending; the durable run remains available for /ingest status.")
                self.call_from_thread(self._show_pack_ingest, latest)
            except Exception as exc:
                latest = dict(latest)
                latest["status"] = "upload_pending"
                latest["reason"] = "Automatic status watch paused: %s" % exc
                self.call_from_thread(self._show_pack_ingest, latest)
            finally:
                self._pack_watch_runs.discard(run_id)

    def _show_pack_ingest(self, result: Dict[str, Any]) -> None:
        status = str(result.get("status") or "unknown").upper()
        lines = [
            "PACK BUILD / OPENCRAB CRABAGENT",
            "Status: %s" % status,
            "Sources: %s  Evidence chunks: %s" % (result.get("source_count", 0), result.get("chunk_count", 0)),
            "Stage: %s" % result.get("stage_path", "unavailable"),
            "Evidence ZIP: %s" % result.get("evidence_zip_path", "not created"),
        ]
        if result.get("package_id"):
            lines.append("Package: %s" % result["package_id"])
        if result.get("reason"):
            lines.append("Reason: %s" % result["reason"])
        if status == "INGESTED":
            lines.append("OpenCrab CrabAgent confirmed the cloud package identity.")
        elif status == "UPLOAD_PENDING":
            lines.append("The local handoff is saved. OpenCrab has not confirmed the cloud package yet.")
            lines.append("Use /ingest status %s to check again." % result.get("run_id", "RUN_ID"))
        elif status == "PLAN_READY":
            lines.append("OpenCrab returned a local build plan. Run the saved handoff, then check the upload status.")
        elif status == "EXPERT_REQUIRED":
            lines.append("This operation is available only on Expert or higher tiers.")
        elif status == "REMOTE_ACCEPTED":
            lines.append("OpenCrab accepted the request, but no package identity was returned yet.")
        else:
            lines.append("No remote ingest is claimed without an observed MCP confirmation.")
        self._append_note("\n".join(lines))
        self._poll(force=True)

    def _slash_doc(self, parts: list[str]) -> None:
        if len(parts) == 1 or parts[1].lower() == "list":
            result = self.client.request("doc.list")
            rows = result.get("documents") or []
            if not rows:
                self._append_note("CRAB DOC\nNo connected documents in this KINGCRAB workspace.")
                return
            lines = ["CRAB DOC\n%s document(s)" % result.get("count", len(rows))]
            lines.extend("%s  %s  %s" % (row.get("title", ""), row.get("format", ""), row.get("markdown_path", "")) for row in rows)
            self._append_note("\n".join(lines))
            return
        if parts[1].lower() in {"import", "open"} and len(parts) >= 3:
            result = self.client.request("doc.import", manifest_path=" ".join(parts[2:]))
            self._append_note("CRAB DOC connected\n%s\nContext: %s" % (result.get("title", ""), result.get("markdown_path", "")))
            return
        self._append_note("Usage: /doc  /doc list  /doc import PATH")

    def _slash_project(self, parts: list[str]) -> None:
        if len(parts) == 1:
            overview = self.client.request("project.list")
            groups: Dict[str, int] = {}
            for king in overview.get("sessions") or []:
                groups[str(king.get("project_name") or "Unassigned")] = groups.get(str(king.get("project_name") or "Unassigned"), 0) + 1
            self._append_note("PROJECTS\n%s" % "\n".join("%s  %d king(s)" % item for item in groups.items()))
            return
        action = parts[1].lower()
        if action == "clear":
            self.session = self.client.request("project.clear", session_id=self.session["session_id"])
        elif action == "set" and len(parts) >= 3:
            self.session = self.client.request("project.assign", session_id=self.session["session_id"], project_name=" ".join(parts[2:]))
        elif action == "opencrab" and len(parts) >= 3:
            wanted = " ".join(parts[2:]).lower()
            cached = self.client.request("ontology.catalog")
            projects = ((cached.get("payload") or {}).get("projects") or {}).get("items") or []
            project = next((row for row in projects if str(row.get("name") or "").lower() == wanted), None)
            if not project:
                self._append_note("Use an exact project name from the live /ontology Workspace catalog.")
                return
            self.session = self.client.request(
                "project.assign",
                session_id=self.session["session_id"],
                project_id=str(project.get("project_id") or ""),
                project_name=str(project.get("name") or "OpenCrab project"),
                ontology_context_count=int(project.get("package_count") or 0),
            )
            packages = [row for row in project.get("packages") or [] if row.get("package_id")]
            context_result = self.client.request(
                "ontology.context",
                session_id=self.session["session_id"],
                project_ids=[str(project.get("project_id") or "")],
                package_ids=[str(row["package_id"]) for row in packages],
                project_labels={str(project.get("project_id")): str(project.get("name") or "OpenCrab project")},
                package_labels={str(row["package_id"]): str(row.get("title") or row.get("name") or "untitled") for row in packages},
            )
            self.session = context_result.get("session") or self.session
            self._ontology_selected_projects = {str(project.get("project_id") or "")}
            self._ontology_selected_packs = {str(row["package_id"]) for row in packages}
        else:
            self._append_note("Usage: /project [set NAME|opencrab EXACT_NAME|clear]")
            return
        self._append_note("Project context: %s (%s ontology packs)" % (self.session.get("project_name") or "Unassigned", self.session.get("ontology_context_count", 0)))
        self._poll(force=True)

    def action_interrupt(self) -> None:
        try:
            result = self.client.request("session.interrupt", session_id=self.session["session_id"])
            if result.get("interrupted"):
                self._submitted_activity = "STOP REQUESTED"
                self._append_note("Stop requested. The current role will close its lease at the next safe checkpoint.")
            else:
                self._append_note("No active role was running.")
            self._poll(force=True)
        except Exception as exc:
            self._append_note("Stop failed: %s" % exc)
            self.notify("Could not stop the current turn: %s" % exc, severity="error")

    def action_copy_conversation(self) -> None:
        transcript = self.query_one("#transcript", ConversationTranscript)
        selected = transcript.selected_text
        content = selected or self._transcript_text
        if not content:
            self.notify("There is no conversation text to copy.", severity="warning")
            return
        self.copy_to_clipboard(content)
        native = self._clipboard_writer(content)
        suffix = "selected conversation" if selected else "conversation"
        self.notify("Copied %s%s." % (suffix, " to macOS clipboard" if native else ""))

    def action_branch_conversation(self) -> None:
        if self.session.get("status") == "running":
            self.notify("Finish or stop the active turn before branching.", severity="warning")
            return
        try:
            result = self.client.request("session.fork", session_id=self.session["session_id"])
        except Exception as exc:
            self.notify("Could not branch this conversation: %s" % exc, severity="error")
            return
        self.session = result["session"]
        self.last_message_id = 0
        self.last_interaction = ""
        self._submitted_activity = ""
        self._saw_active_after_submit = False
        self._load_conversation()
        self._poll(force=True)
        context = "Codex context forked" if result.get("codex_context_forked") else "local conversation copied"
        self.notify("Branch created: %s, %s messages." % (context, result.get("copied_messages", 0)))

    def action_new_session(self) -> None:
        if self.session.get("status") == "running":
            self.notify("Stop or finish the current mission before creating a new session.", severity="warning")
            return
        try:
            self.session = self.client.request(
                "session.create",
                title="CrabAgent session",
                project_id=str(self.session.get("project_id") or ""),
                project_name=str(self.session.get("project_name") or ""),
                project_root_path=str(self.session.get("project_root_path") or ""),
            )
            self.last_message_id = 0
            self.last_interaction = ""
            self._set_transcript("")
            self._append_note("New durable session")
            self._poll(force=True)
        except Exception as exc:
            self._append_note("New session failed: %s" % exc)
            self.notify("Could not create a new session: %s" % exc, severity="error")

    def action_help(self) -> None:
        self._append_note("Ctrl+J send  Ctrl+X stop  Ctrl+N new session  Ctrl+Q quit\nCtrl+Shift+C copy  Ctrl+Shift+B branch\n/ingest [FOLDER]  /doc list  /doc import PATH  ·  During work: Now interrupts safely and queues the new instruction; Wait runs it next.")


def run_tui(workspace: Path) -> None:
    CrabAgentApp(workspace, interactive_onboarding=True).run()
