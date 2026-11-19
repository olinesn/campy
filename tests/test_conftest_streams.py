# The test process's own guards (tests/conftest.py): colorama never wraps sys.stdout, and a test that leaves
# sys.stdout replaced has it put back before the next one
import sys

import pytest

LEFT = {}


def test_colorama_never_wraps_the_tests_stdout():
	colorama = pytest.importorskip("colorama")
	before = sys.stdout
	colorama.init()
	colorama.reinit()
	colorama.just_fix_windows_console() if hasattr(colorama, "just_fix_windows_console") else None
	assert sys.stdout is before


@pytest.mark.filterwarnings("ignore:.*left sys.stdout replaced")
def test_a_test_that_replaces_stdout_and_leaves_it():
	LEFT["stream"] = sys.stdout = open(__file__) # a stream the next test must not get


def test_the_next_test_gets_stdout_back():
	if "stream" not in LEFT:
		pytest.skip("the test before ran on another worker")
	assert sys.stdout is not LEFT["stream"] and not sys.stdout.closed
	print("printed fine")
	LEFT["stream"].close()
