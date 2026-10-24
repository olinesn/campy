"""
Every JSON file campy writes is strict JSON (campy/strictjson.py): an
infinite or NaN number is the text "Infinity", "-Infinity" or "NaN", and
campy's own readers get the number back.
"""

import io
import json
import math
import os
import re

import numpy as np
import pytest

from campy import strictjson

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def strict_json(text):
	# As a strict reader (JavaScript's JSON.parse, jq) reads it
	def refuse(word):
		raise ValueError("not JSON: " + word)
	return json.loads(text, parse_constant=refuse)


DATA = {"inf": math.inf, "minus": -math.inf, "nan": math.nan, "numpy": [np.float64("inf"), np.float32("nan")],
	"tuple": (1.5, -np.inf), "finite": 2.5, "int": 3, "flag": True, "none": None, "name": "Camera0"}


def test_dumps_writes_the_text_and_loads_gives_the_numbers_back():
	text = strictjson.dumps(DATA, indent=1)
	data = strict_json(text)
	assert data["inf"] == "Infinity" and data["minus"] == "-Infinity" and data["nan"] == "NaN"
	assert data["numpy"] == ["Infinity", "NaN"] and data["tuple"] == [1.5, "-Infinity"]
	assert data["finite"] == 2.5 and data["int"] == 3 and data["flag"] is True and data["none"] is None
	back = strictjson.loads(text)
	assert back["inf"] == math.inf and back["minus"] == -math.inf and math.isnan(back["nan"])
	assert back["numpy"][0] == math.inf and math.isnan(back["numpy"][1]) and back["tuple"] == [1.5, -math.inf]
	assert back["name"] == "Camera0" and back["int"] == 3
	f = io.StringIO()
	strictjson.dump(DATA, f)
	assert strictjson.load(io.StringIO(f.getvalue()))["inf"] == math.inf


def test_a_file_from_before_reads_the_same():
	# Written with Python's json: the bare words
	old = json.dumps({"rate": math.inf, "p50": math.nan})
	assert "Infinity" in old and '"Infinity"' not in old
	back = strictjson.loads(old)
	assert back["rate"] == math.inf and math.isnan(back["p50"])


def test_what_default_makes_is_strict_too():
	class Thing:
		pass
	text = strictjson.dumps({"thing": Thing(), "list": np.array([1.0, np.nan])},
		default=lambda o: o.tolist() if hasattr(o, "tolist") else str(o))
	data = strict_json(text)
	assert data["list"] == [1.0, "NaN"] and data["thing"].startswith("<")


def test_a_number_strict_missed_fails_the_write(monkeypatch):
	monkeypatch.setattr(strictjson, "Strict", lambda data: data)
	with pytest.raises(ValueError, match="JSON compliant"):
		strictjson.dumps({"missed": math.inf})


def test_campy_writes_json_only_through_strictjson():
	# A new json.dump(s) in campy would write Infinity and NaN as bare words
	# again. The few left write only text and whole numbers, or are not JSON
	allowed = {
		("campy/session.py", 'print(json.dumps({"python": sys.executable'), # PROBE: paths and True/False
		("campy/liveexport.py", "return json.dumps(v)"), # a string, quoted for calibration.toml
		}
	found = set()
	for folder, _, names in os.walk(os.path.join(REPO, "campy")):
		for name in names:
			if not name.endswith(".py") or name == "strictjson.py":
				continue
			path = os.path.join(folder, name)
			rel = os.path.relpath(path, REPO).replace(os.sep, "/")
			with open(path, encoding="utf-8") as f:
				for line in f:
					if re.search(r"(?<![\w.])json\.dumps?\(", line):
						found.add((rel, line.strip()))
	unexpected = [f for f in found if not any(f[0] == r and f[1].startswith(start) for r, start in allowed)]
	assert not unexpected


def test_campy_reads_back_the_numbers_it_wrote(tmp_path):
	# Readers of the numbers campy wrote (here: the sweep's table, which the
	# launch check reads as capacity.json) get numbers, as before
	from campy import preflight
	path = tmp_path / "capacity.json"
	with open(path, "w") as f:
		strictjson.dump({"entries": [{"fps": math.inf, "streams": 3}]}, f)
	strict_json(path.read_text())
	assert preflight.LoadCapacity(str(path))["entries"][0]["fps"] == math.inf
