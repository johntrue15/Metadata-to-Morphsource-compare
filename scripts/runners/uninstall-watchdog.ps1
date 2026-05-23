#requires -Version 5.1
<#
.SYNOPSIS
  Remove the MorphoClaw runner watchdog Scheduled Task.

.DESCRIPTION
  Companion to install-watchdog.ps1. Unregisters the task and leaves
  the log file alone (so you can still read the history of what it did
  before being uninstalled).

.PARAMETER TaskName
  Name of the Scheduled Task (default: MorphoClaw-RunnerWatchdog).

.PARAMETER PurgeLog
  Also delete %LOCALAPPDATA%\MorphoClaw\runner-watchdog.log and the
  rotated copy if present.
#>

[CmdletBinding()]
param(
    [string]$TaskName = 'MorphoClaw-RunnerWatchdog',
    [switch]$PurgeLog
)

$ErrorActionPreference = 'Stop'

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Host "No scheduled task named '$TaskName' is registered. Nothing to do." -ForegroundColor Yellow
} else {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Unregistered scheduled task '$TaskName'." -ForegroundColor Green
}

if ($PurgeLog) {
    $logDir = Join-Path $env:LOCALAPPDATA 'MorphoClaw'
    foreach ($f in @('runner-watchdog.log', 'runner-watchdog.log.1', 'runner-watchdog-state.json')) {
        $p = Join-Path $logDir $f
        if (Test-Path $p) {
            Remove-Item $p -Force
            Write-Host "  removed $p"
        }
    }
}
