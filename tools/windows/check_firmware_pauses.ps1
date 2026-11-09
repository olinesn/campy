# check_firmware_pauses.ps1 - does this PC pause itself in firmware every 15 minutes (Dell's BIOS WMI)?
#
# On Dell PCs, Dell Optimizer's TechHub agent (Dell.TechHub.Instrumentation.SubAgent.exe, service "DellTechHub")
# reads the embedded controller through Dell's BIOS WMI interface (root\dcim\sysman) twice every 15 minutes. Each
# read runs in the BIOS firmware, which can stop every CPU core for up to about a millisecond. On the Dell Precision
# 7875 that slowed campy-live's sets 2-4 times for about 9 s every 15 minutes. See docs/hardware-guide.html, "On a
# Dell PC, Dell's TechHub agent slows live tracking every 15 minutes", and docs/troubleshooting.md. With the service
# stopped AND its helper programs ended, the 9 s stretch was gone; stopping only the service changed nothing.
#
# It changes nothing on the PC. Run it in PowerShell opened as administrator (Windows shows the WMI log to administrators):
#   powershell -NoProfile -ExecutionPolicy Bypass -File <campy folder>\tools\windows\check_firmware_pauses.ps1
#   ... check_firmware_pauses.ps1 -Probe 1200      # also spin 2 CPUs for 20 min and log every whole-PC pause
#
# Without -Probe it takes a few seconds: it lists when Windows last started Dell's BIOS WMI provider
# ("Provider_PlatformBios" in the WMI-Activity log), how far apart those starts are, and whether Dell's agents run.
# With -Probe N it then spins one thread on each of two CPUs (-Cpus, default the last two) for N seconds and logs
# every gap in their loops longer than -ThrUs (default 150 us). A gap on both CPUs that ends at the same instant
# means the whole computer stopped (firmware), not a program or an interrupt on one core. It keeps 2 CPUs busy: on a
# shared rig run it through bench/rig.py, never during a recording or someone else's timing run.
# Its window's TITLE shows the step and the time so far; at the end it prints DONE (or FAILED) and beeps. Everything
# it prints is also saved next to this file as check_firmware_pauses.log (the gaps in check_firmware_pauses.gaps.csv,
# in QueryPerformanceCounter ns, the same clock as campy's host times on Windows).
param([int]$Probe = 0, [string]$Cpus = '', [double]$ThrUs = 150, [int]$Last = 12)
$ErrorActionPreference = 'Stop'
$log = Join-Path $PSScriptRoot 'check_firmware_pauses.log'
$t0 = Get-Date
function Say($s) { $s; Add-Content -Path $log -Value $s }
function Title($s) { try { $Host.UI.RawUI.WindowTitle = '{0} ({1:mm\:ss})' -f $s, ((Get-Date) - $t0) } catch {} }
Set-Content -Path $log -Value "check_firmware_pauses.ps1 $(Get-Date -Format s) on $((Get-CimInstance Win32_ComputerSystem).Model)"
try {
	Title 'WMI log'
	$ev = @(Get-WinEvent -FilterHashtable @{LogName = 'Microsoft-Windows-WMI-Activity/Operational'; Id = 5857} -ErrorAction SilentlyContinue |
		Where-Object { $_.Message -match 'Provider_PlatformBios' } | Sort-Object TimeCreated)
	if (-not $ev.Count) {
		Say "Dell's BIOS WMI provider (Provider_PlatformBios) hasn't started since the WMI-Activity log began: nothing to see here."
	} else {
		Say "Dell's BIOS WMI provider started $($ev.Count) times since $($ev[0].TimeCreated); the last $Last :"
		$prev = $null
		foreach ($e in ($ev | Select-Object -Last $Last)) {
			$gap = if ($prev) { '  (+{0:N1} min)' -f ($e.TimeCreated - $prev).TotalMinutes } else { '' }
			Say ('  {0:yyyy-MM-dd h:mm:ss tt}{1}' -f $e.TimeCreated, $gap); $prev = $e.TimeCreated
		}
	}
	foreach ($n in 'DellTechHub', 'Dell SupportAssist Remediation') {
		$s = Get-Service -Name $n -ErrorAction SilentlyContinue
		Say $(if ($s) { "service $n : $($s.Status), start $($s.StartType)" } else { "service $n : not installed" })
	}
	$p = Get-Process -Name 'Dell.TechHub.Instrumentation.SubAgent' -ErrorAction SilentlyContinue
	Say $(if ($p) { "Dell.TechHub.Instrumentation.SubAgent.exe runs (pid $($p.Id -join ', '))" } else { 'Dell.TechHub.Instrumentation.SubAgent.exe does not run' })
	$hub = Get-Service -Name 'DellTechHub' -ErrorAction SilentlyContinue
	if ($p -and $hub -and $hub.Status -ne 'Running') {
		# seen on the Precision 7875 (7 November 2026): Stop-Service left the helpers running, and they kept reading
		Say '  The DellTechHub service is stopped but this helper still runs, and it still reads every 15 minutes.'
		Say '  To stop the reads until the service starts again: Get-Process Dell.TechHub* | Stop-Process -Force'
	}
	if ($Probe -gt 0) {
		$n = [Environment]::ProcessorCount
		$cpu = if ($Cpus) { [int[]]($Cpus -split ',') } else { @(($n - 4), ($n - 2)) | Where-Object { $_ -ge 0 } }
		Add-Type -TypeDefinition @'
using System; using System.Threading; using System.Diagnostics; using System.Collections.Generic; using System.Runtime.InteropServices;
public static class FirmwarePauseProbe {
	[DllImport("kernel32")] static extern IntPtr GetCurrentThread();
	[DllImport("kernel32")] static extern UIntPtr SetThreadAffinityMask(IntPtr h, UIntPtr m);
	public static List<long[]> Run(int[] cpus, double sec, double thrUs) {
		var res = new List<long[]>(); var lk = new object();
		long thr = (long)(thrUs * Stopwatch.Frequency / 1e6), end = Stopwatch.GetTimestamp() + (long)(sec * Stopwatch.Frequency);
		var ts = new List<Thread>();
		foreach (int c in cpus) { int cpu = c; var t = new Thread(() => {
			SetThreadAffinityMask(GetCurrentThread(), (UIntPtr)(1UL << cpu)); Thread.CurrentThread.Priority = ThreadPriority.Highest;
			var mine = new List<long[]>(); long last = Stopwatch.GetTimestamp(), k = 0;
			while (true) { long now = Stopwatch.GetTimestamp(); if (now - last > thr) mine.Add(new long[] { cpu, last, now - last });
				last = now; if ((++k & 1023) == 0 && now > end) break; }
			lock (lk) res.AddRange(mine); }); t.Start(); ts.Add(t); }
		foreach (var t in ts) t.Join();
		return res;
	}
}
'@
		Say "spinning CPUs $($cpu -join ' and ') for $Probe s, logging gaps over $ThrUs us (started $(Get-Date -Format 'h:mm:ss tt'))"
		Title "probe until $((Get-Date).AddSeconds($Probe).ToString('h:mm:ss tt'))"
		$f = [Diagnostics.Stopwatch]::Frequency
		$q0 = [Diagnostics.Stopwatch]::GetTimestamp(); $w0 = Get-Date
		$r = [FirmwarePauseProbe]::Run($cpu, $Probe, $ThrUs)
		$csv = Join-Path $PSScriptRoot 'check_firmware_pauses.gaps.csv'
		Set-Content $csv 'cpu,start_ns,gap_us'
		$r | Sort-Object { $_[1] } | ForEach-Object { '{0},{1},{2:F1}' -f $_[0], [long]($_[1] * (1e9 / $f)), ($_[2] * 1e6 / $f) } | Add-Content $csv
		# whole-PC pauses: a gap on each CPU, both ending within 20 us
		$ends = @{}; foreach ($g in $r) { $k = [int]$g[0]; if (-not $ends.ContainsKey($k)) { $ends[$k] = @() }; $ends[$k] += , @(($g[1] + $g[2]), $g[2]) }
		$both = @()
		if ($cpu.Count -ge 2) { foreach ($a in @($ends[[int]$cpu[0]])) { foreach ($b in @($ends[[int]$cpu[1]])) {
			if ($a -and $b -and [Math]::Abs($a[0] - $b[0]) -lt 20e-6 * $f) { $both += , @($a[0], [Math]::Min($a[1], $b[1])) } } } }
		Say "$($r.Count) gaps over $ThrUs us; $($both.Count) stopped both CPUs at once:"
		foreach ($x in $both) { Say ('  {0:h:mm:ss.fff tt}  {1,6:N0} us' -f $w0.AddSeconds(($x[0] - $q0) / $f), ($x[1] * 1e6 / $f)) }
	}
	Title 'DONE'; Say ''; Write-Host 'DONE' -ForegroundColor Green; [Console]::Beep(880, 300)
} catch {
	Title 'FAILED'; Say "FAILED: $_"; Write-Host 'FAILED' -ForegroundColor Red; [Console]::Beep(220, 600); exit 1
}
