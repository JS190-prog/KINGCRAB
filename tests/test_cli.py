from pathlib import Path

from typer.testing import CliRunner

from crabagent import __version__
from crabagent.cli import app
from crabagent.protocol import start_daemon


runner = CliRunner()


def test_version_command() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert f"crab, version {__version__}" in result.stdout


def test_init_command(tmp_path: Path) -> None:
    result = runner.invoke(app, ["init", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert "Initialized CrabAgent" in result.stdout
    assert (tmp_path / ".crabagent" / "state.sqlite3").exists()


def test_crab_doc_import_command(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    manifest = tmp_path / "sample.crabdoc.json"
    manifest.write_text(
        '{"protocol":"crab-doc/v1","product":"CRAB DOC","createdAt":"2026-08-03T00:00:00Z",'
        '"sourceFileName":"sample.md","document":{"fileName":"sample.md","extension":".md",'
        '"kind":"text","title":"Sample notes","text":"Hello from CRAB DOC.","parser":"UTF-8 text",'
        '"capabilities":{"read":true,"edit":true,"export":true,"exportExtension":".md"}},'
        '"suggestedCommand":"crab doc import ./sample.md.crabdoc.json"}\n',
        encoding="utf-8",
    )
    result = runner.invoke(app, ["doc", "import", str(manifest), "--workspace", str(workspace)])
    assert result.exit_code == 0, result.stdout
    assert "CRAB DOC imported" in result.stdout
    client = start_daemon(workspace)
    try:
        listed = client.request("doc.list")
        assert listed["count"] == 1
        assert listed["documents"][0]["title"] == "Sample notes"
    finally:
        client.request("shutdown")
