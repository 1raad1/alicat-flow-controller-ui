import unittest

from flow_controller.domain.models import ControllerInfo


class ControllerInfoTests(unittest.TestCase):
    def test_gas_options_follow_register_order(self):
        controller = ControllerInfo(
            unit="A",
            data={"gas": "H2"},
            supported_gases={4: "N2", 0: "Air", 2: "H2"},
        )
        self.assertEqual(controller.gas_options(), ["Air", "H2", "N2"])

    def test_active_gas_is_fallback_when_table_is_unavailable(self):
        controller = ControllerInfo(unit="B", data={"gas": "NH3"})
        self.assertEqual(controller.gas_options(), ["NH3"])

    def test_duplicate_names_are_case_insensitive(self):
        controller = ControllerInfo(
            unit="C",
            data={"gas": "Unknown"},
            supported_gases={0: "Air", 1: "air", 2: "CH4"},
        )
        self.assertEqual(controller.gas_options(), ["Air", "CH4"])


if __name__ == "__main__":
    unittest.main()

