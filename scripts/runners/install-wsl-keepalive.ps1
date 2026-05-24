#requires -Version 5.1
<#
.SYNOPSIS
  Install (or refresh) the MorphoClaw WSL keepalive Scheduled Task.

.DESCRIPTION
  Creates a Windows Task Scheduler task that periodically launches
  wsl-keepalive-launcher.vbs (every 1 minute and at logon). The launcher
  spawns a persistent `wsl.exe -d <distro> -- bash -c "exec sleep infinity"`
  process if one is not already running. That single attached user-space
  process is enough to stop WSL2 from idling the distro out, which in
  turn keeps the actions-runner systemd service continuously online
  instead of cycling offline every 1-2 minutes.

  The launcher is self-deduplicating, so firing it at a 1-minute cadence
  is safe and does not pile up processes.

.PARAMETER TaskName
  Name of the Scheduled Task (default: MorphoClaw-WSL-Keepalive).

.PARAMETER IntervalMinutes
  How often to fire the launcher (default: 1). A short interval bounds
  the worst-case offline window if the keepalive process ever dies.

.PARAMETER Distro
  Name of the WSL distro to keep warm (default: Ubuntu-24.04). Passed
  to the launcher as a positional arg.

.NOTES
  Companion uninstaller: uninstall-wsl-keepalive.ps1.

  Status / log inspection:
    Get-ScheduledTaskInfo -TaskName MorphoClaw-WSL-Keepalive
    Get-Content "$env:LOCALAPPDATA\MorphoClaw\wsl-keepalive.log" -Tail 20
#>

[CmdletBinding()]
param(
    [string]$TaskName        = 'MorphoClaw-WSL-Keepalive',
    [int]   $IntervalMinutes = 1,
    [string]$Distro          = 'Ubuntu-24.04'
)

$ErrorActionPreference = 'Stop'

$scriptDir = Split-Path -Parent $PSCommandPath
$launcher  = Join-Path $scriptDir 'wsl-keepalive-launcher.vbs'
if (-not (Test-Path $launcher)) {
    Write-Error "wsl-keepalive-launcher.vbs not found next to install-wsl-keepalive.ps1 (looked in $scriptDir)."
    exit 2
}

Write-Host "Installing scheduled task '$TaskName'" -ForegroundColor Cyan
Write-Host "  launcher (vbs):  $launcher"
Write-Host "  distro:          $Distro"
Write-Host "  interval:        every $IntervalMinutes min + at logon"

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "  (existing task found; will be replaced)"
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

# --- action ---------------------------------------------------------------
# We invoke the .vbs launcher via wscript.exe so there is no console flash.
$wscript = Join-Path $env:WINDIR 'System32\wscript.exe'
$action = New-ScheduledTaskAction `
    -Execute $wscript `
    -Argument "`"$launcher`" `"$Distro`""

# --- triggers -------------------------------------------------------------
$triggerInterval = New-ScheduledTaskTrigger -Once -At (Get-Date)
$triggerInterval.Repetition = (New-CimInstance `
    -ClassName MSFT_TaskRepetitionPattern `
    -Namespace Root/Microsoft/Windows/TaskScheduler `
    -ClientOnly `
    -Property @{
        Interval        = "PT${IntervalMinutes}M"
        Duration        = ""
        StopAtDurationEnd = $false
    })

$triggerLogon = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"

# --- principal / settings -------------------------------------------------
$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew

# --- register -------------------------------------------------------------
$task = New-ScheduledTask `
    -Action $action `
    -Trigger @($triggerInterval, $triggerLogon) `
    -Principal $principal `
    -Settings $settings `
    -Description "MorphoClaw WSL keepalive. Holds a persistent wsl.exe session open inside $Distro so the actions-runner systemd service does not cycle offline every 1-2 min due to WSL2 idle-shutdown. See scripts/runners/wsl-keepalive-launcher.vbs."

Register-ScheduledTask -TaskName $TaskName -InputObject $task | Out-Null

# --- summary --------------------------------------------------------------
$registered = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
Write-Host ""
Write-Host "Installed:" -ForegroundColor Green
$registered | Format-List TaskName, State,
    @{n='NextRunTime'; e={ (Get-ScheduledTaskInfo $_).NextRunTime }},
    @{n='LastRunTime'; e={ (Get-ScheduledTaskInfo $_).LastRunTime }}

Write-Host "Log:       $env:LOCALAPPDATA\MorphoClaw\wsl-keepalive.log"
Write-Host "Trigger it manually with:"
Write-Host "  Start-ScheduledTask -TaskName $TaskName"
