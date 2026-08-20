"""Source-tree launcher for Flow Controller v3."""

import sys


def main(argv=None):
    from flow_controller.ui.qt_main_window import main as qt_main
    return qt_main()


if __name__ == "__main__":
    sys.exit(main())
