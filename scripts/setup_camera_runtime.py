"""Verify and prepare the bundled Windows camera libraries during installation."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


def verified_dlls(runtime: Path) -> list[Path]:
    """Check the complete DLL set before removing downloaded-file markers."""
    runtime = runtime.resolve()
    manifest = json.loads((runtime / 'runtime-lock.json').read_text(encoding='utf-8'))
    verified = []
    for name, entry in manifest['runtime'].items():
        if not name.lower().endswith('.dll'):
            continue
        path = (runtime / name).resolve()
        if not path.is_relative_to(runtime):
            raise RuntimeError(f'Invalid camera library path: {name}')
        if not path.is_file():
            raise RuntimeError(f'Missing camera library: {name}. Extract the complete application ZIP again.')
        if hashlib.sha256(path.read_bytes()).hexdigest() != entry['sha256']:
            raise RuntimeError(f'Camera library failed verification: {name}. Extract a fresh application ZIP.')
        verified.append(path)
    if not any(path.name == 'CameraControl.Devices.dll' for path in verified):
        raise RuntimeError('The runtime manifest does not include CameraControl.Devices.dll.')
    actual = {path.resolve() for path in runtime.rglob('*')
              if path.is_file() and path.suffix.lower() == '.dll'}
    if actual != set(verified):
        raise RuntimeError('Unlisted camera DLLs found. Use the complete bundled runtime or rebuild its manifest.')
    return verified


def unblock_dlls(paths: list[Path]) -> None:
    environment = dict(os.environ)
    environment['FLOW_CAMERA_SETUP_FILES'] = json.dumps([str(path) for path in paths])
    subprocess.run([
        'powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
        "$ErrorActionPreference = 'Stop'; "
        '$env:FLOW_CAMERA_SETUP_FILES | ConvertFrom-Json | '
        'ForEach-Object { Unblock-File -LiteralPath $_ -ErrorAction Stop }',
    ], env=environment, check=True)


def check_framework() -> None:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r'SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full',
                            0, winreg.KEY_READ | winreg.KEY_WOW64_32KEY) as key:
            release = winreg.QueryValueEx(key, 'Release')[0]
        if release >= 528040:
            return
    except OSError:
        pass
    raise RuntimeError('Install Microsoft .NET Framework 4.8 or newer, then rerun install.bat. '
                       'Download: https://dotnet.microsoft.com/download/dotnet-framework/net48')


def check_library_load(runtime: Path) -> None:
    # Import types only: setup does not construct a manager or open a camera.
    import pythonnet
    pythonnet.load('netfx')
    import clr
    sys.path.insert(0, str(runtime))
    clr.AddReference(str(runtime / 'CameraControl.Devices.dll'))
    windows_base = (Path(os.environ.get('WINDIR', r'C:\Windows')) /
                    'Microsoft.NET/Framework64/v4.0.30319/WPF/WindowsBase.dll')
    clr.AddReference(str(windows_base))
    from CameraControl.Devices import CameraDeviceManager
    from CameraControl.Devices.Classes import CapabilityEnum
    from System.Windows.Threading import Dispatcher


def main() -> int:
    try:
        if sys.platform != 'win32' or sys.maxsize <= 2**32:
            raise RuntimeError('Camera setup requires 64-bit Windows and 64-bit Python.')
        check_framework()
        runtime = Path(__file__).resolve().parents[1] / 'flow_controller/camera_runtime'
        libraries = verified_dlls(runtime)
        unblock_dlls(libraries)
        check_library_load(runtime)
        print(f'Camera setup passed: verified, unblocked and loaded {len(libraries)} bundled DLLs.')
        root = runtime.parents[1]
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from flow_controller.infrastructure.canon_sdk import installed_sdk_directory
        try:
            canon = installed_sdk_directory()
            if canon:
                print(f'Canon SDK configured: {canon}')
            else:
                print('For Canon, choose Canon setup next or run setup_canon.bat with your official SDK ZIP.')
        except Exception as exc:
            print(f'Existing Canon SDK needs repair: {exc}. Run setup_canon.bat.')
        return 0
    except Exception as exc:
        print(f'Camera setup failed: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
