#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if (( EUID == 0 )); then
    echo 'Run this installer as your normal user, not with sudo.' >&2
    exit 1
fi
command -v python3 >/dev/null || { echo 'Install Python first; see CACHYOS_START_HERE.md.' >&2; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else "Python 3.11 or newer is required")'
if [[ -e .venv && ! -f .venv/pyvenv.cfg ]]; then
    echo '.venv already exists but is not a Python environment; nothing was overwritten.' >&2
    exit 1
fi
python3 -m venv .venv
.venv/bin/python -m pip install --disable-pip-version-check --no-cache-dir -r requirements-relay.txt
echo 'Installed. Next: bash run_relay_host.sh setup'
echo 'The installer has not changed your firewall, router, system services or DNS.'
