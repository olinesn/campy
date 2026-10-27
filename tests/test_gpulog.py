"""
campy/gpulog.py: the kernel log's AMD GPU messages, for the recording watch.
Made-up kernel log lines, in the amdgpu driver's own formats (Linux 6.17),
sorted into firmware stalls, hung jobs and resets; and the reader, with a
made-up journalctl (a script) and a made-up /dev/kmsg (a pipe): it reads new
lines on its own thread, says when it can't read the log, and goes on.
tests/test_watch.py has the alerts they make.
"""

import os, stat, sys, textwrap, time

import pytest

from campy import gpulog

DEV = "amdgpu 0000:c5:00.0: amdgpu: "

STALLS = [DEV + "Fence fallback timer expired on ring comp_1.0.0",
	DEV + "Fence fallback timer expired on ring vcn_unified_0",
	"[drm] Fence fallback timer expired on ring sdma0"] # older kernels' form
HANGS = [DEV + "ring comp_1.1.0 timeout, signaled seq=40917, emitted seq=40919",
	DEV + "ring gfx_0.0.0 timeout, but soft recovered",
	DEV + "MES failed to respond to msg=REMOVE_QUEUE",
	DEV + "MES(0) failed to respond to msg=SUSPEND (timeout)",
	DEV + "Queue preemption failed for queue with doorbell_id: 1002",
	DEV + "cp queue pipe 3 queue 0 preemption failed",
	DEV + "Failed to evict queues of process pid 4242",
	DEV + "IB test timed out",
	DEV + "[gfxhub] page fault (src_id:0 ring:153 vmid:8 pasid:32774)",
	DEV + "[mmhub] retry page fault (src_id:0 ring:0 vmid:1 pasid:32770)"]
RESETS = [DEV + "Starting comp_1.0.0 ring reset", DEV + "Ring comp_1.0.0 reset succeeded",
	DEV + "Ring vcn_unified_0 reset failed", DEV + "GPU reset(2) succeeded!", DEV + "GPU reset(3) failed with error -110",
	DEV + "GPU reset succeeded, trying to resume", DEV + "GPU reset end with ret = 0", DEV + "GPU Recovery Failed: -110",
	DEV + "ASIC reset failed with error, -110 for drm dev, 0000:c5:00.0", DEV + "VRAM is lost due to GPU reset!",
	DEV + "MES might be in unrecoverable state, issue a GPU reset"]
# What the EVO-X2's kernel logged in 10 days of recordings, live runs and benchmarks: none of it a GPU error
HARMLESS = ["amdgpu: Freeing queue vital buffer 0x7f1c3e400000, queue evicted",
	DEV + "[drm] REG_WAIT timeout 1us * 100000 tries - optc35_disable_crtc line:162",
	"workqueue: amdgpu_device_delay_enable_gfx_off [amdgpu] hogged CPU for >10000us 4 times, consider switching to "
	"WQ_UNBOUND", DEV + "ring comp_1.0.0 uses VM inv eng 4 on hub 0",
	"usb 4-1: new SuperSpeed USB device number 3 using xhci_hcd", "amdgpu: Topology: Add dGPU node [0x1586:0x1002]"]


def test_firmware_stalls():
	assert [gpulog.Classify(line) for line in STALLS] == [(gpulog.STALL, "comp_1.0.0", True),
		(gpulog.STALL, "vcn_unified_0", True), (gpulog.STALL, "sdma0", True)]


def test_hung_jobs():
	found = [gpulog.Classify(line) for line in HANGS]
	assert all(f is not None and f[0] == gpulog.HANG and f[2] for f in found), found
	assert [f[1] for f in found] == ["comp_1.1.0", "gfx_0.0.0", None, None, None, None, None, None, "gfxhub", "mmhub"]


def test_resets():
	found = [gpulog.Classify(line) for line in RESETS]
	assert all(f is not None and f[0] == gpulog.RESET for f in found), found
	# Counted once each: a ring reset by its outcome, a GPU reset by its outcome or failure
	assert [f[2] for f in found] == [False, True, True, True, True, False, False, True, True, False, False]
	assert [f[1] for f in found][:3] == ["comp_1.0.0", "comp_1.0.0", "vcn_unified_0"]


def test_harmless_lines_are_no_gpu_error():
	assert [gpulog.Classify(line) for line in HARMLESS] == [None] * len(HARMLESS)


def test_ring_text():
	assert gpulog.RingText("comp_1.0.0").startswith("comp_1.0.0 (compute: campy-live")
	assert gpulog.RingText("vcn_unified_0") == "vcn_unified_0 (video encoder: the cameras' VA-API encoders)"
	assert gpulog.RingText("odd_ring") == "odd_ring"


def test_amdgpu(tmp_path):
	assert not gpulog.Amdgpu(str(tmp_path))
	drivers = tmp_path / "bus" / "pci" / "drivers"
	for card, driver in (("card0", "i915"), ("card1", "amdgpu")):
		(drivers / driver).mkdir(parents=True, exist_ok=True)
		device = tmp_path / "class" / "drm" / card / "device"
		device.mkdir(parents=True)
		os.symlink(str(drivers / driver), str(device / "driver"))
		assert gpulog.Amdgpu(str(tmp_path)) == (driver == "amdgpu")
	assert gpulog.Wanted(str(tmp_path)) == sys.platform.startswith("linux")


def test_the_command_reads_new_kernel_messages_only():
	assert gpulog.JOURNALCTL[:7] == ["journalctl", "-k", "-f", "-n", "0", "-o", "cat"]


# ---------------------------------------------------------------------- the reader, with a made-up journalctl

needs_posix = pytest.mark.skipif(sys.platform == "win32", reason="journalctl and /dev/kmsg: Linux (and a script here)")


def Journalctl(tmp_path, body):
	# An executable named journalctl that runs body (Python)
	path = tmp_path / "journalctl"
	path.write_text("#!{}\nimport sys, time\n".format(sys.executable) + textwrap.dedent(body))
	path.chmod(path.stat().st_mode | stat.S_IXUSR)
	return [str(path)]


def Wait(until, within=10):
	deadline = time.monotonic() + within
	while time.monotonic() < deadline:
		if until():
			return True
		time.sleep(0.02)
	return False


@needs_posix
def test_it_reads_the_gpu_messages_on_its_own_thread(tmp_path):
	command = Journalctl(tmp_path, """
		lines = {!r}
		time.sleep(2.5) # quiet at first, as journalctl -n 0 is
		for line in lines:
			print(line, flush=True)
		time.sleep(60)
	""".format(HARMLESS + STALLS[:1] + HANGS[:1] + RESETS[:2]))
	reader = gpulog.Reader(command=command, kmsg=str(tmp_path / "no-kmsg")).Start()
	try:
		events = []
		assert Wait(lambda: events.extend(reader.Take()) or len(events) >= 4)
		assert reader.source == "journalctl"
		assert [(kind, ring) for kind, ring, _, _ in events] == [(gpulog.STALL, "comp_1.0.0"),
			(gpulog.HANG, "comp_1.1.0"), (gpulog.RESET, "comp_1.0.0"), (gpulog.RESET, "comp_1.0.0")]
		assert events[0][3] == STALLS[0]
		assert reader.Notes() == [("Watch: reading the kernel log (journalctl) for GPU errors.", False)]
		child = reader.child
	finally:
		reader.Stop()
	assert child.poll() is not None # it ends with the recording
	assert not reader.thread.is_alive() and reader.Notes() == [] # its end, asked for, is no news


@needs_posix
def test_a_journal_it_can_not_read_is_said_once(tmp_path):
	# journalctl run by a user in neither adm nor systemd-journal: its hint, and nothing from the kernel
	command = Journalctl(tmp_path, """
		print("Hint: You are currently not seeing messages from other users and the system.", flush=True)
		print("      Users in groups 'adm', 'systemd-journal' can see all messages.", flush=True)
		time.sleep(60)
	""")
	reader = gpulog.Reader(command=command, kmsg=str(tmp_path / "no-kmsg")).Start()
	try:
		assert Wait(lambda: not reader.thread.is_alive())
		assert reader.source is None and reader.child.poll() is not None
		(note, logged), = reader.Notes()
		assert logged and note.startswith("Watch: can't read the kernel log (journalctl can't read the kernel log: "
			"you are in neither the adm nor the systemd-journal group; ") and "no-kmsg" in note
		assert note.endswith("watched in this recording. " + gpulog.FIX)
		assert "sudo usermod -aG adm $USER" in note
	finally:
		reader.Stop()


@needs_posix
def test_a_journalctl_that_ends_at_once(tmp_path):
	command = Journalctl(tmp_path, """
		print("Failed to open journal: Permission denied", flush=True)
		sys.exit(1)
	""")
	reader = gpulog.Reader(command=command, kmsg=str(tmp_path / "no-kmsg")).Start()
	assert Wait(lambda: not reader.thread.is_alive())
	(note, logged), = reader.Notes()
	assert "journalctl ended at once (exit code 1: Failed to open journal: Permission denied)" in note
	reader.Stop()


@needs_posix
def test_no_journalctl(tmp_path):
	reader = gpulog.Reader(command=[str(tmp_path / "journalctl")], kmsg=str(tmp_path / "no-kmsg")).Start()
	assert Wait(lambda: not reader.thread.is_alive())
	(note, logged), = reader.Notes()
	assert note.startswith("Watch: can't read the kernel log ({} did not start".format(tmp_path / "journalctl"))
	reader.Stop()


@needs_posix
def test_a_journalctl_that_stops_mid_recording_is_said(tmp_path):
	command = Journalctl(tmp_path, """
		time.sleep(2.5)
		print({!r}, flush=True)
		time.sleep(0.2)
		sys.exit(3)
	""".format(STALLS[0]))
	reader = gpulog.Reader(command=command, kmsg=str(tmp_path / "no-kmsg")).Start()
	try:
		assert Wait(lambda: not reader.thread.is_alive())
		assert [kind for kind, _, _, _ in reader.Take()] == [gpulog.STALL]
		assert [note for note, _ in reader.Notes()][1:] == ["Watch: journalctl stopped reading the kernel log (exit "
			"code 3). GPU errors are no longer watched in this recording."]
	finally:
		reader.Stop()


@needs_posix
def test_dev_kmsg_when_journalctl_can_not(tmp_path):
	# /dev/kmsg's records: "priority,sequence,microseconds,flags;text", then key=value lines starting with a space
	kmsg = str(tmp_path / "kmsg")
	os.mkfifo(kmsg)
	writer = os.open(kmsg, os.O_RDWR) # kept open, so the reader never sees the pipe's end
	reader = gpulog.Reader(command=[str(tmp_path / "journalctl")], kmsg=kmsg).Start()
	try:
		assert Wait(lambda: reader.source == kmsg)
		os.write(writer, "4,8812,871234567,-;{}\n SUBSYSTEM=pci\n DEVICE=+pci:0000:c5:00.0\n".format(STALLS[0]).encode())
		os.write(writer, "4,8813,871234570,-;{}\n".format(HARMLESS[0]).encode())
		os.write(writer, "3,8814,871299999,-;{}\n".format(RESETS[3]).encode())
		events = []
		assert Wait(lambda: events.extend(reader.Take()) or len(events) >= 2)
		assert [(kind, ring, line) for kind, ring, _, line in events] == [(gpulog.STALL, "comp_1.0.0", STALLS[0]),
			(gpulog.RESET, None, RESETS[3])]
		assert reader.Notes() == [("Watch: reading the kernel log ({}) for GPU errors.".format(kmsg), False)]
	finally:
		reader.Stop()
		os.close(writer)
	assert not reader.thread.is_alive()
