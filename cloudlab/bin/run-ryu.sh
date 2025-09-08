#!/usr/bin/env bash
# Helper to run Ryu from the local venv with greendns disabled.
# FIX: repo root is two levels above this script (cloudlab/bin -> repo root).

set -euo pipefail

THIS="${BASH_SOURCE[0]}"
DIR="$( cd "$( dirname "$THIS" )" >/dev/null 2>&1 && pwd )"
ROOT="$( cd "${DIR}/../.." >/dev/null 2>&1 && pwd )"

VENV_BIN="${ROOT}/.venv/bin"
RYU_APP="${ROOT}/cloudlab/ryu/agent_controller.py"

if [[ ! -x "${VENV_BIN}/ryu-manager" ]]; then
  echo "ERROR: ${VENV_BIN}/ryu-manager not found."
  echo "Hint: run 'make setup' (or 'bash cloudlab/setup-cloudlab.sh') to create the venv and install Ryu."
  exit 127
fi

if [[ ! -f "${RYU_APP}" ]]; then
  echo "ERROR: Ryu app not found at ${RYU_APP}."
  exit 127
fi

export EVENTLET_NO_GREENDNS=yes
exec "${VENV_BIN}/ryu-manager" "${RYU_APP}"
