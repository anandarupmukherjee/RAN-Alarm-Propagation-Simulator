"""
sim_server.py
─────────────
Flask HTTP control server for the ns-3 simulation container.

ns-3 is the CORE and is NOT optional. This server launches the compiled
ns-3 LTE program `ran-alarm-sim` (a real EPC + eNodeB + UE radio-access
simulation), reads the RAN events it prints on stdout, and forwards them to
Redis on the `ns3:events` list. The FastAPI backend then enriches each event
into a full alarm record.

If the ns-3 binary is missing or fails to start, the server reports unhealthy —
there is no statistical fallback.

Endpoints:
  GET  /health              → liveness check
  GET  /status              → simulation status + ns3 availability
  POST /configure           → set topology + speed (restarts if running)
  POST /start               → launch ns-3 simulation
  POST /stop                → terminate ns-3 simulation
  POST /inject              → queue a fault-injection command
  POST /link-failure        → queue a link-failure command
  POST /speed               → update UE mobility intensity ("speed")
"""

import os
import sys
import json
import time
import signal
import shutil
import subprocess
import threading
import redis as redis_lib
from pathlib import Path
from flask import Flask, request, jsonify

app = Flask(__name__)

# ─── Config ───────────────────────────────────────────────────────────────────
REDIS_URL     = os.getenv("REDIS_URL", "redis://redis:6379")
FLASK_PORT    = int(os.getenv("FLASK_PORT", 5000))
NS3_DIR       = os.getenv("NS3_DIR", "/ns3")
DATA_DIR      = "/data"
SCENARIO_FILE = f"{DATA_DIR}/ns3_scenario.txt"
COMMANDS_FILE = f"{DATA_DIR}/ns3_commands.txt"
REDIS_KEY     = "ns3:events"
SIM_PROGRAM   = "ran-alarm-sim"

# Path to the prebuilt ns-3 binary (compiled at image-build time).
NS3_BINARY    = os.getenv(
    "NS3_BINARY", f"{NS3_DIR}/build/scratch/ns3.41-{SIM_PROGRAM}-optimized")

# ─── State ────────────────────────────────────────────────────────────────────
_sim_proc:    "subprocess.Popen | None" = None
_proc_lock    = threading.Lock()
_sim_config:  dict = {}
_sim_speed:   float = 30.0          # time-compression: sim-seconds per wall-second
_ue_speed:    float = 45.0          # UE mobility (m/s) — fixed; drives handover/RLF density
_ue_per_enb:  int = 4
_start_time:  "float | None" = None
_cmd_lock     = threading.Lock()
_redis        = redis_lib.from_url(REDIS_URL, decode_responses=True)
_event_count  = 0


# ─── Helpers ──────────────────────────────────────────────────────────────────
def _is_running() -> bool:
    with _proc_lock:
        return _sim_proc is not None and _sim_proc.poll() is None


def _ns3_available() -> bool:
    return os.path.exists(NS3_BINARY)


def _write_scenario():
    """Translate the topology config into the simple line-format scenario file
    that ran-alarm-sim parses (NODE/EDGE/UEPERENB/UESPEED/REALTIME)."""
    Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
    nodes = _sim_config.get("nodes", [])
    edges = _sim_config.get("edges", [])
    lines = []
    for n in nodes:
        nid = str(n.get("id", n.get("site_id", "0")))
        x = float(n.get("x", 0.0))
        y = float(n.get("y", 0.0))
        lines.append(f"NODE {nid} {x} {y}")
    for e in edges:
        lines.append(f"EDGE {e.get('source')} {e.get('target')}")
    # Scale UEs down for large topologies so the ns-3 LTE sim stays feasible.
    nn = len(nodes)
    ue_per = 4 if nn <= 20 else 3 if nn <= 60 else 2 if nn <= 120 else 1
    ue_per = min(ue_per, _ue_per_enb)
    lines.append(f"UEPERENB {ue_per}")
    lines.append(f"UESPEED {_ue_speed}")
    lines.append(f"SPEED {_sim_speed}")
    lines.append("REALTIME 1")
    with open(SCENARIO_FILE, "w") as f:
        f.write("\n".join(lines) + "\n")


def _write_command(line: str):
    """Append a command line for ran-alarm-sim to poll."""
    with _cmd_lock:
        with open(COMMANDS_FILE, "a") as f:
            f.write(line.rstrip() + "\n")


def _event_pump(proc: subprocess.Popen):
    """Read ns-3 stdout; forward '@@ALARM@@ {json}' lines to Redis, log the rest."""
    global _event_count
    for raw in iter(proc.stdout.readline, b""):
        text = raw.decode("utf-8", errors="replace").rstrip()
        if not text:
            continue
        if text.startswith("@@ALARM@@ "):
            payload = text[len("@@ALARM@@ "):]
            try:
                json.loads(payload)                 # validate
                _redis.rpush(REDIS_KEY, payload)
                _event_count += 1
            except Exception as ex:
                print(f"[ns3] bad event line: {ex}: {payload[:120]}", flush=True)
        elif text.startswith("@@READY@@"):
            print(f"[ns3] {text}", flush=True)
        else:
            print(f"[ns3] {text}", flush=True)
    print("[server] ns-3 stdout closed", flush=True)


def _start_sim():
    """Launch the ns-3 LTE simulation binary directly."""
    global _sim_proc, _start_time

    if not _ns3_available():
        raise RuntimeError(f"ns-3 binary not found at {NS3_BINARY}")

    _write_scenario()
    # Fresh commands file each run
    try:
        os.remove(COMMANDS_FILE)
    except FileNotFoundError:
        pass

    cmd = [
        NS3_BINARY,
        f"--scenario={SCENARIO_FILE}",
        f"--commands={COMMANDS_FILE}",
        "--realtime=1",
    ]
    env = {
        **os.environ,
        "LD_LIBRARY_PATH": f"{NS3_DIR}/build/lib:" + os.environ.get("LD_LIBRARY_PATH", ""),
    }
    print(f"[server] Launching ns-3 core: {' '.join(cmd)}", flush=True)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, env=env)

    t = threading.Thread(target=_event_pump, args=(proc,), daemon=True)
    t.start()

    with _proc_lock:
        _sim_proc = proc
        _start_time = time.time()
    print(f"[server] ns-3 simulation started (PID {proc.pid})", flush=True)


def _stop_sim():
    global _sim_proc, _start_time
    with _proc_lock:
        if _sim_proc and _sim_proc.poll() is None:
            _sim_proc.send_signal(signal.SIGTERM)
            try:
                _sim_proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                _sim_proc.kill()
            print("[server] ns-3 simulation stopped", flush=True)
        _sim_proc = None
        _start_time = None


# ─── Routes ───────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    # Healthy only when the ns-3 core binary exists (ns-3 is mandatory).
    ok = _ns3_available()
    return (jsonify({"status": "ok" if ok else "no_ns3",
                     "running": _is_running(),
                     "ns3_binary": NS3_BINARY,
                     "ns3_available": ok}),
            200 if ok else 503)


@app.get("/status")
def status():
    elapsed = (time.time() - _start_time) if _start_time else 0
    return jsonify({
        "running":       _is_running(),
        "ns3_available": _ns3_available(),
        "engine":        "ns-3 LTE (core)",
        "speed":         _sim_speed,
        "ue_per_enb":    _ue_per_enb,
        "node_count":    len(_sim_config.get("nodes", [])),
        "edge_count":    len(_sim_config.get("edges", [])),
        "elapsed_s":     round(elapsed, 1),
        "events_emitted": _event_count,
    })


@app.post("/configure")
def configure():
    global _sim_config, _sim_speed, _ue_per_enb
    data = request.get_json(force=True)
    _sim_config = data.get("topology", data)
    _sim_speed  = float(data.get("speed", _sim_speed))
    if "ue_per_enb" in data:
        _ue_per_enb = int(data["ue_per_enb"])

    was_running = _is_running()
    if was_running:
        _stop_sim()
    _write_scenario()
    print(f"[server] Configured: {len(_sim_config.get('nodes', []))} eNBs, "
          f"ue/enb={_ue_per_enb}, speed={_sim_speed}", flush=True)
    if was_running:
        _start_sim()
    return jsonify({"status": "configured"})


@app.post("/start")
def start():
    global _sim_speed
    data = request.get_json(force=True) or {}
    _sim_speed = float(data.get("speed", _sim_speed))

    if not _sim_config.get("nodes"):
        return jsonify({"error": "No topology configured — POST /configure first"}), 400
    if not _ns3_available():
        return jsonify({"error": f"ns-3 binary missing at {NS3_BINARY}"}), 503
    if _is_running():
        return jsonify({"status": "already_running"})
    try:
        _start_sim()
    except Exception as ex:
        return jsonify({"error": str(ex)}), 500
    return jsonify({"status": "started", "pid": _sim_proc.pid if _sim_proc else None})


@app.post("/stop")
def stop():
    _stop_sim()
    return jsonify({"status": "stopped"})


@app.post("/inject")
def inject():
    data = request.get_json(force=True)
    node_id    = data.get("node_id")
    event_type = data.get("event_type", "cell_blocked")
    alarm_name = data.get("alarm_name")
    if not node_id:
        return jsonify({"error": "node_id required"}), 400
    line = f"inject {node_id} {event_type}"
    if alarm_name:
        line += f" {alarm_name}"
    _write_command(line)
    return jsonify({"status": "injected", "node_id": node_id,
                    "event_type": event_type, "alarm_name": alarm_name})


@app.post("/link-failure")
def link_failure():
    data = request.get_json(force=True)
    src = data.get("source")
    tgt = data.get("target")
    if not src or not tgt:
        return jsonify({"error": "source and target required"}), 400
    _write_command(f"link_failure {src} {tgt}")
    return jsonify({"status": "injected", "source": src, "target": tgt})


@app.post("/speed")
def set_speed():
    """Speed = time-compression (sim-seconds per wall-second). Applied live."""
    global _sim_speed
    data = request.get_json(force=True)
    _sim_speed = float(data.get("speed", _sim_speed))
    _write_command(f"set_speed {_sim_speed}")   # live update via command poll
    return jsonify({"status": "ok", "speed": _sim_speed})


# ─── Main ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"[server] ns-3 binary: {NS3_BINARY} (exists={_ns3_available()})", flush=True)
    print(f"[server] Starting Flask on port {FLASK_PORT}", flush=True)
    app.run(host="0.0.0.0", port=FLASK_PORT, threaded=True)
