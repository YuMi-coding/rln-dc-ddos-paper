#!/usr/bin/env bash
# CloudLab setup script: Mininet + OVS + tools + Python venv with pinned ryu/eventlet
# This avoids the eventlet>=0.31 'ALREADY_HANDLED' import error with Ryu.

set -euo pipefail

# Resolve repo root (this script lives in cloudlab/)
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
REPO_ROOT="$( cd "${SCRIPT_DIR}/.." >/dev/null 2>&1 && pwd )"
VENV="${REPO_ROOT}/.venv"

echo "==> Repo root: ${REPO_ROOT}"
echo "==> Venv path: ${VENV}"

echo "==> Installing system packages (sudo required)"
sudo apt-get update -y
sudo apt-get install -y \
  git build-essential python3-venv python3-pip \
  tmux xterm curl nginx tcpreplay hping3 rustc cargo \
  net-tools

# ---------- Mininet & OVS ----------
if [ ! -d /opt/mininet ]; then
  echo "==> Installing Mininet into /opt/mininet"
  sudo mkdir -p /opt && sudo chown "$USER":"$USER" /opt
  git clone https://github.com/mininet/mininet /opt/mininet
  bash /opt/mininet/util/install.sh -a
else
  echo "==> Mininet already present at /opt/mininet (skipping clone)"
fi

# ---------- Python venv with pinned Ryu/eventlet ----------
if [ ! -d "${VENV}" ]; then
  echo "==> Creating Python venv at ${VENV}"
  python3 -m venv "${VENV}"
else
  echo "==> Using existing venv at ${VENV}"
fi

echo "==> Upgrading pip in venv and installing pinned packages"
"${VENV}/bin/pip" install --upgrade pip
# Pin a Ryu version that still expects eventlet.wsgi.ALREADY_HANDLED and a compatible eventlet
"${VENV}/bin/pip" install 'ryu==4.34' 'eventlet==0.30.2'

# Optional: show that ALREADY_HANDLED exists
echo "==> Verifying eventlet.wsgi.ALREADY_HANDLED symbol"
"${VENV}/bin/python" - <<'PY'
from eventlet import wsgi
ok = hasattr(wsgi, "ALREADY_HANDLED")
print(f"ALREADY_HANDLED present: {ok}")
PY

cat <<'EONOTES'

======================================================================
Setup complete.

USAGE:
  1) Start the Ryu controller **from the venv** (new shell):
       ${REPO_ROOT}/.venv/bin/ryu-manager cloudlab/ryu/agent_controller.py

     (Alternatively, activate the venv: 
       source ${REPO_ROOT}/.venv/bin/activate
       ryu-manager cloudlab/ryu/agent_controller.py
     )

  2) Launch the topology (single-destination tree) in another shell:
       sudo python3 cloudlab/topos/tree_topo.py

  3) Clean up when done:
       sudo mn -c

Notes:
  - The script does NOT install Ryu globally; it is isolated in the venv to avoid
    conflicts with system packages.
  - If you use a Makefile, point your 'ryu' target to:
       ${REPO_ROOT}/.venv/bin/ryu-manager cloudlab/ryu/agent_controller.py
======================================================================
EONOTES
