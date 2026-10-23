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
    On Windows, choose the "Application Development" profile: it installs the camera drivers and Visual C++ runtimes PySpin needs. If the installer fails, see [Spinnaker 4.4 on Windows: when the installer fails](#spinnaker-44-on-windows-when-the-installer-fails).
  - Manually install binary wheel for PySpin (included in the Spinnaker download). It must match your Python, by its cpXY tag (cp312 for Python 3.12), and the Spinnaker SDK you installed, by its version: a mismatch fails at `import PySpin`. E.g. for Spinnaker 4.4.0.246 and Python 3.12, install "spinnaker_python-4.4.0.246-cp312-cp312-win_amd64.whl" on Windows, or "spinnaker_python-4.4.0.246-cp312-cp312-linux_x86_64.whl" on Ubuntu 24.04 (x86_64). PySpin for Python 3.12 needs NumPy 2.x, and for Python 3.10 NumPy 1.x (`pip install "numpy<2"`); see the README in the PySpin download. On Windows and macOS, PySpin for Python 3.12 needs Spinnaker 4.4 or newer: 4.4.0.246 added it, so an older Spinnaker (e.g. 4.2) has to be replaced by 4.4, SpinView included.
  ```
  pip3 install <wheel>
  ```
  - An environment with an older PySpin (e.g. Spinnaker 2.x's, for Python 3.7) works only with that older SDK, so upgrade a computer's Spinnaker and its campy environment together.
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

## Spinnaker 4.4 on Windows: when the installer fails
On one Windows 11 PC (October 2026), Spinnaker 4.4.0.246's installer failed the same way every time. Teledyne Common Components installed, then the installer's administrator process went away and nothing else installed: no SpinView, and PySpin found 0 cameras, although Settings ▸ Apps listed "Teledyne Spinnaker SDK 4.4.0.246". Its log in `%TEMP%` (`Teledyne_Spinnaker_SDK_4.4.0.246_(x64)_*.log`) shows `Applied execute package: TeledyneCommonComponentsSetup.exe, result: 0x0`, then `Error 0x800700e8: Failed to write message type to pipe`. No crash or Microsoft Defender event explained it. By then the installer has saved its packages under `C:\ProgramData\Package Cache`, so they can be installed directly, in PowerShell run as administrator:

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
