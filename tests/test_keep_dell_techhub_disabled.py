"""tools/windows/keep_dell_techhub_disabled.ps1, run in PowerShell (pwsh, or Windows PowerShell) against a pretend
Windows: Get-CimInstance, the service and process commands replaced by functions, so nothing real is stopped. Skipped
where there is no PowerShell, and on a rig (C:\\campy_data: its log is the real one)."""

import json
import os
import shutil
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "tools", "windows", "keep_dell_techhub_disabled.ps1")
POWERSHELL = shutil.which("pwsh") or (shutil.which("powershell") if sys.platform == "win32" else None)

pytestmark = [
	pytest.mark.skipif(POWERSHELL is None, reason="no PowerShell here"),
	pytest.mark.skipif(os.path.isdir(r"C:\campy_data"), reason="a rig: the script would write into its real log"),
]

PRETEND = r"""
$ErrorActionPreference = 'Stop'
$global:killed = @()
function Get-CimInstance { [CmdletBinding()] param($ClassName, $Filter, $Namespace)
	if ($ClassName -eq 'Win32_Service') {
		[pscustomobject]@{ ProcessId = 100; State = 'Running'; StartMode = 'Auto' }
	} else { $global:procs } }
function Set-Service { [CmdletBinding()] param($Name, $StartupType) }
function Stop-Service { [CmdletBinding()] param($Name, [switch]$Force) }
function Stop-Process { [CmdletBinding()] param($Id, [switch]$Force) $global:killed += [int]$Id }
function Get-Service { [CmdletBinding()] param($Name) [pscustomobject]@{ Status = 'Stopped'; StartType = 'Disabled' } }
function Get-Process { [CmdletBinding()] param($Name) }
function Start-Sleep { [CmdletBinding()] param($Seconds) }
function P($id, $parent, $name, $at) { [pscustomobject]@{ ProcessId = $id; ParentProcessId = $parent; Name = $name;
	CreationDate = [datetime]$at } }
$global:procs = @(
	(P 4 0 'System' '2026-11-10T07:59:00'),
	(P 100 50 'Dell.TechHub.exe' '2026-11-10T10:00:00'),             # the service, its id once userinit.exe's
	(P 200 100 'explorer.exe' '2026-11-10T08:00:00'),                # userinit's child: older than DellTechHub
	(P 300 200 'python.exe' '2026-11-10T09:00:00'),                  # a campy recording, started from Explorer
	(P 400 100 'DellOptimizer.exe' '2026-11-10T10:01:00'),           # DellTechHub's own child
	(P 410 400 'Dell.Update.SubAgent.exe' '2026-11-10T10:02:00'),    # and grandchild
	(P 500 4 'Dell.TechHub.Analytics.exe' '2026-11-10T08:30:00')     # by name
)
$out = & $args[0] 2>&1 | ForEach-Object { "$_" }
[pscustomobject]@{ killed = @($global:killed | Sort-Object); out = @($out) } | ConvertTo-Json -Compress
"""


def run(tmp_path, hold=None):
	if hold is not None:
		(tmp_path / "dell_techhub_hold_until.txt").write_text(hold)
	pretend = tmp_path / "pretend.ps1"
	pretend.write_text(PRETEND)
	env = dict(os.environ, PUBLIC=str(tmp_path)) # the script's folder where there is no C:\campy_data
	r = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(pretend),
		SCRIPT], capture_output=True, text=True, env=env, timeout=120)
	assert r.returncode == 0, r.stdout + r.stderr
	got = json.loads(r.stdout.strip().splitlines()[-1])
	return got["killed"], "\n".join(got["out"]), tmp_path / "dell_techhub_disabled.txt"


def test_only_dell_techhubs_own_programs_are_ended(tmp_path):
	# Explorer (and the recording it started) has DellTechHub's process id as its parent's, an id Windows used again:
	# it started before DellTechHub, so it isn't DellTechHub's
	killed, out, log = run(tmp_path)
	assert killed == [100, 400, 410, 500], out
	assert "ended: Dell.TechHub.exe DellOptimizer.exe Dell.Update.SubAgent.exe Dell.TechHub.Analytics.exe" in out
	assert "explorer" not in log.read_text() and "python" not in log.read_text()


def test_an_empty_hold_file_holds_nothing_and_is_said(tmp_path):
	# (Get-Content -Raw of an empty file is $null: .Trim() on it stopped every run, nothing disabled or written)
	killed, out, log = run(tmp_path, hold="")
	assert killed == [100, 400, 410, 500], out
	assert "is empty or not a date ('')" in log.read_text()


def test_a_hold_in_the_future_leaves_it_on(tmp_path):
	killed, out, log = run(tmp_path, hold="2099-01-01T00:00:00\n")
	assert killed == [], out
	assert "held until 2099-01-01T00:00:00" in log.read_text()
