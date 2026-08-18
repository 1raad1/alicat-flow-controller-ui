"""Allow ``python -m flow_controller``.

``--legacy`` runs the original Tk application; see ``run.py``.
"""

import sys


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--legacy" in argv:
        from .app import main as tk_main
        tk_main()
        return 0
    from .ui.qt_main_window import main as qt_main
    return qt_main()


if __name__ == "__main__":
    sys.exit(main())
