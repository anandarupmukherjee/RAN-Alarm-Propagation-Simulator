#!/bin/bash
# start.sh — Sets up ns-3 Python environment then launches Flask sim server

set -e
NS3_DIR=/ns3
PYTHON_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")

echo "[startup] Python ${PYTHON_VERSION} detected"
echo "[startup] Configuring ns-3 Python paths..."

# Add all possible locations for ns-3 Python bindings (cppyy-based, ns-3.36+)
for candidate in \
    "${NS3_DIR}/build/lib" \
    "${NS3_DIR}/build" \
    "${NS3_DIR}/build/lib/python${PYTHON_VERSION}/site-packages" \
    "${NS3_DIR}/build/lib/python${PYTHON_VERSION}/dist-packages" \
    "${NS3_DIR}/build/bindings/python"; do

    if [ -d "$candidate" ]; then
        export PYTHONPATH="${candidate}:${PYTHONPATH}"
        echo "[startup] Added to PYTHONPATH: $candidate"
    fi
done

export LD_LIBRARY_PATH="${NS3_DIR}/build/lib:${LD_LIBRARY_PATH}"

echo "[startup] LD_LIBRARY_PATH=${LD_LIBRARY_PATH}"
echo "[startup] Launching sim_server.py on port ${FLASK_PORT:-5000}..."

exec python3 /ns3/sim_server.py
