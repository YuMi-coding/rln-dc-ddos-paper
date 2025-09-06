#!/usr/bin/env python3
from mininet.topo import Topo
from mininet.net import Mininet
from mininet.node import RemoteController
from mininet.link import TCLink
from mininet.cli import CLI

K, L, M, N = 2, 3, 2, 4          # change N to 2/4/8/16 for paper scales
LINK_DELAY = '10ms'              # all links
N_HOSTS = K * L * M * N
SERVER_BW = N_HOSTS + 2          # Mbit/s on server-switch only (paper)

class SingleDestTopo(Topo):
  def build(self):
    s_srv = self.addSwitch('ss')        # server-side switch
    srv   = self.addHost('srv')
    self.addLink(srv, s_srv, cls=TCLink, bw=SERVER_BW, delay=LINK_DELAY)
    for t in range(1, K+1):
      s_lead = self.addSwitch(f'sl{t}')
      self.addLink(s_srv, s_lead, cls=TCLink, delay=LINK_DELAY)
      for i in range(1, L+1):
        s_inter = self.addSwitch(f'si{t}_{i}')
        self.addLink(s_lead, s_inter, cls=TCLink, delay=LINK_DELAY)
        for j in range(1, M+1):
          s_eg = self.addSwitch(f'se{t}_{i}_{j}')  # agent/egress
          self.addLink(s_inter, s_eg, cls=TCLink, delay=LINK_DELAY)
          for h in range(1, N+1):
            host = self.addHost(f'h{t}_{i}_{j}_{h}')
            self.addLink(host, s_eg, cls=TCLink, delay=LINK_DELAY)

if __name__ == '__main__':
  topo = SingleDestTopo()
  net = Mininet(topo=topo, link=TCLink,
                controller=lambda name: RemoteController(name, ip='127.0.0.1', port=6633))
  net.start()
  print("*** Server IP:", net.get('srv').IP())
  CLI(net)
  net.stop()
