"""Pinned wormhole.bar helper, not Magic Wormhole. No instrument access."""

import hashlib
import os
from pathlib import Path
import platform
import re
import tempfile
import time
import urllib.request
import zipfile

from .quick_tunnel import HostError, file_digest


VERSION = "0.2.1"
ARCHIVE_SIZE = 3645741
ARCHIVE_SHA256 = "2ce5e4ae45044231d31d42f221bbe1dee4af3b1f434c286d82f15ac540e8e0a7"
WINDOWS_SIZE = 8969216
WINDOWS_SHA256 = "7ecd85e1c545871f39ac0a4c64ffc06280f21bd7532aa127c6782c8816ae95ef"
DOWNLOAD_URL = f"https://github.com/MuhammadHananAsghar/wormhole/releases/download/v{VERSION}/wormhole_windows_amd64.zip"
NETWORK_HINT = "Check approved outbound HTTPS/WSS 443 to relay.wormhole.bar and *.wormhole.bar on both PCs."


def tunnel_event(line):
    """Only accept lifecycle messages, never request paths or arbitrary URLs."""
    line = re.sub(r"\x1b\[[0-9;]*m", "", line).strip()
    if "blocked-due-to-malware.ucl.ac.uk" in line:
        return "error", ("UCL is blocking the Wormhole connection. Ask IT to review wormhole.bar. "
                         "Certificate verification was not bypassed.")
    if "x509:" in line or "failed to verify certificate" in line:
        return "error", ("Wormhole TLS certificate verification failed. Check PC time and ask IT about filtering. "
                         "Certificate verification was not bypassed.")
    match = re.fullmatch(
        r"(?:\S+ )?INF (?:tunnel established|reconnected) url=https://"
        r"([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.wormhole\.bar)", line)
    if match and match[1] not in ("relay.wormhole.bar", "www.wormhole.bar"):
        return "registered", f"wss://{match[1]}/mexa"
    # 'online' is emitted BEFORE the URL; the registration line is authoritative.
    # Ignore the one-shot 'Forwarding' banner, which can contain a stale URL.
    if re.fullmatch(r"(?:\S+ )?INF status changed status=(?:connecting|reconnecting)", line):
        return "disconnected", ""
    return None


def helper_command(executable, port):
    # Anonymous tunnel: no login/config, no inspector, and no helper updater.
    # Wormhole targets localhost; RelayService binds only 127.0.0.1.
    return [str(executable), "http", str(port), "--headless", "--no-inspect"]


def default_helper_path():
    if os.name != "nt" or platform.machine().lower() not in ("amd64", "x86_64"):
        raise HostError("Automatic Wormhole download supports Windows x64. Select an official Wormhole executable for this platform.")
    root = os.environ.get("USERPROFILE", "")
    if not root or not Path(root).is_absolute():
        raise HostError("USERPROFILE is unavailable. Select an official Wormhole executable.")
    return Path(root) / ".flow-controller-v3" / "tools" / f"wormhole-{VERSION}" / "wormhole.exe"


def verify_cached(target):
    if target.is_symlink() or target.stat().st_size != WINDOWS_SIZE or file_digest(target) != WINDOWS_SHA256:
        raise HostError("Cached Wormhole helper failed verification; the existing file was not replaced. Select a fresh official executable.")


def prepare_helper(selected, stop, progress):
    if selected:
        candidate = Path(selected)
        if not candidate.is_absolute() or not candidate.is_file() or (os.name == "nt" and candidate.suffix.lower() != ".exe"):
            raise HostError("Select the official Wormhole executable using its full file path.")
        return candidate.resolve()
    target = default_helper_path()
    if target.exists() or target.is_symlink():
        verify_cached(target)
        return target
    progress("Downloading Wormhole 0.2.1 (3.6 MB); verifying the ZIP and executable SHA-256 before use…")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = binary = None
    try:
        request = urllib.request.Request(DOWNLOAD_URL, headers={"User-Agent": "FlowController-MEXA"})
        with urllib.request.urlopen(request, timeout=5) as response:
            if not response.url.startswith("https://"):
                raise HostError("Wormhole helper download must use HTTPS.")
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix="download-", suffix=".zip.part", delete=False) as stream:
                temporary = Path(stream.name)
                digest, size = hashlib.sha256(), 0
                deadline = time.monotonic() + 120
                while not stop.is_set():
                    chunk = response.read(128 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > ARCHIVE_SIZE or time.monotonic() > deadline:
                        raise HostError("Wormhole download exceeded its size or time limit.")
                    stream.write(chunk)
                    digest.update(chunk)
        if stop.is_set():
            raise HostError("Temporary relay start cancelled")
        if size != ARCHIVE_SIZE or digest.hexdigest() != ARCHIVE_SHA256:
            raise HostError("Wormhole ZIP failed SHA-256 verification; it was not extracted or executed.")
        with zipfile.ZipFile(temporary) as archive:
            members = [entry for entry in archive.infolist() if entry.filename == "wormhole.exe"]
            if len(members) != 1 or members[0].file_size != WINDOWS_SIZE:
                raise HostError("Wormhole ZIP has an unexpected executable layout.")
            # Never extract archive paths. Copy only the exact, bounded member.
            with archive.open(members[0]) as source, tempfile.NamedTemporaryFile(
                    dir=target.parent, prefix="verified-", suffix=".exe.part", delete=False) as stream:
                binary = Path(stream.name)
                digest, size = hashlib.sha256(), 0
                while not stop.is_set():
                    chunk = source.read(128 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > WINDOWS_SIZE:
                        raise HostError("Wormhole executable exceeded its size limit.")
                    digest.update(chunk)
                    stream.write(chunk)
        if stop.is_set():
            raise HostError("Temporary relay start cancelled")
        if size != WINDOWS_SIZE or digest.hexdigest() != WINDOWS_SHA256:
            raise HostError("Wormhole executable failed SHA-256 verification; it was not executed.")
        try:
            # Exclusive atomic installation: never replace another instance's file.
            os.link(binary, target)
        except FileExistsError:
            verify_cached(target)
        return target
    except HostError:
        raise
    except (OSError, ValueError, zipfile.BadZipFile, KeyError):
        raise HostError("Could not download/install Wormhole. Check HTTPS access to GitHub or select an official executable manually.") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        if binary is not None:
            binary.unlink(missing_ok=True)
