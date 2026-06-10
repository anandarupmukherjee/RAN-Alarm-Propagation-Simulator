"""
ran_simulation.py
─────────────────
Core simulation engine for the RAN Network Simulator.

Runs as a standalone subprocess managed by sim_server.py.
Attempts to use ns-3 LTE Python bindings; falls back to a calibrated
statistical simulation when bindings are not available.

Events are published to Redis as JSON objects on the key "ns3:events".
Fault injection commands are polled from "/data/commands.json".

Output: JSON lines to stdout (informational logs).
Redis:  JSON objects pushed to list "ns3:events".
"""

import os
import sys
import json
import time
import math
import random
import signal
import threading
import argparse
import redis as redis_lib

# ─────────────────────────────────────────────────────────────────────────────
# Try to import ns-3 Python bindings (cppyy-based, ns-3.36+)
# ─────────────────────────────────────────────────────────────────────────────
NS3_AVAILABLE = False
try:
    # ns-3.41 cppyy bindings flatten every module into a single `ns` object
    # (the ns3 C++ namespace). Submodule names (ns.core, ns.lte, …) are
    # self-redirects, so we alias each module variable to `ns`.
    from ns import ns
    ns_core = ns_net = ns_lte = ns_ptp = ns_mob = ns_inet = ns_apps = ns
    NS3_AVAILABLE = True
    print("[ns3] Python bindings loaded successfully", flush=True)
except Exception as e:
    print(f"[ns3] Bindings unavailable: {e}", flush=True)
    print("[ns3] Falling back to calibrated statistical simulation", flush=True)


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────
COMMANDS_FILE = "/data/commands.json"

# Mapping from simulation event type → alarm category
# The backend's alarm_mapper.py translates these to specific alarm names.
EVENT_TYPES = [
    # (event_type,                  rate_per_sim_hour,  cascade_capable)
    ("backhaul_link_failure",        0.004,              True),
    ("s1_interface_failure",         0.003,              True),
    ("sinr_drop",                    0.025,              False),
    ("cell_blocked",                 0.012,              False),
    ("cell_unavailable",             0.008,              False),
    ("rf_degradation",               0.010,              False),
    ("ald_current",                  0.018,              False),
    ("ald_link_failure",             0.014,              False),
    ("oml_failure",                  0.005,              True),
    ("esl_failure",                  0.004,              False),
    ("ike_failure",                  0.003,              True),
    ("board_hardware",               0.002,              True),
    ("gsm_cell_blocked",             0.020,              False),
    ("gsm_cell_oos",                 0.006,              False),
    ("radio_link_disconnected",      0.005,              False),
    ("cpri_error",                   0.007,              False),
    ("dc_power_failure",             0.004,              False),
    ("license_fault",                0.001,              False),
    ("cell_interference",            0.030,              False),
    # Samples any of the 171 real alarm types (weighted by dataset frequency)
    # so the live console reflects the full historical alarm vocabulary.
    ("dataset_alarm",                0.045,              False),
]

# SINR thresholds (dBm) — crossing generates an interference alarm
SINR_NORMAL_MIN    = -80.0
SINR_NORMAL_MAX    = -60.0
SINR_DEGRADED_THRESHOLD = -90.0

# Fraction of neighbours that receive a cascaded event
CASCADE_PROBABILITY = 0.35


# ─────────────────────────────────────────────────────────────────────────────
# Simulation class
# ─────────────────────────────────────────────────────────────────────────────
class RANSimulation:
    def __init__(self, config: dict, redis_url: str, speed: float = 60.0,
                 propagation_prob: float = CASCADE_PROBABILITY):
        self.config = config
        self.redis_url = redis_url
        self.speed = speed               # sim-hours per real-second
        self.propagation_prob = propagation_prob
        self.running = False
        self._threads: list[threading.Thread] = []

        # Connect to Redis
        self._redis = redis_lib.from_url(redis_url, decode_responses=True)

        # Parse topology
        self.nodes: list[dict] = config.get("nodes", [])
        self.edges: list[dict] = config.get("edges", [])

        # Build adjacency list (bidirectional)
        self.adj: dict[str, list[str]] = {n["id"]: [] for n in self.nodes}
        for e in self.edges:
            src, tgt = e["source"], e["target"]
            if src in self.adj:
                self.adj[src].append(tgt)
            if tgt in self.adj:
                self.adj[tgt].append(src)

        # Per-node SINR state (simulated) — in dBm
        self.sinr: dict[str, float] = {
            n["id"]: random.uniform(SINR_NORMAL_MIN, SINR_NORMAL_MAX)
            for n in self.nodes
        }

        # Simulation clock (sim-hours elapsed)
        self.sim_clock: float = 0.0
        self._lock = threading.Lock()

        # ns-3 PHY state (set up by _init_ns3_phy when bindings are available)
        self.ns3_phy: bool = False
        self._ue_dist: dict[str, float] = {}   # per-node serving-UE distance (m)

    # ── Redis publish ─────────────────────────────────────────────────────────
    def _publish(self, event: dict):
        """Push a JSON event onto the Redis list."""
        try:
            self._redis.rpush("ns3:events", json.dumps(event))
        except Exception as ex:
            print(f"[redis] publish error: {ex}", flush=True)

    # ── Simulation time helpers ──────────────────────────────────────────────
    def _sim_to_real(self, sim_hours: float) -> float:
        """Convert simulation hours to real seconds."""
        return sim_hours / max(self.speed, 0.1)

    # ── Command polling ──────────────────────────────────────────────────────
    def _poll_commands(self):
        """Poll the commands file for manual injections and speed changes."""
        while self.running:
            try:
                if os.path.exists(COMMANDS_FILE):
                    with open(COMMANDS_FILE, "r") as f:
                        cmds = json.load(f)
                    os.remove(COMMANDS_FILE)

                    for cmd in cmds:
                        ctype = cmd.get("type")
                        if ctype == "inject":
                            node_id = cmd.get("node_id")
                            event_type = cmd.get("event_type", "cell_blocked")
                            alarm_name = cmd.get("alarm_name")
                            # A specific alarm name routes through the manual_injection
                            # path so the mapper emits exactly that alarm.
                            if alarm_name:
                                self._fire_event(node_id, "manual_injection",
                                                 metadata={}, source="manual_injection",
                                                 alarm_name=alarm_name)
                            else:
                                self._fire_event(node_id, event_type,
                                                 source="manual_injection")
                            print(f"[cmd] Injected '{alarm_name or event_type}' "
                                  f"on {node_id}", flush=True)
                        elif ctype == "set_speed":
                            new_speed = float(cmd.get("speed", self.speed))
                            with self._lock:
                                self.speed = new_speed
                            print(f"[cmd] Speed updated to {new_speed}×", flush=True)
                        elif ctype == "link_failure":
                            src = cmd.get("source")
                            tgt = cmd.get("target")
                            self._fire_link_failure(src, tgt)
                            print(f"[cmd] Link failure injected: {src}—{tgt}", flush=True)

            except Exception as ex:
                print(f"[cmd] Error polling commands: {ex}", flush=True)

            time.sleep(0.5)

    # ── Event firing ─────────────────────────────────────────────────────────
    def _fire_event(self, node_id: str, event_type: str,
                    metadata: dict | None = None,
                    source: str = "simulation",
                    cascade_depth: int = 0,
                    alarm_name: str | None = None):
        """Build and publish a single network event."""
        with self._lock:
            clock = self.sim_clock

        event = {
            "event_type":    event_type,
            "node_id":       node_id,
            "source":        source,
            "cascade_depth": cascade_depth,
            "sim_time":      clock,
            "real_time":     time.time(),
            "metadata":      metadata or {},
            "ns3":           self.ns3_phy,
            # SINR snapshot for this node (dBm)
            "sinr_dbm":      round(self.sinr.get(node_id, -75.0), 2),
        }
        if alarm_name:
            event["alarm_name"] = alarm_name
        self._publish(event)

    def _fire_link_failure(self, src: str, tgt: str):
        """Simulate a backhaul link failure between two nodes."""
        self._fire_event(src, "backhaul_link_failure",
                         metadata={"peer": tgt, "link_type": "backhaul"},
                         cascade_depth=0)
        # The target loses its S1 interface
        self._fire_event(tgt, "s1_interface_failure",
                         metadata={"cause": f"backhaul_loss_from_{src}"},
                         source="cascade",
                         cascade_depth=1)
        # Degrade SINR on target (backhaul issue → increased interference)
        self.sinr[tgt] = SINR_DEGRADED_THRESHOLD - random.uniform(0, 10)

    def _maybe_cascade(self, node_id: str, event_type: str, cascade_depth: int):
        """Propagate an event to connected neighbours with some probability."""
        if cascade_depth >= 2:
            return
        neighbours = self.adj.get(node_id, [])
        for nb in neighbours:
            if random.random() < self.propagation_prob:
                # Cascaded event type on neighbour
                cascaded = event_type
                if event_type == "backhaul_link_failure":
                    cascaded = "s1_interface_failure"
                elif event_type == "oml_failure":
                    cascaded = "esl_failure"
                elif event_type == "board_hardware":
                    cascaded = "oml_failure"

                real_delay = self._sim_to_real(random.uniform(0.02, 0.3))
                threading.Timer(
                    real_delay,
                    self._fire_event,
                    args=(nb, cascaded),
                    kwargs={"metadata": {"cause": f"cascade_from_{node_id}"},
                            "source": "cascade",
                            "cascade_depth": cascade_depth + 1}
                ).start()

    # ── SINR drift ────────────────────────────────────────────────────────────
    def _sinr_drift_loop(self):
        """Refresh each node's received power and raise an interference alarm on a
        downward threshold crossing. With ns-3 PHY active the value is computed by
        the ns-3 propagation model; otherwise it's a mean-reverting random walk."""
        while self.running:
            for node in self.nodes:
                nid = node["id"]
                prev = self.sinr[nid]

                if self.ns3_phy:
                    # Genuine ns-3 propagation-model received power (dBm)
                    self.sinr[nid] = max(-120.0, min(-40.0, self._ns3_serving_rsrp(nid)))
                else:
                    # Random walk with mean reversion toward -70 dBm
                    target = -70.0
                    drift  = random.gauss(0, 2.5) + 0.05 * (target - prev)
                    self.sinr[nid] = max(-120.0, min(-40.0, prev + drift))

                # Threshold crossing → interference alarm
                if prev > SINR_DEGRADED_THRESHOLD >= self.sinr[nid]:
                    meta = {"sinr_before": round(prev, 2),
                            "sinr_after":  round(self.sinr[nid], 2),
                            "threshold":   SINR_DEGRADED_THRESHOLD}
                    if self.ns3_phy:
                        meta["sinr_db"] = round(self._ns3_sinr_db(nid), 2)
                    self._fire_event(nid, "sinr_drop", metadata=meta)

            # Refresh every ~5 real seconds (represents time compression)
            time.sleep(5.0)

    # ── Statistical simulation (ns-3 fallback) ───────────────────────────────
    def _statistical_node_loop(self, node: dict):
        """Independent Poisson-process event loop for one eNodeB."""
        node_id = node["id"]
        # Stagger start so not all nodes fire simultaneously
        time.sleep(random.uniform(0, 3.0))

        # Build per-node event rate (vary slightly per node for realism)
        node_rates = [
            (et, rate * random.uniform(0.7, 1.3), cascade)
            for et, rate, cascade in EVENT_TYPES
        ]

        # Weighted event selection
        weights = [r for _, r, _ in node_rates]
        total_rate_per_sim_hour = sum(weights)

        while self.running:
            # Inter-event time: exponential with total rate
            mean_sim_hours = 1.0 / total_rate_per_sim_hour
            inter_sim_hours = random.expovariate(1.0 / mean_sim_hours)
            real_wait = self._sim_to_real(inter_sim_hours)

            # Cap wait: never longer than 15 real-seconds per node
            real_wait = min(real_wait, 15.0)
            time.sleep(real_wait)
            if not self.running:
                break

            # Update sim clock
            with self._lock:
                self.sim_clock += inter_sim_hours

            # Pick event type proportional to rate
            idx = random.choices(
                range(len(node_rates)),
                weights=weights,
                k=1
            )[0]
            event_type, _, cascade_capable = node_rates[idx]

            # Build metadata based on event type
            metadata = self._build_metadata(node, event_type)
            self._fire_event(node_id, event_type, metadata=metadata)

            # Cascade if applicable
            if cascade_capable:
                self._maybe_cascade(node_id, event_type, cascade_depth=0)

    def _build_metadata(self, node: dict, event_type: str) -> dict:
        """Generate realistic-looking metadata for an event."""
        meta: dict = {}
        ne_type = node.get("ne_type", "BTS3900 LTE")

        if event_type in ("sinr_drop", "cell_interference"):
            meta["sinr_dbm"]        = round(self.sinr.get(node["id"], -90), 2)
            meta["threshold_dbm"]   = SINR_DEGRADED_THRESHOLD
            meta["sector"]          = random.randint(1, 3)

        elif event_type in ("ald_current", "ald_link_failure"):
            meta["ald_current_ma"]  = random.choice([0, 65535])
            meta["device_type"]     = "TMA"
            meta["device_no"]       = random.randint(1, 40)
            meta["specific_problem"]= random.choice(["Disconnection Protection",
                                                      "Overcurrent Protection"])

        elif event_type == "cell_blocked":
            meta["cell_id"]         = random.randint(0, 15)
            meta["specific_problem"]= "Cell Blocked"
            meta["fdd_tdd"]         = "FDD"

        elif event_type in ("backhaul_link_failure", "ethernet_link"):
            meta["port_no"]         = 0
            meta["specific_problem"]= "Ethernet Link Fault"

        elif event_type == "board_hardware":
            meta["board_type"]      = random.choice(["GTMU", "UBBP", "MRFU"])
            meta["slot_no"]         = random.randint(0, 7)
            meta["cabinet_no"]      = 0

        elif event_type == "cpri_error":
            meta["board_type"]      = "UBBP"
            meta["port_no"]         = random.randint(0, 3)
            meta["specific_problem"]= "CPRI Interface Reception Error"

        elif event_type == "dc_power_failure":
            meta["input_voltage"]   = random.randint(380, 450)
            meta["specific_problem"]= "Undervoltage"

        return meta

    # ── ns-3 radio PHY engine (hybrid) ─────────────────────────────────────────
    # Rather than driving alarm *timing* through ns-3's event scheduler (which
    # can't accept Python callbacks via cppyy), we use ns-3 as a physics engine:
    # a real ns-3 propagation loss model computes received power / SINR per cell
    # from genuine eNB/UE geometry. The real-time-paced Python loop consumes those
    # ns-3-computed values to drive interference alarms and SINR metadata.
    def _init_ns3_phy(self):
        """Build the ns-3 radio scenario: a propagation loss model plus a
        ConstantPosition mobility model per eNodeB at its topology coordinates."""
        # LogDistance path loss tuned for an urban macro cell (~2.6 GHz @ 1 m)
        self._loss = ns_core.CreateObject("LogDistancePropagationLossModel")
        self._loss.SetAttribute("Exponent",      ns_core.DoubleValue(3.5))
        self._loss.SetAttribute("ReferenceLoss",  ns_core.DoubleValue(46.7))

        self._enb_mob = []
        self._enb_idx = {}
        for idx, node in enumerate(self.nodes):
            m = ns_core.CreateObject("ConstantPositionMobilityModel")
            x = float(node.get("x", 0)) * 5.0   # canvas px → metres
            y = float(node.get("y", 0)) * 5.0
            m.SetPosition(ns_core.Vector(x, y, 30.0))   # 30 m mast
            self._enb_mob.append(m)
            self._enb_idx[node["id"]] = idx

        # One reusable UE mobility model we reposition on demand
        self._ue_mob   = ns_core.CreateObject("ConstantPositionMobilityModel")
        self._tx_dbm   = 43.0      # eNB downlink Tx power (≈20 W)
        self._noise_dbm = -104.0   # thermal noise over ~5 MHz

        print(f"[ns3] PHY scenario built: {len(self._enb_mob)} eNBs, "
              f"LogDistance path loss (exponent 3.5) — alarms are ns-3-backed",
              flush=True)

    def _ns3_serving_rsrp(self, node_id: str) -> float:
        """Compute the serving-cell received power (dBm) for this eNodeB using the
        ns-3 propagation model, with a drifting UE position for temporal variation."""
        i   = self._enb_idx[node_id]
        enb = self._enb_mob[i]
        ep  = enb.GetPosition()

        # Mean-reverting random walk of the serving UE distance (40–750 m)
        d = self._ue_dist.get(node_id, 200.0)
        d = max(40.0, min(750.0, d + random.gauss(0, 45) + 0.05 * (200.0 - d)))
        self._ue_dist[node_id] = d
        ang = random.uniform(0, 2 * math.pi)
        self._ue_mob.SetPosition(
            ns_core.Vector(ep.x + d * math.cos(ang), ep.y + d * math.sin(ang), 1.5))

        return float(self._loss.CalcRxPower(self._tx_dbm, enb, self._ue_mob))

    def _ns3_sinr_db(self, node_id: str) -> float:
        """True downlink SINR (dB): serving power vs. interference from the
        strongest neighbouring eNodeBs plus thermal noise — all ns-3-computed."""
        i = self._enb_idx[node_id]
        serving_dbm = self._ns3_serving_rsrp(node_id)   # also repositions the UE
        interf_lin = 0.0
        for j, m in enumerate(self._enb_mob):
            if j == i:
                continue
            rx = float(self._loss.CalcRxPower(self._tx_dbm, m, self._ue_mob))
            interf_lin += 10 ** (rx / 10.0)
        noise_lin = 10 ** (self._noise_dbm / 10.0)
        return serving_dbm - 10 * math.log10(interf_lin + noise_lin)

    # ── Main run loop ─────────────────────────────────────────────────────────
    def run(self):
        """Start the simulation. Blocks until self.running is False."""
        self.running = True

        # Try to stand up the ns-3 radio PHY; fall back cleanly if anything fails
        if NS3_AVAILABLE:
            try:
                self._init_ns3_phy()
                self.ns3_phy = True
                # Seed each node's received power from the ns-3 model
                for node in self.nodes:
                    self.sinr[node["id"]] = max(-120.0, min(-40.0,
                                                 self._ns3_serving_rsrp(node["id"])))
            except Exception as e:
                self.ns3_phy = False
                print(f"[ns3] PHY init failed, using statistical SINR: {e}", flush=True)

        mode = "ns-3 PHY + paced events" if self.ns3_phy else "statistical"
        print(f"[sim] Starting {mode} simulation: {len(self.nodes)} nodes, "
              f"speed={self.speed}×", flush=True)

        # Command polling thread
        cmd_t = threading.Thread(target=self._poll_commands, daemon=True)
        cmd_t.start()
        self._threads.append(cmd_t)

        # Received-power / SINR refresh thread (ns-3-backed when available)
        sinr_t = threading.Thread(target=self._sinr_drift_loop, daemon=True)
        sinr_t.start()
        self._threads.append(sinr_t)

        # Real-time-paced per-node event loops (used in both modes)
        for node in self.nodes:
            t = threading.Thread(target=self._statistical_node_loop,
                                  args=(node,), daemon=True)
            t.start()
            self._threads.append(t)

        # Keep running until stopped
        while self.running:
            time.sleep(1.0)

    def stop(self):
        self.running = False
        print("[sim] Stopped", flush=True)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point (called as subprocess by sim_server.py)
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RAN ns-3 Simulation Worker")
    parser.add_argument("--config",  default="/data/topology.json",
                        help="Path to topology JSON config file")
    parser.add_argument("--speed",   type=float, default=60.0,
                        help="Simulation hours per real second")
    parser.add_argument("--redis",   default=os.getenv("REDIS_URL", "redis://localhost:6379"),
                        help="Redis URL")
    parser.add_argument("--cascade", type=float, default=CASCADE_PROBABILITY,
                        help="Cascade probability (0–1)")
    args = parser.parse_args()

    # Load topology
    with open(args.config, "r") as f:
        config = json.load(f)

    sim = RANSimulation(
        config=config,
        redis_url=args.redis,
        speed=args.speed,
        propagation_prob=args.cascade,
    )

    # Graceful shutdown on SIGTERM / SIGINT
    def _sighandler(signum, frame):
        sim.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _sighandler)
    signal.signal(signal.SIGINT,  _sighandler)

    sim.run()
