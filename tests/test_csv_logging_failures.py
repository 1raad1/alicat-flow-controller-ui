"""Regression checks for CSV faults during a live polling pass."""

from __future__ import annotations

import os
from datetime import datetime
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from flow_controller.core.csv_logger import CsvLogger
from flow_controller.core.session import FlowSession


STAMP = datetime(2026, 1, 2, 3, 4, 5)


class FailingWriter:
    def writerow(self, _row):
        raise OSError("disk full")


class FailingFlushFile:
    def __init__(self, handle):
        self.handle = handle

    def flush(self):
        raise OSError("flush failed")

    def fileno(self):
        return self.handle.fileno()

    def close(self):
        self.handle.close()


def inject_fault(logger, fault):
    if fault == "row":
        logger._writer = FailingWriter()
    else:
        logger._file = FailingFlushFile(logger._file)


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.mark.parametrize("fault,detail", [
    ("row", "disk full"),
    ("flush", "flush failed"),
])
def test_csv_logger_records_write_and_flush_errors(tmp_path, fault, detail):
    logger = CsvLogger()
    path = tmp_path / "direct.csv"
    logger.start(path, {})
    try:
        assert logger.write_row({}, (None, None, None), STAMP)
        assert logger.last_error is None
        inject_fault(logger, fault)
        assert logger.write_row({}, (None, None, None), STAMP) is False
        assert logger.last_error == f"OSError: {detail}"
    finally:
        logger.stop()

    assert path.exists()
    assert "timestamp" in path.read_text()


@pytest.mark.parametrize("fault,detail", [
    ("row", "disk full"),
    ("flush", "flush failed"),
])
def test_session_reports_one_csv_fault_without_stopping_control(
    tmp_path, fault, detail,
):
    session = FlowSession(worker=SimpleNamespace(shutdown=lambda: None))
    try:
        changes = []
        failures = []
        posted = []
        session.logging_changed.connect(lambda active, path: changes.append((active, path)))
        session.failed.connect(lambda title, message: failures.append((title, message)))
        session._post = lambda callback, *args: posted.append((callback, args))

        path = tmp_path / "live.csv"
        assert session.start_logging(path)
        assert session._csv.active
        session.is_monitoring = True
        pending = object()
        session.setpoint_queue.put(pending)

        session._write_log_row({}, STAMP)
        assert session._csv.active
        assert posted == []

        inject_fault(session._csv, fault)
        session._write_log_row({}, STAMP)
        assert not session._csv.active
        assert session._csv.last_error == f"OSError: {detail}"
        assert len(posted) == 1
        assert changes == [(True, str(path))]
        assert failures == []
        assert session.is_monitoring
        assert session.setpoint_queue.get_nowait() is pending

        callback, args = posted.pop()
        callback(*args)
        assert changes == [(True, str(path)), (False, str(path))]
        assert len(failures) == 1
        assert failures[0][0] == "Logging"
        assert str(path) in failures[0][1]
        assert detail in failures[0][1]
        assert "Live monitoring continues" in failures[0][1]

        session._write_log_row({}, STAMP)
        assert posted == []
        assert len(failures) == 1
        assert session.is_monitoring
        assert path.exists()
    finally:
        session.shutdown()


def test_queued_failure_does_not_deactivate_a_restarted_log(tmp_path):
    session = FlowSession(worker=SimpleNamespace(shutdown=lambda: None))
    try:
        changes = []
        failures = []
        posted = []
        session.logging_changed.connect(lambda active, path: changes.append((active, path)))
        session.failed.connect(lambda title, message: failures.append((title, message)))
        session._post = lambda callback, *args: posted.append((callback, args))

        old_path = tmp_path / "old.csv"
        new_path = tmp_path / "new.csv"
        assert session.start_logging(old_path)
        inject_fault(session._csv, "row")
        session._write_log_row({}, STAMP)
        assert len(posted) == 1
        assert session.start_logging(new_path)

        callback, args = posted.pop()
        callback(*args)
        assert session._csv.active
        assert session._csv.path == str(new_path)
        assert changes == [(True, str(old_path)), (True, str(new_path))]
        assert len(failures) == 1
        assert failures[0][0] == "Logging"

        session._write_log_row({}, STAMP)
        assert session._csv.active
        assert posted == []
    finally:
        session.shutdown()
