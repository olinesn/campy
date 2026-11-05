# disable_usb_selective_suspend.ps1 - stop Windows powering down idle USB devices on a recording PC.
#
# "USB selective suspend" lets Windows put a USB device it thinks is idle into a low-power state. On a rig that
# records for days, the devices that matter (the cameras, a trigger board, the ChronicRecord rig's Pico timer)
# should never be power-managed. With a program holding a device open Windows normally leaves it alone, but
# it costs nothing to rule it out. The ChronicRecord launcher warns at every start while it is on.
#
# Run at the machine (or over Remote Desktop), as administrator:
#   1. Start menu > type "PowerShell" > right-click "Windows PowerShell" > "Run as administrator".
#   2. In that window (the folder is wherever this file is, e.g. your campy checkout):
#        powershell -NoProfile -ExecutionPolicy Bypass -File <campy folder>\tools\windows\disable_usb_selective_suspend.ps1
#
# It takes a few seconds. The window's title shows the step; at the end it says DONE (or FAILED), a big green
# DONE (or red FAILED) is printed, and it beeps. Everything it prints is also saved next to this file as
# disable_usb_selective_suspend.log.
#
# What it changes, and nothing else: in the active power plan, "USB settings > USB selective suspend setting"
# becomes Disabled, plugged in and on battery (the same as Control Panel > Power Options > Change advanced power
# settings). The values it found are saved next to this file in disable_usb_selective_suspend.before.txt.
# Running it again is safe. It doesn't change the per-device "Allow the computer to turn off this device to
# save power" boxes in Device Manager.
#
# To undo: run it again with -Undo (puts back the values in disable_usb_selective_suspend.before.txt).

param([switch]$Undo)
$ErrorActionPreference = 'Stop'
$Started = Get-Date
$Step = 'starting'
$Before = Join-Path $PSScriptRoot 'disable_usb_selective_suspend.before.txt'
$Sub = '2a737441-1930-4402-8d77-b2bebba308a3'     # USB settings
$Setting = '48e6b7a6-50f5-4782-a5d4-53bb8f07e226' # USB selective suspend setting: 0 Disabled, 1 Enabled

try { Start-Transcript -Path (Join-Path $PSScriptRoot 'disable_usb_selective_suspend.log') -Append | Out-Null } catch { }

function Elapsed { $t = (Get-Date) - $Started; '{0}:{1:00}' -f [int][math]::Floor($t.TotalMinutes), $t.Seconds }
function Title($text) { try { $Host.UI.RawUI.WindowTitle = "USB selective suspend: $text" } catch { } }
function Step($text) { $script:Step = $text; Title "$text ($(Elapsed))"; Write-Host ""; Write-Host "== $text" -ForegroundColor Cyan }
function Ok($text) { Write-Host "   OK  $text" -ForegroundColor Green }
function Beep($hz) { try { [console]::Beep($hz, 250); [console]::Beep($hz, 250) } catch { } }
function Fail($text) {
	Title "FAILED at $script:Step"
	Write-Host ""
	Write-Host "   FAILED  $text" -ForegroundColor Red
	Write-Host ""
	Write-Host "   ##########  FAILED  ##########" -ForegroundColor White -BackgroundColor DarkRed
	Write-Host "   Send a photo of this window." -ForegroundColor Red
	Beep 300
	try { Stop-Transcript | Out-Null } catch { }
	Read-Host "Press Enter to close"; exit 1
}
function Values {
	# The active plan's current values: plugged in (AC) and on battery (DC), as 0 or 1
	$q = powercfg /query SCHEME_CURRENT $Sub $Setting | Out-String
	$ac = [regex]::Match($q, 'Current AC Power Setting Index:\s*0x([0-9a-fA-F]+)')
	$dc = [regex]::Match($q, 'Current DC Power Setting Index:\s*0x([0-9a-fA-F]+)')
	if (-not $ac.Success) { Fail "powercfg doesn't list the USB selective suspend setting in the active power plan:`n$q" }
	@([Convert]::ToInt32($ac.Groups[1].Value, 16), $(if ($dc.Success) { [Convert]::ToInt32($dc.Groups[1].Value, 16) } else { $null }))
}
function Name($v) { if ($v -eq 0) { 'Disabled' } elseif ($v -eq 1) { 'Enabled' } elseif ($null -eq $v) { '(none)' } else { "$v" } }
function SetValues($ac, $dc) {
	powercfg /setacvalueindex SCHEME_CURRENT $Sub $Setting $ac | Out-Null
	if ($LASTEXITCODE -ne 0) { Fail "powercfg couldn't set the plugged-in value (exit $LASTEXITCODE)." }
	if ($null -ne $dc) {
		powercfg /setdcvalueindex SCHEME_CURRENT $Sub $Setting $dc | Out-Null
		if ($LASTEXITCODE -ne 0) { Fail "powercfg couldn't set the on-battery value (exit $LASTEXITCODE)." }
	}
	powercfg /setactive SCHEME_CURRENT | Out-Null # applies the changed values now
}

Title "checking"
Write-Host "USB selective suspend. A few seconds. The title shows the step; at the end it says DONE and beeps." -ForegroundColor Cyan
Step "1/3 Check"
$me = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
	Fail "Not running as administrator. Right-click Windows PowerShell > Run as administrator, then run this again."
}
$plan = (powercfg /getactivescheme | Out-String).Trim()
Ok "active plan: $plan"
$now = Values
Ok "USB selective suspend now: plugged in $(Name $now[0]), on battery $(Name $now[1])"

if ($Undo) {
	Step "2/3 Put back the values from before"
	if (-not (Test-Path $Before)) { Fail "$Before is missing, so there is nothing to undo here. Use Control Panel > Power Options instead." }
	$b = (Get-Content $Before | Select-Object -First 1) -split ','
	$dcBack = if ($b[1] -ne '') { [int]$b[1] } else { $null }
	SetValues ([int]$b[0]) $dcBack
	Remove-Item $Before
	$what = 'PUT BACK'
} else {
	Step "2/3 Disable it"
	if ($now[0] -eq 0 -and ($null -eq $now[1] -or $now[1] -eq 0)) {
		Ok "already disabled: nothing to change"
	} else {
		if (-not (Test-Path $Before)) { Set-Content -Path $Before -Value "$($now[0]),$($now[1])" -Encoding ascii }
		SetValues 0 $(if ($null -ne $now[1]) { 0 } else { $null })
	}
	$what = 'DISABLED'
}

Step "3/3 Check"
$after = Values
Ok "USB selective suspend now: plugged in $(Name $after[0]), on battery $(Name $after[1])"
if (-not $Undo -and ($after[0] -ne 0 -or ($null -ne $after[1] -and $after[1] -ne 0))) { Fail "it still reads as enabled after the change." }

Title "DONE ($(Elapsed))"
Write-Host ""
Write-Host "   ##########  DONE: $what  ##########" -ForegroundColor Black -BackgroundColor Green
Write-Host ""
Write-Host "Took: $(Elapsed). The values from before (for -Undo): $(if (Test-Path $Before) { Get-Content $Before } else { 'none saved' })"
Beep 880
try { Stop-Transcript | Out-Null } catch { }
Read-Host "Press Enter to close"
