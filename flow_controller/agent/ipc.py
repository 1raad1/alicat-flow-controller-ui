"""Authenticated local named-pipe bridge from an MCP subprocess to Qt."""

from __future__ import annotations

from multiprocessing import AuthenticationError
from multiprocessing.connection import Client, Listener
import os
from pathlib import Path
import secrets
import tempfile
import threading


CLIENT_REQUEST_TIMEOUT_S = 10.0
CLIENT_POLL_S = 0.05


class AgentIpcServer:
    """Serve one-request authenticated connections on a background thread."""

    def __init__(self, session, service, *, address=None, token=None):
        self.session = session
        self.service = service
        self.token = token or secrets.token_bytes(32)
        if isinstance(self.token, str):
            self.token = bytes.fromhex(self.token)
        self.family = "AF_PIPE" if os.name == "nt" else "AF_UNIX"
        if address is None:
            nonce = secrets.token_hex(12)
            if self.family == "AF_PIPE":
                address = rf"\\.\pipe\flow-controller-agent-{os.getpid()}-{nonce}"
            else:
                address = str(Path(tempfile.gettempdir()) /
                              f"flow-controller-agent-{os.getpid()}-{nonce}.sock")
        self.address = str(address)
        self._listener = None
        self._thread = None
        self._stopping = threading.Event()

    @property
    def connection_info(self):
        return {
            "address": self.address,
            "family": self.family,
            "token": self.token.hex(),
        }

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            if self._listener is not None:
                return self.connection_info
            raise RuntimeError("The previous agent gateway is still stopping.")
        self._stopping.clear()
        self._listener = Listener(
            self.address, family=self.family, authkey=self.token)
        self._thread = threading.Thread(
            target=self._serve, name="flow-agent-ipc", daemon=True)
        self._thread.start()
        return self.connection_info

    def _serve(self):
        listener = self._listener
        while not self._stopping.is_set():
            try:
                connection = listener.accept()
            except AuthenticationError:
                # A stale/revoked agent credential must not take the listener
                # down for the currently authorised session.
                continue
            except (OSError, EOFError):
                break
            try:
                waited = 0.0
                while (not self._stopping.is_set()
                       and waited < CLIENT_REQUEST_TIMEOUT_S
                       and not connection.poll(CLIENT_POLL_S)):
                    waited += CLIENT_POLL_S
                if self._stopping.is_set():
                    break
                if not connection.poll(0):
                    raise TimeoutError(
                        "Authenticated agent did not send a request in time.")
                request = connection.recv()
                if request.get("method") == "__shutdown__":
                    connection.send({"ok": True, "result": None})
                    break
                connection.send(self._call_on_qt(request))
            except Exception as exc:
                try:
                    connection.send({
                        "ok": False,
                        "error": f"{type(exc).__name__}: {exc}"})
                except Exception:
                    pass
            finally:
                connection.close()

    def _call_on_qt(self, request):
        done = threading.Event()
        cancelled = threading.Event()
        answer = {}

        def invoke():
            try:
                if self._stopping.is_set() or cancelled.is_set():
                    raise PermissionError("Agent authority has been revoked.")
                answer["result"] = self.service.handle(
                    request.get("agent_id"), request.get("method"),
                    request.get("arguments"))
                answer["ok"] = True
            except Exception as exc:
                answer.update(ok=False, error=f"{type(exc).__name__}: {exc}")
            finally:
                done.set()

        self.session._post(invoke)
        interactive = request.get("method") in (
            "set_role_setpoint", "start_armed_plan")
        timeout = None if interactive else CLIENT_REQUEST_TIMEOUT_S
        waited = 0.0
        while not done.wait(CLIENT_POLL_S):
            if self._stopping.is_set():
                cancelled.set()
                return {"ok": False, "error": "Agent authority has been revoked."}
            waited += CLIENT_POLL_S
            if timeout is not None and waited >= timeout:
                cancelled.set()
                return {
                    "ok": False,
                    "error": f"The application did not answer in {timeout:g} seconds.",
                }
        return answer

    def shutdown(self):
        if self._listener is None:
            return
        self._stopping.set()
        listener = self._listener

        # Never connect/recv synchronously here: shutdown is called from Qt's
        # GUI thread, and an authenticated client may already be occupying the
        # one accepted pipe while sending nothing.  A daemon wake-up is enough
        # to release an idle accept; an occupied connection observes the stop
        # event through the bounded poll above.
        token = self.token
        address = self.address
        family = self.family

        def wake_listener():
            try:
                with Client(address, family=family, authkey=token) as connection:
                    connection.send({"method": "__shutdown__"})
            except (AuthenticationError, OSError, EOFError):
                pass

        threading.Thread(
            target=wake_listener, name="flow-agent-ipc-wake", daemon=True).start()
        if self._thread is not None:
            self._thread.join(timeout=0.25)
        try:
            listener.close()
        except OSError:
            pass
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=0.1)
        self._listener = None
        if self._thread is not None and not self._thread.is_alive():
            self._thread = None
        if self.family == "AF_UNIX":
            try:
                Path(self.address).unlink()
            except OSError:
                pass
        # Stopping an agent revokes its captured credential. A later launch
        # gets a fresh per-session key even though the local endpoint name is
        # reused.
        self.token = secrets.token_bytes(32)


def call_agent_ipc(connection_info, agent_id, method, arguments=None):
    """Client helper used by the stdio MCP proxy and integration tests."""
    token = bytes.fromhex(connection_info["token"])
    with Client(connection_info["address"], family=connection_info["family"],
                authkey=token) as connection:
        connection.send({
            "agent_id": agent_id,
            "method": method,
            "arguments": dict(arguments or {}),
        })
        answer = connection.recv()
    if not answer.get("ok"):
        raise RuntimeError(answer.get("error", "Agent IPC request failed."))
    return answer.get("result")
