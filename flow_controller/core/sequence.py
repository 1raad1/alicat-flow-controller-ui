"""Recorded setpoint sequences: capture, edit, and replay.

A sequence is what the rig was *asked* to do, not what it did.  Recording
watches every setpoint the session commands -- typed, batched, ramped,
ignition, zeroed -- and stores it as a list of keyframes per controller.
Replaying walks the same keyframes back out through the ordinary setpoint queue, so a
replay is subject to every interlock a hand-typed setpoint is subject to: the
zero lock still wins, the monitor loop is still the only thing that writes to
hardware, and an E-STOP still stops it dead.

Nothing here imports Qt or touches a serial port.  The session owns the clock
and the queue; this module owns the shape of the curve.

Two rules are worth stating because they are the ones that are easy to get
wrong:

* A recorded keyframe *holds* until the next one.  That is what actually
  happened -- a setpoint written to a controller does not decay -- so the
  recorded interpolation is ``HOLD``.  An operator who wants a smooth
  transition between two points changes that keyframe to ``LINEAR``, which is
  an edit, not a re-interpretation of the recording.
* Replay rate-limits the controllers that must never see a step change.  A
  ``HOLD`` edge on the pilot or on either air line is spread over
  :data:`MIN_RAMP_S` instead of being written as a jump, and says so in the
  log.  Silently obeying a step there is a pressure transient into a lit
  flame.  Any line can be given its own limit in flow units per second
  (:attr:`Track.ramp_rate`); on the lines above it can only make them slower.
* A replay can be held.  :class:`SettleGate` compares what each line was told
  against what it is reading, and stops the clock for every controller while
  any one of them is too far off, so the next transition is delayed rather
  than laid on top of one that has not finished.
* A repeated replay treats the wrap from the last keyframe back to the first
  as an ordinary edge, not as a fresh start.  The player keeps the values it
  is currently holding across the cycle boundary, so a rate-limited line ramps
  down to the opening value the same way it would ramp anywhere else.  Priming
  again at the wrap would write that jump straight out.
"""

from __future__ import annotations

import json
import math
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

#: On-disk marker, so a file that is not one of ours is rejected by name
#: rather than by the first ``KeyError`` it happens to raise.
FILE_FORMAT = "flow-controller-sequence"
FILE_VERSION = 1

#: How a keyframe leaves itself, towards the next one.
HOLD = "hold"
LINEAR = "linear"

#: Shortest time a rate-limited track is allowed to take over a step edge.
MIN_RAMP_S = 1.0

#: Player tick.  Fast enough that a 1 s ramp gets ten intermediate points,
#: slow enough that a ten-channel sequence adds ~100 queue items per second
#: in the worst case -- and the deadband below usually cuts that by an order
#: of magnitude.
TICK_S = 0.1

#: A track's value has to move by this fraction of its own span before a new
#: setpoint is worth sending, floored so a small-span track still resolves.
DEADBAND_FRACTION = 0.002
DEADBAND_FLOOR = 0.001

#: How far a line may sit from a sequence's *opening* setpoint before starting
#: that sequence would be a jump rather than a continuation: a fraction of the
#: track's span, floored in flow units.  Much wider than the replay deadband
#: above, which exists to keep the queue quiet -- this one is answering "is the
#: rig standing where this recording began", and a controller holding steady
#: within a couple of percent of its setpoint is standing there.
START_TOLERANCE = 0.02
START_FLOOR = 0.05

#: How far a measured flow may sit from what was commanded before the replay
#: clock is held: a fraction of the track's span, floored in flow units so a
#: small-span line is judged against instrument noise rather than against a
#: tolerance narrower than it can resolve.
SETTLE_TOLERANCE = 0.05
SETTLE_FLOOR = 0.05

#: Longest the clock may be held in one go.  A line that never arrives -- a
#: closed hand valve, a supply that has run out -- must not be able to freeze
#: a run indefinitely; the gate gives up, says so loudly, and lets the
#: sequence continue, because deciding what to do about a controller that
#: cannot follow is the operator's call and not the player's.
SETTLE_MAX_HOLD_S = 30.0


def _clean(value):
    """A finite, non-negative float.  Flows below zero are not commandable."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number) or number < 0.0:
        return 0.0
    return number


@dataclass(frozen=True)
class Keyframe:
    """One commanded value at one instant, and how it leaves itself."""

    t: float
    value: float
    interp: str = HOLD

    def to_dict(self):
        return {'t': round(self.t, 4), 'v': round(self.value, 6),
                'i': self.interp}

    @classmethod
    def from_dict(cls, raw):
        interp = raw.get('i', HOLD)
        return cls(t=max(0.0, float(raw.get('t', 0.0))),
                   value=_clean(raw.get('v', 0.0)),
                   interp=interp if interp in (HOLD, LINEAR) else HOLD)


@dataclass
class Track:
    """One controller's whole story, as a sorted list of keyframes.

    ``key`` is the role the controller was serving when the sequence was
    recorded (``'nh3_rich'``, or ``'custom_A'`` for a controller outside the
    RQL roles).  ``unit`` is only a note of which address that was on the day:
    replay re-resolves the role against the *current* assignment, so a
    sequence recorded with ammonia on unit A still runs when stage-1 ammonia
    has been moved to unit D.
    """

    key: str
    label: str
    gas: str = ""
    unit: str | None = None
    keyframes: list[Keyframe] = field(default_factory=list)
    #: Largest change this line may make per second during replay, in the
    #: controller's own flow units, or ``None`` for the default (see
    #: :meth:`SequencePlayer.rate_for`).  A rate rather than a ramp *time*
    #: because a time-per-span silently changes the actual slew rate whenever
    #: a keyframe is edited, and the slew rate is the thing the burner feels.
    ramp_rate: float | None = None

    # -- reading ---------------------------------------------------------- #

    def sorted_frames(self):
        return sorted(self.keyframes, key=lambda frame: frame.t)

    def value_at(self, t):
        """The commanded value at ``t``, held before the first keyframe."""
        frames = self.sorted_frames()
        if not frames:
            return 0.0
        if t <= frames[0].t:
            return frames[0].value
        if t >= frames[-1].t:
            return frames[-1].value
        for left, right in zip(frames, frames[1:]):
            if left.t <= t <= right.t:
                if left.interp != LINEAR or right.t <= left.t:
                    return left.value
                fraction = (t - left.t) / (right.t - left.t)
                return left.value + (right.value - left.value) * fraction
        return frames[-1].value

    @property
    def span(self):
        """Largest value on the track, used for scaling and the deadband."""
        return max((frame.value for frame in self.keyframes), default=0.0)

    @property
    def duration(self):
        return max((frame.t for frame in self.keyframes), default=0.0)

    def samples(self, step=0.05):
        """``(times, values)`` dense enough to draw, sparse enough to be cheap.

        Every keyframe is emitted, plus a pair either side of each HOLD edge
        so the step renders as a step rather than as a diagonal.
        """
        frames = self.sorted_frames()
        if not frames:
            return [], []
        times, values = [], []
        for index, frame in enumerate(frames):
            times.append(frame.t)
            values.append(frame.value)
            if index + 1 >= len(frames):
                break
            following = frames[index + 1]
            if frame.interp != LINEAR:
                # Hold flat right up to the next keyframe, then step.
                times.append(following.t)
                values.append(frame.value)
            elif step > 0:
                gap = following.t - frame.t
                count = int(gap / step)
                for point in range(1, min(count, 200)):
                    at = frame.t + point * step
                    times.append(at)
                    values.append(self.value_at(at))
        return times, values

    # -- editing ---------------------------------------------------------- #

    def add(self, t, value, interp=None):
        """Insert a keyframe, replacing one already at that instant."""
        t = max(0.0, float(t))
        if interp is None:
            interp = self._interp_at(t)
        self.keyframes = [frame for frame in self.keyframes
                          if abs(frame.t - t) > 1e-6]
        self.keyframes.append(Keyframe(t=t, value=_clean(value), interp=interp))
        self.keyframes.sort(key=lambda frame: frame.t)
        return self.index_at(t)

    def _interp_at(self, t):
        """Inherit the interpolation of the segment being split."""
        previous = [frame for frame in self.sorted_frames() if frame.t <= t]
        return previous[-1].interp if previous else HOLD

    def index_at(self, t):
        for index, frame in enumerate(self.sorted_frames()):
            if abs(frame.t - t) <= 1e-6:
                return index
        return -1

    def move(self, index, t, value):
        """Drag a keyframe.  It cannot be dragged past its neighbours."""
        frames = self.sorted_frames()
        if not 0 <= index < len(frames):
            return None
        low = frames[index - 1].t + 1e-3 if index > 0 else 0.0
        high = (frames[index + 1].t - 1e-3 if index + 1 < len(frames)
                else max(t, frames[-1].t))
        moved = replace(frames[index],
                        t=min(max(float(t), low), max(low, high)),
                        value=_clean(value))
        frames[index] = moved
        self.keyframes = frames
        return moved

    def set_interp(self, index, interp):
        frames = self.sorted_frames()
        if not 0 <= index < len(frames):
            return False
        frames[index] = replace(frames[index],
                                interp=LINEAR if interp == LINEAR else HOLD)
        self.keyframes = frames
        return True

    def set_ramp_rate(self, rate):
        """Set the per-second limit for this line, or clear it.

        ``None``, zero and anything unusable all mean *clear*: fall back to
        the default for this role.  There is deliberately no way to say
        "faster than the default" for a line that must never step -- the
        player clamps that -- so the only thing an operator can do here is
        slow a line down.
        """
        try:
            value = None if rate is None else float(rate)
        except (TypeError, ValueError):
            value = None
        if value is None or not math.isfinite(value) or value <= 0.0:
            self.ramp_rate = None
        else:
            self.ramp_rate = value
        return self.ramp_rate

    def remove(self, index):
        """Delete a keyframe.  The first one is structural and stays."""
        frames = self.sorted_frames()
        if not 0 < index < len(frames):
            return False
        del frames[index]
        self.keyframes = frames
        return True

    # -- persistence ------------------------------------------------------ #

    def to_dict(self):
        data = {'key': self.key, 'label': self.label, 'gas': self.gas,
                'unit': self.unit,
                'keyframes': [frame.to_dict() for frame in self.sorted_frames()]}
        # Written only when set, so the file format stays additive: a v1 file
        # from before per-line ramps loads with ``ramp_rate`` at its default.
        if self.ramp_rate is not None:
            data['ramp'] = round(self.ramp_rate, 6)
        return data

    @classmethod
    def from_dict(cls, raw):
        track = cls(key=str(raw['key']),
                    label=str(raw.get('label', raw['key'])),
                    gas=str(raw.get('gas', '')), unit=raw.get('unit'),
                    keyframes=[Keyframe.from_dict(frame)
                               for frame in raw.get('keyframes', ())])
        track.set_ramp_rate(raw.get('ramp'))
        return track


@dataclass
class Sequence:
    """A named set of tracks plus the markers the operator dropped in it."""

    name: str = "sequence"
    mode: str = "staged"
    created: str = ""
    notes: str = ""
    tracks: list[Track] = field(default_factory=list)
    markers: list[float] = field(default_factory=list)
    path: Path | None = None

    def track(self, key):
        return next((track for track in self.tracks if track.key == key), None)

    @property
    def duration(self):
        return max((track.duration for track in self.tracks), default=0.0)

    @property
    def span(self):
        return max((track.span for track in self.tracks), default=0.0)

    def values_at(self, t):
        return {track.key: track.value_at(t) for track in self.tracks}

    def bind(self, resolve_unit):
        """``(bound, missing)`` for the assignment in force right now.

        ``resolve_unit`` is normally ``FlowSession.unit_for_role``.  A track
        whose role is not assigned is *missing*, not silently skipped: a
        sequence that half-runs is worse than one that refuses.
        """
        bound, missing = {}, []
        for track in self.tracks:
            unit = resolve_unit(track.key)
            if unit:
                bound[track.key] = unit
            else:
                missing.append(track)
        return bound, missing

    # -- persistence ------------------------------------------------------ #

    def to_dict(self):
        return {
            'format': FILE_FORMAT,
            'version': FILE_VERSION,
            'name': self.name,
            'mode': self.mode,
            'created': self.created or datetime.now().isoformat(timespec='seconds'),
            'notes': self.notes,
            'duration': round(self.duration, 4),
            'markers': [round(marker, 4) for marker in sorted(self.markers)],
            'tracks': [track.to_dict() for track in self.tracks],
        }

    @classmethod
    def from_dict(cls, raw, path=None):
        if raw.get('format') != FILE_FORMAT:
            raise ValueError("Not a flow-controller sequence file.")
        if int(raw.get('version', 0)) > FILE_VERSION:
            raise ValueError(
                f"Sequence file version {raw.get('version')} is newer than "
                f"this application understands (v{FILE_VERSION}).")
        return cls(
            name=str(raw.get('name', 'sequence')),
            mode=str(raw.get('mode', 'staged')),
            created=str(raw.get('created', '')),
            notes=str(raw.get('notes', '')),
            tracks=[Track.from_dict(track) for track in raw.get('tracks', ())],
            markers=[float(marker) for marker in raw.get('markers', ())],
            path=Path(path) if path else None)

    def save(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('w', encoding='utf-8') as handle:
            json.dump(self.to_dict(), handle, indent=2)
        self.path = path
        return path

    @classmethod
    def load(cls, path):
        path = Path(path)
        with path.open('r', encoding='utf-8') as handle:
            return cls.from_dict(json.load(handle), path=path)


def opening_mismatches(sequence, flows, *, tolerance=START_TOLERANCE,
                       floor=START_FLOOR):
    """Lines that are not where ``sequence`` expects to begin, worst first.

    ``flows`` is ``{role key: measured flow}``.  Returns
    ``[(track, wanted, actual), ...]``; an empty list means the rig is already
    standing at the opening setpoints, so starting this sequence continues from
    where it is rather than jumping.

    A missing key counts as zero flow.  It is the honest reading for a line
    that is not answering, and a track whose role is not assigned at all is
    caught separately by :meth:`Sequence.bind` -- which refuses the replay
    outright rather than describing it as a mismatch.
    """
    rows = []
    for track in sequence.tracks:
        wanted = track.value_at(0.0)
        actual = _clean(flows.get(track.key))
        allowed = max(float(floor), max(track.span, wanted) * float(tolerance))
        if abs(actual - wanted) > allowed:
            rows.append((track, wanted, actual))
    rows.sort(key=lambda row: abs(row[2] - row[1]), reverse=True)
    return rows


@dataclass(frozen=True)
class TrackMeta:
    """What the session knows about a controller when recording starts."""

    key: str
    label: str
    gas: str = ""
    unit: str | None = None


class SequenceRecorder:
    """Collects commanded setpoints into keyframes.

    ``note`` is called from whichever thread issued the setpoint -- the GUI
    thread for a manual set, a ramp thread for an ignition step -- so the
    keyframe lists are guarded by a lock.  Nothing else here is shared.
    """

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._tracks = {}
        self._markers = []
        self._started_at = None
        self._mode = "staged"

    @property
    def active(self):
        return self._started_at is not None

    @property
    def elapsed(self):
        if self._started_at is None:
            return 0.0
        return max(0.0, self._clock() - self._started_at)

    @property
    def markers(self):
        with self._lock:
            return list(self._markers)

    def start(self, metas, initial=None, *, mode="staged"):
        """Begin at t=0 with a keyframe per track at its current setpoint."""
        initial = initial or {}
        with self._lock:
            self._started_at = self._clock()
            self._markers = []
            self._mode = mode
            self._tracks = {}
            for meta in metas:
                track = Track(key=meta.key, label=meta.label, gas=meta.gas,
                              unit=meta.unit)
                track.keyframes.append(
                    Keyframe(t=0.0, value=_clean(initial.get(meta.key, 0.0)),
                             interp=HOLD))
                self._tracks[meta.key] = track
        return True

    def note(self, key, value, when=None):
        """Record a commanded setpoint.  Unknown or unchanged values are dropped."""
        with self._lock:
            if self._started_at is None:
                return False
            track = self._tracks.get(key)
            if track is None:
                return False
            at = self.elapsed if when is None else max(0.0, float(when))
            value = _clean(value)
            last = track.keyframes[-1] if track.keyframes else None
            if last is not None and abs(last.value - value) < 1e-9:
                return False
            if last is not None and at <= last.t:
                # Two commands inside one clock tick: keep the later value at
                # the same instant rather than inventing an ordering.
                track.keyframes[-1] = replace(last, value=value)
                return True
            track.keyframes.append(Keyframe(t=at, value=value, interp=HOLD))
            return True

    def mark(self, when=None):
        """Drop an anchor on every track at its current held value.

        This is the "key point in between transitions" -- a keyframe the
        operator placed deliberately, so that editing the curve afterwards has
        something to grab at the moment they cared about, not only at the
        moments a setpoint happened to change.
        """
        with self._lock:
            if self._started_at is None:
                return None
            at = self.elapsed if when is None else max(0.0, float(when))
            for track in self._tracks.values():
                held = track.keyframes[-1].value if track.keyframes else 0.0
                if track.keyframes and abs(track.keyframes[-1].t - at) < 1e-6:
                    continue
                track.keyframes.append(
                    Keyframe(t=at, value=held, interp=HOLD))
            self._markers.append(at)
            return at

    def stop(self, name=None):
        """Close the recording and hand back an immutable-enough Sequence."""
        with self._lock:
            if self._started_at is None:
                return None
            at = self.elapsed
            tracks = []
            for track in self._tracks.values():
                # An explicit closing keyframe makes the duration a property
                # of the data rather than of a separate field that can drift.
                if track.keyframes and track.keyframes[-1].t < at:
                    track.keyframes.append(
                        Keyframe(t=at, value=track.keyframes[-1].value,
                                 interp=HOLD))
                tracks.append(track)
            sequence = Sequence(
                name=name or datetime.now().strftime("run_%Y-%m-%d_%H-%M-%S"),
                mode=self._mode,
                created=datetime.now().isoformat(timespec='seconds'),
                tracks=tracks,
                markers=list(self._markers))
            self._started_at = None
            self._tracks = {}
            self._markers = []
            return sequence

    def cancel(self):
        with self._lock:
            self._started_at = None
            self._tracks = {}
            self._markers = []


class SequencePlayer:
    """Walks a sequence back out as setpoints, rate-limiting where it must.

    The player never writes to hardware.  ``emit(unit, value)`` is the
    session's ordinary setpoint queue, which means a replay is stopped by an
    E-STOP, blocked by the zero lock, and dropped for a unit whose connection
    is not open -- exactly like a setpoint the operator typed.

    ``repeats`` is how many passes to make: ``1`` runs the sequence once,
    ``0`` runs it until the operator stops it.  The player counts the passes
    but does not own the clock, so the session drives the wrap by calling
    :meth:`next_cycle` when a pass reports itself finished.
    """

    def __init__(self, sequence, bound, emit, *, log=None,
                 rate_limited=frozenset(), min_ramp_s=MIN_RAMP_S, repeats=1,
                 rate_lookup=None):
        self._sequence = sequence
        self._bound = dict(bound)
        self._emit = emit
        self._log = log if log is not None else (lambda _message: None)
        self._rate_limited = frozenset(rate_limited)
        #: Asked for each track's declared rate, so a replay is paced by the
        #: same figure on the controller's card that paces a typed setpoint.
        self._rate_lookup = rate_lookup
        self._min_ramp_s = max(0.1, float(min_ramp_s))
        self._last_sent = {}
        self._current = {}
        self._position = 0.0
        self._warned = set()
        # Only an explicit 0 means endless.  An unspecified repeat count has to
        # fall to a single pass: the conservative reading of "unspecified" on a
        # rig that is burning something is not "keep going".
        self._repeats = 1 if repeats is None else max(0, int(repeats))
        self._cycle = 1

    @property
    def position(self):
        return self._position

    @property
    def duration(self):
        return self._sequence.duration

    @property
    def bound(self):
        """``{track key: unit}`` for the tracks this replay is driving."""
        return dict(self._bound)

    @property
    def tracks(self):
        """The tracks of the sequence being played."""
        return list(self._sequence.tracks)

    @property
    def commanded(self):
        """The value each track is currently holding, after rate limiting.

        This -- not ``value_at(position)`` -- is what the rig has actually been
        told, so it is what a discrepancy has to be measured against.
        """
        return dict(self._current)

    @property
    def repeats(self):
        """Passes requested; ``0`` means until stopped."""
        return self._repeats

    @property
    def cycle(self):
        """Which pass is running, counting from 1."""
        return self._cycle

    @property
    def endless(self):
        return self._repeats == 0

    @property
    def cycles_left(self):
        """Passes still to come after this one; ``None`` when endless."""
        if self.endless:
            return None
        return max(0, self._repeats - self._cycle)

    @property
    def finished(self):
        """The current pass is past the end *and* every line delivered there.

        This is the end of a pass, not of the replay: with ``repeats`` set it
        is the cue to call :meth:`next_cycle`, which answers whether the replay
        itself is over.

        The clock alone is not enough.  A rate-limited line whose last edge
        is a step is still travelling when the position reaches the end, and
        calling that finished would leave the rig short of the state the
        recording ends in -- silently, which is the worst way to be wrong
        about a flow.  The limiter closes a fixed amount every tick, so this
        settles in bounded time -- ``min_ramp_s`` after the end by default, or
        however long the line's own ``ramp_rate`` asks for -- rather than
        holding the replay open indefinitely.
        """
        if self._position < self._sequence.duration:
            return False
        for track in self._sequence.tracks:
            if track.key not in self._bound:
                continue
            target = track.value_at(self._sequence.duration)
            current = self._current.get(track.key, target)
            deadband = max(DEADBAND_FLOOR, track.span * DEADBAND_FRACTION)
            if abs(target - current) > deadband:
                return False
        return True

    def prime(self):
        """Send the t=0 values, so the run starts from a defined state."""
        self._position = 0.0
        for track in self._sequence.tracks:
            if track.key not in self._bound:
                continue
            value = track.value_at(0.0)
            self._current[track.key] = value
            self._send(track, value, force=True)

    def next_cycle(self):
        """Start another pass, or report that there are none left.

        Deliberately *not* a second :meth:`prime`.  Priming forces the opening
        values out regardless of the rate limiter, which is right at the start
        of a run -- the rig is wherever it was and the operator has asked to go
        to the beginning -- but wrong at a wrap, where the previous pass has
        just left every line at its closing value.  Forcing there would write
        the end-to-start jump directly to the pilot and the air lines.  Keeping
        ``_current`` instead makes the wrap an edge like any other, so
        :meth:`_limited` spreads it over ``min_ramp_s``.

        ``_warned`` and ``_last_sent`` are kept for the same reason they exist:
        one warning per line per replay, not one per line per pass, and a
        deadband measured against what was actually last sent.
        """
        if not self.endless and self._cycle >= self._repeats:
            return False
        self._cycle += 1
        self._position = 0.0
        return True

    def tick(self, position, elapsed):
        """Advance to ``position``; ``elapsed`` is the wall time since the last tick."""
        self._position = max(0.0, float(position))
        elapsed = max(1e-3, float(elapsed))
        for track in self._sequence.tracks:
            if track.key not in self._bound:
                continue
            wanted = track.value_at(self._position)
            value = self._limited(track, wanted, elapsed)
            self._current[track.key] = value
            self._send(track, value)
        return self.finished

    def declared_rate(self, track):
        """The rate the operator has asked this line to move at, or ``None``.

        The controller's own declared rate wins, because that is the figure the
        operator can see on its card and the one that paces a typed setpoint.
        A rate carried on the track is the older per-sequence form of the same
        idea and stands in when the controller has none, so a sequence recorded
        before the rate moved onto the card still replays the way it was saved.
        """
        if self._rate_lookup is not None:
            try:
                rate = self._rate_lookup(track)
            except Exception:
                # A lookup that cannot answer must not stop a replay; the
                # track's own rate, or the default, is a safe answer.
                rate = None
            if rate is not None:
                return rate
        return track.ramp_rate

    def rate_for(self, track):
        """Largest change per second allowed on ``track``, ``None`` if free.

        Three cases, in the order they matter:

        * A line that must never step (``rate_limited``) always has a limit.
          Unset, it is the span spread over ``min_ramp_s``; set, it is the
          operator's rate but never *faster* than that default.  The operator
          can slow the pilot down; they cannot switch the protection off.
        * Any other line with a rate set gets exactly that rate, which is how
          a controller that was recorded stepping can be replayed as a ramp.
        * Everything else is unlimited, which is what the recording says
          happened: a setpoint was written and the controller went there.
        """
        must_ramp = track.key in self._rate_limited
        default = (track.span or 1.0) / self._min_ramp_s if must_ramp else None
        rate = self.declared_rate(track)
        if rate is None:
            return default
        rate = float(rate)
        if not math.isfinite(rate) or rate <= 0.0:
            return default
        return min(rate, default) if must_ramp else rate

    def _limited(self, track, wanted, elapsed):
        """Cap the change a rate-limited track is allowed to make this tick."""
        rate = self.rate_for(track)
        if rate is None:
            return wanted
        current = self._current.get(track.key, wanted)
        allowance = rate * elapsed
        change = wanted - current
        if abs(change) <= allowance:
            return wanted
        if track.key not in self._warned:
            self._warned.add(track.key)
            reason = ("this line must not see an instantaneous change"
                      if track.key in self._rate_limited
                      else "its ramp limit is set")
            self._log(f"{track.label}: step edge spread at {rate:.3g}/s "
                      f"— {reason}.")
        return current + math.copysign(allowance, change)

    def _send(self, track, value, *, force=False):
        deadband = max(DEADBAND_FLOOR, track.span * DEADBAND_FRACTION)
        previous = self._last_sent.get(track.key)
        if not force and previous is not None and abs(previous - value) < deadband:
            return False
        self._last_sent[track.key] = value
        self._emit(self._bound[track.key], value)
        return True


class SettleGate:
    """Holds the replay clock while any line is still far from its setpoint.

    A replay commands setpoints; it cannot know from the sequence alone whether
    the rig arrived at them.  If a controller is a long way from what it was
    told -- slewing slowly, starved of supply, valve saturated -- then walking
    on to the next keyframe stacks a second transition on top of an unfinished
    one, and the mixture between the two is nothing that was ever recorded.

    So the gate holds *the clock*, for *every* controller, when *any one* of
    them is out of tolerance.  Holding one line and letting the others advance
    would be worse than not holding at all: what a sequence describes is the
    relationship between the lines, and a global hold is the only thing that
    preserves it.  Holding the clock also means the position stops advancing
    while the setpoints already commanded stay commanded -- rate-limited lines
    keep travelling towards them -- so the next transition is *delayed*, never
    skipped.

    Nothing here reads hardware.  The session passes in the measured flows it
    already has from the monitor loop; this class only does the arithmetic and
    remembers how long it has been waiting.
    """

    def __init__(self, *, tolerance=SETTLE_TOLERANCE, floor=SETTLE_FLOOR,
                 max_hold_s=SETTLE_MAX_HOLD_S, enabled=True):
        self.enabled = bool(enabled)
        self._tolerance = max(0.0, float(tolerance))
        self._floor = max(0.0, float(floor))
        self._max_hold_s = max(0.0, float(max_hold_s))
        self._holding = False
        self._reason = ""
        self._held_s = 0.0
        self._total_held_s = 0.0
        self._timed_out = False

    @property
    def holding(self):
        return self._holding

    @property
    def tolerance(self):
        """Fraction of a track's span allowed as error."""
        return self._tolerance

    @tolerance.setter
    def tolerance(self, value):
        self._tolerance = max(0.0, float(value))

    @property
    def reason(self):
        """Which line is holding things up, in words fit for the log."""
        return self._reason

    @property
    def held_s(self):
        """Length of the hold in progress."""
        return self._held_s

    @property
    def total_held_s(self):
        """Everything this gate has held the clock for, across the whole run."""
        return self._total_held_s

    @property
    def timed_out(self):
        return self._timed_out

    def tolerance_for(self, track):
        """How far ``track`` may be off before it counts as a discrepancy."""
        return max(self._floor, track.span * self._tolerance)

    def check(self, tracks, commanded, measured, elapsed=0.0):
        """One tick's worth of readings; answer whether to hold the clock.

        ``commanded`` is what each track has been told (``player.commanded``),
        ``measured`` the flow read back.  A track missing from either -- not
        bound, or no sample yet this pass -- is not judged: a reading that has
        not arrived is not evidence of a discrepancy, and treating it as one
        would stall every replay for the first tick or two.
        """
        if not self.enabled:
            return self._release()

        worst = None
        for track in tracks:
            want = commanded.get(track.key)
            have = measured.get(track.key)
            if want is None or have is None:
                continue
            error = abs(float(have) - float(want))
            if error <= self.tolerance_for(track):
                continue
            if worst is None or error > worst[0]:
                worst = (error, track.label, float(want), float(have))
        if worst is None:
            return self._release()

        _error, label, want, have = worst
        if self._timed_out:
            # Already given up on this excursion.  Stay out of the way until
            # the flows come back inside tolerance, which re-arms the gate.
            return False

        self._held_s += max(0.0, float(elapsed))
        if self._max_hold_s and self._held_s >= self._max_hold_s:
            self._timed_out = True
            self._holding = False
            self._reason = (
                f"{label} is still reading {have:.2f} against {want:.2f} "
                f"commanded after {self._max_hold_s:g} s — the replay is being "
                "allowed to continue; check that line.")
            return False

        self._total_held_s += max(0.0, float(elapsed))
        self._holding = True
        self._reason = (f"{label} reading {have:.2f} against {want:.2f} "
                        "commanded")
        return True

    def _release(self):
        """Back inside tolerance: stop holding and re-arm for the next time."""
        self._holding = False
        self._reason = ""
        self._held_s = 0.0
        self._timed_out = False
        return False
