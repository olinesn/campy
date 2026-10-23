"""
campy-session shortcut: a desktop shortcut that opens the panel with a config.

  campy-session shortcut <config>.yaml [--name "Record: 3 cameras"] [--folder <where>] [--key value ...]

Double-clicking it runs, in a window of its own (its console, which shows
the programs' output):

  <this environment's python> -m campy.session panel <config> [--key value ...]

and the panel opens its page in the browser (campy/panel.py). So someone who
does not use a terminal records by double-clicking an icon, pressing Start
and, at the end, Stop. Closing that window closes the panel, never the
recording: the panel runs campy-acquire apart from it, and a panel opened
again finds the recording still running, and can stop it.

- Windows: a .lnk on the desktop (where Windows keeps it, OneDrive's
  included), made with Windows' own WScript.Shell.
- Linux: a .desktop file on the desktop (marked executable; GNOME may ask
  once to "Allow Launching"), and in ~/.local/share/applications, so it is
  in the applications menu too.
- macOS: a .command file on the desktop, which Terminal runs.

The shortcut names this environment's python and the config by their full
paths: make it again after moving either.
"""

import argparse, os, re, shlex, subprocess, sys


def Desktop():
	if sys.platform == "win32":
		try:
			out = subprocess.run(["powershell", "-NoProfile", "-Command", "[Environment]::GetFolderPath('Desktop')"],
				capture_output=True, text=True, timeout=30)
			if out.returncode == 0 and out.stdout.strip():
				return out.stdout.strip()
		except (OSError, subprocess.TimeoutExpired):
			pass
	return os.path.join(os.path.expanduser("~"), "Desktop")


def Python():
	# A python with a console: pythonw has none, and the panel shows the programs' output in it
	exe = sys.executable
	if sys.platform == "win32" and os.path.basename(exe).lower() == "pythonw.exe":
		exe = os.path.join(os.path.dirname(exe), "python.exe")
	return exe


def FileName(name):
	return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", name).strip(" .") or "campy"


def Windows(path, python, arguments, folder, name):
	# A .lnk, made by Windows' WScript.Shell (no package needed); the strings are passed in the environment, so no
	# quoting can go wrong between Python and PowerShell
	script = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:CAMPY_LNK); $s.TargetPath = $env:CAMPY_EXE; "
		"$s.Arguments = $env:CAMPY_ARGS; $s.WorkingDirectory = $env:CAMPY_DIR; $s.Description = $env:CAMPY_NAME; "
		"$s.IconLocation = $env:CAMPY_EXE + ',0'; $s.Save()")
	env = dict(os.environ, CAMPY_LNK=path, CAMPY_EXE=python, CAMPY_ARGS=subprocess.list2cmdline(arguments),
		CAMPY_DIR=folder, CAMPY_NAME=name)
	out = subprocess.run(["powershell", "-NoProfile", "-Command", script], env=env, capture_output=True, text=True,
		timeout=60)
	if out.returncode != 0 or not os.path.isfile(path):
		raise OSError("Windows could not make the shortcut: {}".format((out.stderr or out.stdout).strip()[-500:]))


def Linux(path, python, arguments, folder, name):
	command = " ".join(shlex.quote(a) for a in [python] + arguments)
	text = "\n".join(["[Desktop Entry]", "Type=Application", "Name={}".format(name),
		"Comment=Record with campy: opens campy-session's panel in the browser",
		# Exec's own quoting: each argument in double quotes, with \\, \" and \$ escaped
		"Exec=" + " ".join('"{}"'.format(a.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`"))
			for a in [python] + arguments),
		"Path={}".format(folder), "Terminal=true", "Categories=Science;", ""])
	with open(path, "w") as f:
		f.write(text)
	os.chmod(path, 0o755)
	return command


def Mac(path, python, arguments, folder, name):
	with open(path, "w") as f:
		f.write("#!/bin/sh\n# {}\ncd {} && exec {}\n".format(name, shlex.quote(folder),
			" ".join(shlex.quote(a) for a in [python] + arguments)))
	os.chmod(path, 0o755)


def Make(config, name=None, folder=None, extra=(), python=None):
	"""Makes the shortcut; returns the paths it wrote."""
	config = os.path.abspath(config)
	if not os.path.isfile(config):
		raise OSError("no config {}".format(config))
	name = name or "Record with campy ({})".format(os.path.splitext(os.path.basename(config))[0])
	folder = folder or Desktop()
	os.makedirs(folder, exist_ok=True)
	python = python or Python()
	arguments = ["-m", "campy.session", "panel", config] + list(extra)
	where = os.path.dirname(config) # relative paths in the config (videoFolder) mean what they mean beside it
	paths = []
	if sys.platform == "win32":
		path = os.path.join(folder, FileName(name) + ".lnk")
		Windows(path, python, arguments, where, name)
		paths.append(path)
	elif sys.platform == "darwin":
		path = os.path.join(folder, FileName(name) + ".command")
		Mac(path, python, arguments, where, name)
		paths.append(path)
	else:
		path = os.path.join(folder, FileName(name) + ".desktop")
		Linux(path, python, arguments, where, name)
		paths.append(path)
		menu = os.path.join(os.path.expanduser("~"), ".local", "share", "applications")
		if folder == Desktop():
			os.makedirs(menu, exist_ok=True)
			also = os.path.join(menu, "campy-" + FileName(name).replace(" ", "-") + ".desktop")
			Linux(also, python, arguments, where, name)
			paths.append(also)
	return paths


def Main(argv=None):
	p = argparse.ArgumentParser(prog="campy-session shortcut", description="Make a desktop shortcut that opens "
		"campy-session's panel with this config (python -m pydoc campy.shortcut).",
		epilog="Any other --key value is campy-acquire's, for every recording the shortcut's panel starts.")
	p.add_argument("config", help="the config the panel records with")
	p.add_argument("--name", default="", help="the shortcut's name (default: Record with campy (<config>))")
	p.add_argument("--folder", default="", help="where to put it (default: the desktop)")
	args, rest = p.parse_known_args(argv)
	try:
		paths = Make(args.config, name=args.name or None, folder=args.folder or None, extra=rest)
	except (OSError, subprocess.TimeoutExpired) as e:
		print("campy-session shortcut: {}".format(e), file=sys.stderr)
		return 1
	for path in paths:
		print("Made {}".format(path))
	print("Double-click it to open the panel in the browser; it records with {}.".format(os.path.abspath(args.config)))
	return 0
