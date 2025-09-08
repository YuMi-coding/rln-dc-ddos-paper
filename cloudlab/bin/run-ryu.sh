#!/usr/bin/env bash
# Helper to run Ryu from the local venv with greendns disabled
set -euo pipefail
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
ROOT="$( cd "${DIR}/.." >/dev/null 2>&1 && pwd )"
export EVENTLET_NO_GREENDNS=yes
exec "${ROOT}/.venv/bin/ryu-manager" "${ROOT}/cloudlab/ryu/agent_controller.py"
