# digiCamControl camera runtime provenance

The runtime is built from `dukus/digicamcontrol` commit
`9269e7851e5130f7d2278cc9942eccde0fd5e593` (assembly version `2.1.6.0`). It
contains the complete `CameraControl.Devices`, `Canon.Eos.Framework`, and
`PortableDeviceLib` source graphs plus upstream `DeviceData` XML definitions.
It does not contain the desktop application or web server.

The sole source patch adds `IDisposable` to `CameraDeviceManager`. It tracks and
disposes WMI watchers, detaches WIA and Canon events, closes a snapshot of open
cameras, and ignores device callbacks after disposal. This prevents monitor and
native framework resources surviving an in-process disconnect.

`scripts/build_camera_runtime.py` pins and SHA-256 verifies every download. The
compiler is Microsoft.Net.Compilers.Toolset 4.8.0. Microsoft.Windows.SDK.CPP
10.0.22621.2428 supplies build-only `Windows.winmd` metadata required by the
original GoPro Bluetooth sources. Runtime managed dependencies are Accord
3.8.2-alpha (LGPL-2.1), Newtonsoft.Json 13.0.3 (MIT), Rssdp 2.0.9 (MIT), and
websocket-sharp 1.0.3-rc11 (MIT). Exact package and output hashes are recorded
in `runtime-lock.json`.

The Windows x64 Canon camera runtime is bundled in `camera_runtime/canon`:
`EDSDK.dll` and `EdsImage.dll`, both version `13.19.0.6400`. The files were copied
unchanged from `Windows/EDSDK_64/Dll` in
https://github.com/lsy9344/Cannon_EDSDK at commit
`ef05251e6292dea66f6a113bf6adcd3826348aaa`.
Canon retains copyright in these proprietary binaries; they are not covered by
digiCamControl's open-source license. The SDK's original
`Document/readme.txt` is preserved as `canon/NOTICE.txt`. This software is based
in part on the work of the Independent JPEG Group.

The Canon file manifest records versions, sizes, and SHA-256 hashes. Setup
verifies the files, removes Windows download blocks, and probes native SDK
initialization before reporting success. The managed Canon framework also
passed initialization and discovery with this pair. Capture and live view still
require physical-camera validation. The bundled runtime covers camera control
and file transfer; Canon's separate DPP RAW-development plugins are not used by
the flow app.

Rebuilding the managed device engine preserves this verified Canon bundle.
`CANON_EDSDK_X64_DIR` can select another supported x64 DLL directory containing
its `NOTICE.txt`; the builder validates and probes it before copying.

The digiCamControl license is in `LICENSE`. The runtime `licenses` directory
contains the complete Accord LGPL-2.1, Newtonsoft.Json MIT, RSSDP MIT, and
websocket-sharp MIT texts plus the corresponding NuGet metadata.
