"""What the native frame was doing for the window, kept without the frame.

Dropping the title bar took the caption *and* the window styles Windows keys
its own window management off.  A frameless Qt window is a ``WS_POPUP``, and a
``WS_POPUP`` is not something the shell will snap: dragging to the top edge,
``Win`` + arrow, the maximise animation and the rounded corners all left with
the bar that was replaced.

None of that is reimplemented here.  Snapping worked out from cursor
coordinates gets multi-monitor and per-monitor DPI wrong, and a control screen
that sits beside the rig software on a second screen is exactly where those
cases show up.  Instead the window keeps the styles an ordinary window has --
``WS_THICKFRAME`` and its neighbours -- and then declines to give the frame any
room: ``WM_NCCALCSIZE`` is answered with *the client area is the whole window*,
so Windows still manages the window as a normal one while drawing none of its
chrome.

One thing has to be paid for by hand.  A maximised window's rect is the work
area **inflated** by the frame Windows believes it has, on the assumption that
the frame will eat the difference.  With the frame folded into the client area
nothing eats it, and the content hangs off all four edges and over the task
bar; :func:`_frame_thickness` measures the difference back off.

Everything here is a no-op off Windows, where the frameless window is the
window manager's business and Qt's own move and resize calls are enough.
"""

from __future__ import annotations

import ctypes
import sys

IS_WINDOWS = sys.platform == 'win32'

if IS_WINDOWS:  # pragma: no branch - the import itself is the platform test
    from ctypes import wintypes

    _user32 = ctypes.windll.user32

#: Window styles.  ``WS_THICKFRAME`` is the one snapping actually reads --
#: the shell only snaps windows it believes are resizable -- but the rest come
#: with it: ``WS_MAXIMIZEBOX`` is what makes a drag to the top edge *maximise*
#: rather than merely stop there, and the caption and system menu bits are how
#: the task bar's own right-click menu finds a window's move and size commands.
GWL_STYLE = -16
WS_CAPTION = 0x00C00000
WS_THICKFRAME = 0x00040000
WS_SYSMENU = 0x00080000
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000
NATIVE_STYLES = (WS_CAPTION | WS_THICKFRAME | WS_SYSMENU
                 | WS_MINIMIZEBOX | WS_MAXIMIZEBOX)

WM_NCCALCSIZE = 0x0083

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOOWNERZORDER = 0x0200
SWP_FRAMECHANGED = 0x0020

SM_CXSIZEFRAME = 32
SM_CYSIZEFRAME = 33
SM_CXPADDEDBORDER = 92

#: The event types Qt's Windows plugin hands to ``nativeEvent``.  Two of them,
#: because which one arrives depends on whether the message came through the
#: window procedure or the dispatcher.
_MSG_TYPES = (b'windows_generic_MSG', b'windows_dispatcher_MSG')


def enable(widget):
    """Give ``widget``'s frameless window the styles Windows snaps by.

    Returns whether anything was done, so the caller can tell a window that
    now behaves natively from one still relying on Qt alone.  Safe to call
    more than once: the styles are OR-ed in, and re-announcing the frame
    change is what re-triggers the ``WM_NCCALCSIZE`` that hides it.
    """
    if not IS_WINDOWS:
        return False
    hwnd = int(widget.winId())
    if not hwnd:
        return False
    style = _user32.GetWindowLongW(hwnd, GWL_STYLE)
    _user32.SetWindowLongW(hwnd, GWL_STYLE, style | NATIVE_STYLES)
    # Without this the new styles sit unread until something else forces a
    # frame recalculation, and the window keeps the geometry it was created
    # with -- native behaviour that only starts after the first resize.
    _user32.SetWindowPos(hwnd, None, 0, 0, 0, 0,
                         SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER
                         | SWP_NOOWNERZORDER | SWP_FRAMECHANGED)
    return True


def handle_native_event(widget, message_type, message):
    """Answer ``WM_NCCALCSIZE``; ``None`` for everything else.

    ``None`` means *not ours*, which the caller passes on to Qt.  The answer
    to ``WM_NCCALCSIZE`` is ``0``: keep the proposed rectangle as the client
    area, leaving the non-client area -- the border and caption Windows would
    otherwise draw over the top of ours -- nothing to occupy.
    """
    if not IS_WINDOWS or bytes(message_type) not in _MSG_TYPES:
        return None
    msg = wintypes.MSG.from_address(int(message))
    # wParam clear means Windows is only asking where the client area *would*
    # be; there is no rectangle to adjust and the default answer is right.
    if msg.message != WM_NCCALCSIZE or not msg.wParam:
        return None
    if not msg.lParam:
        return None
    hwnd = int(msg.hWnd)
    # Full screen is the one state where the inflated rect is wanted as it
    # stands: the window really is meant to cover the task bar.
    if _user32.IsZoomed(hwnd) and not widget.isFullScreen():
        # The first of the three rectangles behind lParam is the proposed
        # client rect; the other two only matter for preserving the old
        # client area across a resize, which a repaint-everything window
        # does not care about.
        rect = wintypes.RECT.from_address(msg.lParam)
        across, down = _frame_thickness(hwnd)
        rect.left += across
        rect.top += down
        rect.right -= across
        rect.bottom -= down
    return True, 0


def _frame_thickness(hwnd):
    """How far a maximised window overhangs the work area, in pixels.

    Read per-monitor where Windows can: on a mixed-DPI desk the frame is a
    different number of pixels on each screen, and a thickness measured
    against the primary monitor would leave a gap on one and a clipped edge
    on the other.
    """
    dpi = 0
    if hasattr(_user32, 'GetDpiForWindow'):
        dpi = _user32.GetDpiForWindow(hwnd)
    if dpi and hasattr(_user32, 'GetSystemMetricsForDpi'):
        def metric(index):
            return _user32.GetSystemMetricsForDpi(index, dpi)
    else:
        metric = _user32.GetSystemMetrics
    padding = metric(SM_CXPADDEDBORDER)
    return metric(SM_CXSIZEFRAME) + padding, metric(SM_CYSIZEFRAME) + padding
