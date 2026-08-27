"""Network analyser connection and acquisition status, independent of burner I/O."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                              QPushButton, QSpinBox, QVBoxLayout, QWidget)

from ..mexa.app import default_log_dir
from .qt_widgets import Card
from . import qt_theme as theme


def note(text):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


class MexaTab(QWidget):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        layout = QVBoxLayout(self)
        card = Card("MEXA-584L network receiver")
        layout.addWidget(card)
        card.add(note("Run the replacement MEXA reader on the analyser PC. Enter its lab-LAN IPv4 "
                      "address, port and shared key. This link only receives measurements."))
        form = QFormLayout()
        config = controller.settings
        self.host = QLineEdit(config["host"])
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(config["port"])
        self.token = QLineEdit(config["token"])
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        self.directory = QLineEdit(config["directory"] or str(default_log_dir()))
        for title, widget in (("Analyser PC IPv4", self.host), ("TCP port", self.port),
                              ("Shared key", self.token), ("Local received-data logs", self.directory)):
            form.addRow(title, widget)
        card.add_layout(form)
        actions = QHBoxLayout()
        self.connect_button = QPushButton("Connect MEXA")
        self.disconnect_button = QPushButton("Disconnect")
        self.browse = QPushButton("Log folder…")
        self.connect_button.clicked.connect(self._connect)
        self.disconnect_button.clicked.connect(controller.disconnect_bridge)
        self.browse.clicked.connect(self._folder)
        for button in (self.connect_button, self.disconnect_button, self.browse):
            actions.addWidget(button)
        card.add_layout(actions)
        self.readings = note("NO — ppm   ·   O₂ — %")
        self.readings.setObjectName("SectionTitle")
        self.readings.setStyleSheet(f"font-size: {theme.font_pt(18)}pt;")
        card.add(self.readings)
        self.quality = note("")
        card.add(self.quality)
        self.status = note(controller.status)
        card.add(self.status)
        self.log_label = note("")
        self.log_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        card.add(self.log_label)
        card.add(note("Every received sample is saved to CSV and raw JSONL, including invalid readings. "
                      "The normal flow log adds timestamped MEXA columns; repeated held readings are labelled. "
                      "Use live capture in the Bayesian optimiser to average each analyser sample once. "
                      "No stream reconnection changes burner settings."))
        card.add(note("Keep both PC clocks synchronised. NO is not total NOx. Verify the analyser and "
                      "sampling system for NH3/H2 exhaust before using results. "
                      "Use a trusted LAN: the authenticated stream is not encrypted."))
        layout.addStretch()
        controller.changed.connect(self.refresh)
        self.refresh()

    def _connect(self):
        try:
            if not self.directory.text().strip():
                raise ValueError("Choose a local received-data log directory")
            self.controller.connect_bridge(self.host.text().strip(), self.port.value(), self.token.text(),
                                           self.directory.text().strip())
        except (ValueError, OSError) as exc:
            self.status.setText(str(exc))

    def _folder(self):
        path = QFileDialog.getExistingDirectory(self, "Received analyser logs", self.directory.text())
        if path:
            self.directory.setText(path)

    def refresh(self):
        c = self.controller
        connected = c.client is not None
        self.connect_button.setEnabled(not connected)
        self.disconnect_button.setEnabled(connected)
        for widget in (self.host, self.port, self.token, self.directory, self.browse):
            widget.setEnabled(not connected)
        self.status.setText(c.status)
        self.log_label.setText(f"Receiving audit log: {c.log.path}" if c.log else "No receiver log open")
        sample = c.latest
        if sample is None or sample.problem():
            self.readings.setText("NO — ppm   ·   O₂ — %")
            self.quality.setText(sample.problem() if sample else "No fresh analyser reading")
            return
        p = sample.packet
        prefix = "SIMULATION · " if p["simulated"] else ""
        self.readings.setText(f"{prefix}NO {p['no_ppm']:.0f} ppm   ·   O₂ {p['o2_percent']:.2f}%")
        self.quality.setText(f"Sample {p['seq']} · {p['acquired_at']}\n"
                             + (sample.problem(experimental=True) or "Eligible for operator-confirmed live capture")
                             + ("\n" + ", ".join(p["warnings"]) if p["warnings"] else ""))
