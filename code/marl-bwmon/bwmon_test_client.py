
#!/usr/bin/env python3
# bwmon_debug_client.py
# A debug-first test client for marl-bwmon.

import argparse, socket, struct, time, sys
from contextlib import closing
from typing import List, Tuple

def hexdump(b: bytes, width: int = 16) -> str:
    out = []
    for i in range(0, len(b), width):
        chunk = b[i:i+width]
        hexs = ' '.join(f"{x:02x}" for x in chunk)
        text = ''.join(chr(x) if 32 <= x < 127 else '.' for x in chunk)
        out.append(f"{i:04x}: {hexs:<{width*3}}  {text}")
    return '\n'.join(out)

def ip_to_u32_be(ip_str: str) -> int:
    return struct.unpack("!I", socket.inet_aton(ip_str))[0]

def u32_be_to_ip(u: int) -> str:
    return socket.inet_ntoa(struct.pack("!I", u & 0xffffffff))

def packer_for_u32(mode: str) -> struct.Struct:
    if mode == "native":   return struct.Struct("=I")
    if mode == "network":  return struct.Struct("!I")
    if mode == "little":   return struct.Struct("<I")
    if mode == "big":      return struct.Struct(">I")
    raise ValueError(f"unknown u32 packer mode: {mode}")

def read_exact(sock: socket.socket, nbytes: int, deadline_sec: float) -> bytes:
    data = bytearray()
    end = time.time() + deadline_sec
    while len(data) < nbytes:
        remaining = end - time.time()
        if remaining <= 0:
            raise TimeoutError(f"timed out waiting for {nbytes} bytes, got {len(data)}")
        sock.settimeout(remaining)
        chunk = sock.recv(nbytes - len(data))
        if not chunk:
            raise TimeoutError(f"peer closed while waiting for {nbytes} bytes, got {len(data)}")
        data.extend(chunk)
    sock.settimeout(None)
    return bytes(data)

# Header packers (native on both ends in existing marl-bwmon impl)
TIME_PACK   = struct.Struct("=q")  # int64 native
U64_PACK    = struct.Struct("=Q")  # uint64 native

# FlowMeasurement head (80 bytes native): q, 6Q, 6f
FM_HEAD_PACK = struct.Struct("=q6Q6f")
FM_TOTAL_SIZE = 88  # full size including ip:u32 + pad4

def recv_reply(sock: socket.socket, *, nifs: int, nagents: int,
               count_endian: str = "network",
               flow_ip_endian: str = "native",
               header_deadline: float = 3.0,
               agent_deadline: float = 3.0,
               dump_hex: bool = False):
    # 1) time
    t_bytes = read_exact(sock, TIME_PACK.size, header_deadline)
    (time_ns,) = TIME_PACK.unpack(t_bytes)

    # 2) goods + bads
    goods_bytes = read_exact(sock, nifs * U64_PACK.size, header_deadline)
    bads_bytes  = read_exact(sock, nifs * U64_PACK.size, header_deadline)
    goods = list(struct.unpack(f"={nifs}Q", goods_bytes))
    bads  = list(struct.unpack(f"={nifs}Q", bads_bytes))

    if dump_hex:
        print("DBG RECV header (time/goods/bads) hex:")
        print(hexdump(t_bytes + goods_bytes + bads_bytes), flush=True)

    # 3) per-agent sections
    cnt_pack = packer_for_u32("network" if count_endian == "network" else ("native" if count_endian=="native" else count_endian))
    ip_pack  = packer_for_u32(flow_ip_endian)

    per_agent = []
    for a in range(nagents):
        # count
        cnt_bytes = read_exact(sock, 4, agent_deadline)
        (n_entries,) = cnt_pack.unpack(cnt_bytes)

        flows = []
        total_bytes = n_entries * FM_TOTAL_SIZE
        fm_blob = read_exact(sock, total_bytes, agent_deadline)

        if dump_hex and n_entries:
            print(f"DBG RECV agent {a} count={n_entries} total={total_bytes}B; first record hex:")
            print(hexdump(fm_blob[:FM_TOTAL_SIZE]), flush=True)

        off = 0
        for _ in range(n_entries):
            fm_chunk = fm_blob[off:off+FM_TOTAL_SIZE]
            off += FM_TOTAL_SIZE

            # Parse head (80 bytes) natively, then read IP (4 bytes) with selected endianness
            head = FM_HEAD_PACK.unpack(fm_chunk[:FM_HEAD_PACK.size])
            ip_field = ip_pack.unpack(fm_chunk[FM_HEAD_PACK.size:FM_HEAD_PACK.size+4])[0]

            # Return IP as big-endian integer for consistent dotted printing
            if flow_ip_endian == "native":
                ip_bytes_native = struct.pack("=I", ip_field)
                ip_be = struct.unpack("!I", ip_bytes_native)[0]
            elif flow_ip_endian == "little":
                ip_be = struct.unpack("!I", struct.pack("<I", ip_field))[0]
            elif flow_ip_endian == "big":
                ip_be = ip_field
            else:  # 'network'
                ip_be = ip_field

            flows.append((ip_be, list(head)))  # keep head fields only for brevity
        per_agent.append(flows)

    return time_ns, goods, bads, per_agent

def make_request_payload(ips_be: List[int], count_wire: str, ip_wire: str, dump_hex: bool):
    cnt_pack = packer_for_u32(count_wire)
    ip_pack  = packer_for_u32(ip_wire)
    payload = bytearray()
    payload += cnt_pack.pack(len(ips_be))
    for ip in ips_be:
        payload += ip_pack.pack(ip & 0xffffffff)
    if dump_hex:
        print(f"DBG SEND count={len(ips_be)} ({count_wire}), ip_wire={ip_wire}")
        print(hexdump(payload), flush=True)
    return bytes(payload)

def do_query(sock: socket.socket, ips_be: List[int], *, nifs: int, nagents: int,
             count_wire: str, ip_wire: str, count_recv_endian: str, flow_ip_endian: str,
             header_deadline: float, agent_deadline: float, dump_hex: bool):
    payload = make_request_payload(ips_be, count_wire, ip_wire, dump_hex)
    sock.sendall(payload)
    return recv_reply(sock, nifs=nifs, nagents=nagents,
                      count_endian=count_recv_endian,
                      flow_ip_endian=flow_ip_endian,
                      header_deadline=header_deadline,
                      agent_deadline=agent_deadline,
                      dump_hex=dump_hex)

def main():
    ap = argparse.ArgumentParser(description="Debug client for marl-bwmon (robust reads, hexdumps, endianness toggles)")
    transport = ap.add_mutually_exclusive_group()
    transport.add_argument("--sock", help="UNIX domain socket path (e.g., /tmp/bwmon-sock)")
    transport.add_argument("--tcp", help="TCP host:port (e.g., 127.0.0.1:9932)")

    ap.add_argument("--nifs", type=int, default=20, help="number of interfaces in header (goods/bads)")
    ap.add_argument("--nagents", type=int, default=6, help="number of agents/sections to expect")
    ap.add_argument("--follow", action="store_true", help="send a second query with internal IPs gleaned from the first reply")
    ap.add_argument("--same-socket", action="store_true", help="reuse the same connection for follow-up (default is reconnect)")

    # Wire-format toggles
    ap.add_argument("--count-wire", choices=["native","network","little","big"], default="network",
                    help="endianness for the sent count u32 (default: network)")
    ap.add_argument("--ip-wire", choices=["native","network","little","big"], default="native",
                    help="endianness for the sent IPs u32 (default: native)")
    ap.add_argument("--count-recv", choices=["native","network","little","big"], default="network",
                    help="endianness for the received per-agent count u32 (default: network)")
    ap.add_argument("--flow-ip-wire", choices=["native","network","little","big"], default="native",
                    help="endianness for the received FlowMeasurement.ip field (default: native)")

    # Timeouts
    ap.add_argument("--header-deadline", type=float, default=3.0, help="seconds to read time/goods/bads")
    ap.add_argument("--agent-deadline", type=float, default=3.0, help="seconds to read per-agent sections")

    # Debug
    ap.add_argument("--dump-hex", action="store_true", help="hex-dump requests and first FM of each agent")
    ap.add_argument("--sleep", type=float, default=0.0, help="sleep seconds between initial and follow-up")
    ap.add_argument("ips", nargs="*", help="IPs to query; if empty, uses two public examples")

    args = ap.parse_args()

    if not args.sock and not args.tcp:
        args.sock = "/tmp/bwmon-sock"

    # Prepare initial IP list (big-endian integers)
    if args.ips:
        ips_be = [ip_to_u32_be(ip) for ip in args.ips]
        ip_labels = args.ips
    else:
        ip_labels = ["139.164.230.26", "115.200.53.188"]
        print(f"No IPs specified; using defaults: {', '.join(ip_labels)}", flush=True)
        ips_be = [ip_to_u32_be(ip) for ip in ip_labels]

    print("Initial Querying (dotted) -> (u32 BE):", flush=True)
    for ip, u in zip(ip_labels, ips_be):
        print(f"  {ip:<15} -> 0x{u:08x} ({u})", flush=True)

    def open_sock():
        if args.sock:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(args.sock)
            return s
        host, port = args.tcp.rsplit(":", 1)
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect((host, int(port)))
        return s

    s = open_sock()

    try:
        time_ns, goods, bads, per_agent = do_query(
            s, ips_be,
            nifs=args.nifs, nagents=args.nagents,
            count_wire=args.count_wire, ip_wire=args.ip_wire,
            count_recv_endian=args.count_recv, flow_ip_endian=args.flow_ip_wire,
            header_deadline=args.header_deadline, agent_deadline=args.agent_deadline,
            dump_hex=args.dump_hex
        )

        print(f"\nwindow_ns: {time_ns}", flush=True)
        print("per-interface goods (u64):", goods, flush=True)
        print("per-interface bads  (u64):", bads, flush=True)

        print("\nParsed flows (per agent):", flush=True)
        for a, flows in enumerate(per_agent):
            print(f" Agent {a}: {len(flows)} entries", flush=True)
            if flows:
                ip_be, head = flows[0]
                print(f"   ip={u32_be_to_ip(ip_be)} (0x{ip_be:08x}) head_len={len(head)} head_sample={head[:6]}", flush=True)

        if args.follow:
            if not args.same_socket:
                s.close()
                s = open_sock()

            if args.sleep > 0:
                time.sleep(args.sleep)

            internal_set = {ip_be for flows in per_agent for (ip_be, _) in flows}
            send_list = sorted(internal_set)
            print("\nWill re-query using these internal IPs (dotted/hex):", flush=True)
            for ip_be in send_list:
                print(f"  {u32_be_to_ip(ip_be)} -> 0x{ip_be:08x} ({ip_be})", flush=True)

            if not send_list:
                print("No internal IPs found; skipping follow-up.", flush=True)
            else:
                print("\nRe-querying with allowed IPs...", flush=True)
                time_ns2, goods2, bads2, per_agent2 = do_query(
                    s, send_list,
                    nifs=args.nifs, nagents=args.nagents,
                    count_wire=args.count_wire, ip_wire=args.ip_wire,
                    count_recv_endian=args.count_recv, flow_ip_endian=args.flow_ip_wire,
                    header_deadline=args.header_deadline, agent_deadline=args.agent_deadline,
                    dump_hex=args.dump_hex
                )
                print(f"\n(re-query) window_ns: {time_ns2}", flush=True)
                print("per-interface goods (u64):", goods2, flush=True)
                print("per-interface bads  (u64):", bads2, flush=True)
                print("\nParsed flows after re-query (per agent):", flush=True)
                for a, flows in enumerate(per_agent2):
                    print(f" Agent {a}: {len(flows)} entries", flush=True)
                    if flows:
                        ip_be, head = flows[0]
                        print(f"   ip={u32_be_to_ip(ip_be)} (0x{ip_be:08x}) head_len={len(head)} head_sample={head[:6]}", flush=True)

    finally:
        try:
            s.close()
        except Exception:
            pass

if __name__ == "__main__":
    main()
