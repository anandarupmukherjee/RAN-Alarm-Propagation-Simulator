"""
alarm_mapper.py
───────────────
Translates raw ns-3 network events into calibrated RAN alarm records,
using the real BT alarm dataset (test_dataset.csv) for:

  1.  Alarm name selection  (weighted by frequency in CSV)
  2.  Severity              (most common for that alarm in CSV)
  3.  NE type               (most common for that alarm in CSV)
  4.  Next-alarm prediction (Markov chain from the CSV Next_Alarm column)
  5.  Location information  (template filled from event metadata)
"""

import os
import json
import random
import pandas as pd
import numpy as np
from typing import Optional

# ─── Event type → candidate alarm names ──────────────────────────────────────
# Ordered by priority; the mapper picks from these weighted by CSV frequency.
EVENT_TO_ALARM_CANDIDATES: dict[str, list[str]] = {
    # ── Real ns-3 LTE trace events (the simulation core) ──────────────────────
    # These map genuine ns-3 radio-access events to the matching BT alarm names.
    "radio_link_failure":      ["Radio Link Failure", "Radio Signaling Link Disconnected",
                                 "Cell Unavailable"],
    "handover_failure":        ["Cell PS Service Faulty", "Radio Signaling Link Disconnected",
                                 "eNodeB S1 Control Plane Transmission Interruption"],
    "rrc_connection_timeout":  ["Cell PS Service Faulty", "Cell Unavailable"],
    "random_access_problem":   ["Cell PS Service Faulty",
                                 "Cell RX Channel Interference Noise Power Unbalanced"],
    "connection_release_abnormal": ["Cell PS Service Faulty", "Cell Unavailable"],
    # ── Transport events (real ns-3 S1-U / X2 link cuts) ──────────────────────
    "cell_service_outage":     ["Cell PS Service Faulty", "Cell Unavailable"],
    "backhaul_link_failure":   ["Ethernet Link Fault", "Remote Maintenance Link Failure",
                                 "IKE Negotiation Failure"],
    "s1_interface_failure":    ["S1 Interface Fault",
                                 "eNodeB S1 Control Plane Transmission Interruption"],
    "s1_control_plane":        ["eNodeB S1 Control Plane Transmission Interruption"],
    "sinr_drop":               ["Cell RX Channel Interference Noise Power Unbalanced"],
    "cell_interference":       ["Cell RX Channel Interference Noise Power Unbalanced",
                                 "RF Unit RX Channel RTWP/RSSI Too Low",
                                 "RF Unit RX Channel RTWP/RSSI Unbalanced"],
    "cell_blocked":            ["Cell Blocked"],
    "cell_unavailable":        ["Cell Unavailable"],
    "rf_degradation":          ["RF Unit Maintenance Link Failure",
                                 "RF Unit DC Input Power Failure",
                                 "RF Unit TX Channel Gain Out of Range"],
    "ald_current":             ["RF Unit ALD Current Out of Range"],
    "ald_link_failure":        ["ALD Maintenance Link Failure"],
    "oml_failure":             ["OML Fault"],
    "esl_failure":             ["ESL Link Fault"],
    "ike_failure":             ["IKE Negotiation Failure", "Remote Maintenance Link Failure"],
    "board_hardware":          ["Board Hardware Fault"],
    "gsm_cell_blocked":        ["GSM Cell Manually Blocked"],
    "gsm_cell_oos":            ["GSM Cell out of Service"],
    "radio_link_disconnected": ["Radio Signaling Link Disconnected"],
    "cpri_error":              ["BBU CPRI Interface Error"],
    "dc_power_failure":        ["Base Station DC Power Supply Abnormal",
                                 "RF Unit DC Input Power Failure"],
    "license_fault":           ["License on Trial"],
    "cell_ps_faulty":          ["Cell PS Service Faulty"],
    "manual_injection":        None,   # uses alarm_name directly from the event
}

# Location information templates
LOCATION_TEMPLATES: dict[str, str] = {
    "Ethernet Link Fault":
        "Cabinet No.=0, Subrack No.=0, Slot No.=7, Port No.=0, Specific Problem=Ethernet Link Fault",
    "S1 Interface Fault":
        "eNodeB Function Name={site}, S1 Interface ID=40001, CN Operator ID=1, "
        "Description=MME:MCC_234 MNC_32, Specific Problem=Lower-layer link fault",
    "eNodeB S1 Control Plane Transmission Interruption":
        "eNodeB Function Name={site}, CN Operator ID=0",
    "Cell Blocked":
        "eNodeB Function Name={site}, Local Cell ID={cell_id}, Cell FDD TDD indication=FDD, "
        "NB-IoT Cell Flag=FALSE, Cell Name={site}A11, eNodeB ID=31264, "
        "Cell ID={cell_id}, Specific Problem=Cell Blocked",
    "Cell Unavailable":
        "eNodeB Function Name={site}, Local Cell ID={cell_id}, Cell FDD TDD indication=FDD, "
        "NB-IoT Cell Flag=FALSE, Specific Problem=RF module abnormal",
    "Cell RX Channel Interference Noise Power Unbalanced":
        "eNodeB Function Name={site}, Local Cell ID={cell_id}, Cell FDD TDD indication=FDD, "
        "Max Interference noise power (0.1 dBm)={sinr_dbm_int}, eNodeB ID=31264",
    "OML Fault":
        "Site Index={site}, BSC Subrack No.=1, BSC Slot No.=20, E1 No.=NULL, "
        "Site Name={site}, Alarm Cause=Other Cause",
    "ESL Link Fault":
        "Site Index={site}, Site Name={site}",
    "RF Unit Maintenance Link Failure":
        "Cabinet No.=0, Subrack No.=60, Slot No.=0, Board Type=LRRU, Specific Problem=Other",
    "RF Unit ALD Current Out of Range":
        "Cabinet No.=0, Subrack No.=60, Slot No.=0, Antenna Port No.=ANT A, "
        "Board Type=LRRU, ALD Working Current (mA)={ald_ma}, Specific Problem={ald_prob}",
    "ALD Maintenance Link Failure":
        "Device No.={ald_no}, Device Type=TMA",
    "Board Hardware Fault":
        "Cabinet No.=0, Subrack No.=0, Slot No.=6, Board Type={board_type}, "
        "Site No.={site}, Site Name={site}",
    "GSM Cell Manually Blocked":
        "Site Index={site}, Cell Index={cell_id}, Block Type=LOCK, Site Name={site}",
    "GSM Cell out of Service":
        "Site Index={site}, Cell Index={cell_id}, Alarm Cause=Other causes, Site Name={site}",
    "IKE Negotiation Failure":
        "IKE Peer Name=IKE_DUMMY_NAME, Peer IP Address=10.219.202.195, "
        "Specific Problem=IKE Negotiation Failure",
    "BBU CPRI Interface Error":
        "Cabinet No.=0, Subrack No.=0, Slot No.=3, Port No.={port_no}, "
        "Sub Port No.=0, Board Type=UBBP, Specific Problem=CPRI Interface Reception Error",
    "Remote Maintenance Link Failure":
        "Cabinet No.=0, Subrack No.=0, Slot No.=7",
    "Radio Signaling Link Disconnected":
        "Cabinet No.=1, Subrack No.=4, Slot No.=0, Carrier No.=0, Site Name={site}",
}


class AlarmMapper:
    """
    Loads the CSV dataset and builds lookup tables for alarm names,
    severities, NE types, next-alarm transitions, and inter-alarm timing.
    """

    def __init__(self, csv_path: str, stats_path: Optional[str] = None):
        # Fast path: load precomputed full-dataset calibration tables (all real
        # alarm types) so we don't re-parse the 951k-row CSV at startup.
        if stats_path and os.path.exists(stats_path):
            self.df = None
            self._load_stats(stats_path)
            print(f"[mapper] Loaded calibration from {stats_path}; "
                  f"{len(self.alarm_severity)} unique alarm types "
                  f"(full dataset)", flush=True)
            return

        self.df = pd.read_csv(csv_path, encoding="utf-8-sig")
        self._build_alarm_stats()
        self._build_transitions()
        self._build_timing()
        self.source_count = int(self.df["Alarm Source"].nunique())
        print(f"[mapper] Loaded {len(self.df)} records; "
              f"{len(self.alarm_severity)} unique alarm types", flush=True)

    def _load_stats(self, stats_path: str):
        """Load calibration tables from a precomputed mapper_stats.json."""
        with open(stats_path) as f:
            s = json.load(f)
        self.alarm_frequency = {k: int(v) for k, v in s["alarm_frequency"].items()}
        self.alarm_severity  = s["alarm_severity"]
        self.alarm_ne_type   = s["alarm_ne_type"]
        self.transitions     = s["transitions"]
        self.timing          = s.get("timing", {})
        self.global_timing   = s.get("global_timing", [4.0]) or [4.0]
        self.source_count    = int(s.get("source_count", 0))

    def _build_alarm_stats(self):
        """Compute per-alarm frequency, severity, NE type from CSV."""
        self.alarm_frequency: dict[str, int] = (
            self.df["Name"].value_counts().to_dict()
        )
        self.alarm_severity: dict[str, str] = {}
        self.alarm_ne_type:  dict[str, str] = {}

        for alarm, grp in self.df.groupby("Name"):
            self.alarm_severity[alarm] = grp["Severity"].mode().iloc[0]
            self.alarm_ne_type[alarm]  = grp["NE Type"].mode().iloc[0]

    def _build_transitions(self):
        """Build Markov transition table: alarm_name → {next_alarm: probability}."""
        raw: dict[str, dict[str, int]] = {}
        for _, row in self.df.iterrows():
            src  = row["Name"]
            nxt  = row["Next_Alarm"]
            raw.setdefault(src, {})
            raw[src][nxt] = raw[src].get(nxt, 0) + 1

        self.transitions: dict[str, dict[str, float]] = {}
        for alarm, nexts in raw.items():
            total = sum(nexts.values())
            self.transitions[alarm] = {k: v / total for k, v in nexts.items()}

    def _build_timing(self):
        """Build per-alarm inter-arrival time samples (filtered)."""
        self.timing: dict[str, list[float]] = {}
        for alarm, grp in self.df.groupby("Name"):
            valid = grp["Hours_since_prior"]
            valid = valid[(valid > 0) & (valid < 720)]  # filter sentinels
            if len(valid) > 0:
                self.timing[alarm] = valid.tolist()

        # Global fallback
        all_valid = self.df["Hours_since_prior"]
        all_valid = all_valid[(all_valid > 0) & (all_valid < 720)]
        self.global_timing = all_valid.tolist() if len(all_valid) > 0 else [4.0]

    # ── Public API ─────────────────────────────────────────────────────────────
    def map_event(self, event: dict) -> Optional[dict]:
        """
        Convert a raw ns-3/statistical network event into a full alarm record.

        Returns None if the event type is unknown.
        """
        event_type = event.get("event_type", "")
        node_id    = event.get("node_id", "unknown")
        metadata   = event.get("metadata", {})
        sim_time   = event.get("sim_time", 0.0)
        sinr_dbm   = event.get("sinr_dbm", -75.0)

        # Resolve alarm name
        if event_type == "manual_injection":
            alarm_name = event.get("alarm_name", "Cell Blocked")
        elif event_type == "dataset_alarm":
            # Sample any real alarm type, weighted by its dataset frequency.
            alarm_name = self._pick_any_alarm()
        else:
            candidates = EVENT_TO_ALARM_CANDIDATES.get(event_type)
            if candidates is None:
                return None
            alarm_name = self._pick_alarm(candidates)

        # CSV-calibrated severity and NE type
        severity = self.alarm_severity.get(alarm_name, "Major")
        ne_type  = self.alarm_ne_type.get(alarm_name, "BTS3900 LTE")

        # Next alarm prediction (from Markov chain)
        next_alarm = self._sample_next(alarm_name)

        # Location information
        location = self._build_location(alarm_name, node_id, metadata, sinr_dbm)

        return {
            "alarm_name":  alarm_name,
            "alarm_source": node_id,
            "severity":    severity,
            "ne_type":     ne_type,
            "next_alarm":  next_alarm,
            "location":    location,
            "event_type":  event_type,
            "cascade_depth": event.get("cascade_depth", 0),
            "sinr_dbm":    round(sinr_dbm, 2),
            "sim_time":    round(sim_time, 4),
            "ns3":         event.get("ns3", False),
        }

    def _pick_alarm(self, candidates: list[str]) -> str:
        """Pick an alarm from candidates, weighted by CSV frequency."""
        weights = [self.alarm_frequency.get(c, 1) for c in candidates]
        return random.choices(candidates, weights=weights, k=1)[0]

    def _pick_any_alarm(self) -> str:
        """Pick any real alarm type, weighted by its dataset frequency."""
        if not hasattr(self, "_all_names"):
            self._all_names   = list(self.alarm_frequency.keys())
            self._all_weights = list(self.alarm_frequency.values())
        return random.choices(self._all_names, weights=self._all_weights, k=1)[0]

    def _sample_next(self, alarm_name: str) -> str:
        """Sample next alarm from the Markov transition matrix."""
        trans = self.transitions.get(alarm_name)
        if not trans:
            return "Noalarm"
        names = list(trans.keys())
        probs = list(trans.values())
        return random.choices(names, weights=probs, k=1)[0]

    def _build_location(self, alarm_name: str, site: str,
                         meta: dict, sinr_dbm: float) -> str:
        """Fill in the location information template."""
        template = LOCATION_TEMPLATES.get(alarm_name, "Site Name={site}")

        cell_id     = meta.get("cell_id",    random.randint(0, 12))
        board_type  = meta.get("board_type", "GTMU")
        ald_ma      = meta.get("ald_current_ma", 0)
        ald_prob    = meta.get("specific_problem", "Disconnection Protection")
        ald_no      = meta.get("device_no",  random.randint(1, 40))
        port_no     = meta.get("port_no",    random.randint(0, 3))
        sinr_int    = int(sinr_dbm * 10)  # in 0.1 dBm units

        try:
            return template.format(
                site       = site,
                cell_id    = cell_id,
                board_type = board_type,
                ald_ma     = ald_ma,
                ald_prob   = ald_prob,
                ald_no     = ald_no,
                port_no    = port_no,
                sinr_dbm_int = sinr_int,
            )
        except KeyError:
            return f"Site Name={site}"

    # ── Dataset helpers ────────────────────────────────────────────────────────
    def get_alarm_types(self) -> list[str]:
        return sorted(self.alarm_frequency.keys())

    # Fixed layout positions for the 10-site demo (canvas coords, x/y in pixels)
    DEMO_POSITIONS = {
        "10006": (350, 100), "10010": (600, 200), "10011": (860, 100),
        "10012": (600, 400), "10014": (880, 310), "10015": (600, 600),
        "10016": (860, 520), "10018": (600, 760), "10019": (250, 500),
        "10020": (300, 310),
    }

    def get_sites(self) -> list[str]:
        if self.df is not None:
            return sorted(self.df["Alarm Source"].astype(str).unique().tolist())
        # stats-only mode: the demo sites are all that topology building needs
        return sorted(self.DEMO_POSITIONS.keys())

    def get_transitions(self) -> dict:
        return self.transitions

    # Ordered keyword rules → functional category (first match wins).
    CATEGORY_RULES = [
        ("Transport & Backhaul", ["ethernet", "s1 ", "x2 ", "ip ", "ike", "remote maintenance",
                                   "user plane", "transmission", "backhaul", "route", "bfd",
                                   "sctp", "gtp", "vlan", "ospf", "interface fault", "link fault",
                                   "link failure", "link disconnected"]),
        ("Radio & RF",           ["rf unit", "rtwp", "rssi", "cell rx", "interference", "antenna",
                                   "ald", "tma", "radio", "rru", "vswr", "power amplifier", "carrier"]),
        ("Cell & Service",       ["cell", "service", "paging", "handover", "rrc", "admission",
                                   "gsm cell", "nb-iot"]),
        ("Hardware",             ["board", "hardware", "bbu", "cpri", "clock", "slot", "fan",
                                   "subrack", "cabinet", "optical", "sfp", "module"]),
        ("Power & Environment",  ["power", "dc ", "voltage", "battery", "surge", "temperature",
                                   "environment", "mains", "humidity", "smoke", "door"]),
        ("Security & Management", ["license", "access control", "certificate", "login",
                                    "authentication", "security", "password", "ntp", "oml", "esl"]),
    ]

    def _categorise(self, alarm_name: str) -> str:
        low = alarm_name.lower()
        for cat, kws in self.CATEGORY_RULES:
            if any(k in low for k in kws):
                return cat
        return "Other"

    def get_alarm_catalog(self) -> dict:
        """All real alarm types grouped into functional categories, each tagged
        with its dataset-calibrated severity and frequency."""
        cats: dict[str, list] = {}
        sev_totals: dict[str, int] = {}
        for name, freq in self.alarm_frequency.items():
            sev = self.alarm_severity.get(name, "Major")
            cat = self._categorise(name)
            cats.setdefault(cat, []).append(
                {"name": name, "severity": sev, "count": int(freq)})
            sev_totals[sev] = sev_totals.get(sev, 0) + 1

        ordered = [c for c, _ in self.CATEGORY_RULES] + ["Other"]
        categories = []
        for cat in ordered:
            if cat not in cats:
                continue
            alarms = sorted(cats[cat], key=lambda a: -a["count"])
            sev_breakdown: dict[str, int] = {}
            for a in alarms:
                sev_breakdown[a["severity"]] = sev_breakdown.get(a["severity"], 0) + 1
            categories.append({
                "name": cat, "count": len(alarms),
                "severity_breakdown": sev_breakdown, "alarms": alarms,
            })
        return {"categories": categories, "severity_totals": sev_totals}

    def get_demo_topology(self) -> dict:
        """Return a pre-built demo topology using the 10 real demo sites."""
        sites = sorted(self.DEMO_POSITIONS.keys())
        positions = self.DEMO_POSITIONS

        nodes = []
        for site in sites:
            x, y = positions.get(site, (400, 400))
            nodes.append({
                "id":      site,
                "site_id": site,
                "label":   site,
                "ne_type": "BTS3900 LTE",
                "x":       x,
                "y":       y,
            })

        edges = [
            {"source": "10006", "target": "10010"},
            {"source": "10010", "target": "10011"},
            {"source": "10010", "target": "10012"},
            {"source": "10006", "target": "10020"},
            {"source": "10020", "target": "10019"},
            {"source": "10011", "target": "10014"},
            {"source": "10014", "target": "10016"},
            {"source": "10012", "target": "10015"},
            {"source": "10015", "target": "10018"},
            {"source": "10015", "target": "10016"},
            {"source": "10012", "target": "10014"},
            {"source": "10019", "target": "10015"},
        ]

        return {"nodes": nodes, "edges": edges}
