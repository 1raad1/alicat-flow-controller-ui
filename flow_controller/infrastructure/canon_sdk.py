"""Install and validate a user-supplied Canon EDSDK runtime.

Canon's native SDK is proprietary. This module validates the bundled runtime
and can import a user-supplied SDK archive or extracted SDK directory.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import struct
import subprocess
import sys
import tempfile
import uuid
import zipfile


_REQUIRED = ("EDSDK.dll", "EdsImage.dll")
_SUPPORTED = {(13, 18): (3, 18), (13, 19): (13, 19), (13, 20): (3, 20)}
_MANIFEST = "canon-sdk-manifest.json"
_CONFIG = "canon-sdk.json"
_MAX_DLL_BYTES = 128 * 1024 * 1024
_MAX_TOTAL_BYTES = 512 * 1024 * 1024
_WINDOWS_INVALID_FILENAME_CHARS = frozenset('<>:"/\\|?*')
_WINDOWS_RESERVED_STEMS = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}


def pe_architecture(path: str | os.PathLike[str]) -> str | None:
    """Return the PE machine architecture, or ``None`` for an invalid PE file."""

    names = {0x14C: "32-bit", 0x8664: "64-bit", 0xAA64: "ARM64"}
    try:
        with Path(path).open("rb") as stream:
            if stream.read(2) != b"MZ":
                return None
            stream.seek(0x3C)
            offset_data = stream.read(4)
            if len(offset_data) != 4:
                return None
            pe_offset = struct.unpack("<I", offset_data)[0]
            stream.seek(pe_offset)
            if stream.read(4) != b"PE\0\0":
                return None
            machine_data = stream.read(2)
            if len(machine_data) != 2:
                return None
            machine = struct.unpack("<H", machine_data)[0]
            return names.get(machine, f"machine 0x{machine:04x}")
    except (OSError, struct.error, OverflowError):
        return None


def file_version(path: str | os.PathLike[str]) -> str:
    """Read a DLL's fixed Windows file-version metadata."""

    if sys.platform != "win32":
        raise OSError("Windows file-version metadata is only available on Windows")
    from ctypes import wintypes

    version = ctypes.WinDLL("version", use_last_error=True)
    version.GetFileVersionInfoSizeW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    version.GetFileVersionInfoSizeW.restype = wintypes.DWORD
    version.GetFileVersionInfoW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    version.GetFileVersionInfoW.restype = wintypes.BOOL
    version.VerQueryValueW.argtypes = [
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.UINT),
    ]
    version.VerQueryValueW.restype = wintypes.BOOL

    ignored = wintypes.DWORD()
    size = version.GetFileVersionInfoSizeW(str(Path(path)), ctypes.byref(ignored))
    if not size:
        raise OSError(f"No Windows version metadata in {Path(path).name}")
    buffer = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(str(Path(path)), 0, size, buffer):
        raise ctypes.WinError(ctypes.get_last_error())
    pointer, length = ctypes.c_void_p(), wintypes.UINT()
    if not version.VerQueryValueW(buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)):
        raise ctypes.WinError(ctypes.get_last_error())
    values = ctypes.cast(pointer, ctypes.POINTER(wintypes.DWORD))
    return ".".join(
        str(part)
        for part in (
            values[2] >> 16,
            values[2] & 0xFFFF,
            values[3] >> 16,
            values[3] & 0xFFFF,
        )
    )


def _version_tuple(value: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in value.split("."))
    except ValueError as exc:
        raise RuntimeError(f"Invalid DLL file version {value!r}") from exc


def _dlls(directory: Path) -> dict[str, Path]:
    try:
        entries = [entry for entry in directory.iterdir() if entry.is_file()]
    except OSError as exc:
        raise RuntimeError(f"Could not read Canon SDK directory {directory}: {exc}") from exc
    result: dict[str, Path] = {}
    for entry in entries:
        if entry.suffix.lower() != ".dll":
            continue
        folded = entry.name.casefold()
        if folded in result:
            raise RuntimeError(f"Canon SDK directory contains duplicate DLL name {entry.name}")
        result[folded] = entry
    return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_sdk(
    directory: str | os.PathLike[str], *, verify_manifest: bool = False
) -> dict[str, dict[str, object]]:
    """Validate a direct Canon x64 DLL directory and return its metadata."""

    sdk_dir = Path(directory).expanduser()
    if not sdk_dir.is_dir():
        raise RuntimeError(f"Canon SDK directory does not exist: {sdk_dir}")
    dlls = _dlls(sdk_dir)
    missing = [name for name in _REQUIRED if name.casefold() not in dlls]
    if missing:
        raise RuntimeError(f"Canon SDK directory is missing {', '.join(missing)}")

    required = {name: dlls[name.casefold()] for name in _REQUIRED}
    for name, path in required.items():
        architecture = pe_architecture(path)
        if architecture != "64-bit":
            detail = architecture or "not a valid PE file"
            raise RuntimeError(f"{name} is {detail}; the 64-bit Canon SDK is required")

    try:
        versions = {name: file_version(path) for name, path in required.items()}
    except OSError as exc:
        raise RuntimeError(f"Could not verify Canon SDK version metadata: {exc}") from exc
    edsdk_family = _version_tuple(versions["EDSDK.dll"])[:2]
    image_family = _version_tuple(versions["EdsImage.dll"])[:2]
    if _SUPPORTED.get(edsdk_family) != image_family:
        supported = ("EDSDK 13.18 with EdsImage 3.18, EDSDK 13.19 with EdsImage 13.19, "
                     "or EDSDK 13.20 with EdsImage 3.20")
        raise RuntimeError(
            "Unsupported or mismatched Canon SDK versions: "
            f"EDSDK {versions['EDSDK.dll']}, EdsImage {versions['EdsImage.dll']}; "
            f"expected {supported}"
        )

    files: dict[str, dict[str, object]] = {}
    for path in sorted(dlls.values(), key=lambda item: (item.name.casefold(), item.name)):
        size = path.stat().st_size
        files[path.name] = {"sha256": _sha256(path), "bytes": size}
    metadata: dict[str, dict[str, object]] = {"versions": versions, "files": files}

    if verify_manifest:
        manifest_path = sdk_dir / _MANIFEST
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise RuntimeError(f"Canon SDK manifest is missing: {manifest_path}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Could not read Canon SDK manifest {manifest_path}: {exc}") from exc
        if manifest != metadata:
            raise RuntimeError(
                "Canon SDK files no longer match their installation manifest; reinstall the SDK"
            )
    return metadata


def _default_home() -> Path:
    return Path.home() / ".flow-controller-v3"


def installed_sdk_directory() -> Path | None:
    """Return the configured, validated SDK directory."""

    override = os.environ.get("CANON_EDSDK_X64_DIR")
    if override:
        directory = Path(override).expanduser()
        try:
            validate_sdk(directory)
        except RuntimeError as exc:
            raise RuntimeError(f"CANON_EDSDK_X64_DIR is invalid: {exc}") from exc
        return directory.resolve()

    config_path = _default_home() / _CONFIG
    if not config_path.exists():
        return None
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        raw_directory = config["directory"]
        directory = Path(raw_directory)
        if not directory.is_absolute():
            raise ValueError("directory is not absolute")
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(
            f"Canon SDK configuration is invalid ({config_path}); reinstall the SDK: {exc}"
        ) from exc
    try:
        validate_sdk(directory, verify_manifest=True)
    except RuntimeError as exc:
        raise RuntimeError(f"Configured Canon SDK is invalid; reinstall it: {exc}") from exc
    return directory


def _candidate_directories(root: Path) -> list[Path]:
    result: list[Path] = []
    for directory, subdirectories, filenames in os.walk(root):
        subdirectories.sort(key=str.casefold)
        folded = {name.casefold() for name in filenames}
        if all(name.casefold() in folded for name in _REQUIRED):
            result.append(Path(directory))
    return result


def _choose_folder_candidate(root: Path) -> Path:
    candidates: list[tuple[tuple[int, ...], tuple[int, ...], str, Path]] = []
    errors: list[str] = []
    for directory in _candidate_directories(root):
        try:
            metadata = validate_sdk(directory)
        except RuntimeError as exc:
            errors.append(f"{directory}: {exc}")
            continue
        versions = metadata["versions"]
        candidates.append(
            (
                _version_tuple(str(versions["EDSDK.dll"])),
                _version_tuple(str(versions["EdsImage.dll"])),
                str(directory.relative_to(root)).casefold(),
                directory,
            )
        )
    if not candidates:
        detail = f" ({'; '.join(errors)})" if errors else ""
        raise RuntimeError(f"No supported 64-bit Canon SDK DLL directory was found{detail}")
    candidates.sort(key=lambda item: item[2])
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return candidates[0][3]


def _zip_candidate_names(archive: zipfile.ZipFile) -> dict[str, list[zipfile.ZipInfo]]:
    grouped: dict[str, list[zipfile.ZipInfo]] = {}
    for info in archive.infolist():
        if info.is_dir():
            continue
        name = info.filename.replace("\\", "/")
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or path.suffix.lower() != ".dll":
            continue
        _validate_windows_dll_name(path.name)
        parent = str(path.parent)
        grouped.setdefault(parent, []).append(info)
    return {
        parent: infos
        for parent, infos in grouped.items()
        if all(
            required.casefold()
            in {
                PurePosixPath(info.filename.replace("\\", "/")).name.casefold()
                for info in infos
            }
            for required in _REQUIRED
        )
    }


def _validate_windows_dll_name(filename: str) -> None:
    if (
        not filename
        or filename.endswith((" ", "."))
        or any(character in _WINDOWS_INVALID_FILENAME_CHARS for character in filename)
        or any(ord(character) < 32 for character in filename)
        or Path(filename).stem.casefold() in _WINDOWS_RESERVED_STEMS
    ):
        raise RuntimeError(f"SDK archive contains unsafe Windows DLL filename {filename!r}")


def _safe_rmtree(path: Path, parent: Path) -> None:
    target = path.resolve()
    boundary = parent.resolve()
    if target == boundary or not target.is_relative_to(boundary):
        raise RuntimeError(f"Refusing to remove path outside staging directory: {target}")
    shutil.rmtree(target, ignore_errors=True)


def _extract_zip_infos(
    archive: zipfile.ZipFile, infos: list[zipfile.ZipInfo], destination: Path
) -> None:
    total = 0
    seen: set[str] = set()
    destination.mkdir(parents=True, exist_ok=False)
    for info in sorted(infos, key=lambda item: item.filename.casefold()):
        filename = PurePosixPath(info.filename.replace("\\", "/")).name
        folded = filename.casefold()
        if folded in seen:
            raise RuntimeError(f"SDK archive contains duplicate DLL name {filename}")
        seen.add(folded)
        if info.file_size > _MAX_DLL_BYTES:
            raise RuntimeError(f"SDK DLL {filename} exceeds the 128 MB size limit")
        total += info.file_size
        if total > _MAX_TOTAL_BYTES:
            raise RuntimeError("SDK DLLs exceed the 512 MB total size limit")
        target = destination / filename
        written = 0
        with archive.open(info) as source_stream, target.open("xb") as target_stream:
            while True:
                block = source_stream.read(1024 * 1024)
                if not block:
                    break
                written += len(block)
                if written > _MAX_DLL_BYTES or written > info.file_size:
                    raise RuntimeError(f"SDK DLL {filename} exceeded its declared size")
                target_stream.write(block)
        if written != info.file_size:
            raise RuntimeError(f"SDK DLL {filename} was truncated while extracting")


def _copy_dlls(source: Path, destination: Path) -> None:
    dlls = list(_dlls(source).values())
    total = 0
    destination.mkdir(parents=True, exist_ok=False)
    for path in sorted(dlls, key=lambda item: item.name.casefold()):
        size = path.stat().st_size
        if size > _MAX_DLL_BYTES:
            raise RuntimeError(f"SDK DLL {path.name} exceeds the 128 MB size limit")
        total += size
        if total > _MAX_TOTAL_BYTES:
            raise RuntimeError("SDK DLLs exceed the 512 MB total size limit")
        shutil.copyfile(path, destination / path.name)


def _unblock(directory: Path) -> None:
    if sys.platform != "win32":
        return
    command = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        "$ErrorActionPreference='Stop'; "
        "Get-ChildItem -LiteralPath $env:FLOW_CANON_SETUP_DIR -Filter '*.dll' -File | "
        "Unblock-File -ErrorAction Stop",
    ]
    environment = os.environ.copy()
    environment["FLOW_CANON_SETUP_DIR"] = str(directory.resolve())
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
        env=environment,
    )
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
        raise RuntimeError(f"Could not unblock imported Canon SDK DLLs: {detail}")


def _run_probe(directory: Path) -> None:
    command = [sys.executable, str(Path(__file__).resolve()), "--probe", str(directory)]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=20, check=False)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Canon SDK validation timed out after 20 seconds") from exc
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
        if "WinError 126" in detail or "error 126" in detail.lower():
            detail += (
                " Install the current Microsoft Visual C++ Redistributable for Visual Studio "
                "(x64), then try again."
            )
        raise RuntimeError(f"Canon SDK native validation failed: {detail}")


def _atomic_json(path: Path, value: object) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def install_sdk(
    source: str | os.PathLike[str], *, home: str | os.PathLike[str] | None = None
) -> Path:
    """Import an official Canon SDK ZIP or extracted directory."""

    source_path = Path(source).expanduser()
    sdk_home = Path(home).expanduser() if home is not None else _default_home()
    sdk_root = sdk_home / "canon-sdk"
    sdk_root.mkdir(parents=True, exist_ok=True)
    staging_root = Path(tempfile.mkdtemp(prefix=".staging-", dir=sdk_root))
    imported = staging_root / "dll"
    try:
        if source_path.is_dir():
            chosen = _choose_folder_candidate(source_path)
            _copy_dlls(chosen, imported)
        elif source_path.is_file() and zipfile.is_zipfile(source_path):
            with zipfile.ZipFile(source_path) as archive:
                grouped = _zip_candidate_names(archive)
                if not grouped:
                    raise RuntimeError("No Canon SDK DLL directory was found in the archive")
                viable: list[tuple[tuple[int, ...], tuple[int, ...], str]] = []
                errors: list[str] = []
                for index, parent in enumerate(sorted(grouped, key=str.casefold)):
                    inspection = staging_root / f"inspect-{index}"
                    required_infos = [
                        info
                        for info in grouped[parent]
                        if PurePosixPath(info.filename.replace("\\", "/")).name.casefold()
                        in {name.casefold() for name in _REQUIRED}
                    ]
                    try:
                        _extract_zip_infos(archive, required_infos, inspection)
                        metadata = validate_sdk(inspection)
                    except RuntimeError as exc:
                        errors.append(f"{parent}: {exc}")
                    else:
                        versions = metadata["versions"]
                        viable.append(
                            (
                                _version_tuple(str(versions["EDSDK.dll"])),
                                _version_tuple(str(versions["EdsImage.dll"])),
                                parent,
                            )
                        )
                    finally:
                        _safe_rmtree(inspection, staging_root)
                if not viable:
                    detail = f": {'; '.join(errors)}" if errors else ""
                    raise RuntimeError(f"No supported 64-bit Canon SDK was found{detail}")
                viable.sort(key=lambda item: item[2].casefold())
                viable.sort(key=lambda item: (item[0], item[1]), reverse=True)
                _extract_zip_infos(archive, grouped[viable[0][2]], imported)
        else:
            raise RuntimeError(f"Canon SDK source is not a directory or ZIP file: {source_path}")

        metadata = validate_sdk(imported)
        _unblock(imported)
        _run_probe(imported)
        manifest_bytes = json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode()
        digest = hashlib.sha256(manifest_bytes).hexdigest()[:16]
        destination = sdk_root / f"{digest}-{uuid.uuid4().hex[:8]}"
        if destination.exists():  # practically impossible, and never overwrite
            raise RuntimeError(f"Canon SDK destination unexpectedly exists: {destination}")
        (imported / _MANIFEST).write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        imported.rename(destination)
        _atomic_json(sdk_home / _CONFIG, {"directory": str(destination.resolve())})
        return destination.resolve()
    finally:
        _safe_rmtree(staging_root, sdk_root)


def _probe(directory: Path) -> None:
    validate_sdk(directory)
    if sys.platform != "win32":
        raise RuntimeError("Canon EDSDK native validation requires Windows")
    handle = os.add_dll_directory(str(directory))
    ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    ole32.CoInitializeEx.restype = ctypes.c_long
    initialized_com = False
    initialized_sdk = False
    camera_list = ctypes.c_void_p()
    edsimage = None
    edsdk = None
    try:
        result = ole32.CoInitializeEx(None, 0x2)  # COINIT_APARTMENTTHREADED
        if result not in (0, 1):
            raise OSError(f"CoInitializeEx failed with HRESULT 0x{result & 0xFFFFFFFF:08x}")
        initialized_com = True
        edsimage = ctypes.WinDLL(str(directory / "EdsImage.dll"), use_last_error=True)
        edsdk = ctypes.WinDLL(str(directory / "EDSDK.dll"), use_last_error=True)
        required_exports = (
            "EdsInitializeSDK",
            "EdsTerminateSDK",
            "EdsGetCameraList",
            "EdsGetDirectoryItemInfo",
            "EdsDownload",
            "EdsDownloadEvfImage",
            "EdsRelease",
        )
        missing = [name for name in required_exports if not hasattr(edsdk, name)]
        if missing:
            raise RuntimeError(f"EDSDK.dll is missing required exports: {', '.join(missing)}")
        edsdk.EdsInitializeSDK.restype = ctypes.c_uint32
        edsdk.EdsTerminateSDK.restype = ctypes.c_uint32
        edsdk.EdsGetCameraList.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
        edsdk.EdsGetCameraList.restype = ctypes.c_uint32
        edsdk.EdsRelease.argtypes = [ctypes.c_void_p]
        edsdk.EdsRelease.restype = ctypes.c_uint32
        error = edsdk.EdsInitializeSDK()
        if error:
            raise RuntimeError(f"EdsInitializeSDK failed with EDS error 0x{error:08x}")
        initialized_sdk = True
        error = edsdk.EdsGetCameraList(ctypes.byref(camera_list))
        if error:
            raise RuntimeError(f"EdsGetCameraList failed with EDS error 0x{error:08x}")
    finally:
        if edsdk is not None and camera_list.value:
            edsdk.EdsRelease(camera_list)
        if edsdk is not None and initialized_sdk:
            edsdk.EdsTerminateSDK()
        if initialized_com:
            ole32.CoUninitialize()
        handle.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a Canon EDSDK runtime")
    parser.add_argument("--probe", type=Path, help="run isolated native SDK validation")
    args = parser.parse_args(argv)
    if args.probe is None:
        parser.error("--probe DIRECTORY is required")
    try:
        _probe(args.probe)
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
