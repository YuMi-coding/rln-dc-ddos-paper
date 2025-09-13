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

def repo_root():
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.normpath(os.path.join(here, "..", ".."))

def default_app_path():
    # Default to the Python3 controller you placed under code/marl/
    return os.path.join(repo_root(), "code", "marl", "controller_py3.py")

def resolve_app_path(p):
    if not p:
        return default_app_path()
    if os.path.isabs(p):
        return p
    return os.path.normpath(os.path.join(repo_root(), p))

def main():
    # Priority: CLI arg(s) > env RYU_APP > default
    # If CLI args present and first is a file, treat that as app path
    app_path = os.environ.get("RYU_APP", "")
    if len(sys.argv) > 1 and os.path.exists(sys.argv[1]):
        app_path = sys.argv[1]

    app_path = resolve_app_path(app_path)
    if not os.path.exists(app_path):
        sys.stderr.write(f"ERROR: Ryu app not found: {app_path}\n")
        sys.exit(2)

    # Rebuild argv for ryu-manager
    sys.argv = ["ryu-manager", app_path] + sys.argv[2:]
    manager.main()

if __name__ == "__main__":
    main()
