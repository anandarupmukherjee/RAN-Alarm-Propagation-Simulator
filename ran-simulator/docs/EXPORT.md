# BT-comparable export layer

The batch exporter ([`tools/batch_export.py`](../tools/batch_export.py)) runs the ns-3
LTE core in deterministic, headless batch mode and writes a **BT-comparable alarm log**
(`alarms.csv`) plus a set of **ground-truth companion files** that record the true causal
structure for validation experiments. The 13-field alarm log is the only file an external
analysis pipeline needs; the companion files are the evaluation key and must **never** be
fed to a model.

The live interactive simulator (SSE stream, UI) is unaffected — the export path is
additive and config-gated.

---

## 1. Output layout

Each run writes `exports/run_<id>/`:

| File | Purpose | Fed to model? |
|------|---------|:---:|
| `alarms.csv` | The 13-field BT alarm record (+ `episode_id`), all episodes, sorted by occurrence | ✅ yes |
| `fault_injections.csv` | Every injected fault: class, target, inject/remove time, `group_id` | ❌ key |
| `alarm_provenance.csv` | Per alarm: `fault_id`\|`organic`, raw ns-3 trace, sim-time | ❌ key |
| `causal_edges.csv` | Realised true propagation edges per fault | ❌ key |
| `topology.csv`, `topology_edges.csv` | Per-episode node/edge geometry | ❌ key |
| `run_manifest.json` | Seeds, anchors, mapping constant, git commit, anonymisation map, field provenance, config snapshot | ❌ key |

---

## 2. The 13-field alarm record (`alarms.csv`)

`episode_id` is an extra leading column (opted in); the 13 BT fields follow in order.

| # | Column | Provenance | Rule |
|---|--------|-----------|------|
| 1 | `alarm_severity` | dataset_calibrated | Dataset modal severity for the alarm name; vocabulary {Critical, Major, Minor, Warning} |
| 2 | `alarm_name` | dataset_calibrated | Frequency-weighted sample from the event-type candidate list |
| 3 | `source_bs_id` | mechanistic | Anonymised `sNNNN` (stable per run; raw→anon map in the manifest) |
| 4 | `source_bs_type` | dataset_calibrated | Dataset modal NE type for the alarm name |
| 5 | `alarm_location_info` | dataset_calibrated | Per-alarm template filled from node id + event metadata |
| 6 | `occurred_timestamp` | mechanistic | ns-3 sim-time mapped to the calendar (§3), ISO-8601 UTC |
| 7 | `cleared_timestamp` | mixed | Mechanistic at fault removal for transport faults; otherwise **synthesised** (or empty) |
| 8 | `acknowledged_timestamp` | synthesised | Sampled ack-delay log-normal per severity (independent of clear) |
| 9 | `cleared_status` | derived | `cleared` iff field 7 populated and ≤ horizon, else `uncleared` |
| 10 | `acknowledged_status` | derived | as field 9 for ack |
| 11 | `alarm_log_serial` | mechanistic | Monotonic 1..N over the whole export, ordered by occurrence |
| 12 | `equipment_alarm_serial` | mechanistic | Per-`source_bs_id` monotonic counter |
| 13 | `bs_maintenance_status` | mechanistic | `in_service`, or `under_maintenance` in maintenance windows |

> **Synthesised fields.** The historical dataset has **no cleared/acknowledged timestamps**,
> so fields 7 (non-transport) and 8 are synthesised from configurable per-severity
> log-normal delays (`clear_delay_lognorm_s`, `ack_delay_lognorm_s`). This is recorded in
> `run_manifest.json::field_provenance`.

---

## 3. Calendar mapping (episode placement)

A run is N independent episodes, each with its own RNG seed (`base_seed + ep`). Each
episode is placed on the calendar:

- **Weekday** = `ep % 7` (round-robin → all 7 weekdays covered once N ≥ 7).
- **Week** within `calendar_span_weeks` and **hour** (uniform, or `anchor_hour_weights`)
  are drawn from a seeded RNG.
- **Within an episode, sim-seconds map 1:1 to calendar seconds** from the anchor — so true
  cascade delays are preserved, never stretched across the span.
- `occurred_timestamp = anchor + sim_seconds`.

Anchors, seeds and the mapping constant (1.0 s/sim-s) are written to the manifest. This
keeps per-BS hour-of-day (24-bin) and day-of-week (7-bin) histograms non-degenerate.

---

## 4. Attribution rule (ground truth)

Attribution is computed **at event-capture time** in the ns-3 core (it knows the active
fault set). An alarm is attributed to a fault if its triggering ns-3 event occurred on the
faulted element (or a served/handover cell of it) **within the fault's active window plus a
grace period** (`grace_period_s`, default 5 s after removal); otherwise `organic`.
`causal_edges.csv` is derived from `alarm_provenance.csv` + `fault_injections.csv`: for an
edge (X2) fault, one row is emitted per endpoint as source.

---

## 5. Scenario types (`export.scenario`)

| Scenario | Behaviour |
|----------|-----------|
| `clean` (default) | Organic dynamics + scripted single-element faults (`fault_schedule`: backhaul/X2/radio cuts at randomised times). Genuine propagation. |
| `regional_power` | Clean background **plus** a simultaneous fault across a random connected cluster of K nodes (same sim-second, shared `group_id`) — a shared-cause confounder. The cluster nodes have **no causal edges between them** (only group membership), so a false-link experiment can test for spurious links. |
| `maintenance` | Clean background **plus** per-node maintenance windows: `bs_maintenance_status = under_maintenance` and a burst of **injection-only label alarms** drawn from the Hardware / Power & Environment catalogue categories (names the radio/transport path never produces). Windows recorded in the manifest. The burst severity mix is tunable via `maintenance.severity_weights` (weight = dataset frequency × severity weight); note these categories contain **no Critical** alarms, so only Major/Minor/Warning are reachable. |

Per-cell, per-type alarm rate limiting (`churn_holdoff_s`) keeps per-BS volumes BT-like
(median in the tens per episode) without losing fault onset.

**Diurnal modulation (optional).** `diurnal_load_weights` (a 24-vector of relative hourly
load, or `null` = off) scales each episode's UE mobility intensity by its anchor hour — a
proxy for diurnal traffic, so busier hours produce more handover/RLF activity and a
realistic hour-of-day alarm pattern.

**Transport topology.** `topology_edges.csv` carries both `x2` edges (the inter-eNB links)
and `backhaul` edges from each eNB to a virtual EPC/core element (`s_epc`, also listed in
`topology.csv`) — so an analysis pipeline can reason about S1-U-backhaul-shared faults
(`backhaul_cut` targets a single eNB).

---

## 6. Running a batch export

The exporter runs inside the `ns3-sim` image (it has the compiled binary; no pandas needed):

```bash
cd ran-simulator
# build once (ns-3 is cached after the first ~20-40 min)
docker compose build ns3-sim
# run an export (output to ./exports on the host)
docker compose run --rm -v "$PWD/exports:/out" ns3-sim \
    /ns3/run_export.sh --out /out --run-id 000 --scenario clean --episodes 50
```

Useful flags: `--episodes`, `--sim-seconds`, `--scenario {clean,regional_power,maintenance}`,
`--git-commit <hash>`, `--verify` (prints episode-placement + 1:1-mapping diagnostics).
All distribution parameters and schedules live in [`config/export.json`](../config/export.json).

**Reproducibility:** ns-3 `RngRun` (per episode) + seeded Python RNGs + stable sort ⇒
byte-identical `alarms.csv` for a given config + `base_seed` (wall-clock appears only in the
manifest).

---

## 7. Validation

```bash
# schema / no-leakage / referential-integrity / temporal assertions + comparability report
python3 tools/validate_export.py exports/run_000 [--dataset analysis_data.csv]
# prove the export is self-sufficient for the downstream pipeline (reads only alarms.csv)
python3 tools/run_pipeline_smoke.py exports/run_000/alarms.csv
```

`validate_export.py` exits non-zero on any assertion failure; the statistical comparability
section is reported, never asserted.

---

## 8. Known limitations

- **Physically-grounded fault domains are radio and transport only.** Hardware, DC power,
  ALD/antenna-line, licensing and environmental alarms are **injection-only labels**
  (raised by the `maintenance` scenario from catalogue categories) — an LTE radio simulator
  has no such components to fail. Consequently the `clean` scenario shows ~10 of 171 alarm
  names and a Minor/Major-heavy severity mix; the real dataset is ~80 % Major across 171
  names. This deviation is **reported by the validator, not forced**.
- **ns-3 LTE is CPU-bound** — batch episodes are practical at the demo/town scale (~10–30
  base stations); larger topologies run but slowly.
- **Cross-node propagation** in any downstream analysis should be evaluated against
  `causal_edges.csv`; temporal co-occurrence (e.g. `regional_power` clusters) is a shared
  cause, not a causal edge.
