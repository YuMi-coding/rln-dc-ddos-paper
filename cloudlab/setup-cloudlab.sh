#!/usr/bin/env bash
# CloudLab setup script: Mininet + OVS + tools + Python venv w/ pinned toolchain
# Installs ryu==4.34, eventlet==0.33.3 (Python 3.10+ safe), dnspython>=2.4.2,
# and patches ryu.app.wsgi to define ALREADY_HANDLED when missing.
# Also avoids eventlet greendns pitfalls on import.

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

# ---------- Python venv and packages ----------
if [ ! -d "${VENV}" ]; then
  echo "==> Creating Python venv at ${VENV}"
  python3 -m venv "${VENV}"
else
  echo "==> Using existing venv at ${VENV}"
fi

echo "==> Upgrading pip/wheel and pinning setuptools for Ryu build compatibility"
"${VENV}/bin/pip" install --upgrade 'pip<24' wheel
"${VENV}/bin/pip" install --upgrade 'setuptools<66'

echo "==> Installing Ryu/Eventlet/DNSPython"
"${VENV}/bin/pip" install 'ryu==4.34' 'eventlet==0.33.3' 'dnspython>=2.4.2'

echo "==> Patching ryu.app.wsgi to tolerate missing eventlet.wsgi.ALREADY_HANDLED"
"${VENV}/bin/python" - <<'PY'
import sys, pathlib
wsgi_path = None
for p in map(pathlib.Path, sys.path):
    candidate = p / 'ryu' / 'app' / 'wsgi.py'
    if candidate.exists():
        wsgi_path = candidate
        break
if not wsgi_path:
    raise SystemExit("ERROR: Could not locate ryu/app/wsgi.py to patch.")

txt = wsgi_path.read_text()
needle = 'from eventlet.wsgi import ALREADY_HANDLED'
patched = (
    'try:\n'
    '    from eventlet.wsgi import ALREADY_HANDLED\n'
    'except Exception:\n'
    '    # Newer eventlet removed ALREADY_HANDLED; define a sentinel for compatibility.\n'
    '    ALREADY_HANDLED = object()\n'
)

if needle in txt and 'try:' not in txt:
    txt = txt.replace(needle, patched)
    wsgi_path.write_text(txt)
    print(f"Patched: {wsgi_path}")
else:
    print(f"Patch not needed or already applied: {wsgi_path}")
PY

cat <<'EONOTES'

======================================================================
Setup complete.

USAGE:
  1) Start the Ryu controller **from the venv** with greendns disabled:
       EVENTLET_NO_GREENDNS=yes ${REPO_ROOT}/.venv/bin/ryu-manager cloudlab/ryu/agent_controller.py

     (Alternatively, activate the venv and run the same command: 
       source ${REPO_ROOT}/.venv/bin/activate
       EVENTLET_NO_GREENDNS=yes ryu-manager cloudlab/ryu/agent_controller.py
     )

  2) Launch the topology (single-destination tree) in another shell:
       sudo python3 cloudlab/topos/tree_topo.py

  3) Clean up when done:
       sudo mn -c

Notes:
  - Ryu & deps are isolated in the venv. System Python remains untouched.
  - We use Eventlet 0.33.x for Python 3.10+ compatibility and install dnspython>=2.4.2
    so Eventlet's greendns works with Python 3.10.
  - We also export EVENTLET_NO_GREENDNS=yes to bypass greendns entirely; remove it if
    you specifically need greendns features.
======================================================================
EONOTES
