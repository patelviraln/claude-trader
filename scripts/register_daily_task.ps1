# Registers a Windows Task Scheduler job that fires the daily signal run
# (exit rules -> state reconciliation -> scans -> fill sync -> summary).
#
# Usage (PowerShell, from the repo root):
#   .\scripts\register_daily_task.ps1              # default 14:35 local = 9:35 ET during US DST
#   .\scripts\register_daily_task.ps1 -Time 15:35  # adjust when US DST shifts vs your zone
#   .\scripts\register_daily_task.ps1 -Remove      # unregister
#
# The task runs `python scheduler.py --run-now` Mon-Fri and appends output to
# logs\scheduler_runs.log. The machine must be on (not asleep) at run time.

param(
    [string]$Time = "14:35",
    [switch]$Remove
)

$TaskName = "ClaudeTraderDaily"
$RepoRoot = Split-Path -Parent $PSScriptRoot

if ($Remove) {
    schtasks /delete /tn $TaskName /f
    Write-Host "Removed scheduled task '$TaskName'."
    exit 0
}

$Python = (Get-Command python).Source
$Command = "cmd /c cd /d `"$RepoRoot`" && `"$Python`" scheduler.py --run-now >> logs\scheduler_runs.log 2>&1"

New-Item -ItemType Directory -Force -Path (Join-Path $RepoRoot "logs") | Out-Null

schtasks /create /f /tn $TaskName /sc weekly /d MON,TUE,WED,THU,FRI /st $Time /tr $Command

Write-Host ""
Write-Host "Registered '$TaskName' — Mon-Fri at $Time local time."
Write-Host "NOTE: $Time local should equal 9:35 AM New York. Re-run with -Time when"
Write-Host "US daylight saving shifts relative to your timezone."
Write-Host "Output: $RepoRoot\logs\scheduler_runs.log"
