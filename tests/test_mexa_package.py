"""Smoke-test exported MEXA packages without the checkout or flow dependencies."""

from contextlib import contextmanager
import os
from pathlib import Path, PurePosixPath
import site
import subprocess
import sys
import tempfile
import textwrap
import tomllib
import unittest
import zipfile

from build_mexa_package import build


BRIDGE_MODULES = {
    "__init__.py", "app.py", "bridge.py", "protocol.py", "records.py",
    "transport.py", "relay.py",
}
BRIDGE_FILES = {
    *(f"mexa_bridge/{module}" for module in BRIDGE_MODULES),
    "install_mexa_bridge.bat", "run_mexa_bridge.bat", "requirements-mexa.txt",
    "docs/MEXA_SETUP.md", "docs/MEXA_QUICK_TUNNEL.md",
}


class MexaPackageTests(unittest.TestCase):
    @contextmanager
    def exported(self):
        with tempfile.TemporaryDirectory(prefix="mexa-package-test-") as temporary:
            workspace = Path(temporary)
            archive_path = build(workspace / "package.zip")
            package_name = "MEXA-584L-bridge"
            with zipfile.ZipFile(archive_path) as archive:
                members = [PurePosixPath(name) for name in archive.namelist()]
                self.assertTrue(members)
                for member in members:
                    self.assertFalse(member.is_absolute())
                    self.assertNotIn("..", member.parts)
                    self.assertEqual(member.parts[0], package_name)
                    self.assertNotIn("flow_controller", member.parts)
                modules = {
                    member.name for member in members
                    if member.parent == PurePosixPath(package_name, "mexa_bridge")
                }
                self.assertEqual(modules, BRIDGE_MODULES)
                self.assertEqual(
                    {member.relative_to(package_name).as_posix() for member in members},
                    BRIDGE_FILES,
                )
                # This archive was generated above from the repository's explicit allowlist.
                archive.extractall(workspace / "extracted")
            yield workspace / "extracted" / package_name

    def run_isolated(self, root, body):
        blocked = ["flow_controller", "alicat", "numpy", "scipy", "sklearn"]
        # Reuse installed dependencies, including Windows user-site installations,
        # but never inherit the checkout or arbitrary parent sys.path entries.
        dependency_paths = [*site.getsitepackages(), site.getusersitepackages()]
        prelude = textwrap.dedent(f"""
            import importlib.abc
            from pathlib import Path
            import sys

            root = Path(sys.argv[1]).resolve()
            sys.path.extend({dependency_paths!r})
            sys.path.insert(0, str(root))
            blocked = {blocked!r}
            blocked_attempts = []

            class RejectFlowDependencies(importlib.abc.MetaPathFinder):
                def find_spec(self, fullname, path=None, target=None):
                    if fullname.split('.')[0] in blocked:
                        blocked_attempts.append(fullname)
                        raise ImportError('Forbidden standalone dependency: ' + fullname)

            sys.meta_path.insert(0, RejectFlowDependencies())
            import mexa_bridge
            assert Path(mexa_bridge.__file__).resolve().is_relative_to(root), mexa_bridge.__file__
        """)
        code = prelude + "\n" + textwrap.dedent(body) + "\nassert not blocked_attempts, blocked_attempts\n"
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", code, str(root)],
            cwd=root,
            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def test_bridge_zip_is_independent_and_window_constructs(self):
        with self.exported() as root:
            self.run_isolated(root, """
                from unittest.mock import patch
                from PySide6.QtWidgets import QApplication
                from mexa_bridge.app import BridgeWindow

                app = QApplication([])
                # Do not enumerate ports or start acquisition in this packaging smoke test.
                with patch.object(BridgeWindow, '_ports', return_value=None):
                    window = BridgeWindow()
                assert window.bridge is None
                window.close()
                app.processEvents()
                app.quit()
            """)

    def test_bridge_modules_import_without_burner_dependencies(self):
        with self.exported() as root:
            self.run_isolated(root, """
                import importlib.util
                from mexa_bridge import app, bridge, protocol, records, transport, relay
                assert callable(app.main)
                assert importlib.util.find_spec('mexa_bridge.relay_server') is None
                assert importlib.util.find_spec('mexa_bridge.relay_host') is None
            """)

    def test_build_preserves_existing_destination(self):
        with tempfile.TemporaryDirectory(prefix="mexa-package-test-") as temporary:
            destination = build(Path(temporary) / "package.zip")
            original = destination.read_bytes()
            with self.assertRaises(FileExistsError):
                build(destination)
            self.assertEqual(destination.read_bytes(), original)

    def test_standalone_relay_build_option_is_rejected(self):
        script = Path(__file__).resolve().parents[1] / "build_mexa_package.py"
        with tempfile.TemporaryDirectory(prefix="mexa-package-test-") as temporary:
            destination = Path(temporary) / "relay.zip"
            result = subprocess.run(
                [sys.executable, str(script), str(destination), "--relay"],
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("unrecognized arguments: --relay", result.stderr)
            self.assertFalse(destination.exists())

    def test_project_exposes_no_standalone_relay_entrypoint(self):
        project = Path(__file__).resolve().parents[1] / "pyproject.toml"
        config = tomllib.loads(project.read_text(encoding="utf-8"))
        self.assertEqual(config["project"]["scripts"], {
            "flow-controller-v3": "flow_controller.ui.qt_main_window:main",
            "mexa-584l-bridge": "mexa_bridge.app:main",
        })


if __name__ == "__main__":
    unittest.main()
