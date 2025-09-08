#!/usr/bin/env bash
# Helper to run Ryu from the local venv with greendns disabled and
# a pre-start monkey patch for eventlet.wsgi.ALREADY_HANDLED.

set -euo pipefail

THIS="${BASH_SOURCE[0]}"
DIR="$( cd "$( dirname "$THIS" )" >/dev/null 2>&1 && pwd )"
ROOT="$( cd "${DIR}/../.." >/dev/null 2>&1 && pwd )"

VENV_BIN="${ROOT}/.venv/bin"
LAUNCHER="${ROOT}/cloudlab/bin/launch_ryu.py"

if [[ ! -x "${VENV_BIN}/python" ]]; then
  echo "ERROR: ${VENV_BIN}/python not found. Run 'make setup' first."
  exit 127
fi

if [[ ! -f "${LAUNCHER}" ]]; then
  echo "ERROR: launcher not found at ${LAUNCHER}."
  exit 127
fi

# Pass through any args (e.g., alternate apps)
exec "${VENV_BIN}/python" "${LAUNCHER}" "$@"