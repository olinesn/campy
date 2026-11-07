# Installation

How to install campy for recording, with each camera maker's software. Tracking animals live needs a second Python environment, with PyTorch and sleap-nn: [Live tracking](live-tracking.md#two-python-environments) says how, and the [quickstart](quickstart.md) does both in a few minutes.

1. Update graphics drivers
2. Create and activate a new Anaconda environment with Python 3.12. campy needs Python 3.9 or newer, and 3.12 is the version it is set up for: FLIR's PySpin (Spinnaker 4.4) is built for Python 3.12 on Windows, macOS and Ubuntu 24.04. On Ubuntu 22.04, PySpin is built for Python 3.10 only, so use `python=3.10` there.
```
conda create -n campy python=3.12 imageio-ffmpeg ffmpeg=8.1 -c conda-forge
conda activate campy
pip install -U setuptools
```
To encode on an NVIDIA GPU, check this ffmpeg first: see [NVIDIA GPU encoding (NVENC)](encoding.md#nvidia-gpu-encoding-nvenc).

3. Install camera software
- If using Basler cameras, install Pylon software:
  - Install Basler Pylon with Developer options
  - Install pypylon:
  Windows:
  ```
  pip install pypylon
  ```
  Linux:
  ```
  conda install swig
  git clone https://github.com/basler/pypylon.git
  python ./pypylon/setup.py install
  ```
  
- If using FLIR cameras:
  - Download and install Spinnaker SDK and SpinView software from FLIR's website: 
    https://www.flir.com/support-center/iis/machine-vision/downloads/spinnaker-sdk-and-firmware-download/
    On Windows, choose the "Application Development" profile: it installs the camera drivers and Visual C++ runtimes PySpin needs. To replace an older Spinnaker on a Windows computer that already records, follow [Updating Spinnaker on a Windows computer](#updating-spinnaker-on-a-windows-computer). If the installer fails, see [Spinnaker 4.4 on Windows: when the installer fails](#spinnaker-44-on-windows-when-the-installer-fails).
  - Manually install binary wheel for PySpin (included in the Spinnaker download). It must match your Python, by its cpXY tag (cp312 for Python 3.12), and the Spinnaker SDK you installed, by its version: a mismatch fails at `import PySpin`. E.g. for Spinnaker 4.4.0.246 and Python 3.12, install "spinnaker_python-4.4.0.246-cp312-cp312-win_amd64.whl" on Windows, or "spinnaker_python-4.4.0.246-cp312-cp312-linux_x86_64.whl" on Ubuntu 24.04 (x86_64). PySpin for Python 3.12 needs NumPy 2.x, and for Python 3.10 NumPy 1.x (`pip install "numpy<2"`); see the README in the PySpin download. On Windows and macOS, PySpin for Python 3.12 needs Spinnaker 4.4 or newer: 4.4.0.246 added it. On Windows the wheel brings its own copy of Spinnaker's libraries and uses only the installed SDK's camera driver: on the Dell Pro Tower Plus, PySpin 4.4.0.246 recorded with Spinnaker 4.2.0.88 still installed. Replacing an older installed Spinnaker (e.g. 4.2) with 4.4 as well keeps SpinView on campy's version ([Updating Spinnaker on a Windows computer](#updating-spinnaker-on-a-windows-computer)).
  ```
  pip3 install <wheel>
  ```
  - An environment with an older PySpin (e.g. Spinnaker 2.x's, for Python 3.7) kept working on Windows after the computer's Spinnaker was updated: on the Dell Pro Tower Plus, an older campy with PySpin 2.3.0.77 recorded its 8 cameras on Spinnaker 4.4.0.246's camera driver ([Updating Spinnaker on a Windows computer](#updating-spinnaker-on-a-windows-computer)). On Windows, an older Spinnaker (before 3.2) running next to campy turns Spinnaker's search for GigE cameras back on each time it runs ([FLIR cameras](flir.md#an-older-spinnaker-on-the-same-windows-computer-turns-the-search-back-on)). On Linux this wasn't checked: there, upgrade a computer's Spinnaker and its campy environment together.
  - On Linux, have `campy-flir-reset` run at startup, so that a camera left plugged in through a restart does not stay at 43 MB/s: see [FLIR cameras at 43 MB/s after a restart](flir.md#flir-cameras-at-43-mbs-after-a-restart).
  - If all of a computer's FLIR cameras are USB3, turn off Spinnaker's search for GigE cameras, which costs about a second each time campy starts: `campy-flir-reset --gige off`, after step 7. See [Faster startup: Spinnaker's search for GigE cameras](flir.md#faster-startup-spinnakers-search-for-gige-cameras).
4. Create a folder to clone campy:
```
cd ~\Documents\
mkdir campy
cd campy
```
5. Clone campy to that folder. `master` is the released campy; `dev`, the repository's default branch, holds work not released yet (see [Development and releases](development.md#development-and-releases)):
```
git clone -b master https://github.com/olinesn/campy.git
```
6. Navigate to the cloned campy folder
```
cd campy
```
7. Finally, install campy and its dependencies (see setup.py):
```
pip install -e .
```
8. On Windows, keep Windows Search out of the conda environments and the recording folder. It indexes the user folder, where conda keeps its environments, and installing or updating one keeps its indexer busy for hours, during recordings too ([Troubleshooting](troubleshooting.md#timing-tests)). In a PowerShell window opened with "Run as administrator", from the campy folder:
```
powershell -NoProfile -ExecutionPolicy Bypass -File tools\windows\exclude_from_search_index.ps1 -AlsoSoftware
```
It excludes `C:\campy_data` and `C:\data` (where the ChronicRecord rig's launcher puts each session's folder) and your `miniconda3`, `anaconda3`, `venvs` and `sleap-bench` folders, where they exist, and changes nothing else; `-Undo` takes the exclusions back. If you record somewhere else, name that folder instead: `-Folder D:\videos`. Do it while nothing is recording: Windows then spends several minutes removing those folders from its index.
9. On Windows, turn off USB selective suspend, so that Windows never power-manages a camera or a trigger board it thinks is idle. In the same administrator PowerShell window:
```
powershell -NoProfile -ExecutionPolicy Bypass -File tools\windows\disable_usb_selective_suspend.ps1
```
It sets "USB selective suspend" to Disabled in the active power plan (plugged in and on battery; the same as Control Panel > Power Options > Change advanced power settings > USB settings) and changes nothing else. `-Undo` puts the old values back. It applies to the active plan only: if you switch power plans later, run it again.
10. On Windows, pause Windows Update before recording, so that it can't restart the PC in the middle of a session (unpaused, Windows installs updates and restarts by itself outside its active hours; a restart ends every recording). From PowerShell:
```
powershell -NoProfile -ExecutionPolicy Bypass -File tools\windows\pause_windows_update.ps1 -Days 14
```
It asks for administrator rights itself, does what Settings > Windows Update > Pause updates does (1 to 35 days), prints the date it's paused until, and warns if a restart is already pending (restart first, while nothing records). When the pause ends, Windows installs the waiting updates and usually restarts before it allows another pause: let it, between recordings, then pause again. Settings > Windows Update > Resume updates undoes it.

## Updating Spinnaker on a Windows computer
How to replace an older Spinnaker SDK (and its SpinView) with a newer one on a Windows computer that already records, as done on the Dell Pro Tower Plus on 7 November 2026 (Spinnaker 4.2.0.88 to 4.4.0.246, 8 FLIR Blackfly S on USB3). Do it at the computer or over Remote Desktop, while nothing records.

1. **Before you start.** Keep the old version's installer, so you can go back. Note every campy environment that records on this computer, old ones included: each gets a test recording at the end.
2. **Run the new installer** (`SpinnakerSDK_FULL_4.4.0.246_x64.exe`). It first uninstalls the Spinnaker that is there, then asks for a restart: choose Restart Now. From here until the new version is in, the computer has no FLIR camera driver, so no campy, old or new, can record.
3. **After the restart, run the same installer again.** Choose the "Application Development" profile (Camera Evaluation installs only SpinView and its documentation). Keep the USB3 camera driver. The GigE parts are needed only for GigE cameras. Skip any offer to set up network adapters.
4. **If it stops partway**, with Teledyne Common Components installed and nothing else (no SpinView), don't run it again: install the packages it saved instead ([next section](#spinnaker-44-on-windows-when-the-installer-fails)). On the Pro Tower Plus this happened because the computer's endpoint security software blocked the installer.
5. **Check the cameras**, in PowerShell:
```powershell
Get-PnpDevice -PresentOnly | Where-Object InstanceId -match 'VID_1E10' | Format-Table Status, Class, FriendlyName, InstanceId
```
   Every camera should be `OK`, as a "USB3 Vision Camera" of class `PGRDevices`. The end of a camera's `InstanceId` is its serial number in hexadecimal (`0119E971` is camera 18475377), so a missing camera can be named: unplug it and plug it back in.
6. **Turn Spinnaker's search for GigE cameras off again**, if every FLIR camera is USB3. Uninstalling a Spinnaker deletes the files that keep the setting, so the search is back on, Spinnaker's default. With its full path, campy's copy runs from any prompt, also one where another environment is active:
```
C:\Users\<user>\miniconda3\envs\<campy environment>\Scripts\campy-flir-reset.exe --gige off
```
7. **Open SpinView** once: it should list every camera.
8. **Make a short test recording with each campy environment** that records on this computer, and check every camera's frame count.

What it gave on the Pro Tower Plus:
- The installer uninstalled 4.2.0.88 and asked for a restart. After it, the 4.4.0.246 installer was blocked. So the computer had no Spinnaker and no camera driver, and 7 of its 8 cameras showed as "USB3 Vision Device" with an error, the 8th not at all.
- The packages it had saved installed with the [next section](#spinnaker-44-on-windows-when-the-installer-fails)'s steps, each with exit code 0: VC runtime, GenTL, Binaries (SpinView 4.4.0.246), GenICam, and the USB3 drivers. `pnputil` then installed the camera driver (2.8.3.16) on all 8 cameras, the missing one included, and all 8 were `OK`. Installed this way, Spinnaker is in `C:\Program Files\Spinnaker` and its GenTL producer path (`GENICAM_GENTL64_PATH`) is `C:\Spinnaker\cti64\vs2015`, as on the Dell Pro Max Slim FCS1250.
- `campy-flir-reset --gige off` turned the search off, and SpinView 4.4.0.246 listed all 8 cameras.
- An older campy environment (`campy_v03`: Python 3.7, PySpin 2.3.0.77) recorded all 8 cameras for 25 s at 60 fps on the new driver, 1,504 or 1,505 frames each, and closed normally. It also turned the search back on ([FLIR cameras](flir.md#an-older-spinnaker-on-the-same-windows-computer-turns-the-search-back-on)).
- campy itself (campy 2.5.1, PySpin 4.4.0.246), started from its panel, then recorded all 8 cameras for 21 minutes at 60 fps on the new driver (AV1 with Intel Quick Sync, triggered by the Arduino). Every camera had 78,138 frames, none dropped by campy or missed by the camera, and every camera started on the trigger's first pulse (`campy-align`). It had recorded the same 8 cameras on 4.2.0.88's driver since 28 October.

## Spinnaker 4.4 on Windows: when the installer fails
On two Windows 11 PCs (October and November 2026), Spinnaker 4.4.0.246's installer failed the same way every time. Teledyne Common Components installed, then the installer's administrator process went away and nothing else installed: no SpinView, and PySpin found 0 cameras, although Settings ▸ Apps listed "Teledyne Spinnaker SDK 4.4.0.246". Its log in `%TEMP%` (`Teledyne_Spinnaker_SDK_4.4.0.246_(x64)_*.log`) shows `Applied execute package: TeledyneCommonComponentsSetup.exe, result: 0x0`, then `Error 0x800700e8: Failed to write message type to pipe` as it starts its next package, `GigeVisionInterface.exe`. On the first PC no crash or Microsoft Defender event explained it. On the second, the Dell Pro Tower Plus, the computer's endpoint security software showed an alert at that moment: it had blocked the Spinnaker installer as a "behavioral threat". So security software stopped the administrator process there, not a fault in the installer, and running the installer again is blocked the same way unless whoever manages that software allows it. By then the installer has saved its packages under `C:\ProgramData\Package Cache`, so they can be installed directly, in PowerShell run as administrator:

1. Install the packages with their features. Without `ADDLOCAL` each package installs no features at all and still exits with 0, so the loop prints what each one installs (Binaries' list should include `SpinView_v140`, Drivers' `USBDrivers`). `USBDrivers,USBDriversLegacy` leaves out the GigE drivers; use `ALL` for GigE cameras.
```powershell
$cache = "C:\ProgramData\Package Cache"
$parts = [ordered]@{ VCRedist_v140_x64 = "ALL"; Spinnaker_GenTL_v140_x64 = "ALL"; Spinnaker_Binaries_v140_x64 = "ALL"; Spinnaker_GenICam_v140_x64 = "ALL"; Spinnaker_Drivers_x64 = "USBDrivers,USBDriversLegacy" }
foreach ($n in $parts.Keys) {
  $msi = Get-ChildItem $cache -Recurse -Filter "$n.msi" | Where-Object FullName -like "*v4.4.0.246*" | Select-Object -First 1
  $log = "$env:TEMP\spin440-$n.log"
  $p = Start-Process msiexec.exe -ArgumentList "/i `"$($msi.FullName)`" ADDLOCAL=$($parts[$n]) /qb /norestart /l*v `"$log`"" -Wait -PassThru
  $local = (Select-String -LiteralPath $log -Pattern 'Feature: (\S+);.*Action: Local' | ForEach-Object { $_.Matches[0].Groups[1].Value }) -join ', '
  "$n -> exit code $($p.ExitCode); installing: $local"
}
```
2. Register the USB3 camera driver with Windows. The drivers package copies it to `C:\Program Files\Spinnaker\driver64`, but outside the full installer nothing registers it, so the camera stays a "USB3 Vision Device" with an error. Older USB 2 cameras use `PGRUsb\pgrusbcam.inf` instead.
```powershell
pnputil /add-driver "C:\Program Files\Spinnaker\driver64\PGRUsb3\PGRUSBCam3.inf" /install
pnputil /scan-devices
Get-PnpDevice -PresentOnly | Where-Object InstanceId -match 'VID_1E10' | Format-Table Status, Class, FriendlyName
```
   The camera should then be `OK`, as a "USB3 Vision Camera" of class `PGRDevices`, and PySpin should find it:
```powershell
python -c "import PySpin; s = PySpin.System.GetInstance(); c = s.GetCameras(); print('cameras', c.GetSize()); c.Clear(); s.ReleaseInstance()"
```
3. SpinView is then in the Start menu (`C:\Program Files\Spinnaker\bin64\vs2015\SpinView_WPF_v140.exe`). The half-installed "Teledyne Spinnaker SDK 4.4.0.246" may reopen its wizard at the next login: close it with Exit, since running it again fails the same way. Leave its Settings ▸ Apps entry alone too, as its uninstaller may remove the packages installed above.
