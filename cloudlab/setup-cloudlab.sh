#!/usr/bin/env bash
# CloudLab setup: Mininet/OVS + Python venv with pinned deps for Ryu
# Installs ryu==4.34, eventlet==0.33.3, dnspython>=2.4.2.
set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
REPO_ROOT="$( cd "${SCRIPT_DIR}/.." >/dev/null 2>&1 && pwd )"
VENV="${REPO_ROOT}/.venv"

echo "==> Repo root: ${REPO_ROOT}"
echo "==> Venv path: ${VENV}"

echo "==> Installing system packages (sudo required)"
sudo apt-get update -y
sudo apt-get install -y git build-essential python3-venv python3-pip tmux xterm \
  curl nginx tcpreplay hping3 rustc cargo net-tools

# Mininet & OVS
if [ ! -d /opt/mininet ]; then
  echo "==> Installing Mininet into /opt/mininet"
  sudo mkdir -p /opt && sudo chown "$USER":"$USER" /opt
  git clone https://github.com/mininet/mininet /opt/mininet
  bash /opt/mininet/util/install.sh -a
else
  echo "==> Mininet already present at /opt/mininet (skipping clone)"
fi

# Python venv
if [ ! -d "${VENV}" ]; then
  echo "==> Creating Python venv at ${VENV}"
  python3 -m venv "${VENV}"
else
  echo "==> Using existing venv at ${VENV}"
fi

echo "==> Upgrading toolchain in venv"
"${VENV}/bin/pip" install --upgrade 'pip<24' wheel
"${VENV}/bin/pip" install --upgrade 'setuptools<66'

echo "==> Installing Ryu + compatible deps"
"${VENV}/bin/pip" install 'ryu==4.34' 'eventlet==0.33.3' 'dnspython>=2.4.2' 'networkx==2.8.8' 'numpy==1.23.5' 'scipy==1.9.3' 'matplotlib==3.5.2'

cat <<'EONOTES'

======================================================================
Setup complete.

USAGE:
  # Start the Ryu controller (uses venv + launcher shim):
    cloudlab/bin/run-ryu.sh
  # or foreground:
    make ryu-fg

  # Launch Mininet topology (in another shell):
    sudo mn -c
    sudo python3 cloudlab/topos/tree_topo.py

Notes:
  - Ryu & deps are isolated in the venv.
  - The launcher shim defines eventlet.wsgi.ALREADY_HANDLED when missing
    and disables greendns by default (EVENTLET_NO_GREENDNS=yes).
======================================================================
EONOTES
