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
