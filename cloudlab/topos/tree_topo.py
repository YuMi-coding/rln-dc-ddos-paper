#!/usr/bin/env python3
from mininet.topo import Topo
from mininet.net import Mininet
from mininet.node import RemoteController
from mininet.link import TCLink
from mininet.cli import CLI

K, L, M, N = 2, 3, 2, 4
LINK_DELAY = '10ms'
N_HOSTS = K * L * M * N
SERVER_BW = N_HOSTS + 2

class SingleDestTopo(Topo):
    def build(self):
        sid = 0
        def sname():
            nonlocal sid
            sid += 1
            return f's{sid}'
        s_server = self.addSwitch(sname(), protocols='OpenFlow13')
        server   = self.addHost('srv')
        self.addLink(server, s_server, cls=TCLink, bw=SERVER_BW, delay=LINK_DELAY)
        for t in range(1, K+1):
            s_leader = self.addSwitch(sname(), protocols='OpenFlow13')
            self.addLink(s_server, s_leader, cls=TCLink, delay=LINK_DELAY)
            for i in range(1, L+1):
                s_inter = self.addSwitch(sname(), protocols='OpenFlow13')
                self.addLink(s_leader, s_inter, cls=TCLink, delay=LINK_DELAY)
                for j in range(1, M+1):
                    s_eg = self.addSwitch(sname(), protocols='OpenFlow13')
                    self.addLink(s_inter, s_eg, cls=TCLink, delay=LINK_DELAY)
                    for h in range(1, N+1):
                        host = self.addHost(f'h{t}_{i}_{j}_{h}')
                        self.addLink(host, s_eg, cls=TCLink, delay=LINK_DELAY)

if __name__ == '__main__':
    topo = SingleDestTopo()
    net = Mininet(topo=topo, link=TCLink,
                  controller=lambda name: RemoteController(name, ip='127.0.0.1', port=6633),
                  autoSetMacs=True, autoStaticArp=True)
    net.start()
    print('*** Server IP:', net.get('srv').IP())
    print('*** Tip: start your Ryu controller first: cloudlab/bin/run-ryu.sh (or make ryu)')
    CLI(net)
    net.stop()
