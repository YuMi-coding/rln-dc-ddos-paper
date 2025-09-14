# --- eventlet compatibility shim (for Ryu's WSGI) ---
try:
    import eventlet.wsgi as _wsgi
    # Older Ryu expects eventlet.wsgi.ALREADY_HANDLED; newer eventlet removed it
    if not hasattr(_wsgi, "ALREADY_HANDLED"):
        _wsgi.ALREADY_HANDLED = object()
except Exception:
    pass
# ----------------------------------------------------

# controller.py
from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import MAIN_DISPATCHER, CONFIG_DISPATCHER, set_ev_cls
from ryu.controller.ofp_event import EventOFPStateChange
from ryu.ofproto import ofproto_v1_3 as ofp
from ryu.ofproto import ofproto_v1_3_parser as parser
from ryu.lib.packet import packet, ethernet, arp
import socket, struct, threading, json, pickle

BUILD_PORT = 6666
ACT_PORT   = BUILD_PORT + 1

# Pick a nominal per-port max, e.g., 100000 kbps (100 Mbps). Tune this to your topo link speeds.
NOMINAL_KBPS = 100000

class RLNController(app_manager.RyuApp):
    OFP_VERSIONS = [ofp.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(RLNController, self).__init__(*args, **kwargs)
        self.datapaths = {}
        self.entry_map = {}
        self.escape_map = {}
        self.inner_host_macs = {}
        self.prevent_smart = "udp"
        self.port_dest_map = {}
        # start two threads: bootstrap (pickle) and action server (json)
        threading.Thread(target=self._bootstrap_listener, daemon=True).start()
        threading.Thread(target=self._action_listener, daemon=True).start()

    @set_ev_cls(EventOFPStateChange)
    def _state_change(self, ev):
        dp = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            self.datapaths[dp.id] = dp
        else:
            self.datapaths.pop(dp.id, None)

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def _features(self, ev):
        dp = ev.msg.datapath
        # table-miss: send to controller
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)]
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        dp.send_msg(parser.OFPFlowMod(datapath=dp, priority=0, match=match, instructions=inst))

        # NEW: baseline connectivity
        self._add_base_flows(dp)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in(self, ev):
        # simple ARP handling (optional)
        msg, dp = ev.msg, ev.msg.datapath
        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth.ethertype == 0x0806:
            a = pkt.get_protocol(arp.arp)
            # naive flood ARP
            actions = [parser.OFPActionOutput(ofp.OFPP_FLOOD)]
            dp.send_msg(parser.OFPPacketOut(datapath=dp, buffer_id=ofp.OFP_NO_BUFFER,
                                            in_port=msg.match.get('in_port', ofp.OFPP_CONTROLLER),
                                            actions=actions, data=msg.data))
    def _add_base_flows(self, dp):
        # ARP: flood (priority 10)
        match = parser.OFPMatch(eth_type=0x0806)
        actions = [parser.OFPActionOutput(ofp.OFPP_FLOOD)]
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        dp.send_msg(parser.OFPFlowMod(datapath=dp, priority=10, match=match, instructions=inst))

        # IPv4: normal L2 switching (priority 1)
        match = parser.OFPMatch(eth_type=0x0800)
        actions = [parser.OFPActionOutput(ofp.OFPP_NORMAL)]
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        dp.send_msg(parser.OFPFlowMod(datapath=dp, priority=1, match=match, instructions=inst))
    # def _bootstrap_listener(self):
    #     # same as your code: accept pickle once
    #     s = socket.socket()
    #     s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    #     s.bind(("127.0.0.1", BUILD_PORT))
    #     s.listen(1)
    #     conn, _ = s.accept()
    #     try:
    #         # read length (uint64) then blob
    #         length = struct.unpack("!Q", conn.recv(8))[0]
    #         buf = b""
    #         while len(buf) < length:
    #             buf += conn.recv(min(65536, length-len(buf)))
    #         (self.entry_map, self.escape_map, self.inner_host_macs,
    #          self.prevent_smart, self.port_dest_map) = pickle.loads(buf)
    #     finally:
    #         conn.close()
    #         s.close()

    def _ensure_meter(self, dp, meter_id, kbps):
        # Drop all above kbps (approximate pdrop via rate shaping)
        bands = [parser.OFPMeterBandDrop(rate=int(max(1, kbps)), burst_size=int(kbps // 10 or 1))]
        mod = parser.OFPMeterMod(datapath=dp,
                                command=ofp.OFPMC_ADD,
                                flags=ofp.OFPMF_KBPS,
                                meter_id=meter_id,
                                bands=bands)
        try:
            dp.send_msg(mod)
        except Exception:
            # If it already exists, you might need to modify (OFPMC_MODIFY); keep it simple for now.
            pass


    def _bootstrap_listener(self):
        import time
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        # Try to connect to MARL’s bootstrap server (which binds 127.0.0.1:6666)
        while True:
            try:
                s.connect(("127.0.0.1", BUILD_PORT))
                break
            except Exception:
                time.sleep(0.2)

        try:
            # read length (uint64 big-endian) then blob
            hdr = b""
            while len(hdr) < 8:
                chunk = s.recv(8 - len(hdr))
                if not chunk:
                    raise IOError("bootstrap socket closed")
                hdr += chunk
            length = struct.unpack("!Q", hdr)[0]

            buf = b""
            while len(buf) < length:
                chunk = s.recv(min(65536, length - len(buf)))
                if not chunk:
                    raise IOError("bootstrap socket closed mid-stream")
                buf += chunk

            (self.entry_map, self.escape_map, self.inner_host_macs,
            self.prevent_smart, self.port_dest_map) = pickle.loads(buf)
        finally:
            s.close()


    def _action_listener(self):
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", ACT_PORT))
        s.listen(16)
        while True:
            conn, _ = s.accept()
            threading.Thread(target=self._handle_one, args=(conn,), daemon=True).start()

    def _recv_exact(self, c, n):
        buf = b""
        while len(buf) < n:
            chunk = c.recv(n-len(buf))
            if not chunk: raise IOError("socket closed")
            buf += chunk
        return buf

    def _handle_one(self, c):
        try:
            length = struct.unpack("!I", self._recv_exact(c, 4))[0]
            blob = self._recv_exact(c, length)
            req = json.loads(blob.decode("utf-8"))
            dpid = int(req.get("dpid"))
            dp = self.datapaths.get(dpid)
            
            if not dp:
                c.sendall(struct.pack("!I", 1)); c.close(); return
            op = req.get("op"); payload = req.get("payload", {})
            if op == "ensure_group_and_flow":
                self._ensure_groups(dp, payload.get("ensure_groups", []))
                self._flow_write_actions(dp, payload["flow"])
            elif op == "ensure_group":
                self._ensure_groups(dp, [payload])
            elif op == "flow_write_actions":
                self._flow_write_actions(dp, payload)
            elif op == "goto_table":
                # optional, if you use tables
                pass
            elif op == "rewrite_src_to_controller":
                self._rewrite_src_to_controller(dp, payload)
            c.sendall(struct.pack("!I", 0))
        except Exception as e:
            try: c.sendall(struct.pack("!I", 2))
            except: pass
        finally:
            c.close()

    # --- builders ---
    def _ensure_groups(self, dp, groups):
        # for g in groups:
        #     gid = g["group_id"]
        #     acts = self._actions_from_spec(dp, g.get("actions", []))
        #     buckets = [parser.OFPBucket(actions=acts)]
        #     req = parser.OFPGroupMod(datapath=dp, command=ofp.OFPGC_ADD,
        #                              type_=ofp.OFPGT_INDIRECT, group_id=gid, buckets=buckets)
        #     dp.send_msg(req)

        for g in groups:
            gid = g["group_id"]
            # Map group index to "allow probability" as  gid / (num_groups-1)
            # You can pass num_groups in payload, or just assume a fixed 20 (matches marl default).
            num_groups = 20
            allow_p = float(gid) / float(max(1, num_groups - 1))
            # Allow allow_p * NOMINAL, drop rest
            self._ensure_meter(dp, meter_id=1000 + gid, kbps=int(NOMINAL_KBPS * allow_p))

    # def _flow_write_actions(self, dp, spec):
    #     prio = spec.get("priority", 1)
    #     table = spec.get("table", 0)
    #     match = self._match_from_spec(spec.get("match", {}))
    #     actions = self._actions_from_spec(dp, spec.get("actions", []))
    #     inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
    #     dp.send_msg(parser.OFPFlowMod(datapath=dp, table_id=table, priority=prio,
    #                                   match=match, instructions=inst, idle_timeout=30))

    def _flow_write_actions(self, dp, spec):
        meter_id = getattr(self, "_pending_meter_id", None)
        if meter_id is not None:
            inst.insert(0, parser.OFPInstructionMeter(meter_id))
            self._pending_meter_id = None

        prio = spec.get("priority", 1)
        table = spec.get("table", 0)
        match = self._match_from_spec(spec.get("match", {}))
        actions = self._actions_from_spec(dp, spec.get("actions", []))
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        # If actions included a PDROP_LEGACY pnum or a group index, attach a meter
        meter_id = spec.get("meter_id")
        if meter_id is not None:
            inst.insert(0, parser.OFPInstructionMeter(meter_id))
        dp.send_msg(parser.OFPFlowMod(datapath=dp, table_id=table, priority=prio,
                                    match=match, instructions=inst, idle_timeout=30))


    def _rewrite_src_to_controller(self, dp, spec):
        prio = spec.get("priority", 1)
        match = self._match_from_spec(spec.get("match", {}))
        setf = spec.get("set_fields", {})
        actions = []
        if "ipv4_src" in setf:
            actions.append(parser.OFPActionSetField(ipv4_src=setf["ipv4_src"]))
        actions.append(parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER))
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        dp.send_msg(parser.OFPFlowMod(datapath=dp, priority=prio, match=match, instructions=inst,
                                      idle_timeout=30))

    def _match_from_spec(self, m):
        kw = {}
        if "eth_type" in m: kw["eth_type"] = int(m["eth_type"])
        def ipk(field):
            v = m[field]
            if isinstance(v, dict):
                return (v["value"], v.get("mask", None))
            return (v, None)
        if "ipv4_src" in m:
            val, mask = ipk("ipv4_src")
            if mask: kw["ipv4_src_masked"] = (val, mask)
            else:    kw["ipv4_src"] = val
        if "ipv4_dst" in m:
            val, mask = ipk("ipv4_dst")
            if mask: kw["ipv4_dst_masked"] = (val, mask)
            else:    kw["ipv4_dst"] = val
        return parser.OFPMatch(**kw)

    # def _actions_from_spec(self, dp, acts):
    #     out = []
    #     for a in acts:
    #         t = a["type"]
    #         if t == "OUTPUT":
    #             port = a["port"]
    #             if port == "CONTROLLER": out.append(parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER))
    #             elif port == "FLOOD":    out.append(parser.OFPActionOutput(ofp.OFPP_FLOOD))
    #             elif port == "NORMAL":   out.append(parser.OFPActionOutput(ofp.OFPP_NORMAL))
    #             else:                    out.append(parser.OFPActionOutput(int(port)))
    #         elif t == "GROUP":
    #             out.append(parser.OFPActionGroup(a["group_id"]))
    #         elif t == "PDROP":
    #             # Approximate probabilistic drop: map desired probability into a meter or sample
    #             # Simplest: emulate by sampling at app level; better: use a meter band drop. Placeholder:
    #             pass
    #     return out


    def _actions_from_spec(self, dp, acts):
        out = []
        # default: None; filled if we see PDROP/PDROP_LEGACY
        self._pending_meter_id = None
        for a in acts:
            t = a["type"]
            if t == "OUTPUT":
                ...
            elif t == "GROUP":
                out.append(parser.OFPActionGroup(a["group_id"]))
                # If your group_id encodes prob group, also meter on same id:
                gid = a["group_id"]
                self._pending_meter_id = 1000 + int(gid)
            elif t == "PDROP":
                gid = a.get("group_index", 0)
                self._pending_meter_id = 1000 + int(gid)
            elif t == "PDROP_LEGACY":
                # Map 0xffffffff..0x0 to ~[0..1] allowance; keep a coarse mapping
                pnum = int(a.get("pnum", 0xffffffff))
                gid = int(round((pnum / 0xffffffff) * 19))  # 20 groups
                self._pending_meter_id = 1000 + gid
        return out
