"""
main.py
───────
FastAPI application — the control plane and SSE delivery layer.

Endpoints:
  GET  /api/alarm-types               → all unique alarm names from CSV
  GET  /api/sites                     → all site IDs from CSV
  GET  /api/transitions               → Markov transition table
  GET  /api/demo-topology             → pre-built 10-site demo topology
  POST /api/simulate/start            → configure ns3-sim + start simulation
  DELETE /api/simulate/{id}           → stop simulation
  POST /api/simulate/{id}/inject      → inject a specific alarm on a node
  POST /api/simulate/{id}/link-failure→ inject a link failure between two nodes
  POST /api/simulate/{id}/speed       → update simulation speed
  GET  /api/simulate/{id}/stats       → alarm counts, sim time, ns3 status
  GET  /api/stream/{id}               → SSE alarm stream
"""

import os
import uuid
import asyncio
import json
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from alarm_mapper   import AlarmMapper
from redis_consumer import RedisConsumer

# ─── Config ───────────────────────────────────────────────────────────────────
CSV_PATH       = os.getenv("CSV_PATH",       "/data/test_dataset.csv")
STATS_PATH     = os.getenv("STATS_PATH",     "/app/data/mapper_stats.json")
ANALYTICS_PATH = os.getenv("ANALYTICS_PATH", "/app/data/analytics.json")
REDIS_URL      = os.getenv("REDIS_URL",      "redis://redis:6379")
NS3_URL        = os.getenv("NS3_URL",        "http://ns3-sim:5000")

# ─── Application ──────────────────────────────────────────────────────────────
app = FastAPI(title="RAN Alarm Simulator", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Singletons — initialised on startup
mapper:   Optional[AlarmMapper]   = None
consumer: Optional[RedisConsumer] = None

# Active simulation sessions: session_id → metadata dict
sessions: dict[str, dict] = {}


# ─── Lifecycle ────────────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    global mapper, consumer
    mapper   = AlarmMapper(CSV_PATH, stats_path=STATS_PATH)
    consumer = RedisConsumer(REDIS_URL, mapper)
    await consumer.start()
    print("[api] Startup complete", flush=True)


@app.on_event("shutdown")
async def shutdown():
    if consumer:
        await consumer.stop()


# ─── Pydantic models ──────────────────────────────────────────────────────────
class SimulateRequest(BaseModel):
    topology:          dict
    speed:             float = 60.0   # sim-hours per real-second
    propagation_prob:  float = 0.35

class InjectRequest(BaseModel):
    node_id:    str
    event_type: str = "cell_blocked"
    alarm_name: Optional[str] = None  # override for manual name injection

class LinkFailureRequest(BaseModel):
    source: str
    target: str

class SpeedRequest(BaseModel):
    speed: float


# ─── Dataset endpoints ────────────────────────────────────────────────────────
@app.get("/api/alarm-types")
def get_alarm_types():
    return {"alarm_types": mapper.get_alarm_types()}


@app.get("/api/sites")
def get_sites():
    return {"sites": mapper.get_sites()}


@app.get("/api/transitions")
def get_transitions():
    return {"transitions": mapper.get_transitions()}


@app.get("/api/alarm-catalog")
def get_alarm_catalog():
    """Real alarm types grouped into functional categories with severities."""
    return mapper.get_alarm_catalog()


# Precomputed insights from the full analysis dataset (951k rows, holdout excluded)
_analytics_cache: Optional[dict] = None

@app.get("/api/analytics")
def get_analytics():
    """Return precomputed network analytics: propagation pathways, node
    resilience, intra-node alarm timing, cross-node propagation timing."""
    global _analytics_cache
    if _analytics_cache is None:
        if not os.path.exists(ANALYTICS_PATH):
            raise HTTPException(status_code=404, detail="analytics not available")
        with open(ANALYTICS_PATH) as f:
            _analytics_cache = json.load(f)
    return _analytics_cache


@app.get("/api/demo-topology")
def get_demo_topology():
    return mapper.get_demo_topology()


# ─── ns3-sim proxy helpers ────────────────────────────────────────────────────
async def _ns3_post(path: str, body: dict) -> dict:
    """POST to ns3-sim service; return response dict or raise."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(f"{NS3_URL}{path}", json=body)
            r.raise_for_status()
            return r.json()
    except Exception as ex:
        print(f"[api] ns3-sim call failed ({path}): {ex}", flush=True)
        return {"error": str(ex)}


async def _ns3_get(path: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{NS3_URL}{path}")
            return r.json()
    except Exception as ex:
        return {"error": str(ex)}


# ─── Simulation control ───────────────────────────────────────────────────────
@app.post("/api/simulate/start")
async def start_simulation(req: SimulateRequest):
    # Configure and start the ns-3 simulation
    await _ns3_post("/configure", {
        "topology":          req.topology,
        "speed":             req.speed,
        "propagation_prob":  req.propagation_prob,
    })
    await _ns3_post("/start", {"speed": req.speed})

    # Create a new SSE session
    session_id = str(uuid.uuid4())
    q: asyncio.Queue = asyncio.Queue(maxsize=500)
    consumer.register_session(session_id, q)

    sessions[session_id] = {
        "topology":         req.topology,
        "speed":            req.speed,
        "propagation_prob": req.propagation_prob,
        "total_alarms":     0,
    }

    # Send a "started" status event
    await q.put({
        "type":         "status",
        "status":       "started",
        "session_id":   session_id,
        "node_count":   len(req.topology.get("nodes", [])),
        "ns3_url":      NS3_URL,
    })

    return {"session_id": session_id}


@app.delete("/api/simulate/{session_id}")
async def stop_simulation(session_id: str):
    if session_id not in sessions:
        raise HTTPException(status_code=404, detail="Session not found")

    await _ns3_post("/stop", {})
    consumer.unregister_session(session_id)

    q = consumer._sessions.get(session_id)
    if q:
        await q.put({"type": "status", "status": "stopped", "session_id": session_id})

    sessions.pop(session_id, None)
    return {"status": "stopped"}


@app.post("/api/simulate/{session_id}/inject")
async def inject_alarm(session_id: str, req: InjectRequest):
    if session_id not in sessions:
        raise HTTPException(status_code=404, detail="Session not found")

    body = {"node_id": req.node_id, "event_type": req.event_type}
    if req.alarm_name:
        body["alarm_name"] = req.alarm_name
    result = await _ns3_post("/inject", body)
    return {"status": "injected", **result}


@app.post("/api/simulate/{session_id}/link-failure")
async def inject_link_failure(session_id: str, req: LinkFailureRequest):
    if session_id not in sessions:
        raise HTTPException(status_code=404, detail="Session not found")
    result = await _ns3_post("/link-failure", {"source": req.source, "target": req.target})
    return {"status": "injected", **result}


@app.post("/api/simulate/{session_id}/speed")
async def update_speed(session_id: str, req: SpeedRequest):
    if session_id not in sessions:
        raise HTTPException(status_code=404, detail="Session not found")
    sessions[session_id]["speed"] = req.speed
    result = await _ns3_post("/speed", {"speed": req.speed})
    return {"status": "updated", "speed": req.speed}


@app.get("/api/simulate/{session_id}/stats")
async def get_stats(session_id: str):
    if session_id not in sessions:
        raise HTTPException(status_code=404, detail="Session not found")

    node_counts = consumer.get_node_counts(session_id)
    total = sum(node_counts.values())
    ns3_status = await _ns3_get("/status")

    return {
        "session_id":    session_id,
        "total_alarms":  total,
        "node_counts":   node_counts,
        "speed":         sessions[session_id].get("speed", 60),
        "ns3":           ns3_status,
    }


# ─── SSE stream ───────────────────────────────────────────────────────────────
@app.get("/api/stream/{session_id}")
async def stream_alarms(session_id: str):
    if session_id not in sessions:
        raise HTTPException(status_code=404, detail="Session not found")

    q = consumer._sessions.get(session_id)
    if q is None:
        raise HTTPException(status_code=404, detail="Session queue not found")

    async def event_generator():
        while True:
            try:
                event = await asyncio.wait_for(q.get(), timeout=30.0)
                yield f"data: {json.dumps(event)}\n\n"
                if (event.get("type") == "status"
                        and event.get("status") == "stopped"):
                    break
            except asyncio.TimeoutError:
                # Heartbeat to keep connection alive
                yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"
            except asyncio.CancelledError:
                break

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":       "no-cache",
            "X-Accel-Buffering":   "no",
            "Connection":          "keep-alive",
        },
    )


# ─── ns-3 status proxy ────────────────────────────────────────────────────────
@app.get("/api/ns3/status")
async def ns3_status():
    return await _ns3_get("/status")
