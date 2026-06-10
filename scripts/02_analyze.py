"""
02_analyze.py
─────────────
Compute, from analysis_data.csv (NEVER test_holdout.csv), two artifacts:

  backend/data/analytics.json    — insights for the portal "Analytics" panel:
        1. common alarm propagation pathways  (Name -> Next_Alarm)
        2. node resilience                     (per NE type + best/worst sites)
        3. time between alarms within a node   (Hours_since_prior distribution)
        4. time between alarms propagated between nodes (cross-node timing)

  backend/data/mapper_stats.json — full-dataset calibration tables so the
        backend AlarmMapper knows all real alarm types (frequency / severity /
        NE type / Markov transitions / inter-arrival timing) without re-parsing
        the 951k-row CSV at startup.
"""
import json
import os
import numpy as np
import pandas as pd

SRC = "analysis_data.csv"
OUT_DIR = "ran-simulator/backend/data"
os.makedirs(OUT_DIR, exist_ok=True)

print("loading", SRC, "…")
df = pd.read_csv(SRC, encoding="utf-8-sig", low_memory=False)
df["Name"] = df["Name"].astype(str)
df["Next_Alarm"] = df["Next_Alarm"].astype(str)
df["Alarm Source"] = df["Alarm Source"].astype(str)
df["NE Type"] = df["NE Type"].astype(str)
n = len(df)
print("rows:", n)

VALID = (df["Hours_since_prior"] > 0) & (df["Hours_since_prior"] < 7200)


# ════════════════════════════════════════════════════════════════════════════
# mapper_stats.json — calibration tables (full data)
# ════════════════════════════════════════════════════════════════════════════
freq = df["Name"].value_counts()
alarm_frequency = freq.to_dict()

alarm_severity, alarm_ne_type = {}, {}
for name, grp in df.groupby("Name"):
    alarm_severity[name] = grp["Severity"].mode().iloc[0]
    alarm_ne_type[name] = grp["NE Type"].mode().iloc[0]

# Markov transitions Name -> {Next_Alarm: prob} (vectorised)
pair_counts = df.groupby(["Name", "Next_Alarm"]).size()
transitions = {}
for (src, nxt), c in pair_counts.items():
    transitions.setdefault(src, {})[nxt] = int(c)
for src, nexts in transitions.items():
    tot = sum(nexts.values())
    transitions[src] = {k: v / tot for k, v in nexts.items()}

# Inter-arrival timing samples per alarm (cap to keep file small)
CAP = 250
timing = {}
for name, grp in df[VALID].groupby("Name"):
    vals = grp["Hours_since_prior"].tolist()
    if len(vals) > CAP:
        step = len(vals) / CAP
        vals = [vals[int(i * step)] for i in range(CAP)]
    if vals:
        timing[name] = [round(float(v), 3) for v in vals]
global_timing = df.loc[VALID, "Hours_since_prior"]
gt = global_timing.tolist()
if len(gt) > 5000:
    step = len(gt) / 5000
    gt = [gt[int(i * step)] for i in range(5000)]
global_timing = [round(float(v), 3) for v in gt]

mapper_stats = {
    "alarm_frequency": {k: int(v) for k, v in alarm_frequency.items()},
    "alarm_severity": alarm_severity,
    "alarm_ne_type": alarm_ne_type,
    "transitions": transitions,
    "timing": timing,
    "global_timing": global_timing,
    "source_count": int(df["Alarm Source"].nunique()),
}
with open(f"{OUT_DIR}/mapper_stats.json", "w") as f:
    json.dump(mapper_stats, f)
print("wrote mapper_stats.json —", len(alarm_frequency), "alarm types")


# ════════════════════════════════════════════════════════════════════════════
# 1. Common alarm propagation pathways
# ════════════════════════════════════════════════════════════════════════════
pathways = []
for (src, nxt), c in pair_counts.items():
    if nxt in ("Noalarm", "nan") or src == nxt:
        continue
    prob = c / freq[src]
    pathways.append({"from": src, "to": nxt, "count": int(c), "prob": round(float(prob), 4)})
pathways.sort(key=lambda x: x["count"], reverse=True)
pathways = pathways[:40]

# Clearance (self-healing) rate: fraction of each alarm that resolves to Noalarm
clearance = {}
for src, nexts in transitions.items():
    clearance[src] = nexts.get("Noalarm", 0.0)


# ════════════════════════════════════════════════════════════════════════════
# 2. Node resilience
# ════════════════════════════════════════════════════════════════════════════
def resilience_table(group_col, min_alarms):
    rows = []
    is_crit = (df["Severity"] == "Critical")
    clears = (df["Next_Alarm"] == "Noalarm")
    for key, idx in df.groupby(group_col).groups.items():
        sub = df.loc[idx]
        cnt = len(sub)
        if cnt < min_alarms:
            continue
        recovery = float(clears.loc[idx].mean())          # clears without chaining
        crit = float(is_crit.loc[idx].mean())
        gaps = sub.loc[VALID.loc[idx], "Hours_since_prior"]
        median_gap = float(gaps.median()) if len(gaps) else 0.0
        # resilience index 0-100: recovers fast, alarms rarely, few criticals
        res = 100 * (0.5 * recovery + 0.3 * min(median_gap / 72.0, 1.0) + 0.2 * (1 - crit))
        rows.append({
            "key": str(key), "alarms": cnt,
            "recovery_pct": round(recovery * 100, 1),
            "median_gap_h": round(median_gap, 1),
            "critical_pct": round(crit * 100, 1),
            "resilience": round(res, 1),
        })
    return rows

by_ne = resilience_table("NE Type", min_alarms=1)
by_ne.sort(key=lambda x: x["resilience"], reverse=True)

site_rows = resilience_table("Alarm Source", min_alarms=100)
site_rows.sort(key=lambda x: x["resilience"], reverse=True)
most_resilient = site_rows[:12]
least_resilient = site_rows[-12:][::-1]

resilience = {
    "by_ne_type": [{**r, "ne_type": r.pop("key")} for r in by_ne],
    "most_resilient": [{**r, "site": r.pop("key")} for r in most_resilient],
    "least_resilient": [{**r, "site": r.pop("key")} for r in least_resilient],
    "method": ("Resilience index 0-100 = 50%·self-clear rate (alarm resolves to "
               "Noalarm) + 30%·median inter-alarm gap (capped 72h) + 20%·(1 − critical "
               "rate). Sites need ≥100 alarms to rank."),
}


# ════════════════════════════════════════════════════════════════════════════
# 3. Time between alarms within a node  (Hours_since_prior)
# ════════════════════════════════════════════════════════════════════════════
hsp = df["Hours_since_prior"]
valid_h = hsp[(hsp > 0) & (hsp < 7200)]
buckets = [("<1h", 0, 1), ("1–6h", 1, 6), ("6–24h", 6, 24),
           ("1–7d", 24, 168), ("7–30d", 168, 720), (">30d", 720, 7200)]
hist = []
for label, lo, hi in buckets:
    c = int(((valid_h >= lo) & (valid_h < hi)).sum())
    hist.append({"bucket": label, "count": c, "pct": round(100 * c / len(valid_h), 1)})

by_ne_timing = []
for ne, grp in df.groupby("NE Type"):
    g = grp["Hours_since_prior"]
    g = g[(g > 0) & (g < 7200)]
    if len(g):
        by_ne_timing.append({"ne_type": ne, "median_h": round(float(g.median()), 2),
                             "mean_h": round(float(g.mean()), 2), "n": int(len(g))})
by_ne_timing.sort(key=lambda x: x["median_h"])

intra_node_timing = {
    "stats": {
        "n": int(len(valid_h)),
        "mean_h": round(float(valid_h.mean()), 2),
        "median_h": round(float(valid_h.median()), 2),
        "p25_h": round(float(valid_h.quantile(0.25)), 2),
        "p75_h": round(float(valid_h.quantile(0.75)), 2),
        "p90_h": round(float(valid_h.quantile(0.90)), 2),
    },
    "histogram": hist,
    "by_ne_type": by_ne_timing,
    "burst_count": int((hsp == 0).sum()),
    "first_or_long_gap_count": int((hsp >= 7200).sum()),
    "note": ("Hours_since_prior = hours since the previous alarm on the same network "
             "element. 0 = simultaneous burst; 7200 = sentinel (first sighting / >300d)."),
}


# ════════════════════════════════════════════════════════════════════════════
# 4. Time between alarms propagated BETWEEN nodes
# ════════════════════════════════════════════════════════════════════════════
print("parsing timestamps for cross-node timing …")
ts = pd.to_datetime(df["Occurred On (NT)"], format="%Y/%m/%d %H:%M", errors="coerce")
order = ts.argsort(kind="stable")
ts_s = ts.iloc[order].reset_index(drop=True)
src_s = df["Alarm Source"].iloc[order].reset_index(drop=True)

delta_min = ts_s.diff().dt.total_seconds().to_numpy() / 60.0
diff_node = (src_s.values[1:] != src_s.values[:-1])
gaps = delta_min[1:]
valid_gap = np.isfinite(gaps) & (gaps >= 0)

# inter-node consecutive alarms within 60 min = plausible propagation
inter_mask = diff_node & valid_gap & (gaps <= 60)
inter_gaps = gaps[inter_mask]
all_inter = gaps[diff_node & valid_gap]

ibuckets = [("<1 min", 0, 1), ("1–5 min", 1, 5), ("5–15 min", 5, 15),
            ("15–60 min", 15, 60)]
ihist = []
for label, lo, hi in ibuckets:
    c = int(((inter_gaps >= lo) & (inter_gaps < hi)).sum())
    ihist.append({"bucket": label, "count": c,
                  "pct": round(100 * c / max(len(inter_gaps), 1), 1)})

inter_node_timing = {
    "stats": {
        "pairs_within_60min": int(len(inter_gaps)),
        "median_min": round(float(np.median(inter_gaps)), 2) if len(inter_gaps) else None,
        "mean_min": round(float(np.mean(inter_gaps)), 2) if len(inter_gaps) else None,
        "p90_min": round(float(np.percentile(inter_gaps, 90)), 2) if len(inter_gaps) else None,
        "pct_inter_node_within_60min": round(100 * len(inter_gaps) / max(len(all_inter), 1), 1),
    },
    "histogram": ihist,
    "method": ("Cross-node propagation is estimated from temporally-consecutive alarms "
               "on DIFFERENT network elements within 60 minutes (global time order, "
               "minute-resolution timestamps). A heuristic proxy for fault spread, not "
               "a verified causal link."),
}


# ════════════════════════════════════════════════════════════════════════════
analytics = {
    "meta": {
        "rows_analysed": n,
        "alarm_types": int(df["Name"].nunique()),
        "sources": int(df["Alarm Source"].nunique()),
        "ne_types": sorted(df["NE Type"].unique().tolist()),
        "source_file": SRC,
        "holdout_excluded": "test_holdout.csv (2000 rows, never analysed)",
    },
    "pathways": pathways,
    "clearance_rates": {k: round(v, 4) for k, v in
                        sorted(clearance.items(), key=lambda x: -x[1])[:20]},
    "resilience": resilience,
    "intra_node_timing": intra_node_timing,
    "inter_node_timing": inter_node_timing,
}
with open(f"{OUT_DIR}/analytics.json", "w") as f:
    json.dump(analytics, f, indent=2)
print("wrote analytics.json")
print("  pathways:", len(pathways), "| NE types ranked:", len(by_ne),
      "| sites ranked:", len(site_rows))
print("  intra-node median gap (h):", intra_node_timing["stats"]["median_h"])
print("  inter-node median gap (min):", inter_node_timing["stats"]["median_min"],
      "| pairs:", inter_node_timing["stats"]["pairs_within_60min"])
