"""
Trigger nexus
"""

import json, logging, os
from campy import configurator, newfiles
from campy.utils.utils import Say
from campy.trigger.arduino import TriggerError, TriggerInterrupted

def ImportTrigger(params):
	if params["triggerController"] == "Arduino" or params["triggerController"] == "arduino":
		import campy.trigger.arduino as trigger
	elif params["triggerController"] == "None" or params["triggerController"] == "none":
		import campy.trigger.arduino as trigger
	else:
		Say('The microcontroller you have selected is not supported.')
	return trigger


def DrivesTrigger(params):
	# campy runs the trigger itself, rather than the rig (startArduino False)
	return bool(params["startArduino"]) and params["triggerController"] != "None"


def OpenTriggers(systems, params, interrupted=lambda: False):
	# Raises TriggerError when the trigger does not answer, and TriggerInterrupted
	# (one) when interrupted meanwhile: nothing should start then
	if DrivesTrigger(params):
		systems = ImportTrigger(params).OpenTriggers(systems, params, interrupted)
	return systems


def WaitToStart(systems, params, interrupted=lambda: False):
	# Until the trigger can take the start command; False if interrupted meanwhile
	if DrivesTrigger(params):
		return ImportTrigger(params).WaitToStart(systems, interrupted)
	return True


def StartTriggers(systems, params):
	if DrivesTrigger(params):
		systems = ImportTrigger(params).StartTriggers(systems, params)
	return systems


def SaveStart(params, ready, missing, startedNs):
	"""
	trigger.json in the recording folder: whether campy started the trigger,
	and which cameras were already grabbing when it did. campy-align starts
	those cameras at trigger 1 instead of working it out from arrival times.
	startedNs is when the start command went out, on the clock of every
	camera's hosttimes.npy (campy/clock.py); None if campy did not start it.
	"""
	started = startedNs is not None
	record = {
		"triggerStarted": started,
		"startedBy": "campy" if DrivesTrigger(params) else "external",
		"readyBeforeTrigger": sorted(ready) if started else [],
		"notReady": dict(missing),
		"startedHostNs": startedNs,
		}
	blinkPin, blinkHz = configurator.BlinkSettings(params)
	if started and blinkPin >= 0:
		record.update(blinkPin=blinkPin, blinkHz=blinkHz) # campy-synctest reads the rate
	os.makedirs(params["videoFolder"], exist_ok=True)
	path = os.path.join(params["videoFolder"], newfiles.TRIGGER_FILE)
	with newfiles.Open(path, "w") as f: # never over another recording's
		json.dump(record, f, indent=2)
	return path


def StopTriggers(systems, params):
	if DrivesTrigger(params):
		ImportTrigger(params).StopTriggers(systems)