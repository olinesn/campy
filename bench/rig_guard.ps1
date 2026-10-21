# rig_guard.ps1: a PowerShell script takes the rig (rig.py) by itself. Its first line:
#
#   . C:\campy_data\live\rig_guard.ps1 @args; if ($null -ne $RigExit) { exit $RigExit }
#
# Unless the script already runs inside the rig's current job (started by rig.py run, or by a
# script that took the rig), this re-runs it under rig.py: it waits for the rig, runs holding it,
# and leaves <tag>.rig.log beside the script, ending RIG_CLEAN or RIG_CONTAMINATED. The script
# then ends with the run's exit code, which the guard leaves in $RigExit: `exit` inside a
# dot-sourced file ends only that file, hence the second half of the line. (When the script is
# its PowerShell's own, as with powershell -File, the guard ends that PowerShell itself, so a
# script missing the second half can't go on without the rig.) rig.py and rig_windows.py must be
# beside this file.
#   RIG_OWNER   who runs it (the session's name); "unnamed" otherwise
#   RIG_TAG     the run's name; otherwise the script's first argument, or the script's name
#   RIG_NOWAIT=1  exit 75 at once if the rig is busy, instead of waiting
#   RIG_PYTHON  the python.exe that runs rig.py; otherwise python on the PATH
#   RIG_ROOT    the folder of gpu.lock; otherwise C:\campy_data\live
# rig_guard.sh does the same for bash scripts on Linux.
$_rigRoot = if ($env:RIG_ROOT) { $env:RIG_ROOT } else { 'C:\campy_data\live' }
$RigExit = $null
$_rigInJob = $false
if ($env:RIG_JOB) {
	foreach ($_rigTry in 1..20) {  # rig.py may be writing the lock this moment
		try {
			$_rigInJob = [IO.File]::ReadAllText((Join-Path $_rigRoot 'gpu.lock')).Contains("job=$($env:RIG_JOB) ")
			break
		} catch [IO.FileNotFoundException], [IO.DirectoryNotFoundException] {
			break
		} catch {
			Start-Sleep -Milliseconds 100
		}
	}
}
if (-not $_rigInJob) {
	$_rigScript = $MyInvocation.PSCommandPath
	if (-not $_rigScript) { throw 'rig_guard.ps1 is meant as the first command of a script: . <folder>\rig_guard.ps1 @args' }
	$_rigPython = if ($env:RIG_PYTHON) { $env:RIG_PYTHON } else { 'python' }
	if (-not (Get-Command $_rigPython -ErrorAction SilentlyContinue)) {
		throw "rig_guard.ps1: no $_rigPython to run rig.py with; set RIG_PYTHON to a python.exe"
	}
	$_rigTag = if ($env:RIG_TAG) { $env:RIG_TAG } elseif ($args.Count) { [string]$args[0] } else {
		[IO.Path]::GetFileNameWithoutExtension($_rigScript) }
	$_rigOwner = if ($env:RIG_OWNER) { $env:RIG_OWNER } else { 'unnamed' }
	# From a variable, '--' reaches rig.py; typed on the line, PowerShell could take it for itself
	$_rigArgs = @((Join-Path $PSScriptRoot 'rig.py'), 'run', '--log-dir', (Split-Path -Parent $_rigScript),
		$_rigOwner, ($_rigTag -replace '[\\/]', '_'), '--',
		(Get-Process -Id $PID).Path, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $_rigScript) + $args
	& $_rigPython @_rigArgs
	$RigExit = $LASTEXITCODE
	$_rigCl = [Environment]::GetCommandLineArgs()
	for ($_rigI = 1; $_rigI -lt $_rigCl.Count - 1; $_rigI++) {
		if ($_rigCl[$_rigI] -match '^[-/]f(ile)?$') {
			$_rigMain = $_rigCl[$_rigI + 1]
			if (Test-Path -LiteralPath $_rigMain) { $_rigMain = (Resolve-Path -LiteralPath $_rigMain).Path }
			if ($_rigMain -eq $_rigScript) { [Environment]::Exit($RigExit) }
			break
		}
	}
}
