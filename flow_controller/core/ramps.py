"""Linear setpoint ramps.

Nothing here talks to hardware.  A ramp interpolates and hands each step to
``emit``, which is the session's setpoint queue; the monitor loop is what
actually writes to the instruments.  Keeping it that way means a ramp cannot
bypass the zero-lock, and a zero command issued mid-ramp still wins.

Every ramp checks a guard before each step and again before the final snap,
so cancelling one is immediate at step granularity rather than having to wait
out the remaining interval.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class RampLeg:
    """One controller's journey during a ramp."""

    unit: str
    start: float
    target: float

    def at(self, fraction):
        return self.start + (self.target - self.start) * fraction


class RampRunner:
    """Runs named ramps on daemon threads, one at a time per name."""

    def __init__(self, emit, log=None, sleep=None):
        self._emit = emit
        self._log = log if log is not None else (lambda _message: None)
        self._sleep = sleep if sleep is not None else time.sleep
        self._lock = threading.Lock()
        self._generation: dict[str, int] = {}
        #: Name to the generation token of the thread currently running under
        #: it.  A dict rather than a set because a replaced ramp is still awake
        #: until its sleep ends, and the thread taking over has to be able to
        #: tell "I am the current one" from "I was superseded".
        self._running: dict[str, int] = {}

    # -- state ------------------------------------------------------------ #

    def is_active(self, name):
        with self._lock:
            return name in self._running

    @property
    def active(self):
        with self._lock:
            return set(self._running)

    def cancel(self, name):
        """Stop ``name`` at its next step boundary.

        The name stops counting as active immediately, before the thread has
        woken up to notice: it will emit nothing further, and a caller asking
        "is a ramp in force on this line" is owed the answer no.
        """
        with self._lock:
            self._generation[name] = self._generation.get(name, 0) + 1
            self._running.pop(name, None)

    def cancel_all(self):
        with self._lock:
            for name in list(self._generation) + list(self._running):
                self._generation[name] = self._generation.get(name, 0) + 1
            # Nothing is current any more, so the name is free at once rather
            # than when each thread next wakes.  Otherwise an E-STOP followed by
            # a fresh setpoint on the same line would be refused for up to one
            # step interval, which is exactly when it must not be.
            self._running.clear()

    # -- running ---------------------------------------------------------- #

    def start(self, name, legs, steps, interval, *, guard=None,
              on_progress=None, on_done=None, label=None, replace=False):
        """Begin a ramp.  Returns False if one is already running under ``name``.

        ``guard`` is an additional predicate consulted before every step, used
        by the ignition sequence so that leaving the PRE_IGNITION state stops
        the ramp without anyone having to call :meth:`cancel`.

        ``replace`` supersedes a ramp already running under ``name`` instead of
        refusing: the running one is cancelled and this one takes over in the
        same breath, because an operator giving a line a new setpoint while it
        is still moving means the new one, not "ignore me, I'm busy".  The
        cancelled thread may still be asleep, so the takeover is recorded by
        generation token rather than by the name being free.
        """
        legs = [leg for leg in legs if leg.unit]
        if not legs:
            return False
        with self._lock:
            if name in self._running:
                if not replace:
                    return False
                self._generation[name] = self._generation.get(name, 0) + 1
            token = self._generation.get(name, 0)
            self._running[name] = token
        thread = threading.Thread(
            target=self._run,
            args=(name, token, legs, steps, interval, guard, on_progress,
                  on_done, label or name),
            daemon=True, name=f"ramp-{name}")
        thread.start()
        return True

    def _still_wanted(self, name, token, guard):
        with self._lock:
            if self._generation.get(name, 0) != token:
                return False
        return guard is None or bool(guard())

    def _run(self, name, token, legs, steps, interval, guard, on_progress,
             on_done, label):
        completed = False
        try:
            for step in range(1, steps + 1):
                if not self._still_wanted(name, token, guard):
                    break
                fraction = step / steps
                for leg in legs:
                    self._emit(leg.unit, leg.at(fraction))
                if on_progress is not None:
                    on_progress(int(fraction * 100))
                self._sleep(interval)
            if self._still_wanted(name, token, guard):
                # Snap to the exact targets.  Accumulated floating-point error
                # over the interpolation is small, but a flow left 0.4% shy of
                # its commanded value is a phi that does not match the log.
                for leg in legs:
                    self._emit(leg.unit, leg.target)
                if on_progress is not None:
                    on_progress(100)
                completed = True
                self._log(f"{label} ramp complete. ✓")
            else:
                if on_progress is not None:
                    on_progress(0)
                self._log(f"{label} ramp stopped.")
        except Exception as exc:
            self._log(f"Ramp error ({label}): {type(exc).__name__}: {exc}")
        finally:
            with self._lock:
                # Only if this thread is still the one running under the name.
                # A superseded ramp that clears the entry would leave the ramp
                # that replaced it invisible to :meth:`is_active`.
                if self._running.get(name) == token:
                    del self._running[name]
            if on_done is not None:
                try:
                    on_done(completed)
                except Exception:
                    pass


def scaled_targets(targets, fuel_keys, fuel_scale, air_scale):
    """Pre-ignition targets: fuels at ``fuel_scale``, everything else at ``air_scale``."""
    return {
        key: value * (fuel_scale if key in fuel_keys else air_scale)
        for key, value in targets.items()
    }
