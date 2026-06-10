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

1. **ns-3 core** simulates an LTE RAN: eNodeBs, UEs, EPC, RF propagation and mobility.
   Trace sources (radio-link failure, handover failure, connection establishment,
   SINR/RSRP thresholds, S1/X2 link state) are mapped to RAN alarms calibrated against
   the real dataset (alarm names, severities, NE types, Markov `Next_Alarm` transitions).
2. Events are published to **Redis**.
3. The **FastAPI backend** enriches each event into a full alarm record and streams it
   to clients over SSE.
4. The **frontend** renders the live topology, alarm console, severity dashboard, SINR
   heatmap, and a dataset-analytics modal. Users can edit topology, inject faults, and
   export the generated alarms as CSV in the original `RAN_data` schema.

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
