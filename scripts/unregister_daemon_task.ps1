<#
.SYNOPSIS
    Cleanly removes the MizanDaemon Scheduled Task, stopping the daemon
    gracefully first (via the stop-flag file) rather than force-killing it.

.DESCRIPTION
    Deliberately does NOT use Stop-ScheduledTask, which force-terminates
    the process outright - the whole point of the stop-flag mechanism
    (Stage 8b) is that the daemon finishes whatever bot cycle is in flight
    before exiting, so this script requests that instead and waits for the
    process to exit on its own before unregistering the task.

.PARAMETER TaskName
    Name of the scheduled task to remove. Default: MizanDaemon

.PARAMETER TimeoutSeconds
    How long to wait for a graceful exit before giving up and warning
    (the task is still unregistered either way - see warning below).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\unregister_daemon_task.ps1
#>
param(
    [string]$TaskName = "MizanDaemon",
    [int]$TimeoutSeconds = 30
)

$ErrorActionPreference = "Stop"

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Host "No scheduled task named '$TaskName' found - nothing to remove."
    exit 0
}

$exePath = $task.Actions[0].Execute
$workingDirectory = Split-Path $exePath -Parent
$stopFilePath = Join-Path $workingDirectory "data\daemon.stop"

$running = Get-Process -Name (Split-Path $exePath -LeafBase) -ErrorAction SilentlyContinue
if ($running) {
    Write-Host "MizanDaemon.exe is running (pid=$($running.Id)) - requesting a graceful stop via $stopFilePath ..."
    New-Item -ItemType File -Force -Path $stopFilePath | Out-Null

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (-not (Get-Process -Name (Split-Path $exePath -LeafBase) -ErrorAction SilentlyContinue)) {
            break
        }
        Start-Sleep -Seconds 1
    }

    if (Get-Process -Name (Split-Path $exePath -LeafBase) -ErrorAction SilentlyContinue) {
        Write-Warning ("MizanDaemon.exe did not exit within {0}s of the stop request - it may still be " +
            "finishing a bot cycle. The task will be unregistered anyway (so it won't restart), but the " +
            "running process is left alone; check {1} shortly." -f $TimeoutSeconds, $stopFilePath)
    }
} else {
    Write-Host "MizanDaemon.exe is not currently running - nothing to stop."
}

# Disable first so a startup/restart trigger can't race us mid-removal.
Disable-ScheduledTask -TaskName $TaskName | Out-Null
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false

Write-Host "Removed scheduled task '$TaskName'."
