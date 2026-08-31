"""Stable local workspace defaults for TB missions."""

from pathlib import Path, PureWindowsPath


# Keep the TB source/work root independent from the directory that happened to
# launch the CLI. On the Windows host this resolves to
# ``C:\\Users\\USER\\Downloads\\opencrab_packs``.
TB_WORK_ROOT = Path.home() / "Downloads" / "opencrab_packs"


def is_legacy_tb_scratch_root(value: object) -> bool:
    """Identify the former benchmark scratch default without matching projects."""

    raw = str(value or "")
    path = PureWindowsPath(raw) if "\\" in raw else Path(raw).expanduser()
    return path.name.casefold() == "final-bench-tb-s1" and path.parent.name.casefold() == "scratch"


def default_workspace() -> Path:
    """Return the persistent default workspace for new KINGCRAB missions."""

    return TB_WORK_ROOT
