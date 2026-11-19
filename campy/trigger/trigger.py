"""
Trigger nexus
"""

import json, logging, os
from campy import configurator, newfiles, strictjson
from campy.utils.utils import Say
from campy.trigger.arduino import TriggerError, TriggerInterrupted

def ImportTrigger(params):
	# The config check takes arduino and None, in either case, and pico and box (never started here: they need
	# startArduino False)
	if str(params["triggerController"]).lower() in ("arduino", "none"):
		import campy.trigger.arduino as trigger
	else:
		Say('The microcontroller you have selected is not supported.')
	return trigger


def DrivesTrigger(params):
	# campy runs the trigger itself, rather than the rig (startArduino False).
	# triggerController "None" or "none" (the config check takes both) is no trigger.
	return configurator.DrivesTrigger(params)


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


def SaveStart(params, ready, missing, startedNs, error=None, sync=None):
	"""
	trigger.json in the recording folder: whether campy started the trigger,
	and which cameras were already grabbing when it did. campy-align starts
	those cameras at trigger 1 instead of working it out from arrival times.
	startedNs is when the start command went out, on the clock of every
	camera's hosttimes.npy (campy/clock.py); None if campy did not start it.
	error: why the start command could not be sent (startError), if so.
	sync: the sync output's settings, as sent (arduino.SyncRecord): kept in
	sync.json beside it, for campy-syncmatch, and its pin and seed here.
	"""
	started = startedNs is not None
	record = {
		"triggerStarted": started,
		"startedBy": "campy" if DrivesTrigger(params) else "external",
		"readyBeforeTrigger": sorted(ready) if started else [],
		"notReady": dict(missing),
		"startedHostNs": startedNs,
		}
	if error:
		record["startError"] = error
	blinkPin, blinkHz = configurator.BlinkSettings(params)
	if started and blinkPin >= 0:
		record.update(blinkPin=blinkPin, blinkHz=blinkHz) # campy-synctest reads the rate
	os.makedirs(params["videoFolder"], exist_ok=True)
	if started and sync:
		record.update(syncPin=sync["pin"], syncSeed=sync["seed"], syncFile=newfiles.SYNC_FILE)
		with newfiles.Open(os.path.join(params["videoFolder"], newfiles.SYNC_FILE), "w") as f:
			strictjson.dump(sync, f, indent=2)
	path = os.path.join(params["videoFolder"], newfiles.TRIGGER_FILE)
	with newfiles.Open(path, "w") as f: # never over another recording's
		strictjson.dump(record, f, indent=2)
	return path


def StopTriggers(systems, params):
	if DrivesTrigger(params):
		ImportTrigger(params).StopTriggers(systems)