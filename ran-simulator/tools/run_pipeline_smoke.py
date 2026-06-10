#!/usr/bin/env python3
"""
run_pipeline_smoke.py
─────────────────────
A minimal stand-in for the downstream consumer's first step: per-BS feature
encoding. It reads ONLY alarms.csv (no ground-truth, no simulator internals) to
prove the export is self-sufficient for an external analysis pipeline.

Per base station it builds:
  - severity histogram            (4 bins: Critical/Major/Minor/Warning)
  - alarm-name histogram          (vocabulary = names present in alarms.csv)
  - hour-of-day histogram         (24 bins)
  - day-of-week histogram         (7 bins)
  - inter-alarm-gap summary       (mean, median, std — seconds)
and concatenates them into one feature vector. Prints the feature-matrix shape.

Usage:  python3 run_pipeline_smoke.py exports/run_000/alarms.csv

Stdlib only — must not import anything from the simulator.
"""
import argparse
import csv
import datetime as dt
import statistics as st
from collections import defaultdict

SEV = ["Critical", "Major", "Minor", "Warning"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("alarms_csv")
    args = ap.parse_args()

    with open(args.alarms_csv) as f:
        rows = list(csv.DictReader(f))
    if not rows:
        print("no rows")
        return

    # Vocabulary derived from alarms.csv alone.
    names = sorted({r["alarm_name"] for r in rows})
    name_idx = {nm: i for i, nm in enumerate(names)}

    by_bs = defaultdict(list)
    for r in rows:
        by_bs[r["source_bs_id"]].append(r)

    bs_ids = sorted(by_bs)
    feats = []
    for bs in bs_ids:
        rs = by_bs[bs]
        sev_h = [0] * 4
        name_h = [0] * len(names)
        hod = [0] * 24
        dow = [0] * 7
        times = []
        for r in rs:
            if r["alarm_severity"] in SEV:
                sev_h[SEV.index(r["alarm_severity"])] += 1
            name_h[name_idx[r["alarm_name"]]] += 1
            o = dt.datetime.fromisoformat(r["occurred_timestamp"])
            hod[o.hour] += 1
            dow[o.weekday()] += 1
            times.append(o)
        times.sort()
        gaps = [(times[i] - times[i - 1]).total_seconds() for i in range(1, len(times))]
        summ = [
            st.mean(gaps) if gaps else 0.0,
            st.median(gaps) if gaps else 0.0,
            st.pstdev(gaps) if len(gaps) > 1 else 0.0,
        ]
        feats.append(sev_h + name_h + hod + dow + summ)

    dim = len(feats[0]) if feats else 0
    print(f"per-BS feature matrix shape: ({len(feats)}, {dim})")
    print(f"  feature layout: severity(4) + name({len(names)}) + hour_of_day(24) + day_of_week(7) + gap_summary(3)")
    print(f"  vocabulary (from alarms.csv only): {len(names)} alarm names, {len(bs_ids)} base stations")
    ex = feats[0]
    print(f"  example BS {bs_ids[0]}: vector length {len(ex)}, nonzero entries {sum(1 for x in ex if x)}")
    print("  ✅ feature encoding completed using ONLY alarms.csv (no other inputs required).")


if __name__ == "__main__":
    main()
