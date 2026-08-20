"""Allow ``python -m flow_controller``."""

import sys


def main(argv=None):
    from .ui.qt_main_window import main as qt_main
    return qt_main()


if __name__ == "__main__":
    sys.exit(main())
