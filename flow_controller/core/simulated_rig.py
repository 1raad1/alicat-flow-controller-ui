"""A deterministic, headless flow-meter rig for control-logic tests.

The model deliberately only represents what future condition logic needs:
commanded setpoints and measured flow.  Flow follows the command with a
first-order time constant; calling :meth:`SimulatedRig.advance` is the clock,
so tests never sleep or require a serial port.
"""

from __future__ import annotations

import math


class SimulatedController:
    """Async-shaped controller facade matching the small Alicat API we use."""

    def __init__(self, rig, unit):
        self._rig = rig
        self.unit = unit

    async def set_flow_rate(self, value):
        self._rig.set_setpoint(self.unit, value)

    async def get(self):
        return self._rig.reading(self.unit)


class SimulatedRig:
    """One or more meters whose flows respond exponentially to setpoints."""

    def __init__(self, units=('A',), *, time_constant_s=1.0, pressure=14.7,
                 temperature=20.0):
        tau = float(time_constant_s)
        if not math.isfinite(tau) or tau <= 0.0:
            raise ValueError('time_constant_s must be a positive finite number')
        units = tuple(str(unit) for unit in units)
        if not units or len(set(units)) != len(units):
            raise ValueError('units must be a non-empty collection of unique names')
        self.time_constant_s = tau
        self.pressure = float(pressure)
        self.temperature = float(temperature)
        self.time_s = 0.0
        self._state = {unit: {'flow': 0.0, 'setpoint': 0.0} for unit in units}

    @property
    def units(self):
        return tuple(self._state)

    def controller(self, unit):
        self._require_unit(unit)
        return SimulatedController(self, unit)

    def set_setpoint(self, unit, value):
        self._require_unit(unit)
        value = float(value)
        if not math.isfinite(value) or value < 0.0:
            raise ValueError('setpoint must be a non-negative finite number')
        self._state[unit]['setpoint'] = value

    def advance(self, seconds):
        """Advance the deterministic clock and apply the exact lag solution."""
        seconds = float(seconds)
        if not math.isfinite(seconds) or seconds < 0.0:
            raise ValueError('seconds must be a non-negative finite number')
        fraction = 1.0 - math.exp(-seconds / self.time_constant_s)
        for state in self._state.values():
            state['flow'] += (state['setpoint'] - state['flow']) * fraction
        self.time_s += seconds

    def reading(self, unit):
        self._require_unit(unit)
        state = self._state[unit]
        return {
            'mass_flow': state['flow'],
            'setpoint': state['setpoint'],
            'pressure': self.pressure,
            'temperature': self.temperature,
        }

    def sample(self, unit):
        """Return the application's normalized telemetry sample shape."""
        reading = self.reading(unit)
        return {
            'flow': reading['mass_flow'], 'sp': reading['setpoint'],
            'press': reading['pressure'], 'temp': reading['temperature'],
            'internal_error': reading['mass_flow'] - reading['setpoint'],
            'valve_drives': (),
        }

    def samples(self):
        return {unit: self.sample(unit) for unit in self.units}

    def _require_unit(self, unit):
        if unit not in self._state:
            raise KeyError(f'unknown simulated unit: {unit}')
