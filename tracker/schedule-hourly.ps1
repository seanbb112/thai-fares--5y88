# Registers (or removes with -Remove) an hourly Windows task that runs tracker.py.
# Runs only while you're logged in and the PC is on; a missed hour runs when the PC wakes.
param([switch]$Remove)

$name = "ThailandFlightTracker"
if ($Remove) {
    Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
    Write-Output "Removed $name"
    return
}

$script  = Join-Path $PSScriptRoot "tracker.py"
$python  = (Get-Command python).Source
$pythonw = Join-Path (Split-Path $python) "pythonw.exe"   # no console window
if (-not (Test-Path $pythonw)) { $pythonw = $python }

$action   = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$script`"" -WorkingDirectory $PSScriptRoot
$trigger  = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Hours 1)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -ExecutionTimeLimit (New-TimeSpan -Minutes 50) -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings `
    -Description "Hourly Google Flights sweep, Ireland to Thailand 27/12/26" -Force | Out-Null
Get-ScheduledTask -TaskName $name | Get-ScheduledTaskInfo | Select-Object TaskName, NextRunTime
