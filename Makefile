.PHONY: setup patch-ryu ryu ryu-fg ryu-stop ryu-restart ryu-attach ryu-status tree clean

# Paths
VENV_BIN := .venv/bin
RYU_RUN  := cloudlab/bin/run-ryu.sh
SESSION  := ryu

# One-shot environment setup (Mininet/OVS + venv + pinned deps)
setup:
	bash cloudlab/setup-cloudlab.sh
	@mkdir -p cloudlab/bin
	@chmod +x $(RYU_RUN) || true
	@echo "✓ Setup complete. Helper $(RYU_RUN) is executable."

# Patch Ryu for Eventlet>=0.31 (wrap ALREADY_HANDLED import)
patch-ryu:
	@test -x $(VENV_BIN)/python || (echo "ERROR: venv missing. Run 'make setup' first."; exit 1)
	@$(VENV_BIN)/python - <<'PY'
import sys, pathlib
wsgi_path = None
for p in map(pathlib.Path, sys.path):
    cand = p / 'ryu' / 'app' / 'wsgi.py'
    if cand.exists():
        wsgi_path = cand; break
if not wsgi_path:
    print('ERROR: ryu/app/wsgi.py not found'); sys.exit(1)
txt = wsgi_path.read_text()
needle = 'from eventlet.wsgi import ALREADY_HANDLED'
patched = 'try:\n    from eventlet.wsgi import ALREADY_HANDLED\nexcept Exception:\n    ALREADY_HANDLED = object()\n'
if needle in txt and 'try:' not in txt:
    txt = txt.replace(needle, patched); wsgi_path.write_text(txt)
    print('Patched:', wsgi_path)
else:
    print('No patch needed or already patched:', wsgi_path)
PY

# Start Ryu controller in a tmux session (idempotent; verifies session stays up)
ryu: patch-ryu
	@command -v tmux >/dev/null 2>&1 || (echo "tmux not found; install it or run 'make ryu-fg'"; exit 1)
	@test -x $(RYU_RUN) || (echo "ERROR: $(RYU_RUN) not found or not executable. Re-run 'make setup'."; exit 1)
	@if tmux has-session -t $(SESSION) 2>/dev/null; then \
		echo "Ryu already running in tmux session '$(SESSION)'."; \
		echo "Attach:  tmux attach -t $(SESSION)  |  Restart:  make ryu-restart"; \
	else \
		tmux new-session -d -s $(SESSION) '$(RYU_RUN)'; \
		sleep 1; \
		if tmux has-session -t $(SESSION) 2>/dev/null; then \
			echo "✓ Ryu started in tmux session '$(SESSION)'."; \
			echo "  Attach:  tmux attach -t $(SESSION)"; \
			echo "  Detach:  Ctrl-b d"; \
		else \
			echo "✗ Ryu failed to start. Try 'make ryu-fg' to see errors."; \
			exit 1; \
		fi; \
	fi

# Start Ryu controller in the foreground (no tmux)
ryu-fg: patch-ryu
	@test -x $(RYU_RUN) || (echo "ERROR: $(RYU_RUN) not found or not executable. Re-run 'make setup'."; exit 1)
	$(RYU_RUN)

# Stop the tmux session if running
ryu-stop:
	- tmux kill-session -t $(SESSION)
	@echo "✓ Ryu stopped (if it was running)."

# Restart the tmux session
ryu-restart: ryu-stop ryu

# Attach to the tmux session
ryu-attach:
	@tmux attach -t $(SESSION) || (echo "No tmux session '$(SESSION)'. Start it with 'make ryu'."; exit 1)

# Show tmux sessions (status helper)
ryu-status:
	@tmux ls || true

# Launch the single-destination tree topology
tree:
	@echo "==> Cleaning Mininet state (ok if it errors)"
	- sudo mn -c
	@echo "==> Starting topology"
	sudo python3 cloudlab/topos/tree_topo.py

# Cleanup: stop Ryu and clear Mininet state
clean:
	- tmux kill-session -t $(SESSION)
	- sudo mn -c
	@echo "✓ Cleaned."
