# Windows via WSL2

KINGCRAB does not currently provide a native Windows runtime. The supported
Windows path is WSL2: KINGCRAB runs inside Ubuntu and Windows Terminal displays
the TUI. This keeps the macOS/Linux runtime transport unchanged while avoiding
a second Windows-specific daemon implementation.

## Prerequisites

- Windows 10 version 2004 or later, or Windows 11
- Hardware virtualization enabled
- PowerShell with permission to install WSL
- An Ubuntu user created during the first Ubuntu launch

Microsoft's one-command setup is:

```powershell
wsl --install -d Ubuntu
```

Restart Windows when prompted. Launch Ubuntu once and finish its user setup
before running the KINGCRAB bootstrap.

## Install from a checkout

From PowerShell in a KINGCRAB checkout:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install-wsl2.ps1 -Launch
```

The script is safe to rerun. It does not pull over local changes or overwrite
an existing `~/KINGCRAB` directory that is not a Git checkout. It installs:

- `git`
- `python3`
- `python3-venv`
- `python3-pip`

The repository and virtual environment live inside the Linux filesystem at
`~/KINGCRAB`, which is preferable for SQLite, daemon sockets, and file-watch
performance. Windows drives remain available under `/mnt/c`, `/mnt/d`, and so
on when a mission needs to read a Windows folder.

## Start and connect services

After installation, the panel can be started from Ubuntu or PowerShell:

```bash
wsl -d Ubuntu -- bash -lc 'cd ~/KINGCRAB && . .venv/bin/activate && crab'
```

Install and authenticate the Codex CLI inside WSL2, then run:

```bash
codex login
crab setup status
```

OpenCrab is connected through KINGCRAB's normal onboarding flow. Its MCP
endpoint and local runtime state stay inside the WSL2 user's `.crabagent`
directory.

## Troubleshooting

Check the distribution and its WSL version:

```powershell
wsl --list --verbose
wsl --status
```

If Ubuntu is using version 1, convert it:

```powershell
wsl --set-version Ubuntu 2
```

Update WSL and restart its virtual machine when the runtime behaves strangely:

```powershell
wsl --update
wsl --shutdown
```

Native Windows execution remains a separate future adapter. Do not install the
Python package directly into Windows and expect `crabd` to work there yet.
