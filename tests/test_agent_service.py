import json
import os
import asyncio
import importlib.util
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from multiprocessing.connection import Client
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from flow_controller.agent.ipc import AgentIpcServer, call_agent_ipc
from flow_controller.agent.service import (
    AgentAuditLog, AgentDraftService, AgentRequestError)
from flow_controller.core.sequence import HOLD, Keyframe, Sequence, Track
from flow_controller.core.session import FlowSession


class AgentDraftServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.session = FlowSession(worker=SimpleNamespace(shutdown=lambda: None))
        self.addCleanup(self.session.shutdown)
        self.session.unit_prefs = {}
        self.session.assignments["nh3_rich"] = "A"
        self.session.selection = {"A": ("NH3", "Rich")}
        self.session._latest_samples = {
            "A": {"flow": 1.0, "sp": 1.0, "press": 14.7, "temp": 20.0}}
        self.session._latest_timestamp = datetime.now()
        self.session.sequence_dir = Path(self.tmp.name) / "sequences"
        self.session.sequence_dir.mkdir()
        self.service = AgentDraftService(
            self.session,
            audit=AgentAuditLog(Path(self.tmp.name) / "audit.jsonl"))

    def audit_rows(self, *, phase="completed"):
        rows = [json.loads(line) for line in
                self.service.audit.path.read_text(encoding="utf-8").splitlines()]
        return [row for row in rows if row.get("phase") == phase]

    def test_read_snapshot_is_copied_and_audited(self):
        result = self.service.handle("agent-1", "read_snapshot")
        self.assertEqual(result["telemetry"]["A"]["flow"], 1.0)
        result["telemetry"]["A"]["flow"] = 99
        self.assertEqual(self.session._latest_samples["A"]["flow"], 1.0)
        row = self.audit_rows()[0]
        self.assertEqual(row["agent"], "agent-1")
        self.assertEqual(row["approval"], "not_required")
        self.assertEqual(row["result"], "accepted")

    def test_sequence_draft_lands_in_existing_editor(self):
        sequence = Sequence(name="agent sequence", tracks=[Track(
            "nh3_rich", "NH3", keyframes=[
                Keyframe(0, 0, HOLD), Keyframe(2, 1, HOLD)])])
        answer = self.service.handle(
            "agent-1", "submit_sequence_draft",
            {"sequence": sequence.to_dict()})
        self.assertEqual(answer["status"], "pending_operator_review")
        self.assertEqual(self.session.sequence.name, "agent sequence")
        self.assertEqual(
            self.audit_rows()[0]["approval"], "pending_operator_review")

    def test_saved_sequence_name_rejects_paths(self):
        with self.assertRaisesRegex(AgentRequestError, "without a path"):
            self.service.handle(
                "agent-1", "run_saved_sequence", {"name": "../outside"})

    def test_repeated_agent_reads_are_rate_limited_before_more_audit_io(self):
        self.service.handle("poller", "read_snapshot")

        with self.assertRaisesRegex(AgentRequestError, "calls/s"):
            self.service.handle("poller", "read_snapshot")

        rows = [json.loads(line) for line in
                self.service.audit.path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), 2)

    def test_oversized_sequence_shape_is_rejected_before_model_parsing(self):
        raw = Sequence(name="shape", tracks=[]).to_dict()
        raw["tracks"] = [{} for _index in range(33)]

        with self.assertRaisesRegex(AgentRequestError, "at most 32 tracks"):
            self.service.handle(
                "agent-1", "submit_sequence_draft", {"sequence": raw})
        self.assertIsNone(self.session.sequence)

    def test_live_control_is_default_off(self):
        with self.assertRaisesRegex(AgentRequestError, "disabled"):
            self.service.handle(
                "agent-1", "set_role_setpoint",
                {"role": "nh3_rich", "value": 1})
        self.assertTrue(self.session.setpoint_queue.empty())
        self.assertEqual(self.audit_rows()[0]["result"], "refused")

    def _make_live_ready(self):
        self.session.controllers_connected = True
        self.session.is_monitoring = True
        self.session.unit_prefs["A"] = {
            "max_flow": 5.0, "ramp": 100.0, "ramp_off": False}

    def test_live_toggle_authorizes_automatic_setpoints_and_records_it(self):
        self._make_live_ready()
        self.service.set_live_enabled(True)

        first = self.service.handle(
            "agent-1", "set_role_setpoint",
            {"role": "nh3_rich", "value": 2.0})
        second = self.service.handle(
            "agent-1", "set_role_setpoint",
            {"role": "nh3_rich", "value": 3.0})

        self.assertEqual(first["status"], "queued")
        self.assertEqual(second["status"], "queued")
        self.assertEqual(self.session.setpoint_queue.get_nowait(), ("A", 2.0))
        self.assertEqual(self.session.setpoint_queue.get_nowait(), ("A", 3.0))
        rows = [row for row in self.audit_rows()
                if row["method"] == "set_role_setpoint"]
        self.assertEqual(
            [row["approval"] for row in rows],
            ["live_toggle", "live_toggle"])

    def test_session_refusal_still_fails_closed_after_toggle_approval(self):
        self._make_live_ready()
        self.service.set_live_enabled(True)
        with patch.object(
                self.session, "set_role_setpoint", return_value=False):
            with self.assertRaisesRegex(AgentRequestError, "session refused"):
                self.service.handle(
                    "agent-1", "set_role_setpoint",
                    {"role": "nh3_rich", "value": 2.0})

    def test_live_action_is_refused_if_full_preexecution_audit_fails(self):
        class FailsOnExecutionRecord:
            def __init__(self):
                self.calls = 0

            def write(self, _record):
                self.calls += 1
                if self.calls == 2:
                    raise OSError("disk full")

        self._make_live_ready()
        authority = self.service.authority
        authority.enable()
        service = AgentDraftService(
            self.session, authority=authority,
            audit=FailsOnExecutionRecord())
        with self.assertRaisesRegex(AgentRequestError, "not executed"):
            service.handle(
                "agent-1", "set_role_setpoint",
                {"role": "nh3_rich", "value": 2.0})
        self.assertTrue(self.session.setpoint_queue.empty())

    def test_envelope_change_during_preexecution_audit_fails_closed(self):
        self._make_live_ready()
        authority = self.service.authority
        authority.enable()

        class ChangesEnvelopeDuringExecutionAudit:
            def __init__(self):
                self.calls = 0

            def write(inner_self, _record):
                inner_self.calls += 1
                if inner_self.calls == 2:
                    self.session.unit_prefs["A"]["max_flow"] = 1.0

        service = AgentDraftService(
            self.session, authority=authority,
            audit=ChangesEnvelopeDuringExecutionAudit())
        with self.assertRaisesRegex(AgentRequestError, "revoked"):
            service.handle(
                "agent-1", "set_role_setpoint",
                {"role": "nh3_rich", "value": 2.0})
        self.assertTrue(self.session.setpoint_queue.empty())

    def test_revocation_cannot_be_blocked_by_an_unwritable_audit(self):
        class BrokenAudit:
            def write(self, _record):
                raise OSError("disk unavailable")

        self._make_live_ready()
        authority = self.service.authority
        authority.enable()
        service = AgentDraftService(
            self.session, authority=authority, audit=BrokenAudit())

        result = service.set_live_enabled(
            False, reason="operator disabled live control")

        self.assertFalse(result["enabled"])
        self.assertFalse(authority.status()["enabled"])

    def test_disabling_authority_does_not_abort_an_active_plan(self):
        self._make_live_ready()
        self.service.set_live_enabled(True)
        with patch.object(self.session.experiment_plans, "abort") as abort:
            self.service.set_live_enabled(
                False, reason="operator disabled live control")
        abort.assert_not_called()
        self.assertFalse(self.service.authority.status()["enabled"])

    def _save_sequence(self, filename="agent-run", *, opening=1.0, peak=2.0):
        path = self.session.sequence_dir / f"{filename}.fcseq.json"
        Sequence(name=filename, tracks=[Track(
            "nh3_rich", "NH3", keyframes=[
                Keyframe(0, opening, HOLD),
                Keyframe(1, peak, HOLD)])]).save(path)
        return path

    def test_agent_lists_and_runs_a_saved_sequence_once_per_request(self):
        self._make_live_ready()
        self._save_sequence()
        listed = self.service.handle("agent-1", "list_saved_sequences")
        self.assertEqual(listed["count"], 1)
        self.assertEqual(listed["sequences"][0]["name"], "agent-run")
        self.assertTrue(listed["sequences"][0]["runnable"])
        self.service.set_live_enabled(True)
        with patch.object(self.session, "start_replay", return_value=True) as start:
            answer = self.service.handle(
                "agent-1", "run_saved_sequence", {"name": "agent-run"})
        self.assertEqual(answer["status"], "running")
        start.assert_called_once()
        self.assertEqual(start.call_args.kwargs["repeats"], 1)

    def test_saved_sequence_is_reread_after_preexecution_audit(self):
        self._make_live_ready()
        sequence_path = self._save_sequence()
        self.service.set_live_enabled(True)

        class MutatesOnExecutionAudit:
            def __init__(inner_self):
                inner_self.calls = 0

            def write(inner_self, _record):
                inner_self.calls += 1
                if inner_self.calls == 2:
                    Sequence(name="changed", tracks=[Track(
                        "nh3_rich", "NH3", keyframes=[
                            Keyframe(0, 1, HOLD),
                            Keyframe(1, 4, HOLD)])]).save(sequence_path)

        service = AgentDraftService(
            self.session, authority=self.service.authority,
            audit=MutatesOnExecutionAudit())
        with patch.object(self.session, "start_replay") as start:
            with self.assertRaisesRegex(AgentRequestError, "changed"):
                service.handle(
                    "agent-1", "run_saved_sequence", {"name": "agent-run"})
        start.assert_not_called()

    def test_saved_sequence_refuses_an_opening_flow_mismatch(self):
        self._make_live_ready()
        self._save_sequence(opening=0.0)
        self.service.set_live_enabled(True)
        with patch.object(self.session, "start_replay") as start:
            with self.assertRaisesRegex(AgentRequestError, "does not match"):
                self.service.handle(
                    "agent-1", "run_saved_sequence", {"name": "agent-run"})
        start.assert_not_called()

    def test_saved_sequence_refuses_stale_opening_telemetry(self):
        self._make_live_ready()
        self._save_sequence()
        self.session._latest_timestamp = datetime.now() - timedelta(seconds=30)
        self.service.set_live_enabled(True)
        with patch.object(self.session, "start_replay") as start:
            with self.assertRaisesRegex(AgentRequestError, "stale"):
                self.service.handle(
                    "agent-1", "run_saved_sequence", {"name": "agent-run"})
        start.assert_not_called()

    def test_malformed_arguments_are_refused_and_audited(self):
        with self.assertRaises(AgentRequestError):
            self.service.handle("bad-agent", "read_snapshot", 42)
        row = self.audit_rows()[0]
        self.assertEqual(row["agent"], "bad-agent")
        self.assertEqual(row["result"], "refused")
        self.assertIn("TypeError", row["error"])

    def test_unavailable_audit_refuses_request_before_mutation(self):
        class BrokenAudit:
            def write(self, _record):
                raise OSError("disk unavailable")

        service = AgentDraftService(self.session, audit=BrokenAudit())
        sequence = Sequence(name="must not land", tracks=[Track(
            "nh3_rich", "NH3", keyframes=[Keyframe(0, 0, HOLD)])])
        with self.assertRaisesRegex(AgentRequestError, "not executed"):
            service.handle(
                "agent-1", "submit_sequence_draft",
                {"sequence": sequence.to_dict()})
        self.assertIsNone(self.session.sequence)

    def test_completion_audit_failure_does_not_reverse_adopted_draft(self):
        class FailsOnCompletion:
            def __init__(self):
                self.calls = 0

            def write(self, _record):
                self.calls += 1
                if self.calls == 2:
                    raise OSError("disk became full")

        audit = FailsOnCompletion()
        service = AgentDraftService(self.session, audit=audit)
        messages = []
        self.session.logged.connect(lambda _channel, text: messages.append(text))
        sequence = Sequence(name="still lands", tracks=[Track(
            "nh3_rich", "NH3", keyframes=[Keyframe(0, 0, HOLD)])])
        answer = service.handle(
            "agent-1", "submit_sequence_draft",
            {"sequence": sequence.to_dict()})
        self.assertTrue(answer["accepted"])
        self.assertEqual(self.session.sequence.name, "still lands")
        self.assertTrue(any("audit completion" in text for text in messages))

    def test_authenticated_ipc_marshals_back_to_qt_thread(self):
        server = AgentIpcServer(self.session, self.service)
        info = server.start()
        self.addCleanup(server.shutdown)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                call_agent_ipc, info, "ipc-agent", "read_snapshot")
            deadline = time.monotonic() + 5
            while not future.done() and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.005)
            result = future.result(timeout=1)
        self.assertTrue(result["connection"]["connected"] is False)
        self.assertEqual(self.audit_rows()[0]["agent"], "ipc-agent")

    def test_stopping_gateway_rotates_the_agent_credential(self):
        server = AgentIpcServer(self.session, self.service)
        first = server.start()["token"]
        server.shutdown()
        second = server.start()["token"]
        self.addCleanup(server.shutdown)
        self.assertNotEqual(first, second)

    def test_stalled_authenticated_client_cannot_block_shutdown(self):
        server = AgentIpcServer(self.session, self.service)
        info = server.start()
        stalled = Client(
            info["address"], family=info["family"],
            authkey=bytes.fromhex(info["token"]))
        self.addCleanup(stalled.close)
        started = time.monotonic()
        server.shutdown()
        self.assertLess(time.monotonic() - started, 1.0)

    def test_queued_request_is_refused_after_authority_is_revoked(self):
        queued = []
        self.session._post = lambda function, *args: queued.append(
            lambda: function(*args))
        server = AgentIpcServer(self.session, self.service)
        info = server.start()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                call_agent_ipc, info, "late-agent", "read_snapshot")
            deadline = time.monotonic() + 2.0
            while not queued and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertTrue(queued)
            server.shutdown()
            queued.pop()()
            with self.assertRaisesRegex(RuntimeError, "revoked"):
                future.result(timeout=1.0)

    def test_timed_out_qt_callback_is_cancelled_before_late_execution(self):
        queued = []
        self.session._post = lambda function, *args: queued.append(
            lambda: function(*args))
        server = AgentIpcServer(self.session, self.service)
        request = {
            "agent_id": "late-agent", "method": "read_snapshot",
            "arguments": {},
        }
        with patch("flow_controller.agent.ipc.CLIENT_REQUEST_TIMEOUT_S", 0.05):
            with ThreadPoolExecutor(max_workers=1) as pool:
                answer = pool.submit(server._call_on_qt, request).result(timeout=1)
        self.assertFalse(answer["ok"])
        self.assertIn("did not answer", answer["error"])
        audit_path = self.service.audit.path
        self.assertFalse(audit_path.exists())
        queued.pop()()
        self.assertFalse(audit_path.exists())

    def test_setpoint_request_timeout_cancels_late_qt_execution(self):
        self._make_live_ready()
        self.service.set_live_enabled(True)
        queued = []
        self.session._post = lambda function, *args: queued.append(
            lambda: function(*args))
        server = AgentIpcServer(self.session, self.service)
        request = {
            "agent_id": "setpoint-agent", "method": "set_role_setpoint",
            "arguments": {"role": "nh3_rich", "value": 2.0},
        }
        with patch("flow_controller.agent.ipc.CLIENT_REQUEST_TIMEOUT_S", 0.05):
            with ThreadPoolExecutor(max_workers=1) as pool:
                answer = pool.submit(
                    server._call_on_qt, request).result(timeout=1)
        self.assertFalse(answer["ok"])
        self.assertIn("did not answer", answer["error"])
        queued.pop()()
        self.assertTrue(self.session.setpoint_queue.empty())

    @unittest.skipUnless(importlib.util.find_spec("mcp"),
                         "the optional MCP dependency is not installed")
    def test_stdio_mcp_proxy_reaches_the_authenticated_app_pipe(self):
        from mcp import StdioServerParameters
        from mcp.client import Client

        server = AgentIpcServer(self.session, self.service)
        info = server.start()
        self.addCleanup(server.shutdown)
        environment = {
            "FLOW_AGENT_PIPE": info["address"],
            "FLOW_AGENT_PIPE_FAMILY": info["family"],
            "FLOW_AGENT_TOKEN": info["token"],
            "FLOW_AGENT_ID": "mcp-agent",
        }

        async def invoke():
            parameters = StdioServerParameters(
                command=sys.executable,
                args=["-m", "flow_controller.agent.mcp_server"],
                env=environment,
                cwd=str(Path(__file__).resolve().parents[1]))
            async with Client(parameters) as client:
                return await client.call_tool("read_snapshot", {})

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(asyncio.run, invoke())
            deadline = time.monotonic() + 15
            while not future.done() and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.005)
            result = future.result(timeout=1)
        self.assertFalse(result.is_error)
        self.assertIn("connection", result.content[0].text)
        self.assertEqual(self.audit_rows()[0]["agent"], "mcp-agent")


if __name__ == "__main__":
    unittest.main()
