# train_sleap.ps1 - check sleap-nn training configs with campy-train-check, then train them one after another
# with SLEAP's own Python, without the Run Training dialog.
#
# Why not the dialog: on a large project it freezes for minutes before it opens, its Data Pipeline choice is lost,
# and a reused config carries settings it doesn't show (docs/models.md, "A large package from the SLEAP GUI"). The
# configs here are plain sleap-nn YAML files: export them from the dialog ("Save configuration files...") or copy a
# run folder's training_config.yaml, and edit them.
#
# Run at the machine (or over Remote Desktop), in PowerShell. Not over SSH: started from an SSH session, SLEAP's uv
# Python fails with "The path cannot be traversed because it contains an untrusted mount point" (it sits behind a
# folder link that such sessions may not follow).
#   powershell -NoProfile -ExecutionPolicy Bypass -File <campy folder>\tools\windows\train_sleap.ps1 `
#       -Config C:\path\centroid.yaml, C:\path\centered_instance.yaml
# Options:
#   -CheckOnly        check, start nothing
#   -Yes              start even if the check warns (it never starts after a failure)
#   -Labels <.slp>    the labels to check against (default: the first config's train_labels_path)
#   -Like <folders>   earlier runs of the same models, one per config (comma-separated): the check suggests an
#                     early-stopping min_delta from their validation loss
#   -SleapPython      SLEAP's Python (default: the uv tool's, %APPDATA%\uv\tools\sleap\Scripts\python.exe)
#   -CheckCommand     campy-train-check.exe (default: the one on PATH, else in a miniconda3 env with campy)
#
# How to tell where it is: the window's TITLE shows the config, its epoch and the time so far; SLEAP's own progress
# bar runs below. At the end it prints a big green DONE (or red FAILED), the title says so, and it beeps. Clicking
# in the window doesn't pause it. Its steps are saved in train_sleap.log next to this file.
# To stop: press Ctrl+C. The best model so far stays in its run folder.
#
# What it changes: nothing outside the run folders the configs name. It sets HDF5_USE_FILE_LOCKING=FALSE for its
# own processes (several workers read the same .slp). It refuses to start while another SLEAP training runs.

param(
	[Parameter(Mandatory = $true)][string[]]$Config,
	[switch]$CheckOnly,
	[switch]$Yes,
	[string]$Labels = '',
	[string]$Like = '',
	[string]$SleapPython = (Join-Path $env:APPDATA 'uv\tools\sleap\Scripts\python.exe'),
	[string]$CheckCommand = ''
)
$ErrorActionPreference = 'Stop'
$Log = Join-Path $PSScriptRoot 'train_sleap.log'
$Started = Get-Date

try {
	Add-Type -Namespace TrainSleap -Name Console -MemberDefinition @'
[DllImport("kernel32.dll")] public static extern System.IntPtr GetStdHandle(int n);
[DllImport("kernel32.dll")] public static extern bool GetConsoleMode(System.IntPtr h, out uint m);
[DllImport("kernel32.dll")] public static extern bool SetConsoleMode(System.IntPtr h, uint m);
'@
	$h = [TrainSleap.Console]::GetStdHandle(-10); $mode = 0
	if ([TrainSleap.Console]::GetConsoleMode($h, [ref]$mode)) { [void][TrainSleap.Console]::SetConsoleMode($h, ($mode -band (-bnot 0x40)) -bor 0x80) }
} catch {}

function Say($text) {
	$line = "$(Get-Date -Format 'yyyy-MM-dd hh:mm:ss tt') $text"
	Write-Host $line
	try { Add-Content -LiteralPath $Log $line } catch {}
}
function Elapsed { $e = (Get-Date) - $Started; '{0}:{1:00}' -f [int][math]::Floor($e.TotalHours), $e.Minutes }
function Title($text) { try { $host.UI.RawUI.WindowTitle = "Train SLEAP: $text ($(Elapsed))" } catch {} }
function Finish($ok, $text) {
	Say $text
	if ($ok) {
		Title 'DONE'
		Write-Host "`n  ======  DONE  ======`n  $text`n" -ForegroundColor Green
		try { [console]::Beep(880, 300); [console]::Beep(1175, 400) } catch {}
	} else {
		Title 'FAILED'
		Write-Host "`n  ======  FAILED  ======`n  $text`n" -ForegroundColor Red
		try { [console]::Beep(330, 700); [console]::Beep(262, 900) } catch {}
	}
	exit $(if ($ok) { 0 } else { 1 })
}
# The run folder a config trains into (sleap-nn's YAML: trainer_config's ckpt_dir and run_name, two spaces in).
function RunFolder($path) {
	$ckpt = ''; $run = ''
	foreach ($line in Get-Content -LiteralPath $path) {
		if ($line -match '^  ckpt_dir:\s*(.*)$') { $ckpt = $Matches[1].Trim().Trim("'", '"') }
		if ($line -match '^  run_name:\s*(.*)$') { $run = $Matches[1].Trim().Trim("'", '"') }
		if ($line -match '^  max_epochs:\s*(\d+)') { $script:MaxEpochs = $Matches[1] }
	}
	if ($ckpt -and $run -and $run -ne 'null') { return (Join-Path $ckpt $run) }
	return ''
}

Title 'checking'
$Config = @($Config | ForEach-Object { (Resolve-Path -LiteralPath $_).Path })
if (-not (Test-Path -LiteralPath $SleapPython)) { Finish $false "SLEAP's Python isn't at $SleapPython (give -SleapPython)." }
if (-not $CheckCommand) {
	$found = Get-Command campy-train-check.exe -ErrorAction SilentlyContinue
	if ($found) { $CheckCommand = $found.Source }
	else {
		$CheckCommand = @(Get-ChildItem (Join-Path $env:USERPROFILE 'miniconda3\envs\*\Scripts\campy-train-check.exe') -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty FullName)[0]
	}
}
if (-not $CheckCommand -or -not (Test-Path -LiteralPath $CheckCommand)) { Finish $false 'campy-train-check.exe not found (give -CheckCommand, or install a campy that has it).' }
$running = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' or Name='sleap-nn.exe'" | Where-Object { $_.CommandLine -match 'sleap\.cli["\s]+train|sleap-nn(\.exe)?["\s]+train|sleap_nn\.train' })
if ($running.Count) { Finish $false "Another SLEAP training is running ($($running.Count) processes). Wait for it to finish, then run this again." }

$checkArgs = @($Config)
if ($Labels) { $checkArgs += @('--labels', $Labels) }
if ($Like) { $checkArgs += @('--like', $Like) }
Say "Checking: $($Config -join ', ')"
& $CheckCommand @checkArgs
$level = $LASTEXITCODE
if ($level -ge 2) { Finish $false 'The check failed (see above). Nothing was started.' }
if ($CheckOnly) { Write-Host "`nCheck only: nothing was started."; exit $level }
if ($level -eq 1 -and -not $Yes) {
	$answer = Read-Host "`nThe check warned (see above). Start training anyway? (y/N)"
	if ($answer -notmatch '^(y|yes)$') { Finish $false 'Not started: the check warned and you answered no.' }
}

$env:HDF5_USE_FILE_LOCKING = 'FALSE'
foreach ($c in $Config) {
	$name = [IO.Path]::GetFileNameWithoutExtension($c)
	$script:MaxEpochs = '?'
	$folder = RunFolder $c
	Say "Starting $name$(if ($folder) { " -> $folder" })"
	$args2 = @('-m', 'sleap.cli', 'train', '--config-name', $name, '--config-dir', (Split-Path -Parent $c))
	$proc = Start-Process -FilePath $SleapPython -ArgumentList $args2 -WorkingDirectory (Split-Path -Parent $c) -NoNewWindow -PassThru
	$null = $proc.Handle
	$csv = if ($folder) { Join-Path $folder 'training_log.csv' } else { '' }
	while (-not $proc.WaitForExit(30000)) {
		$epoch = 'starting'
		if ($csv -and (Test-Path -LiteralPath $csv)) { $n = @(Get-Content -LiteralPath $csv).Count - 2; if ($n -ge 0) { $epoch = "epoch $n of up to $script:MaxEpochs" } }
		Title "$name, $epoch"
	}
	if ($proc.ExitCode -ne 0) { Finish $false "$name ended with exit code $($proc.ExitCode). See the messages above." }
	Say "Finished $name"
}
Finish $true "Trained $($Config.Count) model(s)."
