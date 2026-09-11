from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from scripts import package_windows


class WindowsPackageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.temporary_root = Path(self.temporary.name)
        self.root = self.temporary_root / "source"
        runtime = self.root / "flow_controller" / "camera_runtime"
        runtime.mkdir(parents=True)
        (self.root / "app.py").write_bytes(b"tracked application\n")
        (self.root / "private-notes.txt").write_bytes(b"must stay private\n")
        managed = b"fake managed CameraControl assembly"
        (runtime / "CameraControl.Devices.dll").write_bytes(managed)
        lock = {
            "build": {"source": "test fixture"},
            "runtime": {
                "CameraControl.Devices.dll": {
                    "sha256": hashlib.sha256(managed).hexdigest(),
                    "bytes": len(managed),
                }
            },
        }
        lock_text = json.dumps(lock, indent=2, sort_keys=True) + "\n"
        (runtime / "runtime-lock.json").write_text(lock_text, encoding="utf-8")
        third_party = self.root / "third_party" / "digicamcontrol"
        third_party.mkdir(parents=True)
        (third_party / "runtime-lock.json").write_text(lock_text, encoding="utf-8")

        self.sdk_input = self.temporary_root / "official-sdk.zip"
        self.sdk_input.write_bytes(b"official Canon SDK test fixture")
        self.notice = self.temporary_root / "canon-runtime-notice.txt"
        self.notice.write_bytes(b"Canon redistribution notice fixture\n")
        self.installed = self.temporary_root / "isolated-installed-sdk"
        self.installed.mkdir()
        self.canon_files = {
            "EDSDK.dll": b"official EDSDK fixture bytes",
            "EdsImage.dll": b"official EdsImage fixture bytes",
            "canon-sdk-manifest.json": b'{"fixture": true}\n',
        }
        for name, contents in self.canon_files.items():
            (self.installed / name).write_bytes(contents)
        self.metadata = {
            "files": {
                name: {
                    "sha256": hashlib.sha256(contents).hexdigest(),
                    "bytes": len(contents),
                }
                for name, contents in self.canon_files.items()
                if name.lower().endswith(".dll")
            },
            "versions": {
                "EDSDK.dll": "13.20.40.0",
                "EdsImage.dll": "3.20.10.2",
            },
        }
        self.tracked = [
            "app.py",
            "flow_controller/camera_runtime/CameraControl.Devices.dll",
            "flow_controller/camera_runtime/runtime-lock.json",
            "third_party/digicamcontrol/runtime-lock.json",
        ]

    @staticmethod
    def _snapshot(directory: Path) -> dict[str, bytes]:
        return {
            path.relative_to(directory).as_posix(): path.read_bytes()
            for path in directory.rglob("*")
            if path.is_file()
        }

    def _git_output(self) -> str:
        return "\0".join(self.tracked) + "\0"

    def _add_bundled_canon_sdk(self) -> bytes:
        runtime = self.root / "flow_controller" / "camera_runtime"
        destination = runtime / "canon"
        destination.mkdir()
        for name, contents in self.canon_files.items():
            (destination / name).write_bytes(contents)
        bundled_notice = b"tracked Canon redistribution notice\n"
        (destination / "NOTICE.txt").write_bytes(bundled_notice)
        provenance = b"Existing runtime provenance\n"
        (runtime / "PROVENANCE.md").write_bytes(provenance)

        bundled_paths = [
            path for path in sorted(destination.iterdir()) if path.is_file()
        ] + [runtime / "PROVENANCE.md"]
        self.tracked.extend(
            path.relative_to(self.root).as_posix() for path in bundled_paths
        )
        for manifest_path in (
            runtime / "runtime-lock.json",
            self.root / "third_party" / "digicamcontrol" / "runtime-lock.json",
        ):
            lock = json.loads(manifest_path.read_text(encoding="utf-8"))
            for path in bundled_paths:
                relative = path.relative_to(runtime).as_posix()
                contents = path.read_bytes()
                lock["runtime"][relative] = {
                    "sha256": hashlib.sha256(contents).hexdigest(),
                    "bytes": len(contents),
                }
            manifest_path.write_text(
                json.dumps(lock, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        return provenance

    def test_package_contains_only_tracked_app_and_verified_canon_runtime(self) -> None:
        output = self.temporary_root / "release" / "flow-controller.zip"
        profile = self.temporary_root / "user-profile"
        profile.mkdir()
        (profile / "sentinel.txt").write_bytes(b"unchanged")
        source_before = self._snapshot(self.root)
        profile_before = self._snapshot(profile)
        sdk_before = self._snapshot(self.installed)

        with (
            patch.object(package_windows.subprocess, "check_output", return_value=self._git_output()),
            patch.object(package_windows, "install_sdk", return_value=self.installed) as install,
            patch.object(package_windows, "validate_sdk", return_value=self.metadata) as validate,
            patch.dict("os.environ", {"USERPROFILE": str(profile), "HOME": str(profile)}),
        ):
            result = package_windows.package(self.root, self.sdk_input, self.notice, output)

        self.assertEqual(result, output.resolve())
        install.assert_called_once()
        self.assertEqual(install.call_args.args, (self.sdk_input,))
        self.assertNotEqual(Path(install.call_args.kwargs["home"]).resolve(), profile.resolve())
        self.assertEqual(validate.call_count, 2)
        for call in validate.call_args_list:
            self.assertTrue(call.kwargs["verify_manifest"])
        self.assertEqual(self._snapshot(self.root), source_before)
        self.assertEqual(self._snapshot(profile), profile_before)
        self.assertEqual(self._snapshot(self.installed), sdk_before)

        with zipfile.ZipFile(output) as archive:
            names = set(archive.namelist())
            self.assertIn("flow-controller/app.py", names)
            self.assertNotIn("flow-controller/private-notes.txt", names)
            self.assertEqual(archive.read("flow-controller/app.py"), b"tracked application\n")
            for name, contents in self.canon_files.items():
                self.assertEqual(
                    archive.read(f"flow-controller/flow_controller/camera_runtime/canon/{name}"),
                    contents,
                )
            self.assertEqual(
                archive.read(
                    "flow-controller/flow_controller/camera_runtime/canon/NOTICE.txt"
                ),
                self.notice.read_bytes(),
            )
            provenance = archive.read(
                "flow-controller/flow_controller/camera_runtime/PROVENANCE.md"
            ).decode("utf-8")
            self.assertIn("Canon-enabled release packaging", provenance)
            self.assertIn("EDSDK: 13.20.40.0; EdsImage: 3.20.10.2.", provenance)
            primary = archive.read(
                "flow-controller/flow_controller/camera_runtime/runtime-lock.json"
            )
            mirror = archive.read(
                "flow-controller/third_party/digicamcontrol/runtime-lock.json"
            )
            self.assertEqual(primary, mirror)
            manifest = json.loads(primary)
            self.assertEqual(
                manifest["build"]["canon_native"],
                ["canon/EDSDK.dll", "canon/EdsImage.dll"],
            )
            self.assertEqual(manifest["build"]["canon_versions"], self.metadata["versions"])
            prefix = "flow-controller/flow_controller/camera_runtime/"
            for relative, entry in manifest["runtime"].items():
                contents = archive.read(prefix + relative)
                self.assertEqual(entry["bytes"], len(contents))
                self.assertEqual(entry["sha256"], hashlib.sha256(contents).hexdigest())

    def test_failed_sdk_install_does_not_create_archive_or_mutate_source(self) -> None:
        output = self.temporary_root / "failed.zip"
        source_before = self._snapshot(self.root)
        with (
            patch.object(package_windows.subprocess, "check_output", return_value=self._git_output()),
            patch.object(package_windows, "install_sdk", side_effect=RuntimeError("native probe failed")),
        ):
            with self.assertRaisesRegex(RuntimeError, "native probe failed"):
                package_windows.package(self.root, self.sdk_input, self.notice, output)

        self.assertFalse(output.exists())
        self.assertEqual(self._snapshot(self.root), source_before)

    def test_package_uses_tracked_canon_bundle_without_importing_it(self) -> None:
        provenance = self._add_bundled_canon_sdk()
        output = self.temporary_root / "bundled.zip"

        with (
            patch.object(package_windows.subprocess, "check_output", return_value=self._git_output()),
            patch.object(package_windows, "install_sdk") as install,
            patch.object(package_windows, "validate_sdk", return_value=self.metadata) as validate,
            patch.object(package_windows, "_run_probe") as probe,
        ):
            package_windows.package(self.root, None, None, output)

        install.assert_not_called()
        self.assertEqual(validate.call_count, 2)
        for call in validate.call_args_list:
            self.assertTrue(call.kwargs["verify_manifest"])
            self.assertEqual(call.args[0].name, "canon")
        probe.assert_called_once_with(validate.call_args_list[0].args[0])

        with zipfile.ZipFile(output) as archive:
            prefix = "flow-controller/flow_controller/camera_runtime/"
            for name, contents in self.canon_files.items():
                self.assertEqual(archive.read(prefix + "canon/" + name), contents)
            self.assertEqual(
                archive.read(prefix + "canon/NOTICE.txt"),
                b"tracked Canon redistribution notice\n",
            )
            self.assertEqual(archive.read(prefix + "PROVENANCE.md"), provenance)
            manifest = json.loads(archive.read(prefix + "runtime-lock.json"))
            self.assertEqual(manifest["build"]["canon_versions"], self.metadata["versions"])

    def test_missing_bundled_sdk_requires_explicit_sdk_and_notice(self) -> None:
        output = self.temporary_root / "missing-canon.zip"
        with patch.object(
            package_windows.subprocess, "check_output", return_value=self._git_output()
        ):
            with self.assertRaisesRegex(RuntimeError, "does not contain a bundled Canon SDK"):
                package_windows.package(self.root, None, None, output)

        self.assertFalse(output.exists())

    def test_notice_without_sdk_is_rejected_before_packaging(self) -> None:
        output = self.temporary_root / "notice-only.zip"
        with patch.object(package_windows.subprocess, "check_output") as git:
            with self.assertRaisesRegex(RuntimeError, "--canon-notice requires --canon-sdk"):
                package_windows.package(self.root, None, self.notice, output)

        git.assert_not_called()

    def test_explicit_sdk_refuses_to_replace_tracked_bundle(self) -> None:
        self._add_bundled_canon_sdk()
        output = self.temporary_root / "replacement.zip"
        with (
            patch.object(package_windows.subprocess, "check_output", return_value=self._git_output()),
            patch.object(package_windows, "install_sdk") as install,
        ):
            with self.assertRaisesRegex(RuntimeError, "already contains a Canon bundle"):
                package_windows.package(self.root, self.sdk_input, self.notice, output)

        install.assert_not_called()
        self.assertFalse(output.exists())

    def test_existing_output_is_refused_without_touching_it(self) -> None:
        output = self.temporary_root / "existing.zip"
        output.write_bytes(b"existing release")
        with (
            patch.object(package_windows.subprocess, "check_output") as git,
            patch.object(package_windows, "install_sdk") as install,
        ):
            with self.assertRaisesRegex(RuntimeError, "Output already exists"):
                package_windows.package(self.root, self.sdk_input, self.notice, output)

        self.assertEqual(output.read_bytes(), b"existing release")
        git.assert_not_called()
        install.assert_not_called()


if __name__ == "__main__":
    unittest.main()
