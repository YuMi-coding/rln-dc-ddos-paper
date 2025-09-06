#!/usr/bin/env python3
from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import MAIN_DISPATCHER, CONFIG_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, ipv4, tcp, udp, arp

class AgentController(app_manager.RyuApp):
  OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

  def __init__(self, *args, **kwargs):
    super().__init__(*args, **kwargs)
    self.mac_to_port = {}      # dp.id -> {mac: port}
    self.flow_action = {}      # ((dpid), (src,sport,dst,dport,proto)) -> "allow"/"drop"

  @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
  def switch_features_handler(self, ev):
    dp = ev.msg.datapath; ofp = dp.ofproto; parser = dp.ofproto_parser
    # Table-miss: punt to controller
    mod = parser.OFPFlowMod(datapath=dp, priority=0, match=parser.OFPMatch(),
             instructions=[parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS,
                                                        [parser.OFPActionOutput(ofp.OFPP_CONTROLLER, 128)])])
    dp.send_msg(mod)

  @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
  def packet_in_handler(self, ev):
    msg = ev.msg; dp = msg.datapath; ofp = dp.ofproto; parser = dp.ofproto_parser
    in_port = msg.match.get('in_port')
    pkt = packet.Packet(msg.data)
    eth = pkt.get_protocol(ethernet.ethernet)

    # Simple L2 learning
    self.mac_to_port.setdefault(dp.id, {})
    if eth.src not in self.mac_to_port[dp.id]:
      self.mac_to_port[dp.id][eth.src] = in_port

    if eth.ethertype == 0x806:  # ARP: flood
      actions = [parser.OFPActionOutput(ofp.OFPP_FLOOD)]
      dp.send_msg(parser.OFPPacketOut(datapath=dp, buffer_id=ofp.OFP_NO_BUFFER,
                                      in_port=in_port, actions=actions, data=msg.data))
      return

    ip4 = pkt.get_protocol(ipv4.ipv4)
    l4t = pkt.get_protocol(tcp.tcp) or pkt.get_protocol(udp.udp)
    sport = getattr(l4t, 'src_port', 0); dport = getattr(l4t, 'dst_port', 0)
    key = (dp.id, (ip4.src if ip4 else '', sport, ip4.dst if ip4 else '', dport, ip4.proto if ip4 else 0))
    act = self.flow_action.get(key, "allow")

    if act == "drop":
      match = parser.OFPMatch(eth_type=0x0800, ip_proto=(ip4.proto if ip4 else 0),
                              ipv4_src=ip4.src if ip4 else None, ipv4_dst=ip4.dst if ip4 else None,
                              tcp_src=sport if ip4 and ip4.proto == 6 else None,
                              tcp_dst=dport if ip4 and ip4.proto == 6 else None,
                              udp_src=sport if ip4 and ip4.proto == 17 else None,
                              udp_dst=dport if ip4 and ip4.proto == 17 else None)
      mod = parser.OFPFlowMod(datapath=dp, priority=100, match=match, instructions=[], idle_timeout=20, hard_timeout=60)
      dp.send_msg(mod)
      return

    dst = eth.dst
    out_port = self.mac_to_port[dp.id].get(dst, ofp.OFPP_FLOOD)
    actions = [parser.OFPActionOutput(out_port)]
    # install a short-lived forwarding rule
    match = parser.OFPMatch(in_port=in_port, eth_dst=dst)
    inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
    dp.send_msg(parser.OFPFlowMod(datapath=dp, priority=10, match=match, instructions=inst, idle_timeout=30))
    dp.send_msg(parser.OFPPacketOut(datapath=dp, buffer_id=ofp.OFP_NO_BUFFER,
                                    in_port=in_port, actions=actions, data=msg.data))
