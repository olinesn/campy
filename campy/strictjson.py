"""
JSON that any program can read, for every JSON file campy writes.

JSON has no infinity or NaN. Python's json module writes them anyway, as the
bare words Infinity and NaN, and most other readers refuse the whole file
over one: JavaScript's JSON.parse (so LUC3D, and campy-session's panel page),
jq, Go, Rust, R. campy writes them as the text "Infinity", "-Infinity" and
"NaN", which Python's float() and JavaScript's Number() both read back as
the number:

  dumps, dump   json's, with each infinite or NaN number (Python's or
                numpy's) as that text, and allow_nan=False, so one that is
                missed fails rather than writing a file others cannot read
  loads, load   json's, with that text (the whole string, exactly) as the
                number again: for campy's own readers of the numbers it
                wrote, which then work as they did. A file from before (the
                bare words) reads the same.

The recording's manifests (campy/manifest.py) keep the text when read: they
hold free text (notes), and what reads them copies them on (the .slp's
provenance).

Standard library only: campy-live, in an environment of its own, uses it
too.
"""

import json, math, numbers

TEXT = {"Infinity": math.inf, "-Infinity": -math.inf, "NaN": math.nan}


def NonFinite(x):
	# An infinite or NaN number as campy writes it
	return "NaN" if x != x else "Infinity" if x > 0 else "-Infinity"


def Strict(data):
	"""data with each infinite or NaN number as the text "Infinity", "-Infinity" or "NaN"."""
	if isinstance(data, dict):
		return {NonFinite(k) if isinstance(k, float) and not math.isfinite(k) else k: Strict(v)
			for k, v in data.items()}
	if isinstance(data, (list, tuple)):
		return [Strict(v) for v in data]
	if isinstance(data, numbers.Real) and not isinstance(data, numbers.Integral) and not math.isfinite(data):
		return NonFinite(data)
	return data


def Numbers(data):
	"""data with the text "Infinity", "-Infinity" and "NaN" as the numbers again."""
	if isinstance(data, dict):
		return {k: Numbers(v) for k, v in data.items()}
	if isinstance(data, list):
		return [Numbers(v) for v in data]
	if isinstance(data, str) and data in TEXT:
		return TEXT[data]
	return data


def dumps(obj, **kw):
	default = kw.get("default")
	if default is not None: # what it makes of an object json does not know is made strict too
		kw["default"] = lambda o: Strict(default(o))
	return json.dumps(Strict(obj), allow_nan=False, **kw)


def dump(obj, f, **kw):
	f.write(dumps(obj, **kw))


def loads(text, **kw):
	return Numbers(json.loads(text, **kw))


def load(f, **kw):
	return loads(f.read(), **kw)
