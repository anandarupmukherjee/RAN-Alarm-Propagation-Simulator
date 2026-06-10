"""
sim_server.py
─────────────
Flask HTTP control server for the ns-3 simulation container.

Manages the simulation subprocess (ran_simulation.py), reads its
Redis output, and exposes a REST API consumed by the FastAPI backend.

Endpoints:
  GET  /health              → liveness check
  GET  /status              → simulation status + ns3 availability
  POST /configure           → set topology + speed (restarts if running)
  POST /start               → launch simulation subprocess
  POST /stop                → terminate simulation subprocess
  POST /inject              → write a fault-injection command
  POST /link-failure        → inject a specific link failure
"""

import os
import sys
import json
import time
import signal
import subprocess
import threading
from pathlib import Path
from flask import Flask, request, jsonify

app = Flask(__name__)

# ─── State ────────────────────────────────────────────────────────────────────
REDIS_URL   = os.getenv("REDIS_URL", "redis://redis:6379")
FLASK_PORT  = int(os.getenv("FLASK_PORT", 5000))
CONFIG_FILE = "/data/topology.json"
COMMANDS_FILE = "/data/commands.json"
NS3_DIR     = "/ns3"

_sim_proc: subprocess.Popen | None = None
_proc_lock = threading.Lock()
_sim_config: dict = {}
_sim_speed:  float = 60.0
_cascade:    float = 0.35
_start_time: float | None = None
_commands_lock = threading.Lock()


# ─── Helpers ──────────────────────────────────────────────────────────────────
def _is_running() -> bool:
    with _proc_lock:
        return _sim_proc is not None and _sim_proc.poll() is None


def _log_reader(proc: subprocess.Popen):
    """Stream simulation subprocess output to our own stdout."""
    for line in iter(proc.stdout.readline, b""):
        text = line.decode("utf-8", errors="replace").rstrip()
        print(f"[sim] {text}", flush=True)


def _write_config():
    """Persist the current topology config to disk."""
    Path("/data").mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(_sim_config, f)


def _write_command(cmd: dict):
    """Append a command to the commands file (thread-safe)."""
    with _commands_lock:
        existing = []
        if os.path.exists(COMMANDS_FILE):
            try:
                with open(COMMANDS_FILE, "r") as f:
                    existing = json.load(f)
            except Exception:
                existing = []
        existing.append(cmd)
        with open(COMMANDS_FILE, "w") as f:
            json.dump(existing, f)


def _start_sim():
    """Launch ran_simulation.py as a subprocess."""
    global _sim_proc, _start_time

    _write_config()

    cmd = [
        sys.executable,
        os.path.join(NS3_DIR, "ran_simulation.py"),
        "--config",  CONFIG_FILE,
        "--speed",   str(_sim_speed),
        "--redis",   REDIS_URL,
        "--cascade", str(_cascade),
    ]

    print(f"[server] Launching: {' '.join(cmd)}", flush=True)

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env={**os.environ, "REDIS_URL": REDIS_URL},
    )

    # Stream subprocess logs
    log_t = threading.Thread(target=_log_reader, args=(proc,), daemon=True)
    log_t.start()

    with _proc_lock:
        _sim_proc = proc
        _start_time = time.time()

    print(f"[server] Simulation subprocess started (PID {proc.pid})", flush=True)


def _stop_sim():
    """Terminate the simulation subprocess."""
    global _sim_proc, _start_time
    with _proc_lock:
        if _sim_proc and _sim_proc.poll() is None:
            _sim_proc.send_signal(signal.SIGTERM)
            try:
                _sim_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _sim_proc.kill()
            print(f"[server] Simulation subprocess stopped", flush=True)
        _sim_proc = None
        _start_time = None


# ─── Routes ───────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return jsonify({"status": "ok", "running": _is_running()})


@app.get("/status")
def status():
    elapsed = (time.time() - _start_time) if _start_time else 0
    return jsonify({
        "running":       _is_running(),
        "ns3_available": _check_ns3(),
        "speed":         _sim_speed,
        "cascade_prob":  _cascade,
        "node_count":    len(_sim_config.get("nodes", [])),
        "edge_count":    len(_sim_config.get("edges", [])),
        "elapsed_s":     round(elapsed, 1),
        "sim_hours":     round(elapsed * _sim_speed / 3600, 2),
    })


@app.post("/configure")
def configure():
    global _sim_config, _sim_speed, _cascade
    data = request.get_json(force=True)
    _sim_config = data.get("topology", data)
    _sim_speed  = float(data.get("speed", _sim_speed))
    _cascade    = float(data.get("propagation_prob", _cascade))

    was_running = _is_running()
    if was_running:
        _stop_sim()

    _write_config()
    print(f"[server] Configured: {len(_sim_config.get('nodes',[]))} nodes, "
          f"speed={_sim_speed}×", flush=True)

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

    if _is_running():
        return jsonify({"status": "already_running"})

    _start_sim()
    return jsonify({"status": "started", "pid": _sim_proc.pid if _sim_proc else None})


@app.post("/stop")
def stop():
    _stop_sim()
    return jsonify({"status": "stopped"})


@app.post("/inject")
def inject():
    """Inject a named fault on a node."""
    data = request.get_json(force=True)
    node_id    = data.get("node_id")
    event_type = data.get("event_type", "cell_blocked")
    alarm_name = data.get("alarm_name")  # optional: inject a specific real alarm

    if not node_id:
        return jsonify({"error": "node_id required"}), 400

    cmd = {"type": "inject", "node_id": node_id, "event_type": event_type}
    if alarm_name:
        cmd["alarm_name"] = alarm_name
    _write_command(cmd)
    return jsonify({"status": "injected", "node_id": node_id,
                    "event_type": event_type, "alarm_name": alarm_name})


@app.post("/link-failure")
def link_failure():
    """Inject a backhaul link failure between two nodes."""
    data = request.get_json(force=True)
    src = data.get("source")
    tgt = data.get("target")
    if not src or not tgt:
        return jsonify({"error": "source and target required"}), 400

    _write_command({"type": "link_failure", "source": src, "target": tgt})
    return jsonify({"status": "injected", "source": src, "target": tgt})


@app.post("/speed")
def set_speed():
    data = request.get_json(force=True)
    new_speed = float(data.get("speed", _sim_speed))
    _write_command({"type": "set_speed", "speed": new_speed})
    return jsonify({"status": "ok", "speed": new_speed})


# ─── ns-3 availability check ──────────────────────────────────────────────────
_ns3_checked: bool | None = None

def _check_ns3() -> bool:
    global _ns3_checked
    if _ns3_checked is not None:
        return _ns3_checked
    try:
        result = subprocess.run(
            [sys.executable, "-c", "from ns import ns; ns.LogDistancePropagationLossModel; print('ok')"],
            capture_output=True, timeout=30
        )
        _ns3_checked = result.returncode == 0 and b"ok" in result.stdout
    except Exception:
        _ns3_checked = False
    return _ns3_checked


# ─── Main ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"[server] ns-3 available: {_check_ns3()}", flush=True)
    print(f"[server] Starting Flask on port {FLASK_PORT}", flush=True)
    app.run(host="0.0.0.0", port=FLASK_PORT, threaded=True)
