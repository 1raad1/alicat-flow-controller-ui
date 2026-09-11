import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import setup_camera_runtime


class CameraRuntimeVerificationTests(unittest.TestCase):
    def make_runtime(self, files):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        runtime = Path(temporary.name)
        manifest = {"runtime": {}}
        for name, contents in files.items():
            path = runtime / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(contents)
            manifest["runtime"][name] = {
                "sha256": hashlib.sha256(contents).hexdigest(),
            }
        (runtime / "runtime-lock.json").write_text(json.dumps(manifest), encoding="utf-8")
        return runtime, manifest

    def test_verified_dlls_accepts_complete_matching_runtime(self):
        runtime, _ = self.make_runtime({
            "CameraControl.Devices.dll": b"camera library",
            "support.dll": b"support library",
        })

        self.assertEqual(
            setup_camera_runtime.verified_dlls(runtime),
            [runtime / "CameraControl.Devices.dll", runtime / "support.dll"],
        )

    def test_verified_dlls_rejects_tampered_dll(self):
        runtime, _ = self.make_runtime({"CameraControl.Devices.dll": b"expected"})
        (runtime / "CameraControl.Devices.dll").write_bytes(b"tampered")

        with self.assertRaisesRegex(RuntimeError, "failed verification"):
            setup_camera_runtime.verified_dlls(runtime)

    def test_verified_dlls_rejects_missing_dll(self):
        runtime, _ = self.make_runtime({"CameraControl.Devices.dll": b"expected"})
        (runtime / "CameraControl.Devices.dll").unlink()

        with self.assertRaisesRegex(RuntimeError, "Missing camera library"):
            setup_camera_runtime.verified_dlls(runtime)

    def test_verified_dlls_rejects_unlisted_dll(self):
        runtime, _ = self.make_runtime({"CameraControl.Devices.dll": b"expected"})
        (runtime / "unexpected.dll").write_bytes(b"unexpected")

        with self.assertRaisesRegex(RuntimeError, "Unlisted camera DLLs"):
            setup_camera_runtime.verified_dlls(runtime)

    def test_verified_dlls_rejects_manifest_path_traversal(self):
        runtime, manifest = self.make_runtime({"CameraControl.Devices.dll": b"expected"})
        manifest["runtime"]["../outside.dll"] = {
            "sha256": hashlib.sha256(b"outside").hexdigest(),
        }
        (runtime / "runtime-lock.json").write_text(json.dumps(manifest), encoding="utf-8")

        with self.assertRaisesRegex(RuntimeError, "Invalid camera library path"):
            setup_camera_runtime.verified_dlls(runtime)


class CameraRuntimeSetupTests(unittest.TestCase):
    def test_main_does_not_unblock_when_verification_fails(self):
        with patch.object(setup_camera_runtime.sys, "platform", "win32"), \
                patch.object(setup_camera_runtime.sys, "maxsize", 2**63 - 1), \
                patch.object(setup_camera_runtime, "check_framework"), \
                patch.object(setup_camera_runtime, "verified_dlls",
                             side_effect=RuntimeError("bad runtime")), \
                patch.object(setup_camera_runtime, "unblock_dlls") as unblock, \
                patch.object(setup_camera_runtime, "check_library_load") as load:
            self.assertEqual(setup_camera_runtime.main(), 1)

        unblock.assert_not_called()
        load.assert_not_called()


if __name__ == "__main__":
    unittest.main()
