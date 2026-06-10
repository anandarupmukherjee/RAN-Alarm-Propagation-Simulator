#!/usr/bin/env python3
"""
validate_export.py
──────────────────
Acceptance tests for a batch-export run directory: SCHEMA, NO-LEAKAGE,
REFERENTIAL INTEGRITY and TEMPORAL SANITY are asserted (non-zero exit on
failure); a STATISTICAL COMPARABILITY report is printed (never asserted),
optionally side-by-side with the historical dataset.

Usage:
  python3 validate_export.py exports/run_000 [--dataset analysis_data.csv]

Stdlib only for validation; pandas (if available) only for the dataset report.
"""
import argparse
import csv
import datetime as dt
import math
import os
import statistics as st
import sys
from collections import Counter, defaultdict

ALARM_COLUMNS = [
    "episode_id",
    "alarm_severity", "alarm_name", "source_bs_id", "source_bs_type",
    "alarm_location_info", "occurred_timestamp", "cleared_timestamp",
    "acknowledged_timestamp", "cleared_status", "acknowledged_status",
    "alarm_log_serial", "equipment_alarm_serial", "bs_maintenance_status",
]
SEV = {"Critical", "Major", "Minor", "Warning"}
SEV_ORDER = ["Critical", "Major", "Minor", "Warning"]
N_ALARM_TYPES = 171
REF_SEVERITY = {"Major": 81, "Minor": 13, "Critical": 4, "Warning": 2}  # BT reference %


def rd(path):
    with open(path) as f:
        return list(csv.DictReader(f))


def pdt(s):
    return dt.datetime.fromisoformat(s) if s else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("rundir")
    ap.add_argument("--dataset", default=None, help="historical dataset CSV for side-by-side report")
    args = ap.parse_args()
    R = args.rundir

    al = rd(os.path.join(R, "alarms.csv"))
    pv = rd(os.path.join(R, "alarm_provenance.csv"))
    fi = rd(os.path.join(R, "fault_injections.csv"))
    ce = rd(os.path.join(R, "causal_edges.csv"))

    fails = []
    def check(name, cond, detail=""):
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  — {detail}" if not cond and detail else ""))
        if not cond:
            fails.append(name)

    # ── 1. SCHEMA ──
    print("== 1. SCHEMA ==")
    cols = list(al[0].keys())
    check("columns exact + ordered", cols == ALARM_COLUMNS, f"got {cols}")
    check("severity vocab ⊆ {Critical,Major,Minor,Warning}", set(r["alarm_severity"] for r in al) <= SEV)
    ts_ok = True
    for r in al:
        try:
            pdt(r["occurred_timestamp"])
            if r["cleared_timestamp"]:
                pdt(r["cleared_timestamp"])
            if r["acknowledged_timestamp"]:
                pdt(r["acknowledged_timestamp"])
        except Exception:
            ts_ok = False
            break
    check("timestamps parse ISO-8601", ts_ok)
    check("cleared_status ↔ cleared_timestamp", all((r["cleared_status"] == "cleared") == bool(r["cleared_timestamp"]) for r in al))
    check("acknowledged_status ↔ acknowledged_timestamp", all((r["acknowledged_status"] == "acknowledged") == bool(r["acknowledged_timestamp"]) for r in al))
    ser = [int(r["alarm_log_serial"]) for r in al]
    check("alarm_log_serial 1..N monotonic", ser == list(range(1, len(al) + 1)))
    seen, eqok = defaultdict(int), True
    for r in al:
        seen[r["source_bs_id"]] += 1
        if int(r["equipment_alarm_serial"]) != seen[r["source_bs_id"]]:
            eqok = False
            break
    check("equipment_alarm_serial per-BS monotonic", eqok)
    req = ["alarm_severity", "alarm_name", "source_bs_id", "source_bs_type", "alarm_location_info",
           "occurred_timestamp", "alarm_log_serial", "equipment_alarm_serial", "bs_maintenance_status"]
    check("no nulls in required fields (1-6, 11-13)", all(all(r[c] != "" for c in req) for r in al))

    # ── 2. NO LEAKAGE ──
    print("== 2. NO LEAKAGE ==")
    forbidden = {"fault_id", "ns3_event_type", "sim_time", "target_bs_id", "source_element", "group_id"}
    leak = set(cols) & forbidden
    check("alarms.csv has no fault/provenance/topology columns", not leak, f"leaked {leak}")
    check("episode_id present (opted-in)", "episode_id" in cols)

    # ── 3. REFERENTIAL INTEGRITY ──
    print("== 3. REFERENTIAL INTEGRITY ==")
    als = set(int(r["alarm_log_serial"]) for r in al)
    pvs = set(int(r["alarm_log_serial"]) for r in pv)
    check("provenance ⇄ alarms serials bijective", als == pvs, f"sym_diff={len(als ^ pvs)}")
    fids = set(r["fault_id"] for r in fi)
    pvf = set(r["fault_id"] for r in pv if r["fault_id"] != "organic")
    check("non-organic provenance fault_id ⊆ fault_injections", pvf <= fids, f"missing {pvf - fids}")
    cef = set(r["fault_id"] for r in ce)
    check("causal_edges fault_id ⊆ fault_injections", cef <= fids, f"missing {cef - fids}")
    ser2bs = {int(r["alarm_log_serial"]): r["source_bs_id"] for r in al}
    ser2sim = {int(p["alarm_log_serial"]): float(p["sim_time"]) for p in pv}
    fi_inj = {r["fault_id"]: float(r["inject_sim_time"]) for r in fi if r["inject_sim_time"]}
    attr = defaultdict(lambda: defaultdict(list))
    for p in pv:
        if p["fault_id"] != "organic":
            attr[p["fault_id"]][ser2bs[int(p["alarm_log_serial"])]].append(float(p["sim_time"]))
    derivable = True
    for r in ce:
        sims = attr.get(r["fault_id"], {}).get(r["target_bs_id"])
        if not sims or int(r["n_attributed_alarms"]) != len(sims):
            derivable = False
            break
        exp = round(min(sims) - fi_inj.get(r["fault_id"], 0.0), 4)
        if abs(float(r["first_alarm_delay_s"]) - exp) > 0.05:
            derivable = False
            break
    check("every causal_edges row derivable from provenance", derivable)

    # ── 4. TEMPORAL SANITY ──
    print("== 4. TEMPORAL SANITY ==")
    bad_c = sum(1 for r in al if r["cleared_timestamp"] and pdt(r["cleared_timestamp"]) < pdt(r["occurred_timestamp"]))
    bad_a = sum(1 for r in al if r["acknowledged_timestamp"] and pdt(r["acknowledged_timestamp"]) < pdt(r["occurred_timestamp"]))
    check("cleared ≥ occurred", bad_c == 0, f"{bad_c} violations")
    check("acknowledged ≥ occurred", bad_a == 0, f"{bad_a} violations")
    check("first_alarm_delay_s ≥ 0 and finite",
          all(float(r["first_alarm_delay_s"]) >= 0 and math.isfinite(float(r["first_alarm_delay_s"])) for r in ce))
    by_ep = defaultdict(list)
    for r in al:
        s = int(r["alarm_log_serial"])
        if s in ser2sim:
            by_ep[r["episode_id"]].append((ser2sim[s], pdt(r["occurred_timestamp"])))
    pairs, mapok = 0, True
    for rows in by_ep.values():
        rows.sort()
        for i in range(1, len(rows)):
            if pairs >= 100:
                break
            cal = (rows[i][1] - rows[i - 1][1]).total_seconds()
            sim = rows[i][0] - rows[i - 1][0]
            if abs(cal - sim) > 1e-3:
                mapok = False
            pairs += 1
    check(f"intra-episode 1:1 calendar↔sim mapping ({pairs} pairs)", mapok)

    # ── 5. STATISTICAL COMPARABILITY (report only) ──
    print("\n== 5. STATISTICAL COMPARABILITY REPORT (not asserted) ==")
    report(al, ce, fi, pv, ser2bs, args.dataset)

    print()
    if fails:
        print(f"❌ VALIDATION FAILED: {len(fails)} check(s) failed: {fails}")
        sys.exit(1)
    print("✅ VALIDATION PASSED: all schema / leakage / integrity / temporal assertions hold.")


def _bar(p, width=30):
    return "█" * int(round(p / 100 * width))


def report(al, ce, fi, pv, ser2bs, dataset):
    n = len(al)
    # severity
    sev = Counter(r["alarm_severity"] for r in al)
    print("severity distribution (export vs BT reference):")
    for s in SEV_ORDER:
        pc = 100 * sev.get(s, 0) / n
        print(f"  {s:9} {pc:5.1f}%  {_bar(pc)}   (BT ref ~{REF_SEVERITY.get(s, 0)}%)")
    # alarm names
    names = Counter(r["alarm_name"] for r in al)
    print(f"\ndistinct alarm names observed: {len(names)} / {N_ALARM_TYPES} available")
    print("top-20 alarm-name shares:")
    for nm, c in names.most_common(20):
        print(f"  {100*c/n:5.1f}%  {nm}")
    # per-BS
    perbs = Counter(r["source_bs_id"] for r in al)
    vals = sorted(perbs.values())
    print(f"\nalarms-per-BS: n_BS={len(perbs)}  median={st.median(vals)}  "
          f"P90={vals[min(len(vals)-1, int(0.9*len(vals)))]}  max={max(vals)}  (BT ref: median in the tens)")
    print("  per-BS count log-histogram:")
    buckets = [(1, 3), (3, 10), (10, 30), (30, 100), (100, 300), (300, 10**9)]
    for lo, hi in buckets:
        c = sum(1 for v in vals if lo <= v < hi)
        print(f"    [{lo:>4}-{hi if hi < 10**9 else '∞':>4}) {'#'*c} {c}")
    # day-hour matrix
    print("\nday-hour count matrix (rows=weekday Mon..Sun, cols=hour 0..23):")
    mat = [[0]*24 for _ in range(7)]
    for r in al:
        o = pdt(r["occurred_timestamp"])
        mat[o.weekday()][o.hour] += 1
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    print("       " + " ".join(f"{h:>3}" for h in range(24)))
    for i, row in enumerate(mat):
        print(f"  {days[i]}  " + " ".join(f"{v:>3}" for v in row))
    # organic vs attributed
    norg = sum(1 for p in pv if p["fault_id"] == "organic")
    print(f"\norganic vs fault-attributed: {norg} organic / {len(pv)-norg} attributed "
          f"({100*(len(pv)-norg)/len(pv):.1f}% attributed)")
    # per-fault-class mean first_alarm_delay_s
    cls = {r["fault_id"]: r["fault_class"] for r in fi}
    byc = defaultdict(list)
    for r in ce:
        byc[cls.get(r["fault_id"], "?")].append(float(r["first_alarm_delay_s"]))
    print("per-fault-class mean first_alarm_delay_s:")
    for k, v in sorted(byc.items()):
        print(f"  {k:16} mean={st.mean(v):6.2f}s  (n={len(v)})")

    # dataset side-by-side
    if dataset and os.path.exists(dataset):
        try:
            import pandas as pd
            print(f"\n── historical dataset comparison ({dataset}) ──")
            df = pd.read_csv(dataset, usecols=["Severity", "Name", "Alarm Source"],
                             encoding="utf-8-sig", low_memory=False)
            dn = len(df)
            print("dataset severity distribution:")
            for s, c in df["Severity"].value_counts().items():
                print(f"  {s:9} {100*c/dn:5.1f}%")
            print(f"dataset distinct alarm names: {df['Name'].nunique()}")
            pbs = df.groupby("Alarm Source").size()
            print(f"dataset alarms-per-BS: median={pbs.median():.0f}  P90={pbs.quantile(0.9):.0f}  n_BS={len(pbs)}")
        except ImportError:
            print("\n(historical dataset comparison skipped — pandas not installed)")
    else:
        print("\n(historical dataset comparison skipped — no --dataset given)")


if __name__ == "__main__":
    main()
