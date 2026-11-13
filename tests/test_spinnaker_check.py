"""tools/spinnaker_check.py: SHA-256 checks of Spinnaker and PySpin files."""
import hashlib
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "tools"))

import spinnaker_check as sc  # noqa: E402

NAME = "spinnaker_python-9.9.9.9-cp312-cp312-win_amd64.whl"
DATA = b"not really a wheel\n" * 1000


@pytest.fixture
def fake_known(monkeypatch):
    known = {NAME: hashlib.sha256(DATA).hexdigest()}
    monkeypatch.setattr(sc, "KNOWN", known)
    return known


def test_known_table_is_well_formed():
    assert sc.KNOWN, "the table is empty"
    for name, h in sc.KNOWN.items():
        assert sc.PINNED in name, name
        assert sc.is_spinnaker_file(name), name
        assert len(h) == 64 and all(c in "0123456789abcdef" for c in h), name


def test_ok(tmp_path, fake_known):
    f = tmp_path / NAME
    f.write_bytes(DATA)
    assert sc.check(str(f))[0] == "OK"
    assert sc.main([str(f)]) == 0


def test_mismatch(tmp_path, fake_known, capsys):
    f = tmp_path / NAME
    f.write_bytes(DATA + b"x")
    status, msg = sc.check(str(f))
    assert status == "MISMATCH" and "Don't install it" in msg
    assert sc.main([str(f)]) == 1


def test_unlisted_and_missing(tmp_path, fake_known):
    f = tmp_path / "SpinnakerSDK_FULL_1.0.0.1_x64.exe"
    f.write_bytes(b"x")
    assert sc.check(str(f))[0] == "UNLISTED"
    assert sc.main([str(f)]) == 2
    assert sc.check(str(tmp_path / NAME))[0] == "MISSING"
    assert sc.main([str(tmp_path / NAME)]) == 2


def test_folder_checks_only_spinnaker_files(tmp_path, fake_known, capsys):
    (tmp_path / NAME).write_bytes(DATA)
    (tmp_path / "SpinnakerSDK_FULL_1.0.0.1_x64.exe").write_bytes(b"x")
    (tmp_path / "notes.txt").write_text("hi")
    (tmp_path / "other.whl").write_bytes(b"x")
    assert sc.main([str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert f"{NAME}: OK" in out and "not listed" in out
    assert "notes.txt" not in out and "other.whl" not in out


def test_empty_folder(tmp_path):
    assert sc.main([str(tmp_path)]) == 2


def test_list(capsys):
    assert sc.main(["--list"]) == 0
    out = capsys.readouterr().out
    for name, h in sc.KNOWN.items():
        assert f"{h}  {name}" in out
