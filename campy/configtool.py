"""
campy-config: look at campy configs without starting any camera.

  campy-config check configs/campy_config_evox2.yaml my_rig.yaml ...
      Reads each config as campy-acquire would, and lists every problem in it:
      unknown keys (with the key probably meant), values of the wrong kind or
      out of range, per-camera lists of the wrong length, keys that were
      renamed. Exit status 1 if any config has a problem.
  campy-config reference
      Every setting, its default and what it takes, as Markdown
      (docs/config-reference.md is this).
"""

import io, sys, argparse, contextlib
from argparse import ArgumentParser
from campy import configurator, configschema


def Check(path):
	# (problems, notes) for one config, as campy-acquire would read it
	notes = io.StringIO()
	try:
		with contextlib.redirect_stdout(notes):
			clargs = configurator.ParseClargs(ArgumentParser(prog="campy-acquire"), [path])
			configurator.ResolveParams(clargs)
		problems = []
	except configurator.ConfigError as e:
		problems = e.problems
	except (OSError, ValueError) as e:
		problems = [str(e)]
	return problems, [line for line in notes.getvalue().splitlines() if line.strip()]


def Main(argv=None):
	parser = ArgumentParser(prog="campy-config", description=__doc__.split("\n\n")[0].strip(),
		formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__.split("\n\n", 1)[1])
	commands = parser.add_subparsers(dest="command", required=True)
	check = commands.add_parser("check", help="list every problem in each config")
	check.add_argument("configs", nargs="+", metavar="config")
	commands.add_parser("reference", help="every setting, as Markdown")
	args = parser.parse_args(argv)

	if args.command == "reference":
		print(configschema.Reference())
		return 0

	failed = 0
	for path in args.configs:
		problems, notes = Check(path)
		print("{}  {}".format("FAIL" if problems else "OK  ", path))
		for line in problems:
			print("    " + line)
		for line in notes:
			print("    note: " + line)
		failed += bool(problems)
	if len(args.configs) > 1:
		print("{} of {} configs OK".format(len(args.configs) - failed, len(args.configs)))
	return 1 if failed else 0


if __name__ == "__main__":
	sys.exit(Main())
