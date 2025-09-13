.PHONY: setup x11-cookie ryu ryu-fg ryu-stop ryu-restart ryu-attach ryu-status tree marl clean

# Paths
VENV_BIN := .venv/bin
VENV_PY := $(VENV_BIN)/python
RYU_RUN  := cloudlab/bin/run-ryu.sh
SESSION  := ryu
SHELL := /bin/bash
SYS_SITE := $(shell python3 -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')


# NEW: app/scripts we want to run
RYU_APP  := code/marl/controller_py3.py
MARL_DIR := code/marl
MARL_APP := marl_py3.py
MARL_ARGS ?=           # you can override on the CLI, e.g.: MARL_ARGS='--episodes 1'

setup:
	bash cloudlab/setup-cloudlab.sh
	@chmod +x $(RYU_RUN) cloudlab/bin/launch_ryu.py || true
	@echo "==> Enabling X11 for root (so xterm works from Mininet)..."
	@if [[ -n "$$DISPLAY" ]]; then \
		COOKIE_TMP="$$(mktemp)"; trap 'rm -f "$$COOKIE_TMP"' EXIT; \
		if ! xauth nlist "$$DISPLAY" > "$$COOKIE_TMP" 2>/dev/null; then \
			echo "✗ Could not read your X cookie (is X forwarding on? try: ssh -Y ...)"; exit 1; \
		fi; \
		if sudo -H env XAUTHORITY=/root/.Xauthority xauth nmerge - < "$$COOKIE_TMP" >/dev/null 2>&1; then \
			echo "✓ X11 cookie merged into /root/.Xauthority for $$DISPLAY"; \
		else \
			if command -v xhost >/dev/null 2>&1; then \
				xhost +SI:localuser:root >/dev/null; \
				echo "✓ Allowed root to use your X server via xhost (fallback)"; \
			else \
				echo "✗ Merge failed and xhost not available. Install xhost (x11-xserver-utils) or fix X setup."; \
			fi; \
		fi; \
	else \
		echo "Skipping X setup: DISPLAY is empty (not an X-forwarded session)."; \
	fi
	@echo "✓ Setup complete. Helpers are executable."

x11-cookie:
	@echo "==> Enabling X11 for root (so xterm works from Mininet)..."
	@if [[ -n "$$DISPLAY" ]]; then \
		COOKIE_TMP="$$(mktemp)"; trap 'rm -f "$$COOKIE_TMP"' EXIT; \
		if ! xauth nlist "$$DISPLAY" > "$$COOKIE_TMP" 2>/dev/null; then \
			echo "✗ Could not read your X cookie (is X forwarding on? try: ssh -Y ...)"; exit 1; \
		fi; \
		if sudo -H env XAUTHORITY=/root/.Xauthority xauth nmerge - < "$$COOKIE_TMP" >/dev/null 2>&1; then \
			echo "✓ X11 cookie merged into /root/.Xauthority for $$DISPLAY"; \
		else \
			if command -v xhost >/dev/null 2>&1; then \
				xhost +SI:localuser:root >/dev/null; \
				echo "✓ Allowed root to use your X server via xhost (fallback)"; \
			else \
				echo "✗ Merge failed and xhost not available. Install xhost (x11-xserver-utils) or fix X setup."; \
			fi; \
		fi; \
	else \
		echo "DISPLAY is empty; run this from an X-forwarded SSH session (ssh -Y/-X)."; \
		exit 1; \
	fi


ryu:
	@command -v tmux >/dev/null 2>&1 || (echo "tmux not found; install it or run 'make ryu-fg'"; exit 1)
	@test -x $(RYU_RUN) || (echo "ERROR: $(RYU_RUN) not found or not executable. Re-run 'make setup'."; exit 1)
	@if tmux has-session -t $(SESSION) 2>/dev/null; then \
		echo "Ryu already running in tmux session '$(SESSION)'."; \
		echo "Attach:  tmux attach -t $(SESSION)  |  Restart:  make ryu-restart"; \
	else \
		tmux new-session -d -s $(SESSION) 'RYU_APP="$(RYU_APP)" $(RYU_RUN)'; \
		sleep 1; \
		if tmux has-session -t $(SESSION) 2>/dev/null; then \
			echo "✓ Ryu started in tmux session '$(SESSION)' using $(RYU_APP)."; \
			echo "  Attach:  tmux attach -t $(SESSION)"; \
			echo "  Detach:  Ctrl-b d"; \
		else \
			echo "✗ Ryu failed to start. Try 'make ryu-fg' to see errors."; \
			exit 1; \
		fi; \
	fi

ryu-fg:
	@test -x $(RYU_RUN) || (echo "ERROR: $(RYU_RUN) not found or not executable. Re-run 'make setup'."; exit 1)
	RYU_APP="$(RYU_APP)" $(RYU_RUN)

ryu-stop:
	- tmux kill-session -t $(SESSION)
	@echo "✓ Ryu stopped (if it was running)."

ryu-restart: ryu-stop ryu

ryu-attach:
	@tmux attach -t $(SESSION) || (echo "No tmux session '$(SESSION)'. Start it with 'make ryu'."; exit 1)

ryu-status:
	@tmux ls || true

tree:
	@echo "==> Cleaning Mininet state (ok if it errors)"
	- sudo mn -c
	@echo "==> Starting topology (preserving X11 DISPLAY)"
	sudo DISPLAY=$(DISPLAY) python3 cloudlab/topos/tree_topo.py

# NEW: run the MARL experiment (sudo needed for Mininet/OVS)
marl:
	@echo "==> Cleaning Mininet state (ok if it errors)"
	- sudo mn -c
	@echo "==> Running MARL experiment (code/marl/marl_py3.py) with venv Python: $(VENV_PY)"
	cd $(MARL_DIR) && sudo -E env PYTHONPATH="$(SYS_SITE):$$PYTHONPATH" \
		$(abspath $(VENV_PY)) $(MARL_APP) $(MARL_ARGS)

clean:
	- tmux kill-session -t $(SESSION)
	- sudo mn -c
	@echo "✓ Cleaned."
