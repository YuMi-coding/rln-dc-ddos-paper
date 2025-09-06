# CloudLab Mininet + Ryu Testbed (Replica of *rln-dc-ddos-paper* Evaluation)

This README gives you a **drop‑in, CloudLab‑ready quickstart** to replicate the evaluation testbed used in the `rln-dc-ddos-paper` repository—using **Mininet + Ryu**, the single‑destination *tree* topology, and scripted benign/attack traffic. It’s designed to live inside your fork (e.g., `YuMi-coding/rln-dc-ddos-paper`) without modifying the paper code itself.

> If you followed the suggested PR layout, these files exist:
>
> ```text
> cloudlab/
>   setup-cloudlab.sh          # one‑shot installer (Mininet, OVS, Ryu, tools)
>   topos/tree_topo.py         # paper’s single‑destination tree (k=2, ℓ=3, m=2, n∈{2,4,8,16})
>   ryu/agent_controller.py    # minimal L2 + hook for RL/heuristic per‑flow actions
>   experiments/README.md      # (optional) per‑experiment notes
> Makefile                     # shortcuts: setup / ryu / tree / clean
> ```

---

## 1) Prerequisites

- CloudLab node (Ubuntu 20.04/22.04 works well). 8 vCPU / 16–32 GB RAM recommended.
- SSH access with sudo privileges.
- A clean kernel (Mininet will install kernel OVS).

> If you’re on a fresh CloudLab image, you’re good—no special image required.

---

## 2) One‑shot setup

This installs **Mininet**, **Open vSwitch**, **Ryu**, and the traffic tools (**nginx**, **hping3**, **tcpreplay**), plus **tmux** for convenience.

```bash
git clone https://github.com/YuMi-coding/rln-dc-ddos-paper
cd rln-dc-ddos-paper

# Run the installer (idempotent)
make setup
# or
bash cloudlab/setup-cloudlab.sh
```

> Tip: If you’ve run Mininet before on this machine, clear anything stale:
>
> ```bash
> sudo mn -c
> ```

---

## 3) Start controller & topology

Start the Ryu controller (in a tmux session) and bring up the **single‑destination tree** topology.

```bash
# 1) Start controller in a tmux session named "ryu"
make ryu

# 2) Launch the topology (k=2, ℓ=3, m=2, n=4 by default)
make tree
```

You’ll drop into the **Mininet CLI** with hosts named like `h1_1_1_1`, …, and a server `srv`. The controller runs at `127.0.0.1:6633`.

> Change the host density `N` by editing `cloudlab/topos/tree_topo.py` (set `N` to `2/4/8/16`).

---

## 4) Topology & link model (matches the paper)

- **Topology:** *Single‑destination* “tree” with parameters `k=2`, `ℓ=3`, `m=2`, `n∈{2,4,8,16}`  
  Total hosts: `N_hosts = k * ℓ * m * n`
- **Link delay:** `10 ms` on **all links**
- **Capacity limit:** only **server ↔ server‑switch** link is rate‑limited to  
  `U_s = N_hosts + 2` (Mbit/s)  
  All other links are effectively unbounded.
- **Agent placement:** the “egress” switches (where hosts attach) are the agent locations.

This is encoded in `cloudlab/topos/tree_topo.py` using Mininet’s `TCLink` for delays and server‑link bandwidth.

---

## 5) Benign traffic (HTTP/TCP)

1. Open a terminal on the server in Mininet:
   ```bash
   xterm srv &
   ```
2. Start HTTP:
   ```bash
   sudo service nginx start
   # (Optional) Put some files into /var/www/html for variety: index.html, 1M.bin, 5M.bin, etc.
   ```
3. On a few host terminals (e.g., `h1_1_1_1`, `h1_1_1_2`, …):
   ```bash
   xterm h1_1_1_1 &
   while true; do curl -s http://10.0.0.1/index.html -o /dev/null; sleep 0.2; done
   ```

> Paper caps benign hosts to ≤ **1 Mbit/s** each. You can enforce caps via `tc` on host interfaces or emulate lighter request loops.

---

## 6) Attack traffic (UDP flood via hping3)

Attackers send **MTU‑sized** UDP packets, congestion‑unaware, with inter‑arrival time
\\( t_{attack} = \\frac{1500 \\cdot 8}{r \\cdot 10^6} \\) seconds for target rate \\( r \\) Mbit/s.

Examples (per attacker host):

```bash
# 5 Mbit/s -> 2400 µs IAT, port 80, MTU-sized payload (1472 B UDP payload)
sudo hping3 --udp --len 1472 -i u2400 -p 80 10.0.0.1

# Alternate: "just go fast" (less precise)
sudo hping3 --faster --udp --len 1472 -p 80 10.0.0.1
```

Try **r in 2.5–6 Mbit/s** (or **4–7 Mbit/s** when benign TCP is active) to reproduce the same stress regime.

---

## 7) Controller behavior (Ryu app)

`cloudlab/ryu/agent_controller.py` provides:

- **L2 learning switch** defaults (quick connectivity)
- A **per‑flow action hook** where you can program `"allow" | "drop" | "rate:mbps"` (rate is a TODO—start with allow/drop)
- Short‑lived flow installs to reduce controller load

Start it with:

```bash
make ryu
# or
ryu-manager cloudlab/ryu/agent_controller.py
```

You can instrument it to log features (IAT, pkt sizes, last action, etc.) or to call into your RL policy.

---

## 8) Verifying and measuring

Useful commands from a shell (outside Mininet) to inspect OVS:

```bash
# Dump flows on the server-side switch
sudo ovs-ofctl dump-flows ss

# See per-port stats
sudo ovs-ofctl dump-ports ss
```

From Mininet CLI, you can also ping, check routes, etc. For throughput, use `curl -w` or `iperf3` if you add it.

---

## 9) Cleanup

```bash
# In Mininet CLI:
exit

# From a regular shell:
make clean        # stops ryu tmux session & mn -c
```

If anything wedges, rebooting the VM is fine; otherwise `sudo mn -c` clears namespaces/OVS state.

---

## 10) Optional: fat‑tree (k=4) topology

The paper also evaluates a **k=4 fat‑tree** (two servers `s0`, `s1`; external hosts connect at core; uniform choice of `s0`/`s1`; `n ∈ {6,12,24,48}` per learner; bandwidth split `U_{s0} = U_{s1} = U_s/2`).  
You can add a `cloudlab/topos/fat_tree_k4.py` later following the same link/delay/capacity rules.

---

## 11) Repro tips

- Keep everything on **one node** initially (controller + Mininet). For scale, run Ryu on a second node and point `RemoteController` to its IP.
- Ensure `xterm` is installed if you’ll use `xterm <host>`:
  ```bash
  sudo apt-get install -y xterm
  ```
- For consistent TCP results, consider pinning nginx workers and disabling TFO (`/proc/sys/net/ipv4/tcp_fastopen`).
- Make sure **only the server link is rate‑limited**; others use delay only.
- Collect logs: nginx access log, controller decisions, OVS counters.

---

## 12) Makefile targets (recap)

```make
setup   # install Mininet/OVS, Ryu, traffic tools
ryu     # start Ryu controller in tmux session "ryu"
tree    # launch the single-destination tree topology
clean   # kill ryu session and run `mn -c`
```

---

## 13) What this replicates

- **Topology:** single‑destination tree (k=2, ℓ=3, m=2, n variable)
- **Delays:** 10 ms on all links
- **Bottleneck:** server‑side link at `U_s = N_hosts + 2` Mbit/s
- **Traffic:** benign HTTP/TCP clients; attackers send MTU‑sized UDP floods with controllable IAT

> This closely mirrors the paper’s Mininet evaluation environment so you can compare policies (your Hostmon/Loris, etc.) on identical network conditions.

---

## 14) License / attribution

This scaffold is intended to live **alongside** the original paper repository. Respect the original project’s license(s) and cite the paper when you publish results.

Happy testing! 🚀
