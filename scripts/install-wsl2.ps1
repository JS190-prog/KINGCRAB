#Requires -Version 5.1

[CmdletBinding()]
param(
    [ValidatePattern('^[A-Za-z0-9._-]+$')]
    [string]$Distribution = "Ubuntu",
    [switch]$Launch
)

$ErrorActionPreference = "Stop"

function Invoke-WslCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Command
    )

    & wsl.exe -d $Distribution -- bash -lc $Command
    if ($LASTEXITCODE -ne 0) {
        throw "WSL command failed with exit code $LASTEXITCODE."
    }
}

if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
    throw "wsl.exe was not found. Open an elevated PowerShell and run: wsl --install -d Ubuntu"
}

$installedDistros = @(
    & wsl.exe --list --quiet 2>$null |
        ForEach-Object { ($_ -replace "`0", "").Trim() } |
        Where-Object { $_ }
)

if ($installedDistros -notcontains $Distribution) {
    Write-Host "Installing WSL2 distribution '$Distribution'..." -ForegroundColor Cyan
    & wsl.exe --install -d $Distribution
    if ($LASTEXITCODE -ne 0) {
        throw "WSL distribution installation failed with exit code $LASTEXITCODE."
    }
    Write-Host "Finish the first '$Distribution' user setup, then run this script again." -ForegroundColor Yellow
    exit 0
}

Write-Host "Ensuring '$Distribution' uses WSL2..." -ForegroundColor Cyan
& wsl.exe --set-version $Distribution 2
if ($LASTEXITCODE -ne 0) {
    throw "Could not set '$Distribution' to WSL2. Check: wsl --list --verbose"
}

$setup = @'
set -eu
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y git python3 python3-venv python3-pip

if [ -e "$HOME/KINGCRAB" ] && [ ! -d "$HOME/KINGCRAB/.git" ]; then
  printf '%s\n' "Refusing to overwrite existing $HOME/KINGCRAB; move it and rerun." >&2
  exit 2
fi

if [ ! -d "$HOME/KINGCRAB/.git" ]; then
  git clone "https://github.com/AlexAI-MCP/KINGCRAB.git" "$HOME/KINGCRAB"
fi

cd "$HOME/KINGCRAB"
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
crab --version
'@
$setup = $setup -replace "`r`n", "`n"

Write-Host "Installing KINGCRAB inside WSL2 at ~/KINGCRAB..." -ForegroundColor Cyan
Invoke-WslCommand $setup

if ($Launch) {
    Invoke-WslCommand 'cd "$HOME/KINGCRAB" && . .venv/bin/activate && exec crab'
} else {
    Write-Host "Installed. Start KINGCRAB with:" -ForegroundColor Green
    Write-Host "  wsl -d $Distribution -- bash -lc 'cd ~/KINGCRAB && . .venv/bin/activate && crab'"
}
