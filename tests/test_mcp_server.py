import asyncio
import importlib.util
import unittest

from flow_controller.agent.mcp_server import build_server


@unittest.skipUnless(importlib.util.find_spec("mcp"),
                     "the optional MCP dependency is not installed")
class McpServerTests(unittest.TestCase):
    def test_server_advertises_read_draft_and_gated_live_tools(self):
        from mcp.client import Client

        async def inspect():
            async with Client(build_server()) as client:
                return await client.list_tools()

        result = asyncio.run(inspect())
        names = {tool.name for tool in result.tools}
        self.assertEqual(names, {
            "read_snapshot", "read_history", "read_derived_state",
            "list_saved_sequences", "read_sequence",
            "submit_sequence_draft", "create_sequence_variant",
            "prepare_combustion_condition",
            "set_role_setpoint", "run_saved_sequence",
        })


if __name__ == "__main__":
    unittest.main()
