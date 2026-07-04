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
    [string]$WatchdogTime = "15:15",
    [switch]$Remove
)

$TaskName = "ClaudeTraderDaily"
$WatchdogName = "ClaudeTraderWatchdog"
$RepoRoot = Split-Path -Parent $PSScriptRoot

if ($Remove) {
    schtasks /delete /tn $TaskName /f
    schtasks /delete /tn $WatchdogName /f
    Write-Host "Removed scheduled tasks '$TaskName' and '$WatchdogName'."
    exit 0
}

# Resolve the real interpreter (sys.executable), not the WindowsApps alias shim,
# so the task uses the environment where the project's dependencies are installed.
$Python = (& python -c "import sys; print(sys.executable)").Trim()
if (-not (Test-Path $Python)) { $Python = (Get-Command python).Source }
$Command = "cmd /c cd /d `"$RepoRoot`" && `"$Python`" scheduler.py --run-now >> logs\scheduler_runs.log 2>&1"

New-Item -ItemType Directory -Force -Path (Join-Path $RepoRoot "logs") | Out-Null

schtasks /create /f /tn $TaskName /sc weekly /d MON,TUE,WED,THU,FRI /st $Time /tr $Command

# Watchdog: alerts via ntfy when the daily run did not happen / crashed
$WatchdogCommand = "cmd /c cd /d `"$RepoRoot`" && `"$Python`" -m src.watchdog >> logs\watchdog.log 2>&1"
schtasks /create /f /tn $WatchdogName /sc weekly /d MON,TUE,WED,THU,FRI /st $WatchdogTime /tr $WatchdogCommand

# Harden both tasks: wake the machine to run, and run ASAP if the start was missed
try {
    $settings = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable `
        -ExecutionTimeLimit (New-TimeSpan -Hours 1)
    Set-ScheduledTask -TaskName $TaskName -Settings $settings | Out-Null
    Set-ScheduledTask -TaskName $WatchdogName -Settings $settings | Out-Null
    Write-Host "Applied WakeToRun + StartWhenAvailable to both tasks."
} catch {
    Write-Host "WARNING: could not apply WakeToRun/StartWhenAvailable ($_)."
    Write-Host "Tasks still run, but a sleeping machine may miss the start time."
}

Write-Host ""
Write-Host "Registered '$TaskName' (Mon-Fri $Time) and '$WatchdogName' (Mon-Fri $WatchdogTime)."
Write-Host "NOTE: $Time local should equal 9:35 AM New York. Re-run with -Time when"
Write-Host "US daylight saving shifts relative to your timezone."
Write-Host "Output: $RepoRoot\logs\scheduler_runs.log / logs\watchdog.log"
