"""Toggle-controlled, fail-closed authority for AI live commands.

The authority object deliberately owns no command path.  It only freezes the
operator-approved limits and assignments into an envelope which the MCP
service must check immediately before it calls the existing session boundary.
Any relevant rig change revokes the envelope rather than trying to amend live
authority underneath an agent.
"""

from __future__ import annotations

import hashlib
import json
import math

from PySide6.QtCore import QObject, Signal

from ..core.sequence import Sequence


class AuthorityError(ValueError):
    """A requested agent authority or live command is not safe to accept."""


class AgentAuthority(QObject):
    """An operator-enabled snapshot of live command authority."""

    changed = Signal(bool, str)

    def __init__(self, session, parent=None, *, clock=None):
        super().__init__(parent)
        self.session = session
        self._enabled = False
        self._envelope = None
        self._armed_plan = None
        self._armed_sequences = {}
        self._plan_consumed = False

        session.connection_changed.connect(self._on_connection_changed)
        session.monitoring_changed.connect(self._on_monitoring_changed)
        session.assignments_changed.connect(
            lambda _assignments: self._revoke_if_enabled("assignments changed"))
        session.max_flow_changed.connect(
            lambda _unit, _value: self._revoke_if_enabled("maximum flow changed"))
        session.unit_ramp_changed.connect(
            lambda _unit, _value: self._revoke_if_enabled("ramp settings changed"))
        session.communication_fault.connect(
            lambda detail: self._revoke_if_enabled(
                f"communication fault: {detail}"))
        session.experiment_plans.plan_changed.connect(
            lambda _plan: self._revoke_if_enabled("loaded plan changed"))

    # -- operator-facing lifecycle ------------------------------------- #

    def preview(self, plan=None):
        """Build the JSON-safe authority envelope without enabling it."""
        permitted, excluded = self._capture_roles()
        envelope = {
            "roles": permitted,
            "excluded_roles": excluded,
            "plan": None,
        }
        if plan is not None:
            metadata = self._plan_metadata(plan)
            unavailable = sorted(
                set(metadata["command_roles"]) - set(permitted))
            if unavailable:
                raise AuthorityError(
                    "Plan commands roles outside the authority envelope: "
                    + ", ".join(unavailable) + ".")
            envelope["plan"] = metadata
        # Round-trip through the strict encoder both proves this really is a
        # JSON envelope and gives callers a copy they cannot use to mutate us.
        return self._json_copy(envelope)

    def enable(self, plan=None, *, expected_envelope=None):
        """Enable one envelope until explicitly or automatically revoked."""
        if not self.session.controllers_connected:
            raise AuthorityError("Controllers must be connected before enabling agents.")
        if not self.session.is_monitoring:
            raise AuthorityError("Monitoring must be running before enabling agents.")
        envelope = self.preview(plan)
        if (expected_envelope is not None
                and self._json_copy(expected_envelope) != envelope):
            raise AuthorityError(
                "The rig or loaded plan changed after the authority preview; "
                "review the new envelope before enabling live control.")
        if not envelope["roles"]:
            raise AuthorityError(
                "No assigned role has both a positive MAX FLOW and ramp rate.")

        # Capture every referenced sequence into the same immutable bundle
        # that is being armed. If a file changed between the operator preview
        # and this capture, refuse the enable instead of arming mixed state.
        armed_plan = None
        armed_sequences = {}
        if plan is not None:
            current_metadata, armed_plan, armed_sequences = (
                self._plan_material(plan))
            if current_metadata != envelope["plan"]:
                raise AuthorityError(
                    "The loaded plan or a referenced sequence changed after "
                    "the authority preview; review it again before enabling.")

        self._envelope = self._json_copy(envelope)
        self._armed_plan = (
            self._json_copy(armed_plan) if plan is not None else None)
        self._armed_sequences = self._json_copy(armed_sequences)
        self._plan_consumed = False
        self._enabled = True
        self.changed.emit(True, "enabled")
        return self._json_copy(envelope)

    def revoke(self, reason="revoked"):
        """Return to the default-off state and discard the frozen envelope."""
        changed = self._enabled
        self._enabled = False
        self._envelope = None
        self._armed_plan = None
        self._armed_sequences = {}
        self._plan_consumed = False
        if changed:
            self.changed.emit(False, str(reason))
        return changed

    def status(self):
        """Return a JSON-safe account of current toggle authority."""
        return self._json_copy({
            "enabled": self._enabled,
            "envelope": self._envelope if self._enabled else None,
        })

    # -- immediate command checks -------------------------------------- #

    def check_role(self, role, value):
        """Validate one command against both the frozen and current rig state."""
        self._require_current_envelope()
        role = str(role)
        role_envelope = self._envelope["roles"].get(role)
        if role_envelope is None:
            raise AuthorityError(f"Role '{role}' is not in the authority envelope.")
        number = self._number(value, "Setpoint")
        if number < 0.0:
            raise AuthorityError("Setpoint cannot be negative.")
        if number > role_envelope["max_flow"]:
            raise AuthorityError(
                f"Setpoint for '{role}' exceeds the armed MAX FLOW of "
                f"{role_envelope['max_flow']:g} SLPM.")
        return self._json_copy(role_envelope)

    def check_armed_plan(self, plan):
        """Validate the exact plan armed by the operator, without consuming it."""
        self._require_current_envelope()
        armed = self._envelope.get("plan")
        if armed is None:
            raise AuthorityError("No experiment plan is armed for agent start.")
        if self._plan_consumed:
            raise AuthorityError("The armed experiment plan has already been consumed.")
        current = self._plan_metadata(plan)
        if current["fingerprint"] != armed["fingerprint"]:
            self.revoke("armed plan changed")
            raise AuthorityError(
                "The experiment plan does not match the armed plan; authority "
                "was revoked.")
        if current != armed:
            # The digest is the canonical identity, but this guards programming
            # errors if metadata evolves without being covered by the digest.
            self.revoke("armed plan metadata changed")
            raise AuthorityError(
                "The experiment plan metadata no longer matches; authority "
                "was revoked.")
        return self._json_copy(armed)

    def consume_plan(self, plan):
        """Consume the one-shot plan start permission and return its metadata."""
        metadata, _plan, _sequences = self.consume_plan_bundle(plan)
        return metadata

    def consume_plan_bundle(self, plan):
        """Consume and return the exact plan metadata and frozen sequences."""
        metadata = self.check_armed_plan(plan)
        self._plan_consumed = True
        return (
            metadata,
            self._json_copy(self._armed_plan),
            self._json_copy(self._armed_sequences),
        )

    # -- envelope construction and revalidation ------------------------ #

    def _capture_roles(self):
        assigned = {
            str(role): str(unit)
            for role, unit in self.session.assignments.items() if unit
        }
        assigned.update({
            str(role): str(unit)
            for unit, role in self.session.custom_assignments.items() if role
        })
        permitted = {}
        excluded = []
        for role in sorted(assigned):
            unit = assigned[role]
            maximum = self._positive_or_none(self.session.max_flow_for(unit))
            ramp = self._positive_or_none(self.session.ramp_rate_for(unit))
            if (maximum is None or ramp is None
                    or self.session.ramp_disabled_for(unit)):
                excluded.append(role)
                continue
            permitted[role] = {
                "unit": unit,
                "max_flow": maximum,
                "ramp_rate": ramp,
            }
        return permitted, excluded

    def _require_current_envelope(self):
        if not self._enabled or self._envelope is None:
            raise AuthorityError("Live agent authority is disabled.")
        if not self.session.controllers_connected:
            self.revoke("controllers disconnected")
            raise AuthorityError("Controllers are disconnected; authority was revoked.")
        if not self.session.is_monitoring:
            self.revoke("monitoring stopped")
            raise AuthorityError("Monitoring stopped; authority was revoked.")
        permitted, excluded = self._capture_roles()
        if (permitted != self._envelope["roles"]
                or excluded != self._envelope["excluded_roles"]):
            self.revoke("rig authority envelope changed")
            raise AuthorityError("Rig assignments or limits changed; authority was revoked.")

    def _plan_metadata(self, plan):
        metadata, _plan, _sequences = self._plan_material(plan)
        return metadata

    def _plan_material(self, plan):
        try:
            raw = plan.to_dict()
        except (AttributeError, TypeError, ValueError) as exc:
            raise AuthorityError(f"Invalid experiment plan: {exc}") from exc

        command_roles = set()
        sequence_fingerprints = []
        frozen_sequences = {}
        try:
            for stage in plan.stages:
                command_roles.update(str(role) for role in stage.setpoints)
                if stage.sequence:
                    path = self.session.experiment_plans._resolve_sequence(
                        stage.sequence)
                    sequence = Sequence.load(path)
                    command_roles.update(str(track.key) for track in sequence.tracks)
                    sequence_raw = sequence.to_dict()
                    frozen_sequences[str(stage.sequence)] = sequence_raw
                    sequence_canonical = json.dumps(
                        sequence_raw, sort_keys=True, separators=(",", ":"),
                        allow_nan=False)
                    sequence_fingerprints.append({
                        "stage": str(stage.name),
                        "reference": str(stage.sequence),
                        "fingerprint": hashlib.sha256(
                            sequence_canonical.encode("utf-8")).hexdigest(),
                    })
        except (AttributeError, OSError, TypeError, ValueError) as exc:
            raise AuthorityError(f"Could not inspect plan commands: {exc}") from exc
        identity = {
            "plan": raw,
            "sequences": sequence_fingerprints,
        }
        canonical = json.dumps(
            identity, sort_keys=True, separators=(",", ":"), allow_nan=False)
        metadata = {
            "name": str(plan.name),
            "fingerprint": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            "command_roles": sorted(command_roles),
            "sequence_fingerprints": sequence_fingerprints,
        }
        return metadata, raw, frozen_sequences

    def _on_connection_changed(self, connected):
        if not connected:
            self._revoke_if_enabled("controllers disconnected")

    def _on_monitoring_changed(self, monitoring):
        if not monitoring:
            self._revoke_if_enabled("monitoring stopped")

    def _revoke_if_enabled(self, reason):
        if self._enabled:
            self.revoke(reason)

    @staticmethod
    def _number(value, label):
        if isinstance(value, bool):
            raise AuthorityError(f"{label} must be a finite number.")
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise AuthorityError(f"{label} must be a finite number.") from exc
        if not math.isfinite(number):
            raise AuthorityError(f"{label} must be a finite number.")
        return number

    @staticmethod
    def _positive_or_none(value):
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number) or number <= 0.0:
            return None
        return number

    @staticmethod
    def _json_copy(value):
        return json.loads(json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False))
