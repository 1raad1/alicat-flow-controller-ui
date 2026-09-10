from __future__ import annotations

from pathlib import Path
from enum import Enum
import struct
import tempfile
import threading
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

    def test_bulb_busy_state_spans_start_and_stop(self) -> None:
        self.engine.open()
        self.engine.execute("bulb_start", {})
        self.assertTrue(self.engine.snapshot()["busy"])

        self.engine.execute("bulb_stop", {})

        self.assertFalse(self.engine.snapshot()["busy"])

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

    def test_missing_canon_native_sdk_is_a_nonfatal_actionable_event(self) -> None:
        runtime = Path(self.temp_dir.name) / "runtime"
        runtime.mkdir()

        self.engine._prepare_native_runtime(runtime)
        events = self.engine._poll_events()

        self.assertEqual(events[0]["type"], "error")
        self.assertIn("EDSDK.dll", events[0]["message"])
        self.assertIn("EdsImage.dll", events[0]["message"])
        self.assertIn("other digiCamControl drivers remain available", events[0]["message"])
        self.engine._cleanup_runtime()

    def test_wrong_native_sdk_architecture_is_reported_before_loading(self) -> None:
        runtime = Path(self.temp_dir.name) / "runtime"
        runtime.mkdir()
        pe = bytearray(256)
        struct.pack_into("<I", pe, 0x3C, 128)
        pe[128:132] = b"PE\0\0"
        wrong_machine = 0x14C if struct.calcsize("P") == 8 else 0x8664
        struct.pack_into("<H", pe, 132, wrong_machine)
        (runtime / "EDSDK.dll").write_bytes(pe)
        (runtime / "EdsImage.dll").write_bytes(pe)

        self.engine._prepare_native_runtime(runtime)
        events = self.engine._poll_events()

        if struct.calcsize("P") == 8:
            self.assertIn("EDSDK.dll is 32-bit", events[0]["message"])
            self.assertIn("Python is 64-bit", events[0]["message"])
        else:
            self.assertIn("EDSDK.dll is 64-bit", events[0]["message"])
            self.assertIn("Python is 32-bit", events[0]["message"])
        self.engine._cleanup_runtime()

    def test_unmatched_canon_sdk_versions_are_rejected(self) -> None:
        runtime = Path(self.temp_dir.name) / "runtime"
        runtime.mkdir()
        pe = bytearray(256)
        struct.pack_into("<I", pe, 0x3C, 128)
        pe[128:132] = b"PE\0\0"
        machine = 0x8664 if struct.calcsize("P") == 8 else 0x14C
        struct.pack_into("<H", pe, 132, machine)
        (runtime / "EDSDK.dll").write_bytes(pe)
        (runtime / "EdsImage.dll").write_bytes(pe)

        with patch.object(DccEngine, "_file_version", return_value="99.0.0.0"):
            self.engine._prepare_native_runtime(runtime)
        events = self.engine._poll_events()

        self.assertFalse(self.engine._canon_sdk_ready)
        self.assertIn("does not match this digiCamControl binding", events[0]["message"])
        self.assertIn("expected 13.18.40.0", events[0]["message"])
        self.engine._cleanup_runtime()


if __name__ == "__main__":
    unittest.main()
