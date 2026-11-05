# rig_experiment_setup.ps1: make a Windows test machine an experiment machine (docs/design/rig-fleet.md,
# section 5): tests start there only when nobody uses it, show a banner with a Stop button, and stop when
# anyone arrives. Run it as the lab account (over SSH or at the machine), after `rig.py install` has put
# rig_banner.ps1 in the lock's folder. It needs no admin rights. It:
#   - writes experiment.json in the lock's folder (rig.py then applies the experiment-machine rules);
#   - registers the Task Scheduler task "campy test banner", which starts rig_banner.ps1 at logon in the
#     lab account's own session (and which rig.py starts itself when the banner isn't running);
#   - puts two shortcuts on the desktop: "Stop campy test" and "Resume campy tests";
#   - (re)starts the banner in the logged-on session, if there is one.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File rig_experiment_setup.ps1 [-Hours 22:00-06:00] [-IdleMinutes 15] [-PauseUntil 08:00]
#   powershell -NoProfile -ExecutionPolicy Bypass -File rig_experiment_setup.ps1 -Undo     everything above removed again
#
#   -Root  the lock's folder; otherwise RIG_ROOT, otherwise C:\campy_data\live

param(
	[string]$Root = '',
	[string]$Hours = '22:00-06:00',
	[int]$IdleMinutes = 15,
	[string]$PauseUntil = '08:00',
	[switch]$Undo,
	[switch]$NoStart
)
$ErrorActionPreference = 'Stop'
if (-not $Root) { $Root = if ($env:RIG_ROOT) { $env:RIG_ROOT } else { 'C:\campy_data\live' } }
$Task = 'campy test banner'
$Banner = Join-Path $Root 'rig_banner.ps1'
$Config = Join-Path $Root 'experiment.json'
$Desktop = [Environment]::GetFolderPath('Desktop')
$Shortcuts = @{ 'Stop campy test' = '-Stop'; 'Resume campy tests' = '-Resume' }

function Stop-Banners {
	# The banner running now (an older copy, after an install): it ends; the task starts the new one
	Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe'" | Where-Object { $_.CommandLine -match 'rig_banner\.ps1' -and $_.CommandLine -notmatch '-(Stop|Resume)\b' } |
		ForEach-Object { Write-Host "ending the running banner (pid $($_.ProcessId))"; Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

if ($Undo) {
	if (Get-ScheduledTask -TaskName $Task -ErrorAction SilentlyContinue) { Unregister-ScheduledTask -TaskName $Task -Confirm:$false; Write-Host "task '$Task' removed" }
	Stop-Banners
	foreach ($name in $Shortcuts.Keys) {
		$lnk = Join-Path $Desktop "$name.lnk"
		if (Test-Path -LiteralPath $lnk) { Remove-Item -Force -LiteralPath $lnk; Write-Host "shortcut removed: $lnk" }
	}
	if (Test-Path -LiteralPath $Config) {
		Move-Item -Force -LiteralPath $Config -Destination "$Config.off"
		Write-Host "$Config renamed to experiment.json.off: rig.py no longer applies the experiment-machine rules"
	}
	exit 0
}

if (-not (Test-Path -LiteralPath $Banner)) { throw "$Banner is missing: install the lock first (rig.py install)" }
if ($Hours -notmatch '^(any|\d{1,2}:\d\d-\d{1,2}:\d\d(,\d{1,2}:\d\d-\d{1,2}:\d\d)*)$') { throw "-Hours: HH:MM-HH:MM[,...] or any, not $Hours" }
if ($PauseUntil -notmatch '^\d{1,2}:\d\d$') { throw "-PauseUntil: HH:MM, not $PauseUntil" }

$cfg = [ordered]@{ hours = $Hours; idle_minutes = $IdleMinutes; pause_until = $PauseUntil; banner = $true; banner_task = $Task }
[IO.File]::WriteAllText($Config, ($cfg | ConvertTo-Json) + "`r`n", [Text.UTF8Encoding]::new($false))
Write-Host "wrote $Config (tests may start $Hours, after $IdleMinutes min idle; a stop pauses them until $PauseUntil)"

$ps = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$me = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute $ps -Argument ('-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}"' -f $Banner)
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $me
$principal = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $Task -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
	-Description 'campy: the banner that shows a running campy test, with a Stop button (bench/rig_banner.ps1)' -Force | Out-Null
Write-Host "task '$Task' registered: it starts the banner at logon, in $me's session"

$shell = New-Object -ComObject WScript.Shell
foreach ($name in $Shortcuts.Keys) {
	$lnk = $shell.CreateShortcut((Join-Path $Desktop "$name.lnk"))
	$lnk.TargetPath = $ps
	$lnk.Arguments = '-NoProfile -ExecutionPolicy Bypass -File "{0}" {1}' -f $Banner, $Shortcuts[$name]
	$lnk.WorkingDirectory = $Root
	$lnk.Description = "campy: $name"
	$lnk.IconLocation = if ($name -like 'Stop*') { "$env:SystemRoot\System32\imageres.dll,100" } else { "$env:SystemRoot\System32\imageres.dll,101" }
	$lnk.Save()
	Write-Host "shortcut: $(Join-Path $Desktop "$name.lnk")"
}

if (-not $NoStart) {
	Stop-Banners
	Start-Sleep -Milliseconds 500
	Start-ScheduledTask -TaskName $Task
	Write-Host "banner task started (it runs only while $me is logged on; hidden until a test runs)"
}
