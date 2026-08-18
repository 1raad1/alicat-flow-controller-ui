"""Framework-agnostic session layer.

Everything in this package is independent of the presentation toolkit.  The
Tk application grew as a single class in which serial acquisition, CSV
logging, ignition ramps and widget updates were interleaved, which is why the
view could not be replaced without also rewriting the control logic.  These
modules are that control logic, pulled out and given explicit inputs and
outputs so that a Qt view -- or a test -- can drive them.

The one deliberate exception is :mod:`flow_controller.core.session`, which is
a ``QObject`` so that events crossing from the acquisition thread to the view
use Qt's queued connections rather than a hand-rolled callback queue.  The
modules here are used by that session but do not depend on it.
"""
