"""Standalone MEXA reader UI. Starts disconnected and never changes analyser mode."""

import os
from pathlib import Path
import secrets
import sys

from PySide6.QtCore import QObject, QTimer, Signal, Qt
from PySide6.QtGui import QFont
from PySide6.QtNetwork import QAbstractSocket, QNetworkInterface
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout,
                              QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMessageBox, QPushButton,
                              QSpinBox, QVBoxLayout, QWidget)

from .bridge import Bridge
from .records import ReceivedSample, utc_now
from .transport import DEFAULT_PORT
import time

# This reader deliberately starts with a new in-memory shared key on each
# launch. It never writes a network credential into a measurement log.


def default_log_dir():
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "Documents"))) / "MEXA-584L" / "logs"


class BridgeSignals(QObject):
    sample = Signal(object)
    status = Signal(str)


class BridgeWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setFont(QFont("Segoe UI", 10))
        self.setWindowTitle("MEXA-584L reader and network bridge")
        self.resize(680, 650)
        self.bridge = None
        self.last_sample = None
        self.signals = BridgeSignals(self)
        self.signals.sample.connect(self._sample)
        self.signals.status.connect(self._status)
        layout = QVBoxLayout(self)
        title = QLabel("MEXA-584L · NO and O₂ acquisition")
        title.setStyleSheet("font-size: 20px; font-weight: 600")
        layout.addWidget(title)
        hint = QLabel("Close the HORIBA application before connecting. Set MEAS and perform calibration "
                      "using the instrument's front panel. This reader only queries data/status; "
                      "it does not operate the burner or change analyser settings.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        form = QFormLayout()
        self.com = QComboBox()
        self.com.setEditable(True)
        self.com.setPlaceholderText("Choose the analyser's COM port")
        port_line = QHBoxLayout()
        port_line.addWidget(self.com)
        refresh = QPushButton("Refresh ports")
        refresh.clicked.connect(self._ports)
        port_line.addWidget(refresh)
        form.addRow("Serial port (9600, 8N1)", port_line)
        self.host = QLineEdit("127.0.0.1")
        host_line = QHBoxLayout()
        host_line.addWidget(self.host)
        self.local_ip = QPushButton("Local IPv4…")
        self.local_ip.clicked.connect(self._local_ip)
        host_line.addWidget(self.local_ip)
        form.addRow("Listen IPv4 address", host_line)
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(DEFAULT_PORT)
        form.addRow("TCP port", self.port)
        self.token = QLineEdit(secrets.token_hex(24))
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        key_line = QHBoxLayout()
        key_line.addWidget(self.token)
        self.show_key = QCheckBox("Show key")
        self.show_key.toggled.connect(lambda checked: self.token.setEchoMode(
            QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password))
        key_line.addWidget(self.show_key)
        copy_key = QPushButton("Copy key")
        copy_key.clicked.connect(lambda: QApplication.clipboard().setText(self.token.text()))
        key_line.addWidget(copy_key)
        form.addRow("Shared key (copy to receiver)", key_line)
        self.save_logs = QCheckBox("Save CSV + raw logs on this PC")
        self.save_logs.setToolTip("Off: stream only. The receiving PC can save its own logs independently.")
        form.addRow("", self.save_logs)
        self.directory = QLineEdit(str(default_log_dir()))
        folder_line = QHBoxLayout()
        folder_line.addWidget(self.directory)
        browse = QPushButton("Browse…")
        self.log_browse = browse
        browse.clicked.connect(self._folder)
        folder_line.addWidget(browse)
        form.addRow("Local CSV + raw JSONL log", folder_line)
        layout.addLayout(form)
        self.simulated = QCheckBox("Simulation only (no serial port opened; excluded from optimisation)")
        self.dry = QCheckBox("Sampling system verified: these are uncorrected dry NO/O₂ readings")
        self.validated = QCheckBox("Serial values and alarm behaviour validated against this instrument")
        for box in (self.simulated, self.dry, self.validated):
            layout.addWidget(box)
        self.simulated.toggled.connect(self._simulation)
        warning = QLabel("First use: leave validation unchecked and compare this display with the instrument. "
                         "For another PC, use this PC's lab-LAN IPv4 address and the same key on both PCs. "
                         "One receiver at a time. Traffic is authenticated but not encrypted. "
                         "Do not expose the port to the internet.")
        warning.setWordWrap(True)
        layout.addWidget(warning)
        buttons = QHBoxLayout()
        self.start_button = QPushButton("Start reader and stream")
        self.stop_button = QPushButton("Stop")
        self.start_button.clicked.connect(self._start)
        self.stop_button.clicked.connect(self._stop)
        buttons.addWidget(self.start_button)
        buttons.addWidget(self.stop_button)
        layout.addLayout(buttons)
        self.readings = QLabel("NO — ppm     O₂ — %")
        self.readings.setStyleSheet("font-size: 28px; font-weight: 600")
        layout.addWidget(self.readings)
        self.quality = QLabel("No samples")
        self.quality.setTextFormat(Qt.TextFormat.PlainText)
        self.quality.setWordWrap(True)
        layout.addWidget(self.quality)
        self.status = QLabel("Stopped. No serial port or network listener is open.")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.log_label = QLabel("")
        self.log_label.setTextFormat(Qt.TextFormat.PlainText)
        self.log_label.setWordWrap(True)
        layout.addWidget(self.log_label)
        layout.addStretch()
        self._config_widgets = [self.com, refresh, self.host, self.local_ip, self.port, self.directory,
                                browse, self.save_logs, self.simulated, self.dry, self.validated]
        self.save_logs.toggled.connect(self._tick)
        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self._tick)
        self.timer.start()
        self._tick()

    def _ports(self):
        from serial.tools.list_ports import comports
        current = self.com.currentText()
        self.com.clear()
        self.com.addItems([port.device for port in comports()])
        self.com.setCurrentText(current)

    def _folder(self):
        path = QFileDialog.getExistingDirectory(self, "Local analyser logs", self.directory.text())
        if path:
            self.directory.setText(path)

    def _local_ip(self):
        choices = {}
        for adapter in QNetworkInterface.allInterfaces():
            if not adapter.flags() & QNetworkInterface.InterfaceFlag.IsUp:
                continue
            for entry in adapter.addressEntries():
                if entry.ip().protocol() == QAbstractSocket.NetworkLayerProtocol.IPv4Protocol:
                    address = entry.ip().toString()
                    choices[f"{adapter.humanReadableName()} · {address}"] = address
        if not choices:
            QMessageBox.information(self, "Local IPv4", "No active IPv4 adapters found")
            return
        selected, ok = QInputDialog.getItem(self, "Choose the lab-network adapter",
                                            "For eduroam, choose the connected Wi-Fi adapter:", list(choices), editable=False)
        if ok:
            self.host.setText(choices[selected])

    def _simulation(self, checked):
        if checked:
            self.validated.setChecked(False)
        self.validated.setEnabled(not checked)

    def _start(self):
        try:
            if not self.simulated.isChecked() and not self.com.currentText().strip():
                raise ValueError("Select the analyser's COM port")
            if self.save_logs.isChecked() and not self.directory.text().strip():
                raise ValueError("Choose a local log directory")
            if self.bridge:
                if not self.bridge.stop():
                    raise ValueError("Previous reader is still stopping")
            self.last_sample = None
            self.bridge = Bridge(host=self.host.text().strip(), port=self.port.value(),
                                 token=self.token.text(), serial_port=self.com.currentText().strip(),
                                 directory=self.directory.text().strip(), save_logs=self.save_logs.isChecked(),
                                 simulated=self.simulated.isChecked(),
                                 validated=self.validated.isChecked(), dry=self.dry.isChecked(),
                                 on_sample=self.signals.sample.emit, on_status=self.signals.status.emit)
            self.status.setText("Reader started. Waiting for samples/client.")
            self.log_label.setText(f"Audit log: {self.bridge.log.path}" if self.bridge.log else
                                   "Stream only: no files saved on this PC. The receiver can log independently.")
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, "Cannot start MEXA reader", str(exc))
        self._tick()

    def _stop(self):
        if self.bridge and not self.bridge.stop():
            self.status.setText("Reader is stopping; wait before closing")
            return
        self.bridge = None
        self.last_sample = None
        self.status.setText("Stopped. Instrument mode unchanged.")
        self._tick()

    def _status(self, text):
        self.status.setText(text)

    def _sample(self, packet):
        self.last_sample = ReceivedSample(packet, utc_now(), time.monotonic(), "")
        self._tick()

    def _tick(self):
        running = bool(self.bridge and self.bridge.running)
        self.start_button.setEnabled(not running)
        self.stop_button.setEnabled(running)
        for widget in self._config_widgets:
            widget.setEnabled(not running)
        for widget in (self.directory, self.log_browse):
            widget.setEnabled(not running and self.save_logs.isChecked())
        self.token.setReadOnly(running)
        if not running:
            self.validated.setEnabled(not self.simulated.isChecked())
        sample = self.last_sample
        problem = sample.problem() if sample and running else "No live acquisition"
        if not sample or problem:
            self.readings.setText("NO — ppm     O₂ — %")
            self.quality.setText(problem)
            return
        p = sample.packet
        prefix = "SIMULATED · " if p["simulated"] else ""
        self.readings.setText(f"{prefix}NO {p['no_ppm']:.0f} ppm     O₂ {p['o2_percent']:.2f} %")
        self.quality.setText(f"Sample {p['seq']} · {p['acquired_at']}\n"
                             + (sample.problem(experimental=True) or "Validated dry readings; operator checks still required")
                             + ("\n" + ", ".join(p["warnings"]) if p["warnings"] else ""))

    def closeEvent(self, event):
        if self.bridge and not self.bridge.stop():
            event.ignore()
            return
        event.accept()


def main():
    app = QApplication(sys.argv)
    window = BridgeWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
