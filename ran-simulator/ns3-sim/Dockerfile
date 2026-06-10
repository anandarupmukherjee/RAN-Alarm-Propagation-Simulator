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
COPY ran-alarm-sim.cc /ns3/scratch/ran-alarm-sim.cc
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

# Copy the control server + launcher
COPY start.sh sim_server.py ./
RUN chmod +x start.sh

# Shared data directory (scenario + commands files)
RUN mkdir -p /data

EXPOSE 5000

CMD ["/ns3/start.sh"]
