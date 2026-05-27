# Launch PCB / IMPC processing inside WSL (local GPU first, Jetstream pack on failure).
#
#   pwsh scripts/dev/run-local-bright-seed.ps1
#   pwsh scripts/dev/run-local-bright-seed.ps1 -JetstreamOnly
#   pwsh scripts/dev/run-local-bright-seed.ps1 -Preset impc -MaxSteps 8

param(
    [ValidateSet("impc", "pcb")]
    [string]$Preset = "pcb",
    [int]$MaxSteps = 0,
    [int]$MaxAxis = 384,
    [switch]$DryRun,
    [switch]$JetstreamOnly,
    [switch]$SkipPrep
)

$Repo = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path
$Drive = $Repo.Substring(0, 1).ToLower()
$Tail = $Repo.Substring(2) -replace '\\', '/'
$WslRepo = "/mnt/$Drive$Tail"

if ($Preset -eq "impc" -and -not $JetstreamOnly) {
    $Cmd = "cd '$WslRepo' && bash scripts/dev/run_local_bright_seed_test.sh --preset impc"
} else {
    $Cmd = "cd '$WslRepo' && bash scripts/dev/run_pcb_pipeline.sh --max-axis $MaxAxis"
}

if ($MaxSteps -gt 0) { $Cmd += " --max-steps $MaxSteps" }
if ($DryRun) { $Cmd += " --dry-run" }
if ($JetstreamOnly) { $Cmd += " --jetstream-only" }
if ($SkipPrep) { $Cmd += " --skip-prep" }

Write-Host "WSL: $Cmd"
wsl bash -lc $Cmd
exit $LASTEXITCODE
