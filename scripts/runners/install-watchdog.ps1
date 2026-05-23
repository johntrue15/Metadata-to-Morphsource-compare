#requires -Version 5.1
<#
.SYNOPSIS
  Install (or refresh) the MorphoClaw runner watchdog Scheduled Task.

.DESCRIPTION
  Creates a Windows Task Scheduler task that runs runner-watchdog.ps1
  every 5 minutes and at logon. The task runs as the current user with
  no elevation — sufficient because:

    * `wsl.exe` works for any user.
    * The systemd `systemctl start` inside WSL relies on passwordless
      sudo configured by setup-wsl-runner.sh (NOPASSWD entry for the
      runner user).

  Idempotent: if a task named MorphoClaw-RunnerWatchdog already exists,
  it is unregistered and recreated.

.PARAMETER TaskName
  Name of the Scheduled Task (default: MorphoClaw-RunnerWatchdog).

.PARAMETER IntervalMinutes
  How often to fire the watchdog (default: 5).

.NOTES
  Companion uninstaller: uninstall-watchdog.ps1.

  Status / log inspection: `runner-ctl.ps1 watchdog status`.
#>

[CmdletBinding()]
param(
    [string]$TaskName        = 'MorphoClaw-RunnerWatchdog',
    [int]   $IntervalMinutes = 5
)

$ErrorActionPreference = 'Stop'

$scriptDir  = Split-Path -Parent $PSCommandPath
$watchdog   = Join-Path $scriptDir 'runner-watchdog.ps1'
if (-not (Test-Path $watchdog)) {
    Write-Error "runner-watchdog.ps1 not found next to install-watchdog.ps1 (looked in $scriptDir)."
    exit 2
}

Write-Host "Installing scheduled task '$TaskName'" -ForegroundColor Cyan
Write-Host "  watchdog script:  $watchdog"
Write-Host "  interval:         every $IntervalMinutes min + at logon"

# --- remove any existing task with this name (idempotent reinstall) -------
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "  (existing task found; will be replaced)"
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

# --- action ---------------------------------------------------------------
# powershell.exe is used (not pwsh) to avoid requiring PowerShell 7+ on
# the host. The watchdog itself supports both.
$powershell = (Get-Command powershell.exe).Source
$action = New-ScheduledTaskAction `
    -Execute $powershell `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$watchdog`""

# --- triggers -------------------------------------------------------------
# Trigger 1: every IntervalMinutes, indefinitely. We start "Once" at the
# current time and add a repetition interval — there is no native "every
# N minutes forever" trigger in Task Scheduler.
$triggerInterval = New-ScheduledTaskTrigger -Once -At (Get-Date)
$triggerInterval.Repetition = (New-CimInstance `
    -ClassName MSFT_TaskRepetitionPattern `
    -Namespace Root/Microsoft/Windows/TaskScheduler `
    -ClientOnly `
    -Property @{
        Interval        = "PT${IntervalMinutes}M"
        Duration        = ""   # empty = indefinitely
        StopAtDurationEnd = $false
    })

# Trigger 2: at user logon (matches the user that installs the task).
$triggerLogon = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"

# --- principal / settings -------------------------------------------------
# Run as the current user, interactive (no password store). LIMITED priv
# level is fine — we don't need admin.
$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
    -MultipleInstances IgnoreNew

# --- register -------------------------------------------------------------
$task = New-ScheduledTask `
    -Action $action `
    -Trigger @($triggerInterval, $triggerLogon) `
    -Principal $principal `
    -Settings $settings `
    -Description "MorphoClaw self-hosted runner watchdog. Pokes the WSL distro and restarts the actions-runner systemd service if it's down. See scripts/runners/runner-watchdog.ps1."

Register-ScheduledTask -TaskName $TaskName -InputObject $task | Out-Null

# --- summary --------------------------------------------------------------
$registered = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
Write-Host ""
Write-Host "Installed:" -ForegroundColor Green
$registered | Format-List TaskName, State,
    @{n='NextRunTime'; e={ (Get-ScheduledTaskInfo $_).NextRunTime }},
    @{n='LastRunTime'; e={ (Get-ScheduledTaskInfo $_).LastRunTime }}

Write-Host "Log:       $env:LOCALAPPDATA\MorphoClaw\runner-watchdog.log"
Write-Host "Trigger it manually with:"
Write-Host "  Start-ScheduledTask -TaskName $TaskName"
Write-Host "Or from runner-ctl.ps1:"
Write-Host "  pwsh scripts\runners\runner-ctl.ps1 watchdog run-once"
