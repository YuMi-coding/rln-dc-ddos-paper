.PHONY: setup ryu ryu-fg ryu-stop ryu-restart ryu-attach ryu-status tree clean

# Paths
RYU_RUN := cloudlab/bin/run-ryu.sh
SESSION := ryu

# One-shot environment setup (Mininet/OVS + venv + pinned deps)
setup:
	bash cloudlab/setup-cloudlab.sh

# Start Ryu controller in a tmux session (idempotent)
ryu:
	@command -v tmux >/dev/null 2>&1 || (echo "tmux not found; install it or run 'make ryu-fg'"; exit 1)
	@test -x $(RYU_RUN) || (echo "ERROR: $(RYU_RUN) not found or not executable. Re-run 'make setup'."; exit 1)
	@if tmux has-session -t $(SESSION) 2>/dev/null; then \
		echo "Ryu already running in tmux session '$(SESSION)'."; \
		echo "Attach:  tmux attach -t $(SESSION)  |  Restart:  make ryu-restart"; \
	else \
		tmux new-session -d -s $(SESSION) '$(RYU_RUN)'; \
		echo "✓ Ryu started in tmux session '$(SESSION)'."; \
		echo "Attach:  tmux attach -t $(SESSION)"; \
		echo "Detach:  Ctrl-b d"; \
	fi

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

# Start Ryu controller in the foreground (no tmux)
ryu-fg:
	@test -x $(RYU_RUN) || (echo "ERROR: $(RYU_RUN) not found or not executable. Re-run 'make setup'."; exit 1)
	$(RYU_RUN)

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
