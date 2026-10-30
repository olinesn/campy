# FLIR cameras

What campy does with FLIR (Teledyne) cameras through Spinnaker and PySpin, beyond recording their frames: the USB 2 limit a restart can leave them at, Spinnaker's search for GigE cameras, gamma, and the frame rate, frame size and throughput settings campy sets at every open. Installing Spinnaker and PySpin is in [Installation](install.md#installation).

## FLIR cameras at 43 MB/s after a restart
A Blackfly S sets its USB throughput limit (`DeviceLinkThroughputLimit`) to 43 MB/s whenever it connects at USB 2 speed (FLIR's release notes for firmware 1808, item F-300). A computer's restart leaves its USB ports powered, so a camera that stays plugged in keeps running through it, and the computer's startup connects it at USB 2 and then moves it to USB 3. The camera never restarts, so it keeps the USB 2 limit on its USB 3 link until it is reset or unplugged, although every one of its saved settings says 380 MB/s. A 1280x1024 Mono8 camera then runs at 32.8 fps instead of 170. campy raises such a limit itself (`cameraLinkLimitMBps`), but SpinView and other programs get 43 MB/s.

Measured on a GMKtec EVO-X2 (Ubuntu 24.04, AMI BIOS 1.09) with a BFS-U3-13Y3M on firmware 1808.0.120.0, the newest FLIR lists for it, in October 2026. Whether the camera restarted comes from its `DeviceUptime`:

| | Limit afterwards | Camera restarted |
|---|---|---|
| Restart (`sudo reboot`) | 43 MB/s, all 5 times | no: its uptime ran on through the restart, and it was connected twice before Linux started |
| Its USB 3 port turned off (`usb2-port1/disable`), then on again | 43 MB/s | no: it moved to USB 2 and back |
| Shut down, then switched on | 380 MB/s | yes, at power-on |
| Shut down, power cord out for 10 s, switched on | 380 MB/s | yes, at power-on |
| `DeviceReset`, or unplugging the camera | 380 MB/s | yes |

So on this computer, shutting down and switching on clears it, and a restart does not. A computer that keeps its USB ports powered while shut down (a BIOS setting on some) would keep the camera running through that too.

`campy-flir-reset` resets every FLIR camera that is on a USB 3 link with a limit no higher than USB 2's 480 Mbit/s (60 MB/s). A reset (`DeviceReset`) is the same as unplugging the camera: it restarts with its saved settings, and is back about 8 s later. Cameras on a USB 2 link, and cameras with a higher limit, are left alone. A camera that saved a limit that low is reset too, and comes back with it. On Linux, a systemd service can run it at every startup, before anything opens the cameras, so every program finds the saved limit. Put the campy environment's `bin` folder (`dirname $(which campy-flir-reset)` with the environment active) and your user name in it:
```
sudo tee /etc/systemd/system/campy-flir-reset.service <<'EOF'
[Unit]
Description=Reset FLIR USB3 cameras a restart left at their USB 2 throughput limit
After=systemd-udev-trigger.service

[Service]
Type=oneshot
User=<your user name>
ExecStart=<campy environment>/bin/campy-flir-reset
TimeoutStartSec=120

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl enable --now campy-flir-reset.service
journalctl -b -u campy-flir-reset.service    # what it found and did at this startup
```
The user needs the camera access that Spinnaker's installer sets up on Linux (group `flirimaging`); if a FLIR camera is plugged in but Spinnaker does not see it, the service says so. On the EVO-X2, after each of two restarts, it found the camera at 43 MB/s 7 s into startup and had it back at 380 MB/s by 14.4 s; after both shutdowns it found 380 MB/s and left the camera alone. The login screen came up no later than without it (22.5 s after the kernel started, against 23.3 s). `campy-flir-reset` also runs by hand, on Windows too, but the restart behaviour above was not measured on Windows.

## Faster startup: Spinnaker's search for GigE cameras
Each time a program starts Spinnaker (`PySpin.System.GetInstance`), Spinnaker searches for GigE Vision cameras: it broadcasts on every network interface and waits about 0.25 s on each, one after another, whether or not a GigE camera is there. campy starts Spinnaker in `campy-acquire` and again in each camera process. On a computer whose FLIR cameras are all USB3, that is time spent looking for cameras it does not have. Measured on the EVO-X2 (Ubuntu 24.04, Spinnaker 4.4.0.246, one BFS-U3-13Y3M; three network interfaces: Ethernet, Tailscale and Docker's), in October 2026, with 3 runs each way:

| | Search on | Search off |
|---|---:|---:|
| `System.GetInstance` in `campy-acquire` | 0.95 s | 0.016 s |
| `System.GetInstance` in the camera process | 0.96 s | 0.026 s |
| `campy-acquire` started to ready to trigger | 2.86 s | 0.99 s |
| `System.ReleaseInstance` as `campy-acquire` ends | 0.75 s | 0.013 s |

Every run recorded 400 frames at 80 fps, and everything else (imports, opening the camera, ffmpeg) took the same time either way. The camera processes start Spinnaker at the same time, so with more cameras the search still costs about a second there, not a second per camera: 8 processes at once took 0.96-1.0 s each with the search, and 0.09-0.13 s without.

To turn the search off, with the campy environment active, run this as the user who records (not with `sudo`: the setting is per user). It touches no camera:
```
campy-flir-reset --gige off
```
and to turn it back on:
```
campy-flir-reset --gige on
```
It changes Spinnaker's own setting, `EnumerateGEVInterfaces` (on the System's node map), not campy's. Spinnaker 4.0 and newer keep it. On Linux it is kept for that user, in `~/.config/spinnaker/GenTLSysReg.ini`, so while it is off, no program that user runs finds a GigE camera, SpinView included. On Windows it is kept for the whole computer, in `C:\ProgramData\Spinnaker\Shared\GenTLSysReg.ini`, so every user and every program there stops finding GigE cameras (below). Leave it on for a computer with GigE cameras.

`campy-acquire`'s launch check reports it on a `gige` line: a WARN while Spinnaker searches and every FLIR camera found for the config is USB3, and with the search off, an OK, or a WARN when a FLIR camera was not found, in case it is a GigE camera.

On Windows, it was turned off on 29 October 2026 on the Dell Pro Max Slim FCS1250 (4 FLIR Blackfly S) and the Dell Pro Tower Plus (8 FLIR Blackfly S), both Windows 11 with campy's Spinnaker 4.4.0.246 for Python, through campy's own `SetGige` (what `campy-flir-reset --gige off` runs), after checking that Spinnaker listed every camera as USB3. A new Python process then read the search as off and still found all 4 and all 8 cameras, without opening any of them. Spinnaker writes the setting to `C:\ProgramData\Spinnaker\Shared\GenTLSysReg.ini`, a folder shared by every user of the computer, not to the user's own profile as on Linux. So on Windows it applies to every account there, and to other Spinnaker programs that read that file, SpinView included. On the Pro Tower Plus that includes the installed Spinnaker 4.2.0.88 SDK; whether that SDK reads the file wasn't checked. There `campy-flir-reset` says the search is off "for every user of this computer", and the launch check's `gige` line says "for this computer" where Linux says "for this user". What the search costs at startup on Windows was not measured. The GMKtec EVO-X2 already had it off.

## FLIR gamma (cameraGamma)
`cameraGamma` sets the gamma a FLIR camera applies to each frame, before campy gets it: brightness out = brightness in ^ `cameraGamma`, on a 0-1 scale. Below 1 it brightens the dark and middle grays and leaves black and white where they are, so it does not clip a highlight that was not clipped already. The default is 0.6. `"Off"` turns the camera's gamma off (`GammaEnable` False), so that each gray level is in proportion to the light. One value applies to every camera, or give one per camera (`cameraGamma: [0.6, "Off"]`). A value the camera cannot take is set to the nearest it can, and campy says so. Each camera's `metadata.csv` keeps the value asked for (`cameraGamma`) and the one the camera has (`gamma`). Like gain and exposure, gamma is set at every open and not put back when campy closes the camera, so SpinView then shows campy's gamma. Basler cameras take their gamma from their `.pfs` file.

On the EVO-X2's three Blackfly S BFS-U3-13Y3M (firmware 1808.0.120.0, Spinnaker 4.4) in October 2026, `Gamma` took 0.25 to 4, and could be read or set only while `GammaEnable` was on. All three had their gamma off, from `disableGamma`, and a stored `Gamma` of 0.6. On one camera at 1500 us and 1 dB, gamma 1 gave the same frames as gamma off (mean gray level 176.8 both). At 0.6 the mean rose to 194.9 and the darkest 5% of pixels went from 13 to 42. Both matched what 255 x (level / 255) ^ 0.6 predicts from the gamma-off frames to within 1 gray level. The share of pixels at 255 was 36.2% with gamma off and 36.6% at 0.6. A 3 s `campy-acquire` with no `cameraGamma` in its config said `Gamma set to 0.6.`, and its `metadata.csv` said `gamma` 0.6.

`cameraGamma` replaced `disableGamma`, which campy turned on by default. Until then every FLIR recording was made with the camera's gamma off, unless its config said `disableGamma: False`, which left whatever gamma the camera had. A config that still says `disableGamma` is read as `cameraGamma`: True as `"Off"`, False as 0.6. campy says so as it starts. A config that gives both is refused.

## Frame rate, frame size and USB limit
Without a trigger (`cameraTrigger: "None"`), a FLIR camera free-runs: it times its own frames, and campy sets its `AcquisitionFrameRate` to `frameRate`. Left alone, it would run at whatever rate its saved settings say (a Blackfly S at full frame: 170 fps), while campy still stops after `frameRate` × `recTimeInSec` frames and stamps the video at `frameRate`. campy turns on `AcquisitionFrameRateEnable` (on older Point Grey models such as the Grasshopper3 and Chameleon3, `AcquisitionFrameRateEnabled`, with `AcquisitionFrameRateAuto` Off), sets the rate, reads back the rate the camera will run at, and records it in "metadata.csv" as `resultingFrameRate`. `AcquisitionFrameRate` only caps the rate: an exposure longer than a frame, the camera's USB throughput limit, or its sensor's readout can hold it below `frameRate`, and campy then says which, and at what rate it will record (the launch check stops before that; see [Scaling the number of cameras](encoding.md#scaling-the-number-of-cameras)). When it closes the camera, campy puts back the frame rate settings it found: a camera keeps them until it is unplugged, and with the cap on it drops triggers that come faster, so a triggered recording after a free-running one would be held to its `frameRate`. Before it changes them, campy notes what it found in `~/.campy/flir-frame-rate-<serial>.json`, and removes the note once they are back. If campy stops without closing a camera (a crash, a kill, a restart of the computer, which leaves the camera powered), the next run that opens it, triggered or not, puts them back first and says so (`Camera0: campy did not close it last time and left it capped at 80.0 fps. Cap removed, as before.`); the launch check does this too. A cap with no note was set by another program, or by a campy without these notes, and is left as it is (unplug the camera to clear it): on a triggered camera, the launch check reports it if it is below `frameRate`. Triggered cameras are otherwise left as they are: the trigger sets their rate.

A run that stops without closing a FLIR camera, triggered or not, also leaves it acquiring, and while it acquires, its frame size and pixel format cannot be changed. A later run with a different `frameWidth`, `frameHeight` or `pixelFormatInput` then wrote frames of the old size into a video of the new one, which scrambles it (640x512 asked for after a 1280x1024 run: 10 s at 80 fps came out as 3,200 frames instead of 800). So every open, the launch check's too, stops such an acquisition first and says so (`Camera0: still acquiring from a run that did not close it. Stopped it, so its frame size and pixel format can be set.`).

A triggered camera left acquiring also goes on taking a frame at each trigger pulse until the trigger stops, and keeps the frames nobody read in its own transfer queue, which stopping the acquisition does not empty: the next run's first frames were those. Each later frame of that camera was then numbered one ahead of the other cameras' frames of the same pulse, for the whole recording, which shifts it against the others in `campy-align` and pairs frames of different pulses in `campy-live`'s 3D (seen on the Dell Pro Max Slim FCS1250: a camera delivered 11,741 frames to the others' 11,740, each arriving one frame period after the others' of the same number). When campy starts the trigger itself (`startArduino`, or `cameraTrigger: "Software"`), nothing can come before every camera is ready, so each camera discards whatever arrives between the start of its acquisition and then, and says so (`Camera0: discarded 1 frame it still held from the run that did not close it. ...`). It looks only after such an acquisition was stopped (noted in `~/.campy/flir-leftover-frames-<serial>`, since the launch check usually stops it in another process), so a camera closed cleanly starts as fast as before. With a trigger campy does not start, it cannot tell those frames from the recording's, and says to check `campy-align`'s report or to unplug the camera first.

A FLIR frame smaller than the sensor (`frameWidth`, `frameHeight`) is centred on it. campy moves the frame to the sensor's edge before it sets the size, since a camera's width can reach no further than the sensor's edge from its offset: a smaller centred frame an earlier run left on it (640x512 of 1280x1024) would otherwise hold a full frame to 1120x896. If the camera's frame size still is not `frameWidth` x `frameHeight` (larger than the sensor, or a camera that cannot be resized now), the video would be scrambled, so the launch check stops campy and says what to change.

campy sets a FLIR camera's frame size in steps of 16: it rounds `frameWidth` and `frameHeight` up to multiples of 16, and says so (`Camera0: frameWidth 1000 rounded up to 1008: campy sets FLIR frame sizes in steps of 16.`). It does this before the camera and its video open. It used to happen only as the camera was set up, after the video had opened at 1000 wide, so 1008-wide frames went into it, scrambled.

Every FLIR camera's USB throughput limit (`DeviceLinkThroughputLimit`), triggered or not, is set by `cameraLinkLimitMBps`. A camera's live limit is not always the one it saved: a Blackfly S left plugged in through a computer's restart came up at 43 MB/s instead of its saved 380, which holds full-frame Mono8 to 32.8 fps (see [FLIR cameras at 43 MB/s after a restart](#flir-cameras-at-43-mbs-after-a-restart)). With `"auto"` (the default), campy raises a limit that is less than the camera's frames need at `frameRate` (frame size × frame rate, plus 10%) to the camera's maximum, and says so; a limit that is enough is kept, since a lower one can be on purpose, so that cameras can share a USB bus. A number sets the limit in MB/s, and `"None"` leaves the camera's own limit.
