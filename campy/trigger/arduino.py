"""
Module for Python Serial communication with Arduino microcontroller to control trigger rate
Compile campy/trigger/trigger_arduino.ino to operate
Inputs (from config.yaml -> params):
	frameRate: rate in frames/sec to trigger
	serialPort: COM port on PC for Arduino, e.g. 'COM3'
Currently rely on time delay to allow Arduino to initialize
TODO: Implement interactive communication link with Python and Arduino
E.g. Wait for 'Ready' message from Arduino to Python instead of "dumb" sleep
"""

import serial
import time, logging
from campy import configurator


def OpenTriggers(systems, params):
	# Open the serial connection. campy does this while the cameras open,
	# and sends the frame rate (StartTriggers) once they are all grabbing.
	try:
		systems["serial"] = serial.Serial(\
							port=params["serialPort"],
							baudrate=115200,
							timeout=0.1)

		# This sleep is important. Wait for Arduino to initialize
		time.sleep(3)

	except Exception as e:
		print("Could not open the trigger's serial port {}: {}".format(params["serialPort"], e), flush=True)
	return systems


def StartTriggers(systems, params):
	if "serial" not in systems:
		return systems
	try:
		# Serialize pin length, IDs, frame rate and the test LED's pin and rate (trigger.ino
		# reads them in this order; a sketch from before the test LED ignores the last two)
		frameRate = configurator.TriggerFrameRate(params)
		blinkPin, blinkHz = configurator.BlinkSettings(params)
		serialList = [len(params["digitalPins"])] + list(params["digitalPins"]) + [frameRate, blinkPin, blinkHz]
		systems["serialCommand"] = serialList
		serialString = ",".join(str(item) for item in serialList)

		# Send command string, noting when, on hosttimes.npy's clock (trigger.json)
		systems["triggerStartedNs"] = time.monotonic_ns()
		systems["serial"].write(serialString.encode())

		print("Arduino on port {} is triggering pins {} at {} fps{}."\
			.format(params["serialPort"], params["digitalPins"], frameRate,
				", and blinking the test LED on pin {} at {} Hz".format(blinkPin, blinkHz) if blinkPin >= 0 else ""),
			flush=True)

	except Exception as e:
		print("Could not start the trigger on {}: {}".format(params["serialPort"], e), flush=True)
	return systems


def StopTriggers(systems):
	if "serial" not in systems:
		return
	print("Closing serial connection...")

	# Repeat the same Pyserial command, except encode frame rate as "-1" and the test LED as off
	if "serialCommand" in systems:
		serialList = list(systems["serialCommand"])[:-3] + [-1, -1, 0]
		serialString = ",".join(str(item) for item in serialList)
		systems["serial"].write(serialString.encode())

	# Close connection
	systems["serial"].close()


def ReceiveReadySignal(systems, params):

	return readySignal
