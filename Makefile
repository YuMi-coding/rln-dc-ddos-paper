
.PHONY: setup ryu ryu-fg tree clean

# Paths
RYU_RUN := cloudlab/bin/run-ryu.sh

# One-shot environment setup (Mininet/OVS + venv + pinned deps)
setup:
	bash cloudlab/setup-cloudlab.sh

# Start Ryu controller in a tmux session using the helper script
ryu:
	@command -v tmux >/dev/null 2>&1 || (echo "tmux not found; install it or run 'make ryu-fg'"; exit 1)
	@test -x $(RYU_RUN) || (echo "ERROR: $(RYU_RUN) not found or not executable. Re-run 'make setup'."; exit 1)
	- tmux new -d -s ryu '$(RYU_RUN)'
	@echo "✓ Ryu started in tmux session 'ryu'."
	@echo "  Attach:  tmux attach -t ryu"
	@echo "  Detach:  Ctrl-b d"

# Start Ryu controller in the foreground (no tmux)
ryu-fg:
	@test -x $(RYU_RUN) || (echo "ERROR: $(RYU_RUN) not found or not executable. Re-run 'make setup'."; exit 1)
	$(RYU_RUN)

# Launch the single-destination tree topology
tree:
	@echo "==> Cleaning Mininet state (ok if it errors)"
	sudo mn -c
	@echo "==> Starting topology"
	sudo python3 cloudlab/topos/tree_topo.py

# Cleanup
clean:
	tmux kill-session -t ryu
	sudo mn -c
	@echo "✓ Cleaned."
