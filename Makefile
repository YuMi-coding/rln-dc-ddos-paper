.PHONY: setup ryu tree clean

setup:
	bash cloudlab/setup-cloudlab.sh

ryu:
	tmux new -d -s ryu 'ryu-manager cloudlab/ryu/agent_controller.py' || true
	tmux ls | grep ryu || true

tree:
	sudo python3 cloudlab/topos/tree_topo.py

clean:
	sudo mn -c || true
	tmux kill-session -t ryu || true
