# Makes the Enzo PC helper start by itself when you sign in to Windows (no window).
#
#   Right-click this file -> "Run with PowerShell"      (or: powershell -ExecutionPolicy Bypass -File scripts\install_pc_helper_autostart.ps1)
#   Remove it again:  powershell -ExecutionPolicy Bypass -File scripts\install_pc_helper_autostart.ps1 -Remove
#
# The helper connects OUT to Enzo; it logs to data\pc_helper.log. Needs the saved login
# (data\pc_helper.json) - if it's missing, run "python pc_helper.py" once in a terminal first.

param([switch]$Remove)

$taskName = 'Enzo PC Helper'
# The task was called 'SHARP PC Helper' before the rename: remove it so two helpers never run.
Unregister-ScheduledTask -TaskName 'SHARP PC Helper' -Confirm:$false -ErrorAction SilentlyContinue
if ($Remove) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed '$taskName'."
    exit 0
}

$root = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path (Join-Path $root 'data\pc_helper.json'))) {
    Write-Host "No saved login yet - run 'python pc_helper.py' once in this folder first, then run this again."
    exit 1
}

# pythonw = Python without a console window
$python = (Get-Command python -ErrorAction Stop).Source
$pythonw = Join-Path (Split-Path $python) 'pythonw.exe'
if (-not (Test-Path $pythonw)) { Write-Host "pythonw.exe not found next to $python"; exit 1 }

$action = New-ScheduledTaskAction -Execute $pythonw -Argument 'pc_helper.py' -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Force `
    -Description 'Lets Enzo on the NAS use this PC (lock, apps, media, files, screen).' | Out-Null
Start-ScheduledTask -TaskName $taskName
Write-Host "Done. The Enzo PC helper now starts at every Windows login (and was just started)."
