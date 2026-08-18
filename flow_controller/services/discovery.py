"""Controller discovery orchestration without GUI dependencies."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from typing import Any

from alicat import FlowMeter

from ..domain.models import ControllerInfo, DiscoveryResult
from ..infrastructure.alicat_protocol import AlicatProtocol


ProgressCallback = Callable[[int, str], None]
ControllerCallback = Callable[[ControllerInfo], None]
GasProgressCallback = Callable[[int, int, ControllerInfo], None]
ContinueCallback = Callable[[], bool]


class DiscoveryService:
    """Discover addressed devices, then read each device's own gas table."""

    def __init__(
            self,
            protocol: AlicatProtocol,
            meter_factory: Callable[..., Any] = FlowMeter):
        self._protocol = protocol
        self._meter_factory = meter_factory

    async def scan(
            self,
            port: str,
            baudrate: int,
            units: Iterable[str],
            response_timeout: float,
            *,
            should_continue: ContinueCallback = lambda: True,
            on_progress: ProgressCallback | None = None,
            on_controller: ControllerCallback | None = None,
            on_gas_progress: GasProgressCallback | None = None,
    ) -> DiscoveryResult:
        unit_list = tuple(units)
        if not unit_list:
            return DiscoveryResult([])

        controllers: list[ControllerInfo] = []
        meter = None
        scan_error: str | None = None
        try:
            meter = self._meter_factory(
                address=port,
                unit=unit_list[0],
                baudrate=baudrate,
                timeout=response_timeout,
            )
            for index, unit in enumerate(unit_list, start=1):
                if not should_continue():
                    break
                if on_progress is not None:
                    on_progress(index, unit)
                meter.unit = unit
                meter.keys = [
                    'pressure', 'temperature', 'volumetric_flow',
                    'mass_flow', 'setpoint', 'gas',
                ]
                # Unused addresses are expected and must not accumulate as
                # shared-driver connection failures.
                meter.hw.timeouts = 0
                try:
                    reading = await meter.get()
                except asyncio.TimeoutError:
                    continue
                except OSError as exc:
                    if str(exc) == "Could not read values":
                        continue
                    scan_error = f"{type(exc).__name__}: {exc}"
                    break
                except (ValueError, IndexError):
                    continue

                controller = ControllerInfo(unit=unit, data=reading)
                controllers.append(controller)
                if on_controller is not None:
                    on_controller(controller)
        except Exception as exc:
            scan_error = f"{type(exc).__name__}: {exc}"
        finally:
            if meter is not None:
                try:
                    await meter.close()
                except Exception as exc:
                    if scan_error is None:
                        scan_error = f"Could not close {port}: {exc}"

        # Raw gas-table transactions open the port separately, so this phase
        # must remain after the shared FlowMeter connection has closed.
        if scan_error is None and should_continue():
            controller_count = len(controllers)
            for index, controller in enumerate(controllers, start=1):
                if not should_continue():
                    break
                if on_gas_progress is not None:
                    on_gas_progress(index, controller_count, controller)
                controller.supported_gases = self._protocol.query_gases(
                    port, controller.unit, baudrate)

        return DiscoveryResult(controllers, scan_error)

