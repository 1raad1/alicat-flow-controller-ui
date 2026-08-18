import unittest

from flow_controller.infrastructure.alicat_protocol import AlicatProtocol


class AlicatProtocolTests(unittest.TestCase):
    def test_parse_labelled_gas_table(self):
        response = "A G0 Air\r\nA G7 H2\r\nA G14 CH4\r\n"
        self.assertEqual(
            AlicatProtocol.parse_gas_table(response),
            {0: "Air", 7: "H2", 14: "CH4"},
        )

    def test_parse_legacy_gas_table(self):
        self.assertEqual(
            AlicatProtocol.parse_gas_table("A 0 Air 3 He 8 N2"),
            {0: "Air", 3: "He", 8: "N2"},
        )

    def test_parse_numeric_response_ignores_labels(self):
        values = AlicatProtocol.parse_numeric_response(
            "A VD 14.70 23.4 1.25 1.50 -0.25 42.0 %", "A")
        self.assertEqual(values, [14.7, 23.4, 1.25, 1.5, -0.25, 42.0])

    def test_question_mark_response_is_unsupported(self):
        self.assertIsNone(AlicatProtocol.parse_numeric_response("A ?", "A"))


if __name__ == "__main__":
    unittest.main()

