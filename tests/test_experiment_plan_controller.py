import os
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from flow_controller.core.experiment_plan import (
    AbortProcedure,
    ExperimentPlan,
    PlanStage,
    RUN_ABORTED,
    RUN_FINISHED,
    StabilityCondition,
)
from flow_controller.core.session import FlowSession
from flow_controller.core.sequence import HOLD, Keyframe, Sequence, Track


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class ExperimentPlanControllerTests(unittest.TestCase):
    def setUp(self):
        self.session = FlowSession(worker=SimpleNamespace(shutdown=lambda: None))
        self.addCleanup(self.session.shutdown)
        self.session.unit_prefs = {}
        self.session.assignments["nh3_rich"] = "A"
        self.session.selection = {"A": ("NH3", "Rich")}
        self.session.controllers_connected = True
        self.session.is_monitoring = True
        self.session._latest_samples = {
            "A": {"flow": 0.0, "sp": 0.0, "press": 14.7, "temp": 20.0}}
        self.session._latest_timestamp = datetime.now()
        self.clock = FakeClock()
        self.controller = self.session.experiment_plans
        self.controller._clock = self.clock

    def plan(self, stage, *, abort="zero_all"):
        return ExperimentPlan(
            "controller test", AbortProcedure(abort), (stage,))

    def test_setpoint_plan_runs_through_session_boundary(self):
        plan = self.plan(PlanStage(
            "condition", {"nh3_rich": 2.0}, timeout_s=10,
            condition=StabilityCondition(
                targets={"nh3_rich": 2.0}, stable_s=2.0)))
        self.assertTrue(self.controller.set_plan(plan))
        self.assertTrue(self.controller.start())
        self.assertEqual(self.session.setpoint_queue.get_nowait(), ("A", 2.0))

        self.session._latest_samples["A"]["flow"] = 2.0
        self.controller.tick()
        self.clock.advance(2.0)
        self.controller.tick()
        self.assertEqual(self.controller.state, RUN_FINISHED)

    def test_live_command_ceiling_rejects_plan_before_start(self):
        with patch("flow_controller.core.session.unit_prefs.save", return_value=None):
            self.session.set_max_flow("A", 1.0)
        plan = self.plan(PlanStage("too high", {"nh3_rich": 2.0}))
        self.assertFalse(self.controller.set_plan(plan))

    def test_timeout_invokes_the_declared_verified_zero(self):
        plan = self.plan(PlanStage(
            "never", {"nh3_rich": 1.0}, timeout_s=1.0,
            condition=StabilityCondition(targets={"nh3_rich": 1.0})))
        self.controller.set_plan(plan)
        with patch.object(self.session, "zero_all", return_value=True) as zero:
            self.controller.start()
            self.clock.advance(1.0)
            self.controller.tick()
        zero.assert_called_once_with()
        self.assertEqual(self.controller.state, RUN_ABORTED)

    def test_monitor_stop_aborts_an_active_plan(self):
        plan = self.plan(PlanStage(
            "wait", {"nh3_rich": 1.0}, min_dwell_s=5, timeout_s=10))
        self.controller.set_plan(plan)
        with patch.object(self.session, "zero_all", return_value=True) as zero:
            self.controller.start()
            self.session.monitoring_changed.emit(False)
        zero.assert_called_once_with()
        self.assertEqual(self.controller.state, RUN_ABORTED)

    def test_communication_fault_aborts_an_active_plan(self):
        plan = self.plan(PlanStage(
            "wait", {"nh3_rich": 1.0}, min_dwell_s=5, timeout_s=10))
        self.controller.set_plan(plan)
        with patch.object(self.session, "zero_all", return_value=True) as zero:
            self.controller.start()
            self.session.communication_fault.emit("read timeout on Unit A")
        zero.assert_called_once_with()
        self.assertEqual(self.controller.state, RUN_ABORTED)

    def test_agent_started_plan_aborts_if_ramp_envelope_changes(self):
        plan = self.plan(PlanStage(
            "wait", {"nh3_rich": 1.0}, min_dwell_s=5, timeout_s=10))
        self.controller.set_plan(plan)
        with patch.object(self.session, "zero_all", return_value=True) as zero:
            self.assertTrue(self.controller.start(
                frozen_plan=plan.to_dict(), frozen_sequences={},
                agent_started=True))
            self.session.unit_ramp_changed.emit("A", 99.0)
        zero.assert_called_once_with()
        self.assertEqual(self.controller.state, RUN_ABORTED)

    def test_human_started_plan_keeps_existing_ramp_change_semantics(self):
        plan = self.plan(PlanStage(
            "wait", {"nh3_rich": 1.0}, min_dwell_s=5, timeout_s=10))
        self.controller.set_plan(plan)
        with patch.object(self.session, "zero_all", return_value=True) as zero:
            self.assertTrue(self.controller.start())
            self.session.unit_ramp_changed.emit("A", 99.0)
        zero.assert_not_called()
        self.assertNotEqual(self.controller.state, RUN_ABORTED)

    def test_agent_start_executes_frozen_plan_not_later_mutable_dict_change(self):
        plan = self.plan(PlanStage("command", {"nh3_rich": 1.0}))
        self.controller.set_plan(plan)
        frozen_plan = plan.to_dict()
        plan.stages[0].setpoints["nh3_rich"] = 4.0

        self.assertTrue(self.controller.start(
            frozen_plan=frozen_plan, frozen_sequences={}, agent_started=True))

        self.assertEqual(self.session.setpoint_queue.get_nowait(), ("A", 1.0))

    def test_stale_telemetry_cannot_satisfy_a_condition(self):
        plan = self.plan(PlanStage(
            "stale", {"nh3_rich": 1.0}, timeout_s=10,
            condition=StabilityCondition(
                targets={"nh3_rich": 1.0}, stable_s=1.0)))
        self.controller.set_plan(plan)
        self.controller.start()
        self.session._latest_samples["A"]["flow"] = 1.0
        self.session._latest_timestamp = datetime(2000, 1, 1)
        self.controller.tick()
        self.clock.advance(2.0)
        self.controller.tick()
        self.assertNotEqual(self.controller.state, RUN_FINISHED)

    def test_safe_abort_watchdog_queues_zero_without_qt_event_processing(self):
        plan = self.plan(PlanStage(
            "blocked-ui", {"nh3_rich": 1.0}, timeout_s=0.05,
            condition=StabilityCondition(targets={"nh3_rich": 99.0})))
        self.controller.set_plan(plan)
        self.assertTrue(self.controller.start())

        # Intentionally do not process Qt events. The independent timer must
        # still reach the monitor owner's priority queue.
        time.sleep(0.15)

        request = self.session._zero_request_queue.get_nowait()
        self.assertEqual(request.scope, "plan_watchdog_all")
        self.assertEqual(request.units, ("A",))
        self.assertIn("A", self.session._watchdog_locked_units)

    def test_referenced_sequences_are_frozen_when_plan_starts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "later.fcseq.json"
            Sequence(name="original", tracks=[Track(
                "nh3_rich", "NH3", keyframes=[
                    Keyframe(0, 0, HOLD), Keyframe(1, 1, HOLD)])]).save(path)
            plan = self.plan(PlanStage(
                "first", {"nh3_rich": 0.0}, min_dwell_s=1, timeout_s=10))
            plan = ExperimentPlan(
                plan.name, plan.abort,
                plan.stages + (PlanStage(
                    "later", sequence=str(path), timeout_s=10),))
            self.assertTrue(self.controller.set_plan(plan))
            self.assertTrue(self.controller.start())

            Sequence(name="changed", tracks=[Track(
                "nh3_rich", "NH3", keyframes=[
                    Keyframe(0, 0, HOLD), Keyframe(1, 4, HOLD)])]).save(path)

            frozen = self.controller._frozen_sequences[str(path)]
            self.assertEqual(frozen.tracks[0].keyframes[-1].value, 1.0)


if __name__ == "__main__":
    unittest.main()
