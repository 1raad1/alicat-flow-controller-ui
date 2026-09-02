"""Build a small reader-only ZIP; never includes credentials or HORIBA binaries."""

import argparse
from pathlib import Path
import zipfile


def build(destination, *, relay=False):
    root = Path(__file__).resolve().parent
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if relay:
        sources = [*(root / "mexa_bridge" / name for name in
                     ("__init__.py", "records.py", "transport.py", "relay.py", "relay_server.py", "relay_host.py")),
                   root / "requirements-relay.txt", root / "docs" / "MEXA_RELAY.md",
                   root / "docs" / "MEXA_QUICK_TUNNEL.md",
                   root / "run_mexa_relay_local.bat",
                   root / "install_relay_host.sh", root / "run_relay_host.sh",
                   root / "CACHYOS_START_HERE.md",
                   root / "deploy" / "mexa-relay" / "Caddyfile.example",
                   root / "deploy" / "mexa-relay" / "Dockerfile"]
    else:
        sources = [*(root / "mexa_bridge" / name for name in
                     ("__init__.py", "app.py", "bridge.py", "protocol.py", "records.py",
                      "transport.py", "relay.py", "relay_server.py")),
                   root / "install_mexa_bridge.bat", root / "run_mexa_bridge.bat",
                   root / "run_mexa_relay_local.bat",
                   root / "requirements-mexa.txt", root / "docs" / "MEXA_SETUP.md",
                   root / "docs" / "MEXA_RELAY.md", root / "docs" / "MEXA_QUICK_TUNNEL.md"]
    # Exclusive creation makes rerunning a build safe for previously delivered files.
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for source in sources:
            relative = source.relative_to(root)
            name = (Path("MEXA-584L-relay" if relay else "MEXA-584L-bridge") / relative).as_posix()
            if source.suffix == ".sh":
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                info.external_attr = 0o100755 << 16
                archive.writestr(info, source.read_bytes().replace(b"\r\n", b"\n"), compress_type=zipfile.ZIP_DEFLATED)
            else:
                archive.write(source, name)
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", help="New ZIP path (must not already exist)")
    parser.add_argument("--relay", action="store_true", help="Build the separate server, without desktop/serial dependencies")
    args = parser.parse_args()
    print(build(args.destination, relay=args.relay))
