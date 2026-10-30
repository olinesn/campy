# exclude_from_search_index.ps1 - keep Windows Search out of campy's recordings and Python environments.
#
# Windows indexes the user folder (C:\Users\<you>) for Start-menu search, and conda keeps its environments there.
# Creating or updating an environment (tens of thousands of files) keeps the indexer busy for hours: on one PC a
# single new file in it made the indexer read about 10 GB of its own index, and bench/rig.py marked timing runs
# RIG_CONTAMINATED. See docs/troubleshooting.md, "Timing tests".
#
# Run at the machine (or over Remote Desktop), as administrator, while nothing is recording or being measured:
#   1. Start menu > type "PowerShell" > right-click "Windows PowerShell" > "Run as administrator".
#   2. In that window (the folder is wherever this file is, e.g. your campy checkout):
#        powershell -NoProfile -ExecutionPolicy Bypass -File <campy folder>\tools\windows\exclude_from_search_index.ps1 -AlsoSoftware
#
# It normally takes under a minute. The window's TITLE shows the step and the time so far; at the end the title
# says DONE (or FAILED), a big green DONE (or red FAILED) is printed, and it beeps. Clicking in the window
# doesn't pause it. Everything it prints is also saved next to this file as exclude_from_search_index.log.
#
# What it changes, and nothing else: it adds "exclude" rules to Windows Search's list of indexed places (the same
# list as Settings > Privacy & security > Searching Windows > Excluded folders), for
#   - the recording folders given with -Folder (default C:\campy_data). Windows doesn't index a folder outside the
#     user folder anyway; the rule keeps it out if "Enhanced" search (the whole disk) is ever turned on. A recording
#     folder inside the user folder (Documents, Videos, the Desktop) is indexed until excluded;
#   - with -AlsoSoftware: miniconda3, anaconda3, venvs and sleap-bench in your user folder, if they exist.
# Your files are not touched, and nothing is deleted from disk. The Windows Search service keeps running, and
# Start-menu search keeps working for everything else. Windows then drops the excluded folders from its index in
# the background: with a large environment folder that is hundreds of thousands of entries, and the indexer reads
# hard for several minutes. Don't measure anything until it has finished.
#
# To undo: run it again with -Undo (removes exactly the rules it added, listed in exclude_from_search_index.added.txt
# next to this file), or remove the folders under Settings > Privacy & security > Searching Windows > Excluded folders.
# Running it again is safe: every step checks first.

param(
	[string[]]$Folder = @('C:\campy_data'),
	[switch]$AlsoSoftware,
	[switch]$Undo
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$Started = Get-Date
$Step = 'starting'
$Added = Join-Path $PSScriptRoot 'exclude_from_search_index.added.txt'

# Clicking in a classic console window ("QuickEdit") freezes the script until Esc: turn that off for this window
try {
	Add-Type -Namespace IdxSetup -Name Console -MemberDefinition @'
[DllImport("kernel32.dll")] public static extern System.IntPtr GetStdHandle(int n);
[DllImport("kernel32.dll")] public static extern bool GetConsoleMode(System.IntPtr h, out uint m);
[DllImport("kernel32.dll")] public static extern bool SetConsoleMode(System.IntPtr h, uint m);
'@
	$h = [IdxSetup.Console]::GetStdHandle(-10); $m = [uint32]0
	if ([IdxSetup.Console]::GetConsoleMode($h, [ref]$m)) { [void][IdxSetup.Console]::SetConsoleMode($h, ($m -band (-bnot 0x40)) -bor 0x80) }
} catch { }

try { Start-Transcript -Path (Join-Path $PSScriptRoot 'exclude_from_search_index.log') -Append | Out-Null } catch { }

function Elapsed { $t = (Get-Date) - $Started; '{0}:{1:00}' -f [int][math]::Floor($t.TotalMinutes), $t.Seconds }
function Title($text) { try { $Host.UI.RawUI.WindowTitle = "Search index: $text" } catch { } }
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

Title "checking"
Write-Host "Windows Search exclusions. Under a minute. The title shows the step; at the end it says DONE and beeps." -ForegroundColor Cyan

$me = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
	Fail "Not running as administrator. Right-click Windows PowerShell > Run as administrator, then run this again."
}

Step "1/3 Connect to Windows Search"
if ((Get-Service WSearch).Status -ne 'Running') { Fail "The Windows Search service (WSearch) isn't running, so there is no index to change. Nothing was changed." }
try {
	Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
namespace CampyIndex {
	// Windows Search COM interfaces (SearchAPI.h / SearchCrawlScopeManager.idl, Windows SDK).
	// Every method is listed in vtable order; only the named ones are called.
	[ComImport, Guid("AB310581-AC80-11D1-8DF3-00C04FB6EF69"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
	public interface ISearchManager {
		void GetIndexerVersionStr([MarshalAs(UnmanagedType.LPWStr)] out string version);
		void GetIndexerVersion(out uint major, out uint minor);
		void GetParameter(); void SetParameter(); void get_ProxyName(); void get_BypassList(); void SetProxy();
		ISearchCatalogManager GetCatalog([MarshalAs(UnmanagedType.LPWStr)] string catalog);
	}
	[ComImport, Guid("AB310581-AC80-11D1-8DF3-00C04FB6EF50"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
	public interface ISearchCatalogManager {
		void get_Name([MarshalAs(UnmanagedType.LPWStr)] out string name);
		void GetParameter(); void SetParameter(); void GetCatalogStatus(); void Reset(); void Reindex();
		void ReindexMatchingURLs(); void ReindexSearchRoot(); void put_ConnectTimeout(); void get_ConnectTimeout();
		void put_DataTimeout(); void get_DataTimeout();
		void NumberOfItems(out int count);
		void NumberOfItemsToIndex(out int incremental, out int notificationQueue, out int highPriorityQueue);
		void URLBeingIndexed(); void GetURLIndexingState(); void GetPersistentItemsChangedSink();
		void RegisterViewForNotification(); void GetItemsChangedSink(); void UnregisterViewForNotification();
		void SetExtensionClusion(); void EnumerateExcludedExtensions(); void GetQueryHelper();
		void put_DiacriticSensitivity(); void get_DiacriticSensitivity();
		ISearchCrawlScopeManager GetCrawlScopeManager();
	}
	[ComImport, Guid("AB310581-AC80-11D1-8DF3-00C04FB6EF55"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
	public interface ISearchCrawlScopeManager {
		void AddDefaultScopeRule(); void AddRoot(); void RemoveRoot(); void EnumerateRoots(); void AddHierarchicalScope();
		void AddUserScopeRule([MarshalAs(UnmanagedType.LPWStr)] string url, int include, int overrideChildren, uint followFlags);
		void RemoveScopeRule([MarshalAs(UnmanagedType.LPWStr)] string rule);
		void EnumerateScopeRules();
		void HasParentScopeRule([MarshalAs(UnmanagedType.LPWStr)] string url, out int hasParent);
		void HasChildScopeRule([MarshalAs(UnmanagedType.LPWStr)] string url, out int hasChild);
		void IncludedInCrawlScope([MarshalAs(UnmanagedType.LPWStr)] string url, out int included);
		void IncludedInCrawlScopeEx([MarshalAs(UnmanagedType.LPWStr)] string url, out int included, out int reason);
		void RevertToDefaultScopes();
		void SaveAll();
	}
	public static class Index {
		static ISearchManager Manager() {
			var t = Type.GetTypeFromCLSID(new Guid("7D096C5F-AC08-4F1F-BEB7-5C22C517CE39"));  // CSearchManager
			return (ISearchManager)Activator.CreateInstance(t);
		}
		static ISearchCrawlScopeManager Scopes() { return Manager().GetCatalog("SystemIndex").GetCrawlScopeManager(); }
		public static string Info() {
			var m = Manager(); string v; m.GetIndexerVersionStr(out v);
			var c = m.GetCatalog("SystemIndex"); string n; c.get_Name(out n); int items; c.NumberOfItems(out items);
			int a, b, q; c.NumberOfItemsToIndex(out a, out b, out q);
			return String.Format("indexer {0}, catalog {1}, {2} items, waiting {3}/{4}/{5}", v, n, items, a, b, q);
		}
		// included: 1 = Windows Search indexes this URL; reason: 0 no rule covers it, 1 a default rule, 2 a user rule, 3 group policy
		public static int[] Check(string url) {
			var s = Scopes(); int inc, why, parent, child;
			s.IncludedInCrawlScopeEx(url, out inc, out why); s.HasParentScopeRule(url, out parent); s.HasChildScopeRule(url, out child);
			return new int[] { inc, why, parent, child };
		}
		public static void Exclude(string url) { var s = Scopes(); s.AddUserScopeRule(url, 0, 0, 1); s.SaveAll(); }
		public static void RemoveRule(string url) { var s = Scopes(); s.RemoveScopeRule(url); s.SaveAll(); }
	}
}
'@
	$info = [CampyIndex.Index]::Info()
} catch { Fail "Couldn't reach Windows Search: $($_.Exception.Message). Nothing was changed." }
Ok $info

function Url($path) { 'file:///' + $path.TrimEnd('\') + '\' }
function State($path) {
	$r = [CampyIndex.Index]::Check((Url $path))
	$why = @{ 0 = 'no rule covers it'; 1 = 'Windows default rule'; 2 = 'user rule'; 3 = 'group policy'; 4 = 'usage rule' }[$r[1]]
	if ($r[0]) { "indexed ($why)" } else { "not indexed ($why)" }
}

if ($Undo) {
	Step "2/3 Remove the rules this script added"
	if (-not (Test-Path $Added)) { Fail "$Added is missing, so there is nothing to undo here. Use Settings > Privacy & security > Searching Windows > Excluded folders instead." }
	foreach ($p in @(Get-Content $Added | Where-Object { $_.Trim() })) {
		try { [CampyIndex.Index]::RemoveRule((Url $p)) } catch { Fail "Couldn't remove the rule for ${p}: $($_.Exception.Message)" }
		Ok "$p : rule removed, now $(State $p)"
	}
	Remove-Item $Added
	Step "3/3 Check"
	Ok "the Windows Search service is still $((Get-Service WSearch).Status)"
	$what = 'UNDONE'
} else {
	Step "2/3 Add the exclusions"
	$targets = @($Folder)
	if ($AlsoSoftware) {
		foreach ($n in 'miniconda3', 'anaconda3', 'venvs', 'sleap-bench') { $targets += Join-Path $env:USERPROFILE $n }
	}
	$done = @(if (Test-Path $Added) { Get-Content $Added | Where-Object { $_.Trim() } })
	foreach ($p in $targets) {
		if (-not (Test-Path -LiteralPath $p)) { Write-Host "   --  $p doesn't exist here: skipped"; continue }
		$before = State $p
		$r = [CampyIndex.Index]::Check((Url $p))
		if (-not $r[0] -and $r[1] -eq 2) { Ok "$p : already excluded by a user rule"; continue }
		try { [CampyIndex.Index]::Exclude((Url $p)) } catch { Fail "Couldn't add the rule for ${p}: $($_.Exception.Message)" }
		if ($done -notcontains $p) { Add-Content -Path $Added -Value $p -Encoding ascii; $done += $p }
		Ok "$p : was $before, now $(State $p)"
	}
	Step "3/3 Check"
	foreach ($p in $targets) {
		if (-not (Test-Path -LiteralPath $p)) { continue }
		$r = [CampyIndex.Index]::Check((Url $p))
		if ($r[0]) { Fail "$p is still indexed after adding the rule ($(State $p))." }
		$f = [CampyIndex.Index]::Check((Url (Join-Path $p 'x.mp4')))
		if ($f[0]) { Fail "a file inside $p would still be indexed ($(State (Join-Path $p 'x.mp4')))." }
		Ok "$p and everything in it: not indexed"
	}
	Ok "the Windows Search service is still $((Get-Service WSearch).Status)"
	$what = 'EXCLUDED'
}

Title "DONE ($(Elapsed))"
Write-Host ""
Write-Host "   ##########  DONE: $what  ##########" -ForegroundColor Black -BackgroundColor Green
Write-Host ""
Write-Host "Took: $(Elapsed). Rules this script has added (and -Undo would remove): $(if (Test-Path $Added) { (Get-Content $Added) -join ', ' } else { 'none' })"
Beep 880
try { Stop-Transcript | Out-Null } catch { }
Read-Host "Press Enter to close"
