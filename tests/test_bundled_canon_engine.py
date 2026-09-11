from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, call, patch

from flow_controller.infrastructure.digicam_engine import DccEngine


class BundledCanonEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.runtime = Path(self.temporary.name) / "runtime"
        self.canon = self.runtime / "canon"
        self.canon.mkdir(parents=True)
        self.engine = DccEngine(runtime_dir=self.runtime)
        self.addCleanup(self.engine._cleanup_runtime)

    def test_nested_bundle_is_validated_and_preferred_over_installed_sdk(self) -> None:
        runtime_handle = MagicMock()
        canon_handle = MagicMock()
        with (
            patch("flow_controller.infrastructure.canon_sdk.installed_sdk_directory") as installed,
            patch("flow_controller.infrastructure.canon_sdk.validate_sdk", return_value={}) as validate,
            patch("flow_controller.infrastructure.digicam_engine.os.add_dll_directory",
                  side_effect=[runtime_handle, canon_handle]) as add_directory,
            patch("flow_controller.infrastructure.digicam_engine.ctypes.WinDLL",
                  return_value=object()) as load,
        ):
            self.engine._prepare_native_runtime(self.runtime)

        installed.assert_not_called()
        validate.assert_called_once_with(self.canon, verify_manifest=True)
        self.assertEqual(
            add_directory.call_args_list,
            [call(str(self.runtime)), call(str(self.canon))],
        )
        self.assertEqual(
            [item.args[0] for item in load.call_args_list],
            [str(self.canon / "EdsImage.dll"), str(self.canon / "EDSDK.dll")],
        )
        self.assertTrue(self.engine._canon_sdk_ready)
        self.assertIs(self.engine._canon_directory_handle, canon_handle)

    def test_malformed_bundle_disables_canon_without_installed_sdk_fallback(self) -> None:
        runtime_handle = MagicMock()
        with (
            patch("flow_controller.infrastructure.canon_sdk.installed_sdk_directory") as installed,
            patch("flow_controller.infrastructure.canon_sdk.validate_sdk",
                  side_effect=RuntimeError("bundle hash mismatch")) as validate,
            patch("flow_controller.infrastructure.digicam_engine.os.add_dll_directory",
                  return_value=runtime_handle) as add_directory,
            patch("flow_controller.infrastructure.digicam_engine.ctypes.WinDLL") as load,
        ):
            self.engine._prepare_native_runtime(self.runtime)

        installed.assert_not_called()
        validate.assert_called_once_with(self.canon, verify_manifest=True)
        add_directory.assert_called_once_with(str(self.runtime))
        load.assert_not_called()
        self.assertFalse(self.engine._canon_sdk_ready)
        self.assertIsNone(self.engine._canon_directory_handle)
        self.assertIn("bundle hash mismatch", self.engine._poll_events()[0]["message"])


if __name__ == "__main__":
    unittest.main()
