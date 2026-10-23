"""
campy-session shortcut (campy/shortcut.py): a desktop shortcut that opens the
panel with a config, on each system.
"""

import os
import shlex
import subprocess
import sys

import pytest

from campy import shortcut

ODD = "Lab $HOME rig's \"configs\""


def exec_line(path):
	with open(path) as f:
		line = [l for l in f.read().splitlines() if l.startswith("Exec=")][0]
	return line[len("Exec="):]


def unquote_desktop(text):
	# The desktop entry spec's Exec quoting: "..." with \\ \" \$ \` escaped
	out, at = [], 0
	while at < len(text):
		assert text[at] == '"', text
		at += 1
		word = ""
		while text[at] != '"':
			if text[at] == "\\":
				at += 1
			word += text[at]
			at += 1
		out.append(word)
		at += 2 # the closing quote and the space
	return out


def test_a_linux_shortcut_runs_the_panel_with_the_config(tmp_path):
	config = tmp_path / ODD / "3 cameras.yaml"
	config.parent.mkdir()
	config.write_text("numCams: 3\n")
	path = str(tmp_path / "x.desktop")
	shortcut.Linux(path, "/opt/env/bin/python", ["-m", "campy.session", "panel", str(config), "--quality", "24"],
		str(config.parent), "Record: 3 cameras")
	assert os.access(path, os.X_OK)
	text = open(path).read()
	assert "Terminal=true" in text and "Name=Record: 3 cameras" in text and "Path={}".format(config.parent) in text
	assert unquote_desktop(exec_line(path)) == ["/opt/env/bin/python", "-m", "campy.session", "panel", str(config),
		"--quality", "24"]


@pytest.mark.skipif(sys.platform == "win32", reason="a shell script")
def test_a_mac_shortcut_is_a_shell_script_with_the_same_command(tmp_path):
	config = tmp_path / ODD / "c.yaml"
	config.parent.mkdir()
	config.write_text("numCams: 1\n")
	path = str(tmp_path / "x.command")
	shortcut.Mac(path, sys.executable, ["-m", "campy.session", "panel", str(config)], str(config.parent), "n")
	assert os.access(path, os.X_OK)
	assert subprocess.run(["sh", "-n", path]).returncode == 0
	last = open(path).read().splitlines()[-1]
	assert last.startswith("cd ") and " && exec " in last
	assert shlex.split(last.split(" && exec ", 1)[1]) == [sys.executable, "-m", "campy.session", "panel", str(config)]


def test_make_names_it_and_puts_it_where_asked(tmp_path):
	config = tmp_path / "rig.yaml"
	config.write_text("numCams: 1\n")
	paths = shortcut.Make(str(config), folder=str(tmp_path / "desk"))
	assert len(paths) == 1 and os.path.dirname(paths[0]) == str(tmp_path / "desk")
	assert os.path.basename(paths[0]).startswith("Record with campy (rig)")
	with pytest.raises(OSError, match="no config"):
		shortcut.Make(str(tmp_path / "missing.yaml"), folder=str(tmp_path))
	assert shortcut.FileName('a/b:c*"d"') == "a-b-c--d-"


@pytest.mark.skipif(sys.platform != "win32", reason="a Windows .lnk")
def test_a_windows_shortcut(tmp_path):
	config = tmp_path / "rig.yaml"
	config.write_text("numCams: 1\n")
	path, = shortcut.Make(str(config), name="Record: test", folder=str(tmp_path))
	assert path.endswith("Record- test.lnk") and os.path.getsize(path) > 0
	out = subprocess.run(["powershell", "-NoProfile", "-Command", "$s = (New-Object -ComObject WScript.Shell)."
		"CreateShortcut('{}'); $s.TargetPath; $s.Arguments".format(path)], capture_output=True, text=True)
	target, arguments = out.stdout.strip().splitlines()[:2]
	assert target.lower().endswith("python.exe")
	assert arguments == subprocess.list2cmdline(["-m", "campy.session", "panel", str(config)])
