"""Direct in-process digiCamControl camera engine using Python.NET.

All manager and camera access is restricted to one caller-owned STA worker.
Python.NET and the assemblies are loaded only when :meth:`DccEngine.open` runs.
"""

from __future__ import annotations

from datetime import datetime
import ctypes
import inspect
import os
from pathlib import Path
import queue
import re
import struct
import sys
import threading
from typing import Any, Callable


_PROPERTY_SKIP = {
    "AdvancedProperties",
    "BatteryLevel",
    "Capabilities",
    "DeviceName",
    "DisplayName",
    "IsBusy",
    "IsConnected",
    "PortName",
    "Properties",
    "SerialNumber",
}


def _value(obj: object, *names: str, default: Any = None) -> Any:
    for name in names:
        try:
            return getattr(obj, name)
        except Exception:
            continue
    return default


def _items(value: object | None) -> list[Any]:
    if value is None:
        return []
    try:
        return list(value)
    except (TypeError, RuntimeError):
        return []


def _label(name: str) -> str:
    text = re.sub(r"(?<!^)(?=[A-Z])", " ", name).replace("_", " ")
    return text.strip().title()


def _same_object(left: object, right: object) -> bool:
    if left is right:
        return True
    try:
        return bool(left == right)
    except Exception:
        return False


class DccEngine:
    """Synchronous facade over a dedicated STA digiCamControl worker."""

    def __init__(
        self,
        runtime_dir: str | os.PathLike[str] | None = None,
        output_dir: str | os.PathLike[str] | None = None,
        *,
        manager_factory: Callable[..., object] | None = None,
    ) -> None:
        configured_runtime = runtime_dir or os.environ.get("FLOW_CONTROLLER_CAMERA_RUNTIME")
        self.runtime_dir = Path(configured_runtime).expanduser() if configured_runtime else (
            Path(__file__).resolve().parents[1] / "camera_runtime"
        )
        default_output = Path.home() / "Pictures" / "Flow Controller Captures"
        self.output_dir = Path(output_dir).expanduser() if output_dir else default_output
        self._manager_factory = manager_factory
        self._manager: object | None = None
        self._capability_enum: object | None = None
        self._dispatcher: object | None = None
        self._dispatcher_type: object | None = None
        self._dispatcher_frame_type: object | None = None
        self._dispatcher_priority: object | None = None
        self._action_type: object | None = None
        self._dll_directory_handle: object | None = None
        self._canon_directory_handle: object | None = None
        self._native_libraries: list[object] = []
        self._canon_sdk_ready = False
        self._subscriptions: list[tuple[object, object]] = []
        self._device_subscriptions: list[tuple[object, object]] = []
        self._delegates: list[object] = []
        self._pending_captures: queue.SimpleQueue[object] = queue.SimpleQueue()
        self._pending_events: queue.SimpleQueue[tuple[str, object | None]] = queue.SimpleQueue()
        self._owner_ident: int | None = None
        self._bulb_device: object | None = None

    def _call(self, function: Callable[[], Any], *, bind: bool = False) -> Any:
        current = threading.get_ident()
        if self._owner_ident is None:
            if not bind:
                raise RuntimeError("Camera engine is not open")
            self._owner_ident = current
        elif current != self._owner_ident:
            raise RuntimeError("All camera engine calls must use its owning STA thread")
        return function()

    def open(self) -> dict[str, Any]:
        return self._call(self._open, bind=True)

    def scan(self) -> dict[str, Any]:
        return self._call(self._scan)

    def select(self, camera_id: str) -> dict[str, Any]:
        return self._call(lambda: self._select(str(camera_id)))

    def snapshot(self) -> dict[str, Any]:
        return self._call(self._snapshot)

    def execute(self, action: str, params: dict[str, Any] | None = None) -> Any:
        safe_params = dict(params or {})
        return self._call(lambda: self._execute(str(action), safe_params))

    def frame(self) -> bytes | None:
        return self._call(self._frame)

    def poll_events(self) -> list[dict[str, Any]]:
        return self._call(self._poll_events)

    def pump(self) -> None:
        self._call(self._pump)

    def close(self) -> None:
        if self._owner_ident is None:
            return
        self._call(self._close)

    def _open(self) -> dict[str, Any]:
        if self._manager is not None:
            return self._snapshot()
        if self._manager_factory is not None:
            try:
                inspect.signature(self._manager_factory).bind(self.runtime_dir)
            except TypeError:
                self._manager = self._manager_factory()
            else:
                self._manager = self._manager_factory(self.runtime_dir)
        else:
            try:
                self._manager = self._load_manager()
            except Exception:
                self._cleanup_runtime()
                raise
        if self._manager is None:
            raise RuntimeError("digiCamControl DeviceManager could not be created")
        try:
            self._subscribe_events()
            return self._scan()
        except Exception:
            try:
                self._close()
            except Exception:
                self._manager = None
            raise

    def _load_manager(self) -> object:
        runtime = self.runtime_dir.resolve()
        device_dll = runtime / "CameraControl.Devices.dll"
        if not device_dll.is_file():
            raise RuntimeError(
                "digiCamControl runtime is missing CameraControl.Devices.dll at "
                f"{device_dll}. Install or repair the bundled camera runtime."
            )
        try:
            import pythonnet

            pythonnet.load("netfx")
            import clr
        except Exception as exc:
            raise RuntimeError(
                "Direct camera support requires Python.NET with the .NET Framework runtime: "
                f"{exc}"
            ) from exc

        runtime_text = str(runtime)
        if runtime_text not in sys.path:
            sys.path.insert(0, runtime_text)
        if sys.platform == "win32":
            self._prepare_native_runtime(runtime)
        try:
            clr.AddReference(str(device_dll))
            try:
                clr.AddReference("WindowsBase")
            except Exception:
                framework = "Framework64" if sys.maxsize > 2**32 else "Framework"
                windows_base = (
                    Path(os.environ.get("WINDIR", r"C:\Windows"))
                    / "Microsoft.NET"
                    / framework
                    / "v4.0.30319"
                    / "WPF"
                    / "WindowsBase.dll"
                )
                if not windows_base.is_file():
                    raise
                clr.AddReference(str(windows_base))
            from CameraControl.Devices import CameraDeviceManager
            from CameraControl.Devices.Classes import CapabilityEnum
            from System import Action
            from System.Windows.Threading import Dispatcher, DispatcherFrame, DispatcherPriority
        except Exception as exc:
            raise RuntimeError(
                f"Could not load the bundled digiCamControl library {device_dll}: {exc}"
            ) from exc

        self._capability_enum = CapabilityEnum
        self._dispatcher = Dispatcher.CurrentDispatcher
        self._dispatcher_type = Dispatcher
        self._dispatcher_frame_type = DispatcherFrame
        self._dispatcher_priority = DispatcherPriority
        self._action_type = Action
        try:
            manager = CameraDeviceManager(str(runtime / "DeviceData"))
            manager.UseExperimentalDrivers = self._canon_sdk_ready
            return manager
        except Exception as exc:
            raise RuntimeError(f"Could not initialise digiCamControl DeviceManager: {exc}") from exc

    @staticmethod
    def _pe_architecture(path: Path) -> str | None:
        names = {0x14C: "32-bit", 0x8664: "64-bit", 0xAA64: "ARM64"}
        try:
            with path.open("rb") as stream:
                stream.seek(0x3C)
                pe_offset = struct.unpack("<I", stream.read(4))[0]
                stream.seek(pe_offset + 4)
                machine = struct.unpack("<H", stream.read(2))[0]
            return names.get(machine, f"machine 0x{machine:04x}")
        except (OSError, struct.error):
            return None

    @staticmethod
    def _file_version(path: Path) -> str:
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
        size = version.GetFileVersionInfoSizeW(str(path), None)
        if not size:
            raise OSError(f"No Windows version metadata in {path.name}")
        buffer = ctypes.create_string_buffer(size)
        if not version.GetFileVersionInfoW(str(path), 0, size, buffer):
            raise ctypes.WinError(ctypes.get_last_error())
        pointer, length = ctypes.c_void_p(), wintypes.UINT()
        if not version.VerQueryValueW(
            buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)
        ):
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

    def _prepare_native_runtime(self, runtime: Path) -> None:
        from .canon_sdk import installed_sdk_directory, validate_sdk

        self._canon_sdk_ready = False
        self._dll_directory_handle = os.add_dll_directory(str(runtime))
        try:
            # A per-user installation survives application ZIP upgrades and
            # keeps licensed vendor files outside the published app package.
            sdk_directory = installed_sdk_directory() or runtime
            validate_sdk(sdk_directory)
            if sdk_directory.resolve() != runtime.resolve():
                self._canon_directory_handle = os.add_dll_directory(str(sdk_directory))
            for name in ("EdsImage.dll", "EDSDK.dll"):
                self._native_libraries.append(ctypes.WinDLL(str(sdk_directory / name)))
            self._canon_sdk_ready = True
        except Exception as exc:
            self._pending_events.put((
                "error",
                f"Canon camera support is unavailable: {exc}. "
                "Run setup_canon.bat to import your official Windows Canon SDK. "
                "Nikon, WIA, and other digiCamControl drivers remain available.",
            ))

    def _require_manager(self) -> object:
        if self._manager is None:
            raise RuntimeError("Camera engine is not open")
        return self._manager

    def _scan(self) -> dict[str, Any]:
        manager = self._require_manager()
        method = _value(manager, "ConnectToCamera")
        if callable(method):
            method()
        self._refresh_device_subscriptions()
        return self._snapshot()

    def _devices(self) -> list[object]:
        manager = self._require_manager()
        return [
            device
            for device in _items(_value(manager, "ConnectedDevices", default=[]))
            if bool(_value(device, "IsConnected", default=False))
        ]

    @staticmethod
    def _camera_id(device: object) -> str | None:
        for name in ("SerialNumber", "PortName"):
            raw = _value(device, name)
            if raw is not None and str(raw).strip():
                return str(raw).strip()
        return None

    @staticmethod
    def _camera_name(device: object, camera_id: str) -> str:
        raw = _value(device, "DisplayName", "DeviceName", "Name")
        return str(raw).strip() if raw is not None and str(raw).strip() else camera_id

    def _selected(self, *, required: bool = False) -> object | None:
        manager = self._require_manager()
        selected = _value(manager, "SelectedCameraDevice")
        if required and selected is not None and not bool(
            _value(selected, "IsConnected", default=False)
        ):
            raise RuntimeError("The selected camera is not connected")
        connected = self._devices()
        if selected is not None and not any(
            candidate is selected
            or (
                self._camera_id(candidate) is not None
                and self._camera_id(candidate) == self._camera_id(selected)
            )
            for candidate in connected
        ):
            selected = None
        if required:
            if selected is None:
                raise RuntimeError("No camera is selected")
        return selected

    def _select(self, camera_id: str) -> dict[str, Any]:
        if self._bulb_device is not None:
            raise RuntimeError("Stop the active bulb exposure before selecting another camera")
        manager = self._require_manager()
        match = next((d for d in self._devices() if self._camera_id(d) == camera_id), None)
        if match is None:
            raise ValueError(f"Camera {camera_id!r} is not connected")
        try:
            manager.SelectedCameraDevice = match
        except Exception as exc:
            raise RuntimeError(f"Could not select camera {camera_id!r}: {exc}") from exc
        self._refresh_device_subscriptions()
        return self._snapshot()

    def _capability_names(self, device: object) -> list[str]:
        explicit = _value(device, "capabilities", "Capabilities")
        if explicit is not None:
            return sorted({str(item) for item in _items(explicit)})
        enum_type = self._capability_enum
        checker = _value(device, "GetCapability")
        if enum_type is None or not callable(checker):
            return []
        try:
            from System import Enum

            values = list(Enum.GetValues(enum_type))
        except Exception:
            try:
                values = list(enum_type)
            except Exception:
                values = []
        supported: list[str] = []
        for member in values:
            try:
                if checker(member):
                    supported.append(str(_value(member, "name", default=member)))
            except Exception:
                continue
        return sorted(set(supported))

    def _properties(self, device: object) -> tuple[list[dict[str, Any]], dict[str, object]]:
        result: list[dict[str, Any]] = []
        objects: dict[str, object] = {}
        seen: list[object] = []
        for name in dir(device):
            if name.startswith("_") or name in _PROPERTY_SKIP:
                continue
            try:
                prop = getattr(device, name)
            except Exception:
                continue
            if not hasattr(prop, "Value") or not hasattr(prop, "Values"):
                continue
            stable = re.sub(r"[^a-z0-9]+", "_", name.casefold()).strip("_")
            self._append_property(result, objects, stable, _label(name), prop)
            seen.append(prop)

        for index, prop in enumerate(_items(_value(device, "Properties"))):
            if not hasattr(prop, "Value") or not hasattr(prop, "Values"):
                continue
            if any(_same_object(prop, previous) for previous in seen):
                continue
            raw_name = _value(prop, "Name", default=f"property_{index}")
            base = re.sub(r"[^a-z0-9]+", "_", str(raw_name).casefold()).strip("_")
            stable = self._unique_property_key(base, objects, prop, index)
            self._append_property(result, objects, stable, str(raw_name), prop)
            seen.append(prop)

        advanced = _value(device, "AdvancedProperties")
        for index, entry in enumerate(_items(advanced)):
            prop = entry
            if not hasattr(prop, "Value") or not hasattr(prop, "Values"):
                continue
            if any(_same_object(prop, previous) for previous in seen):
                continue
            raw_name = _value(entry, "Name", "Key", default=f"property_{index}")
            base = "advanced." + re.sub(
                r"[^a-z0-9]+", "_", str(raw_name).casefold()
            ).strip("_")
            stable = self._unique_property_key(base, objects, prop, index)
            display = str(_value(entry, "Label", "Name", default=raw_name))
            self._append_property(result, objects, stable, display, prop)
            seen.append(prop)
        result.sort(key=lambda item: item["name"].casefold())
        return result, objects

    @staticmethod
    def _unique_property_key(
        base: str,
        objects: dict[str, object],
        prop: object,
        index: int,
    ) -> str:
        if base not in objects:
            return base
        raw_code = _value(prop, "Code")
        suffix = str(raw_code) if raw_code not in (None, "", 0, "0") else str(index + 1)
        candidate = f"{base}.{suffix}"
        discriminator = 2
        while candidate in objects:
            candidate = f"{base}.{suffix}.{discriminator}"
            discriminator += 1
        return candidate

    @staticmethod
    def _append_property(
        result: list[dict[str, Any]],
        objects: dict[str, object],
        name: str,
        label: str,
        prop: object,
    ) -> None:
        values = [str(item) for item in _items(_value(prop, "Values", default=[]))]
        current = _value(prop, "Value")
        readonly = bool(_value(prop, "IsReadOnly", "Readonly", default=False))
        enabled = _value(prop, "IsEnabled", default=True)
        available = _value(prop, "Available", default=True)
        readonly = readonly or not bool(enabled) or not bool(available) or not values
        result.append(
            {
                "name": name,
                "label": label,
                "value": "" if current is None else str(current),
                "values": values,
                "readonly": readonly,
            }
        )
        objects[name] = prop

    def _snapshot(self) -> dict[str, Any]:
        cameras = []
        for device in self._devices():
            camera_id = self._camera_id(device)
            if camera_id is not None:
                cameras.append({"id": camera_id, "name": self._camera_name(device, camera_id)})
        selected = self._selected()
        selected_id = self._camera_id(selected) if selected is not None else None
        capabilities: list[str] = []
        properties: list[dict[str, Any]] = []
        battery: int | None = None
        busy = False
        capture_in_ram = False
        if selected is not None and selected_id is not None:
            capabilities = self._capability_names(selected)
            properties, _ = self._properties(selected)
            raw_battery = _value(selected, "Battery")
            try:
                parsed_battery = int(str(raw_battery).rstrip("%"))
                battery = parsed_battery if parsed_battery >= 0 else None
            except (TypeError, ValueError):
                battery = None
            busy = bool(_value(selected, "IsBusy", default=False))
            capture_in_ram = bool(_value(selected, "CaptureInSdRam", default=False))
        return {
            "cameras": cameras,
            "selected": selected_id,
            "capabilities": capabilities,
            "properties": properties,
            "battery": battery,
            "busy": busy,
            "capture_in_ram": capture_in_ram,
        }

    def _require_capability(self, device: object, action: str, aliases: tuple[str, ...]) -> None:
        available = {name.casefold() for name in self._capability_names(device)}
        if not any(alias.casefold() in available for alias in aliases):
            raise RuntimeError(f"The selected camera does not support {action}")

    @staticmethod
    def _invoke(device: object, action: str, names: tuple[str, ...], *args: Any) -> Any:
        method = _value(device, *names)
        if not callable(method):
            raise RuntimeError(f"The selected camera does not expose {action}")
        return method(*args)

    def _execute(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        action = action.strip().casefold()
        if action == "set_output":
            raw_path = str(params.get("path", "")).strip()
            if not raw_path or "://" in raw_path:
                raise ValueError("set_output requires a local directory path")
            destination = Path(raw_path).expanduser().resolve()
            destination.mkdir(parents=True, exist_ok=True)
            if not destination.is_dir():
                raise ValueError(f"Camera output path is not a directory: {destination}")
            self.output_dir = destination
            return {"ok": True, "action": action, "output_dir": str(destination)}

        device = self._selected(required=True)
        assert device is not None
        if self._bulb_device is not None and action != "bulb_stop":
            raise RuntimeError("The selected camera is busy with a bulb exposure")
        if action == "capture_target":
            requested = params.get("capture_in_ram")
            if not isinstance(requested, bool):
                raise ValueError("capture_target requires a capture_in_ram boolean")
            self._require_capability(device, "capture in RAM", ("CaptureInRam",))
            device.CaptureInSdRam = requested
            return {"ok": True, "action": action, "snapshot": self._snapshot()}
        if action in {"capture", "capture_no_af"} and "capture_in_ram" in params:
            self._require_capability(device, "capture in RAM", ("CaptureInRam",))
            if not hasattr(device, "CaptureInSdRam"):
                raise RuntimeError("The selected camera does not expose capture-in-RAM control")
            device.CaptureInSdRam = bool(params["capture_in_ram"])

        if action == "capture_no_af":
            self._require_capability(device, action, ("CaptureNoAf",))

        if action in {"capture", "capture_no_af"}:
            if bool(_value(device, "IsBusy", default=False)):
                raise RuntimeError("The selected camera is busy with another capture")
            try:
                device.IsBusy = True
            except Exception:
                pass

        if action == "capture":
            try:
                self._invoke(device, action, ("CapturePhoto",))
            except Exception:
                try:
                    device.IsBusy = False
                except Exception:
                    pass
                raise
        elif action == "capture_no_af":
            try:
                self._invoke(device, action, ("CapturePhotoNoAf",))
            except Exception:
                try:
                    device.IsBusy = False
                except Exception:
                    pass
                raise
        elif action == "live_start":
            self._require_capability(device, action, ("LiveView",))
            self._invoke(device, action, ("StartLiveView",))
        elif action == "live_stop":
            self._require_capability(device, action, ("LiveView",))
            self._invoke(device, action, ("StopLiveView",))
        elif action == "autofocus":
            self._require_capability(device, action, ("LiveView",))
            self._invoke(device, action, ("AutoFocus",))
        elif action == "focus":
            self._require_capability(device, action, ("SimpleManualFocus",))
            self._invoke(device, action, ("Focus",), int(params["step"]))
        elif action == "focus_point":
            self._require_capability(device, action, ("LiveView",))
            self._invoke(device, action, ("Focus",), int(params["x"]), int(params["y"]))
        elif action == "video_start":
            self._require_capability(device, action, ("RecordMovie",))
            self._invoke(device, action, ("StartRecordMovie",))
        elif action == "video_stop":
            self._require_capability(device, action, ("RecordMovie",))
            self._invoke(device, action, ("StopRecordMovie",))
        elif action == "bulb_start":
            self._require_capability(device, action, ("Bulb",))
            if bool(_value(device, "IsBusy", default=False)):
                raise RuntimeError("The selected camera is busy")
            self._invoke(device, action, ("StartBulbMode",))
            device.IsBusy = True
            self._bulb_device = device
        elif action == "bulb_stop":
            self._require_capability(device, action, ("Bulb",))
            bulb_device = self._bulb_device or device
            self._invoke(bulb_device, action, ("EndBulbMode",))
            bulb_device.IsBusy = False
            self._bulb_device = None
            device.IsBusy = False
        elif action == "camera_lock":
            self._require_capability(device, action, ("CanLockFocus",))
            self._invoke(device, action, ("LockCamera",))
        elif action == "camera_unlock":
            self._require_capability(device, action, ("CanLockFocus",))
            self._invoke(device, action, ("UnLockCamera",))
        elif action == "zoom":
            self._require_capability(device, action, ("Zoom",))
            prop = _value(device, "LiveViewImageZoomRatio")
            if prop is None:
                raise RuntimeError("The selected camera does not expose live-view zoom")
            requested = str(params["value"])
            if hasattr(prop, "Values"):
                values = [str(item) for item in _items(prop.Values)]
                if requested not in values:
                    raise ValueError(f"Invalid zoom value {requested!r}; choose one of {values}")
                prop.Value = requested
                if bool(_value(prop, "HaveError", default=False)):
                    raise RuntimeError(
                        f"Camera rejected zoom value {requested!r}; current value is {prop.Value!r}"
                    )
            else:
                device.LiveViewImageZoomRatio = params["value"]
            return {"ok": True, "action": action, "snapshot": self._snapshot()}
        elif action == "set_property":
            name = str(params.get("name", ""))
            requested = str(params.get("value", ""))
            _properties, objects = self._properties(device)
            prop = objects.get(name)
            if prop is None:
                raise ValueError(f"Unknown camera property {name!r}")
            readonly = bool(_value(prop, "IsReadOnly", "Readonly", default=False)) or not bool(
                _value(prop, "IsEnabled", default=True)
            )
            if readonly:
                raise ValueError(f"Camera property {name!r} is read-only")
            values = [str(item) for item in _items(_value(prop, "Values", default=[]))]
            if requested not in values:
                raise ValueError(f"Invalid value {requested!r} for {name}; choose one of {values}")
            prop.Value = requested
            if bool(_value(prop, "HaveError", default=False)):
                raise RuntimeError(
                    f"Camera rejected {name}={requested!r}; current value is {prop.Value!r}"
                )
            return {"ok": True, "action": action, "snapshot": self._snapshot()}
        else:
            raise ValueError(f"Unknown camera action {action!r}")
        return {"ok": True, "action": action}

    def _frame(self) -> bytes | None:
        device = self._selected(required=True)
        assert device is not None
        self._require_capability(device, "live view", ("LiveView",))
        data = self._invoke(device, "live view", ("GetLiveViewImage",))
        image = _value(data, "ImageData", "Data", default=data)
        if image is None:
            return None
        payload = bytes(image)
        try:
            position = int(_value(data, "ImageDataPosition", default=0) or 0)
        except (TypeError, ValueError):
            position = 0
        if 0 < position < len(payload):
            payload = payload[position:]
        return payload if payload.startswith(b"\xff\xd8") else None

    def _subscribe_events(self) -> None:
        manager = self._require_manager()

        def captured(*args: object) -> None:
            self._pending_captures.put(args[-1] if args else None)

        def connected(*_args: object) -> None:
            self._pending_events.put(("connection", None))

        def disconnected(*_args: object) -> None:
            self._pending_events.put(("connection", None))

        for event_name, handler, required in (
            ("PhotoCaptured", captured, True),
            ("CameraConnected", connected, False),
            ("CameraDisconnected", disconnected, False),
        ):
            event = _value(manager, event_name)
            if event is None:
                if required:
                    raise RuntimeError(
                        "The loaded digiCamControl manager has no PhotoCaptured event"
                    )
                continue
            try:
                event.__iadd__(handler)
            except Exception as exc:
                if required:
                    raise RuntimeError(
                        f"Could not subscribe to digiCamControl PhotoCaptured: {exc}"
                    ) from exc
                continue
            self._subscriptions.append((event, handler))
            self._delegates.append(handler)

    def _unsubscribe_events(self) -> list[str]:
        errors = self._unsubscribe_device_events()
        for event, handler in reversed(self._subscriptions):
            try:
                event.__isub__(handler)
            except Exception as exc:
                errors.append(f"could not unsubscribe manager event: {exc}")
        self._subscriptions.clear()
        self._delegates.clear()
        return errors

    def _unsubscribe_device_events(self) -> list[str]:
        errors: list[str] = []
        for event, handler in reversed(self._device_subscriptions):
            try:
                event.__isub__(handler)
            except Exception as exc:
                errors.append(f"could not unsubscribe camera event: {exc}")
            try:
                self._delegates.remove(handler)
            except ValueError:
                pass
        self._device_subscriptions.clear()
        return errors

    def _refresh_device_subscriptions(self) -> None:
        errors = self._unsubscribe_device_events()
        if errors:
            raise RuntimeError("; ".join(errors))
        for device in self._devices():
            event = _value(device, "CaptureCompleted")
            if event is None:
                continue

            def completed(*_args: object, current: object = device) -> None:
                self._pending_events.put(("capture_completed", current))

            try:
                event.__iadd__(completed)
            except Exception:
                continue
            self._device_subscriptions.append((event, completed))
            self._delegates.append(completed)

    def _capture_destination(self, event: object) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        raw_name = _value(event, "FileName", default="")
        supplied = Path(str(raw_name)).name if raw_name else ""
        suffix = Path(supplied).suffix or ".jpg"
        stem = Path(supplied).stem or datetime.now().strftime("capture_%Y%m%d_%H%M%S")
        counter = 0
        while True:
            discriminator = "" if counter == 0 else f"_{counter}"
            candidate = self.output_dir / f"{stem}{discriminator}{suffix}"
            try:
                with candidate.open("xb"):
                    pass
                return candidate
            except FileExistsError:
                counter += 1

    def _transfer_capture(self, event: object) -> dict[str, Any] | None:
        if event is None:
            return None
        device = _value(event, "CameraDevice", default=self._selected())
        handle = _value(event, "Handle")
        if device is None or handle is None:
            raise RuntimeError("PhotoCaptured did not include a camera and file handle")
        destination: Path | None = None
        try:
            destination = self._capture_destination(event)
            self._invoke(device, "file transfer", ("TransferFile",), handle, str(destination))
            return {"path": str(destination)}
        except Exception:
            if destination is not None:
                try:
                    if destination.stat().st_size == 0:
                        destination.unlink()
                except OSError:
                    pass
            raise
        finally:
            try:
                release = _value(device, "ReleaseResurce", "ReleaseResource")
                if not callable(release):
                    raise RuntimeError("Camera does not expose ReleaseResurce")
                release(handle)
            finally:
                try:
                    device.IsBusy = False
                except Exception:
                    pass

    def _poll_events(self) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        while True:
            try:
                captured = self._pending_captures.get_nowait()
            except queue.Empty:
                break
            try:
                result = self._transfer_capture(captured)
                if result is not None:
                    events.append({"type": "captured", **result})
            except Exception as exc:
                events.append(
                    {"type": "error", "message": f"Could not transfer captured photo: {exc}"}
                )
        while True:
            try:
                kind, payload = self._pending_events.get_nowait()
            except queue.Empty:
                break
            if kind == "connection":
                try:
                    self._refresh_device_subscriptions()
                    events.append({"type": "connection", "snapshot": self._snapshot()})
                except Exception as exc:
                    events.append({"type": "error", "message": f"Could not refresh cameras: {exc}"})
            elif kind == "capture_completed":
                device = payload
                try:
                    if device is not None:
                        device.IsBusy = False
                except Exception:
                    pass
                camera_id = self._camera_id(device) if device is not None else None
                events.append({"type": "capture_completed", "camera_id": camera_id})
            elif kind == "error":
                events.append({"type": "error", "message": str(payload)})
        return events

    def _pump(self) -> None:
        if self._dispatcher is None:
            return
        frame = self._dispatcher_frame_type()

        def stop_frame() -> None:
            frame.Continue = False

        callback = self._action_type(stop_frame)
        self._dispatcher.BeginInvoke(self._dispatcher_priority.Background, callback)
        self._dispatcher_type.PushFrame(frame)

    def _close(self) -> None:
        manager = self._manager
        if manager is None:
            self._cleanup_runtime()
            for pending in (self._pending_captures, self._pending_events):
                while True:
                    try:
                        pending.get_nowait()
                    except queue.Empty:
                        break
            return
        cleanup_errors = self._unsubscribe_events()
        try:
            while True:
                try:
                    captured = self._pending_captures.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._transfer_capture(captured)
                except Exception as exc:
                    cleanup_errors.append(f"could not finish captured photo: {exc}")
            if self._bulb_device is not None:
                try:
                    self._invoke(
                        self._bulb_device,
                        "stop bulb exposure during shutdown",
                        ("EndBulbMode",),
                    )
                    self._bulb_device.IsBusy = False
                    self._bulb_device = None
                except Exception as exc:
                    cleanup_errors.append(f"could not stop bulb exposure: {exc}")
            dispose = _value(manager, "Dispose")
            try:
                if callable(dispose):
                    dispose()
                else:
                    close_all = _value(manager, "CloseAll")
                    if callable(close_all):
                        close_all()
            except Exception as exc:
                cleanup_errors.append(f"could not dispose camera manager: {exc}")
        finally:
            self._manager = None
            self._capability_enum = None
            self._dispatcher = None
            self._dispatcher_type = None
            self._dispatcher_frame_type = None
            self._dispatcher_priority = None
            self._action_type = None
            self._bulb_device = None
            self._cleanup_runtime()
            while True:
                try:
                    self._pending_captures.get_nowait()
                except queue.Empty:
                    break
            while True:
                try:
                    self._pending_events.get_nowait()
                except queue.Empty:
                    break
        if cleanup_errors:
            raise RuntimeError("Camera cleanup failed: " + "; ".join(cleanup_errors))

    def _cleanup_runtime(self) -> None:
        self._native_libraries.clear()
        self._canon_sdk_ready = False
        if self._canon_directory_handle is not None:
            try:
                self._canon_directory_handle.close()
            except Exception:
                pass
            self._canon_directory_handle = None
        if self._dll_directory_handle is not None:
            try:
                self._dll_directory_handle.close()
            except Exception:
                pass
            self._dll_directory_handle = None


__all__ = ["DccEngine"]
