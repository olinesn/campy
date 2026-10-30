# rig_hold.ps1: hold a Windows test machine for your own work (training a model, a long manual
# session), so that no timing test, rig check, sweep or update starts on it until you are done.
#
# Run it at the machine (or over Remote Desktop), in a window you leave open:
#   powershell -NoProfile -ExecutionPolicy Bypass -File C:\campy_data\live\rig_hold.ps1 -Owner <you> -Tag <what> [-Why "<one line>"]
# or double-click a .bat that runs that line. It takes seconds to start; then it stays open as long as you hold
# the machine. Starting it before or after your work starts makes no difference to your work: it doesn't
# start, stop or touch any program (SLEAP's GUI, training, campy), and needs no Python.
#
# While it is open, rig.py treats the machine as taken: `rig.py status` says
#   rig held: <owner> <tag> since <date time> rigpid=... (held at the screen with rig_hold.ps1: <why>; ...)
# and every rig.py run, rig_guard.ps1 script and rig.py hold waits until it ends. It writes rig.py's lock
# file, C:\campy_data\live\gpu.lock, the way rig.py does, naming this window's PowerShell as the holder.
# If someone else's run holds the machine when you start it, it waits for that run to end and says so.
#
# How to tell where it is: the window's TITLE says WAITING, HELD (with the time held) or RELEASED. While held,
# a big banner says so and the last line shows the time held, updated every second; a line goes to the log
# every 15 minutes. Clicking in the window doesn't pause it. Everything it prints is also saved next to
# this file as <this file's name>.log.
#
# To end the hold: press R in the window, then Y; or close the window. Closing it works too, because rig.py
# counts a lock whose holder has ended as free; R then Y also deletes the file at once and says RELEASED.
#
#   -Owner    who holds the machine (no spaces), e.g. user
#   -Tag      what for (no spaces), e.g. sleap-1.6.5-training
#   -Why      one line shown in the lock and the window
#   -Root     the folder of gpu.lock; otherwise RIG_ROOT, otherwise C:\campy_data\live
#   -Seconds  release by itself after this many seconds, without asking (for tests)

param(
	[string]$Owner = 'user',
	[string]$Tag = 'hold',
	[string]$Why = '',
	[string]$Root = '',
	[int]$Seconds = 0
)
$ErrorActionPreference = 'Stop'
if (-not $Root) { $Root = if ($env:RIG_ROOT) { $env:RIG_ROOT } else { 'C:\campy_data\live' } }
$Owner = ($Owner.Trim() -replace '\s+', '-'); if (-not $Owner) { $Owner = 'user' }
$Tag = ($Tag.Trim() -replace '\s+', '-'); if (-not $Tag) { $Tag = 'hold' }
$Why = $Why -replace '[\r\n=]+', ' '  # no '=': nothing in it that rig.py would read as a pid or a job
$LockPath = Join-Path $Root 'gpu.lock'
$Me = [Diagnostics.Process]::GetCurrentProcess()
$MyStart = $Me.StartTime.ToFileTimeUtc()  # the creation time rig.py compares (GetProcessTimes)
$Interactive = $Seconds -le 0
$Line = $null

# Clicking in a classic console window ("QuickEdit") freezes the script until Esc: turn that off for this window
try {
	Add-Type -Namespace RigHold -Name Console -MemberDefinition @'
[DllImport("kernel32.dll")] public static extern System.IntPtr GetStdHandle(int n);
[DllImport("kernel32.dll")] public static extern bool GetConsoleMode(System.IntPtr h, out uint m);
[DllImport("kernel32.dll")] public static extern bool SetConsoleMode(System.IntPtr h, uint m);
'@
	$h = [RigHold.Console]::GetStdHandle(-10); $m = [uint32]0
	if ([RigHold.Console]::GetConsoleMode($h, [ref]$m)) { [void][RigHold.Console]::SetConsoleMode($h, ($m -band (-bnot 0x40)) -bor 0x80) }
} catch { }
# Ctrl+C asks too, instead of ending the script half-way
try { [Console]::TreatControlCAsInput = $true } catch { }

$LogFile = if ($PSCommandPath) { [IO.Path]::ChangeExtension($PSCommandPath, '.log') } else { Join-Path $Root 'rig_hold.log' }
try { Start-Transcript -Path $LogFile -Append | Out-Null } catch { }

function Now { (Get-Date).ToString('h:mm:ss tt') }
function Held($since) {
	$t = (Get-Date) - $since
	'{0} h {1:00} min' -f [int][math]::Floor($t.TotalHours), $t.Minutes
}
function Title($text) { try { $Host.UI.RawUI.WindowTitle = "Rig hold ($Tag): $text" } catch { } }
function Beep($hz) { try { [console]::Beep($hz, 250); [console]::Beep($hz, 250) } catch { } }
function Status($text) { try { [Console]::Write("`r$text".PadRight(100)) } catch { } }  # in place, not in the log

# rig.py's own guard: an OS lock on byte 0 of gpu.lock.guard (msvcrt.locking there), held between reading
# gpu.lock and replacing it, so this script and a rig.py never both decide the lock is free
function Guarded([scriptblock]$body) {
	$f = [IO.File]::Open("$LockPath.guard", 'OpenOrCreate', 'ReadWrite', 'ReadWrite')
	try {
		while ($true) { try { $f.Lock(0, 1); break } catch [IO.IOException] { Start-Sleep -Milliseconds 50 } }
		try { & $body } finally { try { $f.Unlock(0, 1) } catch { } }
	} finally { $f.Close() }
}
function ReadLock {
	foreach ($i in 1..20) {
		try { return ([IO.File]::ReadAllText($LockPath)).Trim() }
		catch [IO.FileNotFoundException], [IO.DirectoryNotFoundException] { return $null }
		catch { Start-Sleep -Milliseconds 100 }
	}
	return ''
}
function Running([int]$id, [long]$start) {
	$p = Get-Process -Id $id -ErrorAction SilentlyContinue
	if (-not $p) { return $false }
	if ($start -in 0, -1) { return $true }
	try { return $p.StartTime.ToFileTimeUtc() -eq $start } catch { return $true }  # its times can't be read
}
# 'free', 'held' (its rig.py, job or rig_hold still runs), 'stale' (they have ended) or 'hand' (written by hand),
# as rig.py's FileLock.state sees it
function LockState($text) {
	if ($null -eq $text) { return 'free' }
	if ($text -notmatch '\brigpid=(\d+) rigstart=(-?\d+)') {
		$age = 0
		try { $age = ((Get-Date) - (Get-Item -LiteralPath $LockPath).LastWriteTime).TotalSeconds } catch { }
		if ($text -eq '' -and $age -gt 60) { return 'stale' } else { return 'hand' }
	}
	$pids = @(, @([int]$Matches[1], [long]$Matches[2]))
	if ($text -match '\bpid=(\d+) pidstart=(-?\d+)') { $pids += , @([int]$Matches[1], [long]$Matches[2]) }
	foreach ($p in $pids) { if (Running $p[0] $p[1]) { return 'held' } }
	return 'stale'
}
function TryTake {
	$script:took = $false
	$script:busy = $null
	Guarded {
		$text = ReadLock
		$state = LockState $text
		if ($state -eq 'stale') {
			Write-Host "$(Now)  removed a stale gpu.lock (its holder has ended): $text"
			Remove-Item -LiteralPath $LockPath -Force -ErrorAction SilentlyContinue
			$state = 'free'
		}
		if ($state -ne 'free') { $script:busy = $text; return }
		$since = Get-Date
		$note = if ($Why) { "held at the screen with rig_hold.ps1: $Why" } else { 'held at the screen with rig_hold.ps1' }
		$line = '{0} {1} since {2} rigpid={3} rigstart={4} ({5}; it ends when that window closes)' -f $Owner, $Tag,
			$since.ToString('yyyy-MM-dd HH:mm:ss'), $PID, $MyStart, $note
		try {
			$lf = [IO.File]::Open($LockPath, 'CreateNew', 'Write', 'Read')
		} catch [IO.IOException] {
			$script:busy = (ReadLock); return  # written this moment by something without the guard
		}
		try {
			$b = [Text.UTF8Encoding]::new($false).GetBytes("$line`n")
			$lf.Write($b, 0, $b.Length)
		} finally { $lf.Close() }
		$script:Line = $line
		$script:Since = $since
		$script:took = $true
	}
}
function Release {
	if (-not $script:Line) { return }
	Guarded {
		$text = ReadLock
		if ($text -eq $script:Line) { Remove-Item -LiteralPath $LockPath -Force }
		elseif ($null -ne $text) { Write-Host "gpu.lock is no longer this hold's, so it was left as it is: $text" -ForegroundColor Yellow }
	}
	$script:Line = $null
}
function Key {
	if (-not $Interactive) { return $null }
	try { if ([Console]::KeyAvailable) { return [Console]::ReadKey($true) } } catch { }
	return $null
}
function CtrlC($k) { ($k.Key -eq 'C') -and (($k.Modifiers -band [ConsoleModifiers]::Control) -ne 0) }
function Confirm {
	Write-Host ''
	Write-Host 'Release the hold, so that tests can run on this machine again? Press Y to release, any other key to keep it.' -ForegroundColor Yellow
	$k = [Console]::ReadKey($true)
	return $k.Key -eq 'Y'
}

$ended = 'released'
try {
	if (-not (Test-Path -LiteralPath $Root)) { throw "There is no $Root here, so this machine has no rig.py lock to hold." }
	Title 'starting'
	Write-Host "Rig hold: $Owner, $Tag$(if ($Why) { ": $Why" })" -ForegroundColor Cyan
	Write-Host "It keeps rig.py's tests (timing tests, rig checks, sweeps, updates) from starting on this machine while this window is open." -ForegroundColor Cyan
	Write-Host "It doesn't start, stop or touch your own programs." -ForegroundColor Cyan

	# 1. Take the lock, waiting for whoever holds it now
	$waitStart = Get-Date
	$said = $null
	while ($true) {
		TryTake
		if ($took) { break }
		$text = if ($busy) { $busy } else { '(being written)' }
		if ($text -ne $said) {
			Write-Host ''
			Write-Host "$(Now)  The machine is in use: $text" -ForegroundColor Yellow
			Write-Host '          Waiting: the hold starts as soon as that run ends. Starting your own GPU work now would spoil that run.' -ForegroundColor Yellow
			Write-Host '          Press Q to stop waiting.' -ForegroundColor Yellow
			$said = $text
		}
		Title "WAITING for $(($text -split ' ')[0..1] -join ' ') ($(Held $waitStart))"
		Status "  $(Now)  waiting for $(($text -split ' ')[0..1] -join ' ') ($(Held $waitStart))"
		$k = Key
		if ($k -and ($k.Key -eq 'Q' -or (CtrlC $k))) { $ended = 'not taken'; break }
		Start-Sleep -Seconds 2
	}

	# 2. Hold it
	if ($took) {
		Write-Host ''
		Write-Host ''
		Write-Host '   ##########  HELD: tests wait until this window closes  ##########' -ForegroundColor Black -BackgroundColor Green
		Write-Host ''
		Write-Host "   Since:     $($Since.ToString('h:mm tt')) on $($Since.ToString('ddd d MMM'))"
		Write-Host "   rig.py status now says:  rig held: $Line"
		Write-Host '   To end the hold: press R, then Y. Or close this window.' -ForegroundColor Cyan
		Write-Host ''
		Beep 880
		$nextLog = (Get-Date).AddMinutes(15)
		$nextCheck = (Get-Date).AddSeconds(10)
		$stopAt = if ($Seconds -gt 0) { (Get-Date).AddSeconds($Seconds) } else { $null }
		while ($true) {
			Title "HELD $(Held $Since) since $($Since.ToString('h:mm tt')) - press R to release"
			Status "  $(Now)  HELD for $(Held $Since). Press R to release."
			if ($stopAt -and (Get-Date) -ge $stopAt) { break }
			$k = Key
			if ($k -and ($k.Key -eq 'R' -or (CtrlC $k))) {
				if (Confirm) { break }
				Write-Host '   Still held.' -ForegroundColor Green
			}
			if ((Get-Date) -ge $nextCheck) {
				$nextCheck = (Get-Date).AddSeconds(10)
				$text = ReadLock
				if ($text -ne $Line) {
					# Someone deleted or replaced it (by hand: rig.py break refuses a live lock): take it back once free
					Write-Host ''
					Write-Host "$(Now)  gpu.lock was changed by someone else: $(if ($null -eq $text) { '(deleted)' } else { $text })" -ForegroundColor Red
					$Line = $null
					TryTake
					if ($took) { Write-Host "$(Now)  taken back: $Line" -ForegroundColor Green }
					else {
						Write-Host '          Not held any more. Waiting to take it back...' -ForegroundColor Red
						$Line = $null
						while (-not $took) {
							Title "NOT HELD: waiting to take it back"
							Status "  $(Now)  NOT HELD: gpu.lock is someone else's; waiting to take it back"
							Start-Sleep -Seconds 2
							TryTake
						}
						Write-Host "$(Now)  taken back: $Line" -ForegroundColor Green
					}
				}
			}
			if ((Get-Date) -ge $nextLog) {
				$nextLog = (Get-Date).AddMinutes(15)
				Write-Host ''
				Write-Host "$(Now)  still held ($(Held $Since))"
			}
			Start-Sleep -Milliseconds 500
		}
	}
} catch {
	$ended = "failed: $($_.Exception.Message)"
} finally {
	try { Release } catch { $ended = "failed to delete gpu.lock: $($_.Exception.Message)" }
}

Write-Host ''
Write-Host ''
if ($ended -eq 'released') {
	Title "RELEASED (held $(Held $Since))"
	Write-Host '   ##########  RELEASED: tests may run on this machine again  ##########' -ForegroundColor Black -BackgroundColor Yellow
	Write-Host ''
	Write-Host "   Held from $($Since.ToString('h:mm tt')) to $((Get-Date).ToString('h:mm tt')) ($(Held $Since))."
	Beep 440
} elseif ($ended -eq 'not taken') {
	Title 'NOT HELD (stopped waiting)'
	Write-Host '   ##########  NOT HELD: you stopped waiting  ##########' -ForegroundColor White -BackgroundColor DarkRed
	Beep 300
} else {
	Title 'FAILED'
	Write-Host "   ##########  FAILED: $ended  ##########" -ForegroundColor White -BackgroundColor DarkRed
	Write-Host '   The machine is not held. Send a photo of this window.' -ForegroundColor Red
	Beep 300
}
try { Stop-Transcript | Out-Null } catch { }
if ($Interactive) { Read-Host 'Press Enter to close' }
if ($ended -notin 'released', 'not taken') { exit 1 }
