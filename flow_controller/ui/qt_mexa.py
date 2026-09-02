"""Network analyser connection and acquisition status, independent of burner I/O."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                              QPushButton, QScrollArea, QSpinBox, QVBoxLayout, QWidget)

from mexa_bridge.records import (RECEIVER_LOG_REQUIRED, additional_reading_text,
                                 default_log_dir, reading_text)
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
        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        content = QWidget()
        scroll.setWidget(content)
        outer.addWidget(scroll)
        layout = QVBoxLayout(content)
        card = Card("MEXA-584L network receiver")
        layout.addWidget(card)
        card.add(note("Run the replacement MEXA reader on the analyser PC. Host a temporary Wormhole relay here, "
                      "or use Direct LAN as a backup. This link only receives measurements."))
        form = QFormLayout()
        self.connection_form = form
        config = controller.settings
        self.transport = QComboBox()
        self.transport.addItem("Wormhole (temporary hosting on this PC)", "host")
        self.transport.addItem("Direct LAN (TCP)", "lan")
        mode = "lan" if controller.temporary_host is None and config.get("transport") == "lan" else "host"
        self.transport.setCurrentIndex(self.transport.findData(mode))
        form.addRow("Connection mode", self.transport)
        self.host = QLineEdit(config["host"])
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(config["port"])
        self.token = QLineEdit(config["token"])
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        self.directory = QLineEdit(config["directory"] or str(default_log_dir()))
        for title, widget in (("Analyser PC IPv4", self.host), ("TCP port", self.port),
                              ("Shared key", self.token)):
            form.addRow(title, widget)
        self.save_logs = QCheckBox("Save received MEXA logs on this PC")
        self.save_logs.setChecked(config.get("save_logs", True))
        self.save_logs.setToolTip("CSV + raw JSONL. Required for live optimiser capture; optional for display and the normal flow log.")
        form.addRow("", self.save_logs)
        form.addRow("Local received-data logs", self.directory)
        card.add_layout(form)
        self.host_panel = QWidget()
        host_layout = QVBoxLayout(self.host_panel)
        host_layout.setContentsMargins(0, 0, 0, 0)
        host_layout.addWidget(note(
            "Temporary host: copy the analyser bridge's shared key into Shared key above before starting. "
            "The receiver connects locally; only the measurement relay is published. "
            "Wormhole can see forwarded data. Temporary tunnels have no guaranteed uptime. "
            "Keep this PC awake. A new start creates a new URL and access keys."))
        self.host_consent = QCheckBox()
        host_layout.addWidget(self.host_consent)
        self.helper_help = note("")
        host_layout.addWidget(self.helper_help)
        host_form = QFormLayout()
        self.helper_path = QLineEdit()
        self.helper_browse = QPushButton("Select helper…")
        helper_row = QHBoxLayout()
        helper_row.addWidget(self.helper_path)
        helper_row.addWidget(self.helper_browse)
        host_form.addRow("Tunnel helper", helper_row)
        self.public_url = QLineEdit()
        self.public_url.setReadOnly(True)
        self.public_url.setPlaceholderText("Created when the tunnel registers")
        self.copy_url = QPushButton("Copy URL")
        url_row = QHBoxLayout()
        url_row.addWidget(self.public_url)
        url_row.addWidget(self.copy_url)
        host_form.addRow("URL for analyser bridge", url_row)
        self.publisher_key = QLineEdit()
        self.publisher_key.setReadOnly(True)
        self.publisher_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.copy_publisher = QPushButton("Copy publisher key")
        key_row = QHBoxLayout()
        key_row.addWidget(self.publisher_key)
        key_row.addWidget(self.copy_publisher)
        host_form.addRow("Key for analyser bridge", key_row)
        host_layout.addLayout(host_form)
        host_actions = QHBoxLayout()
        self.start_host = QPushButton("Start temporary relay")
        self.stop_host = QPushButton("Stop temporary relay")
        host_actions.addWidget(self.start_host)
        host_actions.addWidget(self.stop_host)
        host_layout.addLayout(host_actions)
        self.host_status = note("")
        host_layout.addWidget(self.host_status)
        host_layout.addWidget(note("On the analyser PC choose Wormhole (Internet relay in older readers), paste the URL and "
                                   "publisher key, then start with Simulation only. Its shared key must match the field above. "
                                   "Copy keys only into the bridge, not logs or screenshots."))
        card.add(self.host_panel)
        self.host_consent.toggled.connect(self.refresh)
        self.start_host.clicked.connect(self._start_host)
        self.stop_host.clicked.connect(controller.stop_temporary_host)
        self.helper_browse.clicked.connect(self._helper_folder)
        self.copy_url.clicked.connect(lambda: QApplication.clipboard().setText(self.public_url.text()))
        self.copy_publisher.clicked.connect(lambda: QApplication.clipboard().setText(self.publisher_key.text()))
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
        self.network = note(controller.link_status)
        self.network.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        card.add(self.network)
        self.connection_help = note("")
        card.add(self.connection_help)
        self.lan_help = ("For Direct LAN, both address fields must use the analyser PC's active adapter IP, not 127.0.0.1. "
                      "A TCP connection timeout occurs before the key is checked. Verify the bridge is started and "
                      "the analyser PC firewall/network permits this port; do not disable the firewall.")
        self.readings = note("NO — ppm   ·   O₂ — %")
        self.readings.setObjectName("SectionTitle")
        self.readings.setStyleSheet(f"font-size: {theme.font_pt(18)}pt;")
        card.add(self.readings)
        self.additional_readings = note(additional_reading_text(None))
        self.additional_readings.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        card.add(self.additional_readings)
        self.quality = note("")
        card.add(self.quality)
        card.add(note("Fresh out-of-range readings remain visible and are saved if logging is enabled, with INVALID flags. "
                      "They are diagnostic values, not valid emissions measurements or optimiser inputs."))
        self.status = note(controller.status)
        card.add(self.status)
        self.log_label = note("")
        self.log_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        card.add(self.log_label)
        card.add(note("Choose whether to save every received sample to CSV and raw JSONL. "
                      "With this off, the live display still works and the normal flow logger can save "
                      "MEXA values when you start it separately. Live optimiser capture requires receiver logging. "
                      "Use live capture in the Bayesian optimiser to average each analyser sample once. "
                      "No stream reconnection changes burner settings."))
        card.add(note("Choose logging before connecting; disconnect to change it. "
                      "The analyser PC's local logging choice is independent."))
        card.add(note("Keep both PC clocks synchronised. NO is not total NOx. Verify the analyser and "
                      "sampling system for NH3/H2 exhaust before using results. "
                      "Direct LAN is unencrypted. Wormhole uses WSS; the tunnel provider can see forwarded data."))
        layout.addStretch()
        controller.changed.connect(self.refresh)
        self.save_logs.toggled.connect(self._logging_choice)
        self.transport.currentIndexChanged.connect(self.refresh)
        self.refresh()

    def _connect(self):
        try:
            if self.save_logs.isChecked() and not self.directory.text().strip():
                raise ValueError("Choose a local received-data log directory")
            if self.transport.currentData() == "host":
                self.controller.connect_temporary_host(self.token.text(), self.directory.text().strip(),
                                                       save_logs=self.save_logs.isChecked())
                return
            self.controller.connect_bridge(self.host.text().strip(), self.port.value(), self.token.text(),
                                           self.directory.text().strip(), save_logs=self.save_logs.isChecked(),
                                           transport="lan")
        except (ValueError, OSError) as exc:
            self.status.setText(str(exc))

    def _start_host(self):
        if not self.host_consent.isChecked():
            self.host_status.setText("Confirm that publishing through Wormhole is permitted before starting.")
            return
        try:
            self.controller.start_temporary_host(self.token.text(), self.directory.text().strip(),
                                                 save_logs=self.save_logs.isChecked(),
                                                 executable=self.helper_path.text().strip())
        except (ValueError, OSError) as exc:
            self.host_status.setText(str(exc))

    def _helper_folder(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select official Wormhole executable", self.helper_path.text())
        if path:
            self.helper_path.setText(path)

    def _folder(self):
        path = QFileDialog.getExistingDirectory(self, "Received analyser logs", self.directory.text())
        if path:
            self.directory.setText(path)

    def _logging_choice(self, checked):
        self.controller.settings["save_logs"] = checked
        self.refresh()

    def refresh(self):
        c = self.controller
        connected = c.client is not None
        hosting = c.temporary_host is not None
        host_mode = self.transport.currentData() == "host"
        pending = hosting and c.host_status.state in ("starting", "stopping")
        self.connect_button.setEnabled(not connected and (not host_mode or (hosting and c.host_status.state == "ready")))
        self.disconnect_button.setEnabled(connected)
        self.transport.setEnabled(not connected and not hosting)
        for widget in (self.token, self.save_logs):
            widget.setEnabled(not connected and not pending)
        for widget in (self.host, self.port):
            widget.setEnabled(not connected and not host_mode)
            self.connection_form.setRowVisible(widget, not host_mode)
        self.host_panel.setVisible(host_mode)
        self.start_host.setEnabled(host_mode and not connected and not hosting and self.host_consent.isChecked())
        self.stop_host.setEnabled(hosting and c.host_status.state != "stopping")
        for widget in (self.host_consent, self.helper_path, self.helper_browse):
            widget.setEnabled(not hosting and not connected)
        self.host_consent.setText("Allow temporary publishing through Wormhole")
        self.helper_path.setPlaceholderText("Automatic verified download, or select official wormhole.exe")
        self.helper_help.setText("Use only where UCL permits this service and data transfer. Starting downloads the official Windows x64 helper "
                                 "(3.6 MB ZIP; ZIP and executable SHA-256 checked) "
                                 "if needed. Manually selected helpers must be trusted and are not hash-checked. "
                                 "No admin rights or inbound firewall rule is required.")
        self.public_url.setText(c.host_status.public_url)
        self.publisher_key.setText(c.host_status.publisher_key)
        self.copy_url.setEnabled(bool(c.host_status.public_url))
        self.copy_publisher.setEnabled(bool(c.host_status.publisher_key))
        self.host_status.setText(c.host_status.message)
        self.connection_help.setText(
            ("Wormhole: the flow receiver connects to its own private relay. The analyser uses the public WSS URL. "
             "Both PCs need approved outbound HTTPS/WSS 443 to Wormhole. "
             "Stopping the host disconnects MEXA and invalidates live capture, but does not change burner settings.")
            if host_mode else self.lan_help)
        for widget in (self.directory, self.browse):
            widget.setEnabled(not connected and not pending and self.save_logs.isChecked())
        self.status.setText(c.status)
        self.network.setText(c.link_status)
        self.log_label.setText(f"Receiving audit log: {c.log.path}" if c.log else
                               ("Live display only: receiver CSV/JSONL logging is off. The normal flow logger is independent."
                                if connected else "No receiver log open"))
        sample = c.latest
        self.readings.setText(reading_text(sample))
        self.additional_readings.setText(additional_reading_text(sample))
        if sample is None:
            self.quality.setText("No fresh analyser reading")
            return
        p = sample.packet
        self.quality.setText(f"Sample {p['seq']} · {p['acquired_at']}\n"
                             + (sample.problem(experimental=True) or
                                ("Eligible for operator-confirmed live capture" if sample.log_path else RECEIVER_LOG_REQUIRED))
                             + ("\n" + ", ".join(p["warnings"]) if p["warnings"] else ""))
