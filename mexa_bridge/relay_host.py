"""Small terminal host for CachyOS/Linux. Caddy supplies public HTTPS.

Only this user's ~/.config/mexa-relay directory is configured. No root tasks,
router/firewall changes, system services, serial ports or flow controls.
"""

import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import secrets
import sys
import tempfile
from urllib.request import urlopen

from .relay import validate_key
from .relay_server import RelayService


def config_directory():
    base = os.environ.get("XDG_CONFIG_HOME", "")
    root = Path(base) if base and Path(base).is_absolute() else Path.home() / ".config"
    return root / "mexa-relay"


def hostname(value):
    value = value.lower().rstrip(".")
    labels = value.split(".")
    if (not value.isascii() or len(value) > 253 or len(labels) < 2
            or labels[-1].isdigit() or labels[-1] in ("local", "localhost")
            or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels)):
        raise ValueError("Enter a DNS hostname such as relay.your-domain.net, without https://, a path or a port")
    return value


def secure_directory(directory):
    directory = Path(directory)
    if directory.is_symlink():
        raise ValueError("Relay configuration directory must not be a symbolic link")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        if directory.stat().st_uid != os.getuid():
            raise ValueError("Relay configuration directory must belong to the current user")
        directory.chmod(0o700)
    return directory


def load_config(directory):
    path = Path(directory) / "host.json"
    if not path.exists():
        raise ValueError("Run 'bash run_relay_host.sh setup' first")
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4096:
        raise ValueError("Invalid relay configuration file")
    if os.name == "posix":
        info = path.stat()
        parent = path.parent.stat()
        if (info.st_uid != os.getuid() or info.st_mode & 0o077
                or parent.st_uid != os.getuid() or parent.st_mode & 0o077 or path.parent.is_symlink()):
            raise ValueError("Relay configuration must be private: directory mode 700, host.json mode 600, owned by you")
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(config, dict) or set(config) != {"version", "hostname", "port", "publisher_key", "receiver_key"}:
            raise ValueError()
        if type(config["version"]) is not int or config["version"] != 1:
            raise ValueError()
        hostname(config["hostname"])
        if type(config["port"]) is not int or not 1024 <= config["port"] <= 65535:
            raise ValueError()
        validate_key(config["publisher_key"])
        validate_key(config["receiver_key"])
        if config["publisher_key"] == config["receiver_key"]:
            raise ValueError()
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ValueError("Invalid relay configuration; no keys were printed or replaced") from None
    return config


def write_private(path, text):
    """Atomic replacement inside the private application directory."""
    if path.is_symlink():
        raise ValueError("Refusing to replace a symbolic link")
    fd, temporary = tempfile.mkstemp(prefix=".mexa-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def unit_quote(value):
    # systemd performs % expansion even inside quotes; generated absolute
    # paths must remain literal, including spaces and percent signs.
    if any(c in str(value) for c in ("\n", "\r", "\0", "$")):
        raise ValueError("Service paths must not contain newlines, NUL or dollar signs")
    return '"' + str(value).replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"') + '"'


def configure(directory, domain, port=8765):
    domain = hostname(domain)
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("Choose an unprivileged backend port between 1024 and 65535")
    directory = secure_directory(directory)
    path = directory / "host.json"
    if path.exists():
        config = load_config(directory)  # preserve keys when hostname changes
    else:
        config = {"version": 1, "publisher_key": secrets.token_hex(32), "receiver_key": secrets.token_hex(32)}
    config.update(hostname=domain, port=port)
    write_private(path, json.dumps(config, indent=2) + "\n")
    write_private(directory / "Caddyfile", f"{domain} {{\n    reverse_proxy 127.0.0.1:{port}\n}}\n")
    project = Path(__file__).resolve().parents[1]
    unit = ("[Unit]\nDescription=MEXA measurement relay\n\n[Service]\nType=simple\n"
            f"WorkingDirectory={unit_quote(project)}\n"
            f"ExecStart={unit_quote(sys.executable)} -m mexa_bridge.relay_host --config-dir {unit_quote(directory)} run\n"
            "Restart=on-failure\nRestartSec=5\nUMask=0077\nNoNewPrivileges=true\n\n[Install]\nWantedBy=default.target\n")
    write_private(directory / "mexa-relay.service", unit)
    return config


async def run_host(config):
    service = RelayService(config["publisher_key"], config["receiver_key"])
    async with await service.start("127.0.0.1", config["port"]):
        print(f"MEXA relay backend listening on 127.0.0.1:{config['port']}\n"
              f"Configured public URL: wss://{config['hostname']}/mexa\n"
              "Public reachability is NOT verified. Caddy, DNS and your router must be configured.\n"
              "No measurement files are saved. Ctrl+C stops the host.", flush=True)
        previous = None
        while True:
            current = tuple(sorted(service.peers))
            if current != previous:
                print("Analyser: " + ("connected" if "publisher" in current else "waiting")
                      + " | Flow-controller: " + ("connected" if "receiver" in current else "waiting")
                      + " (relay access only; measurement quality is checked on the PCs)", flush=True)
                previous = current
            await asyncio.sleep(.5)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-dir", type=Path, default=config_directory())
    sub = parser.add_subparsers(dest="command", required=True)
    setup = sub.add_parser("setup", help="Create private keys and configuration; never modifies /etc or networking")
    setup.add_argument("--hostname", help="Your public DNS hostname; prompted if omitted")
    setup.add_argument("--port", type=int, default=8765)
    sub.add_parser("run", help="Run until Ctrl+C; use Caddy for public HTTPS")
    sub.add_parser("show-keys", help="Show connection details and private role keys for copying to the two PCs")
    sub.add_parser("status", help="Check only the local backend health endpoint")
    args = parser.parse_args(argv)
    directory = args.config_dir.expanduser().absolute()
    try:
        if args.command == "setup":
            domain = args.hostname or input("Public relay hostname (for example relay.your-domain.net): ").strip()
            config = configure(directory, domain, args.port)
            print(f"Configuration saved privately in {directory}\n"
                  f"Relay URL: wss://{config['hostname']}/mexa\n"
                  "Run 'bash run_relay_host.sh show-keys' to see the PC keys.\n"
                  "Next: set up Caddy/DNS/router using CACHYOS_START_HERE.md, then run the host.\n"
                  "No firewall, router, system service or DNS changes were made.")
            return 0
        config = load_config(directory)
        if args.command == "show-keys":
            print(f"Relay URL (both PCs): wss://{config['hostname']}/mexa\n"
                  f"Analyser PC publisher key: {config['publisher_key']}\n"
                  f"Flow PC receiver key: {config['receiver_key']}\n"
                  "Also copy the bridge's separate shared key to the receiver. Never post these keys in support logs.")
        elif args.command == "status":
            with urlopen(f"http://127.0.0.1:{config['port']}/healthz", timeout=2) as response:
                if response.status != 200 or response.read(100) != b"MEXA relay running\n":
                    raise ValueError("Unexpected service on the backend port")
            print("Local relay is running. This does not test public reachability or analyser readiness.")
        else:
            asyncio.run(run_host(config))
        return 0
    except KeyboardInterrupt:
        print("\nRelay stopped. Instrument and burner settings unchanged.")
        return 0
    except (ValueError, OSError, EOFError) as exc:
        # OS errors could include sensitive content; never print their payload.
        print(str(exc) if isinstance(exc, ValueError) else
              "Host operation failed. Check configuration permissions, dependencies and whether the backend port is in use.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
