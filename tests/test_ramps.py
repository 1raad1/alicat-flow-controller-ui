"""Named setpoint ramps: one in force per line, and superseding the one there.

Threads make timing the enemy of a readable test, so ``sleep`` is injected: a
ramp can be parked inside its first step and held there while the test does
something to it.  Nothing here waits on wall time.
"""

import threading
import unittest

from flow_controller.core.ramps import RampLeg, RampRunner, scaled_targets


class Parking:
    """A sleep that holds the first ramp to call it, and frees the rest."""

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self._park = True

    def __call__(self, _interval):
        if self._park:
            self._park = False
            self.entered.set()
            # Bounded, so a mistake in the test fails it rather than hanging
            # the suite.
            self.release.wait(5.0)


class RampRunnerTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.logged = []
        self.done = threading.Event()
        self.sleep = Parking()
        self.runner = RampRunner(
            lambda unit, value: self.sent.append((unit, round(value, 4))),
            log=self.logged.append, sleep=self.sleep)
        self.addCleanup(self.sleep.release.set)

    def start(self, target, *, steps=2, name='ch4_pilot', **kwargs):
        return self.runner.start(
            name, [RampLeg('A', 0.0, target)], steps, 0.5,
            on_done=lambda _completed: self.done.set(), **kwargs)

    def finished(self):
        self.assertTrue(self.done.wait(5.0), 'the ramp never finished')
        self.done.clear()

    def test_every_step_is_emitted_and_the_target_is_snapped_to(self):
        self.sleep.release.set()
        self.assertTrue(self.start(10.0, steps=2))
        self.finished()
        self.assertEqual(self.sent, [('A', 5.0), ('A', 10.0), ('A', 10.0)])

    def test_a_ramp_with_no_addressable_leg_does_not_start(self):
        self.assertFalse(self.runner.start('x', [RampLeg('', 0.0, 1.0)], 2, 0.5))

    def test_a_second_ramp_under_the_same_name_is_refused(self):
        self.assertTrue(self.start(10.0))
        self.assertTrue(self.sleep.entered.wait(5.0))
        self.assertTrue(self.runner.is_active('ch4_pilot'))
        self.assertFalse(self.start(50.0))

    def test_replace_hands_the_line_to_the_new_ramp(self):
        self.assertTrue(self.start(10.0))
        self.assertTrue(self.sleep.entered.wait(5.0))
        first = list(self.sent)
        self.assertTrue(self.start(50.0, replace=True))
        self.finished()
        # The new ramp ran to its own target, and the name is still recorded as
        # busy for nobody once it is done.
        self.assertEqual(self.sent[len(first):],
                         [('A', 25.0), ('A', 50.0), ('A', 50.0)])

        # The parked ramp wakes up superseded: it emits nothing further and says
        # so, and it does not take the entry of the ramp that replaced it.
        self.sleep.release.set()
        self.finished()
        self.assertEqual(self.sent[len(first) + 3:], [])
        self.assertTrue(any('stopped' in entry for entry in self.logged))
        self.assertFalse(self.runner.is_active('ch4_pilot'))

    def test_a_replaced_ramp_reports_that_it_did_not_complete(self):
        outcomes = []
        self.runner.start('ch4_pilot', [RampLeg('A', 0.0, 10.0)], 2, 0.5,
                          on_done=outcomes.append)
        self.assertTrue(self.sleep.entered.wait(5.0))
        self.assertTrue(self.start(50.0, replace=True))
        self.finished()
        self.sleep.release.set()
        for _ in range(50):
            if outcomes:
                break
            threading.Event().wait(0.02)
        self.assertEqual(outcomes, [False])

    def test_cancelling_frees_the_line_before_the_thread_notices(self):
        self.assertTrue(self.start(10.0))
        self.assertTrue(self.sleep.entered.wait(5.0))
        self.runner.cancel('ch4_pilot')
        self.assertFalse(self.runner.is_active('ch4_pilot'))
        # And the name can be used again without asking to replace anything.
        self.assertTrue(self.start(50.0))

    def test_cancel_all_frees_every_line(self):
        self.assertTrue(self.start(10.0))
        self.assertTrue(self.sleep.entered.wait(5.0))
        self.runner.cancel_all()
        self.assertEqual(self.runner.active, set())
        self.assertTrue(self.start(50.0))

    def test_an_error_in_emit_is_logged_and_the_line_is_released(self):
        self.sleep.release.set()
        runner = RampRunner(lambda _unit, _value: 1 / 0,
                            log=self.logged.append, sleep=self.sleep)
        done = threading.Event()
        runner.start('ch4_pilot', [RampLeg('A', 0.0, 10.0)], 2, 0.5,
                     on_done=lambda _completed: done.set())
        self.assertTrue(done.wait(5.0))
        self.assertTrue(any('Ramp error' in entry for entry in self.logged))
        self.assertFalse(runner.is_active('ch4_pilot'))


class GuardTests(unittest.TestCase):
    """The predicate the ignition sequence rides on."""

    def test_a_guard_that_goes_false_stops_the_ramp_where_it_is(self):
        sent = []
        allowed = {'value': True}
        runner = RampRunner(lambda unit, value: sent.append((unit, value)),
                            sleep=lambda _interval: allowed.update(value=False))
        done = threading.Event()
        runner.start('ignition', [RampLeg('A', 0.0, 10.0)], 4, 0.5,
                     guard=lambda: allowed['value'],
                     on_done=lambda completed: done.set())
        self.assertTrue(done.wait(5.0))
        # One step went out before the guard dropped, and no snap to target.
        self.assertEqual(sent, [('A', 2.5)])


class ScaledTargetTests(unittest.TestCase):
    def test_fuels_and_everything_else_scale_separately(self):
        scaled = scaled_targets({'ch4_pilot': 10.0, 'rich_air': 100.0},
                                {'ch4_pilot'}, 0.5, 0.25)
        self.assertEqual(scaled, {'ch4_pilot': 5.0, 'rich_air': 25.0})


if __name__ == '__main__':
    unittest.main()
