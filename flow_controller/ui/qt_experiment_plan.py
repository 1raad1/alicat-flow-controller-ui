"""Operator controls for reviewed, condition-based experiment plans."""

from pathlib import Path

from PySide6.QtWidgets import (
    QFileDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton,
    QVBoxLayout, QWidget,
)

from ..core.experiment_plan import (
    RUN_ABORTED,
    RUN_AWAITING_OPERATOR,
    RUN_FINISHED,
    RUN_HOLDING,
    RUN_IDLE,
    RUN_RUNNING,
)
from . import qt_theme as theme
from .qt_widgets import StatusDot, label


class ExperimentPlanPane(QWidget):
    """Inline controls for one deterministic automated test sequence."""

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, theme.PAD_SM, 0, 0)
        layout.setSpacing(theme.PAD_SM)

        self._dot = StatusDot(theme.TEXT_DIM)
        self._header_state = label("idle", color=theme.TEXT_DIM, size=8,
                                   monospace=True)
        heading = QHBoxLayout()
        heading.setContentsMargins(0, 0, 0, 0)
        heading.setSpacing(theme.PAD_SM)
        heading.addWidget(label(
            "AUTOMATED TEST SEQUENCE", color=theme.TEXT_DIM, size=7,
            bold=True))
        heading.addStretch(1)
        heading.addWidget(self._dot)
        heading.addWidget(self._header_state)
        layout.addLayout(heading)

        description = label(
            "Run a reviewed sequence of test conditions. Each stage advances "
            "from live flow-meter readings and has a declared timeout and "
            "verified-zero abort action.",
            color=theme.TEXT_MUTED, size=8)
        description.setWordWrap(True)
        layout.addWidget(description)

        self.summary = QLabel("No automated test sequence loaded.")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(theme.PAD_SM)
        self.load_button = QPushButton("Load test sequence…")
        self.load_button.setProperty("variant", "quiet")
        self.load_button.clicked.connect(self._load)
        row.addWidget(self.load_button)
        self.start_button = QPushButton("Review & run test")
        self.start_button.setProperty("variant", "accent")
        self.start_button.clicked.connect(self._review_and_start)
        row.addWidget(self.start_button)
        self.abort_button = QPushButton("Abort test")
        self.abort_button.setProperty("variant", "danger")
        self.abort_button.setToolTip(
            "Stops the test sequence and runs its declared verified-zero abort action.")
        self.abort_button.clicked.connect(self._confirm_abort)
        row.addWidget(self.abort_button)
        self.resolve_button = QPushButton("Resolve test timeout")
        self.resolve_button.setProperty("variant", "quiet")
        self.resolve_button.clicked.connect(
            lambda: self._on_attention(
                self.controller.reason or "The test sequence is waiting for an operator."))
        row.addWidget(self.resolve_button)
        row.addStretch(1)
        layout.addLayout(row)

        self.stage = label("", color=theme.TEXT_MUTED, size=8)
        self.stage.setWordWrap(True)
        layout.addWidget(self.stage)

        controller.plan_changed.connect(self._on_plan)
        controller.state_changed.connect(self._on_state)
        controller.stage_changed.connect(self._on_stage)
        controller.attention_required.connect(self._on_attention)
        self._on_plan(controller.plan)
        self._on_state(controller.state)

    def _load(self):
        start = (self.controller.plan_path.parent
                 if self.controller.plan_path else Path.cwd())
        path, _selected = QFileDialog.getOpenFileName(
            self, "Open automated test sequence", str(start),
            "Automated test sequences (*.fcplan.json *.json);;JSON files (*.json)")
        if path:
            self.controller.load(Path(path))

    def _review_and_start(self):
        plan = self.controller.plan
        if plan is None:
            return
        stages = "\n".join(
            f"  {index}. {stage.name} — timeout {stage.timeout_s:g} s; "
            f"on timeout: {stage.on_timeout}"
            for index, stage in enumerate(plan.stages, start=1))
        answer = QMessageBox.warning(
            self, "Run this automated test sequence?",
            f"Test sequence: {plan.name}\n\n{stages}\n\n"
            f"Abort procedure: {plan.abort.action}\n\n"
            "Starting commands the connected rig. Confirm the declared command "
            "ceilings and keep independent hardware interlocks in service.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        if answer == QMessageBox.StandardButton.Yes:
            self.controller.start()

    def _confirm_abort(self):
        plan = self.controller.plan
        if plan is None:
            return
        answer = QMessageBox.warning(
            self, "Abort the automated test sequence?",
            f"This stops the test sequence and runs its declared '{plan.abort.action}' "
            "procedure.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        if answer == QMessageBox.StandardButton.Yes:
            self.controller.abort()

    def _on_plan(self, plan):
        if plan is None:
            self.summary.setText("No automated test sequence loaded.")
            self.start_button.setEnabled(False)
            return
        self.summary.setText(
            f"{plan.name} · {len(plan.stages)} stage(s) · "
            f"abort: {plan.abort.action}")
        self.start_button.setEnabled(self.controller.state in
                                     (RUN_IDLE, RUN_FINISHED, RUN_ABORTED))

    def _on_state(self, state):
        colors = {
            RUN_RUNNING: theme.OK,
            RUN_HOLDING: theme.WARN,
            RUN_AWAITING_OPERATOR: theme.WARN,
            RUN_ABORTED: theme.DANGER,
            RUN_FINISHED: theme.OK,
        }
        color = colors.get(state, theme.TEXT_DIM)
        self._dot.set_color(color)
        self._header_state.setText(state.replace("_", " "))
        self._header_state.setStyleSheet(
            f"color: {color}; background: transparent;")
        active = state in (RUN_RUNNING, RUN_HOLDING, RUN_AWAITING_OPERATOR)
        self.load_button.setEnabled(not active)
        self.start_button.setEnabled(not active and self.controller.plan is not None)
        self.abort_button.setEnabled(active)
        self.resolve_button.setEnabled(
            state in (RUN_HOLDING, RUN_AWAITING_OPERATOR))

    def _on_stage(self, name, index, total):
        self.stage.setText(f"Stage {index} of {total}: {name}")

    def _on_attention(self, reason):
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Automated test sequence needs an operator")
        box.setText(reason)
        box.setInformativeText(
            "The test sequence is not issuing further stage transitions. Advance only "
            "after checking the rig, or abort through the declared procedure.")
        advance = box.addButton("Advance stage", QMessageBox.ButtonRole.AcceptRole)
        abort = box.addButton("Abort test", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("Keep waiting", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is advance:
            self.controller.resolve_timeout("advance")
        elif box.clickedButton() is abort:
            self.controller.resolve_timeout("abort")
