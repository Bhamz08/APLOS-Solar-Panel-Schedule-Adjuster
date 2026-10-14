# Registers a Windows Task Scheduler task that starts the APLOS daily scheduler
# at logon and restarts it if it stops. The scheduler itself catches up on
# missed runs after a reboot, so no separate "daily" trigger is required.
#
# Usage:  powershell -ExecutionPolicy Bypass -File scripts\install_windows_task.ps1
#         powershell -ExecutionPolicy Bypass -File scripts\install_windows_task.ps1 -Remove
param([switch]$Remove, [string]$TaskName = "APLOS Solar Panel Schedule Adjuster")
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed task '$TaskName'."
    exit 0
}

$python = Join-Path $root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $python)) { throw "Virtual environment not found. Run scripts\setup.ps1 first." }

$action = New-ScheduledTaskAction -Execute $python -Argument "-m aplos scheduler" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 5) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "Daily solar panel cleaning schedule evaluation (runs locally)." -Force | Out-Null
Write-Host "Registered task '$TaskName'. It starts at logon; start it now with:"
Write-Host "  Start-ScheduledTask -TaskName '$TaskName'"
