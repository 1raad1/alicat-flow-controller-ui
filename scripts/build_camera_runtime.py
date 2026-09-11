"""Build the pinned digiCamControl device engine without the desktop application.

This script needs Windows, Git, .NET Framework 4.8, and internet access on the
first run. Downloads are cached below ``%TEMP%`` and verified before use.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import urllib.request
import zipfile


REPOSITORY = "https://github.com/dukus/digicamcontrol.git"
COMMIT = "9269e7851e5130f7d2278cc9942eccde0fd5e593"
ASSEMBLY_VERSION = "2.1.6.0"

PACKAGES = {
    "microsoft.net.compilers.toolset": (
        "4.8.0",
        "37333f4f1e2ce55e621355d6da651dc23d4cb5f94a8f76b9478816e87f110ad9",
    ),
    "microsoft.windows.sdk.cpp": (
        "10.0.22621.2428",
        "1a06225596800cab0c2877c01ac97b1673b9c250efcfd65f008ee531fd39169d",
    ),
    "accord": (
        "3.8.2-alpha",
        "7a4bdb5726b4736abda33ea9c14bc2a1e7d7a29a7e820db01c2fae9bc2d643df",
    ),
    "accord.video": (
        "3.8.2-alpha",
        "7fcbf27dfeac2f5bfe61594bc924da14c6fe8d421ddaf3145b2b557abe1fabd6",
    ),
    "accord.video.directshow": (
        "3.8.2-alpha",
        "5992090e711947d420c5c2a31dbe62dbd9ceb6c5a1ca02e20bd300a18a090cb7",
    ),
    "newtonsoft.json": (
        "13.0.3",
        "872fc189e638ab1056555b03aaa38f68bcb54286e221aa646eb1129babf63c77",
    ),
    "rssdp": (
        "2.0.9",
        "ae061bccc295af031d76f72a02963b200bbf3b1f84b438ce2d32fa5df1a0f56e",
    ),
    "websocketsharp": (
        "1.0.3-rc11",
        "24679c0a316e0ca3e2c73e865be1e663961a40abb6d9a775af6064aa19022266",
    ),
}

RUNTIME_COPIES = {
    "Accord.dll": ("accord", "lib/net462/Accord.dll"),
    "Accord.Video.dll": ("accord.video", "lib/net462/Accord.Video.dll"),
    "Accord.Video.DirectShow.dll": (
        "accord.video.directshow",
        "lib/net462/Accord.Video.DirectShow.dll",
    ),
    "Newtonsoft.Json.dll": ("newtonsoft.json", "lib/net45/Newtonsoft.Json.dll"),
    "Rssdp.Native.dll": ("rssdp", "lib/net45/Rssdp.Native.dll"),
    "websocket-sharp.dll": ("websocketsharp", "lib/websocket-sharp.dll"),
}

LICENSE_DOWNLOADS = {
    "Accord-LGPL-2.1.txt": (
        "https://www.gnu.org/licenses/old-licenses/lgpl-2.1.txt",
        "20e50fe7aae3e56378ebf0417d9de904f55a0e61e4df315333e632a4d3555d95",
    ),
    "Rssdp-MIT.txt": (
        "https://raw.githubusercontent.com/Yortw/RSSDP/master/LICENSE",
        "1d28b8a3f65b941743fa58ab03bb3bcc9d5b3e0cd50bbe0e2fb2d9656856ac11",
    ),
    "websocket-sharp-MIT.txt": (
        "https://raw.githubusercontent.com/sta/websocket-sharp/master/LICENSE.txt",
        "9678c808e025829062bcf63057c26357741fc5f0032f078a248c2f9c7ad291af",
    ),
}

def run(*args: str, cwd: Path | None = None, capture: bool = False) -> bytes | None:
    result = subprocess.run(
        args,
        cwd=cwd,
        check=True,
        stdout=subprocess.PIPE if capture else None,
    )
    return result.stdout if capture else None


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def remove_tree(path: Path, parent: Path) -> None:
    target, boundary = path.resolve(), parent.resolve()
    if target == boundary or boundary not in target.parents:
        raise RuntimeError(f"Refusing to remove path outside {boundary}: {target}")
    if target.exists():
        def clear_readonly(function, item, _error):
            os.chmod(item, stat.S_IWRITE)
            function(item)
        shutil.rmtree(target, onexc=clear_readonly)


def download(url: str, target: Path, expected: str) -> None:
    if target.exists() and sha256(target) == expected:
        return
    partial = target.with_suffix(target.suffix + ".partial")
    if partial.exists():
        partial.unlink()
    print(f"Downloading {url}")
    request = urllib.request.Request(url, headers={"User-Agent": "flow-controller-runtime-builder"})
    with urllib.request.urlopen(request) as response, partial.open("wb") as output:
        shutil.copyfileobj(response, output)
    actual = sha256(partial)
    if actual != expected:
        partial.unlink()
        raise RuntimeError(f"SHA-256 mismatch for {url}: {actual}")
    partial.replace(target)


def extract(package: Path, destination: Path, cache: Path) -> None:
    marker = destination / ".complete"
    if marker.exists() and marker.read_text(encoding="ascii") == sha256(package):
        return
    remove_tree(destination, cache)
    destination.mkdir(parents=True)
    with zipfile.ZipFile(package) as archive:
        root = destination.resolve()
        for item in archive.infolist():
            target = (destination / item.filename).resolve()
            if root not in target.parents and target != root:
                raise RuntimeError(f"Unsafe archive member: {item.filename}")
        archive.extractall(destination)
    marker.write_text(sha256(package), encoding="ascii")


def checkout_source(cache: Path, staging: Path) -> None:
    source = cache / "source"
    if not source.exists():
        run("git", "clone", "--filter=blob:none", "--no-checkout", REPOSITORY, str(source))
    actual = (run("git", "rev-parse", "HEAD", cwd=source, capture=True) or b"").decode().strip()
    if actual != COMMIT:
        run("git", "fetch", "origin", COMMIT, cwd=source)
    run("git", "sparse-checkout", "init", "--cone", cwd=source)
    run(
        "git",
        "sparse-checkout",
        "set",
        "CameraControl.Devices",
        "Canon.Eos.Framework",
        "PortableDeviceLib",
        "DeviceData",
        "refs",
        cwd=source,
    )
    run("git", "checkout", "--detach", COMMIT, cwd=source)
    remove_tree(staging, cache)
    staging.mkdir(parents=True)
    for name in ("CameraControl.Devices", "Canon.Eos.Framework", "PortableDeviceLib", "DeviceData", "refs"):
        shutil.copytree(source / name, staging / name)

def replace_once(text: str, old: str, new: str, label: str) -> str:
    if text.count(old) != 1:
        raise RuntimeError(f"Pinned source changed at lifecycle patch: {label}")
    return text.replace(old, new)


def patch_manager(source: Path) -> None:
    path = source / "CameraControl.Devices" / "CameraDeviceManager.cs"
    text = path.read_text(encoding="utf-8-sig")
    text = replace_once(
        text,
        "public class CameraDeviceManager : BaseFieldClass",
        "public class CameraDeviceManager : BaseFieldClass, IDisposable",
        "IDisposable declaration",
    )
    text = replace_once(
        text,
        "private List<DeviceDescription> _deviceDescriptions = new List<DeviceDescription>();",
        "private List<DeviceDescription> _deviceDescriptions = new List<DeviceDescription>();\n"
        "        private readonly List<ManagementEventWatcher> _managementWatchers = new List<ManagementEventWatcher>();\n"
        "        private bool _disposed;",
        "watcher fields",
    )
    text = replace_once(
        text,
        "            watcher.Query = insertQuery;\n            watcher.Start();",
        "            watcher.Query = insertQuery;\n"
        "            _managementWatchers.Add(watcher);\n"
        "            watcher.Start();",
        "watcher tracking",
    )
    for signature in (
        "        private void watcher_EventRemoved(object sender, EventArrivedEventArgs e)\n        {",
        "        private void watcher_EventArrived(object sender, EventArrivedEventArgs e)\n        {",
        "        private void _framework_CameraAdded(object sender, EventArgs e)\n        {",
        "        private void DeviceManager_OnEvent(string eventId, string deviceId, string itemId)\n        {",
    ):
        text = replace_once(
            text, signature, signature + "\n            if (_disposed) return;", signature.strip()
        )
    close_all = """        public void CloseAll()
        {
            foreach (
                ICameraDevice connectedDevice in ConnectedDevices.Where(connectedDevice => connectedDevice.IsConnected))
            {
                connectedDevice.Close();
            }
        }
"""
    replacement_close_all = """        public void CloseAll()
        {
            foreach (ICameraDevice connectedDevice in
                ConnectedDevices.Where(connectedDevice => connectedDevice.IsConnected).ToList())
            {
                connectedDevice.Close();
            }
        }
"""
    dispose = replacement_close_all + """
        public void Dispose()
        {
            if (_disposed)
                return;
            _disposed = true;
            foreach (var watcher in _managementWatchers.ToList())
            {
                try { watcher.Stop(); } catch (Exception) { }
                try { watcher.Dispose(); } catch (Exception) { }
            }
            _managementWatchers.Clear();
            if (WiaDeviceManager != null)
            {
                try { WiaDeviceManager.OnEvent -= DeviceManager_OnEvent; } catch (Exception) { }
            }
            foreach (var connectedDevice in
                ConnectedDevices.Where(device => device.IsConnected).ToList())
            {
                try { connectedDevice.Close(); } catch (Exception) { }
            }
            if (_framework != null)
            {
                try { _framework.CameraAdded -= _framework_CameraAdded; } catch (Exception) { }
                try { _framework.Dispose(); } catch (Exception) { }
                _framework = null;
            }
        }
"""
    text = replace_once(text, close_all, dispose, "Dispose method")
    path.write_text(text, encoding="utf-8")

def gac(name: str, windows: Path) -> Path:
    matches = list((windows / "Microsoft.NET" / "assembly").glob(f"GAC_*/*{name}*/v4.0_*/*{name}.dll"))
    if not matches:
        matches = list((windows / "Microsoft.NET" / "assembly").glob(f"GAC_*/*/v4.0_*/*{name}.dll"))
    if not matches:
        raise RuntimeError(f".NET Framework assembly is missing: {name}.dll")
    return matches[0]


def compile_library(
    compiler: Path,
    output: Path,
    sources: list[Path],
    references: list[Path],
    links: list[Path] | None = None,
    unsafe: bool = False,
) -> None:
    response = output.with_suffix(".rsp")
    args = [
        "/nologo",
        "/target:library",
        "/platform:x64",
        "/optimize+",
        "/deterministic+",
        f'/out:"{output}"',
    ]
    if unsafe:
        args.append("/unsafe+")
    args.extend(f'/reference:"{item}"' for item in references)
    args.extend(f'/link:"{item}"' for item in (links or []))
    args.extend(f'"{item}"' for item in sources)
    response.write_text("\n".join(args), encoding="utf-8")
    run(str(compiler), f"@{response}")


def pe_machine(path: Path) -> int:
    data = path.read_bytes()
    if data[:2] != b"MZ" or len(data) < 64:
        raise RuntimeError(f"Not a PE binary: {path}")
    offset = struct.unpack_from("<I", data, 0x3C)[0]
    if data[offset : offset + 4] != b"PE\0\0":
        raise RuntimeError(f"Invalid PE header: {path}")
    return struct.unpack_from("<H", data, offset + 4)[0]


def file_version(path: Path) -> str:
    import ctypes
    from ctypes import wintypes

    version = ctypes.WinDLL("version", use_last_error=True)
    version.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    version.GetFileVersionInfoSizeW.restype = wintypes.DWORD
    version.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    version.GetFileVersionInfoW.restype = wintypes.BOOL
    version.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT)]
    version.VerQueryValueW.restype = wintypes.BOOL
    size = version.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        raise RuntimeError(f"No Windows version resource in {path}")
    buffer = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(str(path), 0, size, buffer):
        raise RuntimeError(f"Cannot read Windows version resource in {path}")
    pointer, length = ctypes.c_void_p(), wintypes.UINT()
    if not version.VerQueryValueW(buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)):
        raise RuntimeError(f"Cannot query Windows version resource in {path}")
    values = ctypes.cast(pointer, ctypes.POINTER(wintypes.DWORD))
    return ".".join(str(part) for part in (values[2] >> 16, values[2] & 0xFFFF, values[3] >> 16, values[3] & 0xFFFF))


def add_canon_native(destination: Path, existing_runtime: Path) -> list[str]:
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from flow_controller.infrastructure.canon_sdk import validate_sdk, _run_probe

    supplied = os.environ.get("CANON_EDSDK_X64_DIR")
    source = Path(supplied).expanduser().resolve() if supplied else existing_runtime / "canon"
    if not source.exists() and not supplied:
        print("Canon native SDK omitted: no existing bundle or CANON_EDSDK_X64_DIR")
        return []
    metadata = validate_sdk(source, verify_manifest=(source / "canon-sdk-manifest.json").exists())
    notice = source / "NOTICE.txt"
    if not notice.is_file():
        raise RuntimeError("Canon runtime source must include its redistribution NOTICE.txt")
    _run_probe(source)
    target = destination / "canon"
    target.mkdir()
    for name in metadata["files"]:
        shutil.copy2(source / name, target / name)
    shutil.copy2(notice, target / "NOTICE.txt")
    (target / "canon-sdk-manifest.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return ["canon/" + name for name in metadata["files"]]


def main() -> int:
    if os.name != "nt":
        raise RuntimeError("The camera runtime can only be built on Windows")
    root = Path(__file__).resolve().parents[1]
    runtime = root / "flow_controller" / "camera_runtime"
    output_stage = root / "flow_controller" / ".camera_runtime.build"
    backup = root / "flow_controller" / ".camera_runtime.previous"
    third_party = root / "third_party" / "digicamcontrol"
    lock_path = root / "third_party" / "digicamcontrol" / "runtime-lock.json"
    cache = Path(tempfile.gettempdir()) / "flow-controller-camera-runtime"
    packages_dir = cache / "packages"
    staging = cache / "staging"
    build = cache / "build"
    packages_dir.mkdir(parents=True, exist_ok=True)
    build.mkdir(parents=True, exist_ok=True)

    expanded: dict[str, Path] = {}
    for package_id, (version, expected) in PACKAGES.items():
        filename = f"{package_id}.{version}.nupkg"
        archive = packages_dir / filename
        url = f"https://api.nuget.org/v3-flatcontainer/{package_id}/{version}/{filename}"
        download(url, archive, expected)
        destination = packages_dir / f"{package_id}.{version}"
        extract(archive, destination, cache)
        expanded[package_id] = destination
    license_files: dict[str, Path] = {}
    for filename, (url, expected) in LICENSE_DOWNLOADS.items():
        target = packages_dir / filename
        download(url, target, expected)
        license_files[filename] = target

    checkout_source(cache, staging)
    patch_manager(staging)

    windows = Path(os.environ.get("WINDIR", r"C:\Windows"))
    framework = windows / "Microsoft.NET" / "Framework64" / "v4.0.30319"
    compiler = expanded["microsoft.net.compilers.toolset"] / "tasks" / "net472" / "csc.exe"
    winmd = (expanded["microsoft.windows.sdk.cpp"] / "c" / "UnionMetadata" /
             "10.0.22621.0" / "Windows.winmd")
    if not compiler.exists() or not winmd.exists() or not (framework / "mscorlib.dll").exists():
        raise RuntimeError("Roslyn, Windows.winmd, or .NET Framework 4.8 is unavailable")
    wpf = [gac(name, windows) for name in ("PresentationCore", "PresentationFramework", "WindowsBase")]

    portable = build / "PortableDeviceLib.dll"
    canon = build / "Canon.Eos.Framework.dll"
    devices = build / "CameraControl.Devices.dll"
    compile_library(
        compiler,
        portable,
        sorted((staging / "PortableDeviceLib").rglob("*.cs")),
        [staging / "refs" / "Interop.PortableDeviceApiLib.dll", staging / "refs" / "Interop.PortableDeviceTypesLib.dll"],
        unsafe=True,
    )
    compile_library(
        compiler,
        canon,
        sorted((staging / "Canon.Eos.Framework").rglob("*.cs")),
        [
            framework / "System.Drawing.dll",
            framework / "System.Web.dll",
            framework / "System.Web.Extensions.dll",
            framework / "System.Xaml.dll",
            *wpf,
        ],
    )
    package_refs = [expanded[package] / relative for package, relative in RUNTIME_COPIES.values()]
    compile_library(
        compiler,
        devices,
        sorted((staging / "CameraControl.Devices").rglob("*.cs")),
        [
            portable,
            canon,
            staging / "refs" / "Interop.PortableDeviceApiLib.dll",
            staging / "refs" / "Interop.PortableDeviceTypesLib.dll",
            *package_refs,
            winmd,
            framework / "System.Drawing.dll",
            framework / "System.Management.dll",
            framework / "System.Net.Http.dll",
            framework / "System.Xaml.dll",
            framework / "System.Runtime.dll",
            framework / "System.Runtime.WindowsRuntime.dll",
            framework / "System.Runtime.InteropServices.WindowsRuntime.dll",
            *wpf,
        ],
        links=[staging / "refs" / "Interop.WIA.dll"],
        unsafe=True,
    )

    remove_tree(output_stage, root / "flow_controller")
    output_stage.mkdir()
    for item in (portable, canon, devices):
        shutil.copy2(item, output_stage / item.name)
    for filename, (package, relative) in RUNTIME_COPIES.items():
        shutil.copy2(expanded[package] / relative, output_stage / filename)
    for filename in ("Interop.PortableDeviceApiLib.dll", "Interop.PortableDeviceTypesLib.dll"):
        shutil.copy2(staging / "refs" / filename, output_stage / filename)
    shutil.copytree(staging / "DeviceData", output_stage / "DeviceData")
    canon_native = add_canon_native(output_stage, runtime)
    shutil.copy2(third_party / "LICENSE", output_stage / "LICENSE.digicamcontrol.txt")
    shutil.copy2(third_party / "PROVENANCE.md", output_stage / "PROVENANCE.md")
    licenses = output_stage / "licenses"
    licenses.mkdir()
    for filename, source in license_files.items():
        shutil.copy2(source, licenses / filename)
    shutil.copy2(expanded["newtonsoft.json"] / "LICENSE.md", licenses / "Newtonsoft.Json-MIT.md")
    for package_id in ("accord", "accord.video", "accord.video.directshow", "rssdp", "websocketsharp"):
        nuspec = next(expanded[package_id].glob("*.nuspec"))
        shutil.copy2(nuspec, licenses / f"{package_id}.nuspec")

    lock = {
        "schema": 1,
        "upstream": {"repository": REPOSITORY, "commit": COMMIT, "assembly_version": ASSEMBLY_VERSION},
        "build": {
            "target": ".NET Framework 4.8 / x64",
            "deterministic": True,
            "patches": [
                "CameraDeviceManager implements IDisposable and releases WMI/WIA/Canon resources",
            ],
            "canon_native": canon_native,
        },
        "packages": {
            key: {"version": version, "sha256": digest}
            for key, (version, digest) in PACKAGES.items()
        },
        "runtime": {
            item.relative_to(output_stage).as_posix(): {"sha256": sha256(item), "bytes": item.stat().st_size}
            for item in sorted(path for path in output_stage.rglob("*") if path.is_file())
        },
    }
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_text = json.dumps(lock, indent=2, sort_keys=True) + "\n"
    lock_path.write_text(lock_text, encoding="utf-8")
    (output_stage / "runtime-lock.json").write_text(lock_text, encoding="utf-8")
    remove_tree(backup, root / "flow_controller")
    if runtime.exists():
        runtime.rename(backup)
    try:
        output_stage.rename(runtime)
    except Exception:
        if backup.exists() and not runtime.exists():
            backup.rename(runtime)
        raise
    remove_tree(backup, root / "flow_controller")
    print(f"Built {len(lock['runtime']) + 1} runtime files in {runtime}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"camera runtime build failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
