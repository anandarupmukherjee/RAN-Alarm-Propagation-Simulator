# RAN Alarm Propagation Simulator

A real-time **Radio Access Network (RAN) alarm propagation simulator**. It runs an
[ns-3](https://www.nsnam.org/) LTE radio-access simulation — real eNodeB base stations,
UEs, an EPC core, RF propagation, handovers and radio-link failures — and turns the
genuine network events ns-3 produces into operator-style **alarm logs** that mirror the
schema of a real BT RAN alarm dataset.

The platform streams those alarms live to a browser dashboard with an interactive
topology editor, fault injection, and analytics drawn from the historical alarm dataset.

---

## Architecture

Four containers, orchestrated by [`docker-compose.yml`](ran-simulator/docker-compose.yml):

| Service     | Stack             | Role |
|-------------|-------------------|------|
| `redis`     | Redis 7           | Message bus — ns-3 events on the `ns3:events` list |
| `ns3-sim`   | Flask + **ns-3.41 (LTE)** | The simulation core. Runs the LTE RAN scenario and emits RAN events/alarms |
| `backend`   | FastAPI           | Alarm enrichment + Server-Sent-Events stream to the UI |
| `frontend`  | Nginx + vanilla JS | Topology editor, live alarm console, dashboard, analytics |

```
ns-3 LTE sim ──events──▶ Redis ──▶ FastAPI (enrich) ──SSE──▶ Browser UI
  (eNBs, UEs, EPC,                                            (topology, console,
   RLF, handover, SINR)                                        dashboard, analytics)
```

### Flow

1. **ns-3 core** ([`ns3-sim/ran-alarm-sim.cc`](ran-simulator/ns3-sim/ran-alarm-sim.cc))
   simulates an LTE RAN: EPC, eNodeB base stations, UEs with mobility, RF propagation,
   X2 handover and 3GPP radio-link-failure detection. Real LTE **trace sources** are
   connected to alarm emitters — every alarm is triggered by a genuine ns-3 event.
2. Events are published to **Redis** by the control server ([`sim_server.py`](ran-simulator/ns3-sim/sim_server.py)),
   which runs the compiled ns-3 binary directly (ns-3 is mandatory — no fallback).
3. The **FastAPI backend** enriches each event into a full alarm record — name, severity,
   NE type and Markov `Next_Alarm` calibrated against the real dataset — and streams it to
   clients over SSE. It also appends every alarm to a server-side log in the dataset schema.
4. The **frontend** renders the live topology, alarm console, severity dashboard, SINR
   heatmap, and a dataset-analytics modal. Users can edit topology, inject faults, and
   export the generated alarms as CSV.

### Real ns-3 LTE event → alarm mapping

**Radio layer** — alarms triggered by genuine LTE protocol events:

| ns-3 LTE trace source            | Event              | Example alarm name(s)                              |
|----------------------------------|--------------------|---------------------------------------------------|
| `LteUeRrc::RadioLinkFailure`     | radio_link_failure | Radio Link Failure / Radio Signaling Link Disconnected |
| `LteUeRrc::HandoverEndError`     | handover_failure   | Cell PS Service Faulty                             |
| `LteUeRrc::ConnectionTimeout`    | rrc_connection_timeout | Cell PS Service Faulty / Cell Unavailable      |
| `LteUeRrc::RandomAccessError`    | random_access_problem | Cell RX Channel Interference Noise Power Unbalanced |
| `LteEnbRrc::NotifyConnectionRelease` | connection_release_abnormal | Cell PS Service Faulty                |
| `LteUePhy::ReportCurrentCellRsrpSinr` (low SINR) | sinr_drop | Cell RX Channel Interference Noise Power Unbalanced |

**Transport layer** — alarms triggered by genuine point-to-point link cuts. Each eNB's
**S1-U backhaul** and **X2** interfaces are real ns-3 channels; a fault attaches a
100%-loss error model to both ends, so packets are genuinely dropped:

| Modelled fault (real link cut)   | Observed consequence                | Alarm name(s)                       |
|----------------------------------|-------------------------------------|-------------------------------------|
| S1-U backhaul down (per eNB)     | measured 0 kbps to served UEs       | S1 Interface Fault → Cell PS Service Faulty |
| X2 link down (eNB pair)          | handovers between them fail (`HandoverEndError`) | Remote Maintenance Link Failure / Cell PS Service Faulty |

The backhaul-outage alarm (`cell_service_outage`) is only raised after the sim **measures**
that no user-plane data reached the cell's UEs — so it reflects an observed outage, not just
the trigger.

So the **trigger, node, timing, SINR and link state are real ns-3**, while the alarm
*vocabulary* (names / severities / NE types / `Next_Alarm` chains) mirrors the historical
BT dataset. Radio-type injections black out the eNB downlink (→ real RLF); transport-type
injections cut the real backhaul/X2 links.

**Scope:** the radio and transport fault domains are physically grounded in ns-3. The
remaining dataset categories (hardware, DC power, ALD/antenna-line, licensing, environment)
are not modelled by an LTE radio simulator and are reachable only via manual injection as
labels — ns-3 has no power bus, board, or antenna-line device to fail.

The session alarm log is available at **`GET /api/alarm-log.csv`** (dataset schema).

---

## How it works — algorithm & engine

This section documents the full processing pipeline and the simulation engine. The
first half is written for **developers and users**; the second half for
**researchers** who need the modelling assumptions, formulas and limitations.

### A. Developer & user view

#### A.1 End-to-end pipeline

```
                 ns-3 LTE C++ core (ran-alarm-sim)
   ┌───────────────────────────────────────────────────────────┐
   │  EPC + eNBs + UEs + RF propagation + mobility + RLF + X2   │
   │  LTE trace sources ──► event objects ──► stdout JSON lines │
   └───────────────────────────────────────────────────────────┘
        │  "@@ALARM@@ {json}"  /  "@@CLOCK@@ <sim_seconds>"
        ▼
   sim_server.py (Flask)  ── parses stdout, RPUSH ─►  Redis list  ns3:events
        ▲ REST control (/start /stop /inject /link-failure /speed /status)
        │
   FastAPI backend ── BLPOP ns3:events ─► AlarmMapper.map_event() ─► enriched alarm
        │                                                      │
        │  per-source one-behind buffer ─► ns3_alarm_log.csv   │ (dataset schema)
        ▼                                                      ▼
   SSE  /api/stream/{session}  ─────────────────────────►  Browser UI
                                          (console, topology, dashboard, watcher,
                                           live analytics, CSV export)
```

A single ns-3 event becomes a fully-formed RAN alarm record. The **trigger, node,
timing and SINR/link-state are produced by ns-3**; the **alarm vocabulary** (name,
severity, NE type, `Next_Alarm`) is drawn from tables calibrated on the real dataset.

#### A.2 The ns-3 core process

`ns3-sim/ran-alarm-sim.cc` is a standard ns-3 program (compiled into the image,
run directly — no cppyy). On launch it:

1. reads a line-format **scenario** (`NODE id x y`, `EDGE a b`, `UEPERENB`, `UESPEED`,
   `SPEED`, `REALTIME`) written by `sim_server`;
2. builds the LTE/EPC scenario (§B.1) and connects to LTE trace sources;
3. runs the simulator, printing one JSON line per RAN event and a periodic clock
   heartbeat;
4. every 0.5 sim-seconds polls a **commands file** for fault injections;
5. stops on `SIGTERM`.

Communication with `sim_server` is line-oriented stdout with markers:
`@@ALARM@@ {event-json}`, `@@CLOCK@@ <sim_seconds>`, `@@READY@@ …`.

#### A.3 Alarm-mapping algorithm

For each raw ns-3 event, `backend/alarm_mapper.py::map_event` runs:

1. **Resolve a name.** The event's `event_type` (e.g. `radio_link_failure`) indexes a
   candidate list; one candidate is chosen by **frequency-weighted sampling** —
   `P(name) ∝ dataset_frequency(name)`. (`manual_injection` uses the operator's exact
   name; `dataset_alarm` samples across all 171 types.)
2. **Severity** = the dataset **modal** severity for that alarm name.
3. **NE type** = the dataset modal NE type for that alarm name.
4. **`Next_Alarm`** = a draw from the alarm's **Markov row** `P(next | name)` learned
   from the dataset's `Next_Alarm` column.
5. **Location Information** = a per-alarm template filled from the node id and event
   metadata.

#### A.4 Wall-clock pacing

ns-3 LTE cannot run at 1× wall-clock for many eNBs, and `RealtimeSimulatorImpl`
stalls. Instead the sim runs free and event **emission** is paced in software
(`PaceWall`): for consecutive events at sim-times `t₁, t₂`,

```
wall_gap = min( (t₂ − t₁) / speed , PACE_CAP )          PACE_CAP = 2 s
```

so the inter-alarm wall spacing tracks the inter-alarm sim spacing compressed by
`speed` (sim-seconds per wall-second, the “×” slider), while quiet periods are capped
at 2 s. Genuine bursts/cascades (events microseconds apart in sim-time) stay visually
together. `speed` is the CPU ceiling — if ns-3 is slower than requested, emission
simply runs at the achievable rate.

#### A.5 Fault injection

- **Radio fault** (`inject`, radio-type) → the target eNB's downlink `TxPower` is
  dropped to ~1 dBm for 5 s → its UEs genuinely lose sync → real RLF/release alarms.
- **Transport fault** (`inject` transport-type, or `link-failure`) → a 100%-loss
  error model is enabled on the real S1-U backhaul and/or X2 link → real packet loss;
  the cell's delivered throughput is **measured** and a confirmed outage alarm is
  raised (§B.4).

The operator's requested alarm is also surfaced immediately as a `manual_injection`
marker, and the downstream alarms it causes are genuine ns-3 events.

#### A.6 Server-side alarm log

`redis_consumer.py` appends every enriched alarm to `ns3_alarm_log.csv` in the exact
dataset schema. `Next_Alarm` is filled the way the source dataset was built: each row
is **held one step** and finalised when the next alarm on the same source arrives.
Download it at `GET /api/alarm-log.csv`.

#### A.7 Control plane & sessions

`POST /api/simulate/start` configures + starts the ns-3 core and opens an **SSE
session** with its own queue; the consumer broadcasts every alarm to all sessions.
Other endpoints: `inject`, `link-failure`, `speed`, `stats`, `DELETE` (stop),
`/api/analytics`, `/api/alarm-catalog`, `/api/demo-topology`.

#### A.8 Key parameters

| Parameter | Value | Where |
|---|---|---|
| Time compression `speed` | 30× default (UI slider) | scenario `SPEED` |
| Pace cap | 2 s | `PACE_CAP_S` |
| Interference alarm threshold / holdoff | SINR < 5 dB / 3 s per cell | `g_sinrAlarmThreshDb` |
| Warm-up suppression | 1.0 sim-s | `g_warmupS` |
| UEs per eNB | 4 / 3 / 2 / 1 for ≤20 / ≤60 / ≤120 / >120 nodes | `sim_server` |
| Coordinate scale | 5 m per canvas pixel | `ran-alarm-sim.cc` |

#### A.9 Extending

- **New event→alarm mapping:** add the `event_type` and candidate names to
  `EVENT_TO_ALARM_CANDIDATES` in `alarm_mapper.py`, emit that `event_type` from the C++.
- **New node type:** add to `NODE_TYPES` in `frontend/app.js` (shape/colour/size).
- **New topology:** add a generator to the `TOPOLOGIES` registry in `frontend/app.js`.
- **Recalibrate:** re-run `scripts/02_analyze.py` to regenerate `mapper_stats.json`
  and `analytics.json`.

---

### B. Academic / researcher view

#### B.1 The ns-3 LTE scenario

One ns-3 LTE cell per topology node, an EPC (`PointToPointEpcHelper`: PGW/SGW +
S1-U/S1-AP), and `UEs/eNB` UEs (§A.8) attached to their home cell. Radio configuration:

| Aspect | Setting |
|---|---|
| Bandwidth | 25 PRB (5 MHz) downlink & uplink |
| eNB / UE Tx power | 43 dBm / 23 dBm |
| Path-loss model | `LogDistancePropagationLossModel`, exponent **3.9**, reference loss **38.57 dB @ 1 m** |
| MAC scheduler | Proportional-Fair (`PfFfMacScheduler`) |
| Handover | `A3RsrpHandoverAlgorithm` (event-A3, RSRP) over X2 |
| RRC | real RRC (`UseIdealRrc=false`); Ctrl + Data error models enabled |
| Geometry | eNB mast 30 m, UE 1.5 m, positions = canvas × 5 m |

Mobility: `RandomWalk2dMobilityModel`, 45 m/s, re-orienting every 80 m, bounded to the
topology bounding box + 150 m. UEs are seeded on a **golden-angle spiral** 60–390 m
from their home eNB so a fraction begin at cell edges (front-loading edge effects).

#### B.2 Radio-link-failure model

RLF follows 3GPP TS 36.331 out-of-sync handling. `LteUePhy` issues out-of-sync
indications when downlink quality drops; after **N310 = 2** consecutive out-of-sync
indications, timer **T310 = 500 ms** starts, and on expiry (without **N311 = 1**
in-sync recovery) `LteUeRrc::RadioLinkFailure` fires. These are deliberately sensitive
3GPP-valid values so RLF is reachable at cell edge within a tractable run.

#### B.3 Event → alarm calibration

Calibration tables (`scripts/02_analyze.py` → `mapper_stats.json`) are estimated from
the analysis split (951,468 alarms; holdout excluded):

- **Frequency** `f(a)` = empirical count of alarm `a`; used as sampling weights.
- **Severity / NE type** = modal value per alarm (`argmax` of the conditional).
- **Transition matrix** `T(a, a') = count(a→a') / Σ count(a→·)` from the `Next_Alarm`
  column — a first-order Markov chain over alarm names.
- **Inter-arrival samples** per alarm (capped) from `Hours_since_prior`.

The simulator therefore separates **mechanism** (ns-3 physics decides *when*, *where*
and *which fault class*) from **labelling** (the dataset decides the *name/severity/NE
and the stochastic successor*). This is a calibrated digital-twin construction, not a
replay of the dataset.

#### B.4 Transport-fault model & outage detection

S1-U backhaul and X2 links are real `PointToPoint` channels. A fault attaches a
`RateErrorModel` (`ERROR_UNIT_PACKET`, rate 1.0) to **both endpoints**, dropping all
packets. Backhaul outage is **verified by observation**: at fault time the cumulative
downlink bytes of the UEs served by the cell are snapshotted; after a 2 s window the
delivered rate is computed,

```
throughput_kbps = (ΣRx_after − ΣRx_before) · 8 / 1000 / 2
```

and a `cell_service_outage` alarm is raised only if it falls below 5 kbps — i.e. the
alarm reflects a measured loss of user-plane service, not merely the trigger. X2 cuts
manifest as genuine `HandoverEndError` events. To keep large topologies tractable, X2
follows topology edges rather than a full mesh (O(E) vs O(n²)).

#### B.5 Live analytics

Computed in the browser from the session's alarm stream (`frontend/app.js`):

- **Propagation pathways:** observed `P(a→a') = count(a→a') / Σ count(a→·)`.
- **Node / NE resilience index** (0–100):
  `R = 100·(0.5·recovery + 0.3·min(median_gap / 72 h, 1) + 0.2·(1 − critical_rate))`,
  where `recovery` = fraction self-clearing to `Noalarm`. (Same definition as the
  historical analytic, applied to live counts.)
- **Intra-node timing:** distribution of gaps between consecutive alarms on a node, in
  ns-3 simulated seconds.
- **Cross-node propagation:** gaps between temporally-consecutive alarms on *different*
  nodes (global sim-time order) — a heuristic proxy for fault spread, not a verified
  causal link.

The same four analytics precomputed on the full dataset are available as the
**Historical** baseline.

#### B.6 Validity, assumptions & limitations

- **Physically grounded fault domains:** radio (RLF, handover failure, RRC timeout,
  random-access failure, abnormal release, SINR/interference) and transport (S1-U/X2
  link loss with measured outage). **Not modelled** (injection-only labels): hardware,
  DC power, ALD/antenna-line, licensing, environment — an LTE radio simulator has no
  such components to fail. This boundary is explicit and reported in the UI.
- **Scale:** ns-3 LTE is CPU-bound; live operation is practical to ~30 eNBs. Larger
  topologies load for design/visualisation/watcher analysis but stream sparsely; UE
  counts auto-scale and X2 is edge-based to keep them runnable.
- **Cross-node propagation** is a temporal-correlation proxy, not established causality.
- **Calibration provenance:** names/severities/transitions are dataset-conditioned;
  absolute alarm *rates* are governed by the ns-3 scenario (mobility, density,
  thresholds), not by the dataset's historical rates.

#### B.7 Reproducibility

ns-3 RNG streams are fixed (`AssignStreams`), so a given scenario + parameters is
deterministic up to host-timing in the wall-clock pacing layer (which affects display
cadence only, not the sequence of simulated events). All engine constants are the
literals in §A.8/§B.1–B.2. The dataset split is deterministic (`scripts/01_split_holdout.py`,
seed 42); the 2,000-row holdout is never analysed.

---

## Repository layout

```
scripts/                     Offline analytics pipeline (calibration from the dataset)
  01_split_holdout.py        Reserve a holdout test set (never analysed)
  02_analyze.py              Build calibration + analytics JSON from the dataset
ran-simulator/
  docker-compose.yml
  ns3-sim/                   ns-3 LTE simulation core (the heart of the system)
  backend/                   FastAPI alarm engine + SSE
  frontend/                  Browser UI
test_dataset.csv             Small sample of the alarm schema (used by the backend)
```

> **Data note.** The full alarm datasets (`RAN_data.csv`, `analysis_data.csv`) and the
> reserved `test_holdout.csv` are **not** committed — they exceed GitHub size limits and
> the holdout is deliberately kept out of analysis. The committed calibration artifacts
> (`ran-simulator/backend/data/*.json`) are derived from the analysis split.

---

## Topology editor & analysis

- **Realistic preset topologies** (`Load topology…`) spanning scales from a small-cell
  cluster (~15) → town → rural microwave backhaul → city metro → 5G dense urban → zone →
  region → national RAN (~210), each with proper core/aggregation/access tiers and
  resilience rings.
- **Node types** are rendered by shape + colour (core router, aggregation, eNodeB,
  gNodeB, GSM, microwave relay, small cell); a legend is shown on the canvas. Severity is
  the border ring, so role and health are both visible. Adding a node prompts for a
  **name and type**.
- **Node watcher** — select a node and click **👁 Watch** to place a watcher. The canvas
  focuses the node and its direct connections, and a live panel shows: what it is
  connected to, the alarms occurring on it, how alarms **propagate from it**
  (`alarm ▶ next_alarm` chains), and **cascades** to/from neighbours (correlated alarms
  flash along the links).
- **Network analytics** (`📊 Insights`) has two modes: **Live simulation** (default) —
  propagation pathways, node resilience, and intra/cross-node timing computed from the
  alarms the running ns-3 network is producing this session — and **Historical dataset** —
  the same analyses precomputed from the full BT alarm dataset, as a calibration baseline.

> **ns-3 scale note.** The ns-3 LTE engine is CPU-bound; live simulation is practical up
> to ~30 base stations. Larger topologies still load for design, node-type visualisation
> and watcher analysis, but the live alarm stream is sparse — **inject faults** to drive
> propagation. To keep large topologies feasible, X2 interfaces follow the topology edges
> (not a full mesh) and UE counts scale down with topology size.

---

## Running

```bash
cd ran-simulator
docker compose up --build
```

The first build compiles ns-3.41 with the LTE module and Python bindings (a one-time
~20–40 min cost, cached thereafter). Then open <http://localhost:3000>.

| Service   | URL                      |
|-----------|--------------------------|
| Frontend  | http://localhost:3000    |
| Backend   | http://localhost:8000    |
| ns3-sim   | http://localhost:5001    |

---

## Dataset schema

Generated alarm logs follow the source dataset columns:

```
Alarm Source, Name, Occurred On (NT), Severity, Location Information,
NE Type, Hours_since_prior, Hours_since_samealarm, Alarm_dow, Alarm_hour, Next_Alarm
```
