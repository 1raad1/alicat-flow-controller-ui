"""Build a small reader-only ZIP; never includes credentials or HORIBA binaries."""

import argparse
from pathlib import Path
import zipfile


def build(destination):
    root = Path(__file__).resolve().parent
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    sources = [root / "flow_controller" / "__init__.py",
               *sorted((root / "flow_controller" / "mexa").glob("*.py")),
               root / "install_mexa_bridge.bat", root / "run_mexa_bridge.bat",
               root / "requirements-mexa.txt", root / "docs" / "MEXA_SETUP.md"]
    # Exclusive creation makes rerunning a build safe for previously delivered files.
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for source in sources:
            relative = source.relative_to(root)
            archive.write(source, str(Path("MEXA-584L-bridge") / relative))
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", help="New ZIP path (must not already exist)")
    print(build(parser.parse_args().destination))
