"""
Trigger nexus
"""

import logging

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


def StopTriggers(systems, params):
	if DrivesTrigger(params):
		ImportTrigger(params).StopTriggers(systems)