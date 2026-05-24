#requires -Version 5.1
<#
.SYNOPSIS
  Cross-shell controller for the Dell XPS WSL GPU runner + workflow ops.

.DESCRIPTION
  One entrypoint for everything you commonly do with the self-hosted runner:

    runner-ctl.ps1 status            # GitHub runner state + local process state
    runner-ctl.ps1 start             # Launch ./run.sh inside WSL as a daemon
    runner-ctl.ps1 stop              # Gracefully stop the runner
    runner-ctl.ps1 restart           # stop then start
    runner-ctl.ps1 log [-n 50]       # Tail ~/runner.log inside WSL
    runner-ctl.ps1 dispatch <wf>     # Dispatch a workflow_dispatch workflow
                                     #   -f key=val (repeatable) to pass inputs
                                     #   -Ref <branch> (default: main)
    runner-ctl.ps1 runs [-Workflow runner-smoke.yml] [-Limit 10]
                                     # List recent runs (optionally filtered)
    runner-ctl.ps1 tail <run-id>     # Stream live logs of a run via `gh run watch`
    runner-ctl.ps1 cancel <run-id>   # Cancel an in-progress run
    runner-ctl.ps1 token             # Fetch a fresh runner registration token

  Requires: gh (authenticated), wsl.exe with the `Ubuntu-24.04` distro and
  user `morphoclaw` (set up by scripts/runners/setup-wsl-runner.sh).

.NOTES
  Repo and runner identity are configurable at the top of the file.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet('status', 'start', 'stop', 'restart', 'log', 'dispatch',
                 'runs', 'tail', 'cancel', 'token', 'watchdog', 'keepalive', 'help')]
    [string]$Command,

    [Parameter(Position = 1, ValueFromRemainingArguments = $true)]
    [string[]]$Rest = @()
)

# --------- configuration (override via env vars) ---------------------------
$Repo       = $env:MORPHOCLAW_REPO   ; if (-not $Repo)   { $Repo   = 'johntrue15/MorphoClaw' }
$WslDistro  = $env:MORPHOCLAW_DISTRO ; if (-not $WslDistro) { $WslDistro = 'Ubuntu-24.04' }
$WslUser    = $env:MORPHOCLAW_USER   ; if (-not $WslUser) { $WslUser = 'morphoclaw' }
$RunnerName = $env:MORPHOCLAW_RUNNER ; if (-not $RunnerName) { $RunnerName = 'DellXPS-wsl-gpu' }
$RunnerDir  = "~/actions-runner-morphoclaw"
$LogPath    = "~/runner.log"

# --------- helpers ---------------------------------------------------------
function Invoke-Wsl {
    param([string]$Bash)
    # Use file-based execution to dodge PowerShell <-> bash quoting hell.
    $tmpSh = $null
    try {
        $tmp = New-TemporaryFile
        $tmpSh = "$($tmp.FullName).sh"
        Rename-Item $tmp $tmpSh
        $body = "#!/usr/bin/env bash`nset -e`n" + $Bash
        # Force LF endings (bash chokes on CRLF shebangs). Parens are required so
        # PowerShell does not mis-parse the comma as a method-call separator.
        $lf = ($body -replace "`r`n", "`n")
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($lf)
        [System.IO.File]::WriteAllBytes($tmpSh, $bytes)
        # Convert Windows path -> WSL path locally (avoids backslash escaping
        # when wsl.exe relays arguments to the Linux side).
        $drive   = $tmpSh.Substring(0, 1).ToLower()
        $rest    = $tmpSh.Substring(2) -replace '\\', '/'
        $wslPath = "/mnt/$drive$rest"
        # Stream stdout/stderr straight to console; no PowerShell pipeline
        # capture (so callers don't have to deal with the function return
        # value mixing into the visible output).
        & wsl.exe -d $WslDistro -u $WslUser -- bash $wslPath | Write-Host
    } finally {
        if ($tmpSh -and (Test-Path $tmpSh)) { Remove-Item $tmpSh -Force -ErrorAction SilentlyContinue }
    }
}

function Require-Gh {
    if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
        Write-Error "gh CLI not found on PATH. Install from https://cli.github.com/ or run 'winget install GitHub.cli'."
        exit 2
    }
    $authState = & gh auth status 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Error "gh not authenticated. Run: gh auth login --web --scopes repo"
        exit 2
    }
}

function Show-Help {
@"
runner-ctl.ps1 - MorphoClaw self-hosted runner controller

Usage:
  runner-ctl.ps1 <command> [args...]

Commands:
  status                       Show runner status (GitHub + local process)
  start                        Start the actions-runner daemon inside WSL
  stop                         Stop the actions-runner daemon
  restart                      stop then start
  log [-n LINES]               Tail ~/runner.log inside WSL (default 50)
  dispatch <workflow> [opts]   Dispatch a workflow. Opts:
                                 -Ref <branch>        (default: main)
                                 -f <key>=<value>     (repeatable)
                                 -Wait                Block + tail the new run
  runs [-Workflow NAME] [-Limit N]
                               List recent runs (default last 10)
  tail <run-id>                Stream live logs of a run
  cancel <run-id>              Cancel an in-progress run
  token                        Print a fresh registration token
  watchdog <subcommand>        Manage the Task Scheduler watchdog:
                                 install    register MorphoClaw-RunnerWatchdog
                                 uninstall  remove the scheduled task
                                 status     show task state + last 20 log lines
                                 run-once   trigger the watchdog right now
  keepalive <subcommand>       Manage the WSL keepalive (stops WSL idle-shutdown):
                                 install    register MorphoClaw-WSL-Keepalive
                                 uninstall  remove the task + kill keepalive procs
                                 status     show task state + running keepalive procs
                                 run-once   trigger the keepalive launcher right now

Environment overrides:
  MORPHOCLAW_REPO    (default: johntrue15/MorphoClaw)
  MORPHOCLAW_DISTRO  (default: Ubuntu-24.04)
  MORPHOCLAW_USER    (default: morphoclaw)
  MORPHOCLAW_RUNNER  (default: DellXPS-wsl-gpu)

Examples:
  pwsh runner-ctl.ps1 start
  pwsh runner-ctl.ps1 dispatch runner-smoke.yml -f skip_slicer=true -Wait
  pwsh runner-ctl.ps1 tail 26265314074
  pwsh runner-ctl.ps1 runs -Workflow runner-smoke.yml -Limit 5
"@
}

# --------- command dispatch ------------------------------------------------
switch ($Command) {

    'help' { Show-Help; exit 0 }

    'status' {
        Require-Gh
        Write-Host "== GitHub view of '$RunnerName' ==" -ForegroundColor Cyan
        # Filter in PowerShell rather than via --jq to dodge cross-shell
        # quote escaping (PowerShell strips quotes from jq string literals
        # like "DellXPS-wsl-gpu" â†’ jq then parses it as `gpu/0`).
        $runners = (& gh api "repos/$Repo/actions/runners") | ConvertFrom-Json
        $match   = $runners.runners | Where-Object { $_.name -eq $RunnerName }
        if (-not $match) {
            Write-Host "  (not registered)" -ForegroundColor Yellow
        } else {
            $match | Format-List name, status, busy, os,
                @{n='labels'; e={ ($_.labels | ForEach-Object { $_.name }) -join ', ' }}
        }
        Write-Host "== Local process view ==" -ForegroundColor Cyan
        Invoke-Wsl @"
SVC='actions.runner.johntrue15-MorphoClaw.DellXPS-wsl-gpu.service'
if systemctl is-active --quiet "`$SVC" 2>/dev/null; then
    echo "state: RUNNING (systemd: \`$SVC)"
    systemctl --no-pager status "`$SVC" 2>/dev/null | sed -n '1,3p;/Main PID/p;/Active:/p'
    echo
    echo '-- last 8 journal entries --'
    journalctl -u "`$SVC" --no-pager -n 8 2>/dev/null | tail -8
elif pgrep -af 'Runner.Listener' >/dev/null; then
    echo 'state: RUNNING (foreground ./run.sh)'
    pgrep -af 'Runner.Listener|run.sh|run-helper' | head -5
    echo
    echo '-- last 10 lines of runner.log --'
    tail -10 $LogPath 2>/dev/null || echo '(no log)'
else
    echo 'state: STOPPED'
fi
"@
    }

    'start' {
        Invoke-Wsl @"
SVC='actions.runner.johntrue15-MorphoClaw.DellXPS-wsl-gpu.service'
if systemctl list-unit-files --no-pager | grep -q "`$SVC"; then
    sudo systemctl start "`$SVC"
    sleep 2
    systemctl is-active --quiet "`$SVC" && echo "Started (systemd: `$SVC)" || { echo 'FAILED, journal:'; journalctl -u "`$SVC" --no-pager -n 20; exit 1; }
else
    cd $RunnerDir
    if pgrep -af 'Runner.Listener' >/dev/null; then
        echo 'Already running:'
        pgrep -af 'Runner.Listener' | head -3
        exit 0
    fi
    setsid nohup ./run.sh > $LogPath 2>&1 < /dev/null &
    disown
    sleep 3
    pgrep -af 'Runner.Listener' | head -3 || { echo 'FAILED to start; see runner.log'; tail -30 $LogPath; exit 1; }
    echo 'Started (foreground).'
fi
"@
    }

    'stop' {
        Invoke-Wsl @"
SVC='actions.runner.johntrue15-MorphoClaw.DellXPS-wsl-gpu.service'
if systemctl list-unit-files --no-pager | grep -q "`$SVC"; then
    sudo systemctl stop "`$SVC"
    sleep 1
    if systemctl is-active --quiet "`$SVC"; then
        echo 'Still active!'
        exit 1
    fi
    echo "Stopped (systemd: `$SVC)"
else
    pids=`$(pgrep -f 'Runner.Listener|run.sh|run-helper' || true)
    if [ -z "`$pids" ]; then
        echo 'Not running.'
        exit 0
    fi
    echo "Stopping: `$pids"
    kill `$pids 2>/dev/null || true
    sleep 2
    for p in `$pids; do
        if kill -0 `$p 2>/dev/null; then kill -9 `$p 2>/dev/null || true; fi
    done
    sleep 1
    if pgrep -af 'Runner.Listener' >/dev/null; then echo 'still alive!'; exit 1; fi
    echo 'Stopped.'
fi
"@
    }

    'restart' {
        & $PSCommandPath stop
        Start-Sleep -Seconds 1
        & $PSCommandPath start
    }

    'log' {
        $n = 50
        for ($i = 0; $i -lt $Rest.Length; $i++) {
            if ($Rest[$i] -eq '-n' -and ($i + 1) -lt $Rest.Length) {
                $n = [int]$Rest[$i + 1]
                $i++
            }
        }
        Invoke-Wsl @"
SVC='actions.runner.johntrue15-MorphoClaw.DellXPS-wsl-gpu.service'
if systemctl list-unit-files --no-pager | grep -q "`$SVC"; then
    journalctl -u "`$SVC" --no-pager -n $n
else
    tail -n $n $LogPath
fi
"@
    }

    'dispatch' {
        Require-Gh
        if ($Rest.Length -eq 0) {
            Write-Error "Usage: runner-ctl.ps1 dispatch <workflow.yml> [-Ref main] [-f key=value ...] [-Wait]"
            exit 2
        }
        $workflow = $Rest[0]
        $ref      = 'main'
        $wait     = $false
        $inputs   = @()
        $i = 1
        while ($i -lt $Rest.Length) {
            switch -Regex ($Rest[$i]) {
                '^-(Ref|ref)$'  { $ref     = $Rest[$i + 1]; $i += 2 }
                '^-f$'          { $inputs += @('-f', $Rest[$i + 1]); $i += 2 }
                '^-(Wait|wait)$'{ $wait    = $true;        $i += 1 }
                default         { Write-Warning "Unknown arg: $($Rest[$i])"; $i += 1 }
            }
        }
        Write-Host "Dispatching $workflow on ref $ref ..." -ForegroundColor Cyan
        & gh workflow run $workflow -R $Repo --ref $ref @inputs
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        Start-Sleep -Seconds 4
        $latest = & gh run list -R $Repo --workflow=$workflow --limit 1 --json databaseId,status,createdAt,headBranch,displayTitle |
                  ConvertFrom-Json
        if ($latest) {
            Write-Host "  -> run id: $($latest.databaseId)  status: $($latest.status)  branch: $($latest.headBranch)" -ForegroundColor Green
            if ($wait) {
                Write-Host "  watching run $($latest.databaseId) ..." -ForegroundColor Cyan
                & gh run watch $latest.databaseId -R $Repo --exit-status
                exit $LASTEXITCODE
            }
        }
    }

    'runs' {
        Require-Gh
        $workflow = $null
        $limit    = 10
        for ($i = 0; $i -lt $Rest.Length; $i++) {
            if ($Rest[$i] -in @('-Workflow', '-workflow', '-wf') -and ($i + 1) -lt $Rest.Length) {
                $workflow = $Rest[$i + 1]; $i++
            } elseif ($Rest[$i] -in @('-Limit', '-limit', '-l') -and ($i + 1) -lt $Rest.Length) {
                $limit = [int]$Rest[$i + 1]; $i++
            }
        }
        $args2 = @('-R', $Repo, '--limit', $limit, '--json',
                  'databaseId,status,conclusion,displayTitle,headBranch,event,createdAt,workflowName')
        if ($workflow) { $args2 += @('--workflow', $workflow) }
        $result = & gh run list @args2 | ConvertFrom-Json
        $result | Format-Table @{n='id';e={$_.databaseId}},
                                @{n='status';e={$_.status}},
                                @{n='conclusion';e={$_.conclusion}},
                                @{n='workflow';e={$_.workflowName}},
                                @{n='branch';e={$_.headBranch}},
                                @{n='created';e={[datetime]$_.createdAt}}
    }

    'tail' {
        Require-Gh
        if ($Rest.Length -eq 0) {
            Write-Error "Usage: runner-ctl.ps1 tail <run-id>"
            exit 2
        }
        & gh run watch $Rest[0] -R $Repo --exit-status
    }

    'cancel' {
        Require-Gh
        if ($Rest.Length -eq 0) {
            Write-Error "Usage: runner-ctl.ps1 cancel <run-id>"
            exit 2
        }
        & gh run cancel $Rest[0] -R $Repo
    }

    'token' {
        Require-Gh
        $tok = & gh api -X POST "repos/$Repo/actions/runners/registration-token" --jq .token
        if (-not $tok) { Write-Error 'Failed to mint token.'; exit 3 }
        Write-Host $tok
    }

    'watchdog' {
        if ($Rest.Length -eq 0) {
            Write-Error "Usage: runner-ctl.ps1 watchdog <install|uninstall|status|run-once>"
            exit 2
        }
        $sub      = $Rest[0]
        $scriptDir = Split-Path -Parent $PSCommandPath
        $taskName = 'MorphoClaw-RunnerWatchdog'
        $logPath  = Join-Path $env:LOCALAPPDATA 'MorphoClaw\runner-watchdog.log'

        switch ($sub) {
            'install' {
                $installer = Join-Path $scriptDir 'install-watchdog.ps1'
                if (-not (Test-Path $installer)) {
                    Write-Error "install-watchdog.ps1 not found at $installer"
                    exit 3
                }
                # `& $installer @extra` is splatting; `& $installer @(...)`
                # would pass the array as a single positional argument.
                $extra = @($Rest | Select-Object -Skip 1)
                & $installer @extra
            }
            'uninstall' {
                $uninstaller = Join-Path $scriptDir 'uninstall-watchdog.ps1'
                if (-not (Test-Path $uninstaller)) {
                    Write-Error "uninstall-watchdog.ps1 not found at $uninstaller"
                    exit 3
                }
                $extra = @($Rest | Select-Object -Skip 1)
                & $uninstaller @extra
            }
            'status' {
                Write-Host "== Scheduled Task '$taskName' ==" -ForegroundColor Cyan
                $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
                if (-not $task) {
                    Write-Host "  (not installed; run: runner-ctl.ps1 watchdog install)" -ForegroundColor Yellow
                } else {
                    $info = Get-ScheduledTaskInfo -TaskName $taskName
                    [pscustomobject]@{
                        State              = $task.State
                        LastRunTime        = $info.LastRunTime
                        LastTaskResult     = $info.LastTaskResult
                        NextRunTime        = $info.NextRunTime
                        NumberOfMissedRuns = $info.NumberOfMissedRuns
                    } | Format-List
                }
                Write-Host "== Last 20 lines of $logPath ==" -ForegroundColor Cyan
                if (Test-Path $logPath) {
                    Get-Content -Path $logPath -Tail 20
                } else {
                    Write-Host "  (no log yet)"
                }
            }
            'run-once' {
                $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
                if (-not $task) {
                    Write-Host "Task not installed; running watchdog inline." -ForegroundColor Yellow
                    $watchdog = Join-Path $scriptDir 'runner-watchdog.ps1'
                    if (-not (Test-Path $watchdog)) {
                        Write-Error "runner-watchdog.ps1 not found at $watchdog"
                        exit 3
                    }
                    & $watchdog
                } else {
                    Start-ScheduledTask -TaskName $taskName
                    Write-Host "Triggered '$taskName'. Tail the log with:" -ForegroundColor Green
                    Write-Host "  Get-Content -Wait `"$logPath`""
                }
            }
            default {
                Write-Error "Unknown watchdog subcommand: $sub. Expected install|uninstall|status|run-once."
                exit 2
            }
        }
    }

    'keepalive' {
        if ($Rest.Length -eq 0) {
            Write-Error "Usage: runner-ctl.ps1 keepalive <install|uninstall|status|run-once>"
            exit 2
        }
        $sub        = $Rest[0]
        $scriptDir  = Split-Path -Parent $PSCommandPath
        $taskName   = 'MorphoClaw-WSL-Keepalive'
        $logPath    = Join-Path $env:LOCALAPPDATA 'MorphoClaw\wsl-keepalive.log'
        $sentinel   = 'exec sleep infinity'

        switch ($sub) {
            'install' {
                $installer = Join-Path $scriptDir 'install-wsl-keepalive.ps1'
                if (-not (Test-Path $installer)) {
                    Write-Error "install-wsl-keepalive.ps1 not found at $installer"
                    exit 3
                }
                $extra = @($Rest | Select-Object -Skip 1)
                & $installer @extra
            }
            'uninstall' {
                $uninstaller = Join-Path $scriptDir 'uninstall-wsl-keepalive.ps1'
                if (-not (Test-Path $uninstaller)) {
                    Write-Error "uninstall-wsl-keepalive.ps1 not found at $uninstaller"
                    exit 3
                }
                $extra = @($Rest | Select-Object -Skip 1)
                & $uninstaller @extra
            }
            'status' {
                Write-Host "== Scheduled Task '$taskName' ==" -ForegroundColor Cyan
                $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
                if (-not $task) {
                    Write-Host "  (not installed; run: runner-ctl.ps1 keepalive install)" -ForegroundColor Yellow
                } else {
                    $info = Get-ScheduledTaskInfo -TaskName $taskName
                    [pscustomobject]@{
                        State              = $task.State
                        LastRunTime        = $info.LastRunTime
                        LastTaskResult     = $info.LastTaskResult
                        NextRunTime        = $info.NextRunTime
                        NumberOfMissedRuns = $info.NumberOfMissedRuns
                    } | Format-List
                }
                Write-Host "== Active keepalive wsl.exe processes ==" -ForegroundColor Cyan
                $procs = Get-CimInstance Win32_Process -Filter "Name='wsl.exe'" -ErrorAction SilentlyContinue |
                         Where-Object { $_.CommandLine -and $_.CommandLine.ToLower().Contains($sentinel.ToLower()) }
                if (-not $procs) {
                    Write-Host "  (none — distro will idle-shutdown within ~60s)" -ForegroundColor Yellow
                } else {
                    $procs | Select-Object ProcessId, CommandLine | Format-List
                }
                Write-Host "== Last 20 lines of $logPath ==" -ForegroundColor Cyan
                if (Test-Path $logPath) {
                    Get-Content -Path $logPath -Tail 20
                } else {
                    Write-Host "  (no log yet — likely dedup'd silently on every fire)"
                }
            }
            'run-once' {
                $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
                if (-not $task) {
                    Write-Host "Task not installed; running launcher inline." -ForegroundColor Yellow
                    $launcher = Join-Path $scriptDir 'wsl-keepalive-launcher.vbs'
                    if (-not (Test-Path $launcher)) {
                        Write-Error "wsl-keepalive-launcher.vbs not found at $launcher"
                        exit 3
                    }
                    & cscript.exe //nologo $launcher
                } else {
                    Start-ScheduledTask -TaskName $taskName
                    Write-Host "Triggered '$taskName'. Inspect status with:" -ForegroundColor Green
                    Write-Host "  pwsh runner-ctl.ps1 keepalive status"
                }
            }
            default {
                Write-Error "Unknown keepalive subcommand: $sub. Expected install|uninstall|status|run-once."
                exit 2
            }
        }
    }
}
