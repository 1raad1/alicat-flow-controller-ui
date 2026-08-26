"""Stdio MCP proxy for the running flow-controller application."""

from __future__ import annotations

import os

from .ipc import call_agent_ipc


def _connection_info():
    return {
        "address": os.environ["FLOW_AGENT_PIPE"],
        "family": os.environ.get("FLOW_AGENT_PIPE_FAMILY", "AF_PIPE"),
        "token": os.environ["FLOW_AGENT_TOKEN"],
    }


def _call(method, arguments=None):
    return call_agent_ipc(
        _connection_info(), os.environ.get("FLOW_AGENT_ID", "agent"),
        method, arguments)


def build_server():
    try:
        from mcp.server.mcpserver import MCPServer
    except ImportError as exc:
        raise RuntimeError(
            "Agent MCP support is not installed. Re-run install.bat to install "
            "the application's agent dependency.") from exc

    server = MCPServer(
        "Flow Controller",
        instructions=(
            "Use this MCP server for every flow-controller read and command; "
            "never access controller serial ports or app data files directly. "
            "Live authority is default-off and controlled by one operator "
            "warning and toggle in the app. While enabled, setpoints may be "
            "changed automatically through the same optional MAX FLOW and "
            "ramp policies as manual entry, and saved "
            "sequences may be started when measured flows match their opening. "
            "For sequence edits, read the source then create a named variant. "
            "For combustion-condition changes, prepare targets and, only when "
            "the user asked for live changes, apply every returned target."))

    @server.tool()
    def read_snapshot() -> dict:
        """Read assignments, live Alicat telemetry, ramp policies and limits."""
        return _call("read_snapshot")

    @server.tool()
    def read_history(window_s: float | None = None,
                     units: list[str] | None = None,
                     metrics: list[str] | None = None) -> dict:
        """Read copied, windowed telemetry history without controlling the rig."""
        return _call("read_history", {
            "window_s": window_s, "units": units, "metrics": metrics})

    @server.tool()
    def read_derived_state(duration_s: float = 5.0,
                           tolerance: float = 0.05) -> dict:
        """Read phi estimates and flow/setpoint stability over a time window."""
        return _call("read_derived_state", {
            "duration_s": duration_s, "tolerance": tolerance})

    @server.tool()
    def list_saved_sequences() -> dict:
        """List local saved sequences and whether the current rig can run them."""
        return _call("list_saved_sequences")

    @server.tool()
    def read_sequence(name: str | None = None) -> dict:
        """Read full keyframes for a named or currently selected sequence."""
        return _call("read_sequence", {"name": name})

    @server.tool()
    def submit_sequence_draft(sequence: dict) -> dict:
        """Validate and place a sequence in the editor for operator review."""
        return _call("submit_sequence_draft", {"sequence": sequence})

    @server.tool()
    def create_sequence_variant(new_name: str, sequence: dict,
                                source_fingerprint: str,
                                source_name: str | None = None) -> dict:
        """Validate and save a non-overwriting variant of a sequence just read."""
        return _call("create_sequence_variant", {
            "new_name": new_name,
            "sequence": sequence,
            "source_fingerprint": source_fingerprint,
            "source_name": source_name,
        })

    @server.tool()
    def prepare_combustion_condition(
            power_kw: float | None = None,
            h2_fraction: float | None = None,
            phi_stage1: float | None = None,
            phi_global: float | None = None,
            stage1_split: float | None = None) -> dict:
        """Prepare flow targets, filling omitted fields from the last condition.

        Fractions use 0..1. This stores targets for review but sends no flows.
        """
        return _call("prepare_combustion_condition", {
            "power_kw": power_kw,
            "h2_fraction": h2_fraction,
            "phi_stage1": phi_stage1,
            "phi_global": phi_global,
            "stage1_split": stage1_split,
        })

    @server.tool()
    def set_role_setpoint(role: str, value: float) -> dict:
        """Set one role automatically within the enabled live envelope."""
        return _call("set_role_setpoint", {"role": role, "value": value})

    @server.tool()
    def run_saved_sequence(name: str) -> dict:
        """Run one saved sequence once, within the enabled live envelope."""
        return _call("run_saved_sequence", {"name": name})

    return server


def main():
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
