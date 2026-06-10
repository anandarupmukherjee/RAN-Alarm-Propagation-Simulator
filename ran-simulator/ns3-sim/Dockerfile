# ─────────────────────────────────────────────────────────────────────────────
# Stage 1: Build ns-3.41 with LTE module + Python bindings
# This layer is cached after the first build (~20-40 min one-time cost).
# ─────────────────────────────────────────────────────────────────────────────
FROM ubuntu:22.04 AS builder

ENV DEBIAN_FRONTEND=noninteractive
ENV NS3_VERSION=3.41

RUN apt-get update && apt-get install -y \
    # Build essentials
    g++ gcc cmake ninja-build make \
    # Python with dev headers (for bindings)
    python3 python3-dev python3-pip \
    # Download tools
    wget tar git curl \
    # ns-3 module dependencies
    libgsl-dev libsqlite3-dev \
    libboost-all-dev \
    libxml2-dev \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

# cppyy is required for ns-3 Python bindings (3.36+)
RUN pip3 install --no-cache-dir cppyy

WORKDIR /tmp
RUN wget -q https://www.nsnam.org/releases/ns-allinone-${NS3_VERSION}.tar.bz2 \
    && tar xjf ns-allinone-${NS3_VERSION}.tar.bz2 \
    && mv ns-allinone-${NS3_VERSION}/ns-${NS3_VERSION} /ns3 \
    && rm -rf /tmp/*

WORKDIR /ns3

# Configure: only the modules needed for LTE RAN simulation
# Disabling examples/tests dramatically reduces build time
RUN ./ns3 configure \
    --enable-python-bindings \
    --enable-modules="core,network,lte,point-to-point,mobility,internet,applications,spectrum,propagation,antenna,wifi,bridge" \
    --disable-examples \
    --disable-tests \
    --build-profile=optimized

# Build using all available cores
RUN ./ns3 build -j$(nproc)

# ── Compile the RAN alarm simulation CORE (real ns-3 LTE scenario) ───────────
# Incremental: reuses the cached ns-3 library build above; only this program
# compiles. Produces /ns3/build/scratch/ns3.41-ran-alarm-sim-optimized
# NOTE: build context is the ran-simulator root (see docker-compose.yml).
COPY ns3-sim/ran-alarm-sim.cc /ns3/scratch/ran-alarm-sim.cc
RUN ./ns3 build ran-alarm-sim

# ─────────────────────────────────────────────────────────────────────────────
# Stage 2: Runtime image — prebuilt ns-3 tree + compiled sim core + control server
# The ns-3 binary is run directly (no cmake at runtime). ns-3 is mandatory.
# ─────────────────────────────────────────────────────────────────────────────
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    python3 python3-pip \
    # Runtime libs needed by the ns-3 shared objects the binary links against
    libgsl27 libsqlite3-0 libxml2 libboost-filesystem1.74.0 \
    curl \
    && pip3 install --no-cache-dir flask redis \
    && rm -rf /var/lib/apt/lists/*

# Copy the full ns-3 tree (source + build + compiled scratch binary) from builder
COPY --from=builder /ns3 /ns3

WORKDIR /ns3

# Copy the control server + launcher (paths are relative to the ran-simulator root)
COPY ns3-sim/start.sh ns3-sim/sim_server.py ns3-sim/run_export.sh ./
RUN chmod +x start.sh run_export.sh

# Bake in the BT-comparable batch exporter + its calibration (stats-only, no pandas)
COPY tools/batch_export.py tools/validate_export.py tools/run_pipeline_smoke.py /ns3/tools/
COPY backend/alarm_mapper.py        /ns3/tools/alarm_mapper.py
COPY backend/data/mapper_stats.json /ns3/tools/mapper_stats.json
COPY config/export.json             /ns3/tools/export.json

# Shared data directory (scenario + commands files; exports/ when mounted)
RUN mkdir -p /data

EXPOSE 5000

CMD ["/ns3/start.sh"]
