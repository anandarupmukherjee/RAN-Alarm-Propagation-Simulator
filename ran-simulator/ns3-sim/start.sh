#!/bin/bash
# start.sh — set the ns-3 shared-library path, then launch the control server.
# The control server runs the compiled ns-3 LTE binary (ran-alarm-sim) directly.
set -e
NS3_DIR=/ns3

export LD_LIBRARY_PATH="${NS3_DIR}/build/lib:${LD_LIBRARY_PATH}"

BIN="${NS3_DIR}/build/scratch/ns3.41-ran-alarm-sim-optimized"
if [ -x "$BIN" ]; then
    echo "[startup] ns-3 LTE core binary present: $BIN"
else
    echo "[startup] WARNING: ns-3 core binary not found at $BIN"
fi

echo "[startup] Launching sim_server.py on port ${FLASK_PORT:-5000}..."
exec python3 /ns3/sim_server.py
