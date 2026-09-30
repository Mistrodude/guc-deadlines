# Registers a task that runs the robot at 8:00 and 18:00 every day.
# If the laptop was off/asleep at that time, it runs as soon as it's back on.
# Run once:  right-click this file -> "Run with PowerShell"
$py = (Get-Command pythonw -ErrorAction SilentlyContinue).Source
if (-not $py) { $py = (Get-Command python).Source }
$action   = New-ScheduledTaskAction -Execute $py -Argument "`"$PSScriptRoot\run.py`"" -WorkingDirectory $PSScriptRoot
$triggers = @((New-ScheduledTaskTrigger -Daily -At 8am), (New-ScheduledTaskTrigger -Daily -At 6pm))
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RunOnlyIfNetworkAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "GUC deadlines" -Action $action -Trigger $triggers -Settings $settings -Force | Out-Null
Write-Host "Done. The robot will run at 8:00 and 18:00 (or as soon as your laptop wakes up after those times)."
Write-Host "To run it right now: Start-ScheduledTask -TaskName 'GUC deadlines'"
