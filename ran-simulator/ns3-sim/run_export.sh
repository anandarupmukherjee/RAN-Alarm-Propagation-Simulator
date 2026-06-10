#!/bin/bash
# run_export.sh — run the BT-comparable batch exporter inside the ns3-sim image,
# against the prebuilt ns-3 binary. Extra args ($@) override the defaults below
# (e.g. --out /out --episodes 50 --scenario maintenance).
set -e
export LD_LIBRARY_PATH=/ns3/build/lib:${LD_LIBRARY_PATH}
exec python3 /ns3/tools/batch_export.py \
    --config /ns3/tools/export.json \
    --mapper /ns3/tools/alarm_mapper.py \
    --stats  /ns3/tools/mapper_stats.json \
    --bin    /ns3/build/scratch/ns3.41-ran-alarm-sim-optimized \
    --out    /data/exports "$@"
