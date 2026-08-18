import unittest

from flow_controller.domain.safety import select_zero_units


class SafetySelectionTests(unittest.TestCase):
    def test_zero_fuel_excludes_only_air(self):
        assignments = [
            ("A", "Air"), ("B", "CH4"), ("C", "N2"), ("D", "air"),
        ]
        self.assertEqual(
            select_zero_units(assignments, include_air=False), ["B", "C"])

    def test_zero_all_includes_air(self):
        assignments = [("A", "Air"), ("B", "H2")]
        self.assertEqual(
            select_zero_units(assignments, include_air=True), ["A", "B"])

    def test_duplicate_units_are_returned_once(self):
        assignments = [("A", "H2"), ("A", "CH4"), ("B", "Air")]
        self.assertEqual(
            select_zero_units(assignments, include_air=True), ["A", "B"])


if __name__ == "__main__":
    unittest.main()

