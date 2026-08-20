"""Framework-agnostic session layer.

Everything in this package is independent of the presentation toolkit. Serial
acquisition, CSV logging, ignition ramps and view updates use explicit inputs
and outputs so that a Qt view -- or a test -- can drive the control logic.

The one deliberate exception is :mod:`flow_controller.core.session`, which is
a ``QObject`` so that events crossing from the acquisition thread to the view
use Qt's queued connections rather than a hand-rolled callback queue.  The
modules here are used by that session but do not depend on it.
"""
