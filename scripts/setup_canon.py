"""Import a user's official Canon SDK without redistributing vendor files."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from flow_controller.infrastructure.canon_sdk import install_sdk

    print('Canon camera setup')
    print('Choose your official Windows Canon EDSDK ZIP, or EDSDK_64/Dll/EDSDK.dll.')
    print('Obtain the SDK from Canon: https://developers.canon-europe.com/developers/s/article/Latest-EOS-SDK-Version-13-x')
    if len(sys.argv) > 1:
        source = sys.argv[1]
    else:
        from PySide6.QtWidgets import QApplication, QFileDialog
        app = QApplication.instance() or QApplication([])
        source, _ = QFileDialog.getOpenFileName(
            None, 'Select Canon Windows SDK ZIP or 64-bit EDSDK.dll',
            str(Path.home()), 'Canon SDK (*.zip EDSDK.dll);;All files (*)')
    if not source:
        print('Canon setup cancelled. Any previously installed SDK is unchanged.')
        return 2
    path = Path(source).expanduser()
    if path.is_file() and path.suffix.lower() == '.dll':
        path = path.parent
    try:
        installed = install_sdk(path)
    except Exception as exc:
        print(f'Canon setup failed: {exc}', file=sys.stderr)
        return 1
    print(f'Canon SDK installed and native initialization verified: {installed}')
    print('Restart the flow app, connect the Canon camera by USB, and choose Discover USB cameras.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
