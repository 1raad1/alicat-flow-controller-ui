"""Temporary, loopback-only MEXA relay with an owned Wormhole helper process.

No hardware access. Keys live in memory and never enter child arguments, config,
environment, or logs. The only public service is RelayService's measurement API.
"""

import asyncio
from dataclasses import dataclass, field
import hashlib
import os
import queue
import secrets
import subprocess
import tempfile
import threading
import time

from .relay_server import RelayService


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


def helper_environment():
    # Filter legacy credentials too; an anonymous helper needs none of them.
    return {key: value for key, value in os.environ.items()
            if not key.upper().startswith(("TUNNEL_", "MEXA_", "CLOUDFLARE_", "CF_", "WORMHOLE_"))}


def file_digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class QuickTunnelHost:
    """One-shot host. Starting again requires a new instance and fresh keys."""

    def __init__(self, callback, executable="", *, startup_timeout=90):
        # Lazy import keeps shared HostError/digest imports acyclic.
        from . import wormhole
        self.backend = wormhole
        self.network_hint = wormhole.NETWORK_HINT
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
            executable = self.backend.prepare_helper(
                self.executable, self.stop_event, lambda message: self._emit("starting", message))
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
            # A fresh working directory prevents incidental helper/workspace state.
            # Wormhole is anonymous and needs no generated config file.
            with tempfile.TemporaryDirectory(prefix="mexa-wormhole-") as directory:
                events = queue.Queue(maxsize=128)
                reader = None
                try:
                    if self.stop_event.is_set():
                        return
                    self.process = subprocess.Popen(
                        self.backend.helper_command(executable, port), cwd=directory, env=helper_environment(),
                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                    )
                    reader = threading.Thread(target=self._read_output, args=(self.process.stdout, events, self.backend.tunnel_event),
                                              name="mexa-tunnel-output", daemon=True)
                    reader.start()
                    self._emit("starting", "Starting Wormhole temporary tunnel. " + self.network_hint)
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
                            elif kind == "disconnected":
                                # Repeated retries must not extend the outage deadline forever.
                                if connected:
                                    deadline = time.monotonic() + self.startup_timeout
                                connected = False
                                public_url = ""  # 'online' alone cannot resurrect an old URL
                                if self.status.state in ("ready", "reconnecting"):
                                    self._emit("reconnecting", "Wormhole link interrupted; live capture must be restarted after recovery. " + self.network_hint)
                        if exited:
                            raise HostError("Wormhole helper exited. Check the selected executable and network access. " + self.network_hint)
                        if connected and public_url:
                            if self.status.state != "ready":
                                message = ("Wormhole URL changed; copy the new URL to the analyser bridge and reconnect. Live capture must be restarted."
                                           if url_changed else "Tunnel registered; waiting for analyser measurements. Remote-PC reachability is not yet verified.")
                                self._emit("ready", message,
                                           public_url=public_url, **details)
                        elif time.monotonic() >= deadline:
                            raise HostError("Wormhole tunnel timed out. " + self.network_hint)
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
    def _read_output(stream, events, parser):
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
