"""The rig session: everything the operator's actions actually do.

This is the whole control layer of the application with no widgets in it.
Scanning, connecting, the polling loop, setpoints, the verified zero, ramps,
the ignition sequence, CSV logging and the LabVIEW listener all live here;
a view's job is to render the signals below and call the methods below.

It is the one deliberate Qt dependency outside ``ui``.  Serial work happens
on :class:`SerialIOWorker`'s loop and on ramp threads, and every result has
to reach the GUI thread.  Qt already delivers a signal emitted off-thread by
queueing it onto the receiver's thread, which is exactly the hand-rolled
callback queue this replaces -- so the session is a ``QObject`` and results
travel as signals.

Two rules hold throughout, because they are what makes the rig safe:

* A zero command outranks everything.  It purges pending setpoints for the
  units it targets (and only those), and while it is outstanding no nonzero
  setpoint for a locked unit is written, whoever asks.
* Nothing writes to hardware except the monitor loop.  Ramps and the ignition
  sequence enqueue setpoints like anything else, so they cannot slip past the
  zero lock.
"""

from __future__ import annotations

import asyncio
import math
import time
from datetime import datetime
from pathlib import Path
from queue import Queue
import threading

import serial
import serial.tools.list_ports
from alicat import FlowController, FlowMeter
from PySide6.QtCore import QObject, QTimer, Qt, Signal

from ..domain import combustion, roles
from ..domain.assignments import assess_autocalc
from ..domain.combustion import CombustionCalculator
from ..domain.safety import ZeroRequest, select_zero_units
from ..infrastructure.alicat_protocol import AlicatProtocol
from ..infrastructure.serial_worker import SerialIOWorker
from ..services.discovery import DiscoveryService
from .combustion_prefs import SCOPE_ALL, SCOPE_STAGE1, SCOPE_STAGE2
from .csv_logger import CsvLogger, resolve_path
from .mexa_controller import MexaController
from .graph_history import GraphHistory
from .agent_read_model import build_snapshot, derive_state, windowed_history
from .experiment_plan_controller import ExperimentPlanController
from .ramps import RampLeg, RampRunner
from .sequence import (DEADBAND_FLOOR, DEADBAND_FRACTION, SETTLE_TOLERANCE,
                       TICK_S, Sequence, SequencePlayer, SequenceRecorder,
                       SettleGate, TrackMeta)
from .telemetry import TelemetryReader, blank_sample
from . import combustion_prefs, unit_prefs
from .udp_listener import UdpCommandListener

#: Addresses probed by a scan.  Alicat units are single letters.
SCAN_UNITS = tuple(chr(code) for code in range(ord('A'), ord('Z') + 1))

#: How long the driver waits before deciding an address is unused.  There are
#: no other per-unit sleeps in a scan, so this alone sets its duration.
SCAN_RESPONSE_TIMEOUT_S = 0.15

#: Consecutive read timeouts on one unit before the port is assumed wedged
#: and the monitor is restarted rather than left silently stale.
MAX_TIMEOUTS = 10

#: Windows USB-serial drivers can briefly retain an exclusive COM handle after
#: it is closed.  Retry only that transient open failure; protocol and device
#: errors should still fail immediately.
CONNECT_OPEN_ATTEMPTS = 3
CONNECT_OPEN_RETRY_S = 0.15


def _is_access_denied(error):
    """Whether an exception chain represents a transient Windows port lock."""
    seen = set()
    current = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if (isinstance(current, PermissionError)
                or getattr(current, 'winerror', None) == 5
                or getattr(current, 'errno', None) == 13
                or 'access is denied' in str(current).casefold()):
            return True
        current = current.__cause__ or current.__context__
    return False

#: The application's own serial baud.  Selecting it does not reconfigure the
#: instruments, so every device on the port must already be set to match; the
#: rig this build drives runs at 57,600 rather than Alicat's 19,200 default.
DEFAULT_BAUDRATE = 57600
DEFAULT_LOG_NAME = "flow_log.csv"

#: How long a line that must never be stepped takes to reach a hand-typed
#: setpoint when no rate has been declared for it.  This is the 20 x 0.5 s ramp
#: the pilot and air lines have always been given, expressed as the duration it
#: actually was, so a declared rate can be compared against it.
MANUAL_RAMP_S = 10.0

#: Wall time between steps of a paced setpoint move.  The monitor writes at
#: roughly this cadence anyway, so a finer grain would only queue setpoints the
#: loop coalesces.
RAMP_STEP_S = 0.5

#: Most steps one paced move may be broken into.  A very slow rate over a long
#: journey would otherwise schedule thousands of them.
MAX_RAMP_STEPS = 600

#: The two ways the rig is run.  ``staged`` is the RQL burner -- auto-calc,
#: the ignition sequence and equivalence ratios all mean something.
#: ``standard`` is the same hardware used as plain flow control, where those
#: are noise.  The mode lives here rather than in a widget so that the views
#: and the recorder cannot disagree about which rig they are driving.
MODE_STANDARD = "standard"
MODE_STAGED = "staged"

#: Where ``stop_recording`` puts a run when the operator has not chosen a
#: name.  A recording that is only in memory is one power cut from gone.
DEFAULT_SEQUENCE_DIR = Path.home() / "Documents" / "Flow Controller" / "sequences"

#: Where an acquisition log lands when the operator has not said otherwise.
#: Named here rather than in the view because a LabVIEW datagram can open one
#: with nobody at the keyboard, and the listener lives on this side.
DEFAULT_LOG_DIR = Path.home() / "Documents" / "Flow Controller"

SEQ_IDLE = "idle"
SEQ_RECORDING = "recording"
SEQ_REPLAYING = "replaying"


class FlowSession(QObject):
    """Owns the hardware, the control state, and the rules between them."""

    # -- narration -------------------------------------------------------- #
    logged = Signal(str, str)               # channel ('system'|'connection'), text
    failed = Signal(str, str)               # title, detail -- view decides how to show
    banner = Signal(str, str)               # text, kind

    # -- discovery -------------------------------------------------------- #
    ports_changed = Signal(list)
    scan_started = Signal()
    scan_progress = Signal(str, int, int)   # text, done, total
    scan_controller = Signal(object)        # ControllerInfo
    scan_finished = Signal(object)          # DiscoveryResult

    # -- connection ------------------------------------------------------- #
    connecting_changed = Signal(bool)
    connection_changed = Signal(bool)
    autocalc_changed = Signal(bool, object)  # available, config
    assignments_changed = Signal(dict)      # role key -> unit, after a live edit
    full_scale_changed = Signal(object, object)  # unit, SLPM or None for auto
    unit_ramp_changed = Signal(object, object)   # unit, SLPM/s or None for none
    max_flow_changed = Signal(object, object)    # unit, SLPM or None for none

    # -- monitoring ------------------------------------------------------- #
    monitoring_changed = Signal(bool)
    monitor_stopped = Signal(str, bool)     # message, was a reconnect attempt
    poll_rate = Signal(float, float)        # hz, ms per pass
    samples_updated = Signal(int)           # generation
    restart_status = Signal(str, str)       # text, kind
    reconnect_finished = Signal(int)
    communication_fault = Signal(str)       # uncertain read/write transport state

    # -- safety ----------------------------------------------------------- #
    estop_armed_changed = Signal(bool)
    zero_started = Signal(object)           # ZeroRequest
    zero_finished = Signal(object, dict, dict)

    # -- sequencing ------------------------------------------------------- #
    ramp_progress = Signal(str, int)        # role key, percent
    ignition_changed = Signal(str)
    targets_changed = Signal(dict)
    mode_changed = Signal(str)              # MODE_STANDARD | MODE_STAGED
    #: The live combustion estimate's settings changed -- an inlet bore,
    #: or whether and how often it is computed at all.
    combustion_changed = Signal(dict)

    # -- recorded sequences ----------------------------------------------- #
    sequence_state_changed = Signal(str)    # SEQ_IDLE | SEQ_RECORDING | SEQ_REPLAYING
    sequence_progress = Signal(float, float)  # position s, duration s (0 = open)
    sequence_keyframe_added = Signal(float)   # t of an operator-placed marker
    sequence_changed = Signal(object)       # the loaded Sequence, or None
    sequence_saved = Signal(object)         # Path
    sequence_cycle = Signal(int, int)       # pass now running, of how many (0 = endless)
    sequence_hold = Signal(bool, str)       # replay clock held, why
    sequence_ended = Signal(bool, str)      # completed normally, reason

    # -- recording -------------------------------------------------------- #
    logging_changed = Signal(bool, object)  # active, path
    udp_changed = Signal(bool, str)

    #: Internal: carries a callable from a worker thread to this thread.
    _invoke = Signal(object)

    def __init__(self, parent=None, *, worker=None):
        super().__init__(parent)
        self._invoke.connect(self._run_invoked, Qt.ConnectionType.QueuedConnection)

        self._worker = worker or SerialIOWorker()
        self._protocol = AlicatProtocol(self._log_conn)
        self._discovery = DiscoveryService(self._protocol)
        self._telemetry = TelemetryReader(log=self._log)
        self.calc = CombustionCalculator()
        self.history = GraphHistory()
        self._csv = CsvLogger()
        self.mexa = MexaController(self)
        self._csv_lock = threading.Lock()
        self._ramps = RampRunner(self._emit_ramp_setpoint, log=self._log)
        self._udp = UdpCommandListener(
            on_command=lambda command: self._post(self._on_udp_command, command),
            on_ready=lambda host, port: self._post(self._on_udp_ready, host, port),
            on_error=lambda error, host, port: self._post(
                self._on_udp_error, error, host, port),
            on_ignored=lambda text, sender: self._post(
                self._log, f"LabVIEW: ignored '{text}' from {sender[0]}."))

        # -- connection state --
        self.port = None
        self.baudrate = DEFAULT_BAUDRATE
        self.controllers_connected = False
        self.is_connecting = False
        self.controller_instances = {}
        self._connection_future = None
        self._scan_cancelled = False
        #: The last scan that produced an answer.  Kept on the session rather
        #: than only in the view because a view can be rebuilt — re-theming
        #: replaces every widget in the window — and an operator should not
        #: have to scan the bus again to get their rows back.
        self.last_scan = None

        # -- assignment state --
        self.selection = {}                  # unit -> (gas, zone)
        #: ``{unit: {'full_scale': SLPM, 'ramp': SLPM/s, 'max_flow': SLPM}}``
        #: as declared by the
        #: operator -- the devices report neither.  Loaded from disk because
        #: both describe the meter and its line rather than the run, so they
        #: should outlive the session that typed them.  A unit missing a full
        #: scale is scaled from the run instead; one missing a ramp rate has
        #: its setpoints written straight out.
        self.unit_prefs = unit_prefs.load()
        #: Inlet bores and the pacing of the live combustion estimate.
        #: Loaded from disk for the same reason as the per-unit figures:
        #: a burner's inlet is the same diameter tomorrow morning, and
        #: an operator who turned the estimate down on a slow laptop
        #: should not have to turn it down again.
        self.combustion_prefs = combustion_prefs.load()
        self.assignments = {key: None for key, _label in roles.ROLES}
        self.custom_assignments = {}
        self.autocalc_available = True
        self.autocalc_config = None
        # The last complete condition used by Auto-Calculate. Keeping the
        # inputs beside the targets lets an agent change one field (for
        # example phi_stage1) without guessing the remaining condition.
        self.autocalc_request = None

        # -- monitoring state --
        self.is_monitoring = False
        self.poll_interval_s = 0.0
        self._monitor_future = None
        self._monitor_port = None
        self._monitor_baudrate = None
        self._restart_pending = False
        self._restart_reason = None
        self._reconnect_active = False
        self._generation = 0
        self._live_samples = {}
        self._latest_samples = {}
        self._latest_timestamp = None

        # -- command queues --
        self.setpoint_queue = Queue()
        self._zero_request_queue = Queue()
        self._last_sp = {}

        # -- safety state --
        self._zero_locked_units = set()
        # A plan watchdog keeps its targets locked after the monitor confirms
        # zero, until the GUI thread has stopped the replay and marked the plan
        # aborted. This prevents queued GUI timer work from racing the abort
        # when the event loop recovers from a stall.
        self._watchdog_locked_units = set()
        self._zero_action_active = False
        self._active_zero_request = None
        self._emergency_stop_active = False
        self._estop_armed = False

        # -- sequencing state --
        self.target_flows = {}
        self.ignition_state = "IDLE"
        self.pre_fuel_scale = 1.0
        self.pre_air_scale = 1.0
        # Plain multi-channel control is the safe, least-assumptive startup
        # surface.  RQL controls are exposed only after the operator explicitly
        # selects Staged mode; a complete role assignment must not opt them in.
        self.operating_mode = MODE_STANDARD

        # -- logging destination --
        #: What a log started without a dialog should be called.  The operator
        #: types it on the operation tab, which keeps these in step; they are
        #: held here because a LabVIEW ``log`` datagram has to resolve a path
        #: on its own, and the field that holds the answer belongs to a view
        #: that a re-theme throws away.
        self.log_destination = ''
        self.log_dir = DEFAULT_LOG_DIR

        # -- recorded sequences --
        self.sequence_dir = DEFAULT_SEQUENCE_DIR
        self.sequence = None                 # the loaded / last recorded one
        self._recorder = SequenceRecorder()
        self._player = None
        self._sequence_state = SEQ_IDLE
        #: unit -> track key, frozen while recording so a setpoint issued from
        #: a ramp thread does not have to walk the assignment map.
        self._record_keys = {}
        self._replay_started_at = 0.0
        self._replay_last_tick = 0.0
        #: Time the clock has been held inside the pass now running.  Position
        #: is ``now - started_at - held``, so holding is a matter of growing
        #: this rather than of stopping the timer: the tick has to keep running
        #: or the lines being rate-limited towards their setpoints would never
        #: get there, and the hold would never end.
        self._replay_held_s = 0.0
        self._settle_enabled = True
        self._settle_tolerance = SETTLE_TOLERANCE
        self._settle = SettleGate()
        self._replay_timer = QTimer(self)
        self._replay_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._replay_timer.setInterval(max(20, int(TICK_S * 1000)))
        self._replay_timer.timeout.connect(self._sequence_tick)
        self.experiment_plans = ExperimentPlanController(self, self)

    # ==================================================================== #
    #  Thread marshalling and narration                                    #
    # ==================================================================== #

    def _run_invoked(self, call):
        call()

    def _post(self, function, *args):
        """Run ``function(*args)`` on the thread that owns this session."""
        self._invoke.emit(lambda: function(*args))

    def _submit(self, coroutine, callback=None):
        """Run ``coroutine`` on the serial loop; ``callback`` lands back here."""
        future = self._worker.submit(coroutine)
        if callback is not None:
            future.add_done_callback(
                lambda done: self._post(callback, done))
        return future

    def _log(self, message):
        self.logged.emit('system', message)

    def _log_conn(self, message):
        self.logged.emit('connection', message)

    # ==================================================================== #
    #  Ports and scanning                                                  #
    # ==================================================================== #

    def refresh_ports(self):
        ports = [port.device for port in serial.tools.list_ports.comports()]
        self.ports_changed.emit(ports)
        return ports

    def start_scan(self, port, baudrate=None):
        """Probe every unit address on ``port``.  Refuses while in use."""
        if (self.controllers_connected or self.is_monitoring
                or self.is_connecting or self._emergency_stop_active):
            self.failed.emit(
                "Scan unavailable",
                "Disconnect the controllers and stop monitoring before scanning.")
            return False
        if not port:
            self.failed.emit("Scan", "Select a COM port first.")
            return False
        self.port = port
        if baudrate is not None:
            self.baudrate = baudrate
        self._scan_cancelled = False
        self.scan_started.emit()
        self._log_conn(
            f"Scanning {port} at {self.baudrate} baud for units "
            f"{SCAN_UNITS[0]}–{SCAN_UNITS[-1]}…")
        try:
            self._submit(self._scan_async(port, self.baudrate), self._finish_scan)
        except Exception as exc:
            self.scan_finished.emit(None)
            self.failed.emit("Scan", str(exc))
            return False
        return True

    def cancel_scan(self):
        self._scan_cancelled = True

    async def _scan_async(self, port, baudrate):
        return await self._discovery.scan(
            port, baudrate, SCAN_UNITS, SCAN_RESPONSE_TIMEOUT_S,
            should_continue=lambda: not self._scan_cancelled,
            on_progress=lambda index, unit: self.scan_progress.emit(
                f"Probing unit {unit} ({index}/{len(SCAN_UNITS)})…",
                index, len(SCAN_UNITS)),
            on_controller=lambda controller: self._post(
                self._on_scan_controller, controller),
            on_gas_progress=lambda index, total, controller:
                self.scan_progress.emit(
                    f"Reading gas table {index}/{total} (Unit {controller.unit})…",
                    index, total))

    def _on_scan_controller(self, controller):
        data = controller.data or {}
        gas = str(data.get('gas', '?'))
        flow = data.get('mass_flow', 0.0) or 0.0
        pressure = data.get('pressure', 0.0) or 0.0
        self._log_conn(
            f"OK  {controller.unit}: {gas:<14} Flow={flow:>7.2f} SLPM  "
            f"P={pressure:>6.2f} psia")
        self.scan_controller.emit(controller)

    def _finish_scan(self, future):
        try:
            result = future.result()
        except Exception as exc:
            self._log_conn(f"Scan failed: {type(exc).__name__}: {exc}")
            self.scan_finished.emit(None)
            self.failed.emit("Scan failed", str(exc))
            return
        if result.error:
            self._log_conn(f"Scan error: {result.error}")
        self._log_conn(f"Scan complete: {len(result.controllers)} controller(s) found.")
        # Only a scan that answered replaces the remembered one: a scan that
        # threw leaves the rows already on screen alone, so the remembered
        # result has to keep matching them.
        self.last_scan = result
        self.scan_finished.emit(result)

    def query_gas_table(self, unit, callback, port=None):
        """Re-read one controller's gas table, off the GUI thread.

        A scan already caches every table it finds on the ``ControllerInfo``,
        so this exists for the case where that phase failed or the device has
        since been reprogrammed.  It refuses while the monitor is running: the
        query opens the port for itself, and two owners on one serial line
        corrupt both conversations.
        """
        if self.is_monitoring:
            self.failed.emit(
                "Gas table",
                "Stop live monitoring before querying the controller gas table.")
            return False
        port = port or self.port
        if not port:
            self.failed.emit("Gas table", "Select a COM port first.")
            return False
        baudrate = self.baudrate

        async def read():
            return self._protocol.query_gases(port, unit, baudrate)

        try:
            self._submit(read(), callback)
        except Exception as exc:
            self.failed.emit("Gas table", str(exc))
            return False
        return True

    # ==================================================================== #
    #  Assignment                                                          #
    # ==================================================================== #

    def set_selection(self, selection):
        """Replace the whole ``{unit: (gas, zone)}`` map."""
        self.selection = dict(selection)
        self._rebuild_assignments()

    def pref_for(self, unit, field):
        """One declared per-unit figure, or ``None`` if it was never declared."""
        return self.unit_prefs.get(unit, {}).get(field)

    def _set_pref(self, unit, field, value, describe):
        """Store one per-unit figure and persist the lot.  Returns the value.

        ``describe(cleaned)`` narrates the change for the log; it is called only
        when something actually changed, so re-typing the same number is quiet.
        """
        cleaned = unit_prefs.clean_field(field, value)
        record = self.unit_prefs.get(unit, {})
        if cleaned is None:
            if record.pop(field, None) is None:
                return None
            if not record:
                self.unit_prefs.pop(unit, None)
        else:
            if record.get(field) == cleaned:
                return cleaned
            record[field] = cleaned
            self.unit_prefs[unit] = record
        self._log(describe(cleaned))
        error = unit_prefs.save(self.unit_prefs)
        if error:
            # Worth saying, not worth stopping for -- the figure is in force
            # for this session either way.
            self._log(f"Could not save the per-controller settings: {error}")
        return cleaned

    def full_scale_for(self, unit):
        """The declared full scale of one controller, or ``None``."""
        return self.pref_for(unit, 'full_scale')

    def set_full_scale(self, unit, value):
        """Declare -- or with ``None``/0 withdraw -- one controller's full scale.

        Nothing is written to the hardware: this is the span of the tracking
        bar and of nothing else, so it is safe to change mid-run, and a wrong
        entry costs a redraw rather than a transient on a burner line.  It is
        still saved immediately, because the whole point of the figure is that
        it does not have to be typed again next time.
        """
        cleaned = self._set_pref(
            unit, 'full_scale', value,
            lambda scale: (f"Unit {unit}: bar full scale set to {scale:g} SLPM."
                           if scale is not None else
                           f"Unit {unit}: bar full scale back to automatic."))
        self.full_scale_changed.emit(unit, cleaned)
        return cleaned

    def max_flow_for(self, unit):
        """The declared command ceiling for one controller, or ``None``."""
        return self.pref_for(unit, 'max_flow')

    def set_max_flow(self, unit, value):
        """Declare -- or with ``None``/0 withdraw -- one command ceiling.

        This is deliberately separate from the meter full scale.  A full scale
        only changes a tracking bar; this value rejects any higher command at
        the session boundary, regardless of whether it came from a card,
        replay, ignition sequence, or a future automation interface.
        """
        cleaned = self._set_pref(
            unit, 'max_flow', value,
            lambda maximum: (f"Unit {unit}: command ceiling set to "
                             f"{maximum:g} SLPM."
                             if maximum is not None else
                             f"Unit {unit}: command ceiling cleared."))
        self.max_flow_changed.emit(unit, cleaned)
        previous = self._last_sp.get(unit, 0.0)
        if (cleaned is not None and isinstance(previous, (int, float))
                and math.isfinite(float(previous)) and float(previous) > cleaned):
            # A newly lowered hard limit must not grandfather an already-live
            # higher command.  Use the application's established verified
            # ZERO ALL path: a broad safe shutdown is preferable to inventing
            # an unreviewed capped setpoint or leaving the rig over its limit.
            self._last_sp[unit] = 0.0
            self._log(
                f"Unit {unit}: MAX FLOW was lowered below the last commanded "
                "setpoint; requesting a verified zero for that controller.")
            if self.controllers_connected:
                self._request_zero_units(
                    (str(unit),), scope="limit",
                    scope_label=f"UNIT {unit} LIMIT")
        return cleaned

    def _setpoint_limit_error(self, unit, setpoint):
        """Return why a normal command is unsafe *at this instant*.

        This check is deliberately repeated immediately before every hardware
        write.  A queued command can outlive the preference value under which
        it was accepted, so enqueue-time validation alone is not a safety
        boundary.
        """
        if isinstance(setpoint, bool):
            return "invalid setpoint"
        try:
            value = float(setpoint)
        except (TypeError, ValueError):
            return "invalid setpoint"
        if not math.isfinite(value) or value < 0.0:
            return "invalid setpoint"
        maximum = self.max_flow_for(unit)
        if maximum is not None and value > maximum:
            return f"command ceiling is {maximum:.3f} SLPM"
        return None

    def ramp_rate_for(self, unit):
        """How fast this controller is allowed to move, SLPM/s, or ``None``."""
        return self.pref_for(unit, 'ramp')

    def set_ramp_rate(self, unit, value):
        """Declare -- or with ``None``/0 withdraw -- one controller's ramp rate.

        Unlike the full scale this does reach the hardware, but only by pacing
        setpoints the operator asked for anyway: it never changes where a line
        ends up, only how quickly it gets there.  Changing it mid-run applies
        to the next setpoint rather than to a ramp already in flight, which is
        the conservative reading -- a ramp that changed rate underneath itself
        would leave the log describing a journey the rig did not make.
        """
        cleaned = self._set_pref(
            unit, 'ramp', value,
            lambda rate: (f"Unit {unit}: setpoints ramped at {rate:g} SLPM/s."
                          if rate is not None else
                          f"Unit {unit}: setpoints written without a ramp."))
        self.unit_ramp_changed.emit(unit, cleaned)
        return cleaned

    def ramp_disabled_for(self, unit):
        """Whether application ramping is off; new controllers default off."""
        return bool(self.unit_prefs.get(unit, {}).get('ramp_off', True))

    def set_ramp_disabled(self, unit, off):
        """Turn this controller's ramping off outright, or back on.

        This is a bigger thing than clearing the rate.  No rate means "no pace
        of your own", and the pilot and the two air lines are still walked over
        :data:`MANUAL_RAMP_S` because a step edge on those lines is a flame
        risk rather than a matter of taste.  Off means off: every setpoint
        written to this unit goes out in one write, on any line, including
        those.  It is stored per unit and remembered between runs like the
        other declarations, so it is logged loudly enough to be found later.
        """
        cleaned = self._set_pref(
            unit, 'ramp_off', off,
            lambda flag: (f"Unit {unit}: RAMPING OFF -- setpoints are written "
                          f"straight out, with no rate and no minimum move "
                          f"time, on whatever line this unit is driving."
                          if flag else
                          f"Unit {unit}: ramping back on."))
        # The rate in force is what the rest of the application cares about,
        # and while ramping is off that is nothing, whatever figure is stored.
        self.unit_ramp_changed.emit(unit, self.effective_ramp_rate(unit))
        return bool(cleaned)

    def effective_ramp_rate(self, unit):
        """The rate actually pacing this unit: ``None`` while ramping is off."""
        if self.ramp_disabled_for(unit):
            return None
        return self.ramp_rate_for(unit)

    def ramp_seconds_for(self, unit, key, journey):
        """How long a move of ``journey`` SLPM on this line should take.

        A declared rate sets the pace.  On a line that must never see a step
        edge (:data:`roles.RAMP_KEYS`) the move also cannot be quicker than
        :data:`MANUAL_RAMP_S`, so the operator may ask for something gentler
        than the protective default but not for something sharper, and not for
        nothing at all.  ``key`` is the role being written rather than the unit,
        because it is the *line* that must not be stepped -- a meter moved onto
        a pilot inherits the protection with the job.

        Zero means "write it straight out", which is what an undeclared rate on
        an ordinary line has always done -- and what ramping turned off means
        on any line at all, protected or not.
        """
        # Checked before the floor below rather than after it: turning ramping
        # off is the one way to defeat the protection, which is why it is a
        # deliberate per-unit declaration and not a rate of zero.
        if self.ramp_disabled_for(unit):
            return 0.0
        seconds = 0.0
        rate = self.ramp_rate_for(unit)
        if rate:
            seconds = abs(float(journey)) / float(rate)
        if key in roles.RAMP_KEYS:
            seconds = max(seconds, MANUAL_RAMP_S)
        return seconds

    def _rebuild_assignments(self):
        self.assignments, self.custom_assignments = roles.build_assignments(
            self.selection)

    def assigned_units(self):
        units = {unit for unit in self.assignments.values() if unit}
        units.update(self.custom_assignments)
        return sorted(units)

    def check_autocalc(self):
        """``(config, problems)`` for the current assignment."""
        pairs = [(gas, zone) for gas, zone in self.selection.values()
                 if gas not in (roles.UNSELECTED_GAS, '', None)
                 and zone != roles.UNASSIGNED_ZONE]
        return assess_autocalc(pairs)

    def set_zone(self, unit, zone):
        """Move one controller between zones without dropping the connection.

        A zone is a software grouping: it decides which stage a controller's
        flow is counted in, not anything programmed into the device.  A *gas*
        is programmed into the device, which is why gas stays frozen after
        connecting and this does not.

        The refusals are the interesting part.  A zone change rewrites the
        role map, so it must not happen while anything downstream has already
        committed to the old one: the CSV header is baked at ``start_logging``,
        the ignition ramps are addressed by role, and a recording's tracks are
        keyed by role.  While connected the unit may move anywhere except out
        of the assignment altogether -- the monitor loop already holds that
        controller open, and dropping it would leave the loop polling a unit
        that no longer has a column to write into.
        """
        if unit not in self.selection:
            return False
        gas, current = self.selection[unit]
        zone = str(zone)
        if zone == current:
            return True
        if zone not in roles.ZONE_OPTIONS:
            self.failed.emit("Zone", f"'{zone}' is not a zone.")
            return False
        if self._csv.active:
            self.failed.emit(
                "Zone",
                "Stop CSV logging before changing zones — the column headings "
                "were written from the assignment in force when logging started.")
            return False
        if self.ignition_state != "IDLE":
            self.failed.emit(
                "Zone",
                "Zones cannot be reassigned during the ignition sequence.")
            return False
        if self._zero_action_active:
            self.failed.emit("Zone", "Wait for the zero-flow command to finish.")
            return False
        if self._sequence_state != SEQ_IDLE:
            self.failed.emit(
                "Zone",
                "Stop the sequence first — its tracks follow the assignment "
                "that was in force when it started.")
            return False
        if self.controllers_connected and zone == roles.UNASSIGNED_ZONE:
            self.failed.emit(
                "Zone",
                f"Unit {unit} is connected and being polled. Disconnect before "
                "removing it from the assignment.")
            return False

        self.selection[unit] = (gas, zone)
        self._rebuild_assignments()
        config, problems = self.check_autocalc()
        self.autocalc_available = not problems
        self.autocalc_config = config
        # Targets are keyed by role; a role that just lost its unit has no
        # target any more, and leaving a stale one would arm ignition against
        # a controller that is no longer there.
        pruned = {key: value for key, value in self.target_flows.items()
                  if self.assignments.get(key)}
        if pruned != self.target_flows:
            self.target_flows = pruned
            self.targets_changed.emit(dict(self.target_flows))
        self._log(f"Unit {unit} ({gas}) reassigned: {current} → {zone}.")
        self.autocalc_changed.emit(self.autocalc_available, config)
        self.assignments_changed.emit(dict(self.assignments))
        return True

    # ==================================================================== #
    #  Operating mode                                                      #
    # ==================================================================== #

    @property
    def is_staged(self):
        return self.operating_mode == MODE_STAGED

    def set_operating_mode(self, mode):
        """Switch between the staged burner and plain flow control."""
        mode = MODE_STAGED if mode == MODE_STAGED else MODE_STANDARD
        if mode == self.operating_mode:
            return False
        if self.ignition_state != "IDLE":
            self.failed.emit(
                "Operating mode",
                "The ignition sequence is running. Zero all flows first.")
            return False
        self.operating_mode = mode
        self._log(f"Operating mode: {'staged (RQL)' if mode == MODE_STAGED else 'standard'}.")
        self.mode_changed.emit(mode)
        return True

    # ==================================================================== #
    #  Connecting                                                          #
    # ==================================================================== #

    def connect_all(self, port=None, baudrate=None, *, accept_no_autocalc=False):
        """Program each assigned unit's gas and confirm it answers.

        Returns ``'needs_confirmation'`` when auto-calculation would be lost by
        this assignment and the caller has not already accepted that; the view
        asks the operator and calls again with ``accept_no_autocalc=True``.
        """
        if self.is_connecting:
            return False
        if self.is_monitoring or self._emergency_stop_active:
            self.failed.emit(
                "Connection unavailable",
                "Stop monitoring and wait for zero-flow work to finish first.")
            return False
        self._rebuild_assignments()
        config, problems = self.check_autocalc()
        if problems and not accept_no_autocalc:
            return 'needs_confirmation'
        self.autocalc_available = not problems
        self.autocalc_config = config
        self.autocalc_changed.emit(self.autocalc_available, config)

        port = port or self.port
        if not port:
            self.failed.emit("Connection", "Select a COM port first.")
            return False
        requested_baudrate = self.baudrate if baudrate is None else baudrate
        if (self.last_scan is None
                or (self.last_scan.port, self.last_scan.baudrate)
                != (port, requested_baudrate)):
            self.failed.emit(
                "Scan required",
                "Scan the selected COM port and baud rate before connecting. "
                "Gas-table indices belong to the meters found on that bus.")
            return False
        self.port = port
        if baudrate is not None:
            self.baudrate = baudrate

        units = self.assigned_units()
        if not units:
            self.failed.emit(
                "Connection",
                "Select and assign at least one detected controller first.")
            return False
        gas_map = {unit: gas for unit, (gas, _zone) in self.selection.items()
                   if gas not in (roles.UNSELECTED_GAS, '', None)}
        gas_indexes = self._cached_gas_indexes(gas_map)

        self.is_connecting = True
        self.controllers_connected = False
        self.connecting_changed.emit(True)
        self.connection_changed.emit(False)
        self._set_estop_armed(False)
        self._log_conn(
            f"Connecting to {len(units)} assigned unit(s) on {port} at "
            f"{self.baudrate} baud…")
        try:
            self._connection_future = self._submit(
                self._configure_async(
                    port, self.baudrate, units, gas_map, gas_indexes),
                self._finish_connect)
        except Exception as exc:
            self.is_connecting = False
            self.connecting_changed.emit(False)
            self.failed.emit("Connection failed", str(exc))
            return False
        return True

    def _cached_gas_indexes(self, gas_map):
        """Resolve selected gas names from the table already read by the scan."""
        scanned = {
            controller.unit: controller
            for controller in getattr(self.last_scan, 'controllers', ())
        }
        indexes = {}
        for unit, gas_name in gas_map.items():
            controller = scanned.get(unit)
            if controller is None:
                continue
            wanted = str(gas_name).strip().casefold()
            index = next(
                (index for index, name in controller.supported_gases.items()
                 if str(name).strip().casefold() == wanted),
                None)
            if index is not None:
                indexes[unit] = index
            elif controller.active_gas.casefold() == wanted:
                # Some firmware reports the active gas but omits the table.
                # It is already selected, so confirm it without guessing a
                # register from the driver's unrelated global gas list.
                indexes[unit] = None
        return indexes

    async def _configure_async(
            self, port, baudrate, units, gas_map, gas_indexes=None):
        """Program and confirm every unit through one shared serial handle."""
        units = tuple(units)
        gas_indexes = gas_indexes or {}
        configuration_errors = {
            unit: f'ValueError: No gas-table index is known for "{gas_map[unit]}" on Unit {unit}'
            for unit in units if gas_map.get(unit) and unit not in gas_indexes
        }
        units = tuple(unit for unit in units if unit not in configuration_errors)
        if not units:
            return {}, configuration_errors

        for attempt in range(1, CONNECT_OPEN_ATTEMPTS + 1):
            confirmed = {}
            errors = dict(configuration_errors)
            try:
                async with FlowMeter(
                        address=port, unit=units[0], baudrate=baudrate,
                        timeout=0.3) as meter:
                    for unit in units:
                        meter.unit = unit
                        meter.keys = [
                            'pressure', 'temperature', 'volumetric_flow',
                            'mass_flow', 'setpoint', 'gas',
                        ]
                        meter.hw.timeouts = 0
                        gas_name = gas_map.get(unit)
                        try:
                            if gas_name:
                                gas_index = gas_indexes[unit]
                                if gas_index is not None:
                                    await asyncio.wait_for(
                                        meter.set_gas(gas_index), timeout=3.0)
                            reading = await asyncio.wait_for(
                                meter.get(), timeout=2.0)

                            actual_gas = str(reading.get('gas', '')).strip()
                            if (gas_name and actual_gas.casefold()
                                    != str(gas_name).casefold()):
                                raise OSError(
                                    f"gas readback mismatch: requested {gas_name}, "
                                    f"got {actual_gas or 'unknown'}")
                            confirmed[unit] = reading
                        except Exception as exc:
                            if _is_access_denied(exc):
                                raise
                            errors[unit] = f"{type(exc).__name__}: {exc}"
                return confirmed, errors
            except Exception as exc:
                if not _is_access_denied(exc) or attempt == CONNECT_OPEN_ATTEMPTS:
                    return {}, {"serial port": f"{type(exc).__name__}: {exc}"}
                await asyncio.sleep(CONNECT_OPEN_RETRY_S * attempt)

        raise AssertionError("unreachable")

    def _finish_connect(self, future):
        self._connection_future = None
        self.is_connecting = False
        self.connecting_changed.emit(False)
        try:
            confirmed, errors = future.result()
        except Exception as exc:
            confirmed, errors = {}, {"connection": f"{type(exc).__name__}: {exc}"}

        for unit, reading in confirmed.items():
            gas = reading.get('gas', 'Unknown')
            self._log_conn(
                f"  Unit {unit}: communication and gas '{gas}' confirmed ✓")
        for unit, error in errors.items():
            self._log_conn(f"  Unit {unit}: connection failed — {error}")

        # Partial success is failure.  A run with one silent controller is a
        # run where one gas is unmetered, so nothing is declared connected.
        if errors or len(confirmed) != len(self.assigned_units()):
            self.controllers_connected = False
            self._set_estop_armed(False)
            self.connection_changed.emit(False)
            details = "\n".join(
                f"Unit {unit}: {error}" for unit, error in errors.items())
            self.failed.emit(
                "Connection failed",
                "Not every assigned controller could be confirmed.\n\n" + details)
            return

        self.controllers_connected = True
        self._set_estop_armed(True)
        self.connection_changed.emit(True)
        self._log_conn(f"All {len(confirmed)} selected controllers confirmed.")
        for key, unit in self.assignments.items():
            if unit:
                self._log_conn(f"  {key}: Unit {unit}")

    def disconnect_all(self):
        if self.is_monitoring:
            self.stop_monitoring()
        self.controllers_connected = False
        self.controller_instances.clear()
        self._set_estop_armed(False)
        self.connection_changed.emit(False)
        self._log_conn("Controllers disconnected.")

    # ==================================================================== #
    #  Monitoring lifecycle                                                #
    # ==================================================================== #

    def set_poll_interval(self, seconds):
        """Post-pass delay, applied live.  Raises ValueError if out of range."""
        value = float(seconds)
        if not 0.0 <= value <= 5.0:
            raise ValueError("Polling delay must be between 0 and 5000 ms.")
        self.poll_interval_s = value
        self._log(f"Polling pass delay set to {value * 1000:g} ms"
                  + (" (applied live)." if self.is_monitoring else "."))

    def toggle_monitoring(self, port=None):
        if self.is_monitoring:
            self.stop_monitoring()
        else:
            self.start_monitoring(port)

    def start_monitoring(self, port=None):
        if not self.controllers_connected:
            self.failed.emit("Monitor", "Connect controllers first.")
            return False
        if self.is_connecting or self._emergency_stop_active or self._zero_action_active:
            self.failed.emit(
                "Monitor", "Wait for the current serial operation to finish.")
            return False
        port = port or self.port
        if not port:
            self.failed.emit("Monitor", "Select a COM port first.")
            return False

        self._monitor_port = port
        self._monitor_baudrate = self.baudrate
        self.is_monitoring = True
        self._restart_pending = False

        units = self.assigned_units()
        self.history.set_units(units)
        self.history.clear(generation=self._generation)
        self._drain(self.setpoint_queue)
        self.monitoring_changed.emit(True)
        self._log(f"Monitoring started on {port} at {self._monitor_baudrate} baud "
                  f"with {self.poll_interval_s * 1000:g} ms pass delay.")
        self._log_conn("Live monitoring started.")

        if not self._start_monitor_task():
            self.is_monitoring = False
            self.monitoring_changed.emit(False)
            return False
        return True

    def stop_monitoring(self):
        self.is_monitoring = False
        self._restart_pending = False
        self._restart_reason = None
        self._ramps.cancel_all()
        self.stop_replay(reason="cancelled when the monitor stopped")
        if self._recorder.active:
            # Keep the partial run rather than discard it: the operator asked
            # to stop monitoring, not to throw away what they had recorded.
            self._log("Monitor stopped while recording — saving the run so far.")
            self.stop_recording()
        self.monitoring_changed.emit(False)
        self._log("Monitoring stopped.")
        self._log_conn("Live monitoring stopped.")

    def _start_monitor_task(self):
        if self._monitor_future is not None and not self._monitor_future.done():
            self._log("Monitor start deferred: previous serial task is still closing.")
            return False
        try:
            self._monitor_future = self._submit(
                self._monitor_async(), self._on_monitor_done)
            return True
        except Exception as exc:
            self._monitor_future = None
            self._log(f"ERROR: Could not start monitor: {exc}")
            self.failed.emit("Monitor", f"Could not start serial monitoring:\n{exc}")
            return False

    def _on_monitor_done(self, future):
        if future is not self._monitor_future:
            return
        self._monitor_future = None
        error = None
        try:
            future.result()
        except Exception as exc:
            error = exc
            self._log(
                f"Monitor serial task ended with an error: {type(exc).__name__}: {exc}")

        if self._emergency_stop_active:
            return
        if self._zero_action_active and self._active_zero_request is not None:
            # The zero was in flight when the port went away.  Reporting it as
            # unconfirmed is the only honest answer: nothing read back.
            request = self._active_zero_request
            self._finish_zero_request(request, {}, {
                unit: "live monitor closed before zero was confirmed"
                for unit in request.units})
        if self._restart_pending:
            self._begin_serial_restart()
            return
        if self.is_monitoring:
            reconnect_failed = self._reconnect_active
            self.is_monitoring = False
            self._reconnect_active = False
            self.monitoring_changed.emit(False)
            self._set_estop_armed(self.controllers_connected)
            if reconnect_failed:
                self.restart_status.emit("Reconnect failed", 'danger')
            message = ("The flow meters could not be reconnected."
                       if reconnect_failed
                       else "The serial monitor stopped unexpectedly.")
            if error is not None:
                message += f"\n\n{type(error).__name__}: {error}"
            self.monitor_stopped.emit(message, reconnect_failed)

    # -- restart ---------------------------------------------------------- #

    def restart_connection(self):
        """Operator-requested reopen of the serial port without disconnecting."""
        if not self.controllers_connected or self._emergency_stop_active:
            return False
        self._restart_pending = True
        self._restart_reason = "manual"
        self._reconnect_active = True
        self.restart_status.emit("Closing flow meters…", 'warn')
        if self.is_monitoring:
            self.is_monitoring = False
            self.monitoring_changed.emit(False)
        if self._monitor_future is None or self._monitor_future.done():
            self._begin_serial_restart()
        return True

    def _auto_restart_monitoring(self):
        if self._restart_pending:
            return
        self._restart_pending = True
        self._restart_reason = "auto"
        self._log("COM timeout limit reached — stopping monitor for auto-restart…")
        self.banner.emit("COM TIMEOUT — auto-restarting monitor…", 'warn')
        self.is_monitoring = False
        self.monitoring_changed.emit(False)
        if self._monitor_future is None or self._monitor_future.done():
            self._begin_serial_restart()

    def _begin_serial_restart(self):
        if not self._restart_pending or self._emergency_stop_active:
            return
        try:
            self._submit(self._flush_serial_async(), self._resume_after_restart)
        except Exception as exc:
            self._restart_pending = False
            self._reconnect_active = False
            self._set_estop_armed(self.controllers_connected)
            self._log(f"Connection restart failed: {exc}")
            self.failed.emit("Reconnect Flow Meters", str(exc))

    async def _flush_serial_async(self):
        port = self._monitor_port or self.port
        if not port:
            raise OSError("No COM port is selected")
        baudrate = self._monitor_baudrate or self.baudrate
        with serial.Serial(port, baudrate=baudrate, timeout=0.15) as connection:
            connection.reset_input_buffer()
            connection.reset_output_buffer()

    def _resume_after_restart(self, future):
        try:
            future.result()
        except Exception as exc:
            self._log(f"Serial buffer flush failed during restart: {exc}")
            was_manual = self._restart_reason == "manual"
            self._restart_pending = False
            self._restart_reason = None
            self._reconnect_active = False
            self._set_estop_armed(self.controllers_connected)
            self.restart_status.emit("Reconnect failed", 'danger')
            if was_manual:
                self.failed.emit(
                    "Reconnect Flow Meters",
                    f"Could not reopen the serial port:\n\n{exc}")
            return
        if not self._restart_pending or not self.controllers_connected:
            self._reconnect_active = False
            self._set_estop_armed(self.controllers_connected)
            return
        reason = self._restart_reason
        self._restart_pending = False
        self._restart_reason = None
        self.is_monitoring = True
        self.monitoring_changed.emit(True)
        if not self._start_monitor_task():
            self.is_monitoring = False
            self.monitoring_changed.emit(False)
            return
        if reason == "auto":
            self._log("Monitor auto-restarted successfully. ✓")
            self.banner.emit("Monitor auto-restarted after COM timeout", 'ok')
        else:
            self.restart_status.emit("Reopening flow meters…", 'warn')

    def _finish_reconnect_success(self, opened):
        if not self._reconnect_active:
            return
        self._reconnect_active = False
        self._set_estop_armed(self.controllers_connected)
        self.restart_status.emit(f"✓ {opened} flow meter(s) reconnected", 'ok')
        self._log(f"Flow meter reconnection confirmed on {opened} unit(s). ✓")
        self.reconnect_finished.emit(opened)

    # ==================================================================== #
    #  The polling loop                                                    #
    # ==================================================================== #

    async def _monitor_async(self):
        port = self._monitor_port or self.port
        baudrate = self._monitor_baudrate or self.baudrate
        controllers = {}
        timeout_counts = {}
        self._live_samples.clear()
        self._telemetry.reset()
        event_loop = asyncio.get_running_loop()
        previous_pass_start = None
        period_ema = None
        try:
            all_units = {}
            for key, unit in self.assignments.items():
                if unit:
                    all_units[unit] = key
            for unit, key in self.custom_assignments.items():
                all_units.setdefault(unit, key)

            for unit, key in all_units.items():
                if unit in controllers:
                    self._log(f"WARNING: Unit {unit} assigned to multiple roles "
                              f"(duplicate at '{key}'). Fix assignments.")
                    continue
                try:
                    controller = FlowController(
                        address=port, unit=unit, baudrate=baudrate)
                    await controller.__aenter__()
                    controllers[unit] = controller
                    timeout_counts[unit] = 0
                    self.controller_instances[unit] = controller
                    self._log(f"Opened  Unit {unit}  ({key})")
                except Exception as exc:
                    self._log(f"ERROR: Could not open {key} (Unit {unit}): {exc}  "
                              "Setpoints will be dropped.")
            self._log(f"Connections ready: {len(controllers)}/{len(all_units)} opened.")
            if not controllers:
                raise OSError("No flow meter connections could be opened")
            if self._reconnect_active and len(controllers) != len(all_units):
                raise OSError(
                    f"Reconnect opened {len(controllers)} of {len(all_units)} flow meters")

            await self._restore_setpoints(controllers)
            if self._reconnect_active:
                self._post(self._finish_reconnect_success, len(controllers))

            while self.is_monitoring:
                pass_started = event_loop.time()
                if previous_pass_start is not None:
                    measured = pass_started - previous_pass_start
                    period_ema = (measured if period_ema is None
                                  else 0.8 * period_ema + 0.2 * measured)
                    if period_ema > 0:
                        self.poll_rate.emit(1.0 / period_ema, period_ema * 1000.0)
                previous_pass_start = pass_started

                await self._service_zero_requests(controllers)
                await self._write_pending_setpoints(controllers, timeout_counts)

                # Pass-local blanks keep a failed read blank in the CSV, while
                # the live display keeps showing the last value that was real.
                pass_samples = {unit: blank_sample() for unit in controllers}
                good = set()
                for unit, controller in list(controllers.items()):
                    if not self._zero_request_queue.empty():
                        await self._service_zero_requests(controllers)
                    try:
                        sample = await self._telemetry.read_sample(controller, unit)
                        pass_samples[unit] = sample
                        good.add(unit)
                        timeout_counts[unit] = 0
                    except asyncio.TimeoutError:
                        timeout_counts[unit] = timeout_counts.get(unit, 0) + 1
                        self._log(f"Read timeout Unit {unit} "
                                  f"({timeout_counts[unit]}/{MAX_TIMEOUTS})")
                        self.communication_fault.emit(
                            f"read timeout on Unit {unit}")
                        if timeout_counts[unit] >= MAX_TIMEOUTS:
                            self._post(self._auto_restart_monitoring)
                            return
                    except Exception as exc:
                        self._log(f"Read error Unit {unit}: {type(exc).__name__}: {exc}")
                        self.communication_fault.emit(
                            f"read error on Unit {unit}: {type(exc).__name__}")

                timestamp = datetime.now()
                self._write_log_row(pass_samples, timestamp)
                self._post(self._publish_pass, timestamp, pass_samples, good)

                await self._wait_for_next_poll()
        finally:
            for controller in controllers.values():
                try:
                    await controller.__aexit__(None, None, None)
                except Exception:
                    pass
            self.controller_instances.clear()

    async def _restore_setpoints(self, controllers):
        """Re-apply each unit's last commanded setpoint and verify the readback.

        A reconnect that cannot confirm what it restored is failed outright:
        the alternative is a burner running at a setpoint nobody has seen.
        """
        restored = False
        failures = []
        for unit, controller in controllers.items():
            wanted = self._last_sp.get(unit, 0.0)
            limit_error = self._setpoint_limit_error(unit, wanted)
            if limit_error:
                self._log(
                    f"WARNING: Unit {unit} stored SP {wanted!r} will not be "
                    f"restored; {limit_error}. Restoring zero instead.")
                wanted = 0.0
                self._last_sp[unit] = 0.0
            try:
                await asyncio.wait_for(
                    controller.set_flow_rate(wanted), timeout=2.0)
                reading = await asyncio.wait_for(controller.get(), timeout=1.5)
                reported = float(reading['setpoint'])
                tolerance = max(0.001, abs(wanted) * 0.0001)
                if not math.isfinite(reported) or abs(reported - wanted) > tolerance:
                    raise OSError(
                        f"setpoint readback mismatch: requested {wanted}, "
                        f"reported {reported}")
                if wanted != 0.0:
                    restored = True
            except Exception as exc:
                failures.append(unit)
                self._log(f"WARNING: Unit {unit} initial setpoint was not confirmed: "
                          f"{type(exc).__name__}: {exc}")
        if failures:
            self._log("WARNING: Initial setpoints were not confirmed for Unit(s) "
                      + ", ".join(failures))
            if self._reconnect_active:
                raise OSError(
                    "Reconnect could not confirm restored setpoints for Unit(s) "
                    + ", ".join(failures))
        elif restored:
            self._log("Setpoints restored from last session and confirmed. ✓")
        else:
            self._log("All initial zero setpoints confirmed. ✓")

    async def _write_pending_setpoints(self, controllers, timeout_counts):
        pending = {}
        while not self.setpoint_queue.empty():
            try:
                unit, setpoint = self.setpoint_queue.get_nowait()
                pending[unit] = setpoint
            except Exception:
                break
        for unit, setpoint in pending.items():
            limit_error = self._setpoint_limit_error(unit, setpoint)
            if limit_error:
                self._log(
                    f"Unit {unit}: queued SP {setpoint!r} dropped before write; "
                    f"{limit_error}.")
                continue
            if (unit in self._zero_locked_units
                    or unit in self._watchdog_locked_units) and abs(setpoint) > 0.001:
                self._log(
                    f"Unit {unit}: nonzero setpoint blocked by active zero command.")
                continue
            if unit not in controllers:
                role = next((key for key, value in self.assignments.items()
                             if value == unit), "unknown")
                self._log(f"ERROR: Setpoint dropped — Unit {unit} ({role}) "
                          "has no open connection.")
                self.communication_fault.emit(
                    f"write target Unit {unit} has no open connection")
                continue
            try:
                await asyncio.wait_for(
                    controllers[unit].set_flow_rate(setpoint), timeout=2.0)
                timeout_counts[unit] = 0
                self._last_sp[unit] = setpoint
                self._log(f"Unit {unit}: SP → {setpoint:.3f} SLPM")
            except asyncio.TimeoutError:
                self._log(f"WARNING: Unit {unit} SP={setpoint:.3f} — write sent "
                          "but readback timed out (command likely applied).")
                self.communication_fault.emit(
                    f"setpoint write timeout on Unit {unit}; applied state is uncertain")
            except Exception as exc:
                self._log(f"Set-flow error Unit {unit}: {exc}")
                self.communication_fault.emit(
                    f"setpoint write error on Unit {unit}: {type(exc).__name__}")
            finally:
                await asyncio.sleep(0.05)

    async def _wait_for_next_poll(self):
        """Sleep the configured delay in slices, so a stop is noticed promptly."""
        remaining = self.poll_interval_s
        while self.is_monitoring and remaining > 0:
            chunk = min(0.05, remaining)
            await asyncio.sleep(chunk)
            remaining -= chunk

    def _publish_pass(self, timestamp, pass_samples, good):
        """One acquisition pass, delivered on the GUI thread."""
        self._generation += 1
        self._latest_timestamp = timestamp
        self._latest_samples = pass_samples
        for unit in good:
            self._live_samples[unit] = pass_samples[unit]
        self.history.push(self._generation, timestamp, pass_samples)
        self.samples_updated.emit(self._generation)

    # ==================================================================== #
    #  Setpoints                                                           #
    # ==================================================================== #

    def queue_setpoint(self, unit, setpoint):
        """Queue a normal command unless a priority zero holds this unit.

        Every ordinary setpoint the application issues -- typed, batched,
        ramped, ignition, replayed -- passes through here, which is why the
        recorder listens here; the zero commands are the only other thing it
        hears, and they travel on their own priority queue.  A command that is
        refused is not recorded: the sequence is what the rig was asked *and
        allowed* to do.
        """
        limit_error = self._setpoint_limit_error(unit, setpoint)
        if limit_error == "invalid setpoint":
            self._log(f"Unit {unit}: invalid setpoint rejected.")
            return False
        value = float(setpoint)
        maximum = self.max_flow_for(unit)
        if limit_error:
            self._log(f"Unit {unit}: SP {value:.3f} SLPM rejected; command "
                      f"ceiling is {maximum:.3f} SLPM.")
            return False
        if (unit in self._zero_locked_units
                or unit in self._watchdog_locked_units) and abs(value) > 0.001:
            return False
        self.setpoint_queue.put((unit, value))
        self._note_setpoint(unit, value)
        return True

    def _note_setpoint(self, unit, value):
        """Offer one accepted setpoint to the recorder.

        Separate from :meth:`queue_setpoint` because the zero commands are the
        one kind of setpoint that does not travel on the normal queue, and a
        recording that omitted them would show the flows still up at the moment
        the operator put them down.
        """
        if not self._recorder.active:
            return
        key = self._record_keys.get(unit)
        if key is not None:
            self._recorder.note(key, float(value))

    def unit_for_role(self, key):
        """Resolve a role key to the unit that answers for it *right now*.

        A ``custom_<unit>`` key carries its unit in its own name, but reading
        it back out is not enough on its own: a key that came from a saved
        sequence names whatever controller was on the rig the day it was
        recorded, which may not be on the rig today.  Checking the assignment
        is what stops a replay from writing setpoints into thin air.
        """
        unit = self.assignments.get(key)
        if not unit and key.startswith('custom_'):
            candidate = key[len('custom_'):]
            if self.custom_assignments.get(candidate) == key:
                unit = candidate
        return unit

    def set_role_setpoint(self, key, setpoint):
        """Apply a setpoint to a role, pacing it if that line is paced.

        Whether the move is ramped is decided by the controller's declared ramp
        rate and by whether the line may be stepped at all -- see
        :meth:`ramp_seconds_for`.  A line with neither is written straight out,
        which is what typing a setpoint has always done.
        """
        unit = self.unit_for_role(key)
        if not unit:
            self.failed.emit("Manual Set", f"No unit assigned for {key}.")
            return False
        try:
            value = float(setpoint)
        except (TypeError, ValueError):
            self.failed.emit("Manual Set", "Setpoint must be a finite number.")
            return False
        if not math.isfinite(value) or value < 0.0:
            self.failed.emit("Manual Set", "Setpoint must be finite and non-negative.")
            return False
        maximum = self.max_flow_for(unit)
        if maximum is not None and value > maximum:
            self.failed.emit(
                "Manual Set", f"Setpoint {value:g} SLPM exceeds Unit {unit}'s "
                f"command ceiling of {maximum:g} SLPM.")
            return False
        start = self.flow_for_role(key)
        seconds = self.ramp_seconds_for(unit, key, value - start)
        if seconds > 0.0:
            # replace: a setpoint typed while the line is still moving is a
            # change of mind, and the new figure is the one that counts.  The
            # move restarts from where the flow actually is, so redirecting
            # mid-ramp is a redirection rather than a jump.
            return self.start_ramp(key, value, seconds=seconds, replace=True)
        queued = self.queue_setpoint(unit, value)
        if queued:
            self._log(f"Unit {unit} ({key}): SP → {value:.3f} SLPM (manual)")
        return queued

    def flow_for_role(self, key, samples=None):
        """Live flow for a role, straight from the sample cache."""
        unit = self.unit_for_role(key)
        if not unit:
            return 0.0
        source = self._live_samples if samples is None else samples
        value = source.get(unit, {}).get('flow')
        try:
            return float(value) if value is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    def phi_values(self, samples=None):
        """``(stage 1, stage 2, global)`` equivalence ratios."""
        flow = lambda key: self.flow_for_role(key, samples)
        nh3_r, h2_r = flow('nh3_rich'), flow('h2_rich')
        nh3_l, h2_l = flow('nh3_lean'), flow('h2_lean')
        air_r, air_l = flow('rich_air'), flow('lean_air')
        nh3_pilot = flow('nh3_pilot')
        h2_pilot = flow('h2_pilot')
        ch4_pilot = flow('ch4_pilot')
        ch4_stage1 = flow('ch4_stage1')
        ch4_stage2 = flow('ch4_stage2')
        return (
            self.calc.phi(
                nh3_r + nh3_pilot, h2_r + h2_pilot, air_r,
                ch4_stage1 + ch4_pilot),
            self.calc.phi(nh3_l, h2_l, air_l, ch4_stage2),
            self.calc.phi(
                nh3_r + nh3_l + nh3_pilot,
                h2_r + h2_l + h2_pilot, air_r + air_l,
                ch4_stage1 + ch4_stage2 + ch4_pilot),
        )

    # ------------------------------------------------------------------ #
    #  Live combustion estimate                                          #
    # ------------------------------------------------------------------ #

    #: Which roles feed each staged scope.  The selected pilot fuel is counted
    #: into stage 1, exactly as :meth:`phi_values` counts it. A phi on this card
    #: that disagreed with the phi two tiles above it would be worse than no
    #: phi at all.
    STAGE_ROLES = {
        SCOPE_STAGE1: ({
            'NH3': ('nh3_rich', 'nh3_pilot'),
            'H2': ('h2_rich', 'h2_pilot'),
            'CH4': ('ch4_stage1', 'ch4_pilot'),
        }, ('rich_air',)),
        SCOPE_STAGE2: ({
            'NH3': ('nh3_lean',),
            'H2': ('h2_lean',),
            'CH4': ('ch4_stage2',),
        }, ('lean_air',)),
    }

    def gas_flows(self, samples=None):
        """``{gas: SLPM}`` summed over every assigned controller.

        By gas rather than by role, because standard mode has no roles: a rig
        that is not staged is precisely the case where the role map is empty,
        and two NH3 lines feeding one burner are still two NH3 lines.  A
        controller with no gas or no zone is left out -- an operator who has
        not said what a line carries has not said it carries fuel.
        """
        source = self._live_samples if samples is None else samples
        totals = {}
        for unit, (gas, zone) in self.selection.items():
            if gas in (roles.UNSELECTED_GAS, '', None):
                continue
            if zone == roles.UNASSIGNED_ZONE:
                continue
            value = source.get(unit, {}).get('flow')
            try:
                flow = float(value) if value is not None else 0.0
            except (TypeError, ValueError):
                flow = 0.0
            totals[gas] = totals.get(gas, 0.0) + flow
        return totals

    def combustion_flows(self, scope=SCOPE_ALL, samples=None):
        """``({fuel: SLPM}, air SLPM, other SLPM)`` for one inlet of the rig.

        The third figure is everything assigned that is neither fuel nor air --
        a nitrogen purge or a diluent line.  It takes no part in phi or in the
        power, and it is carried anyway because it still occupies the duct and
        the bulk velocity has to account for it.
        """
        mapping = self.STAGE_ROLES.get(scope)
        if mapping is None:
            totals = self.gas_flows(samples)
            fuels = {fuel: totals.get(fuel, 0.0) for fuel in combustion.FUELS}
            inert = sum(flow for gas, flow in totals.items()
                        if gas != 'Air' and gas not in combustion.FUELS)
            return fuels, totals.get('Air', 0.0), inert
        fuel_roles, air_roles = mapping
        fuels = {
            fuel: sum(self.flow_for_role(key, samples) for key in keys)
            for fuel, keys in fuel_roles.items()
        }
        air = sum(self.flow_for_role(key, samples) for key in air_roles)
        # The RQL roles are fuel and air and nothing else, so a staged scope
        # has no third flow to carry.
        return fuels, air, 0.0

    def combustion_estimate(self, scope=SCOPE_ALL, samples=None):
        """Every derived combustion figure for one inlet, from live flows.

        One call per card refresh: the tiles are read together, so they are
        computed together and describe one moment of the rig rather than three
        consecutive ones.
        """
        fuels, air, inert = self.combustion_flows(scope, samples)
        return combustion.estimate(fuels, air,
                                   self.combustion_effective_diameter(scope),
                                   inert)

    def combustion_diameter(self, scope=SCOPE_ALL):
        """The declared inlet bore in mm for one scope, or ``None``."""
        field = combustion_prefs.DIAMETER_FIELDS.get(scope)
        return self.combustion_prefs.get(field) if field else None

    def combustion_geometry(self, scope=SCOPE_ALL):
        """Whether this inlet is entered as ``diameter`` or ``area``."""
        field = combustion_prefs.GEOMETRY_FIELDS.get(scope)
        return combustion_prefs.clean_geometry(
            self.combustion_prefs.get(field) if field else None)

    def combustion_area(self, scope=SCOPE_ALL):
        """The selected per-inlet cross-sectional area in mm²."""
        if self.combustion_geometry(scope) == combustion_prefs.GEOMETRY_AREA:
            field = combustion_prefs.AREA_FIELDS.get(scope)
            return self.combustion_prefs.get(field) if field else None
        diameter = self.combustion_diameter(scope)
        if diameter is None:
            return None
        return math.pi * (float(diameter) / 2.0) ** 2

    def combustion_inlets(self, scope=SCOPE_ALL):
        """Parallel inlet count for a scope; only Stage 2 may exceed one."""
        if scope != SCOPE_STAGE2:
            return 1
        return combustion_prefs.clean_inlet_count(
            self.combustion_prefs.get('stage2_inlets'))

    def combustion_effective_diameter(self, scope=SCOPE_ALL):
        """Diameter of one circle with the scope's total flow area.

        Stage 2 may have several identical inlets.  Areas add, so the
        equivalent diameter is the entered per-inlet diameter times √count.
        Passing that to the existing velocity calculation is exactly the same
        as dividing total flow by ``count × area_per_inlet``.
        """
        area = self.combustion_area(scope)
        if area is None:
            return None
        total_area = float(area) * self.combustion_inlets(scope)
        return math.sqrt(4.0 * total_area / math.pi)

    def set_combustion_diameter(self, scope, millimetres):
        """Declare -- or with ``None``/0 withdraw -- one inlet bore, in mm.

        Nothing is written to hardware and no flow changes: the bore is an
        input to an on-screen estimate only, so it is safe to correct mid-run.
        Withdrawing it blanks the velocity rather than falling back to a guess.
        """
        field = combustion_prefs.DIAMETER_FIELDS.get(scope)
        if field is None:
            return None
        cleaned = combustion_prefs.clean_diameter(millimetres)
        if self.combustion_prefs.get(field) == cleaned:
            return cleaned
        self.combustion_prefs[field] = cleaned
        if cleaned is None:
            self._log(f"Combustion: {scope} inlet diameter cleared "
                      f"(bulk velocity not shown)")
        else:
            self._log(f"Combustion: {scope} inlet diameter → "
                      f"{cleaned:.2f} mm")
        self._save_combustion_prefs()
        return cleaned

    def set_combustion_area(self, scope, square_millimetres):
        """Declare one inlet's cross-sectional area in square millimetres."""
        field = combustion_prefs.AREA_FIELDS.get(scope)
        if field is None:
            return None
        cleaned = combustion_prefs.clean_area(square_millimetres)
        if self.combustion_prefs.get(field) == cleaned:
            return cleaned
        self.combustion_prefs[field] = cleaned
        if cleaned is None:
            self._log(f'Combustion: {scope} inlet area cleared '
                      '(bulk velocity not shown)')
        else:
            self._log(f'Combustion: {scope} inlet area → '
                      f'{cleaned:.2f} mm²')
        self._save_combustion_prefs()
        return cleaned

    def set_combustion_geometry(self, scope, mode):
        """Choose the inlet input representation without changing its area."""
        field = combustion_prefs.GEOMETRY_FIELDS.get(scope)
        if field is None:
            return combustion_prefs.GEOMETRY_DIAMETER
        cleaned = combustion_prefs.clean_geometry(mode)
        if self.combustion_geometry(scope) == cleaned:
            return cleaned

        # Carry the current cross-section across the representation switch so
        # changing the editor does not make a live estimate jump or disappear.
        current_area = self.combustion_area(scope)
        self.combustion_prefs[field] = cleaned
        if current_area is not None:
            if cleaned == combustion_prefs.GEOMETRY_AREA:
                area_field = combustion_prefs.AREA_FIELDS[scope]
                self.combustion_prefs[area_field] = current_area
            else:
                diameter_field = combustion_prefs.DIAMETER_FIELDS[scope]
                self.combustion_prefs[diameter_field] = math.sqrt(
                    4.0 * current_area / math.pi)
        self._log(f'Combustion: {scope} inlet input → {cleaned}')
        self._save_combustion_prefs()
        return cleaned

    def set_combustion_inlets(self, scope, count):
        """Set the number of identical Stage 2 inlets used for velocity."""
        if scope != SCOPE_STAGE2:
            return 1
        cleaned = combustion_prefs.clean_inlet_count(count)
        if self.combustion_inlets(scope) == cleaned:
            return cleaned
        self.combustion_prefs['stage2_inlets'] = cleaned
        self._log(f'Combustion: Stage 2 inlet count → {cleaned}')
        self._save_combustion_prefs()
        return cleaned

    @property
    def combustion_live(self):
        """Whether the estimate refreshes as the flows come in."""
        return bool(self.combustion_prefs.get('live', True))

    def set_combustion_live(self, running):
        """Turn the live estimate on or off.

        Off is a display decision and nothing more -- acquisition, logging and
        the ramps carry on untouched.  It exists for the machine that is
        already working hard driving the graph, where redrawing a dozen tiles
        ten times a second is the part worth giving up.
        """
        running = bool(running)
        if self.combustion_live == running:
            return running
        self.combustion_prefs['live'] = running
        self._log("Combustion estimate: live"
                  if running else "Combustion estimate: paused")
        self._save_combustion_prefs()
        return running

    @property
    def combustion_interval(self):
        """Acquisition passes between refreshes of the estimate; 1 is every."""
        return combustion_prefs.clean_interval(
            self.combustion_prefs.get('interval'))

    def set_combustion_interval(self, passes):
        """Refresh the estimate every ``passes`` acquisition passes."""
        cleaned = combustion_prefs.clean_interval(passes)
        if self.combustion_interval == cleaned:
            return cleaned
        self.combustion_prefs['interval'] = cleaned
        self._log("Combustion estimate: refreshing every pass" if cleaned == 1
                  else f"Combustion estimate: refreshing every {cleaned} passes")
        self._save_combustion_prefs()
        return cleaned

    def _save_combustion_prefs(self):
        """Persist the settings and tell the views.  A failed write is said."""
        error = combustion_prefs.save(self.combustion_prefs)
        if error:
            # Worth saying, not worth stopping for: the setting is in force for
            # this session whether or not it survives to the next one.
            self._log(f"Could not save the combustion settings: {error}")
        self.combustion_changed.emit(dict(self.combustion_prefs))

    def latest_samples(self):
        return self._latest_samples

    def live_samples(self):
        return self._live_samples

    def read_snapshot(self):
        """A copied read-only description of the current rig configuration."""
        return build_snapshot(self)

    def read_history(self, *, window_s=None, units=None, metric_keys=None):
        """Copied graph history for a caller that must not touch its deques."""
        return windowed_history(
            self.history, window_s=window_s, units=units, metric_keys=metric_keys)

    def read_derived_state(self, *, duration_s, tolerance):
        """Current phi values and role-by-role flow tracking over a window."""
        return derive_state(self, duration_s=duration_s, tolerance=tolerance)

    def clear_history(self):
        """Throw away the plotted history and restart its time axis.

        The CSV log is untouched.  Clearing the graph is housekeeping on the
        screen, and an operator tidying a plot mid-run is not asking to lose
        the record of the run.
        """
        self.history.clear(generation=self._generation)
        self._log("Graph history cleared.")

    def set_history_limit(self, samples):
        """Bound the plotted history to ``samples`` acquisition passes."""
        samples = max(60, int(samples))
        self.history.set_limit(samples)
        self._log(f"Graph history limit set to {samples} samples "
                  f"(~{samples * self.poll_interval_s:.0f} s at the current "
                  f"pass rate).")
        return samples

    # ==================================================================== #
    #  Verified zero (the E-STOP)                                          #
    # ==================================================================== #

    @property
    def estop_armed(self):
        return self._estop_armed

    def _set_estop_armed(self, armed):
        armed = bool(armed)
        if armed != self._estop_armed:
            self._estop_armed = armed
            self.estop_armed_changed.emit(armed)

    def zero_fuel(self):
        return self.request_zero(include_air=False)

    def zero_all(self):
        return self.request_zero(include_air=True)

    def make_zero_request(self, *, include_air, scope=None):
        """Snapshot the currently selected units for a priority zero."""
        unit_gases = [
            (unit, gas) for unit, (gas, zone) in self.selection.items()
            if gas not in ('', roles.UNSELECTED_GAS)
            and zone != roles.UNASSIGNED_ZONE
        ]
        units = select_zero_units(unit_gases, include_air=include_air)
        return ZeroRequest(
            scope=scope or ("all" if include_air else "fuel"),
            units=tuple(units))

    def enqueue_watchdog_zero(self, request, reason):
        """Enqueue a verified zero from a non-Qt deadline watchdog.

        Only the serial monitor loop touches hardware. ``Queue`` and the ramp
        runner are thread-safe, so this method can pre-empt writes even while
        Qt's event loop is blocked. GUI narration is posted for later.
        """
        if not isinstance(request, ZeroRequest) or not request.units:
            return False
        units = tuple(request.units)
        self._zero_action_active = True
        self._active_zero_request = request
        self._zero_locked_units.update(units)
        self._watchdog_locked_units.update(units)
        self._ramps.cancel_all()
        for unit in units:
            self._last_sp[unit] = 0.0
        self._zero_request_queue.put(request)
        self._post(self._announce_watchdog_zero, request, str(reason))
        return True

    def _announce_watchdog_zero(self, request, reason):
        self._set_ignition_state("IDLE")
        self._set_estop_armed(False)
        for unit in request.units:
            self._note_setpoint(unit, 0.0)
        self.banner.emit("PLAN TIMEOUT — VERIFIED ZERO IN PROGRESS", 'danger')
        self._log(
            f"Experiment-plan watchdog fired: {reason} Priority zero queued "
            f"for Unit(s) {', '.join(request.units)}.")
        self.zero_started.emit(request)

    def release_watchdog_zero_lock(self, units):
        """Release the temporary recovery lock after Qt has stopped the plan."""
        self._watchdog_locked_units.difference_update(units)

    def request_zero(self, *, include_air):
        """Command an immediate verified zero without dropping the connection.

        Monitoring keeps running.  Closing the port would mean the operator
        loses sight of the rig at exactly the moment they need it most.
        """
        request = self.make_zero_request(include_air=include_air)
        scope = "all" if include_air else "fuel"
        scope_label = "ALL FLOWS" if include_air else "FUEL FLOWS"
        return self._request_zero_units(
            request.units, scope=scope, scope_label=scope_label)

    def _request_zero_units(self, units, *, scope, scope_label):
        """Start the established verified-zero path for explicit units."""
        units = list(dict.fromkeys(str(unit) for unit in units if str(unit)))
        if not self.controllers_connected:
            self.failed.emit("Zero Flow", "Connect the flow meters first.")
            return False
        if self._zero_action_active:
            return False
        if self.is_connecting or self._restart_pending:
            self.failed.emit(
                "Zero Flow", "Wait for the current connection operation to finish.")
            return False
        if not units:
            self.failed.emit(
                "Zero Flow", "No selected controllers match this zero-flow action.")
            return False

        self._zero_action_active = True
        self._zero_locked_units.update(units)
        self._set_ignition_state("IDLE")
        self._ramps.cancel_all()
        # A replay is a stream of setpoints like any other, and the zero lock
        # would block each one individually anyway; stopping it here means the
        # rig does not sit there being refused a hundred times a second.
        self.stop_replay(reason="cancelled by the zero-flow command")
        for unit in units:
            # A reconnect must never restore the pre-zero value, even if one
            # controller fails to confirm before the operator retries.
            self._last_sp[unit] = 0.0
            # Recorded here rather than where the zero is written, because the
            # write happens on the monitor thread once this command reaches the
            # front of the priority queue -- and the instant that belongs in the
            # recording is the one the operator asked at.
            self._note_setpoint(unit, 0.0)
        self._set_estop_armed(False)
        self.banner.emit(f"ZERO {scope_label} — command in progress", 'danger')
        self._log(f"ZERO {scope_label} requested for Unit(s) {', '.join(units)}. "
                  "Live monitoring will remain connected.")

        request = ZeroRequest(scope=scope, units=tuple(units))
        self._active_zero_request = request
        self.zero_started.emit(request)

        monitor_running = (self.is_monitoring and self._monitor_future is not None
                           and not self._monitor_future.done())
        if monitor_running:
            self._zero_request_queue.put(request)
            return True

        if self._monitor_future is not None and not self._monitor_future.done():
            self._zero_action_active = False
            self._active_zero_request = None
            self._zero_locked_units.difference_update(units)
            self._set_estop_armed(True)
            self.failed.emit(
                "Zero Flow", "Wait for the live monitor connection to finish closing.")
            return False

        # Monitoring is intentionally stopped: use transient handles on the
        # same serial owner, keeping the logical connection intact.
        self._emergency_stop_active = True
        port = self._monitor_port or self.port
        baudrate = self._monitor_baudrate or self.baudrate
        try:
            self._submit(
                self._zero_controllers_async(port, baudrate, units),
                lambda future, req=request: self._finish_direct_zero(req, future))
        except Exception as exc:
            self._finish_zero_request(
                request, {}, {"serial worker": f"{type(exc).__name__}: {exc}"})
        return True

    async def _service_zero_requests(self, controllers):
        """Service priority zero commands on the monitor's serial owner."""
        serviced = False
        while not self._zero_request_queue.empty():
            try:
                request = self._zero_request_queue.get_nowait()
            except Exception:
                break
            serviced = True
            targets = set(request.units)

            # Drop stale normal commands for the targeted units, and *only*
            # those: a unit outside the safety scope must keep the setpoint
            # the operator asked for.
            retained = []
            while not self.setpoint_queue.empty():
                try:
                    item = self.setpoint_queue.get_nowait()
                except Exception:
                    break
                if item[0] not in targets:
                    retained.append(item)
            for item in retained:
                self.setpoint_queue.put(item)

            confirmed = {}
            errors = {}
            for unit in request.units:
                controller = controllers.get(unit)
                if controller is None:
                    errors[unit] = "controller connection is not open"
                    continue
                last_error = None
                for attempt in range(1, 3):
                    try:
                        await asyncio.wait_for(
                            controller.set_flow_rate(0.0), timeout=2.5)
                        reading = await asyncio.wait_for(controller.get(), timeout=1.5)
                        setpoint = float(reading['setpoint'])
                        if not math.isfinite(setpoint) or abs(setpoint) > 0.001:
                            raise OSError(
                                f"zero readback not confirmed (setpoint={setpoint})")
                        confirmed[unit] = setpoint
                        self._last_sp[unit] = 0.0
                        break
                    except Exception as exc:
                        last_error = f"attempt {attempt}: {type(exc).__name__}: {exc}"
                if unit not in confirmed:
                    errors[unit] = last_error or "zero command was not confirmed"
            self._post(self._finish_zero_request, request, confirmed, errors)
        return serviced

    async def _zero_controllers_async(self, port, baudrate, units):
        """Write zero and confirm the reported setpoint, one handle per unit."""
        confirmed = {}
        errors = {}
        for unit in units:
            last_error = None
            for attempt in range(1, 3):
                try:
                    async with FlowController(
                            address=port, unit=unit, baudrate=baudrate,
                            timeout=0.3) as controller:
                        await asyncio.wait_for(
                            controller.set_flow_rate(0.0), timeout=2.5)
                        reading = await asyncio.wait_for(controller.get(), timeout=1.5)
                    setpoint = float(reading['setpoint'])
                    if not math.isfinite(setpoint) or abs(setpoint) > 0.001:
                        raise OSError(
                            f"zero readback not confirmed (setpoint={setpoint})")
                    confirmed[unit] = setpoint
                    break
                except Exception as exc:
                    last_error = f"attempt {attempt}: {type(exc).__name__}: {exc}"
            if unit not in confirmed:
                errors[unit] = last_error or "zero command was not confirmed"
        return confirmed, errors

    def _finish_direct_zero(self, request, future):
        try:
            confirmed, errors = future.result()
        except Exception as exc:
            confirmed, errors = {}, {"serial task": f"{type(exc).__name__}: {exc}"}
        self._finish_zero_request(request, confirmed, errors)

    def _finish_zero_request(self, request, confirmed, errors):
        """Report zero verification while preserving controller connections."""
        self._zero_action_active = False
        self._active_zero_request = None
        self._emergency_stop_active = False
        self._zero_locked_units.difference_update(request.units)
        self._set_estop_armed(self.controllers_connected)
        if request.scope == "all" or request.scope.endswith("_all"):
            scope_label = "ALL FLOWS"
        elif request.scope == "fuel" or request.scope.endswith("_fuel"):
            scope_label = "FUEL FLOWS"
        else:
            scope_label = "LIMIT ENFORCEMENT"
        for unit in confirmed:
            self._last_sp[unit] = 0.0
            self._log(f"ZERO {scope_label}: Unit {unit} confirmed at zero. ✓")
        if errors:
            self.banner.emit(
                f"ZERO {scope_label} — NOT CONFIRMED ON ALL UNITS", 'danger')
            self._log(f"ZERO {scope_label} warning: unconfirmed Unit(s) "
                      + ", ".join(str(unit) for unit in errors))
        else:
            self.banner.emit(
                f"ZERO {scope_label} CONFIRMED — live monitoring continues",
                'ignited')
            self._log(f"ZERO {scope_label} confirmed on {len(confirmed)} "
                      "controller(s); connections remain open.")
        self.zero_finished.emit(request, confirmed, errors)

    # ==================================================================== #
    #  Ramps and the ignition sequence                                     #
    # ==================================================================== #

    def _emit_ramp_setpoint(self, unit, setpoint):
        self.queue_setpoint(unit, setpoint)

    def ramp_active(self, key):
        return self._ramps.is_active(key)

    def start_ramp(self, key, target, steps=20, interval=0.5, *, seconds=None,
                   replace=False):
        """Walk one role to ``target``.

        ``seconds`` asks for a duration and lets the step count follow from it,
        which is how a declared ramp rate is honoured; ``steps``/``interval``
        remain for callers that mean a specific shape.  ``replace`` supersedes a
        ramp already in flight on this line rather than declining, which is what
        a new setpoint typed mid-move means.
        """
        unit = self.unit_for_role(key)
        if not unit:
            self.failed.emit("Ramp", f"No unit assigned for {key}.")
            return False
        if seconds is not None:
            # A rate over a short journey can ask for less than one step; the
            # move still has to happen, so it becomes a single one.
            interval = RAMP_STEP_S
            steps = min(MAX_RAMP_STEPS,
                        max(1, int(round(float(seconds) / interval))))
        if steps < 1 or interval < 0.05:
            self.failed.emit("Ramp", "Steps ≥ 1 and interval ≥ 0.05 s.")
            return False
        start = self.flow_for_role(key)
        started = self._ramps.start(
            key, [RampLeg(unit, start, float(target))], int(steps), float(interval),
            on_progress=lambda percent: self.ramp_progress.emit(key, percent),
            label=roles.ROLE_LABELS.get(key, key), replace=replace)
        if not started:
            self._log(f"{key}: a ramp is already running.")
            return False
        self._log(f"{key}: ramping {start:.3f} → {float(target):.3f} SLPM "
                  f"({steps} × {interval} s).")
        return True

    def set_targets(self, targets):
        """Store auto-calculated targets, keeping only assigned roles."""
        self.target_flows = {
            key: value for key, value in targets.items()
            if self.assignments.get(key)
        }
        self.targets_changed.emit(dict(self.target_flows))
        return self.target_flows

    def set_autocalc_request(self, request):
        """Remember the complete input condition behind calculated targets."""
        self.autocalc_request = request
        return request

    def _set_ignition_state(self, state):
        if state != self.ignition_state:
            self.ignition_state = state
            self.ignition_changed.emit(state)

    def ready_ignition(self, fuel_scale, air_scale, steps, interval):
        """Ramp every assigned flow to a fraction of target, ready to light."""
        # The view hides the ignition card in standard mode, but hiding a
        # button while leaving its method callable is exactly the gap that a
        # UDP command or a stale shortcut walks through.
        if not self.is_staged:
            self.failed.emit(
                "Pre-ignition",
                "The ignition sequence belongs to staged mode. Switch to "
                "staged operation first.")
            return False
        if not self.target_flows:
            self.failed.emit("Pre-ignition", "Calculate targets first.")
            return False
        if not self.is_monitoring:
            self.failed.emit("Pre-ignition", "Start the monitor first.")
            return False
        if not (0 < fuel_scale <= 1) or not (0 < air_scale <= 1):
            self.failed.emit(
                "Pre-ignition", "Fuel% and Air% must be > 0 and ≤ 100.")
            return False
        if steps < 1 or interval < 0.05:
            self.failed.emit(
                "Pre-ignition", "Ramp steps ≥ 1 and interval ≥ 0.05 s.")
            return False

        self.pre_fuel_scale = fuel_scale
        self.pre_air_scale = air_scale
        legs = []
        for key, target in self.target_flows.items():
            unit = self.assignments.get(key)
            if not unit:
                continue
            scale = fuel_scale if key in roles.FUEL_KEYS else air_scale
            legs.append(RampLeg(unit, self.flow_for_role(key), target * scale))
        if not legs:
            self.failed.emit("Pre-ignition", "No assigned unit has a target.")
            return False

        self._set_ignition_state("PRE_IGNITION")
        self.banner.emit(
            f"PRE-IGNITION — ramping to {fuel_scale * 100:.0f}% fuel / "
            f"{air_scale * 100:.0f}% air — ({steps} steps × {interval} s)",
            'pre')
        self._log(f"Pre-ignition: ramping to {fuel_scale * 100:.0f}% fuel, "
                  f"{air_scale * 100:.0f}% air ({steps} x {interval} s).")
        self._ramps.start(
            'pre_ignition', legs, int(steps), float(interval),
            guard=lambda: self.ignition_state == "PRE_IGNITION",
            on_done=lambda completed: self._post(
                self._pre_ignition_done, completed),
            label="Pre-ignition")
        return True

    def _pre_ignition_done(self, completed):
        if not completed or self.ignition_state != "PRE_IGNITION":
            return
        self.banner.emit("PRE-IGNITION COMPLETE — ready to ignite", 'pre')
        self._log("Pre-ignition ramp complete — ready to ignite. ✓")

    def ignite(self, steps, interval):
        """Ramp from the pre-ignition fractions up to the full targets."""
        if self.ignition_state != "PRE_IGNITION":
            self.failed.emit("Ignite", "Pre-ignition first.")
            return False
        if steps < 1 or interval < 0.05:
            self.failed.emit("Ignite", "Steps ≥ 1 and interval ≥ 0.05 s.")
            return False

        legs = []
        for key, target in self.target_flows.items():
            unit = self.assignments.get(key)
            if not unit:
                continue
            scale = (self.pre_fuel_scale if key in roles.FUEL_KEYS
                     else self.pre_air_scale)
            legs.append(RampLeg(unit, target * scale, target))
        if not legs:
            self.failed.emit("Ignite", "No assigned unit has a target.")
            return False

        self._set_ignition_state("IGNITED")
        self.banner.emit(
            f"IGNITED — ramping to full target ({steps} steps × {interval} s)",
            'ignited')
        self._log(f"Ignite: ramping all flows to target "
                  f"({steps} × {interval} s).")
        self._ramps.start(
            'ignition', legs, int(steps), float(interval),
            guard=lambda: self.ignition_state == "IGNITED",
            on_done=lambda completed: self._post(self._ignition_done, completed),
            label="Ignition")
        return True

    def _ignition_done(self, completed):
        if not completed or self.ignition_state != "IGNITED":
            return
        self.banner.emit("IGNITED — full target flows", 'ok')
        self._log("Ignition ramp complete — at full target flows. ✓")

    def abort(self):
        """The abort button: zero everything, including air."""
        return self.zero_all()

    # ==================================================================== #
    #  Recorded sequences                                                  #
    # ==================================================================== #

    @property
    def sequence_state(self):
        return self._sequence_state

    def _set_sequence_state(self, state):
        if state != self._sequence_state:
            self._sequence_state = state
            self.sequence_state_changed.emit(state)

    def track_metas(self):
        """One :class:`TrackMeta` per controller the session can command.

        Roles first and in rig order, so the curve editor lists stage 1 above
        stage 2 above the pilot rather than in dictionary order; anything
        outside the RQL roles follows as ``custom_<unit>``.
        """
        metas = []
        for key, label in roles.ROLES:
            unit = self.assignments.get(key)
            if not unit:
                continue
            gas, _zone = self.selection.get(unit, ('', ''))
            metas.append(TrackMeta(key=key, label=label, gas=gas, unit=unit))
        for unit, key in sorted(self.custom_assignments.items()):
            gas, zone = self.selection.get(unit, ('', ''))
            label = f"Unit {unit}" + (f" · {gas}" if gas else "")
            metas.append(TrackMeta(key=key, label=label, gas=gas, unit=unit))
        return metas

    def commanded_setpoints(self):
        """``{role key: setpoint}`` as last commanded, for a recording's t=0.

        The last written setpoint is the truth here, not the measured flow: a
        recording is of what the rig was asked for, and a controller settling
        towards its setpoint has not changed what it was asked for.
        """
        values = {}
        for meta in self.track_metas():
            value = self._last_sp.get(meta.unit)
            if value is None:
                value = self._live_samples.get(meta.unit, {}).get('sp')
            try:
                values[meta.key] = float(value) if value is not None else 0.0
            except (TypeError, ValueError):
                values[meta.key] = 0.0
        return values

    # -- recording -------------------------------------------------------- #

    def start_recording(self):
        """Begin capturing every setpoint the session commands."""
        if self._sequence_state != SEQ_IDLE:
            return False
        if not self.is_monitoring:
            self.failed.emit(
                "Record",
                "Start the live monitor first — a recording made while nothing "
                "is being written to the controllers would not be a record of "
                "anything that happened.")
            return False
        metas = self.track_metas()
        if not metas:
            self.failed.emit("Record", "No controller is assigned.")
            return False

        self._record_keys = {meta.unit: meta.key for meta in metas}
        self._recorder.start(metas, self.commanded_setpoints(),
                             mode=self.operating_mode)
        self._set_sequence_state(SEQ_RECORDING)
        self._replay_timer.start()
        self._log(f"Recording started: {len(metas)} track(s), "
                  f"{self.operating_mode} mode.")
        self.sequence_progress.emit(0.0, 0.0)
        return True

    def add_sequence_keyframe(self):
        """Drop the operator's own key point at the current instant."""
        if not self._recorder.active:
            return None
        at = self._recorder.mark()
        if at is None:
            return None
        self._log(f"Keyframe placed at {at:.2f} s.")
        self.sequence_keyframe_added.emit(at)
        return at

    def stop_recording(self, name=None, *, save=True):
        """Close the recording and, by default, write it straight to disk."""
        if not self._recorder.active:
            return None
        sequence = self._recorder.stop(name)
        self._record_keys = {}
        self._replay_timer.stop()
        self._set_sequence_state(SEQ_IDLE)
        if sequence is None:
            return None

        self.sequence = sequence
        self._log(f"Recording stopped: '{sequence.name}', "
                  f"{sequence.duration:.1f} s, {len(sequence.tracks)} track(s).")
        self.sequence_changed.emit(sequence)
        self.sequence_progress.emit(0.0, sequence.duration)
        if save:
            self.save_sequence()
        return sequence

    def cancel_recording(self):
        if not self._recorder.active:
            return False
        self._recorder.cancel()
        self._record_keys = {}
        self._replay_timer.stop()
        self._set_sequence_state(SEQ_IDLE)
        self._log("Recording discarded.")
        return True

    # -- the sequence in hand --------------------------------------------- #

    def sequence_validation_errors(self, sequence):
        """Return fail-closed errors for a sequence in this live rig.

        Files are parsed strictly by :class:`Sequence`; this extra pass is
        intentionally session-owned because only the live assignments reveal
        which unit (and therefore which command ceiling) a role drives today.
        Unassigned tracks remain loadable for editing and are rejected by the
        existing binding check when replay is requested.
        """
        if not isinstance(sequence, Sequence):
            return ["The sequence is not a valid sequence object."]
        errors = []
        keys = [track.key for track in sequence.tracks]
        duplicates = sorted({key for key in keys if keys.count(key) > 1})
        if duplicates:
            errors.append(
                "Duplicate sequence role(s): " + ", ".join(duplicates) + ".")
        for track in sequence.tracks:
            unit = self.unit_for_role(track.key)
            maximum = self.max_flow_for(unit) if unit else None
            for index, frame in enumerate(track.keyframes, start=1):
                try:
                    at = float(frame.t)
                except (TypeError, ValueError):
                    errors.append(f"{track.label}, keyframe {index}: time is invalid.")
                else:
                    if not math.isfinite(at) or at < 0.0:
                        errors.append(
                            f"{track.label}, keyframe {index}: time must be finite "
                            "and non-negative.")
                try:
                    value = float(frame.value)
                except (TypeError, ValueError):
                    errors.append(f"{track.label}, keyframe {index}: value is invalid.")
                    continue
                if not math.isfinite(value) or value < 0.0:
                    errors.append(
                        f"{track.label}, keyframe {index}: value must be finite "
                        "and non-negative.")
                elif maximum is not None and value > maximum:
                    errors.append(
                        f"{track.label}, keyframe {index}: {value:g} SLPM exceeds "
                        f"Unit {unit}'s command ceiling of {maximum:g} SLPM.")
        for index, marker in enumerate(sequence.markers, start=1):
            try:
                at = float(marker)
            except (TypeError, ValueError):
                errors.append(f"Marker {index}: time is invalid.")
            else:
                if not math.isfinite(at) or at < 0.0:
                    errors.append(
                        f"Marker {index}: time must be finite and non-negative.")
        return errors

    def _sequence_is_valid(self, sequence, action):
        """Report a validation failure through the normal user-facing signal."""
        errors = self.sequence_validation_errors(sequence)
        if not errors:
            return True
        detail = "\n".join(f"• {error}" for error in errors)
        self.failed.emit("Sequence", f"Cannot {action}:\n\n{detail}")
        return False

    def set_sequence(self, sequence):
        """Adopt a sequence edited by the view, or clear it with ``None``."""
        if self._sequence_state == SEQ_REPLAYING:
            return False
        if sequence is not None and not self._sequence_is_valid(sequence, "use this sequence"):
            return False
        previous = self.sequence
        self.sequence = sequence
        if sequence is None and previous is not None:
            # Worth a line in the log: the operator has put a recording down,
            # and the log is where the shape of a session is reconstructed
            # afterwards -- "cleared, then recorded again" reads very
            # differently from a recording that simply stopped.
            self._log(f"Sequence cleared: '{previous.name}'.")
        self.sequence_changed.emit(sequence)
        self.sequence_progress.emit(
            0.0, sequence.duration if sequence is not None else 0.0)
        return True

    def load_sequence(self, path):
        try:
            sequence = Sequence.load(path)
        except Exception as exc:
            self.failed.emit("Sequence",
                             f"Could not open that sequence:\n\n{exc}")
            return None
        if not self._sequence_is_valid(sequence, "load this sequence"):
            return None
        self.sequence = sequence
        self._log(f"Sequence loaded: '{sequence.name}' ({sequence.duration:.1f} s) "
                  f"from {path}")
        self.sequence_changed.emit(sequence)
        self.sequence_progress.emit(0.0, sequence.duration)
        return sequence

    def save_sequence(self, path=None):
        """Write the sequence in hand; without a path, pick a timestamped one."""
        if self.sequence is None:
            return None
        target = Path(path) if path else (
            self.sequence.path
            or self.sequence_dir / f"{self.sequence.name}.fcseq.json")
        try:
            actual = self.sequence.save(target)
        except Exception as exc:
            self.failed.emit("Sequence", f"Could not save the sequence:\n\n{exc}")
            return None
        self._log(f"Sequence saved: {actual}")
        self.sequence_saved.emit(actual)
        return actual

    # -- replay ----------------------------------------------------------- #

    @property
    def settle_enabled(self):
        return self._settle_enabled

    @property
    def settle_tolerance(self):
        return self._settle_tolerance

    def set_settle_policy(self, enabled=None, tolerance=None):
        """Configure the discrepancy hold; returns the policy in force.

        ``tolerance`` is a fraction of each track's own span.  Both take effect
        immediately, mid-replay included -- an operator who decides the hold is
        being too fussy about a line that is following perfectly well should not
        have to stop the run to say so.
        """
        if enabled is not None:
            self._settle_enabled = bool(enabled)
        if tolerance is not None:
            self._settle_tolerance = min(1.0, max(0.0, float(tolerance)))
        self._settle.enabled = self._settle_enabled
        self._settle.tolerance = self._settle_tolerance
        return self._settle_enabled, self._settle_tolerance

    def _replay_measured(self):
        """Measured flow per replayed track, straight from the sample cache.

        Built from the player's own bindings rather than through
        ``flow_for_role``, which answers 0.0 for a role that is not assigned --
        indistinguishable from a controller reading zero, and it would read as
        an enormous discrepancy.  A unit with no sample yet is simply left out.
        """
        measured = {}
        if self._player is None:
            return measured
        for key, unit in self._player.bound.items():
            value = self._live_samples.get(unit, {}).get('flow')
            if value is None:
                continue
            try:
                measured[key] = float(value)
            except (TypeError, ValueError):
                continue
        return measured

    def start_replay(self, sequence=None, repeats=1):
        """Play a sequence back out through the ordinary setpoint queue.

        ``repeats`` is the number of passes; ``0`` repeats until stopped.
        """
        if self._sequence_state != SEQ_IDLE:
            return False
        sequence = sequence or self.sequence
        if sequence is None:
            self.failed.emit("Replay", "Record or load a sequence first.")
            return False
        if not self.controllers_connected or not self.is_monitoring:
            self.failed.emit(
                "Replay", "Connect the controllers and start the monitor first.")
            return False
        if self.ignition_state != "IDLE":
            self.failed.emit(
                "Replay",
                "The ignition sequence is running. Zero all flows before "
                "replaying a recording.")
            return False
        if self._zero_action_active or self._zero_locked_units:
            self.failed.emit("Replay", "A zero-flow command is still in force.")
            return False
        if sequence.duration <= 0:
            self.failed.emit("Replay", "That sequence has no duration.")
            return False
        if not self._sequence_is_valid(sequence, "replay this sequence"):
            return False

        bound, missing = sequence.bind(self.unit_for_role)
        if missing:
            names = ", ".join(track.label for track in missing)
            self.failed.emit(
                "Replay",
                "This recording drives controllers that are not assigned "
                f"right now:\n\n{names}\n\nAssign them, or edit the sequence, "
                "before replaying it. A sequence that half-runs would leave "
                "the remaining flows unmetered.")
            return False

        # 0 means until stopped, and only an explicit 0: an unspecified repeat
        # count falls to a single pass, matching SequencePlayer.
        repeats = 1 if repeats is None else max(0, int(repeats))
        self.sequence = sequence
        self._player = SequencePlayer(
            sequence, bound, self.queue_setpoint, log=self._log,
            # Same rule as a typed setpoint: a line whose unit has ramping
            # turned off drops out of the protected set for this replay too,
            # or the sequence would pace a line the card says not to.
            rate_limited=frozenset(
                key for key in roles.RAMP_KEYS
                if not self.ramp_disabled_for(bound.get(key))),
            repeats=repeats,
            # A replay is paced by the same figure on the controller's card
            # that paces a typed setpoint, so a line that the operator has
            # slowed down stays slow however the setpoint arrives.
            rate_lookup=lambda track: self.effective_ramp_rate(
                bound.get(track.key)))
        self._player.prime()
        self._replay_started_at = time.monotonic()
        self._replay_last_tick = self._replay_started_at
        self._replay_held_s = 0.0
        # A fresh gate per replay: the hold counters are about this run, and a
        # run must not inherit a timed-out excursion from the previous one.
        self._settle = SettleGate(enabled=self._settle_enabled,
                                  tolerance=self._settle_tolerance)
        self._set_sequence_state(SEQ_REPLAYING)
        self._replay_timer.start()
        passes = ('repeating until stopped' if repeats == 0
                  else f"{repeats} passes" if repeats > 1 else 'once')
        self.banner.emit(
            f"REPLAYING '{sequence.name}' — {sequence.duration:.0f} s"
            + ('' if repeats == 1 else f" × {passes}"), 'pre')
        self._log(f"Replay started: '{sequence.name}' "
                  f"({sequence.duration:.1f} s, {len(bound)} track(s)), {passes}.")
        if self._settle_enabled:
            self._log(
                "Discrepancy hold armed: if any line reads more than "
                f"{self._settle_tolerance * 100:.0f}% of its span away from "
                "what it was told, the clock is held for every controller "
                "until it settles.")
        else:
            self._log("Discrepancy hold is off: transitions run to the clock "
                      "whether or not the flows have arrived.")
        if repeats != 1:
            # Worth saying once, at the point the operator commits to it: a
            # repeated run drives the rig from the closing state back to the
            # opening one on every wrap, which is a transition nobody recorded.
            opening = sequence.values_at(0.0)
            closing = sequence.values_at(sequence.duration)
            moved = [track.label for track in sequence.tracks
                     if track.key in bound
                     and abs(opening.get(track.key, 0.0)
                             - closing.get(track.key, 0.0))
                     > max(DEADBAND_FLOOR, track.span * DEADBAND_FRACTION)]
            if moved:
                self._log(
                    "Each repeat returns to the opening setpoints, so these "
                    f"lines step back at every wrap: {', '.join(moved)}. "
                    "Rate-limited lines are ramped; the rest are written "
                    "directly.")
        self.sequence_progress.emit(0.0, sequence.duration)
        self.sequence_cycle.emit(1, repeats)
        return True

    def stop_replay(self, reason="stopped by the operator"):
        """End a replay.  Flows are left where they are, not zeroed.

        Stopping a replay is not an emergency stop, and turning one into the
        other would make the operator's instinct -- reach for the nearest stop
        -- do something they did not ask for.  ZERO ALL is the button that
        zeroes.
        """
        if self._sequence_state != SEQ_REPLAYING:
            return False
        position = self._player.position if self._player else 0.0
        duration = self._player.duration if self._player else 0.0
        cycle = self._player.cycle if self._player else 1
        total = self._player.repeats if self._player else 1
        held = self._settle.total_held_s
        was_holding = self._settle.holding
        self._player = None
        self._replay_timer.stop()
        self._set_sequence_state(SEQ_IDLE)
        where = f"{position:.1f} s of {duration:.1f} s"
        if cycle > 1 or total != 1:
            where += f" on pass {cycle}" + (f" of {total}" if total else "")
        if held > 0.05:
            where += f" (held {held:.1f} s waiting for flows to settle)"
        self._log(f"Replay {reason} at {where}. Flows left as commanded.")
        if was_holding:
            self.sequence_hold.emit(False, "")
        self.banner.emit("", 'clear')
        self.sequence_progress.emit(position, duration)
        self.sequence_ended.emit(reason == "finished", reason)
        return True

    def _sequence_tick(self):
        """The one timer behind both the recording clock and the replay clock."""
        if self._sequence_state == SEQ_RECORDING:
            self.sequence_progress.emit(self._recorder.elapsed, 0.0)
            return
        if self._sequence_state != SEQ_REPLAYING or self._player is None:
            self._replay_timer.stop()
            return

        # Position comes from the clock, not from a tick count, so a GUI that
        # stalls for half a second resumes at the right place instead of
        # replaying the whole run in slow motion.
        now = time.monotonic()
        elapsed = now - self._replay_last_tick
        self._replay_last_tick = now
        duration = self._player.duration

        # Whether to hold is decided against what the lines were last told and
        # what they are reading now, before the position is worked out, because
        # a hold is spent by adding to ``_replay_held_s`` rather than by
        # stopping the clock.
        was_holding = self._settle.holding
        held_before = self._settle.held_s
        try:
            holding = self._settle.check(
                self._player.tracks, self._player.commanded,
                self._replay_measured(), elapsed)
        except Exception as exc:
            # A fault in the gate must not be able to stop a run that is
            # otherwise proceeding: fail open, loudly, and keep playing.
            self._log(f"Discrepancy hold disabled after an error: "
                      f"{type(exc).__name__}: {exc}")
            self._settle.enabled = False
            holding, was_holding = False, self._settle.holding
        if holding:
            self._replay_held_s += elapsed
        position = min(max(0.0, now - self._replay_started_at
                           - self._replay_held_s), duration)

        try:
            # Ticked even while holding.  A line being ramped towards the
            # setpoint that is holding things up has to keep moving, or it
            # never arrives, never settles, and the hold never ends.
            done = self._player.tick(position, elapsed)
        except Exception as exc:
            self._log(f"Replay failed: {type(exc).__name__}: {exc}")
            self.stop_replay(reason="aborted after an error")
            self.failed.emit("Replay", f"The replay stopped:\n\n{exc}")
            return

        if holding != was_holding:
            if holding:
                self._log(
                    f"Replay held at {position:.1f} s — {self._settle.reason}. "
                    "The next transition is delayed for every controller until "
                    "the flows settle.")
            elif self._settle.timed_out:
                self._log(f"Replay hold given up: {self._settle.reason}")
            else:
                self._log(f"Replay resumed at {position:.1f} s after "
                          f"{held_before:.1f} s — flows are back within "
                          "tolerance.")
            self.sequence_hold.emit(holding, self._settle.reason)

        self.sequence_progress.emit(position, duration)
        # The player decides when it is done, not the clock: a rate-limited
        # line whose last edge is a step is still on its way there when the
        # position runs out, and ending the replay early would leave the rig
        # somewhere the recording never says it should be.
        if not done:
            return
        # A wrap is itself a transition, so a held clock delays it too.
        if holding:
            return
        if self._player.next_cycle():
            # Restart the clock rather than accumulating an offset, so a pass
            # that ran long waiting for a rate-limited line does not shorten
            # the next one.
            self._replay_started_at = time.monotonic()
            self._replay_last_tick = self._replay_started_at
            self._replay_held_s = 0.0
            total = self._player.repeats
            self._log(f"Replay pass {self._player.cycle}"
                      + (f" of {total}" if total else " (repeating until "
                         "stopped)") + " started.")
            self.sequence_progress.emit(0.0, duration)
            self.sequence_cycle.emit(self._player.cycle, total)
            return
        self.stop_replay(reason="finished")

    # ==================================================================== #
    #  CSV logging                                                         #
    # ==================================================================== #

    @property
    def logging_active(self):
        return self._csv.active

    @property
    def log_path(self):
        return self._csv.path

    def resolve_log_path(self, raw, base_dir, source="Manual"):
        return resolve_path(
            raw, fallback=DEFAULT_LOG_NAME, base_dir=base_dir, source=source)

    def start_logging(self, path, *, source="Manual"):
        """Open ``path`` and begin appending one row per polling pass."""
        if self._csv.active:
            return False
        try:
            with self._csv_lock:
                actual = self._csv.start(
                    Path(path), self.selection, source=source, mexa=True)
        except Exception as exc:
            self.failed.emit("Logging", f"Could not open the log file:\n\n{exc}")
            return False
        self._log(f"Logging started ({source}): {actual}")
        self.logging_changed.emit(True, actual)
        return True

    def stop_logging(self):
        if not self._csv.active:
            return None
        with self._csv_lock:
            path = self._csv.stop()
        self._log(f"Logging stopped: {path}")
        self.logging_changed.emit(False, path)
        return path

    def _write_log_row(self, pass_samples, timestamp):
        """Called on the serial thread, once per pass."""
        if not self._csv.active:
            return
        phi_values = self.phi_values(pass_samples)
        with self._csv_lock:
            self._csv.write_row(pass_samples, phi_values, timestamp,
                                mexa=self.mexa.csv_snapshot(timestamp))

    # ==================================================================== #
    #  LabVIEW UDP triggers                                                #
    # ==================================================================== #

    def start_udp(self, host, port):
        self._udp.start(host, int(port))

    def stop_udp(self):
        self._udp.stop()
        self.udp_changed.emit(False, "LabVIEW listener stopped.")

    def _on_udp_ready(self, host, port):
        self._log(f"LabVIEW listener ready on {host}:{port}.")
        self.udp_changed.emit(True, f"Listening on {host}:{port}")

    def _on_udp_error(self, error, host, port):
        self._log(f"LabVIEW listener error on {host}:{port}: {error}")
        self.udp_changed.emit(False, f"Listener error: {error}")

    def _announce_udp(self, note):
        """Report what a datagram did without losing where we are listening.

        The status line is one field, and it is the only place the operator
        can see that the socket is still open.  Replacing it with "Command:
        log" says what just happened and then leaves the screen unable to say
        whether anything is still listening, so both are said at once.
        """
        self.udp_changed.emit(
            self._udp.listening,
            f"Listening on {self._udp.host}:{self._udp.port} · {note}")

    def _on_udp_command(self, command):
        """Act on a datagram.  Announcing one is not the same as obeying it.

        ``log`` and ``stop`` exist so that one operator action starts both
        systems' records at the same instant; a listener that only wrote a
        line in the syslog would leave the LabVIEW record with no counterpart
        to line up against, which is the whole point of accepting the trigger.
        """
        self._log(f"LabVIEW command received: {command}")
        if command == 'log':
            self._udp_start_logging()
        elif command == 'stop':
            self._udp_stop_logging()

    def _udp_start_logging(self):
        if self._csv.active:
            # Not an error, and not a reason to cut the open file short: a
            # repeated trigger is far likelier than an operator wanting the
            # run split in two at an arbitrary instant.
            self._log("LabVIEW: 'log' ignored — a log is already open at "
                      f"{self._csv.path}.")
            self._announce_udp('log ignored, already recording')
            return
        _shown, actual = self.resolve_log_path(
            self.log_destination, self.log_dir, source="LabVIEW")
        if not self.start_logging(actual, source="LabVIEW"):
            self._announce_udp('log failed — see the system log')
            return
        self._announce_udp(f'recording {Path(actual).name}')
        if not self.is_monitoring:
            # The file is open and the header is written, but rows are only
            # produced by a polling pass.  Said plainly here, because from
            # LabVIEW's side the trigger looks like it worked.
            self._log("LabVIEW: the log is open but the monitor is not "
                      "running — no rows will be written until it starts.")

    def _udp_stop_logging(self):
        if not self._csv.active:
            self._log("LabVIEW: 'stop' ignored — nothing is being logged.")
            self._announce_udp('stop ignored, not recording')
            return
        path = self.stop_logging()
        self._announce_udp(f'stopped {Path(path).name}' if path else 'stopped')

    # ==================================================================== #
    #  Shutdown                                                            #
    # ==================================================================== #

    def shutdown(self):
        """Stop everything in the order that leaves the rig safest."""
        self.experiment_plans.shutdown()
        self._ramps.cancel_all()
        self.stop_replay(reason="cancelled at shutdown")
        self._replay_timer.stop()
        if self._recorder.active:
            self.stop_recording()
        self.is_monitoring = False
        self._restart_pending = False
        self.stop_udp()
        self.mexa.shutdown()
        if self._csv.active:
            self.stop_logging()
        future = self._monitor_future
        if future is not None:
            try:
                future.result(timeout=2.0)
            except Exception:
                pass
        self._worker.shutdown()

    @staticmethod
    def _drain(queue):
        while not queue.empty():
            try:
                queue.get_nowait()
            except Exception:
                break
