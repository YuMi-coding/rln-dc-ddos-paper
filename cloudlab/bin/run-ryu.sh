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

# Optional: RYU_APP can be an absolute or repo-relative path to the controller app.
# If unset, the launcher will pick a sane default.
export RYU_APP="${RYU_APP:-}"

# Pass through any extra args (e.g., alternate Ryu apps, ryu flags)
exec "${VENV_BIN}/python" "${LAUNCHER}" "$@"
