"""Smoke-test exported MEXA packages without the checkout or flow dependencies."""

from contextlib import contextmanager
import os
from pathlib import Path, PurePosixPath
import site
import subprocess
import sys
import tempfile
import textwrap
import unittest
import zipfile

from build_mexa_package import build


BRIDGE_MODULES = {
    "__init__.py", "app.py", "bridge.py", "protocol.py", "records.py",
    "transport.py", "relay.py", "relay_server.py",
}
RELAY_MODULES = {
    "__init__.py", "records.py", "transport.py", "relay.py",
    "relay_server.py", "relay_host.py",
}


class MexaPackageTests(unittest.TestCase):
    @contextmanager
    def exported(self, *, relay=False):
        with tempfile.TemporaryDirectory(prefix="mexa-package-test-") as temporary:
            workspace = Path(temporary)
            archive_path = build(workspace / "package.zip", relay=relay)
            package_name = "MEXA-584L-relay" if relay else "MEXA-584L-bridge"
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
                self.assertEqual(modules, RELAY_MODULES if relay else BRIDGE_MODULES)
                # This archive was generated above from the repository's explicit allowlist.
                archive.extractall(workspace / "extracted")
            yield workspace / "extracted" / package_name

    def run_isolated(self, root, body, *, relay=False):
        blocked = ["flow_controller", "alicat", "numpy", "scipy", "sklearn"]
        if relay:
            blocked.extend(["PySide6", "PySide2", "serial"])
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

    def test_relay_zip_imports_without_desktop_or_serial_dependencies(self):
        with self.exported(relay=True) as root:
            self.run_isolated(root, """
                from mexa_bridge import records, transport, relay, relay_host, relay_server
                assert callable(relay_host.main)
                assert callable(relay_server.main)
            """, relay=True)

    def test_relay_help_entrypoints_work_in_exported_package(self):
        with self.exported(relay=True) as root:
            for module in ("mexa_bridge.relay_host", "mexa_bridge.relay_server"):
                with self.subTest(module=module):
                    result = self.run_isolated(root, f"""
                        import runpy
                        sys.argv = [{module!r}, '--help']
                        try:
                            runpy.run_module({module!r}, run_name='__main__')
                        except SystemExit as exc:
                            assert exc.code == 0, exc.code
                        else:
                            raise AssertionError('--help did not exit')
                    """, relay=True)
                    self.assertIn("usage:", result.stdout.lower())


if __name__ == "__main__":
    unittest.main()
