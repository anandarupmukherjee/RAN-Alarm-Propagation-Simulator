#!/usr/bin/env python3
"""
batch_export.py
───────────────
Headless batch exporter for the RAN Alarm Propagation Simulator. It runs the
ns-3 LTE core in deterministic (non-realtime) batch mode, enriches the emitted
events with the existing AlarmMapper, and writes a BT-comparable alarms.csv.

Phase 1 scope: the 13-field alarm record (+ episode_id) → exports/run_<id>/alarms.csv.
Episodes/calendar (Phase 2), ground-truth companion files (Phase 3) and confounder
scenarios (Phase 4) build on the structures captured here.

The exporter is dependency-light (no pandas) so it runs inside the ns3-sim
container alongside the compiled binary. Determinism: ns-3 RngRun + seeded Python
RNGs + stable sort ⇒ byte-identical alarms.csv for a given config + base_seed.

Run (inside ns3-sim container):
  LD_LIBRARY_PATH=/ns3/build/lib python3 batch_export.py \
      --config export.json --mapper alarm_mapper.py --stats mapper_stats.json \
      --bin /ns3/build/scratch/ns3.41-ran-alarm-sim-optimized --out exports
"""

import argparse
import datetime as dt
import importlib.util
import json
import math
import os
import random
import subprocess
import sys
import tempfile

# ─── Alarm record schema (BT Table 1) ───────────────────────────────────────
# episode_id is an extra leading column (per configuration); the 13 BT fields follow.
ALARM_COLUMNS = [
    "episode_id",
    "alarm_severity", "alarm_name", "source_bs_id", "source_bs_type",
    "alarm_location_info", "occurred_timestamp", "cleared_timestamp",
    "acknowledged_timestamp", "cleared_status", "acknowledged_status",
    "alarm_log_serial", "equipment_alarm_serial", "bs_maintenance_status",
]
SEVERITIES = {"Critical", "Major", "Minor", "Warning"}
TRANSPORT_FAULT_CLASSES = {"backhaul_cut", "x2_cut"}


# ─── Topology builders (server-side; mirror the frontend registry minimally) ──
DEMO_POSITIONS = {
    "10006": (350, 100), "10010": (600, 200), "10011": (860, 100), "10012": (600, 400),
    "10014": (880, 310), "10015": (600, 600), "10016": (860, 520), "10018": (600, 760),
    "10019": (250, 500), "10020": (300, 310),
}
DEMO_EDGES = [("10006", "10010"), ("10010", "10011"), ("10010", "10012"), ("10006", "10020"),
              ("10020", "10019"), ("10011", "10014"), ("10014", "10016"), ("10012", "10015"),
              ("10015", "10018"), ("10015", "10016"), ("10012", "10014"), ("10019", "10015")]
ACCESS_MIX = ["BTS3900 LTE", "BTS3900 LTE", "BTS5900 5G", "BTS3900 GSM", "RRU3953"]


def build_topology(name):
    if name == "demo":
        nodes = [{"id": k, "x": x, "y": y, "ne_type": "BTS3900 LTE"}
                 for k, (x, y) in sorted(DEMO_POSITIONS.items())]
        edges = [{"a": a, "b": b} for a, b in DEMO_EDGES]
        return {"nodes": nodes, "edges": edges}
    if name == "town":
        nodes, edges = [], []
        cx, cy, hubR, accR, hubs, acc = 800, 700, 380, 230, 2, 9
        nodes.append({"id": "CORE-1", "x": cx, "y": cy, "ne_type": "NE40E (Core)"})
        for h in range(hubs):
            ha = 2 * math.pi * h / hubs
            hx, hy = cx + hubR * math.cos(ha), cy + hubR * math.sin(ha)
            hub = f"AGG-1-{h+1}"
            nodes.append({"id": hub, "x": hx, "y": hy, "ne_type": "ATN 910 (Agg)"})
            edges.append({"a": "CORE-1", "b": hub})
            edges.append({"a": hub, "b": f"AGG-1-{(h+1) % hubs + 1}"})
            for s in range(acc):
                sa = 2 * math.pi * s / acc
                eid = f"ENB-{h+1}{s+1:02d}"
                nodes.append({"id": eid, "x": hx + accR * math.cos(sa), "y": hy + accR * math.sin(sa),
                              "ne_type": ACCESS_MIX[s % len(ACCESS_MIX)]})
                edges.append({"a": hub, "b": eid})
        # de-dupe edges
        seen, uniq = set(), []
        for e in edges:
            k = tuple(sorted((e["a"], e["b"])))
            if k not in seen:
                seen.add(k); uniq.append(e)
        return {"nodes": nodes, "edges": uniq}
    raise ValueError(f"unknown topology '{name}'")


def ue_per_enb(n_nodes):
    return 4 if n_nodes <= 20 else 3 if n_nodes <= 60 else 2 if n_nodes <= 120 else 1


# ─── ns-3 core invocation ────────────────────────────────────────────────────
def diurnal_ue_speed(cfg, hour):
    """Per-episode UE mobility intensity modulated by the anchor hour, as a proxy
    for diurnal network load: busier hours → faster/more-mobile UEs → more
    handover/RLF activity → more alarms. `diurnal_load_weights` is an optional
    24-vector of relative loads; null/absent ⇒ no modulation (base speed)."""
    base = float(cfg.get("ue_speed", 45))
    w = cfg.get("diurnal_load_weights")
    if not w or len(w) != 24:
        return base
    mean = sum(w) / 24.0
    if mean <= 0:
        return base
    factor = max(0.5, min(2.0, w[hour] / mean))   # clamp to ±2× for stability
    return round(base * factor, 2)


def write_scenario(path, topo, ue_speed, churn_holdoff):
    n = len(topo["nodes"])
    with open(path, "w") as f:
        for nd in topo["nodes"]:
            f.write(f"NODE {nd['id']} {nd['x']} {nd['y']}\n")
        for e in topo["edges"]:
            f.write(f"EDGE {e['a']} {e['b']}\n")
        f.write(f"UEPERENB {ue_per_enb(n)}\n")
        f.write(f"UESPEED {ue_speed}\n")
        f.write("SPEED 30\n")
        f.write(f"CHURNHOLDOFF {churn_holdoff}\n")
        f.write("REALTIME 0\n")


def select_connected_cluster(topo, k, rng):
    """Grow a connected sub-graph of k nodes from a random seed node."""
    adj = {nd["id"]: set() for nd in topo["nodes"]}
    for e in topo["edges"]:
        adj[e["a"]].add(e["b"]); adj[e["b"]].add(e["a"])
    start = rng.choice([nd["id"] for nd in topo["nodes"]])
    cluster, seen, frontier = [start], {start}, list(adj[start])
    while len(cluster) < k and frontier:
        nxt = frontier.pop(rng.randrange(len(frontier)))
        if nxt in seen:
            continue
        seen.add(nxt); cluster.append(nxt)
        frontier.extend(nb for nb in adj[nxt] if nb not in seen)
    return cluster


def build_burst_pool(mapper, severity_weights=None):
    """Injection-only label alarms from the Hardware + Power & Environment
    catalogue categories, for maintenance bursts. Sampling weight = dataset
    frequency × severity_weights[severity] so the mix can be biased toward rarer
    severities (NB: these categories contain no Critical alarms, so the bias can
    only reach the Warning/Minor names that exist there)."""
    sw = severity_weights or {}
    cat = mapper.get_alarm_catalog()
    pool = []
    for c in cat["categories"]:
        if c["name"] in ("Hardware", "Power & Environment"):
            for a in c["alarms"]:
                w = max(1, int(a["count"])) * float(sw.get(a["severity"], 1.0))
                pool.append((a["name"], w))
    return pool or [("Board Hardware Fault", 1.0)]


def build_scenario(cfg, topo, sim_t, seed, ep):
    """Return (fault_defs, cpp_faults_file_lines). fault_defs is authoritative for
    fault_injections.csv; only C++-handled classes go into the faults file.
    Fault ids are episode-unique (e<ep>f<NNN>)."""
    rng = random.Random(seed ^ 0xFA17)
    scenario = cfg.get("scenario", "clean")
    fs = cfg["fault_schedule"]
    lo, hi = fs["window"][0], sim_t - fs["window"][1]
    dur = fs["duration_s"]
    site_ids = [nd["id"] for nd in topo["nodes"]]
    edges = [(e["a"], e["b"]) for e in topo["edges"]]
    defs, lines, seq = [], [], 0

    def add(klass, targets, t0=None, t1=None, group_id="", cpp=True, n_burst=0):
        nonlocal seq
        seq += 1
        fid = f"e{ep}f{seq:03d}"
        if t0 is None:
            t0 = round(rng.uniform(lo, max(lo, hi)), 2)
            t1 = round(min(t0 + dur, sim_t - 1), 2)
        defs.append({"fault_id": fid, "fault_class": klass, "targets": targets,
                     "inject_sim_time": t0, "remove_sim_time": t1, "group_id": group_id,
                     "cpp": cpp, "n_burst": n_burst})
        if cpp:
            lines.append(f"inject {t0} {fid} {klass} {' '.join(targets)}")
            lines.append(f"remove {t1} {fid}")

    # Clean background single-element faults (genuine propagation) — all scenarios.
    for _ in range(fs.get("n_backhaul_cuts", 0)):
        add("backhaul_cut", [rng.choice(site_ids)])
    for _ in range(fs.get("n_x2_cuts", 0)):
        a, b = rng.choice(edges); add("x2_cut", [a, b])
    for _ in range(fs.get("n_radio_blackouts", 0)):
        add("radio_blackout", [rng.choice(site_ids)])

    if scenario == "regional_power":
        rp = cfg["regional_power"]
        cluster = select_connected_cluster(topo, rp.get("cluster_size", 5), rng)
        t0 = float(rp.get("at_sim_time", 40))
        t1 = round(min(t0 + rp.get("duration_s", 12), sim_t - 1), 2)
        gid = f"g_power_e{ep}"
        for node in cluster:                       # simultaneous shared-cause event
            add("radio_blackout", [node], t0=t0, t1=t1, group_id=gid)
    elif scenario == "maintenance":
        mt = cfg["maintenance"]
        targets = rng.sample(site_ids, min(mt.get("n_nodes", 2), len(site_ids)))
        ws = float(mt.get("window_start", 20))
        we = round(min(ws + mt.get("window_duration_s", 60), sim_t - 1), 2)
        for node in targets:                       # Python-only label bursts
            add("maintenance", [node], t0=ws, t1=we, cpp=False, n_burst=mt.get("n_burst_alarms", 6))
    return defs, lines


def run_core(binary, scenario_file, faults_file, seed, sim_t, grace):
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = "/ns3/build/lib:" + env.get("LD_LIBRARY_PATH", "")
    cmd = [binary, f"--scenario={scenario_file}", f"--seed={seed}",
           f"--simTime={sim_t}", "--realtime=0", f"--grace={grace}"]
    if faults_file:
        cmd.append(f"--faults={faults_file}")
    p = subprocess.run(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       timeout=max(600, int(sim_t) * 30))
    return p.stdout.decode("utf-8", "replace").splitlines()


def parse_core_output(lines):
    """Return (raw_alarms, faults_observed) from the ns-3 stdout."""
    alarms, faults = [], {}
    for ln in lines:
        if ln.startswith("@@ALARM@@ "):
            try:
                alarms.append(json.loads(ln[len("@@ALARM@@ "):]))
            except Exception:
                pass
        elif ln.startswith("@@FAULT@@ "):
            toks = ln[len("@@FAULT@@ "):].split()
            # phase fid class <target...> sim_time
            phase, fid, klass = toks[0], toks[1], toks[2]
            sim_time = float(toks[-1])
            target = toks[3:-1]
            f = faults.setdefault(fid, {"fault_id": fid, "fault_class": klass, "targets": target})
            f[f"{phase}_sim_time"] = sim_time
            if target:
                f["targets"] = target
    return alarms, faults


# ─── Clear / acknowledge sampling (synthesised log-normal per severity) ──────
def lognorm_seconds(rng, params):
    mu = math.log(max(params["median"], 1e-6))
    return math.exp(rng.gauss(mu, params["sigma"]))


# ─── Episode pipeline ────────────────────────────────────────────────────────
def run_episode(ep_index, anchor, seed, cfg, mapper, binary, anon, workdir, burst_pool):
    topo = build_topology(cfg["topology"])
    sim_t = float(cfg["sim_seconds_per_episode"])

    scen = os.path.join(workdir, f"scenario_{ep_index}.txt")
    fault_f = os.path.join(workdir, f"faults_{ep_index}.txt")
    ue_speed = diurnal_ue_speed(cfg, anchor.hour)     # diurnal activity modulation
    write_scenario(scen, topo, ue_speed, cfg.get("churn_holdoff_s", 4))

    fault_defs, fault_lines = build_scenario(cfg, topo, sim_t, seed, ep_index)
    with open(fault_f, "w") as f:
        f.write("\n".join(fault_lines) + ("\n" if fault_lines else ""))

    lines = run_core(binary, scen, fault_f, seed, sim_t, cfg.get("grace_period_s", 5))
    raw_alarms, faults_obs = parse_core_output(lines)

    # Class/remove times: fault_defs is authoritative; refine with observed C++ times.
    fault_class = {d["fault_id"]: d["fault_class"] for d in fault_defs}
    remove_sim = {d["fault_id"]: d["remove_sim_time"] for d in fault_defs}
    for fid, f in faults_obs.items():
        if f.get("remove_sim_time") is not None:
            remove_sim[fid] = f["remove_sim_time"]

    # Maintenance: generate injection-only label bursts (Python-side) on the
    # maintenance nodes within their windows, attributed to the maintenance fault.
    maint = [d for d in fault_defs if d["fault_class"] == "maintenance"]
    maint_by_node = {d["targets"][0]: d for d in maint}
    burst_rng = random.Random(seed ^ 0x0B1175)
    names, weights = (list(zip(*burst_pool)) if burst_pool else (["Board Hardware Fault"], [1]))
    for d in maint:
        node, ws, we, fid = d["targets"][0], d["inject_sim_time"], d["remove_sim_time"], d["fault_id"]
        for _ in range(int(d.get("n_burst", 0))):
            name = burst_rng.choices(names, weights=weights, k=1)[0]
            t = ws + burst_rng.uniform(0, max(0.1, we - ws))
            raw_alarms.append({"event_type": "manual_injection", "alarm_name": name,
                               "node_id": node, "sim_seconds": t,
                               "attributed_fault": fid, "ns3_event_type": "MaintenanceBurst"})

    # Deterministic enrichment + clear/ack
    random.seed(seed)                      # AlarmMapper uses the global RNG
    ca_rng = random.Random(seed ^ 0xC1EA12)
    horizon = anchor + dt.timedelta(seconds=sim_t)
    clear_p = cfg["clear_delay_lognorm_s"]
    ack_p = cfg["ack_delay_lognorm_s"]

    def under_maint(node, sim_s):
        d = maint_by_node.get(node)
        return bool(d and d["inject_sim_time"] <= sim_s <= d["remove_sim_time"])

    records, provenance = [], []
    for ev in raw_alarms:
        enr = mapper.map_event(ev)
        if enr is None:
            continue
        node = str(ev.get("node_id"))
        sev = enr["severity"] if enr["severity"] in SEVERITIES else "Major"
        sim_s = float(ev.get("sim_seconds", ev.get("sim_time", 0.0) * 3600.0))
        occurred = anchor + dt.timedelta(seconds=sim_s)
        fid = ev.get("attributed_fault", "organic")

        # cleared — mechanistic for transport faults removed at/after the alarm,
        # otherwise (incl. grace-window alarms that fired after removal) synthesised.
        cleared_dt = None
        mech = None
        if fid != "organic" and fault_class.get(fid) in TRANSPORT_FAULT_CLASSES and remove_sim.get(fid) is not None:
            cand = anchor + dt.timedelta(seconds=remove_sim[fid])
            if cand >= occurred:
                mech = cand
        if mech is not None:
            cleared_dt = mech if mech <= horizon else None
        else:
            d = lognorm_seconds(ca_rng, clear_p.get(sev, clear_p["Major"]))
            c = occurred + dt.timedelta(seconds=d)
            cleared_dt = c if c <= horizon else None
        # acknowledged (independent of clear)
        a = occurred + dt.timedelta(seconds=lognorm_seconds(ca_rng, ack_p.get(sev, ack_p["Major"])))
        ack_dt = a if a <= horizon else None

        rec = {
            "episode_id": ep_index,
            "alarm_severity": sev,
            "alarm_name": enr["alarm_name"],
            "source_bs_id": anon[node],
            "source_bs_type": enr["ne_type"],
            "alarm_location_info": enr["location"],
            "occurred_timestamp": iso(occurred),
            "cleared_timestamp": iso(cleared_dt) if cleared_dt else "",
            "acknowledged_timestamp": iso(ack_dt) if ack_dt else "",
            "cleared_status": "cleared" if cleared_dt else "uncleared",
            "acknowledged_status": "acknowledged" if ack_dt else "unacknowledged",
            "bs_maintenance_status": "under_maintenance" if under_maint(node, sim_s) else "in_service",
            # internal (not exported in alarms.csv; used by Phase 3 ground-truth)
            "_sim_seconds": sim_s, "_occurred_dt": occurred, "_node": node,
            "_fault_id": fid, "_ns3_event_type": ev.get("ns3_event_type", ""),
        }
        records.append(rec)
        provenance.append({"episode_id": ep_index, "_node": node, "fault_id": fid,
                           "ns3_event_type": ev.get("ns3_event_type", ""), "sim_time": sim_s})

    return records, fault_defs, faults_obs, provenance, topo


def iso(d):
    return d.astimezone(dt.timezone.utc).isoformat()


def anonymise(topo):
    ids = sorted(nd["id"] for nd in topo["nodes"])
    return {nid: f"s{ix:04d}" for ix, nid in enumerate(ids)}


def _weighted_hour(rng, weights):
    if not weights:
        return rng.randrange(24)
    tot = sum(weights)
    x = rng.uniform(0, tot)
    acc = 0.0
    for h, w in enumerate(weights):
        acc += w
        if x <= acc:
            return h
    return 23


def assign_anchors(cfg, n_episodes):
    """Deterministically place each episode on the calendar (EPISODE-PLACEMENT).

    Weekday is round-robin (ep % 7) so all 7 weekdays are covered once ≥7 episodes
    exist; the week within the span and the hour (uniform or anchor_hour_weights)
    are sampled from a seeded RNG. Within an episode, sim-seconds map 1:1 to
    calendar seconds from the anchor (cascade delays are preserved, not stretched).
    """
    start = dt.datetime.fromisoformat(cfg["calendar_start"].replace("Z", "+00:00"))
    span_weeks = max(1, int(cfg["calendar_span_weeks"]))
    span_days = span_weeks * 7
    start_wd = start.weekday()
    weights = cfg.get("anchor_hour_weights")
    rng = random.Random(int(cfg["base_seed"]) ^ 0xCA1EDA)
    anchors = []
    for ep in range(n_episodes):
        wd = ep % 7
        week = rng.randrange(span_weeks)
        day_off = min(week * 7 + ((wd - start_wd) % 7), span_days - 1)
        hour = _weighted_hour(rng, weights)
        minute = rng.randrange(60)
        anchor = (start + dt.timedelta(days=day_off)).replace(
            hour=hour, minute=minute, second=0, microsecond=0)
        anchors.append({"episode_id": ep, "seed": int(cfg["base_seed"]) + ep, "anchor": anchor})
    return anchors


def load_mapper(mapper_py, stats_json):
    spec = importlib.util.spec_from_file_location("alarm_mapper", mapper_py)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.AlarmMapper(csv_path="", stats_path=stats_json)


def _verify_mapping(records, anchors):
    """Demonstrate (a) anchors span multiple weekdays/hours, (b) within an episode
    calendar gaps equal sim-time gaps (1:1, cascade delays preserved)."""
    wds = sorted({a["anchor"].strftime("%a") for a in anchors})
    hrs = sorted({a["anchor"].hour for a in anchors})
    print("\n[verify] episode anchors:", file=sys.stderr)
    for a in anchors:
        print(f"   ep{a['episode_id']}: {iso(a['anchor'])}  "
              f"weekday={a['anchor'].strftime('%A')} hour={a['anchor'].hour:02d}", file=sys.stderr)
    print(f"[verify] distinct weekdays covered: {wds}", file=sys.stderr)
    print(f"[verify] distinct hours covered:    {hrs}", file=sys.stderr)

    print("[verify] within-episode 1:1 mapping (calendar gap == sim-time gap):", file=sys.stderr)
    max_err = 0.0
    for a in anchors:
        ep = [r for r in records if r["episode_id"] == a["episode_id"]]
        ep.sort(key=lambda r: r["_sim_seconds"])
        shown = 0
        for i in range(1, len(ep)):
            sim_gap = ep[i]["_sim_seconds"] - ep[i - 1]["_sim_seconds"]
            cal_gap = (ep[i]["_occurred_dt"] - ep[i - 1]["_occurred_dt"]).total_seconds()
            max_err = max(max_err, abs(sim_gap - cal_gap))
            if shown < 3 and sim_gap > 0.001:
                print(f"   ep{a['episode_id']}: sim_gap={sim_gap:.4f}s  cal_gap={cal_gap:.4f}s  equal={abs(sim_gap-cal_gap)<1e-6}", file=sys.stderr)
                shown += 1
    print(f"[verify] max |sim_gap - cal_gap| across all pairs = {max_err:.2e}s "
          f"(0 ⇒ exact 1:1, cascade delays preserved)", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--mapper", required=True, help="path to alarm_mapper.py")
    ap.add_argument("--stats", required=True, help="path to mapper_stats.json")
    ap.add_argument("--bin", required=True, help="path to ns-3 ran-alarm-sim binary")
    ap.add_argument("--out", default=None, help="export base dir (overrides config)")
    ap.add_argument("--run-id", default="000")
    ap.add_argument("--episodes", type=int, default=None, help="override export.episodes")
    ap.add_argument("--sim-seconds", type=float, default=None, help="override sim_seconds_per_episode")
    ap.add_argument("--verify", action="store_true", help="print episode-placement + 1:1 mapping diagnostics")
    ap.add_argument("--git-commit", default="unknown", help="simulator git commit hash (for manifest)")
    ap.add_argument("--scenario", default=None, help="override export.scenario (clean/regional_power/maintenance)")
    args = ap.parse_args()

    cfg = json.load(open(args.config))["export"]
    if args.episodes is not None:
        cfg["episodes"] = args.episodes
    if args.sim_seconds is not None:
        cfg["sim_seconds_per_episode"] = args.sim_seconds
    if args.scenario is not None:
        cfg["scenario"] = args.scenario
    mapper = load_mapper(args.mapper, args.stats)
    out_base = args.out or cfg.get("export_dir", "exports")
    run_dir = os.path.join(out_base, f"run_{args.run_id}")
    os.makedirs(run_dir, exist_ok=True)

    topo0 = build_topology(cfg["topology"])
    anon = anonymise(topo0)
    anchors = assign_anchors(cfg, int(cfg["episodes"]))

    burst_pool = build_burst_pool(mapper, cfg.get("maintenance", {}).get("severity_weights"))
    all_records, faults_all, topo_rows, topo_edge_rows, maint_windows = [], [], [], [], []
    with tempfile.TemporaryDirectory() as workdir:
        for a in anchors:
            recs, fdefs, fobs, prov, topo = run_episode(
                a["episode_id"], a["anchor"], a["seed"], cfg, mapper, args.bin, anon, workdir, burst_pool)
            all_records.extend(recs)
            for d in fdefs:                       # fault_defs is authoritative
                fid = d["fault_id"]
                obs = fobs.get(fid, {})
                inj = obs.get("inject_sim_time", d["inject_sim_time"])
                rem = obs.get("remove_sim_time", d["remove_sim_time"])
                faults_all.append({
                    "fault_id": fid, "episode_id": a["episode_id"], "fault_class": d["fault_class"],
                    "targets": d["targets"], "inject_sim_time": inj, "remove_sim_time": rem,
                    "anchor": a["anchor"], "group_id": d.get("group_id", "")})
                if d["fault_class"] == "maintenance":
                    maint_windows.append({"episode_id": a["episode_id"], "node": anon[d["targets"][0]],
                                          "window_start_datetime": iso(a["anchor"] + dt.timedelta(seconds=inj)),
                                          "window_end_datetime": iso(a["anchor"] + dt.timedelta(seconds=rem)),
                                          "n_burst_alarms": int(d.get("n_burst", 0))})
            for nd in topo["nodes"]:
                topo_rows.append({"episode_id": a["episode_id"], "node_id": anon[nd["id"]],
                                  "x": nd["x"], "y": nd["y"]})
            # Virtual EPC/core aggregation element + per-eNB S1-U backhaul edges
            # (the backhaul is per-node; backhaul_cut faults target single nodes).
            cx = round(sum(nd["x"] for nd in topo["nodes"]) / len(topo["nodes"]), 1)
            cy = round(sum(nd["y"] for nd in topo["nodes"]) / len(topo["nodes"]), 1)
            topo_rows.append({"episode_id": a["episode_id"], "node_id": "s_epc", "x": cx, "y": cy})
            for nd in topo["nodes"]:
                topo_edge_rows.append({"episode_id": a["episode_id"], "node_a": anon[nd["id"]],
                                       "node_b": "s_epc", "edge_type": "backhaul"})
            for e in topo["edges"]:
                topo_edge_rows.append({"episode_id": a["episode_id"], "node_a": anon[e["a"]],
                                       "node_b": anon[e["b"]], "edge_type": "x2"})
            print(f"[batch] episode {a['episode_id']}: {len(recs)} alarms  "
                  f"anchor={iso(a['anchor'])} ({a['anchor'].strftime('%a')} {a['anchor'].hour:02d}h) "
                  f"seed={a['seed']}  faults={len(fdefs)}", file=sys.stderr)

    if args.verify:
        _verify_mapping(all_records, anchors)

    # Global ordering → serials (stable: occurred, sim-time, bs)
    all_records.sort(key=lambda r: (r["_occurred_dt"], r["_sim_seconds"], r["source_bs_id"]))
    per_ne = {}
    for i, r in enumerate(all_records, 1):
        r["alarm_log_serial"] = i
        per_ne[r["source_bs_id"]] = per_ne.get(r["source_bs_id"], 0) + 1
        r["equipment_alarm_serial"] = per_ne[r["source_bs_id"]]

    # ── 1) alarms.csv (the 13-field BT record + episode_id) ──
    write_csv(os.path.join(run_dir, "alarms.csv"), ALARM_COLUMNS, all_records)

    # ── 2) fault_injections.csv ──
    def tgt_str(targets):
        if len(targets) == 1:
            return anon.get(targets[0], targets[0])
        if len(targets) >= 2:
            return f"{anon.get(targets[0], targets[0])}-{anon.get(targets[1], targets[1])}"
        return ""
    fi_rows = []
    for f in faults_all:
        inj, rem, anch = f["inject_sim_time"], f["remove_sim_time"], f["anchor"]
        fi_rows.append({
            "fault_id": f["fault_id"], "episode_id": f["episode_id"], "fault_class": f["fault_class"],
            "target_element": tgt_str(f["targets"]),
            "inject_sim_time": inj, "remove_sim_time": rem,
            "inject_datetime": iso(anch + dt.timedelta(seconds=inj)) if inj is not None else "",
            "remove_datetime": iso(anch + dt.timedelta(seconds=rem)) if rem is not None else "",
            "group_id": f.get("group_id", "")})
    write_csv(os.path.join(run_dir, "fault_injections.csv"),
              ["fault_id", "episode_id", "fault_class", "target_element", "inject_sim_time",
               "remove_sim_time", "inject_datetime", "remove_datetime", "group_id"], fi_rows)

    # ── 3) alarm_provenance.csv (eval key — never fed to the model) ──
    prov_rows = [{"alarm_log_serial": r["alarm_log_serial"], "episode_id": r["episode_id"],
                  "fault_id": r["_fault_id"], "ns3_event_type": r["_ns3_event_type"],
                  "sim_time": round(r["_sim_seconds"], 4)} for r in all_records]
    write_csv(os.path.join(run_dir, "alarm_provenance.csv"),
              ["alarm_log_serial", "episode_id", "fault_id", "ns3_event_type", "sim_time"], prov_rows)

    # ── 4) causal_edges.csv (realised true propagation per fault) ──
    from collections import defaultdict
    by_fault = defaultdict(list)
    for r in all_records:
        if r["_fault_id"] != "organic":
            by_fault[r["_fault_id"]].append(r)
    fault_by_id = {f["fault_id"]: f for f in faults_all}
    ce_rows = []
    for fid, recs in by_fault.items():
        f = fault_by_id.get(fid)
        if not f:
            continue
        inj = f["inject_sim_time"] or 0.0
        sources = f["targets"]
        by_target = defaultdict(list)
        for r in recs:
            by_target[r["_node"]].append(r)
        for tnode, trecs in by_target.items():
            first_delay = round(min(rr["_sim_seconds"] for rr in trecs) - inj, 4)
            for s in sources:
                ce_rows.append({"fault_id": fid, "episode_id": f["episode_id"],
                                "source_bs_id": anon.get(s, s), "target_bs_id": anon.get(tnode, tnode),
                                "first_alarm_delay_s": first_delay, "n_attributed_alarms": len(trecs)})
    write_csv(os.path.join(run_dir, "causal_edges.csv"),
              ["fault_id", "episode_id", "source_bs_id", "target_bs_id",
               "first_alarm_delay_s", "n_attributed_alarms"], ce_rows)

    # ── 5) topology.csv + topology_edges.csv ──
    write_csv(os.path.join(run_dir, "topology.csv"),
              ["episode_id", "node_id", "x", "y"], topo_rows)
    write_csv(os.path.join(run_dir, "topology_edges.csv"),
              ["episode_id", "node_a", "node_b", "edge_type"], topo_edge_rows)

    # ── 6) run_manifest.json ──
    manifest = {
        "base_seed": int(cfg["base_seed"]),
        "scenario": cfg["scenario"],
        "topology": {"id": cfg["topology"], "n_nodes": len(topo0["nodes"]), "n_edges": len(topo0["edges"])},
        "calendar": {"start": cfg["calendar_start"], "span_weeks": cfg["calendar_span_weeks"],
                     "mapping_constant_s_per_sim_s": 1.0, "anchor_hour_weights": cfg.get("anchor_hour_weights")},
        "episodes": [{"episode_id": a["episode_id"], "seed": a["seed"], "anchor": iso(a["anchor"]),
                      "weekday": a["anchor"].strftime("%A"), "hour": a["anchor"].hour} for a in anchors],
        "grace_period_s": cfg.get("grace_period_s"),
        "churn_holdoff_s": cfg.get("churn_holdoff_s"),
        "sim_seconds_per_episode": cfg["sim_seconds_per_episode"],
        "maintenance_windows": maint_windows,
        "git_commit": args.git_commit,
        "dataset_split_seed": 42,
        "anonymisation_map": anon,
        "field_provenance": {
            "episode_id": "mechanistic",
            "alarm_severity": "dataset_calibrated", "alarm_name": "dataset_calibrated",
            "source_bs_id": "mechanistic", "source_bs_type": "dataset_calibrated",
            "alarm_location_info": "dataset_calibrated",
            "occurred_timestamp": "mechanistic",
            "cleared_timestamp": "mixed: mechanistic (transport faults) / synthesised (other)",
            "acknowledged_timestamp": "synthesised",
            "cleared_status": "derived", "acknowledged_status": "derived",
            "alarm_log_serial": "mechanistic", "equipment_alarm_serial": "mechanistic",
            "bs_maintenance_status": "mechanistic (scenario)",
        },
        "config_snapshot": cfg,
    }
    with open(os.path.join(run_dir, "run_manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2)

    print(f"[batch] wrote run dir {run_dir}: alarms={len(all_records)} faults={len(fi_rows)} "
          f"provenance={len(prov_rows)} causal_edges={len(ce_rows)}", file=sys.stderr)


def write_csv(path, columns, rows):
    import csv as _csv
    with open(path, "w", newline="") as f:
        w = _csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


if __name__ == "__main__":
    main()
