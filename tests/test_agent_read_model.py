from datetime import datetime, timedelta
from types import SimpleNamespace
import unittest

from flow_controller.core.agent_read_model import (
    build_snapshot,
    derive_state,
    flow_stability,
    windowed_history,
)
from flow_controller.core.graph_history import GraphHistory
from flow_controller.core.session import FlowSession
from flow_controller.domain.rql import AutoCalcRequest, FULL_RQL


def sample(flow, sp, *, press=14.7, temp=20.0):
    return {
        'flow': flow, 'sp': sp, 'press': press, 'temp': temp,
        'internal_error': None, 'valve_drives': (),
    }


class SnapshotTests(unittest.TestCase):
    def test_snapshot_copies_role_assignment_telemetry_and_policies(self):
        session = SimpleNamespace(
            _live_samples={'A': sample(1.25, 1.5)},
            assignments={'nh3_rich': 'A', 'h2_rich': None, 'nh3_lean': None,
                         'h2_lean': None, 'nh3_pilot': None,
                         'h2_pilot': None, 'ch4_pilot': None,
                         'rich_air': None, 'lean_air': None},
            custom_assignments={'B': 'custom_B'},
            selection={'A': ('NH3', 'Zone 1'), 'B': ('Air', 'General')},
            unit_prefs={'A': {'full_scale': 5.0, 'ramp': 0.2},
                        'B': {'ramp_off': True, 'max_flow': 3.0}},
            controllers_connected=True, is_connecting=False, is_monitoring=True,
            port='COM7', baudrate=57600, poll_interval_s=0.1,
            _latest_timestamp=datetime(2026, 8, 26, 12, 30),
        )

        snapshot = build_snapshot(session)

        self.assertTrue(snapshot['connection']['connected'])
        self.assertEqual(snapshot['connection']['latest_sample_at'],
                         '2026-08-26T12:30:00')
        self.assertEqual(snapshot['assignments']['nh3_rich'], 'A')
        self.assertEqual(snapshot['roles']['nh3_rich']['assignment'],
                         {'gas': 'NH3', 'zone': 'Zone 1'})
        self.assertEqual(snapshot['telemetry']['A']['pressure'], 14.7)
        self.assertEqual(snapshot['units']['A']['ramp_policy']['effective_rate'], 0.2)
        self.assertTrue(snapshot['units']['B']['ramp_policy']['disabled'])
        self.assertEqual(snapshot['declared_limits'], {'B': 3.0})

        snapshot['units']['A']['telemetry']['flow'] = 999.0
        self.assertEqual(session._live_samples['A']['flow'], 1.25)

    def test_full_scale_is_not_misrepresented_as_a_command_limit(self):
        session = SimpleNamespace(
            _live_samples={}, assignments={}, custom_assignments={},
            selection={'A': ('Air', 'General')},
            unit_prefs={'A': {'full_scale': 50.0}},
            controllers_connected=False, is_connecting=False, is_monitoring=False,
            port=None, baudrate=57600, poll_interval_s=0.0,
            _latest_timestamp=None,
        )
        snapshot = build_snapshot(session)
        self.assertEqual(snapshot['units']['A']['display_full_scale'], 50.0)
        self.assertIsNone(snapshot['units']['A']['command_max_flow'])
        self.assertEqual(snapshot['declared_limits'], {})

    def test_snapshot_preserves_a_current_failed_read_as_missing(self):
        session = SimpleNamespace(
            _live_samples={'A': sample(4.0, 4.0)},
            _latest_samples={'A': sample(None, None)}, assignments={},
            custom_assignments={}, selection={'A': ('Air', 'General')},
            unit_prefs={}, controllers_connected=True, is_connecting=False,
            is_monitoring=True, port='COM1', baudrate=57600,
            poll_interval_s=0.0, _latest_timestamp=None,
        )
        snapshot = build_snapshot(session)
        self.assertIsNone(snapshot['telemetry']['A']['flow'])
        self.assertIsNone(snapshot['telemetry']['A']['setpoint'])

    def test_snapshot_includes_last_combustion_condition_and_targets(self):
        request = AutoCalcRequest(
            power_kw=12.0, h2_fraction=0.4, phi_stage1=0.8,
            phi_global=0.6, split_rich=0.75)
        session = SimpleNamespace(
            _live_samples={}, assignments={}, custom_assignments={},
            selection={}, unit_prefs={}, controllers_connected=True,
            is_connecting=False, is_monitoring=True, port='COM1',
            baudrate=57600, poll_interval_s=0.1, _latest_timestamp=None,
            autocalc_request=request, autocalc_available=True,
            autocalc_config=FULL_RQL,
            target_flows={'rich_air': 4.5}, operating_mode='staged',
            phi_values=lambda: (0.79, 0.41, 0.59),
        )

        combustion = build_snapshot(session)['combustion']

        self.assertEqual(combustion['last_condition']['phi_stage1'], 0.8)
        self.assertEqual(combustion['prepared_targets'], {'rich_air': 4.5})
        self.assertEqual(combustion['live_phi']['stage2'], 0.41)


class HistoryAndDerivedStateTests(unittest.TestCase):
    def make_history(self, flows, setpoints):
        history = GraphHistory()
        history.set_units(['A'])
        start = datetime(2026, 8, 26, 12, 0)
        for generation, (flow, sp) in enumerate(zip(flows, setpoints), start=1):
            history.push(generation, start + timedelta(seconds=generation - 1),
                         {'A': sample(flow, sp)})
        return history

    def test_windowed_history_uses_graph_history_times_and_copies_values(self):
        history = self.make_history([0, 1, 2, 3], [0, 1, 2, 3])
        result = windowed_history(history, window_s=1.0, metric_keys=['flow'])
        series = result['series']['A']['flow']
        self.assertEqual(series, {'times_s': [2.0, 3.0], 'values': [2.0, 3.0]})
        series['values'][0] = 99.0
        self.assertEqual(history.series('A', 'flow')[1][-2], 2)

    def test_stability_requires_full_history_window_and_tolerance(self):
        history = self.make_history([0.98, 1.01, 1.0], [1.0, 1.0, 1.0])
        stable = flow_stability(history, 'A', duration_s=2.0, tolerance=0.03)
        self.assertTrue(stable['stable'])
        self.assertEqual(stable['reason'], 'stable')
        self.assertEqual(stable['samples'], 3)

        too_short = flow_stability(history, 'A', duration_s=3.0, tolerance=0.03)
        self.assertFalse(too_short['stable'])
        self.assertEqual(too_short['reason'], 'insufficient history')

        history = self.make_history([0.98, 0.80, 1.0], [1.0, 1.0, 1.0])
        unstable = flow_stability(history, 'A', duration_s=2.0, tolerance=0.03)
        self.assertFalse(unstable['stable'])
        self.assertEqual(unstable['reason'], 'outside tolerance')

    def test_missing_telemetry_cannot_pass_a_stability_gate(self):
        history = self.make_history([1.0, None, 1.0], [1.0, 1.0, 1.0])
        result = flow_stability(history, 'A', duration_s=2.0, tolerance=0.01)
        self.assertFalse(result['stable'])
        self.assertEqual(result['reason'], 'missing telemetry')

    def test_derived_state_uses_the_session_phi_method_and_assigned_roles(self):
        history = self.make_history([1.0, 1.0, 1.0], [1.0, 1.0, 1.0])
        session = SimpleNamespace(
            history=history,
            assignments={'nh3_rich': 'A', 'h2_rich': None},
            phi_values=lambda: (1.1, 0.9, 1.0),
        )
        state = derive_state(session, duration_s=2.0, tolerance=0.01)
        self.assertEqual(state['phi']['global'], 1.0)
        self.assertTrue(state['roles']['nh3_rich']['stable'])
        self.assertEqual(state['roles']['h2_rich']['reason'], 'unassigned')


class FlowSessionReadApiTests(unittest.TestCase):
    def test_session_exposes_only_copied_read_helpers(self):
        session = FlowSession(worker=SimpleNamespace(shutdown=lambda: None))
        self.addCleanup(session.shutdown)
        session.assignments['nh3_rich'] = 'A'
        session.selection = {'A': ('NH3', 'Zone 1')}
        session.unit_prefs = {'A': {'ramp': 0.5}}
        session._live_samples = {'A': sample(1.0, 1.0)}
        start = datetime(2026, 8, 26, 12, 0)
        session.history.set_units(['A'])
        for generation in range(1, 4):
            session.history.push(
                generation, start + timedelta(seconds=generation - 1),
                {'A': sample(1.0, 1.0)})

        snapshot = session.read_snapshot()
        history = session.read_history(window_s=1.0, metric_keys=['flow'])
        derived = session.read_derived_state(duration_s=2.0, tolerance=0.01)

        self.assertEqual(snapshot['units']['A']['ramp_policy']['effective_rate'], 0.5)
        self.assertEqual(history['series']['A']['flow']['values'], [1.0, 1.0])
        self.assertTrue(derived['roles']['nh3_rich']['stable'])


if __name__ == '__main__':
    unittest.main()
