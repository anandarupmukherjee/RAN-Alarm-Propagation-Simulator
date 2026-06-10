"""
01_split_holdout.py
───────────────────
Split RAN_data.csv into:
  • test_holdout.csv   — 2000 randomly-sampled rows reserved as FUTURE TEST DATA.
                         This file must NEVER be inspected or used for analysis.
  • analysis_data.csv  — every other row, used for calibration + analytics.

The script prints ONLY row counts and never echoes holdout content.
Deterministic (fixed seed) so the split is reproducible.
"""
import pandas as pd

SRC      = "RAN_data.csv"
HOLDOUT  = "test_holdout.csv"
ANALYSIS = "analysis_data.csv"
N_HOLD   = 2000
SEED     = 42

df = pd.read_csv(SRC, encoding="utf-8-sig", low_memory=False)
total = len(df)

holdout = df.sample(n=N_HOLD, random_state=SEED)
analysis = df.drop(holdout.index)

# Write holdout WITHOUT ever printing its contents.
holdout.to_csv(HOLDOUT, index=False, encoding="utf-8-sig")
analysis.to_csv(ANALYSIS, index=False, encoding="utf-8-sig")

print(f"source rows   : {total}")
print(f"holdout rows  : {len(holdout)}  -> {HOLDOUT}  (RESERVED, do not inspect)")
print(f"analysis rows : {len(analysis)} -> {ANALYSIS}")
assert len(holdout) + len(analysis) == total, "row count mismatch"
assert len(holdout) == N_HOLD
print("split OK")
