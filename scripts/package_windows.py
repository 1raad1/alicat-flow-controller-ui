"""Build a Windows application ZIP with an authorised Canon runtime included."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from flow_controller.infrastructure.canon_sdk import install_sdk, validate_sdk
from scripts.setup_camera_runtime import verified_dlls


def package(root: Path, sdk: Path, notice: Path, output: Path) -> Path:
    """Package tracked application files; never modify the checkout or user SDK."""
    root, output = root.resolve(), output.resolve()
    if output.exists():
        raise RuntimeError(f"Output already exists: {output}")
    if not notice.is_file():
        raise RuntimeError("Supply the Canon runtime redistribution notice from your SDK package")
    tracked = subprocess.check_output(
        ["git", "-C", str(root), "ls-files", "-z"], text=True
    ).split("\0")
    output.parent.mkdir(parents=True, exist_ok=True)
    # TemporaryDirectory removes only its own absolute, generated directory.
    with tempfile.TemporaryDirectory(prefix="flow-package-") as temporary:
        staging = Path(temporary)
        app = staging / "flow-controller"
        for relative in filter(None, tracked):
            candidate = root / relative
            source = candidate.resolve()
            if not source.is_relative_to(root) or candidate.is_symlink():
                raise RuntimeError(f"Invalid tracked package path: {relative}")
            target = app / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        runtime = app / "flow_controller" / "camera_runtime"
        verified_dlls(runtime)
        destination = runtime / "canon"
        if destination.exists():
            raise RuntimeError("Source already contains a Canon bundle; use a clean source checkout")
        imported = install_sdk(sdk, home=staging / "sdk-validation")
        metadata = validate_sdk(imported, verify_manifest=True)
        shutil.copytree(imported, destination)
        shutil.copyfile(notice, destination / ("NOTICE" + notice.suffix))
        provenance_path = runtime / "PROVENANCE.md"
        with provenance_path.open("a", encoding="utf-8") as provenance:
            provenance.write(
                "\n## Canon-enabled release packaging\n\n"
                "The sections above describe the base device-engine build. This release "
                "also includes the developer-supplied Canon runtime in `canon/`, with "
                "its redistribution notice and verified file manifest.\n\n"
                f"EDSDK: {metadata['versions']['EDSDK.dll']}; "
                f"EdsImage: {metadata['versions']['EdsImage.dll']}.\n"
            )
        manifest_path = runtime / "runtime-lock.json"
        lock = json.loads(manifest_path.read_text(encoding="utf-8"))
        lock.setdefault("build", {})["canon_native"] = [
            "canon/" + name for name in metadata["files"]
        ]
        lock["build"]["canon_versions"] = metadata["versions"]
        lock["runtime"] = {
            path.relative_to(runtime).as_posix(): {
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "bytes": path.stat().st_size,
            }
            for path in sorted(runtime.rglob("*"))
            if path.is_file() and path != manifest_path
        }
        text = json.dumps(lock, indent=2, sort_keys=True) + "\n"
        manifest_path.write_text(text, encoding="utf-8")
        (app / "third_party/digicamcontrol/runtime-lock.json").write_text(text, encoding="utf-8")
        verified_dlls(runtime)
        validate_sdk(destination, verify_manifest=True)
        archive_path = staging / "application.zip"
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(app.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(staging))
        # Exclusive creation keeps an existing release safe, even on a race.
        with archive_path.open("rb") as source, output.open("xb") as target:
            shutil.copyfileobj(source, target)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--canon-sdk", type=Path, required=True, help="Official Windows SDK ZIP or folder")
    parser.add_argument("--canon-notice", type=Path, required=True, help="Runtime notice supplied with the SDK")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        output = package(ROOT, args.canon_sdk, args.canon_notice, args.output)
    except Exception as exc:
        print(f"Packaging failed: {exc}", file=sys.stderr)
        return 1
    print(f"Created Canon-enabled Windows application: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
