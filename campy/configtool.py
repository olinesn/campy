"""
campy-config: look at campy configs without starting any camera.

  campy-config show <config> [--key value ...] [--changed] [--cameras] [--yaml | --json]
      Every setting campy-acquire <config> --key value ... would record with,
      and where each value came from: campy's default, a line of the config or
      of a file it extends, or the command line. --changed shows only what is
      not campy's default; --cameras one column per camera; --yaml the whole
      config as one file with no extends: (campy-acquire records with it as
      with the original); --json the same, for programs.
  campy-config check <config> ...
      Reads each config as campy-acquire would, and lists every problem in it:
      unknown keys (with the key probably meant), values of the wrong kind or
      out of range, per-camera lists of the wrong length, keys that were
      renamed. Exit status 1 if any config has a problem.
  campy-config diff <config a> <config b> [--key value ...]
      The settings whose values differ between two configs, with where each
      came from. Exit status 1 if any differ: a rewritten config (a rig profile
      and a short experiment file) that records exactly as the old one did
      shows no difference.
  campy-config where
      The rig profile folder (extends: <name> is <folder>/<name>.yaml), and
      the profiles in it.
  campy-config new <name> [--from <config>]
      Makes the rig profile <folder>/<name>.yaml, a copy of the config given
      (e.g. configs/campy_config_evox2.yaml), or empty. It never replaces one.
  campy-config reference
      Every setting, its default and what it takes, as Markdown
      (docs/config-reference.md is this).
"""

import os, sys, json, argparse
from argparse import ArgumentParser
from campy import configurator, configschema, strictjson
from campy.configurator import Source, Shown


def Check(path):
	# (problems, notes) for one config, as campy-acquire would read it
	try:
		return [], configurator.Resolve(path).notes
	except configurator.ConfigError as e:
		return e.problems, []
	except (OSError, ValueError) as e:
		return [str(e)], []


def Text(value):
	# A value as the config would write it, on one line
	if isinstance(value, list):
		return "[" + ", ".join(Text(v) for v in value) + "]"
	return str(value)


def Ordered(params):
	# Keys in the reference's order (by section), then any others
	known = [s.name for section in configschema.SECTIONS for s in configschema.SETTINGS
		if s.section == section and s.name in params]
	return known + [key for key in params if key not in known]


def Table(rows):
	# rows of cells, as aligned columns
	widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]) - 1)]
	return "\n".join("  ".join(cell.ljust(w) for cell, w in zip(row, widths)) + "  " + row[-1] for row in rows)


def ShortSources(result):
	# Each setting's source, with a file of the chain named by its file name
	# alone when that is enough to tell them apart (the chain line has the paths)
	names = [os.path.basename(p) for p in result.chain]
	short = {Shown(p): n for p, n in zip(result.chain, names)} if len(set(names)) == len(names) else {}
	def Text(source):
		text = str(source)
		for long, name in short.items():
			if source.kind == Source.FILE and text.startswith(long):
				return name + text[len(long):]
		return text
	return {key: Text(source) for key, source in result.sources.items()}


def Show(result, changed=False, cameras=False):
	params = result.params
	sources = ShortSources(result)
	kinds = result.sources
	keys = [k for k in Ordered(params) if not (changed and kinds[k].kind == Source.DEFAULT)]
	lines = ["chain: " + " -> ".join([Shown(p) for p in result.chain] + ["campy's defaults"])]
	if cameras:
		cams = result.Cameras()
		per_camera = [k for k in keys if configschema.BY_NAME.get(k) is None or
			configschema.BY_NAME[k].per_camera in (configschema.PER_CAMERA, configschema.EVERY_CAMERA)]
		rig = [k for k in keys if k not in per_camera]
		rows = [[""] + [str(c["cameraName"]) for c in cams] + ["source"]]
		rows += [[k] + [Text(c[k]) for c in cams] + [str(sources[k])] for k in per_camera]
		lines += ["", "Each camera", Table(rows)]
		if rig:
			lines += ["", "The whole rig", Table([[k, Text(params[k]), str(sources[k])] for k in rig])]
	else:
		lines += [""] + [Table([[k, Text(params[k]), str(sources[k])] for k in keys])] if keys else []
	for name, values in result.sections.items():
		lines += ["", name + ":", Table([["  " + k, Text(v), str(result.section_sources[name][k])]
			for k, v in values.items()])]
	if result.notes:
		lines += [""] + ["note: " + n for n in result.notes]
	return "\n".join(lines)


def Diff(a, b):
	# (key, value in a, value in b) for every setting that differs
	rows = []
	for key in Ordered(dict(a.params, **b.params)):
		va, vb = a.params.get(key, "(not set)"), b.params.get(key, "(not set)")
		if va != vb:
			rows.append((key, va, vb))
	return rows


def Main(argv=None):
	argv = sys.argv[1:] if argv is None else list(argv)
	parser = ArgumentParser(prog="campy-config", description=__doc__.split("\n\n")[0].strip(),
		formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__.split("\n\n", 1)[1])
	commands = parser.add_subparsers(dest="command", required=True)
	show = commands.add_parser("show", allow_abbrev=False, help="every setting's value and where it came from")
	show.add_argument("config")
	show.add_argument("--changed", action="store_true", help="only the settings that are not campy's default")
	show.add_argument("--cameras", action="store_true", help="one column per camera")
	show.add_argument("--yaml", action="store_true", help="the whole config as one file, with no extends:")
	show.add_argument("--json", action="store_true", help="every setting's value and source, as JSON")
	check = commands.add_parser("check", allow_abbrev=False, help="list every problem in each config")
	check.add_argument("configs", nargs="+", metavar="config")
	diff = commands.add_parser("diff", allow_abbrev=False, help="the settings that differ between two configs")
	diff.add_argument("a")
	diff.add_argument("b")
	commands.add_parser("where", allow_abbrev=False, help="the rig profile folder and its profiles")
	new = commands.add_parser("new", allow_abbrev=False, help="make a rig profile")
	new.add_argument("name")
	new.add_argument("--from", dest="source", help="a config to copy (default: an empty profile)")
	commands.add_parser("reference", allow_abbrev=False, help="every setting, as Markdown")
	# show and diff take campy-acquire's options too: they go to the config
	args, options = parser.parse_known_args(argv)
	if options and args.command not in ("show", "diff"):
		parser.error("unrecognized arguments: " + " ".join(options))

	if args.command == "reference":
		print(configschema.Reference())
		return 0

	if args.command == "where":
		folder = configurator.ProfileFolder()
		profiles = sorted(f[:-5] for f in os.listdir(folder) if f.endswith(".yaml")) if os.path.isdir(folder) else []
		print("rig profiles: {}{}".format(folder, "" if os.path.isdir(folder) else " (not made yet)"))
		for name in profiles:
			print("  extends: {}".format(name))
		return 0

	if args.command == "new":
		if os.path.basename(args.name) != args.name or os.path.splitext(args.name)[1]:
			parser.error("a profile's name is a word, e.g. chronicrecord, not a path")
		path = os.path.join(configurator.ProfileFolder(), args.name + ".yaml")
		if os.path.exists(path):
			print("{} is already there: campy-config new never replaces a profile.".format(path))
			return 1
		if args.source:
			with open(args.source, encoding="utf-8") as f:
				text = f.read()
			if any(line.startswith("extends:") for line in text.splitlines()):
				print("{} extends another file: copy the file it extends instead, or write the profile "
					"by hand.".format(args.source))
				return 1
			head = "# Rig profile {}: this computer's campy settings, copied from {}.\n".format(
				args.name, os.path.basename(args.source))
		else:
			text = ""
			head = "# Rig profile {}: this computer's campy settings. docs/config-reference.md lists every key.\n".format(
				args.name)
		head += "# A config uses them with \"extends: {}\" and lists only what it changes.\n\n".format(args.name)
		os.makedirs(os.path.dirname(path), exist_ok=True)
		with open(path, "x", encoding="utf-8") as f:
			f.write(head + text)
		print("Made {}".format(path))
		problems, _ = Check(path)
		for line in problems:
			print("    " + line)
		return 1 if problems else 0

	try:
		if args.command == "show":
			result = configurator.Resolve(args.config, options)
			if args.json:
				print(strictjson.dumps({"chain": result.chain,
					"settings": result.Effective(), "notes": result.notes}, indent=1))
			elif args.yaml:
				sys.stdout.write(result.ToYaml())
			else:
				print(Show(result, args.changed, args.cameras))
			return 0
		if args.command == "diff":
			a, b = configurator.Resolve(args.a, options), configurator.Resolve(args.b, options)
			rows = Diff(a, b)
			if not rows:
				print("No difference: both record with the same settings.")
				return 0
			sa, sb = ShortSources(a), ShortSources(b)
			table = [["", os.path.basename(args.a), "", os.path.basename(args.b), ""]]
			table += [[k, Text(va), sa.get(k, ""), Text(vb), sb.get(k, "")] for k, va, vb in rows]
			print(Table(table))
			return 1
	except (configurator.ConfigError, OSError) as e:
		print(e)
		return 2

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
