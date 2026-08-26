import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from flow_controller.agent.authority import AgentAuthority, AuthorityError
from flow_controller.core.experiment_plan import (
    AbortProcedure, ExperimentPlan, PlanStage)
from flow_controller.core.sequence import HOLD, Keyframe, Sequence, Track
from flow_controller.core.session import FlowSession


class AgentAuthorityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.prefs_patch = patch.dict(os.environ, {
            "FLOW_CONTROLLER_UNIT_PREFS": str(
                Path(self.tmp.name) / "unit_prefs.json")})
        self.prefs_patch.start()
        self.addCleanup(self.prefs_patch.stop)

        self.session = FlowSession(worker=SimpleNamespace(shutdown=lambda: None))
        self.addCleanup(self.session.shutdown)
        self.session.assignments["nh3_rich"] = "A"
        self.session.selection = {"A": ("NH3", "Rich")}
        self.session.unit_prefs = {
            "A": {"max_flow": 10.0, "ramp": 2.0,
                  "ramp_off": False}}
        self.session.controllers_connected = True
        self.session.is_monitoring = True
        self.authority = AgentAuthority(self.session, self.session)

    @staticmethod
    def plan(value=2.0, name="armed plan"):
        return ExperimentPlan(
            name, AbortProcedure("zero_all"),
            (PlanStage("command", {"nh3_rich": value}),))

    def test_authority_is_default_off_and_requires_live_rig(self):
        self.assertEqual(self.authority.status(), {
            "enabled": False, "envelope": None})
        self.assertFalse(self.authority.revoke("already off"))
        with self.assertRaisesRegex(AuthorityError, "disabled"):
            self.authority.check_role("nh3_rich", 1.0)

        self.session.controllers_connected = False
        with self.assertRaisesRegex(AuthorityError, "connected"):
            self.authority.enable()
        self.session.controllers_connected = True
        self.session.is_monitoring = False
        with self.assertRaisesRegex(AuthorityError, "Monitoring"):
            self.authority.enable()

    def test_role_envelope_is_json_safe_and_returns_frozen_limits(self):
        self.session.assignments["air_stage1"] = "B"
        self.session.unit_prefs["B"] = {"max_flow": 20.0}
        envelope = self.authority.enable()
        self.assertEqual(envelope["roles"], {
            "nh3_rich": {
                "unit": "A", "max_flow": 10.0, "ramp_rate": 2.0}})
        self.assertEqual(envelope["excluded_roles"], ["air_stage1"])
        self.assertIsNone(envelope["plan"])
        json.dumps(self.authority.status(), allow_nan=False)

        role = self.authority.check_role("nh3_rich", 10.0)
        self.assertEqual(role, {
            "unit": "A", "max_flow": 10.0, "ramp_rate": 2.0})
        role["max_flow"] = 999
        self.assertEqual(
            self.authority.status()["envelope"]["roles"]["nh3_rich"]
            ["max_flow"], 10.0)

    def test_setpoint_check_rejects_bad_or_over_envelope_values(self):
        self.authority.enable()
        for value in (True, float("nan"), float("inf"), -0.01):
            with self.subTest(value=value), self.assertRaises(AuthorityError):
                self.authority.check_role("nh3_rich", value)
        with self.assertRaisesRegex(AuthorityError, "exceeds"):
            self.authority.check_role("nh3_rich", 10.01)
        with self.assertRaisesRegex(AuthorityError, "not in"):
            self.authority.check_role("air_stage1", 0)

    def test_authority_stays_enabled_until_revoked(self):
        changes = []
        self.authority.changed.connect(
            lambda enabled, reason: changes.append((enabled, reason)))
        self.authority.enable()
        self.assertTrue(self.authority.status()["enabled"])
        self.authority.check_role("nh3_rich", 1)
        self.assertTrue(self.authority.status()["enabled"])
        self.authority.revoke("operator switched live control off")
        self.assertEqual(
            changes[-1], (False, "operator switched live control off"))
        self.assertFalse(self.authority.status()["enabled"])

    def test_relevant_live_setting_signals_revoke_immediately(self):
        cases = (
            (self.session.assignments_changed, ({},), "assignments changed"),
            (self.session.max_flow_changed, ("A", 9.0), "maximum flow changed"),
            (self.session.unit_ramp_changed, ("A", 1.0), "ramp settings changed"),
            (self.session.connection_changed, (False,), "controllers disconnected"),
            (self.session.monitoring_changed, (False,), "monitoring stopped"),
            (self.session.communication_fault, ("read timeout on Unit A",),
             "communication fault: read timeout on Unit A"),
        )
        for signal, arguments, reason in cases:
            with self.subTest(reason=reason):
                changes = []
                self.authority.changed.connect(
                    lambda enabled, why, sink=changes: sink.append((enabled, why)))
                self.authority.enable()
                signal.emit(*arguments)
                self.assertFalse(self.authority.status()["enabled"])
                self.assertEqual(changes[-1], (False, reason))

    def test_direct_assignment_or_limit_change_is_caught_at_check_time(self):
        self.authority.enable()
        self.session.assignments["nh3_rich"] = "B"
        with self.assertRaisesRegex(AuthorityError, "changed"):
            self.authority.check_role("nh3_rich", 1)
        self.assertFalse(self.authority.status()["enabled"])

        self.session.assignments["nh3_rich"] = "A"
        self.authority.enable()
        self.session.unit_prefs["A"]["max_flow"] = 9.0
        with self.assertRaisesRegex(AuthorityError, "changed"):
            self.authority.check_role("nh3_rich", 1)

    def test_exact_plan_fingerprint_is_one_shot(self):
        plan = self.plan(2.0)
        envelope = self.authority.enable(plan)
        metadata = envelope["plan"]
        self.assertEqual(metadata["command_roles"], ["nh3_rich"])
        self.assertEqual(len(metadata["fingerprint"]), 64)
        self.assertEqual(self.authority.check_armed_plan(plan), metadata)

        with self.assertRaisesRegex(AuthorityError, "does not match"):
            self.authority.check_armed_plan(self.plan(3.0))
        self.assertFalse(self.authority.status()["enabled"])
        self.authority.enable(plan)
        self.assertEqual(self.authority.consume_plan(plan), metadata)
        with self.assertRaisesRegex(AuthorityError, "already been consumed"):
            self.authority.consume_plan(plan)

    def test_sequence_command_roles_must_all_be_in_envelope(self):
        sequence_path = Path(self.tmp.name) / "two-lines.fcseq.json"
        Sequence(name="two lines", tracks=[
            Track("nh3_rich", "Rich NH3", keyframes=[
                Keyframe(0, 0, HOLD), Keyframe(1, 1, HOLD)]),
            Track("air_stage1", "Stage 1 air", keyframes=[
                Keyframe(0, 0, HOLD), Keyframe(1, 2, HOLD)]),
        ]).save(sequence_path)
        plan = ExperimentPlan(
            "sequence plan", AbortProcedure("zero_all"),
            (PlanStage("replay", sequence=str(sequence_path)),))
        with self.assertRaisesRegex(AuthorityError, "air_stage1"):
            self.authority.enable(plan)

        self.session.assignments["air_stage1"] = "B"
        self.session.unit_prefs["B"] = {
            "max_flow": 20.0, "ramp": 4.0, "ramp_off": False}
        envelope = self.authority.enable(plan)
        self.assertEqual(
            envelope["plan"]["command_roles"],
            ["air_stage1", "nh3_rich"])

        Sequence(name="changed", tracks=[
            Track("nh3_rich", "Rich NH3", keyframes=[
                Keyframe(0, 0, HOLD), Keyframe(1, 3, HOLD)]),
            Track("air_stage1", "Stage 1 air", keyframes=[
                Keyframe(0, 0, HOLD), Keyframe(1, 4, HOLD)]),
        ]).save(sequence_path)
        with self.assertRaisesRegex(AuthorityError, "does not match"):
            self.authority.check_armed_plan(plan)

    def test_consumed_plan_bundle_keeps_the_armed_sequence_snapshot(self):
        sequence_path = Path(self.tmp.name) / "armed.fcseq.json"
        Sequence(name="original", tracks=[Track(
            "nh3_rich", "Rich NH3", keyframes=[
                Keyframe(0, 0, HOLD), Keyframe(1, 1, HOLD)])]).save(sequence_path)
        plan = ExperimentPlan(
            "sequence plan", AbortProcedure("zero_all"),
            (PlanStage("replay", sequence=str(sequence_path)),))
        self.authority.enable(plan)

        metadata, frozen_plan, frozen = self.authority.consume_plan_bundle(plan)
        Sequence(name="changed", tracks=[Track(
            "nh3_rich", "Rich NH3", keyframes=[
                Keyframe(0, 0, HOLD), Keyframe(1, 4, HOLD)])]).save(sequence_path)

        self.assertEqual(metadata["name"], "sequence plan")
        self.assertEqual(frozen_plan, plan.to_dict())
        self.assertEqual(
            frozen[str(sequence_path)]["tracks"][0]["keyframes"][-1]["v"],
            1.0)

    def test_missing_or_disabled_limits_exclude_roles_and_refuse_enable(self):
        cases = (
            {},
            {"max_flow": 10.0},
            {"ramp": 2.0},
            {"max_flow": 10.0, "ramp": 2.0, "ramp_off": True},
            {"max_flow": 0.0, "ramp": 2.0},
            {"max_flow": 10.0, "ramp": 0.0},
        )
        for prefs in cases:
            with self.subTest(prefs=prefs):
                self.session.unit_prefs = {"A": prefs}
                preview = self.authority.preview()
                self.assertEqual(preview["roles"], {})
                self.assertEqual(preview["excluded_roles"], ["nh3_rich"])
                with self.assertRaisesRegex(AuthorityError, "No assigned role"):
                    self.authority.enable()

    def test_enable_rejects_changes_after_operator_preview(self):
        preview = self.authority.preview()
        self.session.unit_prefs["A"]["max_flow"] = 9.0
        with self.assertRaisesRegex(AuthorityError, "changed after"):
            self.authority.enable(expected_envelope=preview)
        self.assertFalse(self.authority.status()["enabled"])


if __name__ == "__main__":
    unittest.main()
