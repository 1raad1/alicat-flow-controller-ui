"""Live telemetry reads and the per-device capability cache.

Alicat firmware varies in what it will answer.  The fast path asks for six
statistics in a single ``DV`` transaction; older units answer that with a
``?`` or with the wrong number of fields, and have to be polled one statistic
at a time instead.  Probing for this on every pass would double the serial
traffic, so each unit's answer is remembered in a small capability record and
the probe is not repeated once it has settled.

The reader is deliberately given its logger rather than importing one: the
diagnostics it emits ("combined telemetry active", "valve-drive unavailable")
belong in whatever log the operator is actually watching.
"""

from __future__ import annotations

import asyncio

from ..infrastructure.alicat_protocol import AlicatProtocol

#: Statistic numbers requested by the combined read, in response order:
#: pressure, temperature, mass flow, mass-flow setpoint, internal setpoint
#: error, valve drive.
COMBINED_STATISTICS = "1 2 3 5 37 173 13"

#: A combined read is abandoned after this many consecutive failures.  One
#: failure can be a collision on a shared bus; two in a row is the firmware.
COMBINED_FAILURE_LIMIT = 2


def blank_sample() -> dict:
    """A sample with every field missing.

    Failed reads must stay blank rather than repeating the previous value:
    a CSV column that silently holds the last good number is indistinguishable
    from a controller that genuinely is not moving.
    """
    return {
        'flow': None, 'sp': None, 'press': None, 'temp': None,
        'internal_error': None, 'valve_drives': (),
    }


class TelemetryReader:
    """Reads live samples, remembering what each unit's firmware supports."""

    def __init__(self, log=None, parse=None):
        self._log = log if log is not None else (lambda _message: None)
        self._parse = (parse if parse is not None
                       else AlicatProtocol.parse_numeric_response)
        self._support: dict[str, dict] = {}

    # -- capability cache ------------------------------------------------- #

    def reset(self):
        """Forget every capability answer.

        Called when the monitor restarts, because the port may now be talking
        to a different set of instruments.
        """
        self._support.clear()

    def state(self, unit) -> dict:
        """Return one complete per-device capability cache."""
        support = self._support.setdefault(unit, {})
        defaults = {
            'combined': None,
            'combined_failures': 0,
            'internal_error': None,
            'valve_drive': None,
            'valve_mode': None,
        }
        for key, value in defaults.items():
            support.setdefault(key, value)
        return support

    # -- reads ------------------------------------------------------------ #

    async def read_combined(self, fc, unit):
        """Read six live fields in one DV transaction when firmware supports it."""
        support = self.state(unit)
        if support['combined'] is False:
            return None
        raw = None
        try:
            raw = await asyncio.wait_for(
                fc._write_and_read(f"{unit}DV {COMBINED_STATISTICS}"),
                timeout=1.0)
            if raw and '?' in raw:
                support['combined'] = False
                if not support.get('combined_failure_logged'):
                    self._log(f"Unit {unit}: combined live telemetry is unsupported; "
                              "using compatible multi-request polling.")
                    support['combined_failure_logged'] = True
                return None
            values = self._parse(raw, unit)
            if values and len(values) == 6 and 0.0 <= values[5] <= 100.0:
                support['combined'] = True
                support['combined_failures'] = 0
                return {
                    'press': values[0], 'temp': values[1],
                    'flow': values[2], 'sp': values[3],
                    'internal_error': values[4],
                    'valve_drives': (values[5],),
                }
        except Exception:
            # A failed optimization probe must not interrupt normal polling.
            pass

        support['combined_failures'] += 1
        if support['combined_failures'] >= COMBINED_FAILURE_LIMIT:
            support['combined'] = False
            if not support.get('combined_failure_logged'):
                self._log(f"Unit {unit}: combined live telemetry did not return six valid "
                          f"fields (last response: {raw!r}); using compatible polling.")
                support['combined_failure_logged'] = True
        return None

    async def read_optimized(self, fc, unit):
        """Read all live fields, including valve drive 1, in one request."""
        sample = await self.read_combined(fc, unit)
        if sample is None:
            return None
        support = self.state(unit)
        if not support.get('combined_active_logged'):
            self._log(f"Unit {unit}: combined telemetry active "
                      f"(1 request/pass, valve drive 1).")
            support['combined_active_logged'] = True
        return sample

    async def read_internal_error(self, fc, unit):
        """Read Alicat statistic 173: mass flow minus ramp-limited setpoint."""
        support = self.state(unit)
        if support['internal_error'] is False:
            return None
        try:
            # A one-millisecond request gives a live device sample.
            raw = await asyncio.wait_for(
                fc._write_and_read(f"{unit}DV 1 173"), timeout=1.0)
            if raw and '?' in raw:
                support['internal_error'] = False
                if not support.get('internal_error_diagnostic_logged'):
                    self._log(f"Unit {unit}: internal setpoint-error telemetry is not supported "
                              f"(DV response: {raw!r}; requires compatible firmware).")
                    support['internal_error_diagnostic_logged'] = True
                return None
            values = self._parse(raw, unit)
            if values:
                support['internal_error'] = True
                if not support.get('internal_error_diagnostic_logged'):
                    self._log(f"Unit {unit}: internal setpoint-error telemetry active "
                              f"(DV response: {raw!r}).")
                    support['internal_error_diagnostic_logged'] = True
                return values[-1]
            if not support.get('internal_error_diagnostic_logged'):
                self._log(f"Unit {unit}: could not parse internal setpoint-error response: {raw!r}")
                support['internal_error_diagnostic_logged'] = True
        except Exception:
            # A diagnostic read must never interrupt normal flow control.
            pass
        return None

    async def read_valve_drive(self, fc, unit):
        """Read instantaneous valve-drive 1 using Alicat's VD command."""
        support = self.state(unit)
        if support['valve_drive'] is False or support['valve_mode'] == 'none':
            return ()
        vd_raw = None
        dv_raw = None
        try:
            if support['valve_mode'] != 'dv13':
                try:
                    vd_raw = await asyncio.wait_for(
                        fc._write_and_read(f"{unit}VD"), timeout=1.0)
                except Exception:
                    # During capability detection, a VD timeout is allowed to
                    # fall through to the older DV-statistic request.
                    if support['valve_mode'] == 'vd':
                        return ()
                values = self._parse(vd_raw, unit)
                if (values and 1 <= len(values) <= 3
                        and all(0.0 <= value <= 100.0 for value in values)):
                    support['valve_drive'] = True
                    support['valve_mode'] = 'vd'
                    if not support.get('valve_drive_diagnostic_logged'):
                        self._log(f"Unit {unit}: valve-drive telemetry active "
                                  f"(VD response: {vd_raw!r}).")
                        support['valve_drive_diagnostic_logged'] = True
                    return (values[0],)

                # Once a working VD mode has been established, treat a bad
                # response as transient rather than adding a second request.
                if support['valve_mode'] == 'vd':
                    return ()

            # VD was introduced later than the generic DV request.  Older
            # compatible firmware can still expose statistic 13 (valve drive)
            # through a one-millisecond DV sample.
            dv_raw = await asyncio.wait_for(
                fc._write_and_read(f"{unit}DV 1 13"), timeout=1.0)
            fallback_values = self._parse(dv_raw, unit)
            if fallback_values and 0.0 <= fallback_values[-1] <= 100.0:
                support['valve_drive'] = True
                support['valve_mode'] = 'dv13'
                if not support.get('valve_drive_diagnostic_logged'):
                    self._log(f"Unit {unit}: valve-drive telemetry active via DV fallback "
                              f"(VD response: {vd_raw!r}; DV response: {dv_raw!r}).")
                    support['valve_drive_diagnostic_logged'] = True
                return (fallback_values[-1],)

            if (vd_raw and '?' in vd_raw) and (dv_raw and '?' in dv_raw):
                support['valve_drive'] = False
                support['valve_mode'] = 'none'
            if not support.get('valve_drive_diagnostic_logged'):
                self._log(f"Unit {unit}: valve-drive telemetry unavailable or unparsed "
                          f"(VD response: {vd_raw!r}; DV response: {dv_raw!r}).")
                support['valve_drive_diagnostic_logged'] = True
        except Exception:
            # A diagnostic read must never interrupt normal flow control.
            pass
        return ()

    async def read_sample(self, fc, unit):
        """One complete live sample, by whichever route this unit supports.

        Valve drive is collected whenever the monitor runs, not only while a
        CSV is open, because graph export is available independently of
        logging and a gap in history cannot be filled in afterwards.
        """
        sample = await self.read_optimized(fc, unit)
        if sample is not None:
            return sample
        reading = await asyncio.wait_for(fc.get(), timeout=1.0)
        return {
            'flow': reading.get('mass_flow', 0),
            'sp': reading.get('setpoint', 0),
            'press': reading.get('pressure', 0),
            'temp': reading.get('temperature', 0),
            'internal_error': await self.read_internal_error(fc, unit),
            'valve_drives': await self.read_valve_drive(fc, unit),
        }
