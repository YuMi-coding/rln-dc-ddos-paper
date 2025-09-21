# -*- coding: utf-8 -*-
from __future__ import annotations

import errno
import itertools
import json
import math
import os
import pickle
import random
import select
import signal
import socket
import struct
import sys
import time
from contextlib import closing
from subprocess import PIPE, Popen
from typing import Any, Dict, List, Optional, Tuple
import threading

import networkx as nx
import numpy as np
from mininet.clean import Cleanup
from mininet.cli import CLI
from mininet.link import TCLink
from mininet.net import Mininet
from mininet.node import OVSSwitch, RemoteController, Switch
from mininet.topo import Topo

# --- logging setup (console + file) ---
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime
import builtins

LOG = logging.getLogger("marl")
LOG.propagate = False  # don't duplicate to root
BW_SOCK_PATH = os.environ.get("BWMON_SOCK", "/tmp/bwmon-sock")

def setup_logging(log_dir: str, level: str = "INFO") -> str:
    os.makedirs(log_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    logfile = os.path.join(log_dir, f"run_{ts}.log")

    # parse level
    lvl = getattr(logging, str(level).upper(), logging.INFO)

    # formatter (file is verbose; console is shorter)
    file_fmt = logging.Formatter(
        fmt="%(asctime)s.%(msecs)03d %(levelname)s %(name)s [%(process)d] %(filename)s:%(lineno)d | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    con_fmt = logging.Formatter(
        fmt="%(levelname)s: %(message)s"
    )

    # handlers
    LOG.setLevel(logging.DEBUG)  # logger accepts everything; handlers filter
    # file: rotate at ~10MB, keep 3
    fh = RotatingFileHandler(logfile, maxBytes=10_000_000, backupCount=3)
    fh.setLevel(logging.DEBUG)     # capture everything to file
    fh.setFormatter(file_fmt)

    ch = logging.StreamHandler()
    ch.setLevel(lvl)               # console obeys --log-level
    ch.setFormatter(con_fmt)

    # reset handlers if re-run
    LOG.handlers[:] = []
    LOG.addHandler(fh)
    LOG.addHandler(ch)

    # shadow print inside this module → logs at INFO
    def _print_to_log(*args, **kwargs):
        sep = kwargs.get("sep", " ")
        end = kwargs.get("end", "")
        msg = sep.join(str(a) for a in args) + end
        LOG.info(msg)

    # NOTE: this shadows print only in this module (not globally)
    globals()["print"] = _print_to_log

    LOG.info("logging initialized; file=%s level=%s", logfile, level)
    return logfile

def tee_process_stdout(proc, prefix: str):
    def _reader():
        try:
            for raw in iter(proc.stdout.readline, b""):
                LOG.info("%s%s", prefix, raw.decode(errors="replace").rstrip("\n"))
        except Exception:
            LOG.exception("stdout tee failed for %s", prefix)
    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    return t



# On-wire/request
_SZ_U32_BE       = struct.Struct("!I")   # count
_IP_U32_BE       = struct.Struct("!I")   # IPs we send

# Reply header/counters (native on your build)
_TIME_I64_NATIVE = struct.Struct("=q")
_U64_NATIVE      = struct.Struct("=Q")

# FlowMeasurement head (80 bytes native): q, 6Q, 6f
_FM_HEAD_NATIVE  = struct.Struct("=q6Q6f")
_FM_SIZE         = 88  # 80 + ip:u32 + pad4

def _read_exact(sock, nbytes: int, deadline: float) -> bytes:
    buf = bytearray()
    end = time.time() + float(deadline)
    while len(buf) < nbytes:
        rem = end - time.time()
        if rem <= 0:
            raise TimeoutError(f"timed out waiting for {nbytes} bytes, got {len(buf)}")
        sock.settimeout(rem)
        chunk = sock.recv(nbytes - len(buf))
        if not chunk:
            raise TimeoutError(f"peer closed while waiting for {nbytes} bytes, got {len(buf)}")
        buf.extend(chunk)
    sock.settimeout(None)
    return bytes(buf)

def _ip_be_to_str(u32_be: int) -> str:
    return socket.inet_ntoa(struct.pack("!I", u32_be & 0xffffffff))

def _connect_unix(path, retries=12, init_delay=0.02, max_delay=0.25, timeout=2.0):
    """
    Connect to a UNIX domain socket with a short retry/backoff.
    Returns a *blocking* socket (no per-call settimeout).
    """
    delay = float(init_delay)
    last_exc = None
    for _ in range(int(retries)):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.settimeout(timeout)
            s.connect(path)
            s.settimeout(None)  # back to blocking; we do our own deadlines in _read_exact
            return s
        except (FileNotFoundError, ConnectionRefusedError, TimeoutError, OSError) as e:
            last_exc = e
            try: s.close()
            except: pass
            time.sleep(delay)
            delay = min(max_delay, delay * 1.6)
    raise ConnectionError(f"could not connect to {path}: {last_exc}")
# RL & state machines (your local modules)
from sarsa_py3 import SarsaLearner, QLearner
from spf_py3 import *  # SpfMachine, MarlMachine, etc.

# -------------------------------------------------------------------
# Controller ports (Ryu side)
#   - controller.py should listen for bootstrap on 6666 (pickle)
#   - and JSON actions on 6667 (controller_build_port + 1)
# -------------------------------------------------------------------
controller_build_port = 6666
stats_port = 9932
external_controller = True
ctl_proc = None

# -------------------------
# JSON bridge to controller
# -------------------------
def _send_len_prefixed(sock: socket.socket, blob: bytes) -> None:
    sock.sendall(struct.pack("!I", len(blob)))
    sock.sendall(blob)

def send_ctl(action_obj: Dict[str, Any], port: int = controller_build_port + 1) -> int:
    """
    Send a JSON action to the Ryu controller (127.0.0.1:port).
    Returns simple int status (0 ok).
    """
    with closing(socket.create_connection(("127.0.0.1", port))) as s:
        data = json.dumps(action_obj).encode("utf-8")
        _send_len_prefixed(s, data)
        resp = s.recv(4)
        return struct.unpack("!I", resp)[0] if len(resp) == 4 else 0

def ip_to_int(ip_str: str) -> int:
    return struct.unpack("!I", socket.inet_aton(ip_str))[0]

def int_to_ip(ip_int: int) -> str:
    return socket.inet_ntoa(struct.pack("!I", int(ip_int)))

def ip_to_int_native(ip_str: str) -> int:
    # native-endian u32 (matches non-socketed code path)
    return struct.unpack("=I", socket.inet_aton(ip_str))[0]

def _pick_narrowing_indices(s, always_include_global: bool):
    # Returns either a list of indices (for action/update_narrowing) or None
    mod = 1 if always_include_global else 0
    tsc = int(getattr(s, "tiling_set_count", 0) or 0)

    # If there are no tiles at all, give up gracefully.
    if tsc <= 0:
        return None

    # Start with the bias/global tile if requested.
    base = [0] if always_include_global else []

    # If there are no non-bias tiles to sample, return bias-only (still valid).
    if tsc <= mod:
        return base if base else None

    # Sample one non-bias tile in [mod, tsc-1]
    idx = int(np.random.randint(mod, tsc))
    return base + [idx]

# --- monitor status reporter -------------------------------------------------
class MonitorReporter:
    """
    Logs per-iteration monitor status and saves a CSV:
      step,time_ns,iface,ingood_mbps,inbad_mbps,outgood_mbps,outbad_mbps,flows_0,...,flows_{N-1}
    """
    def __init__(self, log_dir: str, n_ifs: int, n_agents: int, episode: int, log_every: int = 1):
        self.log = logging.getLogger("marl.status")
        self.n_ifs = int(n_ifs)
        self.n_agents = int(n_agents)
        self.step = 0
        self.log_every = max(1, int(log_every))
        os.makedirs(log_dir, exist_ok=True)
        self.path = os.path.join(log_dir, f"monitor_ep{episode:04d}.csv")
        self._fp = open(self.path, "w", buffering=1)
        header = ["step", "time_ns", "iface", "ingood_mbps", "inbad_mbps",
                  "outgood_mbps", "outbad_mbps"] + [f"flows_{i}" for i in range(self.n_agents)]
        self._fp.write(",".join(header) + "\n")

    def tick(self, time_ns: int, unfused_load_mbps, parsed_flows):
        """
        unfused_load_mbps: list of length 2*n_ifs of (good, bad) Mb/s:
            [ (IN_g,IN_b) for if0, (OUT_g,OUT_b) for if0, (IN_g,IN_b) for if1, ... ]
        parsed_flows: list of per-agent flow lists
        """
        self.step += 1

        # per-agent flow counts (for quick health checking)
        flow_counts = [len(parsed_flows[i]) if i < len(parsed_flows) else 0
                       for i in range(self.n_agents)]

        # CSV rows: one per interface
        for ifi in range(self.n_ifs):
            ing, inb = unfused_load_mbps[2*ifi]
            outg, outb = unfused_load_mbps[2*ifi + 1]
            row = [str(self.step), str(int(time_ns)), str(ifi),
                   f"{float(ing):.6f}", f"{float(inb):.6f}",
                   f"{float(outg):.6f}", f"{float(outb):.6f}"] + [str(c) for c in flow_counts]
            self._fp.write(",".join(row) + "\n")

        # Console/file log (compact): total IN/OUT (good/bad) across ifaces + flow counts.
        if (self.step % self.log_every) == 0:
            tot_ing = sum(unfused_load_mbps[2*i][0] for i in range(self.n_ifs))
            tot_inb = sum(unfused_load_mbps[2*i][1] for i in range(self.n_ifs))
            tot_outg = sum(unfused_load_mbps[2*i+1][0] for i in range(self.n_ifs))
            tot_outb = sum(unfused_load_mbps[2*i+1][1] for i in range(self.n_ifs))
            self.log.info(
                "[mon] step=%d t=%.3fms IN=(g=%.2f,b=%.2f) OUT=(g=%.2f,b=%.2f) flows=%s",
                self.step, float(time_ns) / 1e6,
                tot_ing, tot_inb, tot_outg, tot_outb,
                "|".join(str(c) for c in flow_counts)
            )

            # Anomaly hints
            if all(c == 0 for c in flow_counts):
                self.log.warning("[mon] no per-flow entries this step (is bwmon producing flows? allowed set?)")
            if time_ns <= 0:
                self.log.warning("[mon] non-positive time_ns=%s (window duration fallback in effect)", time_ns)

    def close(self):
        try:
            self._fp.close()
        except Exception:
            pass


# ------------------------------------------------------------
# Main experiment: Python-3 rewrite, twink/ofp5 replaced by
# controller JSON ops; preserves original behavior/structure.
# ------------------------------------------------------------
def marlExperiment(
    linkopts = { "delay": 10 },  # ms
    n_teams: int = 1,

    # per-team
    n_inters: int = 2,
    n_learners: int = 3,
    host_range = [2, 2],  # [min,max] per learner

    calc_max_capacity = None,

    P_good: float = 0.6,
    good_range = [0, 1],
    evil_range = [2.5, 6],
    good_file: str = "../../data/pcaps/bigFlows.pcap",
    bad_file: Optional[str] = None,

    topol: str = "tree",   # or "ecmp"
    ecmp_servers: int = 8,
    ecmp_k: int = 4,

    explore_episodes: int = 80000,
    episodes: int = 1000,
    episode_length: int = 5000,
    separate_episodes: bool = False,

    max_bw: Optional[float] = None,
    pdrop_magnitudes = [0.1*n for n in range(10)],

    alpha: float = 0.05,
    epsilon: float = 0.3,
    discount: float = 0.0,
    break_equal = None,

    algo: str = "sarsa",
    trace_decay: float = 0.0,
    trace_threshold: float = 0.0001,
    use_path_measurements: bool = True,

    model: str = "tcpreplay",  # or "nginx"
    submodel: Optional[str] = None,
    rescale_opus: bool = False,
    mix_model = None,

    use_controller: bool = False,
    moralise_ips: bool = True,

    dt: float = 0.001,

    old_style: bool = False,
    force_host_tc: bool = False,
    protect_final_hop: bool = True,

    with_ratio: bool = False,
    override_action = None,
    manual_early_limit = None,
    estimate_const_limit: bool = False,

    rf: str = "ctl",

    rand_seed = 0xcafed00d,
    rand_state = None,
    force_cmd_routes: bool = False,

    rewards: List[List[float]] = [],
    good_traffic_percents: List[List[float]] = [],
    total_loads: List[List[float]] = [],
    store_sarsas: List = [],
    action_comps: List = [],

    reward_direction: str = "in",
    state_direction: str = "in",
    actions_target_flows: bool = False, # <-- set to True in the call below

    bw_mon_socketed: bool = False,
    unix_sock: bool = True,
    print_times: bool = False,
    prevent_smart_switch_recording: str = "udp",  # "no" | "udp" | "always"
    record_times: bool = False,
    record_deltas_in_times: bool = False,

    contributors = [],
    restrict = None,

    single_learner: bool = False,
    single_learner_ep_scale: bool = True,

    spiffy_mode: bool = False,

    randomise: bool = False,
    randomise_count = None,
    randomise_new_ip: bool = False,

    split_codings: bool = False,
    extra_codings: List = [],
    feature_max: int = 12,
    combine_with_last_action = [12, 13, 14, 15, 16, 17, 18, 19],
    strip_last_action: bool = True,

    explore_feature_isolation_modifier: float = 1.0,
    explore_feature_isolation_duration: int = 5,
    always_include_global: bool = True,
    always_include_bias: bool = True,

    trs_maxtime = None,
    reward_band: float = 1.0,

    spiffy_but_bad: bool = False,
    spiffy_act_time: float = 5.0,
    spiffy_max_experiments: int = 16,
    spiffy_min_experiments: int = 1,
    spiffy_pick_prob: float = 0.2,
    spiffy_drop_rate: float = 0.15,
    spiffy_traffic_dir: str = "in",
    spiffy_mbps_cutoff: float = 0.1,
    spiffy_expansion_factor: float = 5.0,

    broken_math: bool = False,
    num_drop_groups: int = 20,    
    
    
    interactive_cli_pre: bool = False,
    interactive_cli_post: bool = True,


    # Reporting for external orchestration
    log_dir: str = "logs",
    log_actions: bool = True,
    allow_threshold: float = 0.5,   # allow_prob < 0.5 => predict "bad"

):

    # --------------- Agent selection ----------------
    agent_classes = {"sarsa": SarsaLearner, "q": QLearner}
    AgentClass = agent_classes[algo]

    linkopts_core = dict(linkopts)
    linkopts_core["bw"] = manual_early_limit

    if rand_state is not None:
        random.setstate(rand_state)
    elif rand_seed is not None:
        random.seed(rand_seed)

    c0 = RemoteController("c0", ip="127.0.0.1", port=6633)

    class MaybeControlledSwitch(OVSSwitch):
        def __init__(self, name, **params):
            super(MaybeControlledSwitch, self).__init__(name, **params)
            self.controlled = False

        def start(self, controllers):
            return super(MaybeControlledSwitch, self).start(self._controller_list())

        # def _controller_list(self):
        #     return [c0] if use_controller and self.controlled else []

        def _controller_list(self):
        # Attach ALL switches to the controller when use_controller is enabled
            return [c0] if use_controller else []


    if max_bw is None:
        max_bw = n_teams * n_inters * n_learners * host_range[1] * evil_range[1]

    if bad_file is None:
        bad_file = good_file

    if calc_max_capacity is None:
        calc_max_capacity = lambda hosts: good_range[1]*hosts + 2

    if manual_early_limit is None and estimate_const_limit:
        linkopts_core["bw"] = calc_max_capacity(
            host_range[1] * n_teams * n_inters * n_learners / (1.0 if topol != "ecmp" else float(ecmp_servers))
        )

    # --------------- Reward functions ----------------
    def std_marl(total_svr_load, legit_svr_load, true_legit_svr_load,
                 total_leader_load, legit_leader_load, true_legit_leader_load,
                 num_teams, max_load):
        svr_fail = total_svr_load > max_load
        return -1 if svr_fail else (legit_svr_load / true_legit_svr_load)

    def itl(total_svr_load, legit_svr_load, true_legit_svr_load,
            total_leader_load, legit_leader_load, true_legit_leader_load,
            num_teams, max_load):
        leader_fail = total_leader_load > (float(max_load) / num_teams)
        return -1 if leader_fail else (legit_leader_load / true_legit_leader_load)

    def ctl(total_svr_load, legit_svr_load, true_legit_svr_load,
            total_leader_load, legit_leader_load, true_legit_leader_load,
            num_teams, max_load):
        svr_fail = total_svr_load > max_load
        leader_fail = total_leader_load > (float(max_load) / num_teams)
        return -1 if (svr_fail and leader_fail) else (legit_leader_load / true_legit_leader_load)

    rfs = {"marl": std_marl, "itl": itl, "ctl": ctl}
    reward_func = rfs[rf]

    def safe_reward_func(f, total_svr_load, legit_svr_load, true_legit_svr_load,
                         total_leader_load, legit_leader_load, true_legit_leader_load,
                         num_teams, max_load, ratio):
        return f(total_svr_load, min(legit_svr_load, true_legit_svr_load), true_legit_svr_load,
                 total_leader_load, min(legit_leader_load, true_legit_leader_load), true_legit_leader_load,
                 num_teams, reward_band * max_load * ratio)

    # --------------- Action/feature setup -------------
    if spiffy_mode or spiffy_but_bad:
        AcTrans = SpfMachine
        aset = range(3)
        default_machine_state = 1
    else:
        AcTrans = MarlMachine
        aset = pdrop_magnitudes
        default_machine_state = 0

    if spiffy_but_bad:
        actions_target_flows = True

    if single_learner and single_learner_ep_scale:
        explore_episodes = int(explore_episodes * float(n_teams * n_inters * n_learners))

    # ---- helpers for JSON policy → controller actions ----
    def pdrop_prob_to_group_idx(prob_allow: float) -> int:
        # original code treated ac_prob as allow probability
        idx = int(prob_allow * num_drop_groups)
        return max(0, min(num_drop_groups - 1, idx))

    def internal_choose_group(group: int, ip="10.0.0.1", subnet="255.255.255.0",
                              target_ip=None, force_old=None) -> Dict[str, Any]:
        payload = {
            "ensure_groups": [{
                "group_id": i,
                "type": "INDIRECT",
                # Controller will set PDROP internally; we pass desired probability per group by index
                "actions": [{"type": "PDROP", "group_index": i},
                            {"type": "OUTPUT", "port": "NORMAL"}]
            } for i in range(num_drop_groups)],
            "flow": {
                "priority": 1 if target_ip is None else 2,
                "match": {"eth_type": 0x0800, "ipv4_dst": {"value": ip, "mask": subnet}},
                "actions": [{"type": "GROUP", "group_id": group}]
            }
        }
        if target_ip is not None:
            payload["flow"]["match"]["ipv4_src"] = {"value": target_ip, "mask": "255.255.255.255"}
        if force_old is not None:
            payload["flow"]["actions"] = [
                {"type": "PDROP_LEGACY", "pnum": int(force_old)},
                {"type": "OUTPUT", "port": "NORMAL"},
            ]
        return {"op": "ensure_group_and_flow", "payload": payload}

    def ip_masker_message(new_ip, old_ip, subnet="255.255.255.255"):
        return {"op": "rewrite_src_to_controller", "payload": {
            "priority": 1,
            "match": {"eth_type": 0x0800, "ipv4_src": {"value": old_ip, "mask": subnet}},
            "set_fields": {"ipv4_src": new_ip},
            "output": "CONTROLLER"
        }}

    # messages that used to be raw bytes:
    flow_pdrop_msg = [""]
    flow_upstream_msg = [""]
    flow_upstream_t1_msg = [""]
    flow_gselect_msg = [""]
    flow_outbound_msg = [""]
    flow_outbound_flood = [""]
    flow_arp_upcall = [""]
    flow_miss_next_table = [""]
    flow_group_msgs = [[]]

    def compute_msg(ip="10.0.0.1", subnet="255.255.255.0", out_port=1, away_port=2):
        flow_pdrop_msg[0] = {"op": "flow_write_actions", "payload": {
            "priority": 1,
            "match": {"eth_type": 0x0800, "ipv4_dst": {"value": ip, "mask": subnet}},
            "actions": [{"type": "PDROP_LEGACY", "pnum": 0xffffffff},
                        {"type": "OUTPUT", "port": out_port}]
        }}

        flow_arp_upcall[0] = {"op": "flow_write_actions", "payload": {
            "priority": 1, "match": {"eth_type": 0x0806},
            "actions": [{"type": "OUTPUT", "port": "CONTROLLER"}]
        }}

        flow_miss_next_table[0] = {"op": "goto_table", "payload": {"from": 0, "to": 1}}

        flow_group_msgs[0] = [{"op": "ensure_group", "payload": {
            "group_id": i, "type": "INDIRECT",
            "actions": [{"type": "PDROP", "group_index": i},
                        {"type": "OUTPUT", "port": out_port}]
        }} for i in range(num_drop_groups)]

        flow_gselect_msg[0] = internal_choose_group(0, ip=ip, subnet=subnet)

        flow_outbound_msg[0] = {"op": "flow_write_actions", "payload": {
            "priority": 0, "match": {}, "actions": [{"type": "OUTPUT", "port": away_port}]
        }}
        flow_outbound_flood[0] = {"op": "flow_write_actions", "payload": {
            "priority": 0, "match": {}, "actions": [{"type": "OUTPUT", "port": "FLOOD"}]
        }}

        flow_upstream_msg[0] = {"op": "flow_write_actions", "payload": {
            "priority": 1,
            "match": {"eth_type": 0x0800, "ipv4_dst": {"value": ip, "mask": subnet}},
            "actions": [{"type": "OUTPUT", "port": out_port}]
        }}

        flow_upstream_t1_msg[0] = {"op": "flow_write_actions", "payload": {
            "priority": 0, "table": 1,
            "match": {"eth_type": 0x0800, "ipv4_dst": {"value": ip, "mask": subnet}},
            "actions": [{"type": "OUTPUT", "port": out_port}]
        }}

    compute_msg()

    # ----------------- utilities -----------------
    def pdrop(p: float) -> int:
        return int(p * 0xffffffff)

    def netip(ip, subnet):
        lhs = struct.unpack("I", socket.inet_aton(ip))[0] if isinstance(ip, str) else ip
        rhs = struct.unpack("I", socket.inet_aton(subnet))[0]
        return struct.pack("I", lhs & rhs)

    route_commands = [[]]  # queued (switch, cmd_list, msg_dict)
    def updateOneRoute(switch, cmd_list, msg, needs_check: bool=False):
        # if cmd_list is present, run in switch namespace (ovs-ofctl)
        if cmd_list:
            switch.cmd(*cmd_list)
        elif msg is not None:
            # send JSON to controller, add dpid
            dpid = int(switch.dpid, 16) if isinstance(switch.dpid, str) else switch.dpid
            payload = dict(msg)  # shallow copy
            payload["dpid"] = dpid
            send_ctl(payload)

    def executeRouteQueue():
        for el in route_commands[0]:
            updateOneRoute(*el)
        route_commands[0] = []

    # ----------------- topology helpers -----------------
    initd_host_count = [1]
    initd_switch_count = [1]
    next_ip = [1]

    def newNamedHost(**kw_args):
        o = net.addHost(f"h{initd_host_count[0]}", **kw_args)
        initd_host_count[0] += 1
        return o

    def newNamedSwitch(**kw_args):
        o = net.addSwitch(f"s{initd_switch_count[0]}", listenPort=7000+initd_switch_count[0], **kw_args)
        initd_switch_count[0] += 1
        return o

    def assignIP(node):
        node.setIP(f"10.0.0.{next_ip[0]}", 24)
        next_ip[0] += 1

    def map_link(port_dict, n1, n2):
        def label(node):
            return node.dpid if isinstance(node, Switch) else node.IP()
        def dict_link(s_l, t_l):
            if s_l not in port_dict: port_dict[s_l] = {}
            d = port_dict[s_l]
            if t_l not in d: port_dict[s_l][t_l] = len(d) + 1
        s_label = label(n1); t_label = label(n2)
        dict_link(s_label, t_label); dict_link(t_label, s_label)

    def trackedLink(src, target, extras=None, port_dict=None):
        if extras is None:
            extras = linkopts
        l = net.addLink(src, target, **extras)
        if port_dict is not None:
            map_link(port_dict, src, target)
        return l

    # ----------------- programming switches -----------------
    alive = False

    def prepLearner(switch, out_port=1, ac_prob=0.0):
        if switch.controlled: return
        cmd_list = []
        default_machine = AcTrans()
        ac = default_machine.action()
        if spiffy_but_bad:
            ac = 0.0
        p_drop_num = pdrop(1 - ac)
        local_gselect_msg = [internal_choose_group(0, force_old=p_drop_num)]
        for msg in flow_group_msgs[0] + local_gselect_msg + [flow_outbound_msg[0]]:
            if alive: updateOneRoute(switch, cmd_list, msg)
            else: route_commands[0].append((switch, cmd_list, msg))

    def prepExternal(switch, out_port=1, ac_prob=0.0):
        if switch.controlled: return
        cmd_list = []
        for msg in [flow_arp_upcall[0], flow_upstream_t1_msg[0], flow_outbound_flood[0], flow_miss_next_table[0]]:
            if alive: updateOneRoute(switch, cmd_list, msg)
            else: route_commands[0].append((switch, cmd_list, msg))

    def prepSpiffyBridge(switch, mac_of_interest, out_port=1, ac_prob=0.0):
        if switch.controlled: return
        cmd_list = []
        msgs = [{
            "op": "flow_write_actions",
            "payload": {
                "priority": 1,
                "match": {"eth_type": 0x0806, "arp_op": 2},
                "actions": [{"type": "OUTPUT", "port": out_port}]
            }
        }]
        msgs += [flow_upstream_msg[0], flow_outbound_msg[0]]
        for msg in msgs:
            if alive: updateOneRoute(switch, cmd_list, msg)
            else: route_commands[0].append((switch, cmd_list, msg))

    def updateUpstreamRoute(switch, out_port=1, ac_prob=0.0, target_ip=None):
        if switch.controlled: return
        group_idx = pdrop_prob_to_group_idx(ac_prob)
        # if target_ip is not None:
        #     (src, dst) = target_ip
        #     msg = internal_choose_group(group_idx, ip=dst, target_ip=src, subnet="255.255.255.255")
        # else:
        #     msg = internal_choose_group(group_idx)
        if target_ip is not None:
            (src, dst) = target_ip
            # ensure dotted strings
            src_str = src if isinstance(src, str) else int_to_ip(src)
            dst_str = dst if isinstance(dst, str) else int_to_ip(dst)
            msg = internal_choose_group(group_idx, ip=dst_str, target_ip=src_str, subnet="255.255.255.255")
        else:
            msg = internal_choose_group(group_idx)
        cmd_list = []
        if alive: updateOneRoute(switch, cmd_list, msg)
        else: route_commands[0].append((switch, cmd_list, msg))

    def switch_cmd(switch, cmd_list, msg, needs_check=False):
        if alive: updateOneRoute(switch, cmd_list, msg, needs_check)
        else: route_commands[0].append((switch, cmd_list, msg))

    def routedSwitch(upstreamNode, variant, *args, **kw_args):
        sw = newNamedSwitch(*args)
        if upstreamNode is not None:
            trackedLink(upstreamNode, sw, **kw_args)
        if not use_controller:
            updateUpstreamRoute(sw)
        elif variant == 0:
            sw.controlled = True
        elif variant == 1:
            prepLearner(sw)
            updateUpstreamRoute(sw)
        elif variant == 2:
            prepExternal(sw)
            sw.controlled = True
        return sw

    # ----------------- feature engineering helpers -----------------
    def flow_to_state_vec(flow_set: Dict[str, Any]) -> List[float]:
        return [
            float(flow_set["ip"]),
            float(flow_set["last_act"]),
            flow_set["length"] / 1000000,  # ns → ms
            flow_set["size"],
            flow_set["cx_ratio"],
            flow_set["mean_iat"],
            flow_set["delta_in"],
            flow_set["delta_out"],
            flow_set["pkt_in_count"],
            flow_set["pkt_out_count"],
            flow_set["pkt_in_wnd_count"],
            flow_set["pkt_out_wnd_count"],
            flow_set["mean_bpp_in"],
            flow_set["mean_bpp_out"],
            flow_set["delta_in"],
            flow_set["delta_out"],
        ]

    def combine_flow_vecs(fv1, fv2):
        in_count = float(fv1[10] + fv2[10])
        out_count = float(fv1[11] + fv2[11])
        fv1_in_weight = float(fv1[10])
        fv2_in_weight = float(fv2[10])
        fv1_out_weight = float(fv1[11])
        fv2_out_weight = float(fv2[11])
        return [
            fv2[0], fv2[1], fv2[2], fv2[3], fv2[4], fv2[5],
            max(fv1[6], fv2[6]), max(fv1[7], fv2[7]),
            fv2[8],
            fv2[9] if in_count == 0.0 else (fv1_in_weight * fv1[9] + fv2_in_weight * fv2[9]) / in_count,
            fv1[10] + fv2[10], fv1[11] + fv2[11],
            fv2[12], fv2[13], fv2[14], fv2[15],
            fv2[16] if in_count == 0.0 else (fv1_in_weight * fv1[16] + fv2_in_weight * fv2[16]) / in_count,
            fv2[17] if out_count == 0.0 else (fv1_out_weight * fv1[17] + fv2_out_weight * fv2[17]) / out_count,
            fv2[18], fv2[19],
        ]

    # ----------------- agent params -----------------
    break_equal = (spiffy_mode) if break_equal is None else break_equal
    sarsaParams = {
        "max_bw": max_bw,
        "vec_size": 4,
        "actions": aset,
        "epsilon": epsilon,
        "learn_rate": alpha,
        "discount": discount,
        "break_equal": break_equal,
        "epsilon_falloff": explore_episodes * episode_length,
        "AcTrans": AcTrans,
        "trace_decay": trace_decay,
        "trace_threshold": trace_threshold,
        "broken_math": broken_math,
        "always_include_bias": always_include_bias,
    }

    if actions_target_flows:
        sarsaParams["extended_mins"] = [
            0.0, 0.0, 0.0, 0.0, 0.0,
            0.0, -50.0, -50.0,
            0.0, 0.0,
            0.0, 0.0,
            0.0, 0.0,
            -50.0, -50.0,
        ][:feature_max-4]
        sarsaParams["extended_maxes"] = [
            4294967296.0, 1.0, 2000.0, float(10 * (1024 ** 2)), 1.0,
            10000.0, 50.0, 50.0,
            7000.0, 7000.0,
            2000.0, 2000.0,
            1560.0, 1560.0,
            50.0, 50.0,
        ][:feature_max-4]

        if restrict is not None:
            sarsaParams["vec_size"] = len(restrict)
            for prop_name in ["extended_mins", "extended_maxes"]:
                old = sarsaParams[prop_name]
                sarsaParams[prop_name] = [old[i-4] for i in restrict]
        else:
            sarsaParams["vec_size"] += len(sarsaParams["extended_maxes"])

        if not split_codings:
            sarsaParams["tc_indices"] = [np.arange(sarsaParams["vec_size"])]
        else:
            sarsaParams["tc_indices"] = [np.arange(4)] + [[i] for i in range(4, sarsaParams["vec_size"])]
            if combine_with_last_action is not None:
                for index in combine_with_last_action:
                    f_index = index - 3
                    if f_index < len(sarsaParams["tc_indices"]):
                        sarsaParams["tc_indices"][f_index] += [5]
            if strip_last_action:
                del sarsaParams["tc_indices"][2]
        sarsaParams["tc_indices"] += extra_codings

    # ----------------- more helpers -----------------
    def moralise(value, good, max_val=255, no_goods=[0, 255]):
        target_mod = 0 if good else 1
        god_mod = max_val + 1
        if moralise_ips and (value % 2) != target_mod:
            value = (value + 1) % god_mod
        if value in no_goods:
            value = (value + 2) % god_mod
        return value

    def genIP(good):
        lims = [0xdf, 0xff, 0xff, 0xff]
        ip_bytes = [random.randint(0, lim) for lim in lims]
        while ip_bytes[0] == 10 or ip_bytes[0] == 0:
            ip_bytes[0] = random.randint(1, lims[0])
        ip_bytes[-1] = moralise(ip_bytes[-1], good)
        ip = "{}.{}.{}.{}".format(*ip_bytes)
        return (ip, ip_bytes)

    def random_target(dests):
        target_dest = dests[0] if len(dests) == 1 else dests[random.randint(0, len(dests)-1)]
        return target_dest[0][0]

    def th_cmd(dests, bw, target_ip=None):
        if target_ip is None:
            target_ip = random_target(dests)
        return [
            "../traffic-host/target/release/traffic-host",
            str(bw),
        ] + (
            ["-s", "http://{}/gcc-8.2.0.tar.gz".format(target_ip)] if not randomise else
            ["-r", "-l", "../traffic-host/htdoc-deps.ron", "-s", "http://{}/".format(target_ip)]
        ) + ([] if randomise_count is None else ["-c", str(randomise_count)])

    total_thing = [0.0]
    def opus_cmd(dests, bw, host, target_ip=None):
        ip_list = [d[0][0] for d in dests]
        divisor = 1.0
        if rescale_opus:
            ub = host_range[1]
            pt = float(max(0, ub - 2)) / 14.0
            divisor = 0.6 + pt * (0.45 - 0.6)
            print("rescaled to", divisor)
        flow_bw = 52.39456 / (divisor * 1024.0)
        subclient_count = int(math.ceil(max(1.0, bw / flow_bw)))
        total_thing[0] += flow_bw * subclient_count
        print(subclient_count, total_thing)
        return [
            "../opus-voip-traffic/target/release/opus-voip-traffic",
            "-i", ",".join(ip_list),
            "-m", "5000",
            "-c", str(subclient_count),
            "-b", "../opus-voip-traffic",
            "--ip-strategy", "even",
            "--constant",
            "--refresh",
        ]

    # -------------------- topology builders (TREE/ECMP) --------------------
    def addHosts(extern, extern_no, hosts_per_learner, hosts_upper):
        scaler = 1 if topol == "tree" else (n_teams * n_inters * n_learners) / ((ecmp_k/2)**2)
        host_count = (hosts_per_learner if hosts_per_learner == hosts_upper else random.randint(hosts_per_learner, hosts_upper))
        host_count *= scaler
        hosts = []
        for _ in range(host_count):
            good = random.random() < P_good
            bw = (random.uniform(*(good_range if good else evil_range)))
            (ip, ip_bytes) = genIP(good)
            print(f"drew: good={good}, bw={bw}, ip={ip}")
            new_host = newNamedHost(ip="{}.{}.{}.{}/24".format(*ip_bytes))
            link = trackedLink(extern, new_host, {"bw": bw} if (old_style or force_host_tc) else {})
            new_host.setIP(ip, 24)

            if mix_model is None:
                sm = submodel
            else:
                draw = random.random()
                total = 0.0
                i = 0
                found_sm = False
                while (not found_sm) and i < len(mix_model):
                    (p, m) = mix_model[i]
                    total += p
                    if draw < total:
                        sm = m["submodel"]
                        found_sm = True
                    i += 1

            hosts.append((new_host, good, bw, link, ip, extern_no, sm))
        return hosts

    def makeTeam(parent, inter_count, learners_per_inter, new_topol_shape, sarsas=[], graph=None, ss_label=None, port_dict=None):
        (monitored_links, dests, dest_links, core_links, actors, externals, vertex_map, link_map, spiffy_dest_switches) = new_topol_shape

        def link_in_new_topol(node1, node2, node1_label, node2_label, critical=False):
            link_name = "{}{}-eth1".format("!" if critical else "", node2.name)
            index = len(monitored_links)
            vertex_map[node1_label] = node1
            vertex_map[node2_label] = node2
            monitored_links.append(link_name)
            link_map[(node1_label, node2_label)] = index
            link_map[(node2_label, node1_label)] = index

        leader = routedSwitch(parent, 0, port_dict=port_dict)

        def add_to_graph(parent, new_child, label=None):
            if label is None:
                label = (None, new_child.dpid)
            if graph is not None and parent is not None:
                graph.add_edge(parent, label)
            return label

        def link_to_outside(parent):
            return add_to_graph(parent, None, label=(("0.0.0.0", None), None))

        leader_label = add_to_graph(ss_label, leader)
        link_in_new_topol(parent, leader, ss_label, leader_label)
        intermediates, learners, extern_switches, hosts = [], [], [], []

        newSarsas = len(sarsas) == 0
        for i in range(inter_count):
            new_interm = routedSwitch(leader, 0, port_dict=port_dict)
            inter_label = add_to_graph(leader_label, new_interm)
            link_in_new_topol(leader, new_interm, leader_label, inter_label)
            for j in range(learners_per_inter):
                new_learn = routedSwitch(new_interm, 1, port_dict=port_dict)
                nll = add_to_graph(inter_label, new_learn)
                link_in_new_topol(new_interm, new_learn, inter_label, nll, critical=True)
                _ = link_to_outside(nll)

                local_sarsa = AgentClass(**sarsaParams) if newSarsas else sarsas[(i * inter_count) + j]
                learners.append(new_learn)
                actors.append((nll, local_sarsa, (ss_label, leader_label)))

                new_extern = routedSwitch(new_learn, 2)
                extern_switches.append(new_extern)
                externals.append(new_extern)

        new_topol_shape = (monitored_links, dests, dest_links, core_links, actors, externals, vertex_map, link_map, spiffy_dest_switches)
        return ((leader, intermediates, learners, extern_switches, hosts, sarsas if not newSarsas else [a[1] for a in actors][-learners_per_inter*inter_count:]), new_topol_shape)

    def makeHosts(hosts, externs, hosts_per_learner, hosts_upper=None):
        if hosts_upper is None: hosts_upper = hosts_per_learner
        for (host, _, _, link, _, _, _) in hosts:
            host.stop()
        new_hosts = []
        for i, extern in enumerate(externs):
            new_hosts += addHosts(extern, i, hosts_per_learner, hosts_upper)
        return new_hosts

    def controlSwitch(switch, msgs, cmd_list=[]):
        for msg in msgs:
            if alive: updateOneRoute(switch, cmd_list, msg)
            else: route_commands[0].append((switch, cmd_list, msg))

    def isolateSpiffyFlow(spiffy_dest_switches, flow_ip, dest_ip, isolate_port=3):
        (close_switch, far_switch) = spiffy_dest_switches[dest_ip]
        subnet = "255.255.255.255"
        controlSwitch(close_switch, [{"op": "flow_write_actions", "payload": {
            "priority": 2,
            "match": {"eth_type": 0x0800, "ipv4_dst": {"value": flow_ip, "mask": subnet}},
            "actions": [{"type": "OUTPUT", "port": isolate_port}]
        }}])
        controlSwitch(far_switch, [{"op": "flow_write_actions", "payload": {
            "priority": 2,
            "match": {"eth_type": 0x0800, "ipv4_src": {"value": flow_ip, "mask": subnet}},
            "actions": [{"type": "OUTPUT", "port": isolate_port}]
        }}])

    def okaySpiffyFlow(spiffy_dest_switches, flow_ip, dest_ip):
        (close_switch, far_switch) = spiffy_dest_switches[dest_ip]
        subnet = "255.255.255.255"
        controlSwitch(close_switch, [{"op": "flow_delete", "payload": {
            "priority": 2,
            "match": {"eth_type": 0x0800, "ipv4_dst": {"value": flow_ip, "mask": subnet}},
        }}])
        controlSwitch(far_switch, [{"op": "flow_delete", "payload": {
            "priority": 2,
            "match": {"eth_type": 0x0800, "ipv4_src": {"value": flow_ip, "mask": subnet}},
        }}])

    def blockSpiffyFlow(spiffy_dest_switches, flow_ip, dest_ip, ingress_switch=None):
        (close_switch, far_switch) = spiffy_dest_switches[dest_ip]
        subnet = "255.255.255.255"
        block = [{"op": "flow_clear_actions", "payload": {
            "priority": 2, "match": {"eth_type": 0x0800, "ipv4_src": {"value": flow_ip, "mask": subnet}}
        }}, {"op": "flow_clear_actions", "payload": {
            "priority": 2, "match": {"eth_type": 0x0800, "ipv4_dst": {"value": flow_ip, "mask": subnet}}
        }}]
        controlSwitch(close_switch, block)
        controlSwitch(far_switch, block)
        if ingress_switch is not None:
            updateUpstreamRoute(ingress_switch, ac_prob=1.0, target_ip=(flow_ip, dest_ip))

    # ------------------ build TREE ------------------
    def buildTreeNet(n_teams, team_sarsas=[]):
        monitored_links, dests, dest_links, core_links = [], [], {}, []
        actors, externals, vertex_map, link_map = [], [], {}, {}
        spiffy_dest_switches = {}

        server = newNamedHost()
        server_switch = newNamedSwitch()
        server_switch.controlled = use_controller
        port_dict = {}

        if spiffy_but_bad:
            close_switch = newNamedSwitch()
            far_switch = newNamedSwitch()
            close_switch.controlled = False
            far_switch.controlled = False
            last_hop = trackedLink(server, close_switch, extras=linkopts_core)
            normal_link = trackedLink(close_switch, far_switch, extras=linkopts_core)
            core_link = trackedLink(far_switch, server_switch, extras=linkopts_core)
            tbe_link = trackedLink(close_switch, far_switch, extras=linkopts_core)
            core_links.append(normal_link); core_links.append(tbe_link)
            mac_of_interest = server.MAC()
            prepSpiffyBridge(close_switch, mac_of_interest)
            prepSpiffyBridge(far_switch, mac_of_interest)
            link_name = "{}-eth1".format(close_switch.name)
        else:
            core_link = trackedLink(server, server_switch, extras=linkopts_core)
            core_links.append(core_link)
            link_name = "{}-eth1".format(server_switch.name)
        monitored_links.append(link_name)

        updateUpstreamRoute(server_switch)
        assignIP(server)
        map_link(port_dict, server, server_switch)

        if spiffy_but_bad:
            spiffy_dest_switches[server.IP()] = (close_switch, far_switch)
        make_sarsas = len(team_sarsas) == 0

        G = nx.Graph()
        server_label = ((server.IP(), server.MAC()), None)
        switch_label = (None, server_switch.dpid)
        G.add_edge(server_label, switch_label)

        dests.append(server_label)
        vertex_map[server_label] = server
        vertex_map[switch_label] = server_switch
        link_map[(server_label, switch_label)] = len(monitored_links)-1
        link_map[(switch_label, server_label)] = len(monitored_links)-1
        dest_links[server_label] = [(server_label, switch_label)]

        new_topol_shape = (monitored_links, dests, dest_links, core_links, actors, externals, vertex_map, link_map, spiffy_dest_switches)

        teams = []
        for i in range(n_teams):
            (t, n_s) = makeTeam(server_switch, n_inters, n_learners, new_topol_shape,
                                 sarsas=[] if make_sarsas else team_sarsas[i],
                                 graph=G, ss_label=switch_label, port_dict=port_dict)
            teams.append(t)
            if make_sarsas: team_sarsas.append(t[-1])
            new_topol_shape = n_s
        print(monitored_links)
        return (server, server_switch, core_link, teams, team_sarsas, G, port_dict, new_topol_shape)

    # ------------------ build ECMP ------------------
    def buildEcmpNet(n_teams, team_sarsas=[]):
        monitored_links, dests, dest_links, core_links = [], [], {}, []
        actors, externals, vertex_map, link_map = [], [], {}, {}
        spiffy_dest_switches = {}
        make_sarsas = True
        G = nx.Graph()
        port_dict = {}

        servers = [newNamedHost() for _ in range(ecmp_servers)]

        def link_in_new_topol(node1, node2, node1_label, node2_label, critical=False, override_link_name=None):
            link_name = "{}{}-eth{}".format("!" if critical else "", node2.name, len(node2.ports)-1) if override_link_name is None else override_link_name
            index = len(monitored_links)
            vertex_map[node1_label] = node1
            vertex_map[node2_label] = node2
            monitored_links.append(link_name)
            link_map[(node1_label, node2_label)] = index
            link_map[(node2_label, node1_label)] = index

        def add_to_graph(parent, new_child, label=None):
            if label is None:
                label = (None, new_child.dpid)
            G.add_edge(parent, label)
            return label

        def link_to_outside(parent):
            return add_to_graph(parent, None, label=(("0.0.0.0", None), None))

        max_hosts_per_pod = (ecmp_k**2) / 4
        e_k_2 = ecmp_k/2
        pods_required = int(math.ceil(float(ecmp_servers) / float(max_hosts_per_pod)))
        edge_nodes = []
        for i in range(pods_required * e_k_2):
            edge_switch = routedSwitch(None, 0, port_dict=port_dict)
            edge_nodes.append(edge_switch)
            for dest in servers[i*e_k_2:min(len(servers), (i+1)*e_k_2)]:
                override_name = None
                if spiffy_but_bad:
                    close_switch = newNamedSwitch()
                    far_switch = newNamedSwitch()
                    close_switch.controlled = False; far_switch.controlled = False
                    last_hop = trackedLink(dest, close_switch, extras=linkopts_core)
                    normal_link = trackedLink(close_switch, far_switch, extras=linkopts_core)
                    core_link = trackedLink(far_switch, edge_switch, extras=linkopts_core)
                    tbe_link = trackedLink(close_switch, far_switch, extras=linkopts_core)
                    core_links.append(normal_link); core_links.append(tbe_link)
                else:
                    core_link = trackedLink(dest, edge_switch, extras=linkopts_core)
                    core_links.append(core_link)

                assignIP(dest)
                server_label = ((dest.IP(), dest.MAC()), None)
                vertex_map[server_label] = dest
                dests.append(server_label)
                map_link(port_dict, dest, edge_switch)
                switch_label = add_to_graph(server_label, edge_switch)

                if spiffy_but_bad:
                    mac_of_interest = dest.MAC()
                    prepSpiffyBridge(close_switch, mac_of_interest)
                    prepSpiffyBridge(far_switch, mac_of_interest)
                    spiffy_dest_switches[dest.IP()] = (close_switch, far_switch)
                    override_name = "{}-eth1".format(close_switch.name)

                link_in_new_topol(dest, edge_switch, server_label, switch_label, override_link_name=override_name)
                dest_links[server_label] = [(server_label, switch_label)]

        def normal_link(n1, n2, n1_l, critical=False, link=True, **kw_args):
            if link: trackedLink(n1, n2, **kw_args)
            n2_l = add_to_graph(n1_l, n2)
            map_link(port_dict, n1, n2)
            link_in_new_topol(n1, n2, n1_l, n2_l, critical)
            return n2_l

        agg_nodes = []
        agg_nodes_by_pod = []
        for i in range(pods_required * e_k_2):
            agg_switch = routedSwitch(None, 0, port_dict=port_dict)
            group_index = (i % e_k_2)
            start = i - group_index
            for child in edge_nodes[start:start+e_k_2]:
                normal_link(agg_switch, child, (None, agg_switch.dpid))
            agg_nodes.append(agg_switch)
            if group_index == 0:
                agg_nodes_by_pod.append([])
            agg_nodes_by_pod[-1].append(agg_switch)

        core_nodes = []
        for i in range(e_k_2*e_k_2):
            core_switch = routedSwitch(None, 0, port_dict=port_dict)
            core_nodes.append(core_switch)
            new_learn = routedSwitch(core_switch, 1, port_dict=port_dict)
            nll = normal_link(core_switch, new_learn, (None, core_switch.dpid), critical=True, link=False)
            _ = add_to_graph(nll, None, label=(("0.0.0.0", None), None))
            local_sarsa = AgentClass(**sarsaParams) if make_sarsas else team_sarsas[i]
            actors.append((nll, local_sarsa, None))
            new_extern = routedSwitch(new_learn, 2)
            externals.append(new_extern)

        for pod in agg_nodes_by_pod:
            for i, node in enumerate(pod):
                targets = core_nodes[i*e_k_2:(i+1)*e_k_2]
                for core in targets:
                    normal_link(node, core, (None, node.dpid))

        new_topol_shape = (monitored_links, dests, dest_links, core_links, actors, externals, vertex_map, link_map, spiffy_dest_switches)
        return (None, edge_nodes[0], None, None, None, G, port_dict, new_topol_shape)

    # ----------------- pick topology builder -----------------
    if topol == "tree":
        buildNet = buildTreeNet
    elif topol == "ecmp":
        buildNet = buildEcmpNet
    else:
        raise ValueError("Unknown topology: {}".format(topol))

    # ----------------- main episode loop -----------------
    net = None
    interrupted = [False]

    def sigint_handle(signum, frame):
        print("Interrupted, cleaning up.")
        interrupted[0] = True

    signal.signal(signal.SIGINT, sigint_handle)
    old_hosts = []

    for ep in range(episodes):

        last_iter_snapshot = {"good": 0.0, "g_reward": 0.0, "selected": 0.0}
        # --- NEW: per-episode confusion counts (we'll evaluate using the last prediction per host) ---
        ep_counts = {"tp": 0, "tn": 0, "fp": 0, "fn": 0}
        # --- NEW: per-episode logs & metrics ---
        os.makedirs(log_dir, exist_ok=True)
        action_fp = None
        actions_written = 0
        if log_actions and actions_target_flows:
            action_fp = open(os.path.join(log_dir, f"actions_ep{ep:04d}.csv"), "w", buffering=1)
            action_fp.write("step,learner,src_ip,dst_ip,allow_prob,pred_bad,truth_bad\n")

        # Ground-truth maps (filled after hosts are created)
        truth_map = {}         # ip_int -> is_good (bool)
        truth_bad_map = {}     # ip_int -> is_bad (bool)
        last_pred_bad = {}     # ip_int -> last predicted bad (bool) within this episode


        Cleanup.cleanup()
        if interrupted[0]: break

        initd_switch_count[:] = [1]
        initd_host_count[:]   = [1]
        alive = False
        if separate_episodes:
            store_sarsas = []

        print("beginning episode {} of {}".format(ep+1, episodes))
        net = Mininet(link=TCLink, switch=MaybeControlledSwitch)

        (server, server_switch, core_link, teams, team_sarsas, graph, port_dict, new_topol_shape) = buildNet(n_teams, team_sarsas=store_sarsas)
        (monitored_links, dests, dest_links, core_links, actors, externals, vertex_map, link_map, spiffy_dest_switches) = new_topol_shape

        dest_from_ip = {dest[0][0]: dest for dest in dests}

        def resolve_dest_label(dst_ip_any):
            # Accept int or str; normalize to dotted-quad string and map to dest label
            if isinstance(dst_ip_any, (int, np.integer)):
                try:
                    dst_ip_s = socket.inet_ntoa(struct.pack("!I", int(dst_ip_any)))
                except Exception:
                    return dests[0]  # fallback to first dest
            elif isinstance(dst_ip_any, str):
                dst_ip_s = dst_ip_any
            else:
                return dests[0]
            return dest_from_ip.get(dst_ip_s, dests[0])

        dest_map = {}
        for (node, _sarsa, _leader) in actors:
            for dest in dests:
                paths = nx.all_shortest_paths(graph, node, dest)
                for path in paths:
                    for (n1, n2) in zip(path, path[1:]):
                        if n1 not in dest_map: dest_map[n1] = {}
                        if dest not in dest_map[n1]: dest_map[n1][dest] = set()
                        dest_map[n1][dest].add(n2)

        learner_pos = {}
        learner_name = []
        for i, (actor, _sarsa, _leader) in enumerate(actors):
            name = vertex_map[actor].name
            learner_pos[name] = len(learner_name)
            learner_name.append(name)
        # Build interface→agent map using the raw monitored_links (with '!' on learner links)
        if_to_agent = []
        for name in monitored_links:
            is_learner_link = name.startswith('!')
            base = name.lstrip('!')
            swname = base.split('-')[0]  # e.g., 's4' from 's4-eth1'
            if is_learner_link and swname in learner_pos:
                if_to_agent.append(learner_pos[swname])
            else:
                if_to_agent.append(None)

        mon_reporter = MonitorReporter(
            log_dir=log_dir,
            n_ifs=len(monitored_links),
            n_agents=len(learner_pos),
            episode=ep,
            log_every=1,     # log every step; bump to 5/10 if too chatty
        )

        ctl_proc = None
        if use_controller:
            # ctl_proc = Popen(["ryu-manager", "controller_py3.py"], stdin=PIPE, stderr=sys.stderr)
            py = sys.executable
            env = os.environ.copy()
            # keep child isolated from system site-packages
            env.pop("PYTHONPATH", None)
            env.pop("PYTHONHOME", None)
            env["PYTHONNOUSERSITE"] = "1"

            ctl_proc = Popen(
                [py, "-u", os.path.join(os.path.dirname(__file__), "ryu_bootstrap.py"), "controller_py3.py"],
                stdin=PIPE,
                stderr=sys.stderr,
                env=env,
            )
            # tee_process_stdout(ctl_proc, "[ctl] ") # commented out because the controller program says nothing
            
            time.sleep(1.0)  # small grace for OVS to connect


            apsp = dict(nx.all_pairs_shortest_path(graph))

            ips = []
            dpids = []
            inner_host_macs = {}
            for node in graph.nodes():
                (maybe_ip, maybe_dpid) = node
                if maybe_ip is not None:
                    (ip, mac) = maybe_ip
                    if mac is not None:
                        ips.append(node)
                        inner_host_macs[ip] = mac
                if maybe_dpid is not None:
                    dpids.append(node)

            def hard_label(node):
                (left, right) = node
                return left[0] if right is None else right

            entry_map = {}
            escape_map = {}
            port_dest_map = {}

            for dnode in dpids:
                (_, dpid) = dnode
                entry_map[dpid] = {}
                escape_map[dpid] = set()
                for inode in ips:
                    ((ip, _), _) = inode
                    path = apsp[dnode][inode]
                    port = port_dict[dpid][hard_label(path[1])]
                    entry_map[dpid][ip] = (port, len(path) == 2)
                escape_paths = nx.all_shortest_paths(graph, dnode, (("0.0.0.0", None), None))
                for path in escape_paths:
                    target = hard_label(path[1])
                    if target == "0.0.0.0": continue
                    port = port_dict[dpid][target]
                    escape_map[dpid].add(port)
                if dnode in dest_map:
                    port_dest_map[dpid] = {}
                    for (dest, next_nodes) in dest_map[dnode].items():
                        target = hard_label(dest)
                        port_dest_map[dpid][target] = set()
                        for next_node in next_nodes:
                            next_l = hard_label(next_node)
                            port = port_dict[dpid][next_l]
                            port_dest_map[dpid][target].add((port, target == next_l))

            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                try:
                    socket.SO_REUSEPORT
                except AttributeError:
                    pass
                else:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(("127.0.0.1", controller_build_port))
                sock.listen(1)
                data_sock = sock.accept()[0]
                try:
                    pickle_str = pickle.dumps((entry_map, escape_map, inner_host_macs, prevent_smart_switch_recording, port_dest_map))
                    data_sock.sendall(struct.pack("!Q", len(pickle_str)))
                    data_sock.sendall(pickle_str)
                finally:
                    data_sock.close()
            finally:
                sock.close()
        # End of use_controller block

        host_procs = []
        bw_all = [0.0 for _ in range(3)]
        bw_teams = {}

        all_hosts = makeHosts(old_hosts, externals, *host_range)
        # --- NEW: build ground-truth from all_hosts ---
        truth_map.clear(); truth_bad_map.clear()
        for (host, good, bw, _, ip, _, _) in all_hosts:
            ip_int = struct.unpack("!I", socket.inet_aton(ip))[0]
            truth_map[ip_int] = bool(good)
            truth_bad_map[ip_int] = (not good)

        # Persist the episode's host truth (human readable)
        with open(os.path.join(log_dir, f"hosts_ep{ep:04d}.json"), "w") as f:
            json.dump(
                { socket.inet_ntoa(struct.pack("!I", ip)): {"good": truth_map[ip], "bad": truth_bad_map[ip]}
                for ip in truth_map.keys() },
                f, indent=2
            )

        old_hosts = all_hosts

        host_ip_mac_map = {}
        flows_to_query = set()
        for (host, good, bw, _, _, extern_no, sm) in all_hosts:
            lhs = ip_to_int(host.IP())
            host_ip_mac_map[lhs] = host.MAC()
            flows_to_query.add(lhs)
            (_a_node, _a_sarsa, a_leader) = actors[extern_no]
            bw_team = None
            if a_leader is not None:
                if a_leader not in bw_teams:
                    bw_teams[a_leader] = [0.0 for _ in range(3)]
                bw_team = bw_teams[a_leader]
            if good:
                if bw_team is not None: bw_team[0] += bw
                bw_all[0] += bw
            else:
                if bw_team is not None: bw_team[1] += bw
                bw_all[1] += bw
            if bw_team is not None: bw_team[2] += bw
            bw_all[2] += bw

        for (_node, sarsa, _link_pts) in actors:
            sarsa.bootstrap(sarsa.to_state(np.zeros(sarsaParams["vec_size"])))

        capacity = calc_max_capacity(len(all_hosts)) / float(len(dests))
        print(capacity, bw_all)
        if protect_final_hop:
            for core_link in core_links:
                core_link.intf1.config(bw=float(capacity))
                core_link.intf2.config(bw=float(capacity))

        rewards.append([]); good_traffic_percents.append([]); total_loads.append([]); action_comps.append([])

        net.build(); net.start()

        for (h, i) in [(host, ip) for (host, _, _, _, ip, _, _) in all_hosts] + [(vertex_map[d], d[0][0]) for d in dests]:
            h.setIP(i, 24)
            h.setDefaultRoute(h.intf())

        alive = True
        executeRouteQueue()
        # --- ADDED: optional pre-run Mininet CLI ---
        if interactive_cli_pre:
            # print("[INFO] Pre-run Mininet CLI. Type 'exit' to start the experiment.")
            LOG.warning("[INFO] Pre-run Mininet CLI. Type 'exit' to start the experiment.")
            CLI(net)

        sanitized_links = [n.lstrip('!') for n in monitored_links]
        print("monitored_links raw:", monitored_links)
        print("monitored_links sanitized:", sanitized_links)

        if bw_mon_socketed:
            bwmon_command = ["../marl-bwmon/marl-bwmon", "-s"] + sanitized_links
            LOG.info("starting bwmon as: %s", " ".join(bwmon_command))
            mon_cmd = server_switch.popen(bwmon_command, stdin=PIPE, stderr=sys.stderr)
            tee_process_stdout(mon_cmd, "[bwmon] ")
            time.sleep(0.5)

            # Persistent bwmon socket (mutated in inner scope)
            bw_sock = [None]

            def _bw_open():
                # (Re)open the persistent socket to bwmon
                try:
                    if bw_sock[0] is not None:
                        try:
                            bw_sock[0].shutdown(socket.SHUT_RDWR)
                        except Exception:
                            pass
                        bw_sock[0].close()
                except Exception:
                    pass
                bw_sock[0] = _connect_unix(BW_SOCK_PATH)

            # initial connect
            _bw_open()

            def ask_stats(flows_be, n_ifs, n_agents,
                        hdr_deadline: float = 5.0,
                        agent_deadline: float = 5.0):

                flows_be = list(flows_be)

                def _one_round(send_socket):
                    LOG.debug("[bwmon] ask_stats: query_size=%d", len(flows_be))
                    if flows_be:
                        LOG.debug("[bwmon] flows: %s", [_ip_be_to_str(x) for x in flows_be])

                    # -------------------------
                    # 1) REQUEST (wire = network/big-endian)
                    # count: !I, each ip: !I
                    send_socket.sendall(_SZ_U32_BE.pack(len(flows_be)))
                    if flows_be:
                        send_socket.sendall(b"".join(_IP_U32_BE.pack(x & 0xffffffff) for x in flows_be))

                    # -------------------------
                    # 2) REPLY HEADER (native)
                    # time_ns: =q, then 4*n_ifs * (=Q)
                    t_bytes = _read_exact(send_socket, _TIME_I64_NATIVE.size, hdr_deadline)
                    (time_ns,) = _TIME_I64_NATIVE.unpack(t_bytes)
                    time_ns = max(1, int(time_ns))

                    cnt_bytes = _read_exact(send_socket, 4 * n_ifs * _U64_NATIVE.size, hdr_deadline)
                    vals = list(struct.unpack(f"={4*n_ifs}Q", cnt_bytes))
                    goods = vals[:2*n_ifs]
                    bads  = vals[2*n_ifs:]

                    def mbps(u64bytes): return 8000.0 * float(u64bytes) / float(time_ns)
                    unfused_load_mbps = [(mbps(goods[j]), mbps(bads[j])) for j in range(2*n_ifs)]

                    # -------------------------
                    # 3) PER-INTERFACE FLOW BLOCKS
                    # For each interface:
                    #   n_entries: !I (network)
                    #   records: n_entries * 88B
                    #   FlowMeasurement head (80B): =q6Q6f (native)
                    #   FlowMeasurement.ip (4B):   =I     (native)   <-- key line
                    per_agent = [[] for _ in range(n_agents)]
                    for if_idx in range(n_ifs):
                        raw = _read_exact(send_socket, 4, agent_deadline)
                        (n_entries_be,) = struct.unpack("!I", raw)
                        n_entries = n_entries_be
                        if n_entries > 100000:  # sanity fallback if we ever see nonsense
                            (n_entries_native,) = struct.unpack("=I", raw)
                            if n_entries_native <= 100000:
                                LOG.warning("[bwmon] if %d: BE count %d insane; using native %d",
                                            if_idx, n_entries_be, n_entries_native)
                                n_entries = n_entries_native

                        flows_here = []
                        for _ in range(n_entries):
                            rec  = _read_exact(send_socket, _FM_SIZE, agent_deadline)
                            head = _FM_HEAD_NATIVE.unpack(rec[:_FM_HEAD_NATIVE.size])
                            # ip field: next 4 bytes; big-endian (network order) from C++
                            (ip_be,) = struct.unpack("!I", rec[_FM_HEAD_NATIVE.size:_FM_HEAD_NATIVE.size+4])
                            if ip_be == 0:
                                continue

                            fl_len = head[0]
                            size_in, size_out, d_in, d_out = head[1], head[2], head[3], head[4]
                            pkt_in_cnt, pkt_out_cnt        = head[5], head[6]
                            pin_mean, pin_var              = head[7], head[8]
                            pout_mean, pout_var            = head[9], head[10]
                            iat_mean, iat_var              = head[11], head[12]
                            props = (
                                fl_len, size_in, size_out, d_in, d_out,
                                pin_mean, pin_var, pkt_in_cnt,
                                pout_mean, pout_var, pkt_out_cnt,
                                iat_mean, iat_var
                            )
                            flows_here.append((ip_be, props))

                        agent_idx = if_to_agent[if_idx]
                        if agent_idx is not None and 0 <= agent_idx < n_agents:
                            per_agent[agent_idx].extend(flows_here)
                        # else: non-learner interface; ignore its flows

                    return (time_ns, unfused_load_mbps, per_agent)

                try:
                    return _one_round(bw_sock[0])
                except (TimeoutError, BrokenPipeError, ConnectionError, OSError) as e:
                    LOG.warning("[bwmon] socket error (%s); reconnecting once...", e)
                    _bw_open()
                    return _one_round(bw_sock[0])


        else:
            bwmon_command = ["../marl-bwmon/marl-bwmon"] + sanitized_links
            # print("starting bwmon as:", " ".join(bwmon_command))
            LOG.info("starting bwmon as: %s", " ".join(bwmon_command))
            mon_cmd = server_switch.popen(
                bwmon_command,
                stdin=PIPE, stdout=PIPE, stderr=sys.stderr
            )
            tee_process_stdout(mon_cmd, "[bwmon] ")

            def ask_stats(_flows, n_ifs, n_agents):
                # Trigger one snapshot
                mon_cmd.stdin.write(b"\n")
                mon_cmd.stdin.flush()

                # First line: e.g.
                # 862420385ns, 18216 12804, 817512 574628, ...
                line = mon_cmd.stdout.readline().decode(errors="replace").strip()
                if not line:
                    time_ns = 1
                    unfused_load_mbps = [(0.0, 0.0) for _ in range(n_ifs * 2)]
                    return (time_ns, unfused_load_mbps, [[] for _ in range(n_agents)])

                parts = [p.strip() for p in line.split(",") if p.strip()]
                ttok = parts[0]
                if ttok.endswith("ns"):
                    ttok = ttok[:-2]
                try:
                    time_ns = int(ttok)
                except ValueError:
                    time_ns = 1

                # Then we expect 2*n_ifs pairs: (IN good bad), (OUT good bad), ...
                pairs = []
                for p in parts[1:]:
                    toks = [t for t in p.split() if t]
                    if len(toks) >= 2:
                        try:
                            a = int(toks[0]); b = int(toks[1])
                        except ValueError:
                            a = b = 0
                        pairs.append((a, b))

                # pad if short
                while len(pairs) < 2 * n_ifs:
                    pairs.append((0, 0))

                def mbpsify(bts):  # bytes over window (ns) -> Mb/s
                    return 8000.0 * float(bts) / max(1, time_ns)

                unfused_load_mbps = [(mbpsify(g), mbpsify(b)) for (g, b) in pairs[: 2 * n_ifs]]

                # No per-agent flow telemetry available in stdout mode
                parsed_flows = [[] for _ in range(n_agents)]

                return (time_ns, unfused_load_mbps, parsed_flows)


        server_procs = []
        for i, dest_node in enumerate(dests):
            dest = vertex_map[dest_node]
            if model == "nginx":
                cmds = []
                if mix_model is None:
                    sms = [submodel]
                else:
                    sms = [m["submodel"] for (p, m) in mix_model]
            for sm in sms:
                if sm in (None, "http"):
                    fname = f"temp{i}.conf"
                    with open("../traffic-host/" + fname, "w") as f:
                        f.write(
                            "events { worker_connections 1024; }\n"
                            "http {\n"
                            f"\tserver {{\n\t\tinclude global.conf;\n\t\tlisten {dest_node[0][0]}:80;\n\t}}\n"
                            "}\n"
                        )
                    cmd = ["nginx", "-p", "../traffic-host", "-c", fname]

                elif sm == "opus-voip":   # <<< was `submodel == "opus-voip"`
                    cmd = ["../opus-voip-traffic/target/release/opus-voip-traffic", "--server"]

                else:
                    # default to nginx/http if unknown submodel to avoid UnboundLocalError
                    fname = f"temp{i}.conf"
                    with open("../traffic-host/" + fname, "w") as f:
                        f.write(
                            "events { worker_connections 1024; }\n"
                            "http {\n"
                            f"\tserver {{\n\t\tinclude global.conf;\n\t\tlisten {dest_node[0][0]}:80;\n\t}}\n"
                            "}\n"
                        )
                    cmd = ["nginx", "-p", "../traffic-host", "-c", fname]

                cmds.append(cmd)
                for cmd in cmds:
                    server_procs.append(dest.popen(cmd, stdin=PIPE, stderr=sys.stderr))
                    tee_process_stdout(server_procs[-1], f"[srv{dest_node[0][0]}] ")

        # End of BWMON setup

        for (host, good, bw, link, ip, extern_no, sm) in all_hosts:
            target_dest = dests[0] if len(dests) == 1 else dests[random.randint(0, len(dests)-1)]
            target_ip = target_dest[0][0]
            if model == "tcpreplay":
                cmd = [
                    "tcpreplay-edit",
                    "-i", host.intfNames()[0],
                    "-l", "0",
                    "-S", "0.0.0.0/0:{}/32".format(ip),
                    "-D", "0.0.0.0/0:{}/32".format(target_ip)
                ] + ([] if old_style else ["-M", str(bw)]) + [(good_file if good else bad_file)]
            elif model == "nginx":
                if sm == "http" or (sm is None and good):
                    traffic_host_bin = "../traffic-host/target/release/traffic-host"
                    if os.path.exists(traffic_host_bin):
                        cmd = th_cmd(dests, bw, target_ip=target_ip)
                    else:
                        # Fallback: simple wget loop
                        cmd = ["bash", "-lc",
                            f"while true; do wget -q -O /dev/null http://{target_ip}/big.bin || true; sleep 0.2; done"]
                elif sm == "opus-voip" and good:
                    cmd = opus_cmd(dests, bw, host, target_ip=target_ip)
                elif sm == "udp-flood" or ((sm is None or sm == "opus-voip") and not good):
                    udp_h_size = 28.0
                    bw_MB = (bw / 8.0) * (10.0**6.0)
                    target = 1500.0
                    s = target - udp_h_size
                    interval_s = target/bw_MB
                    interval_us = int(interval_s * (10.0 ** 6.0))
                    cmd = ["hping3", "--udp", "-i", f"u{interval_us}", "-d", str(int(s)), "-I", "h", target_ip]
            else:
                cmd = []

            if len(cmd) > 0:
                env_new = os.environ.copy()
                env_new["RUST_BACKTRACE"] = "1"
                host_procs.append(host.popen(cmd, stdin=PIPE, stderr=sys.stdout, env=env_new))
                tee_process_stdout(host_procs[-1], f"[hst{ip}] ")

        time.sleep(3)

        ratio = 1.0
        if with_ratio and not bw_mon_socketed:
            mon_cmd.stdin.write(b"\n"); mon_cmd.stdin.flush()
            data = mon_cmd.stdout.readline().strip().decode().split(",")
            _ = mon_cmd.stdout.readline()
            time_ns = int(data[0][:-2])
            def mbpsify_bytes(bts): return 8000*float(bts)/time_ns
            load_mbps = [list(map(mbpsify_bytes, el.strip().split(" "))) for el in data[1:]]
            observed = load_mbps[0][0] + load_mbps[0][1]
            print(observed, bw_all[2])
            ratio = observed / bw_all[2]
            print("new limit is:", ratio*capacity, "from", capacity)
            if protect_final_hop:
                for core_link in core_links:
                    core_link.intf1.config(bw=ratio*float(capacity))
                    core_link.intf2.config(bw=ratio*float(capacity))

        last_traffic_ratio = 0.0
        g_reward = 0.0
        reward = 0.0

        learner_stats = [{} for _ in actors]
        learner_traces = [{} for _ in actors]
        learner_queues = [{"curr": ([], set(), set()), "future": set(), "pos": 0} for _ in actors]
        learner_fvecs = [{} for _ in actors]
        # flows_to_query = []

        spiffy_measurements = [{}, {}]
        spiffy_verdict = {}

        for i in range(episode_length):
            if interrupted[0]: break

            if not spiffy_but_bad:
                # enactActions collapsed inline (calls updateUpstreamRoute)
                for ((node_label, sarsa, _leader), state) in zip(actors, learner_traces):
                    node = vertex_map[node_label]
                    if actions_target_flows:
                        # handled later after selection; we push per-flow updates
                        pass
                    else:
                        if len(state) == 0:
                            action = default_machine_state
                            machine = AcTrans()
                        else:
                            ((svec, action, _), machine) = state
                            action = machine.action()
                        a = action if override_action is None else override_action
                        tx_ac = sarsa.actions[a] if isinstance(a, (int, int)) else a
                        updateUpstreamRoute(node, ac_prob=tx_ac)
            else:
                curr_time = time.time()
                to_delete = []
                inspect = False
                for ip_pair in list(spiffy_measurements[0].keys()):
                    (src_ip, dst_ip) = ip_pair
                    (rate, timestamp, unlimited, node) = spiffy_measurements[0][ip_pair]
                    if not unlimited:
                        unlimited = True
                        isolateSpiffyFlow(spiffy_dest_switches, src_ip, dst_ip)
                    if curr_time - timestamp >= spiffy_act_time and ip_pair in spiffy_measurements[1]:
                        curr_rate = spiffy_measurements[1][ip_pair]
                        tbe = float(curr_rate)/max(float(rate), 0.0001)
                        bad_flow = tbe < spiffy_expansion_factor
                        spiffy_verdict[ip_pair] = bad_flow
                        okaySpiffyFlow(spiffy_dest_switches, src_ip, dst_ip)
                        if bad_flow:
                            blockSpiffyFlow(spiffy_dest_switches, src_ip, dst_ip, ingress_switch=node)
                        to_delete.append(ip_pair)
                        print(socket.inet_ntoa(struct.pack("!I", src_ip)), rate, curr_rate, tbe, spiffy_expansion_factor, bad_flow, "should have been", (src_ip%2 == 1))
                for ip in to_delete:
                    for el in spiffy_measurements:
                        if ip in el: del el[ip]

            time.sleep(dt)

            preask = time.time()
            if i % 50 == 0:
                 LOG.debug("[debug] query_size=%d example=%s",
              len(flows_to_query),
              next(iter(flows_to_query)) if flows_to_query else None)
            (time_ns, unfused_load_mbps, parsed_flows) = ask_stats(list(flows_to_query), len(monitored_links), len(learner_pos))

            # Timely status: log + CSV
            try:
                mon_reporter.tick(time_ns, unfused_load_mbps, parsed_flows)
            except Exception as e:
                LOG.exception("monitor reporter failed: %s", e)

            if i % 50 == 0:
                LOG.debug("[debug] total_flow_entries=%d per_agent=%s",
                          sum(len(x) for x in parsed_flows),
                          [len(x) for x in parsed_flows])
            postask = time.time()
            if print_times: 
                print("total:", postask - preask)

            def mbpsify(bts): return 8000*float(bts)/time_ns

            load_mbps = [(ig+og, ib+ob) for ((ig, ib), (og, ob)) in zip(unfused_load_mbps[::2], unfused_load_mbps[1::2])]
            unfused_total_mbps = [good+bad for (good, bad) in unfused_load_mbps]
            total_mbps = [good + bad for (good, bad) in load_mbps]

            def get_data(n):
                reward_src = load_mbps[n]
                if reward_direction == "in":
                    reward_src = unfused_load_mbps[2*n]
                elif reward_direction == "out":
                    reward_src = unfused_load_mbps[2*n + 1]
                return reward_src

            def get_total(n):
                reward_src = total_mbps[n]
                if state_direction == "in":
                    reward_src = unfused_total_mbps[2*n]
                elif state_direction == "out":
                    reward_src = unfused_total_mbps[2*n + 1]
                return reward_src

            l_cap = (2.0 if state_direction == "fuse" else 1.0) * capacity
            # flows_to_query = []
            datas = {}
            totals = {}
            g_rewards = {}
            g_reward = 0.0
            reward = 0.0

            dest_sum_data = [0.0, 0.0]
            direction_sum = [0.0, 0.0]
            for dest in dests:
                totals[dest] = 0.0
                datas[dest] = (0.0, 0.0)
                for link in dest_links[dest]:
                    index = link_map[link]
                    (data_g_o, data_b_o) = datas[dest]
                    (data_g, data_b) = get_data(index)
                    totals[dest] += get_total(index)
                    datas[dest] = (data_g_o + data_g, data_b_o + data_b)
                    dest_sum_data[0] += data_g
                    dest_sum_data[1] += data_b
                    unfused_start = index*2
                    in_out = unfused_total_mbps[unfused_start:unfused_start+2]
                    direction_sum[0] += in_out[0]
                    direction_sum[1] += in_out[1]
                r_g = safe_reward_func(std_marl, totals[dest], datas[dest][0], bw_all[0]/float(len(dests)),
                                       0.0, 0.0, 0.0, 0, l_cap, ratio)
                g_rewards[dest] = r_g
                g_reward += (r_g / float(len(dests)))

            last_traffic_ratio = min(dest_sum_data[0]/bw_all[0], 1.0)
            if not (i % 10):
                LOG.info("\titer %d/%d, good:%s, load:%.2f (%.2f,%.2f)",
                i, episode_length, last_traffic_ratio,
                dest_sum_data[0] + dest_sum_data[1], *direction_sum)

            intime = time.time()
            for learner_no, (node_label, sarsa, leader_nodes) in enumerate(actors):
                node = vertex_map[node_label]

                first_sarsa = sarsa  # keep original logic
                target_sarsa = first_sarsa if single_learner else sarsa

                def dumb_hash(*args): return 0
                def smart_hash(n_choices, src_ip, dst_ip, src_mac, *args):
                    # simplified constant-hash choice to keep path selection stable
                    base = struct.pack("<III", int(src_ip), 0, 0)
                    hashes = [hash(base + struct.pack("<I", i)) for i in range(n_choices)]
                    winner = int(np.argmax(hashes))
                    return winner
                hash_fn = smart_hash if actions_target_flows else dumb_hash

                def indices_for_state_vec(dst_ip, src_ip, show_choices=False):
                    # normalize dst_ip to dotted-quad string
                    if isinstance(dst_ip, (int, np.integer)):
                        try:
                            dst_ip_s = socket.inet_ntoa(struct.pack("!I", int(dst_ip)))
                        except Exception:
                            dst_ip_s = None
                    elif isinstance(dst_ip, str):
                        dst_ip_s = dst_ip
                    else:
                        dst_ip_s = None

                    # pick destination node
                    if dst_ip_s in dest_from_ip:
                        end = dest_from_ip[dst_ip_s]
                    else:
                        # Fallback if the flow parser gave us something we don't track as a server
                        # (e.g., legacy non-socketed parser or reverse/garbled IP)
                        end = dests[0]

                    curr = node_label
                    path = [curr]
                    while curr != end:
                        next_set = list(dest_map[curr][end])
                        curr = next_set[hash_fn(len(next_set), src_ip, dst_ip, host_ip_mac_map.get(src_ip, "00:00:00:00:00:00"))]
                        path.append(curr)
                    path.reverse()
                    parts = list(zip(path, path[1:]))
                    h0 = parts[0]; h3 = parts[-1]
                    internals = []
                    if len(parts) == 1:
                        internals = [h0, h3]
                    else:
                        usables = parts[1:-1]
                        tert_pt = float(len(usables)) / 3.0
                        internals.append(usables[int(tert_pt)] if usables else h0)
                        internals.append(usables[int(tert_pt*2)] if len(usables) > 1 else h3)
                    return [h0] + internals + [h3]


                t_dest = dests[np.random.choice(len(dests))]
                main_links = indices_for_state_vec(t_dest[0][0], 0)
                state_vec = [total_mbps[link_map[x]] for x in main_links]

                l_rewards = {}
                for dest in dests:
                    if use_path_measurements:
                        r_l = g_rewards[dest]
                    else:
                        lead = leader_nodes if leader_nodes is not None else main_links[2]
                        lead_i = link_map[lead]
                        r_l = safe_reward_func(reward_func, totals[dest], datas[dest][0], bw_all[0]/float(len(dests)),
                                               get_total(lead_i), get_data(lead_i)[0], bw_teams[lead][0],
                                               n_teams, l_cap, ratio)
                    l_rewards[dest] = r_l
                    reward += r_l/float(len(dests)*len(actors))

                l_index = learner_no
                if actions_target_flows:
                    flow_space = learner_stats[l_index]
                    flow_traces = learner_traces[l_index]
                    flows_seen = parsed_flows[l_index] if l_index < len(parsed_flows) else []
                    if i % 50 == 0:
                        print(f"[ep={ep} iter={i}] learner {learner_no}: flows_seen={len(flows_seen)}")
                    queue_holder = learner_queues[l_index]
                    fvec_holder = learner_fvecs[l_index]

                    # Normalize flows_seen so we always iterate over (src_ip, dst_ip, props)
                    norm_flows = []
                    if flows_seen:
                        sample = flows_seen[0]
                        # Socketed path returns (src_ip_int, props_tuple)
                        if isinstance(sample, tuple) and len(sample) == 2 and not isinstance(sample[1], dict):
                            # Build once: list of candidate destination IPs (as native ints in network order)
                            dest_ip_list = list(dest_from_ip.keys())  # ["10.0.0.1", "10.0.0.2", ...]
                            dest_ip_ints = [struct.unpack("!I", socket.inet_aton(x))[0] for x in dest_ip_list]

                            def _pick_dst_for_src(src_ip_int: int) -> int:
                                """Stable per-flow dst pick:
                                - single-destination: trivial
                                - multi-destination: use the same hash you use for path selection
                                """
                                if len(dest_ip_ints) == 1:
                                    return dest_ip_ints[0]
                                mac = host_ip_mac_map.get(src_ip_int, "00:00:00:00:00:00")
                                idx = hash_fn(len(dest_ip_ints), src_ip_int, 0, mac)
                                return dest_ip_ints[idx]

                            for (src_ip_int, props_tuple) in flows_seen:
                                dst_ip_int = _pick_dst_for_src(src_ip_int)
                                if src_ip_int == dst_ip_int and len(dest_ip_ints) > 1:
                                    # extremely rare; re-roll with a different seed to avoid src==dst
                                    mac = host_ip_mac_map.get(src_ip_int, "00:00:00:00:00:00")
                                    idx2 = (hash_fn(len(dest_ip_ints), src_ip_int, 1, mac)) % len(dest_ip_ints)
                                    dst_ip_int = dest_ip_ints[idx2]
                                norm_flows.append((src_ip_int, dst_ip_int, props_tuple))
                        else:
                            # Legacy/non-socketed path: list of (src_ip_int, {dst_ip_str: props_tuple, ...})
                            for (src_ip_int, props_dict) in flows_seen:
                                for (dst_ip_str, props_tuple) in props_dict.items():
                                    dst_ip_int = struct.unpack("!I", socket.inet_aton(dst_ip_str))[0]
                                    norm_flows.append((src_ip_int, dst_ip_int, props_tuple))

                    total_spent = 0.0
                    def can_act(): return trs_maxtime is None or total_spent <= trs_maxtime

                    (local_work, local_work_set, local_visited_set) = queue_holder["curr"]
                    local_pos = queue_holder["pos"]

                    for (src_ip, dst_ip, props) in norm_flows:
                        main_links = indices_for_state_vec(dst_ip, src_ip)
                        state_vec = [total_mbps[link_map[x]] for x in main_links]
                        flows_to_query.add(src_ip)

                        ip_pair = (src_ip, dst_ip)
                        if ip_pair not in flow_space:
                            flow_space[ip_pair] = {
                                "ip": src_ip, "last_act": 0.0 if not spiffy_mode else 0.05,
                                "last_rate_in": -1.0, "last_rate_out": -1.0,
                                "pkt_in_count": 0, "pkt_out_count": 0,
                            }
                        l = flow_space[ip_pair]
                        l["cx_ratio"] = min(*props[1:3]) / max(*props[1:3])
                        l["length"] = props[0]
                        l["size"] = props[1] + props[2]
                        l["mean_iat"] = props[11]
                        l["pkt_in_count"] += props[7]
                        l["pkt_out_count"] += props[10]
                        l["pkt_in_wnd_count"] = props[7]
                        l["pkt_out_wnd_count"] = props[10]
                        l["mean_bpp_in"] = props[5]
                        l["mean_bpp_out"] = props[8]
                        l["bytes_in"] = props[3]
                        l["bytes_out"] = props[4]

                        observed_rate_in = 8000.0*float(props[3])/time_ns
                        observed_rate_out = 8000.0*float(props[4])/time_ns

                        if l["last_rate_in"] < 0.0:
                            l["last_rate_in"] = observed_rate_in
                            l["last_rate_out"] = observed_rate_out
                        l["delta_in"] = observed_rate_in - l["last_rate_in"]
                        l["delta_out"] = observed_rate_out - l["last_rate_out"]

                        total_vec = state_vec + flow_to_state_vec(l)
                        flow_space[ip_pair] = l

                        fvec = combine_flow_vecs(fvec_holder[ip_pair], total_vec) if ip_pair in fvec_holder else total_vec
                        fvec_holder[ip_pair] = fvec

                        if ip_pair not in local_work_set or ip_pair in local_visited_set:
                            queue_holder["future"].add(ip_pair)

                        if spiffy_but_bad:
                            total_s_bw = observed_rate_in
                            if spiffy_traffic_dir == "out": total_s_bw = observed_rate_out
                            elif spiffy_traffic_dir == "inout": total_s_bw += observed_rate_out
                            if spiffy_mbps_cutoff is not None and total_s_bw < spiffy_mbps_cutoff and total_s_bw > 0.0:
                                spiffy_verdict[ip_pair] = False
                            expt_count = max(spiffy_min_experiments, min(spiffy_max_experiments, int(spiffy_pick_prob * float(len(flows_seen)))))
                            if ip_pair in spiffy_measurements[0]:
                                spiffy_measurements[1][ip_pair] = total_s_bw
                            elif ip_pair not in spiffy_verdict and total_s_bw > 0.0 and len(spiffy_measurements[0]) < expt_count and np.random.random() < spiffy_pick_prob:
                                print("spiffy observing flow", ip_pair)
                                spiffy_measurements[0][ip_pair] = (total_s_bw, time.time(), False, node)

                    if local_pos >= len(local_work):
                        local_pos = 0
                        local_work_set = queue_holder["future"]
                        local_visited_set = set()
                        queue_holder["future"] = set()
                        local_work = list(local_work_set)
                        random.shuffle(local_work)

                    flows_procd = 0
                    while can_act() and local_pos < len(local_work):
                        ip_pair = local_work[local_pos]
                        local_visited_set.add(ip_pair)
                        s_t = time.time()
                        l = flow_space[ip_pair]

                        total_vec = (fvec_holder[ip_pair])[:feature_max]
                        s_tree_t = 0; s_tree_l = 0
                        subactors = [(target_sarsa, restrict)] + [ (s_tree[s_tree_t][s_tree_l], r) for (s_tree, r) in contributors ]

                        last_sarsa = sarsa
                        ac_vals = np.zeros(len(sarsa.actions))
                        substates = []
                        need_decay = True

                        for s_ac_num, (s, r) in enumerate(subactors):
                            tx_vec = total_vec if r is None else [total_vec[i] for i in r]
                            state = s.to_state(np.array(tx_vec))
                            if ip_pair in learner_traces[l_index]:
                                dat = learner_traces[l_index][ip_pair]
                                (st, z, narrowing_in_use) = dat[0][s_ac_num]
                                machine = dat[2]

                                allow_update_narrowing = False
                                allow_action_narrowing = False

                                if narrowing_in_use is None and (np.random.uniform() < s.get_epsilon() * explore_feature_isolation_modifier):
                                    cand = _pick_narrowing_indices(s, always_include_global)
                                    if cand is not None and len(cand) > 0:
                                        narrowing_in_use = [explore_feature_isolation_duration, cand]
                                        allow_action_narrowing = True
                                    else:
                                        # No valid tiles to narrow on; leave narrowing disabled for this step
                                        narrowing_in_use = None
                                elif narrowing_in_use is not None:
                                    narrowing_in_use[0] -= 1
                                    allow_update_narrowing = True
                                    allow_action_narrowing = narrowing_in_use[0] > 0

                                dest_label = resolve_dest_label(ip_pair[1])
                                (would_choose, new_vals, z_vec) = s.update(
                                    state,
                                    l_rewards[dest_label],
                                    (st, dat[1], z),
                                    decay=False,
                                    delta_space=[i] if record_deltas_in_times else None,
                                    action_narrowing=None if not allow_action_narrowing else narrowing_in_use[1],
                                    update_narrowing=None if not allow_update_narrowing else narrowing_in_use[1],
                                )
                                if isinstance(ip_pair[1], (int, np.integer)) and ip_pair[1] == 167772161:
                                    LOG.debug("dst %s -> %s", ip_pair[1],
                                                socket.inet_ntoa(struct.pack('!I', int(ip_pair[1]))))

                            else:
                                (would_choose, new_vals, z_vec) = s.bootstrap(state)
                                need_decay = False
                                machine = AcTrans()
                                narrowing_in_use = None
                            ac_vals += new_vals
                            substates.append((state, z_vec, narrowing_in_use))
                            last_sarsa = s

                        l_action = last_sarsa.select_action_from_vals(ac_vals)
                        machine.move(l_action)
                        learner_traces[l_index][ip_pair] = (substates, l_action, machine, z_vec, [True])

                        if need_decay:
                            for (s, _) in subactors: s.decay()

                        observed_rate_in = l["last_rate_in"] + l["delta_in"]
                        observed_rate_out = l["last_rate_out"] + l["delta_out"]
                        l["last_act"] = machine.action()
                        l["last_rate_in"] = observed_rate_in
                        l["last_rate_out"] = observed_rate_out
                        flow_space[ip_pair] = l

                        # APPLY action to switch for that (src,dst) pair:
                        tx_ac = machine.action() if isinstance(l_action, (int, int)) else l_action
                        updateUpstreamRoute(node, ac_prob=tx_ac, target_ip=ip_pair)

                        # --- NEW: log decision and update per-host "last prediction" ---
                        allow_prob = float(tx_ac)
                        src_ip_int, dst_ip_int = ip_pair[0], ip_pair[1]
                        pred_bad = (allow_prob < allow_threshold)

                        truth_flag = truth_bad_map.get(src_ip_int, None)
                        if truth_flag is not None:
                            last_pred_bad[src_ip_int] = pred_bad  # used for end-of-episode scoring

                        if action_fp is not None:
                            src_s = int_to_ip(src_ip_int)        # was inet_ntoa(struct.pack("I", ...))
                            dst_s = int_to_ip(dst_ip_int)
                            truth_bad_str = "" if truth_flag is None else ("1" if truth_flag else "0")
                            action_fp.write(f"{i},{learner_no},{src_s},{dst_s},{allow_prob:.6f},{1 if pred_bad else 0},{truth_bad_str}\n")
                            actions_written += 1



                        e_t = time.time()
                        total_spent += e_t - s_t
                        if record_times: action_comps[-1].append((i, e_t - s_t))
                        local_pos += 1
                        flows_procd += 1
                        del fvec_holder[ip_pair]

                    queue_holder["curr"] = (local_work, local_work_set, local_visited_set)
                    queue_holder["pos"] = local_pos

                else:
                    prev_state = learner_traces[l_index]
                    if prev_state == {}:
                        machine = AcTrans()
                        prev_state = (sarsa.last_act, machine)
                    (last_act, machine) = prev_state
                    s_t = time.time()
                    state = sarsa.to_state(np.array(state_vec))
                    target_sarsa.update(state, reward, last_act)
                    machine.move(sarsa.last_act[1])
                    learner_traces[l_index] = (sarsa.last_act, machine)
                    e_t = time.time()
                    if record_times: action_comps[-1].append((i, e_t - s_t))

            outtime = time.time()
            if print_times: print("choose_acs:", outtime - intime)

            good_traffic_percents[-1].append(last_traffic_ratio)
            rewards[-1].append(g_reward)
            total_loads[-1].append(total_mbps[0])
            last_iter_snapshot["good"] = float(last_traffic_ratio)
            last_iter_snapshot["g_reward"] = float(g_reward)
            last_iter_snapshot["selected"] = float(reward)

        # print("good:", last_traffic_ratio, ", g_reward:", g_reward, ", selected:", reward)

        print(f"good: {last_iter_snapshot['good']} , "
        f"g_reward: {last_iter_snapshot['g_reward']} , "
        f"selected: {last_iter_snapshot['selected']}")
        # Only drop into CLI after the very last episode
        is_last_episode = (ep == episodes - 1)
        if interactive_cli_post and is_last_episode:
            print("[INFO] Final post-run Mininet CLI (after last episode). 'exit' to clean up.")
            CLI(net)

        try:
            if bw_mon_socketed and bw_sock[0] is not None:
                try:
                    bw_sock[0].shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                bw_sock[0].close()
                bw_sock[0] = None
        except Exception:
            LOG.debug("bwmon socket cleanup failed", exc_info=True)

        mon_cmd.stdin.close()

        # Close monitor reporter
        try:
            mon_reporter.close()
        except Exception:
            pass

        for server_proc in server_procs:
            try: server_proc.terminate()
            except: print("couldn't cleanly shutdown server process...")

        if ctl_proc is not None:
            try: ctl_proc.terminate()
            except: print("couldn't cleanly shutdown control process...")

        for proc in host_procs:
            try: proc.terminate()
            except: print("couldn't cleanly shutdown host process...")

        # --- NEW: per-episode metrics from final prediction of each host we saw ---
        if action_fp is not None:
            action_fp.close()

        # Compare last prediction vs truth
        n_pred = 0
        for ip_int, is_bad in truth_bad_map.items():
            if ip_int not in last_pred_bad:
                continue  # no prediction made for this host this episode
            n_pred += 1
            pred_bad = last_pred_bad[ip_int]
            if is_bad and pred_bad: ep_counts["tp"] += 1
            elif (not is_bad) and (not pred_bad): ep_counts["tn"] += 1
            elif (not is_bad) and pred_bad: ep_counts["fp"] += 1
            elif is_bad and (not pred_bad): ep_counts["fn"] += 1

        # Derived metrics
        tp, tn, fp, fn = (ep_counts[k] for k in ("tp","tn","fp","fn"))
        support = tp + tn + fp + fn
        acc = (tp + tn) / support if support else 0.0
        prec = tp / (tp + fp) if (tp + fp) else 0.0
        rec = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = (2*prec*rec)/(prec+rec) if (prec+rec) else 0.0
        coverage = n_pred / max(1, len(truth_bad_map))  # fraction of hosts we actually produced a prediction for

        # Write JSON + append CSV row
        ep_metrics = {
            "episode": ep,
            "tp": tp, "tn": tn, "fp": fp, "fn": fn,
            "support": support,
            "accuracy": acc, "precision": prec, "recall": rec, "f1": f1,
            "coverage": coverage,
            "threshold": allow_threshold,
        }
        with open(os.path.join(log_dir, f"metrics_ep{ep:04d}.json"), "w") as f:
            json.dump(ep_metrics, f, indent=2)

        # Append to summary CSV
        summary_path = os.path.join(log_dir, "metrics_summary.csv")
        if not os.path.exists(summary_path):
            with open(summary_path, "w") as f:
                f.write("episode,tp,tn,fp,fn,support,accuracy,precision,recall,f1,coverage,threshold\n")
        with open(summary_path, "a") as f:
            f.write(f"{ep},{tp},{tn},{fp},{fn},{support},{acc:.6f},{prec:.6f},{rec:.6f},{f1:.6f},{coverage:.6f},{allow_threshold}\n")

        host_procs = []
        server_procs = []
        ctl_proc = None
        net.stop()
        store_sarsas = team_sarsas
        next_ip[:] = [1]

    return (rewards, good_traffic_percents, total_loads, store_sarsas, random.getstate(), action_comps)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--cli", choices=["none", "pre", "post", "both"], default="post")
    ap.add_argument("--episodes", type=int, default=1)
    ap.add_argument("--episode-length", type=int, default=5000)
    ap.add_argument("--log-dir", type=str, default="logs")
    ap.add_argument("--log-level", type=str, default="INFO",
                    choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
    args = ap.parse_args()

    # init logging (console + file) before anything else
    log_file = setup_logging(args.log_dir, args.log_level)
    LOG.info("log file: %s", log_file)

    cli_pre  = args.cli in ("pre", "both")
    cli_post = args.cli in ("post", "both")

    marlExperiment(
        model="nginx",
        submodel="http",
        use_controller=True,
        episodes=args.episodes,
        episode_length=args.episode_length,
        interactive_cli_pre=cli_pre,
        interactive_cli_post=cli_post,
        actions_target_flows=True,
        bw_mon_socketed=True,
        log_actions=True,
        allow_threshold=0.5,
        unix_sock=True,
        log_dir=args.log_dir,
    )
