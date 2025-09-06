#!/usr/bin/env bash
set -euo pipefail

sudo apt-get update
sudo apt-get -y install git build-essential python3-pip python3-venv \
  tmux curl nginx tcpreplay hping3 rustc cargo

# Mininet (installs OVS & deps)
if [ ! -d /opt/mininet ]; then
  sudo mkdir -p /opt && sudo chown "$USER":"$USER" /opt
  git clone https://github.com/mininet/mininet /opt/mininet
  bash /opt/mininet/util/install.sh -a
fi

# Ryu (Python 3)
python3 -m pip install --upgrade pip
python3 -m pip install ryu

echo "OK. Reboot is not required, but 'sudo mn -c' before first use is a good idea."
