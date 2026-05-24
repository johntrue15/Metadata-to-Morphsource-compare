#requires -Version 5.1
<#
.SYNOPSIS
  Uninstall the MorphoClaw WSL keepalive Scheduled Task and kill any
  attached wsl.exe sleep-infinity holder process spawned by it.

.DESCRIPTION
  Removes the MorphoClaw-WSL-Keepalive task created by
  install-wsl-keepalive.ps1 and (best-effort) terminates any wsl.exe
  process whose commandline contains the `exec sleep infinity` sentinel
  used by wsl-keepalive-launcher.vbs.

  WARNING: terminating the keepalive process will let WSL2 idle the
  distro out on its next idle interval, which will cause the
  actions-runner systemd service to look intermittently offline on
  GitHub. Only uninstall the keepalive if you also intend to manage WSL
  longevity by some other means.

.PARAMETER TaskName
  Name of the Scheduled Task (default: MorphoClaw-WSL-Keepalive).
#>

[CmdletBinding()]
param(
    [string]$TaskName = 'MorphoClaw-WSL-Keepalive'
)

$ErrorActionPreference = 'Stop'

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Removing scheduled task '$TaskName'..."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "  removed."
} else {
    Write-Host "No scheduled task named '$TaskName' found."
}

Write-Host "Looking for stray keepalive wsl.exe processes..."
$sentinel = 'exec sleep infinity'
$killed = 0
Get-CimInstance Win32_Process -Filter "Name='wsl.exe'" -ErrorAction SilentlyContinue | ForEach-Object {
    if ($_.CommandLine -and $_.CommandLine.ToLower().Contains($sentinel.ToLower())) {
        Write-Host ("  killing PID {0}: {1}" -f $_.ProcessId, $_.CommandLine)
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        $killed++
    }
}
Write-Host "  $killed keepalive process(es) terminated."
