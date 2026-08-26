"""Allowlisted, audited operations exposed to launched agents."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time
import uuid

from .authority import AgentAuthority, AuthorityError
from ..core.sequence import Sequence, opening_mismatches


DEFAULT_AUDIT_PATH = (
    Path.home() / "Documents" / "Flow Controller" / "agent_audit.jsonl")
READ_METHODS = frozenset((
    "read_snapshot", "read_history", "read_derived_state",
    "list_saved_sequences",
))
DRAFT_METHODS = frozenset(("submit_sequence_draft",))
LIVE_METHODS = frozenset(("set_role_setpoint", "run_saved_sequence"))
ALLOWED_METHODS = READ_METHODS | DRAFT_METHODS | LIVE_METHODS
MIN_READ_INTERVAL_S = 0.1
MAX_AGENT_SEQUENCE_BYTES = 1_000_000
MAX_AGENT_DRAFT_BYTES = 1_000_000
MAX_AGENT_SEQUENCE_TRACKS = 32
MAX_AGENT_SEQUENCE_KEYFRAMES = 5_000
MAX_LISTED_SEQUENCES = 100
SEQUENCE_SUFFIX = ".fcseq.json"


class AgentRequestError(ValueError):
    pass


class AgentAuditLog:
    def __init__(self, path=None):
        override = os.environ.get("FLOW_CONTROLLER_AGENT_AUDIT")
        self.path = Path(path or override or DEFAULT_AUDIT_PATH)

    def write(self, record):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, allow_nan=False, sort_keys=True) + "\n")


class AgentDraftService:
    """Qt-thread service for read, draft, and toggle-authorized operations."""

    def __init__(self, session, *, audit=None, authority=None):
        self.session = session
        self.audit = audit or AgentAuditLog()
        self.authority = authority or AgentAuthority(session)
        self._last_read_at = {}
        self.authority.changed.connect(self._on_authority_changed)

    def preview_live_authority(self):
        return self.authority.preview()

    def set_live_enabled(self, enabled, *,
                         reason="operator changed live-control toggle",
                         expected_envelope=None):
        """Apply the operator's default-off live authority toggle."""
        method = "enable_live_control" if enabled else "disable_live_control"
        previous = self.authority.status()
        if not enabled:
            # Revocation is a safety action and must never depend on disk space
            # or audit-path availability. Record it best-effort after authority
            # is already gone.
            self.authority.revoke(reason)
            result = self.authority.status()
            record = {
                "request_id": str(uuid.uuid4()),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "agent": "operator",
                "method": method,
                "previous": previous,
                "new": result,
                "approval": "operator",
                "result": "accepted",
                "phase": "completed",
            }
            try:
                self.audit.write(record)
            except Exception as exc:
                self.session._log(
                    "Live-authority revocation audit could not be written: "
                    f"{type(exc).__name__}: {exc}")
            return result
        record = {
            "request_id": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "agent": "operator",
            "method": method,
            "previous": previous,
            "new": None,
            "approval": "operator",
            "result": "received",
            "phase": "received",
        }
        try:
            self.audit.write(record)
        except Exception as exc:
            raise AgentRequestError(
                f"Audit log unavailable; authority was not changed: {exc}") from exc
        try:
            result = self.authority.enable(
                expected_envelope=expected_envelope)
            record.update(
                phase="completed", result="accepted", new=result,
                timestamp=datetime.now(timezone.utc).isoformat())
            return result
        except Exception as exc:
            record.update(
                phase="completed", result="refused",
                error=f"{type(exc).__name__}: {exc}")
            if isinstance(exc, AgentRequestError):
                raise
            raise AgentRequestError(str(exc)) from exc
        finally:
            try:
                self.audit.write(record)
            except Exception as exc:
                self.session._log(
                    "Agent audit completion could not be written: "
                    f"{type(exc).__name__}: {exc}")

    def _on_authority_changed(self, enabled, reason):
        state = "ENABLED" if enabled else "disabled"
        self.session._log(f"Live agent control {state}: {reason}.")
        record = {
            "request_id": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "agent": "application",
            "method": "live_authority_changed",
            "previous": None,
            "new": self.authority.status(),
            "approval": "operator" if enabled else "automatic_or_operator",
            "result": "accepted",
            "phase": "completed",
            "reason": str(reason),
        }
        try:
            self.audit.write(record)
        except Exception as exc:
            self.session._log(
                "Live-authority audit could not be written: "
                f"{type(exc).__name__}: {exc}")

    def _audit_before_live_execution(self, record, phase):
        """Persist the full approved action before it can reach the session."""
        execution = dict(
            record, phase=str(phase), result="authorized",
            timestamp=datetime.now(timezone.utc).isoformat())
        try:
            self.audit.write(execution)
        except Exception as exc:
            raise AgentRequestError(
                "Audit log unavailable; the live action was not executed: "
                f"{exc}") from exc

    def _check_read_rate(self, agent_id, method):
        if method not in READ_METHODS:
            return
        now = time.monotonic()
        key = (agent_id, method)
        previous = self._last_read_at.get(key)
        if previous is not None and now - previous < MIN_READ_INTERVAL_S:
            raise AgentRequestError(
                f"{method} is limited to {1.0 / MIN_READ_INTERVAL_S:g} calls/s.")
        self._last_read_at[key] = now

    @staticmethod
    def _draft_size(raw, label):
        try:
            size = len(json.dumps(
                raw, allow_nan=False, separators=(",", ":")).encode("utf-8"))
        except (TypeError, ValueError) as exc:
            raise AgentRequestError(
                f"{label} must be finite JSON data: {exc}") from exc
        if size > MAX_AGENT_DRAFT_BYTES:
            raise AgentRequestError(
                f"{label} exceeds the {MAX_AGENT_DRAFT_BYTES} byte draft limit.")

    def _validate_agent_sequence_payload(self, raw):
        if not isinstance(raw, dict):
            raise AgentRequestError("Sequence draft must be an object.")
        tracks = raw.get("tracks", ())
        if not isinstance(tracks, list):
            raise AgentRequestError("Sequence draft tracks must be a list.")
        if len(tracks) > MAX_AGENT_SEQUENCE_TRACKS:
            raise AgentRequestError(
                "Agent sequence drafts may contain at most "
                f"{MAX_AGENT_SEQUENCE_TRACKS} tracks.")
        keyframe_count = 0
        for track in tracks:
            if not isinstance(track, dict):
                raise AgentRequestError("Every sequence track must be an object.")
            frames = track.get("keyframes", ())
            if not isinstance(frames, list):
                raise AgentRequestError("Sequence keyframes must be lists.")
            keyframe_count += len(frames)
            if keyframe_count > MAX_AGENT_SEQUENCE_KEYFRAMES:
                raise AgentRequestError(
                    "Agent sequence drafts may contain at most "
                    f"{MAX_AGENT_SEQUENCE_KEYFRAMES} total keyframes.")
        self._draft_size(raw, "Sequence draft")

    def _saved_sequence_path(self, name):
        raw_name = str(name or "").strip()
        if (not raw_name or len(raw_name) > 200
                or "/" in raw_name or "\\" in raw_name):
            raise AgentRequestError(
                "Choose a saved sequence by its local name, without a path.")
        filename = (raw_name if raw_name.endswith(SEQUENCE_SUFFIX)
                    else raw_name + SEQUENCE_SUFFIX)
        base = Path(self.session.sequence_dir).resolve()
        candidate = (base / filename).resolve()
        try:
            candidate.relative_to(base)
        except ValueError as exc:
            raise AgentRequestError(
                "Saved sequences must be inside the app sequence directory.") from exc
        if not candidate.is_file():
            raise AgentRequestError(f"Saved sequence not found: {raw_name}")
        try:
            size = candidate.stat().st_size
        except OSError as exc:
            raise AgentRequestError(
                f"Could not inspect saved sequence '{raw_name}': {exc}") from exc
        if size > MAX_AGENT_SEQUENCE_BYTES:
            raise AgentRequestError(
                f"Saved sequence '{raw_name}' exceeds the "
                f"{MAX_AGENT_SEQUENCE_BYTES} byte limit.")
        return candidate

    def _load_saved_sequence(self, name, *, validate_for_rig=True):
        path = self._saved_sequence_path(name)
        try:
            with path.open("r", encoding="utf-8") as handle:
                raw = json.load(handle)
            self._validate_agent_sequence_payload(raw)
            sequence = Sequence.from_dict(raw, path=path)
        except (OSError, TypeError, ValueError) as exc:
            raise AgentRequestError(
                f"Could not load saved sequence '{path.name}': {exc}") from exc
        errors = (self.session.sequence_validation_errors(sequence)
                  if validate_for_rig else [])
        if errors:
            raise AgentRequestError("\n".join(errors))
        canonical = json.dumps(
            raw, sort_keys=True, separators=(",", ":"), allow_nan=False)
        fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return path, sequence, raw, fingerprint

    @staticmethod
    def _sequence_summary(path, sequence, fingerprint):
        return {
            "name": path.name[:-len(SEQUENCE_SUFFIX)],
            "file": path.name,
            "sequence_name": sequence.name,
            "duration_s": sequence.duration,
            "roles": sorted(str(track.key) for track in sequence.tracks),
            "fingerprint": fingerprint,
        }

    def _list_saved_sequences(self):
        base = Path(self.session.sequence_dir)
        try:
            paths = sorted(
                base.glob(f"*{SEQUENCE_SUFFIX}"),
                key=lambda path: path.stat().st_mtime,
                reverse=True)[:MAX_LISTED_SEQUENCES]
        except OSError as exc:
            raise AgentRequestError(
                f"Could not list the saved sequence directory: {exc}") from exc
        entries = []
        for path in paths:
            try:
                loaded_path, sequence, _raw, fingerprint = (
                    self._load_saved_sequence(path.name, validate_for_rig=False))
                entry = self._sequence_summary(
                    loaded_path, sequence, fingerprint)
                errors = self.session.sequence_validation_errors(sequence)
                entry.update(runnable=not errors, errors=list(errors))
            except AgentRequestError as exc:
                entry = {
                    "name": path.name[:-len(SEQUENCE_SUFFIX)],
                    "file": path.name,
                    "runnable": False,
                    "errors": [str(exc)],
                }
            entries.append(entry)
        return {"sequences": entries, "count": len(entries)}

    def _check_sequence_authority(self, sequence):
        if sequence.duration <= 0.0:
            raise AgentRequestError("The saved sequence has no replay duration.")
        for track in sequence.tracks:
            for frame in track.keyframes:
                self.authority.check_role(track.key, frame.value)

    def _opening_mismatches(self, sequence):
        timestamp = self.session._latest_timestamp
        if timestamp is None:
            raise AgentRequestError(
                "No measured telemetry is available for the sequence opening.")
        now = (datetime.now(timestamp.tzinfo)
               if timestamp.tzinfo is not None else datetime.now())
        age = max(0.0, (now - timestamp).total_seconds())
        freshness_s = max(
            1.0, 3.0 * max(0.1, self.session.poll_interval_s))
        if age > freshness_s:
            raise AgentRequestError(
                "Measured telemetry is stale; wait for a fresh monitoring pass.")
        samples = self.session.latest_samples()
        flows = {}
        for track in sequence.tracks:
            unit = self.session.unit_for_role(track.key)
            flows[track.key] = (samples.get(unit, {}) or {}).get("flow")
        return opening_mismatches(sequence, flows)

    @staticmethod
    def _format_opening_mismatches(mismatches):
        return "; ".join(
            f"{track.key}: needs {wanted:g} SLPM, measured {actual:g} SLPM"
            for track, wanted, actual in mismatches)

    def handle(self, agent_id, method, arguments=None):
        agent_id = str(agent_id or "unknown-agent")
        method = str(method)
        started = datetime.now(timezone.utc).isoformat()
        record = {
            "request_id": str(uuid.uuid4()),
            "timestamp": started,
            "agent": agent_id,
            "method": method,
            "previous": None,
            "new": None,
            "approval": "not_required" if method in READ_METHODS else
                        "pending_operator_review",
            "result": "refused",
        }
        received = dict(record, phase="received", result="received")
        # Backpressure happens before synchronous audit I/O so an agent cannot
        # saturate the GUI event loop merely by polling the same read tool.
        self._check_read_rate(agent_id, method)
        try:
            # Record receipt before any read or draft mutation. If the audit
            # destination is unavailable the request is refused fail-closed.
            self.audit.write(received)
        except Exception as exc:
            raise AgentRequestError(
                f"Audit log unavailable; request was not executed: {exc}") from exc
        try:
            arguments = dict(arguments or {})
            if method not in ALLOWED_METHODS:
                raise AgentRequestError(f"Agent method is not allowed: {method}")
            if method == "read_snapshot":
                result = self.session.read_snapshot()
                result["agent_authority"] = self.authority.status()
            elif method == "read_history":
                result = self.session.read_history(
                    window_s=arguments.get("window_s"),
                    units=arguments.get("units"),
                    metric_keys=arguments.get("metrics"))
            elif method == "read_derived_state":
                result = self.session.read_derived_state(
                    duration_s=arguments.get("duration_s", 5.0),
                    tolerance=arguments.get("tolerance", 0.05))
            elif method == "list_saved_sequences":
                result = self._list_saved_sequences()
            elif method == "submit_sequence_draft":
                record["previous"] = (
                    self.session.sequence.name if self.session.sequence else None)
                raw_sequence = arguments.get("sequence")
                self._validate_agent_sequence_payload(raw_sequence)
                sequence = Sequence.from_dict(raw_sequence)
                errors = self.session.sequence_validation_errors(sequence)
                if errors:
                    raise AgentRequestError("\n".join(errors))
                if not self.session.set_sequence(sequence):
                    raise AgentRequestError("The application refused the sequence draft.")
                record["new"] = sequence.name
                result = {
                    "accepted": True,
                    "status": "pending_operator_review",
                    "name": sequence.name,
                    "tracks": len(sequence.tracks),
                }
            elif method == "set_role_setpoint":
                role = str(arguments.get("role", "")).strip()
                value = arguments.get("value")
                role_envelope = self.authority.check_role(role, value)
                unit = role_envelope["unit"]
                previous = self.session._last_sp.get(unit)
                if previous is None:
                    previous = (self.session.latest_samples().get(unit, {}) or {}).get("sp")
                record["previous"] = {
                    "role": role, "unit": unit, "setpoint": previous}
                record["new"] = {
                    "role": role, "unit": unit, "setpoint": float(value)}
                record["approval"] = "live_toggle"
                self._audit_before_live_execution(
                    record, "armed_for_execution")
                # Audit I/O may block on a synced filesystem. Expiry and all
                # live rig invariants therefore get one final check at the
                # session boundary, after the durable record exists.
                self.authority.check_role(role, value)
                if not self.session.set_role_setpoint(role, value):
                    raise AgentRequestError(
                        "The session refused the setpoint.")
                result = {
                    "accepted": True,
                    "status": "queued",
                    "role": role,
                    "unit": unit,
                    "setpoint": float(value),
                }
            else:  # run_saved_sequence
                if self.session.sequence_state != "idle":
                    raise AgentRequestError(
                        "A sequence is already recording or replaying.")
                path, sequence, raw_sequence, fingerprint = (
                    self._load_saved_sequence(arguments.get("name")))
                self._check_sequence_authority(sequence)
                mismatches = self._opening_mismatches(sequence)
                if mismatches:
                    raise AgentRequestError(
                        "The rig does not match the sequence opening: "
                        + self._format_opening_mismatches(mismatches))
                metadata = self._sequence_summary(path, sequence, fingerprint)
                record["previous"] = {
                    "sequence": (self.session.sequence.name
                                 if self.session.sequence else None),
                    "state": self.session.sequence_state,
                }
                record["new"] = {
                    "state": "replaying",
                    "metadata": metadata,
                    "sequence": raw_sequence,
                }
                record["approval"] = "armed_envelope"
                self._audit_before_live_execution(
                    record, "armed_for_execution")
                # Re-read after durable audit I/O, then re-check authority and
                # the measured opening so neither a file edit nor a rig change
                # can slip between validation and dispatch.
                _path, sequence, _raw, current_fingerprint = (
                    self._load_saved_sequence(path.name))
                if current_fingerprint != fingerprint:
                    raise AgentRequestError(
                        "The saved sequence changed before it could start.")
                self._check_sequence_authority(sequence)
                mismatches = self._opening_mismatches(sequence)
                if mismatches:
                    raise AgentRequestError(
                        "The rig moved away from the sequence opening before "
                        "start: " + self._format_opening_mismatches(mismatches))
                if not self.session.start_replay(sequence, repeats=1):
                    raise AgentRequestError(
                        "The application refused to start the saved sequence.")
                result = {
                    "accepted": True,
                    "status": "running",
                    "name": metadata["name"],
                    "fingerprint": fingerprint,
                }
            record["result"] = "accepted"
            return result
        except Exception as exc:
            record["error"] = f"{type(exc).__name__}: {exc}"
            if isinstance(exc, (AgentRequestError, AuthorityError)):
                if isinstance(exc, AuthorityError):
                    raise AgentRequestError(str(exc)) from exc
                raise
            raise AgentRequestError(str(exc)) from exc
        finally:
            record["phase"] = "completed"
            try:
                self.audit.write(record)
            except Exception as exc:
                # The immutable receipt above still proves the request
                # occurred. A completion-write fault must not retroactively
                # turn an already-adopted draft into an apparent refusal.
                self.session._log(
                    "Agent audit completion could not be written: "
                    f"{type(exc).__name__}: {exc}")
