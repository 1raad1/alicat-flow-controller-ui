"""Source-tree launcher for Flow Controller v3.

    python run.py            the Qt application
    python run.py --legacy   the original Tk application

The Tk app stays reachable while the port is being proven against real
hardware: it is the build that has actually run the rig, and falling back to it
should not need a code change.
"""

import sys


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--legacy" in argv:
        from flow_controller.app import main as tk_main
        tk_main()
        return 0
    from flow_controller.ui.qt_main_window import main as qt_main
    return qt_main()


if __name__ == "__main__":
    sys.exit(main())
