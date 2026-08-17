from pathlib import Path

from crabagent.workspace_defaults import TB_WORK_ROOT, default_workspace, is_legacy_tb_scratch_root


def test_tb_work_root_is_stable_and_not_the_old_benchmark_scratch_path() -> None:
    expected = Path.home() / "Downloads" / "opencrab_packs"

    assert TB_WORK_ROOT == expected
    assert default_workspace() == expected
    assert expected.name == "opencrab_packs"
    assert is_legacy_tb_scratch_root(r"C:\scratch\FINAL-Bench-TB-S1")
    assert not is_legacy_tb_scratch_root(expected)
