"""
Trigger nexus
"""

import json, logging, os
from campy import configurator

def ImportTrigger(params):
	if params["triggerController"] == "Arduino" or params["triggerController"] == "arduino":
		import campy.trigger.arduino as trigger
	elif params["triggerController"] == "None" or params["triggerController"] == "none":
		import campy.trigger.arduino as trigger
	else:
		print('The microcontroller you have selected is not supported.')
	return trigger


def DrivesTrigger(params):
	# campy runs the trigger itself, rather than the rig (startArduino False)
	return bool(params["startArduino"]) and params["triggerController"] != "None"


def OpenTriggers(systems, params):
	if DrivesTrigger(params):
		systems = ImportTrigger(params).OpenTriggers(systems, params)
	return systems


def StartTriggers(systems, params):
	if DrivesTrigger(params):
		systems = ImportTrigger(params).StartTriggers(systems, params)
	return systems


def SaveStart(params, ready, missing, startedNs):
	"""
	trigger.json in the recording folder: whether campy started the trigger,
	and which cameras were already grabbing when it did. campy-align starts
	those cameras at trigger 1 instead of working it out from arrival times.
	startedNs is time.monotonic_ns() as the start command went out, on the
	clock of every camera's hosttimes.npy; None if campy did not start it.
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
	path = os.path.join(params["videoFolder"], "trigger.json")
	with open(path, "w") as f:
		json.dump(record, f, indent=2)
	return path


def StopTriggers(systems, params):
	if DrivesTrigger(params):
		ImportTrigger(params).StopTriggers(systems)