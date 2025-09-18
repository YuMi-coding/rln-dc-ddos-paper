#!/usr/bin/env python3
# bwmon_test_client.py
# Enhanced test client for marl-bwmon: prints dotted/hex, can re-query using internal IPs returned,
# and optionally sends both byte-orders for each IP (debugging).

import socket, struct, sys, time, argparse
from contextlib import closing

# --- Packers & fm layout (match server) ---
sz_packer    = struct.Struct("!I")   # network-order u32 for counts
ip_packer    = struct.Struct("=I")   # network-order u32 for IPs (what bwmon expects)
time_packer  = struct.Struct("=q")   # native-endian int64_t (bwmon uses =q)
bytes_packer = struct.Struct("=Q")   # native-endian uint64_t (counters)
fm_packer    = struct.Struct("=q6Q6fI4x")  # 88 bytes FlowMeasurement (as Python code expects)

# --- helpers ---
def ip_to_u32(ip):
    """dotted -> network-order u32 integer"""
    return struct.unpack("!I", socket.inet_aton(ip))[0]

def u32_to_ip(u):
    """network-order u32 -> dotted string"""
    return socket.inet_ntoa(struct.pack("!I", u & 0xffffffff))

def swap32(u):
    """byte-swap a 32-bit integer"""
    return (((u & 0xFF) << 24) |
            ((u & 0xFF00) << 8) |
            ((u & 0xFF0000) >> 8) |
            ((u >> 24) & 0xFF)) & 0xffffffff

def read_until_min(sock, nmin, ntry, timeout=5.0):
    sock.settimeout(0.5)
    start = time.time()
    data = b""
    while len(data) < nmin and (time.time() - start) < timeout:
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            continue
        except BlockingIOError:
            continue
        if not chunk:
            break
        data += chunk
    # top up opportunistically
    try:
        sock.settimeout(0.05)
        while len(data) < ntry:
            chunk = sock.recv(4096)
            if not chunk: break
            data += chunk
    except socket.timeout:
        pass
    finally:
        sock.settimeout(None)
    return data

def parse_reply(recvd, n_ifs, n_agents):
    off = 0
    # 1) time_ns (int64)
    if len(recvd) < off + time_packer.size:
        raise ValueError("short reply (time)")
    (time_ns,) = time_packer.unpack(recvd[off:off+time_packer.size]); off += time_packer.size
    if time_ns <= 0:
        time_ns = 1

    # 2) counters: 4 * n_ifs uint64s
    need_c = 4 * n_ifs * bytes_packer.size
    if len(recvd) < off + need_c:
        raise ValueError(f"short reply (counters) need {need_c} got {len(recvd)-off}")
    vals = []
    for i in range(4 * n_ifs):
        (v,) = bytes_packer.unpack(recvd[off:off+8])
        vals.append(v)
        off += 8
    goods = vals[:2*n_ifs]
    bads  = vals[2*n_ifs:]

    # 3) per-agent flow blocks
    parsed_flows = []
    for a in range(n_agents):
        if len(recvd) < off + sz_packer.size:
            parsed_flows.append([])
            continue
        (n_flow_entries,) = sz_packer.unpack(recvd[off:off+4]); off += 4
        need = n_flow_entries * fm_packer.size
        if len(recvd) < off + need:
            parsed_flows.append([])
            off = len(recvd)
            continue
        flows = []
        for _ in range(n_flow_entries):
            chunk = recvd[off:off+fm_packer.size]; off += fm_packer.size
            vals = list(fm_packer.unpack(chunk))
            # The last 8 bytes are: ip(u32 big-endian) + 4 bytes pad
            ip_be = struct.unpack("=I", chunk[-8:-4])[0]
            flows.append((ip_be, vals[:-1]))
        parsed_flows.append(flows)

    return time_ns, goods, bads, parsed_flows

# --- run helpers for unix & tcp ---
def run_unix(sockpath, ips_u32, n_ifs=10, n_agents=6, timeout=5.0):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(sockpath)
        s.setblocking(True)
        # send count (network-order u32) then packed ips (each "!I")
        s.sendall(sz_packer.pack(len(ips_u32)))
        if ips_u32:
            s.sendall(b"".join(ip_packer.pack(ip & 0xffffffff) for ip in ips_u32))
        # minimal read for header+ counters; then parse
        min_need = time_packer.size + 4 * n_ifs * bytes_packer.size
        recvd = read_until_min(s, min_need, min_need + (n_agents * 4) + (n_agents * 88), timeout=timeout)
        if len(recvd) < min_need:
            raise TimeoutError(f"short read when waiting for headers only received {len(recvd)} bytes")
        time_ns, goods, bads, parsed_flows = parse_reply(recvd, n_ifs, n_agents)
        return s, time_ns, goods, bads, parsed_flows

def run_tcp(host, port, ips_u32, n_ifs=10, n_agents=6, timeout=5.0):
    with closing(socket.create_connection((host, port), timeout=5.0)) as s:
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.setblocking(True)
        s.sendall(sz_packer.pack(len(ips_u32)))
        if ips_u32:
            s.sendall(b"".join(ip_packer.pack(ip & 0xffffffff) for ip in ips_u32))
        recvd = read_until_min(s, time_packer.size + 4 * n_ifs * bytes_packer.size,
                               time_packer.size + 4 * n_ifs * bytes_packer.size + n_agents * (4 + 88),
                               timeout=timeout)
        time_ns, goods, bads, parsed_flows = parse_reply(recvd, n_ifs, n_agents)
        return s, time_ns, goods, bads, parsed_flows

# --- CLI / main ---
def main():
    ap = argparse.ArgumentParser(description="Enhanced bwmon test client")
    ap.add_argument("--sock", default="/tmp/bwmon-sock", help="unix socket path")
    ap.add_argument("--tcp", action="store_true", help="use TCP 127.0.0.1:9932 instead")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9932)
    ap.add_argument("--nifs", type=int, default=10)
    ap.add_argument("--nagents", type=int, default=6)
    ap.add_argument("--follow", action="store_true",
                    help="after first reply, re-query bwmon using internal IPs returned")
    ap.add_argument("--both-endian", action="store_true",
                    help="send both network-order and byte-swapped forms for each IP (debug)")
    ap.add_argument("ips", nargs="*", help="IP addresses to query (dotted form). If empty uses two public examples.")
    args = ap.parse_args()

    if not args.ips:
        ips = ["139.164.230.26", "115.200.53.188"]
    else:
        ips = args.ips

    # convert dotted -> network-order ints
    ips_u32 = [ip_to_u32(ip) for ip in ips]
    print("Initial Querying (dotted)->(u32):")
    for d,u in zip(ips, ips_u32):
        print(f"  {d} -> 0x{u:08x} ({u})")

    try:
        if args.tcp:
            sock, tn, goods, bads, parsed = run_tcp(args.host, args.port, ips_u32, args.nifs, args.nagents)
        else:
            sock, tn, goods, bads, parsed = run_unix(args.sock, ips_u32, args.nifs, args.nagents)
    except Exception as e:
        print("ERROR talking to bwmon:", e)
        raise

    print(f"\nwindow_ns: {tn}")
    print("per-interface goods (u64):", goods)
    print("per-interface bads  (u64):", bads)

    print("\nParsed flows (per agent):")
    for ai, af in enumerate(parsed):
        print(f" Agent {ai}: {len(af)} entries")
        for ipint, props in af:
            dotted = u32_to_ip(ipint)
            print(f"   ip={dotted} (0x{ipint:08x}) props_len={len(props)} props_sample={props[:12]}")

    # If follow mode: build an allowed list from the first agent's returned IPs (internal IPs)
    if args.follow:
        # pick the first agent that returned something
        first_agent_flows = next((af for af in parsed if len(af) > 0), [])
        if not first_agent_flows:
            print("No flows returned to follow. Exiting.")
            return

        # gather unique internal IP ints (they are already network-order u32s)
        internal_u32s = sorted({ipint for ipint, _ in first_agent_flows})
        print("\nWill re-query using these internal IPs (dotted/hex):")
        for u in internal_u32s:
            print(f"  {u32_to_ip(u)} -> 0x{u:08x} ({u})")

        # build final send list: option to include both endian variants
        send_list = []
        for u in internal_u32s:
            send_list.append(u)
            if args.both_endian:
                send_list.append(swap32(u))

        # send a fresh query (reconnect) using the discovered internal IPs
        print("\nRe-querying with allowed IPs (to keep flows in subsequent windows)...")
        try:
            if args.tcp:
                _, tn2, goods2, bads2, parsed2 = run_tcp(args.host, args.port, send_list, args.nifs, args.nagents)
            else:
                _, tn2, goods2, bads2, parsed2 = run_unix(args.sock, send_list, args.nifs, args.nagents)
        except Exception as e:
            print("ERROR re-querying bwmon:", e)
            raise

        print(f"\n(re-query) window_ns: {tn2}")
        print("per-interface goods (u64):", goods2)
        print("per-interface bads  (u64):", bads2)
        print("\nParsed flows after re-query (per agent):")
        for ai, af in enumerate(parsed2):
            print(f" Agent {ai}: {len(af)} entries")
            for ipint, props in af:
                dotted = u32_to_ip(ipint)
                print(f"   ip={dotted} (0x{ipint:08x}) props_len={len(props)} props_sample={props[:13]}")

if __name__ == "__main__":
    main()
