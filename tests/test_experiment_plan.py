import json
import tempfile
import unittest
from pathlib import Path

from flow_controller.core.experiment_plan import (
    ABORT_ZERO_ALL,
    AbortProcedure,
    ExperimentPlan,
    ExperimentPlanExecutor,
    ExperimentPlanRunner,
    PlanStage,
    PlanValidationError,
    RUN_ABORTED,
    RUN_AWAITING_OPERATOR,
    RUN_FINISHED,
    RUN_HOLDING,
    StabilityCondition,
)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def plan_with(stage, *, abort=ABORT_ZERO_ALL):
    return ExperimentPlan(
        name="test", abort=AbortProcedure(abort), stages=(stage,))


class ValidationTests(unittest.TestCase):
    def test_documented_example_is_a_valid_plan(self):
        path = Path(__file__).resolve().parents[1] / "docs" / "example-experiment.fcplan.json"
        plan = ExperimentPlan.load(path)
        self.assertEqual(len(plan.stages), 2)
        self.assertEqual(plan.abort.action, "zero_all")

    def test_abort_procedure_is_required(self):
        raw = {
            "format": "flow-controller-experiment-plan", "version": 1,
            "name": "bad", "stages": [{"name": "one", "setpoints": {"air": 1}}],
        }
        with self.assertRaisesRegex(PlanValidationError, "abort is required"):
            ExperimentPlan.from_dict(raw)

    def test_non_finite_and_negative_setpoints_fail_closed(self):
        for value in (-1, float("nan"), float("inf"), "not a number"):
            with self.subTest(value=value):
                with self.assertRaises(PlanValidationError):
                    PlanStage(name="bad", setpoints={"air": value})

    def test_stage_requires_exactly_one_entry_action(self):
        with self.assertRaisesRegex(PlanValidationError, "exactly one"):
            PlanStage(name="none")
        with self.assertRaisesRegex(PlanValidationError, "exactly one"):
            PlanStage(name="both", setpoints={"air": 1}, sequence="run.json")

    def test_unknown_role_and_ceiling_are_rejected(self):
        plan = plan_with(PlanStage(
            name="one", setpoints={"air": 12, "mystery": 1}))
        with self.assertRaisesRegex(PlanValidationError, "above its 10 limit"):
            plan.validate_for(roles={"air", "mystery"}, ceilings={"air": 10})
        with self.assertRaisesRegex(PlanValidationError, "unknown role"):
            plan.validate_for(roles={"air"})

    def test_temperature_condition_can_target_a_negative_value(self):
        condition = StabilityCondition(
            metric="temperature", targets={"cold_line": -10}, tolerance=1)
        self.assertTrue(condition.evaluate(
            {"cold_line": {"temp": -9.5}})[0])

    def test_non_flow_condition_target_is_not_compared_with_flow_ceiling(self):
        plan = plan_with(PlanStage(
            name="pressure", setpoints={"air": 1},
            condition=StabilityCondition(
                metric="pressure", targets={"air": 14.7})))
        self.assertTrue(plan.validate_for(roles={"air"}, ceilings={"air": 2}))

    def test_cycles_are_rejected(self):
        with self.assertRaisesRegex(PlanValidationError, "does not allow stage cycles"):
            ExperimentPlan(
                name="loop", abort=AbortProcedure("zero_all"),
                stages=(
                    PlanStage("a", {"air": 1}, next_stage="b"),
                    PlanStage("b", {"air": 2}, next_stage="a"),
                ))

    def test_json_round_trip_is_strict_and_portable(self):
        original = plan_with(PlanStage(
            name="settle", setpoints={"air": 2}, min_dwell_s=1,
            timeout_s=12,
            condition=StabilityCondition(targets={"air": 2}, stable_s=3)))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "plan.json"
            original.save(path)
            restored = ExperimentPlan.load(path)
            self.assertEqual(restored.to_dict(), original.to_dict())
            self.assertEqual(json.loads(path.read_text())["format"],
                             "flow-controller-experiment-plan")


class RunnerTests(unittest.TestCase):
    def test_continuous_stability_advances_after_window(self):
        clock = FakeClock()
        plan = plan_with(PlanStage(
            name="settle", setpoints={"air": 2}, timeout_s=20,
            condition=StabilityCondition(
                targets={"air": 2}, tolerance=0.1, stable_s=5)))
        runner = ExperimentPlanRunner(plan, clock=clock)
        entered = runner.start()
        self.assertEqual(entered[0].setpoints, {"air": 2.0})

        clock.advance(1)
        self.assertEqual(runner.tick({"air": {"flow": 2.05}}), [])
        clock.advance(4.9)
        self.assertEqual(runner.tick({"air": {"flow": 2.0}}), [])
        clock.advance(0.1)
        events = runner.tick({"air": {"flow": 2.0}})
        self.assertEqual([event.kind for event in events],
                         ["stage_completed", "finished"])
        self.assertEqual(runner.state, RUN_FINISHED)

    def test_one_bad_sample_resets_stability_window(self):
        clock = FakeClock()
        stage = PlanStage(
            "settle", {"air": 2}, timeout_s=30,
            condition=StabilityCondition(targets={"air": 2}, stable_s=5))
        runner = ExperimentPlanRunner(plan_with(stage), clock=clock)
        runner.start()
        runner.tick({"air": {"flow": 2}})
        clock.advance(4)
        runner.tick({"air": {"flow": 1}})
        clock.advance(4)
        self.assertEqual(runner.tick({"air": {"flow": 2}}), [])
        clock.advance(5)
        self.assertEqual(runner.tick({"air": {"flow": 2}})[-1].kind, "finished")

    def test_timeout_defaults_to_declared_abort(self):
        clock = FakeClock()
        runner = ExperimentPlanRunner(plan_with(PlanStage(
            "never", {"air": 2}, timeout_s=3,
            condition=StabilityCondition(targets={"air": 2}))), clock=clock)
        runner.start()
        clock.advance(3)
        event = runner.tick({})[0]
        self.assertEqual(event.kind, "abort")
        self.assertEqual(event.abort_action, "zero_all")
        self.assertEqual(runner.state, RUN_ABORTED)

    def test_hold_and_operator_timeout_require_resolution(self):
        for action, state in (("hold", RUN_HOLDING),
                              ("request_operator", RUN_AWAITING_OPERATOR)):
            with self.subTest(action=action):
                clock = FakeClock()
                runner = ExperimentPlanRunner(plan_with(PlanStage(
                    "wait", {"air": 1}, timeout_s=1, on_timeout=action,
                    condition=StabilityCondition(targets={"air": 1}))), clock=clock)
                runner.start()
                clock.advance(1)
                self.assertEqual(runner.tick({})[0].kind,
                                 "held" if action == "hold" else "operator_required")
                self.assertEqual(runner.state, state)
                self.assertEqual(runner.tick({"air": {"flow": 1}}), [])
                events = runner.resolve_operator("abort")
                self.assertEqual(events[0].abort_action, "zero_all")

    def test_stage_transition_emits_next_entry_action(self):
        clock = FakeClock()
        plan = ExperimentPlan(
            "two", AbortProcedure("zero_all"), stages=(
                PlanStage("first", {"air": 1}, min_dwell_s=1, timeout_s=5),
                PlanStage("second", {"air": 2}, min_dwell_s=1, timeout_s=5),
            ))
        runner = ExperimentPlanRunner(plan, clock=clock)
        runner.start()
        clock.advance(1)
        events = runner.tick({})
        self.assertEqual(events[-1].kind, "stage_entered")
        self.assertEqual(events[-1].setpoints, {"air": 2.0})


class ExecutorTests(unittest.TestCase):
    def test_entry_setpoints_use_injected_control_boundary(self):
        clock = FakeClock()
        sent = []
        runner = ExperimentPlanRunner(plan_with(PlanStage(
            "one", {"air": 2}, min_dwell_s=1)), clock=clock)
        executor = ExperimentPlanExecutor(
            runner, telemetry=lambda: {},
            set_role_setpoint=lambda role, value: sent.append((role, value)) or True,
            start_sequence=lambda _path: True, sequence_active=lambda: False,
            abort_action=lambda _action: True)
        executor.start()
        self.assertEqual(sent, [("air", 2.0)])

    def test_refused_setpoint_invokes_declared_abort(self):
        clock = FakeClock()
        aborted = []
        runner = ExperimentPlanRunner(plan_with(PlanStage(
            "one", {"air": 2})), clock=clock)
        executor = ExperimentPlanExecutor(
            runner, telemetry=lambda: {},
            set_role_setpoint=lambda _role, _value: False,
            start_sequence=lambda _path: True, sequence_active=lambda: False,
            abort_action=lambda action: aborted.append(action) or True)
        events = executor.start()
        self.assertEqual(events[-1].kind, "abort")
        self.assertEqual(aborted, ["zero_all"])

    def test_sequence_stage_waits_for_replay_before_advancing(self):
        clock = FakeClock()
        active = [True]
        runner = ExperimentPlanRunner(plan_with(PlanStage(
            "recorded", sequence="run.fcseq.json", min_dwell_s=1,
            timeout_s=10)), clock=clock)
        executor = ExperimentPlanExecutor(
            runner, telemetry=lambda: {}, set_role_setpoint=lambda *_: True,
            start_sequence=lambda _path: True,
            sequence_active=lambda: active[0], abort_action=lambda _action: True)
        executor.start()
        clock.advance(2)
        self.assertEqual(executor.tick(), [])
        active[0] = False
        self.assertEqual(executor.tick()[-1].kind, "finished")


if __name__ == "__main__":
    unittest.main()
