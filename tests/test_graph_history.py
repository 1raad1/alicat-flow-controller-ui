import unittest
from datetime import datetime, timedelta

from flow_controller.core.graph_history import GraphHistory


def sample(value):
    return {
        "flow": value,
        "sp": value + 1,
        "press": value + 2,
        "temp": value + 3,
        "internal_error": value + 4,
        "valve_drives": (value + 5,),
    }


class GraphHistoryTests(unittest.TestCase):
    def test_export_preserves_rotation_blanks_and_late_unit_alignment(self):
        history = GraphHistory(limit=3)
        history.set_units([1])
        started = datetime(2026, 1, 1)
        history.push(1, started, {1: sample(10)})
        history.push(2, started + timedelta(seconds=1), {1: sample(20)})
        history.set_units([1, 2])
        history.push(3, started + timedelta(seconds=2), {1: sample(30), 2: sample(40)})
        history.push(4, started + timedelta(seconds=3), {1: sample(50), 2: {}})

        header, rows = history.export_rows(units=[1, 2], metric_keys=["flow"])

        self.assertEqual(header, ["time_s", "U1_flow_SLPM", "U2_flow_SLPM"])
        self.assertEqual(rows, [[1.0, 20, None], [2.0, 30, 40], [3.0, 50, None]])

    def test_revision_tracks_rotation_shape_clear_and_limit_changes(self):
        history = GraphHistory(limit=2)
        started = datetime(2026, 1, 1)
        self.assertEqual(history.revision, 0)
        history.set_units([1])
        after_shape = history.revision
        history.set_units([1])
        self.assertEqual(history.revision, after_shape)

        history.push(1, started, {1: sample(1)})
        history.push(2, started + timedelta(seconds=1), {1: sample(2)})
        before_rotation = history.revision
        history.push(3, started + timedelta(seconds=2), {1: sample(3)})
        self.assertGreater(history.revision, before_rotation)
        self.assertEqual(list(history.times()), [1.0, 2.0])

        history.clear(generation=3)
        after_clear = history.revision
        self.assertFalse(history.push(3, started + timedelta(seconds=3), {1: sample(4)}))
        self.assertEqual(history.revision, after_clear)
        history.set_limit(4)
        self.assertGreater(history.revision, after_clear)


if __name__ == "__main__":
    unittest.main()
