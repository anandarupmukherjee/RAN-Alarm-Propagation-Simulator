"""
redis_consumer.py
─────────────────
Background task that reads ns-3 events from Redis and converts them
into alarm records which are placed onto per-session asyncio queues
for SSE delivery.

Uses BLPOP so it blocks efficiently without busy-waiting.
"""

import asyncio
import json
import time
import redis.asyncio as aioredis
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from alarm_mapper import AlarmMapper

REDIS_KEY = "ns3:events"
BLOCK_TIMEOUT = 2   # seconds to block on BLPOP before looping


class RedisConsumer:
    """
    Singleton consumer that sits between Redis and all active SSE sessions.
    One instance is created at application startup.
    """

    def __init__(self, redis_url: str, mapper: "AlarmMapper"):
        self._redis_url = redis_url
        self._mapper    = mapper
        self._sessions: dict[str, asyncio.Queue] = {}   # session_id → Queue
        self._node_counts: dict[str, dict[str, int]] = {}  # session_id → {node_id: count}
        self._running = False
        self._task: asyncio.Task | None = None

    # ── Session management ────────────────────────────────────────────────────
    def register_session(self, session_id: str, queue: asyncio.Queue):
        self._sessions[session_id] = queue
        self._node_counts[session_id] = {}

    def unregister_session(self, session_id: str):
        self._sessions.pop(session_id, None)
        self._node_counts.pop(session_id, None)

    def get_node_counts(self, session_id: str) -> dict[str, int]:
        return self._node_counts.get(session_id, {})

    # ── Lifecycle ─────────────────────────────────────────────────────────────
    async def start(self):
        self._running = True
        self._task = asyncio.create_task(self._consume_loop())
        print("[consumer] Started Redis consumer task", flush=True)

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()

    # ── Main consume loop ─────────────────────────────────────────────────────
    async def _consume_loop(self):
        r = aioredis.from_url(self._redis_url, decode_responses=True)
        print(f"[consumer] Connected to Redis at {self._redis_url}", flush=True)

        while self._running:
            try:
                # BLPOP blocks up to BLOCK_TIMEOUT seconds
                result = await r.blpop(REDIS_KEY, timeout=BLOCK_TIMEOUT)
                if result is None:
                    continue   # timeout — loop again

                _, raw = result
                event = json.loads(raw)

            except asyncio.CancelledError:
                break
            except Exception as ex:
                print(f"[consumer] Redis error: {ex}", flush=True)
                await asyncio.sleep(1)
                continue

            # Map the raw event to an alarm record
            alarm = self._mapper.map_event(event)
            if alarm is None:
                continue

            # Build the SSE payload
            now_iso = datetime.now(timezone.utc).isoformat()
            node_id = alarm["alarm_source"]

            payload = {
                "type":         "alarm",
                "alarm_name":   alarm["alarm_name"],
                "alarm_source": node_id,
                "severity":     alarm["severity"],
                "ne_type":      alarm["ne_type"],
                "next_alarm":   alarm["next_alarm"],
                "location":     alarm["location"],
                "event_type":   alarm["event_type"],
                "cascade_depth":alarm["cascade_depth"],
                "sinr_dbm":     alarm["sinr_dbm"],
                "sim_time":     alarm["sim_time"],
                "timestamp":    now_iso,
                "ns3_backed":   alarm["ns3"],
            }

            # Broadcast to all active sessions
            for sid, q in list(self._sessions.items()):
                # Update per-session node alarm count
                counts = self._node_counts.setdefault(sid, {})
                counts[node_id] = counts.get(node_id, 0) + 1

                enriched = {**payload, "session_id": sid,
                             "node_alarm_count": counts[node_id]}
                try:
                    q.put_nowait(enriched)
                except asyncio.QueueFull:
                    pass  # drop if consumer is too slow

        await r.aclose()
        print("[consumer] Redis consumer stopped", flush=True)
