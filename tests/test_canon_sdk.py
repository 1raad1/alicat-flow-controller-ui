from __future__ import annotations

import hashlib
import json
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from flow_controller.infrastructure import canon_sdk


def _pe(machine: int, marker: str = "") -> bytes:
    data = bytearray(256)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 128)
    data[128:132] = b"PE\0\0"
    struct.pack_into("<H", data, 132, machine)
    data.extend(marker.encode("ascii"))
    return bytes(data)


def _fake_file_version(path: str | Path) -> str:
    path = Path(path)
    content = path.read_bytes()
    family = next((family for family in ("18", "19", "20") if f"v{family}".encode() in content), "18")
    if family == "19":
        return "13.19.0.6400"
    if path.name.casefold() == "edsdk.dll":
        return f"13.{family}.40.0"
    return f"3.{family}.10.2"


def _write_pair(directory: Path, *, machine: int = 0x8664, family: str = "18") -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "EDSDK.dll").write_bytes(_pe(machine, f"v{family}"))
    (directory / "EdsImage.dll").write_bytes(_pe(machine, f"v{family}"))


class CanonSdkTests(unittest.TestCase):
    def test_pe_architecture_recognises_x64_and_rejects_non_pe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            x64 = root / "x64.dll"
            x86 = root / "x86.dll"
            bad = root / "bad.dll"
            x64.write_bytes(_pe(0x8664))
            x86.write_bytes(_pe(0x14C))
            bad.write_bytes(b"not a PE")

            self.assertEqual(canon_sdk.pe_architecture(x64), "64-bit")
            self.assertEqual(canon_sdk.pe_architecture(x86), "32-bit")
            self.assertIsNone(canon_sdk.pe_architecture(bad))

    def test_validate_accepts_supported_families_and_rejects_wrong_architecture(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for family in ("18", "19", "20"):
                sdk = root / family
                _write_pair(sdk, family=family)
                with patch.object(canon_sdk, "file_version", side_effect=_fake_file_version):
                    metadata = canon_sdk.validate_sdk(sdk)
                expected = "13.19.0.6400" if family == "19" else f"13.{family}.40.0"
                self.assertEqual(metadata["versions"]["EDSDK.dll"], expected)
                if family == "19":
                    self.assertEqual(metadata["versions"]["EdsImage.dll"], expected)

            wrong = root / "x86"
            _write_pair(wrong, machine=0x14C)
            with patch.object(canon_sdk, "file_version", side_effect=_fake_file_version):
                with self.assertRaisesRegex(RuntimeError, "EDSDK.dll is 32-bit"):
                    canon_sdk.validate_sdk(wrong)

    def test_validate_rejects_mismatched_supported_families(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for image_family in ("18", "20"):
                with self.subTest(image_family=image_family):
                    sdk = root / image_family
                    _write_pair(sdk, family="19")
                    (sdk / "EdsImage.dll").write_bytes(_pe(0x8664, f"v{image_family}"))
                    with patch.object(canon_sdk, "file_version", side_effect=_fake_file_version):
                        with self.assertRaisesRegex(RuntimeError, "Unsupported or mismatched"):
                            canon_sdk.validate_sdk(sdk)

    def test_install_zip_selects_newest_x64_pair_and_direct_companions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive_path = root / "canon.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                for directory, machine, family in (
                    ("SDK/EDSDK/Dll", 0x14C, "20"),
                    ("SDK/EDSDK_64/Dll-old", 0x8664, "18"),
                    ("SDK/EDSDK_64/Dll", 0x8664, "20"),
                ):
                    archive.writestr(f"{directory}/EDSDK.dll", _pe(machine, f"v{family}"))
                    archive.writestr(f"{directory}/EdsImage.dll", _pe(machine, f"v{family}"))
                    archive.writestr(f"{directory}/companion.dll", _pe(machine, f"v{family}"))
                archive.writestr("SDK/EDSDK_64/Dll/readme.txt", "documentation")
                archive.writestr("SDK/EDSDK_64/Dll/app/plugin.dll", _pe(0x8664, "v20"))

            home = root / "home"
            with (
                patch.object(canon_sdk, "file_version", side_effect=_fake_file_version),
                patch.object(canon_sdk, "_unblock"),
                patch.object(canon_sdk, "_run_probe") as probe,
            ):
                installed = canon_sdk.install_sdk(archive_path, home=home)

            self.assertEqual(_fake_file_version(installed / "EDSDK.dll"), "13.20.40.0")
            self.assertTrue((installed / "companion.dll").is_file())
            self.assertFalse((installed / "readme.txt").exists())
            self.assertFalse((installed / "plugin.dll").exists())
            probe.assert_called_once_with(unittest.mock.ANY)
            config = json.loads((home / "canon-sdk.json").read_text(encoding="utf-8"))
            self.assertEqual(Path(config["directory"]), installed)

    def test_install_rejects_zip_dll_name_that_can_create_windows_ads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive_path = root / "unsafe.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("SDK/EDSDK.dll", _pe(0x8664, "v20"))
                archive.writestr("SDK/EdsImage.dll", _pe(0x8664, "v20"))
                archive.writestr("SDK/helper:stream.dll", _pe(0x8664))

            with self.assertRaisesRegex(RuntimeError, "unsafe Windows DLL filename"):
                canon_sdk.install_sdk(archive_path, home=root / "home")

    def test_unblock_passes_literal_directory_through_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "SDK [official] & files"
            directory.mkdir()
            completed = subprocess.CompletedProcess([], 0, "", "")
            with (
                patch.object(canon_sdk.sys, "platform", "win32"),
                patch.object(canon_sdk.subprocess, "run", return_value=completed) as run,
            ):
                canon_sdk._unblock(directory)

            arguments = run.call_args.args[0]
            options = run.call_args.kwargs
            self.assertNotIn(str(directory), arguments)
            self.assertEqual(options["env"]["FLOW_CANON_SETUP_DIR"], str(directory.resolve()))
            self.assertIn("-LiteralPath $env:FLOW_CANON_SETUP_DIR", arguments[-1])

    def test_manifest_detects_changed_missing_and_extra_dlls(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            _write_pair(source)
            (source / "helper.dll").write_bytes(_pe(0x8664, "helper"))
            with (
                patch.object(canon_sdk, "file_version", side_effect=_fake_file_version),
                patch.object(canon_sdk, "_unblock"),
                patch.object(canon_sdk, "_run_probe"),
            ):
                installed = canon_sdk.install_sdk(source, home=root / "home")

            with patch.object(canon_sdk, "file_version", side_effect=_fake_file_version):
                canon_sdk.validate_sdk(installed, verify_manifest=True)
                original = (installed / "helper.dll").read_bytes()
                (installed / "helper.dll").write_bytes(original + b"changed")
                with self.assertRaisesRegex(RuntimeError, "manifest"):
                    canon_sdk.validate_sdk(installed, verify_manifest=True)
                (installed / "helper.dll").write_bytes(original)
                (installed / "extra.dll").write_bytes(_pe(0x8664))
                with self.assertRaisesRegex(RuntimeError, "manifest"):
                    canon_sdk.validate_sdk(installed, verify_manifest=True)
                (installed / "extra.dll").unlink()
                (installed / "helper.dll").unlink()
                with self.assertRaisesRegex(RuntimeError, "manifest"):
                    canon_sdk.validate_sdk(installed, verify_manifest=True)

    def test_failed_probe_preserves_previous_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            home = root / "home"
            home.mkdir()
            _write_pair(source)
            old_config = b'{"directory": "C:\\\\previous"}\n'
            (home / "canon-sdk.json").write_bytes(old_config)

            with (
                patch.object(canon_sdk, "file_version", side_effect=_fake_file_version),
                patch.object(canon_sdk, "_unblock"),
                patch.object(canon_sdk, "_run_probe", side_effect=RuntimeError("probe crashed")),
            ):
                with self.assertRaisesRegex(RuntimeError, "probe crashed"):
                    canon_sdk.install_sdk(source, home=home)

            self.assertEqual((home / "canon-sdk.json").read_bytes(), old_config)
            self.assertEqual(list((home / "canon-sdk").iterdir()), [])

    def test_file_hash_metadata_covers_every_direct_dll(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            sdk = Path(temporary)
            _write_pair(sdk)
            helper = b"helper contents"
            (sdk / "helper.dll").write_bytes(helper)
            with patch.object(canon_sdk, "file_version", side_effect=_fake_file_version):
                metadata = canon_sdk.validate_sdk(sdk)

            self.assertEqual(
                metadata["files"]["helper.dll"],
                {"sha256": hashlib.sha256(helper).hexdigest(), "bytes": len(helper)},
            )


if __name__ == "__main__":
    unittest.main()
