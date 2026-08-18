<#
.SYNOPSIS
    Registers a Windows Scheduled Task that runs MizanDaemon.exe at system
    startup, whether or not anyone is logged in - Stage 8c autostart.

.DESCRIPTION
    - Trigger: At system startup (not "at logon" - the task must come up
      with nobody logged into Windows, e.g. right after a headless reboot).
    - Principal: runs as SYSTEM by default (LogonType ServiceAccount), which
      is the standard credential-free way to satisfy "run whether user is
      logged on or not" without storing a password in the task. If you'd
      rather it run as your own account, re-register manually with
      `-UserId "<domain>\<user>" -LogonType Password` and supply
      `-Password` to Register-ScheduledTask (Task Scheduler needs a stored
      credential to log a real user on with nobody present).
    - Restart policy: if MizanDaemon.exe exits with a non-zero (failure)
      exit code, Task Scheduler restarts it every $RestartIntervalMinutes,
      up to $RestartCount attempts. A *clean* exit (stop-flag or graceful
      SIGINT/SIGTERM, both exit 0) is treated as success and is NOT
      restarted - only a genuine crash triggers this. Safe even if the
      previous instance is still shutting down: MizanDaemon.exe's
      single-instance lock (app.automation.lock) makes a restart that
      overlaps a still-alive process a harmless no-op instead of a second
      daemon running against the same accounts.
    - ExecutionTimeLimit is disabled (TimeSpan.Zero): Task Scheduler's
      default 72-hour limit would otherwise kill this long-running daemon
      out from under itself.

.PARAMETER TaskName
    Name of the scheduled task. Default: MizanDaemon

.PARAMETER ExePath
    Path to MizanDaemon.exe. Default: dist\MizanDaemon.exe next to this
    script's repo checkout.

.PARAMETER UserId
    Account the task runs as. Default: SYSTEM.

.PARAMETER RestartIntervalMinutes
    Minutes to wait before restarting after a failed (non-zero exit) run.

.PARAMETER RestartCount
    Maximum number of restart attempts per failure.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\register_daemon_task.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\register_daemon_task.ps1 -ExePath "C:\Mizan\MizanDaemon.exe"
#>
param(
    [string]$TaskName = "MizanDaemon",
    [string]$ExePath = (Join-Path $PSScriptRoot "..\dist\MizanDaemon.exe"),
    [string]$UserId = "SYSTEM",
    [int]$RestartIntervalMinutes = 1,
    [int]$RestartCount = 3
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $ExePath -PathType Leaf)) {
    throw "MizanDaemon.exe not found at '$ExePath'. Build it first: pyinstaller mizan_daemon.spec"
}
$ExePath = (Resolve-Path $ExePath).Path
$WorkingDirectory = Split-Path $ExePath -Parent

Write-Host "Registering task '$TaskName' -> $ExePath (working dir: $WorkingDirectory, user: $UserId)"

$action = New-ScheduledTaskAction -Execute $ExePath -WorkingDirectory $WorkingDirectory
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount $RestartCount `
    -RestartInterval (New-TimeSpan -Minutes $RestartIntervalMinutes) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

$task = New-ScheduledTask `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description "Mizan automation daemon (paper trading only) - runs the bot polling loop independently of any GUI session. Stop with: New-Item -ItemType File -Force '$WorkingDirectory\data\daemon.stop'"

Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null

Write-Host "Registered. Useful next commands:"
Write-Host "  Start now:     Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "  Check status:  Get-ScheduledTask -TaskName '$TaskName' | Get-ScheduledTaskInfo"
Write-Host "  Tail the log:  Get-Content '$WorkingDirectory\logs\daemon.log' -Wait -Tail 20"
Write-Host "  Stop it:       New-Item -ItemType File -Force '$WorkingDirectory\data\daemon.stop'"
Write-Host "  Remove task:   scripts\unregister_daemon_task.ps1"
