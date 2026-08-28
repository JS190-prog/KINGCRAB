from __future__ import annotations

from pathlib import Path

import pytest


@pytest.hookimpl(tryfirst=True)
def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Remove pytest's Windows ``*current`` links before its default cleanup.

    Pytest creates a best-effort symlink such as ``test_namecurrent`` for each
    numbered ``tmp_path`` directory. On some Windows systems, resolving that
    link during pytest's own session-finish cleanup raises ``WinError 448``
    after every test has already passed. Limit the workaround to direct child
    symlinks of pytest's active base temp directory; never remove real test
    directories or ordinary files.
    """
    del exitstatus  # The cleanup is safe and useful regardless of test status.
    tmp_path_factory = getattr(session.config, "_tmp_path_factory", None)
    base_temp = getattr(tmp_path_factory, "_basetemp", None)
    if base_temp is None:
        return

    try:
        entries = list(Path(base_temp).iterdir())
    except OSError:
        return

    for entry in entries:
        if not entry.name.endswith("current"):
            continue
        try:
            if entry.is_symlink():
                entry.unlink(missing_ok=True)
        except OSError:
            # The link is best-effort pytest metadata. A concurrent cleanup or
            # unsupported reparse point must not fail an otherwise valid run.
            continue
