#requires -Version 5.1
<#
.SYNOPSIS
    Prepare a Windows host to run the MorphoClaw self-hosted GitHub Actions
    runner inside WSL2 with NVIDIA CUDA passthrough.

.DESCRIPTION
    Idempotent. Safe to re-run. Does the Windows side only:
      1. Verifies you have an NVIDIA GPU + driver visible to the host.
      2. Enables WSL + Virtual Machine Platform features (if missing).
      3. Installs WSL2 and Ubuntu 24.04 (if missing). A reboot may be required
         the first time -- the script will tell you and exit.
      4. Sets WSL default version to 2.
      5. Prints next steps for the in-WSL bootstrap.

    This script does NOT register the actions runner. After WSL + Ubuntu are
    ready, finish setup from inside Ubuntu with:

        bash /mnt/c/Users/<you>/.../MorphoClaw/scripts/runners/setup-wsl-runner.sh

.PARAMETER Distro
    WSL distribution to install. Default: Ubuntu-24.04.

.PARAMETER SkipWslInstall
    Skip the wsl --install step (useful if WSL is already configured but the
    feature-detection heuristics misfire).

.EXAMPLE
    # In an elevated PowerShell prompt:
    powershell -ExecutionPolicy Bypass -File scripts\runners\setup-windows-host.ps1
#>

[CmdletBinding()]
param(
    [string] $Distro = 'Ubuntu-24.04',
    [switch] $SkipWslInstall
)

$ErrorActionPreference = 'Stop'

function Write-Step  { param([string]$Msg) Write-Host "==> $Msg" -ForegroundColor Cyan }
function Write-Ok    { param([string]$Msg) Write-Host "    [ok] $Msg" -ForegroundColor Green }
function Write-Warn2 { param([string]$Msg) Write-Host "    [warn] $Msg" -ForegroundColor Yellow }
function Write-Err   { param([string]$Msg) Write-Host "    [err] $Msg" -ForegroundColor Red }

function Test-Admin {
    $id = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    $p  = New-Object System.Security.Principal.WindowsPrincipal($id)
    return $p.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-Admin)) {
    Write-Err "This script must be run from an elevated (Administrator) PowerShell."
    Write-Host "Right-click PowerShell and choose 'Run as administrator', then re-run:" -ForegroundColor Yellow
    Write-Host "  powershell -ExecutionPolicy Bypass -File `"$PSCommandPath`"" -ForegroundColor Yellow
    exit 1
}

Write-Step "Checking NVIDIA GPU + driver"
$nvidiaSmi = (Get-Command nvidia-smi -ErrorAction SilentlyContinue)
if (-not $nvidiaSmi) {
    Write-Err "nvidia-smi was not found on PATH. Install the NVIDIA Windows driver from"
    Write-Host "  https://www.nvidia.com/Download/index.aspx" -ForegroundColor Yellow
    Write-Host "(the regular GeForce / Studio driver -- NOT the WSL-specific package; the" -ForegroundColor Yellow
    Write-Host "Windows driver provides CUDA passthrough into WSL automatically since 2022.)" -ForegroundColor Yellow
    exit 1
}
$smiOut = & nvidia-smi 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Err "nvidia-smi failed: $smiOut"
    exit 1
}
$gpuLine = ($smiOut | Select-String -Pattern 'NVIDIA' | Select-Object -First 1).ToString().Trim()
$drvLine = ($smiOut | Select-String -Pattern 'Driver Version' | Select-Object -First 1).ToString().Trim()
Write-Ok  "GPU:    $gpuLine"
Write-Ok  "Driver: $drvLine"

Write-Step "Checking required Windows features (VirtualMachinePlatform, Microsoft-Windows-Subsystem-Linux)"
$features = @(
    'VirtualMachinePlatform',
    'Microsoft-Windows-Subsystem-Linux'
)
$rebootNeeded = $false
foreach ($f in $features) {
    $state = (Get-WindowsOptionalFeature -Online -FeatureName $f -ErrorAction SilentlyContinue).State
    if ($state -eq 'Enabled') {
        Write-Ok "$f already enabled"
    } else {
        Write-Warn2 "$f is $state -- enabling now"
        $r = Enable-WindowsOptionalFeature -Online -FeatureName $f -NoRestart -ErrorAction Stop
        if ($r.RestartNeeded) { $rebootNeeded = $true }
    }
}

if ($rebootNeeded) {
    Write-Warn2 "Windows reports a reboot is required to finish enabling WSL features."
    Write-Warn2 "Reboot, then re-run this script."
    exit 0
}

Write-Step "Ensuring WSL2 is installed and set as default"
$wslCmd = (Get-Command wsl.exe -ErrorAction SilentlyContinue)
if (-not $wslCmd) {
    if ($SkipWslInstall) {
        Write-Err "wsl.exe not found and -SkipWslInstall was supplied. Aborting."
        exit 1
    }
    Write-Warn2 "wsl.exe not found -- installing WSL via 'wsl --install --no-distribution'"
    # --no-distribution avoids the default Ubuntu install on Win10/11 builds that ship it;
    # we install the pinned distro explicitly below.
    & wsl --install --no-distribution
    Write-Warn2 "If a reboot is requested above, reboot and re-run this script."
    if ($LASTEXITCODE -ne 0) {
        Write-Err "'wsl --install' failed with exit code $LASTEXITCODE"
        exit 1
    }
}

# Set WSL2 as the default version. Harmless on systems where it already is.
& wsl --set-default-version 2 2>$null | Out-Null

Write-Step "Listing installed WSL distributions"
$wslList = & wsl --list --quiet 2>$null
$installed = @()
if ($LASTEXITCODE -eq 0 -and $wslList) {
    # `wsl --list --quiet` emits UTF-16 -- normalise to ASCII for matching
    $installed = $wslList | ForEach-Object { ($_ -replace "`0", '').Trim() } | Where-Object { $_ }
}
if ($installed) {
    foreach ($d in $installed) { Write-Ok "Found distro: $d" }
} else {
    Write-Ok "No WSL distros installed yet"
}

$haveDistro = $installed -contains $Distro
if (-not $haveDistro) {
    Write-Step "Installing WSL distro: $Distro"
    & wsl --install -d $Distro
    if ($LASTEXITCODE -ne 0) {
        Write-Err "'wsl --install -d $Distro' failed with exit code $LASTEXITCODE"
        Write-Host "Try: wsl --list --online   to see currently available names" -ForegroundColor Yellow
        exit 1
    }
    Write-Warn2 "Ubuntu setup will open in a new console window. Finish creating your"
    Write-Warn2 "UNIX username + password there, then return here and re-run this script"
    Write-Warn2 "to verify the install and print the next-step command."
    exit 0
} else {
    Write-Ok "$Distro already installed"
}

Write-Step "Sanity-checking CUDA passthrough inside WSL"
$inWslNvidiaSmi = & wsl -d $Distro --exec bash -lc 'command -v nvidia-smi && nvidia-smi -L 2>&1 || true'
if ([string]::IsNullOrWhiteSpace($inWslNvidiaSmi) -or $inWslNvidiaSmi -notmatch 'GPU') {
    Write-Warn2 "Could not see nvidia-smi inside $Distro yet."
    Write-Warn2 "This usually means either:"
    Write-Warn2 "  * the Windows NVIDIA driver is older than r495 (Nov 2021) -- update it, OR"
    Write-Warn2 "  * the distro was created before the driver and needs:  wsl --shutdown"
    Write-Warn2 "Run 'wsl --shutdown' from PowerShell, then re-launch the distro."
} else {
    Write-Ok "CUDA passthrough OK inside ${Distro}:"
    Write-Host ($inWslNvidiaSmi -split "`n" | ForEach-Object { "    $_" }) -ForegroundColor DarkGray
}

# Figure out a clone path the WSL bootstrap can use directly. We resolve the repo
# path on Windows and translate it to /mnt/c/...
$repoRootWin = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path.TrimEnd('\')
# Convert "C:\Users\foo\..." -> "/mnt/c/Users/foo/..."
$repoRootWsl = $repoRootWin -replace '^([A-Za-z]):', { '/mnt/' + $matches[1].ToLower() } -replace '\\', '/'

Write-Host ''
Write-Host '================================================================' -ForegroundColor Green
Write-Host ' Windows host is ready.' -ForegroundColor Green
Write-Host '================================================================' -ForegroundColor Green
Write-Host ''
Write-Host 'Next step: open the Ubuntu shell and run the WSL bootstrap.' -ForegroundColor Cyan
Write-Host ''
Write-Host "  wsl -d $Distro" -ForegroundColor Yellow
Write-Host "  bash `"$repoRootWsl/scripts/runners/setup-wsl-runner.sh`"" -ForegroundColor Yellow
Write-Host ''
Write-Host 'The WSL script will install dependencies, the GitHub Actions runner,' -ForegroundColor Cyan
Write-Host 'nnInteractive, 3D Slicer + SlicerMorph, and prompt you for a' -ForegroundColor Cyan
Write-Host 'one-time repo runner registration token.' -ForegroundColor Cyan
Write-Host ''
