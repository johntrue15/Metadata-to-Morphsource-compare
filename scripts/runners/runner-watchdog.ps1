#requires -Version 5.1
<#
.SYNOPSIS
  One-shot health check + auto-recovery for the MorphoClaw WSL runner.

.DESCRIPTION
  Designed to be invoked every ~5 minutes by Windows Task Scheduler
  (see install-watchdog.ps1). Each invocation:

    1. Verifies the WSL distro is running; if not, wakes it.
    2. Checks the actions-runner systemd service is `active`; if not,
       attempts `systemctl start`.
    3. Logs every action to %LOCALAPPDATA%\MorphoClaw\runner-watchdog.log
       (rotated at LogMaxBytes).

  Backoff: tracks recent restart attempts in a sidecar JSON file. If
  Threshold attempts in WindowSeconds have all failed, the watchdog
  stops trying for QuietSeconds and writes a "giving up, see issue"
  line — the GitHub-side liveness check in .github/workflows/runner-liveness.yml
  takes over from there.

  Exit code is always 0 unless the script itself can't run; we never
  want a stuck watchdog to spam Task Scheduler with failed history.

.PARAMETER WslDistro
  WSL distribution name. Defaults to env:MORPHOCLAW_DISTRO or Ubuntu-24.04.

.PARAMETER WslUser
  Linux user that owns the runner dir. Defaults to env:MORPHOCLAW_USER
  or `morphoclaw`.

.PARAMETER GhRepo
  GitHub repo (owner/name). Defaults to env:MORPHOCLAW_REPO or
  `johntrue15/MorphoClaw`. Used only to build the systemd service name.

.PARAMETER RunnerName
  Runner name as registered with GitHub. Defaults to env:MORPHOCLAW_RUNNER
  or `DellXPS-wsl-gpu`.

.NOTES
  Runs as the current user; no admin needed. The runner service inside
  WSL was installed by `install-runner-service.sh`, which used
  `./svc.sh install $USER` — so `sudo systemctl start ...` is the only
  privileged action, and the WSL user has passwordless sudo for it
  (configured by setup-wsl-runner.sh).
#>

[CmdletBinding()]
param(
    [string]$WslDistro  = $(if ($env:MORPHOCLAW_DISTRO)  { $env:MORPHOCLAW_DISTRO  } else { 'Ubuntu-24.04' }),
    [string]$WslUser    = $(if ($env:MORPHOCLAW_USER)    { $env:MORPHOCLAW_USER    } else { 'morphoclaw' }),
    [string]$GhRepo     = $(if ($env:MORPHOCLAW_REPO)    { $env:MORPHOCLAW_REPO    } else { 'johntrue15/MorphoClaw' }),
    [string]$RunnerName = $(if ($env:MORPHOCLAW_RUNNER)  { $env:MORPHOCLAW_RUNNER  } else { 'DellXPS-wsl-gpu' }),

    [int]$LogMaxBytes   = 100KB,
    [int]$Threshold     = 3,
    [int]$WindowSeconds = 1800,
    [int]$QuietSeconds  = 1800
)

$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
$StateDir   = Join-Path $env:LOCALAPPDATA 'MorphoClaw'
$LogPath    = Join-Path $StateDir 'runner-watchdog.log'
$StatePath  = Join-Path $StateDir 'runner-watchdog-state.json'
$ServiceName = "actions.runner.$($GhRepo -replace '/', '-').${RunnerName}.service"

if (-not (Test-Path $StateDir)) {
    New-Item -ItemType Directory -Path $StateDir -Force | Out-Null
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
function Write-Log {
    param([string]$Level, [string]$Message)
    $line = "{0} [{1}] {2}" -f (Get-Date -Format 'yyyy-MM-ddTHH:mm:ss'), $Level, $Message
    Add-Content -Path $LogPath -Value $line -Encoding utf8
    # Stream to stdout too (Task Scheduler captures stdout when run interactively).
    Write-Host $line
}

function Rotate-Log {
    if (-not (Test-Path $LogPath)) { return }
    $size = (Get-Item $LogPath).Length
    if ($size -le $LogMaxBytes) { return }
    $archive = "$LogPath.1"
    if (Test-Path $archive) { Remove-Item $archive -Force }
    Move-Item $LogPath $archive
    Write-Log 'INFO' "log rotated (>$($LogMaxBytes) bytes); previous saved to $archive"
}

function Load-State {
    if (-not (Test-Path $StatePath)) {
        return [pscustomobject]@{ attempts = @(); quiet_until = $null }
    }
    try {
        $raw = Get-Content $StatePath -Raw -Encoding utf8
        return $raw | ConvertFrom-Json
    } catch {
        Write-Log 'WARN' "could not parse $StatePath ($_); resetting state"
        return [pscustomobject]@{ attempts = @(); quiet_until = $null }
    }
}

function Save-State {
    param($State)
    ($State | ConvertTo-Json -Depth 5) | Set-Content -Path $StatePath -Encoding utf8
}

function In-QuietPeriod {
    param($State)
    if (-not $State.quiet_until) { return $false }
    try { $until = [datetime]::Parse($State.quiet_until) } catch { return $false }
    return ((Get-Date) -lt $until)
}

function Record-Attempt {
    param($State, [bool]$Success)
    $nowIso = (Get-Date).ToString('o')
    $entries = @()
    foreach ($e in @($State.attempts)) {
        try { $t = [datetime]::Parse($e.ts) } catch { continue }
        if (((Get-Date) - $t).TotalSeconds -lt $WindowSeconds) {
            $entries += $e
        }
    }
    $entries += [pscustomobject]@{ ts = $nowIso; success = $Success }
    $State.attempts = $entries

    if (-not $Success) {
        $recentFailures = @($entries | Where-Object { -not $_.success }).Count
        if ($recentFailures -ge $Threshold) {
            $quietUntil = (Get-Date).AddSeconds($QuietSeconds)
            $State.quiet_until = $quietUntil.ToString('o')
            Write-Log 'WARN' "Threshold of $Threshold failed attempts in $WindowSeconds s reached; backing off until $($quietUntil.ToString('yyyy-MM-ddTHH:mm:ss')). The GitHub-side liveness check will alert if the runner stays offline."
        }
    } else {
        # Success clears the quiet window.
        $State.quiet_until = $null
    }
    return $State
}

function Invoke-Wsl {
    param([string]$Bash, [switch]$AsRoot)
    # We don't use file-based exec here (unlike runner-ctl.ps1) because the
    # commands are short and the Task Scheduler logon-session may not have
    # access to %TEMP%. Pipe the script into bash -s instead.
    $bytes  = [System.Text.Encoding]::UTF8.GetBytes($Bash)
    $tmp    = [System.IO.Path]::GetTempFileName()
    try {
        [System.IO.File]::WriteAllBytes($tmp, $bytes)
        $args = @('-d', $WslDistro, '-u', $WslUser, '--', 'bash', '-lc',
                  "cat /mnt/$($tmp.Substring(0,1).ToLower())$(($tmp.Substring(2) -replace '\\','/')) | bash")
        $out = & wsl.exe @args 2>&1
        return @{ ExitCode = $LASTEXITCODE; Output = ($out -join "`n") }
    } finally {
        if (Test-Path $tmp) { Remove-Item $tmp -Force -ErrorAction SilentlyContinue }
    }
}

function Test-WslDistroRunning {
    # `wsl --list --running` output is UTF-16 LE — PowerShell handles it fine
    # but we still trim NULs that occasionally leak through wsl.exe.
    $listing = (& wsl.exe --list --running 2>$null | Out-String).Replace("`0", '')
    return $listing -match [regex]::Escape($WslDistro)
}

function Test-RunnerActive {
    $r = Invoke-Wsl -Bash "systemctl is-active --quiet '$ServiceName' && echo active || echo inactive"
    if ($r.ExitCode -ne 0) {
        # Treat hard failure (wsl.exe error) as inactive so we attempt recovery.
        Write-Log 'WARN' "is-active probe failed (exit $($r.ExitCode)): $($r.Output)"
        return $false
    }
    return ($r.Output.Trim() -eq 'active')
}

function Start-RunnerService {
    Write-Log 'INFO' "attempting: sudo systemctl start $ServiceName"
    $r = Invoke-Wsl -Bash "sudo -n systemctl start '$ServiceName' 2>&1 || echo FAIL_SUDO"
    if ($r.Output -match 'FAIL_SUDO|sudo:') {
        Write-Log 'ERROR' "passwordless sudo not configured for systemctl start. Output: $($r.Output)"
        return $false
    }
    Start-Sleep -Seconds 5
    return (Test-RunnerActive)
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
try {
    Rotate-Log
    $state = Load-State

    if (In-QuietPeriod -State $state) {
        $remaining = ([datetime]::Parse($state.quiet_until) - (Get-Date)).TotalMinutes
        Write-Log 'INFO' ("backoff window active, {0:N0} min remaining; skipping run" -f $remaining)
        exit 0
    }

    # ---- WSL distro alive? ----
    if (-not (Test-WslDistroRunning)) {
        Write-Log 'INFO' "WSL distro '$WslDistro' not running; waking it"
        # `wsl -- true` keeps the distro alive for the duration of the systemd
        # boot. We follow up with a real `is-active` probe below.
        & wsl.exe -d $WslDistro -- true 2>&1 | Out-Null
        Start-Sleep -Seconds 4
        if (-not (Test-WslDistroRunning)) {
            Write-Log 'ERROR' "wake attempt failed; distro still not running"
            $state = Record-Attempt -State $state -Success:$false
            Save-State -State $state
            exit 0
        }
        Write-Log 'INFO' "WSL distro is up"
    }

    # ---- Service active? ----
    if (Test-RunnerActive) {
        Write-Log 'INFO' "service $ServiceName active; no action needed"
        $state = Record-Attempt -State $state -Success:$true
        Save-State -State $state
        exit 0
    }

    Write-Log 'WARN' "service $ServiceName not active; attempting restart"
    $ok = Start-RunnerService

    if ($ok) {
        Write-Log 'INFO' "service is now active after restart"
        $state = Record-Attempt -State $state -Success:$true
    } else {
        Write-Log 'ERROR' "service still not active after restart attempt"
        # Capture last journal lines for diagnosis.
        $j = Invoke-Wsl -Bash "journalctl -u '$ServiceName' --no-pager -n 15 2>/dev/null || true"
        if ($j.Output) {
            Write-Log 'ERROR' "recent journal:`n$($j.Output)"
        }
        $state = Record-Attempt -State $state -Success:$false
    }
    Save-State -State $state
    exit 0
} catch {
    # Last resort: log and swallow so Task Scheduler doesn't flag the run
    # as failed (which would clutter its history).
    try {
        Write-Log 'ERROR' "uncaught exception: $($_.Exception.Message)"
    } catch { }
    exit 0
}
