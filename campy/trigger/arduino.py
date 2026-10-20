"""
Module for Python Serial communication with Arduino microcontroller to control trigger rate
Compile campy/trigger/trigger.ino to operate
Inputs (from config.yaml -> params):
	frameRate: rate in frames/sec to trigger
	serialPort: COM port on PC for Arduino, e.g. 'COM3'
Before any camera starts, campy checks that the Arduino answers (OpenTriggers)
"""

import re, serial
import time, logging
from campy import configurator
from campy.utils.utils import Say

# The check: campy sends the stop command and waits for trigger.ino's answer.
# A command that comes while the sketch is still busy with another (below) is
# thrown away unanswered, so campy sends it again every RESEND_S.
CHECK_TIMEOUT_S = 8.0
RESEND_S = 1.0
ANSWER = re.compile(r"Frame rate set to: 0(\.0+)?(?![\d.])")
# After each command, trigger.ino waits a second for one more number (the
# last value ends the command: Stream's 1 s timeout), then 4 s (ResetTimer),
# then throws away whatever came meanwhile. So the start command goes out
# this long after the check's answer, or the sketch never sees it.
START_AFTER_CHECK_S = 5.5


class TriggerError(Exception):
	pass


def Pins(params):
	# One pin can be written without a list (digitalPins: 2), which len() refused
	pins = params["digitalPins"]
	return list(pins) if isinstance(pins, (list, tuple)) else [pins]


def OpenTriggers(systems, params):
	"""
	Open the trigger's serial port and check that the Arduino answers, before
	any camera starts; TriggerError says why not. Its answer replaces the 3 s
	campy used to wait for it blindly. StartTriggers sends the frame rate once
	every camera is grabbing.
	"""
	port = params["serialPort"]
	again = "Unplug its USB cable and plug it back in (or power-cycle it), then run campy again."
	try:
		connection = serial.Serial(port=port, baudrate=115200, timeout=0.1)
	except Exception as e:
		raise TriggerError("Could not open the trigger Arduino's serial port {} (serialPort): {}. Check that it is "
			"plugged in, that serialPort names it, and that no other program (the Arduino IDE's Serial Monitor, "
			"another campy) has it open. {}".format(port, e, again))
	try:
		pins = Pins(params)
		systems["triggerCheckedAt"] = Check(connection, ",".join(str(item) for item in [len(pins)] + pins + [-1, -1, 0]))
	except Exception as e:
		connection.close()
		raise TriggerError("The trigger Arduino on {} did not answer ({}). {}".format(port, e, again))
	systems["serial"] = connection
	Say("Trigger Arduino on {} answered.".format(port))
	return systems


def Check(connection, command, timeout=None, resend=None):
	# Send the stop command until the sketch answers it. Returns when the
	# answer came (time.monotonic): the sketch says it as it reads the command.
	timeout = CHECK_TIMEOUT_S if timeout is None else timeout
	resend = RESEND_S if resend is None else resend
	connection.reset_input_buffer() # what it said before campy asked
	t0 = time.monotonic()
	sent, heard = None, ""
	while True:
		now = time.monotonic()
		if sent is None or now - sent >= resend:
			if now - t0 >= timeout:
				raise TriggerError("no reply to the stop command {} within {:g} s".format(command, timeout))
			connection.write(command.encode())
			sent = now
		heard = (heard + connection.read(256).decode("ascii", "replace"))[-1000:]
		if ANSWER.search(heard):
			return time.monotonic()


def WaitToStart(systems, interrupted=lambda: False):
	# Until the sketch is done with the check's command, START_AFTER_CHECK_S
	# after its answer. False if interrupted meanwhile.
	if "triggerCheckedAt" not in systems:
		return True
	while time.monotonic() < systems["triggerCheckedAt"] + START_AFTER_CHECK_S:
		if interrupted():
			return False
		time.sleep(0.02)
	return True


def StartTriggers(systems, params):
	if "serial" not in systems:
		return systems
	try:
		# Serialize pin length, IDs, frame rate and the test LED's pin and rate (trigger.ino
		# reads them in this order; a sketch from before the test LED ignores the last two)
		frameRate = configurator.TriggerFrameRate(params)
		blinkPin, blinkHz = configurator.BlinkSettings(params)
		pins = Pins(params)
		serialList = [len(pins)] + pins + [frameRate, blinkPin, blinkHz]
		systems["serialCommand"] = serialList
		serialString = ",".join(str(item) for item in serialList)

		# Send command string, noting when, on hosttimes.npy's clock (trigger.json)
		systems["triggerStartedNs"] = time.monotonic_ns()
		systems["serial"].write(serialString.encode())

		Say("Arduino on port {} is triggering pins {} at {} fps{}."\
			.format(params["serialPort"], pins, frameRate,
				", and blinking the test LED on pin {} at {} Hz".format(blinkPin, blinkHz) if blinkPin >= 0 else ""))

	except Exception as e:
		Say("Could not start the trigger on {}: {}".format(params["serialPort"], e))
	return systems


def StopTriggers(systems):
	if "serial" not in systems:
		return
	Say("Closing serial connection...")

	# Repeat the same Pyserial command, except encode frame rate as "-1" and the test LED as off
	if "serialCommand" in systems:
		serialList = list(systems["serialCommand"])[:-3] + [-1, -1, 0]
		serialString = ",".join(str(item) for item in serialList)
		systems["serial"].write(serialString.encode())

	# Close connection
	systems["serial"].close()
