"""Operator-approved Bayesian experiments with live or manual NO/O2 measurements."""

from pathlib import Path
from copy import deepcopy

import numpy as np

from PySide6.QtCore import Qt, QThread, Signal, QRectF
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QGridLayout,
    QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMessageBox, QPushButton, QScrollArea, QTabWidget, QVBoxLayout, QWidget,
)
import pyqtgraph as pg

from ..core.session import DEFAULT_LOG_DIR
from ..domain.bayesian import SearchConfig, finite
from ..domain.gas_properties import O2_CORRECTION_AIR_PERCENT
from . import qt_theme as theme
from .qt_widgets import Card


SUFFIX = ".fcbo.json"
PRESSURE_LABELS = {"rms_pa": "RMS", "peak_abs_pa": "Peak excursion",
                   "dominant_amplitude_pa": "Dominant spectral amplitude"}
PRESSURE_UNITS = {"rms_pa": "Pa", "peak_abs_pa": "Pa", "dominant_amplitude_pa": "Pa RMS"}
MAP_VARIABLES = {"h2_fraction": ("H2 in fuel", "%", 100),
                 "phi_stage1": ("Stage-1 phi", "", 1),
                 "phi_overall": ("Overall phi", "", 1),
                 "power_kw": ("Thermal input", "kW", 1),
                 "split_rich": ("Stage-1 fuel split", "%", 100)}
# Workers outlive a pane that is destroyed during an appearance refresh. Qt
# disconnects its slots; keeping application ownership prevents a running
# QThread being destroyed with the pane.
_MAP_WORKERS = set()
_TDMS_INSPECTORS = set()


def _dual_transducers(value):
    """Return canonical transducers without confusing legacy pressure objects."""
    items = value.get("transducers") if isinstance(value, dict) else None
    return items if isinstance(items, list) and len(items) == 2 else None


def _transducer_metric(item, key):
    metrics = item.get("metrics") if isinstance(item, dict) else None
    return metrics.get(key) if isinstance(metrics, dict) else None


def inspect_tdms(path):
    """Lazy reader import keeps optional file dependencies out of UI startup."""
    from ..domain.tdms_capture import inspect_tdms as inspect
    return inspect(path)


class _ApplicationWorker(QThread):
    """Keep background work alive until it finishes, even if its pane closes."""

    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self):
        super().__init__(QApplication.instance())
        self._registry.add(self)
        self.finished.connect(self._release)
        QApplication.instance().aboutToQuit.connect(self._shutdown)

    def run(self):
        try:
            result = self._compute()
            if not self.isInterruptionRequested():
                self.succeeded.emit(result)
        except Exception as exc:
            if not self.isInterruptionRequested():
                self.failed.emit(str(exc))

    def _release(self):
        self._registry.discard(self)
        self.deleteLater()

    def _shutdown(self):
        self.requestInterruption()
        self.wait()


class TdmsInspectionWorker(_ApplicationWorker):
    _registry = _TDMS_INSPECTORS

    def __init__(self, path):
        path = str(path)
        super().__init__()
        self.path = path

    def _compute(self):
        return inspect_tdms(self.path)


class TdmsSourceDialog(QDialog):
    """Configure an existing LabVIEW waveform source without changing LabVIEW."""

    TRANSDUCER_IDS = ("pressure_1", "pressure_2")
    DEFAULT_LABELS = ("Pressure transducer 1", "Pressure transducer 2")

    def __init__(self, source=None, parent=None, *, dual=None):
        super().__init__(parent)
        self.setWindowTitle("TDMS pressure source")
        self.resize(theme.scale(920), theme.scale(760))
        self.source = None
        self.worker = None
        self._sample_path = None
        self._source = dict(source or {})
        self.dual = ("transducers" in self._source if dual is None and self._source else
                     True if dual is None else bool(dual))
        outer = QVBoxLayout(self)
        outer.addWidget(note(
            "Use the TDMS files LabVIEW already records. The app matches a new recording "
            "to the local log/stop interval and calculates pressure metrics after NO collection finishes."))
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.viewport().setObjectName("TdmsSourceViewport")
        scroll.viewport().setStyleSheet(f"#TdmsSourceViewport {{ background-color: {theme.BG}; }}")
        content = QWidget()
        body = QVBoxLayout(content)
        scroll.setWidget(content)
        outer.addWidget(scroll)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.entries = {}
        folder_row = QWidget()
        row = QHBoxLayout(folder_row)
        row.setContentsMargins(0, 0, 0, 0)
        self.entries["folder"] = QLineEdit(str(self._source.get("folder", "")))
        self.entries["folder"].setAccessibleName("TDMS recording folder")
        row.addWidget(self.entries["folder"], 1)
        self.folder_button = QPushButton("Browse…")
        self.folder_button.clicked.connect(self._choose_folder)
        row.addWidget(self.folder_button)
        form.addRow("Recording folder", folder_row)
        self.inspect_button = QPushButton("Inspect sample TDMS…")
        self.inspect_button.clicked.connect(self._choose_sample)
        form.addRow(self.inspect_button)
        body.addLayout(form)

        transducers = self._source.get("transducers") or []
        if not self.dual:
            transducers = [self._source]
        self.sensor_entries = {}
        self.channel_pickers = {}
        self.metadata_by_id = {}
        self.pressure_units_by_id = {}
        sensor_grid = QGridLayout()
        sensor_grid.addWidget(QLabel("Setting"), 0, 0)
        for column, sensor_id in enumerate(self.TRANSDUCER_IDS[:2 if self.dual else 1], 1):
            saved = dict(transducers[column - 1]) if column <= len(transducers) else {}
            label_default = self.DEFAULT_LABELS[column - 1]
            fields = {}
            label_entry = QLineEdit(str(saved.get("label", label_default)))
            label_entry.setMaxLength(64)
            label_entry.setAccessibleName(f"{sensor_id} display label")
            fields["label"] = label_entry
            sensor_grid.addWidget(label_entry, 0, column)

            picker = QComboBox()
            picker.setAccessibleName(f"{sensor_id} TDMS waveform channel")
            picker.setPlaceholderText("Inspect a file to list waveform channels")
            picker.currentIndexChanged.connect(
                lambda _index, identity=sensor_id: self._channel_selected(identity))
            self.channel_pickers[sensor_id] = picker
            if column == 1:
                sensor_grid.addWidget(QLabel("Waveform in sample file"), 1, 0)
            sensor_grid.addWidget(picker, 1, column)

            metadata = note("Select a time-domain waveform. FFT/spectrum channels are excluded.")
            metadata.setObjectName("")
            metadata.setWordWrap(True)
            self.metadata_by_id[sensor_id] = metadata
            if column == 1:
                sensor_grid.addWidget(QLabel("Sample metadata"), 2, 0)
            sensor_grid.addWidget(metadata, 2, column)

            units = QComboBox()
            units.setAccessibleName(f"{sensor_id} known units of stored pressure values")
            units.addItem("Custom / enter calibration scale", "custom")
            units.addItem("Values already in Pa", "pa")
            units.addItem("Values already in kPa", "kpa")
            units.setToolTip("Choose documented stored units; TDMS metadata does not infer calibration.")
            units.currentIndexChanged.connect(
                lambda _index, identity=sensor_id: self._pressure_units_changed(identity))
            self.pressure_units_by_id[sensor_id] = units

            for row_number, (key, title, default, placeholder) in enumerate((
                ("group", "TDMS group", "", "Choose or enter the group"),
                ("channel", "TDMS channel", "", "Choose or enter the channel"),
                ("units", "Known pressure units", None, ""),
                ("scale_pa_per_unit", "Pressure scale (Pa per stored unit)", "", "Required calibration scale"),
                ("offset_pa", "Pressure offset (Pa)", 0, "0"),
                ("calibration_id", "Calibration identifier", "", "Required calibration reference"),
                ("clip_min", "Lower clipping limit (stored units)", None, "Optional"),
                ("clip_max", "Upper clipping limit (stored units)", None, "Optional"),
            ), 3):
                if column == 1:
                    sensor_grid.addWidget(QLabel(title), row_number, 0)
                if key == "units":
                    sensor_grid.addWidget(units, row_number, column)
                    continue
                value = saved.get(key, default)
                entry = QLineEdit("" if value is None else str(value))
                entry.setPlaceholderText(placeholder)
                entry.setAccessibleName(f"{sensor_id} {title}")
                fields[key] = entry
                sensor_grid.addWidget(entry, row_number, column)
            self.sensor_entries[sensor_id] = fields
        body.addLayout(sensor_grid)

        shared_form = QFormLayout()
        for key, label, default, placeholder in (
            ("sample_rate_hz", "Fallback sample rate (Hz)", None, "Optional; TDMS timing takes precedence"),
            ("min_recording_s", "Minimum pressure recording (s)", 1.0, "Independent of the NO averaging window"),
            ("band_low_hz", "Spectrum lower frequency (Hz)", 0, "0"),
            ("band_high_hz", "Spectrum upper frequency (Hz)", None, "Blank = Nyquist"),
            ("segment_samples", "Spectral segment (samples)", 4096, "4096"),
            ("overlap_samples", "Spectral overlap (samples)", 2048, "2048"),
        ):
            value = self._source.get(key, default)
            entry = QLineEdit("" if value is None else str(value))
            entry.setPlaceholderText(placeholder)
            entry.setAccessibleName(label)
            self.entries[key] = entry
            shared_form.addRow(label, entry)
        body.addLayout(shared_form)

        # Compatibility aliases for the established one-channel tests and callers.
        primary = self.TRANSDUCER_IDS[0]
        self.entries.update(self.sensor_entries[primary])
        self.channel_picker = self.channel_pickers[primary]
        self.metadata = self.metadata_by_id[primary]
        self.pressure_units = self.pressure_units_by_id[primary]
        body.addWidget(note(
            "Pressure = stored value × scale + offset. A group called 'converted' does not "
            "establish its units. Enter scale 1 only when you know its values already represent Pa."))
        self.use_trigger_time = QCheckBox(
            "If TDMS has no start timestamp, use trigger time\n"
            "I confirm LabVIEW writes one new file per trigger")
        self.use_trigger_time.setChecked(bool(self._source.get("use_trigger_time", False)))
        body.addWidget(self.use_trigger_time)
        self.error = note("")
        outer.addWidget(self.error)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        outer.addWidget(self.buttons)

    def _pressure_units_changed(self, sensor_id="pressure_1"):
        units = self.pressure_units_by_id[sensor_id].currentData()
        entry = self.sensor_entries[sensor_id]["scale_pa_per_unit"]
        entry.setReadOnly(units != "custom")
        if units in ("pa", "kpa"):
            entry.setText("1" if units == "pa" else "1000")

    def _choose_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Choose LabVIEW TDMS recording folder", self.entries["folder"].text() or str(DEFAULT_LOG_DIR))
        if folder:
            self.entries["folder"].setText(folder)

    def _choose_sample(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Inspect a sample TDMS recording", self.entries["folder"].text() or str(DEFAULT_LOG_DIR),
            "TDMS (*.tdms)")
        if path:
            self.inspect_sample(path)

    def inspect_sample(self, path):
        if self.worker is not None:
            return
        self._sample_path = str(path)
        self.inspect_button.setEnabled(False)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        self.metadata.setText("Reading TDMS channel metadata in the background…")
        self.error.setText("")
        self.worker = TdmsInspectionWorker(path)
        self.worker.succeeded.connect(self._inspected)
        self.worker.failed.connect(self.error.setText)
        self.worker.finished.connect(self._inspection_finished)
        self.worker.start()

    def _inspected(self, channels):
        candidates = [channel for channel in channels if not channel.get("is_spectrum")]
        used = set()
        for sensor_id, picker in self.channel_pickers.items():
            picker.blockSignals(True)
            picker.clear()
            for channel in candidates:
                picker.addItem(f"{channel['group']} / {channel['channel']}", channel)
            fields = self.sensor_entries[sensor_id]
            selected = next((index for index, channel in enumerate(candidates)
                             if channel["group"] == fields["group"].text()
                             and channel["channel"] == fields["channel"].text()), -1)
            if selected < 0:
                ranked = sorted(range(len(candidates)), key=lambda index: (
                    2 * ("converted" in candidates[index]["group"].casefold())
                    + (candidates[index]["channel"].casefold() == "pd_cc_3_1")), reverse=True)
                selected = next((index for index in ranked if index not in used), -1)
            if selected >= 0:
                used.add(selected)
            picker.setCurrentIndex(selected)
            picker.blockSignals(False)
        if candidates:
            if not self.entries["folder"].text().strip():
                self.entries["folder"].setText(str(Path(self._sample_path).resolve().parent))
            for sensor_id in self.channel_pickers:
                self._channel_selected(sensor_id)
        else:
            for metadata in self.metadata_by_id.values():
                metadata.setText("No time-domain waveform channels found. Spectrum channels cannot be selected.")

    def _channel_selected(self, sensor_id="pressure_1"):
        channel = self.channel_pickers[sensor_id].currentData()
        if not channel:
            return
        fields = self.sensor_entries[sensor_id]
        fields["group"].setText(channel["group"])
        fields["channel"].setText(channel["channel"])
        rate = channel.get("sample_rate_hz")
        rate_text = f"{rate:g} Hz" if rate else "no sample-rate metadata"
        self.metadata_by_id[sensor_id].setText(
            f"{channel.get('samples', 0):,} samples · {rate_text} · stored unit: {channel.get('unit') or 'unspecified'}\n"
            f"TDMS start: {channel.get('start') or 'not recorded'}")

    def _inspection_finished(self):
        self.worker = None
        self.inspect_button.setEnabled(True)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(True)

    def accept(self):
        if self.worker is not None:
            return
        try:
            from ..domain.tdms_capture import validate_tdms_source
            shared_keys = ("folder", "sample_rate_hz", "min_recording_s", "band_low_hz",
                           "band_high_hz", "segment_samples", "overlap_samples")
            values = {key: self.entries[key].text().strip() for key in shared_keys}
            for key in ("sample_rate_hz", "band_high_hz"):
                values[key] = None if not values[key] else finite(values[key], key)
            for key in ("min_recording_s", "band_low_hz"):
                values[key] = finite(values[key], key)
            for key in ("segment_samples", "overlap_samples"):
                number = finite(values[key], key)
                if not number.is_integer():
                    raise ValueError(f"{key} must be a whole number.")
                values[key] = int(number)
            values["use_trigger_time"] = self.use_trigger_time.isChecked()
            if self.dual:
                values["transducers"] = []
                labels, channels = set(), set()
                for sensor_id in self.TRANSDUCER_IDS:
                    fields = {key: entry.text().strip()
                              for key, entry in self.sensor_entries[sensor_id].items()}
                    label_key = fields["label"].casefold()
                    if not fields["label"] or len(fields["label"]) > 64:
                        raise ValueError(f"{sensor_id} label must contain 1 to 64 characters.")
                    if label_key in labels:
                        raise ValueError("Pressure transducer labels must be distinct.")
                    labels.add(label_key)
                    pair = (fields["group"], fields["channel"])
                    if pair in channels:
                        raise ValueError("Pressure transducer group/channel selections must be distinct.")
                    channels.add(pair)
                    for key in ("clip_min", "clip_max"):
                        fields[key] = None if not fields[key] else finite(fields[key], f"{sensor_id} {key}")
                    for key in ("scale_pa_per_unit", "offset_pa"):
                        fields[key] = finite(fields[key], f"{sensor_id} {key}")
                    fields["id"] = sensor_id
                    values["transducers"].append(fields)
            else:
                fields = {key: entry.text().strip()
                          for key, entry in self.sensor_entries["pressure_1"].items()
                          if key != "label"}
                for key in ("clip_min", "clip_max"):
                    fields[key] = None if not fields[key] else finite(fields[key], key)
                for key in ("scale_pa_per_unit", "offset_pa"):
                    fields[key] = finite(fields[key], key)
                values.update(fields)
            self.source = validate_tdms_source(values)
        except (ValueError, OSError) as exc:
            self.error.setText(str(exc))
            return
        super().accept()

    def reject(self):
        if self.worker is not None:
            self.worker.requestInterruption()
        super().reject()

    def closeEvent(self, event):
        if self.worker is not None:
            self.worker.requestInterruption()
        super().closeEvent(event)


class MappingWorker(_ApplicationWorker):
    _registry = _MAP_WORKERS

    def __init__(self, config, trials, points, context):
        trials, points = deepcopy(trials), points.copy()
        super().__init__()
        self.config, self.trials = config, trials
        self.points, self.context = points, context

    def _compute(self):
        from ..domain.bayesian import predict_mapping
        feasible = self.context["feasible"]
        predictions = predict_mapping(self.config, self.trials, self.points[feasible])
        result = {}
        for name, values in predictions.items():
            grid = np.full(len(self.points), np.nan)
            grid[feasible] = values
            result[name] = grid
        return result, self.context


def note(text):
    widget = QLabel(text)
    widget.setWordWrap(True)
    widget.setObjectName("Hint")
    return widget


def point_text(config, point):
    request = config.request(point)
    parts = [f"H2 {request.h2_fraction * 100:.4g}%",
             f"φ1 {request.phi_stage1:.5g}", f"φ overall {request.phi_global:.5g}"]
    if config.optimise_power:
        parts.append(f"power {request.power_kw:.5g} kW")
    if config.optimise_split:
        parts.append(f"stage-1 split {request.split_rich * 100:.4g}%")
    return " · ".join(parts)


class ExperimentDialog(QDialog):
    def __init__(self, request=None, parent=None, *, mexa=None, dual_pressure=None):
        super().__init__(parent)
        self._mexa = mexa
        self._dual_pressure = request is None if dual_pressure is None else bool(dual_pressure)
        self.setWindowTitle("New Bayesian experiment")
        self.resize(theme.scale(600), theme.scale(650))
        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.viewport().setObjectName("ExperimentViewport")
        scroll.viewport().setStyleSheet(f"#ExperimentViewport {{ background-color: {theme.BG}; }}")
        content = QWidget()
        layout = QVBoxLayout(content)
        scroll.setWidget(content)
        outer.addWidget(scroll)
        layout.addWidget(note(
            "Pilot-off NH3/H2, rich stage 1 and lean overall. Enter a rig-approved search "
            "region; these bounds do not establish flame stability or safe transitions."))
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.entries = {}
        self.objective_mode = QComboBox()
        self.objective_mode.addItem("Minimise NO", "minimise_no")
        self.objective_mode.addItem("Map NO + pressure", "map_no_pressure")
        self.objective_mode.setCurrentIndex(max(0, self.objective_mode.findData(
            getattr(request, "objective_mode", "minimise_no"))))
        form.addRow("Experiment purpose", self.objective_mode)
        self.pressure_metric = QComboBox()
        for key, label in PRESSURE_LABELS.items():
            self.pressure_metric.addItem(label + f" ({PRESSURE_UNITS[key]})", key)
        self.pressure_metric.setCurrentIndex(max(0, self.pressure_metric.findData(
            getattr(request, "pressure_metric", "rms_pa"))))
        form.addRow("Pressure response to map", self.pressure_metric)
        existing_names = getattr(request, "variable_names", ())
        self._minimum_initial_points = 4 + sum(
            name in existing_names for name in ("power_kw", "split_rich"))
        for key, title, value in (
            ("power", "Nominal/fixed thermal input (kW)", getattr(request, "power_kw", 10)),
            ("split", "Nominal/fixed stage-1 fuel split (%)", 100 * getattr(request, "split_rich", 1)),
            ("reference", "Reporting reference O2 (dry vol%)", getattr(request, "reference_o2", 15)),
            ("initial", "Initial space-filling points",
             getattr(request, "initial_points", None) or self._minimum_initial_points),
            ("pool", "Candidate pool size", getattr(request, "candidate_pool_size", None) or 1024),
            ("window", "Minimum averaging window (s)", getattr(request, "window_seconds", 30)),
            ("mapping_weight", "NO mapping weight (0–1)",
             getattr(request, "mapping_no_weight", .5)),
        ):
            entry = QLineEdit(f"{value:g}")
            entry.setAccessibleName(title)
            self.entries[key] = entry
            if key == "reference":
                reference_field = QWidget()
                reference_row = QHBoxLayout(reference_field)
                reference_row.setContentsMargins(0, 0, 0, 0)
                reference_row.addWidget(entry, 1, Qt.AlignmentFlag.AlignVCenter)
                self.record_reference_button = QPushButton("Use current O₂")
                self.record_reference_button.setProperty("density", "compact")
                self.record_reference_button.setAccessibleName("Use current MEXA oxygen as reporting reference")
                self.record_reference_button.setToolTip(
                    "Copy one fresh, validated, uncorrected dry MEXA O2 reading. "
                    "Receiver logging must be enabled. This does not change the burner "
                    "or make the reference follow later readings.")
                self.record_reference_button.setEnabled(mexa is not None)
                self.record_reference_button.clicked.connect(self._record_reference_o2)
                reference_row.addWidget(self.record_reference_button)
                form.addRow(title, reference_field)
                self.record_reference_button.setMinimumHeight(
                    self.record_reference_button.sizeHint().height())
            else:
                form.addRow(title, entry)
        self.objective_mode.currentIndexChanged.connect(self._mapping_options)
        self._mapping_options()
        self.entries["mapping_weight"].setToolTip(
            "Fraction of mapping information assigned to NO; the remaining fraction is assigned to pressure.")
        layout.addLayout(form)
        self.reference_status = note(
            "O₂ reference is copied once or entered manually, then fixed for the campaign.")
        self.entries["reference"].textEdited.connect(lambda _text: self.reference_status.setText(
            "Manually entered O₂ reporting reference; fixed for the campaign."))
        layout.addWidget(self.reference_status)
        grid = QGridLayout()
        for column, title in enumerate(("Variable", "Lower", "Upper")):
            grid.addWidget(QLabel(title), 0, column)
        self.bounds = []
        for row, title in enumerate(("H2 in fuel (volume %)", "Stage-1 phi", "Overall phi"), 1):
            grid.addWidget(QLabel(title), row, 0)
            pair = []
            for column in (1, 2):
                entry = QLineEdit()
                entry.setPlaceholderText("Required")
                entry.setAccessibleName(f"{title} {'lower' if column == 1 else 'upper'}")
                if len(getattr(request, "bounds", ())) >= row:
                    value = request.bounds[row - 1][column - 1]
                    entry.setText(f"{value * (100 if row == 1 else 1):g}")
                grid.addWidget(entry, row, column)
                pair.append(entry)
            self.bounds.append(pair)
        self.optional = {}
        existing_bounds = dict(zip(existing_names, getattr(request, "bounds", ())))
        for row, (key, title, scale) in enumerate((
                ("power_kw", "Optimise thermal input (kW)", 1),
                ("split_rich", "Optimise stage-1 fuel split (%)", 100)), 4):
            enabled = key in existing_names
            check = QCheckBox(title)
            check.setChecked(enabled)
            grid.addWidget(check, row, 0)
            pair = []
            for column in (1, 2):
                entry = QLineEdit()
                entry.setPlaceholderText("Required if selected")
                entry.setAccessibleName(f"{title} {'lower' if column == 1 else 'upper'}")
                if enabled:
                    entry.setText(f"{existing_bounds[key][column - 1] * scale:g}")
                entry.setEnabled(enabled)
                check.toggled.connect(entry.setEnabled)
                grid.addWidget(entry, row, column)
                pair.append(entry)
            self.optional[key] = (check, pair, scale)
        for check, _pair, _scale in self.optional.values():
            check.toggled.connect(self._update_initial_points)
        layout.addLayout(grid)
        layout.addWidget(note(
            "The first three variables are always searched. Select power or fuel split only when "
            "they can be measured reliably and every value inside the bounds is approved."))
        layout.addWidget(note(
            f"Objective: dry NO × ({O2_CORRECTION_AIR_PERCENT:g} − reference O2) / "
            f"({O2_CORRECTION_AIR_PERCENT:g} − measured O2). "
            "Use uncorrected dry analyser readings. NO is not total NOx; NH3 slip, "
            "N2O and combustion efficiency are not measured or constrained here."))
        self.approved = QCheckBox("Search region and transition procedure checked\n"
                                  "Raw dry NO/O2 reporting basis verified")
        layout.addWidget(self.approved)
        self.error = note("")
        outer.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)
        self.config = None

    def _mapping_options(self):
        mapping = self.objective_mode.currentData() == "map_no_pressure"
        if mapping and self._dual_pressure:
            self.pressure_metric.setCurrentIndex(
                self.pressure_metric.findData("dominant_amplitude_pa"))
        self.pressure_metric.setEnabled(mapping and not self._dual_pressure)
        self.pressure_metric.setToolTip(
            "New mapping campaigns use dominant spectral amplitude for both pressure transducers."
            if mapping and self._dual_pressure else
            "Select the pressure response used by this existing one-transducer campaign."
            if mapping else "Pressure mapping is not selected.")
        self.entries["mapping_weight"].setEnabled(mapping)

    def _update_initial_points(self):
        minimum = 4 + sum(check.isChecked() for check, _pair, _scale in self.optional.values())
        entry = self.entries["initial"]
        if entry.text().strip() == str(self._minimum_initial_points):
            entry.setText(str(minimum))
        self._minimum_initial_points = minimum

    def accept(self):
        try:
            if not self.approved.isChecked():
                raise ValueError("Check the search region and reporting basis before continuing.")
            bounds = [[finite(entry.text(), "Bound") for entry in pair] for pair in self.bounds]
            bounds[0] = [value / 100 for value in bounds[0]]
            initial = finite(self.entries["initial"].text(), "Initial points")
            if not initial.is_integer():
                raise ValueError("Initial points must be a whole number.")
            pool = finite(self.entries["pool"].text(), "Candidate pool size")
            if not pool.is_integer():
                raise ValueError("Candidate pool size must be a whole number.")
            selected = {name: check.isChecked()
                        for name, (check, _pair, _scale) in self.optional.items()}
            for name, (_check, pair, scale) in self.optional.items():
                if selected[name]:
                    bounds.append([finite(entry.text(), "Bound") / scale for entry in pair])
            self.config = SearchConfig(
                power_kw=self.entries["power"].text(), bounds=bounds,
                split_rich=finite(self.entries["split"].text(), "Fuel split") / 100,
                reference_o2=self.entries["reference"].text(),
                initial_points=int(initial), window_seconds=self.entries["window"].text(),
                optimise_power=selected["power_kw"], optimise_split=selected["split_rich"],
                candidate_pool_size=int(pool), objective_mode=self.objective_mode.currentData(),
                pressure_metric=self.pressure_metric.currentData(),
                mapping_no_weight=finite(self.entries["mapping_weight"].text(), "Mapping NO weight"))
        except ValueError as exc:
            self.error.setText(str(exc))
            return
        super().accept()

    def _record_reference_o2(self):
        """Copy a checked reading into the draft reference without any device commands."""
        try:
            if self._mexa is None:
                raise ValueError("Connect the MEXA analyser before copying its O2 reading.")
            sample = self._mexa.checked_sample()
            value = finite(sample.packet["o2_percent"], "Current MEXA O2")
            if not 0 <= value < O2_CORRECTION_AIR_PERCENT:
                raise ValueError(f"Reference O2 must be below {O2_CORRECTION_AIR_PERCENT:g}%.")
        except ValueError as exc:
            self.error.setText(str(exc))
            return
        self.entries["reference"].setText(f"{value:.12g}")
        self.reference_status.setText(
            f"Copied dry O₂ {value:g}% from sample acquired {sample.packet['acquired_at']}. "
            "This reporting reference stays fixed for the campaign; burner flows are unchanged.")
        self.error.setText("")


class OptimiserPane(Card):
    def __init__(self, controller, parent=None):
        super().__init__("Bayesian optimiser", collapsed=not controller.expanded,
                         help_text="Live/manual NO/O2 experiments. Bayesian suggestions never actuate the rig. "
                         "The separate response test commands its confirmed A-to-B transition through the normal "
                         "setpoint and ramp safety path.",
                         parent=parent)
        self.controller = controller
        self._trial_id = None
        self.map_worker = None
        self._map_result = None
        self._map_signature = None
        self.toggled.connect(self._expanded)
        self.add(note("MEXA-584L  ·  live or manual NO + O2  ·  pilot off"))
        actions = QHBoxLayout()
        self.new_button = self._button("New experiment", self._new, actions)
        self.open_button = self._button("Open…", self._open, actions)
        self.add_layout(actions)
        self.summary = note("")
        self.add(self.summary)
        self.tabs = QTabWidget()
        self.add(self.tabs)
        self._build_test()
        self._build_history()
        self._build_maps()
        self._build_response()
        self.status = note(controller.last_message)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.add(self.status)
        controller.changed.connect(self.refresh)
        controller.message.connect(self.status.setText)
        controller.progress.connect(self.status.setText)
        controller.response.changed.connect(self.refresh)
        self.refresh()

    def _expanded(self, expanded):
        self.controller.expanded = bool(expanded)

    def _button(self, title, callback, layout):
        button = QPushButton(title)
        button.clicked.connect(lambda _checked=False: self._run(callback))
        layout.addWidget(button)
        return button

    def _run(self, callback):
        try:
            callback()
        except (ValueError, OSError) as exc:
            self.status.setText(str(exc))

    def _build_test(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.candidate = note("No pending test.")
        self.candidate.setObjectName("")
        self.candidate.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.candidate)
        line = QHBoxLayout()
        self.ask_button = self._button("Suggest next test", self.controller.ask, line)
        self.load_button = self._button("Load target fields", self.controller.prepare_targets, line)
        self.load_button.setToolTip("Populate the existing controller fields. Does not send commands.")
        layout.addLayout(line)
        layout.addWidget(note("Review all fields, then apply through the existing controls. "
                              "Switch off the pilot using your established procedure."))
        self.pilot = QCheckBox("Pilot is off throughout this measurement")
        self.settled = QCheckBox(
            "Burner and flows are settled; analyser is settled or a calibrated delay is available")
        layout.addWidget(self.pilot)
        layout.addWidget(self.settled)
        self.live = QCheckBox("Capture NO/O2 automatically from the MEXA network link")
        self.live.setChecked(self.controller.live_mode)
        self.live.toggled.connect(lambda checked: setattr(self.controller, "live_mode", checked))
        layout.addWidget(self.live)
        layout.addWidget(note("Connect in the MEXA analyser tab with receiver logging enabled. Live capture requires validated, "
                              "uncorrected dry readings, with no gaps or alarms. Simulation is excluded."))
        line = QHBoxLayout()
        self.start_button = self._button("Start window", self._start, line)
        self.finish_button = self._button("Finish window", self.controller.finish_window, line)
        self.cancel_button = self._button("Discard window", self.controller.cancel_window, line)
        layout.addLayout(line)
        self.window_label = note("No measurement window saved.")
        layout.addWidget(self.window_label)
        self.pressure_label = note("No pressure summary attached.")
        self.pressure_label.setObjectName("")
        self.pressure_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.pressure_label)
        self.labview_ids = note("")
        self.labview_ids.setObjectName("")
        self.labview_ids.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.labview_ids)
        self.labview_ids.hide()
        self.tdms_source_label = note("TDMS source not configured.")
        self.tdms_source_label.setObjectName("")
        self.tdms_source_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.tdms_source_label)
        self.labview_status = note("Arm locally, then use LabVIEW's existing log/stop controls.")
        self.labview_status.setObjectName("")
        layout.addWidget(self.labview_status)
        line = QHBoxLayout()
        self.arm_button = self._button("Arm LabVIEW trigger", self._arm_labview, line)
        self.disarm_button = self._button("Disarm", lambda: self.controller.disarm_labview(), line)
        self.disarm_button.setToolTip("Disable new LabVIEW start triggers. An active measurement continues; use Discard window to cancel it.")
        layout.addLayout(line)
        line = QHBoxLayout()
        self.tdms_source_button = self._button("TDMS source…", self._configure_tdms, line)
        self.pressure_import_button = self._button("Choose TDMS file…", self._import_tdms, line)
        self.labview_export_button = self._button("Export LabVIEW request…", self._export_labview, line)
        self.labview_export_button.hide()
        layout.addLayout(line)
        form = QFormLayout()
        self.inputs = {}
        for key, title, placeholder in (
            ("no", "Raw dry NO (ppm)", "Window average, 0–5000"),
            ("o2", "Dry O2 (vol%)", f"Same window, below {O2_CORRECTION_AIR_PERCENT:g}"),
            ("sem", "NO standard error (ppm)", "Optional; blank = unknown"),
            ("notes", "Notes", "Calibration, observations, limitations"),
        ):
            entry = QLineEdit(str(self.controller.draft.get(key, "")))
            entry.setPlaceholderText(placeholder)
            entry.setAccessibleName(title)
            entry.textChanged.connect(lambda value, name=key: self.controller.draft.update({name: value}))
            form.addRow(title, entry)
            self.inputs[key] = entry
        layout.addLayout(form)
        self.basis = QCheckBox("Uncorrected dry averages from this saved window")
        layout.addWidget(self.basis)
        layout.addWidget(note("Unknown noise is fitted by the model. Optional SEM covers NO only; "
                              "O2 uncertainty and calibration bias are not propagated. Live samples "
                              "may be autocorrelated, so their SD is recorded but not treated as SEM."))
        line = QHBoxLayout()
        self.save_button = self._button("Save result", self._save, line)
        self.invalid_button = self._button("Mark test invalid…", self._invalid, line)
        layout.addLayout(line)
        layout.addStretch(1)
        self.tabs.addTab(page, "Current test")

    def _build_history(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.best_label = note("No completed tests.")
        layout.addWidget(self.best_label)
        self.history = QListWidget()
        self.history.setFixedHeight(theme.scale(130))
        self.history.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.history.currentItemChanged.connect(self._selection)
        layout.addWidget(self.history)
        self.plot = pg.PlotWidget(background=theme.BG)
        self.plot.setFixedHeight(theme.scale(180))
        self.plot.setLabel("left", "Corrected dry NO", units="ppm")
        self.plot.setLabel("bottom", "Test number")
        self.plot.showGrid(x=True, y=True, alpha=.15)
        layout.addWidget(self.plot)
        self.outcome_plot = pg.PlotWidget(background=theme.BG)
        self.outcome_plot.setFixedHeight(theme.scale(180))
        self.outcome_plot.setLabel("bottom", "Corrected dry NO", units="ppm")
        self.outcome_plot.setLabel("left", "Pressure RMS", units="Pa")
        self.outcome_plot.setTitle("Observed outcomes")
        self.outcome_plot.showGrid(x=True, y=True, alpha=.15)
        self.outcome_plot.addLegend()
        layout.addWidget(self.outcome_plot)
        line = QHBoxLayout()
        self.repeat_button = self._button("Repeat selected", self._repeat, line)
        self.export_button = self._button("Export CSV…", self._export, line)
        layout.addLayout(line)
        layout.addWidget(note("Lowest observed value is provisional. Repeat promising points and "
                              "reference conditions; this does not prove a global minimum."))
        layout.addStretch(1)
        self.tabs.addTab(page, "History")

    def _build_maps(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(note(
            "Operating-space slices: select two variables. Other variables are fixed to the "
            "selected completed test's measured condition, or to their bounds midpoint. "
            "Colours show predicted responses; uncertainty shows latent standard deviation. "
            "Blank cells exceed configured bounds or flow ceilings. These maps do not "
            "establish a safe operating region."))
        row = QHBoxLayout()
        self.map_x, self.map_y = QComboBox(), QComboBox()
        self.map_x.setAccessibleName("Map horizontal variable")
        self.map_y.setAccessibleName("Map vertical variable")
        row.addWidget(QLabel("Horizontal"))
        row.addWidget(self.map_x)
        row.addWidget(QLabel("Vertical"))
        row.addWidget(self.map_y)
        layout.addLayout(row)
        self.map_x.currentIndexChanged.connect(self._map_axes_changed)
        self.map_y.currentIndexChanged.connect(self._map_axes_changed)
        self.map_slice_label = note("")
        self.map_slice_label.setObjectName("")
        layout.addWidget(self.map_slice_label)
        row = QHBoxLayout()
        self.map_refresh_button = self._button("Refresh maps", self._refresh_maps, row)
        self.map_uncertainty = QCheckBox("Show uncertainty (latent SD)")
        self.map_uncertainty.toggled.connect(self._draw_maps)
        row.addWidget(self.map_uncertainty)
        layout.addLayout(row)
        self.map_plots, self.map_images, self.map_colours = [], [], []
        for title, unit in (("Corrected dry NO", "ppm"), ("Pressure", "Pa"), ("Pressure", "Pa")):
            plot = pg.PlotWidget(background=theme.BG)
            plot.setFixedHeight(theme.scale(240))
            plot.setTitle(title)
            image = pg.ImageItem(axisOrder="row-major")
            plot.addItem(image)
            colours = pg.ColorBarItem(values=(0, 1), colorMap=pg.colormap.get("viridis"),
                                      label=unit, interactive=False)
            colours.setImageItem(image, insert_in=plot.getPlotItem())
            layout.addWidget(plot)
            self.map_plots.append(plot)
            self.map_images.append(image)
            self.map_colours.append(colours)
        self.map_status = note("Create a mapping campaign to view operating-space slices.")
        layout.addWidget(self.map_status)
        self.tabs.addTab(page, "Operating-space maps")

    def _map_axes_changed(self, *_args):
        if self.map_x.count() < 2:
            return
        if self.map_x.currentData() == self.map_y.currentData():
            other = self.map_y if self.sender() is self.map_x else self.map_x
            other.blockSignals(True)
            other.setCurrentIndex((other.currentIndex() + 1) % other.count())
            other.blockSignals(False)
        self._invalidate_maps()
        self._update_slice_label()

    def _slice(self):
        experiment = self.controller.experiment
        if experiment is None:
            return None
        item = self.history.currentItem()
        trial = next((t for t in experiment.trials if item
                      and t["id"] == item.data(Qt.ItemDataRole.UserRole)
                      and t["status"] == "completed"), None)
        fixed = (experiment.config.observed_vector(trial["window"]) if trial else
                 [sum(pair) / 2 for pair in experiment.config.bounds])
        origin = f"measured test {trial['number']}" if trial else "bounds midpoints"
        return fixed, origin

    def _update_slice_label(self):
        selected = self._slice()
        if selected is None:
            self.map_slice_label.setText("")
            return
        fixed, origin = selected
        config = self.controller.experiment.config
        axes = (self.map_x.currentData(), self.map_y.currentData())
        values = []
        for name, value in zip(config.variable_names, fixed):
            if name not in axes:
                label, unit, scale = MAP_VARIABLES[name]
                values.append(f"{label} = {value * scale:.5g} {unit}".strip())
        self.map_slice_label.setText(f"Fixed at {origin}: " + "; ".join(values))

    def _invalidate_maps(self):
        self._map_result = None
        for image in self.map_images:
            image.clear()
        self.map_status.setText("Slice changed. Refresh maps to calculate this slice.")

    def _refresh_maps(self):
        experiment = self.controller.experiment
        if experiment is None or self.map_worker is not None:
            return
        config = experiment.config
        names = config.variable_names
        x_name, y_name = self.map_x.currentData(), self.map_y.currentData()
        if x_name == y_name or x_name not in names or y_name not in names:
            raise ValueError("Select two different active variables.")
        fixed, origin = self._slice()
        xi, yi = names.index(x_name), names.index(y_name)
        x = np.linspace(*config.bounds[xi], 20)
        y = np.linspace(*config.bounds[yi], 20)
        xx, yy = np.meshgrid(x, y)
        points = np.tile(fixed, (400, 1))
        points[:, xi], points[:, yi] = xx.ravel(), yy.ravel()
        limits = self.controller.limits()
        feasible = []
        for point in points:
            try:
                targets = config.targets(point)
                feasible.append(all(targets.get(role, 0) <= ceiling
                                    for role, ceiling in limits.items()))
            except ValueError:
                feasible.append(False)
        feasible = np.asarray(feasible, dtype=bool)
        if not feasible.any():
            raise ValueError("This fixed slice has no points inside the bounds and current flow ceilings.")
        context = {"signature": self._current_map_signature(), "x": x, "y": y,
                   "x_name": x_name, "y_name": y_name,
                   "feasible": feasible,
                   "metric": config.pressure_metric,
                   "transducers": deepcopy(_dual_transducers(
                       getattr(self.controller, "tdms_source", None))),
                   "slice_label": self.map_slice_label.text()}
        self.map_worker = MappingWorker(config, experiment.trials, points, context)
        self.map_worker.succeeded.connect(self._maps_ready)
        self.map_worker.failed.connect(self.map_status.setText)
        self.map_worker.finished.connect(self._maps_finished)
        self.map_refresh_button.setEnabled(False)
        self.map_status.setText("Calculating 20 × 20 response maps in the background…")
        self.map_worker.start()

    def _current_map_signature(self):
        experiment = self.controller.experiment
        if experiment is None:
            return None
        fixed, _origin = self._slice()
        return (str(experiment.path), repr(experiment.trials), self.map_x.currentData(),
                self.map_y.currentData(), tuple(fixed), tuple(sorted(self.controller.limits().items())),
                repr(getattr(self.controller, "tdms_source", None)))

    def _maps_ready(self, payload):
        result, context = payload
        if context["signature"] != self._current_map_signature():
            self.map_status.setText("Campaign or slice changed. Refresh maps for current data.")
            return
        self._map_result = payload
        self._draw_maps()
        self.map_status.setText("Predictions on a 20 × 20 slice. " + context["slice_label"])

    def _maps_finished(self):
        self.map_worker = None
        self._update_map_controls()

    def _draw_maps(self, *_args):
        if self._map_result is None:
            return
        result, context = self._map_result
        uncertainty = self.map_uncertainty.isChecked()
        suffix = "sd" if uncertainty else "mean"
        xl, xu, xs = MAP_VARIABLES[context["x_name"]]
        yl, yu, ys = MAP_VARIABLES[context["y_name"]]
        x, y = context["x"] * xs, context["y"] * ys
        dx, dy = x[1] - x[0], y[1] - y[0]
        transducers = context.get("transducers")
        descriptors = ([('no', "Corrected dry NO", "ppm")]
                       + ([(item["id"], item["label"], "Pa RMS") for item in transducers]
                          if transducers else
                          [("pressure", PRESSURE_LABELS[context["metric"]],
                            PRESSURE_UNITS[context["metric"]])]))
        for index, (key, label, unit) in enumerate(descriptors):
            values = np.asarray(result[f"{key}_{suffix}"]).reshape(20, 20)
            image, plot = self.map_images[index], self.map_plots[index]
            image.setImage(values, autoLevels=False)
            image.setRect(QRectF(x[0] - dx / 2, y[0] - dy / 2,
                                x[-1] - x[0] + dx, y[-1] - y[0] + dy))
            low, high = float(np.nanmin(values)), float(np.nanmax(values))
            self.map_colours[index].setLevels((low, high if high > low else low + 1e-9))
            self.map_colours[index].getAxis("left").setLabel(unit)
            plot.setLabel("bottom", xl, units=xu or None)
            plot.setLabel("left", yl, units=yu or None)
            title = f"{label} ({unit})"
            plot.setTitle(title + (" — latent SD" if uncertainty else " — predicted mean"))
            plot.setRange(xRange=(x[0], x[-1]), yRange=(y[0], y[-1]), padding=0)
        for index, plot in enumerate(self.map_plots):
            plot.setVisible(index < len(descriptors))

    def _update_map_controls(self):
        experiment = self.controller.experiment
        mapping = bool(experiment and experiment.config.objective_mode == "map_no_pressure")
        dual = bool(_dual_transducers(getattr(self.controller, "tdms_source", None)))
        for index, plot in enumerate(self.map_plots):
            plot.setVisible(index < (3 if dual else 2))
        count = sum(t["status"] == "completed" for t in experiment.trials) if experiment else 0
        self.map_refresh_button.setEnabled(bool(mapping and count >= experiment.config.initial_points
                                               and self.map_worker is None and not self.controller.busy))

    def closeEvent(self, event):
        if self.map_worker is not None:
            self.map_worker.requestInterruption()
        super().closeEvent(event)

    def _build_response(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        layout.addWidget(note(
            "Store two settled live flow conditions. Start first records a stable NO baseline at A, then "
            "commands B through the existing setpoint/ramp rules and waits for a sustained NO change and "
            "stable plateau. This measures the combined burner, sample-line and analyser path. An "
            "analyser-only test requires a calibration-gas step at its inlet."))
        grid = QGridLayout()
        self.response_labels = {}
        self.response_store_buttons = {}
        for row, label_name in enumerate(("A", "B")):
            title = QLabel(f"Condition {label_name}")
            title.setObjectName("SectionLabel")
            grid.addWidget(title, row, 0)
            description = note("Not stored.")
            description.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            grid.addWidget(description, row, 1)
            button = QPushButton(f"Store live as {label_name}")
            button.clicked.connect(
                lambda _checked=False, label=label_name: self._run(
                    lambda: self.controller.response.store_condition(label)))
            grid.addWidget(button, row, 2)
            self.response_labels[label_name] = description
            self.response_store_buttons[label_name] = button
        layout.addLayout(grid)
        line = QHBoxLayout()
        self.response_start_button = QPushButton("Start A → B response test")
        self.response_start_button.setProperty("variant", "accent")
        self.response_start_button.clicked.connect(self._start_response)
        self.response_cancel_button = QPushButton("Cancel response test")
        self.response_cancel_button.clicked.connect(
            lambda _checked=False: self.controller.response.cancel())
        line.addWidget(self.response_start_button)
        line.addWidget(self.response_cancel_button)
        layout.addLayout(line)
        self.response_result = note("No completed response calibration.")
        self.response_result.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.response_result)
        self.response_status = note(self.controller.response.last_message)
        self.response_status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.response_status)
        self.controller.response.message.connect(self.response_status.setText)
        self.controller.response.progress.connect(self.response_status.setText)
        layout.addWidget(note(
            "For future live optimiser windows, the selected calibration is used as a pre-averaging "
            "delay. Transient readings remain in the receiver audit log but are excluded from the "
            "Bayesian NO/O2 mean."))
        layout.addStretch(1)
        self.tabs.addTab(page, "NO response time")

    def _start_response(self):
        try:
            transition = self.controller.response.transition_text()
        except (ValueError, OSError) as exc:
            self.response_status.setText(str(exc))
            return
        answer = QMessageBox.warning(
            self, "Start live A-to-B response test?",
            "This action will command condition B after a 15 s stable NO baseline at A. "
            "It does not automatically return to A or zero the rig if cancelled. Verify that the "
            "entire transition is approved and that the normal recovery controls are available.\n\n"
            + transition,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer == QMessageBox.StandardButton.Yes:
            self._run(lambda: self.controller.response.start(confirmed=True))

    def _switch_ok(self):
        experiment = self.controller.experiment
        if experiment and experiment.pending:
            return QMessageBox.question(
                self, "Switch experiment?", "The pending test is saved in its experiment file. "
                "Unsaved measurement text will be cleared. Continue?") == QMessageBox.StandardButton.Yes
        return True

    def _new(self):
        if not self._switch_ok():
            return
        dialog = ExperimentDialog(self.controller.session.autocalc_request, self,
                                  mexa=self.controller.session.mexa, dual_pressure=True)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Create experiment", str(DEFAULT_LOG_DIR / "experiments" / ("ammonia-no" + SUFFIX)),
            "Bayesian experiment (*.fcbo.json)")
        if path:
            if not path.lower().endswith(SUFFIX):
                path += SUFFIX
            self.controller.create(path, dialog.config)

    def _open(self):
        if not self._switch_ok():
            return
        path, _ = QFileDialog.getOpenFileName(self, "Open experiment",
                                            str(DEFAULT_LOG_DIR / "experiments"),
                                            "Bayesian experiment (*.fcbo.json)")
        if path:
            self.controller.load(path)

    def _start(self):
        self.controller.start_window(self.pilot.isChecked(), self.settled.isChecked(), live=self.live.isChecked())

    def _arm_labview(self):
        self.controller.arm_labview(pilot_off=self.pilot.isChecked(),
                                   settled=self.settled.isChecked(), live=self.live.isChecked())

    def _import_pressure(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import pressure summary or waveform manifest",
                                            str(DEFAULT_LOG_DIR), "Pressure JSON (*.json)")
        if path:
            self.controller.import_pressure(path)

    def _configure_tdms(self):
        dialog = TdmsSourceDialog(getattr(self.controller, "tdms_source", None), self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.controller.configure_tdms_source(dialog.source)

    def _import_tdms(self):
        source = getattr(self.controller, "tdms_source", None) or {}
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose the TDMS recording for this test", source.get("folder") or str(DEFAULT_LOG_DIR),
            "TDMS (*.tdms)")
        if path:
            self.controller.import_tdms(path)

    def _export_labview(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export LabVIEW request",
                                            str(DEFAULT_LOG_DIR / "labview-request.json"), "JSON (*.json)")
        if path:
            self.controller.export_labview_request(path)

    def _save(self):
        pending = self.controller.experiment.pending if self.controller.experiment else None
        if pending and (pending.get("window") or {}).get("mexa"):
            self.controller.complete_from_mexa(self.inputs["notes"].text(), self.basis.isChecked())
            return
        sem = self.inputs["sem"].text().strip()
        self.controller.complete(self.inputs["no"].text(), self.inputs["o2"].text(),
                                 sem or None, self.inputs["notes"].text(), self.basis.isChecked())

    def _invalid(self):
        reason, ok = QInputDialog.getText(self, "Mark test invalid", "Reason (excluded from the model):")
        if ok:
            self.controller.invalidate(reason)

    def _repeat(self):
        item = self.history.currentItem()
        if item:
            self.controller.repeat(item.data(Qt.ItemDataRole.UserRole))
            self.tabs.setCurrentIndex(0)

    def _export(self):
        experiment = self.controller.experiment
        if experiment is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export experiment results",
                                            str(experiment.path.with_suffix(".csv")), "CSV (*.csv)")
        if path:
            experiment.export_csv(path)
            self.status.setText(f"CSV exported to {path}")

    def _selection(self, *_args):
        experiment = self.controller.experiment
        item = self.history.currentItem()
        trial = next((t for t in experiment.trials if item and t["id"] == item.data(Qt.ItemDataRole.UserRole)),
                     None) if experiment else None
        self.repeat_button.setEnabled(bool(trial and trial["status"] == "completed"
                                           and not experiment.pending and not self.controller.busy))
        self._update_slice_label()
        if self._map_result and self._map_result[1]["signature"] != self._current_map_signature():
            self._invalidate_maps()

    def refresh(self):
        c = self.controller
        experiment = c.experiment
        pending = experiment.pending if experiment else None
        trial_id = pending["id"] if pending else None
        if trial_id != self._trial_id:
            self._trial_id = trial_id
            for key, entry in self.inputs.items():
                entry.setText(str(c.draft.get(key, "")))
            for box in (self.pilot, self.settled, self.basis):
                box.setChecked(False)
        response_active = c.response.active
        busy = c.busy or response_active
        armed = bool(getattr(c, "labview_armed", False))
        capturing = c.capture is not None or c.settle_wait is not None
        window = pending.get("window") if pending else None
        labview_capture = (window or {}).get("labview_capture")
        legacy_capture_active = bool(getattr(c, "legacy_capture_active", False))
        tail = bool(getattr(c, "legacy_collecting_after_stop", False))
        auto_pending = bool(getattr(c, "tdms_auto_pending", False))
        pressure = pending.get("pressure") if pending else None
        mapping = bool(experiment and experiment.config.objective_mode == "map_no_pressure")
        mexa = (window or {}).get("mexa")
        self.live.setEnabled(bool(pending and not capturing and not window and not busy and not armed))
        self.pilot.setEnabled(not capturing and not window and not armed)
        self.settled.setEnabled(not capturing and not window and not armed)
        self.new_button.setEnabled(not busy and not capturing)
        self.open_button.setEnabled(not busy and not capturing)
        self.ask_button.setEnabled(bool(experiment and not pending and not busy))
        self.load_button.setEnabled(bool(pending and not busy and not capturing and not window and not armed))
        self.start_button.setEnabled(bool(pending and not busy and not capturing and not window
                                          and not armed and not mapping))
        self.start_button.setToolTip(
            "Use Arm LabVIEW trigger for pressure mapping so the TDMS interval is recorded."
            if mapping else "Start a manual NO measurement window.")
        self.finish_button.setEnabled(c.capture is not None and not legacy_capture_active)
        self.cancel_button.setEnabled(capturing)
        self.save_button.setEnabled(bool(window and not capturing and not busy and (pressure or not mapping)))
        self.arm_button.setEnabled(bool(pending and not busy and not capturing and not window and not armed))
        self.disarm_button.setEnabled(armed)
        self.pressure_import_button.setEnabled(bool(window and labview_capture and not pressure
                                                  and not busy and not capturing))
        self.tdms_source_button.setEnabled(bool(experiment and not armed and not capturing
                                               and not busy and not pressure))
        self.labview_export_button.setEnabled(bool(pending and not busy))
        def pressure_value(key):
            value = (pressure or {}).get(key)
            return "—" if value is None else f"{value:.5g}"
        measured = _dual_transducers(pressure)
        if measured:
            lines = []
            for item in measured:
                metric = lambda key, current=item: (_transducer_metric(current, key))
                shown = lambda key: "—" if metric(key) is None else f"{metric(key):.5g}"
                lines.append(
                    f"{item['label']}: RMS {shown('rms_pa')} Pa · peak excursion {shown('peak_abs_pa')} Pa\n"
                    f"Dominant frequency {shown('dominant_frequency_hz')} Hz · spectral amplitude "
                    f"{shown('dominant_amplitude_pa')} Pa RMS")
            self.pressure_label.setText("\n".join(lines))
        else:
            self.pressure_label.setText(
                "Pressure: RMS " + pressure_value("rms_pa") + " Pa · peak excursion "
                + pressure_value("peak_abs_pa") + " Pa\nDominant frequency "
                + pressure_value("dominant_frequency_hz") + " Hz · spectral amplitude "
                + pressure_value("dominant_amplitude_pa") + " Pa RMS" if pressure else
                ("Pressure summary required before saving this mapping result." if mapping else
                 "No pressure summary attached."))
        if getattr(c, "pressure_worker", None) is not None:
            self.pressure_label.setText("Processing pressure data in the background…")
        source = getattr(c, "tdms_source", None)
        source_transducers = _dual_transducers(source)
        if source_transducers:
            waveforms = "\n".join(
                f"{item['label']}: {item['group']} / {item['channel']}"
                for item in source_transducers)
            self.tdms_source_label.setText(f"TDMS folder: {source['folder']}\n{waveforms}")
        else:
            self.tdms_source_label.setText(
                f"TDMS folder: {source['folder']}\nWaveform: {source['group']} / {source['channel']}"
                if source else "TDMS source not configured. Choose a folder and calibrated waveform channel.")
        if tail:
            remaining = max(0, float(getattr(c, "labview_tail_remaining_s", 0)))
            self.labview_status.setText(
                f"LabVIEW has stopped. At least {remaining:.1f} s remaining; waiting for full fresh "
                "NO/flow coverage after the analyser delay and minimum NO window. Keep this condition steady. "
                "The app will then find and process the TDMS recording.")
        elif legacy_capture_active:
            self.labview_status.setText(
                "LabVIEW recording active. Use its existing stop control; NO collection may "
                "continue afterwards. Keep the burner and flows steady.")
        elif auto_pending:
            self.labview_status.setText(
                "NO window saved. Waiting for a matching TDMS recording in the configured folder. "
                "You can choose the recording manually if needed.")
        else:
            self.labview_status.setText(
                "Armed for LabVIEW's existing log/stop controls. Keep this condition steady through "
                "the extra NO collection after stop." if armed else
                "Arm locally, then use LabVIEW's existing log/stop controls. No LabVIEW changes are needed.")
        if pending:
            try:
                request = c.labview_request()
                self.labview_ids.setText(("LabVIEW armed" if armed else "LabVIEW disarmed") + "\n"
                    + "\n".join(f"{key}: {request[key]}" for key in
                                ("experiment_id", "trial_id", "capture_id")))
            except (ValueError, AttributeError) as exc:
                self.labview_ids.setText(str(exc))
        else:
            self.labview_ids.setText("")
        self.invalid_button.setEnabled(bool(pending and not capturing and not busy))
        self.export_button.setEnabled(bool(experiment and not busy))
        for entry in self.inputs.values():
            entry.setEnabled(bool(pending))
        for key in ("no", "o2", "sem"):
            self.inputs[key].setReadOnly(bool(mexa))
        if mexa:
            self.inputs["no"].setText(f"{mexa['no_ppm']:.8g}")
            self.inputs["o2"].setText(f"{mexa['o2_percent']:.8g}")
            self.inputs["sem"].setText("")
        self.summary.setText("Create a campaign with approved variable bounds and O2 reference."
                             if not experiment else
                             f"{experiment.path.name}\n{experiment.config.dimensions} variables · "
                             f"{experiment.config.initial_points} initial points · "
                             f"NO @ {experiment.config.reference_o2:g}% O2"
                             + ((" · map NO + two pressure peak spectra"
                                 if _dual_transducers(getattr(c, "tdms_source", None)) else
                                 f" · map NO + {PRESSURE_LABELS[experiment.config.pressure_metric]}")
                                if mapping else ""))
        if experiment:
            names = ", ".join(experiment.config.variable_names)
            self.summary.setToolTip(str(experiment.path) + f"\nVariables: {names}\nBounds: "
                                    + str(experiment.config.bounds))
        self.candidate.setText("No pending test." if not pending else
                               f"Test {pending['number']} · {pending['method']}\n"
                               + point_text(experiment.config, pending["point"]))
        self.window_label.setText(
            f"Saved window: {window['duration_s']:.1f} s, {window['samples']} passes\n"
            f"{window['start']} → {window['end']}"
            + (f"\nMEXA: {mexa['samples']} samples · NO SD {mexa['no_sd']:.3g} ppm · "
               f"O2 SD {mexa['o2_sd']:.3g}%" if mexa else "") if window else
            ("Waiting through the calibrated analyser-response delay before averaging."
             if c.settle_wait else
             ("Window capture in progress." if c.capture else "No measurement window saved.")))
        can_store_response = bool(experiment and not busy and not capturing)
        for button in self.response_store_buttons.values():
            button.setEnabled(can_store_response)
        conditions = {}
        for label_name in ("A", "B"):
            condition = c.response.condition(label_name)
            conditions[label_name] = condition
            if condition:
                flows = " · ".join(
                    f"{key} {value:g}" for key, value in condition["target_flows"].items())
                self.response_labels[label_name].setText(
                    f"{condition['captured_at']}\n{flows} SLPM")
            else:
                self.response_labels[label_name].setText("Not stored.")
        self.response_start_button.setEnabled(bool(
            all(conditions.values()) and not busy and not capturing))
        self.response_cancel_button.setEnabled(response_active)
        run = experiment.selected_response_run if experiment else None
        self.response_result.setText(
            "No completed response calibration." if not run else
            f"Selected response: change {run['command_to_change_s']:.1f} s · "
            f"stable {run['command_to_stable_s']:.1f} s from command · "
            f"{run['flow_to_stable_s']:.1f} s after B flow stability\n"
            f"Pre-averaging delay {run['recommended_delay_s']:g} s · averaging "
            f"{experiment.config.window_seconds:g} s · total live observation "
            f"{experiment.total_live_logging_seconds:g} s")
        selected = self.history.currentItem()
        selected_id = selected.data(Qt.ItemDataRole.UserRole) if selected else None
        self.history.blockSignals(True)
        self.history.clear()
        completed = []
        for t in experiment.trials if experiment else []:
            outcome = f"{t['result']['corrected_no']:.3f} ppm" if t["status"] == "completed" else t["status"]
            metric = experiment.config.pressure_metric
            trial_transducers = _dual_transducers(t.get("pressure"))
            if trial_transducers:
                for transducer in trial_transducers:
                    value = _transducer_metric(transducer, "dominant_amplitude_pa")
                    if value is not None:
                        outcome += f" · {transducer['label']} {value:.4g} Pa RMS"
            elif metric in (t.get("pressure") or {}):
                outcome += f" · {PRESSURE_LABELS[metric]} {t['pressure'][metric]:.4g} {PRESSURE_UNITS[metric]}"
            item = QListWidgetItem(f"#{t['number']}  {outcome}  ·  "
                                   + point_text(experiment.config, t["point"]))
            item.setData(Qt.ItemDataRole.UserRole, t["id"])
            item.setToolTip(str(t))
            self.history.addItem(item)
            if t["id"] == selected_id:
                self.history.setCurrentItem(item)
            if t["status"] == "completed":
                completed.append(t)
        self.history.blockSignals(False)
        self.plot.clear()
        self.outcome_plot.clear()
        self.outcome_plot.setVisible(bool(mapping or any(t.get("pressure") for t in completed)))
        if experiment:
            metric = experiment.config.pressure_metric
            dual_trials = [t for t in completed if _dual_transducers(t.get("pressure"))]
            if dual_trials:
                self.outcome_plot.setLabel("left", "Dominant spectral amplitude", units="Pa RMS")
                for index, sensor_id in enumerate(TdmsSourceDialog.TRANSDUCER_IDS):
                    paired = []
                    for trial in dual_trials:
                        item = next((entry for entry in _dual_transducers(trial["pressure"])
                                     if entry["id"] == sensor_id), None)
                        value = _transducer_metric(item, "dominant_amplitude_pa")
                        if value is not None:
                            paired.append((trial, item, value))
                    if paired:
                        self.outcome_plot.plot(
                            [trial["result"]["corrected_no"] for trial, _item, _value in paired],
                            [value for _trial, _item, value in paired], pen=None,
                            symbol="o" if index == 0 else "t", symbolSize=8,
                            symbolBrush=theme.TEXT_BRIGHT, name=paired[0][1]["label"])
            else:
                self.outcome_plot.setLabel("left", "Pressure " + PRESSURE_LABELS[metric], units=PRESSURE_UNITS[metric])
                paired = [t for t in completed if metric in (t.get("pressure") or {})]
                self.outcome_plot.plot([t["result"]["corrected_no"] for t in paired],
                                       [t["pressure"][metric] for t in paired], pen=None,
                                       symbol="o", symbolSize=8, symbolBrush=theme.TEXT_BRIGHT)
        if completed:
            best = min(completed, key=lambda t: t["result"]["corrected_no"])
            self.best_label.setText(f"Lowest observed: {best['result']['corrected_no']:.3f} ppm "
                                   f"at {experiment.config.reference_o2:g}% O2 · test {best['number']}")
            self.plot.plot([t["number"] for t in completed], [t["result"]["corrected_no"] for t in completed],
                           pen=None, symbol="o", symbolSize=7, symbolBrush=theme.TEXT_BRIGHT)
        else:
            self.best_label.setText("No completed tests.")
        names = experiment.config.variable_names if experiment else ()
        if tuple(self.map_x.itemData(i) for i in range(self.map_x.count())) != names:
            for combo in (self.map_x, self.map_y):
                combo.blockSignals(True)
                combo.clear()
                for name in names:
                    combo.addItem(MAP_VARIABLES[name][0], name)
            self.map_y.setCurrentIndex(1 if len(names) > 1 else -1)
            for combo in (self.map_x, self.map_y):
                combo.blockSignals(False)
            self._invalidate_maps()
        self._update_map_controls()
        self._selection()
