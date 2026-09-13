# USB DSLR camera

The flow application loads digiCamControl's camera engine directly through
Python.NET. You do not need to run digiCamControl, enable a web server, or start
a camera helper process.

## Use the camera

On a fresh PC, extract the complete application ZIP and run `install.bat`.
It installs Python.NET, verifies and unblocks the bundled DLLs, and tests their
loading without opening a camera. Run it again when upgrading an existing
installation. You do not need to add installation commands to `run.bat`.

1. Connect the camera by USB, switch it on, and close other tethering software
   that might already own the USB connection.
2. Open **Camera** and choose **Discover USB cameras**. The engine uses
   digiCamControl's device discovery and camera-specific drivers. Select the
   camera if more than one is connected.
3. Choose the photo output folder, then start live view. The **DSLR camera**
   card at the top left of **Operation & Monitoring** displays the same feed. **Pop out** opens
   a separate preview window, including on a second monitor.
4. Use the camera controls for capture, autofocus, manual focus, video and bulb
   exposures where supported. The property table reads the connected camera's
   settings and allowed values, including advanced properties.

Capability availability depends on the camera and its current mode. A USB
connection alone does not guarantee live view or video support. Unsupported
features are disabled; driver errors appear in the camera status area.

The card includes **Start live view** and **Start video**. Each becomes its
corresponding Stop action while active. The full Camera tab uses the same
toggle controls, including for bulb exposures.

The video toggle follows successful commands sent by this app. Starting or
stopping recording on the camera body is not currently reflected in the button.

Photos transfer to the selected folder without overwriting an existing filename.
RAW and JPEG files retain their extensions. Camera video recording follows the
device's recording behavior; the preview itself is not saved as a video.
Where supported, **Capture in camera RAM** selects the camera's capture target
for both the monitoring card and capture sequences.

Timelapse triggers a fixed number of shots at the chosen interval. Bracketing
uses supported values for a selected property and restores the original value
after the sequence. The scheduler waits for capture completion or file transfer
before proceeding. **Stop workflow** stops further shots; it cannot retract a
shutter command that has already reached the camera.

## Resource use and lifecycle

No camera library or USB worker starts until you discover cameras. Once open,
one STA worker owns the camera engine and serializes driver calls. It also
decodes live frames. The UI receives only the latest frame, shared by the card
and pop-out; there is no accumulating frame queue or second camera stream.

The preview defaults to 10 frames per second and can be adjusted up to 30.
Stopping live view stops preview requests and asks the camera to stop live view.
Disconnect releases camera resources. Closing the flow app waits asynchronously
for camera cleanup, so USB driver cleanup cannot block the Qt event loop.

Camera work is independent of flow acquisition and cannot issue flow setpoints.
The picture is for visual observation, not automatic flame detection or a safety
interlock. An in-process native camera-driver failure can affect the application;
driver support still needs validation with the physical camera used on the rig.

## Runtime and development

64-bit Windows, .NET Framework 4.8, and Python.NET are required. Python.NET is included
in the Python dependency lists. The camera runtime lives in
`flow_controller/camera_runtime`; a development installation can override that
directory with `FLOW_CONTROLLER_CAMERA_RUNTIME`.

The runtime is built from a pinned digiCamControl source revision using
`python scripts/build_camera_runtime.py`. See `third_party/digicamcontrol` for
source provenance, licenses, dependency versions, and local lifecycle patches.
The build downloads dependencies at build time; ordinary USB operation does not
need internet access.

## Set up a Canon camera

The GitHub application ZIP includes the 64-bit Canon 13.19 runtime. Run
`install.bat` normally. It verifies
and tests the included SDK and skips the separate Canon download/import prompt.
The camera engine loads the SDK directly from the application's bundle.

For a source ZIP without the Canon runtime, follow the setup below.

Canon EOS requires Canon's Windows EDSDK. Obtain the SDK under your own Canon
developer agreement from the [Canon Developer Portal](https://developers.canon-europe.com/developers/s/article/Latest-EOS-SDK-Version-13-x).
These steps apply only to a source distribution that omits the bundled SDK.

1. Download the Windows EDSDK 13.20 package from Canon.
2. Choose Canon setup during `install.bat`, or run `setup_canon.bat` later.
3. Select the SDK ZIP. You can also select `EDSDK.dll` inside an extracted
   SDK's `EDSDK_64/Dll` directory. Setup selects the 64-bit files automatically,
   copies companion libraries, removes download blocks, and tests native SDK
   initialization before saving the configuration.
4. Restart the flow app, connect and switch on the camera, then choose
   **Discover USB cameras**. Close other tethering software that owns the camera.

Imported SDKs are stored under `%USERPROFILE%\.flow-controller-v3\canon-sdk`,
outside the application directory, so extracting an application upgrade does
not remove them. A failed SDK import preserves the previous configuration.
The loader verifies the imported file hashes before enabling Canon support.
The supported version families are EDSDK 13.18 with EdsImage 3.18, EDSDK 13.19
with EdsImage 13.19, and EDSDK 13.20 with EdsImage 3.20.

For command-line setup, run `setup_canon.bat "C:\Downloads\CanonSDK.zip"`.
No digiCamControl desktop app or camera web server is required. Camera-specific
capture and live-view capabilities still need verification on the connected
camera.

## Build a Canon-enabled Windows download

The normal GitHub ZIP already includes the Canon runtime. To package that
same checkout locally, run:

```powershell
python scripts/package_windows.py --output "C:\Releases\flow-controller-canon.zip"
```

For a source checkout without a bundled SDK, supply the SDK and notice:

```powershell
python scripts/package_windows.py --canon-sdk "C:\Downloads\CanonSDK.zip" --canon-notice "C:\Downloads\Canon-runtime-notice.txt" --output "C:\Releases\flow-controller-canon.zip"
```

Use the official Windows SDK and its runtime redistribution notice under the
app developer's Canon agreement. The packager selects the supported x64 DLLs,
tests native initialization, and puts the runtime in `camera_runtime/canon`.
It updates the bundle's hash manifest and includes the supplied notice. It does
not change the source checkout or the SDK configured in the user's profile.
The resulting ZIP is ready to give to other PCs.

This integrates the device engine and native flow-app capture controls. It does
not load digiCamControl's desktop, window-command system, photo-editing plugins,
or its external-service integrations. Those are separate application features,
not functions of the USB camera engine.

Sources: [device library](https://github.com/dukus/digicamcontrol/tree/master/CameraControl.Devices),
[direct-use example](https://github.com/dukus/digicamcontrol/blob/master/CameraControl.Devices.Example/Form1.cs),
and [Python.NET embedding](https://pythonnet.github.io/pythonnet/python.html).

## Capture and preview behaviour

Canon single-format capture follows digiCamControl's live-view window: pause
frame reads, then call `CapturePhotoNoAf()`. Live view stays active. Unlike the
desktop's timer, frame reads and capture share one worker here: the current
synchronous download returns before capture starts, so no extra 300 ms
timer-drain sleep is needed. The native driver's own pause handling remains.
The optional **Autofocus before live-view capture** checkbox focuses separately
before that sequence; it is off by default, as in digiCamControl. Sequence
workflows retain their explicit autofocus choice. With live view off, Capture
uses `CapturePhoto()`, matching digiCamControl's main capture command.

The Canon driver controls its busy state. Storage is configured when the
capture destination changes, with native SaveTo readback; ordinary shots do
not reset or re-read it. RAW+JPEG and other drivers retain the stop/capture/
restart path. The last frame remains visible during capture. Failed shots
release the shutter and report the Canon error without retrying. Failed
transfers report an error rather than a saved photo.
If no completion arrives within 60 seconds, the app clears its pending capture
and reports a timeout. Camera errors leave preview stopped so the error can be
addressed before restarting it.

ISO changes wait for the camera write and verify its reported value. A rejected
setting is shown as an error. The embedded preview adjusts to the available
width; both embedded and popped-out images preserve the camera aspect ratio.

## Idle keep-awake

While connected, the camera worker sends Canon's ExtendShutDownTimer command
at 15-second intervals, including when live view is off. A shutdown warning
requests an earlier command. The callback only sets a flag; USB commands run
on the same worker as capture and preview. Busy cameras are deferred until
idle, and the driver's PreventShutDown setting remains respected.

The native result is checked. Rejected commands produce an app error and are
retried with a bounded frequency; identical consecutive errors are reported
once. Disconnecting clears the schedule. This replaces reliance on the
upstream warning handler, which ran on a background thread, ignored native
return codes, and had no running fallback timer.
