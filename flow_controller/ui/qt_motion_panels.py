"""Animated, accessible two-pane splitters.

The splitter keeps panel widgets mounted while folding their clipping parents.
That avoids the layout churn and focus loss caused by hiding complex panels.
"""

from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QRect, QSize, Qt, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QKeyEvent, QMouseEvent, QPainter, QPen
from PySide6.QtWidgets import QSplitter, QSplitterHandle, QWidget

from . import qt_theme


def _axis_extent(size: QSize, orientation: Qt.Orientation) -> int:
    return size.width() if orientation == Qt.Orientation.Horizontal else size.height()


class _ClipPane(QWidget):
    """A zero-minimum viewport which does not squeeze its content."""

    def __init__(self, content: QWidget, orientation: Qt.Orientation, parent=None):
        super().__init__(parent)
        self.content = content
        self._orientation = orientation
        self.setMinimumSize(0, 0)
        self.setFocusProxy(content)
        content.setParent(self)
        content.show()

    def sizeHint(self) -> QSize:
        return self.content.sizeHint()

    def minimumSizeHint(self) -> QSize:
        return QSize(0, 0)

    def content_minimum_extent(self) -> int:
        return max(
            0,
            _axis_extent(self.content.minimumSizeHint(), self._orientation),
            _axis_extent(self.content.minimumSize(), self._orientation),
        )

    def content_minimum_size(self) -> QSize:
        hint = self.content.minimumSizeHint().expandedTo(
            self.content.minimumSize())
        return QSize(max(0, hint.width()), max(0, hint.height()))

    def resizeEvent(self, event):
        size = event.size()
        width, height = size.width(), size.height()
        minimum = self.content_minimum_extent()
        if self._orientation == Qt.Orientation.Horizontal:
            width = max(width, minimum)
        else:
            height = max(height, minimum)
        self.content.setGeometry(QRect(0, 0, width, height))
        super().resizeEvent(event)


class _MotionHandle(QSplitterHandle):
    def __init__(self, orientation, splitter):
        super().__init__(orientation, splitter)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName("Resize panels")
        self.setToolTip("Drag to resize panels; double-click to reset")
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self._pressed = False

    @property
    def _motion_splitter(self) -> "MotionSplitter":
        return self.splitter()

    def mousePressEvent(self, event: QMouseEvent):
        self._pressed = True
        self.update()
        self._motion_splitter._begin_drag()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent):
        super().mouseReleaseEvent(event)
        self._pressed = False
        self.update()
        self._motion_splitter._finish_drag()

    def mouseDoubleClickEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton:
            self._motion_splitter.reset_layout()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event: QKeyEvent):
        if self._motion_splitter._handle_key(event):
            event.accept()
            return
        super().keyPressEvent(event)

    def paintEvent(self, event):
        super().paintEvent(event)
        active = self._pressed or self.underMouse() or self.hasFocus()
        color = QColor(qt_theme.ACCENT if active else qt_theme.TEXT_DIM)
        color.setAlpha(190 if active else 105)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(color, 1.2, Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap))
        centre = self.rect().center()
        for offset in (-3, 0, 3):
            if self.orientation() == Qt.Orientation.Horizontal:
                painter.drawLine(centre.x(), centre.y() + offset - 1,
                                 centre.x(), centre.y() + offset + 1)
            else:
                painter.drawLine(centre.x() + offset - 1, centre.y(),
                                 centre.x() + offset + 1, centre.y())


class MotionSplitter(QSplitter):
    """A two-pane splitter with clipped collapse and restrained motion."""

    panelCollapsedChanged = Signal(int, bool)
    ANIMATION_DURATION_MS = 220

    def __init__(self, orientation: Qt.Orientation, parent=None):
        super().__init__(orientation, parent)
        self.setChildrenCollapsible(True)
        self.setHandleWidth(7)
        self._contents: list[QWidget] = []
        self._panels: dict[int, dict[str, object]] = {}
        self._default_sizes: list[int] = []
        self._expanded_extents: dict[int, int] = {}
        self._expanded_ratios: dict[int, float] = {}
        self._collapsed = {0: False, 1: False}
        self._animation = QVariantAnimation(self)
        self._animation.setDuration(self.ANIMATION_DURATION_MS)
        self._animation.setStartValue(0.0)
        self._animation.setEndValue(1.0)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._animation.valueChanged.connect(self._animation_step)
        self._animation.finished.connect(self._animation_finished)
        self._animation_active = False
        self._animation_start = [0, 0]
        self._animation_target = [0, 0]

    def createHandle(self) -> QSplitterHandle:
        return _MotionHandle(self.orientation(), self)

    def addWidget(self, widget: QWidget) -> None:
        if self.count() >= 2:
            raise ValueError("MotionSplitter supports exactly two panes")
        wrapper = _ClipPane(widget, self.orientation())
        self._contents.append(widget)
        super().addWidget(wrapper)

    def content_widget(self, index: int) -> QWidget:
        """Return the original widget supplied for a pane."""
        return self._contents[index]

    def configure_panel(self, index: int, title: str,
                        collapsible: bool = True) -> None:
        if index not in (0, 1) or index >= self.count():
            raise IndexError("panel index must identify one of the two panes")
        self._panels[index] = {
            "title": str(title),
            "collapsible": bool(collapsible),
        }
        self.setCollapsible(index, bool(collapsible))
        self._update_handle_accessibility()

    def set_default_sizes(self, sizes: list[int]) -> None:
        if len(sizes) != 2 or any(int(size) < 0 for size in sizes):
            raise ValueError("default sizes must contain two non-negative values")
        self._default_sizes = [int(size) for size in sizes]
        total = sum(self._default_sizes)
        if total:
            for index in (0, 1):
                if self._default_sizes[index] > 0:
                    self._expanded_extents[index] = self._default_sizes[index]
                    self._expanded_ratios[index] = self._default_sizes[index] / total
        target_total = self._available_extent()
        self._set_sizes(self._scaled_defaults(target_total))

    def set_panel_collapsed(self, index: int, collapsed: bool,
                            animate: bool = True) -> None:
        self._validate_panel(index)
        collapsed = bool(collapsed)
        if collapsed and not self._is_collapsible(index):
            return
        current = self._current_sizes()
        state_changed = self._collapsed[index] != collapsed
        if collapsed and state_changed and current[index] > 0:
            self._expanded_extents[index] = current[index]
            if sum(current) > 0:
                self._expanded_ratios[index] = current[index] / sum(current)
        total = max(0, sum(current))
        if total == 0:
            total = max(0, self._available_extent())
        target = current[:]
        if collapsed:
            target[index] = 0
            target[1 - index] = total
        else:
            remembered = self._expanded_extents.get(index)
            ratio = self._expanded_ratios.get(index)
            if ratio is not None:
                remembered = round(total * ratio)
            if not remembered:
                remembered = self._scaled_defaults(total)[index]
            target[index] = min(total, max(self._panel_min(index), remembered))
            target[1 - index] = total - target[index]
            target = self._bounded_sizes(target)
        self._set_collapsed_state(index, collapsed)
        self._transition_to(target, animate)

    def is_panel_collapsed(self, index: int) -> bool:
        self._validate_panel(index)
        return bool(self._collapsed[index])

    def reset_layout(self, animate: bool = True) -> None:
        if self.count() != 2:
            return
        target = self._scaled_defaults(sum(self._current_sizes()))
        target = self._bounded_sizes(target)
        for index in (0, 1):
            self._set_collapsed_state(index, target[index] == 0)
        self._transition_to(target, animate)

    def _validate_panel(self, index: int) -> None:
        if index not in (0, 1) or index >= self.count():
            raise IndexError("panel index must identify one of the two panes")

    def _is_collapsible(self, index: int) -> bool:
        panel = self._panels.get(index)
        return bool(panel and panel["collapsible"])

    def _panel_min(self, index: int) -> int:
        wrapper = self.widget(index)
        if isinstance(wrapper, _ClipPane):
            return wrapper.content_minimum_extent()
        return 0

    def minimumSizeHint(self) -> QSize:
        if self.count() != 2:
            return super().minimumSizeHint()
        minima = []
        for index in (0, 1):
            wrapper = self.widget(index)
            minima.append(wrapper.content_minimum_size()
                          if isinstance(wrapper, _ClipPane) else QSize())
        handle = self.handleWidth()
        if self.orientation() == Qt.Orientation.Horizontal:
            width = handle + sum(minima[i].width()
                                 for i in (0, 1)
                                 if not self._is_collapsible(i))
            return QSize(width, max(size.height() for size in minima))
        height = handle + sum(minima[i].height()
                              for i in (0, 1)
                              if not self._is_collapsible(i))
        return QSize(max(size.width() for size in minima), height)

    def _allowed_min(self, index: int) -> int:
        return 0 if self._collapsed[index] else self._panel_min(index)

    def _available_extent(self) -> int:
        extent = self.width() if self.orientation() == Qt.Orientation.Horizontal else self.height()
        return max(0, extent - (self.handleWidth() if self.count() == 2 else 0))

    def _current_sizes(self) -> list[int]:
        sizes = self.sizes()
        return [int(sizes[0]), int(sizes[1])] if len(sizes) == 2 else [0, 0]

    def _scaled_defaults(self, total: int) -> list[int]:
        defaults = self._default_sizes or [1, 1]
        weight = sum(defaults)
        if weight <= 0:
            return [total // 2, total - total // 2]
        first = round(total * defaults[0] / weight)
        return [first, total - first]

    def _bounded_sizes(self, sizes: list[int]) -> list[int]:
        total = max(0, sum(int(value) for value in sizes))
        first_min, second_min = self._allowed_min(0), self._allowed_min(1)
        if first_min + second_min > total:
            # Qt cannot honour both in an undersized container. Divide the
            # space predictably, favouring the minimums' ratio.
            weight = first_min + second_min
            first = round(total * first_min / weight) if weight else total // 2
            return [first, total - first]
        first = max(first_min, min(total - second_min, int(sizes[0])))
        return [first, total - first]

    def _settled_bounded_sizes(self, sizes: list[int],
                               collapsed: int | None = None) -> list[int]:
        total = max(0, sum(sizes))
        minima = [self._panel_min(i) for i in (0, 1)]
        if collapsed is not None:
            minima[collapsed] = 0
        if sum(minima) > total:
            weight = sum(minima)
            first = round(total * minima[0] / weight) if weight else total // 2
            return [first, total - first]
        first = max(minima[0], min(total - minima[1], int(sizes[0])))
        return [first, total - first]

    def _set_sizes(self, sizes: list[int]) -> None:
        if self.count() == 2:
            QSplitter.setSizes(self, self._bounded_sizes(list(sizes)))

    def _settle_sizes(self, sizes: list[int]) -> None:
        self._set_sizes(sizes)
        for index in (0, 1):
            # The viewport is ours to disable. The content remains free to
            # track application-driven enabled state while it is folded.
            self.widget(index).setEnabled(not self._collapsed[index])

    def _set_collapsed_state(self, index: int, collapsed: bool) -> None:
        collapsed = bool(collapsed)
        if self._collapsed[index] != collapsed:
            self._collapsed[index] = collapsed
            self.panelCollapsedChanged.emit(index, collapsed)

    def _stop_animation(self) -> None:
        if self._animation_active:
            self._animation_active = False
            self._animation.stop()

    def _transition_to(self, target: list[int], animate: bool) -> None:
        self._stop_animation()
        start = self._current_sizes()
        target = self._bounded_sizes(target)
        if not animate or start == target or not self.isVisible():
            self._settle_sizes(target)
            return
        for index in (0, 1):
            if not self._collapsed[index]:
                self.widget(index).setEnabled(True)
        self._animation_start = start
        self._animation_target = target
        self._animation_active = True
        self._animation.start()

    def _animation_step(self, value) -> None:
        progress = float(value)
        sizes = [round(start + (end - start) * progress)
                 for start, end in zip(self._animation_start,
                                       self._animation_target)]
        # The clipping wrapper deliberately permits the folding pane to pass
        # below its content minimum during motion. Endpoints are bounded.
        QSplitter.setSizes(self, sizes)

    def _animation_finished(self) -> None:
        if not self._animation_active:
            return
        target = self._animation_target[:]
        self._animation_active = False
        self._settle_sizes(target)

    def _begin_drag(self) -> None:
        self._stop_animation()
        sizes = self._current_sizes()
        total = sum(sizes)
        for index in (0, 1):
            if sizes[index] > 0 and not self._collapsed[index]:
                self._expanded_extents[index] = sizes[index]
                if total:
                    self._expanded_ratios[index] = sizes[index] / total

    def _finish_drag(self) -> None:
        if self.count() != 2:
            return
        sizes = self._current_sizes()
        total = sum(sizes)
        collapsed_index = None
        for index in (0, 1):
            threshold = self._panel_min(index) / 2
            if self._is_collapsible(index) and sizes[index] < threshold:
                collapsed_index = index
                break
        if collapsed_index is not None:
            index = collapsed_index
            if sizes[index] > 0:
                self._expanded_extents.setdefault(index, self._panel_min(index))
            sizes[index] = 0
            sizes[1 - index] = total
        sizes = self._settled_bounded_sizes(sizes, collapsed_index)
        for index in (0, 1):
            collapsed = sizes[index] == 0 and self._is_collapsible(index)
            if not collapsed and sizes[index] > 0:
                self._expanded_extents[index] = sizes[index]
                if total:
                    self._expanded_ratios[index] = sizes[index] / total
            self._set_collapsed_state(index, collapsed)
        self._settle_sizes(sizes)

    def _designated_panel(self) -> int | None:
        candidates = [index for index in (0, 1) if self._is_collapsible(index)]
        return candidates[-1] if candidates else None

    def _handle_key(self, event: QKeyEvent) -> bool:
        if self.count() != 2:
            return False
        key = event.key()
        shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        step = 50 if shift else 10
        negative = ((self.orientation() == Qt.Orientation.Horizontal and
                     key == Qt.Key.Key_Left) or
                    (self.orientation() == Qt.Orientation.Vertical and
                     key == Qt.Key.Key_Up) or key == Qt.Key.Key_Minus)
        positive = ((self.orientation() == Qt.Orientation.Horizontal and
                     key == Qt.Key.Key_Right) or
                    (self.orientation() == Qt.Orientation.Vertical and
                     key == Qt.Key.Key_Down) or key == Qt.Key.Key_Plus)
        if negative or positive:
            self._stop_animation()
            sizes = self._current_sizes()
            delta = -step if negative else step
            raw = [sizes[0] + delta, sizes[1] - delta]
            for index in (0, 1):
                if sizes[index] == 0 and raw[index] > 0:
                    raw[index] = max(raw[index], self._panel_min(index))
                    raw[1 - index] = sum(sizes) - raw[index]
            target = self._settled_bounded_sizes(raw)
            self._set_sizes(target)
            self._finish_drag()
            return True
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            index = self._designated_panel()
            if index is not None:
                self.set_panel_collapsed(index,
                                         not self.is_panel_collapsed(index))
            return True
        if key == Qt.Key.Key_Home:
            index = self._designated_panel()
            if index is not None:
                self.set_panel_collapsed(index, True)
            else:
                total = sum(self._current_sizes())
                self._transition_to([self._panel_min(0),
                                     total - self._panel_min(0)], True)
            return True
        if key == Qt.Key.Key_End:
            index = self._designated_panel()
            total = sum(self._current_sizes())
            if index is not None:
                other = 1 - index
                target = [0, 0]
                target[other] = self._panel_min(other)
                target[index] = total - target[other]
                self._set_collapsed_state(index, False)
                self._transition_to(target, True)
            else:
                self._transition_to([total - self._panel_min(1),
                                     self._panel_min(1)], True)
            return True
        return False

    def _update_handle_accessibility(self) -> None:
        if self.count() != 2:
            return
        titles = [str(self._panels.get(i, {}).get("title", f"panel {i + 1}"))
                  for i in (0, 1)]
        collapsible = self._designated_panel()
        handle = self.handle(1)
        handle.setAccessibleName(f"Resize {titles[0]} and {titles[1]}")
        tip = f"Drag to resize {titles[0]} and {titles[1]}"
        if collapsible is not None:
            tip += f"; press Enter to collapse or expand {titles[collapsible]}"
        tip += "; double-click to reset"
        handle.setToolTip(tip)

    def resizeEvent(self, event):
        animation_target = (self._animation_target[:]
                            if self._animation_active else None)
        self._stop_animation()
        super().resizeEvent(event)
        if animation_target is not None and self.count() == 2:
            old_total = max(1, sum(animation_target))
            new_total = self._available_extent()
            first = round(new_total * animation_target[0] / old_total)
            self._settle_sizes([first, new_total - first])
        elif self.count() == 2:
            self._settle_sizes(self._current_sizes())
