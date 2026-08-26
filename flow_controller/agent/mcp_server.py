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
            "Live authority is default-off and controlled by the operator in "
            "the app. Read the rig state and submit drafts at any time. A "
            "setpoint always needs a per-action operator confirmation. One "
            "exact plan may be started once while the live-control toggle "
            "is armed."))

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
    def submit_sequence_draft(sequence: dict) -> dict:
        """Validate and place a sequence in the editor for operator review."""
        return _call("submit_sequence_draft", {"sequence": sequence})

    @server.tool()
    def submit_plan_draft(plan: dict) -> dict:
        """Validate and place an experiment plan in the UI for operator review."""
        return _call("submit_plan_draft", {"plan": plan})

    @server.tool()
    def set_role_setpoint(role: str, value: float) -> dict:
        """Request one role setpoint; the operator must approve it in the app."""
        return _call("set_role_setpoint", {"role": role, "value": value})

    @server.tool()
    def start_armed_plan() -> dict:
        """Start the one exact plan armed by the operator, once while enabled."""
        return _call("start_armed_plan")

    return server


def main():
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
