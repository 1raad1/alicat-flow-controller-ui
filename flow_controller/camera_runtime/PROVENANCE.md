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

Canon's EDSDK and EdsImage native libraries are proprietary and are not
redistributed here. The upstream repository's copies and the public
Canon.EDSDK 3.6.1 NuGet package are 32 bit and cannot load in the application's
64-bit Python process. An authorised matching Canon EDSDK_64 distribution can
be added at build time through `CANON_EDSDK_X64_DIR`; the builder requires and
loads x64 EDSDK 13.18.40.0 and EdsImage 3.18.10.2 before copying them. These are
the versions shipped at the pinned upstream commit, whose managed ABI uses a
64-bit directory item size and a 288-byte `EdsDirectoryItemInfo`. Arbitrary SDK
versions are rejected. Canon support therefore requires those two
vendor files, while Nikon/PTP, WIA, webcam, Sony, GoPro, and other managed device
paths remain present.

The digiCamControl license is in `LICENSE`. The runtime `licenses` directory
contains the complete Accord LGPL-2.1, Newtonsoft.Json MIT, RSSDP MIT, and
websocket-sharp MIT texts plus the corresponding NuGet metadata.
