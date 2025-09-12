# -------- settings (adjust if you like) --------
PY=python3.10                 # recommend 3.10 for max compat with ryu 4.34/eventlet 0.33.x
VENV="$HOME/.venvs/ryu-4.34"  # pick any path you want

# -------- create venv --------
set -euo pipefail
command -v "$PY" >/dev/null || { echo "Python not found: $PY"; exit 1; }
"$PY" -m venv "$VENV"

echo "==> Upgrading toolchain in venv"
"$VENV/bin/pip" install --upgrade 'pip<24' wheel
"$VENV/bin/pip" install --upgrade 'setuptools<66'

echo "==> Installing Ryu + compatible deps"
"$VENV/bin/pip" install 'ryu==4.34' 'eventlet==0.30.2' 'dnspython>=2.4.2' 'greenlet<2'

echo "==> What got installed?"
"$VENV/bin/python" - <<'PY'
import sys, eventlet
import importlib.metadata as md
print("python:", sys.version.split()[0])
print("ryu   :", md.version("ryu"))
print("eventlet:", eventlet.__version__)
def v(name):
    try: return md.version(name)
    except md.PackageNotFoundError: return "not installed"
print("dnspython:", v("dnspython"))
PY

echo "==> ryu-manager path & version"
echo "$VENV/bin/ryu-manager"
"$VENV/bin/ryu-manager" --version || true

echo "==> Done. Activate with:"
echo "source \"$VENV/bin/activate\""
