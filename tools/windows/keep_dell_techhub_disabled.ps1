# keep_dell_techhub_disabled.ps1 - keep Dell's TechHub agent turned off on a Dell PC used for live tracking.
#
# Dell Optimizer's TechHub agent (service "DellTechHub") reads the embedded controller every 15 minutes, and on the
# Dell Precision 7875 those reads, made through the BIOS, slowed campy-live's sets for about 9 s each time (see
# docs/hardware-guide.html, "On a Dell PC, Dell's TechHub agent slows live tracking every 15 minutes"). Disabling the
# service is not enough on its own: on the Dell Pro Max Slim, a Dell SupportAssist self-update (8 November 2026) set it
# back to start automatically and started it. This script turns it off again whenever it finds it on.
#
# Run in PowerShell as administrator:
#   ... keep_dell_techhub_disabled.ps1             # check once: if DellTechHub is on, disable and stop it
#   ... keep_dell_techhub_disabled.ps1 -Install    # also add the scheduled task "campy keep DellTechHub disabled"
#   ... keep_dell_techhub_disabled.ps1 -Uninstall  # remove that task (the service stays as it is)
#
# The task runs this file where it is (keep it there; C:\campy_data\launchers on campy's rigs) as SYSTEM: daily at
# 7 PM, at startup, and whenever Windows logs that a service's start type changed (System event 7040), which is what
# the SupportAssist update did. Each run that finds the agent on sets the service to Disabled, stops it, and ends the
# programs it started (Dell Optimizer, Dell Update's helper and Dell's other TechHub helpers stop with it), then adds
# a line to C:\campy_data\dell_techhub_disabled.txt. Runs that find it off write nothing.
# To turn the agent on for a while (a test) without the task undoing it, put a time in C:\campy_data\dell_techhub_hold_until.txt,
# e.g. 2026-11-09T21:30:00: until then each run only notes that it was held. Undo everything:
#   ... keep_dell_techhub_disabled.ps1 -Uninstall; Set-Service DellTechHub -StartupType Automatic; Start-Service DellTechHub
param([switch]$Install, [switch]$Uninstall)
$ErrorActionPreference = 'Stop'
$TaskName = 'campy keep DellTechHub disabled'
$Dir = if (Test-Path C:\campy_data) { 'C:\campy_data' } else { $env:PUBLIC }
$Log = Join-Path $Dir 'dell_techhub_disabled.txt'
$Hold = Join-Path $Dir 'dell_techhub_hold_until.txt'
function Say($s) { $s; try { Add-Content -Path $Log -Value $s } catch {} }

if ($Uninstall) {
	Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
	Say "$(Get-Date -Format s) removed the scheduled task '$TaskName' (DellTechHub left as it is: $((Get-Service DellTechHub -ErrorAction SilentlyContinue).StartType))"
	return
}
if ($Install) {
	$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$PSCommandPath`""
	$cls = Get-CimClass -Namespace Root/Microsoft/Windows/TaskScheduler -ClassName MSFT_TaskEventTrigger
	$onChange = New-CimInstance -CimClass $cls -ClientOnly
	$onChange.Enabled = $true
	$onChange.Subscription = '<QueryList><Query Id="0" Path="System"><Select Path="System">*[System[Provider[@Name=''Service Control Manager''] and EventID=7040]]</Select></Query></QueryList>'
	$triggers = @((New-ScheduledTaskTrigger -Daily -At '7:00 PM'), (New-ScheduledTaskTrigger -AtStartup), $onChange)
	$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 5) -MultipleInstances IgnoreNew -StartWhenAvailable
	$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
	Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers -Settings $settings -Principal $principal `
		-Description 'campy: keeps Dell TechHub (Dell Optimizer) disabled; its embedded-controller reads slow live tracking every 15 minutes. Undo: keep_dell_techhub_disabled.ps1 -Uninstall' -Force | Out-Null
	Say "$(Get-Date -Format s) installed the scheduled task '$TaskName' (daily 7 PM, at startup, on service start-type changes) running $PSCommandPath"
}

$svc = Get-CimInstance Win32_Service -Filter "Name='DellTechHub'"
if (-not $svc) { if ($Install) { 'DellTechHub is not installed on this PC: nothing to do.' }; return }
if (Test-Path $Hold) {
	$until = [datetime]::MinValue
	if ([datetime]::TryParse((Get-Content $Hold -Raw).Trim(), [ref]$until) -and (Get-Date) -lt $until) {
		Say "$(Get-Date -Format s) held until $($until.ToString('s')) ($Hold): left DellTechHub $($svc.State), start $($svc.StartMode)"
		return
	}
}
$all = Get-CimInstance Win32_Process
$tree = @($svc.ProcessId | Where-Object { $_ })
$more = [bool]$tree
while ($more) {
	$kids = @($all | Where-Object { $tree -contains $_.ParentProcessId -and $tree -notcontains $_.ProcessId } | ForEach-Object { $_.ProcessId })
	$more = $kids.Count -gt 0; $tree += $kids
}
$names = @($all | Where-Object { $_.Name -like 'Dell.TechHub*' } | ForEach-Object { $_.ProcessId })
$kill = @($tree + $names | Where-Object { $_ } | Sort-Object -Unique)
if ($svc.StartMode -eq 'Disabled' -and $svc.State -eq 'Stopped' -and $kill.Count -eq 0) { return }   # already off
$what = ($all | Where-Object { $kill -contains $_.ProcessId } | ForEach-Object { $_.Name }) -join ' '
Set-Service DellTechHub -StartupType Disabled
Stop-Service DellTechHub -Force -ErrorAction SilentlyContinue
foreach ($id in $kill) { Stop-Process -Id $id -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 3
$now = Get-Service DellTechHub
$left = (Get-Process Dell.TechHub* -ErrorAction SilentlyContinue | ForEach-Object { $_.Name }) -join ' '
Say "$(Get-Date -Format s) found DellTechHub $($svc.State), start $($svc.StartMode); now $($now.Status), start $($now.StartType); ended: $what$(if ($left) { "; still running: $left" })"
