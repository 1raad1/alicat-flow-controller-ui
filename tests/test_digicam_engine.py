from __future__ import annotations

from pathlib import Path
from enum import Enum
import struct
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from flow_controller.infrastructure.digicam_engine import DccEngine


class FakeEvent:
    def __init__(self) -> None:
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def __isub__(self, handler):
        self.handlers.remove(handler)
        return self

    def fire(self, payload=None) -> None:
        for handler in list(self.handlers):
            handler(self, payload)


class FakeProperty:
    def __init__(self, value, values, *, readonly=False) -> None:
        self.Value = value
        self.Values = values
        self.IsReadOnly = readonly
        self.IsEnabled = True
        self.Available = True
        self.HaveError = False


class FakeLiveView:
    ImageData = b"\xff\xd8fake-jpeg\xff\xd9"


class FakeCapability(Enum):
    LiveView = 1
    Bulb = 2
    RecordMovie = 3
    CanLockFocus = 4
    CaptureInRam = 5
    CaptureNoAf = 6
    SimpleManualFocus = 7
    Zoom = 8


class FakeCaptureEvent:
    def __init__(self, device, name="image.jpg", handle=7) -> None:
        self.CameraDevice = device
        self.FileName = name
        self.Handle = handle


class FakeCamera:
    def __init__(self, serial="SERIAL-1", port="USB-1") -> None:
        self.SerialNumber = serial
        self.PortName = port
        self.DisplayName = "Test DSLR"
        self.IsConnected = True
        self.IsBusy = False
        self.Battery = "73%"
        self.capability_names = {
            "LiveView",
            "CaptureNoAf",
            "CaptureInRam",
            "SimpleManualFocus",
            "RecordMovie",
            "Bulb",
            "CanLockFocus",
            "Zoom",
        }
        self.IsoNumber = FakeProperty("100", ["100", "200"])
        self.ReadOnlySetting = FakeProperty("Auto", ["Auto"], readonly=True)
        self.LiveViewImageZoomRatio = FakeProperty("1", ["1", "5"])
        advanced = FakeProperty("Neutral", ["Neutral", "Vivid"])
        advanced.Name = "Picture Style"
        self.AdvancedProperties = [advanced]
        extra = FakeProperty("5000 K", ["5000 K", "5500 K"])
        extra.Name = "Colour Temperature"
        duplicate = FakeProperty("6000 K", ["6000 K", "6500 K"])
        duplicate.Name = "Colour Temperature"
        self.Properties = [self.IsoNumber, extra, duplicate]
        self.calls = []
        self.thread_ids = []
        self.CaptureInSdRam = False
        self.CaptureCompleted = FakeEvent()
        self.released = []

    def _called(self, name, *args):
        self.calls.append((name, args))
        self.thread_ids.append(threading.get_ident())

    def GetCapability(self, capability):
        return capability.name in self.capability_names

    def CapturePhoto(self): self._called("capture")
    def CapturePhotoNoAf(self): self._called("capture_no_af")
    def StartLiveView(self): self._called("live_start")
    def StopLiveView(self): self._called("live_stop")
    def AutoFocus(self): self._called("autofocus")
    def Focus(self, *args): self._called("focus", *args)
    def StartRecordMovie(self): self._called("video_start")
    def StopRecordMovie(self): self._called("video_stop")
    def StartBulbMode(self): self._called("bulb_start")
    def EndBulbMode(self): self._called("bulb_stop")
    def LockCamera(self): self._called("camera_lock")
    def UnLockCamera(self): self._called("camera_unlock")

    def GetLiveViewImage(self):
        self._called("frame")
        return FakeLiveView()

    def TransferFile(self, handle, destination):
        self._called("transfer", handle, destination)
        Path(destination).write_bytes(b"photo")

    def ReleaseResurce(self, handle):
        self.released.append(handle)


class FakeKeepAliveCamera(FakeCamera):
    def __init__(self, serial="SERIAL-1", port="USB-1") -> None:
        super().__init__(serial, port)
        self.PreventShutDown = True
        self.KeepAliveRequested = False
        self.keep_alive_calls = 0
        self.keep_alive_error = None
        self.keep_alive_thread_ids = []

    def KeepAlive(self):
        self.keep_alive_calls += 1
        self.keep_alive_thread_ids.append(threading.get_ident())
        if self.keep_alive_error is not None:
            raise RuntimeError(self.keep_alive_error)
        self.KeepAliveRequested = False


class FakeCameraInterfaceProxy:
    """Python.NET-style interface proxy hiding implementation-only members."""

    def __init__(self, implementation) -> None:
        object.__setattr__(self, "__implementation__", implementation)

    def __getattr__(self, name):
        if name in {"KeepAlive", "KeepAliveRequested"}:
            raise AttributeError(name)
        return getattr(self.__implementation__, name)

    def __setattr__(self, name, value) -> None:
        setattr(self.__implementation__, name, value)


class FakeCanonSdkCamera:
    def __init__(self, destination_events, *, secondary="Unknown", save_to=1) -> None:
        self.ImageQuality = SimpleNamespace(SecondaryImageFormat=secondary)
        self.save_to = save_to
        self.destination_events = destination_events
        self.reset_calls = 0
        self.accept_destination_write = True

    def GetProperty(self, property_id):
        self.destination_events.append(("get_property", property_id))
        return self.save_to

    def ResetShutterButton(self):
        self.reset_calls += 1


class FakeCanonCamera(FakeCamera):
    def __init__(self, *, secondary="Unknown", save_to=1) -> None:
        self.destination_events = []
        self._capture_in_sd_ram = False
        super().__init__()
        self.Manufacturer = "Canon Inc."
        self.Camera = FakeCanonSdkCamera(
            self.destination_events, secondary=secondary, save_to=save_to
        )
        self.destination_events.clear()

    @property
    def CaptureInSdRam(self):
        return self._capture_in_sd_ram

    @CaptureInSdRam.setter
    def CaptureInSdRam(self, value):
        self._capture_in_sd_ram = bool(value)
        self.destination_events.append(("set_capture_in_ram", bool(value)))
        camera = getattr(self, "Camera", None)
        if camera is not None and camera.accept_destination_write:
            camera.save_to = 2 if value else 1


class FakeManager:
    def __init__(self) -> None:
        self.camera = FakeCamera()
        self.ConnectedDevices = [self.camera]
        self.SelectedCameraDevice = self.camera
        self.PhotoCaptured = FakeEvent()
        self.CameraConnected = FakeEvent()
        self.CameraDisconnected = FakeEvent()
        self.scan_thread_ids = []
        self.closed = False
        self.close_thread_id = None

    def ConnectToCamera(self):
        self.scan_thread_ids.append(threading.get_ident())

    def CloseAll(self):
        self.closed = True
        self.close_thread_id = threading.get_ident()


class DccEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.manager = FakeManager()
        self.engine = DccEngine(
            output_dir=self.temp_dir.name, manager_factory=lambda *_: self.manager
        )
        self.engine._capability_enum = FakeCapability

    def use_camera(self, camera) -> None:
        self.manager.camera = camera
        self.manager.ConnectedDevices = [camera]
        self.manager.SelectedCameraDevice = camera

    def tearDown(self) -> None:
        self.engine.close()
        self.temp_dir.cleanup()

    def test_snapshot_uses_stable_id_and_reflected_controls(self) -> None:
        snapshot = self.engine.open()

        self.assertEqual(snapshot["cameras"], [{"id": "SERIAL-1", "name": "Test DSLR"}])
        self.assertEqual(snapshot["selected"], "SERIAL-1")
        self.assertEqual(snapshot["battery"], 73)
        self.assertFalse(snapshot["busy"])
        self.assertFalse(snapshot["capture_in_ram"])
        self.assertIn("LiveView", snapshot["capabilities"])
        iso = next(item for item in snapshot["properties"] if item["name"] == "isonumber")
        self.assertEqual(iso["values"], ["100", "200"])
        self.assertFalse(iso["readonly"])
        advanced = next(
            item for item in snapshot["properties"]
            if item["name"] == "advanced.picture_style"
        )
        self.assertEqual(advanced["value"], "Neutral")
        names = [item["name"] for item in snapshot["properties"]]
        self.assertEqual(names.count("isonumber"), 1)
        colour_keys = [name for name in names if name.startswith("colour_temperature")]
        self.assertEqual(len(colour_keys), 2)
        self.assertEqual(len(set(colour_keys)), 2)

    def test_manager_and_camera_calls_stay_on_owner_thread(self) -> None:
        caller = threading.get_ident()
        self.engine.open()
        self.engine.execute("capture", {})
        self.engine.execute("focus", {"step": -2})
        self.engine.frame()
        self.engine.close()

        worker_ids = self.manager.scan_thread_ids + self.manager.camera.thread_ids
        worker_ids.append(self.manager.close_thread_id)
        self.assertTrue(all(item == worker_ids[0] for item in worker_ids))
        self.assertEqual(worker_ids[0], caller)

    def test_actions_are_whitelisted_and_properties_validated(self) -> None:
        self.engine.open()
        self.engine.execute("set_property", {"name": "isonumber", "value": "200"})
        self.assertEqual(self.manager.camera.IsoNumber.Value, "200")
        self.engine.execute("zoom", {"value": "5"})
        self.assertEqual(self.manager.camera.LiveViewImageZoomRatio.Value, "5")

        with self.assertRaisesRegex(ValueError, "Invalid value"):
            self.engine.execute("set_property", {"name": "isonumber", "value": "6400"})
        with self.assertRaisesRegex(ValueError, "read-only"):
            self.engine.execute(
                "set_property", {"name": "readonlysetting", "value": "Auto"}
            )
        with self.assertRaisesRegex(ValueError, "Unknown camera action"):
            self.engine.execute("DeleteEverything", {})

    def test_property_write_prefers_native_synchronous_setter(self) -> None:
        class NativeProperty(FakeProperty):
            def __init__(self):
                super().__init__("100", ["100", "200"])
                self.writes = []

            def SetValueSynchronously(self, value):
                self.writes.append(value)
                self.Value = value

        prop = NativeProperty()
        self.manager.camera.IsoNumber = prop
        self.engine.open()

        self.engine.execute("set_property", {"name": "isonumber", "value": "200"})

        self.assertEqual(prop.writes, ["200"])
        self.assertEqual(prop.Value, "200")

        def reject(_value):
            raise RuntimeError("native setter rejected value")

        prop.SetValueSynchronously = reject
        with self.assertRaisesRegex(RuntimeError, "native setter rejected value"):
            self.engine.execute(
                "set_property", {"name": "isonumber", "value": "100"}
            )

    def test_disconnected_or_incapable_camera_is_rejected(self) -> None:
        self.engine.open()
        self.manager.camera.capability_names.remove("RecordMovie")
        with self.assertRaisesRegex(RuntimeError, "does not support video_start"):
            self.engine.execute("video_start", {})

        self.manager.camera.IsConnected = False
        with self.assertRaisesRegex(RuntimeError, "not connected"):
            self.engine.execute("capture", {})

    def test_frame_returns_only_jpeg_bytes(self) -> None:
        self.engine.open()
        self.assertEqual(self.engine.frame(), FakeLiveView.ImageData)
        FakeLiveView.ImageData = b"nikon-header\xff\xd8offset-jpeg\xff\xd9"
        FakeLiveView.ImageDataPosition = len(b"nikon-header")
        self.assertEqual(self.engine.frame(), b"\xff\xd8offset-jpeg\xff\xd9")
        FakeLiveView.ImageData = b"not-an-image"
        FakeLiveView.ImageDataPosition = 0
        self.assertIsNone(self.engine.frame())
        FakeLiveView.ImageData = b"\xff\xd8fake-jpeg\xff\xd9"

    def test_photo_callback_defers_transfer_and_makes_unique_files(self) -> None:
        self.engine.open()
        event = FakeCaptureEvent(self.manager.camera)
        self.manager.PhotoCaptured.fire(event)
        self.assertFalse(any(call[0] == "transfer" for call in self.manager.camera.calls))

        first = self.engine.poll_events()
        self.manager.PhotoCaptured.fire(event)
        second = self.engine.poll_events()

        first_path = Path(first[0]["path"])
        second_path = Path(second[0]["path"])
        self.assertEqual(first[0]["type"], "captured")
        self.assertTrue(first_path.is_file())
        self.assertTrue(second_path.is_file())
        self.assertNotEqual(first_path, second_path)
        self.assertEqual(self.manager.camera.released, [7, 7])
        self.assertFalse(self.manager.camera.IsBusy)

    def test_transfer_error_still_releases_handle_and_clears_busy(self) -> None:
        self.engine.open()

        def fail_transfer(_handle, _destination):
            raise OSError("disk full")

        self.manager.camera.TransferFile = fail_transfer
        self.manager.PhotoCaptured.fire(FakeCaptureEvent(self.manager.camera))
        events = self.engine.poll_events()

        self.assertIn("disk full", events[0]["message"])
        self.assertEqual(self.manager.camera.released, [7])
        self.assertFalse(self.manager.camera.IsBusy)

    def test_empty_transfer_is_an_error_and_is_not_reported_as_captured(self) -> None:
        self.engine.open()
        self.manager.camera.TransferFile = lambda _handle, _destination: None
        self.manager.PhotoCaptured.fire(FakeCaptureEvent(self.manager.camera))

        events = self.engine.poll_events()

        self.assertEqual([event["type"] for event in events], ["error"])
        self.assertIn("without a photo file", events[0]["message"])
        self.assertEqual(list(Path(self.temp_dir.name).iterdir()), [])
        self.assertEqual(self.manager.camera.released, [7])
        self.assertFalse(self.manager.camera.IsBusy)

    def test_close_reports_deferred_transfer_failure_after_releasing(self) -> None:
        self.engine.open()

        def fail_transfer(_handle, _destination):
            raise OSError("volume offline")

        self.manager.camera.TransferFile = fail_transfer
        self.manager.PhotoCaptured.fire(FakeCaptureEvent(self.manager.camera))

        with self.assertRaisesRegex(RuntimeError, "volume offline"):
            self.engine.close()

        self.assertEqual(self.manager.camera.released, [7])
        self.assertTrue(self.manager.closed)

    def test_destination_error_still_releases_handle(self) -> None:
        unusable = Path(self.temp_dir.name) / "not-a-directory"
        unusable.write_text("file")
        self.engine.output_dir = unusable
        self.engine.open()
        self.manager.PhotoCaptured.fire(FakeCaptureEvent(self.manager.camera))

        events = self.engine.poll_events()

        self.assertEqual(events[0]["type"], "error")
        self.assertEqual(self.manager.camera.released, [7])
        self.assertFalse(self.manager.camera.IsBusy)

    def test_connection_event_reports_fresh_snapshot(self) -> None:
        self.engine.open()
        self.manager.CameraDisconnected.fire()

        events = self.engine.poll_events()

        self.assertEqual(events[0]["type"], "connection")
        self.assertEqual(events[0]["snapshot"]["selected"], "SERIAL-1")

    def test_select_does_not_invent_a_camera(self) -> None:
        second = FakeCamera(serial="", port="USB-2")
        second.DisplayName = "Backup"
        self.manager.ConnectedDevices.append(second)
        self.engine.open()

        snapshot = self.engine.select("USB-2")
        self.assertEqual(snapshot["selected"], "USB-2")
        with self.assertRaisesRegex(ValueError, "not connected"):
            self.engine.select("missing")

    def test_capture_in_ram_is_opt_in_and_capability_checked(self) -> None:
        self.engine.open()
        self.engine.execute("capture", {})
        self.assertFalse(self.manager.camera.CaptureInSdRam)
        self.manager.camera.IsBusy = False
        self.engine.execute("capture", {"capture_in_ram": True})
        self.assertTrue(self.manager.camera.CaptureInSdRam)

        result = self.engine.execute("capture_target", {"capture_in_ram": False})
        self.assertFalse(result["snapshot"]["capture_in_ram"])

    def test_only_canon_single_format_capture_preserves_live_view(self) -> None:
        camera = FakeCanonCamera(secondary="Unknown")
        self.use_camera(camera)
        self.assertTrue(self.engine.open()["capture_preserves_live_view"])

        camera.Camera.ImageQuality.SecondaryImageFormat = "RAWJPEG"
        self.assertFalse(self.engine.snapshot()["capture_preserves_live_view"])

        camera.Camera.ImageQuality.SecondaryImageFormat = "Unknown"
        camera.Manufacturer = "Nikon Corporation"
        self.assertFalse(self.engine.snapshot()["capture_preserves_live_view"])

    def test_canon_capture_target_verifies_host_and_card_destinations(self) -> None:
        camera = FakeCanonCamera(save_to=2)
        self.use_camera(camera)
        self.engine.open()

        self.engine.execute("capture_target", {"capture_in_ram": True})

        self.assertEqual(camera.destination_events, [
            ("get_property", 0xB), ("set_capture_in_ram", True),
            ("get_property", 0xB),
        ])
        self.assertFalse(any(name == "capture" for name, _args in camera.calls))
        self.assertEqual(camera.Camera.reset_calls, 0)

        camera.destination_events.clear()
        camera.Camera.save_to = 1
        self.engine.execute("capture_target", {"capture_in_ram": False})

        self.assertEqual(camera.destination_events, [
            ("get_property", 0xB), ("set_capture_in_ram", False),
            ("get_property", 0xB),
        ])
        self.assertFalse(any(name == "capture" for name, _args in camera.calls))
        self.assertEqual(camera.Camera.reset_calls, 0)

    def test_canon_destination_mismatch_aborts_before_shutter(self) -> None:
        camera = FakeCanonCamera(save_to=1)
        camera.Camera.accept_destination_write = False
        self.use_camera(camera)
        self.engine.open()

        with self.assertRaisesRegex(RuntimeError, "SaveTo=1"):
            self.engine.execute("capture_target", {"capture_in_ram": True})

        self.assertEqual(camera.destination_events, [
            ("get_property", 0xB), ("set_capture_in_ram", True),
            ("get_property", 0xB),
        ])
        self.assertFalse(any(name == "capture" for name, _args in camera.calls))
        self.assertFalse(camera.IsBusy)
        self.assertIsNone(self.engine._capture_started)
        self.assertIsNone(self.engine._capture_device)

    def test_busy_canon_capture_does_not_mutate_destination(self) -> None:
        camera = FakeCanonCamera(save_to=2)
        self.use_camera(camera)
        self.engine.open()
        camera.IsBusy = True

        with self.assertRaisesRegex(RuntimeError, "busy"):
            self.engine.execute("capture", {"capture_in_ram": True})

        self.assertEqual(camera.destination_events, [])
        self.assertFalse(any(name == "capture" for name, _args in camera.calls))

    def test_canon_capture_without_target_parameter_uses_cached_destination(self) -> None:
        camera = FakeCanonCamera(save_to=1)
        camera.CaptureInSdRam = True
        camera.Camera.save_to = 1
        camera.destination_events.clear()
        self.use_camera(camera)
        self.engine.open()

        self.engine.execute("capture", {})

        self.assertEqual(camera.destination_events, [])
        self.assertEqual([name for name, _args in camera.calls].count("capture"), 1)

    def test_canon_same_target_shots_make_no_native_destination_calls(self) -> None:
        camera = FakeCanonCamera(save_to=2)
        camera.CaptureInSdRam = True
        camera.destination_events.clear()
        self.use_camera(camera)
        self.engine.open()

        self.engine.execute("capture", {"capture_in_ram": True})
        self.assertEqual(camera.destination_events, [])
        camera.CaptureCompleted.fire()
        self.engine.poll_events()

        self.engine.execute("capture", {"capture_in_ram": True})
        self.assertEqual(camera.destination_events, [])
        self.assertEqual([name for name, _args in camera.calls].count("capture"), 2)

    def test_canon_live_view_capture_orders_af_then_no_af_shutter(self) -> None:
        camera = FakeCanonCamera(save_to=1)
        trace = []
        camera.AutoFocus = lambda: trace.append(("autofocus", camera.IsBusy))
        camera.CapturePhoto = lambda: trace.append(("capture", camera.IsBusy))
        camera.CapturePhotoNoAf = lambda: trace.append(("capture_no_af", camera.IsBusy))
        self.use_camera(camera)
        self.engine.open()

        with patch("flow_controller.infrastructure.digicam_engine.time.sleep") as delay:
            self.engine.execute("capture", {
                "live_view_capture": True,
                "autofocus_before_capture": True,
            })

        self.assertEqual(trace, [
            ("autofocus", False), ("capture_no_af", False),
        ])
        delay.assert_not_called()
        self.assertFalse(any(name == "capture" for name, _busy in trace))

    def test_canon_live_view_no_af_path_never_autofocuses(self) -> None:
        camera = FakeCanonCamera(save_to=1)
        trace = []
        camera.AutoFocus = lambda: trace.append("autofocus")
        camera.CapturePhoto = lambda: trace.append("capture")
        camera.CapturePhotoNoAf = lambda: trace.append("capture_no_af")
        self.use_camera(camera)
        self.engine.open()

        with patch("flow_controller.infrastructure.digicam_engine.time.sleep") as delay:
            self.engine.execute("capture_no_af", {
                "live_view_capture": True,
                "autofocus_before_capture": True,
            })

        delay.assert_not_called()
        self.assertEqual(trace, ["capture_no_af"])

    def test_failed_canon_live_view_capture_is_not_retried(self) -> None:
        camera = FakeCanonCamera(save_to=1)
        attempts = []

        def fail_capture():
            attempts.append("capture_no_af")
            raise RuntimeError("shutter rejected")

        camera.CapturePhotoNoAf = fail_capture
        camera.CapturePhoto = lambda: attempts.append("capture")
        self.use_camera(camera)
        self.engine.open()

        with patch("flow_controller.infrastructure.digicam_engine.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "shutter rejected"):
                self.engine.execute("capture", {"live_view_capture": True})

        self.assertEqual(attempts, ["capture_no_af"])
        self.assertIsNone(self.engine._capture_started)
        self.assertIsNone(self.engine._capture_device)

    def test_failed_canon_capture_releases_shutter_once_and_keeps_error_details(self) -> None:
        camera = FakeCanonCamera(save_to=2)
        camera.CaptureInSdRam = True
        camera.destination_events.clear()
        self.use_camera(camera)
        self.engine.open()
        attempts = []

        class CanonError(RuntimeError):
            EosErrorCode = 0x8D01
            Message = "shutter rejected"

        def fail_capture():
            attempts.append("capture")
            raise CanonError("fallback text")

        def fail_reset():
            attempts.append("reset")
            raise RuntimeError("release rejected")

        camera.CapturePhoto = fail_capture
        camera.Camera.ResetShutterButton = fail_reset

        with self.assertRaisesRegex(
            RuntimeError,
            "destination: computer.*code: 36097.*shutter rejected.*release rejected",
        ):
            self.engine.execute("capture", {})

        self.assertEqual(attempts, ["capture", "reset"])
        self.assertFalse(camera.IsBusy)
        self.assertIsNone(self.engine._capture_started)
        self.assertIsNone(self.engine._capture_device)

    def test_bulb_busy_state_spans_start_and_stop(self) -> None:
        self.engine.open()
        self.engine.execute("bulb_start", {})
        self.assertTrue(self.engine.snapshot()["busy"])

        self.engine.execute("bulb_stop", {})

        self.assertFalse(self.engine.snapshot()["busy"])

    def test_video_state_changes_only_after_successful_commands(self) -> None:
        self.engine.open()
        self.assertFalse(self.engine.snapshot()["recording"])

        original_start = self.manager.camera.StartRecordMovie
        self.manager.camera.StartRecordMovie = lambda: (_ for _ in ()).throw(
            RuntimeError("start rejected"))
        with self.assertRaisesRegex(RuntimeError, "start rejected"):
            self.engine.execute("video_start", {})
        self.assertFalse(self.engine.snapshot()["recording"])

        self.manager.camera.StartRecordMovie = original_start
        self.engine.execute("video_start", {})
        self.assertTrue(self.engine.snapshot()["recording"])

        original_stop = self.manager.camera.StopRecordMovie
        self.manager.camera.StopRecordMovie = lambda: (_ for _ in ()).throw(
            RuntimeError("stop rejected"))
        with self.assertRaisesRegex(RuntimeError, "stop rejected"):
            self.engine.execute("video_stop", {})
        self.assertTrue(self.engine.snapshot()["recording"])

        self.manager.camera.StopRecordMovie = original_stop
        self.engine.execute("video_stop", {})
        self.assertFalse(self.engine.snapshot()["recording"])

    def test_empty_snapshot_does_not_report_active_device_modes(self) -> None:
        self.engine.open()
        self.manager.camera.IsConnected = False
        state = self.engine.snapshot()
        self.assertFalse(state["recording"])
        self.assertFalse(state["bulb_active"])

    def test_selecting_another_camera_resets_commanded_video_state(self) -> None:
        self.engine.open()
        self.engine.execute("video_start", {})
        second = FakeCamera()
        second.SerialNumber = "SERIAL-2"
        second.DisplayName = "Second DSLR"
        self.manager.ConnectedDevices.append(second)

        state = self.engine.select("SERIAL-2")

        self.assertEqual(state["selected"], "SERIAL-2")
        self.assertFalse(state["recording"])

    def test_video_state_uses_stable_id_across_equivalent_device_proxies(self) -> None:
        self.engine.open()
        self.engine.execute("video_start", {})
        equivalent = FakeCamera()
        self.manager.camera = equivalent
        self.manager.ConnectedDevices = [equivalent]
        self.manager.SelectedCameraDevice = equivalent

        self.assertTrue(self.engine.snapshot()["recording"])

    def test_close_ends_active_bulb_before_manager_shutdown(self) -> None:
        self.engine.open()
        self.engine.execute("bulb_start", {})

        self.engine.close()

        calls = [name for name, _args in self.manager.camera.calls]
        self.assertEqual(calls[-2:], ["bulb_start", "bulb_stop"])
        self.assertFalse(self.manager.camera.IsBusy)

    def test_capture_busy_state_and_completed_event_prevent_overlap(self) -> None:
        self.engine.open()
        self.engine.execute("capture", {})
        self.assertTrue(self.engine.snapshot()["busy"])
        with self.assertRaisesRegex(RuntimeError, "busy"):
            self.engine.execute("capture", {})

        self.manager.camera.CaptureCompleted.fire()
        events = self.engine.poll_events()

        self.assertEqual(
            events, [{"type": "capture_completed", "camera_id": "SERIAL-1"}]
        )
        self.assertFalse(self.engine.snapshot()["busy"])
        self.assertIsNone(self.engine._capture_started)
        self.assertIsNone(self.engine._capture_device)

    def test_capture_command_failure_clears_tracking_and_software_busy(self) -> None:
        self.engine.open()

        def fail_capture():
            raise RuntimeError("shutter rejected")

        self.manager.camera.CapturePhoto = fail_capture

        with self.assertRaisesRegex(RuntimeError, "shutter rejected"):
            self.engine.execute("capture", {})

        self.assertFalse(self.manager.camera.IsBusy)
        self.assertIsNone(self.engine._capture_started)
        self.assertIsNone(self.engine._capture_device)

    def test_capture_timeout_clears_busy_and_emits_one_actionable_error(self) -> None:
        self.engine.open()
        self.engine.execute("capture", {})
        started = self.engine._capture_started
        self.assertIsNotNone(started)

        with patch(
            "flow_controller.infrastructure.digicam_engine.time.monotonic",
            return_value=started + 61,
        ):
            events = self.engine.poll_events()
            repeated = self.engine.poll_events()

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "error")
        self.assertIn("Check focus, exposure and camera messages",
                      events[0]["message"])
        self.assertEqual(repeated, [])
        self.assertFalse(self.manager.camera.IsBusy)
        self.assertFalse(self.engine.snapshot()["busy"])
        self.assertIsNone(self.engine._capture_started)
        self.assertIsNone(self.engine._capture_device)

    def test_close_unsubscribes_and_closes_manager(self) -> None:
        self.engine.open()
        self.assertEqual(len(self.manager.PhotoCaptured.handlers), 1)
        self.assertEqual(len(self.manager.camera.CaptureCompleted.handlers), 1)

        self.engine.close()

        self.assertEqual(self.manager.PhotoCaptured.handlers, [])
        self.assertEqual(self.manager.camera.CaptureCompleted.handlers, [])
        self.assertTrue(self.manager.closed)

    def test_calls_from_another_thread_are_rejected(self) -> None:
        self.engine.open()
        errors = []

        def call_snapshot() -> None:
            try:
                self.engine.snapshot()
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=call_snapshot)
        thread.start()
        thread.join()

        self.assertEqual(len(errors), 1)
        self.assertIn("owning STA thread", str(errors[0]))

    def test_idle_pump_keeps_every_supported_camera_awake_on_owner_thread(self) -> None:
        first = FakeKeepAliveCamera()
        second = FakeKeepAliveCamera(serial="SERIAL-2", port="USB-2")
        unsupported = FakeCamera(serial="SERIAL-3", port="USB-3")
        self.manager.camera = first
        self.manager.SelectedCameraDevice = first
        self.manager.ConnectedDevices = [first, second, unsupported]
        self.engine.open()
        owner = threading.get_ident()

        with patch("flow_controller.infrastructure.digicam_engine.time.monotonic") as clock:
            clock.return_value = 100.0
            self.engine.pump()
            clock.return_value = 114.999
            self.engine.pump()
            clock.return_value = 115.0
            self.engine.pump()

        self.assertEqual(first.keep_alive_calls, 2)
        self.assertEqual(second.keep_alive_calls, 2)
        self.assertEqual(first.keep_alive_thread_ids, [owner, owner])
        self.assertEqual(second.keep_alive_thread_ids, [owner, owner])
        self.assertEqual(unsupported.calls, [])
        self.assertFalse(any(name.startswith("capture") or name == "frame"
                             for name, _args in first.calls + second.calls))

    def test_keep_alive_unwraps_interface_proxy_to_reach_implementation_members(self) -> None:
        camera = FakeKeepAliveCamera()
        camera.PreventShutDown = False
        proxy = FakeCameraInterfaceProxy(camera)
        self.use_camera(proxy)
        self.engine.open()

        with patch("flow_controller.infrastructure.digicam_engine.time.monotonic") as clock:
            clock.return_value = 0.0
            self.engine.pump()
            self.assertEqual(camera.keep_alive_calls, 1)
            self.assertTrue(camera.PreventShutDown)

            clock.return_value = 14.999
            self.engine.pump()
            self.assertEqual(camera.keep_alive_calls, 1)

            clock.return_value = 15.0
            self.engine.pump()
            self.assertEqual(camera.keep_alive_calls, 2)

            camera.KeepAliveRequested = True
            clock.return_value = 16.0
            self.engine.pump()
            self.assertEqual(camera.keep_alive_calls, 3)
            self.assertFalse(camera.KeepAliveRequested)

            camera.IsConnected = False
            clock.return_value = 60.0
            self.engine.pump()

        self.assertEqual(camera.keep_alive_calls, 3)
        self.assertEqual(self.engine._keep_alive_state, {})

    def test_keep_alive_request_wakes_early_but_attempts_at_most_once_per_second(self) -> None:
        camera = FakeKeepAliveCamera()
        self.use_camera(camera)
        self.engine.open()

        with patch("flow_controller.infrastructure.digicam_engine.time.monotonic") as clock:
            clock.return_value = 20.0
            self.engine.pump()
            camera.KeepAliveRequested = True
            clock.return_value = 20.999
            self.engine.pump()
            self.assertEqual(camera.keep_alive_calls, 1)
            clock.return_value = 21.0
            self.engine.pump()

        self.assertEqual(camera.keep_alive_calls, 2)
        self.assertFalse(camera.KeepAliveRequested)

    def test_busy_camera_waits_and_shutdown_prevention_is_enabled(self) -> None:
        busy = FakeKeepAliveCamera()
        busy.IsBusy = True
        disabled = FakeKeepAliveCamera(serial="SERIAL-2", port="USB-2")
        disabled.PreventShutDown = False
        self.manager.camera = busy
        self.manager.SelectedCameraDevice = busy
        self.manager.ConnectedDevices = [busy, disabled]
        self.engine.open()

        with patch("flow_controller.infrastructure.digicam_engine.time.monotonic") as clock:
            clock.return_value = 0.0
            self.engine.pump()
            clock.return_value = 30.0
            self.engine.pump()
            busy.IsBusy = False
            self.engine.pump()
            clock.return_value = 60.0
            self.engine.pump()

        self.assertEqual(busy.keep_alive_calls, 2)
        self.assertEqual(disabled.keep_alive_calls, 3)
        self.assertTrue(disabled.PreventShutDown)

    def test_shutdown_prevention_is_restored_after_driver_reset(self) -> None:
        camera = FakeKeepAliveCamera()
        self.use_camera(camera)
        self.engine.open()
        with patch("flow_controller.infrastructure.digicam_engine.time.monotonic") as clock:
            clock.return_value = 0.0
            self.engine.pump()
            camera.PreventShutDown = False
            clock.return_value = 15.0
            self.engine.pump()
        self.assertTrue(camera.PreventShutDown)
        self.assertEqual(camera.keep_alive_calls, 2)

    def test_shutdown_prevention_failure_is_reported_and_retried(self) -> None:
        class RefusingCamera(FakeKeepAliveCamera):
            @property
            def PreventShutDown(self):
                return False

            @PreventShutDown.setter
            def PreventShutDown(self, value):
                pass

        camera = RefusingCamera()
        self.use_camera(camera)
        self.engine.open()
        with patch("flow_controller.infrastructure.digicam_engine.time.monotonic") as clock:
            clock.return_value = 0.0
            self.engine.pump()
            events = self.engine.poll_events()
            self.assertIn("did not enable shutdown prevention", events[0]["message"])
            clock.return_value = 5.0
            self.engine.pump()
            self.assertEqual(self.engine.poll_events(), [])
        self.assertEqual(camera.keep_alive_calls, 0)

    def test_keep_alive_failures_are_deduplicated_and_clear_after_success(self) -> None:
        camera = FakeKeepAliveCamera()
        camera.keep_alive_error = "USB warning failed"
        self.use_camera(camera)
        self.engine.open()

        with patch("flow_controller.infrastructure.digicam_engine.time.monotonic") as clock:
            clock.return_value = 0.0
            self.engine.pump()
            first = self.engine.poll_events()
            clock.return_value = 4.999
            self.engine.pump()
            clock.return_value = 5.0
            self.engine.pump()
            repeated = self.engine.poll_events()
            camera.KeepAliveRequested = True
            clock.return_value = 5.999
            self.engine.pump()
            clock.return_value = 6.0
            self.engine.pump()
            camera.keep_alive_error = None
            clock.return_value = 7.0
            self.engine.pump()
            camera.keep_alive_error = "USB warning failed"
            camera.KeepAliveRequested = True
            clock.return_value = 8.0
            self.engine.pump()
            after_success = self.engine.poll_events()

        self.assertEqual(camera.keep_alive_calls, 5)
        self.assertEqual(len(first), 1)
        self.assertIn("USB warning failed", first[0]["message"])
        self.assertEqual(repeated, [])
        self.assertEqual(len(after_success), 1)
        self.assertIn("USB warning failed", after_success[0]["message"])

    def test_disconnected_camera_is_removed_from_keep_alive_schedule(self) -> None:
        camera = FakeKeepAliveCamera()
        self.use_camera(camera)
        self.engine.open()

        with patch("flow_controller.infrastructure.digicam_engine.time.monotonic") as clock:
            clock.return_value = 0.0
            self.engine.pump()
            camera.IsConnected = False
            self.manager.ConnectedDevices = []
            self.manager.SelectedCameraDevice = None
            self.manager.CameraDisconnected.fire()
            self.engine.poll_events()
            clock.return_value = 60.0
            self.engine.pump()

        self.assertEqual(camera.keep_alive_calls, 1)
        self.assertEqual(self.engine._keep_alive_state, {})

    def test_missing_canon_native_sdk_is_a_nonfatal_actionable_event(self) -> None:
        runtime = Path(self.temp_dir.name) / "runtime"
        runtime.mkdir()
        with patch("flow_controller.infrastructure.canon_sdk.installed_sdk_directory", return_value=None):
            self.engine._prepare_native_runtime(runtime)
        events = self.engine._poll_events()
        self.assertFalse(self.engine._canon_sdk_ready)
        self.assertIn("setup_canon.bat", events[0]["message"])
        self.assertIn("other digiCamControl drivers remain available", events[0]["message"])
        self.engine._cleanup_runtime()

    def test_invalid_sdk_is_reported_before_loading(self) -> None:
        runtime = Path(self.temp_dir.name) / "runtime"
        runtime.mkdir()
        with patch("flow_controller.infrastructure.canon_sdk.installed_sdk_directory", return_value=None), \
                patch("flow_controller.infrastructure.canon_sdk.validate_sdk", side_effect=RuntimeError("SDK is not x64")), \
                patch("ctypes.WinDLL") as load:
            self.engine._prepare_native_runtime(runtime)
        load.assert_not_called()
        self.assertFalse(self.engine._canon_sdk_ready)
        self.assertIn("SDK is not x64", self.engine._poll_events()[0]["message"])
        self.engine._cleanup_runtime()

    def test_registered_sdk_loads_dependencies_and_enables_canon(self) -> None:
        runtime = Path(self.temp_dir.name) / "runtime"
        sdk = Path(self.temp_dir.name) / "canon"
        runtime.mkdir()
        sdk.mkdir()
        with patch("flow_controller.infrastructure.canon_sdk.installed_sdk_directory", return_value=sdk), \
                patch("flow_controller.infrastructure.canon_sdk.validate_sdk", return_value={}), \
                patch("ctypes.WinDLL", return_value=object()) as load:
            self.engine._prepare_native_runtime(runtime)
        self.assertTrue(self.engine._canon_sdk_ready)
        self.assertEqual([call.args[0] for call in load.call_args_list],
                         [str(sdk / "EdsImage.dll"), str(sdk / "EDSDK.dll")])
        self.assertIsNotNone(self.engine._canon_directory_handle)
        self.engine._cleanup_runtime()
        self.assertFalse(self.engine._canon_sdk_ready)
        self.assertIsNone(self.engine._canon_directory_handle)

    def test_corrupted_registered_sdk_does_not_fall_back_silently(self) -> None:
        runtime = Path(self.temp_dir.name) / "runtime"
        runtime.mkdir()
        with patch("flow_controller.infrastructure.canon_sdk.installed_sdk_directory", side_effect=RuntimeError("SDK hash mismatch")), \
                patch("ctypes.WinDLL") as load:
            self.engine._prepare_native_runtime(runtime)
        load.assert_not_called()
        self.assertFalse(self.engine._canon_sdk_ready)
        self.assertIn("SDK hash mismatch", self.engine._poll_events()[0]["message"])
        self.engine._cleanup_runtime()


if __name__ == "__main__":
    unittest.main()
