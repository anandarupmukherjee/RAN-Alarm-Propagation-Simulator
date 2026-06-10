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

# ─────────────────────────────────────────────────────────────────────────────
# Stage 2: Runtime image — ns-3 source + build artifacts + sim scripts
# We keep the source because we need ./ns3 run to set Python env correctly.
# ─────────────────────────────────────────────────────────────────────────────
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    python3 python3-pip python3-dev \
    # g++ + python headers needed to build the CPyCppyy extension at pip time
    g++ \
    # Runtime libs needed by ns-3 shared objects
    libgsl27 libsqlite3-0 libxml2 \
    curl \
    # ns-3.41 requires cppyy <= 2.4.2 — newer cppyy (3.x) uses an ORC JIT that
    # fails to materialise ns-3's internal-linkage static initialisers.
    && pip3 install --no-cache-dir "cppyy==2.4.2" flask redis requests \
    && rm -rf /var/lib/apt/lists/*

# Copy the full ns-3 tree (source + build) from builder stage
COPY --from=builder /ns3 /ns3

WORKDIR /ns3

# Copy our simulation scripts
COPY start.sh sim_server.py ran_simulation.py ./
RUN chmod +x start.sh

# Create shared data directory
RUN mkdir -p /data

EXPOSE 5000

CMD ["/ns3/start.sh"]
