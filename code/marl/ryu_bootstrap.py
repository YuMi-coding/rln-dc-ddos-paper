# code/marl/ryu_bootstrap.py
# Ensure Ryu can import eventlet.wsgi.ALREADY_HANDLED on newer eventlet
try:
    import eventlet.wsgi as _wsgi
    if not hasattr(_wsgi, "ALREADY_HANDLED"):
        _wsgi.ALREADY_HANDLED = object()
except Exception:
    pass

# Now import and run the real ryu manager
import sys
from ryu.cmd.manager import main as ryu_main

# Preserve CLI args (e.g., controller_py3.py) passed from Popen
# Example we’ll use: [py, -u, code/marl/ryu_bootstrap.py, controller_py3.py]
sys.argv = ["ryu-manager"] + sys.argv[1:]
ryu_main()
