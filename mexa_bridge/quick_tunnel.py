"""Temporary, loopback-only MEXA relay with an owned tunnel helper process.

No hardware access. Keys live in memory and never enter child arguments, config,
environment, or logs. The only public service is RelayService's measurement API.
"""

import asyncio
from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import platform
import queue
import re
import secrets
import subprocess
import tempfile
import threading
import time
import urllib.request

from .relay_server import RelayService


VERSION = "2026.8.2"
WINDOWS_SHA256 = "c29eee2b121f5436a642eed69fd9767da7e7b8c510fa50aaa130337f931357b5"
WINDOWS_SIZE = 54893480
DOWNLOAD_URL = f"https://github.com/cloudflare/cloudflared/releases/download/{VERSION}/cloudflared-windows-amd64.exe"
NETWORK_HINT = "Check internet/DNS and approved outbound TCP 7844 to Cloudflare; the analyser also needs HTTPS/WSS 443."


@dataclass(frozen=True)
class HostStatus:
    state: str = "stopped"
    message: str = "Temporary relay is stopped"
    public_url: str = ""
    local_url: str = ""
    publisher_key: str = field(default="", repr=False)
    receiver_key: str = field(default="", repr=False)


class HostError(ValueError):
    """Only fixed, credential-free messages may be placed in this exception."""


def tunnel_event(line):
    """Whitelist helper output; never display arbitrary lines or exception text."""
    if "blocked-due-to-malware.ucl.ac.uk" in line:
        return "error", ("UCL is blocking api.trycloudflare.com (blocked-due-to-malware.ucl.ac.uk). "
                         "Ask IT to review and approve Cloudflare Quick Tunnel. Certificate verification was not bypassed.")
    if "failed to verify certificate" in line or "x509:" in line:
        return "error", ("Cloudflare TLS certificate verification failed. Check PC time and ask IT about proxy/filtering certificates. "
                         "Certificate verification was not bypassed.")
    if "failed to request quick Tunnel" in line:
        return "error", "Could not create the temporary address. Check approved HTTPS access to api.trycloudflare.com with IT."
    match = re.search(r"(?<![\w:/])https://([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.trycloudflare\.com)(?=[\s|]|$)", line)
    if match:
        return "url", f"wss://{match[1]}/mexa"
    if "Registered tunnel connection" in line:
        return "connected", ""
    if any(word in line for word in ("Unregistered tunnel connection", "Failed to serve tunnel connection",
                                     "Serve tunnel error", "Connection terminated")):
        return "disconnected", ""
    return None


def helper_command(executable, config, port):
    return [str(executable), "tunnel", "--config", str(config), "--no-autoupdate",
            "--protocol", "http2", "--metrics", "127.0.0.1:0", "--loglevel", "info",
            "--url", f"http://127.0.0.1:{port}"]


def helper_environment():
    return {key: value for key, value in os.environ.items()
            if not key.upper().startswith(("TUNNEL_", "MEXA_", "CLOUDFLARE_", "CF_", "WORMHOLE_"))}


def default_helper_path():
    if os.name != "nt" or platform.machine().lower() not in ("amd64", "x86_64"):
        raise HostError("Automatic helper download supports Windows x64. Select an official cloudflared executable for this platform.")
    # Match install.bat. Store Python virtualises LOCALAPPDATA, hiding files
    # from Explorer and from non-packaged helper processes.
    root = os.environ.get("USERPROFILE", "")
    if not root or not Path(root).is_absolute():
        raise HostError("USERPROFILE is unavailable. Select an official cloudflared executable.")
    return Path(root) / ".flow-controller-v3" / "tools" / f"cloudflared-{VERSION}" / "cloudflared.exe"


def file_digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def prepare_helper(selected, stop, progress):
    if selected:
        candidate = Path(selected)
        if not candidate.is_absolute() or not candidate.is_file() or (os.name == "nt" and candidate.suffix.lower() != ".exe"):
            raise HostError("Select the official cloudflared executable using its full file path.")
        return candidate.resolve()
    target = default_helper_path()
    if target.exists():
        if target.is_symlink() or target.stat().st_size != WINDOWS_SIZE or file_digest(target) != WINDOWS_SHA256:
            raise HostError("Cached tunnel helper failed verification. Select a fresh official executable; the existing file was not replaced.")
        return target
    progress("Downloading the official Cloudflare helper (55 MB); verifying its pinned SHA-256 before use…")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        request = urllib.request.Request(DOWNLOAD_URL, headers={"User-Agent": "FlowController-MEXA"})
        with urllib.request.urlopen(request, timeout=5) as response:
            if not response.url.startswith("https://"):
                raise HostError("Tunnel helper download must use HTTPS.")
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix="download-", suffix=".part", delete=False) as stream:
                temporary = Path(stream.name)
                digest, size = hashlib.sha256(), 0
                deadline = time.monotonic() + 180
                while not stop.is_set():
                    chunk = response.read(128 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > WINDOWS_SIZE or time.monotonic() > deadline:
                        raise HostError("Tunnel helper download exceeded its size or time limit.")
                    stream.write(chunk)
                    digest.update(chunk)
        if stop.is_set():
            raise HostError("Temporary relay start cancelled")
        if size != WINDOWS_SIZE or digest.hexdigest() != WINDOWS_SHA256:
            raise HostError("Tunnel helper download failed SHA-256 verification; it was not executed.")
        # Another app instance may have installed the identical helper meanwhile.
        if target.exists():
            if file_digest(target) != WINDOWS_SHA256:
                raise HostError("A different helper already exists; it was not replaced.")
        else:
            temporary.rename(target)
        return target
    except HostError:
        raise
    except (OSError, ValueError):
        raise HostError("Could not download the tunnel helper. Check HTTPS access to GitHub or select an official executable manually.") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class QuickTunnelHost:
    """One-shot host. Starting again requires a new instance and fresh keys."""

    def __init__(self, callback, executable="", *, provider="cloudflare", startup_timeout=90):
        if provider not in ("cloudflare", "wormhole"):
            raise HostError("Choose Wormhole or Cloudflare as the tunnel provider")
        self.provider = provider
        self.backend = None
        if provider == "wormhole":
            from . import wormhole
            self.backend = wormhole
        self.label = "Wormhole" if self.backend else "Cloudflare"
        self.network_hint = self.backend.NETWORK_HINT if self.backend else NETWORK_HINT
        self.callback, self.executable = callback, executable
        self.startup_timeout = startup_timeout
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name="mexa-quick-tunnel", daemon=True)
        self.status = HostStatus()
        self.process = None

    def start(self):
        self.thread.start()

    def _emit(self, state, message, **details):
        self.status = HostStatus(state, message, **details)
        self.callback(self.status)

    def _run(self):
        failure = None
        try:
            self._emit("starting", "Preparing temporary relay…")
            prepare = self.backend.prepare_helper if self.backend else prepare_helper
            executable = prepare(self.executable, self.stop_event,
                                 lambda message: self._emit("starting", message))
            if not self.stop_event.is_set():
                asyncio.run(self._serve(executable))
        except HostError as exc:
            failure = str(exc)
        except Exception:
            failure = "Temporary relay failed to start or stopped unexpectedly. " + self.network_hint
        finally:
            # Clear URLs and keys only after listener/process cleanup completes.
            self._emit("failed" if failure and not self.stop_event.is_set() else "stopped",
                       failure if failure and not self.stop_event.is_set() else "Temporary relay stopped; its URL and keys are no longer usable")

    async def _serve(self, executable):
        publisher_key, receiver_key = secrets.token_hex(32), secrets.token_hex(32)
        service = RelayService(publisher_key, receiver_key)
        async with await service.start(host="127.0.0.1", port=0) as server:
            port = server.sockets[0].getsockname()[1]
            details = dict(local_url=f"ws://127.0.0.1:{port}/mexa", publisher_key=publisher_key, receiver_key=receiver_key)
            with tempfile.TemporaryDirectory(prefix="mexa-quick-tunnel-") as directory:
                config = Path(directory) / "config.yml"
                config.write_text("{}\n", encoding="utf-8")
                events = queue.Queue(maxsize=128)
                reader = None
                try:
                    if self.stop_event.is_set():
                        return
                    command = self.backend.helper_command if self.backend else helper_command
                    parser = self.backend.tunnel_event if self.backend else tunnel_event
                    self.process = subprocess.Popen(
                        command(executable, config, port), cwd=directory, env=helper_environment(),
                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                    )
                    reader = threading.Thread(target=self._read_output, args=(self.process.stdout, events, parser),
                                              name="mexa-tunnel-output", daemon=True)
                    reader.start()
                    self._emit("starting", f"Starting {self.label} temporary tunnel. " + self.network_hint)
                    public_url, connected = "", False
                    previous_url = ""
                    url_changed = False
                    deadline = time.monotonic() + self.startup_timeout
                    while not self.stop_event.is_set():
                        exited = self.process.poll() is not None
                        if exited:
                            # Drain the final diagnostic before reporting generic exit.
                            await asyncio.to_thread(reader.join, 1)
                        while not events.empty():
                            kind, value = events.get_nowait()
                            if kind == "error":
                                raise HostError(value)
                            elif kind == "registered":
                                if previous_url and previous_url != value:
                                    url_changed = True
                                    self._emit("reconnecting", "Wormhole assigned a new URL; copy it to the analyser bridge. Live capture was interrupted.")
                                public_url = previous_url = value
                                connected = True
                            elif kind == "url":
                                if public_url and public_url != value:
                                    raise HostError("Cloudflare changed the tunnel URL unexpectedly. Stop and start a fresh session.")
                                public_url = value
                            elif kind == "connected":
                                connected = True
                            elif kind == "disconnected":
                                # Repeated retries must not extend the outage deadline forever.
                                if connected:
                                    deadline = time.monotonic() + self.startup_timeout
                                connected = False
                                if self.backend:
                                    public_url = ""  # 'online' alone cannot resurrect an old URL
                                if self.status.state in ("ready", "reconnecting"):
                                    self._emit("reconnecting", f"{self.label} link interrupted; live capture must be restarted after recovery. " + self.network_hint)
                        if exited:
                            raise HostError(f"{self.label} helper exited. Check the selected executable and network access. " + self.network_hint)
                        if connected and public_url:
                            if self.status.state != "ready":
                                message = ("Wormhole URL changed; copy the new URL to the analyser bridge and reconnect. Live capture must be restarted."
                                           if url_changed else "Tunnel registered; waiting for analyser measurements. Remote-PC reachability is not yet verified.")
                                self._emit("ready", message,
                                           public_url=public_url, **details)
                        elif time.monotonic() >= deadline:
                            raise HostError(f"{self.label} tunnel timed out. " + self.network_hint)
                        await asyncio.sleep(.1)
                finally:
                    process = self.process
                    if process is not None:
                        if process.poll() is None:
                            process.terminate()
                            try:
                                await asyncio.to_thread(process.wait, timeout=2)
                            except subprocess.TimeoutExpired:
                                process.kill()
                                await asyncio.to_thread(process.wait, timeout=2)
                        if reader:
                            await asyncio.to_thread(reader.join, 1)
                        process.stdout.close()
                    self.process = None

    @staticmethod
    def _read_output(stream, events, parser=tunnel_event):
        discard = False
        while True:
            raw = stream.readline(4096)
            if not raw:
                return
            complete = raw.endswith(b"\n")
            event = parser(raw.decode("utf-8", errors="replace")) if complete and not discard else None
            discard = not complete
            if event:
                try:
                    events.put_nowait(event)
                except queue.Full:
                    # No unbounded diagnostic history; the newest route state wins.
                    try:
                        events.get_nowait()
                    except queue.Empty:
                        pass
                    events.put_nowait(event)

    def stop(self, *, wait=False):
        self.stop_event.set()
        if wait and self.thread.is_alive() and threading.current_thread() is not self.thread:
            self.thread.join(6)
        return not self.thread.is_alive()
