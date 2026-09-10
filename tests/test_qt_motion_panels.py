"""Regression checks for animated clipping splitters."""

from __future__ import annotations

import os
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QSize, Qt, QVariantAnimation
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QSplitter, QWidget

from flow_controller.ui.qt_motion_panels import MotionSplitter


class _Panel(QWidget):
    def __init__(self, width=120, height=90):
        super().__init__()
        self._hint = QSize(width, height)

    def sizeHint(self):
        return self._hint

    def minimumSizeHint(self):
        return self._hint


class MotionSplitterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(sys.argv)

    def make_splitter(self, orientation=Qt.Orientation.Horizontal):
        splitter = MotionSplitter(orientation)
        first, second = _Panel(), _Panel()
        splitter.addWidget(first)
        splitter.addWidget(second)
        splitter.configure_panel(0, "Controls", collapsible=False)
        splitter.configure_panel(1, "Details", collapsible=True)
        splitter.resize(607 if orientation == Qt.Orientation.Horizontal else 400,
                        400 if orientation == Qt.Orientation.Horizontal else 607)
        splitter.show()
        QTest.qWait(10)
        splitter.set_default_sizes([2, 1])
        QApplication.processEvents()
        self.addCleanup(splitter.close)
        return splitter, first, second

    def test_original_widgets_are_kept_mounted_inside_clipping_panes(self):
        splitter, first, second = self.make_splitter()
        self.assertIs(splitter.content_widget(0), first)
        self.assertIs(splitter.content_widget(1), second)
        self.assertIs(first.parentWidget(), splitter.widget(0))
        self.assertTrue(first.isVisibleTo(splitter))

    def test_keyboard_resizes_horizontal_and_vertical(self):
        for orientation, grow, shrink in (
                (Qt.Orientation.Horizontal, Qt.Key.Key_Right, Qt.Key.Key_Left),
                (Qt.Orientation.Vertical, Qt.Key.Key_Down, Qt.Key.Key_Up)):
            with self.subTest(orientation=orientation):
                splitter, _, _ = self.make_splitter(orientation)
                before = splitter.sizes()[0]
                QTest.keyClick(splitter.handle(1), grow)
                self.assertGreater(splitter.sizes()[0], before)
                QTest.keyClick(splitter.handle(1), shrink,
                               Qt.KeyboardModifier.ShiftModifier)
                self.assertLess(splitter.sizes()[0], before)

    def test_collapse_reopen_remembers_extent_and_keeps_other_usable(self):
        splitter, _, second = self.make_splitter()
        splitter.setSizes([360, 240])
        splitter.set_panel_collapsed(1, True, animate=False)
        self.assertEqual(splitter.sizes()[1], 0)
        self.assertGreaterEqual(splitter.sizes()[0], 120)
        self.assertFalse(second.isHidden())
        self.assertFalse(second.isEnabled())
        splitter.set_panel_collapsed(1, False, animate=False)
        self.assertAlmostEqual(splitter.sizes()[1], 240, delta=2)
        self.assertTrue(second.isEnabled())

    def test_reset_reenables_viewport_and_keeps_content_state(self):
        splitter, _, second = self.make_splitter()
        splitter.set_panel_collapsed(1, True, animate=False)
        second.setEnabled(False)
        splitter.reset_layout(animate=False)
        self.assertTrue(splitter.widget(1).isEnabled())
        self.assertFalse(second.isEnabled())

    def test_rapid_reversal_and_instant_request_settle(self):
        splitter, _, _ = self.make_splitter()
        changes = []
        splitter.panelCollapsedChanged.connect(lambda *args: changes.append(args))
        splitter.set_panel_collapsed(1, True, animate=True)
        QTest.qWait(30)
        splitter.set_panel_collapsed(1, False, animate=True)
        QTest.qWait(splitter.ANIMATION_DURATION_MS + 30)
        self.assertGreater(splitter.sizes()[1], 0)
        splitter.set_panel_collapsed(1, True, animate=True)
        splitter.set_panel_collapsed(1, False, animate=False)
        self.assertGreater(splitter.sizes()[1], 0)
        self.assertEqual(changes[:2], [(1, True), (1, False)])

    def test_reset_restores_default_ratio(self):
        splitter, _, _ = self.make_splitter()
        splitter.setSizes([150, 450])
        splitter.reset_layout(animate=False)
        first, second = splitter.sizes()
        self.assertAlmostEqual(first / second, 2.0, delta=.04)

    def test_drag_release_collapses_and_notifies(self):
        splitter, _, _ = self.make_splitter()
        changes = []
        splitter.panelCollapsedChanged.connect(lambda *args: changes.append(args))
        # Native handle movement can pass below the open endpoint minimum.
        QSplitter.setSizes(splitter, [590, 10])
        splitter._finish_drag()
        self.assertEqual(splitter.sizes()[1], 0)
        self.assertIn((1, True), changes)

    def test_non_collapsible_panel_retains_content_minimum(self):
        splitter, _, _ = self.make_splitter()
        splitter.setSizes([0, 600])
        splitter._finish_drag()
        self.assertGreaterEqual(splitter.sizes()[0], 120)

    def test_mouse_drag_folds_and_restores_previous_extent(self):
        splitter, _, _ = self.make_splitter()
        before = splitter.sizes()[1]
        handle = splitter.handle(1)
        centre = handle.rect().center()
        end = centre + QPoint(before - 20, 0)
        QTest.mousePress(handle, Qt.MouseButton.LeftButton, pos=centre)
        QTest.mouseMove(handle, end)
        QTest.mouseRelease(handle, Qt.MouseButton.LeftButton, pos=end)
        self.assertEqual(splitter.sizes()[1], 0)
        splitter.set_panel_collapsed(1, False, animate=False)
        self.assertAlmostEqual(splitter.sizes()[1], before, delta=2)

    def test_repeated_folds_reuse_animation_and_preserve_disabled_content(self):
        splitter, _, second = self.make_splitter()
        for _ in range(10):
            splitter.set_panel_collapsed(1, True)
            splitter.set_panel_collapsed(1, False)
        splitter.set_panel_collapsed(1, True, animate=False)
        second.setEnabled(False)
        splitter.reset_layout(animate=False)
        self.assertTrue(splitter.widget(1).isEnabled())
        self.assertFalse(second.isEnabled())
        self.assertEqual(len(splitter.findChildren(QVariantAnimation)), 1)

    def test_defaults_are_ratios_and_open_endpoints_respect_minimum(self):
        splitter, _, _ = self.make_splitter()
        first, second = splitter.sizes()
        self.assertAlmostEqual(first / second, 2.0, delta=.04)
        splitter.set_default_sizes([1, 20])
        self.assertGreaterEqual(splitter.sizes()[0], 120)

    def test_arrow_reopens_folded_panel_at_its_minimum(self):
        splitter, _, _ = self.make_splitter()
        splitter.set_panel_collapsed(1, True, animate=False)
        QTest.keyClick(splitter.handle(1), Qt.Key.Key_Left)
        self.assertGreaterEqual(splitter.sizes()[1], 120)

    def test_home_collapses_and_end_reopens_designated_panel(self):
        splitter, _, second = self.make_splitter()
        QTest.keyClick(splitter.handle(1), Qt.Key.Key_Home)
        QTest.qWait(splitter.ANIMATION_DURATION_MS + 20)
        self.assertTrue(splitter.is_panel_collapsed(1))
        QTest.keyClick(splitter.handle(1), Qt.Key.Key_End)
        QTest.qWait(splitter.ANIMATION_DURATION_MS + 20)
        self.assertFalse(splitter.is_panel_collapsed(1))
        self.assertTrue(second.isEnabled())
        self.assertGreater(splitter.sizes()[1], splitter.sizes()[0])


if __name__ == "__main__":
    unittest.main()
