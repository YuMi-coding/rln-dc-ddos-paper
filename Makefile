.PHONY: setup x11-cookie ryu ryu-fg ryu-stop ryu-restart ryu-attach ryu-status tree clean

# Paths
VENV_BIN := .venv/bin
RYU_RUN  := cloudlab/bin/run-ryu.sh
SESSION  := ryu

setup:
	bash cloudlab/setup-cloudlab.sh
	@chmod +x $(RYU_RUN) cloudlab/bin/launch_ryu.py || true
	@echo "==> Merging X11 cookie into root (so xterm works from Mininet)..."
	@if [ -n "$(DISPLAY)" ]; then \
		xauth nlist "$(DISPLAY)" | sudo xauth nmerge -; \
		echo "✓ X11 cookie merged for DISPLAY=$(DISPLAY)"; \
	else \
		echo "Skipping cookie merge: DISPLAY is empty (not an X-forwarded session)."; \
	fi
	@echo "✓ Setup complete. Helpers are executable."

# You can re-run the cookie merge any time with:
x11-cookie:
	@if [ -n "$(DISPLAY)" ]; then \
		xauth nlist "$(DISPLAY)" | sudo xauth nmerge -; \
		echo "✓ X11 cookie merged for DISPLAY=$(DISPLAY)"; \
	else \
		echo "DISPLAY is empty; run this from an X-forwarded SSH session (ssh -Y)."; \
		exit 1; \
	fi

ryu:
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

ryu-fg:
	@test -x $(RYU_RUN) || (echo "ERROR: $(RYU_RUN) not found or not executable. Re-run 'make setup'."; exit 1)
	$(RYU_RUN)

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

clean:
	- tmux kill-session -t $(SESSION)
	- sudo mn -c
	@echo "✓ Cleaned."
