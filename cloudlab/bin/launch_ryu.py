#!/usr/bin/env python3
import os, sys

# Avoid greendns issues unless explicitly needed
os.environ.setdefault("EVENTLET_NO_GREENDNS", "yes")

# Ensure ALREADY_HANDLED exists before Ryu imports ryu.app.wsgi
try:
    import eventlet.wsgi as w
    if not hasattr(w, "ALREADY_HANDLED"):
        setattr(w, "ALREADY_HANDLED", object())
except Exception:
    # If eventlet import fails, Ryu will emit a clearer error later
    pass

from ryu.cmd import manager

# Default to the repo's agent_controller if no app args provided
if len(sys.argv) <= 1:
    here = os.path.dirname(os.path.abspath(__file__))
    default_app = os.path.normpath(os.path.join(here, "..", "ryu", "agent_controller.py"))
    sys.argv = ["ryu-manager", default_app]
else:
    sys.argv = ["ryu-manager"] + sys.argv[1:]

manager.main()