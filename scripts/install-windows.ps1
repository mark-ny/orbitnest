<#
OrbitNest Workspaces: Windows installer (Docker Desktop + WSL 2).

The control plane runs INSIDE a WSL 2 Linux distribution (the file manager needs a POSIX host), and
workspaces run in Docker Desktop's Linux containers. This script checks prerequisites, then runs
scripts/install-linux.sh inside WSL. It installs nothing system-wide and does not need Administrator.
It will not enable Windows features for you; it tells you what to do.

Usage (PowerShell):  .\scripts\install-windows.ps1 [-Distro Ubuntu]
#>
[CmdletBinding()]
param([string]$Distro = "")

$ErrorActionPreference = "Stop"

function Fail($msg) { Write-Host "error: $msg" -ForegroundColor Red; exit 1 }

if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
    Fail "WSL is not installed. In an elevated PowerShell run 'wsl --install', reboot, then re-run this script."
}

# 'wsl -l -q' prints UTF-16; normalise it.
$raw = & wsl.exe -l -q 2>$null
$distros = @($raw | ForEach-Object { ($_ -replace "`0", "").Trim() } | Where-Object { $_ -ne "" })
if ($distros.Count -eq 0) {
    Fail "No WSL distribution is installed. Run 'wsl --install -d Ubuntu', finish its first-run setup, then re-run."
}
if ($Distro -eq "") { $Distro = $distros[0] }
if ($distros -notcontains $Distro) { Fail "Distribution '$Distro' not found. Installed: $($distros -join ', ')" }

$verLine = (& wsl.exe -l -v 2>$null | ForEach-Object { ($_ -replace "`0", "") } | Where-Object { $_ -match [regex]::Escape($Distro) }) | Select-Object -First 1
if ($verLine -and $verLine -notmatch "\s2\s*$") {
    Fail "'$Distro' is WSL version 1. Convert it: wsl --set-version $Distro 2"
}

$dockerOk = $false
if (Get-Command docker.exe -ErrorAction SilentlyContinue) {
    & docker.exe info *> $null
    if ($LASTEXITCODE -eq 0) { $dockerOk = $true }
}
if (-not $dockerOk) {
    Write-Host "warning: Docker Desktop is not running or not installed. OrbitNest will install, but workspaces stay disabled until Docker works." -ForegroundColor Yellow
    Write-Host "         Install Docker Desktop, enable 'Use the WSL 2 based engine' and WSL integration for '$Distro'."
}

$repoWin = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$repoWsl = (& wsl.exe -d $Distro wslpath -a "$repoWin").Trim()
if (-not $repoWsl) { Fail "could not translate $repoWin into a WSL path" }

Write-Host "Installing inside WSL distribution '$Distro' ..."
# Files on /mnt/c are slow and lack Unix permissions; copy the sources into the WSL home first.
$script = @"
set -e
mkdir -p "`$HOME/orbitnest-src"
cp -a "$repoWsl/." "`$HOME/orbitnest-src/"
chmod +x "`$HOME/orbitnest-src/scripts/"*.sh
bash "`$HOME/orbitnest-src/scripts/install-linux.sh"
"@
& wsl.exe -d $Distro bash -lc $script
if ($LASTEXITCODE -ne 0) { Fail "installation inside WSL failed (see output above)." }

Write-Host ""
Write-Host "Done. Create an administrator and start the server from a WSL shell:"
Write-Host "  wsl -d $Distro"
Write-Host "  orbitnest create-admin <username>"
Write-Host "  orbitnest start"
Write-Host "Later, update from GitHub (only works if you installed from a git clone):  orbitnest update"
Write-Host "Then open http://127.0.0.1:8765 in your Windows browser (WSL 2 forwards localhost)."
