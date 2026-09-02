#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if (( EUID == 0 )); then
    echo 'Run the relay as your normal user, not with sudo.' >&2
    exit 1
fi
if [[ ! -x .venv/bin/python ]]; then
    echo 'Run bash install_relay_host.sh first.' >&2
    exit 1
fi
if (( $# == 0 )); then
    set -- run
fi
exec .venv/bin/python -m mexa_bridge.relay_host "$@"
