# pause_windows_update.ps1 - pause Windows Update for a number of days, so it can't restart a recording PC mid-session.
# The same as Settings > Windows Update > Pause updates (1 to 5 weeks): it sets the same registry values that page
# does (HKLM\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings). Microsoft doesn't document them, so a Windows update
# could change how they work: check Settings > Windows Update says "Updates paused until ...".
# Run it from PowerShell (it asks for administrator rights itself: Windows' prompt, click Yes):
#   powershell -NoProfile -ExecutionPolicy Bypass -File <campy folder>\tools\windows\pause_windows_update.ps1 -Days 14
# -Days: 1 to 35 (Windows' limit); a longer pause already in place is kept. It prints "PAUSED until <date>", and
# warns if a restart from earlier updates is already pending (a pause doesn't cancel it: restart first).
# When a pause ends, Windows installs what is waiting, and usually restarts, before it allows another: let it,
# between recordings, then pause again. The log is saved next to this file as pause_windows_update.log.
# To undo: Settings > Windows Update > Resume updates.
param([int]$Days = 14, [switch]$Elevated)
$log = Join-Path $PSScriptRoot 'pause_windows_update.log'
$me = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
	if ($Elevated) { Write-Host "  ===== FAILED: still not administrator. =====" -ForegroundColor Red; exit 1 }
	Write-Host "Asking for administrator rights (Windows' prompt: click Yes)..." -ForegroundColor Cyan
	try {
		$p = Start-Process powershell -Verb RunAs -Wait -PassThru -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"", '-Days', "$Days", '-Elevated')
	} catch { Write-Host "  ===== NOT PAUSED: the administrator prompt was declined. =====" -ForegroundColor Red; exit 2 }
	if (Test-Path $log) { Get-Content $log | Select-String 'PAUSED:|WARNING:|FAILED:' | Select-Object -Last 2 | ForEach-Object { Write-Host $_.Line -ForegroundColor $(if ($_.Line -match 'PAUSED') { 'Green' } else { 'Yellow' }) } }
	exit $p.ExitCode
}
try { $host.UI.RawUI.WindowTitle = 'Pause Windows Update' } catch { }
Start-Transcript -Path $log -Append | Out-Null
$Days = [math]::Max(1, [math]::Min($Days, 35))
$key = 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings'
$now = (Get-Date).ToUniversalTime()
$until = $now.AddDays($Days)
$fmt = 'yyyy-MM-ddTHH:mm:ssZ'
if (-not (Test-Path $key)) { New-Item -Path $key -Force | Out-Null }
$old = Get-ItemProperty $key -ErrorAction SilentlyContinue
if ($old.PauseUpdatesExpiryTime) {
	try { if ([datetime]::Parse($old.PauseUpdatesExpiryTime).ToUniversalTime() -gt $until) { $until = [datetime]::Parse($old.PauseUpdatesExpiryTime).ToUniversalTime() } } catch { }
}
foreach ($kv in @(@('PauseUpdatesStartTime', $now), @('PauseUpdatesExpiryTime', $until), @('PauseFeatureUpdatesStartTime', $now),
	@('PauseFeatureUpdatesEndTime', $until), @('PauseQualityUpdatesStartTime', $now), @('PauseQualityUpdatesEndTime', $until))) {
	Set-ItemProperty -Path $key -Name $kv[0] -Value $kv[1].ToString($fmt) -Type String
}
$back = [datetime]::Parse((Get-ItemProperty $key).PauseUpdatesExpiryTime)
$pending = (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired') -or
	(Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending')
Write-Host ""
Write-Host "  ===== PAUSED: Windows Update is paused until $($back.ToLocalTime().ToString('ddd MMM d, h:mm tt')). =====" -ForegroundColor Green
if ($pending) { Write-Host "  WARNING: a restart is already pending from earlier updates; the pause doesn't cancel it. Restart before recording." -ForegroundColor Yellow }
Stop-Transcript | Out-Null
exit $(if ($pending) { 3 } else { 0 })
