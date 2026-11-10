# rig_banner.ps1: on an experiment machine, a banner at the top of the screen while a campy test runs,
# with a Stop button (docs/design/rig-fleet.md, section 5).
#
# It runs in the lab account's session, at the machine or over Remote Desktop, started at logon by the
# Task Scheduler task "campy test banner" (bench/rig_experiment_setup.ps1 registers it; rig.py starts the
# task itself when the banner isn't running). It is hidden while no test runs. It is PowerShell, not
# Python, on purpose: rig.py counts every python.exe as a GPU user, so a Python banner would block every
# test and spoil its timing.
#
# It looks at rig.py's lock (C:\campy_data\live\gpu.lock) every second. When a test holds the rig, it shows
#   campy test running: <tag> (by <owner>, since <time>). Touching the mouse or keyboard stops it.  [Stop test]
# and writes "shown=<job>" to the BANNER file there; rig.py starts the test only once it has.
#
# The test is stopped (the banner writes C:\campy_data\live\STOP; rig.py ends every process of the test
# within 2 s) by:
#   - a click on Stop test;
#   - any keyboard or mouse input in this session while the test runs;
#   - unlocking the screen, a Remote Desktop connect (which takes the session over), or a logon;
# and, by rig.py itself, by anything starting to use a camera, the GPU, the encoder or the trigger (campy,
# SpinView) or by quser showing the session in use. Each stop also writes PAUSE: no new test starts until
# 9 AM the next morning (experiment.json's pause_until). After a stop the banner says "Stopped. The machine
# is yours." and offers 2 hours or "until resumed" instead. Nothing here needs admin rights, and the
# banner never ends a process itself: the STOP file is the only thing it touches.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File rig_banner.ps1            the banner (the logon task runs this)
#   powershell -NoProfile -ExecutionPolicy Bypass -File rig_banner.ps1 -Stop      stop the test now and pause tests
#                                                                                (the "Stop campy test" shortcut)
#   powershell -NoProfile -ExecutionPolicy Bypass -File rig_banner.ps1 -Resume    allow tests again (removes PAUSE;
#                                                                                the "Resume campy tests" shortcut)
#
#   -Root     the folder of gpu.lock; otherwise RIG_ROOT, otherwise C:\campy_data\live
#   -How      what -Stop says stopped the test (default: the "Stop campy test" shortcut)
#   -Seconds  the banner ends by itself after this many seconds (for tests)
#
# What it does goes to rig_banner.log beside the lock.

param(
	[string]$Root = '',
	[switch]$Stop,
	[switch]$Resume,
	[string]$How = 'the "Stop campy test" shortcut',
	[int]$Seconds = 0
)
$ErrorActionPreference = 'Stop'
if (-not $Root) { $Root = if ($env:RIG_ROOT) { $env:RIG_ROOT } else { 'C:\campy_data\live' } }
$LockPath = Join-Path $Root 'gpu.lock'
$LogPath = Join-Path $Root 'rig_banner.log'
$PauseUntil = '09:00'
try {
	$cfg = Get-Content -Raw -LiteralPath (Join-Path $Root 'experiment.json') | ConvertFrom-Json
	if ($cfg.pause_until) { $PauseUntil = [string]$cfg.pause_until }
} catch { }

function Log([string]$text) {
	$line = '{0} [{1}] {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $PID, $text
	try { [IO.File]::AppendAllText($LogPath, "$line`r`n") } catch { }
}

function Read-Marker([string]$name) {
	# Opened so that rig.py can still replace or delete the file meanwhile
	$path = Join-Path $Root $name
	try {
		$fs = [IO.FileStream]::new($path, 'Open', 'Read', [IO.FileShare]'ReadWrite, Delete')
		try { return ([IO.StreamReader]::new($fs)).ReadToEnd().Trim() } finally { $fs.Close() }
	} catch [IO.FileNotFoundException] { return $null
	} catch [IO.DirectoryNotFoundException] { return $null
	} catch { return '' }
}

function Write-Marker([string]$name, [string]$line) {
	# Whole or not at all: written beside, then renamed into place
	$path = Join-Path $Root $name
	$tmp = '{0}.{1}.tmp' -f $path, $PID
	[IO.File]::WriteAllText($tmp, "$line`r`n")
	Move-Item -Force -LiteralPath $tmp -Destination $path
}

function Fields([string]$text) {
	$f = @{}
	foreach ($part in ($text -split ';')) {
		$kv = $part -split '=', 2
		if ($kv.Count -eq 2) { $f[$kv[0].Trim()] = $kv[1].Trim() }
	}
	return $f
}

function Next-Morning {
	$h, $m = $PauseUntil -split ':'
	$t = (Get-Date).Date.AddHours([int]$h).AddMinutes([int]$m)
	if ($t -le (Get-Date)) { $t = $t.AddDays(1) }
	return $t
}

function Write-Pause([string]$how, [string]$until, [switch]$Replace) {
	# A stop pauses tests until the next morning, unless a pause is already in place (it may be longer)
	if (-not $Replace -and $null -ne (Read-Marker 'PAUSE')) { return }
	$how = $how -replace '[;\r\n]', ','
	Write-Marker 'PAUSE' ('until={0}; how={1}; at={2}' -f $until, $how, (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
	Log "PAUSE until $until ($how)"
}

function Write-Stop([string]$how) {
	$how = $how -replace '[;\r\n]', ','
	Write-Marker 'STOP' ('how={0}; at={1}' -f $how, (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
	Write-Pause $how ((Next-Morning).ToString('yyyy-MM-dd HH:mm'))
	Log "STOP by $how"
}

function Time-Text([datetime]$t) { return $t.ToString('h:mm tt') }

function Pause-Text {
	$p = Read-Marker 'PAUSE'
	if ($null -eq $p) { return 'New campy tests may start again.' }
	$u = (Fields $p)['until']
	try {
		$t = [datetime]::ParseExact($u, 'yyyy-MM-dd HH:mm', $null)
		$day = if ($t.Date -eq (Get-Date).Date) { 'today' } elseif ($t.Date -eq (Get-Date).Date.AddDays(1)) { 'tomorrow' } else { $t.ToString('ddd d MMM') }
		return 'No new campy tests until {0} {1}.' -f (Time-Text $t), $day
	} catch { return 'No new campy tests until someone resumes them ("Resume campy tests" on the desktop).' }
}

if ($Stop) {
	Write-Stop $How
	Write-Host "STOP written: a running campy test ends within 2 s. $(Pause-Text)"
	exit 0
}
if ($Resume) {
	$p = Join-Path $Root 'PAUSE'
	if (Test-Path -LiteralPath $p) { Remove-Item -Force -LiteralPath $p; Log 'PAUSE removed (resumed)' }
	Write-Host 'campy tests may start again on this machine (when nobody uses it, at the allowed hours).'
	exit 0
}

# One banner per session
$mutex = [Threading.Mutex]::new($false, 'Local\campy-test-banner')
if (-not $mutex.WaitOne(0)) { exit 0 }

Add-Type -ReferencedAssemblies System.Windows.Forms, System.Drawing -TypeDefinition @'
using System;
using System.Collections.Concurrent;
using System.Runtime.InteropServices;
using System.Windows.Forms;
using Microsoft.Win32;
namespace CampyBanner {
	public static class Native {
		[StructLayout(LayoutKind.Sequential)] struct LASTINPUTINFO { public uint cbSize; public uint dwTime; }
		[DllImport("user32.dll")] static extern bool GetLastInputInfo(ref LASTINPUTINFO p);
		[DllImport("kernel32.dll")] static extern bool ProcessIdToSessionId(uint pid, out uint session);
		public static uint LastInput() {
			LASTINPUTINFO i = new LASTINPUTINFO();
			i.cbSize = (uint)Marshal.SizeOf(i);
			return GetLastInputInfo(ref i) ? i.dwTime : 0;
		}
		public static uint Session(uint pid) { uint s; return ProcessIdToSessionId(pid, out s) ? s : 0; }
		// Windows' session events (unlock, Remote Desktop connect, logon) come on another thread: queued here
		// for the banner's timer, since a PowerShell script block can't run on that thread
		public static ConcurrentQueue<string> Events = new ConcurrentQueue<string>();
		static bool hooked;
		public static void Hook() {
			if (hooked) return;
			hooked = true;
			SystemEvents.SessionSwitch += delegate(object s, SessionSwitchEventArgs e) { Events.Enqueue(e.Reason.ToString()); };
		}
	}
	// On top of every window, never taking the keyboard from what the person is doing, not in Alt+Tab
	public class BannerForm : Form {
		protected override bool ShowWithoutActivation { get { return true; } }
		protected override CreateParams CreateParams {
			get { CreateParams cp = base.CreateParams; cp.ExStyle |= 0x08000000 | 0x00000080 | 0x00000008; return cp; }
		}
	}
}
'@
[CampyBanner.Native]::Hook()
$Session = [CampyBanner.Native]::Session([uint32]$PID)

$form = [CampyBanner.BannerForm]::new()
$form.FormBorderStyle = 'None'
$form.TopMost = $true
$form.ShowInTaskbar = $false
$form.StartPosition = 'Manual'
$screen = [Windows.Forms.Screen]::PrimaryScreen.WorkingArea
$form.Bounds = [Drawing.Rectangle]::new($screen.Left, $screen.Top, $screen.Width, 56)
$form.BackColor = [Drawing.Color]::FromArgb(255, 176, 0)
$label = [Windows.Forms.Label]::new()
$label.AutoSize = $false
$label.Font = [Drawing.Font]::new('Segoe UI', 12, [Drawing.FontStyle]::Bold)
$label.ForeColor = [Drawing.Color]::Black
$label.TextAlign = 'MiddleLeft'
$label.Dock = 'Fill'
$label.Padding = [Windows.Forms.Padding]::new(12, 0, 0, 0)
$buttons = [Windows.Forms.FlowLayoutPanel]::new()
$buttons.Dock = 'Right'
$buttons.AutoSize = $true
$buttons.WrapContents = $false
$buttons.Padding = [Windows.Forms.Padding]::new(0, 10, 8, 0)
function New-Button([string]$text, [scriptblock]$onClick) {
	$b = [Windows.Forms.Button]::new()
	$b.Text = $text
	$b.AutoSize = $true
	$b.Height = 34
	$b.Font = [Drawing.Font]::new('Segoe UI', 11, [Drawing.FontStyle]::Bold)
	$b.BackColor = [Drawing.Color]::White
	$b.add_Click($onClick)
	return $b
}
$form.Controls.Add($label)
$form.Controls.Add($buttons)

$script:Job = $null        # the job shown
$script:Stopping = $false  # a stop was asked for (by anyone) while it ran
$script:Input0 = 0         # the last input's tick when the test was shown
$script:ShownAt = $null
$script:HideAt = $null
$script:Ends = if ($Seconds -gt 0) { (Get-Date).AddSeconds($Seconds) } else { $null }

function Write-Banner([string]$shown) {
	Write-Marker 'BANNER' ('pid={0}; session={1}; shown={2}; at={3}' -f $PID, $Session, $shown, (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
}

function Show-Stopped([string]$how, [bool]$ended) {
	$script:StopHow = $how
	$buttons.Controls.Clear()
	$label.Text = if ($ended) { "Stopped ($how). The machine is yours. $(Pause-Text)" } else { "Stopping the campy test ($how)..." }
	if ($ended) {
		$form.BackColor = [Drawing.Color]::FromArgb(140, 220, 140)
		$buttons.Controls.Add((New-Button 'Pause 2 hours instead' {
			Write-Pause 'the banner (2 hours)' ((Get-Date).AddHours(2).ToString('yyyy-MM-dd HH:mm')) -Replace
			Show-Stopped $script:StopHow $true }))
		$buttons.Controls.Add((New-Button 'Pause until resumed' {
			Write-Pause 'the banner (until resumed)' 'resume' -Replace
			Show-Stopped $script:StopHow $true }))
		$buttons.Controls.Add((New-Button 'Close' { $form.Hide(); $script:HideAt = $null }))
		$script:HideAt = (Get-Date).AddMinutes(10)
	}
	if (-not $form.Visible) { $form.Show() }
	Log "says: $($label.Text)"
}

function Stop-Test([string]$how) {
	if (-not $script:Job -or $script:Stopping) { return }
	$script:Stopping = $true
	$script:StopHow = $how
	Write-Stop $how
	Show-Stopped $how $false
}

function Show-Test([string]$line, [string]$job) {
	$script:Job = $job
	$script:Stopping = $false
	$script:Input0 = [CampyBanner.Native]::LastInput()
	$script:ShownAt = Get-Date
	$script:HideAt = $null
	$parts = $line -split ' '
	$since = if ($line -match ' since (\S+ \S+)') { try { Time-Text ([datetime]::Parse($Matches[1])) } catch { $Matches[1] } } else { '?' }
	$form.BackColor = [Drawing.Color]::FromArgb(255, 176, 0)
	$label.Text = 'campy test running: {0} (by {1}, since {2}). Touching the mouse or keyboard stops it.' -f $parts[1], $parts[0], $since
	$buttons.Controls.Clear()
	$buttons.Controls.Add((New-Button 'Stop test' { Stop-Test 'the Stop button on the banner' }))
	$form.Show()
	$form.Refresh()
	Write-Banner $job
	Log "showing $job ($line); says: $($label.Text)"
}

function Test-Ended {
	$job, $how = $script:Job, $script:StopHow
	$script:Job = $null
	Write-Banner '-'
	if ($script:Stopping) {
		Show-Stopped $how $true
		Log "$job stopped ($how)"
		return
	}
	# Stopped by rig.py itself (a camera opened, campy started)? Its PAUSE is newer than the banner
	$p = Read-Marker 'PAUSE'
	$at = if ($p) { (Fields $p)['at'] } else { $null }
	[datetime]$t = [datetime]::MinValue
	if ($at -and [datetime]::TryParse($at, [ref]$t) -and $t -ge $script:ShownAt.AddSeconds(-1)) {
		Show-Stopped (Fields $p)['how'] $true
		Log "$job stopped by rig.py ($((Fields $p)['how']))"
		return
	}
	$form.Hide()
	Log "$job ended"
}

function Current-Job {
	# (line, job) of a test holding the rig, or $null: a rig.py run or hold, not a person's rig_hold.ps1 or an install
	$line = Read-Marker 'gpu.lock'
	if ($line -and $line -notmatch 'rig_hold\.ps1' -and $line -match '\bjob=(\S+)') { return @($line, $Matches[1]) }
	return $null
}

$timer = [Windows.Forms.Timer]::new()
$timer.Interval = 1000
$timer.add_Tick({
	try {
		$e = $null
		while ([CampyBanner.Native]::Events.TryDequeue([ref]$e)) {
			Log "session event: $e"
			$what = @{ SessionUnlock = 'unlocking the screen'; RemoteConnect = 'a Remote Desktop connect';
				ConsoleConnect = 'someone at the machine connecting'; SessionLogon = 'a logon' }[$e]
			if ($what) { Stop-Test $what }
		}
		$cur = Current-Job
		if ($cur -and $cur[1] -ne $script:Job) {
			if ($script:Job) { Test-Ended }
			Show-Test $cur[0] $cur[1]
		} elseif (-not $cur -and $script:Job) {
			Test-Ended
		}
		if ($script:Job -and -not $script:Stopping -and [CampyBanner.Native]::LastInput() -ne $script:Input0) {
			Stop-Test 'keyboard or mouse input at the machine'
		}
		if ($script:Job -and -not $script:Stopping) { $form.TopMost = $true }
		if ($script:HideAt -and (Get-Date) -ge $script:HideAt) { $form.Hide(); $script:HideAt = $null }
		if ($script:Ends -and (Get-Date) -ge $script:Ends) { $timer.Stop(); $form.Hide(); [Windows.Forms.Application]::ExitThread() }
	} catch {
		Log "error: $_"
	}
})

Log "banner started in session $Session"
Write-Banner '-'
# Started while a test runs: someone logged on (the logon task starts the banner), or the banner was restarted
$cur = Current-Job
if ($cur) {
	Show-Test $cur[0] $cur[1]
	Stop-Test 'a logon while the test ran'
}
$timer.Start()
try {
	[Windows.Forms.Application]::Run()
} finally {
	try { Write-Marker 'BANNER' ('pid=; session={0}; shown=-; at={1}; ended' -f $Session, (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')) } catch { }
	Log 'banner ended'
	$mutex.ReleaseMutex()
}
